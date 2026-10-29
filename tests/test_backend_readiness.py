import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo


def _load_backend_app():
    app_path = Path(__file__).resolve().parents[1] / "frontend-v2" / "backend" / "app.py"
    backend_path = str(app_path.parent)
    if backend_path not in sys.path:
        sys.path.insert(0, backend_path)
    module_name = "quantaalpha_frontend_backend_readiness_app"
    spec = importlib.util.spec_from_file_location(module_name, app_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load backend app from {app_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


BACKEND_APP = _load_backend_app()


class FakeTask:
    def __init__(self, *, done=False, error=None):
        self._done = done
        self._error = error

    def done(self):
        return self._done

    def exception(self):
        return self._error


class BackendReadinessTests(unittest.TestCase):
    def test_intraday_shadow_coordinator_selects_v9_protocol(self):
        from market_data import intraday_shadow_v9

        with tempfile.TemporaryDirectory() as tmp:
            declaration = Path(tmp) / "v9.json"
            declaration.write_text(
                json.dumps(
                    {
                        "protocolId": (
                            "intraday-shadow-t1-reversal-proxy-v9-provider-maintenance"
                        )
                    }
                ),
                encoding="utf-8",
            )
            expected = object()
            with (
                patch.object(
                    BACKEND_APP,
                    "_intraday_shadow_config",
                    return_value={"declaration": str(declaration)},
                ),
                patch.object(
                    intraday_shadow_v9,
                    "IntradayShadowCoordinator",
                    return_value=expected,
                ) as factory,
            ):
                actual = BACKEND_APP._intraday_shadow_coordinator()

        self.assertIs(actual, expected)
        factory.assert_called_once_with(declaration)

    def test_intraday_shadow_coordinator_selects_v8_protocol(self):
        from market_data import intraday_shadow_v8

        with tempfile.TemporaryDirectory() as tmp:
            declaration = Path(tmp) / "v8.json"
            declaration.write_text(
                json.dumps(
                    {
                        "protocolId": (
                            "intraday-shadow-t1-reversal-proxy-v8-provider-maintenance"
                        )
                    }
                ),
                encoding="utf-8",
            )
            expected = object()
            with (
                patch.object(
                    BACKEND_APP,
                    "_intraday_shadow_config",
                    return_value={"declaration": str(declaration)},
                ),
                patch.object(
                    intraday_shadow_v8,
                    "IntradayShadowCoordinator",
                    return_value=expected,
                ) as factory,
            ):
                actual = BACKEND_APP._intraday_shadow_coordinator()

        self.assertIs(actual, expected)
        factory.assert_called_once_with(declaration)

    def test_intraday_shadow_coordinator_selects_v7_protocol(self):
        from market_data import intraday_shadow_v7

        with tempfile.TemporaryDirectory() as tmp:
            declaration = Path(tmp) / "v7.json"
            declaration.write_text(
                json.dumps(
                    {
                        "protocolId": (
                            "intraday-shadow-t1-reversal-proxy-v7-rebased-provider"
                        )
                    }
                ),
                encoding="utf-8",
            )
            expected = object()
            with (
                patch.object(
                    BACKEND_APP,
                    "_intraday_shadow_config",
                    return_value={"declaration": str(declaration)},
                ),
                patch.object(
                    intraday_shadow_v7,
                    "IntradayShadowCoordinator",
                    return_value=expected,
                ) as factory,
            ):
                actual = BACKEND_APP._intraday_shadow_coordinator()

        self.assertIs(actual, expected)
        factory.assert_called_once_with(declaration)

    def test_intraday_capture_runtime_binding_is_v3_and_pinned(self):
        runner, contract = BACKEND_APP._intraday_capture_runtime_binding()

        self.assertEqual(runner.__module__, "market_data.intraday_capture_v3")
        self.assertEqual(
            contract["providerModule"],
            "market_data.providers.a_stock_data_intraday_v3",
        )
        self.assertEqual(contract["easyTdxPackageVersion"], "1.20.6")
        self.assertEqual(len(contract["realtimeContractSha256"]), 64)
        self.assertTrue(contract["prohibitCrossSourceFieldMerge"])

    def test_intraday_capture_runtime_binding_rejects_dependency_drift(self):
        with (
            patch.object(
                BACKEND_APP.importlib_metadata,
                "version",
                return_value="1.20.5",
            ),
            self.assertRaisesRegex(RuntimeError, "runtime binding drifted"),
        ):
            BACKEND_APP._intraday_capture_runtime_binding()

    def test_intraday_capture_runtime_binding_rejects_provider_alias_drift(self):
        from market_data.intraday_capture_v3 import run_capture_cycle

        with (
            patch.dict(
                run_capture_cycle.__globals__,
                {"IndependentTransportRealtimeClient": object},
            ),
            self.assertRaisesRegex(RuntimeError, "runtime binding drifted"),
        ):
            BACKEND_APP._intraday_capture_runtime_binding()

    def _payload(
        self,
        *,
        now=1000.0,
        process_started=900.0,
        research_enabled=True,
        intraday_enabled=False,
        intraday_shadow_enabled=False,
        tasks=None,
        states=None,
    ):
        with (
            patch.object(
                BACKEND_APP,
                "_research_shadow_auto_config",
                return_value={"enabled": research_enabled},
            ),
            patch.object(
                BACKEND_APP,
                "_intraday_capture_config",
                return_value={"enabled": intraday_enabled},
            ),
            patch.object(
                BACKEND_APP,
                "_intraday_shadow_config",
                return_value={"enabled": intraday_shadow_enabled},
            ),
            patch.object(BACKEND_APP, "PROCESS_STARTED_MONOTONIC", process_started),
            patch.object(BACKEND_APP, "_BACKGROUND_TASKS", tasks or {}),
            patch.object(BACKEND_APP, "_BACKGROUND_WORKER_STATE", states or {}),
        ):
            return BACKEND_APP._runtime_readiness_payload(now_monotonic=now)

    def test_disabled_workers_are_ready_without_task_handles(self):
        payload = self._payload(
            research_enabled=False,
            intraday_enabled=False,
            process_started=0.0,
        )

        self.assertTrue(payload["ready"])
        self.assertEqual(payload["status"], "healthy")
        self.assertEqual(
            payload["workers"][BACKEND_APP.RESEARCH_SHADOW_WORKER]["status"],
            "DISABLED",
        )

    def test_missing_enabled_worker_expires_after_startup_grace(self):
        starting = self._payload(now=50.0, process_started=0.0)
        expired = self._payload(now=61.0, process_started=0.0)

        self.assertTrue(starting["ready"])
        self.assertFalse(expired["ready"])
        self.assertEqual(
            expired["workers"][BACKEND_APP.RESEARCH_SHADOW_WORKER]["reason"],
            "TASK_NOT_REGISTERED",
        )

    def test_completed_task_is_not_ready(self):
        name = BACKEND_APP.RESEARCH_SHADOW_WORKER
        payload = self._payload(
            tasks={name: FakeTask(done=True, error=RuntimeError("worker stopped"))},
            states={name: {"status": "IDLE"}},
        )

        worker = payload["workers"][name]
        self.assertFalse(payload["ready"])
        self.assertEqual(worker["reason"], "TASK_EXITED")
        self.assertEqual(worker["lastError"], "worker stopped")

    def test_running_worker_is_ready_until_operation_timeout(self):
        name = BACKEND_APP.RESEARCH_SHADOW_WORKER
        tasks = {name: FakeTask()}
        state = {
            name: {
                "status": "RUNNING",
                "lastStartedMonotonic": 100.0,
                "maxRunSeconds": 300.0,
            }
        }

        within_limit = self._payload(now=400.0, tasks=tasks, states=state)
        timed_out = self._payload(now=400.001, tasks=tasks, states=state)

        self.assertTrue(within_limit["ready"])
        self.assertFalse(timed_out["ready"])
        self.assertEqual(
            timed_out["workers"][name]["reason"],
            "RUN_TIMEOUT",
        )

    def test_idle_worker_becomes_unready_only_after_deadline(self):
        name = BACKEND_APP.RESEARCH_SHADOW_WORKER
        tasks = {name: FakeTask()}
        state = {
            name: {
                "status": "IDLE",
                "lastHeartbeatMonotonic": 100.0,
                "idleDeadlineMonotonic": 200.0,
                "idleGraceSeconds": 60.0,
            }
        }

        on_deadline = self._payload(now=200.0, tasks=tasks, states=state)
        stale = self._payload(now=200.001, tasks=tasks, states=state)

        self.assertTrue(on_deadline["ready"])
        self.assertFalse(stale["ready"])
        self.assertEqual(stale["workers"][name]["reason"], "HEARTBEAT_STALE")

    def test_transient_business_error_is_degraded_but_ready(self):
        name = BACKEND_APP.RESEARCH_SHADOW_WORKER
        payload = self._payload(
            now=150.0,
            tasks={name: FakeTask()},
            states={
                name: {
                    "status": "DEGRADED",
                    "lastHeartbeatMonotonic": 100.0,
                    "idleDeadlineMonotonic": 200.0,
                    "idleGraceSeconds": 60.0,
                    "consecutiveErrors": 2,
                    "lastError": "temporary TLS timeout",
                }
            },
        )

        worker = payload["workers"][name]
        self.assertTrue(payload["ready"])
        self.assertEqual(worker["status"], "DEGRADED")
        self.assertEqual(worker["consecutiveErrors"], 2)
        self.assertEqual(worker["lastError"], "temporary TLS timeout")

    def test_intraday_preflight_summary_exposes_fresh_ready_contract(self):
        now = datetime(2026, 8, 13, 18, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
        with tempfile.TemporaryDirectory() as tmp:
            report = Path(tmp) / "readiness.json"
            report.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "status": "READY",
                        "ready": True,
                        "checkedAt": (now - timedelta(seconds=30)).isoformat(),
                        "checks": [
                            {"name": "workers", "passed": True},
                            {"name": "storage", "passed": True},
                        ],
                    }
                ),
                encoding="utf-8",
            )

            summary = BACKEND_APP._intraday_preflight_summary(
                report_path=report,
                now=now,
                configured=True,
            )

        self.assertTrue(summary["ready"])
        self.assertEqual(summary["status"], "READY")
        self.assertEqual(summary["passedChecks"], 2)
        self.assertEqual(summary["totalChecks"], 2)
        self.assertEqual(summary["failedChecks"], [])
        self.assertEqual(summary["ageSeconds"], 30)

    def test_intraday_preflight_summary_never_marks_stale_report_ready(self):
        now = datetime(2026, 8, 13, 18, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
        with tempfile.TemporaryDirectory() as tmp:
            report = Path(tmp) / "readiness.json"
            report.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "status": "READY",
                        "ready": True,
                        "checkedAt": (now - timedelta(seconds=901)).isoformat(),
                        "checks": [{"name": "workers", "passed": True}],
                    }
                ),
                encoding="utf-8",
            )

            summary = BACKEND_APP._intraday_preflight_summary(
                report_path=report,
                now=now,
                configured=True,
            )

        self.assertFalse(summary["ready"])
        self.assertEqual(summary["status"], "STALE")
        self.assertEqual(summary["ageSeconds"], 901)

    def test_intraday_preflight_summary_sanitizes_failure_and_invalid_json(self):
        now = datetime(2026, 8, 13, 18, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
        with tempfile.TemporaryDirectory() as tmp:
            report = Path(tmp) / "readiness.json"
            report.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "status": "NOT_READY",
                        "ready": False,
                        "checkedAt": now.isoformat(),
                        "checks": [
                            {
                                "name": "storage",
                                "passed": False,
                                "detail": {"path": "/secret/path"},
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            failed = BACKEND_APP._intraday_preflight_summary(
                report_path=report,
                now=now,
                configured=True,
            )
            report.write_text("not-json", encoding="ascii")
            invalid = BACKEND_APP._intraday_preflight_summary(
                report_path=report,
                now=now,
                configured=True,
            )

        self.assertEqual(failed["status"], "NOT_READY")
        self.assertEqual(failed["failedChecks"], ["storage"])
        self.assertNotIn("/secret/path", json.dumps(failed))
        self.assertEqual(invalid["status"], "INVALID")
        self.assertFalse(invalid["ready"])

    def test_intraday_preflight_summary_handles_disabled_and_missing_report(self):
        missing = Path("/path/that/does/not/exist/readiness.json")

        disabled = BACKEND_APP._intraday_preflight_summary(
            report_path=missing,
            configured=False,
        )
        starting = BACKEND_APP._intraday_preflight_summary(
            report_path=missing,
            configured=True,
        )

        self.assertEqual(disabled["status"], "DISABLED")
        self.assertEqual(starting["status"], "STARTING")
        self.assertFalse(disabled["ready"])
        self.assertFalse(starting["ready"])

    def test_active_intraday_calibration_summary_is_refreshed(self):
        self.assertTrue(
            BACKEND_APP._intraday_calibration_refresh_required(
                enabled=True,
                calibration={
                    "tradeDate": "2026-08-13",
                    "status": "IN_PROGRESS",
                    "finalized": False,
                },
                configured_trade_date="2026-08-13",
            )
        )

    def test_finalized_intraday_calibration_is_not_recomputed(self):
        self.assertFalse(
            BACKEND_APP._intraday_calibration_refresh_required(
                enabled=True,
                calibration={
                    "tradeDate": "2026-08-13",
                    "status": "PASS",
                    "finalized": True,
                },
                configured_trade_date="2026-08-13",
            )
        )
        self.assertTrue(
            BACKEND_APP._intraday_calibration_refresh_required(
                enabled=True,
                calibration={
                    "tradeDate": "2026-08-12",
                    "status": "FAIL",
                    "finalized": True,
                },
                configured_trade_date="2026-08-13",
            )
        )

    def test_intraday_shadow_alerts_are_emitted_only_on_relevant_edges(self):
        self.assertEqual(
            BACKEND_APP._intraday_shadow_alert_events(
                "STAGED",
                cycle_status="DECISION_FROZEN_AWAITING_EXECUTION",
                actions=["DECISION_FROZEN:2026-08-14"],
            ),
            ["INTRADAY_DECISION_INTENT_FROZEN"],
        )
        self.assertEqual(
            BACKEND_APP._intraday_shadow_alert_events(
                "DECISION_FROZEN_AWAITING_EXECUTION",
                cycle_status="AWAITING_OFFICIAL_DAILY",
                actions=["EXECUTION_BOUND:2026-08-14"],
            ),
            ["INTRADAY_EXECUTION_PRICE_BOUND"],
        )
        self.assertEqual(
            BACKEND_APP._intraday_shadow_alert_events(
                None,
                cycle_status="AWAITING_OFFICIAL_DAILY",
                actions=["FROZEN:2026-08-14"],
            ),
            ["INTRADAY_SIGNAL_FROZEN"],
        )
        self.assertEqual(
            BACKEND_APP._intraday_shadow_alert_events(
                "AWAITING_OFFICIAL_DAILY",
                cycle_status="ACTIVE",
                actions=["FINALIZED:2026-08-14"],
            ),
            ["INTRADAY_SESSION_FINALIZED"],
        )
        self.assertEqual(
            BACKEND_APP._intraday_shadow_alert_events(
                "ACTIVE",
                cycle_status="MISSED_SESSION",
            ),
            ["INTRADAY_SESSION_MISSED"],
        )
        self.assertEqual(
            BACKEND_APP._intraday_shadow_alert_events(
                "ACTIVE",
                cycle_status="RISK_STOPPED",
            ),
            ["INTRADAY_RISK_STOPPED"],
        )
        self.assertEqual(
            BACKEND_APP._intraday_shadow_alert_events("ACTIVE", failed=True),
            ["INTRADAY_SHADOW_ERROR"],
        )
        self.assertEqual(
            BACKEND_APP._intraday_shadow_alert_events("ERROR", failed=True),
            [],
        )
        self.assertEqual(
            BACKEND_APP._intraday_shadow_alert_events(
                "ERROR",
                cycle_status="STAGED",
            ),
            ["INTRADAY_SHADOW_RECOVERED"],
        )

    def test_fixed_runtime_alert_uses_only_the_static_event_contract(self):
        completed = subprocess.CompletedProcess([], 0, "", "")
        with patch.object(
            BACKEND_APP.subprocess,
            "run",
            return_value=completed,
        ) as run:
            BACKEND_APP._emit_fixed_runtime_alert("INTRADAY_SIGNAL_FROZEN")

        command = run.call_args.args[0]
        rendered = " ".join(command)
        self.assertEqual(command[2], "INTRADAY_SIGNAL_FROZEN")
        self.assertIn("research_shadow_supervisor_alert.py", command[1])
        self.assertNotIn("TOKEN", rendered)
        self.assertNotIn("API_KEY", rendered)
        self.assertFalse(run.call_args.kwargs["check"])
        self.assertTrue(run.call_args.kwargs["capture_output"])
        self.assertEqual(run.call_args.kwargs["timeout"], 10)

    def test_intraday_capture_pins_provider_to_first_verified_daily_slot(self):
        config = {
            "providerPath": "/providers/current",
            "outputRoot": "/captures",
        }
        now = datetime(2026, 8, 14, 10, tzinfo=ZoneInfo("Asia/Shanghai"))
        with patch(
            "market_data.intraday_capture.list_verified_slot_records",
            return_value=[],
        ):
            self.assertEqual(
                BACKEND_APP._intraday_capture_effective_provider_path(
                    config,
                    now=now,
                ),
                "/providers/current",
            )

        with patch(
            "market_data.intraday_capture.list_verified_slot_records",
            return_value=[
                (
                    Path("slot.json"),
                    {"binding": {"providerPath": "/providers/first-slot"}},
                )
            ],
        ):
            self.assertEqual(
                BACKEND_APP._intraday_capture_effective_provider_path(
                    {**config, "providerPath": "/providers/new-release"},
                    now=now,
                ),
                "/providers/first-slot",
            )

    def test_intraday_capture_config_prefers_runtime_switched_dotenv_provider(self):
        with (
            patch.object(
                BACKEND_APP,
                "_load_dotenv_dict",
                return_value={
                    "QUANTAALPHA_INTRADAY_CAPTURE_ENABLED": "true",
                    "QLIB_DATA_DIR": "/providers/runtime-switched",
                },
            ),
            patch.dict(
                BACKEND_APP.os.environ,
                {"QLIB_DATA_DIR": "/providers/inherited-stale"},
            ),
        ):
            config = BACKEND_APP._intraday_capture_config()

        self.assertEqual(config["providerPath"], "/providers/runtime-switched")

    def test_intraday_capture_config_reloads_provider_from_dotenv_each_cycle(self):
        with tempfile.TemporaryDirectory() as tmp:
            dotenv = Path(tmp) / ".env"
            with (
                patch.object(BACKEND_APP, "DOTENV_PATH", dotenv),
                patch.dict(
                    BACKEND_APP.os.environ,
                    {"QLIB_DATA_DIR": "/providers/inherited-stale"},
                ),
            ):
                dotenv.write_text(
                    "QLIB_DATA_DIR=/providers/first\n",
                    encoding="utf-8",
                )
                first = BACKEND_APP._intraday_capture_config()
                dotenv.write_text(
                    "QLIB_DATA_DIR=/providers/second\n",
                    encoding="utf-8",
                )
                second = BACKEND_APP._intraday_capture_config()

        self.assertEqual(first["providerPath"], "/providers/first")
        self.assertEqual(second["providerPath"], "/providers/second")

    def test_intraday_capture_alerts_are_edge_triggered(self):
        healthy = {"status": "CAPTURED"}
        failed = {"status": "ERROR"}
        self.assertEqual(
            BACKEND_APP._intraday_capture_alert_events(healthy, failed),
            ["INTRADAY_CAPTURE_ERROR"],
        )
        self.assertEqual(
            BACKEND_APP._intraday_capture_alert_events(failed, failed),
            [],
        )
        self.assertEqual(
            BACKEND_APP._intraday_capture_alert_events(failed, healthy),
            ["INTRADAY_CAPTURE_RECOVERED"],
        )
        self.assertEqual(
            BACKEND_APP._intraday_capture_alert_events(healthy, failed=True),
            ["INTRADAY_CAPTURE_ERROR"],
        )
        self.assertEqual(
            BACKEND_APP._intraday_capture_alert_events(failed, failed=True),
            [],
        )

    def test_intraday_temporal_close_alert_only_accepts_degraded_iso_day(self):
        self.assertEqual(
            BACKEND_APP._intraday_temporal_close_alert(
                {
                    "status": "DEGRADED",
                    "tradeDate": "2026-08-14",
                    "expectedSlotCount": 242,
                    "dueSlotCount": 242,
                }
            ),
            ("INTRADAY_TEMPORAL_DEGRADED", "2026-08-14"),
        )
        self.assertIsNone(
            BACKEND_APP._intraday_temporal_close_alert(
                {"status": "COMPLETE", "tradeDate": "2026-08-14"}
            )
        )
        self.assertIsNone(
            BACKEND_APP._intraday_temporal_close_alert(
                {
                    "status": "DEGRADED",
                    "tradeDate": "not-a-date",
                    "expectedSlotCount": 242,
                    "dueSlotCount": 242,
                }
            )
        )
        self.assertIsNone(
            BACKEND_APP._intraday_temporal_close_alert(
                {
                    "status": "DEGRADED",
                    "tradeDate": "2026-08-14",
                    "expectedSlotCount": 242,
                    "dueSlotCount": 121,
                }
            )
        )

    def test_fixed_runtime_alert_passes_validated_subject_date(self):
        completed = subprocess.CompletedProcess([], 0, "", "")
        with patch.object(
            BACKEND_APP.subprocess,
            "run",
            return_value=completed,
        ) as run:
            BACKEND_APP._emit_fixed_runtime_alert(
                "INTRADAY_TEMPORAL_DEGRADED",
                subject_date="2026-08-14",
            )

        command = run.call_args.args[0]
        self.assertEqual(
            command[-2:],
            ["--subject-date", "2026-08-14"],
        )

    def test_intraday_shadow_config_prefers_dotenv_declaration(self):
        with (
            patch.object(
                BACKEND_APP,
                "_load_dotenv_dict",
                return_value={
                    "QUANTAALPHA_INTRADAY_SHADOW_DECLARATION": (
                        "/declarations/quality-gated-v3.json"
                    )
                },
            ),
            patch.dict(
                BACKEND_APP.os.environ,
                {
                    "QUANTAALPHA_INTRADAY_SHADOW_DECLARATION": (
                        "/declarations/inherited-v1.json"
                    )
                },
            ),
        ):
            config = BACKEND_APP._intraday_shadow_config()

        self.assertEqual(
            config["declaration"],
            "/declarations/quality-gated-v3.json",
        )

    def test_official_provider_alerts_are_edge_triggered(self):
        on_time = {
            "status": "NO_NEW_SESSION",
            "result": {
                "providerUpdate": {
                    "officialDataFreshness": {"status": "ON_TIME"}
                }
            },
        }
        overdue = {
            "status": "NO_NEW_SESSION",
            "result": {
                "providerUpdate": {
                    "officialDataFreshness": {"status": "OVERDUE"}
                }
            },
        }
        self.assertEqual(
            BACKEND_APP._research_shadow_alert_events(on_time, overdue),
            ["OFFICIAL_PROVIDER_OVERDUE"],
        )
        self.assertEqual(
            BACKEND_APP._research_shadow_alert_events(overdue, overdue),
            [],
        )
        self.assertEqual(
            BACKEND_APP._research_shadow_alert_events(overdue, on_time),
            ["OFFICIAL_PROVIDER_FRESHNESS_RECOVERED"],
        )
        self.assertEqual(
            BACKEND_APP._research_shadow_alert_events(on_time, failed=True),
            ["OFFICIAL_PROVIDER_UPDATE_ERROR"],
        )
        self.assertEqual(
            BACKEND_APP._research_shadow_alert_events(
                {"status": "ERROR"},
                failed=True,
            ),
            [],
        )
        self.assertEqual(
            BACKEND_APP._research_shadow_alert_events(
                {"status": "ERROR"},
                on_time,
            ),
            ["OFFICIAL_PROVIDER_UPDATE_RECOVERED"],
        )
        review = {
            "status": "REVIEW_REQUIRED",
            "result": {
                "providerUpdate": {
                    "officialDataFreshness": {"status": "ON_TIME"}
                }
            },
        }
        self.assertEqual(
            BACKEND_APP._research_shadow_alert_events(on_time, review),
            ["RESEARCH_SHADOW_REVIEW_REQUIRED"],
        )
        self.assertEqual(
            BACKEND_APP._research_shadow_alert_events(review, review),
            [],
        )


if __name__ == "__main__":
    unittest.main()
