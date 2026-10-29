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

from market_data.baostock_investment import (
    ASSET_GROWTH_FIELD,
    ANNUAL_GROWTH_FIELDS,
    BaoStockInvestmentSyncRequest,
    build_baostock_investment_panel,
    normalize_baostock_annual_asset_growth,
    sync_baostock_investment,
)
from market_data.sync_service import write_minimal_qlib_provider


def _payload(fields, rows, *, start_year=2021, end_year=2023):
    return {
        "code": 0,
        "msg": "success",
        "data": {"fields": list(fields), "items": [list(row) for row in rows]},
        "source": {
            "method": "query_growth_data",
            "reportFrequency": "annual",
            "quarter": 4,
            "startYear": start_year,
            "endYear": end_year,
            "requestCount": end_year - start_year + 1,
        },
    }


def _base_frame(start="2023-06-29", end="2023-07-05"):
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
            for date in pd.date_range(start, end, freq="B")
        ]
    )


class BaoStockInvestmentNormalizationTests(unittest.TestCase):
    def test_normalizes_annual_asset_growth_without_changing_sign(self):
        rows = pd.DataFrame(
            [
                {
                    "code": "sz.000001",
                    "pubDate": "2023-03-30",
                    "statDate": "2022-12-31",
                    "YOYAsset": "0.125",
                }
            ]
        )
        events, audit = normalize_baostock_annual_asset_growth(rows)

        self.assertEqual(events.iloc[0]["code"], "000001.SZ")
        self.assertEqual(events.iloc[0]["announcement_date"], pd.Timestamp("2023-03-30"))
        self.assertEqual(events.iloc[0]["available_date"], pd.Timestamp("2023-06-30"))
        self.assertAlmostEqual(events.iloc[0]["fy_asset_growth"], 0.125)
        self.assertEqual(audit["validAssetGrowthRows"], 1)
        self.assertEqual(audit["sourceCoverage"], 1.0)

    def test_invalid_temporal_rows_fail_closed(self):
        non_annual = pd.DataFrame(
            [
                {
                    "code": "sz.000001",
                    "pubDate": "2023-08-30",
                    "statDate": "2023-06-30",
                    "YOYAsset": "0.1",
                }
            ]
        )
        with self.assertRaisesRegex(ValueError, "non-annual"):
            normalize_baostock_annual_asset_growth(non_annual)

        early = non_annual.copy()
        early.loc[0, "statDate"] = "2023-12-31"
        early.loc[0, "pubDate"] = "2023-12-30"
        with self.assertRaisesRegex(ValueError, "before period end"):
            normalize_baostock_annual_asset_growth(early)

        late = early.copy()
        late.loc[0, "pubDate"] = "2024-07-01"
        events, audit = normalize_baostock_annual_asset_growth(late)
        self.assertTrue(events.empty)
        self.assertEqual(audit["latePublicationRowsExcluded"], 1)

    def test_panel_lags_publication_and_expires_after_eighteen_months(self):
        calendar = list(pd.date_range("2023-06-29", "2024-07-01", freq="B"))
        events = pd.DataFrame(
            [
                {
                    "code": "000001.SZ",
                    "report_end": pd.Timestamp("2022-12-31"),
                    "announcement_date": pd.Timestamp("2023-01-03"),
                    "available_date": pd.Timestamp("2023-06-30"),
                    "fy_asset_growth": 0.2,
                }
            ]
        )
        panel, audit = build_baostock_investment_panel(
            events,
            base_provider=Path("/tmp/base"),
            calendar=calendar,
            source_windows={
                "000001.SZ": (pd.Timestamp("2023-06-29"), pd.Timestamp("2024-07-01"))
            },
            codes=["000001.SZ"],
            publication_lag_sessions=1,
        )
        indexed = panel.set_index("date")

        self.assertTrue(np.isnan(indexed.loc[pd.Timestamp("2023-06-30"), ASSET_GROWTH_FIELD]))
        self.assertEqual(indexed.loc[pd.Timestamp("2023-07-03"), ASSET_GROWTH_FIELD], 0.2)
        self.assertEqual(indexed.loc[pd.Timestamp("2024-06-28"), ASSET_GROWTH_FIELD], 0.2)
        self.assertTrue(np.isnan(indexed.loc[pd.Timestamp("2024-07-01"), ASSET_GROWTH_FIELD]))
        self.assertEqual(audit["availabilityLeakRows"], 0)
        self.assertEqual(audit["reportPeriodLeakRows"], 0)
        self.assertEqual(audit["staleSignalRowsCleared"], 1)


