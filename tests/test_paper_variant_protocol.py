import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import yaml

from quantaalpha.core.exception import FactorEmptyError
from quantaalpha.factors.proposal import (
    AlphaAgentHypothesis2FactorExpression,
)
from quantaalpha.factors.coder.config import FACTOR_COSTEER_SETTINGS
from quantaalpha.factors.qlib_utils import get_data_folder_intro
from quantaalpha.factors.regulator.consistency_checker import (
    FactorConsistencyChecker,
)
from quantaalpha.llm.client import APIBackend
from quantaalpha.llm.config import LLM_SETTINGS
from quantaalpha.pipeline.factor_mining import (
    _configure_llm_runtime,
    _run_model_loop_to_completed_trajectory,
)
from quantaalpha.validation.paper import PROJECT_ROOT, simulate_schedule
from quantaalpha.validation.pilot_audit import (
    audit_paper_validation_pilot,
)


class PilotLauncherTests(unittest.TestCase):
    def test_paid_pilot_uses_serial_tasks_for_llm_endpoint_stability(self):
        config = yaml.safe_load(
            (
                PROJECT_ROOT
                / "configs"
                / "experiment_paper_pilot_v3_1.yaml"
            ).read_text(encoding="utf-8")
        )
        self.assertFalse(config["evolution"]["parallel_enabled"])

    def test_full_and_debug_factor_data_use_the_same_immutable_panel(self):
        launcher = (
            PROJECT_ROOT
            / "scripts"
            / "start_paper_validation_pilot_v3_1.sh"
        ).read_text(encoding="utf-8")
        self.assertIn(
            'export FACTOR_CoSTEER_DATA_FOLDER_OVERRIDE="${FACTOR_DATA_DIR}"',
            launcher,
        )
        self.assertIn(
            'export FACTOR_CoSTEER_DATA_FOLDER_DEBUG_OVERRIDE="${FACTOR_DATA_DIR}"',
            launcher,
        )

    def test_versioned_factor_folder_is_safe_to_describe(self):
        import pandas as pd
        with tempfile.TemporaryDirectory() as temp_dir:
            folder = Path(temp_dir)
            index = pd.MultiIndex.from_tuples(
                [(pd.Timestamp("2026-01-02"), "SH600000")],
                names=["datetime", "instrument"],
            )
            pd.DataFrame({"$close": [10.0], "$vwap": [10.0]}, index=index).to_hdf(
                folder / "daily_pv.h5", key="data")
            (folder / "manifest.json").write_text(json.dumps({
                "schemaVersion": 1, "market": "synthetic-unit-test",
                "fields": ["$close", "$vwap"], "rows": 1,
                "dataPath": "/private/synthetic-data", "providerPath": "/private/synthetic-provider",
            }))
            old_full = FACTOR_COSTEER_SETTINGS.data_folder
            old_debug = FACTOR_COSTEER_SETTINGS.data_folder_debug
            try:
                FACTOR_COSTEER_SETTINGS.data_folder = str(folder)
                FACTOR_COSTEER_SETTINGS.data_folder_debug = str(folder)
                description = get_data_folder_intro()
            finally:
                FACTOR_COSTEER_SETTINGS.data_folder = old_full
                FACTOR_COSTEER_SETTINGS.data_folder_debug = old_debug
            self.assertIn("daily_pv.h5", description)
            self.assertIn("$vwap", description)
            self.assertIn("manifest.json", description)
            self.assertNotIn("dataPath", description)
            self.assertNotIn("providerPath", description)


