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
from quantaalpha.factors.coder.function_lib import SAME_MONTH_RETURN_MEAN


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CANDIDATE_PATH = (
    PROJECT_ROOT / "configs" / "profit_candidates_v2_7_investment_data_ra25.json"
)
VALIDATION_PATH = (
    PROJECT_ROOT / "configs" / "profit_validation_v2_7_investment_data_ra25.yaml"
)
BACKTEST_PATH = (
    PROJECT_ROOT
    / "configs"
    / "backtest_profit_validation_v2_7_investment_data_ra25.yaml"
)
BASE_SOURCE_PATH = (
    PROJECT_ROOT
    / "data"
    / "factorlib"
    / "profit_trials_v2_6_investment_data_pps1_source.json"
)
SOURCE_PATH = (
    PROJECT_ROOT
    / "data"
    / "factorlib"
    / "profit_trials_v2_7_investment_data_ra25_source.json"
)
PROVIDER_PATH = (
    PROJECT_ROOT
    / "data"
    / "factorlib"
    / "profit_trials_v2_7_investment_data_ra25_provider.json"
)
PROVIDER_AUDIT_PATH = PROVIDER_PATH.with_suffix(".audit.json")
FACTOR_ID = "031b3c48bf7fb29b"
EXPRESSION = "SAME_MONTH_RETURN_MEAN($close, 2, 5)"


def _synthetic_close(include_2017_predecessor: bool = True) -> pd.Series:
    observations = [
        ("2015-12-31", 100.0),
        ("2016-01-29", 110.0),
        ("2016-12-30", 100.0),
        ("2017-01-26", 120.0),
        ("2017-12-29", 100.0),
        ("2018-01-31", 130.0),
        ("2018-12-28", 100.0),
        ("2019-01-31", 140.0),
        ("2021-01-04", 200.0),
        ("2021-01-29", 210.0),
        ("2021-02-01", 211.0),
    ]
    if not include_2017_predecessor:
        observations = [item for item in observations if item[0] != "2017-12-29"]
    index = pd.MultiIndex.from_tuples(
        [("SH600000", pd.Timestamp(day)) for day, _ in observations],
        names=["instrument", "datetime"],
    )
    return pd.Series([value for _, value in observations], index=index, name="$close")


class CalendarSeasonalityOperatorTests(unittest.TestCase):
    def test_exact_ra25_mean_is_constant_inside_target_month(self):
        close = _synthetic_close()

        result = SAME_MONTH_RETURN_MEAN(close, 2, 5)

        january = result.loc[("SH600000", slice("2021-01-01", "2021-01-31"))]
        self.assertEqual(len(january), 2)
        np.testing.assert_allclose(january.to_numpy(), [0.25, 0.25])
        self.assertTrue(np.isnan(result.loc[("SH600000", pd.Timestamp("2021-02-01"))]))

    def test_missing_exact_predecessor_month_does_not_bridge_or_partial_mean(self):
        close = _synthetic_close(include_2017_predecessor=False)

        result = SAME_MONTH_RETURN_MEAN(close, 2, 5)

        self.assertTrue(
            np.isnan(result.loc[("SH600000", pd.Timestamp("2021-01-04"))])
        )

    def test_future_extension_cannot_change_earlier_signal(self):
        close = _synthetic_close()
        initial = SAME_MONTH_RETURN_MEAN(close, 2, 5)
        future_index = pd.MultiIndex.from_tuples(
            [("SH600000", pd.Timestamp("2022-01-31"))],
            names=["instrument", "datetime"],
        )
        extended = pd.concat([close, pd.Series([999.0], index=future_index)])

        later = SAME_MONTH_RETURN_MEAN(extended, 2, 5)

        self.assertEqual(
            float(initial.loc[("SH600000", pd.Timestamp("2021-01-04"))]),
            float(later.loc[("SH600000", pd.Timestamp("2021-01-04"))]),
        )

    def test_registered_expression_runs_through_calculator(self):
        close = _synthetic_close()
        data = close.to_frame()

        result = CustomFactorCalculator(
            data,
            auto_extract_cache=False,
        ).calculate_factor("Ra25SameMonthSeasonalityProxy", EXPRESSION)

        self.assertIsNotNone(result)
        self.assertAlmostEqual(
            float(result.loc[("SH600000", pd.Timestamp("2021-01-04"))]),
            0.25,
        )


