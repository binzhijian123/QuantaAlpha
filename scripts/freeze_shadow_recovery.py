#!/usr/bin/env python3
"""Predeclare an isolated future V4 calibration, without enabling any worker."""
from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import date, datetime
import hashlib
import json
import os
from pathlib import Path
import sys
from typing import Any
from zoneinfo import ZoneInfo


PROJECT_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = PROJECT_ROOT / "frontend-v2/backend"
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from market_data import intraday_capture as _capture
from market_data import intraday_capture_v4 as _capture_v4
from market_data.intraday_calibration import (
    calibration_implementation_contract, load_calibration_declaration,
)


_TZ = ZoneInfo("Asia/Shanghai")
_TEMPLATE = "configs/intraday_capture_calibration_v2_20260813.json"
_IMPLEMENTATION_FILES = (
    "frontend-v2/backend/local_app.py", "frontend-v2/backend/system_settings.py",
    "frontend-v2/backend/shadow_recovery.py",
)
_WINDOWS = [["09:30:00", "11:30:00"], ["13:00:00", "15:00:00"]]
_GATES = {
    "maxFutureQuoteLeadSeconds": 5, "maxQuoteAgeSeconds": 120,
    "maximumConsecutiveMissingSlots": 3, "maximumOrphanSnapshots": 0,
    "minimumBenchmarkCoverageRatio": 0.9, "minimumMedianCoreFieldCoverageRatio": 0.95,
    "minimumMedianUsableCoverageRatio": 0.95, "minimumP10UsableCoverageRatio": 0.9,
    "minimumSlotCoverageRatio": 0.9,
}


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _inside(root: Path, value: Path | str) -> Path:
    path = Path(value).expanduser()
    path = (path if path.is_absolute() else root / path).resolve()
    if path == root or not path.is_relative_to(root):
        raise ValueError("recovery paths must stay inside the project")
    return path


def _overlap(left: Path, right: Path) -> bool:
    return left == right or left.is_relative_to(right) or right.is_relative_to(left)


def _new_inside(root: Path, value: Path | str) -> Path:
    lexical = Path(value).expanduser()
    lexical = lexical if lexical.is_absolute() else root / lexical
    if os.path.lexists(lexical):
        raise ValueError("new recovery paths must not already exist, including symlinks")
    return _inside(root, lexical)


