"""Isolated V10 maintenance tests; official network primitives are fixture doubles."""
import copy
import importlib.util
import json
import os
from pathlib import Path
import shutil
import stat
import struct
import sys
import tempfile
import unittest
from datetime import datetime
from unittest.mock import patch

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "frontend-v2/backend"))
from market_data import v10_provider_maintenance as maintenance
from market_data.intraday_capture import _provider_context
from quantaalpha.utils.qlib_provider import calendar_bounds, provider_contract_fingerprint

spec = importlib.util.spec_from_file_location("maintenance_shadow_helpers", Path(__file__).with_name("test_intraday_shadow.py"))
helpers = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helpers)


class MaintenanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.provider = self.make_provider(self.root / "root-provider", helpers.BASE_DATES[:-1])
        self.raw = self.make_provider(self.root / "raw-provider", helpers.BASE_DATES)
        self.declaration = self.root / "declaration.json"
        self.declaration.write_text("fixture-qualified-by-validator-double")
        self.run_dir = self.root / "new-run-v10"
        self.validated = {"declaration": {"runId": "test_maintenance_v10",
            "databasePath": str(self.run_dir / "ledger.sqlite3"),
            "implementation": {"files": [{"path": str(path), "sha256": digest}
                for path, digest in maintenance._LOADED_FILE_SHA256.items()]},
            "providerRoot": {"path": str(self.provider),
                "fingerprint": provider_contract_fingerprint(self.provider, "csi300")["fingerprint"]}},
            "declarationSha256": "d" * 64, "providerContext": _provider_context(self.provider)}
        self.env = self.root / ".env"
        self.env.write_text(f"UNIT_TEST_PRIVATE_KEY=do-not-log-this\nQLIB_DATA_DIR={self.provider}\nQLIB_PROVIDER_URI={self.provider}\n")
        self.env.chmod(0o600)
        self.link = self.root / "qlib-link"
        self.link.symlink_to(self.provider, target_is_directory=True)
        self.addCleanup(patch.stopall)
        patch.dict(os.environ, {"QLIB_DATA_DIR": str(self.provider), "QLIB_PROVIDER_URI": str(self.provider)}).start()
        self.validate = patch.object(maintenance, "validate_v10_declaration", return_value=self.validated).start()
        self.discover = patch.object(maintenance, "discover_latest_provider", side_effect=self.effective_provider).start()
        self.manifest = {"release_tag": "2026-08-13", "target_trade_date": "2026-08-13",
                         "archive_size_bytes": 42, "archive_sha256": "sha256:" + "a" * 64}
        self.release_dir = self.root / "release"
        self.release_dir.mkdir()
        self.refresh_manifest()
        self.query = patch.object(maintenance, "query_official_release_manifest", side_effect=lambda _: dict(self.manifest)).start()
        self.download = patch.object(maintenance, "download_official_release", return_value=self.release_dir).start()
        self.install = patch.object(maintenance, "install_release_provider", side_effect=self.install_fixture).start()

    def make_provider(self, path, dates):
        helpers._write_provider(path, dates)
        for directory in (path / "features").iterdir():
            for field in ("high", "low", "vwap", "volume"):
                helpers._write_feature(directory / f"{field}.day.bin", [10. + i / 100 for i in range(len(dates))])
        return path

    def refresh_manifest(self):
        (self.release_dir / "qlib_bin.manifest.json").write_text(json.dumps(self.manifest))

    def install_fixture(self, source, candidate):
        if not candidate.exists():
            shutil.copytree(self.raw, candidate)
        return {"status": "INSTALLED"}

    def effective_provider(self):
        values = {}
        for line in self.env.read_text().splitlines():
            key, _, value = line.partition("=")
            values[key] = value
        paths = [os.environ[key] for key in ("QLIB_DATA_DIR", "QLIB_PROVIDER_URI") if os.environ.get(key)]
        paths.extend(values[key] for key in ("QLIB_DATA_DIR", "QLIB_PROVIDER_URI"))
        if self.link.exists():
            paths.append(str(self.link.resolve()))
        candidates = [(calendar_bounds(Path(path))[1], Path(path).resolve()) for path in paths
                      if calendar_bounds(Path(path))]
        return max(candidates, key=lambda item: item[0])[1]

    def cycle(self, **kwargs):
        args = {"enabled": True, "now": datetime.fromisoformat("2026-08-13T17:00:00+08:00"),
                "dotenv_path": self.env, "qlib_link": self.link}
        args.update(kwargs)
        return maintenance.run_cycle(self.declaration, **args)

    def runtime_bytes(self):
        return self.env.read_bytes(), os.readlink(self.link), {
            key: os.environ.get(key) for key in ("QLIB_DATA_DIR", "QLIB_PROVIDER_URI")}

    def test_disabled_and_unqualified_have_no_side_effects(self):
        before = self.runtime_bytes()
        self.assertEqual(self.cycle(enabled=False)["status"], "DISABLED")
        self.validate.assert_not_called()
        self.query.assert_not_called()
        self.assertFalse(self.run_dir.exists())
        self.validate.side_effect = ValueError("do-not-log-this")
        result = self.cycle()
        self.assertEqual(result["status"], "BLOCKED")
        self.assertEqual(result["errorCode"], "DECLARATION_NOT_QUALIFIED")
        self.assertNotIn("do-not-log-this", json.dumps(result))
        self.assertEqual(self.runtime_bytes(), before)
        self.assertFalse(self.run_dir.exists())

    def test_explicit_bootstrap_selects_only_qualified_root_from_old_global_provider(self):
        legacy = self.make_provider(self.root / "legacy-provider", helpers.BASE_DATES[:-2])
        protected = legacy / "legacy-ledger.sqlite3"
        protected.write_bytes(b"protected old ledger")
        before = protected.read_bytes()
        self.env.write_text(f"UNIT_TEST_PRIVATE_KEY=do-not-log-this\nQLIB_DATA_DIR={legacy}\nQLIB_PROVIDER_URI={legacy}\n")
        for key in ("QLIB_DATA_DIR", "QLIB_PROVIDER_URI"):
            os.environ[key] = str(legacy)
        self.link.unlink()
        self.link.symlink_to(legacy)
        self.assertEqual(self.effective_provider(), legacy)
        result = maintenance.bootstrap_root_provider(self.declaration, enabled=True,
            dotenv_path=self.env, qlib_link=self.link)
        self.assertEqual(result["status"], "ROOT_SELECTED")
        self.assertEqual(maintenance.resolve_current_provider(self.declaration), self.provider)
        self.assertEqual(self.effective_provider(), self.provider)
        self.assertEqual(protected.read_bytes(), before)
        self.query.assert_not_called()
        self.install.assert_not_called()

    def test_bootstrap_is_opt_in_and_never_resets_an_established_successor(self):
        before = self.runtime_bytes()
        self.assertEqual(maintenance.bootstrap_root_provider(self.declaration)["status"], "DISABLED")
        self.assertFalse(self.run_dir.exists())
        self.assertEqual(self.runtime_bytes(), before)
        advanced = self.cycle()
        self.assertEqual(advanced["status"], "ADVANCED")
        before = self.runtime_bytes()
        result = maintenance.bootstrap_root_provider(self.declaration, enabled=True,
            dotenv_path=self.env, qlib_link=self.link)
        self.assertEqual(result["status"], "ALREADY_INITIALIZED")
        self.assertEqual(result["currentProviderPath"], advanced["currentProviderPath"])
        self.assertEqual(self.runtime_bytes(), before)

    def test_bootstrap_unqualified_has_no_side_effects(self):
        self.validate.side_effect = ValueError("do-not-log-this")
        before = self.runtime_bytes()
        result = maintenance.bootstrap_root_provider(self.declaration, enabled=True,
            dotenv_path=self.env, qlib_link=self.link)
        self.assertEqual(result["status"], "BLOCKED")
        self.assertNotIn("do-not-log-this", json.dumps(result))
        self.assertEqual(self.runtime_bytes(), before)
        self.assertFalse(self.run_dir.exists())

    def test_resolve_without_pointer_is_read_only_and_requires_actual_root(self):
        self.assertEqual(maintenance.resolve_current_provider(self.declaration), self.provider)
        self.assertFalse(self.run_dir.exists())
        self.discover.side_effect = None
        self.discover.return_value = self.raw
        with self.assertRaisesRegex(maintenance.MaintenanceError, "RUNTIME_PROVIDER_MISMATCH"):
            maintenance.resolve_current_provider(self.declaration)
        self.assertFalse(self.run_dir.exists())

    def test_official_append_is_bound_to_new_run_and_all_runtime_targets(self):
        with patch.object(maintenance, "verify_append_only_provider", wraps=maintenance.verify_append_only_provider) as verify:
            result = self.cycle()
        self.assertEqual(result["status"], "ADVANCED")
        self.assertTrue(result["runtimeConsistent"])
        self.assertFalse(result["autoShadowAccepted"])
        self.assertEqual(len(verify.call_args_list), 2)
        self.assertTrue(all(call.kwargs["fields"] == maintenance.FIELDS for call in verify.call_args_list))
        candidate = Path(result["currentProviderPath"])
        self.assertTrue(candidate.is_relative_to(self.run_dir / "provider_maintenance/candidates"))
        self.assertEqual(self.effective_provider(), candidate)
        self.assertEqual(self.link.resolve(), candidate)
        self.assertEqual(stat.S_IMODE(self.env.stat().st_mode), 0o600)
        self.assertIn("UNIT_TEST_PRIVATE_KEY=do-not-log-this\n", self.env.read_text())
        self.assertEqual(maintenance.resolve_current_provider(self.declaration), candidate)
        status = maintenance.read_status(self.declaration)
        self.assertEqual(status["status"], "ADVANCED")
        self.assertNotIn("do-not-log-this", json.dumps(status))
        pointer = maintenance._read(self.run_dir / "provider_maintenance/accepted_provider.json")
        self.assertEqual(len(pointer["proofs"]), 2)
        self.assertFalse((self.run_dir / "ledger.sqlite3").exists())

    def test_no_new_session_fresh_checks_and_does_not_download(self):
        self.manifest.update(release_tag="2026-08-12", target_trade_date="2026-08-12")
        before = self.runtime_bytes()
        result = self.cycle()
        self.assertEqual(result["status"], "NO_NEW_SESSION")
        self.download.assert_not_called()
        self.assertEqual(self.runtime_bytes(), before)
        helpers._write_feature(self.provider / "features/sh600000/close.day.bin", [11.] * 6 + [10.06])
        blocked = self.cycle()
        self.assertEqual(blocked["status"], "BLOCKED")
        self.assertEqual(blocked["errorCode"], "ROOT_FEATURE_TREE_DRIFT")

    def test_same_date_no_new_session_synchronizes_both_process_and_dotenv_keys(self):
        self.manifest.update(release_tag="2026-08-12", target_trade_date="2026-08-12")
        os.environ.pop("QLIB_PROVIDER_URI")
        self.env.chmod(0o644)
        result = self.cycle()
        self.assertEqual(result["status"], "NO_NEW_SESSION")
        self.assertEqual(os.environ["QLIB_PROVIDER_URI"], str(self.provider))
        self.assertEqual(stat.S_IMODE(self.env.stat().st_mode), 0o600)
        self.assertEqual(maintenance.resolve_current_provider(self.declaration), self.provider)

    def test_future_official_session_is_rejected_without_download_or_switch(self):
        self.manifest.update(release_tag="2026-08-14", target_trade_date="2026-08-14")
        before = self.runtime_bytes()
        result = self.cycle()
        self.assertEqual(result["errorCode"], "OFFICIAL_TARGET_IS_FUTURE")
        self.download.assert_not_called()
        self.assertEqual(self.runtime_bytes(), before)

    def test_exact_universe_change_is_blocked_without_switch(self):
        text = (self.raw / "instruments/csi300.txt").read_text()
        (self.raw / "instruments/csi300.txt").write_text(text.replace("SH600000", "SH601059"))
        before = self.runtime_bytes()
        result = self.cycle()
        self.assertEqual(result["status"], "BLOCKED")
        self.assertEqual(result["errorCode"], "FROZEN_UNIVERSE_MISMATCH")
        self.assertEqual(self.runtime_bytes(), before)

    def test_every_history_field_is_checked_and_drift_never_switches(self):
        for field in maintenance.FIELDS:
            with self.subTest(field=field):
                candidate = self.run_dir / "provider_maintenance/candidates/2026-08-13"
                if candidate.exists():
                    shutil.rmtree(candidate)
                original = (self.raw / f"features/sh600000/{field}.day.bin").read_bytes()
                # Keep the latest bar valid: this specifically exercises the
                # historical prefix gate, independently of daily completeness.
                changed = bytearray(original)
                struct.pack_into("<f", changed, 4, 99.)
                (self.raw / f"features/sh600000/{field}.day.bin").write_bytes(changed)
                before = self.runtime_bytes()
                result = self.cycle()
                self.assertEqual(result["status"], "BLOCKED")
                self.assertEqual(self.runtime_bytes(), before)
                (self.raw / f"features/sh600000/{field}.day.bin").write_bytes(original)

    def test_previous_successor_history_is_checked_beyond_root_prefix(self):
        first = self.cycle()
        self.assertEqual(first["status"], "ADVANCED")
        self.raw = self.make_provider(self.root / "raw-next", (*helpers.BASE_DATES, "2026-08-14"))
        values = [10. + i / 100 for i in range(9)]
        values[7] += 1.
        helpers._write_feature(self.raw / "features/sh600000/close.day.bin", values)
        self.manifest.update(release_tag="2026-08-14", target_trade_date="2026-08-14")
        self.refresh_manifest()
        before = self.runtime_bytes()
        result = self.cycle(now=datetime.fromisoformat("2026-08-14T17:00:00+08:00"))
        self.assertEqual(result["status"], "BLOCKED")
        self.assertEqual(self.runtime_bytes(), before)

    def test_accepted_successor_bytes_and_proof_are_rechecked(self):
        result = self.cycle()
        candidate = Path(result["currentProviderPath"])
        helpers._write_feature(candidate / "features/sh600000/vwap.day.bin", [77.] * 8)
        with self.assertRaisesRegex(maintenance.MaintenanceError, "ACCEPTED_PROVIDER_DRIFT"):
            maintenance.resolve_current_provider(self.declaration)
        self.assertFalse(maintenance.read_status(self.declaration)["runtimeConsistent"])

    def test_symlink_failure_rolls_back_env_and_process_without_touching_secret(self):
        before = self.runtime_bytes()
        original = maintenance._replace_link
        def reject_candidate(path, target):
            if "candidates" in target:
                raise OSError("do-not-log-this")
            return original(path, target)
        with patch.object(maintenance, "_replace_link", side_effect=reject_candidate):
            result = self.cycle()
        self.assertEqual(result["errorCode"], "ACTIVATION_FAILED")
        self.assertEqual(self.runtime_bytes(), before)
        self.assertNotIn("do-not-log-this", json.dumps(result))
        self.assertFalse((self.run_dir / "provider_maintenance/accepted_provider.json").exists())

    def test_actual_discovery_readback_mismatch_rolls_back(self):
        before = self.runtime_bytes()
        self.discover.side_effect = None
        self.discover.return_value = self.provider
        result = self.cycle()
        self.assertEqual(result["errorCode"], "ACTIVATION_FAILED")
        self.assertEqual(self.runtime_bytes(), before)

    def test_real_directory_qlib_target_is_never_replaced(self):
        self.link.unlink()
        self.link.mkdir()
        (self.link / "protected").write_text("preserve")
        before = self.env.read_bytes()
        result = self.cycle()
        self.assertEqual(result["errorCode"], "ACTIVATION_TARGET_UNSAFE")
        self.assertEqual(self.env.read_bytes(), before)
        self.assertEqual((self.link / "protected").read_text(), "preserve")

    def test_shared_runtime_lock_is_owned_and_has_no_database_side_effect(self):
        with maintenance.runtime_lock(self.declaration) as validated:
            self.assertEqual(validated, self.validated)
            self.assertTrue((self.run_dir / "provider_maintenance/runtime.lock").exists())
            self.assertEqual(maintenance.resolve_current_provider(self.declaration), self.provider)
        self.assertFalse((self.run_dir / "ledger.sqlite3").exists())

    def test_partial_activation_journal_is_fail_closed_on_restart(self):
        with maintenance.runtime_lock(self.declaration):
            maintenance._write(self.run_dir / "provider_maintenance/activation.json", {
                "runId": self.validated["declaration"]["runId"],
                "declarationSha256": self.validated["declarationSha256"], "phase": "PREPARED"})
        before = self.runtime_bytes()
        result = self.cycle()
        self.assertEqual(result["errorCode"], "ACTIVATION_RECONCILIATION_REQUIRED")
        self.assertEqual(self.runtime_bytes(), before)
        self.query.assert_not_called()

    def test_latest_missing_benchmark_or_nan_stock_cannot_be_accepted(self):
        mutations = [
            ("sh000300", "close", [10. + i / 100 for i in range(7)]),
            ("sh000300", "open", [10. + i / 100 for i in range(7)] + [float("nan")]),
            ("sh600000", "factor", [1.] * 7 + [float("nan")]),
            ("sh600000", "open", [10. + i / 100 for i in range(7)] + [20.]),
            ("sh600000", "volume", [10. + i / 100 for i in range(7)] + [-1.]),
        ]
        for code, field, values in mutations:
            with self.subTest(code=code, field=field):
                candidate = self.run_dir / "provider_maintenance/candidates/2026-08-13"
                if candidate.exists():
                    shutil.rmtree(candidate)
                path = self.raw / f"features/{code}/{field}.day.bin"
                original = path.read_bytes()
                helpers._write_feature(path, values)
                before = self.runtime_bytes()
                result = self.cycle()
                self.assertEqual(result["errorCode"], "DAILY_SESSION_INCOMPLETE")
                self.assertEqual(self.runtime_bytes(), before)
                self.assertFalse((self.run_dir / "provider_maintenance/accepted_provider.json").exists())
                path.write_bytes(original)

    def test_bootstrap_rejects_incomplete_root_latest_benchmark(self):
        helpers._write_feature(self.provider / "features/sh000300/close.day.bin", [10.] * 6 + [float("nan")])
        before = self.runtime_bytes()
        result = maintenance.bootstrap_root_provider(self.declaration, enabled=True,
            dotenv_path=self.env, qlib_link=self.link)
        self.assertEqual(result["errorCode"], "DAILY_SESSION_INCOMPLETE")
        self.assertEqual(self.runtime_bytes(), before)
        self.assertFalse((self.run_dir / "provider_maintenance/accepted_provider.json").exists())

    def test_loaded_code_pin_rejects_new_declaration_without_writing(self):
        own = Path(maintenance.__file__).resolve()
        for item in self.validated["declaration"]["implementation"]["files"]:
            if Path(item["path"]) == own:
                item["sha256"] = "f" * 64
        before = self.runtime_bytes()
        result = self.cycle()
        self.assertEqual(result["errorCode"], "LOADED_IMPLEMENTATION_DRIFT")
        self.assertFalse(self.run_dir.exists())
        self.assertEqual(self.runtime_bytes(), before)

    def test_stop_hook_before_start_has_no_side_effects(self):
        hook = unittest.mock.Mock(return_value=False)
        self.assertEqual(self.cycle(enabled=False, should_continue=hook)["status"], "DISABLED")
        hook.assert_not_called()
        before = self.runtime_bytes()
        result = self.cycle(should_continue=hook)
        self.assertEqual(result["errorCode"], "RUNTIME_STOPPED_OR_CHANGED")
        self.assertFalse(self.run_dir.exists())
        self.assertEqual(self.runtime_bytes(), before)

    def test_stop_hook_after_download_prevents_activation(self):
        alive = {"value": True}
        def download(*args):
            alive["value"] = False
            return self.release_dir
        self.download.side_effect = download
        before = self.runtime_bytes()
        result = self.cycle(should_continue=lambda: alive["value"])
        self.assertEqual(result["errorCode"], "RUNTIME_STOPPED_OR_CHANGED")
        self.assertEqual(self.runtime_bytes(), before)
        self.assertFalse((self.run_dir / "provider_maintenance/accepted_provider.json").exists())

    def test_stop_hook_before_pointer_commit_rolls_back_switch(self):
        before = self.runtime_bytes()
        result = self.cycle(should_continue=lambda: "candidates" not in os.environ.get("QLIB_DATA_DIR", ""))
        self.assertEqual(result["errorCode"], "ACTIVATION_FAILED")
        self.assertEqual(self.runtime_bytes(), before)
        self.assertFalse((self.run_dir / "provider_maintenance/accepted_provider.json").exists())


if __name__ == "__main__":
    unittest.main()
