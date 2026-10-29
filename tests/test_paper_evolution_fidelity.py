import hashlib
import json
import pickle
import tempfile
import unittest
from collections import defaultdict
from pathlib import Path
from queue import Queue
from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd

from quantaalpha.core.exception import FactorEmptyError
from quantaalpha.factors.coder.factor import FactorTask
from quantaalpha.factors.library import FactorLibraryManager
from quantaalpha.factors.regulator.consistency_checker import (
    ComplexityChecker,
    RedundancyChecker,
)
from quantaalpha.pipeline.evolution.crossover import CrossoverOperator
from quantaalpha.pipeline.evolution.controller import (
    EvolutionConfig,
    EvolutionController,
)
from quantaalpha.pipeline.evolution.mutation import MutationOperator
from quantaalpha.pipeline.evolution.trajectory import (
    RoundPhase,
    StrategyTrajectory,
)
from quantaalpha.pipeline.factor_mining import (
    _load_resume_directions,
    _run_evolution_task,
    _parallel_task_worker,
    _require_evaluated_trajectory,
    _suppress_unsafe_rdagent_settings_logging,
)
from quantaalpha.pipeline.loop import AlphaAgentLoop


class _StubConstructor:
    def __init__(self):
        self.evolution_instruction = ""

    def convert(self, hypothesis, trace):
        task = FactorTask(
            factor_name="factor",
            factor_description="description",
            factor_formulation="formulation",
            factor_expression="$close",
            quality_gate={
                "evaluated": True,
                "passed": True,
                "consistency_passed": True,
            },
        )
        workspace = SimpleNamespace(code_dict={"factor.py": "# generated"})
        return SimpleNamespace(
            sub_tasks=[task],
            sub_workspace_list=[workspace],
            result=None,
        )


class _StubDeveloper:
    def develop(self, experiment, **kwargs):
        return experiment


class _StubRunner:
    def develop(self, experiment, **kwargs):
        experiment.result = pd.Series({"RankIC": 0.02})
        return experiment


class _StubFeedback:
    observations = "improved"
    hypothesis_evaluation = "consistent"
    new_hypothesis = ""
    reason = "passed"
    decision = True

    def __str__(self):
        return "passed"


class _StubSummarizer:
    def generate_feedback(self, experiment, hypothesis, trace):
        return _StubFeedback()


def _trajectory(trajectory_id: str, rank_ic: float) -> StrategyTrajectory:
    return StrategyTrajectory(
        trajectory_id=trajectory_id,
        direction_id=0,
        round_idx=1,
        phase=RoundPhase.MUTATION,
        hypothesis=f"hypothesis-{trajectory_id}",
        backtest_metrics={"RankIC": rank_ic},
        step_outputs={
            "factor_propose": {"hypothesis": f"hypothesis-{trajectory_id}"},
            "factor_construct": {
                "factors": [{"expression": f"RANK(${trajectory_id})"}]
            },
            "factor_calculate": {"factors": [{"code": f"# {trajectory_id}"}]},
            "feedback": {"observations": f"repair-{trajectory_id}"},
        },
        step_checkpoints={
            "factor_propose": {
                "path": f"/tmp/{trajectory_id}-propose",
                "sha256": "a" * 64,
                "afterStepIndex": 0,
            },
            "factor_construct": {
                "path": f"/tmp/{trajectory_id}-construct",
                "sha256": "b" * 64,
                "afterStepIndex": 1,
            },
        },
    )


