import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from quantaalpha.trading.unattended_provider_automation import (
    _query_official_manifest_with_bounded_retry,
    advance_official_provider,
    run_cycle_and_record,
)


CHINA_TZ = ZoneInfo("Asia/Shanghai")


def _declaration(root: Path) -> Path:
    path = root / "declaration.json"
    path.write_text(
        json.dumps(
            {
                "runId": "shadow-v1",
                "usageRealm": "RESEARCH_SHADOW",
                "formalPaperEligible": False,
                "providerFingerprint": "a" * 64,
                "providerPath": str(root / "parent"),
                "providerLineageProofDir": str(root / "proofs"),
                "databasePath": str(root / "run" / "shadow.sqlite3"),
                "initialSignalDate": "2026-08-11",
                "firstTradeDate": "2026-08-12",
            }
        ),
        encoding="utf-8",
    )
    return path


def _state(status: str, next_action: str | None = None) -> dict:
    return {
        "status": status,
        "nextAction": next_action
        or (
            "WAIT_FOR_NEXT_OFFICIAL_PROVIDER"
            if status == "WAITING_OFFICIAL_DAILY"
            else "START_NEW_DECLARED_RUN"
        ),
        "marketData": {"latestDate": "2026-08-11"},
        "audit": {"valid": True, "events": 1},
        "arms": {"C1": {"pendingSignal": {"signalDate": "2026-08-11"}}},
    }


class _Coordinator:
    def __init__(self, state: dict):
        self.state = state
        self.executions = 0
        self.generations = 0

    def get_state(self) -> dict:
        return self.state

    def execute_ready_pair(self) -> dict:
        self.executions += 1
        self.state = _state(
            "READY_FOR_CLOSE_SIGNAL",
            "GENERATE_LATEST_CLOSE_SIGNAL",
        )
        return self.state

    def generate_latest_signal(self, now=None) -> dict:
        self.generations += 1
        self.state = _state(
            "WAITING_OFFICIAL_DAILY",
            "WAIT_FOR_NEXT_OFFICIAL_PROVIDER",
        )
        return self.state


