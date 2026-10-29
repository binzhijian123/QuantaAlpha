import json
import sys
import tempfile
import unittest
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import requests


BACKEND_ROOT = Path(__file__).resolve().parents[1] / "frontend-v2" / "backend"
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from market_data.providers.a_stock_data import AStockDataClient, normalize_stock_code
from market_data.providers.a_stock_data_intraday_v2 import CoreFieldRealtimeClient
from market_data.providers.a_stock_data_intraday_v3 import (
    REALTIME_CONTRACT_SHA256,
    IndependentTransportRealtimeClient,
    normalize_easy_tdx_quote_rows,
)
from market_data.realtime_service import build_paper_realtime_state
from market_data.models import SyncRequest
from market_data.sync_service import (
    _promote_qlib_provider,
    read_minimal_qlib_provider,
    sync_a_stock_data_daily,
    write_minimal_qlib_provider,
)


class _FakeResponse:
    def __init__(self, text="", payload=None):
        self.text = text
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        if self.payload is not None:
            return self.payload
        return json.loads(self.text)


class _TencentSession:
    headers = {}

    def get(self, url, **kwargs):
        payload = {
            "data": {
                "sz000001": {
                    "day": [
                        ["2026-07-14", "10", "10.2", "10.3", "9.9", "12.5", {}, "0", "3.4"]
                    ]
                }
            }
        }
        return _FakeResponse("kline_day=" + json.dumps(payload))


class _TencentStarSession:
    headers = {}

    def get(self, url, **kwargs):
        payload = {
            "data": {
                "sh688981": {
                    "day": [
                        ["2026-07-14", "100", "101", "102", "99", "125000", {}, "0", "1262.5"]
                    ]
                }
            }
        }
        return _FakeResponse("kline_day=" + json.dumps(payload))


class _FallbackSession:
    headers = {}

    def get(self, url, **kwargs):
        if "finance.qq.com" in url:
            raise requests.ConnectionError("primary unavailable")
        if url.endswith("last.js"):
            payload = {"data": "20260714,10,10.3,9.9,10.2,1250,34000,0"}
            return _FakeResponse("callback(" + json.dumps(payload) + ")")
        payload = {
            "hs_000001": {
                "1": "20260715",
                "7": "10.2",
                "8": "10.4",
                "9": "10.1",
                "11": "10.3",
                "13": 1500,
                "19": "40000",
            }
        }
        return _FakeResponse("callback(" + json.dumps(payload) + ")")


def _quote_line(
    market_code: str,
    name: str,
    price: str,
    previous_close: str,
    quote_time: str,
    amount_wan: str,
    turnover_percent: str = "2.30",
) -> str:
    values = [""] * 88
    values[1] = name
    values[3] = price
    values[4] = previous_close
    values[5] = previous_close
    values[30] = quote_time
    values[31] = "1.50"
    values[32] = "1.25"
    values[33] = price
    values[34] = previous_close
    values[37] = amount_wan
    values[38] = turnover_percent
    return f'v_{market_code}="{"~".join(values)}";'


class _TencentQuoteSession:
    headers = {}

    def get(self, url, **kwargs):
        return _FakeResponse(
            _quote_line("sh600519", "贵州茅台", "1350.50", "1348.00", "20260811103045", "12345")
            + _quote_line("sh000300", "沪深300", "4663.79", "4702.02", "20260811103040", "64538476")
        )


def _sina_quote_line(
    market_code: str,
    name: str,
    open_price: str,
    previous_close: str,
    price: str,
    high: str,
    low: str,
    amount_cny: str,
    quote_date: str,
    quote_time: str,
) -> str:
    values = [""] * 33
    values[0] = name
    values[1] = open_price
    values[2] = previous_close
    values[3] = price
    values[4] = high
    values[5] = low
    values[9] = amount_cny
    values[30] = quote_date
    values[31] = quote_time
    return f'var hq_str_{market_code}="{",".join(values)}";'