class PaperEvolutionFidelityTest(unittest.TestCase):
    def test_resume_recovers_only_completed_original_progress(self):
        with tempfile.TemporaryDirectory() as temporary:
            pool_path = Path(temporary) / "trajectory_pool.json"
            writer = EvolutionController(
                EvolutionConfig(
                    num_directions=2,
                    pool_save_path=str(pool_path),
                    fresh_start=True,
                )
            )
            for direction_id in (0, 1):
                trajectory = _trajectory(f"original-{direction_id}", 0.02)
                trajectory.direction_id = direction_id
                trajectory.round_idx = 0
                trajectory.phase = RoundPhase.ORIGINAL
                writer.pool.add(trajectory)

            resumed = EvolutionController(
                EvolutionConfig(
                    num_directions=2,
                    pool_save_path=str(pool_path),
                    fresh_start=False,
                )
            )
            resumed.restore_original_progress_from_pool()

        self.assertEqual(resumed._directions_completed, {0, 1})
        self.assertEqual(resumed._current_round, 0)
        self.assertEqual(resumed._current_phase, RoundPhase.ORIGINAL)

    def test_resume_reuses_completed_original_directions(self):
        with tempfile.TemporaryDirectory() as temporary:
            pool_path = Path(temporary) / "trajectory_pool.json"
            writer = EvolutionController(
                EvolutionConfig(
                    num_directions=2,
                    pool_save_path=str(pool_path),
                    fresh_start=True,
                )
            )
            for direction_id in (0, 1):
                trajectory = _trajectory(f"original-{direction_id}", 0.02)
                trajectory.direction_id = direction_id
                trajectory.round_idx = 0
                trajectory.phase = RoundPhase.ORIGINAL
                trajectory.hypothesis = f"direction-{direction_id}"
                writer.pool.add(trajectory)

            self.assertEqual(
                _load_resume_directions(pool_path, 2),
                ["direction-0", "direction-1"],
            )
            self.assertIsNone(_load_resume_directions(pool_path, 3))

    def test_resume_preserves_completed_crossover_index(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            pool_path = root / "trajectory_pool.json"
            state_path = root / "evolution_state.json"
            config = EvolutionConfig(
                num_directions=2,
                max_rounds=3,
                crossover_n=1,
                pool_save_path=str(pool_path),
                fresh_start=True,
            )
            writer = EvolutionController(config)
            for direction_id in (0, 1):
                original = _trajectory(f"original-{direction_id}", 0.02)
                original.direction_id = direction_id
                original.round_idx = 0
                original.phase = RoundPhase.ORIGINAL
                writer.pool.add(original)
                mutation = _trajectory(f"mutation-{direction_id}", 0.03)
                mutation.direction_id = direction_id
                mutation.round_idx = 1
                writer.pool.add(mutation)

            writer._current_round = 2
            writer._current_phase = RoundPhase.CROSSOVER
            writer._prepare_crossover_groups()
            writer._crossover_idx = len(writer._crossover_groups)
            writer.save_state(state_path)

            resumed = EvolutionController(
                EvolutionConfig(
                    num_directions=2,
                    max_rounds=3,
                    crossover_n=1,
                    pool_save_path=str(pool_path),
                    fresh_start=False,
                )
            )
            resumed.load_state(state_path)

            self.assertEqual(
                resumed._crossover_idx,
                len(resumed._crossover_groups),
            )
            self.assertIsNone(resumed.get_next_task())
            self.assertEqual(resumed._current_round, 3)

    def test_resume_without_state_rejects_evolved_pool(self):
        controller = EvolutionController(EvolutionConfig())
        controller.pool.add(_trajectory("mutation", 0.02))

        with self.assertRaisesRegex(
            RuntimeError,
            "evolution_state.json file is required",
        ):
            controller.restore_original_progress_from_pool()

    def test_mutation_task_uses_parent_hypothesis_as_direction(self):
        parent = _trajectory("parent", 0.02)
        task = {
            "phase": RoundPhase.MUTATION,
            "direction_id": 0,
            "parent_trajectories": [parent],
            "round_idx": 1,
            "strategy_suffix": "localized revision",
        }
        completed = {
            "trajectory_id": "child",
            "hypothesis": "child hypothesis",
            "experiment": SimpleNamespace(),
            "feedback": SimpleNamespace(),
        }
        with patch(
            "quantaalpha.pipeline.factor_mining.AlphaAgentLoop"
        ) as loop_class, patch(
            "quantaalpha.pipeline.factor_mining._run_model_loop_to_completed_trajectory",
            return_value=completed,
        ), patch(
            "quantaalpha.pipeline.factor_mining._suppress_unsafe_rdagent_settings_logging",
            return_value=True,
        ):
            loop_class.steps = AlphaAgentLoop.steps
            loop_class.return_value.steps = AlphaAgentLoop.steps
            _run_evolution_task(
                task=task,
                directions=["unrelated resumed direction"],
                step_n=5,
                use_local=True,
                user_direction="seed",
                log_root="",
                stop_event=None,
            )

        self.assertEqual(
            loop_class.call_args.kwargs["potential_direction"],
            parent.hypothesis,
        )

    def test_serial_phase_transitions_stop_at_max_rounds(self):
        cases = (
            (RoundPhase.ORIGINAL, 0, 1),
            (RoundPhase.MUTATION, 1, 2),
            (RoundPhase.CROSSOVER, 2, 3),
        )
        for phase, current_round, max_rounds in cases:
            with self.subTest(phase=phase):
                controller = EvolutionController(
                    EvolutionConfig(
                        num_directions=2,
                        max_rounds=max_rounds,
                        mutation_enabled=True,
                        crossover_enabled=True,
                    )
                )
                controller._current_phase = phase
                controller._current_round = current_round
                controller._directions_completed = {0, 1}
                controller._mutation_targets = []
                controller._mutation_idx = 0
                controller._crossover_groups = []
                controller._crossover_idx = 0

                self.assertIsNone(controller.get_next_task())
                self.assertEqual(controller._current_round, max_rounds)

    def test_evolution_task_requires_factors_and_backtest_result(self):
        valid = {
            "experiment": SimpleNamespace(
                sub_tasks=[SimpleNamespace()],
                result=pd.Series({"RankIC": 0.0}),
            )
        }
        self.assertIs(_require_evaluated_trajectory(valid), valid)

        invalid = [
            {},
            {
                "experiment": SimpleNamespace(
                    sub_tasks=[],
                    result=pd.Series({"RankIC": 0.0}),
                )
            },
            {
                "experiment": SimpleNamespace(
                    sub_tasks=[SimpleNamespace()],
                    result=None,
                )
            },
        ]
        for trajectory_data in invalid:
            with self.subTest(trajectory_data=trajectory_data):
                with self.assertRaisesRegex(
                    FactorEmptyError,
                    "without factors and a completed backtest",
                ):
                    _require_evaluated_trajectory(trajectory_data)

    def test_unsuccessful_trajectories_are_not_evolution_parents(self):
        controller = EvolutionController(EvolutionConfig())
        original = _trajectory("original", 0.03)
        original.phase = RoundPhase.ORIGINAL
        original.round_idx = 0
        valid_mutation = _trajectory("valid-mutation", 0.02)
        invalid_mutation = StrategyTrajectory(
            trajectory_id="empty-mutation",
            direction_id=1,
            round_idx=1,
            phase=RoundPhase.MUTATION,
        )
        controller.pool.add(original)
        controller.pool.add(valid_mutation)
        controller.pool.add(invalid_mutation)

        crossover_ids = {
            trajectory.trajectory_id
            for trajectory in controller._get_crossover_candidates()
        }
        self.assertEqual(crossover_ids, {"original", "valid-mutation"})

        controller._current_round = 1
        controller._prepare_mutation_targets()
        mutation_ids = {
            trajectory.trajectory_id
            for trajectory in controller._mutation_targets
        }
        self.assertEqual(mutation_ids, {"original"})

    def test_rdagent_settings_with_credentials_are_never_logged(self):
        from rdagent.oai.backend.litellm import LiteLLMAPIBackend

        with patch.object(
            LiteLLMAPIBackend,
            "_has_logged_settings",
            False,
        ):
            self.assertTrue(_suppress_unsafe_rdagent_settings_logging())
            self.assertTrue(LiteLLMAPIBackend._has_logged_settings)

    def test_mutation_binds_localized_action_to_frozen_prefix_checkpoint(self):
        parent = _trajectory("parent-a", 0.03)
        response = json.dumps(
            {
                "target_step": "factor_construct",
                "diagnosis": "Expression does not encode the stated regime.",
                "refinement_instruction": "Keep the hypothesis and rebuild only the expression.",
                "expected_effect": "Higher validation RankIC.",
            }
        )
        with patch(
            "quantaalpha.pipeline.evolution.mutation.APIBackend"
        ) as backend:
            backend.return_value.build_messages_and_create_chat_completion.return_value = (
                response
            )
            spec = MutationOperator().build_localized_replay_task(parent)

        request = (
            backend.return_value.build_messages_and_create_chat_completion.call_args.kwargs
        )
        self.assertIn(
            "json",
            f"{request['system_prompt']} {request['user_prompt']}".lower(),
        )
        self.assertTrue(request["json_mode"])
        self.assertEqual(spec["target_step"], "factor_construct")
        self.assertEqual(spec["frozen_prefix"], ["factor_propose"])
        self.assertEqual(
            spec["parent_checkpoint"],
            parent.step_checkpoints["factor_propose"],
        )
        self.assertEqual(
            spec["contract"],
            "paper_v3_localized_prefix_replay",
        )

    def test_mutation_fails_closed_on_unknown_action(self):
        parent = _trajectory("parent-a", 0.03)
        response = json.dumps(
            {
                "target_step": "backtest",
                "diagnosis": "weak",
                "refinement_instruction": "change it",
            }
        )
        with patch(
            "quantaalpha.pipeline.evolution.mutation.APIBackend"
        ) as backend:
            backend.return_value.build_messages_and_create_chat_completion.return_value = (
                response
            )
            with self.assertRaisesRegex(ValueError, "Unsupported mutation target"):
                MutationOperator().build_localized_replay_task(parent)

    def test_crossover_retains_source_for_each_validated_segment(self):
        parents = [_trajectory("parent-a", 0.03), _trajectory("parent-b", 0.02)]
        response = json.dumps(
            {
                "selected_segments": [
                    {
                        "segment_type": "hypothesis_template",
                        "parent_id": "parent-a",
                        "rationale": "stronger hypothesis",
                    },
                    {
                        "segment_type": "construction_pattern",
                        "parent_id": "parent-b",
                        "rationale": "complementary expression",
                    },
                    {
                        "segment_type": "repair_behavior",
                        "parent_id": "parent-a",
                        "rationale": "validated repair",
                    },
                ],
                "synthesis_instruction": "Combine the mechanism and expression pattern.",
                "coherence_reason": "Both segments model the same price-volume regime.",
            }
        )
        with patch(
            "quantaalpha.pipeline.evolution.crossover.APIBackend"
        ) as backend:
            backend.return_value.build_messages_and_create_chat_completion.return_value = (
                response
            )
            spec = CrossoverOperator().build_validated_segment_recombination_task(
                parents
            )

        request = (
            backend.return_value.build_messages_and_create_chat_completion.call_args.kwargs
        )
        self.assertIn(
            "json",
            f"{request['system_prompt']} {request['user_prompt']}".lower(),
        )
        self.assertTrue(request["json_mode"])
        self.assertEqual(
            spec["contract"],
            "paper_v3_validated_segment_recombination",
        )
        self.assertEqual(
            {item["parent_id"] for item in spec["selected_segments"]},
            {"parent-a", "parent-b"},
        )
        self.assertTrue(
            all(item["content"] for item in spec["selected_segments"])
        )

    def test_replay_loader_preserves_prefix_and_moves_to_new_session(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            checkpoint = root / "checkpoint.pkl"
            loop = AlphaAgentLoop.__new__(AlphaAgentLoop)
            loop.step_idx = 1
            loop.loop_idx = 0
            loop.loop_prev_out = {"factor_propose": "frozen-parent-output"}
            loop.quality_gate_config = {"consistency_enabled": True}
            loop.factor_config = {"factors_per_hypothesis": 3}
            loop.factor_constructor = SimpleNamespace(evolution_instruction="")
            loop._trajectory_step_outputs = {
                "factor_propose": {"hypothesis": "frozen"}
            }
            with checkpoint.open("wb") as handle:
                pickle.dump(loop, handle)
            digest = hashlib.sha256(checkpoint.read_bytes()).hexdigest()
            replay_spec = {
                "target_step": "factor_construct",
                "refinement_instruction": "rebuild expression only",
                "inherited_checkpoints": {},
            }

            loaded = AlphaAgentLoop.load_for_replay(
                checkpoint,
                checkpoint_sha256=digest,
                replay_spec=replay_spec,
                log_path=root / "child",
                use_local=True,
                stop_event=None,
                trajectory_id="child-id",
                parent_trajectory_ids=["parent-id"],
                direction_id=2,
                round_idx=3,
                quality_gate_config={"consistency_enabled": True},
                factor_config={"factors_per_hypothesis": 3},
            )

            self.assertEqual(
                loaded.loop_prev_out["factor_propose"],
                "frozen-parent-output",
            )
            self.assertEqual(loaded.step_idx, 1)
            self.assertEqual(
                loaded.factor_constructor.evolution_instruction,
                "rebuild expression only",
            )
            self.assertEqual(
                loaded.session_folder,
                (root / "child" / "__session__").resolve(),
            )

    def test_replay_executes_only_target_and_suffix_with_real_loop_runner(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            checkpoint = root / "parent-after-propose.pkl"
            loop = AlphaAgentLoop.__new__(AlphaAgentLoop)
            loop.step_idx = 1
            loop.loop_idx = 0
            loop.loop_prev_out = {"factor_propose": "frozen-hypothesis"}
            loop.loop_trace = defaultdict(list)
            loop.session_folder = root / "parent" / "__session__"
            loop.quality_gate_config = {"consistency_enabled": True}
            loop.factor_config = {"factors_per_hypothesis": 3}
            loop.factor_constructor = _StubConstructor()
            loop.coder = _StubDeveloper()
            loop.runner = _StubRunner()
            loop.summarizer = _StubSummarizer()
            loop.trace = SimpleNamespace(hist=[])
            loop._last_hypothesis = "frozen-hypothesis"
            loop._last_experiment = None
            loop._last_feedback = None
            loop._trajectory_step_outputs = {
                "factor_propose": {"text": "frozen-hypothesis"}
            }
            loop.replay_spec = {}
            loop._trajectory_loop_idx = 0
            loop.use_local = True
            loop.potential_direction = "parent-direction"
            loop.user_initial_direction = "seed"
            with checkpoint.open("wb") as handle:
                pickle.dump(loop, handle)
            digest = hashlib.sha256(checkpoint.read_bytes()).hexdigest()
            inherited = {
                "factor_propose": {
                    "path": str(checkpoint),
                    "sha256": digest,
                    "afterStepIndex": 0,
                }
            }
            replay_spec = {
                "target_step": "factor_construct",
                "refinement_instruction": "rebuild expression only",
                "inherited_checkpoints": inherited,
            }
            loaded = AlphaAgentLoop.load_for_replay(
                checkpoint,
                checkpoint_sha256=digest,
                replay_spec=replay_spec,
                log_path=root / "child",
                use_local=True,
                stop_event=None,
                trajectory_id="child-id",
                parent_trajectory_ids=["parent-id"],
                direction_id=2,
                round_idx=3,
                quality_gate_config={"consistency_enabled": True},
                factor_config={"factors_per_hypothesis": 3},
            )
            with patch(
                "quantaalpha.factors.library.FactorLibraryManager.add_factors_from_experiment"
            ):
                loaded.run(step_n=4, stop_event=None)
            trajectory = loaded._get_trajectory_data()

            self.assertEqual(
                trajectory["hypothesis"],
                "frozen-hypothesis",
            )
            self.assertEqual(
                trajectory["step_checkpoints"]["factor_propose"],
                inherited["factor_propose"],
            )
            self.assertEqual(
                set(trajectory["step_outputs"]),
                {
                    "factor_propose",
                    "factor_construct",
                    "factor_calculate",
                    "factor_backtest",
                    "feedback",
                },
            )
            self.assertEqual(loaded.loop_idx, 1)

    def test_parallel_worker_forwards_quality_gate_config(self):
        queue = Queue()
        with patch(
            "quantaalpha.pipeline.factor_mining._run_evolution_task",
            return_value={"trajectory_id": "child"},
        ) as run_task:
            _parallel_task_worker(
                task={"phase": RoundPhase.ORIGINAL},
                directions=["direction"],
                step_n=5,
                use_local=True,
                user_direction="direction",
                log_root="/tmp/paper-test",
                result_queue=queue,
                task_idx=0,
                quality_gate_cfg={"consistency_enabled": True},
                factor_cfg={"factors_per_hypothesis": 3},
            )
        self.assertTrue(queue.get_nowait()["success"])
        self.assertEqual(
            run_task.call_args.kwargs["quality_gate_cfg"],
            {"consistency_enabled": True},
        )

    def test_factor_task_exposes_code_repair_instruction_to_coder(self):
        task = FactorTask(
            factor_name="factor",
            factor_description="description",
            factor_formulation="formulation",
            implementation_instruction="preserve expression; repair NaN handling",
        )
        self.assertIn(
            "repair NaN handling",
            task.get_task_information(),
        )

    def test_factor_library_never_treats_enabled_gate_as_pass_evidence(self):
        with tempfile.TemporaryDirectory() as temporary:
            library_path = Path(temporary) / "factors.json"
            task = FactorTask(
                factor_name="factor",
                factor_description="description",
                factor_formulation="formulation",
                factor_expression="$close",
            )
            experiment = SimpleNamespace(
                sub_tasks=[task],
                sub_workspace_list=[],
                result=None,
            )
            manager = FactorLibraryManager(str(library_path))
            manager.add_factors_from_experiment(
                experiment,
                quality_gate={"consistency_enabled": True},
            )
            stored = next(iter(manager.data["factors"].values()))
            self.assertFalse(stored["quality_gate"]["evaluated"])
            self.assertFalse(stored["quality_gate"]["consistency_passed"])

    def test_factor_library_persists_per_factor_gate_evidence(self):
        with tempfile.TemporaryDirectory() as temporary:
            library_path = Path(temporary) / "factors.json"
            task = FactorTask(
                factor_name="factor",
                factor_description="description",
                factor_formulation="formulation",
                factor_expression="$close",
                quality_gate={
                    "evaluated": True,
                    "passed": True,
                    "consistency_passed": True,
                    "consistency_check": {"is_consistent": True},
                },
            )
            experiment = SimpleNamespace(
                sub_tasks=[task],
                sub_workspace_list=[],
                result=None,
            )
            manager = FactorLibraryManager(str(library_path))
            manager.add_factors_from_experiment(
                experiment,
                quality_gate={"consistency_enabled": True},
            )
            stored = next(iter(manager.data["factors"].values()))
            self.assertTrue(stored["quality_gate"]["evaluated"])
            self.assertTrue(stored["quality_gate"]["passed"])
            self.assertTrue(stored["quality_gate"]["consistency_passed"])

    def test_paper_complexity_gate_fails_closed_on_checker_error(self):
        checker = ComplexityChecker(enabled=True, fail_closed=True)
        with patch(
            "quantaalpha.factors.coder.factor_ast.calculate_symbol_length",
            side_effect=RuntimeError("broken parser"),
        ):
            passed, _ = checker.check("$close")
        self.assertFalse(passed)

    def test_paper_redundancy_gate_fails_closed_on_checker_error(self):
        checker = RedundancyChecker(enabled=True, fail_closed=True)
        checker._factor_regulator = SimpleNamespace(
            is_parsable=lambda expression: (_ for _ in ()).throw(
                RuntimeError("broken parser")
            )
        )
        passed, _, _ = checker.check("$close")
        self.assertFalse(passed)

    def test_stale_parallel_library_managers_merge_instead_of_losing_factors(self):
        with tempfile.TemporaryDirectory() as temporary:
            library_path = Path(temporary) / "parallel.json"
            first = FactorLibraryManager(str(library_path))
            second = FactorLibraryManager(str(library_path))

            def experiment(name: str, expression: str):
                return SimpleNamespace(
                    sub_tasks=[
                        FactorTask(
                            factor_name=name,
                            factor_description="description",
                            factor_formulation="formulation",
                            factor_expression=expression,
                        )
                    ],
                    sub_workspace_list=[],
                    result=None,
                )

            first.add_factors_from_experiment(
                experiment("first", "$close")
            )
            second.add_factors_from_experiment(
                experiment("second", "$volume")
            )
            stored = FactorLibraryManager(str(library_path)).data
            self.assertEqual(stored["metadata"]["total_factors"], 2)
            self.assertEqual(
                {factor["factor_name"] for factor in stored["factors"].values()},
                {"first", "second"},
            )


if __name__ == "__main__":
    unittest.main()
