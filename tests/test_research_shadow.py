import hashlib
import json
import sqlite3
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from quantaalpha.backtest.runner import BacktestRunner
from quantaalpha.trading.paper import PaperTradingEngine
from quantaalpha.trading.research_shadow import (
    ResearchShadowCoordinator,
    _execution_implementation_contract,
)
from quantaalpha.trading.signal import ResearchShadowSignalGenerator


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _execution_test_coordinator(
    root: Path, *, causal: bool = False
) -> ResearchShadowCoordinator:
    declaration_path = root / "declaration.json"
    manifest_path = root / "manifest.json"
    database_path = root / "research_shadow.sqlite3"
    declaration = {
        "runId": "execution-contract-test",
        "execution": {
            "dealPrice": "open",
            "buyCostRate": 0.0005,
            "sellCostRate": 0.0015,
            "limitThreshold": 0.095,
            "benchmark": "000300.SH",
        },
    }
    if causal:
        declaration["execution"].update(
            {
                "paperExecutionModelVersion": 2,
                "lotSize": 100,
                "minimumCost": 5.0,
                "slippageBps": 10.0,
                "growthLimitThreshold": 0.195,
                "starMinimumOrderSize": 200,
                "starOrderIncrement": 1,
                "requireListingSessions": True,
            }
        )
    declaration_path.write_text(json.dumps(declaration), encoding="utf-8")
    manifest_path.write_text("{}", encoding="utf-8")

    coordinator = ResearchShadowCoordinator.__new__(ResearchShadowCoordinator)
    coordinator.declaration_path = declaration_path
    coordinator.manifest_path = manifest_path
    coordinator.db_path = database_path
    coordinator.declaration = declaration
    coordinator.execution_contract, config = coordinator._build_execution_contract()
    contract_json = json.dumps(
        coordinator.execution_contract,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    coordinator.execution_contract_sha256 = hashlib.sha256(
        contract_json.encode("utf-8")
    ).hexdigest()
    coordinator.execution_implementation = _execution_implementation_contract()
    implementation_json = json.dumps(
        coordinator.execution_implementation,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    coordinator.execution_implementation_sha256 = hashlib.sha256(
        implementation_json.encode("utf-8")
    ).hexdigest()
    coordinator.execution_implementation_frozen = False
    coordinator.engine = PaperTradingEngine(database_path, config=config)
    coordinator.engine.ensure_account("C1")
    coordinator.engine.ensure_account("B0")
    return coordinator


class _StateEngine:
    def __init__(self, states=None, pending=None):
        self.states = states or {}
        self.pending = pending or {}

    def get_state(self, arm, **_kwargs):
        return self.states[arm]

    def next_pending_signal(self, arm):
        return self.pending.get(arm)


class _Source:
    def __init__(self, latest, next_date=None):
        self.latest = latest
        self.next_date = next_date

    def latest_dates(self, count=1):
        return [self.latest]

    def next_date_after(self, signal_date):
        return self.next_date


def _empty_arm_state():
    return {
        "account": {"lastTradeDate": None},
        "positions": [],
        "pendingSignal": None,
        "latestSignal": None,
        "paperValidation": {
            "maxDrawdown": 0.0,
            "drawdownWarningTriggered": False,
            "drawdownStopTriggered": False,
        },
    }


def _stopped_arm_state():
    state = _empty_arm_state()
    state["paperValidation"] = {
        "maxDrawdown": -0.16,
        "drawdownWarningTriggered": True,
        "drawdownStopTriggered": True,
    }
    return state


class ResearchShadowContractTests(unittest.TestCase):
    def test_execution_implementation_contract_binds_code_and_runtime(self):
        implementation = _execution_implementation_contract()

        paths = {row["path"] for row in implementation["files"]}
        self.assertIn("quantaalpha/trading/paper.py", paths)
        self.assertIn("quantaalpha/trading/research_shadow.py", paths)
        self.assertIn(
            "quantaalpha/utils/investment_data_release.py",
            paths,
        )
        self.assertTrue(
            all(len(row["sha256"]) == 64 for row in implementation["files"])
        )
        self.assertEqual(implementation["version"], 1)
        self.assertIn("pythonVersion", implementation["runtime"])
        self.assertIn("pyqlib", implementation["runtime"]["distributions"])

    def test_execution_contract_binds_declaration_and_legacy_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            coordinator = _execution_test_coordinator(Path(tmp))

            contract = coordinator.execution_contract
            config = coordinator.engine.config

            self.assertEqual(contract["legacyDefaultsApplied"], [
                "lotSize",
                "minimumCost",
                "slippageBps",
            ])
            self.assertEqual(config.lot_size, contract["lotSize"])
            self.assertEqual(config.buy_cost_rate, contract["buyCostRate"])
            self.assertEqual(config.sell_cost_rate, contract["sellCostRate"])
            self.assertEqual(config.minimum_cost, contract["minimumCost"])
            self.assertEqual(config.slippage_bps, contract["slippageBps"])
            self.assertEqual(config.limit_threshold, contract["limitThreshold"])

    def test_causal_execution_contract_freezes_open_known_fields_and_board_rules(self):
        with tempfile.TemporaryDirectory() as tmp:
            coordinator = _execution_test_coordinator(Path(tmp), causal=True)

            contract = coordinator.execution_contract
            config = coordinator.engine.config

            self.assertEqual(contract["engineModelVersion"], 2)
            self.assertEqual(contract["limitPolicy"], "causal_open_board_threshold")
            self.assertEqual(
                contract["slippageModel"],
                "open_plus_minus_bps_unclamped_effective_price",
            )
            self.assertEqual(contract["growthLimitThreshold"], 0.195)
            self.assertEqual(contract["starMinimumOrderSize"], 200)
            self.assertEqual(
                contract["forbiddenOpeningDecisionFields"],
                ["high", "low", "close", "volume"],
            )
            self.assertEqual(contract["legacyDefaultsApplied"], [])
            self.assertEqual(config.execution_model_version, 2)
            self.assertTrue(config.require_listing_sessions)

    def test_causal_execution_contract_requires_all_explicit_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            coordinator = _execution_test_coordinator(root, causal=True)
            del coordinator.declaration["execution"]["starMinimumOrderSize"]

            with self.assertRaisesRegex(ValueError, "fields are missing"):
                coordinator._build_execution_contract()

    def test_execution_contract_is_frozen_once_before_execution(self):
        with tempfile.TemporaryDirectory() as tmp:
            coordinator = _execution_test_coordinator(Path(tmp))

            with coordinator._operation_lock():
                coordinator._initialize_realm()
            coordinator._initialize_realm()

            with coordinator._connect() as connection:
                metadata = {
                    row["key"]: row["value"]
                    for row in connection.execute(
                        "SELECT key, value FROM shadow_metadata"
                    )
                }
                audit = connection.execute(
                    "SELECT event_type FROM shadow_audit ORDER BY sequence"
                ).fetchall()
            self.assertEqual(
                metadata["executionContractSha256"],
                coordinator.execution_contract_sha256,
            )
            self.assertEqual(
                [row["event_type"] for row in audit],
                [
                    "EXECUTION_CONTRACT_FROZEN",
                    "EXECUTION_IMPLEMENTATION_FROZEN",
                ],
            )

    def test_frozen_execution_implementation_tampering_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            coordinator = _execution_test_coordinator(Path(tmp))
            coordinator._initialize_realm()
            with coordinator._connect() as connection:
                connection.execute(
                    "UPDATE shadow_metadata SET value = ? "
                    "WHERE key = 'executionImplementationSha256'",
                    ("0" * 64,),
                )

            with self.assertRaisesRegex(ValueError, "changed after freeze"):
                coordinator._initialize_realm()

    def test_active_run_rejects_current_execution_code_drift(self):
        with tempfile.TemporaryDirectory() as tmp:
            coordinator = _execution_test_coordinator(Path(tmp))
            coordinator._initialize_realm()
            coordinator.execution_implementation = {
                **coordinator.execution_implementation,
                "version": 999,
            }
            coordinator.execution_implementation_sha256 = hashlib.sha256(
                json.dumps(
                    coordinator.execution_implementation,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()

            with self.assertRaisesRegex(ValueError, "changed after freeze"):
                coordinator._initialize_realm()

    def test_retired_run_keeps_historical_implementation_after_code_drift(self):
        with tempfile.TemporaryDirectory() as tmp:
            coordinator = _execution_test_coordinator(Path(tmp))
            coordinator._initialize_realm()
            frozen_sha = coordinator.execution_implementation_sha256
            with patch.object(
                coordinator, "get_state", return_value={"status": "RETIRED"}
            ):
                coordinator.retire_before_execution(
                    "IMPLEMENTATION_TEST_RETIREMENT",
                    "replacement-v2",
                )

            coordinator.execution_implementation = {
                "version": 999,
                "files": [],
                "runtime": {},
            }
            coordinator.execution_implementation_sha256 = hashlib.sha256(
                json.dumps(
                    coordinator.execution_implementation,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            coordinator.execution_implementation_frozen = False

            coordinator._initialize_realm()

            self.assertTrue(coordinator.execution_implementation_frozen)
            self.assertEqual(coordinator.execution_implementation_sha256, frozen_sha)
            self.assertEqual(coordinator.execution_implementation["version"], 1)

    def test_missing_execution_implementation_is_rejected_after_activity(self):
        with tempfile.TemporaryDirectory() as tmp:
            coordinator = _execution_test_coordinator(Path(tmp))
            coordinator._initialize_realm()
            with coordinator._connect() as connection:
                connection.execute(
                    "DELETE FROM shadow_metadata WHERE key LIKE "
                    "'executionImplementation%'"
                )
                connection.execute(
                    "DELETE FROM shadow_audit "
                    "WHERE event_type = 'EXECUTION_IMPLEMENTATION_FROZEN'"
                )
                connection.execute(
                    "INSERT INTO signal_batches(id, account_id, signal_date, status) "
                    "VALUES ('signal', 'C1', '2026-08-11', 'executed')"
                )
                connection.execute(
                    "INSERT INTO runs(id, account_id, signal_batch_id, trade_date, status) "
                    "VALUES ('run', 'C1', 'signal', '2026-08-12', 'completed')"
                )

            with self.assertRaisesRegex(ValueError, "after execution activity"):
                coordinator._initialize_realm()

    def test_frozen_execution_contract_tampering_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            coordinator = _execution_test_coordinator(Path(tmp))
            coordinator._initialize_realm()
            with coordinator._connect() as connection:
                connection.execute(
                    "UPDATE shadow_metadata SET value = ? "
                    "WHERE key = 'executionContractSha256'",
                    ("0" * 64,),
                )

            with self.assertRaisesRegex(ValueError, "changed after freeze"):
                coordinator._initialize_realm()

    def test_execution_contract_without_audit_event_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            coordinator = _execution_test_coordinator(Path(tmp))
            coordinator._initialize_realm()
            with coordinator._connect() as connection:
                connection.execute("DELETE FROM shadow_audit")

            with self.assertRaisesRegex(ValueError, "audit event is missing"):
                coordinator._initialize_realm()

    def test_missing_execution_contract_is_rejected_after_execution_activity(self):
        with tempfile.TemporaryDirectory() as tmp:
            coordinator = _execution_test_coordinator(Path(tmp))
            with sqlite3.connect(coordinator.db_path) as connection:
                connection.execute(
                    "INSERT INTO signal_batches(id, account_id, signal_date, status) "
                    "VALUES ('signal', 'C1', '2026-08-11', 'executed')"
                )
                connection.execute(
                    "INSERT INTO runs(id, account_id, signal_batch_id, trade_date, status) "
                    "VALUES ('run', 'C1', 'signal', '2026-08-12', 'completed')"
                )

            with self.assertRaisesRegex(ValueError, "after execution activity"):
                coordinator._initialize_realm()

    def test_run_can_be_retired_only_before_execution_and_then_rejects_actions(self):
        with tempfile.TemporaryDirectory() as tmp:
            coordinator = _execution_test_coordinator(Path(tmp))
            coordinator._initialize_realm()
            with patch.object(coordinator, "get_state", return_value={"status": "RETIRED"}):
                result = coordinator.retire_before_execution(
                    "NON_CAUSAL_OPEN_EXECUTION",
                    "replacement-v2",
                    now=datetime(
                        2026, 8, 12, 1, 0, tzinfo=ZoneInfo("Asia/Shanghai")
                    ),
                )

            self.assertEqual(result["status"], "RETIRED")
            lifecycle = coordinator._lifecycle()
            self.assertEqual(lifecycle["status"], "RETIRED")
            self.assertEqual(lifecycle["replacementRunId"], "replacement-v2")
            with coordinator._connect() as connection:
                events = connection.execute(
                    "SELECT event_type FROM shadow_audit ORDER BY sequence"
                ).fetchall()
            self.assertEqual(
                [row["event_type"] for row in events],
                [
                    "EXECUTION_CONTRACT_FROZEN",
                    "EXECUTION_IMPLEMENTATION_FROZEN",
                    "RUN_RETIRED_BEFORE_EXECUTION",
                ],
            )
            with self.assertRaisesRegex(ValueError, "已在零成交状态退役"):
                coordinator.generate_latest_signal()

    def test_run_retirement_is_rejected_after_execution_activity(self):
        with tempfile.TemporaryDirectory() as tmp:
            coordinator = _execution_test_coordinator(Path(tmp))
            coordinator._initialize_realm()
            with coordinator._connect() as connection:
                connection.execute(
                    "INSERT INTO signal_batches(id, account_id, signal_date, status) "
                    "VALUES ('signal', 'C1', '2026-08-11', 'executed')"
                )
                connection.execute(
                    "INSERT INTO runs(id, account_id, signal_batch_id, trade_date, status) "
                    "VALUES ('run', 'C1', 'signal', '2026-08-12', 'completed')"
                )

            with self.assertRaisesRegex(ValueError, "before execution activity"):
                coordinator.retire_before_execution(
                    "NON_CAUSAL_OPEN_EXECUTION", "replacement-v2"
                )

    def test_runner_validates_frozen_rejected_declaration(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / "config.yaml"
            decision = root / "decision.json"
            declaration = root / "declaration.json"
            config.write_text("model: fixed\n", encoding="utf-8")
            decision.write_text(
                json.dumps({"status": "REJECTED", "runId": "historical"}),
                encoding="utf-8",
            )
            declaration.write_text(
                json.dumps(
                    {
                        "usageRealm": "RESEARCH_SHADOW",
                        "paperEligible": False,
                        "automaticPromotionAllowed": False,
                        "historicalStatus": "REJECTED",
                        "arms": {"C1": {}},
                        "config": {"path": str(config), "sha256": _sha256(config)},
                        "historicalDecision": {
                            "path": str(decision),
                            "sha256": _sha256(decision),
                            "runId": "historical",
                        },
                    }
                ),
                encoding="utf-8",
            )
            runner = BacktestRunner.__new__(BacktestRunner)
            runner.config_path = config

            result = runner._validate_research_shadow_declaration(declaration, "C1")

            self.assertEqual(result["historicalStatus"], "REJECTED")

    def test_shadow_generator_never_reports_formal_paper_eligibility(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            declaration = root / "declaration.json"
            snapshot = root / "snapshot.pkl"
            declaration.write_text("{}", encoding="utf-8")
            snapshot.write_bytes(b"snapshot")
            generator = ResearchShadowSignalGenerator.__new__(
                ResearchShadowSignalGenerator
            )
            generator.snapshot_path = snapshot
            generator.source = type("Source", (), {"provider_path": root})()
            payload = {
                "profitValidation": {
                    "status": "RESEARCH_ONLY",
                    "historicalStatus": "REJECTED",
                },
                "usageRealm": "RESEARCH_SHADOW",
                "paperEligible": False,
                "automaticPromotionAllowed": False,
                "declarationPath": str(declaration),
                "declarationSha256": _sha256(declaration),
                "providerContract": {"fingerprint": "provider", "market": "csi300"},
                "executionContract": {
                    "version": 2,
                    "type": "topk_dropout",
                    "strategyClass": "TopkDropoutStrategy",
                    "topK": 50,
                    "nDrop": 1,
                    "riskDegree": 0.95,
                    "methodBuy": "top",
                    "methodSell": "bottom",
                    "holdThreshold": 21,
                    "onlyTradable": False,
                    "forbidAllTradeAtLimit": True,
                },
            }
            with patch(
                "quantaalpha.trading.signal.provider_contract_fingerprint",
                return_value={"fingerprint": "provider"},
            ):
                eligibility = generator._paper_eligibility(payload)

            self.assertTrue(eligibility["eligible"])
            self.assertEqual(eligibility["status"], "RESEARCH_ONLY")
            self.assertFalse(payload["paperEligible"])

    def test_initial_signal_cannot_be_backfilled_after_provider_advances(self):
        coordinator = ResearchShadowCoordinator.__new__(ResearchShadowCoordinator)
        coordinator.declaration = {
            "initialSignalDate": "2026-08-11",
            "initialSignalDeadline": "2026-08-12T09:15:00+08:00",
        }
        coordinator.source = _Source("2026-08-12")
        coordinator.engine = _StateEngine(
            {"C1": _empty_arm_state(), "B0": _empty_arm_state()}
        )

        with self.assertRaisesRegex(ValueError, "禁止补生成历史信号"):
            coordinator.generate_latest_signal(
                datetime(2026, 8, 11, 21, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
            )

    def test_drawdown_warning_does_not_stop_the_runtime(self):
        coordinator = ResearchShadowCoordinator.__new__(ResearchShadowCoordinator)
        arms = {"C1": _empty_arm_state(), "B0": _empty_arm_state()}
        arms["C1"]["paperValidation"].update(
            {
                "maxDrawdown": -0.11,
                "drawdownWarningTriggered": True,
            }
        )

        status, next_action = coordinator._runtime_status(arms, "2026-08-11")

        self.assertEqual(status, "FROZEN")
        self.assertEqual(next_action, "GENERATE_LATEST_CLOSE_SIGNAL")

    def test_drawdown_stop_prioritizes_manual_review(self):
        coordinator = ResearchShadowCoordinator.__new__(ResearchShadowCoordinator)
        arms = {"C1": _stopped_arm_state(), "B0": _empty_arm_state()}

        status, next_action = coordinator._runtime_status(arms, "2026-08-11")

        self.assertEqual(status, "RISK_STOPPED")
        self.assertEqual(next_action, "REVIEW_RISK_STOP")

    def test_drawdown_stop_blocks_new_signal_generation(self):
        coordinator = ResearchShadowCoordinator.__new__(ResearchShadowCoordinator)
        coordinator.engine = _StateEngine(
            {"C1": _stopped_arm_state(), "B0": _empty_arm_state()}
        )

        with self.assertRaisesRegex(ValueError, "15% 回撤停止线"):
            coordinator.generate_latest_signal()

    def test_drawdown_stop_blocks_pending_execution(self):
        pending = {
            "C1": {"signalDate": "2026-08-11", "symbols": []},
            "B0": {"signalDate": "2026-08-11", "symbols": []},
        }
        coordinator = ResearchShadowCoordinator.__new__(ResearchShadowCoordinator)
        coordinator.engine = _StateEngine(
            {"C1": _stopped_arm_state(), "B0": _empty_arm_state()},
            pending=pending,
        )

        with self.assertRaisesRegex(ValueError, "15% 回撤停止线"):
            coordinator.execute_ready_pair()

    def test_execution_cannot_backfill_a_missed_next_session(self):
        pending = {
            "C1": {"signalDate": "2026-08-11", "symbols": []},
            "B0": {"signalDate": "2026-08-11", "symbols": []},
        }
        coordinator = ResearchShadowCoordinator.__new__(ResearchShadowCoordinator)
        coordinator.declaration = {
            "initialSignalDate": "2026-08-11",
            "firstTradeDate": "2026-08-12",
        }
        coordinator.source = _Source("2026-08-13", next_date="2026-08-12")
        coordinator.engine = _StateEngine(
            {"C1": _empty_arm_state(), "B0": _empty_arm_state()},
            pending=pending,
        )

        with self.assertRaisesRegex(ValueError, "禁止历史回填撮合"):
            coordinator.execute_ready_pair()


if __name__ == "__main__":
    unittest.main()
