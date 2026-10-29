"""
Factor workflow with session control and evolution support.

Supports three round phases:
- Original: Initial exploration in each direction
- Mutation: Orthogonal exploration from parent trajectories
- Crossover: Hybrid strategies from multiple parents

Supports parallel execution within each phase when enabled.
"""

from typing import Any
from pathlib import Path
import fire
import signal
import sys
import threading
from multiprocessing import Process, Queue
from functools import wraps
import time
import ctypes
import os
import pickle
from quantaalpha.pipeline.settings import ALPHA_AGENT_FACTOR_PROP_SETTING
from quantaalpha.pipeline.planning import generate_parallel_directions
from quantaalpha.pipeline.planning import load_run_config
from quantaalpha.pipeline.loop import AlphaAgentLoop
from quantaalpha.pipeline.evolution import (
    EvolutionController, 
    EvolutionConfig,
    StrategyTrajectory,
    TrajectoryPool,
    RoundPhase,
)
from quantaalpha.core.exception import FactorEmptyError
from quantaalpha.log import logger
from quantaalpha.log.time import measure_time
from quantaalpha.llm.config import LLM_SETTINGS


def _configure_llm_runtime(llm_config: dict[str, Any] | None) -> dict[str, Any]:
    """Apply run-scoped LLM controls before workers are spawned."""
    llm_config = llm_config or {}
    mappings = {
        "json_mode_strict": (
            "structured_json_mode_strict",
            "STRUCTURED_JSON_MODE_STRICT",
            bool,
        ),
        "max_retries": ("max_retry", "MAX_RETRY", int),
        "retry_delay": ("retry_wait_seconds", "RETRY_WAIT_SECONDS", float),
        "factor_mining_timeout": (
            "factor_mining_timeout",
            "FACTOR_MINING_TIMEOUT",
            int,
        ),
    }
    applied: dict[str, Any] = {}
    for config_key, (setting_name, env_name, caster) in mappings.items():
        if config_key not in llm_config:
            continue
        value = caster(llm_config[config_key])
        setattr(LLM_SETTINGS, setting_name, value)
        if isinstance(value, bool):
            os.environ[env_name] = "true" if value else "false"
        else:
            os.environ[env_name] = str(value)
        applied[config_key] = value
    return applied


def _configure_run_log_path(run_config: dict[str, Any] | None) -> Path | None:
    """Bind a validation run to a deterministic trace directory when requested."""
    logging_config = (run_config or {}).get("logging") or {}
    configured = (
        os.getenv("QUANTAALPHA_LOG_TRACE_PATH")
        or logging_config.get("trace_path")
    )
    if not configured:
        return None
    trace_path = Path(str(configured)).expanduser().resolve()
    trace_path.mkdir(parents=True, exist_ok=True)
    logger.set_trace_path(trace_path)
    return trace_path


def _suppress_unsafe_rdagent_settings_logging() -> bool:
    """Prevent rdagent from serializing API credentials in its settings log."""
    try:
        from rdagent.oai.backend.litellm import LiteLLMAPIBackend
    except ImportError:
        return False
    LiteLLMAPIBackend._has_logged_settings = True
    return True


def _require_evaluated_trajectory(
    trajectory_data: dict[str, Any],
) -> dict[str, Any]:
    """Fail closed when a bounded evolution task ends without evaluated factors."""
    experiment = trajectory_data.get("experiment")
    factors = getattr(experiment, "sub_tasks", None) if experiment is not None else None
    result = getattr(experiment, "result", None) if experiment is not None else None
    if not factors or result is None:
        raise FactorEmptyError(
            "Evolution task ended without factors and a completed backtest result"
        )
    return trajectory_data


