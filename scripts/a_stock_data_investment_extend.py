#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = PROJECT_ROOT / "frontend-v2" / "backend"
for path in (PROJECT_ROOT, BACKEND_ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from market_data.investment_data_daily_extension import (
    InvestmentDataDailyExtensionRequest,
    sync_investment_data_daily_extension,
)


DEFAULT_BASE_PROVIDER = (
    PROJECT_ROOT
    / "data"
    / "market"
    / "a_share"
    / "qlib"
    / "investment_data_20260804_cn"
)


def _path(value: str) -> Path:
    path = Path(value).expanduser()
    return path.resolve() if path.is_absolute() else (PROJECT_ROOT / path).resolve()


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Audit Tencent public raw/qfq bars against investment_data, then optionally "
            "build a new immutable frozen-CSI300 Qlib provider. No account or token is used."
        )
    )
    parser.add_argument("--base-provider", default=str(DEFAULT_BASE_PROVIDER))
    parser.add_argument("--end-date", default=date.today().isoformat())
    parser.add_argument("--market", default="csi300")
    parser.add_argument("--benchmark", default="000300.SH")
    parser.add_argument("--batch-id")
    parser.add_argument("--overlap-sessions", type=int, default=40)
    parser.add_argument("--request-interval", type=float, default=0.02)
    parser.add_argument("--snapshot-rechecks", type=int, default=2)
    parser.add_argument(
        "--source-snapshot-dir",
        help=(
            "Replay an existing immutable snapshot_1/snapshot_2 directory instead of "
            "querying today's mutable qfq history."
        ),
    )
    parser.add_argument(
        "--forward-adjustment-evidence-dates",
        nargs="+",
        default=(),
        metavar="YYYY-MM-DD",
        help=(
            "Explicitly approved forward dates whose stable Tencent qfq/raw ratios may "
            "satisfy only the post-action session-count gate."
        ),
    )
    parser.add_argument("--output-provider")
    parser.add_argument(
        "--promote",
        action="store_true",
        help="Build a new provider after the source audit passes.",
    )
    args = parser.parse_args()
    if args.promote and not args.output_provider:
        parser.error("--output-provider is required with --promote")
    if args.output_provider and not args.promote:
        parser.error("--output-provider requires --promote")

    request = InvestmentDataDailyExtensionRequest(
        base_provider=_path(args.base_provider),
        end_date=args.end_date,
        output_provider=_path(args.output_provider) if args.promote else None,
        market=args.market,
        benchmark=args.benchmark,
        batch_id=args.batch_id,
        overlap_sessions=args.overlap_sessions,
        request_interval_seconds=args.request_interval,
        snapshot_rechecks=args.snapshot_rechecks,
        forward_adjustment_evidence_dates=tuple(
            args.forward_adjustment_evidence_dates
        ),
        source_snapshot_dir=(
            _path(args.source_snapshot_dir) if args.source_snapshot_dir else None
        ),
    )
    result = sync_investment_data_daily_extension(PROJECT_ROOT, request)
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
