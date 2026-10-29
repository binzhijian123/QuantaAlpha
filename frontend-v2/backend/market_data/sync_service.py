from __future__ import annotations

import json
import shutil
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from .models import SyncRequest, SyncResult, ValidationIssue
from .paths import manifest_path, qlib_provider_dir, raw_provider_dir, report_path, staging_dir
from .providers.a_stock_data import AStockDataClient, default_window, normalize_stock_code, to_qlib_code
from .validation_service import normalize_history_frame, validate_history_frame


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _dump_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def sync_a_stock_data_daily(project_root: Path, req: SyncRequest) -> SyncResult:
    started_at = _now()
    start_ts = time.perf_counter()
    batch_id = req.batch_id or f"a_stock_data_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}"
    raw_dir = raw_provider_dir(project_root, "a_stock_data", batch_id)
    stg_dir = staging_dir(project_root, batch_id)
    qlib_dir = qlib_provider_dir(project_root, "a_stock_data_daily_cn")
    manifest_file = manifest_path(project_root, batch_id)
    report_file = report_path(project_root, batch_id)
    for path in [raw_dir, stg_dir, manifest_file.parent, report_file.parent]:
        path.mkdir(parents=True, exist_ok=True)

    start_date, end_date = req.start_date, req.end_date
    if not start_date or not end_date:
        start_date, end_date = default_window(req.lookback_trading_days)

    frames: list[pd.DataFrame] = []
    issues: list[ValidationIssue] = []
    failed_codes: dict[str, str] = {}
    client = AStockDataClient()
    existing_codes = _existing_provider_codes(qlib_dir) if req.promote else set()

    expected_dates = pd.date_range(start_date, end_date).strftime("%Y-%m-%d").tolist()
    for code in req.codes:
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                raw = client.history_quotation(code, start_date, end_date)
                last_error = None
                break
            except Exception as exc:
                last_error = exc
                if attempt < 2:
                    time.sleep(0.5 * (attempt + 1))
        if last_error is not None:
            canonical = _canonical_code(code)
            if canonical in existing_codes and _is_incremental_no_data(last_error):
                issues.append(
                    ValidationIssue(
                        code=code,
                        level="warning",
                        message="请求窗口内没有新行情，已保留本地历史数据",
                    )
                )
                continue
            failed_codes[code] = str(last_error)
            issues.append(ValidationIssue(code=code, level="error", message=str(last_error)))
            continue

        try:
            normalized = normalize_history_frame(raw)
            code_issues = validate_history_frame(normalized, code, expected_dates)
            issues.extend(code_issues)
            if any(issue.level == "error" for issue in code_issues):
                failed_codes[code] = "validation_error"
                continue
            out_file = raw_dir / f"{to_qlib_code(code)}.csv"
            normalized.to_csv(out_file, index=False)
            frames.append(normalized)
        except Exception as exc:
            failed_codes[code] = str(exc)
            issues.append(ValidationIssue(code=code, level="error", message=str(exc)))

    all_df = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    all_csv = stg_dir / "daily_bars.csv"
    all_df.to_csv(all_csv, index=False)
    source_counts = all_df["source"].value_counts().to_dict() if "source" in all_df else {}

    qlib_output: str | None = None
    if req.promote and not all_df.empty and not any(issue.level == "error" for issue in issues):
        _promote_qlib_provider(all_df, qlib_dir, batch_id)
        qlib_output = str(qlib_dir)

    success = bool(req.codes) and len(failed_codes) == 0 and not any(issue.level == "error" for issue in issues)
    finished_at = _now()
    elapsed = time.perf_counter() - start_ts
    manifest = {
        "batchId": batch_id,
        "provider": "a_stock_data",
        "request": {
            "codes": req.codes,
            "startDate": req.start_date,
            "endDate": req.end_date,
            "lookbackTradingDays": req.lookback_trading_days,
            "promote": req.promote,
        },
        "resolved": {
            "startDate": start_date,
            "endDate": end_date,
            "failedCodes": failed_codes,
            "sourceCounts": source_counts,
        },
        "outputs": {
            "rawDir": str(raw_dir),
            "stagingDir": str(stg_dir),
            "stagingCsv": str(all_csv),
            "qlibDir": qlib_output,
        },
        "startedAt": started_at,
        "finishedAt": finished_at,
        "elapsedSeconds": elapsed,
    }
    _dump_json(manifest_file, manifest)

    result = SyncResult(
        batch_id=batch_id,
        provider="a_stock_data",
        raw_dir=str(raw_dir),
        staging_dir=str(stg_dir),
        qlib_dir=qlib_output,
        manifest_path=str(manifest_file),
        report_path=str(report_file),
        started_at=started_at,
        finished_at=finished_at,
        success=success,
        codes_requested=len(req.codes),
        codes_succeeded=len(req.codes) - len(failed_codes),
        codes_failed=len(failed_codes),
        rows=int(len(all_df)),
        start_date=start_date,
        end_date=end_date,
        issues=issues,
        elapsed_seconds=elapsed,
    )
    _dump_json(report_file, result.to_dict())
    return result


