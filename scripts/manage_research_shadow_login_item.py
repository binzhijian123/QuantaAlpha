#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import plistlib
import shlex
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any, Callable


PROJECT_ROOT = Path(__file__).resolve().parents[1]
APP_NAME = "QuantaAlpha Research Shadow Startup"
BUNDLE_ID = "com.quantaalpha.research-shadow-startup"
APP_BUILD_VERSION = "5"
START_TIMEOUT_SECONDS = 60
AUTH_TOKEN_PLACEHOLDER = "__QUANTAALPHA_LOGIN_AUTH_TOKEN__"
APP_PATH = Path.home() / "Applications" / f"{APP_NAME}.app"
LOG_PATH = Path.home() / "Library" / "Logs" / "QuantaAlpha" / "login-item.log"
AUTH_MARKER = (
    Path.home()
    / "Library"
    / "Application Support"
    / "QuantaAlpha"
    / "login-item-authorized"
)
MANAGER_PATH = PROJECT_ROOT / "scripts" / "manage_research_shadow_supervisor.py"
LOGIN_MANAGER_PATH = Path(__file__).resolve()
SUPERVISOR_PID_FILE = (
    PROJECT_ROOT / "data" / "results" / "research-shadow-backend-supervisor.pid"
)
BACKEND_HEALTH_URL = "http://127.0.0.1:8000/api/health/ready"
PYTHON_BIN = Path(
    os.environ.get(
        "QUANTAALPHA_PYTHON_BIN",
        str(Path.home() / "miniforge3" / "envs" / "quantaalpha" / "bin" / "python"),
    )
)
UNUSED_USAGE_DESCRIPTION_KEYS = (
    "NSAppleEventsUsageDescription",
    "NSAppleMusicUsageDescription",
    "NSCalendarsUsageDescription",
    "NSCameraUsageDescription",
    "NSContactsUsageDescription",
    "NSHomeKitUsageDescription",
    "NSMicrophoneUsageDescription",
    "NSPhotoLibraryUsageDescription",
    "NSRemindersUsageDescription",
    "NSSiriUsageDescription",
    "NSSystemAdministrationUsageDescription",
)


def source_sha256(source: str) -> str:
    return hashlib.sha256(source.encode("utf-8")).hexdigest()


def launcher_source(
    *,
    python_bin: Path = PYTHON_BIN,
    manager_path: Path = MANAGER_PATH,
    log_path: Path = LOG_PATH,
    auth_marker: Path = AUTH_MARKER,
    login_manager_path: Path = LOGIN_MANAGER_PATH,
    start_timeout_seconds: int = START_TIMEOUT_SECONDS,
    authorization_token_value: str | None = None,
) -> str:
    timeout_seconds = max(1, int(start_timeout_seconds))
    authorization_token_value = (
        authorization_token_value
        if authorization_token_value is not None
        else launcher_authorization_token(
            python_bin=python_bin,
            manager_path=manager_path,
            log_path=log_path,
            auth_marker=auth_marker,
            login_manager_path=login_manager_path,
            start_timeout_seconds=timeout_seconds,
        )
    )
    command = "; ".join(
        (
            "umask 077",
            f"/bin/mkdir -p {shlex.quote(str(log_path.parent))}",
            f"/bin/mkdir -p {shlex.quote(str(auth_marker.parent))}",
            "manager_pid=",
            (
                "cleanup_manager() { "
                'if [ -n "$manager_pid" ] && kill -0 "$manager_pid" 2>/dev/null; '
                "then kill -TERM \"$manager_pid\" 2>/dev/null || true; "
                "/bin/sleep 1; "
                "kill -KILL \"$manager_pid\" 2>/dev/null || true; "
                "fi; }"
            ),
            "trap cleanup_manager EXIT HUP INT TERM",
            (
                f"{shlex.quote(str(python_bin))} "
                f"{shlex.quote(str(manager_path))} start "
                f">> {shlex.quote(str(log_path))} 2>&1 & manager_pid=$!"
            ),
            "attempt=0",
            (
                'while kill -0 "$manager_pid" 2>/dev/null; do '
                f'if [ "$attempt" -ge {timeout_seconds} ]; then exit 124; fi; '
                "/bin/sleep 1; attempt=$((attempt + 1)); done"
            ),
            'wait "$manager_pid"',
            "exit_code=$?",
            "manager_pid=",
            "trap - EXIT HUP INT TERM",
            (
                'if [ "$exit_code" -eq 0 ]; '
                f"then {shlex.quote(str(python_bin))} "
                f"{shlex.quote(str(login_manager_path))} confirm-launch "
                f"--token {shlex.quote(authorization_token_value)} "
                f">> {shlex.quote(str(log_path))} 2>&1; "
                'else exit "$exit_code"; fi'
            ),
        )
    )
    escaped = command.replace("\\", "\\\\").replace('"', '\\"')
    return f'on run\n    do shell script "{escaped}"\nend run\n'


