"""Safe official-data advancement and daily automation for research shadow."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
from contextlib import contextmanager
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from quantaalpha.trading.market_data import discover_latest_provider
from quantaalpha.trading.research_shadow import (
    DEFAULT_DECLARATION,
    ResearchShadowCoordinator,
)
from quantaalpha.trading.research_shadow_evidence import ensure_evidence_checkpoint
from quantaalpha.utils.investment_data_release import (
    download_official_release,
    install_release_provider,
    load_release_manifest,
    query_official_release_manifest,
    switch_runtime_provider,
)
from quantaalpha.utils.provider_lineage import (
    validate_lineage_chain,
    verify_append_only_provider,
    write_lineage_proof,
)
from quantaalpha.utils.qlib_provider import (
    calendar_bounds,
    provider_contract_fingerprint,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
AUTOMATION_STATUS_NAME = "automation_status.json"
OFFICIAL_RELEASE_GRACE_MINUTES_AFTER_MIDNIGHT = 30


def resolve_project_path(value: str | Path) -> Path:
    path = Path(value).expanduser()
    return path.resolve() if path.is_absolute() else (PROJECT_ROOT / path).resolve()


def _shanghai_now(now: datetime | None = None) -> datetime:
    value = now or datetime.now(SHANGHAI_TZ)
    if value.tzinfo is None:
        value = value.replace(tzinfo=SHANGHAI_TZ)
    return value.astimezone(SHANGHAI_TZ)


def assess_official_data_freshness(
    provider: Path,
    active_latest_date: str,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Assess release timeliness from the Provider's future exchange calendar."""
    checked_at = _shanghai_now(now)
    calendar_path = provider.expanduser().resolve() / "calendars" / "day_future.txt"
    base = {
        "activeLatestDate": active_latest_date,
        "calendarSource": str(calendar_path),
        "checkedAt": checked_at.isoformat(),
        "graceMinutesAfterMidnight": OFFICIAL_RELEASE_GRACE_MINUTES_AFTER_MIDNIGHT,
    }
    try:
        active_date = date.fromisoformat(active_latest_date)
        values = [
            line.strip()
            for line in calendar_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        sessions = [date.fromisoformat(value) for value in values]
        if sessions != sorted(set(sessions)):
            raise ValueError("future calendar must be sorted and unique")
    except (OSError, ValueError) as exc:
        return {
            **base,
            "status": "CALENDAR_UNAVAILABLE",
            "monitoringAvailable": False,
            "expectedNextSession": None,
            "releaseDueAt": None,
            "overdueSessions": 0,
            "reason": str(exc),
        }

    pending = [session for session in sessions if session > active_date]
    if not pending:
        return {
            **base,
            "status": "CALENDAR_EXHAUSTED",
            "monitoringAvailable": False,
            "expectedNextSession": None,
            "releaseDueAt": None,
            "overdueSessions": 0,
            "reason": "future calendar has no session after the active Provider",
        }

    def release_due_at(session: date) -> datetime:
        return datetime.combine(
            session + timedelta(days=1),
            time(
                hour=0,
                minute=OFFICIAL_RELEASE_GRACE_MINUTES_AFTER_MIDNIGHT,
            ),
            tzinfo=SHANGHAI_TZ,
        )

    expected = pending[0]
    due_at = release_due_at(expected)
    overdue_sessions = sum(release_due_at(session) <= checked_at for session in pending)
    return {
        **base,
        "status": "OVERDUE" if overdue_sessions else "ON_TIME",
        "monitoringAvailable": True,
        "expectedNextSession": expected.isoformat(),
        "releaseDueAt": due_at.isoformat(),
        "overdueSessions": overdue_sessions,
        "reason": None,
    }


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load_declaration(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("usageRealm") != "RESEARCH_SHADOW":
        raise ValueError("Automation requires a RESEARCH_SHADOW declaration")
    if payload.get("formalPaperEligible") is not False:
        raise ValueError("Research-shadow automation cannot be formal-paper eligible")
    return payload


@contextmanager
def _exclusive_operation_lock(declaration: dict[str, Any]):
    database = resolve_project_path(declaration["databasePath"])
    lock_path = database.parent / ".operation.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="ascii") as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def build_research_shadow_lineage_proof(
    declaration_path: Path,
    parent_provider: Path,
    successor_provider: Path,
) -> tuple[dict[str, Any], Path]:
    declaration_path = declaration_path.expanduser().resolve()
    declaration = _load_declaration(declaration_path)
    declaration_sha = _sha256(declaration_path)
    root_fingerprint = str(declaration["providerFingerprint"])
    run_id = str(declaration["runId"])
    proof_dir = resolve_project_path(declaration["providerLineageProofDir"])
    parent = parent_provider.expanduser().resolve()
    successor = successor_provider.expanduser().resolve()
    market = "csi300"
    benchmark = str((declaration.get("execution") or {}).get("benchmark") or "000300.SH")

    parent_fingerprint = provider_contract_fingerprint(parent, market)["fingerprint"]
    if parent_fingerprint == root_fingerprint:
        frozen_path = resolve_project_path(declaration["providerPath"])
        if parent != frozen_path:
            raise ValueError("Frozen root Provider fingerprint came from an unexpected path")
    else:
        validate_lineage_chain(
            proof_dir,
            root_fingerprint=root_fingerprint,
            successor_fingerprint=parent_fingerprint,
            run_id=run_id,
            declaration_sha256=declaration_sha,
            successor_provider_path=parent,
            verify_active_feature_tree=True,
        )

    proof = verify_append_only_provider(
        parent,
        successor,
        market=market,
        benchmark=benchmark,
    )
    proof.update(
        {
            "runId": run_id,
            "declarationPath": str(declaration_path),
            "declarationSha256": declaration_sha,
            "rootFingerprint": root_fingerprint,
            "baseFingerprint": root_fingerprint,
        }
    )
    return proof, proof_dir / f"{proof['successorFingerprint']}.json"


def _reuse_or_write_proof(
    declaration: dict[str, Any],
    proof: dict[str, Any],
    proof_path: Path,
    successor: Path,
) -> dict[str, str]:
    if proof_path.exists():
        validate_lineage_chain(
            proof_path.parent,
            root_fingerprint=str(declaration["providerFingerprint"]),
            successor_fingerprint=str(proof["successorFingerprint"]),
            run_id=str(declaration["runId"]),
            declaration_sha256=str(proof["declarationSha256"]),
            successor_provider_path=successor,
            verify_active_feature_tree=True,
        )
        sidecar = proof_path.with_suffix(proof_path.suffix + ".sha256")
        return {
            "proofPath": str(proof_path),
            "proofSha256": sidecar.read_text(encoding="ascii").strip(),
            "proofStatus": "REUSED",
        }
    return {
        **write_lineage_proof(proof_path, proof),
        "proofStatus": "CREATED",
    }


def _advance_official_provider_unlocked(
    declaration_path: Path = DEFAULT_DECLARATION,
    *,
    release_tag: str | None = None,
    release_root: Path | None = None,
    active_state: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Advance exactly one official session and switch only after all checks pass."""
    declaration_path = declaration_path.expanduser().resolve()
    declaration = _load_declaration(declaration_path)
    parent = discover_latest_provider().expanduser().resolve()
    parent_bounds = calendar_bounds(parent)
    if parent_bounds is None:
        raise RuntimeError(f"Active Provider has no daily calendar: {parent}")

    releases = release_root or PROJECT_ROOT / "data" / "market" / "a_share" / "releases"
    official_manifest = query_official_release_manifest(release_tag)
    official_tag = str(official_manifest["release_tag"])
    official_target_date = str(official_manifest["target_trade_date"])
    freshness = assess_official_data_freshness(
        parent,
        parent_bounds[1],
        now=now,
    )
    if official_target_date <= parent_bounds[1]:
        return {
            "status": "NO_NEW_SESSION",
            "activeProvider": str(parent),
            "activeLatestDate": parent_bounds[1],
            "officialRelease": official_tag,
            "officialTargetDate": official_target_date,
            "officialDataFreshness": freshness,
            "runtimeSwitched": False,
        }

    source_dir = download_official_release(releases, official_tag)
    manifest = load_release_manifest(
        source_dir / "qlib_bin.manifest.json",
        expected_tag=official_tag,
    )
    target_date = str(manifest["target_trade_date"])
    if target_date != official_target_date:
        raise RuntimeError("official release manifest changed during download")
    if target_date <= parent_bounds[1]:
        raise RuntimeError("downloaded release is not newer than the active Provider")

    release_slug = str(manifest["release_tag"]).replace("-", "")
    successor = (
        PROJECT_ROOT
        / "data"
        / "market"
        / "a_share"
        / "qlib"
        / f"investment_data_{release_slug}_cn"
    ).resolve()
    install_result = install_release_provider(source_dir, successor)
    proof, proof_path = build_research_shadow_lineage_proof(
        declaration_path, parent, successor
    )
    if int(proof["appendedSessions"]) != 1:
        raise ValueError(
            "Official Provider would skip more than one session; automatic historical "
            "catch-up is forbidden"
        )

    state = active_state or ResearchShadowCoordinator(declaration_path).get_state()
    pending = (state.get("arms") or {}).get("C1", {}).get("pendingSignal")
    if pending:
        signal_date = str(pending["signalDate"])
        if signal_date == declaration.get("initialSignalDate") and proof[
            "firstAppendedDate"
        ] != declaration.get("firstTradeDate"):
            raise ValueError("First successor session differs from the frozen first trade date")

    proof_artifact = _reuse_or_write_proof(
        declaration, proof, proof_path, successor
    )
    runtime = switch_runtime_provider(successor)
    return {
        "status": "ADVANCED",
        "runId": declaration["runId"],
        "parentLatestDate": parent_bounds[1],
        "successorLatestDate": proof["successorLatestDate"],
        "officialRelease": official_tag,
        "officialTargetDate": target_date,
        "officialDataFreshness": assess_official_data_freshness(
            successor,
            str(proof["successorLatestDate"]),
            now=now,
        ),
        "appendedSessions": proof["appendedSessions"],
        "runtimeSwitched": True,
        "installStatus": install_result["status"],
        **proof_artifact,
        "runtime": runtime,
    }


def advance_official_provider(
    declaration_path: Path = DEFAULT_DECLARATION,
    *,
    release_tag: str | None = None,
    release_root: Path | None = None,
    active_state: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    declaration_path = declaration_path.expanduser().resolve()
    declaration = _load_declaration(declaration_path)
    with _exclusive_operation_lock(declaration):
        return _advance_official_provider_unlocked(
            declaration_path,
            release_tag=release_tag,
            release_root=release_root,
            active_state=active_state,
            now=now,
        )


def _settle_ready_actions(
    coordinator: ResearchShadowCoordinator,
    state: dict[str, Any],
    actions: list[str],
    now: datetime | None,
) -> dict[str, Any]:
    for _ in range(3):
        next_action = state.get("nextAction")
        if next_action == "EXECUTE_NEXT_OPEN":
            state = coordinator.execute_ready_pair()
            actions.append("PAIRED_NEXT_OPEN_EXECUTED")
            continue
        if next_action == "GENERATE_LATEST_CLOSE_SIGNAL":
            state = coordinator.generate_latest_signal(now=now)
            actions.append("PAIRED_CLOSE_SIGNAL_FROZEN")
            continue
        break
    return state


def run_daily_cycle(
    declaration_path: Path = DEFAULT_DECLARATION,
    *,
    now: datetime | None = None,
    coordinator_factory: Callable[[Path], ResearchShadowCoordinator] = ResearchShadowCoordinator,
    advance_provider: Callable[..., dict[str, Any]] = advance_official_provider,
) -> dict[str, Any]:
    """Run one fail-closed update/execute/freeze cycle without accepting dates."""
    declaration_path = declaration_path.expanduser().resolve()
    coordinator = coordinator_factory(declaration_path)
    state = coordinator.get_state()
    actions: list[str] = []
    state = _settle_ready_actions(coordinator, state, actions, now)
    if state.get("status") == "RETIRED":
        return {
            "status": "RETIRED",
            "actions": actions,
            "shadowStatus": state["status"],
            "nextAction": state["nextAction"],
            "marketData": state["marketData"],
            "audit": state["audit"],
        }
    if state.get("status") in {"MISSED_EXECUTION", "INVALID_PAIR"}:
        return {
            "status": "REVIEW_REQUIRED",
            "actions": actions,
            "shadowStatus": state["status"],
            "nextAction": state["nextAction"],
            "marketData": state["marketData"],
        }

    update = advance_provider(
        declaration_path,
        active_state=state,
        now=now,
    )
    if update.get("status") == "ADVANCED":
        coordinator = coordinator_factory(declaration_path)
        state = coordinator.get_state()
        state = _settle_ready_actions(coordinator, state, actions, now)
    if state.get("status") in {
        "MISSED_EXECUTION",
        "INVALID_PAIR",
        "RISK_STOPPED",
    }:
        return {
            "status": "REVIEW_REQUIRED",
            "actions": actions,
            "providerUpdate": update,
            "shadowStatus": state["status"],
            "nextAction": state["nextAction"],
            "marketData": state["marketData"],
            "audit": state["audit"],
        }
    return {
        "status": (
            "ADVANCED_AND_PROCESSED"
            if update.get("status") == "ADVANCED" and actions
            else str(update.get("status") or "UNKNOWN")
        ),
        "actions": actions,
        "providerUpdate": update,
        "shadowStatus": state["status"],
        "nextAction": state["nextAction"],
        "marketData": state["marketData"],
        "audit": state["audit"],
    }


def automation_status_path(
    declaration_path: Path = DEFAULT_DECLARATION,
) -> Path:
    declaration = _load_declaration(declaration_path.expanduser().resolve())
    return resolve_project_path(declaration["databasePath"]).parent / AUTOMATION_STATUS_NAME


def read_automation_status(
    declaration_path: Path = DEFAULT_DECLARATION,
) -> dict[str, Any] | None:
    path = automation_status_path(declaration_path)
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _write_status(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def run_cycle_and_record(
    declaration_path: Path = DEFAULT_DECLARATION,
    *,
    poll_seconds: int,
    now: datetime | None = None,
) -> dict[str, Any]:
    checked_at = _shanghai_now(now)
    path = automation_status_path(declaration_path)
    previous = read_automation_status(declaration_path) or {}
    declaration = _load_declaration(declaration_path.expanduser().resolve())
    database = resolve_project_path(declaration["databasePath"])
    try:
        if database.is_file():
            ensure_evidence_checkpoint(declaration_path, now=checked_at)
        result = run_daily_cycle(declaration_path, now=checked_at)
        checkpoint = (
            ensure_evidence_checkpoint(declaration_path, now=checked_at)
            if database.is_file()
            else None
        )
        payload = {
            "version": 1,
            "runId": declaration["runId"],
            "enabled": True,
            "status": result["status"],
            "lastCheckedAt": checked_at.isoformat(),
            "lastSuccessAt": checked_at.isoformat(),
            "nextCheckAt": (checked_at + timedelta(seconds=poll_seconds)).isoformat(),
            "pollSeconds": poll_seconds,
            "consecutiveErrors": 0,
            "lastError": None,
            "result": result,
            "evidenceCheckpoint": checkpoint,
        }
        _write_status(path, payload)
        return payload
    except Exception as exc:
        payload = {
            "version": 1,
            "runId": declaration["runId"],
            "enabled": True,
            "status": "ERROR",
            "lastCheckedAt": checked_at.isoformat(),
            "lastSuccessAt": previous.get("lastSuccessAt"),
            "nextCheckAt": (checked_at + timedelta(seconds=poll_seconds)).isoformat(),
            "pollSeconds": poll_seconds,
            "consecutiveErrors": int(previous.get("consecutiveErrors") or 0) + 1,
            "lastError": str(exc),
            "result": None,
            "evidenceCheckpoint": previous.get("evidenceCheckpoint"),
        }
        _write_status(path, payload)
        raise
