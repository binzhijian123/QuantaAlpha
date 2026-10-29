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

from market_data.baostock_abr import BaoStockAbrSyncRequest, sync_baostock_abr
from market_data.fundamentals import load_market_codes


DEFAULT_BASE_PROVIDER = (
    PROJECT_ROOT
    / "data"
    / "market"
    / "a_share"
    / "qlib"
    / "investment_data_20260725_baostock_v1_9_sue_cn"
)
DEFAULT_QUARTERLY_EVENTS = (
    PROJECT_ROOT
    / "data"
    / "market"
    / "a_share"
    / "staging"
    / "baostock_sue_promote_v1_9_windowed"
    / "seasonal_sue_events.csv.gz"
)


def _path(value: str | Path) -> Path:
    path = Path(value).expanduser()
    return path.resolve() if path.is_absolute() else (PROJECT_ROOT / path).resolve()


def _last_provider_date(provider: Path) -> str:
    dates = [
        value
        for value in (provider / "calendars" / "day.txt").read_text(
            encoding="utf-8"
        ).splitlines()
        if value
    ]
    if not dates:
        raise RuntimeError(f"provider calendar is empty: {provider}")
    return dates[-1]


def _probe_values(path: Path) -> dict:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not payload.get("created_before_probe"):
        raise ValueError("probe config was not frozen before the probe")
    if payload.get("forward_labels_or_strategy_returns_may_be_read") is not False:
        raise ValueError("probe config does not prohibit forward-return inspection")
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Construct the frozen earnings-announcement abnormal-return proxy from "
            "immutable BaoStock announcement dates and local Qlib prices."
        )
    )
    parser.add_argument("--probe-config")
    parser.add_argument("--base-provider")
    parser.add_argument("--quarterly-events")
    parser.add_argument("--market", default="csi300")
    parser.add_argument("--codes", help="Comma-separated canonical stock codes")
    parser.add_argument("--start-date")
    parser.add_argument("--end-date")
    parser.add_argument("--batch-id")
    parser.add_argument("--output-provider")
    parser.add_argument(
        "--promote",
        action="store_true",
        help="Write an isolated Provider after all ABR data-contract gates pass.",
    )
    args = parser.parse_args()

    probe = _probe_values(_path(args.probe_config)) if args.probe_config else None
    base_provider = _path(
        args.base_provider
        or (probe or {}).get("base_provider")
        or DEFAULT_BASE_PROVIDER
    )
    quarterly_events = _path(
        args.quarterly_events
        or (probe or {}).get("quarterly_events")
        or DEFAULT_QUARTERLY_EVENTS
    )
    market = (probe or {}).get("market") or args.market
    start_date = args.start_date or (
        (probe or {}).get("probe_window") or ["2016-01-01"]
    )[0]
    end_date = args.end_date or (
        (probe or {}).get("probe_window") or [None, None]
    )[1] or _last_provider_date(base_provider)

    if probe:
        if args.codes:
            parser.error("--codes cannot override a frozen --probe-config")
        codes = list((probe.get("selection") or {}).get("codes") or [])
    elif args.codes:
        codes = [value.strip().upper() for value in args.codes.split(",") if value.strip()]
    else:
        codes = load_market_codes(base_provider, market, start_date, end_date)

    if args.promote and not args.output_provider:
        parser.error("--output-provider is required with --promote")
    if args.output_provider and not args.promote:
        parser.error("--output-provider requires --promote")
    if probe and args.promote:
        parser.error("a fixed partial probe cannot be promoted")

    result = sync_baostock_abr(
        PROJECT_ROOT,
        BaoStockAbrSyncRequest(
            base_provider=base_provider,
            quarterly_events=quarterly_events,
            codes=codes,
            start_date=start_date,
            end_date=end_date,
            market=market,
            output_provider=_path(args.output_provider) if args.promote else None,
            batch_id=args.batch_id,
        ),
    )
    if probe:
        acceptance = probe.get("acceptance") or {}
        audit = result.get("audit") or {}
        coverage = (audit.get("coverage") or {}).get("pit_abr_earnings", 0.0)
        checks = {
            "syncStatus": result.get("status") == "PASS",
            "availabilityLeakRows": audit.get("availabilityLeakRows", 1)
            <= acceptance.get("availability_leak_rows_max", 0),
            "reportPeriodLeakRows": audit.get("reportPeriodLeakRows", 1)
            <= acceptance.get("report_period_leak_rows_max", 0),
            "codesWithAnyAbr": audit.get("codesWithAnyAbr", 0)
            >= acceptance.get("codes_with_any_abr_min", len(codes)),
            "dailyCoverage": coverage
            >= acceptance.get("point_in_time_daily_coverage_min", 1.0),
        }
        result["probeAcceptance"] = {
            "status": "PASS" if all(checks.values()) else "FAIL",
            "checks": checks,
            "determinism": "REQUIRES_IDENTICAL_SECOND_RUN",
        }
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0 if result.get("status") == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
