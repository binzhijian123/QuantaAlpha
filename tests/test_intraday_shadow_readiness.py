import hashlib
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from scripts.verify_intraday_shadow_readiness import (
    build_readiness_report,
    inspect_session_continuity,
    inspect_storage,
    verify_declaration_binding,
)


CHINA_TZ = ZoneInfo("Asia/Shanghai")


def _fixtures():
    supervisor = {
        "running": True,
        "healthy": True,
        "sleepGuardConfigured": True,
        "sleepGuardActive": True,
    }
    health = {
        "ready": True,
        "workers": {
            name: {"ready": True, "taskAlive": True, "consecutiveErrors": 0}
            for name in (
                "researchShadowWatcher",
                "intradayCapture",
                "intradayShadow",
            )
        },
    }
    capture = {
        "status": "WAITING_MARKET",
        "enabled": True,
        "configuredEnabled": True,
        "consecutiveErrors": 0,
        "lastError": None,
        "nextCaptureAt": "2026-08-14T09:15:00+08:00",
        "calibration": {
            "status": "PASS",
            "finalized": True,
            "profitEvidenceEligible": False,
            "capturedExpectedSlotCount": 242,
            "expectedSlotCount": 242,
        },
    }
    state = {
        "runId": "intraday_shadow_test_v4",
        "status": "STAGED",
        "declaration": {"path": "/tmp/declaration.json", "sha256": "a" * 64},
        "protocol": {"firstEligibleSignalDate": "2026-08-14"},
        "pairedEvidence": {"missedSessions": []},
        "quality": {
            "requiredUniverseSize": 301,
            "minimumCoreFieldCoverageRatio": 0.95,
            "maximumQuoteAgeSeconds": 120,
            "requireBenchmarkUsable": True,
            "requireExecutionQuotesForAllTargets": True,
        },
        "qualityExtension": {
            "requireDecisionQuotesForAllTargets": True,
            "requireCoreFieldAwareFallback": True,
            "benchmarkTurnoverRequired": False,
        },
        "audit": {"valid": True, "events": 1},
        "evidenceCheckpoint": {
            "valid": True,
            "upToDate": True,
            "count": 1,
        },
    }
    automation = {
        "runId": state["runId"],
        "enabled": True,
        "configuredEnabled": True,
        "configuredDeclaration": state["declaration"]["path"],
        "consecutiveErrors": 0,
        "lastError": None,
    }
    return {
        "supervisor": supervisor,
        "health": health,
        "capture": capture,
        "state": state,
        "automation": automation,
        "declaration_binding": {"valid": True, "errors": []},
        "session_continuity": {
            "valid": True,
            "phase": "BEFORE_FIRST_SESSION",
            "minimumExpectedSessions": 0,
            "maximumExpectedSessions": 0,
            "representedSessions": 0,
            "errors": [],
        },
        "storage": {
            "exists": True,
            "writable": True,
            "freeBytes": 2_000_000,
            "referenceCaptureBytes": 1_000_000,
        },
        "now": datetime(2026, 8, 13, 18, 0, tzinfo=CHINA_TZ),
    }


