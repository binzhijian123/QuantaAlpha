#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quantaalpha.validation.standard_baseline import run_standard_baseline_validation


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run the frozen Alpha158_20 versus Alpha158 comparison."
    )
    parser.add_argument(
        "--config",
        default="configs/standard_baseline_validation_v1.yaml",
        help="Validation YAML path relative to the project root.",
    )
    parser.add_argument(
        "--audit-only",
        action="store_true",
        help="Freeze and audit the comparison without opening historical returns.",
    )
    args = parser.parse_args()
    config_path = Path(args.config)
    if not config_path.is_absolute():
        config_path = PROJECT_ROOT / config_path
    result = run_standard_baseline_validation(
        PROJECT_ROOT,
        config_path,
        audit_only=args.audit_only,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

