#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = PROJECT_ROOT / "frontend-v2" / "backend"
for path in (PROJECT_ROOT, BACKEND_ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from market_data.fundamentals import (
    DEFAULT_DAILY_BASIC_METRICS,
    DEFAULT_FINANCIAL_METRICS,
    FundamentalSyncRequest,
    load_market_codes,
    sync_tushare_fundamentals,
)
from market_data.providers.tushare import TushareApiError, TushareProClient


DEFAULT_BASE_PROVIDER = (
    PROJECT_ROOT
    / "data"
    / "market"
    / "a_share"
    / "qlib"
    / "investment_data_20260725_cn"
)

load_dotenv(PROJECT_ROOT / ".env", override=False)


def _path(value: str) -> Path:
    path = Path(value).expanduser()
    return path.resolve() if path.is_absolute() else (PROJECT_ROOT / path).resolve()


def _fields(value: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in value.split(",") if item.strip())


def _last_provider_date(provider: Path) -> str:
    calendar = provider / "calendars" / "day.txt"
    dates = [line for line in calendar.read_text(encoding="utf-8").splitlines() if line]
    if not dates:
        raise RuntimeError(f"provider calendar is empty: {calendar}")
    return dates[-1]


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Fetch Tushare point-in-time fundamentals into immutable raw artifacts "
            "and optionally build an isolated enriched Qlib provider."
        )
    )
    parser.add_argument("--base-provider", default=str(DEFAULT_BASE_PROVIDER))
    parser.add_argument("--market", default="csi300")
    parser.add_argument("--codes", help="Comma-separated subset; promotion requires deliberate full-universe use.")
    parser.add_argument("--start-date", default="2014-01-01")
    parser.add_argument("--end-date")
    parser.add_argument(
        "--financial-metrics",
        default=",".join(DEFAULT_FINANCIAL_METRICS),
    )
    parser.add_argument(
        "--daily-basic-metrics",
        default=",".join(DEFAULT_DAILY_BASIC_METRICS),
    )
    parser.add_argument("--publication-lag-sessions", type=int, default=1)
    parser.add_argument("--request-interval", type=float, default=0.15)
    parser.add_argument("--batch-id")
    parser.add_argument(
        "--reuse-raw-batch",
        help="Start a new immutable batch while reusing successful raw responses from an earlier batch ID.",
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
        help="Verify only token and API access; do not create data artifacts.",
    )
    args = parser.parse_args()

    base_provider = _path(args.base_provider)
    end_date = args.end_date or _last_provider_date(base_provider)
    try:
        client = TushareProClient()
        if args.preflight:
            result = client.preflight(end_date)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0

        if args.promote and not args.output_provider:
            parser.error("--output-provider is required with --promote")
        if args.output_provider and not args.promote:
            parser.error("--output-provider requires --promote")
        if args.codes:
            codes = [item.strip().upper() for item in args.codes.split(",") if item.strip()]
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

        request = FundamentalSyncRequest(
            base_provider=base_provider,
            codes=codes,
            start_date=args.start_date,
            end_date=end_date,
            market=args.market,
            financial_metrics=_fields(args.financial_metrics),
            daily_basic_metrics=_fields(args.daily_basic_metrics),
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
                / "tushare_fundamentals"
                / args.reuse_raw_batch
                if args.reuse_raw_batch
                else None
            ),
        )
        result = sync_tushare_fundamentals(PROJECT_ROOT, request, client=client)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["status"] == "PASS" else 1
    except TushareApiError as exc:
        print(
            json.dumps(
                {
                    "status": "BLOCKED",
                    "reason": str(exc),
                    "tokenPersisted": False,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
