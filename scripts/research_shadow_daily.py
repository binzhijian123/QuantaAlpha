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
from quantaalpha.trading.research_shadow_automation import resolve_project_path
from quantaalpha.trading.unattended_provider_automation import run_cycle_and_record


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Run one fail-closed unattended cycle: maintain official data, "
            "and execute shadow-ledger actions only when the unique next session "
            "remains causal."
        )
    )
    parser.add_argument("--declaration", default=str(DEFAULT_DECLARATION))
    parser.add_argument("--poll-seconds", type=int, default=900)
    args = parser.parse_args()
    if args.poll_seconds < 300:
        raise ValueError("poll-seconds must be at least 300")
    result = run_cycle_and_record(
        resolve_project_path(args.declaration),
        poll_seconds=args.poll_seconds,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
