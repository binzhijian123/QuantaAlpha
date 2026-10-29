#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = PROJECT_ROOT / "frontend-v2" / "backend"
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))
load_dotenv(PROJECT_ROOT / ".env", override=False)

from market_data.intraday_calibration import (  # noqa: E402
    build_calibration_report,
    finalize_calibration_report,
)
from market_data.intraday_capture import DEFAULT_OUTPUT_ROOT  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Inspect or finalize the calibration-only intraday capture report."
    )
    parser.add_argument(
        "--declaration",
        type=Path,
        default=Path(
            os.getenv(
                "QUANTAALPHA_INTRADAY_CALIBRATION_DECLARATION",
                PROJECT_ROOT
                / "configs/intraday_capture_calibration_v2_20260813.json",
            )
        ),
    )
    parser.add_argument(
        "--output-root",
        type=Path,
        default=PROJECT_ROOT / DEFAULT_OUTPUT_ROOT,
    )
    parser.add_argument(
        "--finalize",
        action="store_true",
        help="Write one immutable report after the declaration's close-time deadline.",
    )
    args = parser.parse_args()

    payload = (
        finalize_calibration_report(args.declaration, args.output_root)
        if args.finalize
        else build_calibration_report(args.declaration, args.output_root)
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    if payload.get("status") == "FAIL":
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
