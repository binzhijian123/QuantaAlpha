#!/usr/bin/env python3
"""Fail-closed operational preflight for the active intraday shadow run."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sys
import urllib.request
from datetime import date, datetime, time
from pathlib import Path
from typing import Any, Mapping
from zoneinfo import ZoneInfo

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.manage_research_shadow_supervisor import status_payload


BASE_URL = "http://127.0.0.1:8000"
CHINA_TZ = ZoneInfo("Asia/Shanghai")
EXPECTED_WORKERS = (
    "researchShadowWatcher",
    "intradayCapture",
    "intradayShadow",
)
USABLE_CAPTURE_STATES = {"WAITING_MARKET", "WAITING_QUOTES", "CAPTURED", "REUSED"}
USABLE_RUN_STATES = {
    "STAGED",
    "DECISION_FROZEN_AWAITING_EXECUTION",
    "AWAITING_OFFICIAL_DAILY",
    "ACTIVE",
}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _directory_bytes(path: Path) -> int:
    return sum(item.stat().st_size for item in path.rglob("*") if item.is_file())


def inspect_storage(path_text: str | None) -> dict[str, Any]:
    if not path_text:
        return {
            "outputRoot": None,
            "exists": False,
            "writable": False,
            "freeBytes": 0,
            "referenceCaptureBytes": 1,
            "error": "capture output root is not configured",
        }

    output_root = Path(path_text).expanduser().resolve()
    if not output_root.is_dir():
        return {
            "outputRoot": str(output_root),
            "exists": False,
            "writable": False,
            "freeBytes": 0,
            "referenceCaptureBytes": 1,
            "error": "capture output root does not exist",
        }

    try:
        disk = shutil.disk_usage(output_root)
        capture_bytes = max(1, _directory_bytes(output_root))
    except OSError as exc:
        return {
            "outputRoot": str(output_root),
            "exists": True,
            "writable": False,
            "freeBytes": 0,
            "referenceCaptureBytes": 1,
            "error": f"capture storage inspection failed: {exc}",
        }
    return {
        "outputRoot": str(output_root),
        "exists": True,
        "writable": os.access(output_root, os.W_OK),
        "freeBytes": disk.free,
        "referenceCaptureBytes": capture_bytes,
        "error": None,
    }


def _api(path: str) -> dict[str, Any]:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(f"{BASE_URL}{path}", timeout=3.0) as response:
        payload = json.load(response)
    if response.status != 200 or not isinstance(payload, dict):
        raise RuntimeError(f"local API failed: {path}")
    return payload


def verify_declaration_binding(state: Mapping[str, Any]) -> dict[str, Any]:
    declaration_meta = state.get("declaration") or {}
    declaration_path = Path(str(declaration_meta.get("path") or "")).resolve()
    errors: list[str] = []
    if not declaration_path.is_file():
        return {"valid": False, "errors": ["declaration file is missing"]}

    declaration_sha = _sha256(declaration_path)
    if declaration_sha != declaration_meta.get("sha256"):
        errors.append("loaded declaration SHA256 does not match the file")
    sidecar = declaration_path.with_suffix(declaration_path.suffix + ".sha256")
    try:
        sidecar_parts = sidecar.read_text(encoding="ascii").strip().split()
        if sidecar_parts != [declaration_sha, declaration_path.name]:
            errors.append("declaration sidecar does not match the file")
    except OSError:
        errors.append("declaration sidecar is missing or unreadable")

    try:
        declaration = json.loads(declaration_path.read_text(encoding="utf-8"))
        implementation = declaration["implementation"]["files"]
        if not isinstance(declaration, dict) or not isinstance(implementation, list):
            raise TypeError
    except (KeyError, TypeError, ValueError, OSError, json.JSONDecodeError):
        return {
            "valid": False,
            "declarationSha256": declaration_sha,
            "errors": [*errors, "declaration implementation contract is invalid"],
        }

    if declaration.get("runId") != state.get("runId"):
        errors.append("loaded run ID does not match the declaration")
    if declaration.get("quality") != state.get("quality"):
        errors.append("loaded quality contract does not match the declaration")
    if declaration.get("qualityExtension") != state.get("qualityExtension"):
        errors.append("loaded quality extension does not match the declaration")
    if declaration.get("causality") != state.get("causality"):
        errors.append("loaded causality contract does not match the declaration")
    if declaration.get("sourceReliability") != state.get("sourceReliability"):
        errors.append("loaded source reliability contract does not match the declaration")

    for entry in implementation:
        try:
            path = Path(str(entry["path"]))
            expected = str(entry["sha256"])
            if not path.is_file() or _sha256(path) != expected:
                errors.append(f"implementation drift: {path.name}")
        except (KeyError, TypeError, OSError):
            errors.append("implementation contract contains an invalid entry")

    first_eligible = str(state.get("protocol", {}).get("firstEligibleSignalDate") or "")
    try:
        provider_root = Path(str(declaration["providerRoot"]["path"]))
        future_calendar = provider_root / "calendars" / "day_future.txt"
        future_dates = set(future_calendar.read_text(encoding="utf-8").splitlines())
        calendar_contains_first_date = first_eligible in future_dates
    except (KeyError, TypeError, OSError):
        calendar_contains_first_date = False
    if not calendar_contains_first_date:
        errors.append("frozen provider calendar lacks the first eligible date")

    return {
        "valid": not errors,
        "declarationSha256": declaration_sha,
        "boundImplementationFiles": len(implementation),
        "providerCalendarContainsFirstEligibleDate": calendar_contains_first_date,
        "errors": errors,
    }


def inspect_session_continuity(
    state: Mapping[str, Any],
    *,
    now: datetime,
) -> dict[str, Any]:
    """Independently verify that every due frozen-calendar session is represented."""
    errors: list[str] = []
    try:
        if now.tzinfo is None:
            raise ValueError("current time lacks timezone")
        local_now = now.astimezone(CHINA_TZ)
        protocol = state.get("protocol") or {}
        evidence = state.get("pairedEvidence") or {}
        declaration_meta = state.get("declaration") or {}
        if not all(
            isinstance(value, Mapping)
            for value in (protocol, evidence, declaration_meta)
        ):
            raise TypeError("session state contract is invalid")

        first_eligible = date.fromisoformat(
            str(protocol.get("firstEligibleSignalDate") or "")
        )
        freeze_after = time.fromisoformat(str(protocol.get("freezeAfter") or ""))
        freeze_deadline = time.fromisoformat(
            str(protocol.get("freezeDeadline") or "")
        )
        if freeze_after > freeze_deadline:
            raise ValueError("freeze window is invalid")
        causality = state.get("causality")
        two_phase = bool(
            isinstance(causality, Mapping)
            and causality.get("decisionIntentRequired") is True
        )
        decision_after: time | None = None
        decision_deadline: time | None = None
        if two_phase:
            decision_after = time.fromisoformat(
                str(protocol.get("decisionFreezeAfter") or "")
            )
            decision_deadline = time.fromisoformat(
                str(protocol.get("decisionFreezeDeadline") or "")
            )
            if not (
                decision_after
                <= decision_deadline
                < time.fromisoformat(str(protocol.get("executionSlot") or ""))
                < freeze_after
                <= freeze_deadline
            ):
                raise ValueError("two-phase causal window is invalid")

        declaration_path = Path(
            str(declaration_meta.get("path") or "")
        ).resolve()
        declaration = json.loads(declaration_path.read_text(encoding="utf-8"))
        provider_root = Path(str(declaration["providerRoot"]["path"]))
        calendar_path = provider_root / "calendars" / "day_future.txt"
        trading_dates = sorted(
            {
                date.fromisoformat(value.strip())
                for value in calendar_path.read_text(encoding="utf-8").splitlines()
                if value.strip()
            }
        )
        if first_eligible not in trading_dates:
            raise ValueError("first eligible date is absent from frozen calendar")

        frozen_sessions = int(evidence.get("frozenSessions") or 0)
        observation_sessions = int(evidence.get("observationSessions") or 0)
        pending_decisions = int(evidence.get("pendingDecisionSessions") or 0)
        missed_rows = evidence.get("missedSessions") or []
        if not isinstance(missed_rows, list):
            raise TypeError("missed session list is invalid")
        missed_dates: list[date] = []
        for row in missed_rows:
            if not isinstance(row, Mapping):
                raise TypeError("missed session row is invalid")
            value = row.get("trade_date") or row.get("tradeDate")
            missed_dates.append(date.fromisoformat(str(value or "")))

        eligible_through_today = [
            value
            for value in trading_dates
            if first_eligible <= value <= local_now.date()
        ]
        prior_dates = [value for value in eligible_through_today if value < local_now.date()]
        today_is_session = local_now.date() in eligible_through_today
        local_clock = local_now.time().replace(tzinfo=None)
        minimum_expected = len(prior_dates)
        maximum_expected = minimum_expected
        phase = "BEFORE_FIRST_SESSION" if local_now.date() < first_eligible else "NON_TRADING"
        if today_is_session and two_phase:
            if decision_after is None or decision_deadline is None:
                raise ValueError("two-phase causal window is incomplete")
            if local_clock < decision_after:
                phase = "PRE_DECISION"
            elif local_clock <= decision_deadline:
                phase = "DECISION_WINDOW"
                maximum_expected += 1
            elif local_clock < freeze_after:
                phase = "AWAITING_EXECUTION_WINDOW"
                minimum_expected += 1
                maximum_expected += 1
            elif local_clock <= freeze_deadline:
                phase = "EXECUTION_BINDING_WINDOW"
                minimum_expected += 1
                maximum_expected += 1
            else:
                phase = "POST_EXECUTION_DEADLINE"
                minimum_expected += 1
                maximum_expected += 1
        elif today_is_session and local_clock < freeze_after:
            phase = "PRE_FREEZE"
        elif today_is_session and local_clock <= freeze_deadline:
            phase = "FREEZE_WINDOW"
            maximum_expected += 1
        elif today_is_session:
            phase = "POST_FREEZE_DEADLINE"
            minimum_expected += 1
            maximum_expected += 1

        represented_sessions = frozen_sessions + pending_decisions + len(missed_dates)
        allowed_dates = set(prior_dates)
        if maximum_expected > len(prior_dates):
            allowed_dates.add(local_now.date())

        if frozen_sessions < 0 or observation_sessions < 0 or pending_decisions < 0:
            errors.append("session counts cannot be negative")
        if observation_sessions > frozen_sessions:
            errors.append("observation count exceeds frozen session count")
        if pending_decisions > 1:
            errors.append("more than one decision intent is pending execution")
        if len(set(missed_dates)) != len(missed_dates):
            errors.append("missed session dates are duplicated")
        if any(value not in allowed_dates for value in missed_dates):
            errors.append("missed session date is outside the due frozen calendar")
        if missed_dates:
            errors.append("one or more due sessions were permanently missed")
        if not minimum_expected <= represented_sessions <= maximum_expected:
            errors.append(
                "represented session count does not match the frozen calendar deadline"
            )

        latest_session = state.get("latestSession")
        latest_session_date: date | None = None
        latest_session_status: str | None = None
        if latest_session is not None:
            if not isinstance(latest_session, Mapping):
                errors.append("latest session contract is invalid")
            else:
                try:
                    latest_session_date = date.fromisoformat(
                        str(
                            latest_session.get("trade_date")
                            or latest_session.get("tradeDate")
                            or ""
                        )
                    )
                except ValueError:
                    errors.append("latest session date is invalid")
                latest_session_status = str(latest_session.get("status") or "")

        latest_intent_date: date | None = None
        latest_intent = state.get("latestDecisionIntent")
        if pending_decisions:
            if not isinstance(latest_intent, Mapping):
                errors.append("pending decision intent summary is missing")
            else:
                try:
                    latest_intent_date = date.fromisoformat(
                        str(latest_intent.get("tradeDate") or "")
                    )
                except ValueError:
                    errors.append("latest decision intent date is invalid")
                if latest_intent.get("status") != "DECISION_FROZEN":
                    errors.append("pending decision intent status is invalid")
                if latest_intent_date is not None and latest_intent_date < local_now.date():
                    errors.append("a prior decision intent is still pending execution")

        if not missed_dates:
            expected_dates = list(prior_dates)
            if represented_sessions > len(prior_dates):
                expected_dates.append(local_now.date())
            expected_latest = expected_dates[-1] if expected_dates else None
            represented_latest = latest_intent_date or latest_session_date
            if expected_latest != represented_latest:
                errors.append("latest session does not match the latest represented date")
            if latest_session_date is not None and latest_session_status not in {
                "FROZEN_AWAITING_OFFICIAL",
                "FINALIZED",
            }:
                errors.append("latest session status is invalid")

        return {
            "valid": not errors,
            "phase": phase,
            "firstEligibleSignalDate": first_eligible.isoformat(),
            "freezeAfter": freeze_after.isoformat(),
            "freezeDeadline": freeze_deadline.isoformat(),
            "minimumExpectedSessions": minimum_expected,
            "maximumExpectedSessions": maximum_expected,
            "representedSessions": represented_sessions,
            "frozenSessions": frozen_sessions,
            "pendingDecisionSessions": pending_decisions,
            "observationSessions": observation_sessions,
            "missedSessions": len(missed_dates),
            "latestSessionDate": (
                latest_session_date.isoformat() if latest_session_date else None
            ),
            "errors": errors,
        }
    except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        return {
            "valid": False,
            "phase": "INVALID",
            "errors": [f"session continuity inspection failed: {exc}"],
        }


def inspect_capture_history(
    capture: Mapping[str, Any], *, now: datetime,
) -> dict[str, Any]:
    """Keep a recovered quote from hiding immutable failures earlier today."""
    history = capture.get("temporalHealth")
    waiting_market = capture.get("status") == "WAITING_MARKET"
    if history is None:
        return {
            "valid": waiting_market,
            "phase": "WAITING_MARKET" if waiting_market else "MISSING_HISTORY",
        }
    if not isinstance(history, Mapping):
        return {"valid": False, "phase": "INVALID_HISTORY"}
    detail = {
        key: history.get(key)
        for key in (
            "tradeDate", "status", "integrityValid", "errorSlotCount",
            "missingSlotCount", "successfulSlotCoverageRatio",
            "longestConsecutiveUnavailableSlots", "criticalSlots",
        )
    }
    try:
        trade_day = date.fromisoformat(str(history.get("tradeDate") or ""))
        today = now.astimezone(CHINA_TZ).date()
        if history.get("integrityValid") is not True or trade_day > today:
            raise ValueError("invalid capture history integrity or date")
        if trade_day < today:
            # Retain prior-day diagnostics without carrying a daily outage forward.
            return {**detail, "valid": waiting_market, "phase": "PRIOR_SESSION"}
        counts = [history.get(key) for key in ("errorSlotCount", "missingSlotCount")]
        if any(type(value) is not int or value < 0 for value in counts):
            raise ValueError("invalid capture history counts")
        valid = history.get("status") in {"PENDING", "IN_PROGRESS", "COMPLETE"}
        return {**detail, "valid": valid and not any(counts), "phase": "CURRENT_SESSION"}
    except (TypeError, ValueError):
        return {**detail, "valid": False, "phase": "INVALID_HISTORY"}


def build_readiness_report(
    *,
    supervisor: Mapping[str, Any],
    health: Mapping[str, Any],
    capture: Mapping[str, Any],
    state: Mapping[str, Any],
    automation: Mapping[str, Any],
    declaration_binding: Mapping[str, Any],
    session_continuity: Mapping[str, Any],
    storage: Mapping[str, Any],
    now: datetime,
) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []

    def add(name: str, passed: bool, detail: Mapping[str, Any]) -> None:
        checks.append({"name": name, "passed": bool(passed), "detail": dict(detail)})

    add(
        "supervisor",
        bool(supervisor.get("running") and supervisor.get("healthy")),
        {
            "running": bool(supervisor.get("running")),
            "healthy": bool(supervisor.get("healthy")),
        },
    )
    guard_required = bool(supervisor.get("sleepGuardConfigured"))
    add(
        "sleepGuard",
        not guard_required or bool(supervisor.get("sleepGuardActive")),
        {
            "configured": guard_required,
            "active": bool(supervisor.get("sleepGuardActive")),
        },
    )

    workers = health.get("workers") or {}
    worker_detail = {
        name: {
            "ready": bool((workers.get(name) or {}).get("ready")),
            "taskAlive": bool((workers.get(name) or {}).get("taskAlive")),
            "consecutiveErrors": int(
                (workers.get(name) or {}).get("consecutiveErrors") or 0
            ),
        }
        for name in EXPECTED_WORKERS
    }
    workers_ready = bool(health.get("ready")) and all(
        detail["ready"]
        and detail["taskAlive"]
        and detail["consecutiveErrors"] == 0
        for detail in worker_detail.values()
    )
    add("backendWorkers", workers_ready, worker_detail)

    capture_status = str(capture.get("status") or "")
    capture_errors = int(capture.get("consecutiveErrors") or 0)
    add(
        "capturePipeline",
        bool(
            capture.get("enabled")
            and capture.get("configuredEnabled")
            and capture_status in USABLE_CAPTURE_STATES
            and capture_errors == 0
            and not capture.get("lastError")
        ),
        {
            "status": capture_status,
            "enabled": bool(capture.get("enabled")),
            "consecutiveErrors": capture_errors,
        },
    )

    capture_history = inspect_capture_history(capture, now=now)
    add("captureHistory", capture_history["valid"], capture_history)

    first_eligible_text = str(
        (state.get("protocol") or {}).get("firstEligibleSignalDate") or ""
    )
    next_capture_text = str(capture.get("nextCaptureAt") or "")
    try:
        first_eligible = date.fromisoformat(first_eligible_text)
        next_capture = datetime.fromisoformat(next_capture_text).astimezone(CHINA_TZ)
        schedule_ready = now.date() >= first_eligible or next_capture.date() == first_eligible
    except ValueError:
        schedule_ready = False
    add(
        "captureSchedule",
        schedule_ready,
        {
            "firstEligibleSignalDate": first_eligible_text,
            "nextCaptureAt": next_capture_text or None,
        },
    )

    calibration = capture.get("calibration") or {}
    add(
        "calibration",
        bool(
            calibration.get("status") == "PASS"
            and calibration.get("finalized") is True
            and calibration.get("profitEvidenceEligible") is False
            and int(calibration.get("expectedSlotCount") or 0) > 0
            and int(calibration.get("capturedExpectedSlotCount") or 0)
            == int(calibration.get("expectedSlotCount") or 0)
        ),
        {
            "status": calibration.get("status"),
            "finalized": bool(calibration.get("finalized")),
            "capturedExpectedSlotCount": calibration.get("capturedExpectedSlotCount"),
            "expectedSlotCount": calibration.get("expectedSlotCount"),
        },
    )

    add(
        "sessionContinuity",
        bool(session_continuity.get("valid")),
        session_continuity,
    )

    state_status = str(state.get("status") or "")
    missed = (state.get("pairedEvidence") or {}).get("missedSessions") or []
    add(
        "intradayRun",
        bool(
            state_status in USABLE_RUN_STATES
            and not missed
            and automation.get("enabled")
            and automation.get("configuredEnabled")
            and int(automation.get("consecutiveErrors") or 0) == 0
            and not automation.get("lastError")
            and state.get("runId") == automation.get("runId")
            and (state.get("declaration") or {}).get("path")
            == automation.get("configuredDeclaration")
        ),
        {
            "runId": state.get("runId"),
            "status": state_status,
            "missedSessions": len(missed),
            "automationErrors": int(automation.get("consecutiveErrors") or 0),
        },
    )

    quality = state.get("quality") or {}
    quality_extension = state.get("qualityExtension") or {}
    run_version_match = re.search(r"_v(\d+)$", str(state.get("runId") or ""))
    requires_core_field_contract = bool(
        run_version_match and int(run_version_match.group(1)) >= 4
    )
    core_field_contract_valid = bool(
        quality_extension.get("requireCoreFieldAwareFallback") is True
        and quality_extension.get("benchmarkTurnoverRequired") is False
    )
    add(
        "qualityContract",
        bool(
            int(quality.get("requiredUniverseSize") or 0) > 0
            and 0 < float(quality.get("minimumCoreFieldCoverageRatio") or 0) <= 1
            and int(quality.get("maximumQuoteAgeSeconds") or 0) > 0
            and quality.get("requireBenchmarkUsable") is True
            and quality.get("requireExecutionQuotesForAllTargets") is True
            and quality_extension.get("requireDecisionQuotesForAllTargets") is True
            and (
                not requires_core_field_contract
                or core_field_contract_valid
            )
        ),
        {
            "requiredUniverseSize": quality.get("requiredUniverseSize"),
            "minimumCoreFieldCoverageRatio": quality.get(
                "minimumCoreFieldCoverageRatio"
            ),
            "maximumQuoteAgeSeconds": quality.get("maximumQuoteAgeSeconds"),
            "requireDecisionQuotesForAllTargets": quality_extension.get(
                "requireDecisionQuotesForAllTargets"
            ),
            "requireExecutionQuotesForAllTargets": quality.get(
                "requireExecutionQuotesForAllTargets"
            ),
            "requireCoreFieldAwareFallback": quality_extension.get(
                "requireCoreFieldAwareFallback"
            ),
            "benchmarkTurnoverRequired": quality_extension.get(
                "benchmarkTurnoverRequired"
            ),
        },
    )

    requires_two_phase = bool(
        run_version_match and int(run_version_match.group(1)) >= 5
    )
    causality = state.get("causality") or {}
    protocol = state.get("protocol") or {}
    try:
        causal_timing_valid = bool(
            time.fromisoformat(str(protocol.get("decisionSlot") or ""))
            <= time.fromisoformat(
                str(protocol.get("decisionFreezeAfter") or "")
            )
            <= time.fromisoformat(
                str(protocol.get("decisionFreezeDeadline") or "")
            )
            < time.fromisoformat(str(protocol.get("executionSlot") or ""))
            < time.fromisoformat(
                str(protocol.get("executionBindingAfter") or "")
            )
            <= time.fromisoformat(
                str(protocol.get("executionBindingDeadline") or "")
            )
        )
    except ValueError:
        causal_timing_valid = False
    causal_contract_valid = bool(
        causality.get("decisionIntentRequired") is True
        and causality.get("requireNoPriorPendingSession") is True
        and causality.get("requirePortfolioFingerprint") is True
        and causal_timing_valid
    )
    add(
        "causalOrder",
        not requires_two_phase or causal_contract_valid,
        {
            "required": requires_two_phase,
            "decisionIntentRequired": causality.get("decisionIntentRequired"),
            "requireNoPriorPendingSession": causality.get(
                "requireNoPriorPendingSession"
            ),
            "requirePortfolioFingerprint": causality.get(
                "requirePortfolioFingerprint"
            ),
            "timingValid": causal_timing_valid if requires_two_phase else None,
        },
    )

    requires_source_reliability = bool(
        run_version_match and int(run_version_match.group(1)) >= 6
    )
    if requires_source_reliability:
        source_reliability = state.get("sourceReliability") or {}
        qualification = source_reliability.get("qualification") or {}
        capture_runtime = capture.get("runtimeContract") or {}
        contract_sha = str(
            source_reliability.get("realtimeContractSha256") or ""
        )
        qualification_sha = str(qualification.get("reportSha256") or "")
        source_contract_valid = bool(
            source_reliability.get("version") == 1
            and source_reliability.get("sourceOrder")
            == ["tencent", "easy_tdx", "eastmoney", "sina"]
            and source_reliability.get("independentFallbackTransport")
            == "TCP/7709"
            and source_reliability.get("easyTdxPackageVersion") == "1.20.6"
            and re.fullmatch(r"[0-9a-f]{64}", contract_sha)
            and source_reliability.get("requireRealtimeContractForEveryQuote")
            is True
            and source_reliability.get("prohibitCrossSourceFieldMerge") is True
            and re.fullmatch(r"[0-9a-f]{64}", qualification_sha)
            and capture_runtime.get("version") == 1
            and capture_runtime.get("entrypointModule")
            == "market_data.intraday_capture_v3"
            and capture_runtime.get("providerModule")
            == "market_data.providers.a_stock_data_intraday_v3"
            and capture_runtime.get("providerClass")
            == "IndependentTransportRealtimeClient"
            and capture_runtime.get("easyTdxPackageVersion")
            == source_reliability.get("easyTdxPackageVersion")
            and capture_runtime.get("realtimeContractSha256") == contract_sha
            and capture_runtime.get("sourceOrder")
            == source_reliability.get("sourceOrder")
            and capture_runtime.get("prohibitCrossSourceFieldMerge") is True
        )
        add(
            "sourceReliability",
            source_contract_valid,
            {
                "required": True,
                "sourceOrder": source_reliability.get("sourceOrder"),
                "independentFallbackTransport": source_reliability.get(
                    "independentFallbackTransport"
                ),
                "easyTdxPackageVersion": source_reliability.get(
                    "easyTdxPackageVersion"
                ),
                "realtimeContractBound": bool(contract_sha),
                "qualificationBound": bool(qualification_sha),
                "prohibitCrossSourceFieldMerge": source_reliability.get(
                    "prohibitCrossSourceFieldMerge"
                ),
                "captureEntrypoint": capture_runtime.get("entrypointModule"),
                "captureProvider": capture_runtime.get("providerModule"),
                "captureContractMatches": (
                    capture_runtime.get("realtimeContractSha256") == contract_sha
                ),
            },
        )

    audit = state.get("audit") or {}
    checkpoint = state.get("evidenceCheckpoint") or {}
    add(
        "evidenceChain",
        bool(
            audit.get("valid")
            and checkpoint.get("valid")
            and checkpoint.get("upToDate")
            and int(checkpoint.get("count") or 0) >= 1
        ),
        {
            "auditValid": bool(audit.get("valid")),
            "auditEvents": int(audit.get("events") or 0),
            "checkpointValid": bool(checkpoint.get("valid")),
            "checkpointUpToDate": bool(checkpoint.get("upToDate")),
            "checkpointCount": int(checkpoint.get("count") or 0),
        },
    )
    add(
        "declarationBinding",
        bool(declaration_binding.get("valid")),
        declaration_binding,
    )
    add(
        "storage",
        bool(
            storage.get("exists")
            and storage.get("writable")
            and int(storage.get("freeBytes") or 0)
            >= int(storage.get("referenceCaptureBytes") or 1)
        ),
        storage,
    )

    ready = all(check["passed"] for check in checks)
    return {
        "version": 1,
        "status": "READY" if ready else "NOT_READY",
        "ready": ready,
        "checkedAt": now.astimezone(CHINA_TZ).isoformat(),
        "runId": state.get("runId"),
        "firstEligibleSignalDate": first_eligible_text or None,
        "checks": checks,
        "limitations": [
            "Market-closed preflight cannot prove that public quote upstreams will be available during the next session.",
            "Operational readiness does not test or prove profitability.",
            "Logout and reboot recovery require the separately authorized macOS login item.",
        ],
    }


def collect_report(*, now: datetime | None = None) -> dict[str, Any]:
    checked_at = (now or datetime.now(CHINA_TZ)).astimezone(CHINA_TZ)
    supervisor = status_payload()
    health = _api("/api/health/ready")

    def data(path: str) -> dict[str, Any]:
        response = _api(path)
        payload = response.get("data")
        if response.get("success") is not True or not isinstance(payload, dict):
            raise RuntimeError(f"local API returned no data: {path}")
        return payload

    capture = data("/api/v1/market-data/intraday-capture/status")
    state = data("/api/v1/intraday-shadow/state")
    automation = data("/api/v1/intraday-shadow/automation")
    declaration_binding = verify_declaration_binding(state)
    session_continuity = inspect_session_continuity(state, now=checked_at)
    storage = inspect_storage(capture.get("outputRoot"))
    return build_readiness_report(
        supervisor=supervisor,
        health=health,
        capture=capture,
        state=state,
        automation=automation,
        declaration_binding=declaration_binding,
        session_continuity=session_continuity,
        storage=storage,
        now=checked_at,
    )


def main() -> int:
    try:
        report = collect_report()
    except Exception as exc:
        report = {
            "version": 1,
            "status": "NOT_READY",
            "ready": False,
            "checkedAt": datetime.now(CHINA_TZ).isoformat(),
            "fatalError": str(exc)[:1000],
        }
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report.get("ready") else 1


if __name__ == "__main__":
    raise SystemExit(main())
