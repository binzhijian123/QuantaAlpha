#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = PROJECT_ROOT / "frontend-v2" / "backend"
for path in (PROJECT_ROOT, BACKEND_ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from market_data.baostock_fundamentals import (
    BaoStockFundamentalSyncRequest,
    RAW_PROVIDER_NAME,
    sync_baostock_fundamentals,
)
from market_data.fundamentals import load_market_code_spans, load_market_codes
from market_data.providers.a_stock_data import normalize_stock_code
from market_data.providers.baostock import BaoStockApiError, BaoStockClient


DEFAULT_BASE_PROVIDER = (
    PROJECT_ROOT
    / "data"
    / "market"
    / "a_share"
    / "qlib"
    / "investment_data_20260725_cn"
)


def _path(value: str) -> Path:
    path = Path(value).expanduser()
    return path.resolve() if path.is_absolute() else (PROJECT_ROOT / path).resolve()


def _last_provider_date(provider: Path) -> str:
    calendar = provider / "calendars" / "day.txt"
    dates = [
        line
        for line in calendar.read_text(encoding="utf-8").splitlines()
        if line
    ]
    if not dates:
        raise RuntimeError(f"provider calendar is empty: {calendar}")
    return dates[-1]


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Fetch anonymous BaoStock annual fundamentals and daily valuation into "
            "immutable point-in-time artifacts, then optionally build an isolated provider."
        )
    )
    parser.add_argument("--base-provider", default=str(DEFAULT_BASE_PROVIDER))
    parser.add_argument("--market", default="csi300")
    parser.add_argument(
        "--codes",
        help="Comma-separated subset; promotion requires deliberate full-universe use.",
    )
    parser.add_argument("--start-date", default="2016-01-01")
    parser.add_argument("--end-date")
    parser.add_argument("--publication-lag-sessions", type=int, default=1)
    parser.add_argument("--request-interval", type=float, default=0.0)
    parser.add_argument(
        "--include-earnings-to-price",
        action="store_true",
        help=(
            "Request peTTM and add signed 1/peTTM as pit_earnings_to_price; "
            "zero or missing PE remains missing."
        ),
    )
    parser.add_argument("--batch-id")
    parser.add_argument(
        "--reuse-raw-batch",
        help="Reuse successful raw responses from an earlier immutable batch ID.",
    )
    parser.add_argument("--output-provider")
    parser.add_argument(
        "--promote",
        action="store_true",
        help="Build a new provider. The output path must not already exist.",
    )
    parser.add_argument(
        "--preflight",
        action="store_true",
        help="Verify package and anonymous service access without creating artifacts.",
    )
    parser.add_argument(
        "--estimate",
        action="store_true",
        help="Print full request counts without contacting BaoStock or creating artifacts.",
    )
    args = parser.parse_args()

    base_provider = _path(args.base_provider)
    end_date = args.end_date or _last_provider_date(base_provider)
    try:
        if args.preflight:
            result = BaoStockClient().preflight(end_date)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0

        if args.promote and not args.output_provider:
            parser.error("--output-provider is required with --promote")
        if args.output_provider and not args.promote:
            parser.error("--output-provider requires --promote")
        if args.codes:
            codes = [
                item.strip().upper()
                for item in args.codes.split(",")
                if item.strip()
            ]
            if args.promote:
                parser.error(
                    "partial --codes runs cannot be promoted; omit --codes to use the full point-in-time market universe"
                )
        else:
            codes = load_market_codes(
                base_provider,
                args.market,
                args.start_date,
                end_date,
            )

        market_spans = load_market_code_spans(
            base_provider,
            args.market,
            args.start_date,
            end_date,
        )
        canonical_codes = []
        for code in codes:
            digits, exchange, _ = normalize_stock_code(code)
            canonical_codes.append(f"{digits}.{exchange}")
        global_start = int(args.start_date[:4])
        global_end = int(end_date[:4])
        source_windows = {
            code: market_spans.get(
                code,
                (
                    pd.Timestamp(args.start_date),
                    pd.Timestamp(end_date),
                ),
            )
            for code in canonical_codes
        }
        estimated_source_requests = sum(
            (window_end.year - (window_start.year - 1) + 1) * 2 + 1
            for window_start, window_end in source_windows.values()
        )
        if args.estimate:
            print(
                json.dumps(
                    {
                        "status": "PASS",
                        "provider": "baostock",
                        "codes": len(codes),
                        "reportFrequency": "annual",
                        "includeEarningsToPrice": args.include_earnings_to_price,
                        "requestedStartYear": global_start,
                        "requestedEndYear": global_end,
                        "sourceWindowPolicy": "point-in-time market membership envelope plus one annual seed year",
                        "estimatedSourceRequests": estimated_source_requests,
                        "artifactsCreated": False,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0

        request = BaoStockFundamentalSyncRequest(
            base_provider=base_provider,
            codes=codes,
            start_date=args.start_date,
            end_date=end_date,
            market=args.market,
            publication_lag_sessions=args.publication_lag_sessions,
            output_provider=_path(args.output_provider) if args.promote else None,
            batch_id=args.batch_id,
            request_interval_seconds=args.request_interval,
            reuse_raw_dir=(
                PROJECT_ROOT
                / "data"
                / "market"
                / "a_share"
                / "raw"
                / RAW_PROVIDER_NAME
                / args.reuse_raw_batch
                if args.reuse_raw_batch
                else None
            ),
            include_earnings_to_price=args.include_earnings_to_price,
        )
        result = sync_baostock_fundamentals(
            PROJECT_ROOT,
            request,
            client=BaoStockClient(),
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["status"] == "PASS" else 1
    except BaoStockApiError as exc:
        print(
            json.dumps(
                {
                    "status": "BLOCKED",
                    "provider": "baostock",
                    "reason": str(exc),
                    "credentialsRequired": False,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
