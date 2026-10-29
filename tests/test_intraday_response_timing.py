import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

from tests.test_intraday_capture import CHINA_TZ, _write_provider
from market_data.intraday_capture import read_verified_snapshot, run_capture_cycle
from market_data.intraday_shadow_v6 import IntradayShadowCoordinator
from market_data.providers.a_stock_data_intraday_v3 import REALTIME_CONTRACT_SHA256 as V3_HASH
from market_data.providers.a_stock_data_intraday_v4 import (
    REALTIME_CONTRACT_SHA256, ResponseTimedRealtimeClient,
)

START = datetime(2026, 8, 12, 10, 0, tzinfo=CHINA_TZ)


class Clock:
    def __init__(self):
        self.value = START

    def __call__(self):
        return self.value


def quote(symbol="600000.SH", timestamp=START, **updates):
    return {"symbol": symbol, "price": 10.0, "previousClose": 9.9,
            "turnoverPercent": 1.0, "tradeDate": timestamp.date().isoformat(),
            "quoteAsOf": timestamp.isoformat(), "isStale": False,
            "source": "a-stock-data/tencent-public", **updates}


class FixtureClient(ResponseTimedRealtimeClient):
    def __init__(self, clock, fetch):
        super().__init__(clock=clock)
        self.fetch = fetch
        self.calls = []

    def _tencent_realtime_quotes(self, requested):
        self.calls.append("tencent")
        return self.fetch(requested)

    def _easy_tdx_realtime_quotes(self, requested):
        self.calls.append("easy_tdx")
        return {}

    def _eastmoney_realtime_quotes(self, requested, **kwargs):
        self.calls.append("eastmoney")
        self.eastmoney_kwargs = kwargs
        return {}

    def _sina_realtime_quotes(self, requested):
        self.calls.append("sina")
        return {}


def fetch_quotes(client, codes=None):
    return client.realtime_quotes(codes or ["600000.SH"], now=START,
                                 expected_trade_date="2026-08-12", max_quote_age_seconds=120)


