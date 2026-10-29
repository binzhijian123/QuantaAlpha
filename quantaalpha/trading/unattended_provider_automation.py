"""Maintain official Providers without backfilling an invalidated shadow ledger."""

from __future__ import annotations

import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from quantaalpha.trading.market_data import discover_latest_provider
from quantaalpha.trading.research_shadow import (
    DEFAULT_DECLARATION,
    ResearchShadowCoordinator,
)
from quantaalpha.trading import research_shadow_automation as frozen_automation
from quantaalpha.trading.research_shadow_evidence import ensure_evidence_checkpoint
from quantaalpha.utils.investment_data_release import (
    download_official_release,
    install_release_provider,
    load_release_manifest,
    query_official_release_manifest,
    switch_runtime_provider,
)
from quantaalpha.utils.provider_lineage import verify_append_only_provider
from quantaalpha.utils.qlib_provider import calendar_bounds


REVIEW_STATUSES = {"MISSED_EXECUTION", "INVALID_PAIR", "RISK_STOPPED"}
METADATA_QUERY_BATCHES = 2
METADATA_QUERY_BATCH_DELAY_SECONDS = 15.0
METADATA_QUERY_ERROR_PREFIX = "failed to query official release metadata after "
FROZEN_LINEAGE_UNAVAILABLE_ERROR = (
    "Current Provider differs from the frozen Provider and lacks an "
    "append-only lineage proof"
)
FROZEN_LINEAGE_UNAVAILABLE_REASON = (
    "FROZEN_LINEAGE_UNAVAILABLE_NO_LEDGER_BACKFILL"
)
PROVIDER_HISTORY_DRIFT_PREFIXES = {
    "CALENDAR_HISTORY_CHANGED": "Successor calendar is not an exact parent prefix",
    "MEMBERSHIP_HISTORY_CHANGED": "Provider market membership history changed",
    "FEATURE_HISTORY_CHANGED": "Provider feature history changed",
}


def _provider_history_drift(exc: Exception) -> tuple[str, str] | None:
    detail = str(exc)
    for kind, prefix in PROVIDER_HISTORY_DRIFT_PREFIXES.items():
        if detail.startswith(prefix):
            return kind, detail[:1000]
    return None


def _provider_history_was_invalidated(status: dict[str, Any]) -> bool:
    result = status.get("result")
    return bool(
        isinstance(result, dict)
        and result.get("providerHistoryInvalidated") is True
    )


def _terminal_shadow_lacks_frozen_lineage(
    state: dict[str, Any], error: ValueError
) -> bool:
    """Allow Provider-only maintenance for a terminal, non-promotable shadow."""
    return (
        str(state.get("status") or "") in REVIEW_STATUSES | {"RETIRED"}
        and str(error) == FROZEN_LINEAGE_UNAVAILABLE_ERROR
    )


def _query_official_manifest_with_bounded_retry(
    release_tag: str | None,
) -> dict[str, Any]:
    """Retry only the read-only GitHub metadata phase, never install side effects."""
    for batch in range(METADATA_QUERY_BATCHES):
        try:
            return query_official_release_manifest(release_tag)
        except RuntimeError as exc:
            retryable = str(exc).startswith(METADATA_QUERY_ERROR_PREFIX)
            if not retryable or batch == METADATA_QUERY_BATCHES - 1:
                raise
            time.sleep(METADATA_QUERY_BATCH_DELAY_SECONDS)
    raise AssertionError("official metadata retry loop exhausted without a result")