def _run_model_loop_to_completed_trajectory(
    model_loop: AlphaAgentLoop,
    *,
    step_n: int,
    stop_event: threading.Event | None,
    completion_retry_budget: int,
) -> dict[str, Any]:
    """Finish one bounded trajectory even when an aborted loop consumed steps."""
    retries_remaining = max(0, int(completion_retry_budget))
    next_step_n = int(step_n)
    while True:
        model_loop.run(step_n=next_step_n, stop_event=stop_event)
        trajectory_data = model_loop._get_trajectory_data()
        try:
            _require_evaluated_trajectory(trajectory_data)
            if trajectory_data.get("feedback") is None:
                raise FactorEmptyError(
                    "Evolution task ended without completed feedback"
                )
            return trajectory_data
        except FactorEmptyError:
            if retries_remaining <= 0:
                raise
            retries_remaining -= 1
            step_idx = int(getattr(model_loop, "step_idx", 0))
            next_step_n = len(model_loop.steps) - step_idx
            if next_step_n <= 0:
                next_step_n = len(model_loop.steps)
            logger.warning(
                "Completing an interrupted trajectory from step "
                f"{step_idx} with {next_step_n} bounded steps "
                f"({retries_remaining} retries remain)"
            )


def force_timeout():
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            # Prefer the timeout parameter
            seconds = LLM_SETTINGS.factor_mining_timeout

            if sys.platform != "win32":
                # Unix/Linux: Use SIGALRM signal
                def handle_timeout(signum, frame):
                    logger.error(f"Force terminating execution, exceeded {seconds} seconds")
                    sys.exit(1)

                signal.signal(signal.SIGALRM, handle_timeout)
                signal.alarm(seconds)

                try:
                    result = func(*args, **kwargs)
                finally:
                    signal.alarm(0)
                return result
            else:
                # Windows: Use daemon thread for timeout
                result_container = [None]
                exception_container = [None]

                def target():
                    try:
                        result_container[0] = func(*args, **kwargs)
                    except Exception as e:
                        exception_container[0] = e

                worker = threading.Thread(target=target, daemon=True)
                worker.start()
                worker.join(timeout=seconds)

                if worker.is_alive():
                    logger.error(f"Force terminating execution, exceeded {seconds} seconds")
                    os._exit(1)

                if exception_container[0] is not None:
                    raise exception_container[0]

                return result_container[0]
        return wrapper
    return decorator


def _run_branch(
    direction: str | None,
    step_n: int,
    use_local: bool,
    idx: int,
    log_root: str,
    log_prefix: str,
    quality_gate_cfg: dict = None,
    factor_cfg: dict = None,
):
    _suppress_unsafe_rdagent_settings_logging()
    if log_root:
        branch_name = f"{log_prefix}_{idx:02d}"
        branch_log = Path(log_root) / branch_name
        branch_log.mkdir(parents=True, exist_ok=True)
        logger.set_trace_path(branch_log)
    model_loop = AlphaAgentLoop(
        ALPHA_AGENT_FACTOR_PROP_SETTING,
        potential_direction=direction,
        stop_event=None,
        use_local=use_local,
        quality_gate_config=quality_gate_cfg or {},
        factor_config=factor_cfg or {},
    )
    model_loop.user_initial_direction = direction
    model_loop.run(step_n=step_n, stop_event=None)


