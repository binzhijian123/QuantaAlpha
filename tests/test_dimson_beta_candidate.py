from __future__ import annotations

import hashlib
import json
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import numpy as np
import pandas as pd
import yaml

from quantaalpha.backtest.custom_factor_calculator import (
    CustomFactorCalculator,
    get_qlib_stock_data,
)
from quantaalpha.factors.coder.function_lib import MONTHLY_DIMSON_BETA


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CANDIDATE_PATH = (
    PROJECT_ROOT / "configs" / "profit_candidates_v2_8_investment_data_beta3m.json"
)
VALIDATION_PATH = (
    PROJECT_ROOT / "configs" / "profit_validation_v2_8_investment_data_beta3m.yaml"
)
BACKTEST_PATH = (
    PROJECT_ROOT
    / "configs"
    / "backtest_profit_validation_v2_8_investment_data_beta3m.yaml"
)
BASE_SOURCE_PATH = (
    PROJECT_ROOT
    / "data"
    / "factorlib"
    / "profit_trials_v2_7_investment_data_ra25_source.json"
)
SOURCE_PATH = (
    PROJECT_ROOT
    / "data"
    / "factorlib"
    / "profit_trials_v2_8_investment_data_beta3m_source.json"
)
PROVIDER_PATH = (
    PROJECT_ROOT
    / "data"
    / "factorlib"
    / "profit_trials_v2_8_investment_data_beta3m_provider.json"
)
PROVIDER_AUDIT_PATH = PROVIDER_PATH.with_suffix(".audit.json")
FACTOR_ID = "1b7913109f79fb5a"
EXPRESSION = "MONTHLY_DIMSON_BETA($close, $benchmark_close, 3, 20)"


def _synthetic_panel(end: str = "2021-05-31") -> pd.DataFrame:
    dates = pd.bdate_range("2020-11-02", end)
    phase = np.arange(len(dates), dtype=np.float64)
    market_return = pd.Series(
        0.001 + 0.006 * np.sin(phase / 3.7) + 0.003 * np.cos(phase / 7.1),
        index=dates,
    )
    stock_return = (
        0.2 * market_return.shift(1)
        + 0.5 * market_return
        + 0.3 * market_return.shift(-1)
    ).fillna(0.0)
    market_close = 100.0 * (1.0 + market_return).cumprod()
    stock_close = 50.0 * (1.0 + stock_return).cumprod()
    index = pd.MultiIndex.from_product(
        [["SH600000"], dates],
        names=["instrument", "datetime"],
    )
    return pd.DataFrame(
        {
            "$close": stock_close.to_numpy(),
            "$benchmark_close": market_close.to_numpy(),
        },
        index=index,
    )


