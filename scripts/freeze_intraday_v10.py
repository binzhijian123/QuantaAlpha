#!/usr/bin/env python3
"""Freeze a future V10 declaration from already qualified original evidence.

Never starts workers, edits environment settings or creates a ledger/capture tree.
The CLI uses the real current clock; no historical date override is exposed.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import date, datetime
import hashlib
import json
import os
from pathlib import Path
import re
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
for directory in (PROJECT_ROOT, PROJECT_ROOT / "frontend-v2/backend"):
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))

from market_data import intraday_shadow_v10 as v10
from quantaalpha.utils.qlib_provider import provider_contract_fingerprint

TEMPLATE = PROJECT_ROOT / "configs/intraday_shadow_20260910_t1_reversal_v9.json"
RUNTIME_FILES = {
    "frontend-v2/backend/app.py", "frontend-v2/backend/v10_app.py",
    "frontend-v2/backend/v10_runtime.py", "frontend-v2/backend/system_settings.py",
    "frontend-v2/backend/market_data/v10_provider_maintenance.py",
    "frontend-v2/backend/market_data/slot_aligned_capture_runtime.py",
    "quantaalpha/trading/market_data.py", "quantaalpha/trading/paper.py",
    "quantaalpha/utils/investment_data_release.py",
    "scripts/freeze_intraday_v10.py",
    "scripts/verify_v10_runtime_readiness.py", "scripts/run_v10_backend_supervised.sh",
    "scripts/verify_shadow_provider_session.py",
    "scripts/run_research_shadow_backend_supervised.sh", "scripts/qa_env.sh",
}


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _inside(root, value):
    path = Path(value).expanduser()
    path = (path if path.is_absolute() else root / path).resolve()
    if not path.is_relative_to(root) or path == root:
        raise ValueError("V10 paths must stay inside the project")
    return path


def _new(root, value):
    lexical = Path(value).expanduser()
    lexical = lexical if lexical.is_absolute() else root / lexical
    if os.path.lexists(lexical):
        raise ValueError("V10 output must be previously nonexistent")
    return _inside(root, lexical)


def _overlap(a, b):
    return a == b or a in b.parents or b in a.parents


def _binding(path, reader):
    report = reader(path)
    return {"reportPath": str(path), "fileSha256": _sha(path),
            "reportSha256": report["reportSha256"]}


def freeze_intraday_v10(*, provider, qualification, calibration, first_signal_date,
                        run_id, capture_root, database, declaration,
                        project_root=PROJECT_ROOT, now=None):
    root = Path(project_root).resolve()
    stamp = now if now is not None else datetime.now(v10.SHANGHAI_TZ)
    if stamp.tzinfo is None or stamp.utcoffset() is None:
        raise ValueError("V10 declaration clock must be timezone aware")
    stamp = stamp.astimezone(v10.SHANGHAI_TZ)
    if stamp > datetime.now(v10.SHANGHAI_TZ):
        raise ValueError("V10 declaration cannot be future dated")
    target = date.fromisoformat(first_signal_date)
    if target.isoformat() != first_signal_date or target <= stamp.date():
        raise ValueError("V10 first signal must be a future date")
    if not isinstance(run_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*_v10", run_id):
        raise ValueError("V10 requires an independent runId ending in _v10")
    provider = _inside(root, provider)
    source_path = _inside(root, qualification)
    calibration_path = _inside(root, calibration)
    capture = _new(root, capture_root)
    database = _new(root, database)
    if database.parent.exists():
        raise ValueError("V10 run directory must be new")
    path = _new(root, declaration)
    if (path.parent != root / "configs" or not path.name.startswith("intraday_shadow")
            or path.suffix != ".json"):
        raise ValueError("V10 declaration must use configs/intraday_shadow*.json")
    sidecar = _new(root, str(path) + ".sha256")
    outputs = (capture, database.parent, path, sidecar)
    if any(_overlap(a, b) for i, a in enumerate(outputs) for b in outputs[i+1:]):
        raise ValueError("V10 output paths overlap")
    inputs = (provider, source_path, calibration_path, root / "configs")
    if any(_overlap(output, item) for output in (capture, database.parent) for item in inputs):
        raise ValueError("V10 evidence/run outputs overlap existing inputs")
    template = deepcopy(json.loads(TEMPLATE.read_text(encoding="utf-8")))
    # V9 supplies the unchanged strategy/paper/quality contract, never its run,
    # source evidence, supersession assertions, execution output or runtime.
    template.pop("supersedes", None)
    implementation = v10.capture_v4.capture_implementation_contract()
    from v10_runtime import RUNTIME_CONTRACT
    files = sorted({root / name for name in RUNTIME_FILES | v10.REQUIRED_IMPLEMENTATION_FILES}
                   | {_inside(root, item["path"]) for item in implementation["files"]})
    template.update(
        protocolId=v10.PROTOCOL_ID, runId=run_id, declaredAt=stamp.isoformat(),
        firstEligibleSignalDate=first_signal_date, captureRoot=str(capture),
        databasePath=str(database),
        providerRoot={"path": str(provider), "fingerprint":
                      provider_contract_fingerprint(provider, "csi300")["fingerprint"]},
        calibration=_binding(calibration_path, v10.calibration.read_verified_calibration_report),
        sourceReliability={**deepcopy(v10.SOURCE_RELIABILITY_BASE_CONTRACT),
            "captureImplementationSha256": implementation["sha256"],
            "qualification": _binding(source_path, v10.qualification.read_verified_source_qualification_report)},
        implementation={"files": [{"path": str(item), "sha256": _sha(item)} for item in files]},
        execution=deepcopy(v10.EXECUTION), timing=deepcopy(v10.TIMING),
        causality=deepcopy(v10.CAUSALITY_CONTRACT),
        qualityExtension=deepcopy(v10.QUALITY_EXTENSION_CONTRACT),
        runtime=deepcopy(RUNTIME_CONTRACT),
        captureContract={"pollSeconds": 60, "implementationSha256": implementation["sha256"]},
    )
    raw = (json.dumps(template, sort_keys=True, indent=2, ensure_ascii=False,
                      allow_nan=False) + "\n").encode("utf-8")
    digest = hashlib.sha256(raw).hexdigest()
    writes = ((path, raw), (sidecar, f"{digest}  {path.name}\n".encode("ascii")))
    created = []
    complete = False
    try:
        for output, content in writes:
            output.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            identity = os.fstat(fd)
            created.append((output, identity.st_dev, identity.st_ino))
            with os.fdopen(fd, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
                os.fchmod(stream.fileno(), 0o444)
        # Pure validator rebuilds original source qualification and ALL 242
        # slots; its historical collision check runs before any ledger write.
        checked = v10.validate_v10_declaration(path)
        if (checked["declarationSha256"] != digest
                or any(output.read_bytes() != content for output, content in writes)):
            raise ValueError("V10 frozen declaration failed exact readback")
        complete = True
    finally:
        if not complete:
            for output, device, inode in reversed(created):
                try:
                    identity = output.lstat()
                    if (identity.st_dev, identity.st_ino) == (device, inode):
                        output.unlink()
                except FileNotFoundError:
                    pass
    return {"status": "FROZEN_DECLARATION_ONLY", "declaration": str(path),
            "declarationSha256": digest, "runId": run_id,
            "autoShadowAccepted": False, "workersStarted": False,
            "environmentModified": False}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for field in ("provider", "qualification", "calibration", "capture-root", "database", "declaration"):
        parser.add_argument("--" + field, type=Path, required=True)
    parser.add_argument("--first-signal-date", required=True)
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args(argv)
    try:
        result = freeze_intraday_v10(**vars(args))
    except Exception:
        print(json.dumps({"status": "NOT_FROZEN", "autoShadowAccepted": False,
                          "reason": "V10_INPUTS_NOT_QUALIFIED"}))
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