def launcher_authorization_token(
    *,
    python_bin: Path = PYTHON_BIN,
    manager_path: Path = MANAGER_PATH,
    log_path: Path = LOG_PATH,
    auth_marker: Path = AUTH_MARKER,
    login_manager_path: Path = LOGIN_MANAGER_PATH,
    start_timeout_seconds: int = START_TIMEOUT_SECONDS,
) -> str:
    contract_source = launcher_source(
        python_bin=python_bin,
        manager_path=manager_path,
        log_path=log_path,
        auth_marker=auth_marker,
        login_manager_path=login_manager_path,
        start_timeout_seconds=start_timeout_seconds,
        authorization_token_value=AUTH_TOKEN_PLACEHOLDER,
    )
    return source_sha256(contract_source)


def _run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, check=True, text=True, **kwargs)


def bundle_identifier(
    app_path: Path,
    *,
    run: Callable[..., subprocess.CompletedProcess[str]] = _run,
) -> str | None:
    plist = app_path / "Contents" / "Info.plist"
    if not plist.is_file():
        return None
    result = run(
        ["/usr/bin/plutil", "-extract", "CFBundleIdentifier", "raw", str(plist)],
        capture_output=True,
    )
    return result.stdout.strip() or None


def _normalized_applescript(source: str) -> str:
    return "\n".join(line.strip() for line in source.splitlines() if line.strip())


def verify_app_bundle(
    app_path: Path = APP_PATH,
    *,
    run: Callable[..., subprocess.CompletedProcess[str]] = _run,
) -> dict[str, Any]:
    expected_source = launcher_source()
    expected_source_hash = source_sha256(expected_source)
    plist_path = app_path / "Contents" / "Info.plist"
    main_script = app_path / "Contents" / "Resources" / "Scripts" / "main.scpt"
    checks = {
        "appExists": app_path.is_dir(),
        "plistReadable": False,
        "bundleIdentifierMatches": False,
        "displayNameMatches": False,
        "buildVersionMatches": False,
        "launcherSourceHashMatches": False,
        "decompiledLauncherMatches": False,
        "codeSignatureValid": False,
        "documentsDescriptionPresent": False,
        "unusedUsageDescriptionsAbsent": False,
        "mainScriptExists": main_script.is_file(),
        "pythonExists": PYTHON_BIN.is_file(),
        "managerExists": MANAGER_PATH.is_file(),
    }
    plist: dict[str, Any] = {}
    if plist_path.is_file():
        try:
            with plist_path.open("rb") as handle:
                plist = plistlib.load(handle)
            checks["plistReadable"] = True
        except (OSError, plistlib.InvalidFileException):
            pass

    checks["bundleIdentifierMatches"] = (
        plist.get("CFBundleIdentifier") == BUNDLE_ID
    )
    checks["displayNameMatches"] = plist.get("CFBundleDisplayName") == APP_NAME
    checks["buildVersionMatches"] = (
        plist.get("QuantaAlphaLauncherBuildVersion") == APP_BUILD_VERSION
    )
    checks["launcherSourceHashMatches"] = (
        plist.get("QuantaAlphaLauncherSourceSha256") == expected_source_hash
    )
    checks["documentsDescriptionPresent"] = bool(
        str(plist.get("NSDocumentsFolderUsageDescription") or "").strip()
    )
    checks["unusedUsageDescriptionsAbsent"] = not any(
        key in plist for key in UNUSED_USAGE_DESCRIPTION_KEYS
    )

    if checks["appExists"]:
        try:
            run(
                ["/usr/bin/codesign", "--verify", "--deep", "--strict", str(app_path)],
                capture_output=True,
            )
            checks["codeSignatureValid"] = True
        except (OSError, subprocess.CalledProcessError):
            pass
        try:
            decompiled = run(
                ["/usr/bin/osadecompile", str(app_path)],
                capture_output=True,
            ).stdout
            checks["decompiledLauncherMatches"] = (
                _normalized_applescript(decompiled)
                == _normalized_applescript(expected_source)
            )
        except (OSError, subprocess.CalledProcessError):
            pass

    failures = [name for name, passed in checks.items() if not passed]
    return {
        "valid": not failures,
        "checks": checks,
        "failures": failures,
        "bundleId": plist.get("CFBundleIdentifier"),
        "buildVersion": plist.get("QuantaAlphaLauncherBuildVersion"),
        "launcherSourceSha256": plist.get("QuantaAlphaLauncherSourceSha256"),
    }


