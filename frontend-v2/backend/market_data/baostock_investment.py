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
    normalize_financial_events,
    write_enriched_qlib_provider,
)
from .paths import manifest_path, raw_provider_dir, report_path, staging_dir
from .providers.a_stock_data import to_qlib_code
from .providers.baostock import BaoStockClient, from_baostock_code


RAW_PROVIDER_NAME = "baostock_investment_to_assets"
ASSET_GROWTH_METRIC = "fy_asset_growth"
ASSET_GROWTH_FIELD = f"pit_{ASSET_GROWTH_METRIC}"
ANNUAL_GROWTH_FIELDS = (
    "code",
    "pubDate",
    "statDate",
    "YOYAsset",
)
HISTORY_SEED_YEARS = 1
STALENESS_MONTHS = 18


@dataclass(frozen=True)
class BaoStockInvestmentSyncRequest:
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


def _deduplicate_annual_rows(frame: pd.DataFrame) -> pd.DataFrame:
    keys = ["code", "statDate"]
    duplicates = frame[frame.duplicated(keys, keep=False)]
    if not duplicates.empty:
        for key, group in duplicates.groupby(keys, dropna=False, sort=False):
            if len(group.drop_duplicates()) > 1:
                display_key = key if isinstance(key, tuple) else (key,)
                raise ValueError(
                    "query_growth_data contains conflicting duplicate annual reports: "
                    f"{display_key}"
                )
    return frame.drop_duplicates(keys, keep="last").reset_index(drop=True)


