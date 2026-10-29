#!/usr/bin/env python3
from __future__ import annotations

import argparse
import copy
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _resolve(value: str) -> Path:
    path = Path(value).expanduser()
    return path.resolve() if path.is_absolute() else (PROJECT_ROOT / path).resolve()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _factor_id(namespace: str, name: str, expression: str) -> str:
    value = f"{namespace}:{name}:{expression}"
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def _has_consistency_proof(info: dict[str, Any]) -> bool:
    gate = info.get("quality_gate") or {}
    evidence = gate.get("consistency_check") or {}
    return (
        gate.get("consistency_enabled") is True
        and gate.get("consistency_passed") is True
        and evidence.get("is_consistent") is True
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build an immutable old-plus-preregistered profit trial universe."
    )
    parser.add_argument("--base", required=True)
    parser.add_argument("--candidates", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    base_path = _resolve(args.base)
    candidates_path = _resolve(args.candidates)
    output_path = _resolve(args.output)
    if output_path.exists():
        raise FileExistsError(f"refusing to overwrite immutable artifact: {output_path}")

    base_payload = json.loads(base_path.read_text(encoding="utf-8"))
    candidate_payload = json.loads(candidates_path.read_text(encoding="utf-8"))
    base_factors = base_payload.get("factors") or {}
    candidate_factors = candidate_payload.get("factors") or {}
    metadata = candidate_payload.get("metadata") or {}
    namespace = str(metadata.get("factor_id_namespace") or "")
    admission_ids = [str(item) for item in metadata.get("admission_factor_ids") or []]

    if not base_factors or not candidate_factors:
        raise ValueError("base and candidate factor sets must both be non-empty")
    if not namespace:
        raise ValueError("candidate factor_id_namespace is required")
    if admission_ids != list(candidate_factors):
        raise ValueError("admission_factor_ids must exactly match candidate order")
    if len(admission_ids) != len(set(admission_ids)):
        raise ValueError("candidate factor IDs must be unique")

    existing_names = {
        str(info.get("factor_name") or factor_id)
        for factor_id, info in base_factors.items()
    }
    existing_expressions = {
        str(info.get("factor_expression") or "")
        for info in base_factors.values()
    }
    for factor_id, info in candidate_factors.items():
        name = str(info.get("factor_name") or "")
        expression = str(info.get("factor_expression") or "")
        if factor_id != str(info.get("factor_id") or ""):
            raise ValueError(f"candidate factor_id field mismatch: {factor_id}")
        if factor_id != _factor_id(namespace, name, expression):
            raise ValueError(f"candidate factor ID is not deterministic: {factor_id}")
        if (info.get("feedback") or {}).get("decision") is True:
            raise ValueError(f"preregistered candidate must not fake acceptance: {factor_id}")
        if not _has_consistency_proof(info):
            raise ValueError(f"candidate lacks frozen consistency proof: {factor_id}")
        if factor_id in base_factors or name in existing_names or expression in existing_expressions:
            raise ValueError(f"candidate duplicates the base universe: {factor_id}")
        existing_names.add(name)
        existing_expressions.add(expression)

    factors = copy.deepcopy(base_factors)
    factors.update(copy.deepcopy(candidate_factors))
    created_at = datetime.now(timezone.utc).isoformat()
    output = {
        "metadata": {
            "schema_version": "profit_trial_universe_source_v1",
            "protocol_version": metadata.get("protocol_version"),
            "created_at": created_at,
            "preparation_mode": "trial_universe_source",
            "source_library": str(base_path),
            "source_library_sha256": _sha256(base_path),
            "candidate_registry": str(candidates_path),
            "candidate_registry_sha256": _sha256(candidates_path),
            "base_factor_count": len(base_factors),
            "registered_candidate_count": len(candidate_factors),
            "total_factors": len(factors),
            "admission_factor_ids": admission_ids,
            "selection_rule": metadata.get("selection_rule"),
            "historical_evidence_only": True,
        },
        "factors": factors,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_path.with_suffix(output_path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(output, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary.replace(output_path)
    print(
        json.dumps(
            {
                "status": "PASS",
                "output": str(output_path),
                "baseFactors": len(base_factors),
                "candidateFactors": len(candidate_factors),
                "totalFactors": len(factors),
                "admissionFactorIds": admission_ids,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
