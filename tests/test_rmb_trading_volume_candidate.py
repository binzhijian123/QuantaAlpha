from __future__ import annotations

import hashlib
import json
import unittest
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

from quantaalpha.backtest.custom_factor_calculator import CustomFactorCalculator
from quantaalpha.factors.coder.function_lib import TS_MEAN_MIN_OBS


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CANDIDATE_PATH = (
    PROJECT_ROOT / "configs" / "profit_candidates_v2_5_investment_data_rtv1.json"
)
VALIDATION_PATH = (
    PROJECT_ROOT / "configs" / "profit_validation_v2_5_investment_data_rtv1.yaml"
)
BACKTEST_PATH = (
    PROJECT_ROOT
    / "configs"
    / "backtest_profit_validation_v2_5_investment_data_rtv1.yaml"
)
R2_VALIDATION_PATH = (
    PROJECT_ROOT / "configs" / "profit_validation_v2_5_investment_data_rtv1_r2.yaml"
)
R2_BACKTEST_PATH = (
    PROJECT_ROOT
    / "configs"
    / "backtest_profit_validation_v2_5_investment_data_rtv1_r2.yaml"
)
BASE_SOURCE_PATH = (
    PROJECT_ROOT / "data" / "factorlib" / "profit_trials_v2_4_investment_data_mdr1_source.json"
)
SOURCE_PATH = (
    PROJECT_ROOT / "data" / "factorlib" / "profit_trials_v2_5_investment_data_rtv1_source.json"
)
R2_PROVIDER_PATH = (
    PROJECT_ROOT / "data" / "factorlib" / "profit_trials_v2_5_investment_data_rtv1_provider_r2.json"
)
R2_AUDIT_PATH = R2_PROVIDER_PATH.with_suffix(".audit.json")