class MonthlyDimsonBetaOperatorTests(unittest.TestCase):
    def test_prior_three_month_beta_is_constant_in_target_month(self):
        data = _synthetic_panel()

        result = MONTHLY_DIMSON_BETA(
            data["$close"], data["$benchmark_close"], 3, 20
        )

        april = result.loc[("SH600000", slice("2021-04-01", "2021-04-30"))]
        self.assertGreater(len(april), 0)
        np.testing.assert_allclose(april.to_numpy(), 1.0, atol=1e-10)

    def test_future_extension_cannot_change_earlier_month(self):
        initial_data = _synthetic_panel("2021-04-30")
        extended_data = _synthetic_panel("2021-05-31")

        initial = MONTHLY_DIMSON_BETA(
            initial_data["$close"], initial_data["$benchmark_close"], 3, 20
        )
        extended = MONTHLY_DIMSON_BETA(
            extended_data["$close"], extended_data["$benchmark_close"], 3, 20
        )

        initial_april = initial.loc[("SH600000", slice("2021-04-01", "2021-04-30"))]
        extended_april = extended.reindex(initial_april.index)
        np.testing.assert_allclose(initial_april.to_numpy(), extended_april.to_numpy())

    def test_target_month_benchmark_cannot_leak_into_formation(self):
        data = _synthetic_panel("2021-04-30")
        mutated = data.copy()
        april_dates = mutated.index.get_level_values("datetime").to_period("M") == pd.Period(
            "2021-04", freq="M"
        )
        mutated.loc[april_dates, "$benchmark_close"] *= np.linspace(
            1.3, 0.7, int(april_dates.sum())
        )

        baseline = MONTHLY_DIMSON_BETA(
            data["$close"], data["$benchmark_close"], 3, 20
        )
        changed = MONTHLY_DIMSON_BETA(
            mutated["$close"], mutated["$benchmark_close"], 3, 20
        )

        april_index = baseline.loc[
            ("SH600000", slice("2021-04-01", "2021-04-30"))
        ].index
        np.testing.assert_allclose(
            baseline.reindex(april_index).to_numpy(),
            changed.reindex(april_index).to_numpy(),
        )

    def test_registered_expression_runs_through_calculator(self):
        data = _synthetic_panel()

        result = CustomFactorCalculator(
            data,
            auto_extract_cache=False,
        ).calculate_factor("Beta3MCSI300DimsonProxy", EXPRESSION)

        self.assertIsNotNone(result)
        self.assertAlmostEqual(
            float(result.loc[("SH600000", pd.Timestamp("2021-04-01"))]),
            1.0,
            places=10,
        )


class BenchmarkFeatureLoaderTests(unittest.TestCase):
    def test_benchmark_close_is_loaded_once_and_broadcast_by_date(self):
        dates = pd.to_datetime(["2021-01-04", "2021-01-05"])
        stock_index = pd.MultiIndex.from_product(
            [["SH600000", "SH600001"], dates],
            names=["instrument", "datetime"],
        )
        benchmark_index = pd.MultiIndex.from_product(
            [["SH000300"], dates],
            names=["instrument", "datetime"],
        )
        stock_values = pd.DataFrame(
            np.ones((len(stock_index), 6)),
            index=stock_index,
        )
        benchmark_values = pd.DataFrame(
            [10.0, 11.0],
            index=benchmark_index,
        )
        provider = Mock()
        provider.instruments.return_value = "dynamic-csi300"
        provider.features.side_effect = [stock_values, benchmark_values]
        qlib_module = types.ModuleType("qlib")
        qlib_module.init = Mock()
        data_module = types.ModuleType("qlib.data")
        data_module.D = provider
        config = {
            "data": {
                "provider_uri": "/tmp/provider",
                "market": "csi300",
                "start_time": "2021-01-04",
                "end_time": "2021-01-05",
                "additional_fields": ["benchmark_close"],
                "benchmark_instrument": "SH000300",
            }
        }

        with patch.dict(sys.modules, {"qlib": qlib_module, "qlib.data": data_module}):
            result = get_qlib_stock_data(config)

        self.assertEqual(provider.features.call_count, 2)
        self.assertEqual(provider.features.call_args_list[1].args[0], ["SH000300"])
        np.testing.assert_allclose(
            result["$benchmark_close"].to_numpy(),
            [10.0, 11.0, 10.0, 11.0],
        )


