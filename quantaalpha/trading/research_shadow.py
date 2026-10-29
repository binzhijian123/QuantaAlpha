from __future__ import annotations

import fcntl
import hashlib
import importlib.metadata
import json
import math
import platform
import shutil
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping
from zoneinfo import ZoneInfo

from quantaalpha.trading.market_data import QlibBinaryDailySource
from quantaalpha.trading.paper import (
    PAPER_BENCHMARK_SYMBOL,
    PAPER_CAUSAL_EXECUTION_MODEL_VERSION,
    PAPER_DRAWDOWN_STOP,
    PAPER_DRAWDOWN_WARNING,
    PAPER_EXECUTION_MODEL_VERSION,
    PaperTradingConfig,
    PaperTradingEngine,
)
from quantaalpha.trading.signal import (
    ResearchShadowSignalGenerator,
    build_research_shadow_signal_source,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DECLARATION = (
    PROJECT_ROOT
    / "configs"
    / "research_shadow_20260812_deltaes1_causal_v2.declaration.json"
)
SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
ARMS = ("C1", "B0")
_SHADOW_LOCK = threading.RLock()
EXECUTION_IMPLEMENTATION_VERSION = 1
EXECUTION_IMPLEMENTATION_FILES = (
    "quantaalpha/trading/execution_contract.py",
    "quantaalpha/trading/market_data.py",
    "quantaalpha/trading/paper.py",
    "quantaalpha/trading/research_shadow.py",
    "quantaalpha/trading/research_shadow_automation.py",
    "quantaalpha/trading/signal.py",
    "quantaalpha/utils/investment_data_release.py",
    "quantaalpha/utils/provider_lineage.py",
    "quantaalpha/utils/qlib_provider.py",
)
EXECUTION_RUNTIME_DISTRIBUTIONS = (
    "lightgbm",
    "numpy",
    "pandas",
    "pyqlib",
    "scikit-learn",
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_json(payload: Mapping[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _execution_implementation_contract() -> dict[str, Any]:
    files = []
    for relative_path in EXECUTION_IMPLEMENTATION_FILES:
        path = PROJECT_ROOT / relative_path
        if not path.is_file():
            raise RuntimeError(
                f"research-shadow execution implementation file is missing: {path}"
            )
        files.append(
            {
                "path": relative_path,
                "sha256": _sha256(path),
            }
        )
    distributions: dict[str, str] = {}
    for name in EXECUTION_RUNTIME_DISTRIBUTIONS:
        try:
            distributions[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError as error:
            raise RuntimeError(
                f"research-shadow execution runtime distribution is missing: {name}"
            ) from error
    return {
        "version": EXECUTION_IMPLEMENTATION_VERSION,
        "files": files,
        "runtime": {
            "pythonImplementation": platform.python_implementation(),
            "pythonVersion": platform.python_version(),
            "sqliteVersion": sqlite3.sqlite_version,
            "distributions": distributions,
        },
    }


class ResearchShadowCoordinator:
    """Own the isolated paired C1/B0 forward-only research-shadow ledger."""

    def __init__(
        self,
        declaration_path: str | Path = DEFAULT_DECLARATION,
        *,
        source: QlibBinaryDailySource | None = None,
    ):
        self.declaration_path = Path(declaration_path).expanduser().resolve()
        self.declaration = json.loads(
            self.declaration_path.read_text(encoding="utf-8")
        )
        self.manifest_path = Path(self.declaration["manifestPath"]).expanduser().resolve()
        self.manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        self.db_path = Path(self.declaration["databasePath"]).expanduser().resolve()
        if self.db_path.parent == (PROJECT_ROOT / "data" / "trading" / "paper").resolve():
            raise ValueError("research-shadow database cannot use the formal paper directory")
        self.source = source or QlibBinaryDailySource()
        self._validate_frozen_contract()
        self.execution_contract, paper_config = self._build_execution_contract()
        self.execution_contract_sha256 = hashlib.sha256(
            _canonical_json(self.execution_contract).encode("utf-8")
        ).hexdigest()
        self.execution_implementation = _execution_implementation_contract()
        self.execution_implementation_sha256 = hashlib.sha256(
            _canonical_json(self.execution_implementation).encode("utf-8")
        ).hexdigest()
        self.execution_implementation_frozen = False
        self.engine = PaperTradingEngine(self.db_path, config=paper_config)
        self.engine.ensure_account("C1", "RESEARCH_SHADOW C1")
        self.engine.ensure_account("B0", "RESEARCH_SHADOW B0")
        self._initialize_realm()

    def _validate_frozen_contract(self) -> None:
        declaration_sha = _sha256(self.declaration_path)
        if self.declaration.get("usageRealm") != "RESEARCH_SHADOW":
            raise ValueError("invalid research-shadow declaration realm")
        if self.declaration.get("formalPaperEligible") is not False:
            raise ValueError("research-shadow declaration is incorrectly paper eligible")
        if self.manifest.get("usageRealm") != "RESEARCH_SHADOW":
            raise ValueError("invalid research-shadow manifest realm")
        if self.manifest.get("declarationSha256") != declaration_sha:
            raise ValueError("research-shadow declaration changed after model freeze")
        if self.manifest.get("runId") != self.declaration.get("runId"):
            raise ValueError("research-shadow run ID mismatch")
        if self.manifest.get("returnsEvaluatedDuringFreeze") is not False:
            raise ValueError("research-shadow freeze unexpectedly evaluated returns")
        for arm in ARMS:
            snapshot_path = Path(
                self.declaration["arms"][arm]["snapshotPath"]
            ).expanduser().resolve()
            expected_sha = (self.manifest.get("arms") or {}).get(arm, {}).get(
                "snapshotSha256"
            )
            if not snapshot_path.is_file() or not expected_sha:
                raise ValueError(f"research-shadow {arm} snapshot is missing")
            if _sha256(snapshot_path) != expected_sha:
                raise ValueError(f"research-shadow {arm} snapshot hash mismatch")

    def _build_execution_contract(
        self,
    ) -> tuple[dict[str, Any], PaperTradingConfig]:
        execution = self.declaration.get("execution")
        if not isinstance(execution, Mapping):
            raise ValueError("research-shadow execution declaration is missing")
        if execution.get("dealPrice") != "open":
            raise ValueError("research-shadow only supports next-session open execution")
        if execution.get("benchmark") != PAPER_BENCHMARK_SYMBOL:
            raise ValueError("research-shadow benchmark differs from the paper engine")

        defaults = PaperTradingConfig()
        raw_lot_size = execution.get("lotSize", defaults.lot_size)
        if isinstance(raw_lot_size, bool):
            raise ValueError("research-shadow lot size is invalid")
        try:
            execution_model_version = int(
                execution.get(
                    "paperExecutionModelVersion", PAPER_EXECUTION_MODEL_VERSION
                )
            )
            lot_size = int(raw_lot_size)
            numeric_lot_size = float(raw_lot_size)
            buy_cost_rate = float(
                execution.get("buyCostRate", defaults.buy_cost_rate)
            )
            sell_cost_rate = float(
                execution.get("sellCostRate", defaults.sell_cost_rate)
            )
            minimum_cost = float(
                execution.get("minimumCost", defaults.minimum_cost)
            )
            slippage_bps = float(
                execution.get("slippageBps", defaults.slippage_bps)
            )
            limit_threshold = float(
                execution.get("limitThreshold", defaults.limit_threshold)
            )
        except (TypeError, ValueError) as error:
            raise ValueError("research-shadow execution parameters are invalid") from error

        numeric_values = (
            numeric_lot_size,
            buy_cost_rate,
            sell_cost_rate,
            minimum_cost,
            slippage_bps,
            limit_threshold,
        )
        if not all(math.isfinite(value) for value in numeric_values):
            raise ValueError("research-shadow execution parameters must be finite")
        if lot_size <= 0 or not math.isclose(numeric_lot_size, lot_size):
            raise ValueError("research-shadow lot size must be a positive integer")
        if execution_model_version not in {
            PAPER_EXECUTION_MODEL_VERSION,
            PAPER_CAUSAL_EXECUTION_MODEL_VERSION,
        }:
            raise ValueError("unsupported research-shadow paper execution model")
        if min(buy_cost_rate, sell_cost_rate, minimum_cost, slippage_bps) < 0:
            raise ValueError("research-shadow costs and slippage cannot be negative")
        if not 0 < limit_threshold <= 1:
            raise ValueError("research-shadow limit threshold must be in (0, 1]")

        if execution_model_version == PAPER_EXECUTION_MODEL_VERSION:
            compatibility_defaults = [
                field
                for field in ("lotSize", "minimumCost", "slippageBps")
                if field not in execution
            ]
            contract = {
                "version": 1,
                "engineModelVersion": PAPER_EXECUTION_MODEL_VERSION,
                "dealPrice": "open",
                "lotSize": lot_size,
                "buyCostRate": buy_cost_rate,
                "sellCostRate": sell_cost_rate,
                "minimumCost": minimum_cost,
                "slippageBps": slippage_bps,
                "limitThreshold": limit_threshold,
                "transactionCostModel": "max_minimum_or_side_rate",
                "slippageModel": "open_plus_minus_bps_clamped_to_daily_range",
                "settlement": "T+1",
                "oddLotFullExitAllowed": True,
                "legacyDefaultsApplied": compatibility_defaults,
            }
            config = PaperTradingConfig(
                lot_size=lot_size,
                buy_cost_rate=buy_cost_rate,
                sell_cost_rate=sell_cost_rate,
                minimum_cost=minimum_cost,
                slippage_bps=slippage_bps,
                limit_threshold=limit_threshold,
            )
            return contract, config

        required = {
            "paperExecutionModelVersion",
            "lotSize",
            "buyCostRate",
            "sellCostRate",
            "minimumCost",
            "slippageBps",
            "limitThreshold",
            "growthLimitThreshold",
            "starMinimumOrderSize",
            "starOrderIncrement",
            "requireListingSessions",
        }
        missing = sorted(required - set(execution))
        if missing:
            raise ValueError(
                "causal research-shadow execution fields are missing: "
                + ", ".join(missing)
            )
        try:
            growth_limit_threshold = float(execution["growthLimitThreshold"])
            star_minimum_order_size = int(execution["starMinimumOrderSize"])
            star_order_increment = int(execution["starOrderIncrement"])
        except (TypeError, ValueError) as error:
            raise ValueError("causal research-shadow board parameters are invalid") from error
        if not all(
            math.isfinite(value)
            for value in (
                growth_limit_threshold,
                float(star_minimum_order_size),
                float(star_order_increment),
            )
        ):
            raise ValueError("causal research-shadow board parameters must be finite")
        if not 0 < growth_limit_threshold <= 1:
            raise ValueError("growth-board limit threshold must be in (0, 1]")
        if star_minimum_order_size < 1 or star_order_increment < 1:
            raise ValueError("STAR order sizes must be positive integers")
        if execution["requireListingSessions"] is not True:
            raise ValueError("causal execution must require listing-session evidence")

        contract = {
            "version": 2,
            "engineModelVersion": PAPER_CAUSAL_EXECUTION_MODEL_VERSION,
            "dealPrice": "open",
            "lotSize": lot_size,
            "buyCostRate": buy_cost_rate,
            "sellCostRate": sell_cost_rate,
            "minimumCost": minimum_cost,
            "slippageBps": slippage_bps,
            "limitThreshold": limit_threshold,
            "growthLimitThreshold": growth_limit_threshold,
            "starMinimumOrderSize": star_minimum_order_size,
            "starOrderIncrement": star_order_increment,
            "requireListingSessions": True,
            "transactionCostModel": "max_minimum_or_side_rate",
            "slippageModel": "open_plus_minus_bps_unclamped_effective_price",
            "limitPolicy": "causal_open_board_threshold",
            "noLimitWindowPolicy": "reject_first_five_or_unknown",
            "settlement": "T+1",
            "oddLotFullExitAllowed": True,
            "causalKnownFields": [
                "symbol",
                "open",
                "previousClose",
                "factor",
                "listingSessions",
            ],
            "forbiddenOpeningDecisionFields": ["high", "low", "close", "volume"],
            "legacyDefaultsApplied": [],
        }
        config = PaperTradingConfig(
            lot_size=lot_size,
            buy_cost_rate=buy_cost_rate,
            sell_cost_rate=sell_cost_rate,
            minimum_cost=minimum_cost,
            slippage_bps=slippage_bps,
            limit_threshold=limit_threshold,
            execution_model_version=PAPER_CAUSAL_EXECUTION_MODEL_VERSION,
            growth_limit_threshold=growth_limit_threshold,
            star_minimum_order_size=star_minimum_order_size,
            star_order_increment=star_order_increment,
            require_listing_sessions=True,
        )
        return contract, config

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=30)
        connection.row_factory = sqlite3.Row
        return connection

    @contextmanager
    def _operation_lock(self):
        db_path = getattr(self, "db_path", None)
        if db_path is None:
            yield
            return
        lock_path = Path(db_path).parent / ".operation.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a+", encoding="ascii") as stream:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)

    def _initialize_realm(self) -> None:
        declaration_sha = _sha256(self.declaration_path)
        manifest_sha = _sha256(self.manifest_path)
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS uq_shadow_signal_date
                ON signal_batches(account_id, signal_date);
                CREATE TABLE IF NOT EXISTS shadow_metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS shadow_audit (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    previous_hash TEXT NOT NULL,
                    event_hash TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL
                );
                """
            )
            connection.execute("BEGIN IMMEDIATE")
            self._verify_audit_rows(
                connection.execute(
                    "SELECT * FROM shadow_audit ORDER BY sequence"
                ).fetchall()
            )
            expected = {
                "usageRealm": "RESEARCH_SHADOW",
                "runId": self.declaration["runId"],
                "declarationSha256": declaration_sha,
                "manifestSha256": manifest_sha,
                "formalPaperEligible": "false",
            }
            for key, value in expected.items():
                row = connection.execute(
                    "SELECT value FROM shadow_metadata WHERE key = ?", (key,)
                ).fetchone()
                if row is not None and row["value"] != value:
                    raise ValueError(f"research-shadow database metadata mismatch: {key}")
                connection.execute(
                    "INSERT OR IGNORE INTO shadow_metadata(key, value) VALUES (?, ?)",
                    (key, value),
                )
            connection.execute(
                "INSERT OR IGNORE INTO shadow_metadata(key, value) VALUES ('runStatus', 'ACTIVE')"
            )
            contract_json = _canonical_json(self.execution_contract)
            contract_rows = {
                row["key"]: row["value"]
                for row in connection.execute(
                    "SELECT key, value FROM shadow_metadata "
                    "WHERE key IN ('executionContractJson', 'executionContractSha256')"
                )
            }
            if contract_rows and len(contract_rows) != 2:
                raise ValueError("research-shadow execution contract metadata is incomplete")
            if contract_rows:
                stored_json = contract_rows["executionContractJson"]
                stored_sha = contract_rows["executionContractSha256"]
                actual_sha = hashlib.sha256(stored_json.encode("utf-8")).hexdigest()
                if stored_json != contract_json or stored_sha != actual_sha:
                    raise ValueError("research-shadow execution contract changed after freeze")
                if stored_sha != self.execution_contract_sha256:
                    raise ValueError("research-shadow execution contract hash mismatch")
                frozen_events = connection.execute(
                    "SELECT payload_json FROM shadow_audit "
                    "WHERE event_type = 'EXECUTION_CONTRACT_FROZEN'"
                ).fetchall()
                if len(frozen_events) != 1:
                    raise ValueError(
                        "research-shadow execution contract audit event is missing or duplicated"
                    )
                try:
                    frozen_payload = json.loads(frozen_events[0]["payload_json"])
                except json.JSONDecodeError as error:
                    raise ValueError(
                        "research-shadow execution contract audit payload is invalid"
                    ) from error
                if (
                    frozen_payload.get("contract") != self.execution_contract
                    or frozen_payload.get("contractSha256") != stored_sha
                    or frozen_payload.get("frozenBeforeFirstExecution") is not True
                ):
                    raise ValueError(
                        "research-shadow execution contract audit payload mismatch"
                    )
            else:
                executed = {
                    table: int(
                        connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                    )
                    for table in ("runs", "orders", "fills", "nav_history")
                }
                if any(executed.values()):
                    raise ValueError(
                        "cannot freeze a missing research-shadow execution contract "
                        "after execution activity"
                    )
                connection.executemany(
                    "INSERT INTO shadow_metadata(key, value) VALUES (?, ?)",
                    (
                        ("executionContractJson", contract_json),
                        ("executionContractSha256", self.execution_contract_sha256),
                    ),
                )
                self._append_audit_row(
                    connection,
                    "EXECUTION_CONTRACT_FROZEN",
                    {
                        "contract": self.execution_contract,
                        "contractSha256": self.execution_contract_sha256,
                        "frozenBeforeFirstExecution": True,
                    },
                    datetime.now(SHANGHAI_TZ),
                )
            implementation_json = _canonical_json(self.execution_implementation)
            run_status = str(
                connection.execute(
                    "SELECT value FROM shadow_metadata WHERE key = 'runStatus'"
                ).fetchone()["value"]
            )
            if run_status not in {"ACTIVE", "RETIRED"}:
                raise ValueError(
                    f"invalid research-shadow lifecycle status: {run_status}"
                )
            implementation_rows = {
                row["key"]: row["value"]
                for row in connection.execute(
                    "SELECT key, value FROM shadow_metadata WHERE key IN "
                    "('executionImplementationJson', "
                    "'executionImplementationSha256')"
                )
            }
            if implementation_rows and len(implementation_rows) != 2:
                raise ValueError(
                    "research-shadow execution implementation metadata is incomplete"
                )
            if implementation_rows:
                stored_json = implementation_rows["executionImplementationJson"]
                stored_sha = implementation_rows["executionImplementationSha256"]
                actual_sha = hashlib.sha256(stored_json.encode("utf-8")).hexdigest()
                try:
                    stored_implementation = json.loads(stored_json)
                except json.JSONDecodeError as error:
                    raise ValueError(
                        "research-shadow execution implementation metadata is invalid"
                    ) from error
                if (
                    not isinstance(stored_implementation, dict)
                    or _canonical_json(stored_implementation) != stored_json
                    or stored_sha != actual_sha
                ):
                    raise ValueError(
                        "research-shadow execution implementation changed after freeze"
                    )
                if run_status == "ACTIVE" and (
                    stored_json != implementation_json
                    or stored_sha != self.execution_implementation_sha256
                ):
                    raise ValueError(
                        "research-shadow execution implementation changed after freeze"
                    )
                frozen_events = connection.execute(
                    "SELECT payload_json FROM shadow_audit "
                    "WHERE event_type = 'EXECUTION_IMPLEMENTATION_FROZEN'"
                ).fetchall()
                if len(frozen_events) != 1:
                    raise ValueError(
                        "research-shadow execution implementation audit event "
                        "is missing or duplicated"
                    )
                try:
                    frozen_payload = json.loads(frozen_events[0]["payload_json"])
                except json.JSONDecodeError as error:
                    raise ValueError(
                        "research-shadow execution implementation audit payload "
                        "is invalid"
                    ) from error
                if (
                    frozen_payload.get("implementation")
                    != stored_implementation
                    or frozen_payload.get("implementationSha256") != stored_sha
                    or frozen_payload.get("frozenBeforeFirstExecution") is not True
                ):
                    raise ValueError(
                        "research-shadow execution implementation audit payload mismatch"
                    )
                if run_status == "RETIRED":
                    self.execution_implementation = stored_implementation
                    self.execution_implementation_sha256 = stored_sha
                self.execution_implementation_frozen = True
            else:
                if run_status == "ACTIVE":
                    activity = {
                        table: int(
                            connection.execute(
                                f"SELECT COUNT(*) FROM {table}"
                            ).fetchone()[0]
                        )
                        for table in ("runs", "orders", "fills", "nav_history")
                    }
                    activity["positions"] = int(
                        connection.execute(
                            "SELECT COUNT(*) FROM positions WHERE quantity != 0"
                        ).fetchone()[0]
                    )
                    if any(activity.values()):
                        raise ValueError(
                            "cannot freeze a missing research-shadow execution "
                            "implementation after execution activity"
                        )
                    connection.executemany(
                        "INSERT INTO shadow_metadata(key, value) VALUES (?, ?)",
                        (
                            ("executionImplementationJson", implementation_json),
                            (
                                "executionImplementationSha256",
                                self.execution_implementation_sha256,
                            ),
                        ),
                    )
                    self._append_audit_row(
                        connection,
                        "EXECUTION_IMPLEMENTATION_FROZEN",
                        {
                            "implementation": self.execution_implementation,
                            "implementationSha256": (
                                self.execution_implementation_sha256
                            ),
                            "frozenBeforeFirstExecution": True,
                            "executionActivity": activity,
                        },
                        datetime.now(SHANGHAI_TZ),
                    )
                    self.execution_implementation_frozen = True
        self._verify_audit_chain()

    def _lifecycle(self) -> dict[str, Any]:
        with self._connect() as connection:
            values = {
                row["key"]: row["value"]
                for row in connection.execute(
                    "SELECT key, value FROM shadow_metadata WHERE key IN "
                    "('runStatus', 'retiredReason', 'replacementRunId', 'retiredAt')"
                )
            }
        status = values.get("runStatus", "ACTIVE")
        if status == "ACTIVE":
            return {
                "status": "ACTIVE",
                "retiredReason": None,
                "replacementRunId": None,
                "retiredAt": None,
            }
        if status != "RETIRED":
            raise ValueError(f"invalid research-shadow lifecycle status: {status}")
        required = ("retiredReason", "replacementRunId", "retiredAt")
        if any(not values.get(key) for key in required):
            raise ValueError("retired research-shadow lifecycle metadata is incomplete")
        return {
            "status": status,
            "retiredReason": values["retiredReason"],
            "replacementRunId": values["replacementRunId"],
            "retiredAt": values["retiredAt"],
        }

    def _assert_active(self, action: str) -> None:
        if getattr(self, "db_path", None) is None:
            return
        lifecycle = self._lifecycle()
        if lifecycle["status"] == "RETIRED":
            raise ValueError(
                f"研究影子盘已在零成交状态退役，禁止{action}；"
                f"替代运行：{lifecycle['replacementRunId']}"
            )

    def retire_before_execution(
        self,
        reason: str,
        replacement_run_id: str,
        *,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        reason = str(reason).strip()
        replacement_run_id = str(replacement_run_id).strip()
        if not reason or not replacement_run_id:
            raise ValueError("retirement reason and replacement run ID are required")
        retired_at = (now or datetime.now(SHANGHAI_TZ)).astimezone(
            SHANGHAI_TZ
        ).isoformat()
        with _SHADOW_LOCK, self._operation_lock(), self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._verify_audit_rows(
                connection.execute(
                    "SELECT * FROM shadow_audit ORDER BY sequence"
                ).fetchall()
            )
            lifecycle_rows = {
                row["key"]: row["value"]
                for row in connection.execute(
                    "SELECT key, value FROM shadow_metadata WHERE key IN "
                    "('runStatus', 'retiredReason', 'replacementRunId')"
                )
            }
            if lifecycle_rows.get("runStatus") == "RETIRED":
                if (
                    lifecycle_rows.get("retiredReason") != reason
                    or lifecycle_rows.get("replacementRunId") != replacement_run_id
                ):
                    raise ValueError("research-shadow run was retired with different metadata")
                return self.get_state()

            activity = {
                table: int(
                    connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                )
                for table in ("runs", "orders", "fills", "nav_history")
            }
            activity["positions"] = int(
                connection.execute(
                    "SELECT COUNT(*) FROM positions WHERE quantity != 0"
                ).fetchone()[0]
            )
            if any(activity.values()):
                raise ValueError(
                    "research-shadow retirement is only allowed before execution activity"
                )
            pending_signals = int(
                connection.execute(
                    "SELECT COUNT(*) FROM signal_batches WHERE status = 'pending'"
                ).fetchone()[0]
            )
            connection.executemany(
                "INSERT INTO shadow_metadata(key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (
                    ("runStatus", "RETIRED"),
                    ("retiredReason", reason),
                    ("replacementRunId", replacement_run_id),
                    ("retiredAt", retired_at),
                ),
            )
            self._append_audit_row(
                connection,
                "RUN_RETIRED_BEFORE_EXECUTION",
                {
                    "reason": reason,
                    "replacementRunId": replacement_run_id,
                    "executionActivity": activity,
                    "preservedPendingSignals": pending_signals,
                    "profitabilityEvidence": False,
                },
                datetime.fromisoformat(retired_at),
            )
        self._verify_audit_chain()
        return self.get_state()

    def _generator(self, arm: str) -> ResearchShadowSignalGenerator:
        snapshot_path = self.declaration["arms"][arm]["snapshotPath"]
        return ResearchShadowSignalGenerator(snapshot_path, source=self.source)

    def _arm_state(self, arm: str) -> dict[str, Any]:
        state = self.engine.get_state(arm, market_calendar=self.source.calendar)
        state["arm"] = arm
        state["label"] = self.declaration["arms"][arm]["label"]
        state["strategy"] = self._generator(arm).describe()
        return state

    @staticmethod
    def _risk_arms(
        states: Mapping[str, Mapping[str, Any]], field: str
    ) -> list[str]:
        return [
            arm
            for arm in ARMS
            if bool((states[arm].get("paperValidation") or {}).get(field))
        ]

    def _assert_risk_allows_action(
        self, states: Mapping[str, Mapping[str, Any]], action: str
    ) -> None:
        stopped_arms = self._risk_arms(states, "drawdownStopTriggered")
        if not stopped_arms:
            return
        details = ", ".join(
            f"{arm} {float(states[arm]['paperValidation']['maxDrawdown']):.2%}"
            for arm in stopped_arms
        )
        raise ValueError(
            f"研究影子盘已触发 {abs(PAPER_DRAWDOWN_STOP):.0%} 回撤停止线"
            f"（{details}），禁止{action}；需要人工复核并建立新的预声明运行"
        )

    def get_state(self) -> dict[str, Any]:
        with _SHADOW_LOCK:
            lifecycle = self._lifecycle()
            arms = {arm: self._arm_state(arm) for arm in ARMS}
            self._assert_pair_alignment(arms)
            latest_date = self.source.latest_dates(1)[-1]
            c1 = arms["C1"]
            b0 = arms["B0"]
            c1_return = float(c1["account"]["totalReturn"])
            b0_return = float(b0["account"]["totalReturn"])
            incremental_return = (
                (1.0 + c1_return) / (1.0 + b0_return) - 1.0
                if 1.0 + b0_return > 0
                else None
            )
            sessions = min(
                int(c1["paperValidation"]["observationSessions"]),
                int(b0["paperValidation"]["observationSessions"]),
            )
            aligned_dates = [row["date"] for row in c1["equityCurve"]] == [
                row["date"] for row in b0["equityCurve"]
            ]
            stopped = any(
                arm_state["paperValidation"]["drawdownStopTriggered"]
                for arm_state in arms.values()
            )
            warned = any(
                arm_state["paperValidation"]["drawdownWarningTriggered"]
                for arm_state in arms.values()
            )
            worst_drawdown = min(
                float(arm_state["paperValidation"]["maxDrawdown"])
                for arm_state in arms.values()
            )
            c1_excess = c1["paperValidation"].get("afterCostExcessReturn")
            engineering_gate = sessions >= 20 and aligned_dates
            early_gate = bool(
                sessions >= 60
                and engineering_gate
                and c1_excess is not None
                and c1_excess > 0
                and incremental_return is not None
                and incremental_return > 0
                and not stopped
            )
            strong_gate = bool(sessions >= 120 and early_gate)
            status, next_action = self._runtime_status(
                arms,
                latest_date,
                retired=lifecycle["status"] == "RETIRED",
            )
            return {
                "runId": self.declaration["runId"],
                "usageRealm": "RESEARCH_SHADOW",
                "status": status,
                "nextAction": next_action,
                "historicalStatus": "REJECTED",
                "formalPaperEligible": False,
                "automaticPromotionAllowed": False,
                "lifecycle": lifecycle,
                "marketData": {
                    "providerPath": str(self.source.provider_path.resolve()),
                    "latestDate": latest_date,
                    "frozenProviderFingerprint": self.declaration[
                        "providerFingerprint"
                    ],
                },
                "protocol": {
                    "initialSignalDate": self.declaration["initialSignalDate"],
                    "firstTradeDate": self.declaration["firstTradeDate"],
                    "initialSignalDeadline": self.declaration[
                        "initialSignalDeadline"
                    ],
                    "topK": self.declaration["execution"]["topK"],
                    "nDrop": self.declaration["execution"]["nDrop"],
                    "holdThreshold": self.declaration["execution"][
                        "holdThreshold"
                    ],
                    "drawdownWarning": PAPER_DRAWDOWN_WARNING,
                    "drawdownStop": PAPER_DRAWDOWN_STOP,
                },
                "executionContract": {
                    **self.execution_contract,
                    "sha256": self.execution_contract_sha256,
                    "frozen": True,
                },
                "executionImplementation": (
                    {
                        **self.execution_implementation,
                        "sha256": self.execution_implementation_sha256,
                        "frozen": True,
                    }
                    if self.execution_implementation_frozen
                    else None
                ),
                "pairedEvidence": {
                    "observationSessions": sessions,
                    "datesAligned": aligned_dates,
                    "c1AfterCostExcessReturn": c1_excess,
                    "c1MinusB0Return": incremental_return,
                    "engineeringGateReached": engineering_gate,
                    "earlyMetricGatePassed": early_gate,
                    "strongMetricGatePassed": strong_gate,
                    "worstMaxDrawdown": worst_drawdown,
                    "drawdownWarningTriggered": warned,
                    "drawdownStopTriggered": stopped,
                    "evidenceEligible": (
                        lifecycle["status"] == "ACTIVE"
                        and sessions > 0
                        and aligned_dates
                    ),
                },
                "arms": arms,
                "audit": self._audit_summary(),
            }

    def _runtime_status(
        self,
        arms: Mapping[str, Mapping[str, Any]],
        latest_date: str,
        *,
        retired: bool = False,
    ) -> tuple[str, str]:
        if retired:
            return "RETIRED", "START_NEW_DECLARED_RUN"
        if self._risk_arms(arms, "drawdownStopTriggered"):
            return "RISK_STOPPED", "REVIEW_RISK_STOP"
        latest_signals = [arms[arm].get("latestSignal") for arm in ARMS]
        pending = [arms[arm].get("pendingSignal") for arm in ARMS]
        if all(signal is None for signal in latest_signals):
            return "FROZEN", "GENERATE_LATEST_CLOSE_SIGNAL"
        if all(item is not None for item in pending):
            signal_date = str(pending[0]["signalDate"])
            expected = self.source.next_date_after(signal_date)
            if expected is None:
                return "WAITING_OFFICIAL_DAILY", "WAIT_FOR_NEXT_OFFICIAL_PROVIDER"
            if latest_date == expected:
                return "READY_TO_EXECUTE", "EXECUTE_NEXT_OPEN"
            if latest_date > expected:
                return "MISSED_EXECUTION", "START_NEW_DECLARED_RUN"
            return "WAITING_OFFICIAL_DAILY", "WAIT_FOR_NEXT_OFFICIAL_PROVIDER"
        if any(item is not None for item in pending):
            return "INVALID_PAIR", "REVIEW_LEDGER"
        return "READY_FOR_CLOSE_SIGNAL", "GENERATE_LATEST_CLOSE_SIGNAL"

    def generate_latest_signal(
        self, now: datetime | None = None
    ) -> dict[str, Any]:
        with _SHADOW_LOCK, self._operation_lock():
            self._assert_active("生成或提交新信号")
            local_now = (now or datetime.now(SHANGHAI_TZ)).astimezone(SHANGHAI_TZ)
            states = {arm: self.engine.get_state(arm) for arm in ARMS}
            self._assert_pair_alignment(states)
            self._assert_risk_allows_action(states, "生成或提交新信号")
            if any(state.get("pendingSignal") for state in states.values()):
                raise ValueError("研究影子盘已有待执行配对信号")
            latest_date = self.source.latest_dates(1)[-1]
            first_signal = all(
                state.get("latestSignal") is None for state in states.values()
            )
            if first_signal:
                deadline = datetime.fromisoformat(
                    self.declaration["initialSignalDeadline"]
                )
                if local_now >= deadline:
                    raise ValueError("首个前向信号冻结截止时间已过，禁止历史回填")
                if latest_date != self.declaration["initialSignalDate"]:
                    raise ValueError("当前 Provider 已越过首个信号日，禁止补生成历史信号")
            else:
                last_trade_dates = {
                    state["account"].get("lastTradeDate") for state in states.values()
                }
                if last_trade_dates != {latest_date}:
                    raise ValueError("只能在配对账户完成当日正式撮合后生成最新收盘信号")

            generated: dict[str, dict[str, Any]] = {}
            for arm in ARMS:
                positions = [
                    position
                    for position in states[arm]["positions"]
                    if position["quantity"] > 0
                ]
                generated[arm] = self._generator(arm).generate_targets(
                    latest_date,
                    current_symbols=[position["symbol"] for position in positions],
                    current_holding_sessions={
                        position["symbol"]: position["holdingSessions"]
                        for position in positions
                    },
                )

            submitted: list[tuple[str, str]] = []
            try:
                for arm in ARMS:
                    source = build_research_shadow_signal_source(
                        generated[arm], self.declaration["runId"], arm
                    )
                    signal = self.engine.submit_targets(
                        latest_date,
                        generated[arm]["targets"],
                        account_id=arm,
                        source=source,
                    )
                    submitted.append((arm, signal["signalId"]))
            except Exception:
                for arm, signal_id in submitted:
                    self._remove_unexecuted_signal(arm, signal_id)
                raise

            event = {
                "signalDate": latest_date,
                "signals": {
                    arm: {
                        "signalId": signal_id,
                        "snapshotId": generated[arm]["strategy"]["snapshotId"],
                        "targetsSha256": hashlib.sha256(
                            _canonical_json(generated[arm]["targets"]).encode("utf-8")
                        ).hexdigest(),
                    }
                    for arm, signal_id in submitted
                },
            }
            self._append_audit("PAIRED_SIGNAL_FROZEN", event, local_now)
            return self.get_state()

    def execute_ready_pair(self) -> dict[str, Any]:
        with _SHADOW_LOCK, self._operation_lock():
            self._assert_active("执行待撮合信号")
            states = {arm: self.engine.get_state(arm) for arm in ARMS}
            self._assert_pair_alignment(states)
            self._assert_risk_allows_action(states, "执行待撮合信号")
            pending = {
                arm: self.engine.next_pending_signal(arm) for arm in ARMS
            }
            if any(value is None for value in pending.values()):
                raise ValueError("研究影子盘没有完整的 C1/B0 待执行信号")
            signal_dates = {value["signalDate"] for value in pending.values()}
            if len(signal_dates) != 1:
                raise ValueError("C1/B0 待执行信号日期不一致")
            signal_date = next(iter(signal_dates))
            trade_date = self.source.next_date_after(signal_date)
            if trade_date is None:
                raise ValueError(f"{signal_date} 之后还没有官方下一交易日行情")
            latest_date = self.source.latest_dates(1)[-1]
            if latest_date != trade_date:
                if latest_date > trade_date:
                    raise ValueError("已错过唯一允许的下一交易日，禁止历史回填撮合")
                raise ValueError(f"等待 {trade_date} 官方日线")
            if signal_date == self.declaration["initialSignalDate"] and trade_date != self.declaration["firstTradeDate"]:
                raise ValueError("首笔交易日期与冻结声明不一致")

            benchmark_bar = self.source.get_bars(
                [PAPER_BENCHMARK_SYMBOL], trade_date
            ).get(PAPER_BENCHMARK_SYMBOL)
            if benchmark_bar is None:
                raise ValueError(f"{trade_date} 缺少沪深300基准行情")
            bars = {
                arm: self.source.get_bars(pending[arm]["symbols"], trade_date)
                for arm in ARMS
            }
            backup_path = self.db_path.with_name(
                f".{self.db_path.name}.paired_{uuid.uuid4().hex[:8]}.bak"
            )
            self._backup_database(backup_path)
            try:
                for arm in ARMS:
                    self.engine.execute_pending(
                        bars[arm],
                        trade_date,
                        account_id=arm,
                        benchmark_bar=benchmark_bar,
                    )
                self._append_audit(
                    "PAIRED_NEXT_OPEN_EXECUTED",
                    {"signalDate": signal_date, "tradeDate": trade_date},
                    datetime.now(SHANGHAI_TZ),
                )
                post_execution = self.get_state()
                if post_execution["pairedEvidence"]["drawdownStopTriggered"]:
                    stopped_arms = self._risk_arms(
                        post_execution["arms"], "drawdownStopTriggered"
                    )
                    self._append_audit(
                        "DRAWDOWN_STOP_TRIGGERED",
                        {
                            "tradeDate": trade_date,
                            "threshold": PAPER_DRAWDOWN_STOP,
                            "arms": [
                                {
                                    "arm": arm,
                                    "maxDrawdown": post_execution["arms"][arm][
                                        "paperValidation"
                                    ]["maxDrawdown"],
                                }
                                for arm in stopped_arms
                            ],
                        },
                        datetime.now(SHANGHAI_TZ),
                    )
            except Exception:
                self._restore_database(backup_path)
                raise
            finally:
                backup_path.unlink(missing_ok=True)
            return self.get_state()

    def _assert_pair_alignment(
        self, states: Mapping[str, Mapping[str, Any]]
    ) -> None:
        pending_flags = [states[arm].get("pendingSignal") is not None for arm in ARMS]
        if any(pending_flags) and not all(pending_flags):
            raise ValueError("research-shadow paired pending-signal state diverged")
        latest_flags = [states[arm].get("latestSignal") is not None for arm in ARMS]
        if any(latest_flags) and not all(latest_flags):
            raise ValueError("research-shadow paired signal history diverged")
        trade_dates = {states[arm]["account"].get("lastTradeDate") for arm in ARMS}
        if len(trade_dates) > 1:
            raise ValueError("research-shadow paired trade dates diverged")

    def _remove_unexecuted_signal(self, arm: str, signal_id: str) -> None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT status FROM signal_batches WHERE id = ? AND account_id = ?",
                (signal_id, arm),
            ).fetchone()
            if row is None or row["status"] != "pending":
                raise RuntimeError("cannot compensate a non-pending paired signal")
            connection.execute("DELETE FROM signals WHERE batch_id = ?", (signal_id,))
            connection.execute("DELETE FROM signal_batches WHERE id = ?", (signal_id,))

    def _append_audit(
        self, event_type: str, payload: Mapping[str, Any], now: datetime
    ) -> None:
        with self._connect() as connection:
            self._append_audit_row(connection, event_type, payload, now)

    @staticmethod
    def _append_audit_row(
        connection: sqlite3.Connection,
        event_type: str,
        payload: Mapping[str, Any],
        now: datetime,
    ) -> None:
        created_at = now.astimezone(SHANGHAI_TZ).isoformat()
        payload_json = _canonical_json(payload)
        previous = connection.execute(
            "SELECT event_hash FROM shadow_audit ORDER BY sequence DESC LIMIT 1"
        ).fetchone()
        previous_hash = previous["event_hash"] if previous else "0" * 64
        event_hash = hashlib.sha256(
            (
                previous_hash
                + "\0"
                + event_type
                + "\0"
                + payload_json
                + "\0"
                + created_at
            ).encode("utf-8")
        ).hexdigest()
        connection.execute(
            """
            INSERT INTO shadow_audit(
                event_type, payload_json, previous_hash, event_hash, created_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (event_type, payload_json, previous_hash, event_hash, created_at),
        )

    def _verify_audit_chain(self) -> None:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM shadow_audit ORDER BY sequence"
            ).fetchall()
        self._verify_audit_rows(rows)

    @staticmethod
    def _verify_audit_rows(rows: list[sqlite3.Row]) -> None:
        previous_hash = "0" * 64
        for row in rows:
            if row["previous_hash"] != previous_hash:
                raise ValueError("research-shadow audit chain previous hash mismatch")
            expected = hashlib.sha256(
                (
                    previous_hash
                    + "\0"
                    + row["event_type"]
                    + "\0"
                    + row["payload_json"]
                    + "\0"
                    + row["created_at"]
                ).encode("utf-8")
            ).hexdigest()
            if row["event_hash"] != expected:
                raise ValueError("research-shadow audit chain event hash mismatch")
            previous_hash = expected

    def _audit_summary(self) -> dict[str, Any]:
        self._verify_audit_chain()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT sequence, event_type, event_hash, created_at "
                "FROM shadow_audit ORDER BY sequence DESC LIMIT 1"
            ).fetchone()
        return {
            "valid": True,
            "events": int(row["sequence"]) if row else 0,
            "head": row["event_hash"] if row else None,
            "lastEvent": row["event_type"] if row else None,
            "lastEventAt": row["created_at"] if row else None,
        }

    def _backup_database(self, backup_path: Path) -> None:
        with sqlite3.connect(self.db_path) as source, sqlite3.connect(backup_path) as target:
            source.backup(target)

    def _restore_database(self, backup_path: Path) -> None:
        for suffix in ("-wal", "-shm"):
            Path(str(self.db_path) + suffix).unlink(missing_ok=True)
        shutil.copy2(backup_path, self.db_path)
