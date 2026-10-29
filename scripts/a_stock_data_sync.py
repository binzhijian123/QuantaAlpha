#!/usr/bin/env python
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = PROJECT_ROOT / "frontend-v2" / "backend"
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from market_data.models import SyncRequest
from market_data.sync_service import sync_a_stock_data_daily


def load_active_universe(universe: str) -> list[str]:
    provider = PROJECT_ROOT / "data" / "qlib" / "cn_data"
    calendar_file = provider / "calendars" / "day.txt"
    instrument_file = provider / "instruments" / f"{universe}.txt"
    as_of = calendar_file.read_text(encoding="utf-8").splitlines()[-1]
    active: list[str] = []
    for line in instrument_file.read_text(encoding="utf-8").splitlines():
        code, start, end = line.split("\t")[:3]
        if start <= as_of <= end:
            active.append(code)
    if not active:
        raise RuntimeError(f"no active instruments found for {universe} at {as_of}")
    return active


def main() -> int:
    parser = argparse.ArgumentParser(description="Sync a-stock-data daily A-share bars into isolated QuantaAlpha paths.")
    parser.add_argument("--codes", default="000001.SZ,600000.SH", help="Comma-separated A-share codes")
    parser.add_argument("--universe", choices=["csi300"], help="Use the latest active local universe snapshot.")
    parser.add_argument("--start-date")
    parser.add_argument("--end-date")
    parser.add_argument("--lookback-trading-days", type=int, default=20)
    parser.add_argument("--batch-id")
    parser.add_argument("--promote", action="store_true", help="Write an isolated Qlib provider when validation passes.")
    args = parser.parse_args()

    codes = load_active_universe(args.universe) if args.universe else [
        item.strip().upper() for item in args.codes.split(",") if item.strip()
    ]
    req = SyncRequest(
        codes=codes,
        start_date=args.start_date,
        end_date=args.end_date,
        lookback_trading_days=args.lookback_trading_days,
        promote=args.promote,
        batch_id=args.batch_id,
    )
    result = sync_a_stock_data_daily(PROJECT_ROOT, req)
    print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    return 0 if result.success else 1


if __name__ == "__main__":
    raise SystemExit(main())
