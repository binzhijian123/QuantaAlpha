import json
import shutil
import stat
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


BACKEND_ROOT = Path(__file__).resolve().parents[1] / "frontend-v2" / "backend"
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from market_data.intraday_calibration import (
    build_calibration_report,
    calibration_implementation_contract,
    finalize_calibration_report,
    load_calibration_declaration,
    read_verified_calibration_report,
)
from market_data.intraday_capture import (
    _canonical_json_bytes,
    _provider_context,
    _sha256_bytes,
    _slot_binding,
    capture_implementation_contract,
    run_capture_cycle,
)


CHINA_TZ = ZoneInfo("Asia/Shanghai")


def _write_provider(root: Path) -> Path:
    provider = root / "provider"
    (provider / "calendars").mkdir(parents=True)
    (provider / "instruments").mkdir(parents=True)
    (provider / "calendars" / "day.txt").write_text(
        "2026-08-10\n2026-08-11\n", encoding="utf-8"
    )
    (provider / "calendars" / "day_future.txt").write_text(
        "2026-08-10\n2026-08-11\n2026-08-12\n2026-08-13\n",
        encoding="utf-8",
    )
    lines = [
        f"SH{600000 + index:06d}\t2020-01-01\t2026-12-31"
        for index in range(300)
    ]
    (provider / "instruments" / "csi300.txt").write_text(
        "\n".join(lines) + "\n", encoding="utf-8"
    )
    return provider


class _QuoteClient:
    def __init__(self, quote_offset_minutes: int = 0):
        self.quote_offset_minutes = quote_offset_minutes

    def realtime_quotes(self, codes: list[str], **kwargs):
        now = kwargs["now"]
        quote_time = now.replace(minute=now.minute + self.quote_offset_minutes)
        return {
            code: {
                "symbol": code,
                "name": code,
                "price": 10.0,
                "previousClose": 9.9,
                "open": 9.95,
                "high": 10.1,
                "low": 9.9,
                "change": 0.1,
                "changePercent": 1.01,
                "amountCny": 1_000_000.0,
                "turnoverPercent": 0.5,
                "quoteAsOf": quote_time.isoformat(),
                "tradeDate": now.date().isoformat(),
                "isStale": False,
                "staleReason": None,
                "source": "a-stock-data/tencent-public",
            }
            for code in codes
        }


def _write_declaration(root: Path, provider: Path) -> Path:
    provider_context = _provider_context(provider)
    capture_contract = capture_implementation_contract()
    binding = _slot_binding(
        provider=provider_context,
        implementation=capture_contract,
        poll_seconds=60,
    )
    declaration = {
        "schemaVersion": 1,
        "protocolId": "intraday_capture_calibration_test_v1",
        "phase": "CALIBRATION_ONLY",
        "profitEvidenceEligible": False,
        "featureValuesPermitted": False,
        "returnValuesPermitted": False,
        "calibrationImplementationSha256": calibration_implementation_contract()[
            "contractSha256"
        ],
        "captureContract": {
            "pollSeconds": 60,
            "binding": binding,
        },
        "calibration": {
            "tradeDate": "2026-08-12",
            "continuousSessionWindows": [["10:00:00", "10:02:00"]],
            "expectedSlotCount": 3,
            "finalizeAfter": "10:03:00",
            "qualityGates": {
                "minimumSlotCoverageRatio": 1.0,
                "minimumMedianUsableCoverageRatio": 0.95,
                "minimumP10UsableCoverageRatio": 0.90,
                "minimumMedianCoreFieldCoverageRatio": 0.95,
                "minimumBenchmarkCoverageRatio": 1.0,
                "maximumConsecutiveMissingSlots": 0,
                "maximumOrphanSnapshots": 0,
                "maxQuoteAgeSeconds": 120,
                "maxFutureQuoteLeadSeconds": 5,
            },
        },
    }
    path = root / "calibration.json"
    raw = json.dumps(
        declaration,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    ).encode("utf-8") + b"\n"
    path.write_bytes(raw)
    path.with_suffix(".json.sha256").write_text(
        f"{_sha256_bytes(raw)}  {path.name}\n",
        encoding="ascii",
    )
    return path


def _capture_slots(provider: Path, output: Path, minutes=(0, 1, 2), client=None):
    quote_client = client or _QuoteClient()
    for minute in minutes:
        status = run_capture_cycle(
            provider,
            output,
            now=datetime(2026, 8, 12, 10, minute, tzinfo=CHINA_TZ),
            client=quote_client,
        )
        if status["status"] != "CAPTURED":
            raise AssertionError(status)


