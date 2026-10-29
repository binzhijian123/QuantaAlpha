from __future__ import annotations

import hashlib
import json
import unittest
from pathlib import Path

import yaml


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CANDIDATE_PATH = (
    PROJECT_ROOT / "configs" / "profit_candidates_v2_4_investment_data_mdr1.json"
)
VALIDATION_PATH = (
    PROJECT_ROOT / "configs" / "profit_validation_v2_4_investment_data_mdr1.yaml"
)
BACKTEST_PATH = (
    PROJECT_ROOT
    / "configs"
    / "backtest_profit_validation_v2_4_investment_data_mdr1.yaml"
)


class MaximumDailyReturnCandidateTests(unittest.TestCase):
    def test_registry_is_deterministic_and_preregistered(self):
        registry = json.loads(CANDIDATE_PATH.read_text(encoding="utf-8"))
        metadata = registry["metadata"]
        factor_id, factor = next(iter(registry["factors"].items()))

        expected_id = hashlib.sha256(
            (
                f"{metadata['factor_id_namespace']}:"
                f"{factor['factor_name']}:{factor['factor_expression']}"
            ).encode()
        ).hexdigest()[:16]

        self.assertEqual(metadata["current_state"], "PREREGISTERED")
        self.assertTrue(metadata["created_before_candidate_factor_derivation"])
        self.assertTrue(metadata["created_before_candidate_strategy_backtest"])
        self.assertFalse(metadata["candidate_factor_values_viewed_before_registration"])
        self.assertFalse(metadata["candidate_forward_returns_viewed_before_registration"])
        self.assertEqual(metadata["admission_factor_ids"], [factor_id])
        self.assertEqual(factor_id, expected_id)
        self.assertEqual(factor_id, "c2672568e33f0849")
        self.assertEqual(
            factor["factor_expression"],
            "-TS_MAX(TS_PCTCHANGE($close, 1), 21)",
        )
        self.assertFalse(factor["feedback"]["decision"])
        self.assertTrue(factor["quality_gate"]["consistency_check"]["is_consistent"])

    def test_validation_and_runtime_share_the_frozen_contract(self):
        registry = json.loads(CANDIDATE_PATH.read_text(encoding="utf-8"))
        gate = yaml.safe_load(VALIDATION_PATH.read_text(encoding="utf-8"))
        runtime = yaml.safe_load(BACKTEST_PATH.read_text(encoding="utf-8"))
        metadata = registry["metadata"]

        self.assertEqual(gate["protocol_version"], "2.4")
        self.assertEqual(gate["trial_matrix"]["expected_factor_count"], 20)
        self.assertEqual(
            gate["trial_matrix"]["target_factor_id"],
            metadata["admission_factor_ids"][0],
        )
        self.assertEqual(gate["validation"]["label"], runtime["dataset"]["label"])
        self.assertEqual(gate["validation"]["factor_coverage_min"], 0.95)
        self.assertEqual(
            gate["validation"]["return_holding_period_trading_days"],
            runtime["backtest"]["strategy"]["kwargs"]["hold_thresh"],
        )
        self.assertEqual(runtime["backtest"]["strategy"]["kwargs"]["n_drop"], 1)
        self.assertEqual(runtime["backtest"]["strategy"]["kwargs"]["topk"], 50)
        self.assertEqual(
            gate["historical_provider"], runtime["data"]["provider_uri"]
        )
        self.assertEqual(gate["historical_provider"], metadata["provider_path"])
        self.assertEqual(
            gate["promotion"]["multiple_testing"]["total_trials"], 29
        )
        self.assertEqual(
            gate["promotion"]["multiple_testing"]["matrix_trials"], 20
        )
        self.assertEqual(
            len(gate["validation"]["reference_cached_factor_ids"]), 19
        )


if __name__ == "__main__":
    unittest.main()
