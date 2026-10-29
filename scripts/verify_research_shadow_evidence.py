#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quantaalpha.trading.research_shadow import DEFAULT_DECLARATION
from quantaalpha.trading.research_shadow_evidence import (
    ensure_evidence_checkpoint,
    validate_evidence_checkpoint_chain,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Create or verify the tamper-evident research-shadow evidence chain."
    )
    parser.add_argument("--declaration", default=str(DEFAULT_DECLARATION))
    parser.add_argument(
        "--create",
        action="store_true",
        help="Create a checkpoint for the current material state before verification.",
    )
    args = parser.parse_args()
    declaration = Path(args.declaration).expanduser().resolve()
    try:
        created = ensure_evidence_checkpoint(declaration) if args.create else None
        verified = validate_evidence_checkpoint_chain(declaration)
    except Exception as exc:
        print(
            json.dumps(
                {"valid": False, "error": str(exc)},
                ensure_ascii=False,
                indent=2,
            ),
            file=sys.stderr,
        )
        return 1
    print(
        json.dumps(
            {"created": created, "verified": verified},
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
