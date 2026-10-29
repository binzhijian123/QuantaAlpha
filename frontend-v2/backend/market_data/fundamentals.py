from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import numpy as np
import pandas as pd

from quantaalpha.utils.qlib_provider import provider_contract_fingerprint

from .paths import manifest_path, raw_provider_dir, report_path, staging_dir
from .providers.a_stock_data import normalize_stock_code, to_qlib_code
from .providers.tushare import TushareProClient, to_tushare_code, to_tushare_date


DEFAULT_FINANCIAL_METRICS = (
    "q_dt_roe",
    "q_ocf_to_sales",
    "q_netprofit_yoy",
    "debt_to_assets",
)
DEFAULT_DAILY_BASIC_METRICS = (
    "pb",
    "pe_ttm",
    "total_mv",
    "circ_mv",
)
DROE_SOURCE_METRIC = "q_roe"
DROE_FEATURE = "pit_droe_1y"
DROE_STALENESS_MONTHS = 6
DROE_PROMOTION_MIN_CODES = 600
DROE_PROMOTION_MIN_COVERAGE = 0.50
EXTENSION_MANIFEST = "quantaalpha_extension.manifest.json"
_FIELD_RE = re.compile(r"^[a-z][a-z0-9_]*$")


@dataclass(frozen=True)
class FundamentalSyncRequest:
    base_provider: Path
    codes: list[str]
    start_date: str
    end_date: str
    market: str = "csi300"
    financial_metrics: tuple[str, ...] = field(
        default_factory=lambda: DEFAULT_FINANCIAL_METRICS
    )
    daily_basic_metrics: tuple[str, ...] = field(
        default_factory=lambda: DEFAULT_DAILY_BASIC_METRICS
    )
    publication_lag_sessions: int = 1
    output_provider: Path | None = None
    batch_id: str | None = None
    request_interval_seconds: float = 0.15
    reuse_raw_dir: Path | None = None


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    temporary.replace(path)


def _payload_frame(payload: dict[str, Any]) -> pd.DataFrame:
    data = payload.get("data") or {}
    return pd.DataFrame(data.get("items") or [], columns=data.get("fields") or [])


def _load_reusable_payload(
    path: Path,
    expected_fields: Iterable[str],
) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("code") != 0:
        raise ValueError(f"reusable Tushare payload is not successful: {path}")
    data = payload.get("data")
    if not isinstance(data, dict):
        raise ValueError(f"reusable Tushare payload has no data object: {path}")
    fields = data.get("fields")
    items = data.get("items")
    if not isinstance(fields, list) or not isinstance(items, list):
        raise ValueError(f"reusable Tushare payload has an invalid table: {path}")
    missing = sorted(set(expected_fields) - set(fields))
    if missing:
        raise ValueError(f"reusable Tushare payload is missing fields {missing}: {path}")
    if any(not isinstance(row, list) or len(row) != len(fields) for row in items):
        raise ValueError(f"reusable Tushare payload contains malformed rows: {path}")
    return payload


def _canonical_code(code: str) -> str:
    digits, exchange, _ = normalize_stock_code(code)
    return f"{digits}.{exchange}"


def _validate_metric_fields(fields: Iterable[str]) -> tuple[str, ...]:
    result = tuple(dict.fromkeys(str(field).strip() for field in fields if str(field).strip()))
    invalid = [field for field in result if not _FIELD_RE.fullmatch(field)]
    if invalid:
        raise ValueError(f"invalid Tushare metric fields: {invalid}")
    return result


def _parse_tushare_dates(series: pd.Series) -> pd.Series:
    values = series.astype("string").str.replace("-", "", regex=False)
    return pd.to_datetime(values, format="%Y%m%d", errors="coerce")


def _require_columns(frame: pd.DataFrame, required: Iterable[str], table: str) -> None:
    missing = sorted(set(required) - set(frame.columns))
    if missing:
        raise ValueError(f"{table} is missing required columns: {missing}")


def _reject_conflicting_duplicates(
    frame: pd.DataFrame,
    keys: list[str],
    values: list[str],
    table: str,
) -> pd.DataFrame:
    duplicate_groups = frame[frame.duplicated(keys, keep=False)].groupby(
        keys, dropna=False, sort=False
    )
    conflicts = []
    for key, group in duplicate_groups:
        if len(group[values].drop_duplicates()) > 1:
            conflicts.append(key if isinstance(key, tuple) else (key,))
    if conflicts:
        raise ValueError(f"{table} contains conflicting duplicate rows: {conflicts[:5]}")
    return frame.drop_duplicates(keys, keep="last").reset_index(drop=True)


