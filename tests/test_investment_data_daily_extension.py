import json
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = PROJECT_ROOT / "frontend-v2" / "backend"
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from market_data.investment_data_daily_extension import (
    FIELDS,
    RAW_PROVIDER_NAME,
    InvestmentDataDailyExtensionRequest,
    _adjustment_evidence_policy,
    _directory_tree_contract,
    audit_symbol_mapping,
    build_extension_rows,
    build_immutable_provider,
    sync_investment_data_daily_extension,
)
from market_data.paths import raw_provider_dir, report_path
from market_data.providers.a_stock_data import AStockDataClient
from quantaalpha.utils.qlib_provider import provider_contract_fingerprint


class _Response:
    def __init__(self, payload):
        self.text = json.dumps(payload)

    def raise_for_status(self):
        return None


class _AdjustedSession:
    def __init__(self, key):
        self.headers = {}
        self.key = key

    def get(self, url, **kwargs):
        return _Response(
            {
                "data": {
                    "sh600000": {
                        self.key: [
                            ["2026-08-04", "9.5", "9.4", "9.6", "9.3", "1000", {}],
                            ["2026-08-05", "9.4", "9.3", "9.5", "9.2", "900", {}],
                        ]
                    }
                }
            }
        )


def _qlib_code(code):
    digits, exchange = code.split(".")
    return f"{exchange.lower()}{digits}"


def _instrument(code):
    digits, exchange = code.split(".")
    return f"{exchange}{digits}"


def _source_frames(dates, code="600000.SH", factor=0.5):
    close = 10.0 + np.arange(len(dates), dtype="float64") * 0.08
    raw = pd.DataFrame(
        {
            "date": dates,
            "code": code,
            "open": close * 0.99,
            "high": close * 1.01,
            "low": close * 0.98,
            "close": close,
            "volume": 1_000_000.0 + np.arange(len(dates)) * 1000.0,
            "amount": close * (1_000_000.0 + np.arange(len(dates)) * 1000.0),
            "source_volume": (1_000_000.0 + np.arange(len(dates)) * 1000.0) / 100.0,
            "corporate_action": [{} for _ in dates],
            "source": "tencent",
        }
    )
    adjusted = raw[["date", "code", "open", "high", "low", "close", "source_volume", "corporate_action"]].copy()
    adjusted["adjustment_key"] = "qfqday"
    adjusted["source"] = "tencent_qfq"
    return raw, adjusted


def _write_synthetic_provider(root, calendar, codes, market="custom", factor=0.5):
    provider = root / "base"
    (provider / "calendars").mkdir(parents=True)
    (provider / "instruments").mkdir(parents=True)
    (provider / "calendars" / "day.txt").write_text(
        "\n".join(calendar) + "\n", encoding="utf-8"
    )
    future = [*calendar, "2026-08-05", "2026-08-06"]
    (provider / "calendars" / "day_future.txt").write_text(
        "\n".join(future) + "\n", encoding="utf-8"
    )
    all_lines = [f"{_instrument(code)}\t{calendar[0]}\t{calendar[-1]}" for code in codes]
    (provider / "instruments" / "all.txt").write_text(
        "\n".join(all_lines) + "\n", encoding="utf-8"
    )
    (provider / "instruments" / f"{market}.txt").write_text(
        f"{_instrument(codes[0])}\t{calendar[0]}\t{calendar[-1]}\n",
        encoding="utf-8",
    )
    sources = {}
    for code in codes:
        raw, adjusted = _source_frames(calendar, code, factor)
        sources[code] = (raw, adjusted)
        code_dir = provider / "features" / _qlib_code(code)
        code_dir.mkdir(parents=True)
        normalized_close = raw["close"].to_numpy() * factor
        values = {
            "open": raw["open"].to_numpy() * factor,
            "high": raw["high"].to_numpy() * factor,
            "low": raw["low"].to_numpy() * factor,
            "close": normalized_close,
            "volume": raw["volume"].to_numpy() / (100.0 * factor),
            "amount": raw["amount"].to_numpy() / 1000.0,
            "factor": np.full(len(calendar), factor),
            "change": raw["close"].pct_change(fill_method=None).to_numpy(),
            "adjclose": normalized_close * 20.0,
        }
        values["vwap"] = values["amount"] * 10.0 / values["volume"]
        for field in FIELDS:
            np.hstack([[0.0], values[field]]).astype("<f").tofile(
                code_dir / f"{field}.day.bin"
            )
    return provider, sources


