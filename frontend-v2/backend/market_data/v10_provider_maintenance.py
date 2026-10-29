"""Opt-in V10 provider maintenance, isolated from all old daily shadow writers."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta
import fcntl
import hashlib
import json
import os
from pathlib import Path
import stat
from typing import Any, Callable, Iterator, Mapping
import uuid

from quantaalpha.trading.market_data import discover_latest_provider
from quantaalpha.utils.investment_data_release import (
    download_official_release, install_release_provider, load_release_manifest,
    query_official_release_manifest,
)
from quantaalpha.utils.provider_lineage import (
    provider_feature_tree_contract, verify_append_only_provider, write_lineage_proof,
)
from quantaalpha.utils.qlib_provider import provider_contract_fingerprint
from .intraday_capture import BENCHMARK_SYMBOL, _canonical_json_bytes, _provider_context
from .intraday_shadow import PROJECT_ROOT, SHANGHAI_TZ, _resolve
from .intraday_shadow_v10 import validate_v10_declaration
from scripts.verify_shadow_provider_session import verify_session

FIELDS = ("open", "high", "low", "close", "vwap", "volume", "factor")
DEFAULT_DOTENV = PROJECT_ROOT / ".env"
DEFAULT_QLIB_LINK = Path("~/.qlib/qlib_data/cn_data")
_LOADED_FILE_SHA256 = {
    path: hashlib.sha256(path.read_bytes()).hexdigest() for path in (
        Path(__file__).resolve(),
        PROJECT_ROOT / "frontend-v2/backend/market_data/intraday_shadow_v10.py",
        PROJECT_ROOT / "quantaalpha/utils/investment_data_release.py",
        PROJECT_ROOT / "quantaalpha/utils/provider_lineage.py",
        PROJECT_ROOT / "quantaalpha/utils/qlib_provider.py",
        PROJECT_ROOT / "quantaalpha/trading/market_data.py",
        PROJECT_ROOT / "scripts/verify_shadow_provider_session.py",
    )
}


class MaintenanceError(ValueError):
    """Fixed public failure code; upstream exception details are never published."""


def _verify_loaded(validated: Mapping[str, Any]) -> None:
    declared = {_resolve(row["path"]): row["sha256"]
                for row in validated["declaration"]["implementation"]["files"]}
    for path, loaded in _LOADED_FILE_SHA256.items():
        if declared.get(path) != loaded or _hash(path.read_bytes()) != loaded:
            raise MaintenanceError("LOADED_IMPLEMENTATION_DRIFT")


def _continue(hook: Callable[[], bool] | None) -> None:
    try:
        if hook is not None and hook() is not True:
            raise MaintenanceError("RUNTIME_STOPPED_OR_CHANGED")
    except Exception:
        raise MaintenanceError("RUNTIME_STOPPED_OR_CHANGED") from None


def _now(value: datetime | None) -> datetime:
    current = value or datetime.now(SHANGHAI_TZ)
    if current.tzinfo is None or current.utcoffset() is None:
        raise MaintenanceError("INVALID_CLOCK")
    return current.astimezone(SHANGHAI_TZ)


def _hash(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _directory(validated: Mapping[str, Any]) -> Path:
    root = _resolve(validated["declaration"]["databasePath"]).parent / "provider_maintenance"
    if root.resolve() != root:
        raise MaintenanceError("MAINTENANCE_PATH_ALIAS")
    return root


def _replace_bytes(path: Path, raw: bytes, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _write(path: Path, payload: Mapping[str, Any]) -> None:
    body = {key: value for key, value in payload.items() if key != "contentSha256"}
    body["contentSha256"] = _hash(_canonical_json_bytes(body))
    _replace_bytes(path, _canonical_json_bytes(body))


def _read(path: Path) -> dict[str, Any]:
    if path.is_symlink():
        raise MaintenanceError("ARTIFACT_ALIAS")
    value = json.loads(path.read_bytes())
    if not isinstance(value, dict):
        raise MaintenanceError("ARTIFACT_INVALID")
    body = {key: item for key, item in value.items() if key != "contentSha256"}
    if value.get("contentSha256") != _hash(_canonical_json_bytes(body)):
        raise MaintenanceError("ARTIFACT_HASH_MISMATCH")
    return value


def _ownership(payload: Mapping[str, Any], validated: Mapping[str, Any]) -> None:
    if (payload.get("runId") != validated["declaration"]["runId"]
            or payload.get("declarationSha256") != validated["declarationSha256"]):
        raise MaintenanceError("ARTIFACT_OWNERSHIP_MISMATCH")


def _feature_tree(provider: Path) -> dict[str, Any]:
    return provider_feature_tree_contract(provider, market="csi300",
        benchmark=BENCHMARK_SYMBOL, fields=FIELDS)


def _provider(provider: Path, validated: Mapping[str, Any]) -> dict[str, Any]:
    path = provider.expanduser().resolve()
    context = _provider_context(path)
    frozen = validated["providerContext"]
    if context["symbols"] != frozen["symbols"] or context["symbolsSha256"] != frozen["symbolsSha256"]:
        raise MaintenanceError("FROZEN_UNIVERSE_MISMATCH")
    contract = provider_contract_fingerprint(path, "csi300")
    completeness = verify_session(path, context["providerLatestDate"])
    if (completeness["status"] != "PASS" or completeness["validEquityCount"] != 300
            or completeness["benchmarkValid"] is not True):
        raise MaintenanceError("DAILY_SESSION_INCOMPLETE")
    return {"path": str(path), "fingerprint": contract["fingerprint"],
            "latestDate": context["providerLatestDate"],
            "universeSymbolsSha256": context["symbolsSha256"], "featureTree": _feature_tree(path),
            "dailyCompletenessSha256": completeness["reportSha256"],
            "dailyFeatureDigest": completeness["checkedFeatureDigest"]}


def _root_state(validated: Mapping[str, Any]) -> dict[str, Any]:
    root = _resolve(validated["declaration"]["providerRoot"]["path"])
    provider = _provider(root, validated)
    if provider["fingerprint"] != validated["declaration"]["providerRoot"]["fingerprint"]:
        raise MaintenanceError("ROOT_PROVIDER_DRIFT")
    return {"runId": validated["declaration"]["runId"],
            "declarationSha256": validated["declarationSha256"],
            "root": provider, "current": provider, "proofs": []}


@contextmanager
def runtime_lock(declaration_path: Path | str) -> Iterator[dict[str, Any]]:
    """Shared by maintenance, capture and shadow; callers must not nest it."""
    validated = validate_v10_declaration(declaration_path)
    _verify_loaded(validated)
    directory = _directory(validated)
    directory.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(directory / "runtime.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "a+") as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        try:
            yield validated
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _current(validated: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    directory = _directory(validated)
    journal = directory / "activation.json"
    if journal.exists():
        activation = _read(journal)
        _ownership(activation, validated)
        if activation.get("phase") not in {"COMMITTED", "ROLLED_BACK"}:
            raise MaintenanceError("ACTIVATION_RECONCILIATION_REQUIRED")
    initial = _root_state(validated)
    root_state = initial["root"]
    root = Path(root_state["path"])
    pointer = directory / "accepted_provider.json"
    if not pointer.exists():
        state = initial
        current = root_state
    else:
        state = _read(pointer)
        _ownership(state, validated)
        if state.get("root") != root_state:
            raise MaintenanceError("ROOT_FEATURE_TREE_DRIFT")
        current = _provider(Path(state["current"]["path"]), validated)
        if state.get("current") != current:
            raise MaintenanceError("ACCEPTED_PROVIDER_DRIFT")
        if Path(current["path"]) != root:
            if len(state.get("proofs", [])) != 2:
                raise MaintenanceError("ACCEPTED_PROOFS_MISSING")
            fresh = verify_append_only_provider(root, Path(current["path"]),
                market="csi300", benchmark=BENCHMARK_SYMBOL, fields=FIELDS)
            if fresh["successorFeatureTree"] != current["featureTree"]:
                raise MaintenanceError("ACCEPTED_LINEAGE_DRIFT")
        for reference in state.get("proofs", []):
            proof_path = Path(reference["file"])
            if proof_path.parent != directory / "proofs" or proof_path.is_symlink():
                raise MaintenanceError("PROOF_PATH_INVALID")
            raw = proof_path.read_bytes()
            sidecar = proof_path.with_suffix(proof_path.suffix + ".sha256")
            if (_hash(raw) != reference["sha256"]
                    or sidecar.read_text(encoding="ascii").split() != [reference["sha256"]]):
                raise MaintenanceError("PROOF_HASH_MISMATCH")
            proof = json.loads(raw)
            _ownership(proof, validated)
            if (proof.get("successorFingerprint") != current["fingerprint"]
                    or proof.get("successorFeatureTree") != current["featureTree"]
                    or proof.get("fields") != list(FIELDS) or proof.get("status") != "PASS"):
                raise MaintenanceError("PROOF_BINDING_MISMATCH")
        if Path(current["path"]) != root:
            root_proof = json.loads(Path(state["proofs"][0]["file"]).read_bytes())
            if root_proof.get("parentFingerprint") != root_state["fingerprint"]:
                raise MaintenanceError("PROOF_ROOT_MISMATCH")
    if discover_latest_provider().expanduser().resolve() != Path(current["path"]):
        raise MaintenanceError("RUNTIME_PROVIDER_MISMATCH")
    return state, current


def resolve_current_provider(declaration_path: Path | str) -> Path:
    """Read-only accepted provider resolution; runtime actions also take the lock."""
    validated = validate_v10_declaration(declaration_path)
    _verify_loaded(validated)
    _, current = _current(validated)
    return Path(current["path"])


def read_status(declaration_path: Path | str) -> dict[str, Any] | None:
    validated = validate_v10_declaration(declaration_path)
    _verify_loaded(validated)
    path = _directory(validated) / "status.json"
    if not path.exists():
        return None
    status = _read(path)
    _ownership(status, validated)
    try:
        _, current = _current(validated)
        if status.get("currentProviderPath") != current["path"]:
            raise MaintenanceError("STATUS_PROVIDER_MISMATCH")
    except Exception as exc:
        return {**status, "status": "BLOCKED", "runtimeConsistent": False,
                "errorCode": str(exc) if isinstance(exc, MaintenanceError) else "PROVIDER_VERIFICATION_FAILED"}
    return status


def _proof(validated: Mapping[str, Any], proof: Mapping[str, Any], label: str) -> dict[str, str]:
    payload = {**proof, "runId": validated["declaration"]["runId"],
               "declarationSha256": validated["declarationSha256"]}
    tree = proof["successorFeatureTree"]["sha256"]
    path = _directory(validated) / "proofs" / f"{label}_{proof['parentFingerprint']}_{proof['successorFingerprint']}_{tree}.json"
    if path.exists():
        raw = path.read_bytes()
        previous = json.loads(raw)
        if ({k: v for k, v in previous.items() if k != "createdAt"}
                != {k: v for k, v in payload.items() if k != "createdAt"}):
            raise MaintenanceError("PROOF_COLLISION")
        if path.with_suffix(path.suffix + ".sha256").read_text(encoding="ascii").split() != [_hash(raw)]:
            raise MaintenanceError("PROOF_HASH_MISMATCH")
    else:
        write_lineage_proof(path, payload)
        os.chmod(path, 0o444)
        os.chmod(path.with_suffix(path.suffix + ".sha256"), 0o444)
    return {"file": str(path), "sha256": _hash(path.read_bytes())}


def _replace_link(path: Path, target: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.symlink_to(target, target_is_directory=True)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _activate(validated: Mapping[str, Any], state: Mapping[str, Any],
              dotenv: Path, link: Path, should_continue: Callable[[], bool] | None = None) -> None:
    _verify_loaded(validated)
    _continue(should_continue)
    dotenv = dotenv.expanduser().absolute()
    link = link.expanduser().absolute()
    if dotenv.is_symlink() or (link.exists() and not link.is_symlink()):
        raise MaintenanceError("ACTIVATION_TARGET_UNSAFE")
    env_existed = dotenv.exists()
    old_env = dotenv.read_bytes() if env_existed else b""
    old_mode = stat.S_IMODE(dotenv.stat().st_mode) if env_existed else 0o600
    old_link = os.readlink(link) if link.is_symlink() else None
    keys = ("QLIB_DATA_DIR", "QLIB_PROVIDER_URI")
    old_process = {key: os.environ.get(key) for key in keys}
    candidate = str(state["current"]["path"])
    lines = old_env.decode("utf-8").splitlines()
    found = set()
    updated = []
    for line in lines:
        key = line.split("=", 1)[0].strip() if "=" in line else ""
        if key in keys:
            updated.append(f"{key}={candidate}")
            found.add(key)
        else:
            updated.append(line)
    updated.extend(f"{key}={candidate}" for key in keys if key not in found)
    desired_env = ("\n".join(updated) + "\n").encode("utf-8")
    directory = _directory(validated)
    pointer = directory / "accepted_provider.json"
    old_pointer = pointer.read_bytes() if pointer.exists() else None
    journal = {"runId": validated["declaration"]["runId"],
               "declarationSha256": validated["declarationSha256"],
               "phase": "PREPARED", "candidate": candidate,
               "previousEnvSha256": _hash(old_env), "desiredEnvSha256": _hash(desired_env)}
    _write(directory / "activation.json", journal)
    try:
        _continue(should_continue)
        _replace_bytes(dotenv, desired_env)
        for key in keys:
            os.environ[key] = candidate
        _replace_link(link, candidate)
        if (discover_latest_provider().expanduser().resolve() != Path(candidate)
                or link.resolve() != Path(candidate)
                or dotenv.read_bytes() != desired_env
                or any(os.environ.get(key) != candidate for key in keys)):
            raise MaintenanceError("RUNTIME_SWITCH_READBACK_FAILED")
        _verify_loaded(validated)
        _continue(should_continue)
        _write(pointer, state)
        _write(directory / "activation.json", {**journal, "phase": "COMMITTED"})
    except Exception:
        try:
            # Restore only the values written by this transaction. An unrelated
            # concurrent edit is preserved and makes the worker fail closed.
            observed_env = dotenv.read_bytes() if dotenv.exists() else None
            if observed_env == desired_env:
                if env_existed:
                    _replace_bytes(dotenv, old_env, old_mode)
                else:
                    dotenv.unlink()
            elif observed_env != (old_env if env_existed else None):
                raise MaintenanceError("ROLLBACK_OWNERSHIP_LOST")
            for key, value in old_process.items():
                if os.environ.get(key) not in (candidate, value):
                    raise MaintenanceError("ROLLBACK_OWNERSHIP_LOST")
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
            if link.is_symlink() and os.readlink(link) == candidate:
                if old_link is None:
                    link.unlink()
                else:
                    _replace_link(link, old_link)
            elif (os.readlink(link) if link.is_symlink() else None) != old_link:
                raise MaintenanceError("ROLLBACK_OWNERSHIP_LOST")
            if old_pointer is None:
                pointer.unlink(missing_ok=True)
            else:
                _replace_bytes(pointer, old_pointer)
            _write(directory / "activation.json", {**journal, "phase": "ROLLED_BACK"})
        except Exception:
            raise MaintenanceError("ACTIVATION_ROLLBACK_FAILED") from None
        raise MaintenanceError("ACTIVATION_FAILED") from None


def _bindings_match(current: Mapping[str, Any], dotenv: Path, link: Path) -> bool:
    path = current["path"]
    if dotenv.is_symlink() or not dotenv.is_file() or not link.expanduser().is_symlink():
        return False
    values = {}
    for line in dotenv.read_text(encoding="utf-8").splitlines():
        key, separator, value = line.partition("=")
        if separator:
            values[key.strip()] = value.strip().strip("\"'")
    return (all(values.get(key) == path and os.environ.get(key) == path
                for key in ("QLIB_DATA_DIR", "QLIB_PROVIDER_URI"))
            and link.expanduser().resolve() == Path(path)
            and stat.S_IMODE(dotenv.stat().st_mode) == 0o600)


def bootstrap_root_provider(declaration_path: Path | str, *, enabled: bool = False,
                            dotenv_path: Path = DEFAULT_DOTENV,
                            qlib_link: Path = DEFAULT_QLIB_LINK,
                            should_continue: Callable[[], bool] | None = None) -> dict[str, Any]:
    """Explicit first opt-in: select ONLY the declaration-qualified frozen root.

    Existing global providers are not blessed by this operation. An established
    V10 pointer is never reset or replaced by bootstrap; it must still verify.
    """
    result = {"mode": "V10_PROVIDER_MAINTENANCE", "status": "DISABLED",
              "enabled": enabled is True, "runtimeConsistent": False,
              "autoShadowAccepted": False, "errorCode": None}
    if enabled is not True:
        return result
    try:
        _continue(should_continue)
        with runtime_lock(declaration_path) as validated:
            result.update(runId=validated["declaration"]["runId"],
                          declarationSha256=validated["declarationSha256"])
            directory = _directory(validated)
            try:
                _continue(should_continue)
                if (directory / "accepted_provider.json").exists():
                    _, current = _current(validated)
                    status = "ALREADY_INITIALIZED"
                else:
                    state = _root_state(validated)
                    _activate(validated, state, dotenv_path, qlib_link, should_continue)
                    current = state["current"]
                    status = "ROOT_SELECTED"
                result.update(status=status, runtimeConsistent=True,
                    currentProviderPath=current["path"], currentProviderFingerprint=current["fingerprint"],
                    currentProviderLatestDate=current["latestDate"],
                    universeSymbolsSha256=current["universeSymbolsSha256"])
            except Exception as exc:
                result.update(status="BLOCKED", errorCode=str(exc) if isinstance(exc, MaintenanceError)
                              else "PROVIDER_BOOTSTRAP_FAILED")
            return result
    except Exception as exc:
        return {**result, "status": "BLOCKED", "errorCode": str(exc) if isinstance(exc, MaintenanceError)
                else "DECLARATION_NOT_QUALIFIED"}


def run_cycle(declaration_path: Path | str, *, enabled: bool = False,
              poll_seconds: int = 900, now: datetime | None = None,
              release_tag: str | None = None, dotenv_path: Path = DEFAULT_DOTENV,
              qlib_link: Path = DEFAULT_QLIB_LINK,
              should_continue: Callable[[], bool] | None = None) -> dict[str, Any]:
    """One explicit maintenance cycle; never starts or writes an old shadow run."""
    base = {"mode": "V10_PROVIDER_MAINTENANCE", "enabled": bool(enabled),
            "status": "DISABLED", "runtimeConsistent": False,
            "autoShadowAccepted": False, "errorCode": None}
    if enabled is not True:
        return {**base, "enabled": False}
    try:
        _continue(should_continue)
        checked = _now(now)
        if not isinstance(poll_seconds, int) or isinstance(poll_seconds, bool) or not 30 <= poll_seconds <= 86400:
            raise MaintenanceError("INVALID_POLL_INTERVAL")
        with runtime_lock(declaration_path) as validated:
            base.update(runId=validated["declaration"]["runId"],
                declarationSha256=validated["declarationSha256"], checkedAt=checked.isoformat(),
                nextCheckAt=(checked + timedelta(seconds=poll_seconds)).isoformat(), lastSuccessAt=None)
            directory = _directory(validated)
            try:
                _continue(should_continue)
                if (directory / "status.json").exists():
                    previous_status = _read(directory / "status.json")
                    _ownership(previous_status, validated)
                    base["lastSuccessAt"] = previous_status.get("lastSuccessAt")
                state, current = _current(validated)
                base.update(currentProviderPath=current["path"], currentProviderFingerprint=current["fingerprint"],
                    currentProviderLatestDate=current["latestDate"], universeSymbolsSha256=current["universeSymbolsSha256"])
                official = query_official_release_manifest(release_tag)
                if official["target_trade_date"] > checked.date().isoformat():
                    raise MaintenanceError("OFFICIAL_TARGET_IS_FUTURE")
                if official["target_trade_date"] <= current["latestDate"]:
                    if not _bindings_match(current, dotenv_path, qlib_link):
                        _activate(validated, state, dotenv_path, qlib_link, should_continue)
                    elif not (directory / "accepted_provider.json").exists():
                        _verify_loaded(validated)
                        _continue(should_continue)
                        _write(directory / "accepted_provider.json", state)
                    base.update(status="NO_NEW_SESSION", runtimeConsistent=True, lastSuccessAt=checked.isoformat())
                else:
                    source = download_official_release(directory / "releases", official["release_tag"])
                    manifest = load_release_manifest(source / "qlib_bin.manifest.json", expected_tag=official["release_tag"])
                    if manifest != official:
                        raise MaintenanceError("OFFICIAL_MANIFEST_CHANGED")
                    candidate = directory / "candidates" / official["release_tag"]
                    install_release_provider(source, candidate)
                    successor = _provider(candidate, validated)
                    if successor["latestDate"] != official["target_trade_date"]:
                        raise MaintenanceError("CANDIDATE_CALENDAR_MISMATCH")
                    root = Path(state["root"]["path"])
                    root_proof = verify_append_only_provider(root, candidate,
                        market="csi300", benchmark=BENCHMARK_SYMBOL, fields=FIELDS)
                    previous_proof = verify_append_only_provider(Path(current["path"]), candidate,
                        market="csi300", benchmark=BENCHMARK_SYMBOL, fields=FIELDS)
                    if (_provider(root, validated) != state["root"]
                            or _provider(Path(current["path"]), validated) != current
                            or _provider(candidate, validated) != successor):
                        raise MaintenanceError("PROVIDER_CHANGED_DURING_VERIFICATION")
                    proofs = [_proof(validated, root_proof, "root"), _proof(validated, previous_proof, "previous")]
                    state = {"runId": validated["declaration"]["runId"],
                             "declarationSha256": validated["declarationSha256"],
                             "root": state["root"], "current": successor, "proofs": proofs}
                    _activate(validated, state, dotenv_path, qlib_link, should_continue)
                    base.update(status="ADVANCED", runtimeConsistent=True, lastSuccessAt=checked.isoformat(),
                        currentProviderPath=successor["path"], currentProviderFingerprint=successor["fingerprint"],
                        currentProviderLatestDate=successor["latestDate"])
            except Exception as exc:
                base.update(status="BLOCKED", runtimeConsistent=False,
                            errorCode=str(exc) if isinstance(exc, MaintenanceError) else "PROVIDER_MAINTENANCE_FAILED")
            _write(directory / "status.json", base)
            return base
    except Exception as exc:
        return {**base, "status": "BLOCKED", "runtimeConsistent": False,
                "errorCode": str(exc) if isinstance(exc, MaintenanceError) else "DECLARATION_NOT_QUALIFIED"}
