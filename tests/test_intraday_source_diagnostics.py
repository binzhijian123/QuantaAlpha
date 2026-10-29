import unittest
from datetime import datetime, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

from scripts.diagnose_intraday_sources import DiagnosticClient, summarize_rows
from market_data.providers.a_stock_data import AStockDataClient
from market_data.providers.a_stock_data_intraday_v3 import IndependentTransportRealtimeClient


NOW = datetime(2026, 9, 14, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai"))


def quote(**updates):
    return {
        "symbol": "600000.SH", "price": 10.0, "previousClose": 10.1,
        "turnoverPercent": 1.0, "isStale": False,
        "tradeDate": "2026-09-14", "quoteAsOf": NOW.isoformat(), **updates,
    }


class SourceDiagnosticTests(unittest.TestCase):
    def test_rejection_categories_and_exact_age_boundaries(self):
        cases = [
            ({}, "usable"), ({"isStale": True}, "flaggedStale"),
            ({"tradeDate": "2026-09-11"}, "wrongTradeDate"),
            ({"quoteAsOf": "invalid"}, "invalidTimestamp"),
            ({"quoteAsOf": (NOW + timedelta(seconds=6)).isoformat()}, "futureQuote"),
            ({"quoteAsOf": (NOW + timedelta(seconds=5)).isoformat()}, "usable"),
            ({"quoteAsOf": (NOW - timedelta(seconds=121)).isoformat()}, "expiredQuote"),
            ({"quoteAsOf": (NOW - timedelta(seconds=120)).isoformat()}, "usable"),
            ({"price": None}, "invalidPrice"),
            ({"previousClose": None}, "missingCoreFields"),
            ({"turnoverPercent": None}, "missingCoreFields"),
        ]
        for updates, reason in cases:
            with self.subTest(reason=reason, updates=updates):
                result = summarize_rows({"600000.SH": quote(**updates)}, expected_day="2026-09-14", reference=NOW)
                self.assertEqual(result["reasons"], {reason: 1})

    def test_benchmark_does_not_require_turnover(self):
        result = summarize_rows({"000300.SH": quote(turnoverPercent=None)}, expected_day="2026-09-14", reference=NOW)
        self.assertEqual(result["reasons"], {"usable": 1})

    def test_probe_preserves_fallback_and_detects_request_clock_rejection(self):
        response_time = NOW + timedelta(seconds=8)
        rows = {"600000.SH": quote(quoteAsOf=response_time.isoformat())}
        times = iter([NOW, NOW, response_time, response_time, response_time, response_time, response_time, response_time, response_time, response_time])
        with patch.object(AStockDataClient, "_tencent_realtime_quotes", return_value=rows), \
             patch.object(IndependentTransportRealtimeClient, "_easy_tdx_realtime_quotes", return_value=rows), \
             patch.object(AStockDataClient, "_eastmoney_realtime_quotes", return_value=rows), \
             patch.object(AStockDataClient, "_sina_realtime_quotes", return_value=rows):
            report = DiagnosticClient(clock=lambda: next(times)).diagnose(["600000.SH"])
        self.assertEqual(report["returned"], 0)
        self.assertEqual(report["errorType"], "RuntimeError")
        self.assertEqual([r["source"] for r in report["sources"]], ["tencent", "easy_tdx", "eastmoney", "sina"])
        self.assertEqual(report["sources"][0]["atRequestStart"]["reasons"], {"futureQuote": 1})
        self.assertEqual(report["sources"][0]["atResponse"]["reasons"], {"usable": 1})

    def test_success_does_not_probe_unused_sources(self):
        with patch.object(AStockDataClient, "_tencent_realtime_quotes", return_value={"600000.SH": quote()}), \
             patch.object(IndependentTransportRealtimeClient, "_easy_tdx_realtime_quotes") as fallback:
            report = DiagnosticClient(clock=lambda: NOW).diagnose(["600000.SH"])
        self.assertEqual(report["returned"], 1)
        self.assertIsNone(report["errorType"])
        fallback.assert_not_called()

    def test_exception_body_is_not_exposed(self):
        with patch.object(AStockDataClient, "_tencent_realtime_quotes", side_effect=RuntimeError("secret-token-value")), \
             patch.object(IndependentTransportRealtimeClient, "_easy_tdx_realtime_quotes", return_value={"600000.SH": quote()}):
            report = DiagnosticClient(clock=lambda: NOW).diagnose(["600000.SH"])
        self.assertEqual(report["sources"][0]["errorType"], "RuntimeError")
        self.assertIn("failedAt", report["sources"][0])
        self.assertEqual(report["sources"][0]["durationSeconds"], 0)
        self.assertNotIn("secret-token-value", str(report))
        self.assertEqual(report["returned"], 1)


if __name__ == "__main__":
    unittest.main()
