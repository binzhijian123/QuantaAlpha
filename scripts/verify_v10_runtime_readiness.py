#!/usr/bin/env python3
"""V10-bound runtime preflight; operational readiness is not release acceptance."""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
import sys
import urllib.request

PROJECT_ROOT = Path(__file__).resolve().parents[1]
for directory in (PROJECT_ROOT, PROJECT_ROOT / "frontend-v2/backend"):
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))

from market_data import intraday_shadow_v10 as v10
from market_data.intraday_capture_health import summarize_latest_capture_day, _expected_slots, DEFAULT_CONTINUOUS_WINDOWS

WORKERS = ("capture", "shadow", "maintenance")


def _api(path):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open("http://127.0.0.1:8000" + path, timeout=10) as response:
        data = json.load(response)
    return data


def evaluate(declaration_path, *, now=None, fetch=_api):
    stamp = now if now is not None else datetime.now(v10.SHANGHAI_TZ)
    if stamp.tzinfo is None or stamp.utcoffset() is None:
        raise ValueError("V10 preflight requires an aware clock")
    stamp = stamp.astimezone(v10.SHANGHAI_TZ)
    checks = {}
    run_id = digest = None
    try:
        validated = v10.validate_v10_declaration(declaration_path)
        declaration = validated["declaration"]
        run_id = declaration["runId"]
        digest = validated["declarationSha256"]
        checks["declaration"] = True
        status = fetch("/api/v1/v10-runtime/status")
        status = status.get("data", status)
        health = fetch("/api/v1/v10-runtime/ready")
        health = health.get("data", health)
        checks["runtimeBinding"] = (status.get("mode") == "FORWARD_RUNTIME"
            and status.get("configured") is True
            and status.get("runId") == run_id and status.get("declarationSha256") == digest)
        checks["healthBinding"] = (health.get("runId") == run_id
            and health.get("declarationSha256") == digest and health.get("ready") is True)
        checks["explicitWorkers"] = all(status.get(key) is True for key in
                                        ("captureEnabled", "shadowEnabled", "maintenanceEnabled"))
        workers = status.get("workers") or {}
        for name in WORKERS:
            worker = workers.get(name) or {}
            checks[name + "Worker"] = worker.get("ready") is True and worker.get("taskAlive") is True
        checks["noRuntimeError"] = not status.get("errorCode")
        # Never accept a stale good earlier session while today's due slots are
        # absent. Earlier calibration captures do not belong in this new tree.
        root = v10._resolve(declaration["captureRoot"])
        temporal = summarize_latest_capture_day(root, now=stamp, poll_seconds=60)
        calendar = set(validated["providerContext"]["tradingDates"])
        today = stamp.date().isoformat()
        due_today = (today >= declaration["firstEligibleSignalDate"] and today in calendar
                     and any(slot + timedelta(seconds=60) <= stamp for slot in
                             _expected_slots(today, 60, DEFAULT_CONTINUOUS_WINDOWS)))
        checks["captureHistory"] = (
            (not due_today and temporal is None)
            or (isinstance(temporal, dict) and temporal.get("integrityValid") is True
                and temporal.get("errorSlotCount") == 0 and temporal.get("missingSlotCount") == 0
                and temporal.get("tradeDate", "") <= today
                and (not due_today or temporal.get("tradeDate") == today)))
    except Exception:
        # Fixed public failure; no raw exception/configuration/URLs emitted.
        checks["verificationCompleted"] = False
    ready = bool(checks) and all(value is True for value in checks.values())
    return {"version": 1, "protocolId": v10.PROTOCOL_ID,
            "checkedAt": stamp.isoformat(), "runId": run_id,
            "declarationSha256": digest, "status": "READY" if ready else "NOT_READY",
            "ready": ready, "autoShadowAccepted": False,
            "releaseAccepted": False, "checks": checks,
            "failedChecks": [name for name, passed in checks.items() if passed is not True]}


def main():
    from dotenv import dotenv_values
    values = dotenv_values(PROJECT_ROOT / ".env", interpolate=False)
    configured = values.get("QA_V10_DECLARATION")
    result = evaluate(configured or PROJECT_ROOT / "configs/UNCONFIGURED_V10.json")
    print(json.dumps(result, sort_keys=True))
    return 0 if result["ready"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
