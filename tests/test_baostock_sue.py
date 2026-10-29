import hashlib
import json
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

import numpy as np
import pandas as pd
import yaml


PROJECT_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = PROJECT_ROOT / "frontend-v2" / "backend"
for path in (PROJECT_ROOT, BACKEND_ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from market_data.baostock_sue import (
    QUARTERLY_PROFIT_FIELDS,
    BaoStockSueSyncRequest,
    build_baostock_sue_panel,
    normalize_baostock_quarterly_sue,
    sync_baostock_sue,
)
from market_data.providers.baostock import BaoStockClient
from market_data.sync_service import write_minimal_qlib_provider


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


def _quarterly_rows(
    standalone_profits,
    *,
    start_period="2019Q1",
    code="sz.000001",
):
    rows = []
    periods = pd.period_range(start_period, periods=len(standalone_profits), freq="Q-DEC")
    cumulative = 0.0
    current_year = None
    for period, standalone in zip(periods, standalone_profits):
        if period.year != current_year:
            current_year = period.year
            cumulative = 0.0
        cumulative += float(standalone)
        stat_date = period.end_time.normalize()
        pub_date = stat_date + pd.Timedelta(days=30)
        rows.append(
            {
                "code": code,
                "pubDate": pub_date.strftime("%Y-%m-%d"),
                "statDate": stat_date.strftime("%Y-%m-%d"),
                "netProfit": str(cumulative),
                "epsTTM": "1.0",
                "totalShare": "100.0",
            }
        )
    return rows


def _payload(rows, *, start_year, end_year):
    return {
        "code": 0,
        "msg": "success",
        "data": {
            "fields": list(QUARTERLY_PROFIT_FIELDS),
            "items": [
                [row[field] for field in QUARTERLY_PROFIT_FIELDS]
                for row in rows
            ],
        },
        "source": {
            "method": "query_profit_data",
            "reportFrequency": "quarterly",
            "quarters": [1, 2, 3, 4],
            "startYear": start_year,
            "endYear": end_year,
            "requestCount": (end_year - start_year + 1) * 4,
            "packageVersion": "test",
            "capturedAt": "2026-08-04T00:00:00+00:00",
        },
    }


def _base_frame():
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
            for date in pd.date_range("2023-01-02", "2023-01-06", freq="B")
        ]
    )


class BaoStockSueClientTests(unittest.TestCase):
    def test_quarterly_query_fetches_all_four_quarters(self):
        class _Module:
            __version__ = "test"

            def __init__(self):
                self.calls = []
                self.login_calls = 0
                self.logout_calls = 0

            def login(self):
                self.login_calls += 1
                return _Result()

            def logout(self):
                self.logout_calls += 1
                return _Result()

            def query_profit_data(self, code, year=None, quarter=None):
                self.calls.append((code, year, quarter))
                return _Result(
                    QUARTERLY_PROFIT_FIELDS,
                    (
                        (
                            code,
                            f"{year}-05-01",
                            f"{year}-03-31",
                            "1",
                            "1",
                            "1",
                        ),
                    ),
                )

        module = _Module()
        client = BaoStockClient(module=module, retries=1)
        payload = client.query_quarterly_history_raw(
            "query_profit_data",
            code="000001.SZ",
            start_year=2022,
            end_year=2022,
            required_fields=QUARTERLY_PROFIT_FIELDS,
        )
        client.close()

        self.assertEqual(payload["source"]["requestCount"], 4)
        self.assertEqual([call[2] for call in module.calls], [1, 2, 3, 4])
        self.assertEqual(module.login_calls, 1)
        self.assertEqual(module.logout_calls, 1)