def current_boot_session_sha256(
    *,
    run: Callable[..., subprocess.CompletedProcess[str]] = _run,
) -> str:
    boot_time = run(
        ["/usr/sbin/sysctl", "-n", "kern.boottime"],
        capture_output=True,
    ).stdout.strip()
    if not boot_time:
        raise RuntimeError("macOS boot session is unavailable")
    return source_sha256(boot_time)


def _backend_health() -> dict[str, Any]:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(BACKEND_HEALTH_URL, timeout=5) as response:
        payload = json.load(response)
    if not isinstance(payload, dict):
        raise RuntimeError("backend health payload is invalid")
    return payload


def load_launch_receipt(marker: Path | None = None) -> dict[str, Any] | None:
    marker = marker or AUTH_MARKER
    try:
        payload = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _write_launch_receipt(marker: Path, receipt: dict[str, Any]) -> None:
    marker.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{marker.name}.",
        dir=marker.parent,
        text=True,
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            descriptor = -1
            json.dump(receipt, handle, ensure_ascii=True, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, marker)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


def record_login_launch_receipt(
    authorization_token_value: str,
    *,
    marker: Path | None = None,
    app_path: Path = APP_PATH,
    supervisor_pid_file: Path = SUPERVISOR_PID_FILE,
) -> dict[str, Any]:
    marker = marker or AUTH_MARKER
    expected_token = launcher_authorization_token()
    if authorization_token_value != expected_token:
        raise RuntimeError("login launch authorization token does not match")

    verification = verify_app_bundle(app_path)
    if not verification["valid"]:
        failures = ", ".join(verification["failures"])
        raise RuntimeError(f"login app integrity check failed: {failures}")

    health = _backend_health()
    required_workers = (
        "researchShadowWatcher",
        "intradayCapture",
        "intradayShadow",
    )
    workers = health.get("workers") or {}
    backend_ready = health.get("status") == "healthy" and health.get("ready") is True
    workers_ready = all(
        isinstance(workers.get(name), dict)
        and workers[name].get("ready") is True
        and workers[name].get("taskAlive") is True
        and int(workers[name].get("consecutiveErrors") or 0) == 0
        for name in required_workers
    )
    if not backend_ready or not workers_ready:
        raise RuntimeError("backend workers are not ready for login launch confirmation")

    try:
        supervisor_pid = int(
            supervisor_pid_file.read_text(encoding="ascii").strip()
        )
        os.kill(supervisor_pid, 0)
    except (OSError, TypeError, ValueError) as exc:
        raise RuntimeError("research-shadow supervisor is not running") from exc

    receipt = {
        "version": 1,
        "recordedAt": datetime.now().astimezone().isoformat(),
        "authorizationToken": expected_token,
        "appBuildVersion": APP_BUILD_VERSION,
        "launcherSourceSha256": source_sha256(launcher_source()),
        "bootSessionSha256": current_boot_session_sha256(),
        "supervisorPid": supervisor_pid,
        "backendPid": health.get("pid"),
        "backendInstanceId": health.get("instanceId"),
        "backendReady": True,
        "workersReady": True,
    }
    _write_launch_receipt(marker, receipt)
    return receipt


