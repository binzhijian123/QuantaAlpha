from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import numpy as np
import pandas as pd

from quantaalpha.utils.qlib_provider import provider_contract_fingerprint

from .fundamentals import (
    _canonical_code,
    _payload_frame,
    _read_calendar,
    _read_instrument_spans,
    _sha256,
    _write_json,
    build_point_in_time_panel,
    load_market_code_spans,
    write_enriched_qlib_provider,
)
from .paths import manifest_path, raw_provider_dir, report_path, staging_dir
from .providers.a_stock_data import to_qlib_code
from .providers.baostock import BaoStockClient, from_baostock_code


RAW_PROVIDER_NAME = "baostock_seasonal_sue"
SUE_METRIC = "sue_seasonal"
SUE_FIELD = f"pit_{SUE_METRIC}"
QUARTERLY_PROFIT_FIELDS = (
    "code",
    "pubDate",
    "statDate",
    "netProfit",
    "epsTTM",
    "totalShare",
)
HISTORY_SEED_YEARS = 4
STANDARDIZATION_WINDOW_QUARTERS = 8
MIN_STANDARDIZATION_OBSERVATIONS = 6
STALENESS_MONTHS = 6


@dataclass(frozen=True)
class BaoStockSueSyncRequest:
    base_provider: Path
    codes: list[str]
    start_date: str
    end_date: str
    market: str = "csi300"
    publication_lag_sessions: int = 1
    output_provider: Path | None = None
    batch_id: str | None = None
    request_interval_seconds: float = 0.0
    reuse_raw_dir: Path | None = None


def _require_columns(frame: pd.DataFrame, required: Iterable[str], table: str) -> None:
    missing = sorted(set(required) - set(frame.columns))
    if missing:
        raise ValueError(f"{table} is missing required columns: {missing}")


def _deduplicate_quarters(frame: pd.DataFrame) -> pd.DataFrame:
    keys = ["code", "statDate"]
    duplicates = frame[frame.duplicated(keys, keep=False)]
    if not duplicates.empty:
        for key, group in duplicates.groupby(keys, dropna=False, sort=False):
            if len(group.drop_duplicates()) > 1:
                display_key = key if isinstance(key, tuple) else (key,)
                raise ValueError(
                    "query_profit_data contains conflicting duplicate quarters: "
                    f"{display_key}"
                )
    return frame.drop_duplicates(keys, keep="last").reset_index(drop=True)