class ResponseTimingTests(unittest.TestCase):
    def test_delayed_response_uses_receipt_clock(self):
        clock = Clock()
        def fetch(requested):
            clock.value += timedelta(seconds=8)
            return {"600000.SH": quote(timestamp=clock.value)}
        client = FixtureClient(clock, fetch)
        result = fetch_quotes(client)
        self.assertIn("600000.SH", result)
        self.assertEqual(client.calls, ["tencent"])
        self.assertNotEqual(REALTIME_CONTRACT_SHA256, V3_HASH)
        self.assertEqual(result["600000.SH"]["realtimeContractSha256"], REALTIME_CONTRACT_SHA256)

    def test_real_future_and_expiry_boundaries(self):
        for age, valid in [(-6, False), (-5, True), (120, True), (121, False)]:
            with self.subTest(age=age):
                client = FixtureClient(Clock(), lambda requested: {
                    "600000.SH": quote(timestamp=START-timedelta(seconds=age)),
                })
                if valid:
                    self.assertIn("600000.SH", fetch_quotes(client))
                else:
                    with self.assertRaises(RuntimeError): fetch_quotes(client)
                    self.assertEqual(client.calls, ["tencent", "easy_tdx", "eastmoney", "sina"])
                    self.assertIsNone(client.eastmoney_kwargs["now"])
                    self.assertEqual(client.eastmoney_kwargs["max_quote_age_seconds"], 120)

    def test_wrong_day_stale_and_naive_quotes_fail(self):
        for updates in [{"tradeDate": "2026-08-11"}, {"isStale": True},
                        {"quoteAsOf": START.replace(tzinfo=None).isoformat()}]:
            with self.subTest(updates=updates):
                client = FixtureClient(Clock(), lambda requested: {"600000.SH": quote(**updates)})
                with self.assertRaises(RuntimeError): fetch_quotes(client)

    def test_clock_regression_is_not_hidden_by_fallback(self):
        clock = Clock()
        def fetch(requested):
            clock.value -= timedelta(seconds=1)
            return {"600000.SH": quote()}
        client = FixtureClient(clock, fetch)
        with self.assertRaisesRegex(ValueError, "backwards"): fetch_quotes(client)
        self.assertEqual(client.calls, ["tencent"])

    def test_frozen_v9_parent_rejects_candidate_contract(self):
        snapshot = {"quotes": [quote(realtimeContractSha256=REALTIME_CONTRACT_SHA256)]}
        with self.assertRaisesRegex(ValueError, "source contract"):
            IntradayShadowCoordinator._quality_quote_map(None, snapshot, label="candidate")

    def test_final_check_expires_earlier_source_during_slow_fallback(self):
        clock = Clock()
        client = FixtureClient(clock, lambda requested: {"600000.SH": quote()})
        def slow_tdx(requested):
            clock.value += timedelta(seconds=121)
            return {"600001.SH": quote("600001.SH", clock.value)}
        with patch.object(client, "_easy_tdx_realtime_quotes", side_effect=slow_tdx):
            result = fetch_quotes(client, ["600000.SH", "600001.SH"])
        self.assertEqual(set(result), {"600001.SH"})

    def test_missing_core_fields_continue_fallback_without_field_merge(self):
        client = FixtureClient(Clock(), lambda requested: {"600000.SH": quote(turnoverPercent=None)})
        with patch.object(client, "_easy_tdx_realtime_quotes", return_value={
            "600000.SH": quote(price=11, source="a-stock-data/easy-tdx-public"),
        }): result = fetch_quotes(client)
        self.assertEqual(result["600000.SH"]["price"], 11)
        self.assertEqual(result["600000.SH"]["source"], "a-stock-data/easy-tdx-public")

    def test_eastmoney_internal_filter_uses_response_wall_clock(self):
        clock = Clock()
        class ResponseClock(datetime):
            @classmethod
            def now(cls, tz=None):
                return clock.value.astimezone(tz) if tz else clock.value.replace(tzinfo=None)

        session = Mock()
        def get(*args, **kwargs):
            clock.value += timedelta(seconds=8)
            response = Mock()
            response.json.return_value = {"rc": 0, "data": {"diff": [{
                "f12": "600000", "f13": 1, "f2": 1000, "f18": 990,
                "f8": 100, "f6": 100000, "f124": int(clock.value.timestamp()),
            }]}}
            return response
        session.get.side_effect = get
        client = ResponseTimedRealtimeClient(clock=clock, session=session)
        with patch.object(client, "_tencent_realtime_quotes", return_value={}), \
             patch.object(client, "_easy_tdx_realtime_quotes", return_value={}), \
             patch.object(client, "_sina_realtime_quotes") as sina, \
             patch("market_data.providers.a_stock_data.datetime", ResponseClock):
            result = fetch_quotes(client)
        self.assertEqual(result["600000.SH"]["source"], "a-stock-data/eastmoney-public")
        self.assertEqual(session.get.call_count, 1)
        sina.assert_not_called()

    def test_delayed_client_integrates_with_real_collector_in_temporary_root(self):
        clock = Clock()
        def fetch(requested):
            clock.value += timedelta(seconds=8)
            return {s: quote(s, clock.value) for s, _ in requested}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            status = run_capture_cycle(_write_provider(root), root/"capture",
                                       client=FixtureClient(clock, fetch), clock=clock)
            self.assertEqual(status["status"], "CAPTURED")
            self.assertEqual(status["latest"]["usableQuoteCount"], 301)
            snapshot = read_verified_snapshot(status["latest"]["file"])
            self.assertEqual(snapshot["captureRequestedAt"], START.isoformat())
            self.assertEqual(snapshot["availableAt"], (START+timedelta(seconds=8)).isoformat())
            self.assertTrue(all(q["realtimeContractSha256"] == REALTIME_CONTRACT_SHA256 for q in snapshot["quotes"]))


if __name__ == "__main__":
    unittest.main()
