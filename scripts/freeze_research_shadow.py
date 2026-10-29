#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quantaalpha.backtest.runner import BacktestRunner


DEFAULT_CONFIG = (
    PROJECT_ROOT / "configs" / "research_shadow_20260812_deltaes1_causal_v2.yaml"
)
DEFAULT_DECLARATION = (
    PROJECT_ROOT
    / "configs"
    / "research_shadow_20260812_deltaes1_causal_v2.declaration.json"
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    temporary.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Fit and freeze the paired research-shadow models without return evaluation."
    )
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    parser.add_argument("--declaration", default=str(DEFAULT_DECLARATION))
    args = parser.parse_args()

    config_path = Path(args.config).expanduser().resolve()
    declaration_path = Path(args.declaration).expanduser().resolve()
    declaration = json.loads(declaration_path.read_text(encoding="utf-8"))
    deadline = datetime.fromisoformat(declaration["initialSignalDeadline"])
    if datetime.now(deadline.tzinfo) >= deadline:
        raise RuntimeError("initial research-shadow freeze deadline has passed")

    manifest_path = Path(declaration["manifestPath"]).expanduser().resolve()
    database_path = Path(declaration["databasePath"]).expanduser().resolve()
    final_snapshots = {
        arm: Path(spec["snapshotPath"]).expanduser().resolve()
        for arm, spec in declaration["arms"].items()
    }
    protected = [manifest_path, database_path]
    protected.extend(final_snapshots.values())
    protected.extend(path.with_suffix(path.suffix + ".sha256") for path in final_snapshots.values())
    existing = [str(path) for path in protected if path.exists()]
    if existing:
        raise FileExistsError(
            "refusing to overwrite research-shadow artifacts: " + ", ".join(existing)
        )

    root = manifest_path.parent
    staging = root.parent / f".{root.name}_freeze_{uuid.uuid4().hex[:8]}"
    staging.mkdir(parents=True, exist_ok=False)
    runner = BacktestRunner(str(config_path))
    frozen: dict[str, dict[str, Any]] = {}
    try:
        for arm in ("B0", "C1"):
            arm_spec = declaration["arms"][arm]
            staged_snapshot = staging / "snapshots" / f"{arm}.pkl"
            result = runner.train_only(
                strategy_snapshot=str(staged_snapshot),
                shadow_declaration=str(declaration_path),
                arm=arm,
                factor_source=arm_spec["factorSource"],
                factor_json=arm_spec.get("factorJson", []),
                skip_uncached=True,
            )
            if result["featureCount"] != int(arm_spec["expectedFeatureCount"]):
                raise RuntimeError(
                    f"{arm} feature count mismatch: "
                    f"{result['featureCount']} != {arm_spec['expectedFeatureCount']}"
                )
            frozen[arm] = {
                **result,
                "snapshotPath": str(final_snapshots[arm]),
            }

        for arm, staged in {
            arm: staging / "snapshots" / f"{arm}.pkl" for arm in frozen
        }.items():
            final = final_snapshots[arm]
            final.parent.mkdir(parents=True, exist_ok=True)
            staged.replace(final)
            staged.with_suffix(staged.suffix + ".sha256").replace(
                final.with_suffix(final.suffix + ".sha256")
            )
            if _sha256(final) != frozen[arm]["snapshotSha256"]:
                raise RuntimeError(f"{arm} snapshot hash changed during finalization")

        manifest = {
            "version": 1,
            "runId": declaration["runId"],
            "status": "FROZEN",
            "usageRealm": "RESEARCH_SHADOW",
            "formalPaperEligible": False,
            "automaticPromotionAllowed": False,
            "historicalStatus": "REJECTED",
            "createdAt": datetime.now(deadline.tzinfo).isoformat(),
            "declarationPath": str(declaration_path),
            "declarationSha256": _sha256(declaration_path),
            "configPath": str(config_path),
            "configSha256": _sha256(config_path),
            "providerPath": declaration["providerPath"],
            "providerFingerprint": declaration["providerFingerprint"],
            "initialSignalDate": declaration["initialSignalDate"],
            "firstTradeDate": declaration["firstTradeDate"],
            "initialSignalDeadline": declaration["initialSignalDeadline"],
            "returnsEvaluatedDuringFreeze": False,
            "arms": frozen,
        }
        _write_json(manifest_path, manifest)
        print(json.dumps(manifest, ensure_ascii=False, indent=2))
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
