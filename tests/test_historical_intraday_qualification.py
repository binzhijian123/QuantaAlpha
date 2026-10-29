import sys
import unittest
from datetime import date, datetime, time, timedelta
from pathlib import Path

import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = PROJECT_ROOT / "frontend-v2" / "backend"
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from market_data.historical_intraday_qualification import (
    EXPECTED_THROUGH_DECISION,
    MINUTE_FIELDS,
    PREMARKET_FIELDS,
    TushareIntradayQualificationRequest,
    normalize_tushare_intraday_symbol,
    qualify_tushare_intraday_sample,
)


def _minutes(code: str, days: list[date], *, include_execution: bool = True) -> pd.DataFrame:
    rows = []
    clocks = sorted(EXPECTED_THROUGH_DECISION | {time(9, 30)})
    if include_execution:
        clocks.append(time(14, 55))
    for day in days:
        for clock in clocks:
            rows.append(
                {
                    "ts_code": code,
                    "trade_time": datetime.combine(day, clock).strftime("%Y-%m-%d %H:%M:%S"),
                    "open": 10.0,
                    "close": 10.0,
                    "high": 10.0,
                    "low": 10.0,
                    "vol": 1_000.0,
                    "amount": 10_000.0,
                }
            )
    return pd.DataFrame(rows, columns=MINUTE_FIELDS)


def _premarket(code: str, days: list[date]) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "trade_date": day.strftime("%Y%m%d"),
                "ts_code": code,
                "total_share": 200.0,
                "float_share": 100.0,
                "pre_close": 10.0,
                "up_limit": 11.0,
                "down_limit": 9.0,
            }
            for day in days
        ],
        columns=PREMARKET_FIELDS,
    )


class _Client:
    def __init__(self, days: list[date], *, omit_execution_for: str | None = None):
        self.days = days
        self.omit_execution_for = omit_execution_for
        self.calls = []

    def query(self, api_name, *, params=None, fields=()):
        self.calls.append((api_name, params, fields))
        code = params["ts_code"]
        if api_name == "stk_premarket":
            return _premarket(code, self.days)
        start = date.fromisoformat(params["start_date"][:10])
        end = date.fromisoformat(params["end_date"][:10])
        days = [day for day in self.days if start <= day <= end]
        return _minutes(
            code,
            days,
            include_execution=code != self.omit_execution_for,
        )


def _snapshot(codes: tuple[str, ...], reference_date: date) -> dict:
    # 47 bars through 14:50 * 1,000 shares / (100 * 10,000 shares) * 100 = 4.7%.
    return {
        "captureSlot": f"{reference_date.isoformat()}T14:50:00+08:00",
        "contentSha256": "reference-hash",
        "quotes": [
            {
                "symbol": code,
                "tradeDate": reference_date.isoformat(),
                "source": "a-stock-data/tencent-public",
                "previousClose": 10.0,
                "turnoverPercent": 4.7,
            }
            for code in codes
        ],
    }


class TushareIntradayNormalizationTests(unittest.TestCase):
    def test_reconstructs_turnover_from_shares_and_premarket_10k_shares(self):
        day = date(2026, 8, 13)
        daily, summary = normalize_tushare_intraday_symbol(
            "600519.SH", _minutes("600519.SH", [day]), _premarket("600519.SH", [day])
        )
        self.assertEqual(summary["validSessions"], 1)
        self.assertEqual(summary["barsThroughDecisionMin"], 47)
        self.assertAlmostEqual(daily.iloc[0]["turnover_percent"], 4.7)
        self.assertTrue(daily.iloc[0]["has_auction_bar"])

    def test_missing_execution_bar_marks_session_invalid(self):
        day = date(2026, 8, 13)
        daily, summary = normalize_tushare_intraday_symbol(
            "600519.SH",
            _minutes("600519.SH", [day], include_execution=False),
            _premarket("600519.SH", [day]),
        )
        self.assertTrue(daily.empty)
        self.assertEqual(summary["invalidSessions"], 1)
        self.assertIn(
            "execution_bar_count_not_one",
            summary["invalidDateExamples"][0]["reasons"],
        )

    def test_duplicate_minute_timestamp_fails_closed(self):
        day = date(2026, 8, 13)
        minutes = _minutes("600519.SH", [day])
        minutes = pd.concat([minutes, minutes.iloc[[0]]], ignore_index=True)
        with self.assertRaisesRegex(ValueError, "duplicate trade_time"):
            normalize_tushare_intraday_symbol(
                "600519.SH", minutes, _premarket("600519.SH", [day])
            )

    def test_negative_volume_fails_closed(self):
        day = date(2026, 8, 13)
        minutes = _minutes("600519.SH", [day])
        minutes.loc[0, "vol"] = -1
        with self.assertRaisesRegex(ValueError, "non-negative"):
            normalize_tushare_intraday_symbol(
                "600519.SH", minutes, _premarket("600519.SH", [day])
            )