class CompletedTrajectoryTests(unittest.TestCase):
    class _InterruptedLoop:
        steps = [
            "factor_propose",
            "factor_construct",
            "factor_calculate",
            "factor_backtest",
            "feedback",
        ]

        def __init__(self):
            self.step_idx = 0
            self.run_calls = []
            self.completed = False

        def run(self, step_n, stop_event):
            self.run_calls.append(step_n)
            if len(self.run_calls) == 1:
                self.step_idx = 3
            else:
                self.step_idx = 0
                self.completed = True

        def _get_trajectory_data(self):
            if not self.completed:
                return {"experiment": None, "feedback": None}
            return {
                "experiment": SimpleNamespace(
                    sub_tasks=[SimpleNamespace()],
                    result={"RankIC": 0.01},
                ),
                "feedback": SimpleNamespace(decision=True),
            }

    def test_interrupted_loop_gets_one_bounded_completion_topup(self):
        model_loop = self._InterruptedLoop()
        result = _run_model_loop_to_completed_trajectory(
            model_loop,
            step_n=5,
            stop_event=None,
            completion_retry_budget=1,
        )
        self.assertIsNotNone(result["feedback"])
        self.assertEqual(model_loop.run_calls, [5, 2])

    def test_interrupted_loop_fails_when_completion_budget_is_zero(self):
        model_loop = self._InterruptedLoop()
        with self.assertRaises(FactorEmptyError):
            _run_model_loop_to_completed_trajectory(
                model_loop,
                step_n=5,
                stop_event=None,
                completion_retry_budget=0,
            )
        self.assertEqual(model_loop.run_calls, [5])


class _AcceptingRegulator:
    duplication_threshold = 8
    symbol_length_threshold = 300
    base_features_threshold = 6

    def is_parsable(self, expression):
        return bool(expression)

    def evaluate(self, expression):
        return True, {
            "expr": expression,
            "duplicated_subtree_size": 0,
            "duplicated_subtree": "",
            "matched_alpha": "",
            "num_free_args": 0,
            "num_unique_vars": 1,
            "num_all_nodes": 2,
            "symbol_length": 1,
            "num_base_features": 1,
        }

    def is_expression_acceptable(self, *args, **kwargs):
        return True


def _factor_payload(name, expression):
    return json.dumps(
        {
            name: {
                "description": name,
                "variables": {"$close": "close"},
                "formulation": expression,
                "expression": expression,
            }
        }
    )


class ExactFactorCountTests(unittest.TestCase):
    def _constructor(self):
        constructor = object.__new__(
            AlphaAgentHypothesis2FactorExpression
        )
        constructor.factors_per_hypothesis = 3
        constructor.generation_max_attempts = 1
        constructor.require_exact_factor_count = True
        constructor.factor_regulator = _AcceptingRegulator()
        constructor.consistency_enabled = False
        constructor.complexity_enabled = True
        constructor.redundancy_enabled = True
        constructor._quality_gate = None
        return constructor

    @staticmethod
    def _context():
        return {
            "target_hypothesis": "test",
            "hypothesis_and_feedback": "none",
            "function_lib_description": "",
            "target_list": [],
            "RAG": None,
        }

    def test_two_or_four_factors_fail_closed(self):
        for count in (2, 4):
            with self.subTest(count=count):
                payload = {
                    f"factor_{index}": {"expression": "$close"}
                    for index in range(count)
                }
                backend = Mock()
                backend.build_messages_and_create_chat_completion.return_value = (
                    json.dumps(payload)
                )
                with patch(
                    "quantaalpha.factors.proposal.APIBackend",
                    return_value=backend,
                ):
                    with self.assertRaises(FactorEmptyError):
                        self._constructor()._generate_candidate_response(
                            hypothesis="h",
                            user_prompt="u",
                            system_prompt="s",
                            json_flag=True,
                            context=self._context(),
                        )


