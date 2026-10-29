from __future__ import annotations

import json
import math
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Iterable, Mapping

from quantaalpha.trading.execution_contract import (
    position_sessions_sha256,
    symbols_sha256,
    validate_topk_dropout_contract,
)


PAPER_BENCHMARK_SYMBOL = "000300.SH"
PAPER_ENGINEERING_SESSIONS = 20
PAPER_EARLY_EVIDENCE_SESSIONS = 60
PAPER_STRONG_EVIDENCE_SESSIONS = 120
PAPER_DRAWDOWN_WARNING = -0.10
PAPER_DRAWDOWN_STOP = -0.15
PAPER_EXECUTION_MODEL_VERSION = 1
PAPER_CAUSAL_EXECUTION_MODEL_VERSION = 2


def normalize_symbol(symbol: str) -> str:
    raw = symbol.strip().upper()
    if len(raw) == 8 and raw[:2] in {"SH", "SZ"} and raw[2:].isdigit():
        return f"{raw[2:]}.{raw[:2]}"
    if len(raw) == 9 and raw[6] == "." and raw[:6].isdigit() and raw[7:] in {"SH", "SZ"}:
        return raw
    if len(raw) == 6 and raw.isdigit():
        exchange = "SH" if raw.startswith(("6", "9")) else "SZ"
        return f"{raw}.{exchange}"
    raise ValueError(f"unsupported A-share symbol: {symbol}")


@dataclass(frozen=True)
class DailyBar:
    symbol: str
    trade_date: str
    open: float
    high: float
    low: float
    close: float
    volume: float
    previous_close: float | None = None
    factor: float | None = None
    listing_sessions: int | None = None


@dataclass(frozen=True)
class PaperTradingConfig:
    lot_size: int = 100
    buy_cost_rate: float = 0.0005
    sell_cost_rate: float = 0.0015
    minimum_cost: float = 5.0
    slippage_bps: float = 10.0
    limit_threshold: float = 0.095
    execution_model_version: int = PAPER_EXECUTION_MODEL_VERSION
    growth_limit_threshold: float = 0.195
    star_minimum_order_size: int = 200
    star_order_increment: int = 1
    require_listing_sessions: bool = False