def normalize_financial_events(
    financial_rows: pd.DataFrame,
    disclosure_rows: pd.DataFrame,
    metric_fields: Iterable[str] = DEFAULT_FINANCIAL_METRICS,
) -> pd.DataFrame:
    metrics = _validate_metric_fields(metric_fields)
    financial = financial_rows.copy()
    _require_columns(
        financial,
        ("ts_code", "ann_date", "end_date", *metrics),
        "fina_indicator",
    )
    financial["code"] = financial["ts_code"].map(_canonical_code)
    financial["announcement_date"] = _parse_tushare_dates(financial["ann_date"])
    financial["report_end"] = _parse_tushare_dates(financial["end_date"])
    for metric in metrics:
        financial[metric] = pd.to_numeric(financial[metric], errors="coerce")
    invalid_dates = financial[
        financial[["announcement_date", "report_end"]].isna().any(axis=1)
    ]
    if not invalid_dates.empty:
        raise ValueError(
            f"fina_indicator contains {len(invalid_dates)} rows with invalid dates"
        )

    disclosures = disclosure_rows.copy()
    _require_columns(
        disclosures,
        ("ts_code", "end_date", "actual_date"),
        "disclosure_date",
    )
    disclosures["code"] = disclosures["ts_code"].map(_canonical_code)
    disclosures["report_end"] = _parse_tushare_dates(disclosures["end_date"])
    disclosures["disclosure_actual_date"] = _parse_tushare_dates(
        disclosures["actual_date"]
    )
    disclosures = disclosures.dropna(subset=["report_end"])
    actual = (
        disclosures.dropna(subset=["disclosure_actual_date"])
        .groupby(["code", "report_end"], as_index=False)["disclosure_actual_date"]
        .max()
    )

    selected_columns = [
        "code",
        "report_end",
        "announcement_date",
        *metrics,
    ]
    if "update_flag" in financial:
        selected_columns.append("update_flag")
    events = financial[selected_columns].merge(
        actual,
        on=["code", "report_end"],
        how="left",
        validate="many_to_one",
    )
    events["available_date"] = events[
        ["announcement_date", "disclosure_actual_date"]
    ].max(axis=1)
    bad_temporal_order = events[events["available_date"] < events["report_end"]]
    if not bad_temporal_order.empty:
        raise ValueError(
            "fina_indicator contains reports available before their report period ended"
        )

    duplicate_values = [
        "available_date",
        "disclosure_actual_date",
        *metrics,
    ]
    if "update_flag" in events:
        duplicate_values.append("update_flag")
    events = _reject_conflicting_duplicates(
        events,
        ["code", "report_end", "announcement_date"],
        duplicate_values,
        "fina_indicator",
    )
    return events.sort_values(
        ["code", "available_date", "report_end", "announcement_date"]
    ).reset_index(drop=True)


def normalize_daily_basic(
    daily_rows: pd.DataFrame,
    metric_fields: Iterable[str] = DEFAULT_DAILY_BASIC_METRICS,
) -> pd.DataFrame:
    metrics = _validate_metric_fields(metric_fields)
    if not metrics:
        return pd.DataFrame(columns=["date", "code"])
    daily = daily_rows.copy()
    _require_columns(daily, ("ts_code", "trade_date", *metrics), "daily_basic")
    daily["code"] = daily["ts_code"].map(_canonical_code)
    daily["date"] = _parse_tushare_dates(daily["trade_date"])
    if daily["date"].isna().any():
        raise ValueError("daily_basic contains invalid trade_date values")
    for metric in metrics:
        daily[metric] = pd.to_numeric(daily[metric], errors="coerce")
    daily = _reject_conflicting_duplicates(
        daily,
        ["code", "date"],
        list(metrics),
        "daily_basic",
    )
    return daily[["date", "code", *metrics]].sort_values(
        ["code", "date"]
    ).reset_index(drop=True)