def _encoded(payload: dict[str, Any]) -> bytes:
    return (json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n").encode("utf-8")


def _production_capture_contract() -> dict[str, Any]:
    contract = _capture_v4.capture_implementation_contract()
    runtime = contract["runtime"]
    if (runtime.get("clientInjected") is not False
            or runtime.get("entrypointModule") != "market_data.intraday_capture_v4"
            or runtime.get("providerModule") != "market_data.providers.a_stock_data_intraday_v4"
            or runtime.get("providerClass") != "ResponseTimedRealtimeClient"):
        raise ValueError("freezing requires the production V4 capture implementation")
    return contract


def freeze_shadow_recovery(
    provider: Path | str,
    trade_date: str,
    output: Path | str,
    declaration: Path | str,
    calibration_declaration: Path | str,
    *,
    project_root: Path = PROJECT_ROOT,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Write a future calibration declaration and recovery binding as one group.

    ``now`` is a test seam; the CLI always uses the actual timezone-aware clock.
    No environment configuration, capture directory, service or ledger is changed.
    """
    root = project_root.expanduser().resolve()
    checked_at = now if now is not None else datetime.now(_TZ)
    if checked_at.tzinfo is None or checked_at.utcoffset() is None:
        raise ValueError("recovery freezing requires a timezone-aware current time")
    checked_at = checked_at.astimezone(_TZ)
    target = date.fromisoformat(trade_date)
    if target.isoformat() != trade_date or target <= checked_at.date():
        raise ValueError("calibration trade date must be strictly after the current date")
    provider_path = _inside(root, provider)
    output_path = _new_inside(root, output)
    declaration_path = _new_inside(root, declaration)
    calibration_path = _new_inside(root, calibration_declaration)
    if os.path.lexists(output_path) or _overlap(provider_path, output_path):
        raise ValueError("capture output must be a new independent directory")
    targets = [calibration_path, calibration_path.with_suffix(calibration_path.suffix + ".sha256"),
               declaration_path, declaration_path.with_suffix(declaration_path.suffix + ".sha256")]
    if len(set(targets)) != 4 or any(os.path.lexists(path) for path in targets):
        raise ValueError("all declaration paths must be distinct and previously nonexistent")
    if any(_overlap(left, right) for index, left in enumerate(targets) for right in targets[index + 1:]):
        raise ValueError("declaration files cannot be parents of one another")
    if any(_overlap(path, provider_path) or _overlap(path, output_path) for path in targets):
        raise ValueError("declarations cannot overlap Provider or capture directories")
    legacy_outputs = {root / "data/market/a_share/intraday/a_stock_data_csi300"}
    for prior_path in (root / "configs").glob("intraday_shadow*.json"):
        prior = json.loads(prior_path.read_text(encoding="utf-8"))
        if prior.get("captureRoot"):
            legacy_outputs.add(_inside(root, prior["captureRoot"]))
        old_provider = (prior.get("providerRoot") or {}).get("path")
        if old_provider and provider_path == _inside(root, old_provider):
            raise ValueError("calibration Provider must not be a historical run's root")
    if any(_overlap(path, old) for path in [output_path, *targets] for old in legacy_outputs):
        raise ValueError("capture output must not overlap historical capture directories")

    context = _capture._provider_context(provider_path)
    if (len(context["symbols"]) != 301 or _capture.BENCHMARK_SYMBOL not in context["symbols"]
            or target.isoformat() not in context["tradingDates"]
            or date.fromisoformat(context["providerLatestDate"]) > checked_at.date()):
        raise ValueError("Provider universe or frozen future calendar does not qualify")
    implementation = _production_capture_contract()
    binding = _capture._slot_binding(provider=context, implementation=implementation, poll_seconds=60)
    template_path = root / _TEMPLATE
    template, template_sha = load_calibration_declaration(template_path)
    details = deepcopy(template["calibration"])
    if (details["continuousSessionWindows"] != _WINDOWS or details["expectedSlotCount"] != 242
            or details["finalizeAfter"] != "15:05:00" or details["qualityGates"] != _GATES
            or any(type(value) is bool for value in details["qualityGates"].values())):
        raise ValueError("the fixed full-session calibration template has drifted")
    details["tradeDate"] = target.isoformat()
    calibration_implementation = calibration_implementation_contract()
    created_at = checked_at.isoformat()
    calibration = {
        "schemaVersion": 1,
        "protocolId": f"intraday_capture_calibration_v4_{target.strftime('%Y%m%d')}",
        "phase": "CALIBRATION_ONLY", "createdAt": created_at,
        "createdBeforeCurrentTradeDateQuotesObserved": True,
        "profitEvidenceEligible": False, "featureValuesPermitted": False,
        "returnValuesPermitted": False,
        "calibration": details,
        "calibrationImplementationSha256": calibration_implementation["contractSha256"],
        "captureContract": {
            "binding": binding, "pollSeconds": 60, "requestedSymbolCount": 301,
            "usageRealm": "INTRADAY_RESEARCH_RAW",
            "sourceFallbackOrder": ["a-stock-data/tencent-public", "a-stock-data/easy-tdx-public",
                                    "a-stock-data/eastmoney-public", "a-stock-data/sina-public"],
        },
        "template": {"path": _TEMPLATE, "sha256": template_sha},
        "nextPhaseRule": "Calibration only; no trade, historical backfill, profit evidence or automatic promotion. A passing full-day report still requires separate source qualification and a future-run declaration.",
    }
    recovery_files = [{"path": name, "sha256": _sha(root / name)} for name in _IMPLEMENTATION_FILES]
    calibration_raw = _encoded(calibration)
    calibration_sha = hashlib.sha256(calibration_raw).hexdigest()
    recovery = {
        "schemaVersion": 1, "phase": "CALIBRATION_ONLY", "createdAt": created_at,
        "createdBeforeTradeDate": True, "profitEvidenceEligible": False,
        "tradeDate": target.isoformat(), "pollSeconds": 60,
        "providerPath": str(provider_path), "outputRoot": str(output_path),
        "providerBinding": binding,
        "captureImplementationSha256": implementation["sha256"],
        "calibrationDeclaration": str(calibration_path),
        "calibrationDeclarationSha256": calibration_sha,
        "recoveryImplementation": {"files": recovery_files},
    }
    recovery_raw = _encoded(recovery)
    recovery_sha = hashlib.sha256(recovery_raw).hexdigest()
    writes = [
        (calibration_path, calibration_raw),
        (targets[1], f"{calibration_sha}  {calibration_path.name}\n".encode("ascii")),
        (declaration_path, recovery_raw),
        (targets[3], f"{recovery_sha}  {declaration_path.name}\n".encode("ascii")),
    ]
    created: list[tuple[Path, int, int]] = []
    complete = False
    try:
        for path, raw in writes:
            path.parent.mkdir(parents=True, exist_ok=True)
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            identity = os.fstat(descriptor)
            created.append((path, identity.st_dev, identity.st_ino))
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
                os.fchmod(stream.fileno(), 0o444)
        for path, raw in writes:
            if path.read_bytes() != raw:
                raise ValueError("written declaration did not read back exactly")
        if (_production_capture_contract()["sha256"] != implementation["sha256"]
                or calibration_implementation_contract() != calibration_implementation
                or _sha(template_path) != template_sha
                or any(_sha(root / item["path"]) != item["sha256"] for item in recovery_files)
                or _capture._slot_binding(provider=_capture._provider_context(provider_path),
                                           implementation=implementation, poll_seconds=60) != binding):
            raise ValueError("recovery inputs changed while declarations were being frozen")
        complete = True
    finally:
        if not complete:
            for path, device, inode in reversed(created):
                try:
                    identity = path.lstat()
                    if (identity.st_dev, identity.st_ino) == (device, inode):
                        path.unlink()
                except FileNotFoundError:
                    pass
    return {"status": "FROZEN_CALIBRATION_ONLY", "tradeDate": target.isoformat(),
            "declaration": str(declaration_path), "declarationSha256": recovery_sha,
            "calibrationDeclaration": str(calibration_path), "calibrationDeclarationSha256": calibration_sha,
            "captureImplementationSha256": implementation["sha256"],
            "autoShadowAccepted": False, "workersStarted": False, "environmentModified": False}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", required=True, type=Path)
    parser.add_argument("--trade-date", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--declaration", required=True, type=Path)
    parser.add_argument("--calibration-declaration", required=True, type=Path)
    args = parser.parse_args()
    try:
        result = freeze_shadow_recovery(args.provider, args.trade_date, args.output,
                                        args.declaration, args.calibration_declaration)
    except Exception:
        print(json.dumps({"status": "FREEZE_FAILED", "error": "Recovery declarations were not frozen; verify inputs, dependency versions and independent paths."}), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