def _run_evolution_task(
    task: dict[str, Any],
    directions: list[str],
    step_n: int,
    use_local: bool,
    user_direction: str | None,
    log_root: str,
    stop_event: threading.Event | None,
    quality_gate_cfg: dict[str, Any] | None = None,
    factor_cfg: dict[str, Any] | None = None,
    failed_loop_retry_budget: int = 0,
) -> dict[str, Any]:
    """
    Run a single evolution task (one small loop).

    Args:
        task: Evolution task descriptor
        directions: List of original directions
        step_n: Steps per round
        use_local: Use local backtest
        user_direction: User initial direction
        log_root: Log root directory
        stop_event: Stop event
        quality_gate_cfg: Quality gate config

    Returns:
        Dict containing trajectory data
    """
    if not _suppress_unsafe_rdagent_settings_logging():
        raise RuntimeError("Unable to activate rdagent credential-log suppression")

    phase = task["phase"]
    direction_id = task["direction_id"]
    strategy_suffix = task.get("strategy_suffix", "")
    round_idx = task["round_idx"]
    parent_trajectories = task.get("parent_trajectories", [])
    
    # A mutation must remain anchored to its evaluated parent, including after
    # a resumed run whose newly planned directions may differ.
    if phase == RoundPhase.ORIGINAL:
        direction = directions[direction_id] if direction_id < len(directions) else None
    elif phase == RoundPhase.MUTATION:
        direction = (
            parent_trajectories[0].hypothesis
            if parent_trajectories
            else directions[direction_id]
            if direction_id < len(directions)
            else None
        )
    else:  # CROSSOVER
        direction = None

    trajectory_id = StrategyTrajectory.generate_id(direction_id, round_idx, phase)
    parent_ids = [p.trajectory_id for p in parent_trajectories] or list(
        task.get("parent_trajectory_ids", [])
    )

    task_log_path = Path(logger.log_trace_path)
    if log_root:
        branch_name = f"{phase.value}_{round_idx:02d}_{direction_id:02d}"
        branch_log = Path(log_root) / branch_name
        branch_log.mkdir(parents=True, exist_ok=True)
        logger.set_trace_path(branch_log)
        task_log_path = branch_log

    logger.info(f"Starting evolution task: phase={phase.value}, round={round_idx}, direction={direction_id}")

    replay_spec = task.get("replay_spec") or {}
    target_step = replay_spec.get("target_step")
    target_index = (
        AlphaAgentLoop.steps.index(target_step)
        if target_step in AlphaAgentLoop.steps
        else 0
    )
    checkpoint = replay_spec.get("parent_checkpoint")
    if target_index > 0:
        if not isinstance(checkpoint, dict):
            raise ValueError("Localized replay is missing its parent checkpoint")
        model_loop = AlphaAgentLoop.load_for_replay(
            checkpoint["path"],
            checkpoint_sha256=checkpoint["sha256"],
            replay_spec=replay_spec,
            log_path=task_log_path,
            use_local=use_local,
            stop_event=stop_event,
            trajectory_id=trajectory_id,
            parent_trajectory_ids=parent_ids,
            direction_id=direction_id,
            round_idx=round_idx,
            quality_gate_config=quality_gate_cfg or {},
            factor_config=factor_cfg or {},
        )
    else:
        model_loop = AlphaAgentLoop(
            ALPHA_AGENT_FACTOR_PROP_SETTING,
            potential_direction=direction,
            stop_event=stop_event,
            use_local=use_local,
            strategy_suffix=strategy_suffix,
            evolution_phase=phase.value,
            trajectory_id=trajectory_id,
            parent_trajectory_ids=parent_ids,
            direction_id=direction_id,
            round_idx=round_idx,
            quality_gate_config=quality_gate_cfg or {},
            factor_config=factor_cfg or {},
        )
        model_loop.replay_spec = (
            task.get("evolution_spec") or replay_spec or {}
        )
    model_loop.user_initial_direction = user_direction
    
    # A localized mutation resumes at the selected action and runs its suffix once.
    replay_step_n = len(model_loop.steps) - target_index
    traj_data = _run_model_loop_to_completed_trajectory(
        model_loop,
        step_n=replay_step_n if replay_spec else step_n,
        stop_event=stop_event,
        completion_retry_budget=(
            0 if replay_spec else failed_loop_retry_budget
        ),
    )
    traj_data["task"] = task
    return traj_data


def _parallel_task_worker(
    task: dict[str, Any],
    directions: list[str],
    step_n: int,
    use_local: bool,
    user_direction: str | None,
    log_root: str,
    result_queue: Queue,
    task_idx: int,
    quality_gate_cfg: dict[str, Any] | None = None,
    factor_cfg: dict[str, Any] | None = None,
    failed_loop_retry_budget: int = 0,
):
    """
    Worker for parallel evolution tasks. Runs one evolution task in a separate process and puts result in queue.
    Args: task, directions, step_n, use_local, user_direction, log_root, result_queue, task_idx.
    """
    try:
        from quantaalpha.core.conf import RD_AGENT_SETTINGS
        RD_AGENT_SETTINGS.use_file_lock = False
        RD_AGENT_SETTINGS.pickle_cache_folder_path_str = str(
            Path(log_root) / f"pickle_cache_{task_idx}"
        )

        traj_data = _run_evolution_task(
            task=task,
            directions=directions,
            step_n=step_n,
            use_local=use_local,
            user_direction=user_direction,
            log_root=log_root,
            stop_event=None,
            quality_gate_cfg=quality_gate_cfg or {},
            factor_cfg=factor_cfg or {},
            failed_loop_retry_budget=failed_loop_retry_budget,
        )
        result_queue.put({
            "success": True,
            "task_idx": task_idx,
            "task": task,
            "traj_data": traj_data,
        })
    except Exception as e:
        import traceback
        result_queue.put({
            "success": False,
            "task_idx": task_idx,
            "task": task,
            "error": str(e),
            "traceback": traceback.format_exc(),
        })