class _SinaFallbackQuoteSession:
    headers = {}

    def __init__(
        self,
        *,
        partial_tencent=False,
        stale_tencent=False,
        old_date_tencent=False,
    ):
        self.partial_tencent = partial_tencent
        self.stale_tencent = stale_tencent
        self.old_date_tencent = old_date_tencent
        self.urls = []

    def get(self, url, **kwargs):
        self.urls.append(url)
        if "gtimg.cn" in url:
            if self.old_date_tencent:
                return _FakeResponse(
                    _quote_line(
                        "sh600519",
                        "贵州茅台",
                        "1350.50",
                        "1348.00",
                        "20260810103045",
                        "12345",
                    )
                )
            if self.stale_tencent:
                return _FakeResponse(
                    _quote_line(
                        "sh600519",
                        "贵州茅台",
                        "1348.00",
                        "1348.00",
                        "20260811103045",
                        "0",
                    )
                )
            if self.partial_tencent:
                return _FakeResponse(
                    _quote_line(
                        "sh600519",
                        "贵州茅台",
                        "1350.50",
                        "1348.00",
                        "20260811103045",
                        "12345",
                    )
                )
            raise requests.ConnectionError("Tencent unavailable")
        return _FakeResponse(
            _sina_quote_line(
                "sh600519",
                "贵州茅台",
                "1348.00",
                "1348.86",
                "1346.50",
                "1352.65",
                "1338.00",
                "3640046368.00",
                "2026-08-11",
                "15:34:59",
            )
            + _sina_quote_line(
                "sh000300",
                "沪深300",
                "4689.4493",
                "4702.0247",
                "4663.7887",
                "4715.8812",
                "4660.4742",
                "645384756136",
                "2026-08-11",
                "15:35:41",
            )
        )


class _UnavailableQuoteSession:
    headers = {}

    def get(self, url, **kwargs):
        raise requests.ConnectionError(f"unavailable: {url}")


def _eastmoney_quote_payload(codes, quote_time):
    rows = {
        "600519.SH": {
            "f2": 134300,
            "f3": -26,
            "f4": -350,
            "f6": 4_717_613_108.0,
            "f8": 123,
            "f12": "600519",
            "f13": 1,
            "f14": "贵州茅台",
            "f15": 135688,
            "f16": 133251,
            "f17": 134650,
            "f18": 134650,
            "f124": quote_time,
        },
        "000001.SZ": {
            "f2": 1125,
            "f3": -9,
            "f4": -1,
            "f6": 711_612_877.97,
            "f8": 45,
            "f12": "000001",
            "f13": 0,
            "f14": "平安银行",
            "f15": 1129,
            "f16": 1120,
            "f17": 1126,
            "f18": 1126,
            "f124": quote_time,
        },
        "000300.SH": {
            "f2": 469092,
            "f3": 58,
            "f4": 2713,
            "f6": 575_326_282_008.4,
            "f8": None,
            "f12": "000300",
            "f13": 1,
            "f14": "沪深300",
            "f15": 470043,
            "f16": 465769,
            "f17": 466047,
            "f18": 466379,
            "f124": quote_time,
        },
    }
    return {
        "rc": 0,
        "data": {"diff": [rows[code] for code in codes]},
    }


class _EastmoneyFallbackQuoteSession:
    headers = {}

    def __init__(self, *, progressive=False, first_node_fails=False):
        self.progressive = progressive
        self.first_node_fails = first_node_fails
        self.calls = []
        self.quote_time = int(
            datetime(
                2026,
                8,
                12,
                15,
                0,
                tzinfo=ZoneInfo("Asia/Shanghai"),
            ).timestamp()
        )

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if "gtimg.cn" in url:
            if self.progressive:
                return _FakeResponse(
                    _quote_line(
                        "sh600519",
                        "贵州茅台",
                        "1343.00",
                        "1346.50",
                        "20260812150000",
                        "471761",
                    )
                )
            raise requests.ConnectionError("Tencent unavailable")
        if "sinajs.cn" in url:
            if self.progressive:
                return _FakeResponse(
                    _sina_quote_line(
                        "sh000300",
                        "沪深300",
                        "4660.47",
                        "4663.79",
                        "4690.92",
                        "4700.43",
                        "4657.69",
                        "575326282008.4",
                        "2026-08-12",
                        "15:00:00",
                    )
                )
            raise requests.ConnectionError("Sina unavailable")
        if self.first_node_fails and (
            "push2delay" in url or "18.push2" in url
        ):
            raise requests.ConnectionError("Eastmoney node unavailable")
        secids = kwargs["params"]["secids"].split(",")
        code_by_secid = {
            "1.600519": "600519.SH",
            "0.000001": "000001.SZ",
            "1.000300": "000300.SH",
        }
        codes = [code_by_secid[secid] for secid in secids]
        return _FakeResponse(
            payload=_eastmoney_quote_payload(codes, self.quote_time)
        )


