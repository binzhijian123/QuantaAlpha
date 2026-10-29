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
    MONTHLY_DELTA_EXPECTED_SHORTFALL,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CANDIDATE_PATH = (
    PROJECT_ROOT
    / "configs"
    / "profit_candidates_v3_1_investment_data_deltaes1.json"
)
VALIDATION_PATH = (
    PROJECT_ROOT
    / "configs"
    / "profit_validation_v3_1_investment_data_deltaes1.yaml"
)
BACKTEST_PATH = (
    PROJECT_ROOT
    / "configs"
    / "backtest_profit_validation_v3_1_investment_data_deltaes1.yaml"
)
BASE_SOURCE_PATH = (
    PROJECT_ROOT
    / "data"
    / "factorlib"
    / "profit_trials_v3_0_investment_data_lowvol52w_provider.json"
)
SOURCE_PATH = (
    PROJECT_ROOT
    / "data"
    / "factorlib"
    / "profit_trials_v3_1_investment_data_deltaes1_source.json"
)
PROVIDER_PATH = (
    PROJECT_ROOT
    / "data"
    / "factorlib"
    / "profit_trials_v3_1_investment_data_deltaes1_provider.json"
)
PROVIDER_AUDIT_PATH = PROVIDER_PATH.with_suffix(".audit.json")
FACTOR_ID = "efaf6a5f1248d592"
EXPRESSION = "-MONTHLY_DELTA_EXPECTED_SHORTFALL($close, 1, 12, 200)"


def _synthetic_panel(end: str = "2020-03-31") -> pd.DataFrame:
    dates = pd.bdate_range("2018-01-01", end)
    positions = np.arange(len(dates))
    returns = 0.0004 + 0.006 * np.sin(positions / 8.7)
    previous_window = (dates >= "2018-02-01") & (dates <= "2019-01-31")
    current_window = (dates >= "2019-02-01") & (dates <= "2020-01-31")
    returns[np.flatnonzero(previous_window)[::47]] = -0.025
    returns[np.flatnonzero(current_window)[::43]] = -0.045
    close = 100.0 * np.cumprod(1.0 + returns)
    index = pd.MultiIndex.from_product(
        [["SH600000"], dates],
        names=["instrument", "datetime"],
    )
    return pd.DataFrame({"$close": close}, index=index)


def _expected_delta(data: pd.DataFrame, target_month: str) -> float:
    close = data["$close"].droplevel("instrument")
    daily_return = close.pct_change(fill_method=None)
    target = pd.Period(target_month, freq="M")

    def expected_shortfall(start: pd.Period, end: pd.Period) -> float:
        periods = daily_return.index.to_period("M")
        values = daily_return[(periods >= start) & (periods < end)].dropna().to_numpy()
        count = int(np.ceil(len(values) * 0.01))
        return -float(np.mean(np.partition(values, count - 1)[:count]))

    current_start = target - 12
    previous_start = target - 24
    return expected_shortfall(current_start, target) - expected_shortfall(
        previous_start, current_start
    )


