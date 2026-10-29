from datetime import datetime
import hashlib
import json
from pathlib import Path
import shutil
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

from scripts import freeze_shadow_recovery as freeze
from shadow_recovery import ShadowRecovery


NOW = datetime(2026, 10, 23, 11, 0, tzinfo=ZoneInfo("Asia/Shanghai"))


class FreezeShadowRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.provider = self.root / "data/market/a_share/qlib/new-independent-provider"
        for directory in ["calendars", "instruments", "features"]:
            (self.provider / directory).mkdir(parents=True, exist_ok=True)
        (self.provider / "calendars/day.txt").write_text("2026-10-21\n2026-10-22\n")
        (self.provider / "calendars/day_future.txt").write_text("2026-10-23\n2026-10-26\n2026-10-27\n")
        (self.provider / "instruments/csi300.txt").write_text("".join(
            f"SH{600000+n}\t2026-10-21\t2026-10-22\n" for n in range(300)))
        self.output = self.root / "data/market/a_share/intraday/recovery_20261026"
        self.declaration = self.root / "configs/recovery_20261026.json"
        self.calibration = self.root / "configs/calibration_v4_20261026.json"
        for name in [freeze._TEMPLATE, freeze._TEMPLATE + ".sha256", *freeze._IMPLEMENTATION_FILES]:
            target = self.root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(freeze.PROJECT_ROOT / name, target)

    def _freeze(self, **kwargs):
        args = dict(provider=self.provider, trade_date="2026-10-26", output=self.output,
                    declaration=self.declaration, calibration_declaration=self.calibration,
                    project_root=self.root, now=NOW)
        args.update(kwargs)
        return freeze.freeze_shadow_recovery(**args)

    def _targets(self):
        return [self.declaration, self.calibration,
                self.declaration.with_suffix(".json.sha256"),
                self.calibration.with_suffix(".json.sha256")]

    def test_freezes_matching_future_contracts_without_enabling_anything(self):
        environment = self.root / ".env"
        environment.write_text("QUANTAALPHA_INTRADAY_SHADOW_ENABLED=false\n")
        before = environment.read_bytes()
        result = self._freeze()
        self.assertEqual(result["status"], "FROZEN_CALIBRATION_ONLY")
        self.assertFalse(result["workersStarted"])
        self.assertFalse(result["autoShadowAccepted"])
        self.assertEqual(environment.read_bytes(), before)
        self.assertFalse(self.output.exists())
        recovery = json.loads(self.declaration.read_text())
        calibration = json.loads(self.calibration.read_text())
        self.assertEqual(recovery["providerBinding"], calibration["captureContract"]["binding"])
        self.assertEqual(calibration["calibration"]["expectedSlotCount"], 242)
        self.assertEqual(calibration["calibration"]["continuousSessionWindows"], freeze._WINDOWS)
        self.assertEqual(calibration["calibration"]["qualityGates"], freeze._GATES)
        self.assertEqual(calibration["calibration"]["finalizeAfter"], "15:05:00")
        self.assertTrue(calibration["createdBeforeCurrentTradeDateQuotesObserved"])
        self.assertFalse(calibration["returnValuesPermitted"])
        self.assertFalse(calibration["featureValuesPermitted"])
        self.assertFalse(calibration["profitEvidenceEligible"])
        for declaration in [self.declaration, self.calibration]:
            digest = hashlib.sha256(declaration.read_bytes()).hexdigest()
            self.assertEqual(declaration.with_suffix(".json.sha256").read_text().split(),
                             [digest, declaration.name])
            self.assertEqual(declaration.stat().st_mode & 0o777, 0o444)

    def test_runtime_adapter_accepts_the_frozen_pair(self):
        self._freeze()
        backend = SimpleNamespace(
            PROJECT_ROOT=self.root,
            _load_dotenv_dict=lambda: {"QA_SHADOW_RECOVERY_DECLARATION": str(self.declaration)},
            _intraday_capture_config=lambda: {"enabled": False, "outputRoot": str(self.root / "legacy")},
            _intraday_capture_runtime_binding=lambda: None,
            _intraday_capture_effective_provider_path=lambda config, **kwargs: config["providerPath"],
            _research_shadow_auto_config=lambda: {"enabled": False},
            _intraday_shadow_config=lambda: {"enabled": False},
        )
        recovery = ShadowRecovery(backend, str(self.declaration))
        with patch("shadow_recovery._now", return_value=NOW):
            self.assertIsNotNone(recovery.validate(), recovery.error)
            self.assertEqual(recovery.status()["stage"], "WAITING_CALIBRATION")
        self.assertFalse(self.output.exists())

    def test_refuses_today_naive_time_and_non_trading_day(self):
        for changes in [{"trade_date": "2026-10-23"},
                        {"now": NOW.replace(tzinfo=None)},
                        {"trade_date": "2026-10-25"}]:
            with self.subTest(fields=list(changes)):
                with self.assertRaises(ValueError):
                    self._freeze(**changes)
                self.assertTrue(all(not path.exists() for path in self._targets()))

    def test_refuses_existing_declarations_or_sidecars_without_touching_them(self):
        self.calibration.with_suffix(".json.sha256").write_text("prior-freeze\n")
        with self.assertRaises(ValueError):
            self._freeze()
        self.assertEqual(self.calibration.with_suffix(".json.sha256").read_text(), "prior-freeze\n")
        self.assertFalse(self.declaration.exists())

    def test_refuses_dangling_symlink_and_overlapping_declaration_files(self):
        self.declaration.symlink_to(self.root / "configs/not-created-target.json")
        with self.assertRaises(ValueError):
            self._freeze()
        self.assertTrue(self.declaration.is_symlink())
        self.assertFalse((self.root / "configs/not-created-target.json").exists())
        self.declaration.unlink()
        with self.assertRaises(ValueError):
            self._freeze(calibration_declaration=self.declaration / "nested.json")
        self.assertTrue(all(not path.exists() for path in self._targets()))

    def test_refuses_legacy_output_provider_and_outside_project(self):
        legacy = self.root / "configs/intraday_shadow_existing.json"
        legacy.write_text(json.dumps({"providerRoot": {"path": str(self.provider)},
                                     "captureRoot": str(self.root / "historical-capture")}))
        with self.assertRaises(ValueError):
            self._freeze()
        legacy.unlink()
        for output in [self.root / "data/market/a_share/intraday/a_stock_data_csi300/subdirectory",
                       self.provider / "capture", self.root.parent / "outside-capture"]:
            with self.subTest(output=str(output)):
                with self.assertRaises(ValueError):
                    self._freeze(output=output)
        self.assertTrue(all(not path.exists() for path in self._targets()))

    def test_rolls_back_its_own_files_if_a_later_exclusive_create_fails(self):
        sentinel = self.root / "configs/keep.txt"
        sentinel.write_bytes(b"unrelated")
        original_open = freeze.os.open
        calls = 0
        def fail_third(path, flags, mode=0o777):
            nonlocal calls
            calls += 1
            if calls == 3:
                raise OSError("fixture failure")
            return original_open(path, flags, mode)
        with patch("scripts.freeze_shadow_recovery.os.open", side_effect=fail_third):
            with self.assertRaises(OSError):
                self._freeze()
        self.assertTrue(all(not path.exists() for path in self._targets()))
        self.assertEqual(sentinel.read_bytes(), b"unrelated")

    def test_rolls_back_if_bound_inputs_change_during_freeze(self):
        actual = freeze._production_capture_contract
        count = 0
        def drift():
            nonlocal count
            result = actual()
            count += 1
            if count > 1:
                result = {**result, "sha256": "0" * 64}
            return result
        with patch("scripts.freeze_shadow_recovery._production_capture_contract", side_effect=drift):
            with self.assertRaisesRegex(ValueError, "inputs changed"):
                self._freeze()
        self.assertTrue(all(not path.exists() for path in self._targets()))


if __name__ == "__main__":
    unittest.main()