class TushareIntradayQualificationTests(unittest.TestCase):
    def setUp(self):
        self.codes = ("600519.SH", "000001.SZ", "300750.SZ")
        self.days = [date(2026, 8, 12), date(2026, 8, 13)]
        self.request = TushareIntradayQualificationRequest(
            codes=self.codes,
            start_date=self.days[0],
            end_date=self.days[-1],
            reference_date=self.days[-1],
            reference_snapshot=Path("reference.json.gz"),
            minimum_common_sessions=2,
            chunk_days=1,
            request_interval_seconds=0,
        )

    def test_sample_pass_is_still_not_source_or_profit_qualified(self):
        client = _Client(self.days)
        result = qualify_tushare_intraday_sample(
            client,
            self.request,
            snapshot_loader=lambda _: _snapshot(self.codes, self.days[-1]),
            sleep=lambda _: None,
        )
        self.assertEqual(result["status"], "SAMPLE_PASS")
        self.assertEqual(result["coverage"]["commonValidSessions"], 2)
        self.assertFalse(result["sourceQualified"])
        self.assertFalse(result["adapterEligible"])
        self.assertFalse(result["profitEvidenceEligible"])
        self.assertIn(
            "current_stk_premarket_contract_covers_only_about_two_years_not_four_complete_years",
            result["remainingSourceGates"],
        )
        self.assertEqual(result["transport"]["queryCount"], 9)
        self.assertEqual(
            [call[0] for call in client.calls].count("stk_mins"),
            6,
        )

    def test_one_symbol_missing_execution_fails_common_session_gate(self):
        client = _Client(self.days, omit_execution_for="300750.SZ")
        result = qualify_tushare_intraday_sample(
            client,
            self.request,
            snapshot_loader=lambda _: _snapshot(self.codes, self.days[-1]),
            sleep=lambda _: None,
        )
        self.assertEqual(result["status"], "FAIL")
        self.assertFalse(result["coverage"]["passed"])
        self.assertEqual(result["coverage"]["commonValidSessions"], 0)

    def test_reference_turnover_outside_frozen_tolerance_fails(self):
        payload = _snapshot(self.codes, self.days[-1])
        payload["quotes"][0]["turnoverPercent"] = 4.8
        result = qualify_tushare_intraday_sample(
            _Client(self.days),
            self.request,
            snapshot_loader=lambda _: payload,
            sleep=lambda _: None,
        )
        self.assertEqual(result["status"], "FAIL")
        self.assertEqual(result["referenceCrosscheck"]["status"], "FAIL")

    def test_request_rejects_reference_date_outside_range(self):
        with self.assertRaisesRegex(ValueError, "reference date"):
            TushareIntradayQualificationRequest(
                codes=self.codes,
                start_date=self.days[0],
                end_date=self.days[-1],
                reference_date=self.days[-1] + timedelta(days=1),
                reference_snapshot=Path("reference.json.gz"),
            )


if __name__ == "__main__":
    unittest.main()