class MonthlyDeltaExpectedShortfallOperatorTests(unittest.TestCase):
    def test_two_complete_years_are_constant_in_target_month(self):
        data = _synthetic_panel()

        result = MONTHLY_DELTA_EXPECTED_SHORTFALL(data["$close"], 1, 12, 200)

        february = result.loc[("SH600000", slice("2020-02-03", "2020-02-28"))]
        self.assertGreater(len(february), 0)
        np.testing.assert_allclose(
            february.to_numpy(),
            _expected_delta(data, "2020-02"),
            atol=1e-12,
        )

    def test_target_month_prices_cannot_leak_into_target_month_signal(self):
        data = _synthetic_panel("2020-02-28")
        mutated = data.copy()
        dates = mutated.index.get_level_values("datetime")
        target = dates.to_period("M") == pd.Period("2020-02", freq="M")
        mutated.loc[target, "$close"] *= np.linspace(0.6, 1.7, int(target.sum()))

        baseline = MONTHLY_DELTA_EXPECTED_SHORTFALL(data["$close"], 1, 12, 200)
        changed = MONTHLY_DELTA_EXPECTED_SHORTFALL(
            mutated["$close"], 1, 12, 200
        )
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

        initial_result = MONTHLY_DELTA_EXPECTED_SHORTFALL(
            initial["$close"], 1, 12, 200
        )
        extended_result = MONTHLY_DELTA_EXPECTED_SHORTFALL(
            extended["$close"], 1, 12, 200
        )
        february = initial_result.loc[
            ("SH600000", slice("2020-02-03", "2020-02-28"))
        ]
        np.testing.assert_allclose(
            february.to_numpy(),
            extended_result.reindex(february.index).to_numpy(),
        )

    def test_each_year_requires_two_hundred_finite_returns(self):
        data = _synthetic_panel("2020-02-28")
        dates = data.index.get_level_values("datetime")
        missing = (dates >= "2019-06-01") & (dates <= "2019-09-30")

        result = MONTHLY_DELTA_EXPECTED_SHORTFALL(
            data.loc[~missing, "$close"], 1, 12, 200
        )

        february = result.loc[("SH600000", slice("2020-02-03", "2020-02-28"))]
        self.assertTrue(february.isna().all())

    def test_dataframe_preserves_shape_and_column(self):
        data = _synthetic_panel("2020-02-28")

        result = MONTHLY_DELTA_EXPECTED_SHORTFALL(data[["$close"]], 1, 12, 200)

        self.assertIsInstance(result, pd.DataFrame)
        self.assertEqual(list(result.columns), ["$close"])
        self.assertTrue(result.index.equals(data.index))

    def test_instruments_are_calculated_independently(self):
        first = _synthetic_panel("2020-02-28")
        second = first.copy()
        dates = second.index.get_level_values("datetime")
        second.index = pd.MultiIndex.from_arrays(
            [["SZ000001"] * len(second), dates],
            names=["instrument", "datetime"],
        )
        perturbation = np.cumprod(
            1.0 + 0.004 * np.cos(np.arange(len(second)) / 5.3)
        )
        second["$close"] = second["$close"].to_numpy() * perturbation

        standalone = MONTHLY_DELTA_EXPECTED_SHORTFALL(
            first["$close"], 1, 12, 200
        )
        combined = MONTHLY_DELTA_EXPECTED_SHORTFALL(
            pd.concat([second, first])["$close"], 1, 12, 200
        )

        np.testing.assert_allclose(
            standalone.xs("SH600000", level="instrument").to_numpy(),
            combined.xs("SH600000", level="instrument").to_numpy(),
            equal_nan=True,
        )

    def test_unsorted_input_matches_sorted_input(self):
        data = _synthetic_panel("2020-02-28")

        expected = MONTHLY_DELTA_EXPECTED_SHORTFALL(
            data["$close"], 1, 12, 200
        )
        shuffled = data.sample(frac=1.0, random_state=17)
        actual = MONTHLY_DELTA_EXPECTED_SHORTFALL(
            shuffled["$close"], 1, 12, 200
        )

        np.testing.assert_allclose(
            expected.sort_index().to_numpy(),
            actual.sort_index().to_numpy(),
            equal_nan=True,
        )

    def test_internal_nan_and_zero_prices_do_not_create_infinite_signals(self):
        data = _synthetic_panel("2020-02-28")
        damaged = data.copy()
        damaged.iloc[110, damaged.columns.get_loc("$close")] = np.nan
        damaged.iloc[360, damaged.columns.get_loc("$close")] = 0.0

        result = MONTHLY_DELTA_EXPECTED_SHORTFALL(
            damaged["$close"], 1, 12, 200
        )

        february = result.loc[("SH600000", slice("2020-02-03", "2020-02-28"))]
        self.assertTrue(np.isfinite(february.to_numpy()).all())
        self.assertFalse(np.isinf(result.to_numpy()).any())

    def test_duplicate_instrument_dates_are_rejected(self):
        data = _synthetic_panel("2020-02-28")
        duplicate = pd.concat([data, data.iloc[[0]]])

        with self.assertRaisesRegex(ValueError, "unique"):
            MONTHLY_DELTA_EXPECTED_SHORTFALL(
                duplicate["$close"], 1, 12, 200
            )

    def test_invalid_parameters_are_rejected(self):
        data = _synthetic_panel("2020-02-28")
        invalid = (
            {"tail_percent": 0},
            {"tail_percent": 51},
            {"window_months": 0},
            {"min_obs": 0},
        )

        for kwargs in invalid:
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                MONTHLY_DELTA_EXPECTED_SHORTFALL(data["$close"], **kwargs)

    def test_registered_negative_expression_runs_through_calculator(self):
        data = _synthetic_panel("2020-02-28")

        result = CustomFactorCalculator(
            data,
            auto_extract_cache=False,
        ).calculate_factor("NegDeltaExpectedShortfall1Pct24MProxy", EXPRESSION)

        self.assertIsNotNone(result)
        self.assertAlmostEqual(
            float(result.loc[("SH600000", pd.Timestamp("2020-02-03"))]),
            -_expected_delta(data, "2020-02"),
            places=12,
        )


