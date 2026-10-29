#!/usr/bin/env python3
"""Freeze a future r4 scheduler binding; no environment or service activation."""
from __future__ import annotations

import argparse
from datetime import datetime
import importlib.util
import json
import os
from pathlib import Path
import sys
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
for directory in (PROJECT_ROOT, PROJECT_ROOT / "frontend-v2/backend"):
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))

from shadow_recovery_r4 import REQUIRED_FILES, metadata, scheduler_contract, _output_isolated
import shadow_recovery_r4 as adapter_module
from scripts import freeze_shadow_recovery as original

_LOADED_SHA = original._sha(Path(__file__).resolve())
_ORIGINAL_SHA = original._sha(Path(original.__file__).resolve())


def freeze_shadow_recovery_r4(provider: Path | str, trade_date: str, output: Path | str,
        declaration: Path | str, calibration_declaration: Path | str, *,
        project_root: Path = PROJECT_ROOT, now: datetime | None = None) -> dict[str, Any]:
    root = project_root.expanduser().resolve()
    _output_isolated(root, original._new_inside(root, output))
    if (original._sha(Path(__file__)) != _LOADED_SHA
            or original._sha(Path(original.__file__)) != _ORIGINAL_SHA):
        raise ValueError("r4 freezer loaded implementation changed")
    scheduler = scheduler_contract()
    files = sorted({str(original._inside(root, name).relative_to(root))
                    for name in [*REQUIRED_FILES, *(row["path"] for row in scheduler["files"])]})
    expected = {name: original._sha(root / name) for name in files}
    if expected["scripts/freeze_shadow_recovery_r4.py"] != _LOADED_SHA or expected["scripts/freeze_shadow_recovery.py"] != _ORIGINAL_SHA:
        raise ValueError("r4 freezer implementation does not match loaded source")
    if (expected["frontend-v2/backend/shadow_recovery_r4.py"] != adapter_module._LOADED_SHA
            or expected["frontend-v2/backend/shadow_recovery.py"] != adapter_module._BASE_LOADED_SHA):
        raise ValueError("r4 adapter implementation does not match loaded source")
    # The frozen builder remains untouched. A private module instance shares its
    # proven path/template/O_EXCL/rollback implementation, with an r4 serializer.
    spec = importlib.util.spec_from_file_location("_private_r4_freezer", original.__file__)
    builder = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(builder)
    builder._IMPLEMENTATION_FILES = tuple(files)
    encode = builder._encoded
    def encoded(payload):
        value = {**payload, **metadata(scheduler["sha256"])}
        if "recoveryImplementation" in value:
            value["schemaVersion"] = 4
        elif "calibration" in value:
            value["protocolId"] += "_slot_aligned_r4"
        return encode(value)
    builder._encoded = encoded
    owned = []
    class PrivateOS:
        def __getattr__(self, name):
            return getattr(os, name)
        def open(self, path, flags, mode=0o777):
            descriptor = os.open(path, flags, mode)
            if flags & os.O_EXCL and flags & os.O_CREAT:
                identity = os.fstat(descriptor)
                owned.append((Path(path), identity.st_dev, identity.st_ino))
            return descriptor
    builder.os = PrivateOS()
    complete = False
    try:
        result = builder.freeze_shadow_recovery(provider, trade_date, output, declaration,
            calibration_declaration, project_root=root, now=now)
        if scheduler_contract() != scheduler or any(original._sha(root / name) != digest for name, digest in expected.items()):
            raise ValueError("r4 inputs changed during freeze")
        for key in ("declaration", "calibrationDeclaration"):
            path = Path(result[key])
            payload = json.loads(path.read_bytes())
            if {name: payload.get(name) for name in metadata(scheduler["sha256"])} != metadata(scheduler["sha256"]):
                raise ValueError("r4 serialized binding did not read back")
            directory = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        complete = True
        return {**result, "recoveryVersion": 4, "runtimeEntrypoint": "recovery_r4_app",
                "schedulerImplementationSha256": scheduler["sha256"]}
    finally:
        if not complete:
            for path, device, inode in reversed(owned):
                try:
                    current = path.lstat()
                    if (current.st_dev, current.st_ino) == (device, inode):
                        path.unlink()
                except FileNotFoundError:
                    pass


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise ValueError("invalid r4 arguments")


def main(argv: list[str] | None = None) -> int:
    try:
        parser = _Parser(description=__doc__)
        for name in ("provider", "output", "declaration", "calibration-declaration"):
            parser.add_argument("--" + name, required=True, type=Path)
        parser.add_argument("--trade-date", required=True)
        args = parser.parse_args(argv)
        result = freeze_shadow_recovery_r4(args.provider, args.trade_date, args.output,
                                           args.declaration, args.calibration_declaration)
    except Exception:
        print(json.dumps({"status": "FREEZE_FAILED", "error": "R4 declarations were not frozen; verify future date, bindings and independent paths."}), file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