def _write_source_manifest(
    root, base_fingerprint, targets, audit_summary, forward_evidence_dates=()
):
    artifacts = root / "source_artifacts"
    artifacts.mkdir()
    (artifacts / "snapshot.csv").write_text("date,close\n2026-08-04,1\n", encoding="utf-8")
    artifact_contract = _directory_tree_contract(artifacts)
    manifest = root / "source.json"
    manifest.write_text(
        json.dumps(
            {
                "schemaVersion": "quantaalpha-market-data-source-v2",
                "kind": "frozen_market_daily_prices",
                "baseProviderFingerprint": base_fingerprint,
                "request": {
                    "targetDates": targets,
                    "forwardAdjustmentEvidenceDates": list(
                        forward_evidence_dates
                    ),
                },
                "artifacts": {"root": str(artifacts), **artifact_contract},
                "audit": audit_summary,
            }
        ),
        encoding="utf-8",
    )
    return manifest


class InvestmentDataDailyExtensionTests(unittest.TestCase):
    def test_adjusted_parser_accepts_qfqday_and_identity_day(self):
        for key in ("qfqday", "day"):
            client = AStockDataClient(session=_AdjustedSession(key))
            frame = client.tencent_adjusted_history(
                "600000.SH", "2026-08-04", "2026-08-05"
            )
            self.assertEqual(frame["date"].tolist(), ["2026-08-04", "2026-08-05"])
            self.assertEqual(set(frame["adjustment_key"]), {key})
            self.assertEqual(float(frame.loc[0, "close"]), 9.4)

    def test_mapping_audit_and_missing_latest_qfq_are_reconciled(self):
        calendar = pd.bdate_range(end="2026-08-04", periods=25).strftime("%Y-%m-%d").tolist()
        targets = ["2026-08-05", "2026-08-06"]
        with tempfile.TemporaryDirectory() as tmp:
            provider, _ = _write_synthetic_provider(
                Path(tmp), calendar, ["600000.SH", "000300.SH"]
            )
            raw, adjusted = _source_frames([*calendar, *targets])
            audit, context = audit_symbol_mapping(
                provider,
                "600000.SH",
                raw,
                adjusted,
                calendar,
                calendar,
                calendar[-1],
            )
            adjusted = adjusted[adjusted["date"] != targets[-1]]
            panel = build_extension_rows(
                "600000.SH", raw, adjusted, targets, context
            )

        self.assertEqual(audit["status"], "PASS")
        self.assertEqual(panel["syntheticAdjustment"].tolist(), [False, True])
        self.assertAlmostEqual(float(panel.loc[0, "factor"]), 0.5, places=7)
        expected_change = float(raw.set_index("date").loc[targets[0], "close"]) / float(
            raw.set_index("date").loc[calendar[-1], "close"]
        ) - 1.0
        self.assertAlmostEqual(float(panel.loc[0, "change"]), expected_change, places=10)

    def test_raw_change_is_preserved_across_a_corporate_action(self):
        calendar = pd.bdate_range(end="2026-08-04", periods=25).strftime("%Y-%m-%d").tolist()
        with tempfile.TemporaryDirectory() as tmp:
            provider, _ = _write_synthetic_provider(
                Path(tmp), calendar, ["600000.SH", "000300.SH"]
            )
            raw, adjusted = _source_frames(calendar)
            action_index = len(calendar) - 5
            raw.loc[action_index, "close"] *= 0.9
            raw.loc[action_index, "open"] *= 0.9
            raw.loc[action_index, "high"] *= 0.9
            raw.loc[action_index, "low"] *= 0.9
            raw.loc[action_index, "amount"] = raw.loc[action_index, "close"] * raw.loc[action_index, "volume"]
            raw.at[action_index, "corporate_action"] = {"cash": "1.0"}
            adjusted.loc[: action_index - 1, ["open", "high", "low", "close"]] *= 0.9

            # Rebuild the base fields from the adjusted prices while keeping raw change.
            for field in ("open", "high", "low", "close"):
                values = adjusted[field].to_numpy() * 0.5
                np.hstack([[0.0], values]).astype("<f").tofile(
                    provider / "features" / "sh600000" / f"{field}.day.bin"
                )
            factor_values = adjusted["close"].to_numpy() * 0.5 / raw["close"].to_numpy()
            volume_values = raw["volume"].to_numpy() / (100.0 * factor_values)
            amount_values = raw["amount"].to_numpy() / 1000.0
            derived = {
                "factor": factor_values,
                "volume": volume_values,
                "amount": amount_values,
                "vwap": amount_values * 10.0 / volume_values,
                "change": raw["close"].pct_change(fill_method=None).to_numpy(),
                "adjclose": adjusted["close"].to_numpy() * 10.0,
            }
            for field, values in derived.items():
                np.hstack([[0.0], values]).astype("<f").tofile(
                    provider / "features" / "sh600000" / f"{field}.day.bin"
                )
            audit, _ = audit_symbol_mapping(
                provider,
                "600000.SH",
                raw,
                adjusted,
                calendar,
                calendar,
                calendar[-1],
            )

        self.assertEqual(audit["status"], "PASS")
        self.assertLess(
            audit["metrics"]["changeAbsoluteError"]["maximum"], 2e-6
        )

    def test_suspension_requires_evidence_and_carries_only_factor(self):
        dates = pd.bdate_range(end="2026-08-05", periods=25).strftime("%Y-%m-%d").tolist()
        target = "2026-08-06"
        raw, adjusted = _source_frames(dates)
        context = {
            "priceScale": 0.5,
            "adjcloseToClose": 20.0,
            "adjustmentKey": "qfqday",
            "lastFactor": 0.5,
        }
        with self.assertRaisesRegex(ValueError, "suspension evidence"):
            build_extension_rows(
                "600000.SH", raw, adjusted, [target], context
            )
        panel = build_extension_rows(
            "600000.SH",
            raw,
            adjusted,
            [target],
            context,
            explicit_no_bar_dates={target},
        )
        self.assertTrue(panel.loc[0, "noBar"])
        self.assertEqual(float(panel.loc[0, "factor"]), 0.5)
        self.assertTrue(np.isnan(panel.loc[0, "close"]))

    def test_day_identity_payload_with_action_fails_audit(self):
        calendar = pd.bdate_range(end="2026-08-04", periods=25).strftime("%Y-%m-%d").tolist()
        with tempfile.TemporaryDirectory() as tmp:
            provider, _ = _write_synthetic_provider(
                Path(tmp), calendar, ["600000.SH", "000300.SH"]
            )
            raw, adjusted = _source_frames(calendar)
            adjusted["adjustment_key"] = "day"
            raw.at[raw.index[-2], "corporate_action"] = {"cash": "1.0"}
            audit, _ = audit_symbol_mapping(
                provider,
                "600000.SH",
                raw,
                adjusted,
                calendar,
                calendar,
                calendar[-1],
            )
        self.assertEqual(audit["status"], "FAIL")
        self.assertIn("day payload cannot cover corporate actions", audit["failures"])

    def test_qfq_gate_uses_the_post_action_adjustment_regime(self):
        calendar = pd.bdate_range(end="2026-08-04", periods=25).strftime("%Y-%m-%d").tolist()
        with tempfile.TemporaryDirectory() as tmp:
            provider, _ = _write_synthetic_provider(
                Path(tmp), calendar, ["600000.SH", "000300.SH"]
            )
            raw, adjusted = _source_frames(calendar)
            action_index = len(calendar) - 5
            raw.at[action_index, "corporate_action"] = {"cash": "1.0"}
            adjusted.loc[: action_index - 1, ["open", "high", "low", "close"]] *= 0.97
            audit, _ = audit_symbol_mapping(
                provider,
                "600000.SH",
                raw,
                adjusted,
                calendar,
                calendar,
                calendar[-1],
            )

        self.assertEqual(audit["status"], "PASS")
        self.assertEqual(audit["lastCorporateActionDate"], calendar[action_index])
        self.assertGreater(
            audit["metrics"]["qfqReplayRelativeError"]["maximum"], 0.01
        )
        self.assertLess(
            audit["metrics"]["qfqAdjustmentRegimeRelativeError"]["maximum"],
            1e-6,
        )

    def test_forward_adjustment_evidence_is_explicit_and_uses_the_full_chain(self):
        calendar = pd.bdate_range(end="2026-08-04", periods=25).strftime("%Y-%m-%d").tolist()
        targets = ["2026-08-05", "2026-08-06"]
        with tempfile.TemporaryDirectory() as tmp:
            provider, _ = _write_synthetic_provider(
                Path(tmp), calendar, ["600000.SH", "000300.SH"]
            )
            raw, adjusted = _source_frames([*calendar, *targets])
            raw.at[len(calendar) - 2, "corporate_action"] = {"cash": "1.0"}
            blocked, _ = audit_symbol_mapping(
                provider,
                "600000.SH",
                raw,
                adjusted,
                calendar,
                calendar,
                calendar[-1],
            )
            approved, _ = audit_symbol_mapping(
                provider,
                "600000.SH",
                raw,
                adjusted,
                calendar,
                calendar,
                calendar[-1],
                forward_adjustment_evidence_dates=targets,
            )

        self.assertEqual(blocked["status"], "FAIL")
        self.assertIn(
            "insufficient post-corporate-action adjustment regime",
            blocked["failures"],
        )
        self.assertEqual(approved["status"], "PASS")
        self.assertEqual(approved["directAdjustmentRegimeSessions"], 2)
        self.assertTrue(approved["forwardAdjustmentEvidenceUsed"])
        self.assertEqual(
            approved["forwardAdjustmentEvidence"]["requestedDates"],
            [calendar[-1], *targets],
        )
        self.assertEqual(approved["forwardAdjustmentEvidence"]["stableDates"], [calendar[-1], *targets])
        self.assertEqual(approved["adjustmentRegimeSessions"], 2)
        self.assertEqual(approved["adjustmentEvidenceSessions"], 4)

    def test_forward_adjustment_evidence_rejects_unstable_missing_or_action_rows(self):
        calendar = pd.bdate_range(end="2026-08-04", periods=25).strftime("%Y-%m-%d").tolist()
        targets = ["2026-08-05", "2026-08-06"]
        with tempfile.TemporaryDirectory() as tmp:
            provider, _ = _write_synthetic_provider(
                Path(tmp), calendar, ["600000.SH", "000300.SH"]
            )
            for case in ("unstable", "missing", "action", "synthetic"):
                with self.subTest(case=case):
                    raw, adjusted = _source_frames([*calendar, *targets])
                    raw.at[len(calendar) - 2, "corporate_action"] = {"cash": "1.0"}
                    if case == "unstable":
                        adjusted.loc[
                            adjusted["date"] == targets[-1], "close"
                        ] *= 0.999
                    elif case == "missing":
                        adjusted = adjusted[adjusted["date"] != targets[-1]]
                    else:
                        if case == "action":
                            raw.loc[
                                raw["date"] == targets[-1], "corporate_action"
                            ] = raw.loc[
                                raw["date"] == targets[-1], "corporate_action"
                            ].map(lambda _: {"cash": "0.1"})
                        else:
                            adjusted["synthetic"] = False
                            adjusted.loc[
                                adjusted["date"] == targets[-1], "synthetic"
                            ] = True
                    audit, _ = audit_symbol_mapping(
                        provider,
                        "600000.SH",
                        raw,
                        adjusted,
                        calendar,
                        calendar,
                        calendar[-1],
                        forward_adjustment_evidence_dates=targets,
                    )
                    self.assertEqual(audit["status"], "FAIL")
                    self.assertFalse(audit["forwardAdjustmentEvidenceUsed"])
                    self.assertEqual(
                        audit["forwardAdjustmentEvidence"]["status"], "FAIL"
                    )

    def test_forward_adjustment_evidence_keeps_two_direct_base_sessions(self):
        calendar = pd.bdate_range(end="2026-08-04", periods=25).strftime("%Y-%m-%d").tolist()
        targets = ["2026-08-05", "2026-08-06"]
        with tempfile.TemporaryDirectory() as tmp:
            provider, _ = _write_synthetic_provider(
                Path(tmp), calendar, ["600000.SH", "000300.SH"]
            )
            raw, adjusted = _source_frames([*calendar, *targets])
            raw.at[len(calendar) - 1, "corporate_action"] = {"cash": "1.0"}
            audit, _ = audit_symbol_mapping(
                provider,
                "600000.SH",
                raw,
                adjusted,
                calendar,
                calendar,
                calendar[-1],
                forward_adjustment_evidence_dates=targets,
            )

        self.assertEqual(audit["status"], "FAIL")
        self.assertIn(
            "insufficient direct base overlap for forward adjustment evidence",
            audit["failures"],
        )

    def test_immutable_build_preserves_base_prefix_and_all_market_scope(self):
        calendar = pd.bdate_range(end="2026-08-04", periods=25).strftime("%Y-%m-%d").tolist()
        targets = ["2026-08-05", "2026-08-06"]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            provider, _ = _write_synthetic_provider(
                root, calendar, ["600000.SH", "000300.SH"]
            )
            custom_file = provider / "instruments" / "custom.txt"
            custom_file.write_text(
                custom_file.read_text(encoding="utf-8")
                + "SHT00018\t2005-04-08\t2007-01-03\n",
                encoding="utf-8",
            )
            panels = []
            for code in ("600000.SH", "000300.SH"):
                raw, adjusted = _source_frames([*calendar, *targets], code)
                audit, context = audit_symbol_mapping(
                    provider,
                    code,
                    raw,
                    adjusted,
                    calendar,
                    calendar,
                    calendar[-1],
                )
                self.assertEqual(audit["status"], "PASS")
                panels.append(build_extension_rows(code, raw, adjusted, targets, context))
            panel = pd.concat(panels, ignore_index=True)
            base_fingerprint = provider_contract_fingerprint(provider, "custom")[
                "fingerprint"
            ]
            audit_summary = {
                "status": "PASS",
                "adjustmentEvidencePolicy": _adjustment_evidence_policy(
                    calendar[-1], []
                ),
            }
            source_manifest = _write_source_manifest(
                root, base_fingerprint, targets, audit_summary
            )
            output = root / "extended"
            base_feature = provider / "features" / "sh600000" / "close.day.bin"
            base_bytes = base_feature.read_bytes()
            all_bytes = (provider / "instruments" / "all.txt").read_bytes()

            with self.assertRaisesRegex(RuntimeError, "audit summary mismatch"):
                build_immutable_provider(
                    provider,
                    root / "policy-mismatch",
                    panel,
                    ["600000.SH"],
                    targets,
                    market="custom",
                    benchmark="000300.SH",
                    batch_id="policy-mismatch",
                    source_manifest=source_manifest,
                    created_at="2026-08-06T16:00:00+08:00",
                    audit_summary={
                        "status": "PASS",
                        "adjustmentEvidencePolicy": _adjustment_evidence_policy(
                            calendar[-1], targets
                        ),
                    },
                )

            artifact_file = root / "source_artifacts" / "snapshot.csv"
            artifact_bytes = artifact_file.read_bytes()
            artifact_file.write_text("tampered\n", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "source artifact tree mismatch"):
                build_immutable_provider(
                    provider,
                    root / "artifact-mismatch",
                    panel,
                    ["600000.SH"],
                    targets,
                    market="custom",
                    benchmark="000300.SH",
                    batch_id="artifact-mismatch",
                    source_manifest=source_manifest,
                    created_at="2026-08-06T16:00:00+08:00",
                    audit_summary=audit_summary,
                )
            artifact_file.write_bytes(artifact_bytes)

            result = build_immutable_provider(
                provider,
                output,
                panel,
                ["600000.SH"],
                targets,
                market="custom",
                benchmark="000300.SH",
                batch_id="unit-test",
                source_manifest=source_manifest,
                created_at="2026-08-06T16:00:00+08:00",
                audit_summary=audit_summary,
            )

            self.assertEqual(base_feature.read_bytes(), base_bytes)
            output_bytes = (output / "features" / "sh600000" / "close.day.bin").read_bytes()
            self.assertEqual(output_bytes[: len(base_bytes)], base_bytes)
            self.assertEqual(len(output_bytes), len(base_bytes) + 8)
            self.assertEqual((output / "instruments" / "all.txt").read_bytes(), all_bytes)
            output_custom = (output / "instruments" / "custom.txt").read_text(
                encoding="utf-8"
            )
            self.assertIn(
                f"SH600000\t{calendar[0]}\t{targets[-1]}", output_custom
            )
            self.assertIn(
                "SHT00018\t2005-04-08\t2007-01-03",
                output_custom,
            )
            self.assertEqual(result["extendedMarketInstruments"], 1)
            self.assertNotEqual(result["fingerprint"], result["baseFingerprint"])
            self.assertEqual(
                provider_contract_fingerprint(output, "custom")["fingerprint"],
                result["fingerprint"],
            )

            values = np.fromfile(
                output / "features" / "sh600000" / "close.day.bin", dtype="<f"
            )
            values[-1] += 1.0
            values.tofile(output / "features" / "sh600000" / "close.day.bin")
            with self.assertRaisesRegex(RuntimeError, "feature tree"):
                provider_contract_fingerprint(output, "custom")

    def test_build_rejects_failed_audit_contract(self):
        calendar = pd.bdate_range(end="2026-08-04", periods=25).strftime("%Y-%m-%d").tolist()
        targets = ["2026-08-05", "2026-08-06"]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            provider, _ = _write_synthetic_provider(
                root, calendar, ["600000.SH", "000300.SH"]
            )
            rows = []
            for code in ("600000.SH", "000300.SH"):
                raw, adjusted = _source_frames([*calendar, *targets], code)
                _, context = audit_symbol_mapping(
                    provider, code, raw, adjusted, calendar, calendar, calendar[-1]
                )
                rows.append(build_extension_rows(code, raw, adjusted, targets, context))
            source_manifest = root / "source.json"
            source_manifest.write_text(
                json.dumps(
                    {
                        "kind": "frozen_market_daily_prices",
                        "baseProviderFingerprint": provider_contract_fingerprint(
                            provider, "custom"
                        )["fingerprint"],
                        "request": {"targetDates": targets},
                        "audit": {"status": "PASS"},
                    }
                ),
                encoding="utf-8",
            )
            output = root / "blocked"
            with self.assertRaisesRegex(RuntimeError, "audit must pass"):
                build_immutable_provider(
                    provider,
                    output,
                    pd.concat(rows, ignore_index=True),
                    ["600000.SH"],
                    targets,
                    market="custom",
                    benchmark="000300.SH",
                    batch_id="failed-audit",
                    source_manifest=source_manifest,
                    created_at="2026-08-06T16:00:00+08:00",
                    audit_summary={"status": "FAIL"},
                )
            self.assertFalse(output.exists())

    def test_sync_rejects_an_unimplemented_snapshot_count_before_io(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            request = InvestmentDataDailyExtensionRequest(
                base_provider=root / "missing",
                end_date="2026-08-06",
                snapshot_rechecks=3,
            )
            with self.assertRaisesRegex(ValueError, "one or two"):
                sync_investment_data_daily_extension(root, request)

    def test_sync_forward_evidence_requires_two_snapshots_before_io(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            request = InvestmentDataDailyExtensionRequest(
                base_provider=root / "missing",
                end_date="2026-08-06",
                snapshot_rechecks=1,
                forward_adjustment_evidence_dates=("2026-08-05", "2026-08-06"),
            )
            with self.assertRaisesRegex(ValueError, "requires two source snapshots"):
                sync_investment_data_daily_extension(root, request)

    def test_sync_forward_evidence_dates_must_match_the_target_dates(self):
        calendar = pd.bdate_range(end="2026-08-04", periods=25).strftime("%Y-%m-%d").tolist()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            provider, _ = _write_synthetic_provider(
                root, calendar, ["600000.SH", "000300.SH"]
            )
            request = InvestmentDataDailyExtensionRequest(
                base_provider=provider,
                end_date="2026-08-06",
                market="custom",
                forward_adjustment_evidence_dates=("2026-08-05",),
            )
            with self.assertRaisesRegex(ValueError, "exactly match the target dates"):
                sync_investment_data_daily_extension(root, request)

    def test_sync_can_replay_immutable_double_snapshots_without_network(self):
        calendar = pd.bdate_range(end="2026-08-04", periods=25).strftime("%Y-%m-%d").tolist()
        targets = ["2026-08-05", "2026-08-06"]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            provider, _ = _write_synthetic_provider(
                root, calendar, ["600000.SH", "000300.SH"]
            )
            source_batch = "prior-failed-audit"
            source_root = raw_provider_dir(root, RAW_PROVIDER_NAME, source_batch)
            for code in ("600000.SH", "000300.SH"):
                raw, adjusted = _source_frames([*calendar, *targets], code)
                raw.at[len(calendar) - 2, "corporate_action"] = {"cash": "1.0"}
                code_key = _qlib_code(code)
                for snapshot in ("snapshot_1", "snapshot_2"):
                    raw_path = source_root / snapshot / "raw" / f"{code_key}.csv"
                    adjusted_path = (
                        source_root / snapshot / "adjusted" / f"{code_key}.csv"
                    )
                    raw_path.parent.mkdir(parents=True, exist_ok=True)
                    adjusted_path.parent.mkdir(parents=True, exist_ok=True)
                    raw.to_csv(raw_path, index=False)
                    adjusted.to_csv(adjusted_path, index=False)
            base_fingerprint = provider_contract_fingerprint(provider, "custom")[
                "fingerprint"
            ]
            prior_report = report_path(root, source_batch)
            prior_report.parent.mkdir(parents=True, exist_ok=True)
            prior_report.write_text(
                json.dumps(
                    {
                        "status": "FAIL",
                        "provider": "a-stock-data/tencent-public",
                        "baseProviderFingerprint": base_fingerprint,
                        "targetDates": targets,
                        "rawDir": str(source_root.resolve()),
                        "finishedAt": "2026-08-06T16:00:00+08:00",
                    }
                ),
                encoding="utf-8",
            )
            output = root / "replayed-provider"
            request = InvestmentDataDailyExtensionRequest(
                base_provider=provider,
                output_provider=output,
                end_date="2026-08-06",
                market="custom",
                batch_id="replay-publish",
                forward_adjustment_evidence_dates=tuple(targets),
                source_snapshot_dir=source_root,
            )

            result = sync_investment_data_daily_extension(root, request)

            self.assertEqual(result["status"], "PASS")
            self.assertEqual(result["sourceReplay"]["batchId"], source_batch)
            self.assertEqual(
                result["audit"]["sourceCaptureMode"],
                "immutable_snapshot_replay",
            )
            self.assertTrue(output.is_dir())
            self.assertFalse(
                raw_provider_dir(root, RAW_PROVIDER_NAME, "replay-publish").exists()
            )


if __name__ == "__main__":
    unittest.main()
