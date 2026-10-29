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

from market_data.intraday_capture import (  # noqa: E402
    DEFAULT_OUTPUT_ROOT,
    read_capture_status,
    run_capture_cycle,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Capture or inspect research-only CSI300 a-stock-data snapshots."
    )
    parser.add_argument(
        "--provider",
        default=os.getenv("QLIB_DATA_DIR"),
        help="Active immutable Qlib Provider path (defaults to QLIB_DATA_DIR).",
    )
    parser.add_argument(
        "--output-root",
        type=Path,
        default=PROJECT_ROOT / DEFAULT_OUTPUT_ROOT,
    )
    parser.add_argument("--poll-seconds", type=int, default=60)
    parser.add_argument(
        "--force",
        action="store_true",
        help="Capture outside market hours for connectivity testing; output remains ineligible.",
    )
    parser.add_argument("--status", action="store_true")
    args = parser.parse_args()

    if args.status:
        payload = read_capture_status(args.output_root)
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0 if payload else 1
    if not args.provider:
        parser.error("--provider or QLIB_DATA_DIR is required")

    payload = run_capture_cycle(
        args.provider,
        args.output_root,
        poll_seconds=args.poll_seconds,
        force=args.force,
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 1 if payload.get("status") == "ERROR" else 0


if __name__ == "__main__":
    raise SystemExit(main())
