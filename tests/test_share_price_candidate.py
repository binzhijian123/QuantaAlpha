from __future__ import annotations

import hashlib
import json
import unittest
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

from quantaalpha.backtest.custom_factor_calculator import CustomFactorCalculator
from quantaalpha.factors.coder.function_lib import MASK


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CANDIDATE_PATH = (
    PROJECT_ROOT / "configs" / "profit_candidates_v2_6_investment_data_pps1.json"
)
VALIDATION_PATH = (
    PROJECT_ROOT / "configs" / "profit_validation_v2_6_investment_data_pps1.yaml"
)
BACKTEST_PATH = (
    PROJECT_ROOT
    / "configs"
    / "backtest_profit_validation_v2_6_investment_data_pps1.yaml"
)
BASE_SOURCE_PATH = (
    PROJECT_ROOT
    / "data"
    / "factorlib"
    / "profit_trials_v2_5_investment_data_rtv1_source.json"
)
SOURCE_PATH = (
    PROJECT_ROOT
    / "data"
    / "factorlib"
    / "profit_trials_v2_6_investment_data_pps1_source.json"
)
PROVIDER_PATH = (
    PROJECT_ROOT
    / "data"
    / "factorlib"
    / "profit_trials_v2_6_investment_data_pps1_provider.json"
)
PROVIDER_AUDIT_PATH = PROVIDER_PATH.with_suffix(".audit.json")
FACTOR_ID = "9ed74ff7c9df603e"
EXPRESSION = "MASK(-1 * ($close / $factor), ($close / $factor) >= 5)"


