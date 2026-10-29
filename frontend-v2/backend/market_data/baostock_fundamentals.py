from __future__ import annotations

import json
import time
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
    _write_json,
    build_point_in_time_panel,
    load_market_code_spans,
    normalize_daily_basic,
    normalize_financial_events,
    write_enriched_qlib_provider,
)
from .paths import manifest_path, raw_provider_dir, report_path, staging_dir
from .providers.a_stock_data import to_qlib_code
from .providers.baostock import BaoStockClient, from_baostock_code


BAOSTOCK_FINANCIAL_METRICS = ("fy_roe_avg", "fy_netprofit_yoy")
BAOSTOCK_DAILY_METRICS = ("pb",)
BAOSTOCK_EARNINGS_TO_PRICE_METRIC = "earnings_to_price"
PROFIT_FIELDS = ("code", "pubDate", "statDate", "roeAvg")
GROWTH_FIELDS = ("code", "pubDate", "statDate", "YOYNI")
VALUATION_FIELDS = ("date", "code", "pbMRQ", "tradestatus", "isST")
VALUATION_EARNINGS_TO_PRICE_FIELDS = (
    "date",
    "code",
    "pbMRQ",
    "peTTM",
    "tradestatus",
    "isST",
)
RAW_PROVIDER_NAME = "baostock_fundamentals"


@dataclass(frozen=True)
class BaoStockFundamentalSyncRequest:
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
    include_earnings_to_price: bool = False


class ReusablePayloadFieldMismatch(ValueError):
    def __init__(self, path: Path, missing_fields: list[str]):
        self.path = path
        self.missing_fields = missing_fields
        super().__init__(
            f"reusable BaoStock payload is missing fields {missing_fields}: {path}"
        )


def _load_reusable_payload(
    path: Path,
    expected_fields: Iterable[str],
) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("code") != 0:
        raise ValueError(f"reusable BaoStock payload is not successful: {path}")
    data = payload.get("data")
    if not isinstance(data, dict):
        raise ValueError(f"reusable BaoStock payload has no data object: {path}")
    fields = data.get("fields")
    items = data.get("items")
    if not isinstance(fields, list) or not isinstance(items, list):
        raise ValueError(f"reusable BaoStock payload has an invalid table: {path}")
    missing = sorted(set(expected_fields) - set(fields))
    if missing:
        raise ReusablePayloadFieldMismatch(path, missing)
    if any(not isinstance(row, list) or len(row) != len(fields) for row in items):
        raise ValueError(f"reusable BaoStock payload contains malformed rows: {path}")
    return payload


def _validate_reusable_window(
    payload: dict[str, Any],
    dataset: str,
    start_date: pd.Timestamp,
    end_date: pd.Timestamp,
) -> None:
    source = payload.get("source")
    if not isinstance(source, dict):
        raise ValueError(f"reusable BaoStock {dataset} payload has no source window")
    if dataset in {"profit_annual", "growth_annual"}:
        required_start_year = start_date.year - 1
        required_end_year = end_date.year
        actual_start_year = source.get("startYear")
        actual_end_year = source.get("endYear")
        if not isinstance(actual_start_year, int) or not isinstance(
            actual_end_year, int
        ):
            raise ValueError(
                f"reusable BaoStock {dataset} payload has no annual query range"
            )
        if (
            actual_start_year > required_start_year
            or actual_end_year < required_end_year
        ):
            raise ValueError(
                f"reusable BaoStock {dataset} payload does not cover "
                f"{required_start_year}..{required_end_year}"
            )
        return
    actual_start = pd.to_datetime(source.get("startDate"), errors="coerce")
    actual_end = pd.to_datetime(source.get("endDate"), errors="coerce")
    if pd.isna(actual_start) or pd.isna(actual_end):
        raise ValueError(
            "reusable BaoStock daily_valuation payload has no daily query range"
        )
    if actual_start > start_date or actual_end < end_date:
        raise ValueError(
            "reusable BaoStock daily_valuation payload does not cover "
            f"{start_date.date()}..{end_date.date()}"
        )


def _require_columns(frame: pd.DataFrame, required: Iterable[str], table: str) -> None:
    missing = sorted(set(required) - set(frame.columns))
    if missing:
        raise ValueError(f"{table} is missing required columns: {missing}")


def _deduplicate_source_rows(
    frame: pd.DataFrame,
    *,
    keys: list[str],
    table: str,
) -> pd.DataFrame:
    duplicates = frame[frame.duplicated(keys, keep=False)]
    if not duplicates.empty:
        for key, group in duplicates.groupby(keys, dropna=False, sort=False):
            if len(group.drop_duplicates()) > 1:
                display_key = key if isinstance(key, tuple) else (key,)
                raise ValueError(
                    f"{table} contains conflicting duplicate rows: {display_key}"
                )
    return frame.drop_duplicates(keys, keep="last").reset_index(drop=True)