class BaoStockSueNormalizationTests(unittest.TestCase):
    def test_seasonal_sue_uses_standalone_profit_and_prior_window_only(self):
        rows = _quarterly_rows(
            [
                100,
                100,
                100,
                100,
                101,
                102,
                103,
                104,
                106,
                108,
                110,
                112,
                115,
            ]
        )
        events, audit = normalize_baostock_quarterly_sue(pd.DataFrame(rows))
        last = events.iloc[-1]

        self.assertAlmostEqual(last["standalone_net_profit"], 115.0)
        self.assertAlmostEqual(last["unexpected_net_profit"], 9.0)
        self.assertAlmostEqual(last["standardization_std"], np.std(range(1, 9), ddof=1))
        self.assertAlmostEqual(
            last["sue_seasonal"],
            9.0 / np.std(range(1, 9), ddof=1),
        )
        self.assertEqual(audit["minimumSourceQuartersPerCode"], 13)
        self.assertTrue(audit["currentObservationExcludedFromDenominator"])

    def test_missing_cumulative_quarter_does_not_bridge_the_gap(self):
        rows = _quarterly_rows([100 + index for index in range(16)])
        missing_period = "2020-06-30"
        rows = [row for row in rows if row["statDate"] != missing_period]
        events, audit = normalize_baostock_quarterly_sue(pd.DataFrame(rows))
        indexed = events.set_index("report_end")

        self.assertTrue(
            np.isnan(indexed.loc[pd.Timestamp("2020-09-30"), "standalone_net_profit"])
        )
        self.assertEqual(audit["continuityGapQuarters"], 1)

    def test_zero_standardization_variance_is_missing(self):
        rows = _quarterly_rows(
            [100] * 4 + [101] * 4 + [102] * 4 + [103]
        )
        events, audit = normalize_baostock_quarterly_sue(pd.DataFrame(rows))

        self.assertTrue(np.isnan(events.iloc[-1]["sue_seasonal"]))
        self.assertGreater(audit["zeroDenominatorRows"], 0)

    def test_conflicting_duplicate_quarter_fails_closed(self):
        rows = _quarterly_rows([100] * 13)
        duplicate = dict(rows[-1])
        duplicate["netProfit"] = "999"
        rows.append(duplicate)

        with self.assertRaisesRegex(ValueError, "conflicting duplicate quarters"):
            normalize_baostock_quarterly_sue(pd.DataFrame(rows))

    def test_non_quarter_end_fails_closed(self):
        rows = _quarterly_rows([100] * 13)
        rows[-1]["statDate"] = "2022-04-01"

        with self.assertRaisesRegex(ValueError, "non-quarter-end"):
            normalize_baostock_quarterly_sue(pd.DataFrame(rows))

    def test_panel_lags_announcement_and_expires_after_six_months(self):
        calendar = list(pd.date_range("2023-01-02", "2023-07-03", freq="B"))
        events = pd.DataFrame(
            [
                {
                    "code": "000001.SZ",
                    "report_end": pd.Timestamp("2022-12-31"),
                    "announcement_date": pd.Timestamp("2023-01-03"),
                    "available_date": pd.Timestamp("2023-01-03"),
                    "sue_seasonal": 2.0,
                }
            ]
        )
        panel, audit = build_baostock_sue_panel(
            events,
            base_provider=Path("/tmp/base"),
            calendar=calendar,
            source_windows={
                "000001.SZ": (pd.Timestamp("2023-01-02"), pd.Timestamp("2023-07-03"))
            },
            codes=["000001.SZ"],
            publication_lag_sessions=1,
        )
        indexed = panel.set_index("date")

        self.assertTrue(np.isnan(indexed.loc[pd.Timestamp("2023-01-03"), "pit_sue_seasonal"]))
        self.assertEqual(indexed.loc[pd.Timestamp("2023-01-04"), "pit_sue_seasonal"], 2.0)
        self.assertTrue(np.isnan(indexed.loc[pd.Timestamp("2023-07-03"), "pit_sue_seasonal"]))
        self.assertEqual(audit["availabilityLeakRows"], 0)
        self.assertEqual(audit["reportPeriodLeakRows"], 0)
        self.assertGreater(audit["staleSignalRowsCleared"], 0)


