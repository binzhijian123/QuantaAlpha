#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import shlex
import signal
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any, Callable

from dotenv import dotenv_values


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCREEN_NAME = "qa_backend_supervised"
WRAPPER_PATH = PROJECT_ROOT / "scripts" / "run_research_shadow_backend_supervised.sh"
PID_FILE = PROJECT_ROOT / "data" / "results" / "research-shadow-backend-supervisor.pid"
SLEEP_GUARD_PID_FILE = (
    PROJECT_ROOT / "data" / "results" / "research-shadow-sleep-guard.pid"
)
LOG_PATH = PROJECT_ROOT / "data" / "results" / "backend.log"
INTRADAY_PREFLIGHT_REPORT_FILE = (
    PROJECT_ROOT / "data" / "results" / "intraday-shadow-readiness-latest.json"
)
HEALTH_URL = "http://127.0.0.1:8000/api/health/ready"
SCREEN_BIN = "/usr/bin/screen"


def prevent_idle_sleep_enabled() -> bool:
    value = os.environ.get("QA_BACKEND_PREVENT_IDLE_SLEEP")
    if value is None:
        value = dotenv_values(PROJECT_ROOT / ".env").get(
            "QA_BACKEND_PREVENT_IDLE_SLEEP", "false"
        )
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def intraday_preflight_enabled() -> bool:
    value = os.environ.get("QA_BACKEND_INTRADAY_PREFLIGHT_ENABLED")
    if value is None:
        value = dotenv_values(PROJECT_ROOT / ".env").get(
            "QA_BACKEND_INTRADAY_PREFLIGHT_ENABLED", "false"
        )
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def port_open(host: str = "127.0.0.1", port: int = 8000) -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.25):
            return True
    except OSError:
        return False


def health_ok() -> bool:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(HEALTH_URL, timeout=1.0) as response:
            payload = json.load(response)
        return response.status == 200 and payload.get("status") == "healthy"
    except (OSError, ValueError):
        return False