def _read_calendar(provider: Path) -> list[pd.Timestamp]:
    path = provider / "calendars" / "day.txt"
    if not path.exists():
        raise FileNotFoundError(f"provider calendar is missing: {path}")
    calendar = [pd.Timestamp(value) for value in path.read_text(encoding="utf-8").splitlines() if value]
    if not calendar or calendar != sorted(set(calendar)):
        raise ValueError(f"provider calendar is empty, duplicated, or unsorted: {path}")
    return calendar


def _read_instrument_spans(provider: Path) -> dict[str, tuple[pd.Timestamp, pd.Timestamp]]:
    path = provider / "instruments" / "all.txt"
    if not path.exists():
        raise FileNotFoundError(f"provider instrument file is missing: {path}")
    spans: dict[str, tuple[pd.Timestamp, pd.Timestamp]] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        raw_code = parts[0].strip().upper()
        if re.fullmatch(r"(?:BJ\d{6}|\d{6}\.BJ)", raw_code):
            continue
        code = _canonical_code(parts[0])
        start, end = pd.Timestamp(parts[1]), pd.Timestamp(parts[2])
        if code in spans:
            old_start, old_end = spans[code]
            spans[code] = min(old_start, start), max(old_end, end)
        else:
            spans[code] = start, end
    return spans


def load_market_codes(
    provider: Path,
    market: str,
    start_date: str,
    end_date: str,
) -> list[str]:
    return sorted(load_market_code_spans(provider, market, start_date, end_date))


def load_market_code_spans(
    provider: Path,
    market: str,
    start_date: str,
    end_date: str,
) -> dict[str, tuple[pd.Timestamp, pd.Timestamp]]:
    path = provider / "instruments" / f"{market}.txt"
    if not path.exists():
        raise FileNotFoundError(f"provider market file is missing: {path}")
    start_ts, end_ts = pd.Timestamp(start_date), pd.Timestamp(end_date)
    spans: dict[str, tuple[pd.Timestamp, pd.Timestamp]] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        member_start, member_end = pd.Timestamp(parts[1]), pd.Timestamp(parts[2])
        if member_start <= end_ts and member_end >= start_ts:
            code = _canonical_code(parts[0])
            clipped_start = max(member_start, start_ts)
            clipped_end = min(member_end, end_ts)
            current = spans.get(code)
            if current is None:
                spans[code] = clipped_start, clipped_end
            else:
                spans[code] = min(current[0], clipped_start), max(
                    current[1], clipped_end
                )
    if not spans:
        raise RuntimeError(
            f"no {market} instruments overlap {start_date}..{end_date} in {provider}"
        )
    return spans


def _effective_session(
    calendar: list[pd.Timestamp],
    available_date: pd.Timestamp,
    lag_sessions: int,
) -> pd.Timestamp | None:
    if lag_sessions < 1:
        raise ValueError("publication_lag_sessions must be at least 1")
    index = int(np.searchsorted(np.asarray(calendar, dtype="datetime64[ns]"), available_date.to_datetime64(), side="right"))
    index += lag_sessions - 1
    return calendar[index] if index < len(calendar) else None