class _CoreFieldFallbackQuoteSession:
    headers = {}

    def __init__(self):
        self.calls = []
        self.quote_time = int(
            datetime(
                2026,
                8,
                12,
                15,
                0,
                tzinfo=ZoneInfo("Asia/Shanghai"),
            ).timestamp()
        )

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if "gtimg.cn" in url:
            return _FakeResponse(
                _quote_line(
                    "sh600519",
                    "贵州茅台",
                    "1343.00",
                    "1346.50",
                    "20260812150000",
                    "471761",
                    turnover_percent="",
                )
                + _quote_line(
                    "sh000300",
                    "沪深300",
                    "4690.92",
                    "4663.79",
                    "20260812150000",
                    "57532628",
                    turnover_percent="",
                )
            )
        if "eastmoney.com" in url:
            secids = kwargs["params"]["secids"].split(",")
            code_by_secid = {
                "1.600519": "600519.SH",
                "1.000300": "000300.SH",
            }
            codes = [code_by_secid[secid] for secid in secids]
            return _FakeResponse(
                payload=_eastmoney_quote_payload(codes, self.quote_time)
            )
        return _FakeResponse(
            _sina_quote_line(
                "sh600519",
                "贵州茅台",
                "1346.50",
                "1346.50",
                "1343.00",
                "1356.88",
                "1332.51",
                "4717613108",
                "2026-08-12",
                "15:00:00",
            )
        )


class _BenchmarkSinaQuoteSession:
    headers = {}

    def __init__(self):
        self.urls = []

    def get(self, url, **kwargs):
        self.urls.append(url)
        if "gtimg.cn" in url or "eastmoney.com" in url:
            raise requests.ConnectionError("source unavailable")
        return _FakeResponse(
            _sina_quote_line(
                "sh000300",
                "沪深300",
                "4660.47",
                "4663.79",
                "4690.92",
                "4700.43",
                "4657.69",
                "575326282008.4",
                "2026-08-12",
                "15:00:00",
            )
        )


class _AllUnusableQuoteSession(_EastmoneyFallbackQuoteSession):
    def get(self, url, **kwargs):
        if "gtimg.cn" in url:
            return _FakeResponse(
                _quote_line(
                    "sh600519",
                    "贵州茅台",
                    "1346.50",
                    "1346.50",
                    "20260811150000",
                    "0",
                )
            )
        if "sinajs.cn" in url:
            return _FakeResponse(
                _sina_quote_line(
                    "sh600519",
                    "贵州茅台",
                    "1346.50",
                    "1346.50",
                    "1346.50",
                    "1346.50",
                    "1346.50",
                    "0",
                    "2026-08-11",
                    "15:00:00",
                )
            )
        payload = _eastmoney_quote_payload(["600519.SH"], self.quote_time)
        payload["data"]["diff"][0]["f2"] = 0
        return _FakeResponse(payload=payload)


class _RealtimeClient:
    def realtime_quotes(self, codes):
        return {
            "600519.SH": {
                "symbol": "600519.SH",
                "price": 60.0,
                "quoteAsOf": "2026-08-11T09:59:30+08:00",
                "isStale": False,
                "source": "a-stock-data/sina-public",
            },
            "000300.SH": {
                "symbol": "000300.SH",
                "name": "沪深300",
                "price": 4663.79,
                "changePercent": -0.81,
                "quoteAsOf": "2026-08-11T09:59:20+08:00",
                "isStale": False,
                "source": "a-stock-data/sina-public",
            },
        }