def normalize_baostock_annual_events(
    profit_rows: pd.DataFrame,
    growth_rows: pd.DataFrame,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    profit = profit_rows.copy()
    growth = growth_rows.copy()
    _require_columns(profit, PROFIT_FIELDS, "query_profit_data")
    _require_columns(growth, GROWTH_FIELDS, "query_growth_data")

    for frame, table in (
        (profit, "query_profit_data"),
        (growth, "query_growth_data"),
    ):
        frame["code"] = frame["code"].map(from_baostock_code)
        frame["statDate"] = pd.to_datetime(frame["statDate"], errors="coerce")
        frame["pubDate"] = pd.to_datetime(frame["pubDate"], errors="coerce")
        if frame[["statDate", "pubDate"]].isna().any(axis=None):
            raise ValueError(f"{table} contains invalid publication or report dates")
        if (frame["statDate"].dt.month != 12).any():
            raise ValueError(f"{table} contains a non-annual report")

    profit["fy_roe_avg"] = pd.to_numeric(profit["roeAvg"], errors="coerce")
    growth["fy_netprofit_yoy"] = pd.to_numeric(growth["YOYNI"], errors="coerce")
    profit = _deduplicate_source_rows(
        profit[["code", "statDate", "pubDate", "fy_roe_avg"]],
        keys=["code", "statDate"],
        table="query_profit_data",
    ).rename(columns={"pubDate": "profit_pub_date"})
    growth = _deduplicate_source_rows(
        growth[["code", "statDate", "pubDate", "fy_netprofit_yoy"]],
        keys=["code", "statDate"],
        table="query_growth_data",
    ).rename(columns={"pubDate": "growth_pub_date"})

    merged = profit.merge(
        growth,
        on=["code", "statDate"],
        how="outer",
        validate="one_to_one",
    )
    merged["announcement_date"] = merged[
        ["profit_pub_date", "growth_pub_date"]
    ].max(axis=1)
    if merged["announcement_date"].isna().any():
        raise ValueError("BaoStock annual rows contain no usable publication date")
    publication_mismatch = int(
        (
            merged["profit_pub_date"].notna()
            & merged["growth_pub_date"].notna()
            & (merged["profit_pub_date"] != merged["growth_pub_date"])
        ).sum()
    )

    financial = pd.DataFrame(
        {
            "ts_code": merged["code"],
            "ann_date": merged["announcement_date"],
            "end_date": merged["statDate"],
            "fy_roe_avg": merged["fy_roe_avg"],
            "fy_netprofit_yoy": merged["fy_netprofit_yoy"],
        }
    )
    disclosures = pd.DataFrame(
        {
            "ts_code": merged["code"],
            "end_date": merged["statDate"],
            "actual_date": merged["announcement_date"],
        }
    )
    events = normalize_financial_events(
        financial,
        disclosures,
        BAOSTOCK_FINANCIAL_METRICS,
    )
    audit = {
        "annualRows": int(len(merged)),
        "profitRows": int(len(profit)),
        "growthRows": int(len(growth)),
        "publicationDateMismatchRows": publication_mismatch,
        "publicationBundlePolicy": "max(profit.pubDate, growth.pubDate)",
        "revisionHistoryAvailable": False,
    }
    return events, audit


def normalize_baostock_daily_valuation(
    rows: pd.DataFrame,
    *,
    include_earnings_to_price: bool = False,
) -> pd.DataFrame:
    source = rows.copy()
    valuation_fields = (
        VALUATION_EARNINGS_TO_PRICE_FIELDS
        if include_earnings_to_price
        else VALUATION_FIELDS
    )
    daily_metrics = BAOSTOCK_DAILY_METRICS + (
        (BAOSTOCK_EARNINGS_TO_PRICE_METRIC,)
        if include_earnings_to_price
        else ()
    )
    _require_columns(source, valuation_fields, "query_history_k_data_plus")
    normalized = pd.DataFrame(
        {
            "ts_code": source["code"].map(from_baostock_code),
            "trade_date": source["date"],
            "pb": source["pbMRQ"],
        }
    )
    if include_earnings_to_price:
        pe_ttm = pd.to_numeric(source["peTTM"], errors="coerce")
        valid = pe_ttm.notna() & np.isfinite(pe_ttm) & pe_ttm.ne(0)
        normalized[BAOSTOCK_EARNINGS_TO_PRICE_METRIC] = np.nan
        normalized.loc[valid, BAOSTOCK_EARNINGS_TO_PRICE_METRIC] = (
            1.0 / pe_ttm.loc[valid]
        )
    return normalize_daily_basic(normalized, daily_metrics)


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


def sync_baostock_fundamentals(
    project_root: Path,
    request: BaoStockFundamentalSyncRequest,
    client: BaoStockClient | Any | None = None,
) -> dict[str, Any]:
    started = datetime.now(timezone.utc)
    batch_id = request.batch_id or (
        f"baostock_fundamentals_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}"
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
    market_spans = load_market_code_spans(
        base,
        request.market,
        request.start_date,
        request.end_date,
    )
    source_windows: dict[str, tuple[pd.Timestamp, pd.Timestamp]] = {}
    for code in codes:
        listing_start, listing_end = listing_spans[code]
        market_start, market_end = market_spans.get(
            code,
            (requested_start, requested_end),
        )
        window_start = max(requested_start, listing_start, market_start)
        window_end = min(requested_end, listing_end, market_end)
        if window_end < window_start:
            raise ValueError(
                f"requested code has no usable source window: {code}"
            )
        source_windows[code] = window_start, window_end

    raw_dir.mkdir(parents=True)
    stage_dir.mkdir(parents=True)
    api = client or BaoStockClient()
    package_version = str(getattr(api, "package_version", "unknown"))
    payloads: dict[str, list[dict[str, Any]]] = {
        "profit_annual": [],
        "growth_annual": [],
        "daily_valuation": [],
    }
    failures: dict[str, str] = {}
    reused_responses = 0
    queried_responses = 0
    reuse_misses: list[dict[str, Any]] = []
    valuation_fields = (
        VALUATION_EARNINGS_TO_PRICE_FIELDS
        if request.include_earnings_to_price
        else VALUATION_FIELDS
    )
    daily_metrics = BAOSTOCK_DAILY_METRICS + (
        (BAOSTOCK_EARNINGS_TO_PRICE_METRIC,)
        if request.include_earnings_to_price
        else ()
    )
    expected_fields = {
        "profit_annual": PROFIT_FIELDS,
        "growth_annual": GROWTH_FIELDS,
        "daily_valuation": valuation_fields,
    }

    try:
        for code_index, code in enumerate(codes, start=1):
            window_start, window_end = source_windows[code]
            start_year = window_start.year - 1
            end_year = window_end.year
            code_dir = raw_dir / to_qlib_code(code)
            code_dir.mkdir()
            try:
                for dataset in (
                    "profit_annual",
                    "growth_annual",
                    "daily_valuation",
                ):
                    reusable = (
                        request.reuse_raw_dir
                        / to_qlib_code(code)
                        / f"{dataset}.json"
                        if request.reuse_raw_dir
                        else None
                    )
                    was_reused = False
                    if reusable is not None and reusable.is_file():
                        try:
                            payload = _load_reusable_payload(
                                reusable,
                                expected_fields[dataset],
                            )
                            _validate_reusable_window(
                                payload,
                                dataset,
                                window_start,
                                window_end,
                            )
                            was_reused = True
                            reused_responses += 1
                        except ReusablePayloadFieldMismatch as exc:
                            reuse_misses.append(
                                {
                                    "code": code,
                                    "dataset": dataset,
                                    "path": str(exc.path),
                                    "missingFields": exc.missing_fields,
                                    "action": "refetch",
                                }
                            )
                    if was_reused:
                        pass
                    elif dataset == "profit_annual":
                        payload = api.query_annual_history_raw(
                            "query_profit_data",
                            code=code,
                            start_year=start_year,
                            end_year=end_year,
                            required_fields=PROFIT_FIELDS,
                            request_interval_seconds=request.request_interval_seconds,
                        )
                        queried_responses += 1
                    elif dataset == "growth_annual":
                        payload = api.query_annual_history_raw(
                            "query_growth_data",
                            code=code,
                            start_year=start_year,
                            end_year=end_year,
                            required_fields=GROWTH_FIELDS,
                            request_interval_seconds=request.request_interval_seconds,
                        )
                        queried_responses += 1
                    else:
                        payload = api.query_daily_valuation_raw(
                            code=code,
                            start_date=window_start.date().isoformat(),
                            end_date=window_end.date().isoformat(),
                            required_fields=valuation_fields,
                        )
                        queried_responses += 1
                    _write_json(code_dir / f"{dataset}.json", payload)
                    payloads[dataset].append(payload)
                    if (
                        not was_reused
                        and request.request_interval_seconds > 0
                        and dataset != "daily_valuation"
                    ):
                        time.sleep(request.request_interval_seconds)
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
                    "reuseMisses": len(reuse_misses),
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

    estimated_source_requests = sum(
        (window_end.year - (window_start.year - 1) + 1) * 2 + 1
        for window_start, window_end in source_windows.values()
    )
    manifest = {
        "schemaVersion": "baostock-fundamental-sync-v1",
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
            "sourceWindowPolicy": (
                "intersection of requested dates, listing span, and the code's "
                "point-in-time market membership envelope; annual queries seed one prior year"
            ),
            "sourceWindows": {
                code: {
                    "startDate": window_start.date().isoformat(),
                    "endDate": window_end.date().isoformat(),
                    "seedStartYear": window_start.year - 1,
                    "endYear": window_end.year,
                }
                for code, (window_start, window_end) in source_windows.items()
            },
            "publicationLagSessions": request.publication_lag_sessions,
            "includeEarningsToPrice": request.include_earnings_to_price,
            "dailyMetrics": list(daily_metrics),
            "valuationFields": list(valuation_fields),
            "outputProvider": (
                str(request.output_provider) if request.output_provider else None
            ),
            "reuseRawDirectory": (
                str(request.reuse_raw_dir) if request.reuse_raw_dir else None
            ),
            "estimatedSourceRequests": estimated_source_requests,
        },
        "rawDirectory": str(raw_dir),
        "reusedResponses": reused_responses,
        "queriedResponses": queried_responses,
        "reuseMisses": reuse_misses,
        "failures": failures,
    }
    _write_json(manifest_file, manifest)

    output_info = None
    audit: dict[str, Any] = {"status": "FAIL", "reason": "source requests failed"}
    rows = 0
    if not failures:
        profit_raw = pd.concat(
            [_payload_frame(payload) for payload in payloads["profit_annual"]],
            ignore_index=True,
        )
        growth_raw = pd.concat(
            [_payload_frame(payload) for payload in payloads["growth_annual"]],
            ignore_index=True,
        )
        valuation_raw = pd.concat(
            [_payload_frame(payload) for payload in payloads["daily_valuation"]],
            ignore_index=True,
        )
        events, source_audit = normalize_baostock_annual_events(
            profit_raw,
            growth_raw,
        )
        daily = normalize_baostock_daily_valuation(
            valuation_raw,
            include_earnings_to_price=request.include_earnings_to_price,
        )
        panel, audit = build_point_in_time_panel(
            events,
            daily,
            _calendar_between(base, request.start_date, request.end_date),
            source_windows,
            codes,
            BAOSTOCK_FINANCIAL_METRICS,
            daily_metrics,
            request.publication_lag_sessions,
        )
        audit.update(
            {
                "sourceProvider": "baostock",
                "sourcePackageVersion": package_version,
                "reportFrequency": "annual",
                "sourceAudit": source_audit,
                "revisionHistoryAvailable": False,
                "revisionRisk": (
                    "BaoStock returns the current historical snapshot and does not "
                    "expose revision vintages; pubDate lag prevents ordinary announcement "
                    "look-ahead but cannot reconstruct superseded values."
                ),
            }
        )
        rows = len(panel)
        events.to_csv(
            stage_dir / "financial_events.csv.gz",
            index=False,
            compression="gzip",
        )
        daily.to_csv(
            stage_dir / "daily_basic.csv.gz",
            index=False,
            compression="gzip",
        )
        panel.to_csv(
            stage_dir / "point_in_time_features.csv.gz",
            index=False,
            compression="gzip",
        )
        _write_json(stage_dir / "as_of_audit.json", audit)
        if request.output_provider and audit.get("status") == "PASS":
            output_info = write_enriched_qlib_provider(
                base,
                request.output_provider,
                panel,
                audit["featureFields"],
                batch_id=batch_id,
                source_manifest=manifest_file,
                as_of_audit=audit,
                market=request.market,
                as_of_policy={
                    "financialAvailability": (
                        "first provider session strictly after max(profit.pubDate, growth.pubDate)"
                    ),
                    "dailyValuationAvailability": (
                        "history quotation trade-date close for next-session execution"
                    ),
                    "earningsToPriceProxy": (
                        "signed reciprocal of BaoStock peTTM; zero or missing PE is missing"
                        if request.include_earnings_to_price
                        else None
                    ),
                    "reportFrequency": "annual quarter 4 only",
                    "restatementPolicy": (
                        "current BaoStock historical snapshot; revision vintages unavailable"
                    ),
                },
                source_metadata={
                    "provider": "baostock",
                    "packageVersion": package_version,
                    "credentialsRequired": False,
                    "revisionHistoryAvailable": False,
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
        "codesSucceeded": len(codes) - len([key for key in failures if key != "_client_close"]),
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
            "reuseMisses": len(reuse_misses),
            "updatedAt": finished.isoformat(),
        },
    )
    _write_json(report_file, result)
    return result
