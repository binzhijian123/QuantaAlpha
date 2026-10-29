#!/usr/bin/env python3
"""Build the immutable validation-only paper-v3 factor pool."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quantaalpha.validation.paper_pool import build_paper_factor_pool


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--config",
        default="configs/paper_validation_v3.yaml",
    )
    parser.add_argument("--library", required=True)
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args()
    result = build_paper_factor_pool(
        args.config,
        args.library,
        args.output_dir,
    )
    print(
        json.dumps(
            {
                "outputDir": result["outputDir"],
                "selectedLibrary": result["selectedLibrary"],
                "selectedCount": result["selection"]["selectedCount"],
                "poolLimit": result["selection"]["poolLimit"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
