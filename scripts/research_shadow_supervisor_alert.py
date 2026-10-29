#!/usr/bin/env python3
from __future__ import annotations

import argparse
import fcntl
import json
import os
import subprocess
import sys
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from dotenv import dotenv_values


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_EVENT_LOG = (
    PROJECT_ROOT / "data" / "results" / "research-shadow-supervisor-events.jsonl"
)
CHINA_TZ = ZoneInfo("Asia/Shanghai")
TRUE_VALUES = {"1", "true", "yes", "on"}
EVENT_DEFINITIONS = {
    "BACKEND_UNHEALTHY": (
        "critical",
        "Backend readiness failed repeatedly; the supervisor is restarting it.",
    ),
    "BACKEND_EXITED": (
        "critical",
        "Backend exited unexpectedly; the supervisor scheduled a restart.",
    ),
    "BACKEND_RECOVERED": (
        "info",
        "Backend readiness recovered after an automatic restart.",
    ),
    "SLEEP_GUARD_FAILED": (
        "critical",
        "The macOS idle-sleep guard stopped; unattended market capture is at risk.",
    ),
    "SLEEP_GUARD_RECOVERED": (
        "info",
        "The macOS idle-sleep guard recovered automatically.",
    ),
    "OFFICIAL_PROVIDER_UPDATE_ERROR": (
        "critical",
        "The official daily Provider update cycle failed; the active Provider was unchanged.",
    ),
    "OFFICIAL_PROVIDER_UPDATE_RECOVERED": (
        "info",
        "The official daily Provider update cycle recovered after an error.",
    ),
    "OFFICIAL_PROVIDER_OVERDUE": (
        "critical",
        "The next official daily Provider session is overdue past its release grace period.",
    ),
    "OFFICIAL_PROVIDER_FRESHNESS_RECOVERED": (
        "info",
        "Official daily Provider freshness recovered within the declared release schedule.",
    ),
    "RESEARCH_SHADOW_REVIEW_REQUIRED": (
        "critical",
        "The daily research shadow requires review; ledger actions remain blocked while official Provider maintenance continues.",
    ),
    "INTRADAY_CAPTURE_ERROR": (
        "critical",
        "An immutable intraday capture cycle failed; failed slots cannot be backfilled.",
    ),
    "INTRADAY_CAPTURE_RECOVERED": (
        "info",
        "Intraday capture resumed; any previously failed immutable slot remains failed.",
    ),
    "INTRADAY_TEMPORAL_DEGRADED": (
        "critical",
        "The completed intraday capture day has failed or missing immutable slots; inspect temporal health before using the minute series.",
    ),
    "INTRADAY_SHADOW_ERROR": (
        "critical",
        "The intraday shadow cycle failed; inspect the local automation status.",
    ),
    "INTRADAY_SHADOW_RECOVERED": (
        "info",
        "The intraday shadow cycle recovered after an error.",
    ),
    "INTRADAY_SIGNAL_FROZEN": (
        "info",
        "The paired intraday signal and simulated execution prices were frozen.",
    ),
    "INTRADAY_DECISION_INTENT_FROZEN": (
        "info",
        "The intraday target intent was frozen before the execution quote window.",
    ),
    "INTRADAY_EXECUTION_PRICE_BOUND": (
        "info",
        "The immutable execution prices were bound to the prior intraday target intent.",
    ),
    "INTRADAY_SESSION_FINALIZED": (
        "info",
        "The paired intraday shadow session was finalized with official daily data.",
    ),
    "INTRADAY_SESSION_MISSED": (
        "critical",
        "An intraday shadow session was missed and cannot be backfilled.",
    ),
    "INTRADAY_RISK_STOPPED": (
        "critical",
        "The intraday shadow run reached its drawdown stop and blocked new signals.",
    ),
    "INTRADAY_PREFLIGHT_FAILED": (
        "critical",
        "The unattended intraday readiness preflight failed; inspect the private readiness report.",
    ),
    "INTRADAY_PREFLIGHT_RECOVERED": (
        "info",
        "The unattended intraday readiness preflight recovered.",
    ),
}


@dataclass(frozen=True)
class AlertConfig:
    event_log: Path
    desktop_notifications: bool
    webhook_url: str | None
    timeout_seconds: float


def load_alert_config() -> AlertConfig:
    file_values = {
        key: value
        for key, value in dotenv_values(PROJECT_ROOT / ".env").items()
        if value is not None
    }

    def configured(name: str, default: str = "") -> str:
        return str(os.environ.get(name, file_values.get(name, default))).strip()

    event_log_value = configured("QA_BACKEND_ALERT_EVENT_LOG")
    event_log = Path(event_log_value).expanduser() if event_log_value else DEFAULT_EVENT_LOG
    if not event_log.is_absolute():
        event_log = PROJECT_ROOT / event_log
    try:
        timeout_seconds = float(configured("QA_BACKEND_ALERT_TIMEOUT_SECONDS", "3"))
    except ValueError:
        timeout_seconds = 3.0
    return AlertConfig(
        event_log=event_log.resolve(),
        desktop_notifications=(
            configured("QA_BACKEND_DESKTOP_NOTIFICATIONS", "false").lower()
            in TRUE_VALUES
        ),
        webhook_url=configured("QA_BACKEND_ALERT_WEBHOOK_URL") or None,
        timeout_seconds=min(10.0, max(1.0, timeout_seconds)),
    )