def authorization_confirmed(
    marker: Path | None = None,
    *,
    expected_token: str | None = None,
) -> bool:
    receipt = load_launch_receipt(marker)
    token = expected_token or launcher_authorization_token()
    return bool(
        receipt
        and receipt.get("version") == 1
        and receipt.get("authorizationToken") == token
        and receipt.get("appBuildVersion") == APP_BUILD_VERSION
        and receipt.get("launcherSourceSha256")
        == source_sha256(launcher_source())
        and receipt.get("backendReady") is True
        and receipt.get("workersReady") is True
        and isinstance(receipt.get("supervisorPid"), int)
        and isinstance(receipt.get("bootSessionSha256"), str)
        and len(receipt["bootSessionSha256"]) == 64
    )


def current_boot_launch_confirmed(marker: Path | None = None) -> bool:
    receipt = load_launch_receipt(marker)
    if not authorization_confirmed(marker) or receipt is None:
        return False
    try:
        return receipt.get("bootSessionSha256") == current_boot_session_sha256()
    except (OSError, RuntimeError, subprocess.CalledProcessError):
        return False


def build_app(app_path: Path = APP_PATH) -> Path:
    if not PYTHON_BIN.is_file():
        raise RuntimeError(f"QuantaAlpha Python is missing: {PYTHON_BIN}")
    if not MANAGER_PATH.is_file():
        raise RuntimeError(f"supervisor manager is missing: {MANAGER_PATH}")

    source = launcher_source()
    source_hash = source_sha256(source)
    if app_path.is_dir():
        try:
            existing_id = bundle_identifier(app_path)
        except subprocess.CalledProcessError as exc:
            raise RuntimeError(f"cannot verify existing login app: {app_path}") from exc
        if existing_id != BUNDLE_ID:
            raise RuntimeError(f"refusing to replace an unrelated app: {app_path}")
        if verify_app_bundle(app_path)["valid"]:
            return app_path

    app_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=app_path.parent) as tmp:
        staged = Path(tmp) / app_path.name
        _run(["/usr/bin/osacompile", "-o", str(staged), "-e", source])
        plist = staged / "Contents" / "Info.plist"
        for key, value in (
            ("CFBundleIdentifier", BUNDLE_ID),
            ("CFBundleDisplayName", APP_NAME),
            ("NSDocumentsFolderUsageDescription", "QuantaAlpha needs access to its repository to start the research simulator."),
            ("QuantaAlphaLauncherBuildVersion", APP_BUILD_VERSION),
            ("QuantaAlphaLauncherSourceSha256", source_hash),
        ):
            _run(["/usr/bin/plutil", "-replace", key, "-string", value, str(plist)])
        for key in UNUSED_USAGE_DESCRIPTION_KEYS:
            subprocess.run(
                ["/usr/bin/plutil", "-remove", key, str(plist)],
                check=False,
                text=True,
                capture_output=True,
            )
        _run(["/usr/bin/codesign", "--force", "--deep", "--sign", "-", str(staged)])
        verification = verify_app_bundle(staged)
        if not verification["valid"]:
            failures = ", ".join(verification["failures"])
            raise RuntimeError(f"built login app failed verification: {failures}")
        if app_path.exists():
            shutil.rmtree(app_path)
        staged.rename(app_path)
    return app_path


def login_item_registered(
    *,
    run: Callable[..., subprocess.CompletedProcess[str]] = _run,
) -> bool:
    script = (
        'tell application "System Events" to return '
        f'exists login item "{APP_NAME}"'
    )
    result = run(["/usr/bin/osascript", "-e", script], capture_output=True)
    return result.stdout.strip().lower() == "true"