def _serialize_task_for_parallel(task: dict[str, Any]) -> dict[str, Any]:
    """Serialize task for use in child process (parent_trajectories are complex objects)."""
    serialized = task.copy()
    
    # RoundPhase -> string
    if "phase" in serialized and isinstance(serialized["phase"], RoundPhase):
        serialized["phase"] = serialized["phase"]
    
    # Convert parent_trajectories to serializable info
    if "parent_trajectories" in serialized:
        serialized["parent_trajectory_ids"] = [
            p.trajectory_id for p in serialized.get("parent_trajectories", [])
        ]
        # Child process does not need full trajectory objects; strategy_suffix has required info
        serialized["parent_trajectories"] = []
    
    return serialized


def _run_tasks_parallel(
    tasks: list[dict[str, Any]],
    directions: list[str],
    step_n: int,
    use_local: bool,
    user_direction: str | None,
    log_root: str,
    quality_gate_cfg: dict[str, Any] | None = None,
    factor_cfg: dict[str, Any] | None = None,
    failed_loop_retry_budget: int = 0,
) -> list[dict[str, Any]]:
    """
    Run multiple evolution tasks in parallel.
    Returns list of results, each with task and traj_data.
    """
    if not tasks:
        return []
    
    result_queue = Queue()
    processes = []
    
    logger.info(f"Starting {len(tasks)} parallel evolution tasks")

    for idx, task in enumerate(tasks):
        serialized_task = _serialize_task_for_parallel(task)
        
        p = Process(
            target=_parallel_task_worker,
            args=(
                serialized_task,
                directions,
                step_n,
                use_local,
                user_direction,
                log_root,
                result_queue,
                idx,
                quality_gate_cfg or {},
                factor_cfg or {},
                failed_loop_retry_budget,
            ),
        )
        p.start()
        processes.append(p)
        logger.info(f"Started task {idx}: phase={task['phase'].value}, direction={task['direction_id']}")

    results = []
    for _ in range(len(tasks)):
        result = result_queue.get()
        if result["success"]:
            original_task = tasks[result["task_idx"]]
            result["task"] = original_task
            result["traj_data"]["task"] = original_task
            results.append(result)
            logger.info(f"Task {result['task_idx']} completed")
        else:
            logger.error(f"Task {result['task_idx']} failed: {result['error']}")
            logger.error(result.get('traceback', ''))

    for p in processes:
        p.join()

    logger.info(f"Parallel tasks done: {len(results)}/{len(tasks)} succeeded")
    
    return results


def _load_resume_directions(
    pool_save_path: Path,
    expected_count: int,
) -> list[str] | None:
    """Reuse completed original hypotheses instead of replanning on resume."""
    if not pool_save_path.exists():
        return None
    pool = TrajectoryPool(save_path=pool_save_path, fresh_start=False)
    originals = {
        trajectory.direction_id: trajectory.hypothesis
        for trajectory in pool.get_by_phase(RoundPhase.ORIGINAL)
        if trajectory.round_idx == 0
        and trajectory.is_successful()
        and trajectory.hypothesis
    }
    expected_ids = set(range(expected_count))
    if set(originals) != expected_ids:
        return None
    return [originals[direction_id] for direction_id in range(expected_count)]


