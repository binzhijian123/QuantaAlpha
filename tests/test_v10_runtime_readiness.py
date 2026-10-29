from datetime import datetime
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts import verify_v10_runtime_readiness as verify


class V10ReadinessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.validated = {"declaration": {"runId": "test_v10", "captureRoot": str(self.root),
                            "firstEligibleSignalDate": "2026-10-27"},
                          "declarationSha256": "digest",
                          "providerContext": {"tradingDates": ["2026-10-27"]}}
        self.status = {"mode": "FORWARD_RUNTIME", "configured": True,
                       "runId": "test_v10", "declarationSha256": "digest",
                       "captureEnabled": True, "shadowEnabled": True, "maintenanceEnabled": True,
                       "workers": {name: {"ready": True, "taskAlive": True} for name in verify.WORKERS}}
        self.health = {"ready": True, "runId": "test_v10", "declarationSha256": "digest"}
        self.now = datetime.fromisoformat("2026-10-26T16:00:00+08:00")

    def evaluate(self, temporal=None):
        def fetch(path):
            return self.status if path.endswith("status") else self.health
        with patch.object(verify.v10, "validate_v10_declaration", return_value=self.validated), \
             patch.object(verify, "summarize_latest_capture_day", return_value=temporal):
            return verify.evaluate(self.root / "decl.json", now=self.now, fetch=fetch)

    def test_ready_never_means_auto_shadow_or_release_accepted(self):
        result = self.evaluate()
        self.assertTrue(result["ready"])
        self.assertFalse(result["autoShadowAccepted"])
        self.assertFalse(result["releaseAccepted"])

    def test_other_run_or_declaration_is_not_ready(self):
        self.status["runId"] = "old_v9"
        self.assertIn("runtimeBinding", self.evaluate()["failedChecks"])
        self.status["runId"] = "test_v10"
        self.health["declarationSha256"] = "old"
        self.assertIn("healthBinding", self.evaluate()["failedChecks"])

    def test_missing_maintenance_and_dead_task_are_not_ready(self):
        self.status["workers"].pop("maintenance")
        self.assertIn("maintenanceWorker", self.evaluate()["failedChecks"])
        self.status["workers"]["maintenance"] = {"ready": True, "taskAlive": False}
        self.assertFalse(self.evaluate()["ready"])

    def test_no_today_history_and_old_success_cannot_hide_missing_current_slots(self):
        self.now = datetime.fromisoformat("2026-10-27T10:00:00+08:00")
        self.assertIn("captureHistory", self.evaluate()["failedChecks"])
        temporal = {"integrityValid": True, "errorSlotCount": 0, "missingSlotCount": 0,
                    "tradeDate": "2026-10-26"}
        self.assertFalse(self.evaluate(temporal)["ready"])
        temporal["tradeDate"] = "2026-10-27"
        self.assertTrue(self.evaluate(temporal)["ready"])
        temporal["errorSlotCount"] = 1
        self.assertFalse(self.evaluate(temporal)["ready"])

    def test_first_slot_due_boundary_matches_capture_temporal_contract(self):
        self.now = datetime.fromisoformat("2026-10-27T09:30:59+08:00")
        self.assertTrue(self.evaluate()["ready"])
        self.now = datetime.fromisoformat("2026-10-27T09:31:00+08:00")
        self.assertFalse(self.evaluate()["ready"])

    def test_invalid_declaration_and_api_errors_fail_closed_without_error_leak(self):
        with patch.object(verify.v10, "validate_v10_declaration", side_effect=ValueError("secret")):
            result = verify.evaluate(self.root / "decl.json", now=self.now)
        self.assertFalse(result["ready"])
        self.assertNotIn("secret", str(result))
        with patch.object(verify.v10, "validate_v10_declaration", return_value=self.validated):
            result = verify.evaluate(self.root / "decl.json", now=self.now,
                                     fetch=lambda path: (_ for _ in ()).throw(RuntimeError("secret")))
        self.assertFalse(result["ready"])
        self.assertNotIn("secret", str(result))


if __name__ == "__main__":
    unittest.main()