def normalize_baostock_quarterly_sue(
    rows: pd.DataFrame,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    source = rows.copy()
    _require_columns(source, QUARTERLY_PROFIT_FIELDS, "query_profit_data")
    source["code"] = source["code"].map(from_baostock_code)
    source["pubDate"] = pd.to_datetime(source["pubDate"], errors="coerce")
    source["statDate"] = pd.to_datetime(source["statDate"], errors="coerce")
    if source[["pubDate", "statDate"]].isna().any(axis=None):
        raise ValueError("query_profit_data contains invalid publication or report dates")
    if (~source["statDate"].dt.is_quarter_end).any():
        raise ValueError("query_profit_data contains a non-quarter-end report")
    if (source["pubDate"] < source["statDate"]).any():
        raise ValueError("query_profit_data contains a report published before period end")

    for field in ("netProfit", "epsTTM", "totalShare"):
        source[field] = pd.to_numeric(source[field], errors="coerce")
    source = _deduplicate_quarters(source)
    source["period"] = source["statDate"].dt.to_period("Q-DEC")

    event_frames: list[pd.DataFrame] = []
    continuity_gaps = 0
    zero_denominators = 0
    for code, group in source.groupby("code", sort=True):
        group = group.sort_values("period")
        periods = pd.period_range(
            group["period"].min(),
            group["period"].max(),
            freq="Q-DEC",
        )
        grid = group.set_index("period").reindex(periods)
        continuity_gaps += int(grid["statDate"].isna().sum())
        ytd_profit = grid["netProfit"]
        quarter_numbers = pd.Series(periods.quarter, index=periods)
        standalone = ytd_profit.where(
            quarter_numbers.eq(1),
            ytd_profit - ytd_profit.shift(1),
        )
        unexpected = standalone - standalone.shift(4)
        denominator = (
            unexpected.shift(1)
            .rolling(
                STANDARDIZATION_WINDOW_QUARTERS,
                min_periods=MIN_STANDARDIZATION_OBSERVATIONS,
            )
            .std(ddof=1)
        )
        valid_denominator = (
            denominator.notna()
            & np.isfinite(denominator)
            & denominator.ne(0)
        )
        zero_denominators += int(
            (unexpected.notna() & denominator.eq(0)).sum()
        )
        grid["standalone_net_profit"] = standalone
        grid["unexpected_net_profit"] = unexpected
        grid["standardization_std"] = denominator
        grid[SUE_METRIC] = np.nan
        grid.loc[valid_denominator, SUE_METRIC] = (
            unexpected.loc[valid_denominator]
            / denominator.loc[valid_denominator]
        )
        actual = grid[grid["statDate"].notna()].copy()
        actual["code"] = code
        event_frames.append(actual.reset_index(names="period"))

    calculated = (
        pd.concat(event_frames, ignore_index=True)
        if event_frames
        else pd.DataFrame()
    )
    if calculated.empty:
        events = pd.DataFrame(
            columns=[
                "code",
                "report_end",
                "announcement_date",
                "available_date",
                SUE_METRIC,
            ]
        )
    else:
        events = pd.DataFrame(
            {
                "code": calculated["code"],
                "report_end": calculated["statDate"],
                "announcement_date": calculated["pubDate"],
                "available_date": calculated["pubDate"],
                SUE_METRIC: calculated[SUE_METRIC],
                "standalone_net_profit": calculated["standalone_net_profit"],
                "unexpected_net_profit": calculated["unexpected_net_profit"],
                "standardization_std": calculated["standardization_std"],
            }
        )

    source_counts = source.groupby("code").size().astype(int).to_dict()
    sue_counts = (
        events.dropna(subset=[SUE_METRIC]).groupby("code").size().astype(int).to_dict()
        if not events.empty
        else {}
    )
    audit = {
        "sourceRows": int(len(source)),
        "codes": int(source["code"].nunique()),
        "sourceQuartersPerCode": source_counts,
        "minimumSourceQuartersPerCode": min(source_counts.values(), default=0),
        "standaloneQuarterRows": int(
            events.get("standalone_net_profit", pd.Series(dtype=float)).notna().sum()
        ),
        "unexpectedEarningsRows": int(
            events.get("unexpected_net_profit", pd.Series(dtype=float)).notna().sum()
        ),
        "sueRows": int(events.get(SUE_METRIC, pd.Series(dtype=float)).notna().sum()),
        "sueQuartersPerCode": sue_counts,
        "codesWithAnySue": len(sue_counts),
        "continuityGapQuarters": continuity_gaps,
        "zeroDenominatorRows": zero_denominators,
        "standardizationWindowQuarters": STANDARDIZATION_WINDOW_QUARTERS,
        "minimumStandardizationObservations": MIN_STANDARDIZATION_OBSERVATIONS,
        "currentObservationExcludedFromDenominator": True,
        "revisionHistoryAvailable": False,
        "formula": (
            "standalone quarterly net profit seasonal change divided by the "
            "sample standard deviation of the prior eight seasonal changes"
        ),
    }
    return events.sort_values(["code", "report_end"]).reset_index(drop=True), audit


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


def build_baostock_sue_panel(
    events: pd.DataFrame,
    *,
    base_provider: Path,
    calendar: list[pd.Timestamp],
    source_windows: dict[str, tuple[pd.Timestamp, pd.Timestamp]],
    codes: list[str],
    publication_lag_sessions: int,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    scaffold = pd.DataFrame.from_records(
        (
            {"date": date, "code": code}
            for code in codes
            for date in calendar
            if source_windows[code][0] <= date <= source_windows[code][1]
        )
    )
    panel, audit = build_point_in_time_panel(
        events,
        scaffold,
        calendar,
        source_windows,
        codes,
        financial_metrics=(SUE_METRIC,),
        daily_basic_metrics=(),
        publication_lag_sessions=publication_lag_sessions,
    )
    stale_limit = panel["pit_report_end"] + pd.DateOffset(
        months=STALENESS_MONTHS
    )
    stale = panel["pit_report_end"].notna() & panel["date"].gt(stale_limit)
    stale_signals = int((stale & panel[SUE_FIELD].notna()).sum())
    panel.loc[stale, SUE_FIELD] = np.nan
    audit.update(
        {
            "sourceProvider": "baostock",
            "baseProvider": str(base_provider),
            "featureFields": [SUE_FIELD],
            "coverage": {SUE_FIELD: float(panel[SUE_FIELD].notna().mean())},
            "stalenessMonths": STALENESS_MONTHS,
            "staleSignalRowsCleared": stale_signals,
            "revisionHistoryAvailable": False,
            "revisionRisk": (
                "BaoStock returns the current historical snapshot and does not "
                "expose superseded quarterly report values."
            ),
        }
    )
    return panel.sort_values(["date", "code"]).reset_index(drop=True), audit


def _load_reusable_payload(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("code") != 0:
        raise ValueError(f"reusable BaoStock SUE payload is not successful: {path}")
    data = payload.get("data")
    if not isinstance(data, dict):
        raise ValueError(f"reusable BaoStock SUE payload has no data object: {path}")
    fields = data.get("fields")
    items = data.get("items")
    if not isinstance(fields, list) or not isinstance(items, list):
        raise ValueError(f"reusable BaoStock SUE payload has an invalid table: {path}")
    missing = sorted(set(QUARTERLY_PROFIT_FIELDS) - set(fields))
    if missing:
        raise ValueError(
            f"reusable BaoStock SUE payload is missing fields {missing}: {path}"
        )
    if any(not isinstance(row, list) or len(row) != len(fields) for row in items):
        raise ValueError(f"reusable BaoStock SUE payload has malformed rows: {path}")
    return payload


def _validate_reusable_window(
    payload: dict[str, Any],
    *,
    required_start_year: int,
    required_end_year: int,
) -> None:
    source = payload.get("source")
    if not isinstance(source, dict):
        raise ValueError("reusable BaoStock SUE payload has no source contract")
    if source.get("reportFrequency") != "quarterly":
        raise ValueError("reusable BaoStock SUE payload is not quarterly")
    actual_start = source.get("startYear")
    actual_end = source.get("endYear")
    if not isinstance(actual_start, int) or not isinstance(actual_end, int):
        raise ValueError("reusable BaoStock SUE payload has no query range")
    if actual_start > required_start_year or actual_end < required_end_year:
        raise ValueError(
            "reusable BaoStock SUE payload does not cover "
            f"{required_start_year}..{required_end_year}"
        )


def sync_baostock_sue(
    project_root: Path,
    request: BaoStockSueSyncRequest,
    client: BaoStockClient | Any | None = None,
) -> dict[str, Any]:
    started = datetime.now(timezone.utc)
    batch_id = request.batch_id or (
        f"baostock_sue_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}"
    )
    raw_dir = raw_provider_dir(project_root, RAW_PROVIDER_NAME, batch_id)
    stage_dir = staging_dir(project_root, batch_id)
    manifest_file = manifest_path(project_root, batch_id)
    report_file = report_path(project_root, batch_id)
    collisions = [
        path
        for path in (raw_dir, stage_dir, manifest_file, report_file)
        if path.exists()
    ]
    if collisions:
        raise FileExistsError(
            f"refusing to overwrite immutable sync artifacts: {collisions}"
        )
    if request.output_provider and request.output_provider.exists():
        raise FileExistsError(
            f"refusing to overwrite provider: {request.output_provider}"
        )

    base = request.base_provider.expanduser().resolve()
    codes = list(dict.fromkeys(_canonical_code(code) for code in request.codes))
    if not codes:
        raise ValueError("at least one code is required")
    listing_spans = _read_instrument_spans(base)
    missing_codes = sorted(set(codes) - set(listing_spans))
    if missing_codes:
        raise ValueError(
            f"requested codes are missing from base provider: {missing_codes[:10]}"
        )
    base_contract = provider_contract_fingerprint(base, request.market)
    requested_start = pd.Timestamp(request.start_date).normalize()
    requested_end = pd.Timestamp(request.end_date).normalize()
    if requested_end < requested_start:
        raise ValueError("end_date must be on or after start_date")
    market_spans = load_market_code_spans(
        base,
        request.market,
        request.start_date,
        request.end_date,
    )
    source_windows: dict[str, tuple[pd.Timestamp, pd.Timestamp]] = {}
    query_years: dict[str, tuple[int, int]] = {}
    for code in codes:
        listing_start, listing_end = listing_spans[code]
        market_start, market_end = market_spans.get(
            code,
            (requested_start, requested_end),
        )
        window_start = max(requested_start, listing_start, market_start)
        window_end = min(requested_end, listing_end, market_end)
        if window_end < window_start:
            raise ValueError(f"requested code has no usable source window: {code}")
        source_windows[code] = window_start, window_end
        query_years[code] = window_start.year - HISTORY_SEED_YEARS, window_end.year

    raw_dir.mkdir(parents=True)
    stage_dir.mkdir(parents=True)
    api = client or BaoStockClient()
    package_version = str(getattr(api, "package_version", "unknown"))
    payloads: list[dict[str, Any]] = []
    failures: dict[str, str] = {}
    raw_hashes: dict[str, str] = {}
    reused_responses = 0
    queried_responses = 0

    try:
        for code_index, code in enumerate(codes, start=1):
            code_dir = raw_dir / to_qlib_code(code)
            code_dir.mkdir()
            destination = code_dir / "profit_quarterly.json"
            start_year, end_year = query_years[code]
            try:
                reusable = (
                    request.reuse_raw_dir
                    / to_qlib_code(code)
                    / "profit_quarterly.json"
                    if request.reuse_raw_dir
                    else None
                )
                if reusable is not None and reusable.is_file():
                    payload = _load_reusable_payload(reusable)
                    _validate_reusable_window(
                        payload,
                        required_start_year=start_year,
                        required_end_year=end_year,
                    )
                    reused_responses += 1
                else:
                    payload = api.query_quarterly_history_raw(
                        "query_profit_data",
                        code=code,
                        start_year=start_year,
                        end_year=end_year,
                        required_fields=QUARTERLY_PROFIT_FIELDS,
                        request_interval_seconds=request.request_interval_seconds,
                    )
                    payload.setdefault("source", {})["capturedAt"] = datetime.now(
                        timezone.utc
                    ).isoformat()
                    queried_responses += 1
                _write_json(destination, payload)
                raw_hashes[code] = _sha256(destination)
                payloads.append(payload)
            except Exception as exc:
                failures[code] = str(exc)
            _write_json(
                stage_dir / "progress.json",
                {
                    "batchId": batch_id,
                    "status": "RUNNING",
                    "completedCodes": code_index,
                    "totalCodes": len(codes),
                    "codesFailed": len(failures),
                    "lastCode": code,
                    "reusedResponses": reused_responses,
                    "queriedResponses": queried_responses,
                    "updatedAt": datetime.now(timezone.utc).isoformat(),
                },
            )
    finally:
        closer = getattr(api, "close", None)
        if callable(closer):
            try:
                closer()
            except Exception as exc:
                failures["_client_close"] = str(exc)

    source_requests = sum(
        int((payload.get("source") or {}).get("requestCount") or 0)
        for payload in payloads
    )
    estimated_source_requests = sum(
        (end_year - start_year + 1) * 4
        for start_year, end_year in query_years.values()
    )
    manifest = {
        "schemaVersion": "baostock-seasonal-sue-sync-v1",
        "batchId": batch_id,
        "createdAt": started.isoformat(),
        "provider": "baostock",
        "packageVersion": package_version,
        "credentialsRequired": False,
        "credentialPersisted": False,
        "revisionHistoryAvailable": False,
        "request": {
            "baseProvider": str(base),
            "baseProviderFingerprint": base_contract["fingerprint"],
            "market": request.market,
            "codes": codes,
            "startDate": request.start_date,
            "endDate": request.end_date,
            "reportFrequency": "quarterly",
            "sourceFields": list(QUARTERLY_PROFIT_FIELDS),
            "historySeedYears": HISTORY_SEED_YEARS,
            "standardizationWindowQuarters": STANDARDIZATION_WINDOW_QUARTERS,
            "minimumStandardizationObservations": MIN_STANDARDIZATION_OBSERVATIONS,
            "publicationLagSessions": request.publication_lag_sessions,
            "stalenessMonths": STALENESS_MONTHS,
            "formula": (
                "standalone quarterly net profit seasonal change divided by "
                "sample std of the prior eight seasonal changes"
            ),
            "sourceWindows": {
                code: {
                    "startDate": source_windows[code][0].date().isoformat(),
                    "endDate": source_windows[code][1].date().isoformat(),
                    "queryStartYear": query_years[code][0],
                    "queryEndYear": query_years[code][1],
                }
                for code in codes
            },
            "outputProvider": (
                str(request.output_provider) if request.output_provider else None
            ),
            "reuseRawDirectory": (
                str(request.reuse_raw_dir) if request.reuse_raw_dir else None
            ),
            "estimatedSourceRequests": estimated_source_requests,
        },
        "rawDirectory": str(raw_dir),
        "rawPayloadSha256": raw_hashes,
        "sourceRequestsRepresented": source_requests,
        "reusedResponses": reused_responses,
        "queriedResponses": queried_responses,
        "failures": failures,
    }
    _write_json(manifest_file, manifest)

    output_info = None
    rows = 0
    audit: dict[str, Any] = {"status": "FAIL", "reason": "source requests failed"}
    if not failures:
        raw = pd.concat(
            [_payload_frame(payload) for payload in payloads],
            ignore_index=True,
        )
        events, source_audit = normalize_baostock_quarterly_sue(raw)
        panel, audit = build_baostock_sue_panel(
            events,
            base_provider=base,
            calendar=_calendar_between(base, request.start_date, request.end_date),
            source_windows=source_windows,
            codes=codes,
            publication_lag_sessions=request.publication_lag_sessions,
        )
        audit["sourceAudit"] = source_audit
        rows = len(panel)
        events.to_csv(
            stage_dir / "seasonal_sue_events.csv.gz",
            index=False,
            compression="gzip",
        )
        panel.to_csv(
            stage_dir / "point_in_time_sue.csv.gz",
            index=False,
            compression="gzip",
        )
        _write_json(stage_dir / "as_of_audit.json", audit)
        if request.output_provider and audit.get("status") == "PASS":
            output_info = write_enriched_qlib_provider(
                base,
                request.output_provider,
                panel,
                (SUE_FIELD,),
                batch_id=batch_id,
                source_manifest=manifest_file,
                as_of_audit=audit,
                market=request.market,
                as_of_policy={
                    "financialAvailability": (
                        "first provider session strictly after BaoStock pubDate"
                    ),
                    "reportFrequency": "quarterly",
                    "stalenessPolicy": (
                        "signal is missing after six calendar months from statDate"
                    ),
                    "standardizationPolicy": (
                        "current seasonal change divided by sample std of the prior "
                        "eight seasonal changes, minimum six observations"
                    ),
                    "restatementPolicy": (
                        "current BaoStock historical snapshot; revision vintages unavailable"
                    ),
                },
                source_metadata={
                    "provider": "baostock",
                    "packageVersion": package_version,
                    "credentialsRequired": False,
                    "revisionHistoryAvailable": False,
                    "signalKind": "seasonal_unexpected_aggregate_net_profit_proxy",
                },
            )

    finished = datetime.now(timezone.utc)
    result = {
        "batchId": batch_id,
        "status": (
            "PASS" if not failures and audit.get("status") == "PASS" else "FAIL"
        ),
        "provider": "baostock",
        "packageVersion": package_version,
        "codesRequested": len(codes),
        "codesSucceeded": len(codes)
        - len([key for key in failures if key != "_client_close"]),
        "codesFailed": len([key for key in failures if key != "_client_close"]),
        "rows": int(rows),
        "rawDir": str(raw_dir),
        "stagingDir": str(stage_dir),
        "manifestPath": str(manifest_file),
        "reportPath": str(report_file),
        "audit": audit,
        "outputProvider": output_info,
        "startedAt": started.isoformat(),
        "finishedAt": finished.isoformat(),
        "elapsedSeconds": (finished - started).total_seconds(),
        "failures": failures,
    }
    _write_json(
        stage_dir / "progress.json",
        {
            "batchId": batch_id,
            "status": result["status"],
            "completedCodes": len(codes),
            "totalCodes": len(codes),
            "codesFailed": result["codesFailed"],
            "lastCode": codes[-1],
            "reusedResponses": reused_responses,
            "queriedResponses": queried_responses,
            "updatedAt": finished.isoformat(),
        },
    )
    _write_json(report_file, result)
    return result
