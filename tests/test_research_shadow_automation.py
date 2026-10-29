import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from quantaalpha.trading.research_shadow_automation import (
    advance_official_provider,
    assess_official_data_freshness,
    read_automation_status,
    run_cycle_and_record,
    run_daily_cycle,
)


def _state(status: str, next_action: str) -> dict:
    return {
        "status": status,
        "nextAction": next_action,
        "marketData": {"latestDate": "2026-08-11"},
        "audit": {"valid": True, "events": 1},
        "arms": {"C1": {"pendingSignal": {"signalDate": "2026-08-11"}}},
    }


class _Coordinator:
    def __init__(self, initial: dict):
        self.state = initial
        self.executions = 0
        self.generations = 0

    def get_state(self):
        return self.state

    def execute_ready_pair(self):
        self.executions += 1
        self.state = _state("READY_FOR_CLOSE_SIGNAL", "GENERATE_LATEST_CLOSE_SIGNAL")
        return self.state

    def generate_latest_signal(self, now=None):
        self.generations += 1
        self.state = _state("WAITING_OFFICIAL_DAILY", "WAIT_FOR_NEXT_OFFICIAL_PROVIDER")
        return self.state


class _StoppingCoordinator(_Coordinator):
    def execute_ready_pair(self):
        self.executions += 1
        self.state = _state("RISK_STOPPED", "REVIEW_RISK_STOP")
        return self.state


