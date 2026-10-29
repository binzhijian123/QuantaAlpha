#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quantaalpha.utils.qlib_provider import candidate_provider_paths  # noqa: E402
from quantaalpha.validation.holdout_exposure import (  # noqa: E402
    write_holdout_exposure_audit,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Inventory historical research exposure without reading return values."
    )
    parser.add_argument("--provider", type=Path)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--minimum-training-sessions", type=int, default=504)
    parser.add_argument("--minimum-holdout-sessions", type=int, default=1008)
    args = parser.parse_args()

    provider = args.provider
    if provider is None:
        candidates = candidate_provider_paths({})
        if not candidates:
            parser.error("no configured Qlib Provider was found")
        provider = candidates[0]
    if not provider.is_absolute():
        provider = PROJECT_ROOT / provider
    output_dir = args.output_dir or (
        PROJECT_ROOT
        / "data"
        / "results"
        / "profit_validation"
        / f"holdout_exposure_audit_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    )
    if not output_dir.is_absolute():
        output_dir = PROJECT_ROOT / output_dir

    result = write_holdout_exposure_audit(
        PROJECT_ROOT,
        provider,
        output_dir,
        minimum_training_sessions=args.minimum_training_sessions,
        minimum_holdout_sessions=args.minimum_holdout_sessions,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 2 if result["status"] == "INDETERMINATE" else 0


if __name__ == "__main__":
    raise SystemExit(main())
