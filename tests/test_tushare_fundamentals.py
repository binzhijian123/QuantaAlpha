import hashlib
import json
import os
import re
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd
import yaml


PROJECT_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = PROJECT_ROOT / "frontend-v2" / "backend"
for path in (PROJECT_ROOT, BACKEND_ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from market_data.fundamentals import (
    DROE_FEATURE,
    FundamentalSyncRequest,
    build_point_in_time_panel,
    normalize_daily_basic,
    normalize_financial_events,
    sync_tushare_fundamentals,
    write_enriched_qlib_provider,
)
from market_data.providers.tushare import TushareApiError, TushareProClient
from market_data.sync_service import write_minimal_qlib_provider
from quantaalpha.utils.qlib_provider import provider_contract_fingerprint
from quantaalpha.trading.market_data import QlibBinaryDailySource


class _Response:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class _Session:
    def __init__(self, payload):
        self.payload = payload
        self.request_json = None

    def post(self, url, **kwargs):
        self.request_json = kwargs["json"]
        return _Response(self.payload)


def _base_frame() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "date": "2023-01-02",
                "code": "000001.SZ",
                "open": 10,
                "high": 11,
                "low": 9,
                "close": 10,
                "volume": 100,
                "amount": 1000,
            },
            {
                "date": "2023-01-03",
                "code": "000001.SZ",
                "open": 10,
                "high": 11,
                "low": 9,
                "close": 10,
                "volume": 100,
                "amount": 1000,
            },
        ]
    )


def _financial_rows() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "ts_code": "000001.SZ",
                "ann_date": "20230103",
                "end_date": "20220930",
                "update_flag": "0",
                "q_dt_roe": 10,
            },
            {
                "ts_code": "000001.SZ",
                "ann_date": "20230105",
                "end_date": "20221231",
                "update_flag": "0",
                "q_dt_roe": 20,
            },
            {
                "ts_code": "000001.SZ",
                "ann_date": "20230109",
                "end_date": "20220930",
                "update_flag": "1",
                "q_dt_roe": 99,
            },
        ]
    )


def _disclosure_rows() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "ts_code": "000001.SZ",
                "end_date": "20220930",
                "actual_date": "20230104",
            },
            {
                "ts_code": "000001.SZ",
                "end_date": "20221231",
                "actual_date": "20230106",
            },
        ]
    )


