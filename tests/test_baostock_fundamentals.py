import hashlib
import json
import re
import sys
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path

import numpy as np
import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = PROJECT_ROOT / "frontend-v2" / "backend"
for path in (PROJECT_ROOT, BACKEND_ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from market_data.baostock_fundamentals import (
    BaoStockFundamentalSyncRequest,
    normalize_baostock_annual_events,
    normalize_baostock_daily_valuation,
    sync_baostock_fundamentals,
)
from market_data.providers.baostock import (
    BaoStockApiError,
    BaoStockClient,
    _SocketReadGuard,
    from_baostock_code,
    to_baostock_code,
)
from market_data.sync_service import write_minimal_qlib_provider
from scripts.prepare_profit_factor_library import _additional_raw_fields


class _Result:
    def __init__(self, fields=(), rows=(), error_code="0", error_msg="success"):
        self.fields = list(fields)
        self.rows = [list(row) for row in rows]
        self.error_code = error_code
        self.error_msg = error_msg
        self._index = -1

    def next(self):
        self._index += 1
        return self._index < len(self.rows)

    def get_row_data(self):
        return self.rows[self._index]


class _Module:
    __version__ = "test"

    def __init__(self):
        self.login_calls = 0
        self.logout_calls = 0

    def login(self):
        self.login_calls += 1
        return _Result()

    def logout(self):
        self.logout_calls += 1
        return _Result()

    def query_profit_data(self, code, year=None, quarter=None):
        return _Result(
            ("code", "pubDate", "statDate", "roeAvg"),
            ((code, f"{year + 1}-03-30", f"{year}-12-31", "0.12"),),
        )


def _base_frame() -> pd.DataFrame:
    dates = pd.date_range("2023-01-02", "2023-01-06", freq="B")
    return pd.DataFrame(
        [
            {
                "date": date.strftime("%Y-%m-%d"),
                "code": "000001.SZ",
                "open": 10,
                "high": 11,
                "low": 9,
                "close": 10,
                "volume": 100,
                "amount": 1000,
            }
            for date in dates
        ]
    )


def _payload(fields, rows):
    return {
        "code": 0,
        "msg": "success",
        "data": {"fields": list(fields), "items": [list(row) for row in rows]},
    }


class BaoStockClientTests(unittest.TestCase):
    def test_socket_guard_rejects_empty_reads(self):
        class ClosedSocket:
            def recv(self, size):
                del size
                return b""

        with self.assertRaisesRegex(ConnectionError, "closed the connection"):
            _SocketReadGuard(ClosedSocket()).recv(8192)

    def test_code_conversion_is_explicit(self):
        self.assertEqual(to_baostock_code("600000.SH"), "sh.600000")
        self.assertEqual(from_baostock_code("sz.000001"), "000001.SZ")
        with self.assertRaises(ValueError):
            from_baostock_code("bj.430047")

    def test_annual_query_aggregates_q4_and_reuses_login(self):
        module = _Module()
        client = BaoStockClient(module=module, retries=1)
        payload = client.query_annual_history_raw(
            "query_profit_data",
            code="600000.SH",
            start_year=2021,
            end_year=2022,
            required_fields=("code", "pubDate", "statDate", "roeAvg"),
        )
        client.close()

        self.assertEqual(len(payload["data"]["items"]), 2)
        self.assertEqual(payload["source"]["requestCount"], 2)
        self.assertEqual(module.login_calls, 1)
        self.assertEqual(module.logout_calls, 1)

    def test_missing_field_fails_closed(self):
        module = _Module()

        def missing_field(code, year=None, quarter=None):
            return _Result(("code", "pubDate", "statDate"), ())

        module.query_profit_data = missing_field
        client = BaoStockClient(module=module, retries=1)
        with self.assertRaisesRegex(BaoStockApiError, "omitted requested fields"):
            client.query_annual_history_raw(
                "query_profit_data",
                code="600000.SH",
                start_year=2022,
                end_year=2022,
                required_fields=("code", "pubDate", "statDate", "roeAvg"),
            )
        client.close()

    def test_failed_request_reconnects_before_retry(self):
        module = _Module()
        query_calls = 0

        def flaky_query(code, year=None, quarter=None):
            nonlocal query_calls
            query_calls += 1
            if query_calls == 1:
                return _Result(error_code="10002007", error_msg="network closed")
            return _Result(
                ("code", "pubDate", "statDate", "roeAvg"),
                ((code, f"{year + 1}-03-30", f"{year}-12-31", "0.12"),),
            )

        module.query_profit_data = flaky_query
        client = BaoStockClient(module=module, retries=2)
        payload = client.query_annual_history_raw(
            "query_profit_data",
            code="600000.SH",
            start_year=2022,
            end_year=2022,
            required_fields=("code", "pubDate", "statDate", "roeAvg"),
        )
        client.close()

        self.assertEqual(len(payload["data"]["items"]), 1)
        self.assertEqual(module.login_calls, 2)
        self.assertEqual(module.logout_calls, 1)

    def test_request_deadline_breaks_a_blocked_query(self):
        module = _Module()

        def blocked_query(code, year=None, quarter=None):
            del code, year, quarter
            time.sleep(1)
            return _Result()

        module.query_profit_data = blocked_query
        client = BaoStockClient(
            module=module,
            retries=1,
            socket_timeout_seconds=0.05,
        )
        with self.assertRaisesRegex(BaoStockApiError, "timed out"):
            client.query_annual_history_raw(
                "query_profit_data",
                code="600000.SH",
                start_year=2022,
                end_year=2022,
                required_fields=("code",),
            )


class BaoStockNormalizationTests(unittest.TestCase):
    def test_factor_preparation_extracts_non_default_provider_fields(self):
        fields = _additional_raw_fields(
            [
                {"factor_expression": "$close + $pit_fy_roe_avg"},
                {"factor_expression": "$pit_book_to_price + $pit_fy_roe_avg"},
            ]
        )
        self.assertEqual(fields, ["pit_fy_roe_avg", "pit_book_to_price"])

    def test_annual_bundle_uses_later_publication_date(self):
        profit = pd.DataFrame(
            [
                {
                    "code": "sz.000001",
                    "pubDate": "2023-01-03",
                    "statDate": "2022-12-31",
                    "roeAvg": "0.12",
                }
            ]
        )
        growth = pd.DataFrame(
            [
                {
                    "code": "sz.000001",
                    "pubDate": "2023-01-04",
                    "statDate": "2022-12-31",
                    "YOYNI": "0.08",
                }
            ]
        )
        events, audit = normalize_baostock_annual_events(profit, growth)

        self.assertEqual(events.iloc[0]["available_date"], pd.Timestamp("2023-01-04"))
        self.assertEqual(events.iloc[0]["fy_roe_avg"], 0.12)
        self.assertEqual(events.iloc[0]["fy_netprofit_yoy"], 0.08)
        self.assertEqual(audit["publicationDateMismatchRows"], 1)
        self.assertFalse(audit["revisionHistoryAvailable"])

    def test_non_annual_report_fails_closed(self):
        profit = pd.DataFrame(
            [
                {
                    "code": "sz.000001",
                    "pubDate": "2023-08-30",
                    "statDate": "2023-06-30",
                    "roeAvg": "0.12",
                }
            ]
        )
        growth = pd.DataFrame(
            [
                {
                    "code": "sz.000001",
                    "pubDate": "2023-08-30",
                    "statDate": "2023-06-30",
                    "YOYNI": "0.08",
                }
            ]
        )
        with self.assertRaisesRegex(ValueError, "non-annual"):
            normalize_baostock_annual_events(profit, growth)

    def test_daily_pb_normalization(self):
        daily = normalize_baostock_daily_valuation(
            pd.DataFrame(
                [
                    {
                        "date": "2023-01-02",
                        "code": "sz.000001",
                        "pbMRQ": "1.5",
                        "tradestatus": "1",
                        "isST": "0",
                    }
                ]
            )
        )
        self.assertEqual(daily.iloc[0]["code"], "000001.SZ")
        self.assertEqual(daily.iloc[0]["pb"], 1.5)

    def test_daily_earnings_to_price_keeps_signed_values(self):
        daily = normalize_baostock_daily_valuation(
            pd.DataFrame(
                [
                    {
                        "date": f"2023-01-0{day}",
                        "code": "sz.000001",
                        "pbMRQ": "1.5",
                        "peTTM": pe,
                        "tradestatus": "1",
                        "isST": "0",
                    }
                    for day, pe in ((2, "10"), (3, "-5"), (4, "0"), (5, ""))
                ]
            ),
            include_earnings_to_price=True,
        )

        self.assertAlmostEqual(daily.iloc[0]["earnings_to_price"], 0.1)
        self.assertAlmostEqual(daily.iloc[1]["earnings_to_price"], -0.2)
        self.assertTrue(np.isnan(daily.iloc[2]["earnings_to_price"]))
        self.assertTrue(np.isnan(daily.iloc[3]["earnings_to_price"]))


class BaoStockSyncTests(unittest.TestCase):
    class _Client:
        package_version = "0.test"

        def query_annual_history_raw(self, method_name, **kwargs):
            if method_name == "query_profit_data":
                payload = _payload(
                    ("code", "pubDate", "statDate", "roeAvg"),
                    (("sz.000001", "2023-01-03", "2022-12-31", "0.12"),),
                )
            else:
                payload = _payload(
                    ("code", "pubDate", "statDate", "YOYNI"),
                    (("sz.000001", "2023-01-03", "2022-12-31", "0.08"),),
                )
            payload["source"] = {
                "startYear": kwargs["start_year"],
                "endYear": kwargs["end_year"],
            }
            return payload

        def query_daily_valuation_raw(self, **kwargs):
            payload = _payload(
                ("date", "code", "pbMRQ", "tradestatus", "isST"),
                tuple(
                    (
                        date.strftime("%Y-%m-%d"),
                        "sz.000001",
                        "1.5",
                        "1",
                        "0",
                    )
                    for date in pd.date_range("2023-01-02", "2023-01-06", freq="B")
                ),
            )
            payload["source"] = {
                "startDate": kwargs["start_date"],
                "endDate": kwargs["end_date"],
            }
            return payload

        def close(self):
            return None

    def test_sync_is_immutable_lagged_and_provider_isolated(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            base = root / "base"
            output = root / "enriched"
            write_minimal_qlib_provider(_base_frame(), base)
            with (base / "instruments" / "all.txt").open("a", encoding="utf-8") as file:
                file.write("BJ430047\t2023-01-02\t2023-01-06\n")
            request = BaoStockFundamentalSyncRequest(
                base_provider=base,
                codes=["000001.SZ"],
                start_date="2023-01-02",
                end_date="2023-01-06",
                output_provider=output,
                batch_id="baostock-test",
            )
            result = sync_baostock_fundamentals(
                root,
                request,
                client=self._Client(),
            )
            manifest = json.loads(
                Path(result["manifestPath"]).read_text(encoding="utf-8")
            )
            extension = json.loads(
                (output / "quantaalpha_extension.manifest.json").read_text(
                    encoding="utf-8"
                )
            )
            panel = pd.read_csv(
                Path(result["stagingDir"]) / "point_in_time_features.csv.gz"
            ).set_index("date")
            progress = json.loads(
                (Path(result["stagingDir"]) / "progress.json").read_text(
                    encoding="utf-8"
                )
            )

            self.assertEqual(result["status"], "PASS")
            self.assertTrue(np.isnan(panel.loc["2023-01-03", "pit_fy_roe_avg"]))
            self.assertEqual(panel.loc["2023-01-04", "pit_fy_roe_avg"], 0.12)
            self.assertAlmostEqual(panel.loc["2023-01-02", "pit_book_to_price"], 2 / 3)
            self.assertFalse(manifest["credentialsRequired"])
            self.assertFalse(manifest["credentialPersisted"])
            self.assertFalse(manifest["revisionHistoryAvailable"])
            self.assertEqual(manifest["request"]["estimatedSourceRequests"], 5)
            self.assertEqual(progress["status"], "PASS")
            self.assertEqual(progress["completedCodes"], 1)
            self.assertEqual(extension["source"]["provider"], "baostock")
            self.assertFalse(
                (base / "features" / "sz000001" / "pit_fy_roe_avg.day.bin").exists()
            )
            self.assertTrue(
                (output / "features" / "sz000001" / "pit_fy_roe_avg.day.bin").exists()
            )
            with self.assertRaises(FileExistsError):
                sync_baostock_fundamentals(
                    root,
                    request,
                    client=self._Client(),
                )

            class _NeverClient:
                package_version = "never"

                def query_annual_history_raw(self, *args, **kwargs):
                    raise AssertionError("raw response should have been reused")

                def query_daily_valuation_raw(self, *args, **kwargs):
                    raise AssertionError("raw response should have been reused")

                def close(self):
                    return None

            reused = sync_baostock_fundamentals(
                root,
                replace(
                    request,
                    batch_id="baostock-reused",
                    output_provider=None,
                    reuse_raw_dir=Path(result["rawDir"]),
                ),
                client=_NeverClient(),
            )
            reused_manifest = json.loads(
                Path(reused["manifestPath"]).read_text(encoding="utf-8")
            )
            self.assertEqual(reused["status"], "PASS")
            self.assertEqual(reused_manifest["reusedResponses"], 3)

            class _PeRefreshClient:
                package_version = "pe-refresh"

                def __init__(self):
                    self.daily_calls = 0

                def query_annual_history_raw(self, *args, **kwargs):
                    raise AssertionError("annual responses should have been reused")

                def query_daily_valuation_raw(self, **kwargs):
                    self.daily_calls += 1
                    fields = tuple(kwargs["required_fields"])
                    rows = []
                    for date in pd.date_range("2023-01-02", "2023-01-06", freq="B"):
                        values = {
                            "date": date.strftime("%Y-%m-%d"),
                            "code": "sz.000001",
                            "pbMRQ": "1.5",
                            "peTTM": "10",
                            "tradestatus": "1",
                            "isST": "0",
                        }
                        rows.append(tuple(values[field] for field in fields))
                    payload = _payload(fields, rows)
                    payload["source"] = {
                        "startDate": kwargs["start_date"],
                        "endDate": kwargs["end_date"],
                    }
                    return payload

                def close(self):
                    return None

            pe_client = _PeRefreshClient()
            pe_result = sync_baostock_fundamentals(
                root,
                replace(
                    request,
                    batch_id="baostock-pe-refresh",
                    output_provider=None,
                    reuse_raw_dir=Path(result["rawDir"]),
                    include_earnings_to_price=True,
                ),
                client=pe_client,
            )
            pe_manifest = json.loads(
                Path(pe_result["manifestPath"]).read_text(encoding="utf-8")
            )
            pe_panel = pd.read_csv(
                Path(pe_result["stagingDir"]) / "point_in_time_features.csv.gz"
            )
            self.assertEqual(pe_result["status"], "PASS")
            self.assertEqual(pe_client.daily_calls, 1)
            self.assertEqual(pe_manifest["reusedResponses"], 2)
            self.assertEqual(pe_manifest["queriedResponses"], 1)
            self.assertEqual(len(pe_manifest["reuseMisses"]), 1)
            self.assertEqual(pe_manifest["reuseMisses"][0]["dataset"], "daily_valuation")
            self.assertEqual(pe_manifest["reuseMisses"][0]["missingFields"], ["peTTM"])
            self.assertTrue(pe_manifest["request"]["includeEarningsToPrice"])
            self.assertAlmostEqual(pe_panel.iloc[0]["pit_earnings_to_price"], 0.1)

    def test_preregistered_candidates_match_baostock_contract(self):
        config = json.loads(
            (PROJECT_ROOT / "configs" / "profit_candidates_v1_7_baostock.json").read_text(
                encoding="utf-8"
            )
        )
        metadata = config["metadata"]
        self.assertFalse(metadata["created_before_data_sync"])
        self.assertTrue(metadata["created_before_full_historical_sync"])
        self.assertFalse(metadata["historical_returns_viewed_before_registration"])
        self.assertIn("revision vintages", metadata["revision_policy"])
        self.assertEqual(metadata["admission_factor_ids"], list(config["factors"]))
        allowed = {
            "pit_fy_roe_avg",
            "pit_fy_netprofit_yoy",
            "pit_book_to_price",
        }
        namespace = metadata["factor_id_namespace"]
        for factor_id, factor in config["factors"].items():
            expected = hashlib.sha256(
                f"{namespace}:{factor['factor_name']}:{factor['factor_expression']}".encode()
            ).hexdigest()[:16]
            self.assertEqual(factor_id, expected)
            fields = set(
                re.findall(
                    r"\$([A-Za-z][A-Za-z0-9_]*)",
                    factor["factor_expression"],
                )
            )
            self.assertTrue(fields)
            self.assertLessEqual(fields, allowed)
            self.assertFalse(factor["feedback"]["decision"])

    def test_v1_8_preregisters_one_signed_ep_proxy(self):
        config = json.loads(
            (
                PROJECT_ROOT
                / "configs"
                / "profit_candidates_v1_8_baostock_ep.json"
            ).read_text(encoding="utf-8")
        )
        metadata = config["metadata"]
        self.assertTrue(metadata["created_before_full_historical_sync"])
        self.assertFalse(metadata["candidate_historical_returns_viewed_before_registration"])
        self.assertTrue(metadata["project_historical_returns_previously_viewed"])
        self.assertTrue(metadata["historical_evidence_only"])
        self.assertEqual(metadata["homogeneous_trial_count_after_v1_8"], 23)
        self.assertEqual(metadata["admission_factor_ids"], list(config["factors"]))
        self.assertEqual(len(config["factors"]), 1)

        factor_id, factor = next(iter(config["factors"].items()))
        expected = hashlib.sha256(
            (
                f"{metadata['factor_id_namespace']}:"
                f"{factor['factor_name']}:{factor['factor_expression']}"
            ).encode()
        ).hexdigest()[:16]
        self.assertEqual(factor_id, expected)
        self.assertEqual(factor["factor_expression"], "$pit_earnings_to_price")
        self.assertIn("signed reciprocal", metadata["proxy_policy"])
        self.assertIn("negative", metadata["negative_earnings_policy"])


if __name__ == "__main__":
    unittest.main()
