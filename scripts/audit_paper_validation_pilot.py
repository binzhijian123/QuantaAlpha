#!/usr/bin/env python3
"""Audit a paper-v3.1 validation-only pilot."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quantaalpha.validation.pilot_audit import (
    audit_paper_validation_pilot,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--config",
        default="configs/paper_validation_pilot_v3_1.yaml",
    )
    parser.add_argument("--trajectory-pool", required=True)
    parser.add_argument("--library", required=True)
    parser.add_argument("--pool-dir", required=True)
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args()
    result = audit_paper_validation_pilot(
        args.config,
        args.trajectory_pool,
        args.library,
        args.pool_dir,
        args.output_dir,
    )
    print(
        json.dumps(
            {
                "status": result["status"],
                "failureIds": result["summary"]["failureIds"],
                "outputDir": result["outputDir"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