def run_evolution_loop(
    initial_direction: str | None,
    evolution_cfg: dict[str, Any],
    exec_cfg: dict[str, Any],
    planning_cfg: dict[str, Any],
    stop_event: threading.Event | None = None,
    quality_gate_cfg: dict[str, Any] | None = None,
    factor_cfg: dict[str, Any] | None = None,
):
    """
    Run evolution loop: Original -> Mutation -> Crossover -> Mutation -> ...
    Supports parallel execution per phase.
    """
    quality_gate_cfg = quality_gate_cfg or {}
    factor_cfg = factor_cfg or {}
    from quantaalpha.core.conf import RD_AGENT_SETTINGS
    RD_AGENT_SETTINGS.use_file_lock = False
    logger.info("Evolution mode: file lock disabled to avoid deadlock")

    # Parse config
    num_directions = int(planning_cfg.get("num_directions", 2))
    max_rounds = int(evolution_cfg.get("max_rounds", 10))
    crossover_size = int(evolution_cfg.get("crossover_size", 2))
    crossover_n = int(evolution_cfg.get("crossover_n", 3))
    steps_per_loop = int(exec_cfg.get("steps_per_loop", 5))
    max_loops = int(exec_cfg.get("max_loops", 1))
    configured_step_n = exec_cfg.get("step_n")
    step_n = int(configured_step_n) if configured_step_n is not None else max_loops * steps_per_loop
    failed_loop_retry_budget = max(
        0,
        int(exec_cfg.get("failed_loop_retry_budget", 0) or 0),
    )
    use_local = bool(exec_cfg.get("use_local", True))
    
    mutation_enabled = bool(evolution_cfg.get("mutation_enabled", True))
    crossover_enabled = bool(evolution_cfg.get("crossover_enabled", True))
    parent_selection_strategy = str(evolution_cfg.get("parent_selection_strategy", "best"))
    top_percent_threshold = float(evolution_cfg.get("top_percent_threshold", 0.3))
    log_root = str(logger.log_trace_path)
    parallel_enabled = bool(evolution_cfg.get("parallel_enabled", False))
    fresh_start = bool(evolution_cfg.get("fresh_start", True))
    fresh_start_override = os.getenv("QUANTAALPHA_EVOLUTION_FRESH_START")
    if fresh_start_override is not None:
        normalized = fresh_start_override.strip().lower()
        if normalized not in {"0", "1", "false", "true"}:
            raise ValueError(
                "QUANTAALPHA_EVOLUTION_FRESH_START must be one of "
                "0, 1, false, or true"
            )
        fresh_start = normalized in {"1", "true"}
    cleanup_on_finish = bool(evolution_cfg.get("cleanup_on_finish", False))
    pool_save_path = Path(log_root) / "trajectory_pool.json"

    # Generate initial directions
    planning_enabled = bool(planning_cfg.get("enabled", False))
    prompt_file = planning_cfg.get("prompt_file") or "planning_prompts.yaml"
    prompt_path = Path(__file__).parent / "prompts" / str(prompt_file)

    resume_directions = (
        _load_resume_directions(pool_save_path, num_directions)
        if not fresh_start
        else None
    )
    if resume_directions is not None:
        directions = resume_directions
        logger.info("Reused completed original directions for resumed evolution")
    elif planning_enabled and initial_direction:
        directions = generate_parallel_directions(
            initial_direction=initial_direction,
            n=num_directions,
            prompt_file=prompt_path,
            max_attempts=int(planning_cfg.get("max_attempts", 5)),
            use_llm=bool(planning_cfg.get("use_llm", True)),
            allow_fallback=bool(planning_cfg.get("allow_fallback", True)),
        )
    elif planning_enabled:
        directions = [None] * num_directions
    else:
        directions = [initial_direction] if initial_direction else [None]

    logger.info(f"Generated {len(directions)} exploration directions")
    for i, d in enumerate(directions):
        logger.info(f"  Direction {i}: {d}")

    mutation_prompt_path = Path(__file__).parent / "prompts" / "evolution_prompts.yaml"
    
    logger.info(f"Trajectory pool path: {pool_save_path} (fresh_start={fresh_start})")

    config = EvolutionConfig(
        num_directions=len(directions),
        steps_per_loop=steps_per_loop,
        max_rounds=max_rounds,
        mutation_enabled=mutation_enabled,
        crossover_enabled=crossover_enabled,
        crossover_size=crossover_size,
        crossover_n=crossover_n,
        prefer_diverse_crossover=True,
        parent_selection_strategy=parent_selection_strategy,
        top_percent_threshold=top_percent_threshold,
        parallel_enabled=parallel_enabled,
        paper_fidelity_mode=bool(
            evolution_cfg.get("paper_fidelity_mode", False)
        ),
        pool_save_path=str(pool_save_path),
        mutation_prompt_path=str(mutation_prompt_path) if mutation_prompt_path.exists() else None,
        crossover_prompt_path=str(mutation_prompt_path) if mutation_prompt_path.exists() else None,
        fresh_start=fresh_start,
    )

    controller = EvolutionController(config)
    state_path = Path(log_root) / "evolution_state.json"
    if not fresh_start:
        if state_path.exists():
            controller.load_state(state_path)
        else:
            controller.restore_original_progress_from_pool()

    logger.info("="*60)
    logger.info("Starting evolution loop")
    logger.info(f"Config: directions={len(directions)}, max_rounds={max_rounds}, "
               f"crossover_size={crossover_size}, crossover_n={crossover_n}")
    logger.info(f"Phases: mutation={'on' if mutation_enabled else 'off'}, "
               f"crossover={'on' if crossover_enabled else 'off'}")
    if mutation_enabled and not crossover_enabled:
        logger.info("Mode: mutation only (Original -> Mutation -> ...)")
    elif crossover_enabled and not mutation_enabled:
        logger.info("Mode: crossover only (Original -> Crossover -> ...)")
    elif mutation_enabled and crossover_enabled:
        logger.info("Mode: full evolution (Original -> Mutation -> Crossover -> ...)")
    else:
        logger.info("Mode: original only (no evolution)")
    logger.info(f"Parent selection: {parent_selection_strategy}" +
               (f" (top_percent={top_percent_threshold})" if parent_selection_strategy == "top_percent_plus_random" else ""))
    logger.info(f"Parallel execution: {'on' if parallel_enabled else 'off'}")
    logger.info(
        "Task steps: "
        f"max_loops={max_loops}, steps_per_loop={steps_per_loop}, "
        f"step_n={step_n}, failed_loop_retry_budget={failed_loop_retry_budget}"
    )
    logger.info(f"Factors per hypothesis: {int(factor_cfg.get('factors_per_hypothesis', 1) or 1)}")
    logger.info("="*60)

    if parallel_enabled:
        while not controller.is_complete():
            if stop_event and stop_event.is_set():
                logger.info("Stop signal received, ending evolution loop")
                break

            tasks = controller.get_all_tasks_for_current_phase()
            if not tasks:
                logger.info("Evolution complete: no more tasks")
                break

            current_phase = tasks[0]["phase"]
            current_round = tasks[0]["round_idx"]
            logger.info(f"Parallel phase: phase={current_phase.value}, round={current_round}, tasks={len(tasks)}")

            results = _run_tasks_parallel(
                tasks=tasks,
                directions=directions,
                step_n=step_n,
                use_local=use_local,
                user_direction=initial_direction,
                log_root=log_root,
                quality_gate_cfg=quality_gate_cfg,
                factor_cfg=factor_cfg,
                failed_loop_retry_budget=failed_loop_retry_budget,
            )
            if (
                config.paper_fidelity_mode
                and len(results) != len(tasks)
            ):
                raise RuntimeError(
                    "Paper-fidelity phase failed closed: "
                    f"{len(results)}/{len(tasks)} tasks completed"
                )
            
            completed_tasks = []
            for result in results:
                if result["success"]:
                    task = result["task"]
                    traj_data = result["traj_data"]
                    trajectory = controller.create_trajectory_from_loop_result(
                        task=task,
                        hypothesis=traj_data.get("hypothesis"),
                        experiment=traj_data.get("experiment"),
                        feedback=traj_data.get("feedback"),
                        step_outputs=traj_data.get("step_outputs"),
                        step_checkpoints=traj_data.get("step_checkpoints"),
                        evolution_spec=traj_data.get("evolution_spec"),
                        trajectory_id=traj_data.get("trajectory_id"),
                    )
                    controller.report_task_complete(task, trajectory)
                    controller.save_state(state_path)
                    completed_tasks.append(task)
                    logger.info(f"Trajectory done: {trajectory.trajectory_id}, RankIC={trajectory.get_primary_metric()}")

            controller.advance_phase_after_parallel_completion(completed_tasks)
            controller.save_state(state_path)

    else:
        while not controller.is_complete():
            if stop_event and stop_event.is_set():
                logger.info("Stop signal received, ending evolution loop")
                break

            task = controller.get_next_task()
            if task is None:
                logger.info("Evolution complete: no more tasks")
                break

            logger.info(f"Running task: phase={task['phase'].value}, round={task['round_idx']}, direction={task['direction_id']}")

            try:
                traj_data = _run_evolution_task(
                    task=task,
                    directions=directions,
                    step_n=step_n,
                    use_local=use_local,
                    user_direction=initial_direction,
                    log_root=log_root,
                    stop_event=stop_event,
                    quality_gate_cfg=quality_gate_cfg,
                    factor_cfg=factor_cfg,
                    failed_loop_retry_budget=failed_loop_retry_budget,
                )
                trajectory = controller.create_trajectory_from_loop_result(
                    task=task,
                    hypothesis=traj_data.get("hypothesis"),
                    experiment=traj_data.get("experiment"),
                    feedback=traj_data.get("feedback"),
                    step_outputs=traj_data.get("step_outputs"),
                    step_checkpoints=traj_data.get("step_checkpoints"),
                    evolution_spec=traj_data.get("evolution_spec"),
                    trajectory_id=traj_data.get("trajectory_id"),
                )
                controller.report_task_complete(task, trajectory)
                controller.save_state(state_path)
                logger.info(f"Task done: trajectory_id={trajectory.trajectory_id}, RankIC={trajectory.get_primary_metric()}")
            except Exception as e:
                logger.error(f"Task failed: {e}")
                import traceback
                logger.error(traceback.format_exc())
                if config.paper_fidelity_mode:
                    raise
                continue

    controller.save_state(state_path)
    best_trajs = controller.get_best_trajectories(top_n=5)
    logger.info("="*60)
    logger.info(f"Evolution complete. Top {len(best_trajs)} trajectories:")
    for i, t in enumerate(best_trajs):
        metric = t.get_primary_metric()
        metric_str = f"{metric:.4f}" if metric is not None else "N/A"
        logger.info(f"  {i+1}. {t.trajectory_id}: phase={t.phase.value}, RankIC={metric_str}")
    logger.info(f"Pool stats: {controller.pool.get_statistics()}")
    logger.info("="*60)
    if cleanup_on_finish:
        logger.info("Cleaning up trajectory pool file...")
        controller.pool.cleanup_file()


