#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = PROJECT_ROOT / "frontend-v2" / "backend"
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))
load_dotenv(PROJECT_ROOT / ".env", override=False)

from market_data.historical_intraday_qualification import (  # noqa: E402
    DEFAULT_MAX_TURNOVER_DIFFERENCE,
    DEFAULT_MINIMUM_COMMON_SESSIONS,
    DEFAULT_SAMPLE_CODES,
    TushareIntradayQualificationRequest,
    qualify_tushare_intraday_sample,
)
from market_data.providers.tushare import TushareApiError, TushareProClient  # noqa: E402


DEFAULT_REFERENCE_SNAPSHOT = (
    PROJECT_ROOT
    / "data/market/a_share/intraday/a_stock_data_csi300/snapshots/2026-08-13"
    / "snapshot_145000_cf708231ba51.json.gz"
)


def _parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"invalid ISO date: {value}") from exc


def _parse_codes(value: str) -> tuple[str, ...]:
    codes = tuple(item.strip().upper() for item in value.split(",") if item.strip())
    if not codes:
        raise argparse.ArgumentTypeError("at least one code is required")
    return codes


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Read-only Tushare stk_mins/stk_premarket sample qualification. "
            "It never writes market data or promotes a Provider."
        )
    )
    parser.add_argument("--codes", type=_parse_codes, default=DEFAULT_SAMPLE_CODES)
    parser.add_argument("--start-date", type=_parse_date, default=date(2025, 7, 1))
    parser.add_argument("--end-date", type=_parse_date, default=date(2026, 8, 13))
    parser.add_argument("--reference-date", type=_parse_date, default=date(2026, 8, 13))
    parser.add_argument("--reference-snapshot", type=Path, default=DEFAULT_REFERENCE_SNAPSHOT)
    parser.add_argument(
        "--minimum-common-sessions",
        type=int,
        default=DEFAULT_MINIMUM_COMMON_SESSIONS,
    )
    parser.add_argument("--chunk-days", type=int, default=120)
    parser.add_argument("--request-interval", type=float, default=0.2)
    parser.add_argument(
        "--max-turnover-difference",
        type=float,
        default=DEFAULT_MAX_TURNOVER_DIFFERENCE,
        help="Maximum absolute turnover difference in percentage points.",
    )
    args = parser.parse_args()

    try:
        request = TushareIntradayQualificationRequest(
            codes=args.codes,
            start_date=args.start_date,
            end_date=args.end_date,
            reference_date=args.reference_date,
            reference_snapshot=args.reference_snapshot,
            minimum_common_sessions=args.minimum_common_sessions,
            chunk_days=args.chunk_days,
            request_interval_seconds=args.request_interval,
            max_turnover_difference=args.max_turnover_difference,
        )
        result = qualify_tushare_intraday_sample(TushareProClient(), request)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["status"] == "SAMPLE_PASS" else 1
    except TushareApiError as exc:
        result = {
            "status": "BLOCKED",
            "reason": str(exc),
            "sourceQualified": False,
            "adapterEligible": False,
            "tokenPersisted": False,
        }
    except (FileNotFoundError, ValueError) as exc:
        result = {
            "status": "FAIL",
            "reason": str(exc),
            "sourceQualified": False,
            "adapterEligible": False,
            "tokenPersisted": False,
        }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
