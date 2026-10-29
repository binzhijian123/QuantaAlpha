from __future__ import annotations

import hashlib
import json
import unittest
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

from quantaalpha.backtest.custom_factor_calculator import CustomFactorCalculator
from quantaalpha.factors.coder.function_lib import (
    MONTHLY_WEEKLY_RETURN_VOLATILITY,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CANDIDATE_PATH = (
    PROJECT_ROOT
    / "configs"
    / "profit_candidates_v3_0_investment_data_lowvol52w.json"
)
VALIDATION_PATH = (
    PROJECT_ROOT
    / "configs"
    / "profit_validation_v3_0_investment_data_lowvol52w.yaml"
)
BACKTEST_PATH = (
    PROJECT_ROOT
    / "configs"
    / "backtest_profit_validation_v3_0_investment_data_lowvol52w.yaml"
)
BASE_SOURCE_PATH = (
    PROJECT_ROOT
    / "data"
    / "factorlib"
    / "profit_trials_v2_9_investment_data_lowvol36m_provider.json"
)
SOURCE_PATH = (
    PROJECT_ROOT
    / "data"
    / "factorlib"
    / "profit_trials_v3_0_investment_data_lowvol52w_source.json"
)
PROVIDER_PATH = (
    PROJECT_ROOT
    / "data"
    / "factorlib"
    / "profit_trials_v3_0_investment_data_lowvol52w_provider.json"
)
PROVIDER_AUDIT_PATH = PROVIDER_PATH.with_suffix(".audit.json")
FACTOR_ID = "f7d3a0874fe65303"
EXPRESSION = "-MONTHLY_WEEKLY_RETURN_VOLATILITY($close, 52, 52)"


def _synthetic_panel(end: str = "2020-03-31") -> pd.DataFrame:
    dates = pd.bdate_range("2018-12-31", end)
    weeks = dates.to_period("W-FRI").unique()
    weekly_returns = 0.008 + 0.012 * np.sin(np.arange(1, len(weeks)) / 3.1)
    weekly_close = np.concatenate(
        [[100.0], 100.0 * np.cumprod(1.0 + weekly_returns)]
    )
    close_by_week = pd.Series(weekly_close, index=weeks)
    index = pd.MultiIndex.from_product(
        [["SH600000"], dates],
        names=["instrument", "datetime"],
    )
    close = close_by_week.reindex(dates.to_period("W-FRI")).to_numpy()
    return pd.DataFrame({"$close": close}, index=index)


def _expected_february_volatility(data: pd.DataFrame) -> float:
    series = data["$close"].droplevel("instrument")
    weekly_close = series.groupby(series.index.to_period("W-FRI")).last()
    weekly_return = weekly_close.pct_change(fill_method=None)
    eligible = weekly_return.index.end_time.normalize() < pd.Timestamp("2020-02-01")
    return float(weekly_return.loc[eligible].tail(52).std(ddof=1))


class MonthlyWeeklyReturnVolatilityOperatorTests(unittest.TestCase):
    def test_prior_52_week_sample_volatility_is_constant_in_target_month(self):
        data = _synthetic_panel()

        result = MONTHLY_WEEKLY_RETURN_VOLATILITY(data["$close"], 52, 52)

        february = result.loc[("SH600000", slice("2020-02-03", "2020-02-28"))]
        self.assertGreater(len(february), 0)
        np.testing.assert_allclose(
            february.to_numpy(),
            _expected_february_volatility(data),
            atol=1e-12,
        )

    def test_target_month_prices_cannot_leak_into_target_month_signal(self):
        data = _synthetic_panel("2020-02-28")
        mutated = data.copy()
        dates = mutated.index.get_level_values("datetime")
        target = dates.to_period("M") == pd.Period("2020-02", freq="M")
        mutated.loc[target, "$close"] *= np.linspace(0.5, 1.8, int(target.sum()))

        baseline = MONTHLY_WEEKLY_RETURN_VOLATILITY(data["$close"], 52, 52)
        changed = MONTHLY_WEEKLY_RETURN_VOLATILITY(mutated["$close"], 52, 52)
        target_index = baseline.loc[
            ("SH600000", slice("2020-02-03", "2020-02-28"))
        ].index
        np.testing.assert_allclose(
            baseline.reindex(target_index).to_numpy(),
            changed.reindex(target_index).to_numpy(),
        )

    def test_future_extension_cannot_change_earlier_month(self):
        initial = _synthetic_panel("2020-02-28")
        extended = _synthetic_panel("2020-03-31")

        initial_result = MONTHLY_WEEKLY_RETURN_VOLATILITY(
            initial["$close"], 52, 52
        )
        extended_result = MONTHLY_WEEKLY_RETURN_VOLATILITY(
            extended["$close"], 52, 52
        )
        february = initial_result.loc[
            ("SH600000", slice("2020-02-03", "2020-02-28"))
        ]
        np.testing.assert_allclose(
            february.to_numpy(),
            extended_result.reindex(february.index).to_numpy(),
        )

    def test_missing_complete_week_breaks_required_52_return_window(self):
        data = _synthetic_panel("2020-02-28")
        dates = data.index.get_level_values("datetime")
        missing = dates.to_period("W-FRI") == pd.Period(
            "2019-06-03", freq="W-FRI"
        )

        result = MONTHLY_WEEKLY_RETURN_VOLATILITY(
            data.loc[~missing, "$close"], 52, 52
        )

        february = result.loc[("SH600000", slice("2020-02-03", "2020-02-28"))]
        self.assertTrue(february.isna().all())

    def test_registered_negative_expression_runs_through_calculator(self):
        data = _synthetic_panel("2020-02-28")

        result = CustomFactorCalculator(
            data,
            auto_extract_cache=False,
        ).calculate_factor("LowVolatility52WWeeklyProxy", EXPRESSION)

        self.assertIsNotNone(result)
        self.assertAlmostEqual(
            float(result.loc[("SH600000", pd.Timestamp("2020-02-03"))]),
            -_expected_february_volatility(data),
            places=12,
        )


class WeeklyLowVolatilityContractTests(unittest.TestCase):
    def test_registry_is_deterministic_and_preregistered(self):
        registry = json.loads(CANDIDATE_PATH.read_text(encoding="utf-8"))
        metadata = registry["metadata"]
        factor_id, factor = next(iter(registry["factors"].items()))
        expected_id = hashlib.sha256(
            (
                f"{metadata['factor_id_namespace']}:"
                f"{factor['factor_name']}:{factor['factor_expression']}"
            ).encode("utf-8")
        ).hexdigest()[:16]

        self.assertEqual(metadata["current_state"], "PREREGISTERED")
        self.assertTrue(metadata["lowvol36_admission_outcome_previously_viewed"])
        self.assertFalse(metadata["candidate_factor_values_viewed_before_registration"])
        self.assertFalse(metadata["candidate_forward_returns_viewed_before_registration"])
        self.assertFalse(metadata["external_token_required"])
        self.assertFalse(metadata["exact_lowvol52w_weekly_previously_tested"])
        self.assertEqual(metadata["admission_factor_ids"], [FACTOR_ID])
        self.assertEqual(factor_id, expected_id)
        self.assertEqual(factor_id, FACTOR_ID)
        self.assertEqual(factor["factor_expression"], EXPRESSION)
        self.assertTrue(factor["metadata"]["cache_code_binding_required"])
        self.assertFalse(factor["feedback"]["decision"])

    def test_validation_and_runtime_share_frozen_contract(self):
        registry = json.loads(CANDIDATE_PATH.read_text(encoding="utf-8"))
        gate = yaml.safe_load(VALIDATION_PATH.read_text(encoding="utf-8"))
        runtime = yaml.safe_load(BACKTEST_PATH.read_text(encoding="utf-8"))
        metadata = registry["metadata"]

        self.assertEqual(gate["protocol_version"], "3.0")
        self.assertEqual(gate["trial_matrix"]["expected_factor_count"], 26)
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
        self.assertEqual(gate["promotion"]["multiple_testing"]["total_trials"], 383)
        self.assertEqual(
            gate["promotion"]["multiple_testing"]["planned_new_trials"], 28
        )
        self.assertEqual(gate["promotion"]["multiple_testing"]["matrix_trials"], 26)
        self.assertEqual(len(gate["validation"]["reference_cached_factor_ids"]), 25)
        self.assertEqual(
            gate["validation"]["reference_cached_factor_ids"][-1],
            "af197059a9fb9b9e",
        )

    @unittest.skipUnless(all(path.is_file() for path in (BASE_SOURCE_PATH, SOURCE_PATH,)), "External research artifacts are not bundled with source; provide the original factor library and audit files to run this integration check.")
    def test_source_preserves_v2_9_order_and_appends_candidate(self):
        base = json.loads(BASE_SOURCE_PATH.read_text(encoding="utf-8"))
        source = json.loads(SOURCE_PATH.read_text(encoding="utf-8"))
        base_ids = list(base["factors"])
        source_ids = list(source["factors"])

        self.assertEqual(len(base_ids), 25)
        self.assertEqual(source_ids[:25], base_ids)
        self.assertEqual(source_ids[25:], [FACTOR_ID])
        self.assertEqual(source["metadata"]["base_factor_count"], 25)
        self.assertEqual(source["metadata"]["total_factors"], 26)

    @unittest.skipUnless(PROVIDER_PATH.exists(), "provider library is not prepared yet")
    @unittest.skipUnless(all(path.is_file() for path in (PROVIDER_AUDIT_PATH, PROVIDER_PATH,)), "External research artifacts are not bundled with source; provide the original factor library and audit files to run this integration check.")
    def test_provider_reuses_base_caches_and_binds_candidate_code(self):
        audit = json.loads(PROVIDER_AUDIT_PATH.read_text(encoding="utf-8"))
        provider = json.loads(PROVIDER_PATH.read_text(encoding="utf-8"))
        metadata = provider["metadata"]
        candidate = provider["factors"][FACTOR_ID]
        cache = candidate["cache_location"]

        self.assertEqual(audit["status"], "PASS")
        self.assertEqual(audit["sourceFactorCount"], 26)
        self.assertEqual(audit["preparedFactorCount"], 26)
        self.assertEqual(len(audit["reusedFactorIds"]), 24)
        self.assertNotIn("af197059a9fb9b9e", audit["reusedFactorIds"])
        self.assertNotIn(FACTOR_ID, audit["reusedFactorIds"])
        self.assertEqual(metadata["reused_factor_count"], 24)
        self.assertEqual(metadata["calculation_range"], ["2010-01-01", "2026-08-04"])
        for field in (
            "function_library_sha256",
            "parser_code_sha256",
            "calculator_code_sha256",
            "preparation_code_sha256",
        ):
            self.assertEqual(cache[field], metadata["factor_code_contract"][field])
        self.assertTrue(Path(cache["result_h5_path"]).is_file())


if __name__ == "__main__":
    unittest.main()
