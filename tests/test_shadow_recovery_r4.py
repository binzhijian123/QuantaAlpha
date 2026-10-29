"""R4 binding tests use owned temporary files and never start a real worker."""
from contextlib import redirect_stderr
from datetime import datetime
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import uuid
from zoneinfo import ZoneInfo

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from scripts import freeze_shadow_recovery_r4 as freeze
from scripts import freeze_shadow_recovery as original
import shadow_recovery as legacy
import shadow_recovery_r4 as recovery
from market_data import slot_aligned_capture_runtime as scheduler

ROOT = freeze.PROJECT_ROOT
NOW = datetime(2026, 10, 29, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai"))


class R4Tests(unittest.TestCase):
    def setUp(self):
        (ROOT / ".codex_backups").mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="r4_unit_", dir=ROOT / ".codex_backups")
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name).resolve()
        self.provider = self.folder / "provider"
        (self.provider / "calendars").mkdir(parents=True)
        (self.provider / "instruments").mkdir()
        (self.provider / "calendars/day.txt").write_text("2026-10-28\n2026-10-29\n")
        (self.provider / "calendars/day_future.txt").write_text("2026-10-28\n2026-10-29\n2026-10-30\n2026-11-02\n")
        (self.provider / "instruments/csi300.txt").write_text("".join(
            f"SH{600000+i}\t2020-01-01\t2026-12-31\n" for i in range(300)))
        self.output = ROOT / "data/market/a_share/intraday" / ("r4_unit_" + uuid.uuid4().hex)
        self.output.parent.mkdir(parents=True, exist_ok=True)
        self.declaration = self.folder / "recovery_r4.json"
        self.calibration = self.folder / "calibration_r4.json"
        self.time = patch.object(legacy, "_now", return_value=NOW)
        self.time.start()
        self.addCleanup(self.time.stop)

    def do_freeze(self, **kwargs):
        args = dict(provider=self.provider, trade_date="2026-10-30", output=self.output,
                    declaration=self.declaration, calibration_declaration=self.calibration,
                    project_root=ROOT, now=NOW)
        args.update(kwargs)
        return freeze.freeze_shadow_recovery_r4(**args)

    def targets(self):
        return [self.declaration, self.calibration,
                self.declaration.with_suffix(".json.sha256"), self.calibration.with_suffix(".json.sha256")]

    def backend(self):
        backend = SimpleNamespace(
            PROJECT_ROOT=ROOT, app=FastAPI(), _find_factor_jsons=lambda: [],
            _load_dotenv_dict=lambda: {legacy.DECLARATION_KEY: "unused-old-r3-selector"},
            _intraday_capture_config=lambda: {"enabled": False, "pollSeconds": 60,
                "outputRoot": str(ROOT / "data/market/a_share/intraday/a_stock_data_csi300")},
            _intraday_capture_runtime_binding=lambda: (None, {}),
            _intraday_capture_effective_provider_path=lambda config, **kwargs: config["providerPath"],
            _research_shadow_auto_config=lambda: {"enabled": False},
            _intraday_shadow_config=lambda: {"enabled": False},
            _runtime_readiness_payload=lambda **kwargs: {"ready": True, "status": "healthy", "configError": None},
            _intraday_capture_loop=lambda: None,
        )
        @backend.app.get("/api/health/ready")
        async def ready():
            payload = backend._runtime_readiness_payload()
            return JSONResponse(status_code=200 if payload["ready"] else 503, content=payload)
        return backend

    def install(self, backend=None):
        backend = backend or self.backend()
        backend._recovery_r4_entrypoint_sha = legacy._sha(ROOT / "frontend-v2/backend/recovery_r4_app.py")
        adapter = recovery.install_shadow_recovery_r4(backend)
        scheduler.install_slot_aligned_capture_runtime(backend)
        return backend, adapter

    def rewrite(self, path, payload):
        path.chmod(0o600)
        path.write_text(json.dumps(payload, sort_keys=True))
        sidecar = path.with_suffix(path.suffix + ".sha256")
        sidecar.chmod(0o600)
        sidecar.write_text(f"{legacy._sha(path)}  {path.name}\n")

    def test_real_scheduler_paths_deduplicate_to_relative_bound_files(self):
        before_files = original._IMPLEMENTATION_FILES
        before_encoder = original._encoded
        result = self.do_freeze()
        payload = json.loads(self.declaration.read_bytes())
        calibration = json.loads(self.calibration.read_bytes())
        self.assertEqual(payload["schemaVersion"], 4)
        self.assertEqual(payload["runtimeEntrypoint"], "recovery_r4_app")
        self.assertEqual(payload["schedulerImplementationSha256"], scheduler.capture_scheduler_implementation_contract()["sha256"])
        self.assertEqual(calibration["schedulerImplementationSha256"], payload["schedulerImplementationSha256"])
        paths = [row["path"] for row in payload["recoveryImplementation"]["files"]]
        self.assertEqual(len(paths), len(set(paths)))
        self.assertTrue(all(not Path(name).is_absolute() for name in paths))
        for item in scheduler.capture_scheduler_implementation_contract()["files"]:
            self.assertTrue(Path(item["path"]).is_absolute())  # actual contract, not a path mock
            self.assertIn(str(Path(item["path"]).relative_to(ROOT)), paths)
        self.assertEqual(original._IMPLEMENTATION_FILES, before_files)
        self.assertIs(original._encoded, before_encoder)
        self.assertFalse(self.output.exists())
        self.assertFalse(result["environmentModified"])
        for path in self.targets():
            self.assertEqual(path.stat().st_mode & 0o777, 0o444)
        self.assertEqual(calibration["calibration"]["qualityGates"], original._GATES)
        self.assertEqual(calibration["calibration"]["expectedSlotCount"], 242)

    def test_old_adapter_rejects_schema4_even_when_explicitly_selected(self):
        self.do_freeze()
        backend = self.backend()
        backend._load_dotenv_dict = lambda: {legacy.DECLARATION_KEY: str(self.declaration)}
        adapter = legacy.ShadowRecovery(backend, str(self.declaration))
        self.assertIsNone(adapter.validate())
        self.assertEqual(adapter.error, "RECOVERY_DECLARATION_CONTRACT_INVALID")

    def test_r4_adapter_requires_explicit_selector_and_installed_new_loop(self):
        self.do_freeze()
        with patch.dict(os.environ, {recovery.DECLARATION_KEY: str(self.declaration)}):
            backend = self.backend()
            backend._recovery_r4_entrypoint_sha = legacy._sha(ROOT / "frontend-v2/backend/recovery_r4_app.py")
            adapter = recovery.install_shadow_recovery_r4(backend)
            self.assertIsNone(adapter.validate())
            self.assertEqual(adapter.error, "RECOVERY_R4_SCHEDULER_NOT_INSTALLED")
            scheduler.install_slot_aligned_capture_runtime(backend)
            self.assertIsNotNone(adapter.validate(), adapter.error)
            state = adapter.status()
            self.assertEqual(state["tradeDate"], "2026-10-30")
            self.assertEqual(state["stage"], "WAITING_CALIBRATION")
            self.assertFalse(state["captureScheduledToday"])
            self.assertFalse(state["accepted"])
            self.assertFalse(state["shadowEnabled"])
            self.assertFalse(state["researchEnabled"])
        self.assertIsNone(adapter.validate())
        self.assertEqual(adapter.error, "RECOVERY_R4_EXPLICIT_SELECTOR_REQUIRED")

    def test_future_binding_does_not_dispatch_or_create_capture_directory(self):
        self.do_freeze()
        with patch.dict(os.environ, {recovery.DECLARATION_KEY: str(self.declaration)}):
            backend, adapter = self.install()
            capture, _ = adapter.runtime_binding()
            with patch.object(legacy._capture_v4, "run_capture_cycle") as actual:
                result = capture(self.provider, self.output, poll_seconds=60)
                self.assertEqual(result["status"], "WAITING_CALIBRATION_DATE")
                actual.assert_not_called()
            self.assertEqual(backend._intraday_capture_config()["recoveryTradeDate"], "2026-10-30")
        self.assertFalse(self.output.exists())

    def test_only_target_real_day_accepts_plain_v4_call_and_rejects_overrides(self):
        self.do_freeze()
        target_now = NOW.replace(day=30)
        with patch.dict(os.environ, {recovery.DECLARATION_KEY: str(self.declaration)}), \
             patch.object(legacy, "_now", return_value=target_now):
            _, adapter = self.install()
            capture, _ = adapter.runtime_binding()
            for options in ({"now": target_now}, {"clock": lambda: target_now}, {"client": object()},
                            {"force": True}, {"poll_seconds": 30}, {"poll_seconds": True}):
                with self.subTest(options=list(options)):
                    with self.assertRaises(legacy.RecoveryError):
                        capture(self.provider, self.output, **options)
            with patch.object(legacy._capture_v4, "run_capture_cycle", return_value={"status": "CAPTURED"}) as actual:
                self.assertEqual(capture(self.provider, self.output, poll_seconds=60)["status"], "CAPTURED")
                actual.assert_called_once_with(self.provider, self.output, poll_seconds=60)
        self.assertFalse(self.output.exists())

    def test_r3_declaration_in_new_adapter_fails_without_fallback(self):
        original.freeze_shadow_recovery(self.provider, "2026-10-30", self.output,
            self.declaration, self.calibration, now=NOW)
        with patch.dict(os.environ, {recovery.DECLARATION_KEY: str(self.declaration)}):
            backend, adapter = self.install()
            self.assertIsNone(adapter.validate())
            self.assertEqual(adapter.error, "RECOVERY_R4_CONTRACT_INVALID")
            self.assertFalse(backend._intraday_capture_config()["enabled"])
        self.assertFalse(self.output.exists())

    def test_entry_exposes_invalid_status_and_never_uses_dotenv_r3_selector(self):
        backend = self.backend()
        # The real SettingsStore reader still has the old .env selection. No new
        # process selector is supplied; this must remain invalid and read-only.
        real_reader = backend._load_dotenv_dict
        def fake_settings(module):
            module._load_dotenv_dict = real_reader
            return object()
        with patch.dict(os.environ, {recovery.DECLARATION_KEY: ""}), \
             patch.dict("sys.modules", {"app": backend}), \
             patch("system_settings.install_settings_routes", side_effect=fake_settings):
            spec = importlib.util.spec_from_file_location("r4_entry_test", ROOT / "frontend-v2/backend/recovery_r4_app.py")
            entry = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(entry)
            with TestClient(backend.app) as client:
                status = client.get("/api/v1/shadow-recovery/status").json()["data"]
            self.assertEqual(status["error"], "RECOVERY_R4_EXPLICIT_SELECTOR_REQUIRED")
            self.assertFalse(status["captureEnabled"])
            self.assertFalse(backend._runtime_readiness_payload()["ready"])
            self.assertIsNotNone(entry.scheduler)

    def test_entry_valid_future_binding_and_changed_loaded_entry_is_rejected(self):
        self.do_freeze()
        backend = self.backend()
        with patch.dict(os.environ, {recovery.DECLARATION_KEY: str(self.declaration)}), \
             patch.dict("sys.modules", {"app": backend}), \
             patch("system_settings.install_settings_routes", return_value=object()):
            spec = importlib.util.spec_from_file_location("r4_entry_valid_test", ROOT / "frontend-v2/backend/recovery_r4_app.py")
            entry = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(entry)
            self.assertTrue(backend._runtime_readiness_payload()["ready"])
            self.assertEqual(entry.recovery.status()["tradeDate"], "2026-10-30")
            backend._recovery_r4_entrypoint_sha = "0" * 64
            self.assertFalse(backend._runtime_readiness_payload()["ready"])
            self.assertFalse(backend._intraday_capture_config()["enabled"])

    def test_clock_invalid_missing_or_unreadable_scheduler_is_http503(self):
        self.do_freeze()
        backend = self.backend()
        with patch.dict(os.environ, {recovery.DECLARATION_KEY: str(self.declaration)}), \
             patch.dict("sys.modules", {"app": backend}), \
             patch("system_settings.install_settings_routes", return_value=object()):
            spec = importlib.util.spec_from_file_location("r4_entry_health_test", ROOT / "frontend-v2/backend/recovery_r4_app.py")
            entry = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(entry)
            with TestClient(backend.app) as client:
                self.assertEqual(client.get("/api/health/ready").status_code, 200)
                entry.scheduler.phase = "CLOCK_INVALID"
                entry.scheduler.clock_fault = True
                result = client.get("/api/health/ready")
                self.assertEqual(result.status_code, 503)
                self.assertFalse(result.json()["ready"])
                self.assertEqual(result.json()["recoveryR4"]["schedulerPhase"], "CLOCK_INVALID")
                entry.scheduler.phase = "CYCLE_ERROR"
                entry.scheduler.clock_fault = False
                self.assertEqual(client.get("/api/health/ready").status_code, 200)
                with patch.object(entry.scheduler, "status", side_effect=RuntimeError("PRIVATE_TOKEN=secret")):
                    result = client.get("/api/health/ready")
                    self.assertEqual(result.status_code, 503)
                    self.assertNotIn("PRIVATE_TOKEN", result.text)
                backend._slot_aligned_capture_runtime = None
                self.assertEqual(client.get("/api/health/ready").status_code, 503)

    def test_missing_new_pin_bad_sidecar_and_trader_enabled_fail_closed(self):
        self.do_freeze()
        original_payload = json.loads(self.declaration.read_bytes())
        missing = {**original_payload, "recoveryImplementation": {"files": [
            row for row in original_payload["recoveryImplementation"]["files"]
            if row["path"] != "frontend-v2/backend/recovery_r4_app.py"]}}
        self.rewrite(self.declaration, missing)
        with patch.dict(os.environ, {recovery.DECLARATION_KEY: str(self.declaration)}):
            backend, adapter = self.install()
            self.assertIsNone(adapter.validate())
            self.assertEqual(adapter.error, "RECOVERY_IMPLEMENTATION_INCOMPLETE")
            self.assertFalse(backend._intraday_capture_config()["enabled"])
        self.rewrite(self.declaration, original_payload)
        sidecar = self.declaration.with_suffix(".json.sha256")
        sidecar.write_text("wrong hash\n")
        with patch.dict(os.environ, {recovery.DECLARATION_KEY: str(self.declaration)}):
            _, adapter = self.install()
            self.assertIsNone(adapter.validate())
            self.assertEqual(adapter.error, "RECOVERY_DECLARATION_HASH_INVALID")
        sidecar.write_text(f"{legacy._sha(self.declaration)}  {self.declaration.name}\n")
        with patch.dict(os.environ, {recovery.DECLARATION_KEY: str(self.declaration)}):
            backend = self.backend()
            backend._research_shadow_auto_config = lambda: {"enabled": True}
            backend, adapter = self.install(backend)
            self.assertIsNone(adapter.validate())
            self.assertEqual(adapter.error, "RECOVERY_REQUIRES_TRADING_WORKERS_DISABLED")
            self.assertFalse(backend._research_shadow_auto_config()["enabled"])
            self.assertFalse(backend._intraday_shadow_config()["enabled"])
        self.assertFalse(self.output.exists())

    def test_freezer_rejects_today_existing_output_watch_and_ledger_paths(self):
        self.output.mkdir()
        try:
            with self.assertRaises(ValueError):
                self.do_freeze()
        finally:
            self.output.rmdir()
        for updates in ({"trade_date": "2026-10-29"},
                        {"output": ROOT / "data/market/a_share/intraday/v4_qualification_watch/new"},
                        {"output": ROOT / "data/trading/new_r4"}):
            with self.subTest(updates=list(updates)):
                with self.assertRaises(ValueError):
                    self.do_freeze(**updates)
                self.assertTrue(all(not path.exists() for path in self.targets()))

    def test_partial_exclusive_write_failure_cleans_only_owned_files(self):
        sentinel = self.folder / "sentinel"
        sentinel.write_bytes(b"keep")
        open_actual = os.open
        count = 0
        def fail_third(path, flags, mode=0o777):
            nonlocal count
            if flags & os.O_EXCL and flags & os.O_CREAT:
                count += 1
                if count == 3:
                    raise OSError("private error")
            return open_actual(path, flags, mode)
        with patch.object(freeze.os, "open", side_effect=fail_third):
            with self.assertRaises(OSError):
                self.do_freeze()
        self.assertTrue(all(not path.exists() for path in self.targets()))
        self.assertEqual(sentinel.read_bytes(), b"keep")

    def test_postfreeze_failure_preserves_foreign_replacement_inode(self):
        original_contract = recovery.scheduler_contract()
        calls = 0
        def contract():
            nonlocal calls
            calls += 1
            if calls == 2:
                replacement = self.folder / "foreign"
                replacement.write_bytes(b"foreign owned replacement")
                os.replace(replacement, self.calibration)
                return {**original_contract, "sha256": "f" * 64}
            return original_contract
        with patch.object(freeze, "scheduler_contract", side_effect=contract):
            with self.assertRaises(ValueError):
                self.do_freeze()
        self.assertEqual(self.calibration.read_bytes(), b"foreign owned replacement")
        self.assertTrue(all(not path.exists() for path in self.targets() if path != self.calibration))

    def test_freezer_cli_has_no_clock_override_and_fixed_error_output(self):
        error = io.StringIO()
        with redirect_stderr(error):
            code = freeze.main(["--now", "PRIVATE_TOKEN=never-print"])
        self.assertEqual(code, 1)
        self.assertNotIn("PRIVATE_TOKEN", error.getvalue())
        self.assertTrue(all(not path.exists() for path in self.targets()))


if __name__ == "__main__":
    unittest.main()
