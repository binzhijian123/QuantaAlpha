#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = PROJECT_ROOT / "frontend-v2" / "backend"
for path in (PROJECT_ROOT, BACKEND_ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from market_data.providers.eastmoney_financials import (
    EastmoneyFinancialError,
    audit_conservative_droe_structure,
)


DEFAULT_CODES = "600519.SH,000001.SZ,601318.SH,600030.SH"


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Audit token-free Eastmoney statement dates and fields for conservative "
            "dROE construction without printing financial values or reading returns."
        )
    )
    parser.add_argument("--codes", default=DEFAULT_CODES)
    parser.add_argument("--start-report-date", default="2016-01-01")
    parser.add_argument("--end-report-date", default="2021-12-31")
    parser.add_argument("--selection-year", type=int, default=2021)
    parser.add_argument("--minimum-ratio", type=float, default=0.5)
    args = parser.parse_args()
    codes = [item.strip().upper() for item in args.codes.split(",") if item.strip()]
    if not codes:
        parser.error("--codes must contain at least one code")
    if not 0 <= args.minimum_ratio <= 1:
        parser.error("--minimum-ratio must be between 0 and 1")

    try:
        result = audit_conservative_droe_structure(
            codes,
            start_report_date=args.start_report_date,
            end_report_date=args.end_report_date,
            selection_year=args.selection_year,
            minimum_ratio=args.minimum_ratio,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["status"] == "PASS" else 1
    except (EastmoneyFinancialError, ValueError) as exc:
        print(
            json.dumps(
                {
                    "status": "BLOCKED",
                    "reason": str(exc),
                    "financialValuesExposed": False,
                    "returnsOrPricesRead": False,
                    "artifactsWritten": False,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
