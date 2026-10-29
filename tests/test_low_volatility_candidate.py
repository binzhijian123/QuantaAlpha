from __future__ import annotations

import hashlib
import json
import unittest
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

from quantaalpha.backtest.custom_factor_calculator import CustomFactorCalculator
from quantaalpha.factors.coder.function_lib import MONTHLY_RETURN_VOLATILITY


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CANDIDATE_PATH = (
    PROJECT_ROOT
    / "configs"
    / "profit_candidates_v2_9_investment_data_lowvol36m.json"
)
VALIDATION_PATH = (
    PROJECT_ROOT
    / "configs"
    / "profit_validation_v2_9_investment_data_lowvol36m.yaml"
)
BACKTEST_PATH = (
    PROJECT_ROOT
    / "configs"
    / "backtest_profit_validation_v2_9_investment_data_lowvol36m.yaml"
)
BASE_SOURCE_PATH = (
    PROJECT_ROOT
    / "data"
    / "factorlib"
    / "profit_trials_v2_8_investment_data_beta3m_provider.json"
)
SOURCE_PATH = (
    PROJECT_ROOT
    / "data"
    / "factorlib"
    / "profit_trials_v2_9_investment_data_lowvol36m_source.json"
)
PROVIDER_PATH = (
    PROJECT_ROOT
    / "data"
    / "factorlib"
    / "profit_trials_v2_9_investment_data_lowvol36m_provider.json"
)
PROVIDER_AUDIT_PATH = PROVIDER_PATH.with_suffix(".audit.json")
FACTOR_ID = "af197059a9fb9b9e"
EXPRESSION = "-MONTHLY_RETURN_VOLATILITY($close, 36, 36)"


def _synthetic_panel(end: str = "2020-03-31") -> tuple[pd.DataFrame, np.ndarray]:
    dates = pd.bdate_range("2017-01-02", end)
    periods = pd.period_range("2017-01", pd.Period(end, freq="M"), freq="M")
    monthly_returns = 0.01 + 0.006 * np.sin(np.arange(1, len(periods)) / 2.7)
    monthly_close = np.concatenate(
        [[100.0], 100.0 * np.cumprod(1.0 + monthly_returns)]
    )
    close_by_period = pd.Series(monthly_close, index=periods)
    index = pd.MultiIndex.from_product(
        [["SH600000"], dates],
        names=["instrument", "datetime"],
    )
    close = close_by_period.reindex(dates.to_period("M")).to_numpy()
    return pd.DataFrame({"$close": close}, index=index), monthly_returns


class MonthlyReturnVolatilityOperatorTests(unittest.TestCase):
    def test_prior_36_month_sample_volatility_is_constant_in_target_month(self):
        data, monthly_returns = _synthetic_panel()

        result = MONTHLY_RETURN_VOLATILITY(data["$close"], 36, 36)

        february = result.loc[("SH600000", slice("2020-02-03", "2020-02-28"))]
        self.assertGreater(len(february), 0)
        np.testing.assert_allclose(
            february.to_numpy(),
            np.std(monthly_returns[:36], ddof=1),
            atol=1e-12,
        )

    def test_target_month_prices_cannot_leak_into_target_month_signal(self):
        data, _ = _synthetic_panel("2020-02-28")
        mutated = data.copy()
        dates = mutated.index.get_level_values("datetime")
        target = dates.to_period("M") == pd.Period("2020-02", freq="M")
        mutated.loc[target, "$close"] *= np.linspace(0.5, 1.8, int(target.sum()))

        baseline = MONTHLY_RETURN_VOLATILITY(data["$close"], 36, 36)
        changed = MONTHLY_RETURN_VOLATILITY(mutated["$close"], 36, 36)
        target_index = baseline.loc[
            ("SH600000", slice("2020-02-03", "2020-02-28"))
        ].index
        np.testing.assert_allclose(
            baseline.reindex(target_index).to_numpy(),
            changed.reindex(target_index).to_numpy(),
        )

    def test_future_extension_cannot_change_earlier_month(self):
        initial, _ = _synthetic_panel("2020-02-28")
        extended, _ = _synthetic_panel("2020-03-31")

        initial_result = MONTHLY_RETURN_VOLATILITY(initial["$close"], 36, 36)
        extended_result = MONTHLY_RETURN_VOLATILITY(extended["$close"], 36, 36)
        february = initial_result.loc[
            ("SH600000", slice("2020-02-03", "2020-02-28"))
        ]
        np.testing.assert_allclose(
            february.to_numpy(),
            extended_result.reindex(february.index).to_numpy(),
        )

    def test_missing_calendar_month_breaks_required_36_return_window(self):
        data, _ = _synthetic_panel("2020-02-28")
        dates = data.index.get_level_values("datetime")
        missing = dates.to_period("M") == pd.Period("2018-06", freq="M")

        result = MONTHLY_RETURN_VOLATILITY(data.loc[~missing, "$close"], 36, 36)

        february = result.loc[("SH600000", slice("2020-02-03", "2020-02-28"))]
        self.assertTrue(february.isna().all())

    def test_registered_negative_expression_runs_through_calculator(self):
        data, monthly_returns = _synthetic_panel("2020-02-28")

        result = CustomFactorCalculator(
            data,
            auto_extract_cache=False,
        ).calculate_factor("LowVolatility36MMonthlyProxy", EXPRESSION)

        self.assertIsNotNone(result)
        self.assertAlmostEqual(
            float(result.loc[("SH600000", pd.Timestamp("2020-02-03"))]),
            -float(np.std(monthly_returns[:36], ddof=1)),
            places=12,
        )