class ResearchShadowAutomationTests(unittest.TestCase):
    def test_same_release_tag_skips_archive_download(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            declaration = root / "declaration.json"
            declaration.write_text(
                json.dumps(
                    {
                        "runId": "shadow-v1",
                        "usageRealm": "RESEARCH_SHADOW",
                        "formalPaperEligible": False,
                        "providerFingerprint": "a" * 64,
                        "providerPath": str(root / "parent"),
                        "providerLineageProofDir": str(root / "proofs"),
                        "databasePath": str(root / "run" / "shadow.sqlite3"),
                    }
                ),
                encoding="utf-8",
            )
            parent = root / "parent"
            with (
                patch(
                    "quantaalpha.trading.research_shadow_automation.discover_latest_provider",
                    return_value=parent,
                ),
                patch(
                    "quantaalpha.trading.research_shadow_automation.calendar_bounds",
                    return_value=("2000-01-04", "2026-08-11", 6447),
                ),
                patch(
                    "quantaalpha.trading.research_shadow_automation.query_official_release_manifest",
                    return_value={
                        "release_tag": "2026-08-11",
                        "target_trade_date": "2026-08-11",
                    },
                ) as query,
                patch(
                    "quantaalpha.trading.research_shadow_automation.download_official_release"
                ) as download,
            ):
                result = advance_official_provider(declaration)

            self.assertEqual(result["status"], "NO_NEW_SESSION")
            self.assertFalse(result["runtimeSwitched"])
            query.assert_called_once_with(None)
            download.assert_not_called()

    def test_weekend_release_target_skips_archive_download(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            declaration = root / "declaration.json"
            declaration.write_text(
                json.dumps(
                    {
                        "runId": "shadow-v1",
                        "usageRealm": "RESEARCH_SHADOW",
                        "formalPaperEligible": False,
                        "providerFingerprint": "a" * 64,
                        "providerPath": str(root / "parent"),
                        "providerLineageProofDir": str(root / "proofs"),
                        "databasePath": str(root / "run" / "shadow.sqlite3"),
                    }
                ),
                encoding="utf-8",
            )
            parent = root / "parent"
            with (
                patch(
                    "quantaalpha.trading.research_shadow_automation.discover_latest_provider",
                    return_value=parent,
                ),
                patch(
                    "quantaalpha.trading.research_shadow_automation.calendar_bounds",
                    return_value=("2000-01-04", "2026-08-07", 6445),
                ),
                patch(
                    "quantaalpha.trading.research_shadow_automation.query_official_release_manifest",
                    return_value={
                        "release_tag": "2026-08-09",
                        "target_trade_date": "2026-08-07",
                    },
                ),
                patch(
                    "quantaalpha.trading.research_shadow_automation.download_official_release"
                ) as download,
            ):
                result = advance_official_provider(declaration)

            self.assertEqual(result["status"], "NO_NEW_SESSION")
            self.assertEqual(result["officialRelease"], "2026-08-09")
            self.assertEqual(result["officialTargetDate"], "2026-08-07")
            download.assert_not_called()

    def test_freshness_uses_future_sessions_and_next_day_grace(self):
        with tempfile.TemporaryDirectory() as tmp:
            provider = Path(tmp) / "provider"
            calendar = provider / "calendars" / "day_future.txt"
            calendar.parent.mkdir(parents=True)
            calendar.write_text(
                "2026-08-07\n2026-08-10\n2026-08-11\n2026-08-12\n",
                encoding="utf-8",
            )
            tz = ZoneInfo("Asia/Shanghai")
            sunday = assess_official_data_freshness(
                provider,
                "2026-08-07",
                now=datetime(2026, 8, 9, 23, 59, tzinfo=tz),
            )
            overdue = assess_official_data_freshness(
                provider,
                "2026-08-07",
                now=datetime(2026, 8, 11, 0, 31, tzinfo=tz),
            )

            self.assertEqual(sunday["status"], "ON_TIME")
            self.assertEqual(sunday["expectedNextSession"], "2026-08-10")
            self.assertEqual(sunday["releaseDueAt"], "2026-08-11T00:30:00+08:00")
            self.assertEqual(sunday["overdueSessions"], 0)
            self.assertEqual(overdue["status"], "OVERDUE")
            self.assertEqual(overdue["overdueSessions"], 1)

    def test_freshness_reports_missing_future_calendar_without_raising(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = assess_official_data_freshness(
                Path(tmp) / "provider",
                "2026-08-11",
            )

        self.assertEqual(result["status"], "CALENDAR_UNAVAILABLE")
        self.assertFalse(result["monitoringAvailable"])

    def test_no_new_release_does_not_mutate_shadow_actions(self):
        coordinator = _Coordinator(
            _state("WAITING_OFFICIAL_DAILY", "WAIT_FOR_NEXT_OFFICIAL_PROVIDER")
        )

        result = run_daily_cycle(
            Path("declaration.json"),
            coordinator_factory=lambda _: coordinator,
            advance_provider=lambda *args, **kwargs: {
                "status": "NO_NEW_SESSION",
                "runtimeSwitched": False,
            },
        )

        self.assertEqual(result["status"], "NO_NEW_SESSION")
        self.assertEqual(result["actions"], [])
        self.assertEqual(coordinator.executions, 0)
        self.assertEqual(coordinator.generations, 0)

    def test_new_release_executes_then_freezes_close_signal_once(self):
        waiting = _Coordinator(
            _state("WAITING_OFFICIAL_DAILY", "WAIT_FOR_NEXT_OFFICIAL_PROVIDER")
        )
        ready = _Coordinator(_state("READY_TO_EXECUTE", "EXECUTE_NEXT_OPEN"))
        coordinators = iter([waiting, ready])

        result = run_daily_cycle(
            Path("declaration.json"),
            coordinator_factory=lambda _: next(coordinators),
            advance_provider=lambda *args, **kwargs: {
                "status": "ADVANCED",
                "runtimeSwitched": True,
            },
        )

        self.assertEqual(result["status"], "ADVANCED_AND_PROCESSED")
        self.assertEqual(
            result["actions"],
            ["PAIRED_NEXT_OPEN_EXECUTED", "PAIRED_CLOSE_SIGNAL_FROZEN"],
        )
        self.assertEqual(ready.executions, 1)
        self.assertEqual(ready.generations, 1)

    def test_review_required_never_advances_provider(self):
        coordinator = _Coordinator(
            _state("MISSED_EXECUTION", "START_NEW_DECLARED_RUN")
        )
        calls = []

        result = run_daily_cycle(
            Path("declaration.json"),
            coordinator_factory=lambda _: coordinator,
            advance_provider=lambda *args, **kwargs: calls.append(True),
        )

        self.assertEqual(result["status"], "REVIEW_REQUIRED")
        self.assertEqual(calls, [])

    def test_risk_stop_keeps_data_probe_but_blocks_ledger_actions(self):
        coordinator = _Coordinator(_state("RISK_STOPPED", "REVIEW_RISK_STOP"))
        calls = []

        result = run_daily_cycle(
            Path("declaration.json"),
            coordinator_factory=lambda _: coordinator,
            advance_provider=lambda *args, **kwargs: calls.append(True)
            or {
                "status": "NO_NEW_SESSION",
                "runtimeSwitched": False,
            },
        )

        self.assertEqual(result["status"], "REVIEW_REQUIRED")
        self.assertEqual(result["shadowStatus"], "RISK_STOPPED")
        self.assertEqual(result["actions"], [])
        self.assertEqual(calls, [True])

    def test_new_session_drawdown_stop_does_not_freeze_another_signal(self):
        waiting = _Coordinator(
            _state("WAITING_OFFICIAL_DAILY", "WAIT_FOR_NEXT_OFFICIAL_PROVIDER")
        )
        stopped = _StoppingCoordinator(
            _state("READY_TO_EXECUTE", "EXECUTE_NEXT_OPEN")
        )
        coordinators = iter([waiting, stopped])

        result = run_daily_cycle(
            Path("declaration.json"),
            coordinator_factory=lambda _: next(coordinators),
            advance_provider=lambda *args, **kwargs: {
                "status": "ADVANCED",
                "runtimeSwitched": True,
            },
        )

        self.assertEqual(result["status"], "REVIEW_REQUIRED")
        self.assertEqual(result["shadowStatus"], "RISK_STOPPED")
        self.assertEqual(result["actions"], ["PAIRED_NEXT_OPEN_EXECUTED"])
        self.assertEqual(stopped.executions, 1)
        self.assertEqual(stopped.generations, 0)

    def test_multi_session_successor_is_rejected_before_runtime_switch(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            declaration = root / "declaration.json"
            declaration.write_text(
                json.dumps(
                    {
                        "runId": "shadow-v1",
                        "usageRealm": "RESEARCH_SHADOW",
                        "formalPaperEligible": False,
                        "providerFingerprint": "a" * 64,
                        "providerPath": str(root / "parent"),
                        "providerLineageProofDir": str(root / "proofs"),
                        "databasePath": str(root / "shadow.sqlite3"),
                        "initialSignalDate": "2026-08-11",
                        "firstTradeDate": "2026-08-12",
                    }
                ),
                encoding="utf-8",
            )
            parent = root / "parent"
            successor = root / "successor"
            source = root / "release"
            source.mkdir()
            proof = {
                "appendedSessions": 2,
                "firstAppendedDate": "2026-08-12",
                "successorLatestDate": "2026-08-13",
            }
            with (
                patch(
                    "quantaalpha.trading.research_shadow_automation.discover_latest_provider",
                    return_value=parent,
                ),
                patch(
                    "quantaalpha.trading.research_shadow_automation.calendar_bounds",
                    return_value=("2000-01-04", "2026-08-11", 6447),
                ),
                patch(
                    "quantaalpha.trading.research_shadow_automation.query_official_release_manifest",
                    return_value={
                        "release_tag": "2026-08-13",
                        "target_trade_date": "2026-08-13",
                    },
                ),
                patch(
                    "quantaalpha.trading.research_shadow_automation.download_official_release",
                    return_value=source,
                ),
                patch(
                    "quantaalpha.trading.research_shadow_automation.load_release_manifest",
                    return_value={
                        "release_tag": "2026-08-13",
                        "target_trade_date": "2026-08-13",
                    },
                ),
                patch(
                    "quantaalpha.trading.research_shadow_automation.install_release_provider",
                    return_value={"status": "INSTALLED"},
                ),
                patch(
                    "quantaalpha.trading.research_shadow_automation.build_research_shadow_lineage_proof",
                    return_value=(proof, root / "proofs" / "successor.json"),
                ),
                patch(
                    "quantaalpha.trading.research_shadow_automation.switch_runtime_provider"
                ) as switch,
            ):
                with self.assertRaisesRegex(ValueError, "skip more than one session"):
                    advance_official_provider(
                        declaration,
                        active_state=_state(
                            "WAITING_OFFICIAL_DAILY",
                            "WAIT_FOR_NEXT_OFFICIAL_PROVIDER",
                        ),
                    )
            switch.assert_not_called()

    def test_cycle_error_is_recorded_without_creating_a_ledger(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            declaration = root / "declaration.json"
            database = root / "run" / "shadow.sqlite3"
            declaration.write_text(
                json.dumps(
                    {
                        "runId": "shadow-v1",
                        "usageRealm": "RESEARCH_SHADOW",
                        "formalPaperEligible": False,
                        "databasePath": str(database),
                    }
                ),
                encoding="utf-8",
            )
            with patch(
                "quantaalpha.trading.research_shadow_automation.run_daily_cycle",
                side_effect=RuntimeError("offline"),
            ):
                with self.assertRaisesRegex(RuntimeError, "offline"):
                    run_cycle_and_record(declaration, poll_seconds=900)

            status = read_automation_status(declaration)
            self.assertEqual(status["status"], "ERROR")
            self.assertEqual(status["consecutiveErrors"], 1)
            self.assertEqual(status["lastError"], "offline")
            self.assertFalse(database.exists())

    def test_cycle_records_the_latest_evidence_checkpoint(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            database = root / "run" / "shadow.sqlite3"
            database.parent.mkdir(parents=True)
            database.write_bytes(b"ledger")
            declaration = root / "declaration.json"
            declaration.write_text(
                json.dumps(
                    {
                        "runId": "shadow-v1",
                        "usageRealm": "RESEARCH_SHADOW",
                        "formalPaperEligible": False,
                        "databasePath": str(database),
                    }
                ),
                encoding="utf-8",
            )
            checkpoints = [
                {"valid": True, "status": "CREATED", "count": 1},
                {"valid": True, "status": "REUSED", "count": 1},
            ]
            with (
                patch(
                    "quantaalpha.trading.research_shadow_automation.ensure_evidence_checkpoint",
                    side_effect=checkpoints,
                ) as ensure,
                patch(
                    "quantaalpha.trading.research_shadow_automation.run_daily_cycle",
                    return_value={"status": "NO_NEW_SESSION", "actions": []},
                ),
            ):
                status = run_cycle_and_record(declaration, poll_seconds=900)

            self.assertEqual(ensure.call_count, 2)
            self.assertEqual(status["evidenceCheckpoint"]["status"], "REUSED")
            self.assertEqual(status["evidenceCheckpoint"]["count"], 1)


if __name__ == "__main__":
    unittest.main()