def register_login_item(
    app_path: Path = APP_PATH,
    *,
    run: Callable[..., subprocess.CompletedProcess[str]] = _run,
) -> None:
    script = (
        'tell application "System Events"\n'
        f'if exists login item "{APP_NAME}" then delete login item "{APP_NAME}"\n'
        f'make login item at end with properties {{name:"{APP_NAME}", path:"{app_path}", hidden:true}}\n'
        "end tell"
    )
    run(["/usr/bin/osascript", "-e", script], capture_output=True)


def unregister_login_item(
    *,
    run: Callable[..., subprocess.CompletedProcess[str]] = _run,
) -> None:
    script = (
        'tell application "System Events"\n'
        f'if exists login item "{APP_NAME}" then delete login item "{APP_NAME}"\n'
        "end tell"
    )
    run(["/usr/bin/osascript", "-e", script], capture_output=True)


def status_payload(*, query_login_item: bool = True) -> dict[str, Any]:
    verification = verify_app_bundle(APP_PATH)
    receipt = load_launch_receipt()
    registered: bool | None
    permission_error: str | None = None
    if query_login_item:
        try:
            registered = login_item_registered()
        except subprocess.CalledProcessError:
            registered = None
            permission_error = "System Events access is not authorized"
    else:
        registered = None
    return {
        "appPath": str(APP_PATH),
        "appExists": verification["checks"]["appExists"],
        "appValid": verification["valid"],
        "appVerificationFailures": verification["failures"],
        "appVerification": verification["checks"],
        "bundleId": verification["bundleId"],
        "buildVersion": verification["buildVersion"],
        "launcherSourceSha256": verification["launcherSourceSha256"],
        "loginItemRegistered": registered,
        "permissionError": permission_error,
        "logPath": str(LOG_PATH),
        "authorizationConfirmed": authorization_confirmed(),
        "currentBootLaunchConfirmed": current_boot_launch_confirmed(),
        "lastLaunchReceipt": (
            {
                "version": receipt.get("version"),
                "recordedAt": receipt.get("recordedAt"),
                "appBuildVersion": receipt.get("appBuildVersion"),
                "launcherSourceSha256": receipt.get("launcherSourceSha256"),
                "bootSessionSha256": receipt.get("bootSessionSha256"),
                "supervisorPid": receipt.get("supervisorPid"),
                "backendPid": receipt.get("backendPid"),
                "backendInstanceId": receipt.get("backendInstanceId"),
                "backendReady": receipt.get("backendReady"),
                "workersReady": receipt.get("workersReady"),
            }
            if receipt
            else None
        ),
        "requiresDocumentsApproval": True,
    }


def install() -> None:
    app_path = build_app()
    AUTH_MARKER.unlink(missing_ok=True)
    _run(["/usr/bin/open", "-W", str(app_path)])
    if not authorization_confirmed():
        raise RuntimeError(
            "login app did not confirm repository access; allow Documents access "
            f"and inspect {LOG_PATH}"
        )
    register_login_item(app_path)


def uninstall() -> None:
    unregister_login_item()
    if APP_PATH.is_dir():
        if bundle_identifier(APP_PATH) != BUNDLE_ID:
            raise RuntimeError(f"refusing to remove an unrelated app: {APP_PATH}")
        shutil.rmtree(APP_PATH)
    AUTH_MARKER.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Manage the login item that starts the research-shadow supervisor."
    )
    parser.add_argument(
        "command",
        choices=("build", "install", "uninstall", "status", "confirm-launch"),
    )
    parser.add_argument("--token", help=argparse.SUPPRESS)
    args = parser.parse_args()
    try:
        if args.command == "confirm-launch":
            if not args.token:
                raise RuntimeError("confirm-launch requires an authorization token")
            receipt = record_login_launch_receipt(args.token)
            safe_receipt = {
                key: value
                for key, value in receipt.items()
                if key != "authorizationToken"
            }
            print(json.dumps(safe_receipt, ensure_ascii=False, indent=2))
            return 0
        if args.command == "build":
            build_app()
        elif args.command == "install":
            install()
        elif args.command == "uninstall":
            uninstall()
        print(
            json.dumps(
                status_payload(query_login_item=args.command != "build"),
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    except (OSError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