class VariantFamilyTests(unittest.TestCase):
    def _constructor(self):
        constructor = object.__new__(
            AlphaAgentHypothesis2FactorExpression
        )
        constructor.factors_per_hypothesis = 3
        constructor.variant_max_attempts = 2
        constructor.variant_contract = {
            "enabled": True,
            "protocol_version": "paper_family_v1",
            "allowed_features": ["$close"],
            "roles": [
                {
                    "id": "canonical",
                    "variation_axis": "none",
                    "instruction": "canonical",
                },
                {
                    "id": "normalization_robustness",
                    "variation_axis": "normalization",
                    "instruction": "normalization only",
                },
                {
                    "id": "horizon_robustness",
                    "variation_axis": "window",
                    "instruction": "window only",
                },
            ],
        }
        return constructor

    def test_failed_role_retries_without_regenerating_passed_sibling(self):
        constructor = self._constructor()
        constructor._generate_candidate_response = Mock(
            side_effect=[
                (_factor_payload("factor_a", "$close"), ["factor_a"], ["$close"]),
                FactorEmptyError("normalization failed"),
                (
                    _factor_payload("factor_b", "RANK($close)"),
                    ["factor_b"],
                    ["RANK($close)"],
                ),
                (
                    _factor_payload("factor_c", "TS_MEAN($close, 5)"),
                    ["factor_c"],
                    ["TS_MEAN($close, 5)"],
                ),
            ]
        )
        response, names, _ = constructor._generate_variant_family_response(
            hypothesis="family hypothesis",
            user_prompt="base",
            system_prompt="system",
            json_flag=True,
            context={},
        )
        calls = constructor._generate_candidate_response.call_args_list
        hypotheses = [
            call.kwargs["hypothesis"] for call in calls
        ]
        self.assertEqual(names, ["factor_a", "factor_b", "factor_c"])
        self.assertEqual(len(calls), 4)
        self.assertEqual(
            sum("variant_id: canonical" in value for value in hypotheses),
            1,
        )
        self.assertEqual(
            sum(
                "variant_id: normalization_robustness" in value
                for value in hypotheses
            ),
            2,
        )
        self.assertTrue(
            all(
                "Generate exactly one factor"
                in call.kwargs["system_prompt"]
                for call in calls
            )
        )
        payload = json.loads(response)
        roles = {
            factor["quality_gate"]["variant_contract"]["variantId"]
            for factor in payload.values()
        }
        self.assertEqual(
            roles,
            {
                "canonical",
                "normalization_robustness",
                "horizon_robustness",
            },
        )

    def test_unapproved_raw_feature_is_rejected(self):
        constructor = self._constructor()
        constructor.variant_max_attempts = 1
        constructor._generate_candidate_response = Mock(
            return_value=(
                _factor_payload("factor_a", "$open"),
                ["factor_a"],
                ["$open"],
            )
        )
        with self.assertRaises(FactorEmptyError):
            constructor._generate_variant_family_response(
                hypothesis="family hypothesis",
                user_prompt="base",
                system_prompt="system",
                json_flag=True,
                context={},
            )

    def test_variant_conversion_has_no_three_factor_prompt_conflict(self):
        constructor = self._constructor()
        constructor.targets = "factors"
        constructor.factor_regulator = SimpleNamespace(add_factor=Mock())
        constructor.prepare_context = Mock(
            return_value=(
                {
                    "target_hypothesis": "family",
                    "hypothesis_and_feedback": "",
                    "function_lib_description": "functions",
                    "experiment_output_format": (
                        "Return exactly one top-level factor object."
                    ),
                    "target_list": [],
                    "RAG": None,
                },
                True,
            )
        )
        constructor._generate_variant_family_response = Mock(
            return_value=(
                _factor_payload("factor_a", "$close"),
                ["factor_a"],
                ["$close"],
            )
        )
        constructor.convert_response = Mock(return_value="experiment")
        result = constructor._convert_with_history_limit(
            hypothesis="family",
            trace=SimpleNamespace(
                scen=SimpleNamespace(background="scenario")
            ),
            history_limit=1,
        )
        call = constructor._generate_variant_family_response.call_args.kwargs
        self.assertEqual(result, "experiment")
        self.assertIn(
            "Ensure each generation produces exactly 1 factor.",
            call["system_prompt"],
        )
        self.assertNotIn(
            "Ensure each generation produces 2-3 factors.",
            call["system_prompt"],
        )
        self.assertIn(
            "Generate exactly 1 factor.",
            call["user_prompt"],
        )
        self.assertNotIn(
            "Generate exactly 3 factors.",
            call["user_prompt"],
        )


