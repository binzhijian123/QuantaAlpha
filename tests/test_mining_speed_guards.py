import json
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import numpy as np
import pandas as pd
import yaml

from quantaalpha.core.exception import FactorEmptyError
from quantaalpha.factors.coder.function_lib import MAX, MIN
from quantaalpha.factors.proposal import AlphaAgentHypothesis2FactorExpression
from quantaalpha.factors.regulator.consistency_checker import FactorConsistencyChecker


class MaxMinContractTests(unittest.TestCase):
    def setUp(self):
        index = pd.MultiIndex.from_tuples(
            [
                ("2024-01-02", "A"),
                ("2024-01-02", "B"),
                ("2024-01-03", "A"),
                ("2024-01-03", "B"),
            ],
            names=["datetime", "instrument"],
        )
        self.values = pd.Series([1.0, 3.0, 4.0, np.nan], index=index)

    def test_unary_max_min_broadcast_cross_sectionally(self):
        expected_max = pd.Series([3.0, 3.0, 4.0, 4.0], index=self.values.index)
        expected_min = pd.Series([1.0, 1.0, 4.0, 4.0], index=self.values.index)
        pd.testing.assert_series_equal(MAX(self.values), expected_max)
        pd.testing.assert_series_equal(MIN(self.values), expected_min)

    def test_binary_and_ternary_keep_elementwise_contract(self):
        left = pd.Series([1.0, np.nan, 4.0], index=["a", "b", "c"])
        right = pd.Series([2.0, 3.0, 1.0], index=left.index)
        third = pd.Series([0.0, 5.0, 6.0], index=left.index)
        pd.testing.assert_series_equal(MAX(left, right), np.maximum(left, right))
        pd.testing.assert_series_equal(MIN(left, right), np.minimum(left, right))
        pd.testing.assert_series_equal(MAX(left, right, third), np.maximum(np.maximum(left, right), third))
        pd.testing.assert_series_equal(MIN(left, right, third), np.minimum(np.minimum(left, right), third))

    def test_invalid_arities_fail_fast(self):
        with self.assertRaises(TypeError):
            MAX()
        with self.assertRaises(TypeError):
            MIN(1, 2, 3, 4)


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