class PaperTradingEngine:
    """Persistent daily paper account for manual targets and frozen model plans."""

    def __init__(self, db_path: str | Path, config: PaperTradingConfig | None = None):
        self.db_path = Path(db_path)
        self.config = config or PaperTradingConfig()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS accounts (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    initial_cash REAL NOT NULL,
                    cash REAL NOT NULL,
                    last_trade_date TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS signal_batches (
                    id TEXT PRIMARY KEY,
                    account_id TEXT NOT NULL REFERENCES accounts(id),
                    signal_date TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS signals (
                    batch_id TEXT NOT NULL REFERENCES signal_batches(id),
                    symbol TEXT NOT NULL,
                    target_weight REAL NOT NULL,
                    PRIMARY KEY (batch_id, symbol)
                );
                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY,
                    account_id TEXT NOT NULL REFERENCES accounts(id),
                    signal_batch_id TEXT NOT NULL UNIQUE REFERENCES signal_batches(id),
                    trade_date TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE (account_id, trade_date)
                );
                CREATE TABLE IF NOT EXISTS positions (
                    account_id TEXT NOT NULL REFERENCES accounts(id),
                    symbol TEXT NOT NULL,
                    quantity INTEGER NOT NULL,
                    available_quantity INTEGER NOT NULL,
                    average_cost REAL NOT NULL,
                    last_price REAL NOT NULL,
                    holding_sessions INTEGER NOT NULL DEFAULT 1,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (account_id, symbol)
                );
                CREATE TABLE IF NOT EXISTS orders (
                    id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL REFERENCES runs(id),
                    account_id TEXT NOT NULL REFERENCES accounts(id),
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL,
                    quantity INTEGER NOT NULL,
                    trade_date TEXT NOT NULL,
                    status TEXT NOT NULL,
                    reason TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS fills (
                    id TEXT PRIMARY KEY,
                    order_id TEXT NOT NULL UNIQUE REFERENCES orders(id),
                    account_id TEXT NOT NULL REFERENCES accounts(id),
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL,
                    quantity INTEGER NOT NULL,
                    trade_date TEXT NOT NULL,
                    price REAL NOT NULL,
                    gross_amount REAL NOT NULL,
                    transaction_cost REAL NOT NULL,
                    slippage_cost REAL NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS nav_history (
                    account_id TEXT NOT NULL REFERENCES accounts(id),
                    trade_date TEXT NOT NULL,
                    cash REAL NOT NULL,
                    market_value REAL NOT NULL,
                    total_value REAL NOT NULL,
                    daily_return REAL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (account_id, trade_date)
                );
                """
            )
            columns = {row[1] for row in connection.execute("PRAGMA table_info(signal_batches)")}
            if "source_type" not in columns:
                connection.execute("ALTER TABLE signal_batches ADD COLUMN source_type TEXT NOT NULL DEFAULT 'manual'")
            if "source_ref" not in columns:
                connection.execute("ALTER TABLE signal_batches ADD COLUMN source_ref TEXT")
            if "metadata_json" not in columns:
                connection.execute("ALTER TABLE signal_batches ADD COLUMN metadata_json TEXT")
            nav_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(nav_history)")
            }
            if "benchmark_close" not in nav_columns:
                connection.execute("ALTER TABLE nav_history ADD COLUMN benchmark_close REAL")
            if "benchmark_value" not in nav_columns:
                connection.execute("ALTER TABLE nav_history ADD COLUMN benchmark_value REAL")
            if "benchmark_daily_return" not in nav_columns:
                connection.execute(
                    "ALTER TABLE nav_history ADD COLUMN benchmark_daily_return REAL"
                )
            for table in ("positions", "orders", "fills"):
                table_columns = {
                    row[1]
                    for row in connection.execute(f"PRAGMA table_info({table})")
                }
                if "factor" not in table_columns:
                    connection.execute(
                        f"ALTER TABLE {table} ADD COLUMN factor REAL NOT NULL DEFAULT 1.0"
                    )
            position_columns = {
                row[1]
                for row in connection.execute("PRAGMA table_info(positions)")
            }
            if "holding_sessions" not in position_columns:
                connection.execute(
                    "ALTER TABLE positions ADD COLUMN holding_sessions "
                    "INTEGER NOT NULL DEFAULT 1"
                )

    def ensure_account(
        self,
        account_id: str = "default",
        name: str = "A股日频模拟盘",
        initial_cash: float = 1_000_000.0,
    ) -> dict:
        if initial_cash <= 0:
            raise ValueError("initial_cash must be positive")
        with self._connect() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO accounts(id, name, initial_cash, cash) VALUES (?, ?, ?, ?)",
                (account_id, name, float(initial_cash), float(initial_cash)),
            )
        return self.get_state(account_id)

    def reset_account(self, account_id: str = "default", initial_cash: float = 1_000_000.0) -> dict:
        if initial_cash <= 0:
            raise ValueError("initial_cash must be positive")
        with self._connect() as connection:
            for table in ("fills", "orders", "nav_history", "positions", "runs", "signals", "signal_batches"):
                if table in {"fills", "orders", "nav_history", "positions", "runs"}:
                    connection.execute(f"DELETE FROM {table} WHERE account_id = ?", (account_id,))
                elif table == "signals":
                    connection.execute(
                        "DELETE FROM signals WHERE batch_id IN (SELECT id FROM signal_batches WHERE account_id = ?)",
                        (account_id,),
                    )
                else:
                    connection.execute("DELETE FROM signal_batches WHERE account_id = ?", (account_id,))
            connection.execute(
                """
                UPDATE accounts SET initial_cash = ?, cash = ?, last_trade_date = NULL,
                    updated_at = CURRENT_TIMESTAMP WHERE id = ?
                """,
                (float(initial_cash), float(initial_cash), account_id),
            )
        return self.get_state(account_id)

    def submit_targets(
        self,
        signal_date: str,
        targets: Mapping[str, float],
        account_id: str = "default",
        source: Mapping[str, object] | None = None,
    ) -> dict:
        parsed_date = date.fromisoformat(signal_date).isoformat()
        normalized: dict[str, float] = {}
        for symbol, weight in targets.items():
            canonical = normalize_symbol(symbol)
            numeric_weight = float(weight)
            if numeric_weight < 0 or numeric_weight > 1:
                raise ValueError(f"target weight for {canonical} must be between 0 and 1")
            if canonical in normalized:
                raise ValueError(f"duplicate target symbol: {canonical}")
            normalized[canonical] = numeric_weight
        if sum(normalized.values()) > 1.000001:
            raise ValueError("target weights must not exceed 100%")

        source_payload = dict(source or {"type": "manual"})
        source_type = str(source_payload.get("type") or "manual")
        raw_source_ref = source_payload.get("ref")
        source_ref = str(raw_source_ref) if raw_source_ref is not None else None
        metadata_json = json.dumps(source_payload, ensure_ascii=False, sort_keys=True)

        batch_id = f"signal_{uuid.uuid4().hex[:12]}"
        with self._connect() as connection:
            self._require_account(connection, account_id)
            self._validate_observation_lineage_on_submit(
                connection,
                account_id,
                source_type,
                source_ref,
            )
            existing = connection.execute(
                "SELECT id, signal_date FROM signal_batches WHERE account_id = ? AND status = 'pending'",
                (account_id,),
            ).fetchone()
            if existing:
                raise ValueError(
                    f"a pending signal already exists for {existing['signal_date']}"
                )
            connection.execute(
                """
                INSERT INTO signal_batches(
                    id, account_id, signal_date, status, source_type, source_ref, metadata_json
                ) VALUES (?, ?, ?, 'pending', ?, ?, ?)
                """,
                (batch_id, account_id, parsed_date, source_type, source_ref, metadata_json),
            )
            connection.executemany(
                "INSERT INTO signals(batch_id, symbol, target_weight) VALUES (?, ?, ?)",
                [(batch_id, symbol, weight) for symbol, weight in sorted(normalized.items())],
            )
        return {
            "signalId": batch_id,
            "signalDate": parsed_date,
            "status": "pending",
            "targets": normalized,
            "source": source_payload,
        }

    @staticmethod
    def _validate_observation_lineage_on_submit(
        connection: sqlite3.Connection,
        account_id: str,
        source_type: str,
        source_ref: str | None,
    ) -> None:
        completed_sources = connection.execute(
            """
            SELECT DISTINCT signal_batches.source_type, signal_batches.source_ref
            FROM runs
            JOIN signal_batches ON signal_batches.id = runs.signal_batch_id
            WHERE runs.account_id = ? AND runs.status = 'completed'
            """,
            (account_id,),
        ).fetchall()
        if not completed_sources:
            if source_type == "model_snapshot" and not source_ref:
                raise ValueError("模型信号缺少策略快照标识")
            return

        completed_model_refs = {
            str(row["source_ref"])
            for row in completed_sources
            if row["source_type"] == "model_snapshot" and row["source_ref"]
        }
        has_non_model_run = any(
            row["source_type"] != "model_snapshot" for row in completed_sources
        )
        has_unidentified_model_run = any(
            row["source_type"] == "model_snapshot" and not row["source_ref"]
            for row in completed_sources
        )
        if source_type == "model_snapshot":
            if not source_ref:
                raise ValueError("模型信号缺少策略快照标识")
            if has_non_model_run:
                raise ValueError("账户已有手工执行记录，请重置后开始模型前向观察")
            if has_unidentified_model_run:
                raise ValueError("现有模型观察记录缺少快照标识，请重置账户")
            if completed_model_refs != {source_ref}:
                raise ValueError("策略快照已变化，请重置账户后开始新的前向观察期")
        elif completed_model_refs:
            raise ValueError("账户正在进行模型前向观察，请重置后再执行手工目标")

    def next_pending_signal(self, account_id: str = "default") -> dict | None:
        with self._connect() as connection:
            batch = connection.execute(
                """
                SELECT * FROM signal_batches WHERE account_id = ? AND status = 'pending'
                ORDER BY signal_date, created_at LIMIT 1
                """,
                (account_id,),
            ).fetchone()
            if batch is None:
                return None
            targets = connection.execute(
                "SELECT symbol, target_weight FROM signals WHERE batch_id = ? ORDER BY symbol",
                (batch["id"],),
            ).fetchall()
            positions = connection.execute(
                "SELECT symbol FROM positions WHERE account_id = ? AND quantity > 0",
                (account_id,),
            ).fetchall()
        return {
            "signalId": batch["id"],
            "signalDate": batch["signal_date"],
            "targets": {row["symbol"]: row["target_weight"] for row in targets},
            "symbols": sorted({row["symbol"] for row in targets} | {row["symbol"] for row in positions}),
            "source": self._signal_source(batch),
        }

    def latest_signal(self, account_id: str = "default") -> dict | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM signal_batches WHERE account_id = ?
                ORDER BY signal_date DESC, created_at DESC, rowid DESC LIMIT 1
                """,
                (account_id,),
            ).fetchone()
        if row is None:
            return None
        return {
            "signalId": row["id"],
            "signalDate": row["signal_date"],
            "status": row["status"],
            "source": self._signal_source(row),
        }

    def execute_pending(
        self,
        bars: Mapping[str, DailyBar],
        trade_date: str,
        account_id: str = "default",
        benchmark_bar: DailyBar | None = None,
    ) -> dict:
        pending = self.next_pending_signal(account_id)
        if pending is None:
            raise ValueError("no pending target signal")
        return self.execute_signal(
            pending["signalId"],
            bars,
            trade_date,
            account_id,
            benchmark_bar=benchmark_bar,
        )

    def execute_signal(
        self,
        signal_id: str,
        bars: Mapping[str, DailyBar],
        trade_date: str,
        account_id: str = "default",
        benchmark_bar: DailyBar | None = None,
    ) -> dict:
        parsed_trade_date = date.fromisoformat(trade_date).isoformat()
        canonical_bars = {normalize_symbol(symbol): bar for symbol, bar in bars.items()}

        with self._connect() as connection:
            account = self._require_account(connection, account_id)
            batch = connection.execute(
                "SELECT * FROM signal_batches WHERE id = ? AND account_id = ?",
                (signal_id, account_id),
            ).fetchone()
            if batch is None:
                raise ValueError(f"unknown signal batch: {signal_id}")
            if parsed_trade_date <= batch["signal_date"]:
                raise ValueError("trade date must be after signal date")
            if batch["status"] != "pending":
                raise ValueError(f"signal batch is already {batch['status']}")
            same_day = connection.execute(
                "SELECT id FROM runs WHERE account_id = ? AND trade_date = ?",
                (account_id, parsed_trade_date),
            ).fetchone()
            if same_day:
                raise ValueError(f"account already ran on {parsed_trade_date}")

            target_rows = connection.execute(
                "SELECT symbol, target_weight FROM signals WHERE batch_id = ?",
                (signal_id,),
            ).fetchall()
            targets = {row["symbol"]: float(row["target_weight"]) for row in target_rows}
            position_rows = connection.execute(
                "SELECT * FROM positions WHERE account_id = ?",
                (account_id,),
            ).fetchall()
            positions = {row["symbol"]: dict(row) for row in position_rows}
            symbols = sorted(set(targets) | set(positions))
            source = self._signal_source(batch)
            execution_plan = self._validated_execution_plan(source, positions, targets)
            if execution_plan is not None:
                missing_factors = sorted(
                    symbol
                    for symbol in symbols
                    if canonical_bars.get(symbol) is not None
                    and canonical_bars[symbol].factor is None
                )
                if missing_factors:
                    raise ValueError(
                        "模型模拟盘行情缺少 Qlib factor，无法执行真实整手换算: "
                        + ", ".join(missing_factors)
                    )

            for symbol, bar in canonical_bars.items():
                if bar.trade_date != parsed_trade_date:
                    raise ValueError(f"bar date mismatch for {symbol}: {bar.trade_date}")

            previous_nav = connection.execute(
                "SELECT * FROM nav_history WHERE account_id = ? ORDER BY trade_date DESC LIMIT 1",
                (account_id,),
            ).fetchone()
            benchmark_close, benchmark_value, benchmark_daily_return = self._benchmark_values(
                account,
                previous_nav,
                benchmark_bar,
                parsed_trade_date,
                required=execution_plan is not None,
            )

            run_id = f"run_{uuid.uuid4().hex[:12]}"
            connection.execute(
                "INSERT INTO runs(id, account_id, signal_batch_id, trade_date, status) VALUES (?, ?, ?, ?, 'running')",
                (run_id, account_id, signal_id, parsed_trade_date),
            )
            connection.execute(
                "UPDATE positions SET available_quantity = quantity WHERE account_id = ?",
                (account_id,),
            )
            for position in positions.values():
                position["available_quantity"] = position["quantity"]

            cash = float(account["cash"])
            pre_trade_value = cash
            for symbol, position in positions.items():
                bar = canonical_bars.get(symbol)
                usable_at_open = bool(
                    bar
                    and (
                        self._open_bar_is_usable(bar)
                        if self.config.execution_model_version
                        >= PAPER_CAUSAL_EXECUTION_MODEL_VERSION
                        else self._bar_is_usable(bar)
                    )
                )
                mark = bar.open if usable_at_open else position["last_price"]
                pre_trade_value += position["quantity"] * mark

            if execution_plan is not None:
                cash = self._execute_topk_dropout_plan(
                    connection,
                    run_id,
                    account_id,
                    positions,
                    canonical_bars,
                    parsed_trade_date,
                    execution_plan,
                    cash,
                )
            else:
                desired: dict[str, float | None] = {}
                for symbol in symbols:
                    bar = canonical_bars.get(symbol)
                    if bar is None or not self._bar_is_usable(bar):
                        desired[symbol] = None
                        continue
                    target_value = pre_trade_value * targets.get(symbol, 0.0)
                    desired[symbol] = self._round_amount_by_trade_unit(
                        target_value / bar.open,
                        bar,
                    )

                deltas = {
                    symbol: None if desired[symbol] is None else desired[symbol] - positions.get(symbol, {}).get("quantity", 0)
                    for symbol in symbols
                }

                for symbol in symbols:
                    if deltas[symbol] is not None:
                        continue
                    position_quantity = positions.get(symbol, {}).get("quantity", 0)
                    side = "sell" if position_quantity and targets.get(symbol, 0.0) == 0 else "buy"
                    bar = canonical_bars.get(symbol)
                    reason = self._blocked_reason(bar, side) if bar else "缺少交易日行情"
                    self._record_rejected_order(
                        connection,
                        run_id,
                        account_id,
                        symbol,
                        side,
                        position_quantity if side == "sell" else 0,
                        parsed_trade_date,
                        reason,
                        (
                            self._bar_factor(bar)
                            if bar is not None and bar.factor is not None
                            else float(positions.get(symbol, {}).get("factor", 1.0))
                        ),
                    )

                for symbol in symbols:
                    delta = deltas[symbol]
                    if (
                        delta is None
                        or not math.isclose(delta, 0.0, rel_tol=0, abs_tol=1e-9)
                        or targets.get(symbol, 0.0) <= 0
                    ):
                        continue
                    if positions.get(symbol, {}).get("quantity", 0) > 0:
                        continue
                    self._record_rejected_order(
                        connection,
                        run_id,
                        account_id,
                        symbol,
                        "buy",
                        0,
                        parsed_trade_date,
                        "目标金额不足一手",
                    )

                for symbol in symbols:
                    delta = deltas[symbol]
                    if delta is None or delta >= -1e-9:
                        continue
                    bar = canonical_bars[symbol]
                    available = float(positions[symbol]["available_quantity"])
                    quantity = min(-delta, available)
                    if not math.isclose(quantity, available, rel_tol=0, abs_tol=1e-9):
                        quantity = self._round_amount_by_trade_unit(quantity, bar)
                    cash = self._process_order(
                        connection, run_id, account_id, positions, bar, "sell", quantity, cash
                    )

                for symbol in symbols:
                    delta = deltas[symbol]
                    if delta is None or delta <= 1e-9:
                        continue
                    bar = canonical_bars[symbol]
                    cash = self._process_order(
                        connection, run_id, account_id, positions, bar, "buy", delta, cash
                    )

            market_value = 0.0
            for symbol, position in list(positions.items()):
                if position["quantity"] <= 0:
                    connection.execute(
                        "DELETE FROM positions WHERE account_id = ? AND symbol = ?", (account_id, symbol)
                    )
                    del positions[symbol]
                    continue
                bar = canonical_bars.get(symbol)
                if bar and self._bar_is_usable(bar):
                    position["last_price"] = bar.close
                    position["factor"] = self._bar_factor(bar)
                position["holding_sessions"] = int(
                    position.get("holding_sessions", 0)
                ) + 1
                market_value += position["quantity"] * position["last_price"]
                connection.execute(
                    """
                    INSERT INTO positions(
                        account_id, symbol, quantity, available_quantity,
                        average_cost, last_price, factor, holding_sessions
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(account_id, symbol) DO UPDATE SET
                        quantity = excluded.quantity,
                        available_quantity = excluded.available_quantity,
                        average_cost = excluded.average_cost,
                        last_price = excluded.last_price,
                        factor = excluded.factor,
                        holding_sessions = excluded.holding_sessions,
                        updated_at = CURRENT_TIMESTAMP
                    """,
                    (
                        account_id,
                        symbol,
                        position["quantity"],
                        position["available_quantity"],
                        position["average_cost"],
                        position["last_price"],
                        position.get("factor", 1.0),
                        position["holding_sessions"],
                    ),
                )

            total_value = cash + market_value
            previous_total_value = (
                float(previous_nav["total_value"])
                if previous_nav is not None
                else float(account["initial_cash"])
            )
            daily_return = (
                total_value / previous_total_value - 1.0
                if previous_total_value > 0
                else None
            )
            connection.execute(
                """
                INSERT INTO nav_history(
                    account_id, trade_date, cash, market_value, total_value, daily_return,
                    benchmark_close, benchmark_value, benchmark_daily_return
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    account_id,
                    parsed_trade_date,
                    cash,
                    market_value,
                    total_value,
                    daily_return,
                    benchmark_close,
                    benchmark_value,
                    benchmark_daily_return,
                ),
            )
            connection.execute(
                """
                UPDATE accounts SET cash = ?, last_trade_date = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (cash, parsed_trade_date, account_id),
            )
            connection.execute("UPDATE runs SET status = 'completed' WHERE id = ?", (run_id,))
            connection.execute("UPDATE signal_batches SET status = 'executed' WHERE id = ?", (signal_id,))

        state = self.get_state(account_id)
        state["latestRun"] = {"runId": run_id, "signalId": signal_id, "tradeDate": parsed_trade_date}
        return state

    def _benchmark_values(
        self,
        account: sqlite3.Row,
        previous_nav: sqlite3.Row | None,
        benchmark_bar: DailyBar | None,
        trade_date: str,
        required: bool,
    ) -> tuple[float | None, float | None, float | None]:
        if benchmark_bar is None:
            if required:
                raise ValueError("模型模拟盘执行缺少沪深300基准行情")
            return None, None, None
        if normalize_symbol(benchmark_bar.symbol) != PAPER_BENCHMARK_SYMBOL:
            raise ValueError("模拟盘基准必须是沪深300 000300.SH")
        if benchmark_bar.trade_date != trade_date:
            raise ValueError(
                f"benchmark bar date mismatch: {benchmark_bar.trade_date} != {trade_date}"
            )
        if not self._bar_is_usable(benchmark_bar) or benchmark_bar.close <= 0:
            raise ValueError("沪深300基准行情不可用")

        benchmark_close = float(benchmark_bar.close)
        if previous_nav is None:
            previous_close = benchmark_bar.previous_close
            if previous_close is None or not math.isfinite(previous_close) or previous_close <= 0:
                if required:
                    raise ValueError("模型模拟盘首次执行缺少沪深300前收盘价")
                return benchmark_close, None, None
            benchmark_daily_return = benchmark_close / previous_close - 1.0
            benchmark_value = float(account["initial_cash"]) * (
                1.0 + benchmark_daily_return
            )
            return benchmark_close, benchmark_value, benchmark_daily_return

        previous_close = previous_nav["benchmark_close"]
        previous_value = previous_nav["benchmark_value"]
        if previous_close is None or previous_value is None:
            if required:
                raise ValueError("现有模拟盘净值缺少连续基准，请重置账户后重新开始")
            return benchmark_close, None, None
        benchmark_daily_return = benchmark_close / float(previous_close) - 1.0
        benchmark_value = float(previous_value) * (1.0 + benchmark_daily_return)
        return benchmark_close, benchmark_value, benchmark_daily_return

    def _validated_execution_plan(
        self,
        source: Mapping[str, object],
        positions: Mapping[str, Mapping[str, object]],
        targets: Mapping[str, float],
    ) -> dict | None:
        if source.get("type") != "model_snapshot":
            return None
        if source.get("executionMode") != "topk_dropout":
            raise ValueError("模型信号缺少受支持的冻结执行模式")

        try:
            contract = validate_topk_dropout_contract(
                source.get("executionContract")
            )
        except ValueError as exc:
            raise ValueError(f"模型信号执行契约无效：{exc}") from exc

        plan = source.get("executionPlan")
        if not isinstance(plan, dict):
            raise ValueError("模型信号缺少冻结换仓计划")
        try:
            top_k = contract["topK"]
            n_drop = contract["nDrop"]
            risk_degree = contract["riskDegree"]
            hold_threshold = contract["holdThreshold"]
            plan_top_k = int(plan["topK"])
            plan_n_drop = int(plan["nDrop"])
            plan_risk_degree = float(plan["riskDegree"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("模型信号的 TopK 执行参数无效") from exc
        if top_k < 1 or n_drop < 1 or n_drop > top_k or not 0 < risk_degree <= 1:
            raise ValueError("模型信号的冻结执行参数超出范围")
        if (
            plan_top_k != top_k
            or plan_n_drop != n_drop
            or not math.isclose(plan_risk_degree, risk_degree, rel_tol=0, abs_tol=1e-12)
        ):
            raise ValueError("模型信号的换仓计划与执行契约不一致")

        normalized: dict[str, list[str]] = {}
        fields = [
            "currentSymbols",
            "keepSymbols",
            "sellSymbols",
            "buySymbols",
            "desiredSymbols",
        ]
        if contract["version"] >= 2:
            fields.extend(("sellCandidates", "holdBlockedSymbols"))
        for field in fields:
            values = plan.get(field)
            if not isinstance(values, list):
                raise ValueError(f"模型信号换仓计划缺少 {field}")
            try:
                symbols = [normalize_symbol(str(value)) for value in values]
            except ValueError as exc:
                raise ValueError(f"模型信号换仓计划的 {field} 含无效股票") from exc
            if len(symbols) != len(set(symbols)):
                raise ValueError(f"模型信号换仓计划的 {field} 含重复股票")
            normalized[field] = symbols

        current = normalized["currentSymbols"]
        keep = normalized["keepSymbols"]
        sell = normalized["sellSymbols"]
        buy = normalized["buySymbols"]
        desired = normalized["desiredSymbols"]
        sell_candidates = (
            normalized["sellCandidates"]
            if contract["version"] >= 2
            else list(sell)
        )
        hold_blocked = (
            normalized["holdBlockedSymbols"]
            if contract["version"] >= 2
            else []
        )
        actual_current = sorted(
            symbol for symbol, position in positions.items() if int(position.get("quantity", 0)) > 0
        )
        if sorted(current) != actual_current:
            raise ValueError("模型信号生成后账户持仓已变化，请重新生成信号")
        if len(current) > top_k + n_drop:
            raise ValueError("模型账户持仓超过冻结策略可恢复上限")

        if plan.get("currentSymbolsSha256") != symbols_sha256(current):
            raise ValueError("模型信号的持仓指纹校验失败")

        actual_sessions: dict[str, int] = {}
        for symbol in actual_current:
            try:
                sessions = int(positions[symbol].get("holding_sessions", 1))
            except (TypeError, ValueError) as exc:
                raise ValueError("模型账户的持有交易日无效") from exc
            if sessions < 1:
                raise ValueError("模型账户的持有交易日无效")
            actual_sessions[symbol] = sessions
        if contract["version"] >= 2:
            raw_sessions = plan.get("currentHoldingSessions")
            if not isinstance(raw_sessions, dict):
                raise ValueError("模型信号换仓计划缺少 currentHoldingSessions")
            plan_sessions: dict[str, int] = {}
            for raw_symbol, raw_value in raw_sessions.items():
                try:
                    symbol = normalize_symbol(str(raw_symbol))
                    sessions = int(raw_value)
                    numeric_value = float(raw_value)
                except (TypeError, ValueError) as exc:
                    raise ValueError("模型信号的持有交易日无效") from exc
                if (
                    symbol in plan_sessions
                    or sessions < 1
                    or not math.isfinite(numeric_value)
                    or numeric_value != sessions
                ):
                    raise ValueError("模型信号的持有交易日无效")
                plan_sessions[symbol] = sessions
            if plan_sessions != actual_sessions:
                raise ValueError("模型信号生成后持有交易日已变化，请重新生成信号")
            if plan.get("currentPositionsSha256") != position_sessions_sha256(
                plan_sessions
            ):
                raise ValueError("模型信号的持仓交易日指纹校验失败")
        else:
            plan_sessions = actual_sessions

        candidate_set = set(sell_candidates)
        if len(sell_candidates) > n_drop or candidate_set - set(current):
            raise ValueError("模型信号的候选卖出计划违反冻结 nDrop 规则")
        expected_sell = [
            symbol
            for symbol in sell_candidates
            if plan_sessions[symbol] >= hold_threshold
        ]
        expected_blocked = [
            symbol
            for symbol in sell_candidates
            if plan_sessions[symbol] < hold_threshold
        ]
        if sell != expected_sell or hold_blocked != expected_blocked:
            raise ValueError("模型信号的最短持有期卖出计划无效")
        if keep != [symbol for symbol in current if symbol not in candidate_set]:
            raise ValueError("模型信号的保留/候选卖出计划与当前持仓不一致")
        if set(buy) & set(current):
            raise ValueError("模型信号买入计划包含已有持仓")
        if desired != [*keep, *buy] or len(desired) != top_k:
            raise ValueError("模型信号的目标组合与换仓计划不一致")
        if len(buy) != len(sell_candidates) + top_k - len(current):
            raise ValueError("模型信号的换仓数量违反冻结 nDrop 规则")
        if set(targets) != set(desired):
            raise ValueError("模型信号的目标股票与冻结换仓计划不一致")
        target_weight = risk_degree / top_k
        if any(
            not math.isclose(float(weight), target_weight, rel_tol=0, abs_tol=1e-12)
            for weight in targets.values()
        ):
            raise ValueError("模型信号的指示权重与冻结执行契约不一致")

        return {
            **normalized,
            "sellCandidates": sell_candidates,
            "holdBlockedSymbols": hold_blocked,
            "currentHoldingSessions": plan_sessions,
            "topK": top_k,
            "nDrop": n_drop,
            "riskDegree": risk_degree,
            "holdThreshold": hold_threshold,
        }

    def _execute_topk_dropout_plan(
        self,
        connection: sqlite3.Connection,
        run_id: str,
        account_id: str,
        positions: dict[str, dict],
        bars: Mapping[str, DailyBar],
        trade_date: str,
        plan: Mapping[str, object],
        cash: float,
    ) -> float:
        for symbol in plan["sellSymbols"]:
            bar = bars.get(symbol)
            quantity = float(positions[symbol]["available_quantity"])
            if bar is None:
                self._record_rejected_order(
                    connection, run_id, account_id, symbol, "sell", quantity,
                    trade_date, "缺少交易日行情",
                    float(positions[symbol].get("factor", 1.0)),
                )
                continue
            reason = self._topk_dropout_blocked_reason(bar)
            if reason:
                self._record_rejected_order(
                    connection, run_id, account_id, symbol, "sell", quantity,
                    trade_date, reason, self._bar_factor(bar),
                )
                continue
            cash = self._process_order(
                connection, run_id, account_id, positions, bar, "sell", quantity, cash
            )

        buy_symbols = plan["buySymbols"]
        buy_value = cash * float(plan["riskDegree"]) / len(buy_symbols) if buy_symbols else 0.0
        for symbol in buy_symbols:
            bar = bars.get(symbol)
            if bar is None:
                self._record_rejected_order(
                    connection, run_id, account_id, symbol, "buy", 0,
                    trade_date, "缺少交易日行情",
                )
                continue
            reason = self._topk_dropout_blocked_reason(bar)
            if reason:
                self._record_rejected_order(
                    connection, run_id, account_id, symbol, "buy", 0,
                    trade_date, reason, self._bar_factor(bar),
                )
                continue
            quantity = self._round_amount_by_trade_unit(buy_value / bar.open, bar)
            cash = self._process_order(
                connection, run_id, account_id, positions, bar, "buy", quantity, cash
            )
        return cash

    def _topk_dropout_blocked_reason(self, bar: DailyBar) -> str | None:
        if self.config.execution_model_version >= PAPER_CAUSAL_EXECUTION_MODEL_VERSION:
            return self._causal_open_blocked_reason(bar)
        if not self._bar_is_usable(bar):
            return "停牌或行情不可用"
        if bar.previous_close and bar.previous_close > 0:
            change = bar.close / bar.previous_close - 1.0
            if abs(change) >= self.config.limit_threshold:
                return "达到涨跌停阈值，冻结策略禁止全部交易"
        return None

    @staticmethod
    def _record_rejected_order(
        connection: sqlite3.Connection,
        run_id: str,
        account_id: str,
        symbol: str,
        side: str,
        quantity: float,
        trade_date: str,
        reason: str,
        factor: float = 1.0,
    ) -> None:
        connection.execute(
            """
            INSERT INTO orders(
                id, run_id, account_id, symbol, side, quantity,
                trade_date, status, reason, factor
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'rejected', ?, ?)
            """,
            (
                f"order_{uuid.uuid4().hex[:12]}", run_id, account_id, symbol, side,
                max(quantity, 0.0), trade_date, reason, factor,
            ),
        )

    def _process_order(
        self,
        connection: sqlite3.Connection,
        run_id: str,
        account_id: str,
        positions: dict[str, dict],
        bar: DailyBar,
        side: str,
        quantity: float,
        cash: float,
    ) -> float:
        symbol = normalize_symbol(bar.symbol)
        reason = self._blocked_reason(bar, side)
        factor = self._bar_factor(bar)
        minimum_order = self._minimum_order_amount(bar)
        trade_increment = self._trade_increment_amount(bar)
        existing = positions.get(symbol)
        selling_entire_position = bool(
            side == "sell"
            and existing
            and math.isclose(
                float(quantity),
                float(existing["available_quantity"]),
                rel_tol=0,
                abs_tol=1e-9,
            )
        )
        if quantity < minimum_order - 1e-9 and not selling_entire_position:
            reason = reason or "交易数量不足一手"

        fill_price = self._fill_price(bar, side)
        rate = self.config.buy_cost_rate if side == "buy" else self.config.sell_cost_rate
        if side == "buy" and reason is None:
            affordable = self._round_amount_by_trade_unit(
                cash / (fill_price * (1.0 + rate)),
                bar,
            )
            quantity = min(quantity, affordable)
            while quantity > 0 and quantity * fill_price + max(self.config.minimum_cost, quantity * fill_price * rate) > cash:
                quantity = max(0.0, quantity - trade_increment)
            if quantity < minimum_order - 1e-9:
                reason = "可用资金不足"

        order_id = f"order_{uuid.uuid4().hex[:12]}"
        status = "rejected" if reason else "filled"
        connection.execute(
            """
            INSERT INTO orders(
                id, run_id, account_id, symbol, side, quantity,
                trade_date, status, reason, factor
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                order_id, run_id, account_id, symbol, side, max(quantity, 0.0),
                bar.trade_date, status, reason, factor,
            ),
        )
        if reason:
            return cash

        gross = quantity * fill_price
        transaction_cost = max(self.config.minimum_cost, gross * rate)
        slippage_cost = abs(fill_price - bar.open) * quantity
        position = positions.setdefault(
            symbol,
            {
                "symbol": symbol,
                "quantity": 0.0,
                "available_quantity": 0.0,
                "average_cost": 0.0,
                "last_price": bar.open,
                "factor": factor,
                "holding_sessions": 0,
            },
        )
        if side == "buy":
            old_quantity = position["quantity"]
            new_quantity = old_quantity + quantity
            position["average_cost"] = (
                old_quantity * position["average_cost"] + gross + transaction_cost
            ) / new_quantity
            position["quantity"] = new_quantity
            cash -= gross + transaction_cost
        else:
            position["quantity"] -= quantity
            position["available_quantity"] -= quantity
            cash += gross - transaction_cost
            if position["quantity"] == 0:
                position["average_cost"] = 0.0
        position["last_price"] = fill_price
        position["factor"] = factor

        connection.execute(
            """
            INSERT INTO fills(
                id, order_id, account_id, symbol, side, quantity, trade_date,
                price, gross_amount, transaction_cost, slippage_cost, factor
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                f"fill_{uuid.uuid4().hex[:12]}", order_id, account_id, symbol, side, quantity,
                bar.trade_date, fill_price, gross, transaction_cost, slippage_cost, factor,
            ),
        )
        return cash

    def _blocked_reason(self, bar: DailyBar, side: str) -> str | None:
        if self.config.execution_model_version >= PAPER_CAUSAL_EXECUTION_MODEL_VERSION:
            return self._causal_open_blocked_reason(bar)
        if not self._bar_is_usable(bar):
            return "停牌或行情不可用"
        if bar.previous_close and bar.previous_close > 0 and math.isclose(bar.high, bar.low, rel_tol=0, abs_tol=1e-8):
            change = bar.open / bar.previous_close - 1.0
            if side == "buy" and change >= self.config.limit_threshold:
                return "一字涨停不可买入"
            if side == "sell" and change <= -self.config.limit_threshold:
                return "一字跌停不可卖出"
        return None

    def _causal_open_blocked_reason(self, bar: DailyBar) -> str | None:
        if not self._open_bar_is_usable(bar):
            return "开盘行情不可用"
        if self.config.require_listing_sessions:
            if bar.listing_sessions is None:
                return "上市交易日龄未知，因果执行模型拒绝交易"
            if bar.listing_sessions <= 5:
                return "上市前五个交易日无涨跌幅限制，因果执行模型拒绝交易"
        if bar.previous_close is None or not math.isfinite(bar.previous_close) or bar.previous_close <= 0:
            return "前收盘价不可用"
        change = bar.open / bar.previous_close - 1.0
        threshold = self._limit_threshold_for_symbol(bar.symbol)
        if abs(change) >= threshold:
            return "开盘达到板块涨跌停阈值，冻结策略禁止全部交易"
        return None

    @staticmethod
    def _bar_is_usable(bar: DailyBar) -> bool:
        values = (bar.open, bar.high, bar.low, bar.close, bar.volume)
        factor_usable = (
            bar.factor is None
            or (math.isfinite(float(bar.factor)) and float(bar.factor) > 0)
        )
        return (
            all(math.isfinite(value) for value in values)
            and bar.open > 0
            and bar.volume > 0
            and factor_usable
        )

    @staticmethod
    def _open_bar_is_usable(bar: DailyBar) -> bool:
        factor_usable = (
            bar.factor is None
            or (math.isfinite(float(bar.factor)) and float(bar.factor) > 0)
        )
        return math.isfinite(bar.open) and bar.open > 0 and factor_usable

    @staticmethod
    def _bar_factor(bar: DailyBar) -> float:
        if bar.factor is None:
            return 1.0
        factor = float(bar.factor)
        if not math.isfinite(factor) or factor <= 0:
            raise ValueError(f"{normalize_symbol(bar.symbol)} 的复权因子不可用")
        return factor

    @staticmethod
    def _is_star_market(symbol: str) -> bool:
        normalized = normalize_symbol(symbol)
        code, exchange = normalized.split(".")
        return exchange == "SH" and code.startswith(("688", "689"))

    @staticmethod
    def _is_twenty_percent_board(symbol: str) -> bool:
        normalized = normalize_symbol(symbol)
        code, exchange = normalized.split(".")
        return (exchange == "SZ" and code.startswith("30")) or (
            exchange == "SH" and code.startswith(("688", "689"))
        )

    def _limit_threshold_for_symbol(self, symbol: str) -> float:
        if self._is_twenty_percent_board(symbol):
            return self.config.growth_limit_threshold
        return self.config.limit_threshold

    def _minimum_order_amount(self, bar: DailyBar) -> float:
        actual_minimum = self.config.lot_size
        if (
            self.config.execution_model_version >= PAPER_CAUSAL_EXECUTION_MODEL_VERSION
            and self._is_star_market(bar.symbol)
        ):
            actual_minimum = self.config.star_minimum_order_size
        return float(actual_minimum) / self._bar_factor(bar)

    def _trade_increment_amount(self, bar: DailyBar) -> float:
        actual_increment = self.config.lot_size
        if (
            self.config.execution_model_version >= PAPER_CAUSAL_EXECUTION_MODEL_VERSION
            and self._is_star_market(bar.symbol)
        ):
            actual_increment = self.config.star_order_increment
        return float(actual_increment) / self._bar_factor(bar)

    def _round_amount_by_trade_unit(self, amount: float, bar: DailyBar) -> float:
        if amount <= 0:
            return 0.0
        factor = self._bar_factor(bar)
        if (
            self.config.execution_model_version >= PAPER_CAUSAL_EXECUTION_MODEL_VERSION
            and self._is_star_market(bar.symbol)
        ):
            actual_shares = math.floor(float(amount) * factor + 1e-9)
            if actual_shares < self.config.star_minimum_order_size:
                return 0.0
            increment = self.config.star_order_increment
            actual_shares -= (
                actual_shares - self.config.star_minimum_order_size
            ) % increment
            return float(actual_shares) / factor
        actual_lots = math.floor(
            (float(amount) * factor + 0.1) / float(self.config.lot_size)
        )
        return actual_lots * float(self.config.lot_size) / factor

    @staticmethod
    def _display_quantity(adjusted_quantity: float, factor: float) -> float | int:
        actual = float(adjusted_quantity) * float(factor)
        nearest = round(actual)
        return int(nearest) if math.isclose(actual, nearest, rel_tol=0, abs_tol=1e-6) else actual

    def _fill_price(self, bar: DailyBar, side: str) -> float:
        multiplier = 1.0 + (self.config.slippage_bps / 10_000.0) * (1 if side == "buy" else -1)
        if self.config.execution_model_version >= PAPER_CAUSAL_EXECUTION_MODEL_VERSION:
            return bar.open * multiplier
        return min(max(bar.open * multiplier, bar.low), bar.high)

    @staticmethod
    def _require_account(connection: sqlite3.Connection, account_id: str) -> sqlite3.Row:
        account = connection.execute("SELECT * FROM accounts WHERE id = ?", (account_id,)).fetchone()
        if account is None:
            raise ValueError(f"unknown paper account: {account_id}")
        return account

    @staticmethod
    def _signal_source(row: sqlite3.Row) -> dict:
        raw = row["metadata_json"] if "metadata_json" in row.keys() else None
        if raw:
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, dict):
                    return parsed
            except (TypeError, json.JSONDecodeError):
                pass
        return {
            "type": row["source_type"] if "source_type" in row.keys() else "manual",
            "ref": row["source_ref"] if "source_ref" in row.keys() else None,
        }

    @classmethod
    def _position_state(cls, row: sqlite3.Row) -> dict:
        factor = float(row["factor"] if "factor" in row.keys() else 1.0)
        return {
            "symbol": row["symbol"],
            "quantity": cls._display_quantity(row["quantity"], factor),
            "availableQuantity": cls._display_quantity(
                row["available_quantity"], factor
            ),
            "averageCost": float(row["average_cost"]) / factor,
            "lastPrice": float(row["last_price"]) / factor,
            "marketValue": row["quantity"] * row["last_price"],
            "holdingSessions": int(
                row["holding_sessions"] if "holding_sessions" in row.keys() else 1
            ),
        }

    @classmethod
    def _order_state(cls, row: sqlite3.Row) -> dict:
        payload = dict(row)
        factor = float(payload.get("factor") or 1.0)
        payload["quantity"] = cls._display_quantity(payload["quantity"], factor)
        return payload

    @classmethod
    def _fill_state(cls, row: sqlite3.Row) -> dict:
        payload = dict(row)
        factor = float(payload.get("factor") or 1.0)
        payload["quantity"] = cls._display_quantity(payload["quantity"], factor)
        payload["price"] = float(payload["price"]) / factor
        return payload

    @staticmethod
    def _relative_return(value_return: float | None, benchmark_return: float | None) -> float | None:
        if value_return is None or benchmark_return is None or 1.0 + benchmark_return <= 0:
            return None
        return (1.0 + value_return) / (1.0 + benchmark_return) - 1.0

    @classmethod
    def _paper_validation_summary(
        cls,
        account: sqlite3.Row,
        nav: list[sqlite3.Row],
        market_calendar: Iterable[str] | None,
    ) -> dict:
        sessions = len(nav)
        initial_cash = float(account["initial_cash"])
        model_rows = [row for row in nav if row["signal_source_type"] == "model_snapshot"]
        manual_sessions = sessions - len(model_rows)
        snapshot_refs = {
            str(row["signal_source_ref"])
            for row in model_rows
            if row["signal_source_ref"]
        }
        lineage_reasons: list[str] = []
        if sessions and manual_sessions:
            lineage_reasons.append(f"观察期包含 {manual_sessions} 个手工或未知来源交易日")
        if sessions and not model_rows:
            lineage_reasons.append("观察期没有模型快照交易日")
        if sessions and any(not row["signal_source_ref"] for row in model_rows):
            lineage_reasons.append("模型交易日缺少策略快照标识")
        if len(snapshot_refs) > 1:
            lineage_reasons.append("观察期混用了多个策略快照，必须重置账户重新开始")
        lineage_valid = sessions > 0 and not lineage_reasons

        missing_benchmark_sessions = sum(
            1
            for row in nav
            if row["benchmark_close"] is None
            or row["benchmark_value"] is None
            or row["benchmark_daily_return"] is None
        )
        calendar_complete: bool | None = None
        missing_market_sessions: list[str] = []
        if market_calendar is not None:
            calendar_complete = True
            if nav:
                observed_dates = {str(row["trade_date"]) for row in nav}
                first_date = str(nav[0]["trade_date"])
                last_date = str(nav[-1]["trade_date"])
                expected_dates = {
                    str(value)
                    for value in market_calendar
                    if first_date <= str(value) <= last_date
                }
                missing_market_sessions = sorted(expected_dates - observed_dates)
                calendar_complete = not missing_market_sessions

        reasons = list(lineage_reasons)
        if not sessions:
            reasons.append("尚未开始冻结策略的前向观察")
        elif missing_benchmark_sessions:
            reasons.append(f"有 {missing_benchmark_sessions} 个交易日缺少连续沪深300基准")
        if sessions and calendar_complete is None:
            reasons.append("尚未使用 Provider 交易日历核验观察期连续性")
        elif sessions and not calendar_complete:
            reasons.append(f"观察期缺少 {len(missing_market_sessions)} 个市场交易日")

        data_complete = (
            lineage_valid
            and missing_benchmark_sessions == 0
            and calendar_complete is True
        )
        strategy_return = None
        benchmark_return = None
        excess_return = None
        max_drawdown = 0.0
        if nav:
            strategy_return = float(nav[-1]["total_value"]) / initial_cash - 1.0
            if missing_benchmark_sessions == 0:
                benchmark_return = float(nav[-1]["benchmark_value"]) / initial_cash - 1.0
                excess_return = cls._relative_return(strategy_return, benchmark_return)
            peak = initial_cash
            for row in nav:
                value = float(row["total_value"])
                peak = max(peak, value)
                max_drawdown = min(max_drawdown, value / peak - 1.0)

        warning_triggered = max_drawdown <= PAPER_DRAWDOWN_WARNING
        stop_triggered = max_drawdown <= PAPER_DRAWDOWN_STOP
        engineering_gate = data_complete and sessions >= PAPER_ENGINEERING_SESSIONS
        early_metric_gate = (
            data_complete
            and sessions >= PAPER_EARLY_EVIDENCE_SESSIONS
            and excess_return is not None
            and excess_return > 0
            and not warning_triggered
        )
        strong_metric_gate = (
            early_metric_gate
            and sessions >= PAPER_STRONG_EVIDENCE_SESSIONS
            and not stop_triggered
        )

        if sessions == 0:
            stage = "NOT_STARTED"
        elif not data_complete:
            stage = "INVALID"
        elif stop_triggered:
            stage = "STOPPED"
        elif sessions < PAPER_ENGINEERING_SESSIONS:
            stage = "ENGINEERING"
        elif sessions < PAPER_EARLY_EVIDENCE_SESSIONS:
            stage = "FORWARD_OBSERVATION"
        elif sessions < PAPER_STRONG_EVIDENCE_SESSIONS:
            stage = "EARLY_METRIC_PASS" if early_metric_gate else "EARLY_METRIC_FAIL"
        else:
            stage = "STRONG_METRIC_PASS" if strong_metric_gate else "STRONG_METRIC_FAIL"

        next_milestone = None
        for milestone in (
            PAPER_ENGINEERING_SESSIONS,
            PAPER_EARLY_EVIDENCE_SESSIONS,
            PAPER_STRONG_EVIDENCE_SESSIONS,
        ):
            if sessions < milestone:
                next_milestone = milestone
                break

        return {
            "benchmarkSymbol": PAPER_BENCHMARK_SYMBOL,
            "stage": stage,
            "observationSessions": sessions,
            "startDate": str(nav[0]["trade_date"]) if nav else None,
            "endDate": str(nav[-1]["trade_date"]) if nav else None,
            "strategySnapshotId": next(iter(snapshot_refs)) if len(snapshot_refs) == 1 else None,
            "modelSessions": len(model_rows),
            "manualSessions": manual_sessions,
            "lineageValid": lineage_valid,
            "calendarComplete": calendar_complete,
            "missingMarketSessions": len(missing_market_sessions),
            "missingBenchmarkSessions": missing_benchmark_sessions,
            "dataComplete": data_complete,
            "strategyReturn": strategy_return,
            "benchmarkReturn": benchmark_return,
            "afterCostExcessReturn": excess_return,
            "maxDrawdown": max_drawdown,
            "drawdownWarningTriggered": warning_triggered,
            "drawdownStopTriggered": stop_triggered,
            "engineeringGateReached": engineering_gate,
            "earlyMetricGatePassed": early_metric_gate,
            "strongMetricGatePassed": strong_metric_gate,
            "nextMilestone": next_milestone,
            "validationReasons": reasons,
            "automaticPromotionAllowed": False,
        }

    def get_state(
        self,
        account_id: str = "default",
        market_calendar: Iterable[str] | None = None,
    ) -> dict:
        with self._connect() as connection:
            account = self._require_account(connection, account_id)
            positions = connection.execute(
                "SELECT * FROM positions WHERE account_id = ? ORDER BY symbol", (account_id,)
            ).fetchall()
            pending = connection.execute(
                """
                SELECT * FROM signal_batches WHERE account_id = ? AND status = 'pending'
                ORDER BY signal_date, created_at LIMIT 1
                """,
                (account_id,),
            ).fetchone()
            latest_signal = connection.execute(
                """
                SELECT * FROM signal_batches WHERE account_id = ?
                ORDER BY signal_date DESC, created_at DESC, rowid DESC LIMIT 1
                """,
                (account_id,),
            ).fetchone()
            pending_targets = []
            if pending:
                pending_targets = connection.execute(
                    "SELECT symbol, target_weight FROM signals WHERE batch_id = ? ORDER BY symbol",
                    (pending["id"],),
                ).fetchall()
            fills = connection.execute(
                "SELECT * FROM fills WHERE account_id = ? ORDER BY created_at DESC, rowid DESC LIMIT 50", (account_id,)
            ).fetchall()
            orders = connection.execute(
                "SELECT * FROM orders WHERE account_id = ? ORDER BY created_at DESC, rowid DESC LIMIT 50", (account_id,)
            ).fetchall()
            nav = connection.execute(
                """
                SELECT nav_history.*,
                       signal_batches.source_type AS signal_source_type,
                       signal_batches.source_ref AS signal_source_ref
                FROM nav_history
                LEFT JOIN runs
                  ON runs.account_id = nav_history.account_id
                 AND runs.trade_date = nav_history.trade_date
                LEFT JOIN signal_batches
                  ON signal_batches.id = runs.signal_batch_id
                WHERE nav_history.account_id = ?
                ORDER BY nav_history.trade_date
                """,
                (account_id,),
            ).fetchall()

        market_value = sum(row["quantity"] * row["last_price"] for row in positions)
        total_value = float(account["cash"]) + market_value
        return {
            "account": {
                "id": account["id"],
                "name": account["name"],
                "initialCash": account["initial_cash"],
                "cash": account["cash"],
                "marketValue": market_value,
                "totalValue": total_value,
                "totalReturn": total_value / account["initial_cash"] - 1.0,
                "lastTradeDate": account["last_trade_date"],
            },
            "positions": [self._position_state(row) for row in positions],
            "pendingSignal": None if pending is None else {
                "signalId": pending["id"],
                "signalDate": pending["signal_date"],
                "targets": [
                    {"symbol": row["symbol"], "targetWeight": row["target_weight"]}
                    for row in pending_targets
                ],
                "source": self._signal_source(pending),
            },
            "latestSignal": None if latest_signal is None else {
                "signalId": latest_signal["id"],
                "signalDate": latest_signal["signal_date"],
                "status": latest_signal["status"],
                "source": self._signal_source(latest_signal),
            },
            "orders": [self._order_state(row) for row in orders],
            "fills": [self._fill_state(row) for row in fills],
            "paperValidation": self._paper_validation_summary(
                account, list(nav), market_calendar
            ),
            "equityCurve": [
                {
                    "date": row["trade_date"],
                    "value": row["total_value"],
                    "dailyReturn": row["daily_return"],
                    "benchmarkValue": row["benchmark_value"],
                    "benchmarkDailyReturn": row["benchmark_daily_return"],
                    "excessDailyReturn": self._relative_return(
                        row["daily_return"], row["benchmark_daily_return"]
                    ),
                }
                for row in nav
            ],
        }
