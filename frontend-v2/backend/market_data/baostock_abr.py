from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import numpy as np
import pandas as pd

from quantaalpha.trading.market_data import QlibBinaryDailySource
from quantaalpha.utils.qlib_provider import provider_contract_fingerprint

from .fundamentals import (
    _canonical_code,
    _read_calendar,
    _sha256,
    _write_json,
    build_point_in_time_panel,
    load_market_code_spans,
    write_enriched_qlib_provider,
)
from .paths import manifest_path, report_path, staging_dir


ABR_METRIC = "abr_earnings"
ABR_FIELD = f"pit_{ABR_METRIC}"
BENCHMARK_CODE = "000300.SH"
REACTION_RETURN_OFFSETS = (-2, -1, 0, 1)
STALENESS_MONTHS = 6
MIN_CODES_WITH_ABR_FRACTION = 0.8
MIN_PANEL_COVERAGE = 0.5


@dataclass(frozen=True)
class BaoStockAbrSyncRequest:
    base_provider: Path
    quarterly_events: Path
    codes: list[str]
    start_date: str
    end_date: str
    market: str = "csi300"
    output_provider: Path | None = None
    batch_id: str | None = None


def _require_columns(frame: pd.DataFrame, required: Iterable[str], table: str) -> None:
    missing = sorted(set(required) - set(frame.columns))
    if missing:
        raise ValueError(f"{table} is missing required columns: {missing}")


def normalize_baostock_announcement_events(rows: pd.DataFrame) -> pd.DataFrame:
    source = rows.copy()
    aliases = {
        "pubDate": "announcement_date",
        "statDate": "report_end",
    }
    source = source.rename(
        columns={key: value for key, value in aliases.items() if value not in source}
    )
    _require_columns(
        source,
        ("code", "announcement_date", "report_end"),
        "BaoStock quarterly announcements",
    )
    source["code"] = source["code"].map(_canonical_code)
    source["announcement_date"] = pd.to_datetime(
        source["announcement_date"], errors="coerce"
    ).dt.normalize()
    source["report_end"] = pd.to_datetime(
        source["report_end"], errors="coerce"
    ).dt.normalize()
    if source[["announcement_date", "report_end"]].isna().any(axis=None):
        raise ValueError("BaoStock quarterly announcements contain invalid dates")
    if (~source["report_end"].dt.is_quarter_end).any():
        raise ValueError("BaoStock quarterly announcements contain a non-quarter-end report")
    if (source["announcement_date"] < source["report_end"]).any():
        raise ValueError("BaoStock quarterly announcements contain pre-period publication")

    keys = ["code", "report_end"]
    duplicate_rows = source[source.duplicated(keys, keep=False)]
    conflicts = []
    for key, group in duplicate_rows.groupby(keys, dropna=False, sort=False):
        if group["announcement_date"].nunique(dropna=False) > 1:
            conflicts.append(key if isinstance(key, tuple) else (key,))
    if conflicts:
        raise ValueError(
            "BaoStock quarterly announcements contain conflicting duplicates: "
            f"{conflicts[:5]}"
        )
    return (
        source[["code", "report_end", "announcement_date"]]
        .drop_duplicates(keys, keep="last")
        .sort_values(["code", "report_end"])
        .reset_index(drop=True)
    )


def _normalize_price_frame(prices: pd.DataFrame) -> pd.DataFrame:
    work = prices.copy()
    work = work.rename(
        columns={
            "datetime": "date",
            "instrument": "code",
            "$close": "close",
            "$volume": "volume",
        }
    )
    _require_columns(work, ("date", "code", "close", "volume"), "Provider prices")
    work["date"] = pd.to_datetime(work["date"], errors="coerce").dt.normalize()
    work["code"] = work["code"].map(_canonical_code)
    work["close"] = pd.to_numeric(work["close"], errors="coerce")
    work["volume"] = pd.to_numeric(work["volume"], errors="coerce")
    if work["date"].isna().any():
        raise ValueError("Provider prices contain invalid dates")

    duplicate_rows = work[work.duplicated(["date", "code"], keep=False)]
    for key, group in duplicate_rows.groupby(["date", "code"], sort=False):
        if len(group[["close", "volume"]].drop_duplicates()) > 1:
            raise ValueError(f"Provider prices contain conflicting duplicate: {key}")
    return work.drop_duplicates(["date", "code"], keep="last")