class BaoStockSueSyncTests(unittest.TestCase):
    class _Client:
        package_version = "test"

        def query_quarterly_history_raw(self, method_name, **kwargs):
            self.assert_method_name = method_name
            rows = _quarterly_rows(
                [
                    100,
                    100,
                    100,
                    100,
                    101,
                    102,
                    103,
                    104,
                    106,
                    108,
                    110,
                    112,
                    115,
                    118,
                    121,
                    124,
                ]
            )
            rows[-1]["pubDate"] = "2023-01-03"
            return _payload(
                rows,
                start_year=kwargs["start_year"],
                end_year=kwargs["end_year"],
            )

        def close(self):
            return None

    def test_sync_is_immutable_lagged_promotable_and_reusable(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            base = root / "base"
            output = root / "enriched"
            write_minimal_qlib_provider(_base_frame(), base)
            request = BaoStockSueSyncRequest(
                base_provider=base,
                codes=["000001.SZ"],
                start_date="2023-01-02",
                end_date="2023-01-06",
                output_provider=output,
                batch_id="sue-test",
            )
            result = sync_baostock_sue(root, request, client=self._Client())
            manifest = json.loads(
                Path(result["manifestPath"]).read_text(encoding="utf-8")
            )
            panel = pd.read_csv(
                Path(result["stagingDir"]) / "point_in_time_sue.csv.gz"
            ).set_index("date")

            self.assertEqual(result["status"], "PASS")
            self.assertEqual(
                panel.loc["2023-01-02", "pit_sue_seasonal"],
                panel.loc["2023-01-03", "pit_sue_seasonal"],
            )
            self.assertNotEqual(
                panel.loc["2023-01-03", "pit_sue_seasonal"],
                panel.loc["2023-01-04", "pit_sue_seasonal"],
            )
            self.assertEqual(len(manifest["rawPayloadSha256"]), 1)
            self.assertEqual(manifest["request"]["historySeedYears"], 4)
            self.assertFalse(manifest["revisionHistoryAvailable"])
            self.assertTrue(
                (output / "features" / "sz000001" / "pit_sue_seasonal.day.bin").exists()
            )
            with self.assertRaises(FileExistsError):
                sync_baostock_sue(root, request, client=self._Client())

            class _NeverClient:
                package_version = "never"

                def query_quarterly_history_raw(self, *args, **kwargs):
                    raise AssertionError("quarterly response should have been reused")

                def close(self):
                    return None

            reused = sync_baostock_sue(
                root,
                replace(
                    request,
                    batch_id="sue-reused",
                    output_provider=None,
                    reuse_raw_dir=Path(result["rawDir"]),
                ),
                client=_NeverClient(),
            )
            reused_manifest = json.loads(
                Path(reused["manifestPath"]).read_text(encoding="utf-8")
            )
            self.assertEqual(reused["status"], "PASS")
            self.assertEqual(reused_manifest["reusedResponses"], 1)
            self.assertEqual(
                reused_manifest["rawPayloadSha256"],
                manifest["rawPayloadSha256"],
            )

    def test_v1_9_preregisters_one_frozen_seasonal_sue_proxy(self):
        config = json.loads(
            (
                PROJECT_ROOT
                / "configs"
                / "profit_candidates_v1_9_baostock_seasonal_sue.json"
            ).read_text(encoding="utf-8")
        )
        metadata = config["metadata"]
        self.assertTrue(metadata["created_before_full_historical_sync"])
        self.assertFalse(metadata["candidate_historical_returns_viewed_before_registration"])
        self.assertEqual(metadata["selection_horizon_trading_days"], 21)
        self.assertEqual(metadata["homogeneous_trial_count_after_v1_9"], 24)
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
        self.assertEqual(factor["factor_expression"], "$pit_sue_seasonal")
        self.assertIn("excluding the current observation", metadata["formula_policy"])
        self.assertIn("RESEARCH_ONLY", metadata["revision_policy"])

    def test_v1_9_runtime_and_gate_share_the_frozen_label_contract(self):
        backtest = yaml.safe_load(
            (
                PROJECT_ROOT
                / "configs"
                / "backtest_profit_validation_v1_9_baostock_sue.yaml"
            ).read_text(encoding="utf-8")
        )
        gate = yaml.safe_load(
            (
                PROJECT_ROOT
                / "configs"
                / "profit_validation_v1_9_baostock_sue.yaml"
            ).read_text(encoding="utf-8")
        )

        self.assertEqual(backtest["dataset"]["label"], gate["validation"]["label"])
        self.assertEqual(gate["validation"]["label_horizon_trading_days"], 22)
        self.assertEqual(gate["validation"]["return_holding_period_trading_days"], 21)
        self.assertEqual(backtest["backtest"]["strategy"]["kwargs"]["hold_thresh"], 21)
        self.assertEqual(backtest["dataset"]["segments"]["valid"][1], "2021-12-01")
        self.assertIn("pit_sue_seasonal", backtest["data"]["additional_fields"])
        self.assertEqual(gate["trial_matrix"]["expected_factor_count"], 24)


if __name__ == "__main__":
    unittest.main()
