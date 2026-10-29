"""Isolated next-session rehearsal for a frozen research-shadow run."""

from __future__ import annotations

import hashlib
import json
import math
import os
import pickle
import re
import shutil
import sqlite3
import subprocess
import tempfile
from datetime import date, datetime, time
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence
from zoneinfo import ZoneInfo

import numpy as np

from quantaalpha.trading.market_data import (
    QlibBinaryDailySource,
    discover_latest_provider,
)
from quantaalpha.trading.research_shadow import (
    ARMS,
    DEFAULT_DECLARATION,
    ResearchShadowCoordinator,
)
from quantaalpha.trading.research_shadow_automation import (
    build_research_shadow_lineage_proof,
)
from quantaalpha.trading.research_shadow_evidence import (
    _audit_summary,
    _logical_database_contract,
    ensure_evidence_checkpoint,
    validate_evidence_checkpoint_chain,
)
from quantaalpha.trading.signal import ResearchShadowSignalGenerator
from quantaalpha.utils.provider_lineage import (
    DEFAULT_FIELDS,
    provider_feature_tree_contract,
    validate_lineage_chain,
    write_lineage_proof,
)
from quantaalpha.utils.qlib_provider import (
    calendar_bounds,
    provider_contract_fingerprint,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
REHEARSAL_KIND = "research_shadow_next_session_rehearsal"
REHEARSAL_VERSION = 1


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


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(dict(payload), ensure_ascii=False, indent=2, sort_keys=True)
        + "\n",
        encoding="utf-8",
    )