class DimsonBetaContractTests(unittest.TestCase):
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
        self.assertFalse(metadata["candidate_factor_values_viewed_before_registration"])
        self.assertFalse(metadata["candidate_forward_returns_viewed_before_registration"])
        self.assertFalse(metadata["external_token_required"])
        self.assertFalse(metadata["exact_recent_dimson_beta_previously_tested"])
        self.assertEqual(metadata["admission_factor_ids"], [FACTOR_ID])
        self.assertEqual(factor_id, expected_id)
        self.assertEqual(factor_id, FACTOR_ID)
        self.assertEqual(factor["factor_expression"], EXPRESSION)
        self.assertFalse(factor["feedback"]["decision"])

    def test_validation_and_runtime_share_frozen_contract(self):
        registry = json.loads(CANDIDATE_PATH.read_text(encoding="utf-8"))
        gate = yaml.safe_load(VALIDATION_PATH.read_text(encoding="utf-8"))
        runtime = yaml.safe_load(BACKTEST_PATH.read_text(encoding="utf-8"))
        metadata = registry["metadata"]

        self.assertEqual(gate["protocol_version"], "2.8")
        self.assertEqual(gate["trial_matrix"]["expected_factor_count"], 24)
        self.assertEqual(gate["trial_matrix"]["target_factor_id"], FACTOR_ID)
        self.assertEqual(gate["validation"]["admission_factor_ids"], [FACTOR_ID])
        self.assertEqual(gate["validation"]["label"], runtime["dataset"]["label"])
        self.assertEqual(gate["validation"]["factor_coverage_min"], 0.95)
        self.assertEqual(
            gate["validation"]["return_holding_period_trading_days"],
            runtime["backtest"]["strategy"]["kwargs"]["hold_thresh"],
        )
        self.assertEqual(gate["historical_provider"], runtime["data"]["provider_uri"])
        self.assertEqual(gate["historical_provider"], metadata["provider_path"])
        self.assertEqual(runtime["data"]["benchmark_instrument"], "SH000300")
        self.assertIn("benchmark_close", runtime["data"]["additional_fields"])
        self.assertEqual(gate["promotion"]["multiple_testing"]["total_trials"], 33)
        self.assertEqual(gate["promotion"]["multiple_testing"]["matrix_trials"], 24)
        self.assertEqual(len(gate["validation"]["reference_cached_factor_ids"]), 23)
        self.assertEqual(
            gate["validation"]["reference_cached_factor_ids"][-1],
            "031b3c48bf7fb29b",
        )

    @unittest.skipUnless(all(path.is_file() for path in (BASE_SOURCE_PATH, SOURCE_PATH,)), "External research artifacts are not bundled with source; provide the original factor library and audit files to run this integration check.")
    def test_source_preserves_v2_7_order_and_appends_candidate(self):
        base = json.loads(BASE_SOURCE_PATH.read_text(encoding="utf-8"))
        source = json.loads(SOURCE_PATH.read_text(encoding="utf-8"))
        base_ids = list(base["factors"])
        source_ids = list(source["factors"])

        self.assertEqual(len(base_ids), 23)
        self.assertEqual(source_ids[:23], base_ids)
        self.assertEqual(source_ids[23:], [FACTOR_ID])
        self.assertEqual(source["metadata"]["base_factor_count"], 23)
        self.assertEqual(source["metadata"]["total_factors"], 24)

    @unittest.skipUnless(all(path.is_file() for path in (PROVIDER_AUDIT_PATH, PROVIDER_PATH,)), "External research artifacts are not bundled with source; provide the original factor library and audit files to run this integration check.")
    def test_provider_reuses_base_caches_and_prepares_candidate(self):
        audit = json.loads(PROVIDER_AUDIT_PATH.read_text(encoding="utf-8"))
        provider = json.loads(PROVIDER_PATH.read_text(encoding="utf-8"))
        metadata = provider["metadata"]
        candidate = provider["factors"][FACTOR_ID]

        self.assertEqual(audit["status"], "PASS")
        self.assertEqual(audit["sourceFactorCount"], 24)
        self.assertEqual(audit["preparedFactorCount"], 24)
        self.assertEqual(len(audit["reusedFactorIds"]), 23)
        self.assertNotIn(FACTOR_ID, audit["reusedFactorIds"])
        self.assertEqual(metadata["reused_factor_count"], 23)
        self.assertEqual(metadata["calculation_range"], ["2010-01-01", "2026-08-04"])
        self.assertIn("benchmark_close", metadata["additional_raw_fields"])
        self.assertTrue(Path(candidate["cache_location"]["result_h5_path"]).is_file())


if __name__ == "__main__":
    unittest.main()