def _canonical_code(code: str) -> str:
    digits, exchange, _ = normalize_stock_code(code)
    return f"{digits}.{exchange}"


def _existing_provider_codes(provider_dir: Path) -> set[str]:
    instrument_file = provider_dir / "instruments" / "all.txt"
    if not instrument_file.exists():
        return set()
    result = set()
    for line in instrument_file.read_text(encoding="utf-8").splitlines():
        qlib_code = line.split("\t", 1)[0]
        try:
            result.add(_canonical_code(qlib_code.upper()))
        except ValueError:
            continue
    return result


def _is_incremental_no_data(exc: Exception) -> bool:
    message = str(exc)
    return (
        "Tencent returned no daily bars" in message
        and "Tonghuashun returned no" in message
    )


def _promote_qlib_provider(df: pd.DataFrame, provider_dir: Path, batch_id: str) -> None:
    provider_dir.parent.mkdir(parents=True, exist_ok=True)
    build_dir = provider_dir.with_name(f".{provider_dir.name}_build_{batch_id}")
    if build_dir.exists():
        shutil.rmtree(build_dir)

    try:
        existing = read_minimal_qlib_provider(provider_dir) if provider_dir.exists() else pd.DataFrame()
        merged = pd.concat([existing, df], ignore_index=True) if not existing.empty else df.copy()
        if not merged.empty:
            merged = (
                merged.sort_values(["date", "code"])
                .drop_duplicates(subset=["date", "code"], keep="last")
                .reset_index(drop=True)
            )
        write_minimal_qlib_provider(merged, build_dir)
    except Exception:
        shutil.rmtree(build_dir, ignore_errors=True)
        raise

    backup: Path | None = None
    try:
        if provider_dir.exists():
            backup = provider_dir.with_name(
                f"{provider_dir.name}_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
            )
            shutil.move(str(provider_dir), str(backup))
        shutil.move(str(build_dir), str(provider_dir))
    except Exception:
        if backup is not None and backup.exists() and not provider_dir.exists():
            shutil.move(str(backup), str(provider_dir))
        raise
    finally:
        shutil.rmtree(build_dir, ignore_errors=True)


