import hashlib
import json
import sys
import unittest
from pathlib import Path

import numpy as np
import pandas as pd
import yaml


PROJECT_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = PROJECT_ROOT / "frontend-v2" / "backend"
for path in (PROJECT_ROOT, BACKEND_ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from market_data.baostock_abr import (
    ABR_FIELD,
    ABR_METRIC,
    BENCHMARK_CODE,
    build_baostock_abr_panel,
    compute_earnings_announcement_abr,
    normalize_baostock_announcement_events,
)


STOCK_CODE = "600000.SH"


def _announcement(date="2023-01-05"):
    return pd.DataFrame(
        [
            {
                "code": STOCK_CODE,
                "report_end": "2022-12-31",
                "announcement_date": date,
            }
        ]
    )


def _prices(calendar, stock_closes, *, volumes=None, benchmark_closes=None):
    volumes = volumes or [100.0] * len(calendar)
    benchmark_closes = benchmark_closes or [100.0] * len(calendar)
    rows = []
    for date, close, volume in zip(calendar, stock_closes, volumes):
        rows.append(
            {
                "date": date,
                "code": STOCK_CODE,
                "close": close,
                "volume": volume,
            }
        )
    for date, close in zip(calendar, benchmark_closes):
        rows.append(
            {
                "date": date,
                "code": BENCHMARK_CODE,
                "close": close,
                "volume": 1.0,
            }
        )
    return pd.DataFrame(rows)


class BaoStockAbrCalculationTests(unittest.TestCase):
    def test_abr_sums_day_minus_two_through_plus_one_and_waits_until_plus_two(self):
        calendar = list(pd.date_range("2023-01-02", "2023-01-13", freq="B"))
        stock_closes = [100.0, 101.0, 103.02, 106.1106, 110.355024]
        stock_closes.extend([stock_closes[-1]] * (len(calendar) - len(stock_closes)))

        events, source_audit = compute_earnings_announcement_abr(
            _announcement(),
            _prices(calendar, stock_closes),
            calendar,
        )

        event = events.iloc[0]
        self.assertAlmostEqual(event[ABR_METRIC], 0.10, places=7)
        self.assertEqual(event["reaction_session"], pd.Timestamp("2023-01-05"))
        self.assertEqual(event["reaction_end"], pd.Timestamp("2023-01-06"))
        self.assertEqual(event["available_date"], pd.Timestamp("2023-01-06"))

        panel, audit = build_baostock_abr_panel(
            events,
            base_provider=Path("synthetic"),
            calendar=calendar,
            source_windows={
                STOCK_CODE: (calendar[0], calendar[-1]),
            },
            codes=[STOCK_CODE],
            source_audit=source_audit,
        )
        indexed = panel.set_index("date")
        self.assertTrue(np.isnan(indexed.loc[pd.Timestamp("2023-01-06"), ABR_FIELD]))
        self.assertAlmostEqual(
            indexed.loc[pd.Timestamp("2023-01-09"), ABR_FIELD],
            0.10,
            places=7,
        )
        self.assertEqual(audit["availabilityLeakRows"], 0)
        self.assertEqual(audit["status"], "PASS")

    def test_weekend_announcement_maps_day_zero_to_next_provider_session(self):
        calendar = list(pd.date_range("2023-01-02", "2023-01-13", freq="B"))
        events, audit = compute_earnings_announcement_abr(
            _announcement("2023-01-07"),
            _prices(calendar, [100.0] * len(calendar)),
            calendar,
        )

        self.assertEqual(events.iloc[0]["reaction_session"], pd.Timestamp("2023-01-09"))
        self.assertEqual(events.iloc[0]["reaction_end"], pd.Timestamp("2023-01-10"))
        self.assertEqual(events.iloc[0][ABR_METRIC], 0.0)
        self.assertEqual(audit["abrRows"], 1)

    def test_zero_volume_in_reaction_window_fails_closed(self):
        calendar = list(pd.date_range("2023-01-02", "2023-01-13", freq="B"))
        volumes = [100.0] * len(calendar)
        volumes[3] = 0.0
        events, audit = compute_earnings_announcement_abr(
            _announcement(),
            _prices(calendar, [100.0] * len(calendar), volumes=volumes),
            calendar,
        )

        self.assertTrue(np.isnan(events.iloc[0][ABR_METRIC]))
        self.assertEqual(events.iloc[0]["missing_reason"], "nontrading_stock_window")
        self.assertEqual(audit["missingReasonCounts"]["nontrading_stock_window"], 1)

    def test_conflicting_announcement_dates_fail_closed(self):
        rows = pd.concat(
            [_announcement("2023-01-05"), _announcement("2023-01-06")],
            ignore_index=True,
        )
        with self.assertRaisesRegex(ValueError, "conflicting duplicates"):
            normalize_baostock_announcement_events(rows)

    def test_signal_expires_six_calendar_months_after_report_end(self):
        calendar = list(pd.date_range("2023-01-02", "2023-07-03", freq="B"))
        events, source_audit = compute_earnings_announcement_abr(
            _announcement(),
            _prices(calendar, [100.0] * len(calendar)),
            calendar,
        )
        panel, audit = build_baostock_abr_panel(
            events,
            base_provider=Path("synthetic"),
            calendar=calendar,
            source_windows={STOCK_CODE: (calendar[0], calendar[-1])},
            codes=[STOCK_CODE],
            source_audit=source_audit,
        )
        indexed = panel.set_index("date")

        self.assertEqual(indexed.loc[pd.Timestamp("2023-06-30"), ABR_FIELD], 0.0)
        self.assertTrue(np.isnan(indexed.loc[pd.Timestamp("2023-07-03"), ABR_FIELD]))
        self.assertEqual(audit["staleSignalRowsCleared"], 1)


class BaoStockAbrProtocolTests(unittest.TestCase):
    def test_v2_preregisters_one_abr6_candidate_before_abr_computation(self):
        payload = json.loads(
            (
                PROJECT_ROOT
                / "configs"
                / "profit_candidates_v2_0_baostock_abr6.json"
            ).read_text(encoding="utf-8")
        )
        metadata = payload["metadata"]
        self.assertTrue(metadata["created_before_abr_computation"])
        self.assertFalse(metadata["candidate_abr_values_viewed_before_registration"])
        self.assertFalse(metadata["candidate_forward_returns_viewed_before_registration"])
        self.assertEqual(metadata["selection_horizon_trading_days"], 126)
        self.assertEqual(metadata["label_dependency_offset_trading_days"], 127)
        self.assertEqual(metadata["homogeneous_trial_count_after_v2_0"], 25)
        self.assertEqual(len(payload["factors"]), 1)

        factor_id, factor = next(iter(payload["factors"].items()))
        expected = hashlib.sha256(
            (
                f"{metadata['factor_id_namespace']}:"
                f"{factor['factor_name']}:{factor['factor_expression']}"
            ).encode()
        ).hexdigest()[:16]
        self.assertEqual(factor_id, expected)
        self.assertEqual(factor["factor_expression"], ABR_FIELD.replace("pit_", "$pit_"))
        self.assertIn("RESEARCH_ONLY", metadata["revision_policy"])

    def test_probe_sample_is_frozen_before_event_window_prices_are_read(self):
        payload = json.loads(
            (
                PROJECT_ROOT / "configs" / "profit_probe_v2_0_baostock_abr.json"
            ).read_text(encoding="utf-8")
        )
        self.assertTrue(payload["created_before_probe"])
        self.assertTrue(payload["event_window_prices_may_be_read"])
        self.assertFalse(payload["forward_labels_or_strategy_returns_may_be_read"])
        self.assertEqual(len(payload["selection"]["codes"]), 20)
        self.assertEqual(payload["acceptance"]["codes_with_any_abr_min"], 16)

    def test_v2_runtime_and_gate_share_the_frozen_abr6_contract(self):
        registry = json.loads(
            (
                PROJECT_ROOT
                / "configs"
                / "profit_candidates_v2_0_baostock_abr6.json"
            ).read_text(encoding="utf-8")
        )
        backtest = yaml.safe_load(
            (
                PROJECT_ROOT
                / "configs"
                / "backtest_profit_validation_v2_0_baostock_abr.yaml"
            ).read_text(encoding="utf-8")
        )
        gate = yaml.safe_load(
            (
                PROJECT_ROOT
                / "configs"
                / "profit_validation_v2_0_baostock_abr.yaml"
            ).read_text(encoding="utf-8")
        )

        self.assertEqual(backtest["dataset"]["label"], gate["validation"]["label"])
        self.assertEqual(
            gate["validation"]["label"], registry["metadata"]["selection_label"]
        )
        self.assertEqual(gate["validation"]["label_horizon_trading_days"], 127)
        self.assertEqual(gate["validation"]["return_holding_period_trading_days"], 126)
        self.assertEqual(backtest["backtest"]["strategy"]["kwargs"]["hold_thresh"], 126)
        self.assertEqual(backtest["backtest"]["strategy"]["kwargs"]["n_drop"], 1)
        self.assertEqual(backtest["dataset"]["segments"]["train"][1], "2019-06-27")
        self.assertEqual(backtest["dataset"]["segments"]["valid"][1], "2021-06-28")
        self.assertIn("pit_abr_earnings", backtest["data"]["additional_fields"])
        self.assertEqual(gate["trial_matrix"]["expected_factor_count"], 25)
        self.assertEqual(
            gate["validation"]["admission_factor_ids"],
            registry["metadata"]["admission_factor_ids"],
        )


if __name__ == "__main__":
    unittest.main()
