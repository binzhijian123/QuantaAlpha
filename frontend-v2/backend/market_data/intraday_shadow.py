"""Causal, paired intraday paper ledger for future-only A-share evidence.

The strategy consumes immutable 14:50 snapshots, prices simulated fills from the
immutable 14:55 snapshot, and settles only after the official daily Provider has
published that session's factor.  It never mutates the raw capture tree.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
import shutil
import sqlite3
import tempfile
from contextlib import contextmanager
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Iterable, Mapping
from zoneinfo import ZoneInfo

from quantaalpha.trading.market_data import QlibBinaryDailySource, discover_latest_provider
from quantaalpha.utils.provider_lineage import (
    verify_append_only_provider,
    write_lineage_proof,
)
from quantaalpha.utils.qlib_provider import provider_contract_fingerprint

from .intraday_capture import (
    BENCHMARK_SYMBOL,
    list_verified_slot_records,
    read_verified_snapshot,
)


PROJECT_ROOT = Path(__file__).resolve().parents[3]
SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
DEFAULT_DECLARATION = (
    PROJECT_ROOT / "configs" / "intraday_shadow_20260814_t1_reversal_v1.json"
)
ARMS = ("C1", "B0")
SCHEMA_VERSION = 1
STATUS_VERSION = 1
EVIDENCE_VERSION = 1
EVIDENCE_KIND = "intraday_shadow_evidence_checkpoint"
EVIDENCE_DIRECTORY = "evidence_checkpoints"
DRAW_DOWN_WARNING = -0.10
DRAW_DOWN_STOP = -0.15


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _resolve(value: str | Path) -> Path:
    path = Path(value).expanduser()
    return path.resolve() if path.is_absolute() else (PROJECT_ROOT / path).resolve()


def _atomic_json_write(path: Path, payload: Mapping[str, Any], mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = (json.dumps(dict(payload), ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(
        "utf-8"
    )
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def _read_hash_sidecar(path: Path) -> str:
    sidecar = path.with_suffix(path.suffix + ".sha256")
    if not sidecar.is_file():
        raise ValueError(f"hash sidecar is missing: {sidecar}")
    recorded = sidecar.read_text(encoding="ascii").split()[0]
    if recorded != _sha256_file(path):
        raise ValueError(f"file hash mismatch: {path}")
    return recorded


def _json_value(value: Any) -> Any:
    return {"bytesHex": value.hex()} if isinstance(value, bytes) else value


def _rows_contract(columns: list[str], rows: Iterable[sqlite3.Row]) -> dict[str, Any]:
    serialized = sorted(
        _canonical_json({column: _json_value(row[column]) for column in columns})
        for row in rows
    )
    digest = hashlib.sha256()
    for row in serialized:
        encoded = row.encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    return {"rows": len(serialized), "sha256": digest.hexdigest()}


def _logical_database_contract(connection: sqlite3.Connection) -> dict[str, Any]:
    schema_rows = connection.execute(
        "SELECT type, name, tbl_name, sql FROM sqlite_master "
        "WHERE sql IS NOT NULL ORDER BY type, name"
    ).fetchall()
    schema = _rows_contract(["type", "name", "tbl_name", "sql"], schema_rows)
    tables: dict[str, dict[str, Any]] = {}
    for row in connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
    ):
        name = str(row[0])
        quoted = '"' + name.replace('"', '""') + '"'
        cursor = connection.execute(f"SELECT * FROM {quoted}")
        columns = [str(item[0]) for item in cursor.description or ()]
        tables[name] = _rows_contract(columns, cursor.fetchall())
    logical_sha = _sha256_bytes(
        _canonical_json({"schema": schema, "tables": tables}).encode("utf-8")
    )
    return {"schema": schema, "tables": tables, "logicalSha256": logical_sha}


def _parse_clock(value: str, field: str) -> time:
    try:
        return time.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid {field}: {value}") from exc


def load_intraday_shadow_declaration(
    path: Path | str = DEFAULT_DECLARATION,
) -> tuple[dict[str, Any], str]:
    declaration_path = _resolve(path)
    declaration_sha = _read_hash_sidecar(declaration_path)
    payload = json.loads(declaration_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("schemaVersion") != SCHEMA_VERSION:
        raise ValueError("intraday-shadow declaration schema is unsupported")
    if payload.get("usageRealm") != "INTRADAY_SHADOW":
        raise ValueError("intraday-shadow declaration has the wrong usage realm")
    for field in ("formalPaperEligible", "automaticPromotionAllowed"):
        if payload.get(field) is not False:
            raise ValueError(f"intraday-shadow declaration requires {field}=false")
    if payload.get("historicalReturnValuesUsedForSelection") is not False:
        raise ValueError("intraday-shadow declaration permits historical return selection")

    paper_reference = payload.get("paperReference") or {}
    if (
        paper_reference.get("doi") != "10.2139/ssrn.6736959"
        or paper_reference.get("fullMethodVerified") is not False
        or paper_reference.get("implementationKind") != "MECHANISM_PROXY"
    ):
        raise ValueError("intraday-shadow paper-reference boundary drifted")

    strategy = payload.get("strategy") or {}
    if strategy.get("candidateFormula") != "max(-rawReturn, 0) * turnoverPercent":
        raise ValueError("intraday-shadow candidate formula drifted")
    if strategy.get("baselineFormula") != "max(-rawReturn, 0)":
        raise ValueError("intraday-shadow baseline formula drifted")
    if strategy.get("eligibleReturnDirection") != "NEGATIVE_ONLY":
        raise ValueError("intraday-shadow eligibility direction drifted")
    if strategy.get("mechanismProxy") is not True or strategy.get("paperReplication") is not False:
        raise ValueError("intraday-shadow strategy scope drifted")
    top_k = int(strategy.get("topK") or 0)
    if not 1 <= top_k <= 300:
        raise ValueError("intraday-shadow topK must be between 1 and 300")

    timing = payload.get("timing") or {}
    clocks = {
        key: _parse_clock(str(timing.get(key) or ""), key)
        for key in ("decisionSlot", "executionSlot", "freezeAfter", "freezeDeadline")
    }
    if not (
        clocks["decisionSlot"]
        < clocks["executionSlot"]
        < clocks["freezeAfter"]
        < clocks["freezeDeadline"]
    ):
        raise ValueError("intraday-shadow timing is not strictly causal")

    execution = payload.get("execution") or {}
    required_execution = {
        "initialCash",
        "riskDegree",
        "lotSize",
        "buyCostRate",
        "sellCostRate",
        "minimumCost",
        "slippageBps",
        "limitThreshold",
        "growthLimitThreshold",
        "starMinimumOrderSize",
        "starOrderIncrement",
    }
    if required_execution - set(execution):
        raise ValueError("intraday-shadow execution contract is incomplete")
    if execution.get("settlement") != "T+1" or execution.get("priceField") != "price":
        raise ValueError("intraday-shadow execution contract drifted")
    try:
        numeric_values = [float(execution[field]) for field in required_execution]
    except (TypeError, ValueError) as exc:
        raise ValueError("intraday-shadow execution values must be numeric") from exc
    if not all(math.isfinite(value) and value >= 0 for value in numeric_values):
        raise ValueError("intraday-shadow execution values must be finite and non-negative")
    if float(execution["initialCash"]) <= 0:
        raise ValueError("intraday-shadow initial cash must be positive")
    if not 0 < float(execution["riskDegree"]) <= 1:
        raise ValueError("intraday-shadow risk degree must be in (0, 1]")
    for field in ("lotSize", "starMinimumOrderSize", "starOrderIncrement"):
        value = float(execution[field])
        if value < 1 or not value.is_integer():
            raise ValueError(f"intraday-shadow {field} must be a positive integer")
    for field in ("buyCostRate", "sellCostRate"):
        if float(execution[field]) > 0.1:
            raise ValueError(f"intraday-shadow {field} is implausibly large")
    for field in ("limitThreshold", "growthLimitThreshold"):
        if not 0 < float(execution[field]) < 1:
            raise ValueError(f"intraday-shadow {field} must be in (0, 1)")
    if float(execution["slippageBps"]) > 1000:
        raise ValueError("intraday-shadow slippage exceeds the frozen safety bound")

    calibration = payload.get("calibration") or {}
    report_path = _resolve(str(calibration.get("reportPath") or ""))
    report_file_sha = _read_hash_sidecar(report_path)
    if report_file_sha != calibration.get("fileSha256"):
        raise ValueError("intraday-shadow calibration file hash drifted")
    report = json.loads(report_path.read_text(encoding="utf-8"))
    if (
        report.get("status") != "PASS"
        or report.get("finalized") is not True
        or report.get("reportSha256") != calibration.get("reportSha256")
        or report.get("profitEvidenceEligible") is not False
        or report.get("featureValuesIncluded") is not False
        or report.get("returnValuesIncluded") is not False
    ):
        raise ValueError("intraday-shadow calibration contract is invalid")

    implementation = payload.get("implementation") or {}
    files = implementation.get("files") or []
    if not files:
        raise ValueError("intraday-shadow implementation contract is empty")
    for item in files:
        implementation_path = _resolve(str(item.get("path") or ""))
        if _sha256_file(implementation_path) != item.get("sha256"):
            raise ValueError(f"intraday-shadow implementation drifted: {implementation_path}")

    root_provider = _resolve(str((payload.get("providerRoot") or {}).get("path") or ""))
    root_fingerprint = provider_contract_fingerprint(root_provider, "csi300")["fingerprint"]
    if root_fingerprint != (payload.get("providerRoot") or {}).get("fingerprint"):
        raise ValueError("intraday-shadow root Provider drifted")
    first_eligible = date.fromisoformat(str(payload["firstEligibleSignalDate"]))
    declared_at = datetime.fromisoformat(str(payload["declaredAt"]))
    if declared_at.tzinfo is None:
        raise ValueError("intraday-shadow declaredAt must include a timezone")
    if first_eligible <= declared_at.astimezone(SHANGHAI_TZ).date():
        raise ValueError("intraday-shadow first signal date must be future-only")
    future_calendar = root_provider / "calendars" / "day_future.txt"
    future_dates = {
        line.strip()
        for line in future_calendar.read_text(encoding="utf-8").splitlines()
        if line.strip()
    }
    if first_eligible.isoformat() not in future_dates:
        raise ValueError("intraday-shadow first signal date is not in the frozen calendar")
    calibration_date = report.get("tradeDate")
    if calibration_date and date.fromisoformat(str(calibration_date)) >= first_eligible:
        raise ValueError("intraday-shadow calibration must precede the first signal date")
    return payload, declaration_sha


def _rank_signals(
    snapshot: Mapping[str, Any],
    *,
    top_k: int,
) -> dict[str, list[dict[str, Any]]]:
    rows: list[dict[str, Any]] = []
    trade_date = str(snapshot.get("quoteTradeDate") or "")
    for raw in snapshot.get("quotes") or []:
        if not isinstance(raw, Mapping) or raw.get("symbol") == BENCHMARK_SYMBOL:
            continue
        try:
            price = float(raw["price"])
            previous = float(raw["previousClose"])
            turnover = float(raw["turnoverPercent"])
        except (KeyError, TypeError, ValueError):
            continue
        if (
            raw.get("tradeDate") != trade_date
            or raw.get("isStale")
            or not all(math.isfinite(value) for value in (price, previous, turnover))
            or min(price, previous, turnover) <= 0
        ):
            continue
        raw_return = price / previous - 1.0
        if not math.isfinite(raw_return) or raw_return >= 0:
            continue
        reversal = -raw_return
        rows.append(
            {
                "symbol": str(raw["symbol"]),
                "rawReturn": raw_return,
                "turnoverPercent": turnover,
                "candidateScore": reversal * turnover,
                "baselineScore": reversal,
            }
        )
    if len(rows) < top_k:
        raise ValueError(
            f"intraday-shadow has only {len(rows)} eligible negative-return stocks; "
            f"topK={top_k}"
        )

    result: dict[str, list[dict[str, Any]]] = {}
    for arm, field in (("C1", "candidateScore"), ("B0", "baselineScore")):
        ranked = sorted(rows, key=lambda row: (-float(row[field]), str(row["symbol"])))[:top_k]
        result[arm] = [
            {
                **row,
                "score": float(row[field]),
                "rank": rank,
            }
            for rank, row in enumerate(ranked, 1)
        ]
    return result


class IntradayShadowCoordinator:
    def __init__(self, declaration_path: Path | str = DEFAULT_DECLARATION):
        self.declaration_path = _resolve(declaration_path)
        self.declaration, self.declaration_sha256 = load_intraday_shadow_declaration(
            self.declaration_path
        )
        self.run_id = str(self.declaration["runId"])
        self.capture_root = _resolve(self.declaration["captureRoot"])
        self.db_path = _resolve(self.declaration["databasePath"])
        self.run_dir = self.db_path.parent
        self.status_path = self.run_dir / "automation_status.json"
        self.lineage_dir = self.run_dir / "provider_lineage"
        self.evidence_dir = self.run_dir / EVIDENCE_DIRECTORY
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize_database()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        return connection

    @contextmanager
    def _operation_lock(self):
        lock_path = self.run_dir / ".operation.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a+", encoding="ascii") as stream:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)

    def _initialize_database(self) -> None:
        initial_cash = float(self.declaration["execution"]["initialCash"])
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS shadow_metadata(
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS accounts(
                    arm TEXT PRIMARY KEY,
                    initial_cash REAL NOT NULL,
                    cash REAL NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS signal_sessions(
                    trade_date TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    decision_record_sha256 TEXT NOT NULL,
                    decision_snapshot_sha256 TEXT NOT NULL,
                    decision_snapshot_path TEXT NOT NULL,
                    execution_record_sha256 TEXT NOT NULL,
                    execution_snapshot_sha256 TEXT NOT NULL,
                    execution_snapshot_path TEXT NOT NULL,
                    provider_path TEXT,
                    provider_fingerprint TEXT,
                    created_at TEXT NOT NULL,
                    finalized_at TEXT
                );
                CREATE TABLE IF NOT EXISTS signals(
                    trade_date TEXT NOT NULL REFERENCES signal_sessions(trade_date),
                    arm TEXT NOT NULL,
                    rank INTEGER NOT NULL,
                    symbol TEXT NOT NULL,
                    score REAL NOT NULL,
                    raw_return REAL NOT NULL,
                    turnover_percent REAL NOT NULL,
                    PRIMARY KEY(trade_date, arm, symbol),
                    UNIQUE(trade_date, arm, rank)
                );
                CREATE TABLE IF NOT EXISTS positions(
                    arm TEXT NOT NULL REFERENCES accounts(arm),
                    symbol TEXT NOT NULL,
                    adjusted_quantity REAL NOT NULL,
                    factor REAL NOT NULL,
                    acquired_date TEXT NOT NULL,
                    last_raw_price REAL NOT NULL,
                    PRIMARY KEY(arm, symbol)
                );
                CREATE TABLE IF NOT EXISTS orders(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    trade_date TEXT NOT NULL,
                    arm TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL,
                    actual_quantity REAL NOT NULL,
                    status TEXT NOT NULL,
                    reason TEXT,
                    raw_quote_price REAL,
                    raw_fill_price REAL,
                    gross_amount REAL,
                    transaction_cost REAL,
                    slippage_cost REAL
                );
                CREATE TABLE IF NOT EXISTS nav_history(
                    trade_date TEXT NOT NULL,
                    arm TEXT NOT NULL,
                    cash REAL NOT NULL,
                    market_value REAL NOT NULL,
                    total_value REAL NOT NULL,
                    daily_return REAL,
                    benchmark_price REAL NOT NULL,
                    benchmark_value REAL NOT NULL,
                    benchmark_daily_return REAL,
                    PRIMARY KEY(trade_date, arm)
                );
                CREATE TABLE IF NOT EXISTS missed_sessions(
                    trade_date TEXT PRIMARY KEY,
                    reason TEXT NOT NULL,
                    recorded_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS shadow_audit(
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
            expected = {
                "runId": self.run_id,
                "usageRealm": "INTRADAY_SHADOW",
                "declarationSha256": self.declaration_sha256,
                "formalPaperEligible": "false",
            }
            for key, value in expected.items():
                row = connection.execute(
                    "SELECT value FROM shadow_metadata WHERE key = ?", (key,)
                ).fetchone()
                if row is not None and row["value"] != value:
                    raise ValueError(f"intraday-shadow metadata mismatch: {key}")
                connection.execute(
                    "INSERT OR IGNORE INTO shadow_metadata(key, value) VALUES (?, ?)",
                    (key, value),
                )
            now = datetime.now(SHANGHAI_TZ).isoformat()
            for arm in ARMS:
                connection.execute(
                    "INSERT OR IGNORE INTO accounts(arm, initial_cash, cash, updated_at) "
                    "VALUES (?, ?, ?, ?)",
                    (arm, initial_cash, initial_cash, now),
                )
            if connection.execute("SELECT COUNT(*) FROM shadow_audit").fetchone()[0] == 0:
                self._append_audit_row(
                    connection,
                    "DECLARATION_FROZEN",
                    {
                        "runId": self.run_id,
                        "declarationSha256": self.declaration_sha256,
                        "firstEligibleSignalDate": self.declaration[
                            "firstEligibleSignalDate"
                        ],
                        "historicalReturnValuesUsedForSelection": False,
                    },
                    datetime.now(SHANGHAI_TZ),
                )

    @staticmethod
    def _append_audit_row(
        connection: sqlite3.Connection,
        event_type: str,
        payload: Mapping[str, Any],
        created_at: datetime,
    ) -> None:
        row = connection.execute(
            "SELECT event_hash FROM shadow_audit ORDER BY sequence DESC LIMIT 1"
        ).fetchone()
        previous_hash = str(row["event_hash"]) if row else "0" * 64
        payload_json = _canonical_json(payload)
        timestamp = created_at.astimezone(SHANGHAI_TZ).isoformat()
        event_hash = _sha256_bytes(
            (previous_hash + "\0" + event_type + "\0" + payload_json + "\0" + timestamp).encode(
                "utf-8"
            )
        )
        connection.execute(
            "INSERT INTO shadow_audit(event_type, payload_json, previous_hash, "
            "event_hash, created_at) VALUES (?, ?, ?, ?, ?)",
            (event_type, payload_json, previous_hash, event_hash, timestamp),
        )

    @staticmethod
    def _verify_audit(connection: sqlite3.Connection) -> dict[str, Any]:
        rows = connection.execute(
            "SELECT * FROM shadow_audit ORDER BY sequence"
        ).fetchall()
        previous_hash = "0" * 64
        for sequence, row in enumerate(rows, 1):
            if int(row["sequence"]) != sequence or row["previous_hash"] != previous_hash:
                raise ValueError("intraday-shadow audit chain is discontinuous")
            payload = json.loads(row["payload_json"])
            if _canonical_json(payload) != row["payload_json"]:
                raise ValueError("intraday-shadow audit payload is not canonical")
            expected = _sha256_bytes(
                (
                    previous_hash
                    + "\0"
                    + row["event_type"]
                    + "\0"
                    + row["payload_json"]
                    + "\0"
                    + row["created_at"]
                ).encode("utf-8")
            )
            if row["event_hash"] != expected:
                raise ValueError("intraday-shadow audit hash mismatch")
            previous_hash = expected
        latest = rows[-1] if rows else None
        return {
            "valid": True,
            "events": len(rows),
            "head": latest["event_hash"] if latest else None,
            "lastEvent": latest["event_type"] if latest else None,
            "lastEventAt": latest["created_at"] if latest else None,
        }

    @staticmethod
    def _open_immutable_database(path: Path) -> sqlite3.Connection:
        uri = path.expanduser().resolve().as_uri() + "?mode=ro&immutable=1"
        connection = sqlite3.connect(uri, uri=True, timeout=30)
        connection.row_factory = sqlite3.Row
        return connection

    def _validate_evidence_checkpoint_chain(
        self,
        *,
        verify_all_snapshots: bool = False,
        live_audit: Mapping[str, Any] | None = None,
        live_logical_sha256: str | None = None,
    ) -> dict[str, Any]:
        if not self.evidence_dir.exists():
            return {
                "valid": True,
                "status": "EMPTY",
                "directory": str(self.evidence_dir),
                "count": 0,
                "latest": None,
                "upToDate": False,
            }
        checkpoint_dirs = sorted(
            path
            for path in self.evidence_dir.iterdir()
            if path.name.startswith("checkpoint_")
        )
        if any(not path.is_dir() for path in checkpoint_dirs):
            raise ValueError("intraday-shadow evidence contains a non-directory checkpoint")

        previous: dict[str, str] | None = None
        latest: dict[str, Any] | None = None
        for sequence, directory in enumerate(checkpoint_dirs, 1):
            expected_files = {
                "ledger.sqlite3",
                "ledger.sqlite3.sha256",
                "manifest.json",
                "manifest.json.sha256",
            }
            actual_files = {path.name for path in directory.iterdir() if path.is_file()}
            if actual_files != expected_files:
                raise ValueError("intraday-shadow evidence checkpoint artifacts drifted")
            manifest_path = directory / "manifest.json"
            manifest_sha = _read_hash_sidecar(manifest_path)
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            audit = manifest.get("audit") or {}
            expected_name = f"checkpoint_{sequence:06d}_{str(audit.get('head') or '')[:12]}"
            if (
                manifest.get("version") != EVIDENCE_VERSION
                or manifest.get("kind") != EVIDENCE_KIND
                or manifest.get("sequence") != sequence
                or manifest.get("runId") != self.run_id
                or manifest.get("declarationSha256") != self.declaration_sha256
                or manifest.get("previousCheckpoint") != previous
                or directory.name != expected_name
            ):
                raise ValueError("intraday-shadow evidence checkpoint header is invalid")

            ledger_path = directory / "ledger.sqlite3"
            ledger_sidecar = ledger_path.with_suffix(ledger_path.suffix + ".sha256")
            if not ledger_sidecar.is_file():
                raise ValueError("intraday-shadow evidence SQLite sidecar is missing")
            recorded_ledger_sha = ledger_sidecar.read_text(encoding="ascii").split()[0]
            ledger = manifest.get("ledger") or {}
            if (
                recorded_ledger_sha != ledger.get("sha256")
                or int(ledger.get("bytes") or -1) != ledger_path.stat().st_size
            ):
                raise ValueError("intraday-shadow evidence SQLite binding is invalid")

            if verify_all_snapshots or sequence == len(checkpoint_dirs):
                if _sha256_file(ledger_path) != recorded_ledger_sha:
                    raise ValueError("intraday-shadow evidence SQLite hash mismatch")
                with self._open_immutable_database(ledger_path) as connection:
                    integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
                    snapshot_audit = self._verify_audit(connection)
                    logical = _logical_database_contract(connection)
                if integrity != "ok":
                    raise ValueError(
                        f"intraday-shadow evidence SQLite integrity failed: {integrity}"
                    )
                if snapshot_audit != audit:
                    raise ValueError("intraday-shadow evidence audit summary drifted")
                if logical["logicalSha256"] != ledger.get("logicalSha256"):
                    raise ValueError("intraday-shadow evidence logical ledger drifted")

            latest = {
                "sequence": sequence,
                "directory": directory.name,
                "createdAt": manifest.get("createdAt"),
                "manifestSha256": manifest_sha,
                "ledgerSha256": recorded_ledger_sha,
                "logicalSha256": ledger.get("logicalSha256"),
                "audit": dict(audit),
                "latestSession": manifest.get("latestSession"),
            }
            previous = {"directory": directory.name, "manifestSha256": manifest_sha}

        up_to_date = False
        if latest is not None and live_audit is not None:
            checkpoint_events = int(latest["audit"].get("events") or 0)
            live_events = int(live_audit.get("events") or 0)
            if live_events < checkpoint_events:
                raise ValueError("intraday-shadow live audit moved behind its evidence anchor")
            if checkpoint_events:
                with self._connect() as connection:
                    anchor = connection.execute(
                        "SELECT event_hash FROM shadow_audit WHERE sequence = ?",
                        (checkpoint_events,),
                    ).fetchone()
                if anchor is None or anchor["event_hash"] != latest["audit"].get("head"):
                    raise ValueError("intraday-shadow live audit is not append-only")
            if (
                live_events == checkpoint_events
                and live_logical_sha256 != latest["logicalSha256"]
            ):
                raise ValueError("intraday-shadow ledger changed without an audit event")
            up_to_date = (
                live_events == checkpoint_events
                and live_logical_sha256 == latest["logicalSha256"]
            )
        return {
            "valid": True,
            "status": "VALID" if latest else "EMPTY",
            "directory": str(self.evidence_dir),
            "count": len(checkpoint_dirs),
            "latest": latest,
            "upToDate": up_to_date,
        }

    def ensure_evidence_checkpoint(
        self, *, now: datetime | None = None
    ) -> dict[str, Any]:
        created_at = (now or datetime.now(SHANGHAI_TZ)).astimezone(SHANGHAI_TZ)
        pending: Path | None = None
        with self._operation_lock():
            existing = self._validate_evidence_checkpoint_chain()
            with self._connect() as connection:
                audit = self._verify_audit(connection)
                logical = _logical_database_contract(connection)
                latest_session_row = connection.execute(
                    "SELECT * FROM signal_sessions ORDER BY trade_date DESC LIMIT 1"
                ).fetchone()
                latest_session = dict(latest_session_row) if latest_session_row else None
            latest = existing.get("latest")
            if latest and (
                latest["audit"].get("head") == audit.get("head")
                and latest["logicalSha256"] == logical["logicalSha256"]
            ):
                return {**existing, "status": "REUSED", "upToDate": True}

            self.evidence_dir.mkdir(parents=True, exist_ok=True)
            pending = Path(
                tempfile.mkdtemp(prefix=".pending_", dir=self.evidence_dir)
            )
            try:
                snapshot_path = pending / "ledger.sqlite3"
                source_uri = self.db_path.expanduser().resolve().as_uri() + "?mode=ro"
                with sqlite3.connect(source_uri, uri=True, timeout=30) as source:
                    source.execute("PRAGMA query_only = ON")
                    with sqlite3.connect(snapshot_path, timeout=30) as target:
                        source.backup(target)
                with self._open_immutable_database(snapshot_path) as snapshot:
                    integrity = str(snapshot.execute("PRAGMA integrity_check").fetchone()[0])
                    snapshot_audit = self._verify_audit(snapshot)
                    snapshot_logical = _logical_database_contract(snapshot)
                if integrity != "ok":
                    raise ValueError(
                        f"intraday-shadow evidence backup integrity failed: {integrity}"
                    )
                if snapshot_audit != audit or snapshot_logical != logical:
                    raise ValueError("intraday-shadow evidence backup is not transactionally stable")

                snapshot_sha = _sha256_file(snapshot_path)
                snapshot_sidecar = snapshot_path.with_suffix(
                    snapshot_path.suffix + ".sha256"
                )
                snapshot_sidecar.write_text(snapshot_sha + "\n", encoding="ascii")
                sequence = int(existing.get("count") or 0) + 1
                final_name = f"checkpoint_{sequence:06d}_{str(audit.get('head') or '')[:12]}"
                manifest = {
                    "version": EVIDENCE_VERSION,
                    "kind": EVIDENCE_KIND,
                    "sequence": sequence,
                    "createdAt": created_at.isoformat(),
                    "runId": self.run_id,
                    "declarationSha256": self.declaration_sha256,
                    "previousCheckpoint": (
                        {
                            "directory": latest["directory"],
                            "manifestSha256": latest["manifestSha256"],
                        }
                        if latest
                        else None
                    ),
                    "audit": audit,
                    "ledger": {
                        "file": snapshot_path.name,
                        "sha256": snapshot_sha,
                        "bytes": snapshot_path.stat().st_size,
                        "logicalSha256": logical["logicalSha256"],
                    },
                    "latestSession": latest_session,
                }
                manifest_path = pending / "manifest.json"
                _atomic_json_write(manifest_path, manifest, mode=0o444)
                manifest_sha = _sha256_file(manifest_path)
                manifest_sidecar = manifest_path.with_suffix(
                    manifest_path.suffix + ".sha256"
                )
                manifest_sidecar.write_text(manifest_sha + "\n", encoding="ascii")
                for artifact in (snapshot_path, snapshot_sidecar, manifest_sidecar):
                    os.chmod(artifact, 0o444)
                destination = self.evidence_dir / final_name
                if destination.exists():
                    raise FileExistsError(
                        f"refusing to overwrite intraday-shadow evidence: {destination}"
                    )
                os.replace(pending, destination)
                pending = None
                result = self._validate_evidence_checkpoint_chain(
                    live_audit=audit,
                    live_logical_sha256=logical["logicalSha256"],
                )
                return {**result, "status": "CREATED"}
            finally:
                if pending is not None and pending.exists():
                    shutil.rmtree(pending)

    def _snapshot_at(
        self,
        trade_date: str,
        slot_value: str,
        *,
        available_by: datetime,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        wanted = datetime.combine(
            date.fromisoformat(trade_date),
            _parse_clock(slot_value, "slot"),
            tzinfo=SHANGHAI_TZ,
        ).isoformat()
        matches = [
            record
            for _, record in list_verified_slot_records(self.capture_root, trade_date)
            if record.get("captureSlot") == wanted
        ]
        if len(matches) != 1:
            raise ValueError(f"intraday-shadow requires exactly one {wanted} slot record")
        record = matches[0]
        if record.get("status") != "CAPTURED" or record.get("forcedCapture") is not False:
            raise ValueError(f"intraday-shadow slot is not an eligible automatic capture: {wanted}")
        snapshot_meta = record.get("snapshot") or {}
        snapshot_path = _resolve(str(snapshot_meta.get("file") or ""))
        snapshot = read_verified_snapshot(snapshot_path)
        if (
            snapshot.get("captureSlot") != wanted
            or snapshot.get("quoteTradeDate") != trade_date
            or snapshot.get("forcedCapture") is not False
            or snapshot.get("usageRealm") != "INTRADAY_RESEARCH_RAW"
        ):
            raise ValueError(f"intraday-shadow snapshot contract mismatch: {snapshot_path}")
        for source, fields in (
            (record, ("availableAt", "committedAt")),
            (snapshot, ("availableAt", "capturedAt")),
        ):
            for field in fields:
                try:
                    timestamp = datetime.fromisoformat(str(source[field]))
                except (KeyError, TypeError, ValueError) as exc:
                    raise ValueError(
                        f"intraday-shadow snapshot lacks a valid {field}: {snapshot_path}"
                    ) from exc
                if timestamp.tzinfo is None:
                    raise ValueError(
                        f"intraday-shadow snapshot {field} lacks a timezone: {snapshot_path}"
                    )
                timestamp = timestamp.astimezone(SHANGHAI_TZ)
                if timestamp > available_by:
                    raise ValueError(
                        f"intraday-shadow snapshot was not available by freeze time: {field}"
                    )
        return record, snapshot

    def freeze_session(self, trade_date: str, *, now: datetime | None = None) -> dict[str, Any]:
        parsed = date.fromisoformat(trade_date).isoformat()
        if parsed < str(self.declaration["firstEligibleSignalDate"]):
            raise ValueError("intraday-shadow forbids pre-declaration or calibration-day signals")
        local_now = (now or datetime.now(SHANGHAI_TZ)).astimezone(SHANGHAI_TZ)
        timing = self.declaration["timing"]
        freeze_after = datetime.combine(
            date.fromisoformat(parsed), _parse_clock(timing["freezeAfter"], "freezeAfter"), SHANGHAI_TZ
        )
        freeze_deadline = datetime.combine(
            date.fromisoformat(parsed),
            _parse_clock(timing["freezeDeadline"], "freezeDeadline"),
            SHANGHAI_TZ,
        )
        if local_now < freeze_after:
            raise ValueError("intraday-shadow signal cannot freeze before freezeAfter")
        if local_now > freeze_deadline or local_now.date().isoformat() != parsed:
            raise ValueError("intraday-shadow signal freeze deadline passed; backfill is forbidden")
        current_state = self.get_state()
        if current_state["status"] in {"RISK_STOPPED", "MISSED_SESSION"}:
            raise ValueError(
                f"intraday-shadow run is {current_state['status']}; new signals are forbidden"
            )

        with self._operation_lock(), self._connect() as connection:
            existing = connection.execute(
                "SELECT * FROM signal_sessions WHERE trade_date = ?", (parsed,)
            ).fetchone()
            if existing is not None:
                return self.get_state()
            decision_record, decision = self._snapshot_at(
                parsed, timing["decisionSlot"], available_by=local_now
            )
            execution_record, execution = self._snapshot_at(
                parsed, timing["executionSlot"], available_by=local_now
            )
            if decision.get("universe") != execution.get("universe"):
                raise ValueError("intraday-shadow decision and execution universes differ")
            if decision.get("implementation") != execution.get("implementation"):
                raise ValueError("intraday-shadow capture implementation drifted within session")
            ranked = _rank_signals(
                decision,
                top_k=int(self.declaration["strategy"]["topK"]),
            )
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "INSERT INTO signal_sessions(trade_date, status, decision_record_sha256, "
                "decision_snapshot_sha256, decision_snapshot_path, execution_record_sha256, "
                "execution_snapshot_sha256, execution_snapshot_path, created_at) "
                "VALUES (?, 'FROZEN_AWAITING_OFFICIAL', ?, ?, ?, ?, ?, ?, ?)",
                (
                    parsed,
                    decision_record["recordSha256"],
                    decision["contentSha256"],
                    str(_resolve(decision_record["snapshot"]["file"])),
                    execution_record["recordSha256"],
                    execution["contentSha256"],
                    str(_resolve(execution_record["snapshot"]["file"])),
                    local_now.isoformat(),
                ),
            )
            for arm in ARMS:
                connection.executemany(
                    "INSERT INTO signals(trade_date, arm, rank, symbol, score, raw_return, "
                    "turnover_percent) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    [
                        (
                            parsed,
                            arm,
                            row["rank"],
                            row["symbol"],
                            row["score"],
                            row["rawReturn"],
                            row["turnoverPercent"],
                        )
                        for row in ranked[arm]
                    ],
                )
            self._append_audit_row(
                connection,
                "PAIRED_SIGNAL_AND_EXECUTION_PRICE_FROZEN",
                {
                    "tradeDate": parsed,
                    "decisionRecordSha256": decision_record["recordSha256"],
                    "decisionSnapshotSha256": decision["contentSha256"],
                    "executionRecordSha256": execution_record["recordSha256"],
                    "executionSnapshotSha256": execution["contentSha256"],
                    "targetsSha256": {
                        arm: _sha256_bytes(
                            _canonical_json(
                                [row["symbol"] for row in ranked[arm]]
                            ).encode("utf-8")
                        )
                        for arm in ARMS
                    },
                },
                local_now,
            )
        return self.get_state()

    def _verify_provider(self, provider_path: Path) -> tuple[dict[str, Any], dict[str, Any] | None]:
        provider = provider_path.expanduser().resolve()
        contract = provider_contract_fingerprint(provider, "csi300")
        root = _resolve(self.declaration["providerRoot"]["path"])
        root_fingerprint = str(self.declaration["providerRoot"]["fingerprint"])
        if provider == root:
            if contract["fingerprint"] != root_fingerprint:
                raise ValueError("intraday-shadow root Provider fingerprint changed")
            return contract, None
        proof_path = self.lineage_dir / f"{contract['fingerprint']}.json"
        if proof_path.is_file():
            _read_hash_sidecar(proof_path)
            proof = json.loads(proof_path.read_text(encoding="utf-8"))
            if (
                proof.get("parentFingerprint") != root_fingerprint
                or proof.get("successorFingerprint") != contract["fingerprint"]
                or proof.get("fields") != ["factor"]
            ):
                raise ValueError("intraday-shadow Provider lineage proof mismatch")
            return contract, proof
        proof = verify_append_only_provider(
            root,
            provider,
            market="csi300",
            benchmark=BENCHMARK_SYMBOL,
            fields=("factor",),
        )
        if proof["parentFingerprint"] != root_fingerprint:
            raise ValueError("intraday-shadow Provider lineage has the wrong root")
        proof.update(
            {
                "runId": self.run_id,
                "declarationSha256": self.declaration_sha256,
            }
        )
        write_lineage_proof(proof_path, proof)
        os.chmod(proof_path, 0o444)
        os.chmod(proof_path.with_suffix(proof_path.suffix + ".sha256"), 0o444)
        return contract, proof

    @staticmethod
    def _quote_map(snapshot: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
        return {
            str(row["symbol"]): dict(row)
            for row in snapshot.get("quotes") or []
            if isinstance(row, Mapping) and row.get("symbol")
        }

    def _board_lot(self, symbol: str) -> tuple[int, int]:
        execution = self.declaration["execution"]
        if symbol.endswith(".SH") and symbol.startswith(("688", "689")):
            return int(execution["starMinimumOrderSize"]), int(
                execution["starOrderIncrement"]
            )
        lot = int(execution["lotSize"])
        return lot, lot

    def _limit_threshold(self, symbol: str) -> float:
        execution = self.declaration["execution"]
        if (symbol.endswith(".SZ") and symbol.startswith("30")) or (
            symbol.endswith(".SH") and symbol.startswith(("688", "689"))
        ):
            return float(execution["growthLimitThreshold"])
        return float(execution["limitThreshold"])

    def _rounded_actual_quantity(self, symbol: str, desired: float) -> float:
        minimum, increment = self._board_lot(symbol)
        if desired < minimum:
            return 0.0
        units = math.floor((desired - minimum) / increment + 1e-9)
        return float(minimum + units * increment)

    def _trade_reason(
        self,
        symbol: str,
        side: str,
        quote: Mapping[str, Any],
        listing_sessions: int | None,
    ) -> str | None:
        try:
            price = float(quote["price"])
            previous = float(quote["previousClose"])
        except (KeyError, TypeError, ValueError):
            return "执行报价缺少现价或昨收"
        if (
            quote.get("isStale")
            or not math.isfinite(price)
            or not math.isfinite(previous)
            or min(price, previous) <= 0
        ):
            return "执行报价不可用"
        if listing_sessions is None or listing_sessions <= 5:
            return "上市交易日龄不足或未知"
        change = price / previous - 1.0
        threshold = self._limit_threshold(symbol)
        if side == "buy" and change >= threshold:
            return "执行时达到板块涨停阈值"
        if side == "sell" and change <= -threshold:
            return "执行时达到板块跌停阈值"
        return None

    def _record_order(
        self,
        connection: sqlite3.Connection,
        *,
        trade_date: str,
        arm: str,
        symbol: str,
        side: str,
        actual_quantity: float,
        quote_price: float | None,
        fill_price: float | None,
        status: str,
        reason: str | None,
        gross: float = 0.0,
        cost: float = 0.0,
        slippage: float = 0.0,
    ) -> None:
        connection.execute(
            "INSERT INTO orders(trade_date, arm, symbol, side, actual_quantity, status, "
            "reason, raw_quote_price, raw_fill_price, gross_amount, transaction_cost, "
            "slippage_cost) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                trade_date,
                arm,
                symbol,
                side,
                actual_quantity,
                status,
                reason,
                quote_price,
                fill_price,
                gross,
                cost,
                slippage,
            ),
        )

    def _settle_arm(
        self,
        connection: sqlite3.Connection,
        *,
        arm: str,
        trade_date: str,
        quotes: Mapping[str, Mapping[str, Any]],
        factor_bars: Mapping[str, Any],
        desired_symbols: list[str],
        benchmark_price: float,
    ) -> None:
        execution = self.declaration["execution"]
        account = connection.execute(
            "SELECT * FROM accounts WHERE arm = ?", (arm,)
        ).fetchone()
        positions = {
            row["symbol"]: dict(row)
            for row in connection.execute(
                "SELECT * FROM positions WHERE arm = ?", (arm,)
            )
        }
        required_symbols = set(desired_symbols) | set(positions) | {BENCHMARK_SYMBOL}
        missing = sorted(required_symbols - set(quotes))
        if missing:
            raise ValueError(
                f"{trade_date} execution snapshot lacks required quotes: {missing[:5]}"
            )
        factor_missing = sorted(
            symbol
            for symbol in required_symbols - {BENCHMARK_SYMBOL}
            if symbol not in factor_bars or factor_bars[symbol].factor is None
        )
        if factor_missing:
            raise ValueError(
                f"{trade_date} official Provider lacks factors: {factor_missing[:5]}"
            )

        cash = float(account["cash"])
        pre_trade_value = cash
        for symbol, position in positions.items():
            factor = float(factor_bars[symbol].factor)
            raw_price = float(quotes[symbol]["price"])
            pre_trade_value += float(position["adjusted_quantity"]) * factor * raw_price
        target_value = pre_trade_value * float(execution["riskDegree"]) / len(desired_symbols)
        desired_actual: dict[str, float] = {}
        for symbol in desired_symbols:
            raw_price = float(quotes[symbol]["price"])
            desired_actual[symbol] = self._rounded_actual_quantity(
                symbol, target_value / raw_price
            )

        current_actual = {
            symbol: float(position["adjusted_quantity"])
            * float(factor_bars[symbol].factor)
            for symbol, position in positions.items()
        }
        slippage_rate = float(execution["slippageBps"]) / 10_000.0

        for symbol in sorted(set(positions) | set(desired_actual)):
            delta = desired_actual.get(symbol, 0.0) - current_actual.get(symbol, 0.0)
            if delta >= -1e-6:
                continue
            quote = quotes[symbol]
            bar = factor_bars[symbol]
            sell_all = symbol not in desired_actual
            actual_quantity = current_actual[symbol] if sell_all else self._rounded_actual_quantity(
                symbol, -delta
            )
            reason = None
            if positions[symbol]["acquired_date"] >= trade_date:
                reason = "T+1 可卖数量不足"
            reason = reason or self._trade_reason(
                symbol, "sell", quote, bar.listing_sessions
            )
            raw_quote = float(quote["price"])
            raw_fill = raw_quote * (1.0 - slippage_rate)
            if reason or actual_quantity <= 0:
                self._record_order(
                    connection,
                    trade_date=trade_date,
                    arm=arm,
                    symbol=symbol,
                    side="sell",
                    actual_quantity=max(actual_quantity, 0.0),
                    quote_price=raw_quote,
                    fill_price=None,
                    status="rejected",
                    reason=reason or "卖出数量不足交易单位",
                )
                continue
            gross = actual_quantity * raw_fill
            cost = max(float(execution["minimumCost"]), gross * float(execution["sellCostRate"]))
            slippage = actual_quantity * (raw_quote - raw_fill)
            cash += gross - cost
            remaining_actual = max(0.0, current_actual[symbol] - actual_quantity)
            if remaining_actual <= 1e-6:
                connection.execute(
                    "DELETE FROM positions WHERE arm = ? AND symbol = ?", (arm, symbol)
                )
                positions.pop(symbol, None)
                current_actual.pop(symbol, None)
            else:
                factor = float(bar.factor)
                connection.execute(
                    "UPDATE positions SET adjusted_quantity = ?, factor = ?, "
                    "last_raw_price = ? WHERE arm = ? AND symbol = ?",
                    (remaining_actual / factor, factor, raw_quote, arm, symbol),
                )
                positions[symbol]["adjusted_quantity"] = remaining_actual / factor
                current_actual[symbol] = remaining_actual
            self._record_order(
                connection,
                trade_date=trade_date,
                arm=arm,
                symbol=symbol,
                side="sell",
                actual_quantity=actual_quantity,
                quote_price=raw_quote,
                fill_price=raw_fill,
                status="filled",
                reason=None,
                gross=gross,
                cost=cost,
                slippage=slippage,
            )

        for symbol in sorted(desired_actual):
            delta = desired_actual[symbol] - current_actual.get(symbol, 0.0)
            if delta <= 1e-6:
                continue
            quote = quotes[symbol]
            bar = factor_bars[symbol]
            actual_quantity = self._rounded_actual_quantity(symbol, delta)
            reason = self._trade_reason(symbol, "buy", quote, bar.listing_sessions)
            raw_quote = float(quote["price"])
            raw_fill = raw_quote * (1.0 + slippage_rate)
            minimum, increment = self._board_lot(symbol)
            rate = float(execution["buyCostRate"])
            while actual_quantity >= minimum:
                gross = actual_quantity * raw_fill
                cost = max(float(execution["minimumCost"]), gross * rate)
                if gross + cost <= cash + 1e-9:
                    break
                actual_quantity -= increment
            if reason or actual_quantity < minimum:
                self._record_order(
                    connection,
                    trade_date=trade_date,
                    arm=arm,
                    symbol=symbol,
                    side="buy",
                    actual_quantity=max(actual_quantity, 0.0),
                    quote_price=raw_quote,
                    fill_price=None,
                    status="rejected",
                    reason=reason or "可用资金不足",
                )
                continue
            gross = actual_quantity * raw_fill
            cost = max(float(execution["minimumCost"]), gross * rate)
            slippage = actual_quantity * (raw_fill - raw_quote)
            cash -= gross + cost
            factor = float(bar.factor)
            new_actual = current_actual.get(symbol, 0.0) + actual_quantity
            acquired_date = (
                positions[symbol]["acquired_date"] if symbol in positions else trade_date
            )
            connection.execute(
                "INSERT INTO positions(arm, symbol, adjusted_quantity, factor, acquired_date, "
                "last_raw_price) VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(arm, symbol) DO UPDATE SET adjusted_quantity=excluded.adjusted_quantity, "
                "factor=excluded.factor, acquired_date=excluded.acquired_date, "
                "last_raw_price=excluded.last_raw_price",
                (arm, symbol, new_actual / factor, factor, acquired_date, raw_quote),
            )
            current_actual[symbol] = new_actual
            positions[symbol] = {
                "adjusted_quantity": new_actual / factor,
                "factor": factor,
                "acquired_date": acquired_date,
            }
            self._record_order(
                connection,
                trade_date=trade_date,
                arm=arm,
                symbol=symbol,
                side="buy",
                actual_quantity=actual_quantity,
                quote_price=raw_quote,
                fill_price=raw_fill,
                status="filled",
                reason=None,
                gross=gross,
                cost=cost,
                slippage=slippage,
            )

        market_value = 0.0
        for symbol, position in positions.items():
            factor = float(factor_bars[symbol].factor)
            raw_price = float(quotes[symbol]["price"])
            adjusted_quantity = float(position["adjusted_quantity"])
            market_value += adjusted_quantity * factor * raw_price
            connection.execute(
                "UPDATE positions SET factor = ?, last_raw_price = ? "
                "WHERE arm = ? AND symbol = ?",
                (factor, raw_price, arm, symbol),
            )
        total_value = cash + market_value
        previous_nav = connection.execute(
            "SELECT * FROM nav_history WHERE arm = ? ORDER BY trade_date DESC LIMIT 1",
            (arm,),
        ).fetchone()
        previous_value = (
            float(previous_nav["total_value"])
            if previous_nav
            else float(account["initial_cash"])
        )
        daily_return = total_value / previous_value - 1.0
        if previous_nav:
            benchmark_daily_return = benchmark_price / float(previous_nav["benchmark_price"]) - 1.0
            benchmark_value = float(previous_nav["benchmark_value"]) * (
                1.0 + benchmark_daily_return
            )
        else:
            benchmark_daily_return = 0.0
            benchmark_value = float(account["initial_cash"])
        connection.execute(
            "INSERT INTO nav_history(trade_date, arm, cash, market_value, total_value, "
            "daily_return, benchmark_price, benchmark_value, benchmark_daily_return) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                trade_date,
                arm,
                cash,
                market_value,
                total_value,
                daily_return,
                benchmark_price,
                benchmark_value,
                benchmark_daily_return,
            ),
        )
        connection.execute(
            "UPDATE accounts SET cash = ?, updated_at = ? WHERE arm = ?",
            (cash, datetime.now(SHANGHAI_TZ).isoformat(), arm),
        )

    def settle_ready_sessions(self, *, now: datetime | None = None) -> list[str]:
        local_now = (now or datetime.now(SHANGHAI_TZ)).astimezone(SHANGHAI_TZ)
        settled: list[str] = []
        with self._operation_lock():
            with self._connect() as connection:
                pending = connection.execute(
                    "SELECT * FROM signal_sessions WHERE status = 'FROZEN_AWAITING_OFFICIAL' "
                    "ORDER BY trade_date"
                ).fetchall()
            if not pending:
                return settled
            provider = discover_latest_provider().expanduser().resolve()
            source = QlibBinaryDailySource(provider)
            if str(pending[0]["trade_date"]) not in source.calendar:
                return settled
            provider_contract, _ = self._verify_provider(provider)
            for session in pending:
                trade_date = str(session["trade_date"])
                if trade_date not in source.calendar:
                    break
                execution = read_verified_snapshot(Path(session["execution_snapshot_path"]))
                if execution.get("contentSha256") != session["execution_snapshot_sha256"]:
                    raise ValueError("intraday-shadow frozen execution snapshot changed")
                quotes = self._quote_map(execution)
                benchmark = quotes.get(BENCHMARK_SYMBOL)
                if benchmark is None or benchmark.get("isStale"):
                    raise ValueError(f"{trade_date} benchmark execution quote is unavailable")
                benchmark_price = float(benchmark["price"])
                with self._connect() as connection:
                    signals = {
                        arm: [
                            row["symbol"]
                            for row in connection.execute(
                                "SELECT symbol FROM signals WHERE trade_date = ? AND arm = ? "
                                "ORDER BY rank",
                                (trade_date, arm),
                            )
                        ]
                        for arm in ARMS
                    }
                    position_symbols = {
                        row["symbol"]
                        for row in connection.execute("SELECT symbol FROM positions")
                    }
                required = sorted(
                    set().union(*[set(values) for values in signals.values()])
                    | position_symbols
                )
                factor_bars = source.get_bars(required, trade_date)

                backup = self.run_dir / f".{self.db_path.name}.{trade_date}.bak"
                with sqlite3.connect(self.db_path) as origin, sqlite3.connect(backup) as target:
                    origin.backup(target)
                try:
                    with self._connect() as connection:
                        connection.execute("BEGIN IMMEDIATE")
                        current = connection.execute(
                            "SELECT status FROM signal_sessions WHERE trade_date = ?",
                            (trade_date,),
                        ).fetchone()
                        if current is None or current["status"] != "FROZEN_AWAITING_OFFICIAL":
                            continue
                        for arm in ARMS:
                            self._settle_arm(
                                connection,
                                arm=arm,
                                trade_date=trade_date,
                                quotes=quotes,
                                factor_bars=factor_bars,
                                desired_symbols=signals[arm],
                                benchmark_price=benchmark_price,
                            )
                        connection.execute(
                            "UPDATE signal_sessions SET status='FINALIZED', provider_path=?, "
                            "provider_fingerprint=?, finalized_at=? WHERE trade_date=?",
                            (
                                str(provider),
                                provider_contract["fingerprint"],
                                local_now.isoformat(),
                                trade_date,
                            ),
                        )
                        self._append_audit_row(
                            connection,
                            "PAIRED_T_PLUS_ONE_SESSION_FINALIZED",
                            {
                                "tradeDate": trade_date,
                                "providerFingerprint": provider_contract["fingerprint"],
                                "executionSnapshotSha256": session[
                                    "execution_snapshot_sha256"
                                ],
                            },
                            local_now,
                        )
                    settled.append(trade_date)
                except Exception:
                    for suffix in ("-wal", "-shm"):
                        Path(str(self.db_path) + suffix).unlink(missing_ok=True)
                    shutil.copy2(backup, self.db_path)
                    raise
                finally:
                    backup.unlink(missing_ok=True)
        return settled

    def _arm_state(self, connection: sqlite3.Connection, arm: str) -> dict[str, Any]:
        account = connection.execute(
            "SELECT * FROM accounts WHERE arm = ?", (arm,)
        ).fetchone()
        nav = connection.execute(
            "SELECT * FROM nav_history WHERE arm = ? ORDER BY trade_date", (arm,)
        ).fetchall()
        positions = connection.execute(
            "SELECT * FROM positions WHERE arm = ? ORDER BY symbol", (arm,)
        ).fetchall()
        initial = float(account["initial_cash"])
        total = float(nav[-1]["total_value"]) if nav else initial
        benchmark = float(nav[-1]["benchmark_value"]) if nav else initial
        peak = initial
        max_drawdown = 0.0
        curve = []
        for row in nav:
            value = float(row["total_value"])
            peak = max(peak, value)
            max_drawdown = min(max_drawdown, value / peak - 1.0)
            curve.append(dict(row))
        return {
            "arm": arm,
            "label": (
                "负收益 × 换手率反转代理"
                if arm == "C1"
                else "仅负收益反转基线"
            ),
            "account": {
                "initialCash": initial,
                "cash": float(account["cash"]),
                "totalValue": total,
                "totalReturn": total / initial - 1.0,
                "benchmarkReturn": benchmark / initial - 1.0,
                "afterCostExcessReturn": total / benchmark - 1.0,
                "lastTradeDate": nav[-1]["trade_date"] if nav else None,
            },
            "positions": [
                {
                    "symbol": row["symbol"],
                    "quantity": float(row["adjusted_quantity"]) * float(row["factor"]),
                    "lastPrice": float(row["last_raw_price"]),
                    "marketValue": float(row["adjusted_quantity"])
                    * float(row["factor"])
                    * float(row["last_raw_price"]),
                    "acquiredDate": row["acquired_date"],
                }
                for row in positions
            ],
            "observationSessions": len(nav),
            "maxDrawdown": max_drawdown,
            "drawdownWarningTriggered": max_drawdown <= DRAW_DOWN_WARNING,
            "drawdownStopTriggered": max_drawdown <= DRAW_DOWN_STOP,
            "equityCurve": curve,
        }

    def get_state(self) -> dict[str, Any]:
        with self._connect() as connection:
            audit = self._verify_audit(connection)
            logical = _logical_database_contract(connection)
            arms = {arm: self._arm_state(connection, arm) for arm in ARMS}
            sessions = connection.execute(
                "SELECT * FROM signal_sessions ORDER BY trade_date"
            ).fetchall()
            missed = connection.execute(
                "SELECT * FROM missed_sessions ORDER BY trade_date"
            ).fetchall()
            latest_signals = {
                arm: [
                    dict(row)
                    for row in connection.execute(
                        "SELECT rank, symbol, score FROM signals WHERE trade_date = "
                        "(SELECT MAX(trade_date) FROM signals) AND arm = ? ORDER BY rank",
                        (arm,),
                    )
                ]
                for arm in ARMS
            }
        evidence_checkpoint = self._validate_evidence_checkpoint_chain(
            live_audit=audit,
            live_logical_sha256=logical["logicalSha256"],
        )
        finalized = [row for row in sessions if row["status"] == "FINALIZED"]
        pending = [row for row in sessions if row["status"] == "FROZEN_AWAITING_OFFICIAL"]
        c1_return = arms["C1"]["account"]["totalReturn"]
        b0_return = arms["B0"]["account"]["totalReturn"]
        incremental = (1.0 + c1_return) / (1.0 + b0_return) - 1.0
        risk_stopped = any(arms[arm]["drawdownStopTriggered"] for arm in ARMS)
        if missed:
            status = "MISSED_SESSION"
        elif risk_stopped:
            status = "RISK_STOPPED"
        elif pending:
            status = "AWAITING_OFFICIAL_DAILY"
        elif finalized:
            status = "ACTIVE"
        else:
            status = "STAGED"
        evidence_eligible = bool(finalized) and not missed
        engineering_gate = len(finalized) >= 20 and not missed
        early_gate = (
            len(finalized) >= 60
            and engineering_gate
            and arms["C1"]["account"]["afterCostExcessReturn"] > 0
            and incremental > 0
        )
        strong_gate = len(finalized) >= 120 and early_gate
        return {
            "runId": self.run_id,
            "usageRealm": "INTRADAY_SHADOW",
            "status": status,
            "formalPaperEligible": False,
            "automaticPromotionAllowed": False,
            "profitabilityProven": False,
            "paperReplication": False,
            "mechanismProxy": True,
            "declaration": {
                "path": str(self.declaration_path),
                "sha256": self.declaration_sha256,
                "declaredAt": self.declaration["declaredAt"],
            },
            "protocol": {
                "firstEligibleSignalDate": self.declaration["firstEligibleSignalDate"],
                **self.declaration["timing"],
                "topK": int(self.declaration["strategy"]["topK"]),
                "candidateFormula": self.declaration["strategy"]["candidateFormula"],
                "baselineFormula": self.declaration["strategy"]["baselineFormula"],
                "settlement": "T+1",
                "drawdownWarning": DRAW_DOWN_WARNING,
                "drawdownStop": DRAW_DOWN_STOP,
            },
            "pairedEvidence": {
                "frozenSessions": len(sessions),
                "observationSessions": len(finalized),
                "missedSessions": [dict(row) for row in missed],
                "c1AfterCostExcessReturn": arms["C1"]["account"][
                    "afterCostExcessReturn"
                ],
                "c1MinusB0Return": incremental,
                "engineeringGateReached": engineering_gate,
                "earlyMetricGatePassed": early_gate,
                "strongMetricGatePassed": strong_gate,
                "evidenceEligible": evidence_eligible,
            },
            "latestSession": dict(sessions[-1]) if sessions else None,
            "latestSignals": latest_signals,
            "arms": arms,
            "audit": audit,
            "evidenceCheckpoint": evidence_checkpoint,
        }

    def read_automation_status(self) -> dict[str, Any] | None:
        if not self.status_path.is_file():
            return None
        payload = json.loads(self.status_path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or payload.get("version") != STATUS_VERSION:
            raise ValueError("intraday-shadow automation status is invalid")
        return payload

    def _record_missed_session(self, trade_date: str, now: datetime) -> None:
        with self._operation_lock(), self._connect() as connection:
            existing = connection.execute(
                "SELECT 1 FROM signal_sessions WHERE trade_date = ?", (trade_date,)
            ).fetchone()
            if existing is not None:
                return
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                "INSERT OR IGNORE INTO missed_sessions(trade_date, reason, recorded_at) "
                "VALUES (?, 'FREEZE_DEADLINE_PASSED', ?)",
                (trade_date, now.astimezone(SHANGHAI_TZ).isoformat()),
            )
            if cursor.rowcount:
                self._append_audit_row(
                    connection,
                    "SIGNAL_SESSION_MISSED_NO_BACKFILL",
                    {"tradeDate": trade_date, "reason": "FREEZE_DEADLINE_PASSED"},
                    now,
                )

    def run_cycle(
        self,
        *,
        poll_seconds: int,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        local_now = (now or datetime.now(SHANGHAI_TZ)).astimezone(SHANGHAI_TZ)
        previous = self.read_automation_status() or {}
        actions: list[str] = []
        try:
            settled = self.settle_ready_sessions(now=local_now)
            actions.extend(f"FINALIZED:{value}" for value in settled)
            pre_freeze_state = self.get_state()
            today = local_now.date().isoformat()
            if (
                today >= str(self.declaration["firstEligibleSignalDate"])
                and pre_freeze_state["status"] not in {"RISK_STOPPED", "MISSED_SESSION"}
            ):
                root_provider = _resolve(self.declaration["providerRoot"]["path"])
                future_calendar = root_provider / "calendars" / "day_future.txt"
                trading_dates = {
                    line.strip()
                    for line in future_calendar.read_text(encoding="utf-8").splitlines()
                    if line.strip()
                }
                with self._connect() as connection:
                    recorded_dates = {
                        str(row[0])
                        for row in connection.execute(
                            "SELECT trade_date FROM signal_sessions "
                            "UNION SELECT trade_date FROM missed_sessions"
                        )
                    }
                missing_prior_dates = sorted(
                    trade_date
                    for trade_date in trading_dates
                    if str(self.declaration["firstEligibleSignalDate"])
                    <= trade_date
                    < today
                    and trade_date not in recorded_dates
                )
                for missing_date in missing_prior_dates:
                    self._record_missed_session(missing_date, local_now)
                    actions.append(f"MISSED:{missing_date}")
                if missing_prior_dates:
                    pre_freeze_state = self.get_state()
                timing = self.declaration["timing"]
                after = datetime.combine(
                    local_now.date(), _parse_clock(timing["freezeAfter"], "freezeAfter"), SHANGHAI_TZ
                )
                deadline = datetime.combine(
                    local_now.date(),
                    _parse_clock(timing["freezeDeadline"], "freezeDeadline"),
                    SHANGHAI_TZ,
                )
                if (
                    pre_freeze_state["status"] not in {"RISK_STOPPED", "MISSED_SESSION"}
                    and today in trading_dates
                    and after <= local_now <= deadline
                ):
                    before = self.get_state()["pairedEvidence"]["frozenSessions"]
                    self.freeze_session(today, now=local_now)
                    after_count = self.get_state()["pairedEvidence"]["frozenSessions"]
                    if after_count > before:
                        actions.append(f"FROZEN:{today}")
                elif (
                    pre_freeze_state["status"] not in {"RISK_STOPPED", "MISSED_SESSION"}
                    and today in trading_dates
                    and local_now > deadline
                ):
                    self._record_missed_session(today, local_now)
                    actions.append(f"MISSED:{today}")
            checkpoint = self.ensure_evidence_checkpoint(now=local_now)
            state = self.get_state()
            payload = {
                "version": STATUS_VERSION,
                "runId": self.run_id,
                "enabled": True,
                "status": state["status"],
                "lastCheckedAt": local_now.isoformat(),
                "lastSuccessAt": local_now.isoformat(),
                "nextCheckAt": (local_now + timedelta(seconds=poll_seconds)).isoformat(),
                "pollSeconds": poll_seconds,
                "consecutiveErrors": 0,
                "lastError": None,
                "actions": actions,
                "state": state,
                "evidenceCheckpoint": checkpoint,
            }
            _atomic_json_write(self.status_path, payload)
            return payload
        except Exception as exc:
            payload = {
                "version": STATUS_VERSION,
                "runId": self.run_id,
                "enabled": True,
                "status": "ERROR",
                "lastCheckedAt": local_now.isoformat(),
                "lastSuccessAt": previous.get("lastSuccessAt"),
                "nextCheckAt": (local_now + timedelta(seconds=poll_seconds)).isoformat(),
                "pollSeconds": poll_seconds,
                "consecutiveErrors": int(previous.get("consecutiveErrors") or 0) + 1,
                "lastError": str(exc)[:1000],
                "actions": actions,
                "state": previous.get("state"),
                "evidenceCheckpoint": previous.get("evidenceCheckpoint"),
            }
            _atomic_json_write(self.status_path, payload)
            raise