def read_minimal_qlib_provider(provider_dir: Path) -> pd.DataFrame:
    calendar_file = provider_dir / "calendars" / "day.txt"
    instrument_file = provider_dir / "instruments" / "all.txt"
    if not calendar_file.exists() or not instrument_file.exists():
        return pd.DataFrame()

    calendar = [line.strip() for line in calendar_file.read_text(encoding="utf-8").splitlines() if line.strip()]
    date_index = {value: index for index, value in enumerate(calendar)}
    frames: list[pd.DataFrame] = []
    for line in instrument_file.read_text(encoding="utf-8").splitlines():
        parts = line.split("\t")
        if len(parts) < 3 or parts[1] not in date_index or parts[2] not in date_index:
            continue
        qlib_code, start, end = parts[:3]
        digits, exchange, _ = normalize_stock_code(qlib_code.upper())
        start_index = date_index[start]
        end_index = date_index[end]
        dates = calendar[start_index : end_index + 1]
        code_dir = provider_dir / "features" / qlib_code.lower()
        data: dict[str, Any] = {
            "date": dates,
            "code": [f"{digits}.{exchange}"] * len(dates),
            "source": ["existing_qlib"] * len(dates),
        }
        for field in ("open", "high", "low", "close", "volume", "amount"):
            data[field] = _read_qlib_field(code_dir / f"{field}.day.bin", start_index, end_index)
        frames.append(pd.DataFrame(data))
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def _read_qlib_field(path: Path, start_index: int, end_index: int) -> np.ndarray:
    result = np.full(end_index - start_index + 1, np.nan, dtype="float64")
    if not path.exists():
        return result
    values = np.fromfile(path, dtype="<f")
    if len(values) < 2:
        return result
    field_start = int(values[0])
    field_end = field_start + len(values) - 2
    overlap_start = max(start_index, field_start)
    overlap_end = min(end_index, field_end)
    if overlap_start > overlap_end:
        return result
    source_start = overlap_start - field_start + 1
    source_end = overlap_end - field_start + 2
    target_start = overlap_start - start_index
    target_end = overlap_end - start_index + 1
    result[target_start:target_end] = values[source_start:source_end]
    return result


def write_minimal_qlib_provider(df: pd.DataFrame, provider_dir: Path) -> None:
    provider_dir.mkdir(parents=True, exist_ok=True)
    calendars_dir = provider_dir / "calendars"
    instruments_dir = provider_dir / "instruments"
    features_dir = provider_dir / "features"
    calendars_dir.mkdir(parents=True, exist_ok=True)
    instruments_dir.mkdir(parents=True, exist_ok=True)
    features_dir.mkdir(parents=True, exist_ok=True)

    work = df.copy()
    work["qlib_code"] = work["code"].map(to_qlib_code)
    work["date"] = pd.to_datetime(work["date"]).dt.strftime("%Y-%m-%d")
    dates = sorted(work["date"].unique())
    date_index = {d: i for i, d in enumerate(dates)}
    (calendars_dir / "day.txt").write_text("\n".join(dates) + "\n", encoding="utf-8")

    instrument_lines: list[str] = []
    field_map = {
        "open": "open",
        "high": "high",
        "low": "low",
        "close": "close",
        "volume": "volume",
        "amount": "amount",
        "factor": None,
        "vwap": None,
    }
    for qlib_code, code_df in work.groupby("qlib_code", sort=True):
        code_df = code_df.sort_values("date").drop_duplicates(subset=["date"], keep="last")
        start = code_df["date"].iloc[0]
        end = code_df["date"].iloc[-1]
        instrument_lines.append(f"{qlib_code}\t{start}\t{end}")
        code_dir = features_dir / qlib_code.lower()
        code_dir.mkdir(parents=True, exist_ok=True)
        start_idx = date_index[start]

        instrument_dates = dates[start_idx : date_index[end] + 1]
        numeric = code_df.set_index("date").reindex(instrument_dates)
        numeric["factor"] = np.where(numeric["close"].notna(), 1.0, np.nan)
        numeric["vwap"] = np.where(numeric["volume"] > 0, numeric["amount"] / numeric["volume"], numeric["close"])
        for output_field, source_field in field_map.items():
            values = numeric[output_field if source_field is None else source_field].astype("float32").to_numpy()
            arr = np.hstack([[float(start_idx)], values]).astype("<f")
            arr.tofile(code_dir / f"{output_field}.day.bin")

    instruments_text = "\n".join(instrument_lines) + "\n"
    (instruments_dir / "all.txt").write_text(instruments_text, encoding="utf-8")
    (instruments_dir / "csi300.txt").write_text(instruments_text, encoding="utf-8")