def normalize_baostock_annual_asset_growth(
    rows: pd.DataFrame,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    source = rows.copy()
    _require_columns(source, ANNUAL_GROWTH_FIELDS, "query_growth_data")
    source["code"] = source["code"].map(from_baostock_code)
    source["pubDate"] = pd.to_datetime(source["pubDate"], errors="coerce")
    source["statDate"] = pd.to_datetime(source["statDate"], errors="coerce")
    if source[["pubDate", "statDate"]].isna().any(axis=None):
        raise ValueError("query_growth_data contains invalid publication or report dates")
    annual = source["statDate"].dt.month.eq(12) & source["statDate"].dt.day.eq(31)
    if (~annual).any():
        raise ValueError("query_growth_data contains a non-annual report")
    if (source["pubDate"] < source["statDate"]).any():
        raise ValueError("query_growth_data contains a report published before period end")

    source[ASSET_GROWTH_METRIC] = pd.to_numeric(
        source["YOYAsset"],
        errors="coerce",
    )
    source = _deduplicate_annual_rows(source)
    source["formation_cutoff"] = source["statDate"].map(
        lambda value: pd.Timestamp(year=value.year + 1, month=6, day=30)
    )
    late_publication = source["pubDate"].gt(source["formation_cutoff"])
    eligible = source.loc[~late_publication].copy()
    financial = pd.DataFrame(
        {
            "ts_code": eligible["code"],
            "ann_date": eligible["pubDate"],
            "end_date": eligible["statDate"],
            ASSET_GROWTH_METRIC: eligible[ASSET_GROWTH_METRIC],
        }
    )
    disclosures = pd.DataFrame(
        {
            "ts_code": eligible["code"],
            "end_date": eligible["statDate"],
            "actual_date": eligible["pubDate"],
        }
    )
    events = normalize_financial_events(
        financial,
        disclosures,
        metric_fields=(ASSET_GROWTH_METRIC,),
    )
    events["available_date"] = events["report_end"].map(
        lambda value: pd.Timestamp(year=value.year + 1, month=6, day=30)
    )
    counts = source.groupby("code").size().astype(int).to_dict()
    valid = source.dropna(subset=[ASSET_GROWTH_METRIC])
    audit = {
        "sourceRows": int(len(source)),
        "codes": int(source["code"].nunique()),
        "validAssetGrowthRows": int(len(valid)),
        "sourceCoverage": float(len(valid) / len(source)) if len(source) else 0.0,
        "formationEligibleRows": int(len(eligible)),
        "latePublicationRowsExcluded": int(late_publication.sum()),
        "sourceYearsPerCode": counts,
        "minimumSourceYearsPerCode": min(counts.values(), default=0),
        "revisionHistoryAvailable": False,
        "formula": (
            "query_growth_data.YOYAsset = (assets_t - assets_t-1) / "
            "abs(assets_t-1); annual portfolios form after June 30"
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


def build_baostock_investment_panel(
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
        financial_metrics=(ASSET_GROWTH_METRIC,),
        daily_basic_metrics=(),
        publication_lag_sessions=publication_lag_sessions,
    )
    stale_limit = panel["pit_report_end"] + pd.DateOffset(months=STALENESS_MONTHS)
    stale = panel["pit_report_end"].notna() & panel["date"].gt(stale_limit)
    stale_signals = int((stale & panel[ASSET_GROWTH_FIELD].notna()).sum())
    panel.loc[stale, ASSET_GROWTH_FIELD] = np.nan
    audit.update(
        {
            "sourceProvider": "baostock",
            "baseProvider": str(base_provider),
            "featureFields": [ASSET_GROWTH_FIELD],
            "coverage": {
                ASSET_GROWTH_FIELD: float(panel[ASSET_GROWTH_FIELD].notna().mean())
            },
            "stalenessMonths": STALENESS_MONTHS,
            "staleSignalRowsCleared": stale_signals,
            "revisionHistoryAvailable": False,
            "revisionRisk": (
                "BaoStock returns the current historical snapshot and does not "
                "expose superseded annual report values."
            ),
        }
    )
    return panel.sort_values(["date", "code"]).reset_index(drop=True), audit


def _load_reusable_payload(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("code") != 0:
        raise ValueError(f"reusable BaoStock investment payload is not successful: {path}")
    data = payload.get("data")
    if not isinstance(data, dict):
        raise ValueError(f"reusable BaoStock investment payload has no data object: {path}")
    fields = data.get("fields")
    items = data.get("items")
    if not isinstance(fields, list) or not isinstance(items, list):
        raise ValueError(f"reusable BaoStock investment payload has an invalid table: {path}")
    missing = sorted(set(ANNUAL_GROWTH_FIELDS) - set(fields))
    if missing:
        raise ValueError(
            f"reusable BaoStock investment payload is missing fields {missing}: {path}"
        )
    if any(not isinstance(row, list) or len(row) != len(fields) for row in items):
        raise ValueError(f"reusable BaoStock investment payload has malformed rows: {path}")
    return payload


def _validate_reusable_window(
    payload: dict[str, Any],
    *,
    required_start_year: int,
    required_end_year: int,
) -> None:
    source = payload.get("source")
    if not isinstance(source, dict):
        raise ValueError("reusable BaoStock investment payload has no source contract")
    if source.get("reportFrequency") != "annual" or source.get("quarter") != 4:
        raise ValueError("reusable BaoStock investment payload is not annual quarter 4")
    actual_start = source.get("startYear")
    actual_end = source.get("endYear")
    if not isinstance(actual_start, int) or not isinstance(actual_end, int):
        raise ValueError("reusable BaoStock investment payload has no query range")
    if actual_start > required_start_year or actual_end < required_end_year:
        raise ValueError(
            "reusable BaoStock investment payload does not cover "
            f"{required_start_year}..{required_end_year}"
        )


def sync_baostock_investment(
    project_root: Path,
    request: BaoStockInvestmentSyncRequest,
    client: BaoStockClient | Any | None = None,
) -> dict[str, Any]:
    started = datetime.now(timezone.utc)
    batch_id = request.batch_id or (
        "baostock_investment_"
        f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}"
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
        raise FileExistsError(f"refusing to overwrite provider: {request.output_provider}")

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
            destination = code_dir / "growth_annual.json"
            start_year, end_year = query_years[code]
            try:
                reusable = (
                    request.reuse_raw_dir
                    / to_qlib_code(code)
                    / "growth_annual.json"
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
                    payload = api.query_annual_history_raw(
                        "query_growth_data",
                        code=code,
                        start_year=start_year,
                        end_year=end_year,
                        required_fields=ANNUAL_GROWTH_FIELDS,
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
        end_year - start_year + 1 for start_year, end_year in query_years.values()
    )
    manifest = {
        "schemaVersion": "baostock-investment-to-assets-sync-v1",
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
            "reportFrequency": "annual",
            "annualQuarter": 4,
            "sourceFields": list(ANNUAL_GROWTH_FIELDS),
            "historySeedYears": HISTORY_SEED_YEARS,
            "publicationLagSessions": request.publication_lag_sessions,
            "stalenessMonths": STALENESS_MONTHS,
            "formula": "pit_fy_asset_growth = query_growth_data.YOYAsset",
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
        events, source_audit = normalize_baostock_annual_asset_growth(raw)
        panel, audit = build_baostock_investment_panel(
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
            stage_dir / "investment_to_assets_events.csv.gz",
            index=False,
            compression="gzip",
        )
        panel.to_csv(
            stage_dir / "point_in_time_investment.csv.gz",
            index=False,
            compression="gzip",
        )
        _write_json(stage_dir / "as_of_audit.json", audit)
        if request.output_provider and audit.get("status") == "PASS":
            output_info = write_enriched_qlib_provider(
                base,
                request.output_provider,
                panel,
                (ASSET_GROWTH_FIELD,),
                batch_id=batch_id,
                source_manifest=manifest_file,
                as_of_audit=audit,
                market=request.market,
                as_of_policy={
                    "financialAvailability": (
                        "first provider session strictly after the June 30 formation "
                        "cutoff; reports published after the cutoff are excluded"
                    ),
                    "reportFrequency": "annual quarter 4 only",
                    "stalenessPolicy": (
                        "signal is missing after 18 calendar months from statDate"
                    ),
                    "factorDirection": (
                        "provider stores asset growth; candidate expression negates it"
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
                    "signalKind": "annual_investment_to_assets",
                },
            )

    finished = datetime.now(timezone.utc)
    result = {
        "batchId": batch_id,
        "status": "PASS" if not failures and audit.get("status") == "PASS" else "FAIL",
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
