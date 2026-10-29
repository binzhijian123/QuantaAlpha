"""
Model workflow with session control.
"""

import time
import pandas as pd
from typing import Any
import hashlib

from quantaalpha.pipeline.settings import BaseFacSetting
from quantaalpha.core.developer import Developer
from quantaalpha.core.proposal import (
    Hypothesis2Experiment,
    HypothesisExperiment2Feedback,
    HypothesisGen,  
    Trace,
)
from quantaalpha.core.scenario import Scenario
from quantaalpha.core.utils import import_class
from quantaalpha.log import logger
from quantaalpha.log.time import measure_time
from quantaalpha.utils.workflow import LoopBase, LoopMeta
from quantaalpha.core.exception import FactorEmptyError
import threading


import datetime
import pickle
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from tqdm.auto import tqdm

from quantaalpha.core.exception import CoderError
from quantaalpha.log import logger
from functools import wraps

# Decorator: check stop_event before invoking the function

def stop_event_check(func):
    @wraps(func)
    def wrapper(self, *args, **kwargs):
        if STOP_EVENT is not None and STOP_EVENT.is_set():
            raise Exception("Operation stopped due to stop_event flag.")
        return func(self, *args, **kwargs)
    return wrapper


class AlphaAgentLoop(LoopBase, metaclass=LoopMeta):
    skip_loop_error = (FactorEmptyError,)
    
    @measure_time
    def __init__(
        self, 
        PROP_SETTING: BaseFacSetting, 
        potential_direction, 
        stop_event: threading.Event, 
        use_local: bool = True,
        strategy_suffix: str = "",
        evolution_phase: str = "original",
        trajectory_id: str = "",
        parent_trajectory_ids: list = None,
        direction_id: int = 0,
        round_idx: int = 0,
        quality_gate_config: dict = None,
        factor_config: dict = None,
    ):
        with logger.tag("init"):
            self.use_local = use_local
            # Store initial direction for factor provenance
            self.potential_direction = potential_direction

            # Evolution-related attributes
            self.strategy_suffix = strategy_suffix
            self.evolution_phase = evolution_phase  # original / mutation / crossover
            self.trajectory_id = trajectory_id
            self.parent_trajectory_ids = parent_trajectory_ids or []
            self.direction_id = direction_id
            self.round_idx = round_idx  # 0=original, 1=mutation, 2=crossover, ...

            # Quality gate config
            self.quality_gate_config = quality_gate_config or {}
            self.factor_config = factor_config or {}
            self.factors_per_hypothesis = int(self.factor_config.get("factors_per_hypothesis", 1) or 1)
            self.factor_generation_max_attempts = max(
                1,
                int(self.factor_config.get("generation_max_attempts", 5) or 5),
            )

            # For trajectory collection
            self._last_hypothesis = None
            self._last_experiment = None
            self._last_feedback = None
            self._trajectory_step_outputs: dict[str, Any] = {}
            self.replay_spec: dict[str, Any] = {}
            
            logger.info(f"Initialized AlphaAgentLoop, backtest in {'local' if use_local else 'Docker'}")
            if potential_direction:
                logger.info(f"Initial direction: {potential_direction}")
            if evolution_phase != "original":
                logger.info(f"Evolution phase: {evolution_phase}, round: {round_idx}, trajectory_id: {trajectory_id}")

            consistency_enabled = self.quality_gate_config.get("consistency_enabled", False)
            complexity_enabled = self.quality_gate_config.get("complexity_enabled", True)
            redundancy_enabled = self.quality_gate_config.get("redundancy_enabled", True)
            complexity_config = self.factor_config.get("complexity") or {}
            duplication_config = self.factor_config.get("duplication") or {}
            logger.info(f"Quality gate: consistency={'on' if consistency_enabled else 'off'}, "
                       f"complexity={'on' if complexity_enabled else 'off'}, "
                       f"redundancy={'on' if redundancy_enabled else 'off'}")
            logger.info(f"Factors per hypothesis: {self.factors_per_hypothesis}")
            logger.info(f"Factor generation max attempts: {self.factor_generation_max_attempts}")
            logger.info(
                "Factor constraints: "
                f"symbol_length={complexity_config.get('symbol_length_threshold', 'runtime-default')}, "
                f"base_features={complexity_config.get('base_features_threshold', 'runtime-default')}, "
                f"free_args_ratio={complexity_config.get('free_args_ratio_threshold', 'runtime-default')}, "
                f"unique_vars_ratio={complexity_config.get('unique_vars_ratio_threshold', 'runtime-default')}, "
                f"duplication={duplication_config.get('threshold', 'runtime-default')}"
            )
                
            scen: Scenario = import_class(PROP_SETTING.scen)(use_local=use_local)
            logger.log_object(scen, tag="scenario")

            # If strategy suffix is set, append it to the direction
            effective_direction = potential_direction
            if strategy_suffix:
                effective_direction = (potential_direction or "") + "\n" + strategy_suffix
            
            self.hypothesis_generator: HypothesisGen = import_class(PROP_SETTING.hypothesis_gen)(scen, effective_direction)
            logger.log_object(self.hypothesis_generator, tag="hypothesis generator")

            # Pass consistency check config into factor constructor
            self.factor_constructor: Hypothesis2Experiment = import_class(PROP_SETTING.hypothesis2experiment)(
                consistency_enabled=consistency_enabled,
                complexity_enabled=complexity_enabled,
                redundancy_enabled=redundancy_enabled,
                factors_per_hypothesis=self.factors_per_hypothesis,
                generation_max_attempts=self.factor_generation_max_attempts,
                require_exact_factor_count=bool(
                    self.factor_config.get(
                        "require_exact_factor_count",
                        False,
                    )
                ),
                variant_max_attempts=int(
                    self.factor_config.get("variant_max_attempts", 2) or 2
                ),
                variant_contract=(
                    self.factor_config.get("variant_contract") or {}
                ),
                consistency_strict_mode=bool(
                    self.quality_gate_config.get("consistency_strict_mode", False)
                ),
                consistency_require_pass=bool(
                    self.quality_gate_config.get("consistency_require_pass", False)
                ),
                quality_gate_fail_closed_on_error=bool(
                    self.quality_gate_config.get(
                        "fail_closed_on_error",
                        False,
                    )
                ),
                max_correction_attempts=int(
                    self.quality_gate_config.get("max_correction_attempts", 3) or 3
                ),
                symbol_length_threshold=complexity_config.get("symbol_length_threshold"),
                base_features_threshold=complexity_config.get("base_features_threshold"),
                free_args_ratio_threshold=complexity_config.get("free_args_ratio_threshold"),
                unique_vars_ratio_threshold=complexity_config.get("unique_vars_ratio_threshold"),
                duplication_threshold=duplication_config.get("threshold"),
                factor_zoo_path=duplication_config.get("factor_zoo_path"),
            )
            self.factor_constructor.evolution_instruction = ""
            logger.log_object(self.factor_constructor, tag="experiment generation")

            self.coder: Developer = import_class(PROP_SETTING.coder)(scen)
            logger.log_object(self.coder, tag="coder")
            
            self.runner: Developer = import_class(PROP_SETTING.runner)(scen)
            logger.log_object(self.runner, tag="runner")

            self.summarizer: HypothesisExperiment2Feedback = import_class(PROP_SETTING.summarizer)(scen)
            logger.log_object(self.summarizer, tag="summarizer")
            self.trace = Trace(scen=scen)
            
            global STOP_EVENT
            STOP_EVENT = stop_event
            super().__init__()
            self._trajectory_loop_idx = self.loop_idx

    @classmethod
    def load(cls, path, use_local: bool = True):
        """Load existing session."""
        instance = super().load(path)
        instance.use_local = use_local
        logger.info(f"Loaded AlphaAgentLoop, backtest in {'local' if use_local else 'Docker'}")
        return instance

    @measure_time
    @stop_event_check
    def factor_propose(self, prev_out: dict[str, Any]):
        """Propose hypothesis as the basis for factor construction."""
        with logger.tag("r"):  
            idea = self.hypothesis_generator.gen(self.trace)
            logger.log_object(idea, tag="hypothesis generation")
            self._last_hypothesis = idea
            self._trajectory_step_outputs["factor_propose"] = (
                self._serialize_hypothesis(idea)
            )
        return idea

    @measure_time
    @stop_event_check
    def factor_construct(self, prev_out: dict[str, Any]):
        """Construct multiple factors from the hypothesis."""
        with logger.tag("r"): 
            factor = self.factor_constructor.convert(prev_out["factor_propose"], self.trace)
            logger.log_object(factor.sub_tasks, tag="experiment generation")
            self._trajectory_step_outputs["factor_construct"] = (
                self._serialize_experiment(factor, include_code=False)
            )
        return factor

    @measure_time
    @stop_event_check
    def factor_calculate(self, prev_out: dict[str, Any]):
        """Compute factor values from factor expressions."""
        with logger.tag("d"):  # develop
            factor_input = prev_out["factor_construct"]
            if self.replay_spec.get("target_step") == "factor_calculate":
                instruction = str(
                    self.replay_spec.get("refinement_instruction", "")
                ).strip()
                for task in getattr(factor_input, "sub_tasks", []) or []:
                    task.implementation_instruction = instruction
            factor = self.coder.develop(factor_input)
            logger.log_object(factor.sub_workspace_list, tag="coder result")
            self._trajectory_step_outputs["factor_calculate"] = (
                self._serialize_experiment(factor, include_code=True)
            )
        return factor
    

    @measure_time
    @stop_event_check
    def factor_backtest(self, prev_out: dict[str, Any]):
        """Run backtest for factors."""
        with logger.tag("ef"):  # evaluate and feedback
            logger.info(f"Start factor backtest (Local: {self.use_local})")
            exp = self.runner.develop(prev_out["factor_calculate"], use_local=self.use_local)
            if exp is None:
                logger.error(f"Factor extraction failed.")
                raise FactorEmptyError("Factor extraction failed.")
            logger.log_object(exp, tag="runner result")
            self._last_experiment = exp
            self._trajectory_step_outputs["factor_backtest"] = (
                self._serialize_backtest(exp)
            )
        return exp

    @measure_time
    @stop_event_check
    def feedback(self, prev_out: dict[str, Any]):
        feedback = self.summarizer.generate_feedback(prev_out["factor_backtest"], prev_out["factor_propose"], self.trace)
        with logger.tag("ef"):  # evaluate and feedback
            logger.log_object(feedback, tag="feedback")
        self.trace.hist.append((prev_out["factor_propose"], prev_out["factor_backtest"], feedback))
        
        self._last_feedback = feedback
        self._trajectory_step_outputs["feedback"] = self._serialize_feedback(
            feedback
        )

        # Auto-save factors to unified factor library
        try:
            import os
            from pathlib import Path
            from quantaalpha.factors.library import FactorLibraryManager
            
            # Project root: loop.py -> pipeline/ -> quantaalpha/ -> project_root/
            project_root = Path(__file__).resolve().parent.parent.parent

            experiment_id = "unknown"
            if hasattr(self, 'session_folder') and self.session_folder:
                parts = Path(self.session_folder).parts
                for part in parts:
                    if part.startswith("202") and len(part) > 10:
                        experiment_id = part
                        break

            round_number = self.round_idx

            hypothesis_text = None
            if prev_out.get("factor_propose"):
                hypothesis_text = str(prev_out["factor_propose"])

            planning_direction = getattr(self, 'potential_direction', None)
            user_initial_direction = getattr(self, 'user_initial_direction', None)

            evolution_phase = getattr(self, 'evolution_phase', 'original')
            trajectory_id = getattr(self, 'trajectory_id', '')
            parent_trajectory_ids = getattr(self, 'parent_trajectory_ids', [])

            # Factor library filename can be customized via env FACTOR_LIBRARY_SUFFIX
            library_suffix = os.environ.get('FACTOR_LIBRARY_SUFFIX', '')
            if library_suffix:
                library_filename = f"all_factors_library_{library_suffix}.json"
            else:
                library_filename = "all_factors_library.json"
            factorlib_dir = project_root / "data" / "factorlib"
            factorlib_dir.mkdir(parents=True, exist_ok=True)
            library_path = factorlib_dir / library_filename
            manager = FactorLibraryManager(str(library_path))
            manager.add_factors_from_experiment(
                experiment=prev_out["factor_backtest"],
                experiment_id=experiment_id,
                round_number=round_number,
                hypothesis=hypothesis_text,
                feedback=feedback,
                initial_direction=planning_direction,
                user_initial_direction=user_initial_direction,
                planning_direction=planning_direction,
                evolution_phase=evolution_phase,
                trajectory_id=trajectory_id,
                parent_trajectory_ids=parent_trajectory_ids,
                quality_gate=self.quality_gate_config,
            )
            logger.info(f"Saved factors to library: {library_path} (phase={evolution_phase})")
        except Exception as e:
            logger.warning(f"Failed to save factors to library: {e}")
    
    def _get_trajectory_data(self) -> dict[str, Any]:
        """
        Get trajectory data for the current round (used by evolution controller).
        Method name is prefixed with underscore so the workflow system does not treat it as a step.
        Returns:
            Dict with hypothesis, experiment, feedback, etc.
        """
        return {
            "hypothesis": self._last_hypothesis,
            "experiment": self._last_experiment,
            "feedback": self._last_feedback,
            "direction_id": self.direction_id,
            "evolution_phase": self.evolution_phase,
            "trajectory_id": self.trajectory_id,
            "parent_trajectory_ids": self.parent_trajectory_ids,
            "loop_idx": self.loop_idx,
            "round_idx": self.round_idx,
            "step_outputs": self._trajectory_step_outputs,
            "step_checkpoints": self._collect_step_checkpoints(),
            "evolution_spec": self.replay_spec,
        }

    @staticmethod
    def _serialize_hypothesis(hypothesis: Any) -> dict[str, Any]:
        fields = (
            "hypothesis",
            "reason",
            "concise_reason",
            "concise_observation",
            "concise_justification",
            "concise_knowledge",
        )
        return {
            key: getattr(hypothesis, key, "")
            for key in fields
            if hasattr(hypothesis, key)
        } or {"text": str(hypothesis)}

    @staticmethod
    def _serialize_experiment(
        experiment: Any,
        *,
        include_code: bool,
    ) -> dict[str, Any]:
        tasks = getattr(experiment, "sub_tasks", []) or []
        workspaces = getattr(experiment, "sub_workspace_list", []) or []
        factors: list[dict[str, Any]] = []
        for index, task in enumerate(tasks):
            factor = {
                "name": getattr(task, "factor_name", f"factor_{index}"),
                "description": getattr(task, "factor_description", ""),
                "formulation": getattr(task, "factor_formulation", ""),
                "expression": getattr(task, "factor_expression", ""),
                "variables": getattr(task, "variables", {}) or {},
                "quality_gate": getattr(task, "quality_gate", {}) or {},
            }
            if include_code and index < len(workspaces):
                code_dict = getattr(workspaces[index], "code_dict", {}) or {}
                factor["code"] = code_dict.get("factor.py", "")
            factors.append(factor)
        return {"factors": factors}

    @staticmethod
    def _serialize_backtest(experiment: Any) -> dict[str, Any]:
        result = getattr(experiment, "result", None)
        if isinstance(result, pd.DataFrame):
            return {
                "type": "DataFrame",
                "shape": list(result.shape),
                "index": [str(value) for value in result.index],
            }
        if isinstance(result, pd.Series):
            return {
                "type": "Series",
                "size": int(result.size),
                "index": [str(value) for value in result.index],
            }
        return {"type": type(result).__name__ if result is not None else None}

    @staticmethod
    def _serialize_feedback(feedback: Any) -> dict[str, Any]:
        fields = (
            "observations",
            "hypothesis_evaluation",
            "new_hypothesis",
            "reason",
            "decision",
        )
        return {
            key: getattr(feedback, key, "")
            for key in fields
            if hasattr(feedback, key)
        } or {"text": str(feedback)}

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def _collect_step_checkpoints(self) -> dict[str, dict[str, Any]]:
        inherited = self.replay_spec.get("inherited_checkpoints") or {}
        target_step = self.replay_spec.get("target_step")
        target_index = (
            self.steps.index(target_step)
            if target_step in self.steps
            else 0
        )
        checkpoints: dict[str, dict[str, Any]] = {}
        for index, step_name in enumerate(self.steps):
            if index < target_index and step_name in inherited:
                checkpoints[step_name] = dict(inherited[step_name])
                continue
            path = (
                Path(self.session_folder)
                / str(self._trajectory_loop_idx)
                / f"{index}_{step_name}"
            )
            if path.exists():
                checkpoints[step_name] = {
                    "path": str(path.resolve()),
                    "sha256": self._sha256(path),
                    "afterStepIndex": index,
                }
        return checkpoints

    @classmethod
    def load_for_replay(
        cls,
        checkpoint_path: str | Path,
        *,
        checkpoint_sha256: str,
        replay_spec: dict[str, Any],
        log_path: str | Path,
        use_local: bool,
        stop_event: threading.Event | None,
        trajectory_id: str,
        parent_trajectory_ids: list[str],
        direction_id: int,
        round_idx: int,
        quality_gate_config: dict[str, Any],
        factor_config: dict[str, Any],
    ) -> "AlphaAgentLoop":
        """Load a trusted local step checkpoint and replay only its suffix."""
        checkpoint = Path(checkpoint_path).resolve()
        if not checkpoint.is_file():
            raise FileNotFoundError(f"Replay checkpoint does not exist: {checkpoint}")
        if cls._sha256(checkpoint) != checkpoint_sha256:
            raise ValueError(f"Replay checkpoint hash mismatch: {checkpoint}")
        with checkpoint.open("rb") as handle:
            instance = pickle.load(handle)
        if not isinstance(instance, cls):
            raise TypeError(
                f"Replay checkpoint contains {type(instance).__name__}, expected {cls.__name__}"
            )

        target_step = replay_spec.get("target_step")
        if target_step not in instance.steps:
            raise ValueError(f"Unsupported replay target step: {target_step}")
        target_index = instance.steps.index(target_step)
        if instance.step_idx != target_index:
            raise ValueError(
                "Replay checkpoint boundary mismatch: "
                f"expected step {target_index}, got {instance.step_idx}"
            )
        if (
            instance.quality_gate_config != quality_gate_config
            or instance.factor_config != factor_config
        ):
            raise ValueError(
                "Replay config differs from the parent checkpoint; "
                "a frozen prefix cannot be reused under changed gates"
            )

        instance.use_local = use_local
        instance.replay_spec = dict(replay_spec)
        instance.evolution_phase = "mutation"
        instance.trajectory_id = trajectory_id
        instance.parent_trajectory_ids = list(parent_trajectory_ids)
        instance.direction_id = direction_id
        instance.round_idx = round_idx
        instance.session_folder = Path(log_path).resolve() / "__session__"
        instance._trajectory_loop_idx = instance.loop_idx
        instance.factor_constructor.evolution_instruction = (
            str(replay_spec.get("refinement_instruction", ""))
            if target_step == "factor_construct"
            else ""
        )

        global STOP_EVENT
        STOP_EVENT = stop_event
        return instance