class AStockDataProviderTests(unittest.TestCase):
    def test_code_normalization(self):
        self.assertEqual(normalize_stock_code("000001.SZ"), ("000001", "SZ", "sz000001"))
        self.assertEqual(normalize_stock_code("SH600000"), ("600000", "SH", "sh600000"))

    def test_tencent_units_are_normalized(self):
        frame = AStockDataClient(session=_TencentSession()).history_quotation(
            "000001.SZ", "2026-07-14", "2026-07-14"
        )
        self.assertEqual(frame.loc[0, "volume"], 1250.0)
        self.assertEqual(frame.loc[0, "amount"], 34000.0)
        self.assertEqual(frame.loc[0, "source"], "tencent")

    def test_tencent_star_market_volume_is_already_in_shares(self):
        frame = AStockDataClient(session=_TencentStarSession()).history_quotation(
            "688981.SH", "2026-07-14", "2026-07-14"
        )
        self.assertEqual(frame.loc[0, "volume"], 125000.0)

    def test_tonghuashun_recent_fallback(self):
        frame = AStockDataClient(session=_FallbackSession()).history_quotation(
            "000001.SZ", "2026-07-14", "2026-07-15"
        )
        self.assertEqual(frame["date"].tolist(), ["2026-07-14", "2026-07-15"])
        self.assertEqual(set(frame["source"]), {"tonghuashun_public_fallback"})

    def test_tencent_realtime_quotes_are_normalized_and_timestamped(self):
        quotes = AStockDataClient(session=_TencentQuoteSession()).realtime_quotes(
            ["600519.SH", "SH600519", "000300.SH"]
        )

        self.assertEqual(sorted(quotes), ["000300.SH", "600519.SH"])
        self.assertEqual(quotes["600519.SH"]["price"], 1350.5)
        self.assertEqual(quotes["600519.SH"]["amountCny"], 123_450_000.0)
        self.assertEqual(
            quotes["600519.SH"]["quoteAsOf"],
            "2026-08-11T10:30:45+08:00",
        )
        self.assertFalse(quotes["600519.SH"]["isStale"])

    def test_sina_realtime_fallback_preserves_units_and_timestamp(self):
        session = _SinaFallbackQuoteSession()
        quotes = AStockDataClient(session=session).realtime_quotes(
            ["600519.SH", "000300.SH"]
        )

        self.assertEqual(sorted(quotes), ["000300.SH", "600519.SH"])
        self.assertEqual(quotes["600519.SH"]["price"], 1346.5)
        self.assertEqual(quotes["600519.SH"]["amountCny"], 3_640_046_368.0)
        self.assertEqual(
            quotes["600519.SH"]["quoteAsOf"],
            "2026-08-11T15:34:59+08:00",
        )
        self.assertAlmostEqual(
            quotes["600519.SH"]["changePercent"],
            (1346.5 - 1348.86) / 1348.86 * 100.0,
        )
        self.assertEqual(quotes["600519.SH"]["source"], "a-stock-data/sina-public")
        self.assertIn("hq.sinajs.cn", session.urls[-1])

    def test_sina_fallback_only_requests_missing_tencent_quotes(self):
        session = _SinaFallbackQuoteSession(partial_tencent=True)
        quotes = AStockDataClient(session=session).realtime_quotes(
            ["600519.SH", "000300.SH"]
        )

        self.assertEqual(quotes["600519.SH"]["source"], "a-stock-data/tencent-public")
        self.assertEqual(quotes["000300.SH"]["source"], "a-stock-data/sina-public")
        self.assertIn("list=sh000300", session.urls[-1])
        self.assertNotIn("sh600519", session.urls[-1])

    def test_sina_fallback_replaces_stale_tencent_quote(self):
        session = _SinaFallbackQuoteSession(stale_tencent=True)
        quotes = AStockDataClient(session=session).realtime_quotes(["600519.SH"])

        self.assertEqual(quotes["600519.SH"]["price"], 1346.5)
        self.assertEqual(
            quotes["600519.SH"]["source"], "a-stock-data/sina-public"
        )
        self.assertIn("list=sh600519", session.urls[-1])

    def test_expected_trade_date_routes_old_tencent_quote_to_sina(self):
        session = _SinaFallbackQuoteSession(old_date_tencent=True)
        quotes = AStockDataClient(session=session).realtime_quotes(
            ["600519.SH"],
            expected_trade_date="2026-08-11",
        )

        self.assertEqual(quotes["600519.SH"]["tradeDate"], "2026-08-11")
        self.assertEqual(quotes["600519.SH"]["source"], "a-stock-data/sina-public")
        self.assertIn("list=sh600519", session.urls[-1])

    def test_eastmoney_batch_third_fallback_preserves_units_and_mapping(self):
        session = _EastmoneyFallbackQuoteSession(first_node_fails=True)
        now = datetime(2026, 8, 12, 15, 0, 30, tzinfo=ZoneInfo("Asia/Shanghai"))
        quotes = AStockDataClient(session=session).realtime_quotes(
            ["600519.SH", "000001.SZ", "000300.SH"],
            expected_trade_date="2026-08-12",
            max_quote_age_seconds=120,
            now=now,
        )

        self.assertEqual(sorted(quotes), ["000001.SZ", "000300.SH", "600519.SH"])
        self.assertEqual(quotes["600519.SH"]["price"], 1343.0)
        self.assertEqual(quotes["600519.SH"]["previousClose"], 1346.5)
        self.assertEqual(quotes["600519.SH"]["amountCny"], 4_717_613_108.0)
        self.assertEqual(quotes["600519.SH"]["changePercent"], -0.26)
        self.assertEqual(quotes["600519.SH"]["turnoverPercent"], 1.23)
        self.assertEqual(quotes["000001.SZ"]["symbol"], "000001.SZ")
        self.assertEqual(quotes["000300.SH"]["symbol"], "000300.SH")
        self.assertTrue(all(
            quote["source"] == "a-stock-data/eastmoney-public"
            for quote in quotes.values()
        ))
        eastmoney_urls = [url for url, _ in session.calls if "eastmoney.com" in url]
        self.assertEqual(len(eastmoney_urls), 3)
        self.assertIn("82.push2.eastmoney.com", eastmoney_urls[-1])

    def test_progressive_fallback_only_requests_unresolved_symbols(self):
        session = _EastmoneyFallbackQuoteSession(progressive=True)
        now = datetime(2026, 8, 12, 15, 0, 30, tzinfo=ZoneInfo("Asia/Shanghai"))
        quotes = AStockDataClient(session=session).realtime_quotes(
            ["600519.SH", "000300.SH", "000001.SZ"],
            expected_trade_date="2026-08-12",
            max_quote_age_seconds=120,
            now=now,
        )

        self.assertEqual(
            quotes["600519.SH"]["source"], "a-stock-data/tencent-public"
        )
        self.assertEqual(quotes["000300.SH"]["source"], "a-stock-data/sina-public")
        self.assertEqual(
            quotes["000001.SZ"]["source"], "a-stock-data/eastmoney-public"
        )
        eastmoney_calls = [
            kwargs for url, kwargs in session.calls if "eastmoney.com" in url
        ]
        self.assertEqual(len(eastmoney_calls), 1)
        self.assertEqual(eastmoney_calls[0]["params"]["secids"], "0.000001")

    def test_core_field_fallback_continues_past_incomplete_tencent_stock(self):
        session = _CoreFieldFallbackQuoteSession()
        now = datetime(2026, 8, 12, 15, 0, 30, tzinfo=ZoneInfo("Asia/Shanghai"))

        quotes = CoreFieldRealtimeClient(session=session).realtime_quotes(
            ["600519.SH", "000300.SH"],
            expected_trade_date="2026-08-12",
            max_quote_age_seconds=120,
            now=now,
        )

        self.assertEqual(
            quotes["600519.SH"]["source"],
            "a-stock-data/eastmoney-public",
        )
        self.assertEqual(quotes["600519.SH"]["turnoverPercent"], 1.23)
        self.assertEqual(
            quotes["000300.SH"]["source"],
            "a-stock-data/tencent-public",
        )
        eastmoney_calls = [
            kwargs for url, kwargs in session.calls if "eastmoney.com" in url
        ]
        self.assertEqual(len(eastmoney_calls), 1)
        self.assertEqual(eastmoney_calls[0]["params"]["secids"], "1.600519")
        self.assertFalse(any("sinajs.cn" in url for url, _ in session.calls))

    def test_core_field_fallback_allows_benchmark_without_turnover(self):
        session = _BenchmarkSinaQuoteSession()
        now = datetime(2026, 8, 12, 15, 0, 30, tzinfo=ZoneInfo("Asia/Shanghai"))

        quotes = CoreFieldRealtimeClient(session=session).realtime_quotes(
            ["000300.SH"],
            expected_trade_date="2026-08-12",
            max_quote_age_seconds=120,
            now=now,
        )

        self.assertEqual(
            quotes["000300.SH"]["source"],
            "a-stock-data/sina-public",
        )
        self.assertIsNone(quotes["000300.SH"]["turnoverPercent"])
        self.assertTrue(any("eastmoney.com" in url for url in session.urls))
        self.assertIn("hq.sinajs.cn", session.urls[-1])

    def test_easy_tdx_rows_preserve_core_fields_units_and_server_time(self):
        quotes = normalize_easy_tdx_quote_rows(
            [
                {
                    "market": 1,
                    "code": "600519",
                    "name": "贵州茅台",
                    "pre_close": 1355.29,
                    "open": 1355.0,
                    "high": 1359.0,
                    "low": 1338.14,
                    "close": 1341.99,
                    "vol": 29853,
                    "amount": 4_024_065_536.0,
                    "turnover": 0.2388084,
                    "server_update_date": 20260814,
                    "server_update_time": 145023,
                },
                {
                    "market": 0,
                    "code": "000001",
                    "name": "平安银行",
                    "pre_close": 11.25,
                    "open": 11.22,
                    "high": 11.23,
                    "low": 11.11,
                    "close": 11.11,
                    "vol": 832343,
                    "amount": 929_098_432.0,
                    "turnover": 0.4289189,
                    "server_update_date": 20260814,
                    "server_update_time": 145022,
                },
            ],
            {"600519.SH", "000001.SZ"},
        )

        self.assertEqual(sorted(quotes), ["000001.SZ", "600519.SH"])
        self.assertEqual(quotes["600519.SH"]["price"], 1341.99)
        self.assertEqual(quotes["600519.SH"]["previousClose"], 1355.29)
        self.assertEqual(quotes["600519.SH"]["turnoverPercent"], 0.2388084)
        self.assertEqual(quotes["600519.SH"]["amountCny"], 4_024_065_536.0)
        self.assertEqual(
            quotes["600519.SH"]["quoteAsOf"],
            "2026-08-14T14:50:23+08:00",
        )
        self.assertEqual(
            quotes["600519.SH"]["source"],
            "a-stock-data/easy-tdx-public",
        )

    def test_independent_transport_only_requests_unresolved_core_quotes(self):
        now = datetime(2026, 8, 14, 14, 50, 30, tzinfo=ZoneInfo("Asia/Shanghai"))
        client = IndependentTransportRealtimeClient(session=_UnavailableQuoteSession())
        incomplete_tencent = {
            "600519.SH": {
                "symbol": "600519.SH",
                "price": 1341.99,
                "previousClose": 1355.29,
                "turnoverPercent": None,
                "quoteAsOf": "2026-08-14T14:50:20+08:00",
                "tradeDate": "2026-08-14",
                "isStale": False,
                "source": "a-stock-data/tencent-public",
            },
            "000300.SH": {
                "symbol": "000300.SH",
                "price": 4665.88,
                "previousClose": 4663.95,
                "turnoverPercent": None,
                "quoteAsOf": "2026-08-14T14:50:20+08:00",
                "tradeDate": "2026-08-14",
                "isStale": False,
                "source": "a-stock-data/tencent-public",
            },
        }
        tdx_quote = {
            "600519.SH": {
                **incomplete_tencent["600519.SH"],
                "turnoverPercent": 0.2388084,
                "source": "a-stock-data/easy-tdx-public",
            }
        }
        with (
            patch.object(
                client,
                "_tencent_realtime_quotes",
                return_value=incomplete_tencent,
            ),
            patch.object(
                client,
                "_easy_tdx_realtime_quotes",
                return_value=tdx_quote,
            ) as easy_tdx,
            patch.object(client, "_eastmoney_realtime_quotes") as eastmoney,
            patch.object(client, "_sina_realtime_quotes") as sina,
        ):
            quotes = client.realtime_quotes(
                ["600519.SH", "000300.SH"],
                expected_trade_date="2026-08-14",
                max_quote_age_seconds=120,
                now=now,
            )

        self.assertEqual(
            quotes["600519.SH"]["source"],
            "a-stock-data/easy-tdx-public",
        )
        self.assertEqual(
            quotes["000300.SH"]["source"],
            "a-stock-data/tencent-public",
        )
        self.assertEqual(
            quotes["600519.SH"]["realtimeContractSha256"],
            REALTIME_CONTRACT_SHA256,
        )
        self.assertEqual(
            quotes["000300.SH"]["realtimeContractSha256"],
            REALTIME_CONTRACT_SHA256,
        )
        self.assertEqual(
            easy_tdx.call_args.args[0],
            [("600519.SH", "sh600519")],
        )
        eastmoney.assert_not_called()
        sina.assert_not_called()

    def test_independent_transport_rejects_unpinned_easy_tdx_version(self):
        client = IndependentTransportRealtimeClient(session=_UnavailableQuoteSession())
        with (
            patch(
                "market_data.providers.a_stock_data_intraday_v3.importlib_metadata.version",
                return_value="1.20.5",
            ),
            self.assertRaisesRegex(
                RuntimeError,
                "version mismatch: expected 1.20.6, got 1.20.5",
            ),
        ):
            client._easy_tdx_realtime_quotes([("600519.SH", "sh600519")])

    def test_independent_transport_failure_remains_fail_closed(self):
        client = IndependentTransportRealtimeClient(session=_UnavailableQuoteSession())
        with (
            patch.object(
                client,
                "_easy_tdx_realtime_quotes",
                side_effect=RuntimeError("TCP unavailable"),
            ),
            self.assertRaisesRegex(
                RuntimeError,
                "Tencent=.*unavailable.*EasyTdx=TCP unavailable.*Eastmoney=.*unavailable.*Sina=.*unavailable",
            ),
        ):
            client.realtime_quotes(["600519.SH"])

    def test_all_sources_with_only_unusable_rows_fail_closed(self):
        now = datetime(2026, 8, 12, 15, 0, 30, tzinfo=ZoneInfo("Asia/Shanghai"))
        with self.assertRaisesRegex(RuntimeError, "all rows were unusable"):
            AStockDataClient(session=_AllUnusableQuoteSession()).realtime_quotes(
                ["600519.SH"],
                expected_trade_date="2026-08-12",
                max_quote_age_seconds=120,
                now=now,
            )

    def test_realtime_failure_reports_both_sources(self):
        with self.assertRaisesRegex(
            RuntimeError,
            "Tencent=.*unavailable.*Sina=.*unavailable.*Eastmoney=.*unavailable",
        ):
            AStockDataClient(session=_UnavailableQuoteSession()).realtime_quotes(
                ["600519.SH"]
            )

    def test_realtime_overlay_is_provisional_and_does_not_mutate_paper_state(self):
        paper_state = {
            "account": {
                "cash": 500.0,
                "initialCash": 1_000.0,
                "totalValue": 1_000.0,
            },
            "positions": [
                {
                    "symbol": "600519.SH",
                    "quantity": 10,
                    "lastPrice": 50.0,
                }
            ],
        }
        original = deepcopy(paper_state)

        result = build_paper_realtime_state(
            paper_state,
            "2026-08-10",
            client=_RealtimeClient(),
            now=datetime(2026, 8, 11, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
        )

        self.assertEqual(result["status"], "LIVE")
        self.assertEqual(result["account"]["estimatedTotalValue"], 1_100.0)
        self.assertEqual(result["account"]["coveredPositions"], 1)
        self.assertEqual(result["reconciliationStatus"], "AWAITING_OFFICIAL_DAILY")
        self.assertFalse(result["evidenceEligible"])
        self.assertFalse(result["persisted"])
        self.assertEqual(result["source"], "a-stock-data/sina-public")
        self.assertEqual(result["quoteSources"], ["a-stock-data/sina-public"])
        self.assertEqual(paper_state, original)

    def test_realtime_overlay_reports_mixed_upstreams(self):
        result = build_paper_realtime_state(
            {
                "account": {
                    "cash": 500.0,
                    "initialCash": 1_000.0,
                    "totalValue": 1_000.0,
                },
                "positions": [
                    {"symbol": "600519.SH", "quantity": 10, "lastPrice": 50.0}
                ],
            },
            "2026-08-10",
            prefetched_quotes={
                "600519.SH": {
                    "symbol": "600519.SH",
                    "price": 60.0,
                    "quoteAsOf": "2026-08-11T09:59:30+08:00",
                    "isStale": False,
                    "source": "a-stock-data/sina-public",
                },
                "000300.SH": {
                    "symbol": "000300.SH",
                    "price": 4663.79,
                    "quoteAsOf": "2026-08-11T09:59:20+08:00",
                    "isStale": False,
                    "source": "a-stock-data/tencent-public",
                },
            },
            now=datetime(2026, 8, 11, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
        )

        self.assertEqual(result["source"], "a-stock-data/mixed")
        self.assertEqual(
            result["quoteSources"],
            ["a-stock-data/sina-public", "a-stock-data/tencent-public"],
        )
        self.assertFalse(result["persisted"])
        self.assertFalse(result["evidenceEligible"])

    def test_fresh_benchmark_cannot_hide_delayed_position_quote(self):
        result = build_paper_realtime_state(
            {
                "account": {
                    "cash": 500.0,
                    "initialCash": 1_000.0,
                    "totalValue": 1_000.0,
                },
                "positions": [
                    {"symbol": "600519.SH", "quantity": 10, "lastPrice": 50.0}
                ],
            },
            "2026-08-10",
            prefetched_quotes={
                "600519.SH": {
                    "symbol": "600519.SH",
                    "price": 60.0,
                    "quoteAsOf": "2026-08-11T09:55:00+08:00",
                    "isStale": False,
                    "source": "a-stock-data/tencent-public",
                },
                "000300.SH": {
                    "symbol": "000300.SH",
                    "price": 4663.79,
                    "quoteAsOf": "2026-08-11T09:59:30+08:00",
                    "isStale": False,
                    "source": "a-stock-data/tencent-public",
                },
            },
            now=datetime(2026, 8, 11, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
        )

        self.assertEqual(result["status"], "DELAYED")
        self.assertEqual(result["ageSeconds"], 30.0)
        self.assertEqual(result["delayedSymbols"], ["600519.SH"])
        self.assertEqual(result["account"]["estimatedTotalValue"], 1_000.0)
        self.assertEqual(result["account"]["coveredPositions"], 0)
        self.assertIsNone(result["positions"][0]["livePrice"])
        self.assertEqual(result["positions"][0]["valuationPrice"], 50.0)
        self.assertEqual(result["positions"][0]["quoteAgeSeconds"], 300.0)
        self.assertTrue(result["positions"][0]["isDelayed"])

    def test_lunch_break_does_not_age_out_last_morning_quote(self):
        result = build_paper_realtime_state(
            {
                "account": {
                    "cash": 500.0,
                    "initialCash": 1_000.0,
                    "totalValue": 1_000.0,
                },
                "positions": [
                    {"symbol": "600519.SH", "quantity": 10, "lastPrice": 50.0}
                ],
            },
            "2026-08-10",
            prefetched_quotes={
                "600519.SH": {
                    "symbol": "600519.SH",
                    "price": 60.0,
                    "quoteAsOf": "2026-08-11T11:29:30+08:00",
                    "isStale": False,
                    "source": "a-stock-data/tencent-public",
                },
                "000300.SH": {
                    "symbol": "000300.SH",
                    "price": 4663.79,
                    "quoteAsOf": "2026-08-11T11:29:30+08:00",
                    "isStale": False,
                    "source": "a-stock-data/tencent-public",
                },
            },
            now=datetime(2026, 8, 11, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
        )

        self.assertEqual(result["status"], "CLOSED")
        self.assertFalse(result["delayed"])
        self.assertEqual(result["delayedSymbols"], [])
        self.assertEqual(result["account"]["estimatedTotalValue"], 1_100.0)
        self.assertEqual(result["account"]["coveredPositions"], 1)

    def test_qlib_writer_preserves_missing_calendar_dates(self):
        frame = pd.DataFrame(
            [
                {"date": "2026-07-13", "code": "000001.SZ", "open": 10, "high": 11, "low": 9, "close": 10, "volume": 100, "amount": 1000},
                {"date": "2026-07-15", "code": "000001.SZ", "open": 12, "high": 13, "low": 11, "close": 12, "volume": 120, "amount": 1440},
                {"date": "2026-07-13", "code": "600000.SH", "open": 8, "high": 9, "low": 7, "close": 8, "volume": 80, "amount": 640},
                {"date": "2026-07-14", "code": "600000.SH", "open": 9, "high": 10, "low": 8, "close": 9, "volume": 90, "amount": 810},
                {"date": "2026-07-15", "code": "600000.SH", "open": 10, "high": 11, "low": 9, "close": 10, "volume": 100, "amount": 1000},
            ]
        )
        with tempfile.TemporaryDirectory() as tmp:
            provider = Path(tmp) / "provider"
            write_minimal_qlib_provider(frame, provider)
            values = np.fromfile(provider / "features" / "sz000001" / "close.day.bin", dtype="<f")
        self.assertEqual(values[0], 0.0)
        self.assertEqual(values[1], 10.0)
        self.assertTrue(np.isnan(values[2]))
        self.assertEqual(values[3], 12.0)

    def test_failed_provider_build_keeps_existing_provider(self):
        with tempfile.TemporaryDirectory() as tmp:
            provider = Path(tmp) / "provider"
            provider.mkdir()
            marker = provider / "existing.txt"
            marker.write_text("keep", encoding="utf-8")

            with patch("market_data.sync_service.write_minimal_qlib_provider", side_effect=OSError("disk full")):
                with self.assertRaisesRegex(OSError, "disk full"):
                    _promote_qlib_provider(pd.DataFrame(), provider, "failed-build")

            self.assertEqual(marker.read_text(encoding="utf-8"), "keep")
            self.assertFalse((Path(tmp) / ".provider_build_failed-build").exists())

    def test_promote_merges_incremental_rows_with_existing_history(self):
        old = pd.DataFrame(
            [
                {"date": "2026-07-15", "code": "000001.SZ", "open": 10, "high": 11, "low": 9, "close": 10, "volume": 100, "amount": 1000},
                {"date": "2026-07-16", "code": "000001.SZ", "open": 11, "high": 12, "low": 10, "close": 11, "volume": 110, "amount": 1210},
            ]
        )
        incremental = pd.DataFrame(
            [
                {"date": "2026-07-16", "code": "000001.SZ", "open": 11, "high": 13, "low": 10, "close": 12, "volume": 120, "amount": 1440},
                {"date": "2026-07-17", "code": "000001.SZ", "open": 12, "high": 13, "low": 11, "close": 12, "volume": 130, "amount": 1560},
            ]
        )
        with tempfile.TemporaryDirectory() as tmp:
            provider = Path(tmp) / "provider"
            write_minimal_qlib_provider(old, provider)
            _promote_qlib_provider(incremental, provider, "incremental")
            merged = read_minimal_qlib_provider(provider)

        self.assertEqual(merged["date"].tolist(), ["2026-07-15", "2026-07-16", "2026-07-17"])
        self.assertEqual(float(merged.loc[merged["date"] == "2026-07-16", "close"].iloc[0]), 12.0)

    def test_incremental_no_data_keeps_existing_symbol(self):
        old = pd.DataFrame(
            [
                {"date": "2026-07-15", "code": "000001.SZ", "open": 10, "high": 11, "low": 9, "close": 10, "volume": 100, "amount": 1000},
            ]
        )

        class _NoDataClient:
            def history_quotation(self, code, start_date, end_date):
                raise RuntimeError(
                    "Tencent returned no daily bars inside the requested window; "
                    "Tonghuashun returned no recent daily bars"
                )

        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp)
            provider = project_root / "data" / "market" / "a_share" / "qlib" / "a_stock_data_daily_cn"
            write_minimal_qlib_provider(old, provider)
            request = SyncRequest(
                codes=["000001.SZ"],
                start_date="2026-07-16",
                end_date="2026-07-17",
                promote=True,
                batch_id="no-new-bars",
            )
            with patch("market_data.sync_service.AStockDataClient", return_value=_NoDataClient()):
                result = sync_a_stock_data_daily(project_root, request)

            preserved = read_minimal_qlib_provider(provider)

        self.assertTrue(result.success)
        self.assertEqual(result.codes_failed, 0)
        self.assertEqual(result.issues[0].level, "warning")
        self.assertEqual(preserved["date"].tolist(), ["2026-07-15"])


if __name__ == "__main__":
    unittest.main()