class TushareClientTests(unittest.TestCase):
    def test_missing_token_fails_before_network(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(TushareApiError, "TUSHARE_TOKEN"):
                TushareProClient()

    def test_api_error_redacts_token(self):
        token = "secret-token-value"
        session = _Session({"code": -1, "msg": f"invalid {token}", "data": None})
        client = TushareProClient(token=token, session=session)
        with self.assertRaises(TushareApiError) as caught:
            client.query("trade_cal", fields=("cal_date",))
        self.assertNotIn(token, str(caught.exception))
        self.assertEqual(session.request_json["token"], token)

    def test_valid_payload_becomes_dataframe(self):
        payload = {
            "code": 0,
            "msg": None,
            "data": {"fields": ["cal_date", "is_open"], "items": [["20260803", 1]]},
        }
        frame = TushareProClient(token="token", session=_Session(payload)).query(
            "trade_cal", fields=("cal_date", "is_open")
        )
        self.assertEqual(frame.to_dict("records"), [{"cal_date": "20260803", "is_open": 1}])

    def test_missing_requested_field_fails_closed(self):
        payload = {
            "code": 0,
            "msg": None,
            "data": {"fields": ["cal_date"], "items": [["20260803"]]},
        }
        client = TushareProClient(token="token", session=_Session(payload))
        with self.assertRaisesRegex(TushareApiError, "omitted requested fields"):
            client.query("trade_cal", fields=("cal_date", "is_open"))


class PointInTimeFundamentalTests(unittest.TestCase):
    def test_financial_values_start_after_disclosure_and_old_restatement_does_not_roll_back(self):
        events = normalize_financial_events(
            _financial_rows(), _disclosure_rows(), ("q_dt_roe",)
        )
        daily = normalize_daily_basic(
            pd.DataFrame(
                [
                    {
                        "ts_code": "000001.SZ",
                        "trade_date": "20230104",
                        "pb": 1.5,
                    }
                ]
            ),
            ("pb",),
        )
        calendar = pd.to_datetime(
            [
                "2023-01-02",
                "2023-01-03",
                "2023-01-04",
                "2023-01-05",
                "2023-01-06",
                "2023-01-09",
                "2023-01-10",
            ]
        )
        panel, audit = build_point_in_time_panel(
            events,
            daily,
            calendar,
            {"000001.SZ": (pd.Timestamp("2023-01-02"), pd.Timestamp("2023-01-10"))},
            ["000001.SZ"],
            ("q_dt_roe",),
            ("pb",),
            publication_lag_sessions=1,
        )
        values = panel.set_index("date")

        self.assertTrue(np.isnan(values.loc[pd.Timestamp("2023-01-04"), "pit_q_dt_roe"]))
        self.assertEqual(values.loc[pd.Timestamp("2023-01-05"), "pit_q_dt_roe"], 10)
        self.assertEqual(values.loc[pd.Timestamp("2023-01-09"), "pit_q_dt_roe"], 20)
        self.assertEqual(values.loc[pd.Timestamp("2023-01-10"), "pit_q_dt_roe"], 20)
        self.assertEqual(values.loc[pd.Timestamp("2023-01-04"), "pit_pb"], 1.5)
        self.assertAlmostEqual(
            values.loc[pd.Timestamp("2023-01-04"), "pit_book_to_price"],
            2 / 3,
        )
        self.assertEqual(audit["status"], "PASS")
        self.assertEqual(audit["availabilityLeakRows"], 0)

    def test_droe_uses_exact_year_ago_quarter_revisions_and_six_month_staleness(self):
        financial = pd.DataFrame(
            [
                {
                    "ts_code": "000001.SZ",
                    "ann_date": "20220420",
                    "end_date": "20220331",
                    "update_flag": "0",
                    "q_roe": 10,
                },
                {
                    "ts_code": "000001.SZ",
                    "ann_date": "20230420",
                    "end_date": "20230331",
                    "update_flag": "0",
                    "q_roe": 13,
                },
                {
                    "ts_code": "000001.SZ",
                    "ann_date": "20230422",
                    "end_date": "20220331",
                    "update_flag": "1",
                    "q_roe": 11,
                },
            ]
        )
        disclosures = pd.DataFrame(
            [
                {
                    "ts_code": "000001.SZ",
                    "end_date": "20220331",
                    "actual_date": "20220420",
                },
                {
                    "ts_code": "000001.SZ",
                    "end_date": "20230331",
                    "actual_date": "20230420",
                },
            ]
        )
        events = normalize_financial_events(financial, disclosures, ("q_roe",))
        calendar = pd.to_datetime(
            [
                "2022-04-20",
                "2022-04-21",
                "2023-04-20",
                "2023-04-21",
                "2023-04-24",
                "2023-09-29",
                "2023-10-02",
            ]
        )
        panel, audit = build_point_in_time_panel(
            events,
            normalize_daily_basic(pd.DataFrame(), ()),
            calendar,
            {"000001.SZ": (calendar[0], calendar[-1])},
            ["000001.SZ"],
            ("q_roe",),
            (),
            publication_lag_sessions=1,
        )
        values = panel.set_index("date")

        self.assertTrue(np.isnan(values.loc[pd.Timestamp("2023-04-20"), DROE_FEATURE]))
        self.assertEqual(values.loc[pd.Timestamp("2023-04-21"), DROE_FEATURE], 3)
        self.assertEqual(values.loc[pd.Timestamp("2023-04-24"), DROE_FEATURE], 2)
        self.assertEqual(values.loc[pd.Timestamp("2023-09-29"), DROE_FEATURE], 2)
        self.assertTrue(np.isnan(values.loc[pd.Timestamp("2023-10-02"), DROE_FEATURE]))
        self.assertEqual(audit["status"], "PASS")
        self.assertEqual(audit["derivedFinancialFields"], [DROE_FEATURE])
        self.assertEqual(audit["droeSourceMetric"], "q_roe")
        self.assertEqual(audit["droeStalenessMonths"], 6)
        self.assertEqual(audit["availabilityLeakRows"], 0)
        self.assertEqual(audit["dailyBasicCodes"], 0)

    def test_conflicting_financial_duplicates_fail_closed(self):
        rows = _financial_rows().iloc[[0]].copy()
        rows = pd.concat([rows, rows.assign(q_dt_roe=11)], ignore_index=True)
        with self.assertRaisesRegex(ValueError, "conflicting duplicate"):
            normalize_financial_events(rows, _disclosure_rows(), ("q_dt_roe",))

    def test_enriched_provider_is_isolated_and_changes_contract_fingerprint(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            base = root / "base"
            output = root / "enriched"
            write_minimal_qlib_provider(_base_frame(), base)
            source_manifest = root / "source.json"
            source_manifest.write_text('{"batchId":"test"}', encoding="utf-8")
            panel = pd.DataFrame(
                [
                    {"date": "2023-01-02", "code": "000001.SZ", "pit_q_dt_roe": 10},
                    {"date": "2023-01-03", "code": "000001.SZ", "pit_q_dt_roe": 11},
                ]
            )
            before = provider_contract_fingerprint(base)["fingerprint"]
            result = write_enriched_qlib_provider(
                base,
                output,
                panel,
                ("pit_q_dt_roe",),
                batch_id="test",
                source_manifest=source_manifest,
                as_of_audit={"status": "PASS"},
            )

            self.assertFalse((base / "features" / "sz000001" / "pit_q_dt_roe.day.bin").exists())
            feature = output / "features" / "sz000001" / "pit_q_dt_roe.day.bin"
            values = np.fromfile(feature, dtype="<f")
            self.assertEqual(values.tolist(), [0.0, 10.0, 11.0])
            history = QlibBinaryDailySource(output).history_frame(
                "2023-01-03", fields=("close", "pit_q_dt_roe")
            )
            self.assertEqual(
                history["$pit_q_dt_roe"].dropna().tolist(),
                [10.0, 11.0],
            )
            self.assertNotEqual(before, result["fingerprint"])
            self.assertIn("extensionManifest", provider_contract_fingerprint(output)["files"])
            original_feature_bytes = feature.read_bytes()
            layered = root / "layered"
            with self.assertRaisesRegex(ValueError, "already contains extension field"):
                write_enriched_qlib_provider(
                    output,
                    layered,
                    panel,
                    ("pit_q_dt_roe",),
                    batch_id="layered",
                    source_manifest=source_manifest,
                    as_of_audit={"status": "PASS"},
                )
            self.assertEqual(feature.read_bytes(), original_feature_bytes)
            self.assertFalse(layered.exists())
            with self.assertRaises(FileExistsError):
                write_enriched_qlib_provider(
                    base,
                    output,
                    panel,
                    ("pit_q_dt_roe",),
                    batch_id="again",
                    source_manifest=source_manifest,
                    as_of_audit={"status": "PASS"},
                )

    def test_sync_writes_immutable_raw_manifest_without_token(self):
        class _Client:
            def query_raw(self, api_name, **kwargs):
                tables = {
                    "fina_indicator": pd.DataFrame(
                        [
                            {
                                "ts_code": "000001.SZ",
                                "ann_date": "20230102",
                                "end_date": "20221231",
                                "update_flag": "0",
                                "q_dt_roe": 10,
                            }
                        ]
                    ),
                    "disclosure_date": pd.DataFrame(
                        [
                            {
                                "ts_code": "000001.SZ",
                                "ann_date": "20230102",
                                "end_date": "20221231",
                                "pre_date": "20230102",
                                "actual_date": "20230102",
                                "modify_date": None,
                            }
                        ]
                    ),
                    "daily_basic": pd.DataFrame(
                        [
                            {
                                "ts_code": "000001.SZ",
                                "trade_date": "20230102",
                                "pb": 1.5,
                            }
                        ]
                    ),
                }
                frame = tables[api_name]
                return {
                    "code": 0,
                    "msg": None,
                    "data": {"fields": list(frame.columns), "items": frame.values.tolist()},
                }

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            base = root / "base"
            write_minimal_qlib_provider(_base_frame(), base)
            request = FundamentalSyncRequest(
                base_provider=base,
                codes=["000001.SZ"],
                start_date="2022-01-01",
                end_date="2023-01-03",
                financial_metrics=("q_dt_roe",),
                daily_basic_metrics=("pb",),
                batch_id="fundamental-test",
                request_interval_seconds=0,
            )
            result = sync_tushare_fundamentals(root, request, client=_Client())
            manifest = json.loads(Path(result["manifestPath"]).read_text(encoding="utf-8"))

            self.assertEqual(result["status"], "PASS")
            self.assertFalse(manifest["tokenPersisted"])
            self.assertNotIn("token", json.dumps(manifest).lower().replace("tokenpersisted", ""))

            class _NeverClient:
                def query_raw(self, *args, **kwargs):
                    raise AssertionError("reused raw response should avoid API calls")

            reused = sync_tushare_fundamentals(
                root,
                replace(
                    request,
                    batch_id="fundamental-reused",
                    reuse_raw_dir=Path(result["rawDir"]),
                ),
                client=_NeverClient(),
            )
            reused_manifest = json.loads(
                Path(reused["manifestPath"]).read_text(encoding="utf-8")
            )
            self.assertEqual(reused["status"], "PASS")
            self.assertEqual(reused_manifest["reusedResponses"], 3)
            with self.assertRaises(FileExistsError):
                sync_tushare_fundamentals(root, request, client=_Client())

    def test_q_roe_only_sync_does_not_call_daily_basic(self):
        class _Client:
            def __init__(self):
                self.calls = []

            def query_raw(self, api_name, **kwargs):
                self.calls.append(api_name)
                tables = {
                    "fina_indicator": pd.DataFrame(
                        [
                            {
                                "ts_code": "000001.SZ",
                                "ann_date": "20230102",
                                "end_date": "20221231",
                                "update_flag": "0",
                                "q_roe": 10,
                            }
                        ]
                    ),
                    "disclosure_date": pd.DataFrame(
                        [
                            {
                                "ts_code": "000001.SZ",
                                "ann_date": "20230102",
                                "end_date": "20221231",
                                "pre_date": "20230102",
                                "actual_date": "20230102",
                                "modify_date": None,
                            }
                        ]
                    ),
                }
                frame = tables[api_name]
                return {
                    "code": 0,
                    "msg": None,
                    "data": {
                        "fields": list(frame.columns),
                        "items": frame.values.tolist(),
                    },
                }

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            base = root / "base"
            write_minimal_qlib_provider(_base_frame(), base)
            client = _Client()
            result = sync_tushare_fundamentals(
                root,
                FundamentalSyncRequest(
                    base_provider=base,
                    codes=["000001.SZ"],
                    start_date="2022-01-01",
                    end_date="2023-01-03",
                    financial_metrics=("q_roe",),
                    daily_basic_metrics=(),
                    batch_id="droe-no-daily-basic",
                    request_interval_seconds=0,
                ),
                client=client,
            )

            self.assertEqual(result["status"], "PASS")
            self.assertEqual(client.calls, ["fina_indicator", "disclosure_date"])
            self.assertFalse(list(Path(result["rawDir"]).rglob("daily_basic.json")))
            self.assertIn(DROE_FEATURE, result["audit"]["featureFields"])
            self.assertEqual(result["audit"]["missingDailyBasicCodes"], [])

            output = root / "droe-provider"
            promoted = sync_tushare_fundamentals(
                root,
                replace(
                    FundamentalSyncRequest(
                        base_provider=base,
                        codes=["000001.SZ"],
                        start_date="2022-01-01",
                        end_date="2023-01-03",
                        financial_metrics=("q_roe",),
                        daily_basic_metrics=(),
                        request_interval_seconds=0,
                    ),
                    batch_id="droe-promotion-gate",
                    output_provider=output,
                ),
                client=client,
            )
            self.assertEqual(promoted["status"], "FAIL")
            self.assertEqual(promoted["audit"]["droePromotionGate"]["status"], "FAIL")
            self.assertEqual(
                set(promoted["audit"]["droePromotionGate"]["failedChecks"]),
                {
                    "minimumCodes",
                    "minimumCoverage",
                    "historicalRevisionVintages",
                },
            )
            self.assertFalse(output.exists())

    def test_preregistered_candidates_only_use_declared_point_in_time_fields(self):
        config = json.loads(
            (PROJECT_ROOT / "configs" / "profit_candidates_v1_6_fundamentals.json").read_text(
                encoding="utf-8"
            )
        )
        expected_ids = config["metadata"]["admission_factor_ids"]
        self.assertEqual(expected_ids, list(config["factors"]))
        self.assertTrue(config["metadata"]["created_before_data_sync"])
        allowed = {
            "pit_q_dt_roe",
            "pit_q_ocf_to_sales",
            "pit_q_netprofit_yoy",
            "pit_debt_to_assets",
            "pit_book_to_price",
        }
        for factor in config["factors"].values():
            fields = set(re.findall(r"\$([A-Za-z][A-Za-z0-9_]*)", factor["factor_expression"]))
            self.assertTrue(fields)
            self.assertLessEqual(fields, allowed)
            self.assertFalse(factor["feedback"]["decision"])

    def test_v2_2_droe_registry_is_deterministic_and_frozen_before_live_probe(self):
        config = json.loads(
            (
                PROJECT_ROOT
                / "configs"
                / "profit_candidates_v2_2_tushare_droe1.json"
            ).read_text(encoding="utf-8")
        )
        metadata = config["metadata"]
        self.assertTrue(metadata["created_before_real_tushare_api_probe"])
        self.assertTrue(metadata["created_before_candidate_factor_derivation"])
        self.assertFalse(metadata["candidate_factor_values_viewed_before_registration"])
        self.assertFalse(metadata["candidate_forward_returns_viewed_before_registration"])
        self.assertEqual(metadata["current_state"], "BLOCKED_DATA_ACCESS")
        self.assertEqual(metadata["selection_horizon_trading_days"], 21)
        self.assertEqual(metadata["homogeneous_trial_count_after_v2_2"], 27)
        self.assertEqual(metadata["admission_factor_ids"], list(config["factors"]))

        factor_id, factor = next(iter(config["factors"].items()))
        expected = hashlib.sha256(
            (
                f"{metadata['factor_id_namespace']}:"
                f"{factor['factor_name']}:{factor['factor_expression']}"
            ).encode()
        ).hexdigest()[:16]
        self.assertEqual(factor_id, expected)
        self.assertEqual(factor["factor_expression"], f"${DROE_FEATURE}")
        self.assertIn("RESEARCH_ONLY", metadata["revision_policy"])

    def test_v2_2_runtime_and_gate_share_the_frozen_monthly_contract(self):
        gate = yaml.safe_load(
            (
                PROJECT_ROOT
                / "configs"
                / "profit_validation_v2_2_tushare_droe1.yaml"
            ).read_text(encoding="utf-8")
        )
        runtime = yaml.safe_load(
            (
                PROJECT_ROOT
                / "configs"
                / "backtest_profit_validation_v2_2_tushare_droe1.yaml"
            ).read_text(encoding="utf-8")
        )

        self.assertEqual(gate["protocol_version"], "2.2")
        self.assertEqual(gate["trial_matrix"]["expected_factor_count"], 27)
        self.assertEqual(
            gate["trial_matrix"]["target_factor_id"],
            "b0a4d1dde215df2f",
        )
        self.assertEqual(
            gate["validation"]["label"], runtime["dataset"]["label"]
        )
        self.assertEqual(gate["validation"]["return_holding_period_trading_days"], 21)
        self.assertEqual(runtime["backtest"]["strategy"]["kwargs"]["hold_thresh"], 21)
        self.assertEqual(
            gate["historical_provider"], runtime["data"]["provider_uri"]
        )
        self.assertIn(DROE_FEATURE, runtime["data"]["additional_fields"])
        self.assertEqual(gate["promotion"]["multiple_testing"]["total_trials"], 27)


if __name__ == "__main__":
    unittest.main()
