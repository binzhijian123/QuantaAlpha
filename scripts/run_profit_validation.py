#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

DETERMINISTIC_HASH_SEED = "0"


def _ensure_deterministic_process() -> None:
    if os.environ.get("PYTHONHASHSEED") == DETERMINISTIC_HASH_SEED:
        return
    os.environ["PYTHONHASHSEED"] = DETERMINISTIC_HASH_SEED
    os.execv(
        sys.executable,
        [sys.executable, str(Path(__file__).resolve()), *sys.argv[1:]],
    )


def main() -> int:
    _ensure_deterministic_process()
    from quantaalpha.validation import run_profit_validation

    parser = argparse.ArgumentParser(
        description="Run the frozen QuantaAlpha profit-validation protocol."
    )
    parser.add_argument(
        "--config",
        default="configs/profit_validation.yaml",
        help="Validation YAML path relative to the project root.",
    )
    parser.add_argument(
        "--audit-only",
        action="store_true",
        help="Write data/factor audits without running candidate backtests.",
    )
    args = parser.parse_args()
    config_path = Path(args.config)
    if not config_path.is_absolute():
        config_path = PROJECT_ROOT / config_path
    result = run_profit_validation(PROJECT_ROOT, config_path, audit_only=args.audit_only)
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
