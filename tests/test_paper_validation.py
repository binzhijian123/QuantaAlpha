import unittest
from unittest.mock import MagicMock, patch

import yaml

from quantaalpha.factors.regulator.consistency_checker import (
    ConsistencyCheckResult,
    FactorConsistencyChecker,
)
from quantaalpha.factors.regulator.factor_regulator import FactorRegulator
from quantaalpha.validation.paper import (
    PROJECT_ROOT,
    Preflight,
    _check_algorithm_fidelity,
    _effective_llm_configuration,
    probe_llm_auth,
    simulate_schedule,
)


class PaperValidationTest(unittest.TestCase):
    def test_paper_profile_maps_to_five_complete_evolution_cycles(self):
        config = yaml.safe_load(
            (PROJECT_ROOT / "configs/experiment_paper_v3.yaml").read_text(
                encoding="utf-8"
            )
        )
        schedule = simulate_schedule(config)
        self.assertEqual(
            schedule["phaseSequence"],
            ["original"] + ["mutation", "crossover"] * 5,
        )
        self.assertEqual(schedule["rounds"], 11)
        self.assertEqual(schedule["llmCalls"], 0)
        self.assertEqual(schedule["taskCounts"][:3], [10, 10, 3])
        self.assertEqual(sum(schedule["taskCounts"]) * 3, 141)

    def test_paper_unique_variable_gate_can_be_disabled_without_weakening_free_args(self):
        regulator = FactorRegulator(
            free_args_ratio_threshold=0.5,
            unique_vars_ratio_threshold=1.0,
            symbol_length_threshold=250,
            base_features_threshold=6,
        )
        evaluation = {
            "expr": "$open + $close",
            "duplicated_subtree_size": 0,
            "num_free_args": 1,
            "num_unique_vars": 2,
            "num_all_nodes": 3,
            "symbol_length": 10,
            "num_base_features": 2,
        }
        self.assertTrue(regulator.is_expression_acceptable(evaluation))
        evaluation["num_free_args"] = 2
        self.assertFalse(regulator.is_expression_acceptable(evaluation))

    def test_consistency_corrections_can_run_but_final_pass_is_required(self):
        checker = FactorConsistencyChecker(
            enabled=True,
            strict_mode=False,
            require_consistent=True,
        )
        result = ConsistencyCheckResult(
            is_consistent=False,
            hypothesis_to_description="minor mismatch",
            description_to_formulation="",
            formulation_to_expression="",
            overall_feedback="minor mismatch",
            severity="minor",
        )
        self.assertFalse(checker.should_proceed_to_backtest(result))

    def test_auth_probe_never_returns_api_key(self):
        response = MagicMock()
        response.status = 200
        response.__enter__.return_value = response
        with patch(
            "quantaalpha.validation.paper.urllib.request.urlopen",
            return_value=response,
        ):
            result = probe_llm_auth("https://example.test/v1", "secret-key")
        self.assertTrue(result["ok"])
        self.assertNotIn("secret-key", str(result))

    def test_project_dotenv_credentials_override_ambient_environment(self):
        configuration = _effective_llm_configuration(
            dotenv={
                "CHAT_MODEL": "project-model",
                "OPENAI_API_KEY": "project-key",
                "OPENAI_BASE_URL": "https://project.example",
            },
            environ={
                "CHAT_MODEL": "ambient-model",
                "OPENAI_API_KEY": "ambient-key",
                "OPENAI_BASE_URL": "https://ambient.example",
            },
        )
        self.assertEqual(configuration["chat_model"], "project-model")
        self.assertEqual(configuration["api_key"], "project-key")
        self.assertEqual(
            configuration["base_url"],
            "https://project.example",
        )

    def test_preflight_accepts_all_local_paper_algorithm_contracts(self):
        preflight = Preflight()
        _check_algorithm_fidelity(preflight)
        self.assertEqual(preflight.hard_failures(), [])


if __name__ == "__main__":
    unittest.main()