def _advance_official_provider_unlocked(
    declaration_path: Path,
    *,
    release_tag: str | None = None,
    release_root: Path | None = None,
    active_state: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Advance verified data even when doing so invalidates the old shadow run."""
    declaration_path = declaration_path.expanduser().resolve()
    declaration = frozen_automation._load_declaration(declaration_path)
    parent = discover_latest_provider().expanduser().resolve()
    parent_bounds = calendar_bounds(parent)
    if parent_bounds is None:
        raise RuntimeError(f"Active Provider has no daily calendar: {parent}")

    releases = release_root or (
        frozen_automation.PROJECT_ROOT / "data" / "market" / "a_share" / "releases"
    )
    official_manifest = _query_official_manifest_with_bounded_retry(release_tag)
    official_tag = str(official_manifest["release_tag"])
    official_target_date = str(official_manifest["target_trade_date"])
    freshness = frozen_automation.assess_official_data_freshness(
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
            "maintenanceMode": "UNATTENDED_PROVIDER",
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
        frozen_automation.PROJECT_ROOT
        / "data"
        / "market"
        / "a_share"
        / "qlib"
        / f"investment_data_{release_slug}_cn"
    ).resolve()
    install_result = install_release_provider(source_dir, successor)
    history_already_invalidated = bool(
        (active_state or {}).get("providerHistoryInvalidated")
    )
    try:
        if history_already_invalidated:
            proof = verify_append_only_provider(
                parent,
                successor,
                market="csi300",
                benchmark="000300.SH",
            )
            proof_path = None
        else:
            proof, proof_path = (
                frozen_automation.build_research_shadow_lineage_proof(
                    declaration_path,
                    parent,
                    successor,
                )
            )
    except ValueError as exc:
        drift = _provider_history_drift(exc)
        if drift is None:
            raise
        successor_bounds = calendar_bounds(successor)
        if (
            successor_bounds is None
            or successor_bounds[1] != target_date
            or successor_bounds[2] <= parent_bounds[2]
        ):
            raise RuntimeError(
                "history-drift Provider does not extend the active calendar"
            ) from exc
        appended_sessions = successor_bounds[2] - parent_bounds[2]
        runtime = switch_runtime_provider(successor)
        return {
            "status": "ADVANCED_WITH_HISTORY_DRIFT",
            "runId": declaration["runId"],
            "parentLatestDate": parent_bounds[1],
            "successorLatestDate": successor_bounds[1],
            "officialRelease": official_tag,
            "officialTargetDate": target_date,
            "officialDataFreshness": (
                frozen_automation.assess_official_data_freshness(
                    successor,
                    successor_bounds[1],
                    now=now,
                )
            ),
            "appendedSessions": appended_sessions,
            "runtimeSwitched": True,
            "installStatus": install_result["status"],
            "maintenanceMode": "UNATTENDED_PROVIDER",
            "recoveryMode": "HISTORY_DRIFT_NO_LEDGER_BACKFILL",
            "ledgerBackfillAllowed": False,
            "providerHistoryInvalidated": True,
            "historyDrift": {"kind": drift[0], "detail": drift[1]},
            "runtime": runtime,
        }

    appended_sessions = int(proof["appendedSessions"])
    if appended_sessions <= 0:
        raise ValueError("Official Provider did not append a trading session")

    state = active_state or ResearchShadowCoordinator(declaration_path).get_state()
    pending = (state.get("arms") or {}).get("C1", {}).get("pendingSignal")
    if pending:
        signal_date = str(pending["signalDate"])
        if (
            signal_date == declaration.get("initialSignalDate")
            and proof["firstAppendedDate"] != declaration.get("firstTradeDate")
        ):
            raise ValueError(
                "First successor session differs from the frozen first trade date"
            )

    proof_artifact = (
        {
            "proofStatus": "VERIFIED_FROM_INVALIDATED_RUNTIME_BASELINE",
            "parentFingerprint": proof["parentFingerprint"],
            "successorFingerprint": proof["successorFingerprint"],
        }
        if proof_path is None
        else frozen_automation._reuse_or_write_proof(
            declaration,
            proof,
            proof_path,
            successor,
        )
    )
    runtime = switch_runtime_provider(successor)
    multi_session = appended_sessions > 1
    return {
        "status": "ADVANCED",
        "runId": declaration["runId"],
        "parentLatestDate": parent_bounds[1],
        "successorLatestDate": proof["successorLatestDate"],
        "officialRelease": official_tag,
        "officialTargetDate": target_date,
        "officialDataFreshness": frozen_automation.assess_official_data_freshness(
            successor,
            str(proof["successorLatestDate"]),
            now=now,
        ),
        "appendedSessions": appended_sessions,
        "runtimeSwitched": True,
        "installStatus": install_result["status"],
        "maintenanceMode": "UNATTENDED_PROVIDER",
        "recoveryMode": (
            "MULTI_SESSION_NO_LEDGER_BACKFILL"
            if multi_session
            else "SINGLE_SESSION_CAUSAL"
        ),
        "ledgerBackfillAllowed": False,
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
    declaration = frozen_automation._load_declaration(declaration_path)
    with frozen_automation._exclusive_operation_lock(declaration):
        return _advance_official_provider_unlocked(
            declaration_path,
            release_tag=release_tag,
            release_root=release_root,
            active_state=active_state,
            now=now,
        )


def _review_result(
    state: dict[str, Any],
    provider_update: dict[str, Any],
    *,
    reason: str,
    actions: list[str] | None = None,
) -> dict[str, Any]:
    shadow_status = str(state.get("status") or "UNKNOWN")
    return {
        "status": "RETIRED" if shadow_status == "RETIRED" else "REVIEW_REQUIRED",
        "actions": list(actions or []),
        "providerUpdate": provider_update,
        "providerRecoveryReason": reason,
        "shadowStatus": shadow_status,
        "nextAction": state.get("nextAction"),
        "marketData": state.get("marketData"),
        "audit": state.get("audit"),
    }


def run_cycle_and_record(
    declaration_path: Path = DEFAULT_DECLARATION,
    *,
    poll_seconds: int,
    now: datetime | None = None,
    coordinator_factory: Callable[[Path], ResearchShadowCoordinator] = (
        ResearchShadowCoordinator
    ),
) -> dict[str, Any]:
    """Finish currently causal actions, then update data without historical backfill."""
    checked_at = frozen_automation._shanghai_now(now)
    declaration_path = declaration_path.expanduser().resolve()
    path = frozen_automation.automation_status_path(declaration_path)
    previous = frozen_automation.read_automation_status(declaration_path) or {}
    declaration = frozen_automation._load_declaration(declaration_path)
    database = frozen_automation.resolve_project_path(declaration["databasePath"])
    try:
        history_was_invalidated = _provider_history_was_invalidated(previous)
        checkpoint_before_update = previous.get("evidenceCheckpoint")
        checkpoint_lineage_error: ValueError | None = None
        if database.is_file() and not history_was_invalidated:
            try:
                checkpoint_before_update = ensure_evidence_checkpoint(
                    declaration_path, now=checked_at
                )
            except ValueError as error:
                if str(error) != FROZEN_LINEAGE_UNAVAILABLE_ERROR:
                    raise
                checkpoint_lineage_error = error

        actions: list[str] = []
        if history_was_invalidated:
            previous_result = previous.get("result") or {}
            state = {
                "status": str(previous_result.get("shadowStatus") or "REVIEW_REQUIRED"),
                "nextAction": "START_NEW_DECLARED_RUN",
                "marketData": previous_result.get("marketData"),
                "audit": previous_result.get("audit"),
                "arms": {},
                "providerHistoryInvalidated": True,
                "providerHistoryInvalidationReason": previous_result.get(
                    "providerRecoveryReason"
                ),
            }
            blocked_before_update = True
        else:
            coordinator = coordinator_factory(declaration_path)
            state = coordinator.get_state()
            state = frozen_automation._settle_ready_actions(
                coordinator,
                state,
                actions,
                checked_at,
            )
            if checkpoint_lineage_error is not None:
                if not _terminal_shadow_lacks_frozen_lineage(
                    state, checkpoint_lineage_error
                ):
                    raise checkpoint_lineage_error
                state = {
                    **state,
                    "providerHistoryInvalidated": True,
                    "providerHistoryInvalidationReason": (
                        FROZEN_LINEAGE_UNAVAILABLE_REASON
                    ),
                }
            blocked_before_update = (
                str(state.get("status") or "")
                in REVIEW_STATUSES | {"RETIRED"}
            )
            if actions and database.is_file():
                checkpoint_before_update = ensure_evidence_checkpoint(
                    declaration_path, now=checked_at
                )
        try:
            provider_update = advance_official_provider(
                declaration_path,
                active_state=state,
                now=checked_at,
            )
        except ValueError as error:
            if not _terminal_shadow_lacks_frozen_lineage(state, error):
                raise
            state = {
                **state,
                "providerHistoryInvalidated": True,
                "providerHistoryInvalidationReason": (
                    FROZEN_LINEAGE_UNAVAILABLE_REASON
                ),
            }
            provider_update = advance_official_provider(
                declaration_path,
                active_state=state,
                now=checked_at,
            )
        appended_sessions = int(provider_update.get("appendedSessions") or 0)
        history_invalidated = (
            history_was_invalidated
            or bool(state.get("providerHistoryInvalidated"))
            or bool(provider_update.get("providerHistoryInvalidated"))
        )
        if provider_update.get("status") == "ADVANCED" and not history_invalidated:
            coordinator = coordinator_factory(declaration_path)
            state = coordinator.get_state()
            if not blocked_before_update and appended_sessions <= 1:
                state = frozen_automation._settle_ready_actions(
                    coordinator,
                    state,
                    actions,
                    checked_at,
                )

        shadow_status = str(state.get("status") or "")
        if history_invalidated:
            result = _review_result(
                state,
                provider_update,
                reason=str(
                    state.get("providerHistoryInvalidationReason")
                    or "PROVIDER_HISTORY_DRIFT_NO_LEDGER_BACKFILL"
                ),
                actions=actions,
            )
            result["providerHistoryInvalidated"] = True
        elif (
            shadow_status in REVIEW_STATUSES | {"RETIRED"}
            or appended_sessions > 1
        ):
            result = _review_result(
                state,
                provider_update,
                reason=(
                    "MULTI_SESSION_PROVIDER_RECOVERY_NO_LEDGER_BACKFILL"
                    if appended_sessions > 1
                    else "SHADOW_ALREADY_REQUIRES_REVIEW"
                ),
                actions=actions,
            )
        else:
            result = {
                "status": (
                    "ADVANCED_AND_PROCESSED"
                    if provider_update.get("status") == "ADVANCED" and actions
                    else str(provider_update.get("status") or "UNKNOWN")
                ),
                "actions": actions,
                "providerUpdate": provider_update,
                "shadowStatus": state.get("status"),
                "nextAction": state.get("nextAction"),
                "marketData": state.get("marketData"),
                "audit": state.get("audit"),
            }

        checkpoint = checkpoint_before_update
        if database.is_file() and not history_invalidated:
            checkpoint = ensure_evidence_checkpoint(
                declaration_path, now=checked_at
            )
        payload = {
            "version": 1,
            "runId": declaration["runId"],
            "enabled": True,
            "status": result["status"],
            "lastCheckedAt": checked_at.isoformat(),
            "lastSuccessAt": checked_at.isoformat(),
            "nextCheckAt": (
                checked_at + timedelta(seconds=poll_seconds)
            ).isoformat(),
            "pollSeconds": poll_seconds,
            "consecutiveErrors": 0,
            "lastError": None,
            "result": result,
            "evidenceCheckpoint": checkpoint,
        }
        frozen_automation._write_status(path, payload)
        return payload
    except Exception as exc:
        payload = {
            "version": 1,
            "runId": declaration["runId"],
            "enabled": True,
            "status": "ERROR",
            "lastCheckedAt": checked_at.isoformat(),
            "lastSuccessAt": previous.get("lastSuccessAt"),
            "nextCheckAt": (
                checked_at + timedelta(seconds=poll_seconds)
            ).isoformat(),
            "pollSeconds": poll_seconds,
            "consecutiveErrors": int(previous.get("consecutiveErrors") or 0) + 1,
            "lastError": str(exc),
            "result": None,
            "evidenceCheckpoint": previous.get("evidenceCheckpoint"),
        }
        frozen_automation._write_status(path, payload)
        raise