def build_point_in_time_panel(
    financial_events: pd.DataFrame,
    daily_basic: pd.DataFrame,
    calendar: Iterable[pd.Timestamp | str],
    instrument_spans: dict[str, tuple[pd.Timestamp, pd.Timestamp]],
    codes: Iterable[str],
    financial_metrics: Iterable[str] = DEFAULT_FINANCIAL_METRICS,
    daily_basic_metrics: Iterable[str] = DEFAULT_DAILY_BASIC_METRICS,
    publication_lag_sessions: int = 1,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    financial_fields = _validate_metric_fields(financial_metrics)
    daily_fields = _validate_metric_fields(daily_basic_metrics)
    derived_fields = (
        (DROE_FEATURE,) if DROE_SOURCE_METRIC in financial_fields else ()
    )
    calendar_values = sorted({pd.Timestamp(value).normalize() for value in calendar})
    if not calendar_values:
        raise ValueError("calendar is empty")
    requested_codes = list(dict.fromkeys(_canonical_code(code) for code in codes))
    unknown = sorted(set(requested_codes) - set(instrument_spans))
    if unknown:
        raise ValueError(f"codes are missing from base provider instruments/all.txt: {unknown[:10]}")

    events = financial_events.copy()
    report_version_counts = (
        events.groupby(["code", "report_end"], dropna=False).size()
        if not events.empty
        else pd.Series(dtype="int64")
    )
    multi_version_periods = int((report_version_counts > 1).sum())
    revised_event_rows = 0
    latest_snapshot_only_risk_periods = 0
    if "update_flag" in events:
        revised = (
            events["update_flag"]
            .astype("string")
            .str.strip()
            .str.upper()
            .isin({"1", "Y", "YES", "TRUE"})
        )
        revised_event_rows = int(revised.sum())
        revised_periods = set(
            zip(events.loc[revised, "code"], events.loc[revised, "report_end"])
        )
        latest_snapshot_only_risk_periods = sum(
            int(report_version_counts.get(period, 0) == 1)
            for period in revised_periods
        )
    effective_dates = []
    for value in events.get("available_date", pd.Series(dtype="datetime64[ns]")):
        effective_dates.append(
            _effective_session(calendar_values, pd.Timestamp(value), publication_lag_sessions)
        )
    events["effective_date"] = effective_dates
    events = events.dropna(subset=["effective_date"])

    records: list[dict[str, Any]] = []
    for code in requested_codes:
        span_start, span_end = instrument_spans[code]
        dates = [date for date in calendar_values if span_start <= date <= span_end]
        code_events = events[events["code"] == code].sort_values(
            ["effective_date", "report_end", "announcement_date"]
        )
        event_rows = list(code_events.to_dict("records"))
        state: dict[pd.Timestamp, dict[str, Any]] = {}
        cursor = 0
        for date in dates:
            while cursor < len(event_rows) and pd.Timestamp(
                event_rows[cursor]["effective_date"]
            ) <= date:
                event = event_rows[cursor]
                report_end = pd.Timestamp(event["report_end"])
                current = state.get(report_end)
                if current is None or (
                    pd.Timestamp(event["available_date"]),
                    pd.Timestamp(event["announcement_date"]),
                ) >= (
                    pd.Timestamp(current["available_date"]),
                    pd.Timestamp(current["announcement_date"]),
                ):
                    state[report_end] = event
                cursor += 1

            row: dict[str, Any] = {
                "date": date,
                "code": code,
                "pit_report_end": pd.NaT,
                "pit_announcement_date": pd.NaT,
                "pit_available_date": pd.NaT,
                "pit_effective_date": pd.NaT,
                "pit_report_age_days": np.nan,
                "pit_disclosure_lag_days": np.nan,
            }
            for metric in financial_fields:
                row[f"pit_{metric}"] = np.nan
            for field_name in derived_fields:
                row[field_name] = np.nan
            if state:
                latest_period = max(state)
                selected = state[latest_period]
                row.update(
                    {
                        "pit_report_end": latest_period,
                        "pit_announcement_date": selected["announcement_date"],
                        "pit_available_date": selected["available_date"],
                        "pit_effective_date": selected["effective_date"],
                        "pit_report_age_days": float((date - latest_period).days),
                        "pit_disclosure_lag_days": float(
                            (pd.Timestamp(selected["available_date"]) - latest_period).days
                        ),
                    }
                )
                for metric in financial_fields:
                    row[f"pit_{metric}"] = selected.get(metric)
                if derived_fields:
                    lag_period = latest_period - pd.DateOffset(years=1)
                    lagged = state.get(lag_period)
                    fresh_until = latest_period + pd.DateOffset(
                        months=DROE_STALENESS_MONTHS
                    )
                    if (
                        latest_period.is_quarter_end
                        and lag_period.is_quarter_end
                        and lagged is not None
                        and date <= fresh_until
                    ):
                        current_roe = pd.to_numeric(
                            selected.get(DROE_SOURCE_METRIC), errors="coerce"
                        )
                        lagged_roe = pd.to_numeric(
                            lagged.get(DROE_SOURCE_METRIC), errors="coerce"
                        )
                        if np.isfinite(current_roe) and np.isfinite(lagged_roe):
                            row[DROE_FEATURE] = float(current_roe - lagged_roe)
            records.append(row)

    panel = pd.DataFrame.from_records(records)
    daily = daily_basic.copy()
    if daily_fields:
        _require_columns(daily, ("date", "code", *daily_fields), "daily_basic")
        daily = daily[daily["code"].isin(requested_codes)]
        daily = daily.rename(
            columns={metric: f"pit_{metric}" for metric in daily_fields}
        )
        panel = panel.merge(
            daily[["date", "code", *(f"pit_{metric}" for metric in daily_fields)]],
            on=["date", "code"],
            how="left",
            validate="one_to_one",
        )

    feature_fields = [
        *(f"pit_{metric}" for metric in financial_fields),
        *derived_fields,
        "pit_report_age_days",
        "pit_disclosure_lag_days",
        *(f"pit_{metric}" for metric in daily_fields),
    ]
    if "pb" in daily_fields:
        panel["pit_book_to_price"] = np.nan
        positive_pb = panel["pit_pb"] > 0
        panel.loc[positive_pb, "pit_book_to_price"] = (
            1.0 / panel.loc[positive_pb, "pit_pb"]
        )
        feature_fields.append("pit_book_to_price")
    if "total_mv" in daily_fields:
        panel["pit_log_total_mv"] = np.where(
            panel["pit_total_mv"] > 0,
            np.log(panel["pit_total_mv"]),
            np.nan,
        )
        feature_fields.append("pit_log_total_mv")
    for field_name in feature_fields:
        panel[field_name] = pd.to_numeric(panel[field_name], errors="coerce")

    availability_leaks = int(
        (
            panel["pit_available_date"].notna()
            & (panel["date"] <= panel["pit_available_date"])
        ).sum()
    )
    report_period_leaks = int(
        (
            panel["pit_report_end"].notna()
            & (panel["pit_report_end"] > panel["date"])
        ).sum()
    )
    coverage = {
        field_name: float(panel[field_name].notna().mean())
        for field_name in feature_fields
    }
    financial_event_codes = set(events["code"]) if not events.empty else set()
    daily_basic_codes = (
        set(daily["code"]) if daily_fields and not daily.empty else set()
    )
    missing_financial_codes = sorted(set(requested_codes) - financial_event_codes)
    missing_daily_codes = (
        sorted(set(requested_codes) - daily_basic_codes) if daily_fields else []
    )
    audit = {
        "status": "PASS"
        if (
            availability_leaks == 0
            and report_period_leaks == 0
            and not missing_financial_codes
            and not missing_daily_codes
        )
        else "FAIL",
        "rows": int(len(panel)),
        "codes": len(requested_codes),
        "calendarStart": calendar_values[0].date().isoformat(),
        "calendarEnd": calendar_values[-1].date().isoformat(),
        "publicationLagSessions": publication_lag_sessions,
        "featureFields": feature_fields,
        "derivedFinancialFields": list(derived_fields),
        "droeSourceMetric": (
            DROE_SOURCE_METRIC if derived_fields else None
        ),
        "droeStalenessMonths": (
            DROE_STALENESS_MONTHS if derived_fields else None
        ),
        "financialReportPeriods": int(len(report_version_counts)),
        "multiVersionReportPeriods": multi_version_periods,
        "revisedEventRows": revised_event_rows,
        "latestSnapshotOnlyRiskPeriods": latest_snapshot_only_risk_periods,
        "revisionVintageEvidence": multi_version_periods > 0,
        "coverage": coverage,
        "availabilityLeakRows": availability_leaks,
        "reportPeriodLeakRows": report_period_leaks,
        "financialEventCodes": len(financial_event_codes),
        "dailyBasicCodes": len(daily_basic_codes),
        "missingFinancialCodes": missing_financial_codes,
        "missingDailyBasicCodes": missing_daily_codes,
    }
    return panel.sort_values(["date", "code"]).reset_index(drop=True), audit


def _link_or_copy(source: str, destination: str) -> str:
    try:
        os.link(source, destination)
        return destination
    except OSError:
        return shutil.copy2(source, destination)


def _feature_tree_digest(provider: Path, feature_fields: Iterable[str]) -> dict[str, Any]:
    suffixes = {f"{field}.day.bin" for field in feature_fields}
    paths = sorted(
        path
        for path in (provider / "features").glob("*/*.day.bin")
        if path.name in suffixes
    )
    digest = hashlib.sha256()
    for path in paths:
        relative = path.relative_to(provider).as_posix()
        file_digest = _sha256(path)
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(file_digest.encode("ascii"))
    return {"count": len(paths), "sha256": digest.hexdigest()}


def write_enriched_qlib_provider(
    base_provider: Path,
    output_provider: Path,
    panel: pd.DataFrame,
    feature_fields: Iterable[str],
    *,
    batch_id: str,
    source_manifest: Path,
    as_of_audit: dict[str, Any],
    market: str = "csi300",
    as_of_policy: dict[str, Any] | None = None,
    source_metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    base = base_provider.expanduser().resolve()
    output = output_provider.expanduser().resolve()
    if base == output:
        raise ValueError("fundamental features must be written to a new provider")
    if output.exists():
        raise FileExistsError(f"refusing to overwrite provider: {output}")
    fields = _validate_metric_fields(feature_fields)
    required = {"date", "code", *fields}
    missing = sorted(required - set(panel.columns))
    if missing:
        raise ValueError(f"point-in-time panel is missing output fields: {missing}")
    if as_of_audit.get("status") != "PASS":
        raise RuntimeError("point-in-time audit must pass before provider promotion")

    base_contract = provider_contract_fingerprint(base, market)
    calendar = _read_calendar(base)
    calendar_index = {date: index for index, date in enumerate(calendar)}
    spans = _read_instrument_spans(base)
    build = output.with_name(f".{output.name}_build_{batch_id}")
    if build.exists():
        raise FileExistsError(f"stale provider build path exists: {build}")
    build.parent.mkdir(parents=True, exist_ok=True)

    try:
        shutil.copytree(base, build, copy_function=_link_or_copy)
        work = panel.copy()
        work["date"] = pd.to_datetime(work["date"]).dt.normalize()
        work["code"] = work["code"].map(_canonical_code)
        work = work.drop_duplicates(["date", "code"], keep="last")
        for code, code_frame in work.groupby("code", sort=True):
            if code not in spans:
                raise ValueError(f"panel code is missing from base provider: {code}")
            span_start, span_end = spans[code]
            dates = [date for date in calendar if span_start <= date <= span_end]
            indexed = code_frame.set_index("date").reindex(dates)
            start_index = calendar_index[dates[0]]
            code_dir = build / "features" / to_qlib_code(code)
            code_dir.mkdir(parents=True, exist_ok=True)
            for field_name in fields:
                destination = code_dir / f"{field_name}.day.bin"
                if destination.exists():
                    raise ValueError(
                        f"base provider already contains extension field: {destination.relative_to(build)}"
                    )
                values = pd.to_numeric(indexed[field_name], errors="coerce").astype(
                    "float32"
                )
                binary = np.hstack(
                    [[float(start_index)], values.to_numpy(dtype="float32")]
                ).astype("<f")
                binary.tofile(destination)

        extension = {
            "schemaVersion": "quantaalpha-provider-extension-v1",
            "kind": "point_in_time_fundamentals",
            "batchId": batch_id,
            "createdAt": datetime.now(timezone.utc).isoformat(),
            "baseProvider": str(base),
            "baseProviderFingerprint": base_contract["fingerprint"],
            "sourceManifest": str(source_manifest.resolve()),
            "sourceManifestSha256": _sha256(source_manifest),
            "asOfPolicy": as_of_policy
            or {
                "financialAvailability": "first provider session strictly after max(fina_indicator.ann_date, disclosure_date.actual_date)",
                "dailyBasicAvailability": "trade_date close for next-session execution",
                "restatementPolicy": "latest announced version of the latest available report period",
            },
            "audit": as_of_audit,
            "featureFields": list(fields),
            "featureTree": _feature_tree_digest(build, fields),
        }
        if source_metadata:
            extension["source"] = source_metadata
        _write_json(build / EXTENSION_MANIFEST, extension)
        build_contract = provider_contract_fingerprint(build, market)
        if build_contract["fingerprint"] == base_contract["fingerprint"]:
            raise RuntimeError("enriched provider fingerprint did not change")
        build.replace(output)
    except Exception:
        shutil.rmtree(build, ignore_errors=True)
        raise

    return {
        "path": str(output),
        "fingerprint": build_contract["fingerprint"],
        "baseFingerprint": base_contract["fingerprint"],
        "extensionManifest": str(output / EXTENSION_MANIFEST),
    }


def _query_with_retry(
    client: TushareProClient,
    api_name: str,
    params: dict[str, Any],
    fields: tuple[str, ...],
) -> dict[str, Any]:
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            return client.query_raw(api_name, params=params, fields=fields)
        except Exception as exc:
            last_error = exc
            if attempt < 2:
                time.sleep(0.5 * (2**attempt))
    assert last_error is not None
    raise last_error


def sync_tushare_fundamentals(
    project_root: Path,
    request: FundamentalSyncRequest,
    client: TushareProClient | None = None,
) -> dict[str, Any]:
    started = datetime.now(timezone.utc)
    batch_id = request.batch_id or (
        f"tushare_fundamentals_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}"
    )
    raw_dir = raw_provider_dir(project_root, "tushare_fundamentals", batch_id)
    stage_dir = staging_dir(project_root, batch_id)
    manifest_file = manifest_path(project_root, batch_id)
    report_file = report_path(project_root, batch_id)
    collisions = [path for path in (raw_dir, stage_dir, manifest_file, report_file) if path.exists()]
    if collisions:
        raise FileExistsError(f"refusing to overwrite immutable sync artifacts: {collisions}")
    if request.output_provider and request.output_provider.exists():
        raise FileExistsError(
            f"refusing to overwrite provider: {request.output_provider}"
        )

    base = request.base_provider.expanduser().resolve()
    financial_metrics = _validate_metric_fields(request.financial_metrics)
    daily_metrics = _validate_metric_fields(request.daily_basic_metrics)
    codes = list(dict.fromkeys(_canonical_code(code) for code in request.codes))
    if not codes:
        raise ValueError("at least one code is required")
    spans = _read_instrument_spans(base)
    missing_codes = sorted(set(codes) - set(spans))
    if missing_codes:
        raise ValueError(f"requested codes are missing from base provider: {missing_codes[:10]}")
    base_contract = provider_contract_fingerprint(base, request.market)

    raw_dir.mkdir(parents=True)
    stage_dir.mkdir(parents=True)
    api = client or TushareProClient()
    payloads: dict[str, list[dict[str, Any]]] = {
        "fina_indicator": [],
        "disclosure_date": [],
        "daily_basic": [],
    }
    failures: dict[str, str] = {}
    reused_responses = 0
    api_fields = {
        "fina_indicator": (
            "ts_code",
            "ann_date",
            "end_date",
            "update_flag",
            *financial_metrics,
        ),
        "disclosure_date": (
            "ts_code",
            "ann_date",
            "end_date",
            "pre_date",
            "actual_date",
            "modify_date",
        ),
        "daily_basic": ("ts_code", "trade_date", *daily_metrics),
    }
    for code in codes:
        code_dir = raw_dir / to_qlib_code(code)
        code_dir.mkdir()
        ts_code = to_tushare_code(code)
        requests_for_code = [
            (
                "fina_indicator",
                {
                    "ts_code": ts_code,
                    "start_date": to_tushare_date(request.start_date),
                    "end_date": to_tushare_date(request.end_date),
                },
            ),
            ("disclosure_date", {"ts_code": ts_code}),
        ]
        if daily_metrics:
            requests_for_code.append(
                (
                    "daily_basic",
                    {
                        "ts_code": ts_code,
                        "start_date": to_tushare_date(request.start_date),
                        "end_date": to_tushare_date(request.end_date),
                    },
                )
            )
        try:
            for api_name, params in requests_for_code:
                reusable = (
                    request.reuse_raw_dir
                    / to_qlib_code(code)
                    / f"{api_name}.json"
                    if request.reuse_raw_dir
                    else None
                )
                was_reused = reusable is not None and reusable.is_file()
                if was_reused:
                    payload = _load_reusable_payload(reusable, api_fields[api_name])
                    reused_responses += 1
                else:
                    payload = _query_with_retry(api, api_name, params, api_fields[api_name])
                _write_json(code_dir / f"{api_name}.json", payload)
                payloads[api_name].append(payload)
                if not was_reused:
                    if request.request_interval_seconds > 0:
                        time.sleep(request.request_interval_seconds)
        except Exception as exc:
            failures[code] = str(exc)

    manifest = {
        "schemaVersion": "tushare-fundamental-sync-v1",
        "batchId": batch_id,
        "createdAt": started.isoformat(),
        "provider": "tushare_pro",
        "tokenPersisted": False,
        "request": {
            "baseProvider": str(base),
            "baseProviderFingerprint": base_contract["fingerprint"],
            "market": request.market,
            "codes": codes,
            "startDate": request.start_date,
            "endDate": request.end_date,
            "financialMetrics": list(financial_metrics),
            "dailyBasicMetrics": list(daily_metrics),
            "publicationLagSessions": request.publication_lag_sessions,
            "outputProvider": str(request.output_provider) if request.output_provider else None,
            "reuseRawDirectory": str(request.reuse_raw_dir) if request.reuse_raw_dir else None,
        },
        "rawDirectory": str(raw_dir),
        "reusedResponses": reused_responses,
        "failures": failures,
    }
    _write_json(manifest_file, manifest)

    output_info = None
    audit: dict[str, Any] = {"status": "FAIL", "reason": "source requests failed"}
    rows = 0
    if not failures:
        financial_raw = pd.concat(
            [_payload_frame(payload) for payload in payloads["fina_indicator"]],
            ignore_index=True,
        )
        disclosure_raw = pd.concat(
            [_payload_frame(payload) for payload in payloads["disclosure_date"]],
            ignore_index=True,
        )
        daily_raw = (
            pd.concat(
                [_payload_frame(payload) for payload in payloads["daily_basic"]],
                ignore_index=True,
            )
            if payloads["daily_basic"]
            else pd.DataFrame(columns=["ts_code", "trade_date"])
        )
        events = normalize_financial_events(
            financial_raw, disclosure_raw, financial_metrics
        )
        daily = normalize_daily_basic(daily_raw, daily_metrics)
        panel, audit = build_point_in_time_panel(
            events,
            daily,
            _read_calendar(base),
            spans,
            codes,
            financial_metrics,
            daily_metrics,
            request.publication_lag_sessions,
        )
        if request.output_provider and DROE_SOURCE_METRIC in financial_metrics:
            droe_coverage = float(audit["coverage"].get(DROE_FEATURE, 0.0))
            promotion_checks = {
                "minimumCodes": {
                    "actual": len(codes),
                    "required": DROE_PROMOTION_MIN_CODES,
                    "passed": len(codes) >= DROE_PROMOTION_MIN_CODES,
                },
                "minimumCoverage": {
                    "actual": droe_coverage,
                    "required": DROE_PROMOTION_MIN_COVERAGE,
                    "passed": droe_coverage >= DROE_PROMOTION_MIN_COVERAGE,
                },
                "historicalRevisionVintages": {
                    "multiVersionReportPeriods": audit[
                        "multiVersionReportPeriods"
                    ],
                    "latestSnapshotOnlyRiskPeriods": audit[
                        "latestSnapshotOnlyRiskPeriods"
                    ],
                    "passed": audit["revisionVintageEvidence"],
                },
            }
            failed_checks = [
                name
                for name, check in promotion_checks.items()
                if not check["passed"]
            ]
            audit["droePromotionGate"] = {
                "status": "PASS" if not failed_checks else "FAIL",
                "checks": promotion_checks,
                "failedChecks": failed_checks,
            }
            if failed_checks:
                audit["status"] = "FAIL"
                audit["reason"] = (
                    "dROE provider promotion gate failed: "
                    + ", ".join(failed_checks)
                )
        rows = len(panel)
        events.to_csv(stage_dir / "financial_events.csv.gz", index=False, compression="gzip")
        daily.to_csv(stage_dir / "daily_basic.csv.gz", index=False, compression="gzip")
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
                        "first provider session strictly after max("
                        "fina_indicator.ann_date, disclosure_date.actual_date)"
                    ),
                    "dailyBasicAvailability": (
                        "trade_date close for next-session execution"
                        if daily_metrics
                        else "not requested"
                    ),
                    "restatementPolicy": (
                        "latest announced version of every report period as of each session"
                    ),
                    "droePolicy": (
                        "latest available q_roe minus the latest available q_roe for "
                        "the exact quarter one year earlier; missing after six calendar "
                        "months from report end"
                        if DROE_SOURCE_METRIC in financial_metrics
                        else "not requested"
                    ),
                },
                source_metadata={
                    "provider": "tushare_pro",
                    "financialMetrics": list(financial_metrics),
                    "dailyBasicMetrics": list(daily_metrics),
                },
            )

    finished = datetime.now(timezone.utc)
    result = {
        "batchId": batch_id,
        "status": "PASS"
        if not failures and audit.get("status") == "PASS"
        else "FAIL",
        "provider": "tushare_pro",
        "codesRequested": len(codes),
        "codesSucceeded": len(codes) - len(failures),
        "codesFailed": len(failures),
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
    _write_json(report_file, result)
    return result