class BaoStockInvestmentSyncTests(unittest.TestCase):
    class _Client:
        package_version = "0.test"

        def query_annual_history_raw(self, method_name, **kwargs):
            if method_name != "query_growth_data":
                raise AssertionError(f"unexpected method: {method_name}")
            fields = tuple(kwargs["required_fields"])
            values = {
                "code": "sz.000001",
                "pubDate": "2023-01-03",
                "statDate": "2022-12-31",
                "YOYAsset": "0.2",
            }
            return _payload(
                fields,
                (tuple(values[field] for field in fields),),
                start_year=kwargs["start_year"],
                end_year=kwargs["end_year"],
            )

        def close(self):
            return None

    def test_sync_is_isolated_and_reuse_is_network_free(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            base = root / "base"
            output = root / "enriched"
            write_minimal_qlib_provider(_base_frame(), base)
            request = BaoStockInvestmentSyncRequest(
                base_provider=base,
                codes=["000001.SZ"],
                start_date="2023-06-29",
                end_date="2023-07-05",
                output_provider=output,
                batch_id="investment-test",
            )
            result = sync_baostock_investment(root, request, client=self._Client())
            panel = pd.read_csv(
                Path(result["stagingDir"]) / "point_in_time_investment.csv.gz"
            ).set_index("date")

            self.assertEqual(result["status"], "PASS")
            self.assertTrue(np.isnan(panel.loc["2023-06-30", ASSET_GROWTH_FIELD]))
            self.assertEqual(panel.loc["2023-07-03", ASSET_GROWTH_FIELD], 0.2)
            self.assertFalse(
                (base / "features" / "sz000001" / f"{ASSET_GROWTH_FIELD}.day.bin").exists()
            )
            self.assertTrue(
                (output / "features" / "sz000001" / f"{ASSET_GROWTH_FIELD}.day.bin").exists()
            )

            class _NeverClient:
                package_version = "never"

                def query_annual_history_raw(self, *args, **kwargs):
                    raise AssertionError("raw response should have been reused")

                def close(self):
                    return None

            reused = sync_baostock_investment(
                root,
                replace(
                    request,
                    batch_id="investment-reused",
                    output_provider=None,
                    reuse_raw_dir=Path(result["rawDir"]),
                ),
                client=_NeverClient(),
            )
            first_manifest = json.loads(Path(result["manifestPath"]).read_text())
            reused_manifest = json.loads(Path(reused["manifestPath"]).read_text())
            self.assertEqual(reused["status"], "PASS")
            self.assertEqual(reused_manifest["reusedResponses"], 1)
            self.assertEqual(reused_manifest["queriedResponses"], 0)
            self.assertEqual(
                first_manifest["rawPayloadSha256"],
                reused_manifest["rawPayloadSha256"],
            )

    def test_v2_1_registry_is_deterministic_and_frozen(self):
        path = PROJECT_ROOT / "configs" / "profit_candidates_v2_1_baostock_investment_to_assets.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        metadata = payload["metadata"]
        self.assertTrue(metadata["created_before_factor_derivation"])
        self.assertFalse(metadata["candidate_factor_values_viewed_before_registration"])
        self.assertFalse(metadata["candidate_forward_returns_viewed_before_registration"])
        self.assertEqual(metadata["selection_horizon_trading_days"], 252)
        self.assertEqual(metadata["homogeneous_trial_count_after_v2_1"], 26)
        self.assertEqual(metadata["admission_factor_ids"], list(payload["factors"]))
        self.assertEqual(len(payload["factors"]), 1)

        factor_id, factor = next(iter(payload["factors"].items()))
        expected = hashlib.sha256(
            (
                f"{metadata['factor_id_namespace']}:"
                f"{factor['factor_name']}:{factor['factor_expression']}"
            ).encode()
        ).hexdigest()[:16]
        self.assertEqual(factor_id, expected)
        self.assertEqual(factor["factor_expression"], "-$pit_fy_asset_growth")
        self.assertIn("RESEARCH_ONLY", metadata["revision_policy"])
        self.assertEqual(set(ANNUAL_GROWTH_FIELDS), {"code", "pubDate", "statDate", "YOYAsset"})

    def test_v2_1_runtime_and_gate_share_the_frozen_annual_contract(self):
        gate = yaml.safe_load(
            (
                PROJECT_ROOT
                / "configs"
                / "profit_validation_v2_1_baostock_investment_to_assets.yaml"
            ).read_text(encoding="utf-8")
        )
        runtime = yaml.safe_load(
            (
                PROJECT_ROOT
                / "configs"
                / "backtest_profit_validation_v2_1_baostock_investment_to_assets.yaml"
            ).read_text(encoding="utf-8")
        )

        self.assertEqual(gate["validation"]["label"], runtime["dataset"]["label"])
        self.assertEqual(gate["validation"]["return_holding_period_trading_days"], 252)
        self.assertEqual(runtime["backtest"]["strategy"]["kwargs"]["hold_thresh"], 252)
        self.assertEqual(runtime["dataset"]["segments"]["train"][1], "2018-12-17")
        self.assertEqual(runtime["dataset"]["segments"]["valid"][1], "2020-12-17")
        self.assertEqual(gate["validation"]["validation_end"], "2020-12-17")
        self.assertEqual(gate["trial_matrix"]["expected_factor_count"], 26)
        self.assertEqual(gate["promotion"]["multiple_testing"]["total_trials"], 26)
        self.assertIn("pit_fy_asset_growth", runtime["data"]["additional_fields"])


if __name__ == "__main__":
    unittest.main()
