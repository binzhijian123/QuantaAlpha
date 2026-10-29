#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import plistlib
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path
from typing import Any, Callable, Sequence


PROJECT_ROOT = Path(__file__).resolve().parents[1]
LABEL = "com.quantaalpha.research-shadow-backend"
WRAPPER_PATH = PROJECT_ROOT / "scripts" / "run_research_shadow_backend.sh"
LOG_DIR = PROJECT_ROOT / "data" / "results"
STDOUT_PATH = LOG_DIR / "research-shadow-backend.stdout.log"
STDERR_PATH = LOG_DIR / "research-shadow-backend.stderr.log"
DEFAULT_PLIST_PATH = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
HEALTH_URL = "http://127.0.0.1:8000/api/health/ready"


def build_plist() -> dict[str, Any]:
    return {
        "Label": LABEL,
        "ProgramArguments": ["/bin/bash", str(WRAPPER_PATH)],
        "WorkingDirectory": str(PROJECT_ROOT),
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 15,
        "ProcessType": "Background",
        "StandardOutPath": str(STDOUT_PATH),
        "StandardErrorPath": str(STDERR_PATH),
        "EnvironmentVariables": {
            "PYTHONUNBUFFERED": "1",
        },
    }


def render_plist() -> bytes:
    return plistlib.dumps(build_plist(), fmt=plistlib.FMT_XML, sort_keys=True)


def launch_target(uid: int | None = None) -> str:
    return f"gui/{uid if uid is not None else os.getuid()}/{LABEL}"


def _run_launchctl(
    args: Sequence[str], *, check: bool = True
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["/bin/launchctl", *args],
        check=check,
        text=True,
        capture_output=True,
    )


def service_loaded(
    run_launchctl: Callable[..., subprocess.CompletedProcess[str]] = _run_launchctl,
) -> bool:
    result = run_launchctl(["print", launch_target()], check=False)
    return result.returncode == 0


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


def write_plist(path: Path = DEFAULT_PLIST_PATH) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        stream.write(render_plist())
    temporary.chmod(0o644)
    os.replace(temporary, path)
    subprocess.run(["/usr/bin/plutil", "-lint", str(path)], check=True)
    return path


def startup_failure_message() -> str:
    try:
        error_tail = STDERR_PATH.read_text(encoding="utf-8", errors="replace")[-8192:]
    except OSError:
        error_tail = ""
    if "Operation not permitted" in error_tail:
        return (
            "macOS denied LaunchAgent access to the project under Documents; "
            "keep the supervised Screen backend or explicitly grant background "
            "file access before retrying"
        )
    return f"LaunchAgent loaded but backend health check failed; inspect {STDERR_PATH}"


def install(
    path: Path = DEFAULT_PLIST_PATH,
    *,
    run_launchctl: Callable[..., subprocess.CompletedProcess[str]] = _run_launchctl,
    is_port_open: Callable[[], bool] = port_open,
) -> None:
    if not WRAPPER_PATH.is_file():
        raise RuntimeError(f"backend wrapper is missing: {WRAPPER_PATH}")
    loaded = service_loaded(run_launchctl)
    if loaded:
        run_launchctl(["bootout", launch_target()])
        if not wait_until(is_port_open, False, 5.0):
            raise RuntimeError("launchd backend did not release port 8000")
    elif is_port_open():
        raise RuntimeError(
            "port 8000 is already in use by a non-LaunchAgent process; "
            "stop that backend before installing"
        )

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    bootstrapped = False
    try:
        write_plist(path)
        domain = f"gui/{os.getuid()}"
        run_launchctl(["bootstrap", domain, str(path)])
        bootstrapped = True
        run_launchctl(["enable", launch_target()])
        run_launchctl(["kickstart", "-k", launch_target()])
        if not wait_until(health_ok, True, 20.0):
            raise RuntimeError(startup_failure_message())
    except Exception:
        if bootstrapped:
            try:
                run_launchctl(["bootout", launch_target()], check=False)
            except Exception:
                pass
        path.unlink(missing_ok=True)
        raise


def uninstall(
    path: Path = DEFAULT_PLIST_PATH,
    *,
    run_launchctl: Callable[..., subprocess.CompletedProcess[str]] = _run_launchctl,
) -> None:
    if service_loaded(run_launchctl):
        run_launchctl(["bootout", launch_target()])
    path.unlink(missing_ok=True)


def restart(
    *,
    run_launchctl: Callable[..., subprocess.CompletedProcess[str]] = _run_launchctl,
) -> None:
    if not service_loaded(run_launchctl):
        raise RuntimeError("LaunchAgent is not installed")
    run_launchctl(["kickstart", "-k", launch_target()])
    if not wait_until(health_ok, True, 20.0):
        raise RuntimeError(f"backend restart failed; inspect {STDERR_PATH}")


def status_payload() -> dict[str, Any]:
    result = _run_launchctl(["print", launch_target()], check=False)
    loaded = result.returncode == 0
    backend_healthy = health_ok()
    return {
        "label": LABEL,
        "loaded": loaded,
        "managedHealthy": loaded and backend_healthy,
        "backendHealthy": backend_healthy,
        "plistPath": str(DEFAULT_PLIST_PATH),
        "stdoutPath": str(STDOUT_PATH),
        "stderrPath": str(STDERR_PATH),
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Manage the user LaunchAgent that hosts the research-shadow backend."
    )
    parser.add_argument(
        "command", choices=("render", "install", "uninstall", "restart", "status")
    )
    args = parser.parse_args()

    try:
        if args.command == "render":
            sys.stdout.buffer.write(render_plist())
        elif args.command == "install":
            install()
            print(json.dumps(status_payload(), ensure_ascii=False, indent=2))
        elif args.command == "uninstall":
            uninstall()
            print(json.dumps(status_payload(), ensure_ascii=False, indent=2))
        elif args.command == "restart":
            restart()
            print(json.dumps(status_payload(), ensure_ascii=False, indent=2))
        else:
            print(json.dumps(status_payload(), ensure_ascii=False, indent=2))
    except RuntimeError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