class DeltaExpectedShortfallContractTests(unittest.TestCase):
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
        self.assertTrue(metadata["low_volatility_admission_outcomes_previously_viewed"])
        self.assertFalse(metadata["candidate_factor_values_viewed_before_registration"])
        self.assertFalse(metadata["candidate_forward_returns_viewed_before_registration"])
        self.assertFalse(metadata["external_token_required"])
        self.assertFalse(metadata["exact_deltaes1_candidate_previously_tested"])
        self.assertFalse(metadata["paper_exact_quantile_interpolation_verified"])
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

        self.assertEqual(gate["protocol_version"], "3.1")
        self.assertEqual(gate["trial_matrix"]["expected_factor_count"], 27)
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
        penalty = gate["promotion"]["multiple_testing"]
        self.assertEqual(penalty["total_trials"], 385)
        self.assertEqual(penalty["planned_new_trials"], 29)
        self.assertEqual(penalty["matrix_trials"], 27)
        self.assertEqual(
            penalty["planned_new_evaluations"],
            {"admission": 1, "formal_matrix": 27, "factor_only": 1},
        )
        self.assertEqual(len(gate["validation"]["reference_cached_factor_ids"]), 26)
        self.assertEqual(
            gate["validation"]["reference_cached_factor_ids"][-1],
            "f7d3a0874fe65303",
        )

    @unittest.skipUnless(all(path.is_file() for path in (BASE_SOURCE_PATH, SOURCE_PATH,)), "External research artifacts are not bundled with source; provide the original factor library and audit files to run this integration check.")
    def test_source_preserves_v3_0_order_and_appends_candidate(self):
        base = json.loads(BASE_SOURCE_PATH.read_text(encoding="utf-8"))
        source = json.loads(SOURCE_PATH.read_text(encoding="utf-8"))
        base_ids = list(base["factors"])
        source_ids = list(source["factors"])

        self.assertEqual(len(base_ids), 26)
        self.assertEqual(source_ids[:26], base_ids)
        self.assertEqual(source_ids[26:], [FACTOR_ID])
        self.assertEqual(source["metadata"]["base_factor_count"], 26)
        self.assertEqual(source["metadata"]["total_factors"], 27)

    @unittest.skipUnless(PROVIDER_PATH.exists(), "provider library is not prepared yet")
    @unittest.skipUnless(all(path.is_file() for path in (PROVIDER_AUDIT_PATH, PROVIDER_PATH,)), "External research artifacts are not bundled with source; provide the original factor library and audit files to run this integration check.")
    def test_provider_reuses_base_caches_and_binds_candidate_code(self):
        audit = json.loads(PROVIDER_AUDIT_PATH.read_text(encoding="utf-8"))
        provider = json.loads(PROVIDER_PATH.read_text(encoding="utf-8"))
        metadata = provider["metadata"]
        candidate = provider["factors"][FACTOR_ID]
        cache = candidate["cache_location"]

        self.assertEqual(audit["status"], "PASS")
        self.assertEqual(audit["sourceFactorCount"], 27)
        self.assertEqual(audit["preparedFactorCount"], 27)
        self.assertEqual(len(audit["reusedFactorIds"]), 24)
        self.assertNotIn("af197059a9fb9b9e", audit["reusedFactorIds"])
        self.assertNotIn("f7d3a0874fe65303", audit["reusedFactorIds"])
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
