#!/usr/bin/env python3
"""Run the zero-LLM QuantaAlpha paper-validation preflight."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quantaalpha.validation.paper import run_preflight


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--config",
        default="configs/paper_validation_v3.yaml",
        help="Paper-validation protocol YAML",
    )
    parser.add_argument("--run-id", default=None)
    args = parser.parse_args()
    result = run_preflight(args.config, run_id=args.run_id)
    print(
        json.dumps(
            {
                "runId": result["runId"],
                "status": result["status"],
                "outputDir": result["outputDir"],
                "hardFailureIds": result["hardFailureIds"],
                "warningIds": result["warningIds"],
                "llmCalls": result["llmCalls"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 1 if result["status"] == "BLOCKED" else 0


if __name__ == "__main__":
    raise SystemExit(main())