class IntradayShadowReadinessTests(unittest.TestCase):
    def test_complete_preflight_is_ready(self):
        report = build_readiness_report(**_fixtures())

        self.assertTrue(report["ready"])
        self.assertEqual(report["status"], "READY")
        self.assertEqual(len(report["checks"]), 14)
        self.assertTrue(all(check["passed"] for check in report["checks"]))

    def test_recovered_capture_does_not_hide_earlier_errors(self):
        fixtures = _fixtures()
        fixtures["capture"].update({
            "status": "CAPTURED",
            "temporalHealth": {
                "tradeDate": "2026-08-13", "integrityValid": True,
                "status": "DEGRADED", "errorSlotCount": 66, "missingSlotCount": 0,
            },
        })
        report = build_readiness_report(**fixtures)
        self.assertFalse(report["ready"])
        self.assertEqual(
            [c["name"] for c in report["checks"] if not c["passed"]],
            ["captureHistory"],
        )

    def test_capture_history_respects_day_and_integrity(self):
        cases = [
            ("2026-08-13", "IN_PROGRESS", 0, 0, True, "CAPTURED", True),
            ("2026-08-13", "COMPLETE", 0, 0, True, "WAITING_MARKET", True),
            ("2026-08-13", "DEGRADED", 1, 0, True, "WAITING_MARKET", False),
            ("2026-08-13", "IN_PROGRESS", 0, 1, True, "CAPTURED", False),
            ("2026-08-12", "DEGRADED", 66, 0, True, "WAITING_MARKET", True),
            ("2026-08-12", "COMPLETE", 0, 0, True, "CAPTURED", False),
            ("2026-08-14", "COMPLETE", 0, 0, True, "CAPTURED", False),
            ("2026-08-12", "COMPLETE", 0, 0, False, "WAITING_MARKET", False),
            ("2026-08-13", "UNKNOWN", 0, 0, True, "CAPTURED", False),
            ("2026-08-13", "COMPLETE", -1, 0, True, "CAPTURED", False),
            ("2026-08-13", "COMPLETE", None, 0, True, "CAPTURED", False),
        ]
        for day, status, errors, missing, integrity, capture_status, expected in cases:
            with self.subTest(day=day, status=status, errors=errors, missing=missing):
                fixtures = _fixtures()
                fixtures["capture"].update({
                    "status": capture_status,
                    "temporalHealth": {
                        "tradeDate": day, "status": status, "integrityValid": integrity,
                        "errorSlotCount": errors, "missingSlotCount": missing,
                    },
                })
                report = build_readiness_report(**fixtures)
                check = next(c for c in report["checks"] if c["name"] == "captureHistory")
                self.assertEqual(check["passed"], expected)

    def test_active_capture_requires_history(self):
        for history in (None, [], "invalid"):
            with self.subTest(history=history):
                fixtures = _fixtures()
                fixtures["capture"].update({"status": "CAPTURED", "temporalHealth": history})
                self.assertFalse(build_readiness_report(**fixtures)["ready"])

    def test_session_continuity_failure_fails_readiness(self):
        fixtures = _fixtures()
        fixtures["session_continuity"] = {
            "valid": False,
            "phase": "POST_FREEZE_DEADLINE",
            "errors": ["missing due session"],
        }

        report = build_readiness_report(**fixtures)

        self.assertFalse(report["ready"])
        failed = [check["name"] for check in report["checks"] if not check["passed"]]
        self.assertEqual(failed, ["sessionContinuity"])

    def test_session_continuity_allows_both_states_during_freeze_window(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = self._session_state(Path(tmp))
            now = datetime(2026, 8, 14, 15, 10, tzinfo=CHINA_TZ)

            waiting = inspect_session_continuity(state, now=now)
            state["pairedEvidence"]["frozenSessions"] = 1
            state["latestSession"] = {
                "trade_date": "2026-08-14",
                "status": "FROZEN_AWAITING_OFFICIAL",
            }
            frozen = inspect_session_continuity(state, now=now)

        self.assertTrue(waiting["valid"])
        self.assertTrue(frozen["valid"])
        self.assertEqual(waiting["phase"], "FREEZE_WINDOW")
        self.assertEqual(waiting["minimumExpectedSessions"], 0)
        self.assertEqual(waiting["maximumExpectedSessions"], 1)

    def test_session_continuity_fails_after_deadline_without_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = self._session_state(Path(tmp))

            result = inspect_session_continuity(
                state,
                now=datetime(2026, 8, 14, 15, 21, tzinfo=CHINA_TZ),
            )

        self.assertFalse(result["valid"])
        self.assertEqual(result["phase"], "POST_FREEZE_DEADLINE")
        self.assertEqual(result["minimumExpectedSessions"], 1)
        self.assertIn(
            "represented session count does not match the frozen calendar deadline",
            result["errors"],
        )

    def test_session_continuity_accepts_frozen_session_after_deadline(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = self._session_state(Path(tmp))
            state["pairedEvidence"]["frozenSessions"] = 1
            state["latestSession"] = {
                "trade_date": "2026-08-14",
                "status": "FROZEN_AWAITING_OFFICIAL",
            }

            result = inspect_session_continuity(
                state,
                now=datetime(2026, 8, 14, 15, 21, tzinfo=CHINA_TZ),
            )

        self.assertTrue(result["valid"])
        self.assertEqual(result["representedSessions"], 1)

    def test_two_phase_continuity_accepts_intent_before_execution_binding(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = self._session_state(Path(tmp))
            state["causality"] = {"decisionIntentRequired": True}
            state["protocol"].update(
                {
                    "decisionSlot": "14:50:00",
                    "executionSlot": "14:55:00",
                    "decisionFreezeAfter": "14:50:00",
                    "decisionFreezeDeadline": "14:54:30",
                    "freezeAfter": "14:56:00",
                    "freezeDeadline": "15:05:30",
                }
            )
            state["pairedEvidence"]["pendingDecisionSessions"] = 1
            state["latestDecisionIntent"] = {
                "tradeDate": "2026-08-14",
                "status": "DECISION_FROZEN",
            }

            result = inspect_session_continuity(
                state,
                now=datetime(2026, 8, 14, 14, 55, 20, tzinfo=CHINA_TZ),
            )

        self.assertTrue(result["valid"])
        self.assertEqual(result["phase"], "AWAITING_EXECUTION_WINDOW")
        self.assertEqual(result["representedSessions"], 1)
        self.assertEqual(result["pendingDecisionSessions"], 1)

    @staticmethod
    def _session_state(root: Path):
        provider = root / "provider"
        (provider / "calendars").mkdir(parents=True)
        (provider / "calendars" / "day_future.txt").write_text(
            "2026-08-14\n2026-08-17\n",
            encoding="ascii",
        )
        declaration = root / "declaration.json"
        declaration.write_text(
            json.dumps({"providerRoot": {"path": str(provider)}}),
            encoding="utf-8",
        )
        return {
            "declaration": {"path": str(declaration)},
            "protocol": {
                "firstEligibleSignalDate": "2026-08-14",
                "freezeAfter": "15:06:00",
                "freezeDeadline": "15:20:00",
            },
            "pairedEvidence": {
                "frozenSessions": 0,
                "observationSessions": 0,
                "missedSessions": [],
            },
            "latestSession": None,
        }

    def test_worker_error_fails_closed(self):
        fixtures = _fixtures()
        fixtures["health"]["workers"]["intradayCapture"]["consecutiveErrors"] = 1

        report = build_readiness_report(**fixtures)

        self.assertFalse(report["ready"])
        failed = [check["name"] for check in report["checks"] if not check["passed"]]
        self.assertEqual(failed, ["backendWorkers"])

    def test_pre_first_day_capture_schedule_must_match_first_date(self):
        fixtures = _fixtures()
        fixtures["capture"]["nextCaptureAt"] = "2026-08-17T09:15:00+08:00"

        report = build_readiness_report(**fixtures)

        self.assertFalse(report["ready"])
        failed = [check["name"] for check in report["checks"] if not check["passed"]]
        self.assertEqual(failed, ["captureSchedule"])

    def test_ranked_target_quality_gates_are_mandatory(self):
        fixtures = _fixtures()
        fixtures["state"]["qualityExtension"][
            "requireDecisionQuotesForAllTargets"
        ] = False

        report = build_readiness_report(**fixtures)

        self.assertFalse(report["ready"])
        failed = [check["name"] for check in report["checks"] if not check["passed"]]
        self.assertEqual(failed, ["qualityContract"])

    def test_v4_core_field_fallback_contract_is_mandatory(self):
        fixtures = _fixtures()
        fixtures["state"]["qualityExtension"][
            "requireCoreFieldAwareFallback"
        ] = False

        report = build_readiness_report(**fixtures)

        self.assertFalse(report["ready"])
        failed = [check["name"] for check in report["checks"] if not check["passed"]]
        self.assertEqual(failed, ["qualityContract"])

    def test_v5_two_phase_causal_contract_is_mandatory(self):
        fixtures = _fixtures()
        state = fixtures["state"]
        state["runId"] = "intraday_shadow_test_v5"
        fixtures["automation"]["runId"] = state["runId"]
        state["causality"] = {
            "decisionIntentRequired": True,
            "requireNoPriorPendingSession": True,
            "requirePortfolioFingerprint": True,
        }
        state["protocol"].update(
            {
                "decisionSlot": "14:50:00",
                "executionSlot": "14:55:00",
                "decisionFreezeAfter": "14:50:00",
                "decisionFreezeDeadline": "14:54:30",
                "executionBindingAfter": "14:56:00",
                "executionBindingDeadline": "15:05:30",
            }
        )
        self.assertTrue(build_readiness_report(**fixtures)["ready"])

        state["causality"]["decisionIntentRequired"] = False
        report = build_readiness_report(**fixtures)

        self.assertFalse(report["ready"])
        failed = [check["name"] for check in report["checks"] if not check["passed"]]
        self.assertEqual(failed, ["causalOrder"])

    def test_v6_independent_source_contract_is_mandatory(self):
        fixtures = _fixtures()
        state = fixtures["state"]
        state["runId"] = "intraday_shadow_test_v6"
        fixtures["automation"]["runId"] = state["runId"]
        state["causality"] = {
            "decisionIntentRequired": True,
            "requireNoPriorPendingSession": True,
            "requirePortfolioFingerprint": True,
        }
        state["protocol"].update(
            {
                "decisionSlot": "14:50:00",
                "executionSlot": "14:55:00",
                "decisionFreezeAfter": "14:50:00",
                "decisionFreezeDeadline": "14:54:30",
                "executionBindingAfter": "14:56:00",
                "executionBindingDeadline": "15:05:30",
            }
        )
        state["sourceReliability"] = {
            "version": 1,
            "sourceOrder": ["tencent", "easy_tdx", "eastmoney", "sina"],
            "independentFallbackTransport": "TCP/7709",
            "easyTdxPackageVersion": "1.20.6",
            "realtimeContractSha256": "a" * 64,
            "requireRealtimeContractForEveryQuote": True,
            "prohibitCrossSourceFieldMerge": True,
            "qualification": {"reportSha256": "b" * 64},
        }
        fixtures["capture"]["runtimeContract"] = {
            "version": 1,
            "entrypointModule": "market_data.intraday_capture_v3",
            "providerModule": (
                "market_data.providers.a_stock_data_intraday_v3"
            ),
            "providerClass": "IndependentTransportRealtimeClient",
            "easyTdxPackageVersion": "1.20.6",
            "realtimeContractSha256": "a" * 64,
            "sourceOrder": ["tencent", "easy_tdx", "eastmoney", "sina"],
            "prohibitCrossSourceFieldMerge": True,
        }

        ready_report = build_readiness_report(**fixtures)
        self.assertTrue(ready_report["ready"])
        self.assertEqual(len(ready_report["checks"]), 15)

        state["sourceReliability"]["requireRealtimeContractForEveryQuote"] = False
        failed_report = build_readiness_report(**fixtures)
        failed = [
            check["name"]
            for check in failed_report["checks"]
            if not check["passed"]
        ]
        self.assertEqual(failed, ["sourceReliability"])

        state["sourceReliability"]["requireRealtimeContractForEveryQuote"] = True
        fixtures["capture"]["runtimeContract"]["entrypointModule"] = (
            "market_data.intraday_capture_v2"
        )
        wrong_runtime_report = build_readiness_report(**fixtures)
        failed = [
            check["name"]
            for check in wrong_runtime_report["checks"]
            if not check["passed"]
        ]
        self.assertEqual(failed, ["sourceReliability"])

    def test_declaration_verifier_detects_bound_file_drift(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            implementation = root / "implementation.py"
            implementation.write_text("value = 1\n", encoding="ascii")
            provider = root / "provider"
            (provider / "calendars").mkdir(parents=True)
            (provider / "calendars" / "day_future.txt").write_text(
                "2026-08-14\n", encoding="ascii"
            )
            declaration = root / "declaration.json"
            implementation_sha = hashlib.sha256(implementation.read_bytes()).hexdigest()
            payload = {
                "runId": "intraday_shadow_test_v3",
                "quality": {"requiredUniverseSize": 301},
                "qualityExtension": {
                    "requireDecisionQuotesForAllTargets": True
                },
                "providerRoot": {"path": str(provider)},
                "implementation": {
                    "files": [
                        {"path": str(implementation), "sha256": implementation_sha}
                    ]
                },
            }
            declaration.write_text(json.dumps(payload), encoding="utf-8")
            declaration_sha = hashlib.sha256(declaration.read_bytes()).hexdigest()
            declaration.with_suffix(".json.sha256").write_text(
                f"{declaration_sha}  {declaration.name}\n", encoding="ascii"
            )
            state = {
                "declaration": {
                    "path": str(declaration),
                    "sha256": declaration_sha,
                },
                "protocol": {"firstEligibleSignalDate": "2026-08-14"},
                "runId": payload["runId"],
                "quality": payload["quality"],
                "qualityExtension": payload["qualityExtension"],
            }
            self.assertTrue(verify_declaration_binding(state)["valid"])

            implementation.write_text("value = 2\n", encoding="ascii")
            result = verify_declaration_binding(state)

            self.assertFalse(result["valid"])
            self.assertIn("implementation drift: implementation.py", result["errors"])

    def test_missing_storage_root_is_not_created(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "missing"

            result = inspect_storage(str(missing))

            self.assertFalse(result["exists"])
            self.assertFalse(result["writable"])
            self.assertFalse(missing.exists())


if __name__ == "__main__":
    unittest.main()
