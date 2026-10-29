"""Freeze transaction tests use isolated files and explicit validator seams."""
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace

BACKEND = Path(__file__).resolve().parents[1] / "frontend-v2/backend"
sys.path.insert(0, str(BACKEND))
from scripts import freeze_intraday_v10 as freeze


class FreezeV10Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.provider = self.root / "provider"
        self.provider.mkdir()
        self.source = self.root / "source.json"
        self.calib = self.root / "calibration.json"
        for path in (self.source, self.calib):
            path.write_text('{"reportSha256":"test"}')
        self.path = self.root / "configs/intraday_shadow_test_v10.json"
        self.database = self.root / "ledger-v10/ledger.sqlite3"
        self.capture = self.root / "capture-v10"
        for relative in freeze.RUNTIME_FILES | freeze.v10.REQUIRED_IMPLEMENTATION_FILES:
            path = self.root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("# isolated fixture\n")
        self.environment = self.root / ".env"
        self.environment.write_text("DONT_CHANGE=true\n")
        report_reader = lambda path: json.loads(Path(path).read_text())
        self.patches = [
            patch.object(freeze.v10.capture_v4, "capture_implementation_contract",
                         return_value={"sha256": "capture", "files": []}),
            patch.object(freeze, "provider_contract_fingerprint", return_value={"fingerprint": "provider"}),
            patch.object(freeze.v10.calibration, "read_verified_calibration_report", side_effect=report_reader),
            patch.object(freeze.v10.qualification, "read_verified_source_qualification_report", side_effect=report_reader),
        ]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)

    def run_freeze(self, **overrides):
        args = dict(provider=self.provider, qualification=self.source, calibration=self.calib,
                    first_signal_date="2026-10-27", run_id="test_v10", capture_root=self.capture,
                    database=self.database, declaration=self.path, project_root=self.root,
                    now=datetime.fromisoformat("2026-10-23T12:00:00+08:00"))
        args.update(overrides)
        return freeze.freeze_intraday_v10(**args)

    @staticmethod
    def qualified(path):
        return {"declarationSha256": hashlib.sha256(Path(path).read_bytes()).hexdigest()}

    def assert_no_outputs(self):
        self.assertFalse(self.path.exists())
        self.assertFalse(Path(str(self.path) + ".sha256").exists())
        self.assertFalse(self.capture.exists())
        self.assertFalse(self.database.parent.exists())
        self.assertEqual(self.environment.read_text(), "DONT_CHANGE=true\n")

    def test_fixed_runtime_bound_and_no_workers_environment_or_ledger(self):
        with patch.object(freeze.v10, "validate_v10_declaration", side_effect=self.qualified) as validator:
            result = self.run_freeze()
        validator.assert_called_once_with(self.path)
        payload = json.loads(self.path.read_text())
        self.assertEqual(result["status"], "FROZEN_DECLARATION_ONLY")
        self.assertFalse(result["autoShadowAccepted"])
        self.assertNotIn("supersedes", payload)
        self.assertEqual(payload["runtime"]["mode"], "FORWARD_RUNTIME")
        self.assertEqual(payload["sourceReliability"]["captureImplementationSha256"], "capture")
        self.assertEqual(payload["execution"], freeze.v10.EXECUTION)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o444)
        self.assertFalse(self.database.parent.exists())
        self.assertFalse(self.capture.exists())
        self.assertEqual(self.environment.read_text(), "DONT_CHANGE=true\n")

    def test_unqualified_original_evidence_rolls_back_only_new_declaration(self):
        with patch.object(freeze.v10, "validate_v10_declaration", side_effect=ValueError("unqualified")):
            with self.assertRaises(ValueError):
                self.run_freeze()
        self.assert_no_outputs()

    def test_today_past_naive_future_clock_and_bad_run_rejected(self):
        for overrides in ({"first_signal_date": "2026-10-23"},
                          {"now": datetime(2026, 10, 23)},
                          {"now": datetime.fromisoformat("2099-10-23T12:00:00+08:00")},
                          {"run_id": "../v9"}):
            with self.subTest(overrides=overrides), self.assertRaises(ValueError):
                self.run_freeze(**overrides)
            self.assert_no_outputs()

    def test_existing_target_or_dangling_link_preserved(self):
        self.path.parent.mkdir(exist_ok=True)
        self.path.symlink_to(self.root / "missing")
        with self.assertRaises(ValueError):
            self.run_freeze()
        self.assertTrue(self.path.is_symlink())
        self.path.unlink()
        self.path.write_text("preserve")
        with self.assertRaises(ValueError):
            self.run_freeze()
        self.assertEqual(self.path.read_text(), "preserve")

    def test_overlapping_outputs_and_existing_run_directory_rejected(self):
        for overrides in ({"database": self.capture / "ledger.sqlite3"},
                          {"capture_root": self.provider / "capture"},
                          {"capture_root": self.root.parent / "outside"}):
            with self.subTest(overrides=overrides), self.assertRaises(ValueError):
                self.run_freeze(**overrides)
            self.assert_no_outputs()
        self.database.parent.mkdir()
        with self.assertRaises(ValueError):
            self.run_freeze()

    def test_readback_drift_rolls_back_declaration(self):
        with patch.object(freeze.v10, "validate_v10_declaration", return_value={"declarationSha256": "drift"}):
            with self.assertRaises(ValueError):
                self.run_freeze()
        self.assert_no_outputs()

    def test_relative_capture_files_resolve_against_project_from_other_cwd(self):
        source = self.root / "relative-capture.py"
        source.write_text("# capture fixture\n")
        previous = Path.cwd()
        try:
            os.chdir(self.root.parent)
            with patch.object(freeze.v10.capture_v4, "capture_implementation_contract",
                              return_value={"sha256": "capture", "files": [{"path": source.name}]}), \
                 patch.object(freeze.v10, "validate_v10_declaration", side_effect=self.qualified):
                self.run_freeze()
        finally:
            os.chdir(previous)
        files = json.loads(self.path.read_text())["implementation"]["files"]
        self.assertIn(str(source), [item["path"] for item in files])
        self.assertEqual(len(files), len({item["path"] for item in files}))

    def test_generated_contract_is_accepted_by_actual_runtime_adapter(self):
        import v10_runtime as runtime
        target = self.root / "frontend-v2/backend/v10_runtime.py"
        target.write_bytes(Path(runtime.__file__).read_bytes())
        with patch.object(freeze.v10, "validate_v10_declaration", side_effect=self.qualified):
            self.run_freeze()
        payload = json.loads(self.path.read_text())
        backend = SimpleNamespace(PROJECT_ROOT=self.root,
                    _load_dotenv_dict=lambda: {runtime.DECLARATION_KEY: str(self.path)},
                    app=SimpleNamespace(router=SimpleNamespace(lifespan_context=None)))
        # Original source report recomputation is exercised by V10's own full
        # evidence tests; this test covers the real freeze/runtime schema seam.
        checked = {"declaration": payload, "declarationSha256": freeze._sha(self.path)}
        with patch.object(runtime, "validate_v10_declaration", return_value=checked):
            adapter = runtime.V10Runtime(backend)
            self.assertIsNotNone(adapter.validate(), adapter.error)
        self.assertFalse(self.database.exists())


if __name__ == "__main__":
    unittest.main()