class StaticInstrumentHistoryTests(unittest.TestCase):
    @staticmethod
    def _fake_qlib_modules():
        index = pd.MultiIndex.from_tuples(
            [("SH600000", pd.Timestamp("2021-01-04"))],
            names=["instrument", "datetime"],
        )
        provider = Mock()
        provider.instruments.return_value = "dynamic-csi300"
        provider.list_instruments.return_value = ["SH600000", "SH600001"]
        provider.features.return_value = pd.DataFrame(
            [[1.0] * 6],
            index=index,
            columns=["a", "b", "c", "d", "e", "f"],
        )
        qlib_module = types.ModuleType("qlib")
        qlib_module.init = Mock()
        data_module = types.ModuleType("qlib.data")
        data_module.D = provider
        return qlib_module, data_module, provider

    def test_static_scope_loads_union_members_as_plain_instrument_list(self):
        qlib_module, data_module, provider = self._fake_qlib_modules()
        config = {
            "data": {
                "provider_uri": "/tmp/provider",
                "market": "csi300",
                "start_time": "2010-01-01",
                "end_time": "2026-08-04",
                "static_instrument_history": True,
                "instrument_membership_start_time": "2016-01-01",
                "instrument_membership_end_time": "2026-08-04",
            }
        }

        with patch.dict(
            sys.modules,
            {"qlib": qlib_module, "qlib.data": data_module},
        ):
            get_qlib_stock_data(config)

        provider.list_instruments.assert_called_once_with(
            "dynamic-csi300",
            start_time="2016-01-01",
            end_time="2026-08-04",
            freq="day",
            as_list=True,
        )
        self.assertEqual(provider.features.call_args.args[0], ["SH600000", "SH600001"])

    def test_default_scope_preserves_dynamic_market_membership(self):
        qlib_module, data_module, provider = self._fake_qlib_modules()
        config = {
            "data": {
                "provider_uri": "/tmp/provider",
                "market": "csi300",
                "start_time": "2021-01-01",
                "end_time": "2021-01-31",
            }
        }

        with patch.dict(
            sys.modules,
            {"qlib": qlib_module, "qlib.data": data_module},
        ):
            get_qlib_stock_data(config)

        provider.list_instruments.assert_not_called()
        self.assertEqual(provider.features.call_args.args[0], "dynamic-csi300")


class CalendarSeasonalityContractTests(unittest.TestCase):
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
        self.assertFalse(metadata["exact_same_calendar_month_seasonality_previously_tested"])
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

        self.assertEqual(gate["protocol_version"], "2.7")
        self.assertEqual(gate["trial_matrix"]["expected_factor_count"], 23)
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
        self.assertTrue(gate["factor_cache_preparation"]["static_instrument_history"])
        self.assertEqual(gate["promotion"]["multiple_testing"]["total_trials"], 32)
        self.assertEqual(gate["promotion"]["multiple_testing"]["matrix_trials"], 23)
        self.assertEqual(len(gate["validation"]["reference_cached_factor_ids"]), 22)
        self.assertEqual(
            gate["validation"]["reference_cached_factor_ids"][-1],
            "9ed74ff7c9df603e",
        )

    @unittest.skipUnless(all(path.is_file() for path in (BASE_SOURCE_PATH, SOURCE_PATH,)), "External research artifacts are not bundled with source; provide the original factor library and audit files to run this integration check.")
    def test_source_preserves_v2_6_order_and_appends_candidate(self):
        base = json.loads(BASE_SOURCE_PATH.read_text(encoding="utf-8"))
        source = json.loads(SOURCE_PATH.read_text(encoding="utf-8"))
        base_ids = list(base["factors"])
        source_ids = list(source["factors"])

        self.assertEqual(len(base_ids), 22)
        self.assertEqual(source_ids[:22], base_ids)
        self.assertEqual(source_ids[22:], [FACTOR_ID])
        self.assertEqual(source["metadata"]["base_factor_count"], 22)
        self.assertEqual(source["metadata"]["total_factors"], 23)

    @unittest.skipUnless(all(path.is_file() for path in (PROVIDER_AUDIT_PATH, PROVIDER_PATH, SOURCE_PATH,)), "External research artifacts are not bundled with source; provide the original factor library and audit files to run this integration check.")
    def test_provider_cache_is_complete_and_uses_static_history_scope(self):
        audit = json.loads(PROVIDER_AUDIT_PATH.read_text(encoding="utf-8"))
        provider = json.loads(PROVIDER_PATH.read_text(encoding="utf-8"))
        metadata = provider["metadata"]
        candidate = provider["factors"][FACTOR_ID]

        self.assertEqual(audit["status"], "PASS")
        self.assertEqual(audit["sourceFactorCount"], 23)
        self.assertEqual(audit["preparedFactorCount"], 23)
        self.assertEqual(audit["instrumentScope"]["mode"], "static_union_of_historical_market_members")
        self.assertEqual(metadata["calculation_range"][0], "2010-01-01")
        self.assertEqual(metadata["instrument_scope"], audit["instrumentScope"])
        self.assertEqual(list(provider["factors"]), list(json.loads(
            SOURCE_PATH.read_text(encoding="utf-8")
        )["factors"]))
        self.assertTrue(Path(candidate["cache_location"]["result_h5_path"]).is_file())


if __name__ == "__main__":
    unittest.main()
