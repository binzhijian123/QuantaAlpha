"""Tamper-evident SQLite checkpoints for the paired research-shadow run."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Mapping
from zoneinfo import ZoneInfo

from quantaalpha.trading.research_shadow import (
    ARMS,
    DEFAULT_DECLARATION,
    ResearchShadowCoordinator,
)
from quantaalpha.utils.provider_lineage import validate_lineage_chain
from quantaalpha.utils.qlib_provider import (
    calendar_bounds,
    provider_contract_fingerprint,
)


SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
CHECKPOINT_VERSION = 1
CHECKPOINT_KIND = "research_shadow_evidence_checkpoint"
CHECKPOINT_ALGORITHM = "sha256-json-chain-with-sqlite-backup-v1"
CHECKPOINT_DIR_NAME = "evidence_checkpoints"
_EVIDENCE_LOCK = threading.RLock()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _json_value(value: Any) -> Any:
    if isinstance(value, bytes):
        return {"bytesHex": value.hex()}
    return value


def _rows_contract(columns: list[str], rows: list[sqlite3.Row]) -> dict[str, Any]:
    serialized = sorted(
        _canonical_json(
            {column: _json_value(row[column]) for column in columns}
        )
        for row in rows
    )
    digest = hashlib.sha256()
    for row in serialized:
        encoded = row.encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    return {"rows": len(serialized), "sha256": digest.hexdigest()}


def _query_contract(
    connection: sqlite3.Connection,
    query: str,
    parameters: tuple[Any, ...] = (),
) -> dict[str, Any]:
    cursor = connection.execute(query, parameters)
    columns = [str(item[0]) for item in cursor.description or ()]
    return _rows_contract(columns, cursor.fetchall())


def _logical_database_contract(connection: sqlite3.Connection) -> dict[str, Any]:
    schema_rows = connection.execute(
        "SELECT type, name, tbl_name, sql FROM sqlite_master "
        "WHERE sql IS NOT NULL ORDER BY type, name"
    ).fetchall()
    schema_contract = _rows_contract(
        ["type", "name", "tbl_name", "sql"], schema_rows
    )
    tables: dict[str, dict[str, Any]] = {}
    for row in connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
    ):
        name = str(row[0])
        quoted = '"' + name.replace('"', '""') + '"'
        cursor = connection.execute(f"SELECT * FROM {quoted}")
        columns = [str(item[0]) for item in cursor.description or ()]
        tables[name] = _rows_contract(columns, cursor.fetchall())
    logical_sha = hashlib.sha256(
        _canonical_json({"schema": schema_contract, "tables": tables}).encode("utf-8")
    ).hexdigest()
    return {
        "integrityCheck": "ok",
        "schema": schema_contract,
        "tables": tables,
        "logicalSha256": logical_sha,
    }


def _audit_summary(connection: sqlite3.Connection) -> dict[str, Any]:
    rows = connection.execute(
        "SELECT sequence, event_type, payload_json, previous_hash, event_hash, "
        "created_at FROM shadow_audit ORDER BY sequence"
    ).fetchall()
    previous_hash = "0" * 64
    for expected_sequence, row in enumerate(rows, start=1):
        if int(row["sequence"]) != expected_sequence:
            raise ValueError("research-shadow audit sequence is not contiguous")
        if row["previous_hash"] != previous_hash:
            raise ValueError("research-shadow audit previous hash mismatch")
        payload = json.loads(str(row["payload_json"]))
        if _canonical_json(payload) != row["payload_json"]:
            raise ValueError("research-shadow audit payload is not canonical JSON")
        expected_hash = hashlib.sha256(
            (
                previous_hash
                + "\0"
                + str(row["event_type"])
                + "\0"
                + str(row["payload_json"])
                + "\0"
                + str(row["created_at"])
            ).encode("utf-8")
        ).hexdigest()
        if row["event_hash"] != expected_hash:
            raise ValueError("research-shadow audit event hash mismatch")
        previous_hash = expected_hash
    latest = rows[-1] if rows else None
    return {
        "valid": True,
        "events": len(rows),
        "head": latest["event_hash"] if latest else None,
        "lastEvent": latest["event_type"] if latest else None,
        "lastEventAt": latest["created_at"] if latest else None,
    }


def _account_contract(
    connection: sqlite3.Connection, account_id: str
) -> dict[str, Any]:
    contracts = {
        "accounts": _query_contract(
            connection, "SELECT * FROM accounts WHERE id = ?", (account_id,)
        ),
        "signalBatches": _query_contract(
            connection,
            "SELECT * FROM signal_batches WHERE account_id = ?",
            (account_id,),
        ),
        "signals": _query_contract(
            connection,
            "SELECT signals.* FROM signals JOIN signal_batches "
            "ON signal_batches.id = signals.batch_id "
            "WHERE signal_batches.account_id = ?",
            (account_id,),
        ),
        "runs": _query_contract(
            connection, "SELECT * FROM runs WHERE account_id = ?", (account_id,)
        ),
        "positions": _query_contract(
            connection,
            "SELECT * FROM positions WHERE account_id = ?",
            (account_id,),
        ),
        "orders": _query_contract(
            connection, "SELECT * FROM orders WHERE account_id = ?", (account_id,)
        ),
        "fills": _query_contract(
            connection, "SELECT * FROM fills WHERE account_id = ?", (account_id,)
        ),
        "navHistory": _query_contract(
            connection,
            "SELECT * FROM nav_history WHERE account_id = ?",
            (account_id,),
        ),
    }
    return {
        "tables": contracts,
        "logicalSha256": hashlib.sha256(
            _canonical_json(contracts).encode("utf-8")
        ).hexdigest(),
    }


def _collection_contract(values: list[Any]) -> dict[str, Any]:
    return {
        "rows": len(values),
        "sha256": hashlib.sha256(
            _canonical_json(values).encode("utf-8")
        ).hexdigest(),
    }


def _signal_summary(signal: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if signal is None:
        return None
    targets = list(signal.get("targets") or [])
    return {
        "signalId": signal.get("signalId"),
        "signalDate": signal.get("signalDate"),
        "status": signal.get("status"),
        "targets": _collection_contract(targets),
        "sourceSha256": hashlib.sha256(
            _canonical_json(signal.get("source") or {}).encode("utf-8")
        ).hexdigest(),
    }


def _arm_evidence(
    arm: str,
    arm_state: Mapping[str, Any],
    connection: sqlite3.Connection,
) -> dict[str, Any]:
    strategy = arm_state.get("strategy") or {}
    equity = list(arm_state.get("equityCurve") or [])
    positions = list(arm_state.get("positions") or [])
    return {
        "label": arm_state.get("label"),
        "account": dict(arm_state.get("account") or {}),
        "pendingSignal": _signal_summary(arm_state.get("pendingSignal")),
        "latestSignal": _signal_summary(arm_state.get("latestSignal")),
        "positions": _collection_contract(positions),
        "equityCurve": {
            **_collection_contract(equity),
            "latest": equity[-1] if equity else None,
        },
        "paperValidation": dict(arm_state.get("paperValidation") or {}),
        "strategy": {
            "snapshotId": strategy.get("snapshotId"),
            "providerFingerprint": strategy.get("providerFingerprint"),
            "validationStatus": strategy.get("validationStatus"),
            "featureCount": strategy.get("featureCount"),
            "executionContractSha256": hashlib.sha256(
                _canonical_json(strategy.get("executionContract") or {}).encode("utf-8")
            ).hexdigest(),
        },
        "ledger": _account_contract(connection, arm),
    }


def _load_frozen_contract(declaration_path: Path) -> dict[str, Any]:
    path = declaration_path.expanduser().resolve()
    declaration = json.loads(path.read_text(encoding="utf-8"))
    if declaration.get("usageRealm") != "RESEARCH_SHADOW":
        raise ValueError("Evidence checkpoints require a RESEARCH_SHADOW declaration")
    if declaration.get("formalPaperEligible") is not False:
        raise ValueError("Research-shadow evidence cannot be formal-paper eligible")
    declaration_sha = _sha256(path)
    manifest_path = Path(declaration["manifestPath"]).expanduser().resolve()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if (
        manifest.get("usageRealm") != "RESEARCH_SHADOW"
        or manifest.get("runId") != declaration.get("runId")
        or manifest.get("declarationSha256") != declaration_sha
    ):
        raise ValueError("Research-shadow manifest does not bind the declaration")
    snapshots: dict[str, dict[str, Any]] = {}
    for arm in ARMS:
        snapshot_path = Path(
            declaration["arms"][arm]["snapshotPath"]
        ).expanduser().resolve()
        expected_sha = (manifest.get("arms") or {}).get(arm, {}).get(
            "snapshotSha256"
        )
        if not snapshot_path.is_file() or not expected_sha:
            raise ValueError(f"Research-shadow {arm} snapshot is missing")
        actual_sha = _sha256(snapshot_path)
        if actual_sha != expected_sha:
            raise ValueError(f"Research-shadow {arm} snapshot hash mismatch")
        snapshots[arm] = {"path": str(snapshot_path), "sha256": actual_sha}
    return {
        "path": path,
        "declaration": declaration,
        "declarationSha256": declaration_sha,
        "manifestPath": manifest_path,
        "manifestSha256": _sha256(manifest_path),
        "snapshots": snapshots,
    }


def evidence_checkpoint_directory(
    declaration_path: Path = DEFAULT_DECLARATION,
) -> Path:
    declaration = json.loads(
        declaration_path.expanduser().resolve().read_text(encoding="utf-8")
    )
    return (
        Path(declaration["databasePath"]).expanduser().resolve().parent
        / CHECKPOINT_DIR_NAME
    )


@contextmanager
def _operation_lock(declaration: Mapping[str, Any]):
    database = Path(declaration["databasePath"]).expanduser().resolve()
    lock_path = database.parent / ".operation.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="ascii") as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _sqlite_snapshot(database: Path, destination: Path) -> None:
    source_uri = database.expanduser().resolve().as_uri() + "?mode=ro"
    with sqlite3.connect(source_uri, uri=True, timeout=30) as source:
        source.execute("PRAGMA query_only = ON")
        with sqlite3.connect(destination, timeout=30) as target:
            source.backup(target)
    with _connect_frozen_snapshot(destination) as check:
        result = str(check.execute("PRAGMA integrity_check").fetchone()[0])
    if result != "ok":
        raise ValueError(f"Research-shadow SQLite backup failed integrity check: {result}")


def _connect_frozen_snapshot(path: Path) -> sqlite3.Connection:
    uri = path.expanduser().resolve().as_uri() + "?mode=ro&immutable=1"
    return sqlite3.connect(uri, uri=True, timeout=30)


def _provider_evidence(
    contract: Mapping[str, Any], state: Mapping[str, Any]
) -> dict[str, Any]:
    declaration = contract["declaration"]
    state_market = state.get("marketData") or {}
    provider_path = Path(str(state_market.get("providerPath") or "")).expanduser().resolve()
    provider = provider_contract_fingerprint(provider_path, "csi300")
    bounds = calendar_bounds(provider_path)
    if bounds is None or bounds[1] != state_market.get("latestDate"):
        raise ValueError("Evidence state does not match the active Provider calendar")
    root_fingerprint = str(declaration["providerFingerprint"])
    active_fingerprint = str(provider["fingerprint"])
    if state_market.get("frozenProviderFingerprint") != root_fingerprint:
        raise ValueError("Evidence state has the wrong frozen Provider fingerprint")
    lineage: list[dict[str, Any]] = []
    if active_fingerprint == root_fingerprint:
        frozen_path = Path(declaration["providerPath"]).expanduser().resolve()
        if provider_path != frozen_path:
            raise ValueError("Frozen Provider fingerprint came from an unexpected path")
    else:
        proof_dir = Path(
            declaration["providerLineageProofDir"]
        ).expanduser().resolve()
        proofs = validate_lineage_chain(
            proof_dir,
            root_fingerprint=root_fingerprint,
            successor_fingerprint=active_fingerprint,
            run_id=str(declaration["runId"]),
            declaration_sha256=str(contract["declarationSha256"]),
            successor_provider_path=provider_path,
            verify_active_feature_tree=True,
        )
        for proof in proofs:
            proof_path = proof_dir / f"{proof['successorFingerprint']}.json"
            lineage.append(
                {
                    "parentFingerprint": proof["parentFingerprint"],
                    "successorFingerprint": proof["successorFingerprint"],
                    "proofSha256": _sha256(proof_path),
                }
            )
    return {
        "path": str(provider_path),
        "latestDate": bounds[1],
        "calendarSessions": bounds[2],
        "rootFingerprint": root_fingerprint,
        "activeFingerprint": active_fingerprint,
        "contract": provider,
        "lineageDepth": len(lineage),
        "lineage": lineage,
    }


def _build_evidence(
    contract: Mapping[str, Any],
    state: Mapping[str, Any],
    snapshot_path: Path,
) -> dict[str, Any]:
    declaration = contract["declaration"]
    if (
        state.get("runId") != declaration.get("runId")
        or state.get("usageRealm") != "RESEARCH_SHADOW"
    ):
        raise ValueError("Evidence state does not belong to the frozen run")
    implementation = state.get("executionImplementation")
    lifecycle = state.get("lifecycle") or {}
    if lifecycle.get("status") == "ACTIVE" and (
        not isinstance(implementation, Mapping)
        or implementation.get("frozen") is not True
        or not implementation.get("sha256")
    ):
        raise ValueError("Active research-shadow execution implementation is not frozen")
    with _connect_frozen_snapshot(snapshot_path) as connection:
        connection.row_factory = sqlite3.Row
        logical = _logical_database_contract(connection)
        audit = _audit_summary(connection)
        state_audit = state.get("audit") or {}
        if any(
            state_audit.get(key) != audit.get(key)
            for key in ("valid", "events", "head", "lastEvent", "lastEventAt")
        ):
            raise ValueError("Evidence state audit does not match the SQLite snapshot")
        arms = {
            arm: _arm_evidence(arm, state["arms"][arm], connection)
            for arm in ARMS
        }
    return {
        "runId": declaration["runId"],
        "usageRealm": "RESEARCH_SHADOW",
        "formalPaperEligible": False,
        "declaration": {
            "path": str(contract["path"]),
            "sha256": contract["declarationSha256"],
        },
        "manifest": {
            "path": str(contract["manifestPath"]),
            "sha256": contract["manifestSha256"],
        },
        "modelSnapshots": contract["snapshots"],
        "provider": _provider_evidence(contract, state),
        "ledger": {
            "databasePath": str(
                Path(declaration["databasePath"]).expanduser().resolve()
            ),
            "sqliteBackupSha256": _sha256(snapshot_path),
            "sqliteBackupBytes": snapshot_path.stat().st_size,
            **logical,
        },
        "state": {
            "status": state.get("status"),
            "nextAction": state.get("nextAction"),
            "protocol": dict(state.get("protocol") or {}),
            "executionContract": dict(state.get("executionContract") or {}),
            "executionImplementation": (
                dict(implementation) if isinstance(implementation, Mapping) else None
            ),
            "pairedEvidence": dict(state.get("pairedEvidence") or {}),
            "audit": audit,
            "arms": arms,
        },
    }


def _checkpoint_payloads(directory: Path) -> list[tuple[Path, dict[str, Any], str]]:
    payloads: list[tuple[Path, dict[str, Any], str]] = []
    for path in sorted(directory.glob("checkpoint_*.json")):
        sidecar = path.with_suffix(path.suffix + ".sha256")
        if not sidecar.is_file():
            raise ValueError(f"Evidence checkpoint sidecar is missing: {sidecar}")
        actual_sha = _sha256(path)
        if sidecar.read_text(encoding="ascii").strip() != actual_sha:
            raise ValueError("Evidence checkpoint JSON SHA256 sidecar mismatch")
        payloads.append((path, json.loads(path.read_text(encoding="utf-8")), actual_sha))
    return payloads


def _validate_snapshot_artifact(
    directory: Path, payload: Mapping[str, Any], verify_contents: bool
) -> None:
    artifact = payload.get("sqliteSnapshot") or {}
    filename = str(artifact.get("file") or "")
    if not filename or Path(filename).name != filename:
        raise ValueError("Evidence checkpoint SQLite artifact name is invalid")
    snapshot = directory / filename
    sidecar = snapshot.with_suffix(snapshot.suffix + ".sha256")
    if not snapshot.is_file() or not sidecar.is_file():
        raise ValueError("Evidence checkpoint SQLite artifact is missing")
    actual_sha = _sha256(snapshot)
    expected_sha = str(artifact.get("sha256") or "")
    if sidecar.read_text(encoding="ascii").strip() != actual_sha or actual_sha != expected_sha:
        raise ValueError("Evidence checkpoint SQLite SHA256 mismatch")
    evidence_ledger = (payload.get("evidence") or {}).get("ledger") or {}
    if (
        evidence_ledger.get("sqliteBackupSha256") != actual_sha
        or int(evidence_ledger.get("sqliteBackupBytes") or -1) != snapshot.stat().st_size
    ):
        raise ValueError("Evidence JSON does not bind its SQLite snapshot")
    if not verify_contents:
        return
    with _connect_frozen_snapshot(snapshot) as connection:
        connection.row_factory = sqlite3.Row
        if str(connection.execute("PRAGMA integrity_check").fetchone()[0]) != "ok":
            raise ValueError("Evidence SQLite snapshot failed integrity check")
        logical = _logical_database_contract(connection)
        audit = _audit_summary(connection)
    for key in ("schema", "tables", "logicalSha256", "integrityCheck"):
        if evidence_ledger.get(key) != logical.get(key):
            raise ValueError("Evidence SQLite logical contract mismatch")
    if ((payload.get("evidence") or {}).get("state") or {}).get("audit") != audit:
        raise ValueError("Evidence SQLite audit summary mismatch")


def _chain_summary(
    directory: Path,
    payloads: list[tuple[Path, dict[str, Any], str]],
    status: str,
) -> dict[str, Any]:
    if not payloads:
        return {
            "valid": True,
            "status": status,
            "directory": str(directory),
            "count": 0,
            "latest": None,
        }
    path, payload, file_sha = payloads[-1]
    evidence = payload["evidence"]
    return {
        "valid": True,
        "status": status,
        "directory": str(directory),
        "count": len(payloads),
        "latest": {
            "sequence": payload["sequence"],
            "createdAt": payload["createdAt"],
            "file": path.name,
            "sha256": file_sha,
            "evidenceSha256": payload["evidenceSha256"],
            "sqliteFile": payload["sqliteSnapshot"]["file"],
            "sqliteSha256": payload["sqliteSnapshot"]["sha256"],
            "providerLatestDate": evidence["provider"]["latestDate"],
            "auditEvents": evidence["state"]["audit"]["events"],
            "auditHead": evidence["state"]["audit"]["head"],
        },
    }


def validate_evidence_checkpoint_chain(
    declaration_path: Path = DEFAULT_DECLARATION,
    *,
    verify_snapshot_contents: bool = True,
) -> dict[str, Any]:
    contract = _load_frozen_contract(declaration_path)
    directory = evidence_checkpoint_directory(declaration_path)
    if not directory.exists():
        return _chain_summary(directory, [], "EMPTY")
    payloads = _checkpoint_payloads(directory)
    expected_previous: dict[str, str] | None = None
    expected_files: set[str] = set()
    for expected_sequence, (path, payload, file_sha) in enumerate(payloads, start=1):
        evidence_sha = hashlib.sha256(
            _canonical_json(payload.get("evidence") or {}).encode("utf-8")
        ).hexdigest()
        provider_date = str(
            ((payload.get("evidence") or {}).get("provider") or {}).get(
                "latestDate"
            )
            or ""
        )
        expected_base = (
            f"checkpoint_{expected_sequence:06d}_{provider_date}_{evidence_sha[:12]}"
        )
        if (
            payload.get("version") != CHECKPOINT_VERSION
            or payload.get("kind") != CHECKPOINT_KIND
            or payload.get("algorithm") != CHECKPOINT_ALGORITHM
            or payload.get("sequence") != expected_sequence
            or payload.get("runId") != contract["declaration"]["runId"]
            or (payload.get("evidence") or {}).get("declaration", {}).get("sha256")
            != contract["declarationSha256"]
            or path.name != f"{expected_base}.json"
            or (payload.get("sqliteSnapshot") or {}).get("file")
            != f"{expected_base}.sqlite3"
        ):
            raise ValueError("Evidence checkpoint header is invalid")
        if payload.get("evidenceSha256") != evidence_sha:
            raise ValueError("Evidence checkpoint evidence SHA256 mismatch")
        if payload.get("previousCheckpoint") != expected_previous:
            raise ValueError("Evidence checkpoint previous link mismatch")
        _validate_snapshot_artifact(directory, payload, verify_snapshot_contents)
        sqlite_file = str(payload["sqliteSnapshot"]["file"])
        expected_files.update(
            {
                path.name,
                path.name + ".sha256",
                sqlite_file,
                sqlite_file + ".sha256",
            }
        )
        expected_previous = {"file": path.name, "sha256": file_sha}
    actual_files = {
        item.name for item in directory.glob("checkpoint_*") if item.is_file()
    }
    if actual_files != expected_files:
        raise ValueError("Evidence checkpoint directory has unexpected artifacts")
    return _chain_summary(directory, payloads, "VALID")


def _assert_append_only_transition(
    previous: Mapping[str, Any],
    current: Mapping[str, Any],
    current_snapshot: Path,
) -> None:
    previous_audit = previous["state"]["audit"]
    current_audit = current["state"]["audit"]
    previous_events = int(previous_audit["events"])
    current_events = int(current_audit["events"])
    if current_events < previous_events:
        raise ValueError("Evidence audit history moved backwards")
    if previous_events:
        with _connect_frozen_snapshot(current_snapshot) as connection:
            row = connection.execute(
                "SELECT event_hash FROM shadow_audit WHERE sequence = ?",
                (previous_events,),
            ).fetchone()
        if row is None or row[0] != previous_audit["head"]:
            raise ValueError("Evidence audit history is not append-only")

    previous_ledger = previous["ledger"]
    current_ledger = current["ledger"]
    ledger_changed = (
        current_ledger["logicalSha256"] != previous_ledger["logicalSha256"]
    )
    provider_changed = any(
        current["provider"][key] != previous["provider"][key]
        for key in ("path", "latestDate", "activeFingerprint")
    )
    if ledger_changed and current_events == previous_events:
        raise ValueError("Research-shadow ledger changed without an audit-chain advance")
    if not ledger_changed and not provider_changed:
        raise ValueError("Evidence state changed without a ledger or Provider transition")


def _write_new_file(path: Path, content: bytes, mode: int) -> None:
    if path.exists():
        raise FileExistsError(f"Refusing to overwrite immutable evidence: {path}")
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_bytes(content)
    os.chmod(temporary, mode)
    os.replace(temporary, path)


def ensure_evidence_checkpoint(
    declaration_path: Path = DEFAULT_DECLARATION,
    *,
    now: datetime | None = None,
    coordinator_factory: Callable[[Path], ResearchShadowCoordinator] = ResearchShadowCoordinator,
) -> dict[str, Any]:
    path = declaration_path.expanduser().resolve()
    with _EVIDENCE_LOCK:
        contract = _load_frozen_contract(path)
        declaration = contract["declaration"]
        database = Path(declaration["databasePath"]).expanduser().resolve()
        if not database.is_file():
            raise FileNotFoundError(f"Research-shadow database is missing: {database}")
        with _operation_lock(declaration):
            directory = evidence_checkpoint_directory(path)
            directory.mkdir(parents=True, exist_ok=True)
            existing_summary = validate_evidence_checkpoint_chain(
                path, verify_snapshot_contents=True
            )
            payloads = _checkpoint_payloads(directory)
            pending_snapshot = directory / (
                f".pending_{os.getpid()}_{uuid.uuid4().hex}.sqlite3"
            )
            try:
                coordinator = coordinator_factory(path)
                state = coordinator.get_state()
                _sqlite_snapshot(database, pending_snapshot)
                evidence = _build_evidence(contract, state, pending_snapshot)
                evidence_sha = hashlib.sha256(
                    _canonical_json(evidence).encode("utf-8")
                ).hexdigest()
                if payloads and payloads[-1][1]["evidenceSha256"] == evidence_sha:
                    return {**existing_summary, "status": "REUSED"}
                if payloads:
                    _assert_append_only_transition(
                        payloads[-1][1]["evidence"], evidence, pending_snapshot
                    )

                sequence = len(payloads) + 1
                trade_date = str(evidence["provider"]["latestDate"])
                base = f"checkpoint_{sequence:06d}_{trade_date}_{evidence_sha[:12]}"
                snapshot_path = directory / f"{base}.sqlite3"
                json_path = directory / f"{base}.json"
                snapshot_sha = evidence["ledger"]["sqliteBackupSha256"]
                snapshot_sidecar = snapshot_path.with_suffix(
                    snapshot_path.suffix + ".sha256"
                )
                json_sidecar = json_path.with_suffix(json_path.suffix + ".sha256")
                for target in (snapshot_path, snapshot_sidecar, json_path, json_sidecar):
                    if target.exists():
                        raise FileExistsError(
                            f"Refusing to overwrite immutable evidence: {target}"
                        )

                os.chmod(pending_snapshot, 0o444)
                os.replace(pending_snapshot, snapshot_path)
                _write_new_file(
                    snapshot_sidecar, (snapshot_sha + "\n").encode("ascii"), 0o444
                )
                previous = (
                    {"file": payloads[-1][0].name, "sha256": payloads[-1][2]}
                    if payloads
                    else None
                )
                payload = {
                    "version": CHECKPOINT_VERSION,
                    "kind": CHECKPOINT_KIND,
                    "algorithm": CHECKPOINT_ALGORITHM,
                    "sequence": sequence,
                    "createdAt": (now or datetime.now(SHANGHAI_TZ))
                    .astimezone(SHANGHAI_TZ)
                    .isoformat(),
                    "runId": declaration["runId"],
                    "previousCheckpoint": previous,
                    "evidenceSha256": evidence_sha,
                    "sqliteSnapshot": {
                        "file": snapshot_path.name,
                        "sha256": snapshot_sha,
                    },
                    "evidence": evidence,
                }
                encoded = (
                    json.dumps(
                        payload, ensure_ascii=False, indent=2, sort_keys=True
                    )
                    + "\n"
                ).encode("utf-8")
                _write_new_file(json_path, encoded, 0o444)
                _write_new_file(
                    json_sidecar,
                    (_sha256(json_path) + "\n").encode("ascii"),
                    0o444,
                )
                return validate_evidence_checkpoint_chain(path) | {"status": "CREATED"}
            finally:
                pending_snapshot.unlink(missing_ok=True)