def build_event(
    event_name: str,
    *,
    supervisor_pid: int,
    child_pid: int | None = None,
    exit_code: int | None = None,
    child_runtime_seconds: int | None = None,
    restart_delay_seconds: int | None = None,
    subject_date: str | None = None,
    occurred_at: datetime | None = None,
) -> dict[str, Any]:
    if event_name not in EVENT_DEFINITIONS:
        raise ValueError(f"unsupported supervisor event: {event_name}")
    severity, message = EVENT_DEFINITIONS[event_name]
    now = (occurred_at or datetime.now(CHINA_TZ)).astimezone(CHINA_TZ)
    event = {
        "schemaVersion": 1,
        "kind": "quantaalpha_supervisor_event",
        "event": event_name,
        "severity": severity,
        "message": message,
        "occurredAt": now.isoformat(),
        "supervisorPid": int(supervisor_pid),
        "childPid": int(child_pid) if child_pid is not None else None,
        "exitCode": int(exit_code) if exit_code is not None else None,
        "childRuntimeSeconds": (
            int(child_runtime_seconds)
            if child_runtime_seconds is not None
            else None
        ),
        "restartDelaySeconds": (
            int(restart_delay_seconds)
            if restart_delay_seconds is not None
            else None
        ),
    }
    if subject_date is not None:
        parsed_subject_date = date.fromisoformat(subject_date)
        if parsed_subject_date.isoformat() != subject_date:
            raise ValueError("subject date must be canonical ISO format")
        event["subjectDate"] = subject_date
    return event


def append_event(path: Path, event: dict[str, Any]) -> bool:
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(
        event,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    with path.open("a+", encoding="utf-8") as handle:
        os.fchmod(handle.fileno(), 0o600)
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            subject_date = event.get("subjectDate")
            if subject_date is not None:
                handle.seek(0)
                for line in handle:
                    try:
                        existing = json.loads(line)
                    except (TypeError, ValueError):
                        continue
                    if (
                        existing.get("event") == event.get("event")
                        and existing.get("subjectDate") == subject_date
                    ):
                        return False
            handle.seek(0, os.SEEK_END)
            handle.write(raw + "\n")
            handle.flush()
            os.fsync(handle.fileno())
            return True
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _validate_webhook_url(url: str) -> None:
    parsed = urlparse(url)
    local_http = parsed.scheme == "http" and parsed.hostname in {
        "127.0.0.1",
        "localhost",
        "::1",
    }
    if not parsed.hostname or (parsed.scheme != "https" and not local_http):
        raise ValueError("webhook URL must use HTTPS or local HTTP")


def send_webhook(
    url: str,
    event: dict[str, Any],
    *,
    timeout_seconds: float,
    opener: Any | None = None,
) -> None:
    _validate_webhook_url(url)
    request = urllib.request.Request(
        url,
        data=json.dumps(event, ensure_ascii=True, sort_keys=True).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    direct_opener = opener or urllib.request.build_opener(
        urllib.request.ProxyHandler({})
    )
    with direct_opener.open(request, timeout=timeout_seconds) as response:
        status = int(response.getcode())
        if status < 200 or status >= 300:
            raise RuntimeError(f"webhook returned HTTP {status}")


def send_desktop_notification(
    event: dict[str, Any],
    *,
    timeout_seconds: float,
) -> None:
    script = (
        "on run argv\n"
        "display notification (item 2 of argv) with title (item 1 of argv)\n"
        "end run"
    )
    subprocess.run(
        [
            "/usr/bin/osascript",
            "-e",
            script,
            "QuantaAlpha supervisor",
            str(event["message"]),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
    )


def emit_event(event: dict[str, Any], config: AlertConfig) -> list[str]:
    errors: list[str] = []
    appended = True
    try:
        appended = append_event(config.event_log, event)
    except Exception as exc:
        errors.append(f"event-log:{type(exc).__name__}")
    if not appended:
        return errors
    if config.desktop_notifications:
        try:
            send_desktop_notification(event, timeout_seconds=config.timeout_seconds)
        except Exception as exc:
            errors.append(f"desktop:{type(exc).__name__}")
    if config.webhook_url:
        try:
            send_webhook(
                config.webhook_url,
                event,
                timeout_seconds=config.timeout_seconds,
            )
        except Exception as exc:
            errors.append(f"webhook:{type(exc).__name__}")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Record and deliver a backend supervisor lifecycle alert."
    )
    parser.add_argument("event", choices=tuple(EVENT_DEFINITIONS))
    parser.add_argument("--supervisor-pid", type=int, required=True)
    parser.add_argument("--child-pid", type=int)
    parser.add_argument("--exit-code", type=int)
    parser.add_argument("--child-runtime-seconds", type=int)
    parser.add_argument("--restart-delay-seconds", type=int)
    parser.add_argument("--subject-date")
    args = parser.parse_args()
    event = build_event(
        args.event,
        supervisor_pid=args.supervisor_pid,
        child_pid=args.child_pid,
        exit_code=args.exit_code,
        child_runtime_seconds=args.child_runtime_seconds,
        restart_delay_seconds=args.restart_delay_seconds,
        subject_date=args.subject_date,
    )
    for error in emit_event(event, load_alert_config()):
        print(f"supervisor alert delivery failed: {error}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