def compute_earnings_announcement_abr(
    announcements: pd.DataFrame,
    prices: pd.DataFrame,
    calendar: Iterable[pd.Timestamp | str],
    *,
    benchmark_code: str = BENCHMARK_CODE,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    events = normalize_baostock_announcement_events(announcements)
    price_frame = _normalize_price_frame(prices)
    calendar_values = sorted(
        {pd.Timestamp(value).normalize() for value in calendar}
    )
    if not calendar_values:
        raise ValueError("Provider calendar is empty")
    benchmark = _canonical_code(benchmark_code)
    if benchmark not in set(price_frame["code"]):
        raise ValueError(f"Provider prices are missing benchmark {benchmark}")

    closes = price_frame.pivot(index="date", columns="code", values="close").reindex(
        calendar_values
    )
    volumes = price_frame.pivot(index="date", columns="code", values="volume").reindex(
        calendar_values
    )
    benchmark_close = closes[benchmark].to_numpy(dtype="float64")
    calendar_array = np.asarray(calendar_values, dtype="datetime64[ns]")

    missing_reasons: dict[str, int] = {}
    records: list[dict[str, Any]] = []
    for event in events.itertuples(index=False):
        announcement_date = pd.Timestamp(event.announcement_date)
        event_index = int(
            np.searchsorted(
                calendar_array,
                announcement_date.to_datetime64(),
                side="left",
            )
        )
        record: dict[str, Any] = {
            "code": event.code,
            "report_end": event.report_end,
            "announcement_date": announcement_date,
            "reaction_session": pd.NaT,
            "reaction_end": pd.NaT,
            "available_date": pd.NaT,
            ABR_METRIC: np.nan,
            "missing_reason": None,
        }

        reason = None
        if event_index >= len(calendar_values):
            reason = "no_reaction_session"
        elif event_index - 3 < 0:
            reason = "insufficient_pre_event_calendar"
        elif event_index + 2 >= len(calendar_values):
            reason = "insufficient_post_event_calendar"
        else:
            reaction_session = calendar_values[event_index]
            reaction_end = calendar_values[event_index + 1]
            record.update(
                {
                    "reaction_session": reaction_session,
                    "reaction_end": reaction_end,
                    # build_point_in_time_panel makes this usable strictly after +1.
                    "available_date": reaction_end,
                }
            )
            if event.code not in closes:
                reason = "missing_stock_prices"
            else:
                close_slice = closes[event.code].to_numpy(dtype="float64")[
                    event_index - 3 : event_index + 2
                ]
                benchmark_slice = benchmark_close[
                    event_index - 3 : event_index + 2
                ]
                volume_slice = volumes.get(
                    event.code,
                    pd.Series(np.nan, index=calendar_values),
                ).to_numpy(dtype="float64")[event_index - 2 : event_index + 2]
                if (
                    len(close_slice) != 5
                    or not np.isfinite(close_slice).all()
                    or (close_slice <= 0).any()
                ):
                    reason = "invalid_stock_close_window"
                elif (
                    len(benchmark_slice) != 5
                    or not np.isfinite(benchmark_slice).all()
                    or (benchmark_slice <= 0).any()
                ):
                    reason = "invalid_benchmark_close_window"
                elif (
                    len(volume_slice) != 4
                    or not np.isfinite(volume_slice).all()
                    or (volume_slice <= 0).any()
                ):
                    reason = "nontrading_stock_window"
                else:
                    stock_returns = close_slice[1:] / close_slice[:-1] - 1.0
                    market_returns = benchmark_slice[1:] / benchmark_slice[:-1] - 1.0
                    abr = float(np.sum(stock_returns - market_returns))
                    if np.isfinite(abr):
                        record[ABR_METRIC] = abr
                    else:
                        reason = "nonfinite_abr"

        if reason:
            record["missing_reason"] = reason
            missing_reasons[reason] = missing_reasons.get(reason, 0) + 1
        records.append(record)

    calculated = pd.DataFrame.from_records(records)
    valid = calculated.dropna(subset=[ABR_METRIC]) if not calculated.empty else calculated
    audit = {
        "sourceRows": int(len(events)),
        "sourceCodes": int(events["code"].nunique()),
        "abrRows": int(len(valid)),
        "codesWithAnyAbr": int(valid["code"].nunique()) if not valid.empty else 0,
        "missingReasonCounts": missing_reasons,
        "benchmark": benchmark,
        "reactionReturnOffsets": list(REACTION_RETURN_OFFSETS),
        "eventDayPolicy": "first Provider session on or after BaoStock pubDate",
        "effectiveDayPolicy": "first Provider session strictly after reaction day +1",
        "formula": "sum d=-2..+1 of stock adjusted close return minus SH000300 adjusted close return",
        "calendarStart": calendar_values[0].date().isoformat(),
        "calendarEnd": calendar_values[-1].date().isoformat(),
        "calendarSessionCount": len(calendar_values),
    }
    return calculated.sort_values(["code", "report_end"]).reset_index(drop=True), audit


def _calendar_between(
    provider: Path,
    start_date: str,
    end_date: str,
) -> list[pd.Timestamp]:
    start = pd.Timestamp(start_date).normalize()
    end = pd.Timestamp(end_date).normalize()
    if end < start:
        raise ValueError("end_date must be on or after start_date")
    calendar = [date for date in _read_calendar(provider) if start <= date <= end]
    if not calendar:
        raise ValueError(f"provider has no sessions in {start_date}..{end_date}")
    return calendar


def _load_provider_prices(
    provider: Path,
    codes: list[str],
    announcements: pd.DataFrame,
    end_date: str,
) -> pd.DataFrame:
    source = QlibBinaryDailySource(provider)
    end_sessions = [date for date in source.calendar if date <= end_date]
    if not end_sessions:
        raise ValueError(f"provider has no price session on or before {end_date}")
    earliest_announcement = pd.Timestamp(announcements["announcement_date"].min())
    requested_start = (earliest_announcement - pd.Timedelta(days=14)).date().isoformat()
    frame = source.history_frame(
        end_sessions[-1],
        start_date=requested_start,
        symbols=[BENCHMARK_CODE, *codes],
        fields=("close", "volume"),
    )
    return frame.reset_index()


def build_baostock_abr_panel(
    events: pd.DataFrame,
    *,
    base_provider: Path,
    calendar: list[pd.Timestamp],
    source_windows: dict[str, tuple[pd.Timestamp, pd.Timestamp]],
    codes: list[str],
    source_audit: dict[str, Any],
) -> tuple[pd.DataFrame, dict[str, Any]]:
    scaffold = pd.DataFrame.from_records(
        (
            {"date": date, "code": code}
            for code in codes
            for date in calendar
            if source_windows[code][0] <= date <= source_windows[code][1]
        )
    )
    usable_events = events.dropna(subset=["available_date"]).copy()
    panel, audit = build_point_in_time_panel(
        usable_events,
        scaffold,
        calendar,
        source_windows,
        codes,
        financial_metrics=(ABR_METRIC,),
        daily_basic_metrics=(),
        publication_lag_sessions=1,
    )
    stale_limit = panel["pit_report_end"] + pd.DateOffset(months=STALENESS_MONTHS)
    stale = panel["pit_report_end"].notna() & panel["date"].gt(stale_limit)
    stale_signals = int((stale & panel[ABR_FIELD].notna()).sum())
    panel.loc[stale, ABR_FIELD] = np.nan

    codes_with_abr = int(source_audit.get("codesWithAnyAbr", 0))
    code_fraction = codes_with_abr / len(codes) if codes else 0.0
    coverage = float(panel[ABR_FIELD].notna().mean()) if len(panel) else 0.0
    integrity_passed = audit.get("status") == "PASS"
    threshold_passed = (
        code_fraction >= MIN_CODES_WITH_ABR_FRACTION
        and coverage >= MIN_PANEL_COVERAGE
    )
    audit.update(
        {
            "status": "PASS" if integrity_passed and threshold_passed else "FAIL",
            "sourceProvider": "baostock_announcement_dates_plus_qlib_prices",
            "baseProvider": str(base_provider),
            "featureFields": [ABR_FIELD],
            "coverage": {ABR_FIELD: coverage},
            "codesWithAnyAbr": codes_with_abr,
            "codesWithAnyAbrFraction": code_fraction,
            "minimumCodesWithAbrFraction": MIN_CODES_WITH_ABR_FRACTION,
            "minimumPanelCoverage": MIN_PANEL_COVERAGE,
            "stalenessMonths": STALENESS_MONTHS,
            "staleSignalRowsCleared": stale_signals,
            "sourceAudit": source_audit,
            "revisionHistoryAvailable": False,
            "announcementTimestampAvailable": False,
            "revisionRisk": (
                "BaoStock returns the current historical announcement snapshot and "
                "does not expose superseded dates or intraday release timestamps."
            ),
        }
    )
    return panel.sort_values(["date", "code"]).reset_index(drop=True), audit


def _write_deterministic_csv_gzip(frame: pd.DataFrame, path: Path) -> None:
    frame.to_csv(
        path,
        index=False,
        compression={"method": "gzip", "mtime": 0},
        date_format="%Y-%m-%d",
    )


def sync_baostock_abr(
    project_root: Path,
    request: BaoStockAbrSyncRequest,
) -> dict[str, Any]:
    started = datetime.now(timezone.utc)
    batch_id = request.batch_id or (
        f"baostock_abr_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}"
    )
    stage_dir = staging_dir(project_root, batch_id)
    manifest_file = manifest_path(project_root, batch_id)
    report_file = report_path(project_root, batch_id)
    collisions = [path for path in (stage_dir, manifest_file, report_file) if path.exists()]
    if collisions:
        raise FileExistsError(f"refusing to overwrite immutable ABR artifacts: {collisions}")
    if request.output_provider and request.output_provider.exists():
        raise FileExistsError(f"refusing to overwrite provider: {request.output_provider}")

    base = request.base_provider.expanduser().resolve()
    source_file = request.quarterly_events.expanduser().resolve()
    if not source_file.is_file():
        raise FileNotFoundError(f"quarterly announcement source is missing: {source_file}")
    codes = list(dict.fromkeys(_canonical_code(code) for code in request.codes))
    if not codes:
        raise ValueError("at least one code is required")
    requested_start = pd.Timestamp(request.start_date).normalize()
    requested_end = pd.Timestamp(request.end_date).normalize()
    if requested_end < requested_start:
        raise ValueError("end_date must be on or after start_date")

    source_windows = load_market_code_spans(
        base,
        request.market,
        request.start_date,
        request.end_date,
    )
    missing_codes = sorted(set(codes) - set(source_windows))
    if missing_codes:
        raise ValueError(
            f"requested codes do not overlap the point-in-time market window: {missing_codes[:10]}"
        )
    source_windows = {code: source_windows[code] for code in codes}
    base_contract = provider_contract_fingerprint(base, request.market)
    stage_dir.mkdir(parents=True)

    manifest = {
        "schemaVersion": "baostock-earnings-announcement-abr-sync-v1",
        "batchId": batch_id,
        "createdAt": started.isoformat(),
        "request": {
            "baseProvider": str(base),
            "baseProviderFingerprint": base_contract["fingerprint"],
            "quarterlyEvents": str(source_file),
            "quarterlyEventsSha256": _sha256(source_file),
            "market": request.market,
            "benchmark": BENCHMARK_CODE,
            "codes": codes,
            "startDate": request.start_date,
            "endDate": request.end_date,
            "outputProvider": str(request.output_provider) if request.output_provider else None,
        },
    }
    _write_json(manifest_file, manifest)

    source_rows = pd.read_csv(source_file)
    announcements = normalize_baostock_announcement_events(source_rows)
    announcements = announcements[announcements["code"].isin(codes)].copy()
    earliest_report = requested_start - pd.DateOffset(months=STALENESS_MONTHS)
    announcements = announcements[
        (announcements["report_end"] >= earliest_report)
        & (announcements["report_end"] <= requested_end)
        & (announcements["announcement_date"] <= requested_end)
    ].reset_index(drop=True)
    if announcements.empty:
        raise ValueError("no quarterly announcements overlap the requested ABR window")

    prices = _load_provider_prices(base, codes, announcements, request.end_date)
    full_calendar = _read_calendar(base)
    events, source_audit = compute_earnings_announcement_abr(
        announcements,
        prices,
        full_calendar,
    )
    panel, audit = build_baostock_abr_panel(
        events,
        base_provider=base,
        calendar=_calendar_between(base, request.start_date, request.end_date),
        source_windows=source_windows,
        codes=codes,
        source_audit=source_audit,
    )

    events_file = stage_dir / "earnings_announcement_abr_events.csv.gz"
    panel_file = stage_dir / "point_in_time_abr.csv.gz"
    _write_deterministic_csv_gzip(events, events_file)
    _write_deterministic_csv_gzip(panel, panel_file)
    _write_json(stage_dir / "as_of_audit.json", audit)
    manifest["outputs"] = {
        "events": {"path": str(events_file), "sha256": _sha256(events_file)},
        "panel": {"path": str(panel_file), "sha256": _sha256(panel_file)},
    }
    _write_json(manifest_file, manifest)

    output_info = None
    if request.output_provider and audit.get("status") == "PASS":
        output_info = write_enriched_qlib_provider(
            base,
            request.output_provider,
            panel,
            (ABR_FIELD,),
            batch_id=batch_id,
            source_manifest=manifest_file,
            as_of_audit=audit,
            market=request.market,
            as_of_policy={
                "eventDay": "first Provider session on or after BaoStock pubDate",
                "reactionWindow": "adjusted close returns from day -2 through day +1",
                "financialAvailability": "first Provider session strictly after reaction day +1",
                "stalenessPolicy": "signal is missing after six calendar months from statDate",
                "benchmarkPolicy": "SH000300 adjusted close return",
                "restatementPolicy": "current BaoStock historical snapshot; revision vintages unavailable",
            },
            source_metadata={
                "provider": "baostock_announcement_dates_plus_qlib_prices",
                "credentialsRequired": False,
                "revisionHistoryAvailable": False,
                "announcementTimestampAvailable": False,
                "signalKind": "earnings_announcement_abnormal_return_proxy",
            },
        )

    finished = datetime.now(timezone.utc)
    result = {
        "batchId": batch_id,
        "status": audit.get("status", "FAIL"),
        "codesRequested": len(codes),
        "rows": int(len(panel)),
        "events": int(len(events)),
        "stagingDir": str(stage_dir),
        "manifestPath": str(manifest_file),
        "reportPath": str(report_file),
        "audit": audit,
        "outputProvider": output_info,
        "startedAt": started.isoformat(),
        "finishedAt": finished.isoformat(),
        "elapsedSeconds": (finished - started).total_seconds(),
    }
    _write_json(report_file, result)
    return result
