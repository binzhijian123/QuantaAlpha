#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quantaalpha.trading.market_data import discover_latest_provider
from quantaalpha.trading.research_shadow_automation import (
    build_research_shadow_lineage_proof,
    resolve_project_path,
)
from quantaalpha.utils.provider_lineage import write_lineage_proof


DEFAULT_DECLARATION = (
    PROJECT_ROOT
    / "configs"
    / "research_shadow_20260812_deltaes1_causal_v2.declaration.json"
)


def _path(value: str | Path) -> Path:
    return resolve_project_path(value)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Prove that a new Qlib Provider only appends sessions to the currently "
            "accepted research-shadow Provider."
        )
    )
    parser.add_argument(
        "--declaration",
        default=str(DEFAULT_DECLARATION),
        help="Frozen RESEARCH_SHADOW declaration JSON.",
    )
    parser.add_argument(
        "--parent-provider",
        help="Accepted parent Provider. Defaults to the active runtime Provider.",
    )
    parser.add_argument("--successor-provider", required=True)
    args = parser.parse_args()

    declaration_path = _path(args.declaration)
    parent = (
        _path(args.parent_provider)
        if args.parent_provider
        else discover_latest_provider().expanduser().resolve()
    )
    successor = _path(args.successor_provider)
    proof, proof_path = build_research_shadow_lineage_proof(
        declaration_path, parent, successor
    )
    artifact = write_lineage_proof(proof_path, proof)
    print(
        json.dumps(
            {
                "status": "PASS",
                "runId": proof["runId"],
                "parentProvider": proof["parentProviderPath"],
                "successorProvider": proof["successorProviderPath"],
                "historyCutoff": proof["historyCutoff"],
                "successorLatestDate": proof["successorLatestDate"],
                "appendedSessions": proof["appendedSessions"],
                "checkedInstruments": proof["checkedInstruments"],
                "checkedFeatureFiles": proof["checkedFeatureFiles"],
                **artifact,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
