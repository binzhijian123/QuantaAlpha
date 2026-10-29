import importlib.util
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


def _load_backend_app():
    app_path = Path(__file__).resolve().parents[1] / "frontend-v2" / "backend" / "app.py"
    spec = importlib.util.spec_from_file_location("quantaalpha_frontend_backend_app", app_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load backend app from {app_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


BACKEND_APP = _load_backend_app()


class BackendDiagnosticTests(unittest.TestCase):
    def _diagnostic(self, line):
        with patch.object(BACKEND_APP, "ENABLE_LOG_DIAGNOSTICS", True):
            return BACKEND_APP._diagnostic_from_line(line, "test")

    def test_qlib_optional_model_notices_are_not_dependency_errors(self):
        notices = (
            "ModuleNotFoundError. CatBoostModel are skipped. (optional: maybe installing CatBoostModel can fix it.)",
            "ModuleNotFoundError. DEnsembleModel and LGBModel are skipped. (optional: maybe installing lightgbm can fix it.)",
            "ModuleNotFoundError. XGBModel is skipped(optional: maybe installing xgboost can fix it).",
            "ModuleNotFoundError. LinearModel is skipped(optional: maybe installing scipy and sklearn can fix it).",
            "ModuleNotFoundError. PyTorch models are skipped (optional: maybe installing pytorch can fix it).",
            "MODULENOTFOUNDERROR. OPTIONAL MODEL IS SKIPPED (OPTIONAL: NOT REQUIRED).",
        )

        for notice in notices:
            with self.subTest(notice=notice):
                self.assertIsNone(self._diagnostic(notice))

    def test_real_missing_modules_still_report_dependency_errors(self):
        errors = (
            "ModuleNotFoundError: No module named 'lightgbm'",
            "ImportError: NO MODULE NAMED 'pyyaml'",
        )

        for error in errors:
            with self.subTest(error=error):
                diagnostic = self._diagnostic(error)
                self.assertIsNotNone(diagnostic)
                self.assertEqual(diagnostic["code"], "python_module_missing")
                self.assertEqual(diagnostic["severity"], "error")

    def test_optional_words_in_module_names_do_not_hide_real_errors(self):
        errors = (
            "ModuleNotFoundError: No module named 'optional_dependency'",
            "ModuleNotFoundError: No module named 'skipped_rows'",
            "ModuleNotFoundError: No module named 'optional.skipped'",
            "ModuleNotFoundError: No module named 'skipped.optional'",
        )

        for error in errors:
            with self.subTest(error=error):
                diagnostic = self._diagnostic(error)
                self.assertIsNotNone(diagnostic)
                self.assertEqual(diagnostic["code"], "python_module_missing")


class MiningQualityGateConfigTests(unittest.TestCase):
    def test_enabled_quality_gate_forces_consistency_check(self):
        request = BACKEND_APP.MiningStartRequest(direction="test", qualityGateEnabled=True)
        config = {"quality_gate": {"consistency_enabled": False}}

        BACKEND_APP._apply_mining_overrides(config, request)

        self.assertIs(config["quality_gate"]["consistency_enabled"], True)

    def test_disabled_quality_gate_disables_all_checks(self):
        request = BACKEND_APP.MiningStartRequest(direction="test", qualityGateEnabled=False)
        config = {"quality_gate": {"consistency_enabled": True}}

        BACKEND_APP._apply_mining_overrides(config, request)

        self.assertIs(config["quality_gate"]["consistency_enabled"], False)


if __name__ == "__main__":
    unittest.main()