def wait_until(predicate: Callable[[], bool], expected: bool, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate() is expected:
            return True
        time.sleep(0.1)
    return predicate() is expected


def process_command(pid: int) -> str:
    result = subprocess.run(
        ["/bin/ps", "-p", str(pid), "-o", "command="],
        check=False,
        text=True,
        capture_output=True,
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def supervisor_pid() -> int | None:
    if not PID_FILE.is_file():
        return None
    try:
        pid = int(PID_FILE.read_text(encoding="ascii").strip())
    except (OSError, ValueError):
        PID_FILE.unlink(missing_ok=True)
        return None
    command = process_command(pid)
    if not command:
        PID_FILE.unlink(missing_ok=True)
        return None
    if WRAPPER_PATH.name not in command:
        raise RuntimeError(
            f"supervisor pid file points to an unrelated live process: {pid}"
        )
    return pid


def sleep_guard_pid(expected_supervisor_pid: int | None) -> int | None:
    if not SLEEP_GUARD_PID_FILE.is_file():
        return None
    try:
        pid = int(SLEEP_GUARD_PID_FILE.read_text(encoding="ascii").strip())
    except (OSError, ValueError):
        SLEEP_GUARD_PID_FILE.unlink(missing_ok=True)
        return None
    command = process_command(pid)
    if not command:
        SLEEP_GUARD_PID_FILE.unlink(missing_ok=True)
        return None
    try:
        parts = shlex.split(command)
    except ValueError as exc:
        raise RuntimeError(f"sleep guard has an invalid process command: {pid}") from exc
    expected = str(expected_supervisor_pid) if expected_supervisor_pid else None
    valid = (
        bool(parts)
        and Path(parts[0]).name == "caffeinate"
        and "-i" in parts
        and "-w" in parts
        and expected is not None
        and parts[parts.index("-w") + 1 : parts.index("-w") + 2] == [expected]
    )
    if not valid:
        raise RuntimeError(
            f"sleep guard pid file points to an unrelated live process: {pid}"
        )
    return pid


def screen_command() -> list[str]:
    sleep_guard_value = "true" if prevent_idle_sleep_enabled() else "false"
    preflight_value = "true" if intraday_preflight_enabled() else "false"
    command = (
        f"export QA_BACKEND_PREVENT_IDLE_SLEEP={sleep_guard_value} && "
        f"export QA_BACKEND_INTRADAY_PREFLIGHT_ENABLED={preflight_value} && "
        f"cd {shlex.quote(str(PROJECT_ROOT))} && "
        f"exec {shlex.quote(str(WRAPPER_PATH))} >> "
        f"{shlex.quote(str(LOG_PATH))} 2>&1"
    )
    return [SCREEN_BIN, "-dmS", SCREEN_NAME, "/bin/bash", "-lc", command]


def _screen_cleanup() -> None:
    subprocess.run(
        [SCREEN_BIN, "-S", SCREEN_NAME, "-X", "quit"],
        check=False,
        capture_output=True,
    )
    subprocess.run([SCREEN_BIN, "-wipe"], check=False, capture_output=True)


def start() -> None:
    pid = supervisor_pid()
    if pid is not None:
        if health_ok():
            return
        raise RuntimeError(f"supervisor {pid} is running but backend is unhealthy")
    if port_open():
        raise RuntimeError(
            "port 8000 is already in use by a process outside the managed supervisor"
        )
    _screen_cleanup()
    subprocess.run(screen_command(), check=True)

    def managed_ready() -> bool:
        pid = supervisor_pid()
        if pid is None or not health_ok():
            return False
        return not prevent_idle_sleep_enabled() or sleep_guard_pid(pid) is not None

    if not wait_until(managed_ready, True, 20.0):
        try:
            stop()
        finally:
            raise RuntimeError(
                "supervised backend or sleep guard failed to start; "
                f"inspect {LOG_PATH}"
            )


def stop() -> None:
    pid = supervisor_pid()
    if pid is None:
        if port_open():
            raise RuntimeError(
                "port 8000 is occupied but no managed supervisor owns it"
            )
        _screen_cleanup()
        return
    os.kill(pid, signal.SIGTERM)
    if not wait_until(lambda: bool(process_command(pid)), False, 10.0):
        raise RuntimeError(f"supervisor {pid} did not stop after SIGTERM")
    if not wait_until(port_open, False, 5.0):
        raise RuntimeError("backend port 8000 remained open after supervisor stopped")
    PID_FILE.unlink(missing_ok=True)
    _screen_cleanup()


def restart() -> None:
    stop()
    start()


def status_payload() -> dict[str, Any]:
    pid = supervisor_pid()
    guard_configured = prevent_idle_sleep_enabled()
    guard_pid = sleep_guard_pid(pid)
    preflight_report: dict[str, Any] = {}
    try:
        loaded_report = json.loads(
            INTRADAY_PREFLIGHT_REPORT_FILE.read_text(encoding="utf-8")
        )
        if isinstance(loaded_report, dict):
            preflight_report = loaded_report
    except (OSError, ValueError):
        pass
    return {
        "screenName": SCREEN_NAME,
        "supervisorPid": pid,
        "running": pid is not None,
        "healthy": health_ok(),
        "sleepGuardConfigured": guard_configured,
        "sleepGuardPid": guard_pid,
        "sleepGuardActive": guard_pid is not None,
        "sleepGuardPidFile": str(SLEEP_GUARD_PID_FILE),
        "intradayPreflightConfigured": intraday_preflight_enabled(),
        "intradayPreflightReportFile": str(INTRADAY_PREFLIGHT_REPORT_FILE),
        "intradayPreflightStatus": preflight_report.get("status"),
        "intradayPreflightCheckedAt": preflight_report.get("checkedAt"),
        "pidFile": str(PID_FILE),
        "logPath": str(LOG_PATH),
        "restartDelaySeconds": 5,
        "restartMaxDelaySeconds": 300,
        "restartResetSeconds": 900,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Manage the crash-restarting Screen backend supervisor."
    )
    parser.add_argument("command", choices=("start", "stop", "restart", "status"))
    args = parser.parse_args()
    try:
        if args.command == "start":
            start()
        elif args.command == "stop":
            stop()
        elif args.command == "restart":
            restart()
        print(json.dumps(status_payload(), ensure_ascii=False, indent=2))
        return 0
    except (OSError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