class SharePriceCandidateTests(unittest.TestCase):
    def test_mask_preserves_index_and_marks_ineligible_values_missing(self):
        index = pd.MultiIndex.from_product(
            [["SH600000"], pd.date_range("2021-01-01", periods=3, freq="B")],
            names=["instrument", "datetime"],
        )
        values = pd.Series([1.0, 2.0, 3.0], index=index)

        result = MASK(values, values >= 2.0)

        self.assertTrue(result.index.equals(index))
        self.assertTrue(np.isnan(result.iloc[0]))
        self.assertEqual(result.iloc[1:].tolist(), [2.0, 3.0])

    def test_registered_expression_reconstructs_price_and_applies_threshold(self):
        index = pd.MultiIndex.from_tuples(
            [
                ("SH600000", pd.Timestamp("2021-01-04")),
                ("SH600001", pd.Timestamp("2021-01-04")),
                ("SH600002", pd.Timestamp("2021-01-04")),
            ],
            names=["instrument", "datetime"],
        )
        factor = pd.Series([0.5, 0.5, 2.0], index=index)
        raw_price = pd.Series([4.99, 5.0, 20.0], index=index)
        data = pd.DataFrame(
            {
                "$close": raw_price * factor,
                "$factor": factor,
            },
            index=index,
        )

        result = CustomFactorCalculator(
            data,
            auto_extract_cache=False,
        ).calculate_factor("Pps1LowNominalPriceProxy", EXPRESSION)

        self.assertIsNotNone(result)
        self.assertTrue(np.isnan(result.iloc[0]))
        self.assertAlmostEqual(float(result.iloc[1]), -5.0)
        self.assertAlmostEqual(float(result.iloc[2]), -20.0)
        self.assertGreater(float(result.iloc[1]), float(result.iloc[2]))

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
        self.assertFalse(metadata["exact_nominal_share_price_factor_previously_tested"])
        self.assertEqual(metadata["admission_factor_ids"], [FACTOR_ID])
        self.assertEqual(factor_id, expected_id)
        self.assertEqual(factor_id, FACTOR_ID)
        self.assertEqual(factor["factor_expression"], EXPRESSION)
        self.assertFalse(factor["feedback"]["decision"])
        self.assertTrue(factor["quality_gate"]["consistency_check"]["is_consistent"])

    def test_validation_and_runtime_share_frozen_contract(self):
        registry = json.loads(CANDIDATE_PATH.read_text(encoding="utf-8"))
        gate = yaml.safe_load(VALIDATION_PATH.read_text(encoding="utf-8"))
        runtime = yaml.safe_load(BACKTEST_PATH.read_text(encoding="utf-8"))
        metadata = registry["metadata"]

        self.assertEqual(gate["protocol_version"], "2.6")
        self.assertEqual(gate["trial_matrix"]["expected_factor_count"], 22)
        self.assertEqual(gate["trial_matrix"]["target_factor_id"], FACTOR_ID)
        self.assertEqual(gate["validation"]["admission_factor_ids"], [FACTOR_ID])
        self.assertEqual(gate["validation"]["label"], runtime["dataset"]["label"])
        self.assertEqual(gate["validation"]["factor_coverage_min"], 0.95)
        self.assertEqual(
            gate["validation"]["return_holding_period_trading_days"],
            runtime["backtest"]["strategy"]["kwargs"]["hold_thresh"],
        )
        self.assertEqual(runtime["backtest"]["strategy"]["kwargs"]["n_drop"], 1)
        self.assertEqual(runtime["backtest"]["strategy"]["kwargs"]["topk"], 50)
        self.assertEqual(runtime["data"]["additional_fields"], ["amount", "factor"])
        self.assertEqual(gate["historical_provider"], runtime["data"]["provider_uri"])
        self.assertEqual(gate["historical_provider"], metadata["provider_path"])
        self.assertEqual(gate["promotion"]["multiple_testing"]["total_trials"], 31)
        self.assertEqual(gate["promotion"]["multiple_testing"]["matrix_trials"], 22)
        self.assertEqual(len(gate["validation"]["reference_cached_factor_ids"]), 21)
        self.assertEqual(
            gate["validation"]["reference_cached_factor_ids"][-1],
            "61c75b5b09f24f51",
        )

    @unittest.skipUnless(all(path.is_file() for path in (BASE_SOURCE_PATH, SOURCE_PATH,)), "External research artifacts are not bundled with source; provide the original factor library and audit files to run this integration check.")
    def test_source_preserves_v2_5_order_and_appends_candidate(self):
        base = json.loads(BASE_SOURCE_PATH.read_text(encoding="utf-8"))
        source = json.loads(SOURCE_PATH.read_text(encoding="utf-8"))
        base_ids = list(base["factors"])
        source_ids = list(source["factors"])

        self.assertEqual(len(base_ids), 21)
        self.assertEqual(source_ids[:21], base_ids)
        self.assertEqual(source_ids[21:], [FACTOR_ID])
        self.assertEqual(source["metadata"]["base_factor_count"], 21)
        self.assertEqual(source["metadata"]["total_factors"], 22)

    @unittest.skipUnless(all(path.is_file() for path in (PROVIDER_AUDIT_PATH, PROVIDER_PATH, SOURCE_PATH,)), "External research artifacts are not bundled with source; provide the original factor library and audit files to run this integration check.")
    def test_provider_cache_is_complete_and_bound_to_factor_field(self):
        audit = json.loads(PROVIDER_AUDIT_PATH.read_text(encoding="utf-8"))
        provider = json.loads(PROVIDER_PATH.read_text(encoding="utf-8"))
        metadata = provider["metadata"]
        candidate = provider["factors"][FACTOR_ID]

        self.assertEqual(audit["status"], "PASS")
        self.assertEqual(audit["sourceFactorCount"], 22)
        self.assertEqual(audit["preparedFactorCount"], 22)
        self.assertEqual(audit["additionalRawFields"], ["amount", "factor"])
        self.assertEqual(metadata["calculation_range"][0], "2014-01-01")
        self.assertEqual(metadata["additional_raw_fields"], ["amount", "factor"])
        self.assertEqual(list(provider["factors"]), list(json.loads(
            SOURCE_PATH.read_text(encoding="utf-8")
        )["factors"]))

        cache_path = Path(candidate["cache_location"]["result_h5_path"])
        self.assertTrue(cache_path.is_file())
        values = pd.read_hdf(cache_path)
        if isinstance(values, pd.DataFrame):
            values = values.iloc[:, 0]
        first_training_session = values.xs(
            pd.Timestamp("2016-01-04"),
            level="datetime",
        )
        reference_path = Path(
            provider["factors"]["61c75b5b09f24f51"]["cache_location"][
                "result_h5_path"
            ]
        )
        reference = pd.read_hdf(reference_path)
        if isinstance(reference, pd.DataFrame):
            reference = reference.iloc[:, 0]
        reference_session = reference.xs(
            pd.Timestamp("2016-01-04"),
            level="datetime",
        )
        eligible_count = int(first_training_session.notna().sum())
        active_count = int(reference_session.notna().sum())
        self.assertGreaterEqual(eligible_count, 50)
        self.assertGreaterEqual(eligible_count / active_count, 0.95)


if __name__ == "__main__":
    unittest.main()