class LowVolatilityContractTests(unittest.TestCase):
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
        self.assertTrue(metadata["created_before_candidate_factor_derivation"])
        self.assertFalse(metadata["candidate_factor_values_viewed_before_registration"])
        self.assertFalse(metadata["candidate_forward_returns_viewed_before_registration"])
        self.assertFalse(metadata["external_token_required"])
        self.assertFalse(metadata["exact_lowvol36_monthly_previously_tested"])
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

        self.assertEqual(gate["protocol_version"], "2.9")
        self.assertEqual(gate["trial_matrix"]["expected_factor_count"], 25)
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
        self.assertEqual(gate["promotion"]["multiple_testing"]["total_trials"], 381)
        self.assertEqual(
            gate["promotion"]["multiple_testing"]["planned_new_trials"], 27
        )
        self.assertEqual(gate["promotion"]["multiple_testing"]["matrix_trials"], 25)
        self.assertEqual(len(gate["validation"]["reference_cached_factor_ids"]), 24)
        self.assertEqual(
            gate["validation"]["reference_cached_factor_ids"][-1],
            "1b7913109f79fb5a",
        )

    @unittest.skipUnless(all(path.is_file() for path in (BASE_SOURCE_PATH, SOURCE_PATH,)), "External research artifacts are not bundled with source; provide the original factor library and audit files to run this integration check.")
    def test_source_preserves_v2_8_order_and_appends_candidate(self):
        base = json.loads(BASE_SOURCE_PATH.read_text(encoding="utf-8"))
        source = json.loads(SOURCE_PATH.read_text(encoding="utf-8"))
        base_ids = list(base["factors"])
        source_ids = list(source["factors"])

        self.assertEqual(len(base_ids), 24)
        self.assertEqual(source_ids[:24], base_ids)
        self.assertEqual(source_ids[24:], [FACTOR_ID])
        self.assertEqual(source["metadata"]["base_factor_count"], 24)
        self.assertEqual(source["metadata"]["total_factors"], 25)

    @unittest.skipUnless(PROVIDER_PATH.exists(), "provider library is not prepared yet")
    @unittest.skipUnless(all(path.is_file() for path in (PROVIDER_AUDIT_PATH, PROVIDER_PATH,)), "External research artifacts are not bundled with source; provide the original factor library and audit files to run this integration check.")
    def test_provider_reuses_base_caches_and_binds_candidate_code(self):
        audit = json.loads(PROVIDER_AUDIT_PATH.read_text(encoding="utf-8"))
        provider = json.loads(PROVIDER_PATH.read_text(encoding="utf-8"))
        metadata = provider["metadata"]
        candidate = provider["factors"][FACTOR_ID]
        cache = candidate["cache_location"]

        self.assertEqual(audit["status"], "PASS")
        self.assertEqual(audit["sourceFactorCount"], 25)
        self.assertEqual(audit["preparedFactorCount"], 25)
        self.assertEqual(len(audit["reusedFactorIds"]), 24)
        self.assertNotIn(FACTOR_ID, audit["reusedFactorIds"])
        self.assertEqual(metadata["reused_factor_count"], 24)
        self.assertEqual(metadata["calculation_range"], ["2010-01-01", "2026-08-04"])
        self.assertEqual(cache["function_library_sha256"], metadata["factor_code_contract"]["function_library_sha256"])
        self.assertEqual(cache["parser_code_sha256"], metadata["factor_code_contract"]["parser_code_sha256"])
        self.assertEqual(cache["calculator_code_sha256"], metadata["factor_code_contract"]["calculator_code_sha256"])
        self.assertEqual(cache["preparation_code_sha256"], metadata["factor_code_contract"]["preparation_code_sha256"])
        self.assertTrue(Path(cache["result_h5_path"]).is_file())


if __name__ == "__main__":
    unittest.main()