@force_timeout()
def main(path=None, step_n=100, direction=None, stop_event=None, config_path=None, evolution_mode=None):
    """
    Autonomous alpha factor mining with optional evolution support.

    Args:
        path: Session path (for resume)
        step_n: Number of steps (default 100 = 20 loops * 5 steps/loop)
        direction: Initial direction
        stop_event: Stop event
        config_path: Run config file path
        evolution_mode: Enable evolution (None=from config, True/False=override)

    Evolution flow: Original -> Mutation -> Crossover -> Mutation -> ...

    You can continue running session by

    .. code-block:: python

        quantaalpha mine --direction "[Initial Direction]" --config_path configs/experiment.yaml

    """
    try:
        from quantaalpha.core.conf import RD_AGENT_SETTINGS
        logger.info("="*60)
        logger.info("Experiment config")
        logger.info(f"  Workspace: {RD_AGENT_SETTINGS.workspace_path}")
        logger.info(f"  Cache dir: {RD_AGENT_SETTINGS.pickle_cache_folder_path_str}")
        logger.info(f"  Cache enabled: {RD_AGENT_SETTINGS.cache_with_pickle}")
        logger.info("="*60)

        # Config file default: project_root/configs/
        _project_root = Path(__file__).resolve().parents[2]
        config_default = _project_root / "configs" / "experiment.yaml"
        config_file = Path(config_path) if config_path else config_default
        run_cfg = load_run_config(config_file)
        configured_log_path = _configure_run_log_path(run_cfg)
        llm_runtime = _configure_llm_runtime(
            (run_cfg.get("llm") or {}) if isinstance(run_cfg, dict) else {}
        )
        planning_cfg = (run_cfg.get("planning") or {}) if isinstance(run_cfg, dict) else {}
        exec_cfg = (run_cfg.get("execution") or {}) if isinstance(run_cfg, dict) else {}
        evolution_cfg = (run_cfg.get("evolution") or {}) if isinstance(run_cfg, dict) else {}
        quality_gate_cfg = (run_cfg.get("quality_gate") or {}) if isinstance(run_cfg, dict) else {}
        factor_cfg = (run_cfg.get("factor") or {}) if isinstance(run_cfg, dict) else {}
        if configured_log_path is not None:
            logger.info(f"Trace path override: {configured_log_path}")
        if llm_runtime:
            logger.info(f"Run-scoped LLM controls: {llm_runtime}")

        if evolution_mode is not None:
            use_evolution = evolution_mode
        else:
            use_evolution = bool(evolution_cfg.get("enabled", False))

        if step_n is None or step_n == 100:
            if exec_cfg.get("step_n") is not None:
                step_n = exec_cfg.get("step_n")
            else:
                max_loops = int(exec_cfg.get("max_loops", 10))
                steps_per_loop = int(exec_cfg.get("steps_per_loop", 5))
                step_n = max_loops * steps_per_loop

        use_local = os.getenv("USE_LOCAL", "True").lower()
        use_local = True if use_local in ["true", "1"] else False
        if exec_cfg.get("use_local") is not None:
            use_local = bool(exec_cfg.get("use_local"))
        exec_cfg["use_local"] = use_local
        
        logger.info(f"Use {'Local' if use_local else 'Docker container'} to execute factor backtest")
        
        if use_evolution and path is None:
            logger.info("="*60)
            logger.info("Evolution mode: Original -> Mutation -> Crossover loop")
            logger.info("="*60)
            
            run_evolution_loop(
                initial_direction=direction,
                evolution_cfg=evolution_cfg,
                exec_cfg=exec_cfg,
                planning_cfg=planning_cfg,
                stop_event=stop_event,
                quality_gate_cfg=quality_gate_cfg,
                factor_cfg=factor_cfg,
            )
        
        elif path is None:
            planning_enabled = bool(planning_cfg.get("enabled", False))
            n_dirs = int(planning_cfg.get("num_directions", 1))
            max_attempts = int(planning_cfg.get("max_attempts", 5))
            use_llm = bool(planning_cfg.get("use_llm", True))
            allow_fallback = bool(planning_cfg.get("allow_fallback", True))
            prompt_file = planning_cfg.get("prompt_file") or "planning_prompts.yaml"
            prompt_path = Path(__file__).parent / "prompts" / str(prompt_file)
            if planning_enabled and direction:
                directions = generate_parallel_directions(
                    initial_direction=direction,
                    n=n_dirs,
                    prompt_file=prompt_path,
                    max_attempts=max_attempts,
                    use_llm=use_llm,
                    allow_fallback=allow_fallback,
                )
            else:
                directions = [direction] if direction else [None]

            log_root = exec_cfg.get("branch_log_root") or "log"
            log_prefix = exec_cfg.get("branch_log_prefix") or "branch"
            use_branch_logs = planning_enabled and len(directions) > 1
            parallel_execution = bool(exec_cfg.get("parallel_execution", False))

            if parallel_execution and len(directions) > 1:
                procs: list[Process] = []
                for idx, dir_text in enumerate(directions, start=1):
                    if dir_text:
                        logger.info(f"[Planning] Branch {idx}/{len(directions)} direction: {dir_text}")
                    p = Process(
                        target=_run_branch,
                        args=(dir_text, step_n, use_local, idx, log_root if use_branch_logs else "", log_prefix, quality_gate_cfg, factor_cfg),
                    )
                    p.start()
                    procs.append(p)
                for p in procs:
                    p.join()
            else:
                for idx, dir_text in enumerate(directions, start=1):
                    if dir_text:
                        logger.info(f"[Planning] Branch {idx}/{len(directions)} direction: {dir_text}")
                    if use_branch_logs:
                        branch_name = f"{log_prefix}_{idx:02d}"
                        branch_log = Path(log_root) / branch_name
                        branch_log.mkdir(parents=True, exist_ok=True)
                        logger.set_trace_path(branch_log)
                    model_loop = AlphaAgentLoop(
                        ALPHA_AGENT_FACTOR_PROP_SETTING,
                        potential_direction=dir_text,
                        stop_event=stop_event,
                        use_local=use_local,
                        quality_gate_config=quality_gate_cfg,
                        factor_config=factor_cfg,
                    )
                    model_loop.user_initial_direction = direction
                    model_loop.run(step_n=step_n, stop_event=stop_event)
        else:
            model_loop = AlphaAgentLoop.load(path, use_local=use_local)
            model_loop.run(step_n=step_n, stop_event=stop_event)
    except Exception as e:
        logger.error(f"Error during execution: {str(e)}")
        raise
    finally:
        logger.info("Run finished or terminated")

if __name__ == "__main__":
    fire.Fire(main)