class StructuredJsonRuntimeTests(unittest.TestCase):
    def _backend(self):
        backend = object.__new__(APIBackend)
        backend.use_chat_cache = False
        backend.dump_chat_cache = False
        backend.use_llama2 = False
        backend.use_gcr_endpoint = False
        backend.chat_model = "chat-model"
        backend.reasoning_model = "reasoning-model"
        backend.chat_model_map = {}
        backend.chat_stream = False
        backend.chat_seed = None
        response = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content='{"ok": true}'),
                    finish_reason="stop",
                )
            ]
        )
        backend.chat_client = SimpleNamespace(
            chat=SimpleNamespace(
                completions=SimpleNamespace(create=Mock(return_value=response))
            )
        )
        return backend

    def test_strict_json_preserves_response_format_and_uses_chat_model(self):
        backend = self._backend()
        old_strict = LLM_SETTINGS.structured_json_mode_strict
        old_log = LLM_SETTINGS.log_llm_chat_content
        try:
            LLM_SETTINGS.structured_json_mode_strict = True
            LLM_SETTINGS.log_llm_chat_content = False
            backend._create_chat_completion_inner_function(
                messages=[{"role": "user", "content": "json"}],
                reasoning_flag=True,
                json_mode=True,
            )
        finally:
            LLM_SETTINGS.structured_json_mode_strict = old_strict
            LLM_SETTINGS.log_llm_chat_content = old_log
        kwargs = (
            backend.chat_client.chat.completions.create.call_args.kwargs
        )
        self.assertEqual(kwargs["model"], "chat-model")
        self.assertEqual(
            kwargs["response_format"],
            {"type": "json_object"},
        )

    def test_run_config_applies_llm_controls_to_settings_and_environment(self):
        old_values = {
            "strict": LLM_SETTINGS.structured_json_mode_strict,
            "max_retry": LLM_SETTINGS.max_retry,
            "retry": LLM_SETTINGS.retry_wait_seconds,
            "timeout": LLM_SETTINGS.factor_mining_timeout,
        }
        try:
            applied = _configure_llm_runtime(
                {
                    "json_mode_strict": True,
                    "max_retries": 3,
                    "retry_delay": 1.0,
                    "factor_mining_timeout": 99,
                }
            )
            self.assertEqual(
                applied,
                {
                    "json_mode_strict": True,
                    "max_retries": 3,
                    "retry_delay": 1.0,
                    "factor_mining_timeout": 99,
                },
            )
            self.assertTrue(
                LLM_SETTINGS.structured_json_mode_strict
            )
            self.assertEqual(LLM_SETTINGS.max_retry, 3)
        finally:
            LLM_SETTINGS.structured_json_mode_strict = old_values["strict"]
            LLM_SETTINGS.max_retry = old_values["max_retry"]
            LLM_SETTINGS.retry_wait_seconds = old_values["retry"]
            LLM_SETTINGS.factor_mining_timeout = old_values["timeout"]

    def test_strict_consistency_prompt_disallows_unprescribed_changes(self):
        checker = FactorConsistencyChecker(
            enabled=True,
            strict_mode=True,
        )
        backend = Mock()
        backend.build_messages_and_create_chat_completion.return_value = (
            json.dumps({"is_consistent": True, "severity": "none"})
        )
        with patch(
            "quantaalpha.factors.regulator.consistency_checker.APIBackend",
            return_value=backend,
        ):
            checker.check_consistency(
                hypothesis="h",
                factor_name="f",
                factor_description="d",
                factor_formulation="m",
                factor_expression="$close",
            )
        system_prompt = (
            backend.build_messages_and_create_chat_completion.call_args.kwargs[
                "system_prompt"
            ]
        )
        self.assertIn("STRICT PAPER PROTOCOL OVERRIDE", system_prompt)


class PilotProfileTests(unittest.TestCase):
    def test_v3_1_schedule_is_five_trajectories_and_fifteen_factors(self):
        config = yaml.safe_load(
            (
                PROJECT_ROOT
                / "configs/experiment_paper_pilot_v3_1.yaml"
            ).read_text(encoding="utf-8")
        )
        schedule = simulate_schedule(config)
        self.assertEqual(
            schedule["phaseSequence"],
            ["original", "mutation", "crossover"],
        )
        self.assertEqual(schedule["taskCounts"], [2, 2, 1])
        self.assertEqual(sum(schedule["taskCounts"]) * 3, 15)
        self.assertEqual(schedule["llmCalls"], 0)
        self.assertEqual(
            config["planning"]["prompt_file"],
            "planning_prompts_paper_v3_1.yaml",
        )