class RmbTradingVolumeCandidateTests(unittest.TestCase):
    def test_minimum_observation_mean_stays_missing_until_threshold(self):
        index = pd.MultiIndex.from_arrays(
            [
                ["SH000001"] * 5 + ["SZ000001"] * 3,
                pd.date_range("2021-01-01", periods=5).tolist()
                + pd.date_range("2021-01-01", periods=3).tolist(),
            ],
            names=["instrument", "datetime"],
        )
        amount = pd.Series(
            [1.0, np.nan, 2.0, 3.0, 4.0, 10.0, 20.0, 30.0],
            index=index,
        )

        result = TS_MEAN_MIN_OBS(amount, 4, 3)

        first = result.xs("SH000001", level="instrument")
        second = result.xs("SZ000001", level="instrument")
        self.assertTrue(first.iloc[:3].isna().all())
        self.assertAlmostEqual(float(first.iloc[3]), 2.0)
        self.assertAlmostEqual(float(first.iloc[4]), 3.0)
        self.assertTrue(second.iloc[:2].isna().all())
        self.assertAlmostEqual(float(second.iloc[2]), 20.0)

    def test_minimum_observation_mean_rejects_invalid_contract(self):
        index = pd.MultiIndex.from_tuples(
            [("SH000001", pd.Timestamp("2021-01-01"))],
            names=["instrument", "datetime"],
        )
        amount = pd.Series([1.0], index=index)

        for window, minimum in ((0, 1), (5, 0), (5, 6)):
            with self.subTest(window=window, minimum=minimum):
                with self.assertRaises(ValueError):
                    TS_MEAN_MIN_OBS(amount, window, minimum)

    def test_registered_expression_runs_through_parser_and_calculator(self):
        index = pd.MultiIndex.from_product(
            [
                ["SH600000"],
                pd.date_range("2021-01-01", periods=21, freq="B"),
            ],
            names=["instrument", "datetime"],
        )
        data = pd.DataFrame(
            {
                "$close": np.arange(21, dtype=float) + 10.0,
                "$amount": np.arange(21, dtype=float) + 1.0,
            },
            index=index,
        )

        result = CustomFactorCalculator(
            data,
            auto_extract_cache=False,
        ).calculate_factor(
            "NegRmbTradingVolume21",
            "-TS_MEAN_MIN_OBS($amount, 21, 16)",
        )

        self.assertIsNotNone(result)
        self.assertTrue(result.iloc[:15].isna().all())
        self.assertAlmostEqual(float(result.iloc[15]), -8.5)
        self.assertAlmostEqual(float(result.iloc[-1]), -11.0)

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
        self.assertTrue(metadata["related_volume_mechanisms_previously_tested"])
        self.assertFalse(metadata["exact_amount_factor_previously_tested"])
        self.assertEqual(metadata["admission_factor_ids"], [factor_id])
        self.assertEqual(factor_id, expected_id)
        self.assertEqual(factor_id, "61c75b5b09f24f51")
        self.assertEqual(
            factor["factor_expression"],
            "-TS_MEAN_MIN_OBS($amount, 21, 16)",
        )
        self.assertFalse(factor["feedback"]["decision"])
        self.assertTrue(factor["quality_gate"]["consistency_check"]["is_consistent"])

    def test_validation_and_runtime_share_the_frozen_contract(self):
        registry = json.loads(CANDIDATE_PATH.read_text(encoding="utf-8"))
        gate = yaml.safe_load(VALIDATION_PATH.read_text(encoding="utf-8"))
        runtime = yaml.safe_load(BACKTEST_PATH.read_text(encoding="utf-8"))
        metadata = registry["metadata"]

        self.assertEqual(gate["protocol_version"], "2.5")
        self.assertEqual(gate["trial_matrix"]["expected_factor_count"], 21)
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
        self.assertEqual(runtime["data"]["additional_fields"], ["amount"])
        self.assertEqual(gate["historical_provider"], runtime["data"]["provider_uri"])
        self.assertEqual(gate["historical_provider"], metadata["provider_path"])
        self.assertEqual(gate["promotion"]["multiple_testing"]["total_trials"], 30)
        self.assertEqual(gate["promotion"]["multiple_testing"]["matrix_trials"], 21)
        self.assertEqual(len(gate["validation"]["reference_cached_factor_ids"]), 20)
        self.assertIn(
            "c2672568e33f0849",
            gate["validation"]["reference_cached_factor_ids"],
        )

    @unittest.skipUnless(all(path.is_file() for path in (BASE_SOURCE_PATH, SOURCE_PATH,)), "External research artifacts are not bundled with source; provide the original factor library and audit files to run this integration check.")
    def test_source_preserves_frozen_reference_order_and_appends_candidate(self):
        base = json.loads(BASE_SOURCE_PATH.read_text(encoding="utf-8"))
        source = json.loads(SOURCE_PATH.read_text(encoding="utf-8"))
        base_ids = list(base["factors"])
        source_ids = list(source["factors"])

        self.assertEqual(len(base_ids), 20)
        self.assertEqual(source_ids[:20], base_ids)
        self.assertEqual(source_ids[20:], ["61c75b5b09f24f51"])
        self.assertEqual(source["metadata"]["base_factor_count"], 20)
        self.assertEqual(source["metadata"]["total_factors"], 21)

    @unittest.skipUnless(all(path.is_file() for path in (R2_BACKTEST_PATH, R2_VALIDATION_PATH, SOURCE_PATH, VALIDATION_PATH,)), "External research artifacts are not bundled with source; provide the original factor library and audit files to run this integration check.")
    def test_r2_is_only_a_warmup_and_reference_tightening_amendment(self):
        original = yaml.safe_load(VALIDATION_PATH.read_text(encoding="utf-8"))
        revised = yaml.safe_load(R2_VALIDATION_PATH.read_text(encoding="utf-8"))
        runtime = yaml.safe_load(R2_BACKTEST_PATH.read_text(encoding="utf-8"))
        source_ids = list(
            json.loads(SOURCE_PATH.read_text(encoding="utf-8"))["factors"]
        )

        self.assertEqual(revised["technical_amendment"]["revision"], "r2")
        self.assertEqual(
            revised["technical_amendment"]["cache_calculation_start"],
            "2014-01-01",
        )
        self.assertEqual(
            revised["validation"]["reference_cached_factor_ids"],
            source_ids[:20],
        )
        self.assertEqual(
            revised["validation"]["reference_factor_expressions"],
            {
                "Amihud20HistoricalProxy": (
                    "Mean(Abs($close / Ref($close, 1) - 1) / "
                    "(($volume * $close) + 1e-8), 20)"
                )
            },
        )
        for key in (
            "trial_matrix",
            "promotion",
            "walk_forward",
            "cost_multipliers",
        ):
            self.assertEqual(revised[key], original[key])
        for key in (
            "label",
            "label_horizon_trading_days",
            "return_holding_period_trading_days",
            "correlation_threshold",
            "factor_coverage_min",
            "rank_ic_min",
            "admission_factor_ids",
        ):
            self.assertEqual(revised["validation"][key], original["validation"][key])
        self.assertEqual(revised["validation"]["label"], runtime["dataset"]["label"])
        self.assertEqual(runtime["data"]["start_time"], "2016-01-01")
        self.assertEqual(runtime["data"]["additional_fields"], ["amount"])

    @unittest.skipUnless(all(path.is_file() for path in (R2_AUDIT_PATH, R2_PROVIDER_PATH, SOURCE_PATH,)), "External research artifacts are not bundled with source; provide the original factor library and audit files to run this integration check.")
    def test_r2_provider_has_warmup_and_nonempty_first_training_session(self):
        audit = json.loads(R2_AUDIT_PATH.read_text(encoding="utf-8"))
        provider = json.loads(R2_PROVIDER_PATH.read_text(encoding="utf-8"))
        metadata = provider["metadata"]
        candidate = provider["factors"]["61c75b5b09f24f51"]

        self.assertEqual(audit["status"], "PASS")
        self.assertEqual(audit["sourceFactorCount"], 21)
        self.assertEqual(audit["preparedFactorCount"], 21)
        self.assertEqual(audit["additionalRawFields"], ["amount"])
        self.assertEqual(metadata["calculation_range"][0], "2014-01-01")
        self.assertEqual(metadata["additional_raw_fields"], ["amount"])
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
        self.assertGreater(int(first_training_session.notna().sum()), 0)


if __name__ == "__main__":
    unittest.main()