class IntradayCalibrationTest(unittest.TestCase):
    def test_production_declaration_is_bound_to_current_runtime(self):
        declaration_path = (
            Path(__file__).resolve().parents[1]
            / "configs/intraday_capture_calibration_v2_20260813.json"
        )
        declaration, _ = load_calibration_declaration(declaration_path)
        binding = declaration["captureContract"]["binding"]
        if not (Path(binding["providerPath"]) / "calendars/day.txt").is_file():
            self.skipTest("Original calibrated market Provider is not bundled with source; this is an external deployment integration check.")
        provider = _provider_context(Path(binding["providerPath"]))
        expected_binding = _slot_binding(
            provider=provider,
            implementation=capture_implementation_contract(),
            poll_seconds=declaration["captureContract"]["pollSeconds"],
        )

        self.assertEqual(declaration["calibration"]["tradeDate"], "2026-08-13")
        self.assertTrue(declaration["createdBeforeCurrentTradeDateQuotesObserved"])
        self.assertEqual(binding, expected_binding)
        self.assertEqual(
            declaration["calibrationImplementationSha256"],
            calibration_implementation_contract()["contractSha256"],
        )
        self.assertEqual(declaration["priorCalibration"]["status"], "FAIL")

    def test_complete_day_passes_without_feature_or_return_values(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            provider = _write_provider(root)
            declaration = _write_declaration(root, provider)
            output = root / "capture"
            _capture_slots(provider, output)

            report = build_calibration_report(
                declaration,
                output,
                now=datetime(2026, 8, 12, 10, 3, tzinfo=CHINA_TZ),
            )

            self.assertEqual(report["status"], "PASS")
            self.assertEqual(report["decision"], "DATA_CALIBRATION_PASS")
            self.assertFalse(report["profitEvidenceEligible"])
            self.assertFalse(report["featureValuesIncluded"])
            self.assertFalse(report["returnValuesIncluded"])
            self.assertEqual(report["observed"]["capturedExpectedSlotCount"], 3)
            self.assertEqual(report["observed"]["slotCoverageRatio"], 1.0)
            self.assertNotIn("quotes", json.dumps(report["sourceManifest"]))

    def test_report_is_deterministic_and_finalization_is_immutable(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            provider = _write_provider(root)
            declaration = _write_declaration(root, provider)
            output = root / "capture"
            _capture_slots(provider, output)
            now = datetime(2026, 8, 12, 10, 3, tzinfo=CHINA_TZ)

            first = build_calibration_report(declaration, output, now=now)
            second = build_calibration_report(declaration, output, now=now)
            self.assertEqual(first["reportSha256"], second["reportSha256"])

            summary = finalize_calibration_report(declaration, output, now=now)
            replay = finalize_calibration_report(declaration, output, now=now)
            self.assertEqual(summary, replay)
            report_path = Path(summary["file"])
            self.assertEqual(stat.S_IMODE(report_path.stat().st_mode), 0o444)
            self.assertEqual(
                read_verified_calibration_report(report_path)["reportSha256"],
                first["reportSha256"],
            )

    def test_missing_slot_fails_after_deadline(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            provider = _write_provider(root)
            declaration = _write_declaration(root, provider)
            output = root / "capture"
            _capture_slots(provider, output, minutes=(0, 2))

            report = build_calibration_report(
                declaration,
                output,
                now=datetime(2026, 8, 12, 10, 3, tzinfo=CHINA_TZ),
            )

            self.assertEqual(report["status"], "FAIL")
            self.assertFalse(report["gateResults"]["slotCoverage"])
            self.assertFalse(report["gateResults"]["longestMissingRun"])

    def test_future_quote_timestamp_fails_time_gate(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            provider = _write_provider(root)
            declaration = _write_declaration(root, provider)
            output = root / "capture"
            _capture_slots(provider, output, client=_QuoteClient(quote_offset_minutes=1))

            report = build_calibration_report(
                declaration,
                output,
                now=datetime(2026, 8, 12, 10, 3, tzinfo=CHINA_TZ),
            )

            self.assertEqual(report["status"], "FAIL")
            self.assertFalse(report["gateResults"]["quoteTimes"])

    def test_before_deadline_is_in_progress_and_not_persisted(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            provider = _write_provider(root)
            declaration = _write_declaration(root, provider)
            output = root / "capture"
            _capture_slots(provider, output, minutes=(0,))

            summary = finalize_calibration_report(
                declaration,
                output,
                now=datetime(2026, 8, 12, 10, 1, tzinfo=CHINA_TZ),
            )

            self.assertEqual(summary["status"], "IN_PROGRESS")
            self.assertFalse(summary["finalized"])
            self.assertIsNone(summary["file"])
            self.assertFalse((output / "audits").exists())

    def test_declaration_sidecar_tampering_fails_closed(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            provider = _write_provider(root)
            declaration = _write_declaration(root, provider)
            declaration.write_text("{}", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "declaration SHA256 mismatch"):
                build_calibration_report(declaration, root / "capture")

    def test_uncommitted_snapshot_is_reported_as_a_crash_orphan(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            provider = _write_provider(root)
            declaration = _write_declaration(root, provider)
            output = root / "capture"
            _capture_slots(provider, output)
            source = sorted((output / "snapshots" / "2026-08-12").glob("*.json.gz"))[0]
            orphan = source.parent / "snapshot_105959_orphan.json.gz"
            shutil.copyfile(source, orphan)

            report = build_calibration_report(
                declaration,
                output,
                now=datetime(2026, 8, 12, 10, 3, tzinfo=CHINA_TZ),
            )

            self.assertEqual(report["status"], "FAIL")
            self.assertFalse(report["gateResults"]["orphanSnapshots"])
            self.assertEqual(report["observed"]["orphanSnapshots"], [str(orphan.resolve())])


if __name__ == "__main__":
    unittest.main()