class BacktestLoop(LoopBase, metaclass=LoopMeta):
    skip_loop_error = (FactorEmptyError,)
    @measure_time
    def __init__(self, PROP_SETTING: BaseFacSetting, factor_path=None):
        with logger.tag("init"):

            self.factor_path = factor_path

            scen: Scenario = import_class(PROP_SETTING.scen)()
            logger.log_object(scen, tag="scenario")

            self.hypothesis_generator: HypothesisGen = import_class(PROP_SETTING.hypothesis_gen)(scen)
            logger.log_object(self.hypothesis_generator, tag="hypothesis generator")

            self.factor_constructor: Hypothesis2Experiment = import_class(PROP_SETTING.hypothesis2experiment)(factor_path=factor_path)
            logger.log_object(self.factor_constructor, tag="experiment generation")

            self.coder: Developer = import_class(PROP_SETTING.coder)(scen, with_feedback=False, with_knowledge=False, knowledge_self_gen=False)
            logger.log_object(self.coder, tag="coder")
            
            self.runner: Developer = import_class(PROP_SETTING.runner)(scen)
            logger.log_object(self.runner, tag="runner")

            self.summarizer: HypothesisExperiment2Feedback = import_class(PROP_SETTING.summarizer)(scen)
            logger.log_object(self.summarizer, tag="summarizer")
            self.trace = Trace(scen=scen)
            super().__init__()

    def factor_propose(self, prev_out: dict[str, Any]):
        """
        Market hypothesis on which factors are built
        """
        with logger.tag("r"):  
            idea = self.hypothesis_generator.gen(self.trace)
            logger.log_object(idea, tag="hypothesis generation")
        return idea
        

    @measure_time
    def factor_construct(self, prev_out: dict[str, Any]):
        """
        Construct a variety of factors that depend on the hypothesis
        """
        with logger.tag("r"): 
            factor = self.factor_constructor.convert(prev_out["factor_propose"], self.trace)
            logger.log_object(factor.sub_tasks, tag="experiment generation")
        return factor

    @measure_time
    def factor_calculate(self, prev_out: dict[str, Any]):
        """
        Debug factors and calculate their values
        """
        with logger.tag("d"):  # develop
            factor = self.coder.develop(prev_out["factor_construct"])
            logger.log_object(factor.sub_workspace_list, tag="coder result")
        return factor
    

    @measure_time
    def factor_backtest(self, prev_out: dict[str, Any]):
        """
        Conduct Backtesting
        """
        with logger.tag("ef"):  # evaluate and feedback
            exp = self.runner.develop(prev_out["factor_calculate"])
            if exp is None:
                logger.error(f"Factor extraction failed.")
                raise FactorEmptyError("Factor extraction failed.")
            logger.log_object(exp, tag="runner result")
        return exp

    @measure_time
    def stop(self, prev_out: dict[str, Any]):
        exit(0)