def _sqlite_backup(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    source_uri = source.expanduser().resolve().as_uri() + "?mode=ro"
    with sqlite3.connect(source_uri, uri=True, timeout=30) as source_connection:
        source_connection.execute("PRAGMA query_only = ON")
        with sqlite3.connect(destination, timeout=30) as target_connection:
            source_connection.backup(target_connection)
    immutable_uri = destination.resolve().as_uri() + "?mode=ro&immutable=1"
    with sqlite3.connect(immutable_uri, uri=True, timeout=30) as connection:
        if str(connection.execute("PRAGMA integrity_check").fetchone()[0]) != "ok":
            raise ValueError("rehearsal SQLite backup failed integrity_check")


def _qlib_code(symbol: str) -> str:
    normalized = symbol.strip().upper()
    if "." in normalized:
        digits, exchange = normalized.split(".", 1)
        if digits.isdigit() and exchange in {"SH", "SZ", "BJ"}:
            return f"{exchange.lower()}{digits}"
    if normalized[:2] in {"SH", "SZ", "BJ"} and normalized[2:].isdigit():
        return normalized.lower()
    raise ValueError(f"unsupported rehearsal instrument: {symbol}")


def _snapshot_payload(path: Path) -> dict[str, Any]:
    with path.open("rb") as stream:
        payload = pickle.load(stream)
    if not isinstance(payload, dict) or payload.get("version") != 1:
        raise ValueError(f"unsupported strategy snapshot: {path}")
    return payload


def snapshot_model_contract(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Fingerprint the fitted model and all inputs that affect its ranking."""
    wrapper = payload.get("model")
    booster = getattr(wrapper, "model", None)
    if booster is None or not hasattr(booster, "model_to_string"):
        raise ValueError("strategy snapshot does not expose a LightGBM model contract")
    components = {
        "modelSha256": hashlib.sha256(
            booster.model_to_string().encode("utf-8")
        ).hexdigest(),
        "featureColumnsSha256": hashlib.sha256(
            _canonical_json(list(payload.get("featureColumns") or [])).encode("utf-8")
        ).hexdigest(),
        "factorsSha256": hashlib.sha256(
            _canonical_json(list(payload.get("factors") or [])).encode("utf-8")
        ).hexdigest(),
        "executionContractSha256": hashlib.sha256(
            _canonical_json(payload.get("executionContract") or {}).encode("utf-8")
        ).hexdigest(),
        "modelConfigSha256": hashlib.sha256(
            _canonical_json(payload.get("modelConfig") or {}).encode("utf-8")
        ).hexdigest(),
    }
    return {
        **components,
        "semanticSha256": hashlib.sha256(
            _canonical_json(components).encode("utf-8")
        ).hexdigest(),
    }


def copy_snapshot_for_rehearsal(
    source: Path,
    destination: Path,
) -> dict[str, Any]:
    """Copy a frozen snapshot byte-for-byte so its observation ID cannot change."""
    payload = _snapshot_payload(source)
    before = snapshot_model_contract(payload)
    payload_binding = {
        key: payload.get(key)
        for key in (
            "runId",
            "declarationPath",
            "declarationSha256",
            "providerLineageProofDir",
        )
    }
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)
    copied = _snapshot_payload(destination)
    after = snapshot_model_contract(copied)
    snapshot_sha = _sha256(destination)
    source_sha = _sha256(source)
    if after != before or snapshot_sha != source_sha:
        raise ValueError("rehearsal snapshot copy changed the frozen model artifact")
    destination.with_suffix(destination.suffix + ".sha256").write_text(
        snapshot_sha + "\n", encoding="ascii"
    )
    return {
        "sourcePath": str(source.resolve()),
        "sourceSha256": source_sha,
        "copiedPath": str(destination.resolve()),
        "copiedSha256": snapshot_sha,
        "byteIdentical": True,
        "modelContract": before,
        "payloadBinding": payload_binding,
    }


class RehearsalSignalGenerator(ResearchShadowSignalGenerator):
    """Use an isolated proof directory while retaining the exact snapshot bytes."""

    def __init__(
        self,
        snapshot_path: Path,
        *,
        source: QlibBinaryDailySource,
        proof_dir: Path,
    ):
        super().__init__(snapshot_path, source=source)
        self.rehearsal_proof_dir = proof_dir.expanduser().resolve()

    def _validate_successor_proof(
        self,
        payload: dict[str, Any],
        base_fingerprint: str,
        successor_fingerprint: str,
    ) -> None:
        validate_lineage_chain(
            self.rehearsal_proof_dir,
            root_fingerprint=base_fingerprint,
            successor_fingerprint=successor_fingerprint,
            run_id=str(payload.get("runId") or ""),
            declaration_sha256=str(payload.get("declarationSha256") or ""),
            successor_provider_path=self.source.provider_path,
            verify_active_feature_tree=True,
        )


class RehearsalCoordinator(ResearchShadowCoordinator):
    """Production coordinator with only the model proof directory redirected."""

    def __init__(
        self,
        declaration_path: Path,
        *,
        source: QlibBinaryDailySource,
        model_proof_dir: Path,
    ):
        self.model_proof_dir = model_proof_dir.expanduser().resolve()
        super().__init__(declaration_path, source=source)

    def _generator(self, arm: str) -> ResearchShadowSignalGenerator:
        return RehearsalSignalGenerator(
            Path(self.declaration["arms"][arm]["snapshotPath"]),
            source=self.source,
            proof_dir=self.model_proof_dir,
        )


def snapshot_raw_fields(snapshot_paths: Iterable[Path]) -> tuple[str, ...]:
    fields = set(DEFAULT_FIELDS)
    for path in snapshot_paths:
        payload = _snapshot_payload(path)
        for factor in payload.get("factors") or []:
            expression = str((factor or {}).get("expression") or "")
            fields.update(
                value.lower()
                for value in re.findall(r"\$([A-Za-z][A-Za-z0-9_]*)", expression)
            )
    fields.discard("return")
    return tuple(sorted(fields))


def clone_provider_copy_on_write(
    source: Path,
    destination: Path,
    *,
    allow_full_copy: bool = False,
) -> dict[str, Any]:
    """Clone a Provider without hard links; prefer APFS clonefile semantics."""
    source = source.expanduser().resolve()
    destination = destination.expanduser().resolve()
    if destination.exists():
        raise FileExistsError(f"rehearsal Provider destination exists: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    clone_error: str | None = None
    if Path("/bin/cp").is_file():
        result = subprocess.run(
            ["/bin/cp", "-cR", str(source), str(destination)],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            method = "apfs_clonefile"
        else:
            clone_error = (result.stderr or result.stdout).strip()
            method = ""
    else:
        clone_error = "/bin/cp is unavailable"
        method = ""
    if not method:
        if not allow_full_copy:
            raise RuntimeError(
                "copy-on-write Provider clone is unavailable; refusing an implicit "
                f"full copy ({clone_error or 'unknown error'})"
            )
        shutil.copytree(source, destination)
        method = "full_copy"

    representative = Path("calendars/day.txt")
    source_file = source / representative
    destination_file = destination / representative
    if not destination_file.is_file() or _sha256(source_file) != _sha256(destination_file):
        raise ValueError("rehearsal Provider clone does not match its source")
    if source_file.stat().st_ino == destination_file.stat().st_ino:
        raise ValueError("rehearsal Provider clone unexpectedly uses hard links")
    return {
        "method": method,
        "sourcePath": str(source),
        "destinationPath": str(destination),
        "representativeSha256": _sha256(source_file),
        "sourceInode": source_file.stat().st_ino,
        "destinationInode": destination_file.stat().st_ino,
    }


def _feature_array(path: Path, expected_end_index: int) -> np.ndarray:
    if not path.is_file():
        raise FileNotFoundError(f"required rehearsal feature is missing: {path}")
    values = np.fromfile(path, dtype="<f4")
    if len(values) < 2 or not math.isfinite(float(values[0])):
        raise ValueError(f"invalid Qlib feature file: {path}")
    start = int(values[0])
    if float(start) != float(values[0]) or start < 0:
        raise ValueError(f"invalid Qlib feature start index: {path}")
    actual_end = start + len(values) - 2
    if actual_end != expected_end_index:
        raise ValueError(
            f"rehearsal feature does not end at the Provider cutoff: {path} "
            f"({actual_end} != {expected_end_index})"
        )
    return values


def _last_finite(values: np.ndarray, path: Path) -> float:
    finite = values[1:][np.isfinite(values[1:])]
    if len(finite) == 0:
        raise ValueError(f"rehearsal feature has no finite history: {path}")
    return float(finite[-1])


def _synthetic_row(symbol: str, previous: Mapping[str, float]) -> dict[str, float]:
    previous_close = float(previous["close"])
    if not math.isfinite(previous_close) or previous_close <= 0:
        raise ValueError(f"invalid prior close for rehearsal symbol {symbol}")
    seed = hashlib.sha256(symbol.encode("ascii")).digest()
    gap = (int.from_bytes(seed[:4], "big") / (2**32 - 1) - 0.5) * 0.006
    intraday = (int.from_bytes(seed[4:8], "big") / (2**32 - 1) - 0.5) * 0.012
    open_price = previous_close * (1.0 + gap)
    close = open_price * (1.0 + intraday)
    high = max(open_price, close) * 1.003
    low = min(open_price, close) * 0.997
    vwap = (open_price + high + low + close) / 4.0
    volume = max(float(previous.get("volume") or 0.0), 1.0)
    factor = float(previous.get("factor") or 1.0)
    return {
        "open": open_price,
        "high": high,
        "low": low,
        "close": close,
        "vwap": vwap,
        "volume": volume,
        "factor": factor,
        "amount": volume * vwap,
        "change": close / previous_close - 1.0,
        "adjclose": close,
    }


def append_synthetic_session(
    provider: Path,
    trade_date: str,
    *,
    market: str = "csi300",
    benchmark: str = "000300.SH",
    fields: Sequence[str] = DEFAULT_FIELDS,
) -> dict[str, Any]:
    """Append one deterministic, clearly synthetic session to a cloned Provider."""
    provider = provider.expanduser().resolve()
    trade_date = date.fromisoformat(trade_date).isoformat()
    calendar_path = provider / "calendars" / "day.txt"
    calendar = [
        line.strip()
        for line in calendar_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    if not calendar or calendar != sorted(set(calendar)):
        raise ValueError("rehearsal Provider calendar is invalid")
    parent_date = calendar[-1]
    if trade_date <= parent_date:
        raise ValueError(
            f"rehearsal trade date must follow {parent_date}: {trade_date}"
        )

    instrument_path = provider / "instruments" / f"{market}.txt"
    rows: list[str] = []
    active: set[str] = set()
    for line_number, line in enumerate(
        instrument_path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        if not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) < 3:
            raise ValueError(f"invalid instrument row {instrument_path}:{line_number}")
        code, start, end = parts[:3]
        if start <= parent_date <= end:
            active.add(code.strip().upper())
            if end < trade_date:
                parts[2] = trade_date
        rows.append("\t".join(parts))
    if not active:
        raise ValueError("rehearsal Provider has no active market instruments")

    normalized_fields = tuple(
        dict.fromkeys(str(field).strip().lower() for field in fields if str(field).strip())
    )
    invalid_fields = [
        field
        for field in normalized_fields
        if not re.fullmatch(r"[a-z][a-z0-9_]*", field)
    ]
    if invalid_fields:
        raise ValueError(f"invalid rehearsal feature fields: {invalid_fields}")
    if not set(DEFAULT_FIELDS).issubset(set(normalized_fields)):
        raise ValueError("rehearsal fields must include the lineage contract fields")
    parent_index = len(calendar) - 1
    symbols = sorted(active | {benchmark.upper()})
    appended_values = hashlib.sha256()
    for symbol in symbols:
        code = _qlib_code(symbol)
        arrays: dict[str, np.ndarray] = {}
        previous: dict[str, float] = {}
        for field in normalized_fields:
            feature_path = provider / "features" / code / f"{field}.day.bin"
            values = _feature_array(feature_path, parent_index)
            arrays[field] = values
            previous[field] = _last_finite(values, feature_path)
        row = _synthetic_row(symbol, previous)
        for field in normalized_fields:
            value = float(row.get(field, previous[field]))
            feature_path = provider / "features" / code / f"{field}.day.bin"
            with feature_path.open("ab") as stream:
                np.asarray([value], dtype="<f4").tofile(stream)
            appended_values.update(symbol.encode("ascii"))
            appended_values.update(b"\0")
            appended_values.update(field.encode("ascii"))
            appended_values.update(b"\0")
            appended_values.update(np.asarray([value], dtype="<f4").tobytes())

    instrument_path.write_text("\n".join(rows) + "\n", encoding="utf-8")
    calendar_path.write_text("\n".join([*calendar, trade_date]) + "\n", encoding="utf-8")
    manifest_path = provider / "qlib_bin.manifest.json"
    manifest = (
        json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest_path.is_file()
        else {}
    )
    base_release = manifest.get("release_tag")
    manifest.update(
        {
            "release_tag": f"REHEARSAL-{trade_date}",
            "target_trade_date": trade_date,
            "rehearsal_synthetic": {
                "synthetic": True,
                "evidence_eligible": False,
                "profitability_evidence": False,
                "base_release_tag": base_release,
                "base_cutoff": parent_date,
                "appended_trade_date": trade_date,
                "method": "deterministic-low-amplitude-engineering-bars-v1",
            },
        }
    )
    _write_json(manifest_path, manifest)

    source = QlibBinaryDailySource(provider)
    available = source.available_symbols(trade_date)
    if set(available) != {
        f"{value[2:]}.{value[:2]}" for value in active
    }:
        raise ValueError("rehearsal market membership did not extend exactly one session")
    bars = source.get_bars([*available, benchmark], trade_date)
    if len(bars) != len(set(available) | {benchmark}):
        raise ValueError("rehearsal session does not have complete executable bars")
    return {
        "parentDate": parent_date,
        "tradeDate": trade_date,
        "market": market,
        "benchmark": benchmark,
        "activeSymbols": len(active),
        "appendedFeatureFiles": len(symbols) * len(normalized_fields),
        "fields": list(normalized_fields),
        "syntheticValuesSha256": appended_values.hexdigest(),
    }


def _targets_mapping(value: Mapping[str, Any] | Sequence[Mapping[str, Any]]) -> dict[str, float]:
    if isinstance(value, Mapping):
        return {str(symbol): float(weight) for symbol, weight in value.items()}
    return {
        str(item["symbol"]): float(item["targetWeight"])
        for item in value
    }


def _targets_contract(value: Mapping[str, Any] | Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    mapping = _targets_mapping(value)
    return {
        "count": len(mapping),
        "sha256": hashlib.sha256(_canonical_json(mapping).encode("utf-8")).hexdigest(),
    }


def _database_contract(database: Path) -> dict[str, Any]:
    uri = database.expanduser().resolve().as_uri() + "?mode=ro"
    with sqlite3.connect(uri, uri=True, timeout=30) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only = ON")
        logical = _logical_database_contract(connection)
        audit = _audit_summary(connection)
    return {"logical": logical, "audit": audit}


def production_integrity_signature(
    declaration_path: Path,
    *,
    feature_fields: Sequence[str] = DEFAULT_FIELDS,
) -> dict[str, Any]:
    """Capture a WAL-aware, relevant-feature complete production signature."""
    declaration_path = declaration_path.expanduser().resolve()
    declaration = json.loads(declaration_path.read_text(encoding="utf-8"))
    manifest_path = Path(declaration["manifestPath"]).expanduser().resolve()
    provider = Path(declaration["providerPath"]).expanduser().resolve()
    database = Path(declaration["databasePath"]).expanduser().resolve()
    files: dict[str, str | None] = {
        "dotenv": _sha256(PROJECT_ROOT / ".env")
        if (PROJECT_ROOT / ".env").is_file()
        else None,
        "declaration": _sha256(declaration_path),
        "manifest": _sha256(manifest_path),
    }
    for arm in ARMS:
        snapshot = Path(declaration["arms"][arm]["snapshotPath"]).expanduser().resolve()
        files[f"snapshot{arm}"] = _sha256(snapshot)
    database_contract = _database_contract(database)
    evidence = validate_evidence_checkpoint_chain(declaration_path)
    payload = {
        "files": files,
        "runtimeProviderPath": str(discover_latest_provider().resolve()),
        "provider": provider_contract_fingerprint(provider, "csi300"),
        "providerFeatureTree": provider_feature_tree_contract(
            provider,
            market="csi300",
            benchmark=str(
                (declaration.get("execution") or {}).get("benchmark")
                or "000300.SH"
            ),
            fields=tuple(feature_fields),
        ),
        "database": database_contract,
        "evidenceCheckpoint": evidence,
    }
    return {
        **payload,
        "signatureSha256": hashlib.sha256(
            _canonical_json(payload).encode("utf-8")
        ).hexdigest(),
    }


def _prepare_isolated_contract(
    original_declaration_path: Path,
    workspace: Path,
    trade_date: str,
) -> tuple[Path, dict[str, Any], dict[str, Any]]:
    original_declaration_path = original_declaration_path.expanduser().resolve()
    original = json.loads(original_declaration_path.read_text(encoding="utf-8"))
    original_manifest_path = Path(original["manifestPath"]).expanduser().resolve()
    original_manifest = json.loads(
        original_manifest_path.read_text(encoding="utf-8")
    )
    run_root = workspace / "isolated_run"
    declaration_path = run_root / "declaration.json"
    manifest_path = run_root / "manifest.json"
    database_path = run_root / "research_shadow.sqlite3"
    proof_dir = run_root / "provider_lineage"
    snapshots = {arm: run_root / "snapshots" / f"{arm}.pkl" for arm in ARMS}

    declaration = json.loads(json.dumps(original))
    declaration.update(
        {
            "manifestPath": str(manifest_path.resolve()),
            "databasePath": str(database_path.resolve()),
            "providerLineageProofDir": str(proof_dir.resolve()),
            "rehearsalOnly": True,
            "rehearsalTradeDate": trade_date,
        }
    )
    for arm in ARMS:
        declaration["arms"][arm]["snapshotPath"] = str(snapshots[arm].resolve())
    _write_json(declaration_path, declaration)
    declaration_sha = _sha256(declaration_path)

    snapshot_contracts: dict[str, Any] = {}
    for arm in ARMS:
        source = Path(original["arms"][arm]["snapshotPath"]).expanduser().resolve()
        snapshot_contracts[arm] = copy_snapshot_for_rehearsal(
            source,
            snapshots[arm],
        )

    manifest = json.loads(json.dumps(original_manifest))
    manifest.update(
        {
            "createdAt": datetime.now(SHANGHAI_TZ).isoformat(),
            "declarationPath": str(declaration_path.resolve()),
            "declarationSha256": declaration_sha,
            "rehearsalOnly": True,
            "rehearsalTradeDate": trade_date,
        }
    )
    for arm in ARMS:
        manifest["arms"][arm]["snapshotPath"] = str(snapshots[arm].resolve())
        manifest["arms"][arm]["snapshotSha256"] = snapshot_contracts[arm][
            "copiedSha256"
        ]
    _write_json(manifest_path, manifest)
    manifest_sha = _sha256(manifest_path)

    original_database = Path(original["databasePath"]).expanduser().resolve()
    _sqlite_backup(original_database, database_path)
    with sqlite3.connect(database_path, timeout=30) as connection:
        replacements = {
            "declarationSha256": declaration_sha,
            "manifestSha256": manifest_sha,
        }
        for key, value in replacements.items():
            cursor = connection.execute(
                "UPDATE shadow_metadata SET value = ? WHERE key = ?",
                (value, key),
            )
            if cursor.rowcount != 1:
                raise ValueError(f"isolated ledger lacks frozen metadata key: {key}")
    return declaration_path, snapshot_contracts, {
        "declarationSha256": declaration_sha,
        "manifestSha256": manifest_sha,
        "databaseLogical": _database_contract(database_path),
    }


def _table_counts(database: Path) -> dict[str, dict[str, int]]:
    result: dict[str, dict[str, int]] = {}
    with sqlite3.connect(database, timeout=30) as connection:
        for arm in ARMS:
            result[arm] = {}
            for table in (
                "signal_batches",
                "signals",
                "runs",
                "positions",
                "orders",
                "fills",
                "nav_history",
            ):
                if table == "signals":
                    query = (
                        "SELECT COUNT(*) FROM signals JOIN signal_batches "
                        "ON signal_batches.id = signals.batch_id "
                        "WHERE signal_batches.account_id = ?"
                    )
                else:
                    query = f"SELECT COUNT(*) FROM {table} WHERE account_id = ?"
                result[arm][table] = int(connection.execute(query, (arm,)).fetchone()[0])
    return result


def _state_summary(state: Mapping[str, Any]) -> dict[str, Any]:
    paired_evidence = dict(state.get("pairedEvidence") or {})
    engine_evidence_eligible = bool(paired_evidence.get("evidenceEligible"))
    paired_evidence.update(
        {
            "evidenceEligible": False,
            "engineStateEvidenceEligible": engine_evidence_eligible,
            "evidenceExclusionReason": "synthetic_rehearsal_market_data",
        }
    )
    return {
        "status": state.get("status"),
        "nextAction": state.get("nextAction"),
        "marketLatestDate": (state.get("marketData") or {}).get("latestDate"),
        "pairedEvidence": paired_evidence,
        "audit": dict(state.get("audit") or {}),
        "arms": {
            arm: {
                "lastTradeDate": state["arms"][arm]["account"].get("lastTradeDate"),
                "positions": len(state["arms"][arm].get("positions") or []),
                "pendingSignalDate": (
                    (state["arms"][arm].get("pendingSignal") or {}).get("signalDate")
                ),
                "pendingTargets": len(
                    (state["arms"][arm].get("pendingSignal") or {}).get("targets")
                    or []
                ),
                "equityCurveRows": len(state["arms"][arm].get("equityCurve") or []),
            }
            for arm in ARMS
        },
    }


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(f"rehearsal acceptance gate failed: {message}")


def run_research_shadow_rehearsal(
    declaration_path: Path = DEFAULT_DECLARATION,
    *,
    trade_date: str | None = None,
    keep_workspace: bool = False,
    allow_full_copy: bool = False,
    progress: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Run the frozen pair through one synthetic next session without production writes."""
    emit = progress or (lambda _message: None)
    started_at = datetime.now(SHANGHAI_TZ)
    declaration_path = declaration_path.expanduser().resolve()
    declaration = json.loads(declaration_path.read_text(encoding="utf-8"))
    if declaration.get("usageRealm") != "RESEARCH_SHADOW":
        raise ValueError("rehearsal requires a RESEARCH_SHADOW declaration")
    if declaration.get("formalPaperEligible") is not False:
        raise ValueError("rehearsal declaration must remain formal-paper ineligible")

    source = QlibBinaryDailySource()
    parent = source.provider_path.expanduser().resolve()
    frozen_parent = Path(declaration["providerPath"]).expanduser().resolve()
    _require(
        parent == frozen_parent,
        "the active runtime Provider is not the frozen root Provider",
    )
    parent_bounds = calendar_bounds(parent)
    if parent_bounds is None:
        raise ValueError("frozen Provider has no calendar")
    selected_trade_date = trade_date or str(declaration["firstTradeDate"])
    _require(
        parent_bounds[1] == declaration["initialSignalDate"],
        "the frozen root Provider is no longer on the initial signal date",
    )
    _require(
        selected_trade_date == declaration["firstTradeDate"],
        "the synthetic session must equal the frozen first trade date",
    )
    original_snapshot_paths = [
        Path(declaration["arms"][arm]["snapshotPath"]).expanduser().resolve()
        for arm in ARMS
    ]
    raw_fields = snapshot_raw_fields(original_snapshot_paths)

    emit("capturing production integrity signature")
    before = production_integrity_signature(
        declaration_path, feature_fields=raw_fields
    )
    _require(
        before["provider"]["fingerprint"] == declaration["providerFingerprint"],
        "production Provider fingerprint differs from the frozen declaration",
    )
    live = ResearchShadowCoordinator(declaration_path, source=source)
    live_state = live.get_state()
    _require(live_state["status"] == "WAITING_OFFICIAL_DAILY", live_state["status"])
    _require(
        all(len(live_state["arms"][arm].get("positions") or []) == 0 for arm in ARMS),
        "the first-session rehearsal requires zero existing positions",
    )

    replay_contracts: dict[str, Any] = {}
    for arm in ARMS:
        emit(f"replaying frozen {arm} targets on {declaration['initialSignalDate']}")
        generated = live._generator(arm).generate_targets(
            declaration["initialSignalDate"]
        )
        pending = live_state["arms"][arm]["pendingSignal"]
        expected = _targets_contract(pending["targets"])
        actual = _targets_contract(generated["targets"])
        _require(actual == expected, f"{arm} replay differs from its frozen targets")
        replay_contracts[arm] = {"pending": expected, "replayed": actual, "match": True}

    temporary: tempfile.TemporaryDirectory[str] | None = None
    if keep_workspace:
        workspace = Path(tempfile.mkdtemp(prefix="quantaalpha-shadow-rehearsal-"))
    else:
        temporary = tempfile.TemporaryDirectory(
            prefix="quantaalpha-shadow-rehearsal-"
        )
        workspace = Path(temporary.name)
    try:
        emit("preparing isolated declaration, snapshots, and SQLite backup")
        isolated_declaration, snapshot_contracts, isolated_contract = (
            _prepare_isolated_contract(
                declaration_path, workspace, selected_trade_date
            )
        )
        synthetic_provider = workspace / "synthetic_provider"
        clone = clone_provider_copy_on_write(
            parent,
            synthetic_provider,
            allow_full_copy=allow_full_copy,
        )
        emit("appending one deterministic synthetic session")
        synthetic = append_synthetic_session(
            synthetic_provider,
            selected_trade_date,
            benchmark=str(declaration["execution"]["benchmark"]),
            fields=raw_fields,
        )

        emit("building append-only Provider lineage proof")
        proof, proof_path = build_research_shadow_lineage_proof(
            isolated_declaration, parent, synthetic_provider
        )
        _require(int(proof["appendedSessions"]) == 1, "Provider appended more than one session")
        _require(
            proof["firstAppendedDate"] == selected_trade_date,
            "Provider appended the wrong first session",
        )
        proof_artifact = write_lineage_proof(proof_path, proof)

        payload_bindings = [
            snapshot_contracts[arm]["payloadBinding"] for arm in ARMS
        ]
        model_run_ids = {str(item.get("runId") or "") for item in payload_bindings}
        model_declaration_shas = {
            str(item.get("declarationSha256") or "") for item in payload_bindings
        }
        _require(len(model_run_ids) == 1 and "" not in model_run_ids, "model run IDs diverged")
        _require(
            len(model_declaration_shas) == 1 and "" not in model_declaration_shas,
            "model declaration hashes diverged",
        )
        model_proof = dict(proof)
        model_proof.update(
            {
                "runId": next(iter(model_run_ids)),
                "declarationPath": str(
                    payload_bindings[0].get("declarationPath") or ""
                ),
                "declarationSha256": next(iter(model_declaration_shas)),
            }
        )
        model_proof_dir = workspace / "model_provider_lineage"
        model_proof_path = (
            model_proof_dir / f"{model_proof['successorFingerprint']}.json"
        )
        model_proof_artifact = write_lineage_proof(model_proof_path, model_proof)

        parent_source = QlibBinaryDailySource(parent)
        successor_source = QlibBinaryDailySource(synthetic_provider)
        baseline_factory = lambda path: RehearsalCoordinator(
            path,
            source=parent_source,
            model_proof_dir=model_proof_dir,
        )
        successor_factory = lambda path: RehearsalCoordinator(
            path,
            source=successor_source,
            model_proof_dir=model_proof_dir,
        )
        emit("creating isolated baseline evidence checkpoint")
        baseline_checkpoint = ensure_evidence_checkpoint(
            isolated_declaration,
            now=started_at,
            coordinator_factory=baseline_factory,
        )
        _require(baseline_checkpoint["count"] == 1, "baseline checkpoint count is not one")

        coordinator = RehearsalCoordinator(
            isolated_declaration,
            source=successor_source,
            model_proof_dir=model_proof_dir,
        )
        ready_state = coordinator.get_state()
        baseline_audit_events = int(ready_state["audit"]["events"])
        _require(ready_state["status"] == "READY_TO_EXECUTE", ready_state["status"])
        emit("executing both frozen arms at the synthetic next open")
        executed_state = coordinator.execute_ready_pair()
        _require(
            executed_state["status"] == "READY_FOR_CLOSE_SIGNAL",
            executed_state["status"],
        )
        _require(
            executed_state["pairedEvidence"]["observationSessions"] == 1,
            "paired observation count is not one after execution",
        )
        _require(
            executed_state["pairedEvidence"]["datesAligned"] is True,
            "paired NAV dates diverged",
        )

        synthetic_close = datetime.combine(
            datetime.fromisoformat(selected_trade_date).date(),
            time(15, 30),
            tzinfo=SHANGHAI_TZ,
        )
        emit("generating the next paired close signal from the real fitted models")
        signaled_state = coordinator.generate_latest_signal(now=synthetic_close)
        _require(
            all(
                signaled_state["arms"][arm]["pendingSignal"]["signalDate"]
                == selected_trade_date
                for arm in ARMS
            ),
            "next paired signal was not frozen on the synthetic close",
        )
        _require(
            signaled_state["audit"]["events"] == baseline_audit_events + 2,
            "execution and next signal did not append exactly two audit events",
        )

        emit("creating and validating the second evidence checkpoint")
        final_checkpoint = ensure_evidence_checkpoint(
            isolated_declaration,
            now=synthetic_close,
            coordinator_factory=successor_factory,
        )
        checkpoint_chain = validate_evidence_checkpoint_chain(isolated_declaration)
        _require(final_checkpoint["count"] == 2, "final checkpoint count is not two")
        _require(checkpoint_chain["count"] == 2, "checkpoint chain count is not two")

        database = Path(
            json.loads(isolated_declaration.read_text(encoding="utf-8"))[
                "databasePath"
            ]
        )
        counts = _table_counts(database)
        for arm in ARMS:
            _require(counts[arm]["orders"] > 0, f"{arm} produced no orders")
            _require(counts[arm]["fills"] > 0, f"{arm} produced no fills")
            _require(counts[arm]["positions"] > 0, f"{arm} produced no positions")
            _require(counts[arm]["nav_history"] == 1, f"{arm} NAV count is not one")
            _require(counts[arm]["runs"] == 1, f"{arm} execution run count is not one")

        emit("verifying that production state is byte/logically unchanged")
        after = production_integrity_signature(
            declaration_path, feature_fields=raw_fields
        )
        production_unchanged = before["signatureSha256"] == after["signatureSha256"]
        _require(production_unchanged, "production integrity signature changed")

        completed_at = datetime.now(SHANGHAI_TZ)
        report = {
            "version": REHEARSAL_VERSION,
            "kind": REHEARSAL_KIND,
            "status": "PASS",
            "synthetic": True,
            "rehearsalOnly": True,
            "evidenceEligible": False,
            "profitabilityConclusion": "NOT_EVALUATED",
            "startedAt": started_at.isoformat(),
            "completedAt": completed_at.isoformat(),
            "durationSeconds": (completed_at - started_at).total_seconds(),
            "runId": declaration["runId"],
            "workspace": {
                "path": str(workspace.resolve()),
                "retained": keep_workspace,
            },
            "production": {
                "unchanged": production_unchanged,
                "beforeSignatureSha256": before["signatureSha256"],
                "afterSignatureSha256": after["signatureSha256"],
                "providerFingerprint": before["provider"]["fingerprint"],
                "providerFeatureTreeSha256": before["providerFeatureTree"]["sha256"],
                "ledgerLogicalSha256": before["database"]["logical"]["logicalSha256"],
                "auditHead": before["database"]["audit"]["head"],
                "evidenceCheckpointHead": (
                    (before["evidenceCheckpoint"].get("latest") or {}).get("sha256")
                ),
            },
            "frozenTargetReplay": replay_contracts,
            "isolatedContract": isolated_contract,
            "snapshotCopies": snapshot_contracts,
            "syntheticProvider": {
                **synthetic,
                "clone": clone,
                "fingerprint": proof["successorFingerprint"],
                "lineage": {
                    "status": proof["status"],
                    "historyUnchanged": proof["historyUnchanged"],
                    "checkedFeatureFiles": proof["checkedFeatureFiles"],
                    "checkedNonNullFeatureValues": proof[
                        "checkedNonNullFeatureValues"
                    ],
                    "proofPath": proof_artifact["proofPath"],
                    "proofSha256": proof_artifact["proofSha256"],
                    "modelProofPath": model_proof_artifact["proofPath"],
                    "modelProofSha256": model_proof_artifact["proofSha256"],
                },
            },
            "states": {
                "productionBefore": _state_summary(live_state),
                "ready": _state_summary(ready_state),
                "afterExecution": _state_summary(executed_state),
                "afterNextSignal": _state_summary(signaled_state),
            },
            "ledgerTableCounts": counts,
            "evidenceCheckpoints": {
                "baseline": baseline_checkpoint,
                "final": final_checkpoint,
                "validatedChain": checkpoint_chain,
            },
            "acceptance": {
                "frozenTargetsReplayedExactly": True,
                "modelContractsPreserved": all(
                    item["byteIdentical"]
                    for item in snapshot_contracts.values()
                ),
                "providerAppendedExactlyOneSession": True,
                "providerHistoryUnchanged": True,
                "pairedExecutionCompleted": True,
                "pairedNavDatesAligned": True,
                "nextSignalsFrozen": True,
                "twoCheckpointChainValid": True,
                "productionUnchanged": production_unchanged,
            },
            "limitations": [
                "Synthetic bars test engineering behavior only and are not market observations.",
                "Synthetic returns, drawdown, NAV, orders, and fills are not profitability evidence.",
                "The rehearsal does not authorize runtime Provider switching or formal-paper promotion.",
            ],
        }
        return report
    finally:
        if temporary is not None:
            temporary.cleanup()