class PilotAuditTests(unittest.TestCase):
    @staticmethod
    def _quality_gate(family_id, role):
        digest = hashlib.sha256(
            f"{family_id}-{role}".encode()
        ).hexdigest()
        return {
            "evaluated": True,
            "passed": True,
            "consistency_passed": True,
            "variant_contract": {
                "protocolVersion": "paper_family_v1",
                "familyId": family_id,
                "familyHypothesisSha256": digest,
                "variantId": role,
                "variationAxis": {
                    "canonical": "none",
                    "normalization_robustness": "normalization",
                    "horizon_robustness": "window",
                }[role],
                "variantHypothesis": "hypothesis",
                "generationAttempt": 1,
                "model": "test-model",
                "promptSha256": digest,
                "responseSha256": digest,
            },
        }

    def _write_fixture(self, root, selected_rankic=0.01, feedback=True):
        roles = [
            "canonical",
            "normalization_robustness",
            "horizon_robustness",
        ]
        trajectories = {}
        library_factors = {}
        for trajectory_index in range(5):
            family_id = f"family-{trajectory_index}"
            factors = []
            for role_index, role in enumerate(roles):
                factor_id = f"factor-{trajectory_index}-{role_index}"
                quality_gate = self._quality_gate(family_id, role)
                factor = {
                    "name": factor_id,
                    "expression": f"$close + {role_index}",
                    "quality_gate": quality_gate,
                }
                factors.append(factor)
                library_factors[factor_id] = {
                    "factor_name": factor_id,
                    "quality_gate": quality_gate,
                    "feedback": {"decision": feedback},
                }
            trajectories[f"trajectory-{trajectory_index}"] = {
                "factors": factors,
                "backtest_metrics": {"RankIC": 0.01},
            }
        trajectory_path = root / "trajectory_pool.json"
        library_path = root / "library.json"
        pool_dir = root / "pool"
        pool_dir.mkdir()
        selected_id = "factor-0-0"
        selection = {
            "selection": {
                "selectedCount": 1,
                "selectedFactors": [selected_id],
                "decisions": [
                    {
                        "factor": selected_id,
                        "meanRankIC": selected_rankic,
                    }
                ],
            }
        }
        selected = {
            "factors": {
                selected_id: library_factors[selected_id]
            }
        }
        trajectory_path.write_text(
            json.dumps({"trajectories": trajectories})
        )
        library_path.write_text(
            json.dumps({"factors": library_factors})
        )
        (pool_dir / "selection.json").write_text(
            json.dumps(selection)
        )
        (pool_dir / "selected_factor_library.json").write_text(
            json.dumps(selected)
        )
        return trajectory_path, library_path, pool_dir

    def test_complete_positive_fixture_is_forward_candidate(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            trajectory, library, pool = self._write_fixture(root)
            result = audit_paper_validation_pilot(
                PROJECT_ROOT
                / "configs/paper_validation_pilot_v3_1.yaml",
                trajectory,
                library,
                pool,
                root / "audit",
            )
        self.assertEqual(
            result["status"],
            "FORWARD_SIMULATION_CANDIDATE",
        )
        self.assertFalse(
            result["evidenceScope"]["historicalTestAccessed"]
        )
        self.assertFalse(
            result["evidenceScope"]["historicalTestEvaluated"]
        )
        self.assertTrue(
            result["evidenceScope"]["providerContainsHistoricalTestRows"]
        )
        self.assertFalse(
            result["evidenceScope"]["physicalZeroReadGuaranteed"]
        )

    def test_negative_selected_rankic_and_feedback_remain_research_only(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            trajectory, library, pool = self._write_fixture(
                root,
                selected_rankic=-0.01,
                feedback=False,
            )
            result = audit_paper_validation_pilot(
                PROJECT_ROOT
                / "configs/paper_validation_pilot_v3_1.yaml",
                trajectory,
                library,
                pool,
                root / "audit",
            )
        self.assertEqual(result["status"], "RESEARCH_ONLY")
        self.assertIn(
            "selected_validation_rankic",
            result["summary"]["failureIds"],
        )
        self.assertIn(
            "selected_feedback",
            result["summary"]["failureIds"],
        )


if __name__ == "__main__":
    unittest.main()