class FactorGenerationBudgetTests(unittest.TestCase):
    def _constructor(self, max_attempts):
        constructor = object.__new__(AlphaAgentHypothesis2FactorExpression)
        constructor.factors_per_hypothesis = 1
        constructor.generation_max_attempts = max_attempts
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

    def test_invalid_json_stops_at_configured_budget(self):
        backend = Mock()
        backend.build_messages_and_create_chat_completion.return_value = "not-json"
        constructor = self._constructor(3)
        with patch("quantaalpha.factors.proposal.APIBackend", return_value=backend):
            with self.assertRaises(FactorEmptyError):
                constructor._generate_candidate_response(
                    hypothesis="h",
                    user_prompt="u",
                    system_prompt="s",
                    json_flag=True,
                    context=self._context(),
                )
        self.assertEqual(backend.build_messages_and_create_chat_completion.call_count, 3)

    def test_non_object_json_stays_inside_retry_budget(self):
        for payload in ("[]", "[{}]", "42", '"factor"'):
            with self.subTest(payload=payload):
                backend = Mock()
                backend.build_messages_and_create_chat_completion.return_value = payload
                constructor = self._constructor(2)
                with patch("quantaalpha.factors.proposal.APIBackend", return_value=backend):
                    with self.assertRaises(FactorEmptyError):
                        constructor._generate_candidate_response(
                            hypothesis="h",
                            user_prompt="u",
                            system_prompt="s",
                            json_flag=True,
                            context=self._context(),
                        )
                self.assertEqual(backend.build_messages_and_create_chat_completion.call_count, 2)

    def test_non_object_json_can_recover_on_later_attempt(self):
        valid = json.dumps({"factor_a": {"expression": "$close"}})
        backend = Mock()
        backend.build_messages_and_create_chat_completion.side_effect = ["[]", valid]
        constructor = self._constructor(2)
        with patch("quantaalpha.factors.proposal.APIBackend", return_value=backend):
            _, names, expressions = constructor._generate_candidate_response(
                hypothesis="h",
                user_prompt="u",
                system_prompt="s",
                json_flag=True,
                context=self._context(),
            )
        self.assertEqual(names, ["factor_a"])
        self.assertEqual(expressions, ["$close"])
        self.assertEqual(backend.build_messages_and_create_chat_completion.call_count, 2)

    def test_legacy_constructor_without_budget_uses_safe_default(self):
        valid = json.dumps({"factor_a": {"expression": "$close"}})
        backend = Mock()
        backend.build_messages_and_create_chat_completion.return_value = valid
        constructor = self._constructor(5)
        del constructor.generation_max_attempts
        with patch("quantaalpha.factors.proposal.APIBackend", return_value=backend):
            _, names, expressions = constructor._generate_candidate_response(
                hypothesis="h",
                user_prompt="u",
                system_prompt="s",
                json_flag=True,
                context=self._context(),
            )
        self.assertEqual(names, ["factor_a"])
        self.assertEqual(expressions, ["$close"])
        self.assertEqual(backend.build_messages_and_create_chat_completion.call_count, 1)

    def test_consistency_rejection_is_fail_closed(self):
        valid = json.dumps({"factor_a": {"expression": "$close"}})
        backend = Mock()
        backend.build_messages_and_create_chat_completion.return_value = valid
        constructor = self._constructor(2)
        constructor.consistency_enabled = True
        constructor._quality_gate = Mock()
        constructor._quality_gate.evaluate.return_value = (
            False,
            "hypothesis and expression diverge",
            {},
        )

        with patch("quantaalpha.factors.proposal.APIBackend", return_value=backend):
            with self.assertRaises(FactorEmptyError):
                constructor._generate_candidate_response(
                    hypothesis="h",
                    user_prompt="u",
                    system_prompt="s",
                    json_flag=True,
                    context=self._context(),
                )

        self.assertEqual(backend.build_messages_and_create_chat_completion.call_count, 2)

    def test_consistency_correction_is_persisted_in_response(self):
        valid = json.dumps(
            {"factor_a": {"expression": "$close", "description": "original"}}
        )
        backend = Mock()
        backend.build_messages_and_create_chat_completion.return_value = valid
        constructor = self._constructor(1)
        constructor.consistency_enabled = True
        constructor._quality_gate = Mock()
        constructor._quality_gate.evaluate.return_value = (
            True,
            "corrected",
            {
                "corrected_expression": "-$close",
                "corrected_description": "corrected description",
            },
        )

        with patch("quantaalpha.factors.proposal.APIBackend", return_value=backend):
            response, _, expressions = constructor._generate_candidate_response(
                hypothesis="h",
                user_prompt="u",
                system_prompt="s",
                json_flag=True,
                context=self._context(),
            )

        payload = json.loads(response)["factor_a"]
        self.assertEqual(expressions, ["-$close"])
        self.assertEqual(payload["expression"], "-$close")
        self.assertEqual(payload["description"], "corrected description")

    def test_consistency_checker_error_is_fail_closed(self):
        checker = FactorConsistencyChecker(enabled=True)
        backend = Mock()
        backend.build_messages_and_create_chat_completion.side_effect = RuntimeError("offline")

        with patch(
            "quantaalpha.factors.regulator.consistency_checker.APIBackend",
            return_value=backend,
        ):
            result = checker.check_consistency(
                hypothesis="h",
                factor_name="factor_a",
                factor_description="d",
                factor_formulation="f",
                factor_expression="$close",
            )

        self.assertFalse(result.is_consistent)
        self.assertEqual(result.severity, "critical")
        self.assertFalse(checker.should_proceed_to_backtest(result))

    def test_last_allowed_attempt_can_succeed_unchanged(self):
        valid = json.dumps({"factor_a": {"expression": "$close"}})
        backend = Mock()
        backend.build_messages_and_create_chat_completion.side_effect = ["{}", "bad", valid]
        constructor = self._constructor(3)
        with patch("quantaalpha.factors.proposal.APIBackend", return_value=backend):
            response, names, expressions = constructor._generate_candidate_response(
                hypothesis="h",
                user_prompt="u",
                system_prompt="s",
                json_flag=True,
                context=self._context(),
            )
        self.assertEqual(response, valid)
        self.assertEqual(names, ["factor_a"])
        self.assertEqual(expressions, ["$close"])
        self.assertEqual(backend.build_messages_and_create_chat_completion.call_count, 3)

    def test_yaml_budget_is_explicit(self):
        config_path = Path(__file__).resolve().parents[1] / "configs" / "experiment.yaml"
        config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        self.assertEqual(config["factor"]["generation_max_attempts"], 5)

if __name__ == "__main__":
    unittest.main()