class UnattendedProviderAutomationTests(unittest.TestCase):
    def test_metadata_query_retries_one_read_only_batch_after_network_failure(self):
        manifest = {
            "release_tag": "2026-08-13",
            "target_trade_date": "2026-08-13",
        }
        with (
            patch(
                "quantaalpha.trading.unattended_provider_automation.query_official_release_manifest",
                side_effect=[
                    RuntimeError(
                        "failed to query official release metadata after 3 attempt(s): EOF"
                    ),
                    manifest,
                ],
            ) as query,
            patch(
                "quantaalpha.trading.unattended_provider_automation.time.sleep"
            ) as sleep,
        ):
            result = _query_official_manifest_with_bounded_retry(None)

        self.assertIs(result, manifest)
        self.assertEqual(query.call_count, 2)
        sleep.assert_called_once_with(15.0)

    def test_metadata_query_does_not_retry_contract_failures(self):
        with (
            patch(
                "quantaalpha.trading.unattended_provider_automation.query_official_release_manifest",
                side_effect=RuntimeError("official release is missing its manifest"),
            ) as query,
            patch(
                "quantaalpha.trading.unattended_provider_automation.time.sleep"
            ) as sleep,
        ):
            with self.assertRaisesRegex(RuntimeError, "missing its manifest"):
                _query_official_manifest_with_bounded_retry(None)

        query.assert_called_once_with(None)
        sleep.assert_not_called()

    def test_multi_session_provider_advances_without_enabling_ledger_backfill(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            declaration = _declaration(root)
            parent = root / "parent"
            source = root / "release"
            source.mkdir()
            proof = {
                "appendedSessions": 2,
                "firstAppendedDate": "2026-08-12",
                "successorLatestDate": "2026-08-13",
            }
            with (
                patch(
                    "quantaalpha.trading.unattended_provider_automation.discover_latest_provider",
                    return_value=parent,
                ),
                patch(
                    "quantaalpha.trading.unattended_provider_automation.calendar_bounds",
                    return_value=("2000-01-04", "2026-08-11", 6447),
                ),
                patch(
                    "quantaalpha.trading.unattended_provider_automation.query_official_release_manifest",
                    return_value={
                        "release_tag": "2026-08-13",
                        "target_trade_date": "2026-08-13",
                    },
                ),
                patch(
                    "quantaalpha.trading.unattended_provider_automation.download_official_release",
                    return_value=source,
                ),
                patch(
                    "quantaalpha.trading.unattended_provider_automation.load_release_manifest",
                    return_value={
                        "release_tag": "2026-08-13",
                        "target_trade_date": "2026-08-13",
                    },
                ),
                patch(
                    "quantaalpha.trading.unattended_provider_automation.install_release_provider",
                    return_value={"status": "INSTALLED"},
                ),
                patch(
                    "quantaalpha.trading.unattended_provider_automation.frozen_automation.build_research_shadow_lineage_proof",
                    return_value=(proof, root / "proofs" / "successor.json"),
                ),
                patch(
                    "quantaalpha.trading.unattended_provider_automation.frozen_automation._reuse_or_write_proof",
                    return_value={"proofStatus": "CREATED"},
                ),
                patch(
                    "quantaalpha.trading.unattended_provider_automation.frozen_automation.assess_official_data_freshness",
                    return_value={"status": "ON_TIME"},
                ),
                patch(
                    "quantaalpha.trading.unattended_provider_automation.switch_runtime_provider",
                    return_value={"providerPath": "/providers/successor"},
                ) as switch,
            ):
                result = advance_official_provider(
                    declaration,
                    active_state=_state("WAITING_OFFICIAL_DAILY"),
                )

        self.assertEqual(result["status"], "ADVANCED")
        self.assertEqual(result["appendedSessions"], 2)
        self.assertEqual(
            result["recoveryMode"], "MULTI_SESSION_NO_LEDGER_BACKFILL"
        )
        self.assertFalse(result["ledgerBackfillAllowed"])
        switch.assert_called_once()

    def test_verified_release_advances_when_history_drift_invalidates_old_shadow(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            declaration = _declaration(root)
            parent = root / "parent"
            successor = (
                root
                / "data"
                / "market"
                / "a_share"
                / "qlib"
                / "investment_data_20260813_cn"
            ).resolve()
            source = root / "release"
            source.mkdir()
            with (
                patch(
                    "quantaalpha.trading.unattended_provider_automation.discover_latest_provider",
                    return_value=parent,
                ),
                patch(
                    "quantaalpha.trading.unattended_provider_automation.calendar_bounds",
                    side_effect=[
                        ("2000-01-04", "2026-08-11", 6447),
                        ("2000-01-04", "2026-08-13", 6449),
                    ],
                ),
                patch(
                    "quantaalpha.trading.unattended_provider_automation._query_official_manifest_with_bounded_retry",
                    return_value={
                        "release_tag": "2026-08-13",
                        "target_trade_date": "2026-08-13",
                    },
                ),
                patch(
                    "quantaalpha.trading.unattended_provider_automation.download_official_release",
                    return_value=source,
                ),
                patch(
                    "quantaalpha.trading.unattended_provider_automation.load_release_manifest",
                    return_value={
                        "release_tag": "2026-08-13",
                        "target_trade_date": "2026-08-13",
                    },
                ),
                patch(
                    "quantaalpha.trading.unattended_provider_automation.frozen_automation.PROJECT_ROOT",
                    root,
                ),
                patch(
                    "quantaalpha.trading.unattended_provider_automation.install_release_provider",
                    return_value={"status": "REUSED"},
                ) as install,
                patch(
                    "quantaalpha.trading.unattended_provider_automation.frozen_automation.build_research_shadow_lineage_proof",
                    side_effect=ValueError(
                        "Provider feature history changed at features/sz000016/factor.day.bin"
                    ),
                ),
                patch(
                    "quantaalpha.trading.unattended_provider_automation.frozen_automation.assess_official_data_freshness",
                    return_value={"status": "ON_TIME"},
                ),
                patch(
                    "quantaalpha.trading.unattended_provider_automation.switch_runtime_provider",
                    return_value={"providerPath": str(successor)},
                ) as switch,
            ):
                result = advance_official_provider(
                    declaration,
                    active_state=_state("WAITING_OFFICIAL_DAILY"),
                )

        self.assertEqual(result["status"], "ADVANCED_WITH_HISTORY_DRIFT")
        self.assertEqual(result["appendedSessions"], 2)
        self.assertEqual(
            result["recoveryMode"], "HISTORY_DRIFT_NO_LEDGER_BACKFILL"
        )
        self.assertEqual(result["historyDrift"]["kind"], "FEATURE_HISTORY_CHANGED")
        self.assertTrue(result["providerHistoryInvalidated"])
        self.assertFalse(result["ledgerBackfillAllowed"])
        install.assert_called_once_with(source, successor)
        switch.assert_called_once_with(successor)

    def test_non_history_lineage_error_still_blocks_provider_switch(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            declaration = _declaration(root)
            parent = root / "parent"
            source = root / "release"
            source.mkdir()
            with (
                patch(
                    "quantaalpha.trading.unattended_provider_automation.discover_latest_provider",
                    return_value=parent,
                ),
                patch(
                    "quantaalpha.trading.unattended_provider_automation.calendar_bounds",
                    return_value=("2000-01-04", "2026-08-11", 6447),
                ),
                patch(
                    "quantaalpha.trading.unattended_provider_automation._query_official_manifest_with_bounded_retry",
                    return_value={
                        "release_tag": "2026-08-13",
                        "target_trade_date": "2026-08-13",
                    },
                ),
                patch(
                    "quantaalpha.trading.unattended_provider_automation.download_official_release",
                    return_value=source,
                ),
                patch(
                    "quantaalpha.trading.unattended_provider_automation.load_release_manifest",
                    return_value={
                        "release_tag": "2026-08-13",
                        "target_trade_date": "2026-08-13",
                    },
                ),
                patch(
                    "quantaalpha.trading.unattended_provider_automation.install_release_provider",
                    return_value={"status": "INSTALLED"},
                ),
                patch(
                    "quantaalpha.trading.unattended_provider_automation.frozen_automation.build_research_shadow_lineage_proof",
                    side_effect=ValueError("Provider lineage proof path is invalid"),
                ),
                patch(
                    "quantaalpha.trading.unattended_provider_automation.switch_runtime_provider"
                ) as switch,
            ):
                with self.assertRaisesRegex(ValueError, "proof path is invalid"):
                    advance_official_provider(
                        declaration,
                        active_state=_state("WAITING_OFFICIAL_DAILY"),
                    )

        switch.assert_not_called()

    def test_multi_session_cycle_skips_all_shadow_ledger_actions(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            declaration = _declaration(root)
            database = root / "run" / "shadow.sqlite3"
            database.parent.mkdir(parents=True)
            database.write_bytes(b"ledger")
            states = iter(
                [
                    _Coordinator(_state("WAITING_OFFICIAL_DAILY")),
                    _Coordinator(_state("MISSED_EXECUTION")),
                ]
            )
            update = {
                "status": "ADVANCED",
                "appendedSessions": 2,
                "runtimeSwitched": True,
                "officialDataFreshness": {"status": "ON_TIME"},
            }
            with (
                patch(
                    "quantaalpha.trading.unattended_provider_automation.advance_official_provider",
                    return_value=update,
                ),
                patch(
                    "quantaalpha.trading.unattended_provider_automation.ensure_evidence_checkpoint",
                    side_effect=[{"count": 1}, {"count": 1}],
                ),
            ):
                result = run_cycle_and_record(
                    declaration,
                    poll_seconds=900,
                    now=datetime(2026, 8, 14, 23, tzinfo=CHINA_TZ),
                    coordinator_factory=lambda _: next(states),
                )

            recorded = json.loads(
                (database.parent / "automation_status.json").read_text(
                    encoding="utf-8"
                )
            )

        self.assertEqual(result["status"], "REVIEW_REQUIRED")
        self.assertEqual(result["result"]["shadowStatus"], "MISSED_EXECUTION")
        self.assertEqual(result["result"]["actions"], [])
        self.assertEqual(
            result["result"]["providerRecoveryReason"],
            "MULTI_SESSION_PROVIDER_RECOVERY_NO_LEDGER_BACKFILL",
        )
        self.assertEqual(recorded, result)

    def test_history_invalidated_cycle_enters_persistent_provider_only_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            declaration = _declaration(root)
            database = root / "run" / "shadow.sqlite3"
            database.parent.mkdir(parents=True)
            database.write_bytes(b"ledger")
            coordinator = _Coordinator(_state("WAITING_OFFICIAL_DAILY"))
            checkpoint = {"count": 1, "status": "REUSED"}
            update = {
                "status": "ADVANCED_WITH_HISTORY_DRIFT",
                "appendedSessions": 2,
                "runtimeSwitched": True,
                "providerHistoryInvalidated": True,
            }
            with (
                patch(
                    "quantaalpha.trading.unattended_provider_automation.advance_official_provider",
                    return_value=update,
                ) as advance,
                patch(
                    "quantaalpha.trading.unattended_provider_automation.ensure_evidence_checkpoint",
                    return_value=checkpoint,
                ) as evidence,
            ):
                first = run_cycle_and_record(
                    declaration,
                    poll_seconds=900,
                    coordinator_factory=lambda _: coordinator,
                )

            with (
                patch(
                    "quantaalpha.trading.unattended_provider_automation.advance_official_provider",
                    return_value={
                        "status": "NO_NEW_SESSION",
                        "runtimeSwitched": False,
                    },
                ) as maintenance_advance,
                patch(
                    "quantaalpha.trading.unattended_provider_automation.ensure_evidence_checkpoint"
                ) as maintenance_evidence,
            ):
                second = run_cycle_and_record(
                    declaration,
                    poll_seconds=900,
                    coordinator_factory=lambda _: self.fail(
                        "invalidated shadow coordinator must not be loaded"
                    ),
                )

        self.assertEqual(first["status"], "REVIEW_REQUIRED")
        self.assertTrue(first["result"]["providerHistoryInvalidated"])
        self.assertEqual(first["evidenceCheckpoint"], checkpoint)
        self.assertEqual(second["status"], "REVIEW_REQUIRED")
        self.assertTrue(second["result"]["providerHistoryInvalidated"])
        self.assertEqual(second["consecutiveErrors"], 0)
        self.assertEqual(second["evidenceCheckpoint"], checkpoint)
        evidence.assert_called_once()
        maintenance_evidence.assert_not_called()
        self.assertTrue(
            maintenance_advance.call_args.kwargs["active_state"][
                "providerHistoryInvalidated"
            ]
        )
        advance.assert_called_once()

    def test_terminal_shadow_with_missing_frozen_lineage_retries_provider_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            declaration = _declaration(root)
            coordinator = _Coordinator(_state("MISSED_EXECUTION"))
            update = {"status": "NO_NEW_SESSION", "runtimeSwitched": False}
            with patch(
                "quantaalpha.trading.unattended_provider_automation.advance_official_provider",
                side_effect=[
                    ValueError(
                        "Current Provider differs from the frozen Provider and "
                        "lacks an append-only lineage proof"
                    ),
                    update,
                ],
            ) as advance:
                result = run_cycle_and_record(
                    declaration,
                    poll_seconds=900,
                    coordinator_factory=lambda _: coordinator,
                )

            with patch(
                "quantaalpha.trading.unattended_provider_automation.advance_official_provider",
                return_value=update,
            ) as maintenance_advance:
                repeated = run_cycle_and_record(
                    declaration,
                    poll_seconds=900,
                    coordinator_factory=lambda _: self.fail(
                        "provider-only mode must not reload the terminal shadow"
                    ),
                )

        self.assertEqual(result["status"], "REVIEW_REQUIRED")
        self.assertTrue(result["result"]["providerHistoryInvalidated"])
        self.assertEqual(
            result["result"]["providerRecoveryReason"],
            "FROZEN_LINEAGE_UNAVAILABLE_NO_LEDGER_BACKFILL",
        )
        self.assertEqual(result["consecutiveErrors"], 0)
        self.assertEqual(advance.call_count, 2)
        self.assertTrue(
            advance.call_args_list[1].kwargs["active_state"][
                "providerHistoryInvalidated"
            ]
        )
        self.assertEqual(repeated["status"], "REVIEW_REQUIRED")
        self.assertTrue(repeated["result"]["providerHistoryInvalidated"])
        maintenance_advance.assert_called_once()

    def test_terminal_shadow_skips_lineage_blocked_checkpoint_for_provider_only_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            declaration = _declaration(root)
            database = root / "run" / "shadow.sqlite3"
            database.parent.mkdir(parents=True)
            database.write_bytes(b"ledger")
            coordinator = _Coordinator(_state("MISSED_EXECUTION"))
            with (
                patch(
                    "quantaalpha.trading.unattended_provider_automation.ensure_evidence_checkpoint",
                    side_effect=ValueError(
                        "Current Provider differs from the frozen Provider and "
                        "lacks an append-only lineage proof"
                    ),
                ) as evidence,
                patch(
                    "quantaalpha.trading.unattended_provider_automation.advance_official_provider",
                    return_value={
                        "status": "NO_NEW_SESSION",
                        "runtimeSwitched": False,
                    },
                ) as advance,
            ):
                result = run_cycle_and_record(
                    declaration,
                    poll_seconds=900,
                    coordinator_factory=lambda _: coordinator,
                )

        self.assertEqual(result["status"], "REVIEW_REQUIRED")
        self.assertTrue(result["result"]["providerHistoryInvalidated"])
        self.assertEqual(evidence.call_count, 1)
        self.assertTrue(
            advance.call_args.kwargs["active_state"]["providerHistoryInvalidated"]
        )

    def test_legal_actions_are_checkpointed_before_history_drift_switch(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            declaration = _declaration(root)
            database = root / "run" / "shadow.sqlite3"
            database.parent.mkdir(parents=True)
            database.write_bytes(b"ledger")
            coordinator = _Coordinator(
                _state("READY_TO_EXECUTE", "EXECUTE_NEXT_OPEN")
            )
            with (
                patch(
                    "quantaalpha.trading.unattended_provider_automation.advance_official_provider",
                    return_value={
                        "status": "ADVANCED_WITH_HISTORY_DRIFT",
                        "appendedSessions": 1,
                        "runtimeSwitched": True,
                        "providerHistoryInvalidated": True,
                    },
                ),
                patch(
                    "quantaalpha.trading.unattended_provider_automation.ensure_evidence_checkpoint",
                    side_effect=[{"count": 1}, {"count": 2}],
                ) as evidence,
            ):
                result = run_cycle_and_record(
                    declaration,
                    poll_seconds=900,
                    coordinator_factory=lambda _: coordinator,
                )

        self.assertEqual(result["status"], "REVIEW_REQUIRED")
        self.assertEqual(
            result["result"]["actions"],
            ["PAIRED_NEXT_OPEN_EXECUTED", "PAIRED_CLOSE_SIGNAL_FROZEN"],
        )
        self.assertEqual(result["evidenceCheckpoint"], {"count": 2})
        self.assertEqual(evidence.call_count, 2)

    def test_already_missed_shadow_still_updates_data_without_ledger_actions(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            declaration = _declaration(root)
            states = iter(
                [
                    _Coordinator(_state("MISSED_EXECUTION")),
                    _Coordinator(_state("MISSED_EXECUTION")),
                ]
            )
            with (
                patch(
                    "quantaalpha.trading.unattended_provider_automation.advance_official_provider",
                    return_value={
                        "status": "ADVANCED",
                        "appendedSessions": 1,
                        "runtimeSwitched": True,
                    },
                ) as advance,
            ):
                result = run_cycle_and_record(
                    declaration,
                    poll_seconds=900,
                    coordinator_factory=lambda _: next(states),
                )

        self.assertEqual(result["status"], "REVIEW_REQUIRED")
        self.assertEqual(result["result"]["actions"], [])
        advance.assert_called_once()

    def test_single_session_cycle_preserves_existing_causal_ledger_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            declaration = _declaration(root)
            before = _Coordinator(_state("WAITING_OFFICIAL_DAILY"))
            ready = _Coordinator(
                _state("READY_TO_EXECUTE", "EXECUTE_NEXT_OPEN")
            )
            coordinators = iter([before, ready])
            update = {
                "status": "ADVANCED",
                "appendedSessions": 1,
                "runtimeSwitched": True,
            }
            with (
                patch(
                    "quantaalpha.trading.unattended_provider_automation.advance_official_provider",
                    return_value=update,
                ),
            ):
                result = run_cycle_and_record(
                    declaration,
                    poll_seconds=900,
                    coordinator_factory=lambda _: next(coordinators),
                )

        self.assertEqual(result["status"], "ADVANCED_AND_PROCESSED")
        self.assertIs(result["result"]["providerUpdate"], update)
        self.assertEqual(len(result["result"]["actions"]), 2)
        self.assertEqual(ready.executions, 1)
        self.assertEqual(ready.generations, 1)

    def test_ready_ledger_actions_run_before_a_multi_session_provider_recovery(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            declaration = _declaration(root)
            before = _Coordinator(
                _state("READY_TO_EXECUTE", "EXECUTE_NEXT_OPEN")
            )
            after = _Coordinator(_state("MISSED_EXECUTION"))
            coordinators = iter([before, after])
            order: list[str] = []

            def advance(*args, **kwargs):
                order.append("advance")
                return {
                    "status": "ADVANCED",
                    "appendedSessions": 2,
                    "runtimeSwitched": True,
                }

            original_execute = before.execute_ready_pair

            def execute():
                order.append("execute")
                return original_execute()

            before.execute_ready_pair = execute
            with patch(
                "quantaalpha.trading.unattended_provider_automation.advance_official_provider",
                side_effect=advance,
            ):
                result = run_cycle_and_record(
                    declaration,
                    poll_seconds=900,
                    coordinator_factory=lambda _: next(coordinators),
                )

        self.assertEqual(order, ["execute", "advance"])
        self.assertEqual(result["status"], "REVIEW_REQUIRED")
        self.assertEqual(
            result["result"]["actions"],
            ["PAIRED_NEXT_OPEN_EXECUTED", "PAIRED_CLOSE_SIGNAL_FROZEN"],
        )
        self.assertEqual(result["result"]["shadowStatus"], "MISSED_EXECUTION")


if __name__ == "__main__":
    unittest.main()
