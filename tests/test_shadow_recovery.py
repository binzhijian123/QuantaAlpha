"""Opt-in recovery tests never start a worker, network request, or real ledger."""
from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
import shutil
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

from fastapi import FastAPI
from fastapi.testclient import TestClient

BACKEND_ROOT = Path(__file__).resolve().parents[1] / "frontend-v2/backend"
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))
import shadow_recovery as recovery

NOW = datetime(2026, 10, 23, 11, 0, tzinfo=ZoneInfo("Asia/Shanghai"))


def _write_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    path.with_suffix(path.suffix + ".sha256").write_text(
        f"{recovery._sha(path)}  {path.name}\n", encoding="ascii")


def _fixture(root):
    root = Path(root).resolve()
    provider = root / "data/market/a_share/qlib/new_provider"
    (provider / "calendars").mkdir(parents=True)
    (provider / "instruments").mkdir()
    (provider / "calendars/day.txt").write_text("2026-10-21\n2026-10-22\n")
    (provider / "calendars/day_future.txt").write_text("2026-10-22\n2026-10-23\n2026-10-26\n")
    (provider / "instruments/csi300.txt").write_text("\n".join(
        f"SH{600000+i}\t2020-01-01\t2026-12-31" for i in range(300)) + "\n")
    implementation = recovery._capture_v4.capture_implementation_contract()
    binding = recovery._capture._slot_binding(
        provider=recovery._capture._provider_context(provider), implementation=implementation,
        poll_seconds=60)
    calibration = {
        "schemaVersion": 1, "protocolId": "recovery_calibration_test",
        "phase": "CALIBRATION_ONLY", "profitEvidenceEligible": False,
        "featureValuesPermitted": False, "returnValuesPermitted": False,
        "createdAt": "2026-10-23T10:00:00+08:00",
        "createdBeforeCurrentTradeDateQuotesObserved": True,
        "calibrationImplementationSha256": recovery.calibration_implementation_contract()["contractSha256"],
        "calibration": {"tradeDate": "2026-10-26", "continuousSessionWindows": recovery._WINDOWS,
                        "expectedSlotCount": 242, "finalizeAfter": "15:05:00",
                        "qualityGates": dict(recovery._QUALITY_GATES)},
        "captureContract": {"pollSeconds": 60, "requestedSymbolCount": 301, "binding": binding},
    }
    calibration_path = root / "configs/recovery_calibration.json"
    _write_json(calibration_path, calibration)
    files = []
    for relative in sorted(recovery._REQUIRED_FILES):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.name == "shadow_recovery.py":
            shutil.copy2(Path(recovery.__file__), path)
        else:
            path.write_text("# temporary fixture implementation\n")
        files.append({"path": relative, "sha256": recovery._sha(path)})
    declaration = {
        "schemaVersion": 1, "phase": "CALIBRATION_ONLY", "profitEvidenceEligible": False,
        "createdAt": "2026-10-23T10:30:00+08:00", "createdBeforeTradeDate": True,
        "tradeDate": "2026-10-26", "providerPath": str(provider),
        "outputRoot": str(root / "data/market/a_share/intraday/recovery_test"),
        "pollSeconds": 60, "calibrationDeclaration": str(calibration_path),
        "calibrationDeclarationSha256": recovery._sha(calibration_path),
        "captureImplementationSha256": implementation["sha256"],
        "providerBinding": binding, "recoveryImplementation": {"files": files},
    }
    declaration_path = root / "configs/recovery.json"
    _write_json(declaration_path, declaration)
    values = {recovery.DECLARATION_KEY: str(declaration_path)}
    flags = {"research": False, "shadow": False}
    capture_config = {"enabled": False, "pollSeconds": 60, "maxCycleSeconds": 300,
                      "providerPath": str(root / "data/old_provider"),
                      "outputRoot": str(root / "data/market/a_share/intraday/a_stock_data_csi300"),
                      "calibrationEnabled": False, "calibrationDeclaration": "old.json"}
    backend = SimpleNamespace(
        PROJECT_ROOT=root, app=FastAPI(), _load_dotenv_dict=lambda: dict(values),
        _intraday_capture_config=lambda: dict(capture_config),
        _intraday_capture_runtime_binding=lambda: (object(), {}),
        _intraday_capture_effective_provider_path=lambda config, **kwargs: config["providerPath"],
        _research_shadow_auto_config=lambda: {"enabled": flags["research"]},
        _intraday_shadow_config=lambda: {"enabled": flags["shadow"]},
    )
    return SimpleNamespace(backend=backend, declaration=declaration, path=declaration_path,
                           calibration=calibration, calibration_path=calibration_path,
                           values=values, flags=flags, provider=provider,
                           output=Path(declaration["outputRoot"]))


class ShadowRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.time_patch = patch.object(recovery, "_now", return_value=NOW)
        self.time_patch.start()
        self.addCleanup(self.time_patch.stop)

    def test_no_opt_in_preserves_runtime_and_exposes_disabled_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = _fixture(tmp)
            f.values.clear()
            original = f.backend._intraday_capture_config
            shadow_original = f.backend._intraday_shadow_config
            self.assertIsNone(recovery.install_shadow_recovery(f.backend))
            self.assertIs(f.backend._intraday_capture_config, original)
            self.assertIs(f.backend._intraday_shadow_config, shadow_original)
            data = TestClient(f.backend.app).get("/api/v1/shadow-recovery/status").json()["data"]
            self.assertFalse(data["enabled"])
            self.assertFalse(data["accepted"])
            self.assertEqual(data["stage"], "DISABLED")

    def test_valid_opt_in_enables_only_independent_capture_and_calibration(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = _fixture(tmp)
            adapter = recovery.install_shadow_recovery(f.backend)
            config = f.backend._intraday_capture_config()
            self.assertTrue(config["enabled"])
            self.assertTrue(config["calibrationEnabled"])
            self.assertEqual(config["providerPath"], str(f.provider.resolve()))
            self.assertEqual(config["outputRoot"], str(f.output.resolve()))
            self.assertEqual(config["pollSeconds"], 60)
            self.assertFalse(f.backend._research_shadow_auto_config()["enabled"])
            self.assertFalse(f.backend._intraday_shadow_config()["enabled"])
            runner, contract = f.backend._intraday_capture_runtime_binding()
            self.assertIs(runner, recovery._capture_v4.run_capture_cycle)
            self.assertEqual(contract["providerClass"], "ResponseTimedRealtimeClient")
            self.assertEqual(f.backend._intraday_capture_effective_provider_path(config), str(f.provider.resolve()))
            data = adapter.status()
            self.assertEqual(data["stage"], "WAITING_CALIBRATION")
            self.assertFalse(data["accepted"])
            self.assertNotIn(str(tmp), json.dumps(data))
            self.assertFalse(f.output.exists())

    def test_active_trading_flag_is_clamped_and_disables_capture(self):
        for flag in ("research", "shadow"):
            with self.subTest(flag=flag), tempfile.TemporaryDirectory() as tmp:
                f = _fixture(tmp)
                f.flags[flag] = True
                adapter = recovery.install_shadow_recovery(f.backend)
                self.assertFalse(f.backend._research_shadow_auto_config()["enabled"])
                self.assertFalse(f.backend._intraday_shadow_config()["enabled"])
                self.assertFalse(f.backend._intraday_capture_config()["enabled"])
                self.assertEqual(adapter.status()["error"], "RECOVERY_REQUIRES_TRADING_WORKERS_DISABLED")
                runner, runtime = f.backend._intraday_capture_runtime_binding()
                self.assertFalse(runtime["enabled"])
                self.assertEqual(runner()["status"], "DISABLED")

    def test_provider_metadata_and_implementation_drift_disable_every_cycle(self):
        for drift in ("provider", "implementation"):
            with self.subTest(drift=drift), tempfile.TemporaryDirectory() as tmp:
                f = _fixture(tmp)
                recovery.install_shadow_recovery(f.backend)
                self.assertTrue(f.backend._intraday_capture_config()["enabled"])
                path = (f.provider / "calendars/day_future.txt" if drift == "provider"
                        else Path(tmp) / "frontend-v2/backend/local_app.py")
                path.write_text(path.read_text() + "\n")
                self.assertFalse(f.backend._intraday_capture_config()["enabled"])
                status = TestClient(f.backend.app).get("/api/v1/shadow-recovery/status")
                self.assertEqual(status.status_code, 200)
                self.assertFalse(status.json()["data"]["captureEnabled"])

    def test_unsafe_phase_dates_paths_or_hash_cannot_enable_capture(self):
        cases = [
            ("phase", "TRADING"), ("pollSeconds", 30),
            ("createdBeforeTradeDate", False),
            ("createdAt", "2026-10-26T08:00:00+08:00"),
            ("createdAt", "2026-10-23T10:30:00"),
            ("outputRoot", "/outside-project/recovery"),
            ("captureImplementationSha256", "0" * 64),
        ]
        for key, value in cases:
            with self.subTest(key=key, value=value), tempfile.TemporaryDirectory() as tmp:
                f = _fixture(tmp)
                f.declaration[key] = value
                _write_json(f.path, f.declaration)
                recovery.install_shadow_recovery(f.backend)
                self.assertFalse(f.backend._intraday_capture_config()["enabled"])

    def test_legacy_capture_or_provider_directories_are_refused(self):
        for kind in ("output", "provider"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as tmp:
                f = _fixture(tmp)
                if kind == "output":
                    f.declaration["outputRoot"] = str(Path(tmp) / "data/market/a_share/intraday/a_stock_data_csi300/child")
                else:
                    _write_json(Path(tmp) / "configs/intraday_shadow_old.json",
                                {"providerRoot": {"path": str(f.provider)}})
                _write_json(f.path, f.declaration)
                recovery.install_shadow_recovery(f.backend)
                self.assertFalse(f.backend._intraday_capture_config()["enabled"])

    def test_weakened_or_early_finalized_calibration_cannot_enable_capture(self):
        for change in ("quality", "time", "binding"):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as tmp:
                f = _fixture(tmp)
                if change == "quality":
                    f.calibration["calibration"]["qualityGates"]["minimumMedianCoreFieldCoverageRatio"] = 0.5
                elif change == "time":
                    f.calibration["calibration"]["finalizeAfter"] = "10:00:00"
                else:
                    f.calibration["captureContract"]["binding"] = {}
                _write_json(f.calibration_path, f.calibration)
                f.declaration["calibrationDeclarationSha256"] = recovery._sha(f.calibration_path)
                _write_json(f.path, f.declaration)
                recovery.install_shadow_recovery(f.backend)
                self.assertFalse(f.backend._intraday_capture_config()["enabled"])

    def test_reselected_or_resigned_declaration_requires_process_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = _fixture(tmp)
            recovery.install_shadow_recovery(f.backend)
            self.assertTrue(f.backend._intraday_capture_config()["enabled"])
            f.declaration["createdAt"] = "2026-10-23T10:31:00+08:00"
            _write_json(f.path, f.declaration)
            self.assertFalse(f.backend._intraday_capture_config()["enabled"])

    def test_effective_provider_cannot_follow_unrelated_first_slot(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = _fixture(tmp)
            f.backend._intraday_capture_effective_provider_path = lambda config, **kwargs: str(Path(tmp) / "other_provider")
            recovery.install_shadow_recovery(f.backend)
            config = f.backend._intraday_capture_config()
            with self.assertRaisesRegex(recovery.RecoveryError, "FIRST_SLOT_PROVIDER"):
                f.backend._intraday_capture_effective_provider_path(config)

    def test_verified_calibration_pass_is_only_waiting_for_a_future_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = _fixture(tmp)
            adapter = recovery.install_shadow_recovery(f.backend)
            self.assertTrue(f.backend._intraday_capture_config()["enabled"])
            report = {"schemaVersion": 1, "kind": "intraday_capture_calibration_report", "phase": "CALIBRATION_ONLY",
                      "tradeDate": "2026-10-26", "status": "PASS", "finalized": True,
                      "profitEvidenceEligible": False, "evaluatedAt": "2026-10-26T15:05:00+08:00",
                      "declaration": {"sha256": f.declaration["calibrationDeclarationSha256"]},
                      "qualityGates": dict(recovery._QUALITY_GATES),
                      "gateResults": {key: True for key in recovery._CALIBRATION_REPORT_GATES},
                      "observed": {"capturedExpectedSlotCount": 242},
                      "expected": {"binding": f.declaration["providerBinding"], "slotCount": 242}}
            digest = recovery._capture._sha256_bytes(recovery._capture._canonical_json_bytes(report))
            report["reportSha256"] = digest
            report_path = f.output / "audits/2026-10-26" / f"calibration_{digest[:12]}.json"
            _write_json(report_path, report)
            recovery._capture._write_status(f.output, {"version": 1, "status": "WAITING_MARKET",
                "calibration": {"status": "PASS", "finalized": True, "file": str(report_path)}})
            # A future-dated PASS cannot advance the phase before its actual close.
            self.assertEqual(adapter.status()["stage"], "CALIBRATION_REVIEW_REQUIRED")
            with patch.object(recovery, "_now", return_value=datetime(2026, 10, 26, 15, 6, tzinfo=NOW.tzinfo)):
                status = adapter.status()
            self.assertEqual(status["stage"], "WAIT_FORWARD_RUN")
            self.assertFalse(status["accepted"])
            self.assertFalse(status["autoShadowAccepted"])
            self.assertFalse(status["shadowEnabled"])


if __name__ == "__main__":
    unittest.main()
