from __future__ import annotations

import copy
import csv
import gc
import hashlib
import json
import math
import os
from datetime import datetime, timezone
from itertools import combinations
from pathlib import Path
from statistics import NormalDist
from typing import Any, Iterable

import numpy as np
import pandas as pd
import yaml

from quantaalpha.backtest.runner import BacktestRunner, normalize_market_index
from quantaalpha.backtest.factor_loader import FactorLoader
from quantaalpha.trading.market_data import QlibBinaryDailySource
from quantaalpha.utils.qlib_provider import provider_contract_fingerprint
from quantaalpha.validation.trial_history import (
    AUDIT_SCHEMA as TRIAL_HISTORY_AUDIT_SCHEMA,
    audit_id as trial_history_audit_id,
    canonical_sha256,
)


PAPER_ELIGIBLE_STATUSES = {"PAPER_CANDIDATE", "PAPER_VALIDATED"}


def _resolve(project_root: Path, value: str | Path) -> Path:
    path = Path(value).expanduser()
    return path.resolve() if path.is_absolute() else (project_root / path).resolve()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _manifest_key(path: Path, project_root: Path) -> str:
    try:
        return str(path.resolve().relative_to(project_root))
    except ValueError:
        return str(path.resolve())


def _json_clean(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _json_clean(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_clean(item) for item in value]
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating, float)):
        return None if not math.isfinite(float(value)) else float(value)
    if isinstance(value, (pd.Timestamp, datetime)):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    return value


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(_json_clean(payload), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary.replace(path)


def _instrument_entries(provider: Path, market: str) -> list[tuple[str, str, str]]:
    path = provider / "instruments" / f"{market}.txt"
    if not path.exists():
        return []
    entries = []
    for line in path.read_text(encoding="utf-8").splitlines():
        parts = line.split("\t")
        if len(parts) >= 3:
            entries.append((parts[0].upper(), parts[1], parts[2]))
    return entries


def provider_summary(provider: Path, market: str = "csi300") -> dict[str, Any]:
    source = QlibBinaryDailySource(provider)
    entries = _instrument_entries(provider, market)
    contract = provider_contract_fingerprint(provider, market)
    interval_pairs = {(start, end) for _, start, end in entries}
    latest = source.calendar[-1]
    active = [code for code, start, end in entries if start <= latest <= end]
    active_count = len(set(active))
    has_point_in_time_history = len(entries) > max(active_count + 20, int(active_count * 1.5))
    return {
        "path": str(provider.resolve()),
        "calendarStart": source.calendar[0],
        "calendarEnd": latest,
        "calendarDays": len(source.calendar),
        "market": market,
        "instrumentIntervals": len(entries),
        "activeInstruments": active_count,
        "distinctIntervalPairs": len(interval_pairs),
        "singleStaticInterval": bool(entries) and len(interval_pairs) == 1,
        "hasPointInTimeHistory": has_point_in_time_history,
        "contractFingerprint": contract["fingerprint"],
        "contractStrength": contract["strength"],
        "release": contract.get("release"),
    }


def _relative_error(actual: float, expected: float) -> float:
    scale = max(abs(actual), abs(expected), 1e-12)
    return abs(actual - expected) / scale


def _median(values: Iterable[float]) -> float | None:
    clean = [float(value) for value in values if value is not None and math.isfinite(float(value))]
    return float(np.median(clean)) if clean else None


def _percentile(values: Iterable[float], percentile: float) -> float | None:
    clean = [float(value) for value in values if value is not None and math.isfinite(float(value))]
    return float(np.percentile(clean, percentile)) if clean else None


def audit_data_providers(
    historical_provider: Path,
    recent_provider: Path,
    required_start: str,
    required_end: str,
    market: str = "csi300",
    sample_size: int = 20,
) -> dict[str, Any]:
    historical = provider_summary(historical_provider, market)
    recent = provider_summary(recent_provider, market)
    historical_source = QlibBinaryDailySource(historical_provider)
    recent_source = QlibBinaryDailySource(recent_provider)

    overlap_start = max(historical["calendarStart"], recent["calendarStart"])
    overlap_end = min(historical["calendarEnd"], recent["calendarEnd"])
    sample_rows: list[dict[str, Any]] = []
    direct_price_errors: list[float] = []
    adjusted_price_errors: list[float] = []
    adjusted_volume_errors: list[float] = []

    if overlap_start <= overlap_end:
        common_symbols = sorted(
            set(historical_source.available_symbols(overlap_end))
            & set(recent_source.available_symbols(overlap_end))
        )
        if len(common_symbols) > sample_size:
            positions = np.linspace(0, len(common_symbols) - 1, sample_size, dtype=int)
            common_symbols = [common_symbols[index] for index in positions]

        historical_index = historical_source._date_index[overlap_end]
        recent_index = recent_source._date_index[overlap_end]
        for symbol in common_symbols:
            historical_close = historical_source._value(symbol, "close", historical_index)
            recent_close = recent_source._value(symbol, "close", recent_index)
            historical_factor = historical_source._value(symbol, "factor", historical_index)
            recent_factor = recent_source._value(symbol, "factor", recent_index)
            historical_volume = historical_source._value(symbol, "volume", historical_index)
            recent_volume = recent_source._value(symbol, "volume", recent_index)
            if historical_close is None or recent_close is None:
                continue

            direct_error = _relative_error(historical_close, recent_close)
            direct_price_errors.append(direct_error)
            adjusted_error = None
            volume_error = None
            if historical_factor not in (None, 0):
                adjusted_error = _relative_error(
                    historical_close,
                    recent_close * historical_factor,
                )
                adjusted_price_errors.append(adjusted_error)
                if historical_volume is not None and recent_volume is not None:
                    volume_error = _relative_error(
                        historical_volume,
                        recent_volume / historical_factor / 100.0,
                    )
                    adjusted_volume_errors.append(volume_error)
            sample_rows.append(
                {
                    "symbol": symbol,
                    "date": overlap_end,
                    "historicalClose": historical_close,
                    "recentClose": recent_close,
                    "historicalFactor": historical_factor,
                    "recentFactor": recent_factor,
                    "directPriceRelativeError": direct_error,
                    "adjustedPriceRelativeError": adjusted_error,
                    "adjustedVolumeRelativeError": volume_error,
                }
            )

    same_provider = historical_provider.resolve() == recent_provider.resolve()
    direct_error_median = _median(direct_price_errors)
    adjusted_error_median = _median(adjusted_price_errors)
    adjusted_volume_error_median = _median(adjusted_volume_errors)
    adjusted_price_error_p95 = _percentile(adjusted_price_errors, 95)
    adjusted_volume_error_p95 = _percentile(adjusted_volume_errors, 95)
    checks = [
        {
            "id": "historical_coverage",
            "status": "PASS"
            if historical["calendarStart"] <= required_start
            and historical["calendarEnd"] >= required_end
            else "FAIL",
            "details": {
                "required": [required_start, required_end],
                "actual": [historical["calendarStart"], historical["calendarEnd"]],
            },
        },
        {
            "id": "historical_point_in_time_universe",
            "status": "PASS" if historical["hasPointInTimeHistory"] else "FAIL",
            "details": {
                "instrumentIntervals": historical["instrumentIntervals"],
                "activeInstruments": historical["activeInstruments"],
            },
        },
        {
            "id": "recent_point_in_time_universe",
            "status": "PASS" if recent["hasPointInTimeHistory"] else "FAIL",
            "details": {
                "instrumentIntervals": recent["instrumentIntervals"],
                "activeInstruments": recent["activeInstruments"],
            },
        },
        {
            "id": "train_inference_price_parity",
            "status": "PASS"
            if same_provider
            or (direct_error_median is not None and direct_error_median <= 0.005)
            else "FAIL",
            "details": {"medianDirectRelativeError": direct_error_median},
        },
        {
            "id": "adjustment_transform_diagnostic",
            "status": "NOT_APPLICABLE"
            if same_provider
            else "PASS"
            if adjusted_error_median is not None
            and adjusted_error_median <= 0.005
            and adjusted_price_error_p95 is not None
            and adjusted_price_error_p95 <= 0.01
            and adjusted_volume_error_median is not None
            and adjusted_volume_error_median <= 0.01
            and adjusted_volume_error_p95 is not None
            and adjusted_volume_error_p95 <= 0.02
            else "WARNING",
            "details": {
                "medianAdjustedPriceRelativeError": adjusted_error_median,
                "p95AdjustedPriceRelativeError": adjusted_price_error_p95,
                "medianAdjustedVolumeRelativeError": adjusted_volume_error_median,
                "p95AdjustedVolumeRelativeError": adjusted_volume_error_p95,
                "sameProvider": same_provider,
            },
        },
    ]
    failed = [check["id"] for check in checks if check["status"] == "FAIL"]
    reasons = {
        "recent_point_in_time_universe": "近期提供器使用静态股票区间，不能用于历史收益声明。",
        "train_inference_price_parity": "训练特征与近期信号特征的复权/单位不一致。",
        "historical_point_in_time_universe": "历史回测股票池不是点时成分股。",
        "historical_coverage": "历史提供器未覆盖冻结的回测区间。",
    }
    return {
        "status": "FAIL" if failed else "PASS",
        "historicalProvider": historical,
        "recentProvider": recent,
        "overlap": [overlap_start, overlap_end] if overlap_start <= overlap_end else None,
        "checks": checks,
        "blockingReasons": [reasons[item] for item in failed if item in reasons],
        "sample": sample_rows,
    }


def audit_segment_embargo(
    provider: Path,
    segments: dict[str, list[str]],
    label_horizon_days: int,
) -> dict[str, Any]:
    """Verify that future-label dependencies do not cross dataset boundaries."""
    source = QlibBinaryDailySource(provider)
    calendar = source.calendar
    positions = {date: index for index, date in enumerate(calendar)}
    segment_items = (
        ("train", segments.get("train")),
        ("valid", segments.get("valid")),
        ("test", segments.get("test")),
    )
    ordered = [
        (name, bounds)
        for name, bounds in segment_items
        if bounds and len(bounds) == 2
    ]
    checks = []
    for (left_name, left), (right_name, right) in zip(ordered, ordered[1:]):
        left_dates = [date for date in calendar if left[0] <= date <= left[1]]
        right_dates = [date for date in calendar if right[0] <= date <= right[1]]
        if not left_dates or not right_dates:
            checks.append(
                {
                    "segments": [left_name, right_name],
                    "status": "FAIL",
                    "reason": "segment has no provider trading days",
                }
            )
            continue
        gap = positions[right_dates[0]] - positions[left_dates[-1]] - 1
        checks.append(
            {
                "segments": [left_name, right_name],
                "status": "PASS" if gap >= label_horizon_days else "FAIL",
                "leftLastTradingDay": left_dates[-1],
                "rightFirstTradingDay": right_dates[0],
                "embargoTradingDays": gap,
                "requiredTradingDays": label_horizon_days,
            }
        )
    return {
        "status": "PASS"
        if checks and all(check["status"] == "PASS" for check in checks)
        else "FAIL",
        "labelHorizonTradingDays": label_horizon_days,
        "checks": checks,
    }


def _load_factor_library(path: Path) -> tuple[dict[str, Any], list[tuple[str, dict[str, Any]]]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    factors = payload.get("factors") or {}
    if not isinstance(factors, dict):
        raise ValueError("factor library 'factors' must be an object")
    return payload, list(factors.items())


def _load_cached_factor(
    factor_info: dict[str, Any],
    start: str,
    end: str,
) -> pd.Series:
    cache_path = Path((factor_info.get("cache_location") or {}).get("result_h5_path", ""))
    if not cache_path.exists():
        raise FileNotFoundError(f"factor cache is unavailable: {cache_path}")
    result = pd.read_hdf(cache_path)
    if isinstance(result, pd.DataFrame):
        result = result.iloc[:, 0]
    if not isinstance(result, pd.Series) or not isinstance(result.index, pd.MultiIndex):
        raise ValueError(f"unsupported factor cache format: {cache_path}")
    if set(result.index.names) == {"datetime", "instrument"} and list(result.index.names) != [
        "datetime",
        "instrument",
    ]:
        result = result.swaplevel().sort_index()
    dates = pd.to_datetime(result.index.get_level_values("datetime"))
    mask = (dates >= pd.Timestamp(start)) & (dates <= pd.Timestamp(end))
    return result.loc[mask].copy()


def _validation_label_panel(
    provider: Path,
    market: str,
    label_expression: str,
    start: str,
    end: str,
) -> pd.DataFrame:
    import qlib
    from qlib.data import D

    qlib.init(provider_uri=str(provider), region="cn")
    label = D.features(
        D.instruments(market),
        [label_expression],
        start_time=start,
        end_time=end,
        freq="day",
    )
    label.columns = ["LABEL0"]
    return normalize_market_index(label, "validation label").dropna()


def _validation_feature_panel(
    provider: Path,
    market: str,
    expressions: dict[str, str],
    start: str,
    end: str,
) -> pd.DataFrame:
    if not expressions:
        return pd.DataFrame()
    import qlib
    from qlib.data import D

    qlib.init(provider_uri=str(provider), region="cn")
    frame = D.features(
        D.instruments(market),
        list(expressions.values()),
        start_time=start,
        end_time=end,
        freq="day",
    )
    frame.columns = list(expressions)
    return normalize_market_index(frame, "reference features")


def _mean_daily_rank_ic(factor: pd.Series, label: pd.Series) -> float | None:
    joined = pd.concat([factor.rename("factor"), label.rename("label")], axis=1).dropna()
    values = []
    for _, group in joined.groupby(level="datetime", sort=False):
        if len(group) < 10 or group["factor"].nunique() < 2 or group["label"].nunique() < 2:
            continue
        correlation = group["factor"].rank().corr(group["label"].rank())
        if pd.notna(correlation):
            values.append(float(correlation))
    return float(np.mean(values)) if values else None


def select_factor_pool(
    factor_ids: list[str],
    rank_ic: dict[str, float | None],
    correlations: pd.DataFrame,
    threshold: float,
) -> tuple[list[str], dict[str, str]]:
    original_order = {factor_id: index for index, factor_id in enumerate(factor_ids)}
    ordered = sorted(
        factor_ids,
        key=lambda factor_id: (
            -(rank_ic.get(factor_id) if rank_ic.get(factor_id) is not None else -math.inf),
            original_order[factor_id],
        ),
    )
    selected: list[str] = []
    rejected: dict[str, str] = {}
    for factor_id in ordered:
        conflict = next(
            (
                kept
                for kept in selected
                if factor_id in correlations.index
                and kept in correlations.columns
                and pd.notna(correlations.loc[factor_id, kept])
                and abs(float(correlations.loc[factor_id, kept])) >= threshold
            ),
            None,
        )
        if conflict is None:
            selected.append(factor_id)
        else:
            rejected[factor_id] = conflict
    return selected, rejected


def _has_consistency_proof(factor_info: dict[str, Any]) -> bool:
    quality_gate = factor_info.get("quality_gate") or {}
    evidence = quality_gate.get("consistency_check") or {}
    return (
        quality_gate.get("consistency_enabled") is True
        and quality_gate.get("consistency_passed") is True
        and evidence.get("is_consistent") is True
    )


def _effective_session_range(
    calendar: Iterable[str | pd.Timestamp],
    required_start: str,
    required_end: str,
) -> tuple[str, str]:
    start = pd.Timestamp(required_start).normalize()
    end = pd.Timestamp(required_end).normalize()
    if end < start:
        raise ValueError("required cache end must be on or after start")
    sessions = sorted(
        {
            pd.Timestamp(value).normalize()
            for value in calendar
            if start <= pd.Timestamp(value).normalize() <= end
        }
    )
    if not sessions:
        raise ValueError(
            f"provider has no sessions in required cache range {required_start}..{required_end}"
        )
    return sessions[0].date().isoformat(), sessions[-1].date().isoformat()


def _factor_cache_code_contract() -> dict[str, str]:
    project_root = Path(__file__).resolve().parents[2]
    paths = {
        "calculator_code_sha256": project_root
        / "quantaalpha"
        / "backtest"
        / "custom_factor_calculator.py",
        "parser_code_sha256": project_root
        / "quantaalpha"
        / "factors"
        / "coder"
        / "expr_parser.py",
        "function_library_sha256": project_root
        / "quantaalpha"
        / "factors"
        / "coder"
        / "function_lib.py",
        "preparation_code_sha256": project_root
        / "scripts"
        / "prepare_profit_factor_library.py",
    }
    return {field: _sha256(path) for field, path in paths.items()}


def _cache_matches_provider(
    factor_info: dict[str, Any],
    provider_fingerprint: str,
    *,
    market: str | None = None,
    required_start: str | None = None,
    required_end: str | None = None,
) -> bool:
    cache = factor_info.get("cache_location") or {}
    cache_path = Path(str(cache.get("result_h5_path") or "")).expanduser()
    expected_digest = str(cache.get("result_h5_sha256") or "")
    date_range = cache.get("date_range")
    expression = str(factor_info.get("factor_expression") or "")
    expression_digest = hashlib.sha256(expression.encode("utf-8")).hexdigest()
    code_contract = _factor_cache_code_contract()
    code_binding_required = (
        (factor_info.get("metadata") or {}).get("cache_code_binding_required")
        is True
    )
    declared_code_binding = {
        field: str(cache.get(field) or "") for field in code_contract
    }
    code_binding_present = any(declared_code_binding.values())
    if (
        cache.get("provider_fingerprint") != provider_fingerprint
        or (market is not None and cache.get("market") != market)
        or not isinstance(date_range, (list, tuple))
        or len(date_range) != 2
        or not expression
        or cache.get("expression_sha256") != expression_digest
        or not cache_path.is_file()
        or not expected_digest
        or _sha256(cache_path) != expected_digest
        or (
            (code_binding_required or code_binding_present)
            and declared_code_binding != code_contract
        )
    ):
        return False

    try:
        metadata_start = pd.Timestamp(date_range[0])
        metadata_end = pd.Timestamp(date_range[1])
        if required_start is not None and metadata_start > pd.Timestamp(required_start):
            return False
        if required_end is not None and metadata_end < pd.Timestamp(required_end):
            return False
        if required_start is None and required_end is None:
            return True

        cached = pd.read_hdf(cache_path)
        if not isinstance(cached.index, pd.MultiIndex) or "datetime" not in cached.index.names:
            return False
        dates = pd.to_datetime(cached.index.get_level_values("datetime"))
        if dates.empty:
            return False
        return (
            required_start is None or dates.min() <= pd.Timestamp(required_start)
        ) and (
            required_end is None or dates.max() >= pd.Timestamp(required_end)
        )
    except (KeyError, OSError, TypeError, ValueError):
        return False


def audit_factor_library(
    factor_library: Path,
    historical_provider: Path,
    market: str,
    label_expression: str,
    validation_start: str,
    validation_end: str,
    correlation_threshold: float,
    coverage_threshold: float = 0.5,
    rank_ic_min: float = 0.0,
    required_cache_start: str | None = None,
    required_cache_end: str | None = None,
    admission_factor_ids: Iterable[str] | None = None,
    reference_factor_expressions: dict[str, str] | None = None,
    reference_cached_factor_ids: Iterable[str] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    payload, factor_items = _load_factor_library(factor_library)
    factor_map = dict(factor_items)
    provider_fingerprint = provider_contract_fingerprint(
        historical_provider,
        market,
    )["fingerprint"]
    feedback_accepted = [
        (factor_id, info)
        for factor_id, info in factor_items
        if (info.get("feedback") or {}).get("decision") is True
    ]
    configured_admission = admission_factor_ids is not None
    requested_admission_ids = (
        [str(factor_id) for factor_id in admission_factor_ids]
        if configured_admission
        else [factor_id for factor_id, _ in feedback_accepted]
    )
    duplicate_admission_ids = sorted(
        {
            factor_id
            for factor_id in requested_admission_ids
            if requested_admission_ids.count(factor_id) > 1
        }
    )
    unknown_admission_ids = sorted(
        set(requested_admission_ids).difference(factor_map)
    )
    admitted_ids = list(dict.fromkeys(requested_admission_ids))
    admitted = [
        (factor_id, factor_map[factor_id])
        for factor_id in admitted_ids
        if factor_id in factor_map
    ]
    admitted_id_set = {factor_id for factor_id, _ in admitted}
    requested_cache_start = required_cache_start or validation_start
    requested_cache_end = required_cache_end or validation_end
    effective_cache_start, effective_cache_end = _effective_session_range(
        QlibBinaryDailySource(historical_provider).calendar,
        requested_cache_start,
        requested_cache_end,
    )
    cache_matches = {
        factor_id: _cache_matches_provider(
            info,
            provider_fingerprint,
            market=market,
            required_start=effective_cache_start,
            required_end=effective_cache_end,
        )
        for factor_id, info in admitted
    }
    requested_reference_cached_ids = [
        str(factor_id) for factor_id in reference_cached_factor_ids or []
    ]
    duplicate_reference_cached_ids = sorted(
        {
            factor_id
            for factor_id in requested_reference_cached_ids
            if requested_reference_cached_ids.count(factor_id) > 1
        }
    )
    unknown_reference_cached_ids = sorted(
        set(requested_reference_cached_ids).difference(factor_map)
    )
    overlapping_reference_cached_ids = sorted(
        set(requested_reference_cached_ids).intersection(admitted_id_set)
    )
    reference_cached_ids = [
        factor_id
        for factor_id in dict.fromkeys(requested_reference_cached_ids)
        if factor_id in factor_map and factor_id not in admitted_id_set
    ]
    reference_cache_matches = {
        factor_id: _cache_matches_provider(
            factor_map[factor_id],
            provider_fingerprint,
            market=market,
            required_start=effective_cache_start,
            required_end=effective_cache_end,
        )
        for factor_id in reference_cached_ids
    }
    reference_missing_consistency = [
        factor_id
        for factor_id in reference_cached_ids
        if not _has_consistency_proof(factor_map[factor_id])
    ]
    reference_stale_cache = [
        factor_id
        for factor_id in reference_cached_ids
        if not reference_cache_matches[factor_id]
    ]
    accepted = [
        (factor_id, info)
        for factor_id, info in admitted
        if _has_consistency_proof(info)
        and cache_matches[factor_id]
    ]
    accepted_map = dict(accepted)
    label_frame = _validation_label_panel(
        historical_provider,
        market,
        label_expression,
        validation_start,
        validation_end,
    )
    label = label_frame["LABEL0"]
    panel = pd.DataFrame(index=label.index)
    errors: dict[str, str] = {}
    coverage: dict[str, float] = {}
    rank_ic: dict[str, float | None] = {}

    for factor_id, info in accepted:
        try:
            series = _load_cached_factor(info, validation_start, validation_end)
            series = normalize_market_index(series.to_frame(factor_id), factor_id)[factor_id]
            aligned = series.reindex(label.index)
            panel[factor_id] = aligned
            coverage[factor_id] = float(aligned.notna().mean())
            rank_ic[factor_id] = _mean_daily_rank_ic(aligned, label)
        except Exception as exc:
            errors[factor_id] = str(exc)
            coverage[factor_id] = 0.0
            rank_ic[factor_id] = None
        finally:
            gc.collect()

    ranked_panel = panel.groupby(level="datetime").rank(pct=True)
    correlations = ranked_panel.corr(min_periods=100)
    reference_correlations = pd.DataFrame()
    reference_redundant: dict[str, dict[str, Any]] = {}
    reference_frames: list[pd.DataFrame] = []
    reference_cached_errors: dict[str, str] = {}
    if reference_factor_expressions and not panel.empty:
        expression_reference_panel = _validation_feature_panel(
            historical_provider,
            market,
            reference_factor_expressions,
            validation_start,
            validation_end,
        ).reindex(label.index)
        reference_frames.append(expression_reference_panel.add_prefix("reference::"))
    if reference_cached_ids and not panel.empty:
        cached_reference_panel = pd.DataFrame(index=label.index)
        for factor_id in reference_cached_ids:
            if (
                factor_id in reference_missing_consistency
                or factor_id in reference_stale_cache
            ):
                continue
            try:
                series = _load_cached_factor(
                    factor_map[factor_id], validation_start, validation_end
                )
                series = normalize_market_index(
                    series.to_frame(factor_id), factor_id
                )[factor_id]
                cached_reference_panel[f"cached::{factor_id}"] = series.reindex(
                    label.index
                )
            except Exception as exc:
                reference_cached_errors[factor_id] = str(exc)
            finally:
                gc.collect()
        if not cached_reference_panel.empty:
            reference_frames.append(cached_reference_panel.add_prefix("reference::"))
    if reference_frames and not panel.empty:
        reference_panel = pd.concat(reference_frames, axis=1)
        ranked_reference = reference_panel.groupby(level="datetime").rank(pct=True)
        combined_ranked = pd.concat([ranked_panel, ranked_reference], axis=1)
        combined_correlations = combined_ranked.corr(min_periods=100)
        reference_columns = list(ranked_reference.columns)
        reference_correlations = combined_correlations.loc[
            list(ranked_panel.columns),
            reference_columns,
        ]
        for factor_id in ranked_panel.columns:
            values = reference_correlations.loc[factor_id].dropna()
            if values.empty:
                continue
            reference_name = str(values.abs().idxmax())
            correlation = float(values.loc[reference_name])
            if abs(correlation) >= correlation_threshold:
                reference_redundant[str(factor_id)] = {
                    "referenceFactor": reference_name.removeprefix("reference::"),
                    "correlation": correlation,
                }
    accepted_ids = [
        factor_id
        for factor_id, _ in accepted
        if factor_id not in errors
        and coverage.get(factor_id, 0.0) >= coverage_threshold
        and rank_ic.get(factor_id) is not None
        and float(rank_ic[factor_id]) > rank_ic_min
        and factor_id not in reference_redundant
    ]
    selected, redundant = select_factor_pool(
        accepted_ids,
        rank_ic,
        correlations,
        correlation_threshold,
    )

    records = []
    missing_consistency = []
    stale_cache = []
    low_coverage = []
    nonpositive_rank_ic = []
    for factor_id, info in factor_items:
        decision = (info.get("feedback") or {}).get("decision")
        consistency_proof = _has_consistency_proof(info)
        cache_matches_provider = cache_matches.get(factor_id, False)
        if factor_id in admitted_id_set and not consistency_proof:
            missing_consistency.append(factor_id)
        if factor_id in admitted_id_set and not cache_matches_provider:
            stale_cache.append(factor_id)
        if (
            factor_id in accepted_map
            and coverage.get(factor_id, 0.0) < coverage_threshold
        ):
            low_coverage.append(factor_id)
        if (
            factor_id in accepted_map
            and rank_ic.get(factor_id) is not None
            and float(rank_ic[factor_id]) <= rank_ic_min
        ):
            nonpositive_rank_ic.append(factor_id)
        records.append(
            {
                "factorId": factor_id,
                "factorName": info.get("factor_name", factor_id),
                "feedbackDecision": decision,
                "admitted": factor_id in admitted_id_set,
                "consistencyProven": consistency_proof,
                "cacheMatchesProvider": cache_matches_provider,
                "coverage": coverage.get(factor_id),
                "validationRankIC": rank_ic.get(factor_id),
                "rankICEligible": factor_id in accepted_ids,
                "selected": factor_id in selected,
                "redundantWith": redundant.get(factor_id),
                "referenceRedundantWith": (
                    reference_redundant.get(factor_id) or {}
                ).get("referenceFactor"),
                "referenceCorrelation": (
                    reference_redundant.get(factor_id) or {}
                ).get("correlation"),
                "error": errors.get(factor_id),
            }
        )

    selected_payload = {
        "metadata": {
            **(payload.get("metadata") or {}),
            "source_library": str(factor_library.resolve()),
            "selection_period": [validation_start, validation_end],
            "correlation_threshold": correlation_threshold,
            "rank_ic_min": rank_ic_min,
            "admission_mode": (
                "preregistered_ids" if configured_admission else "feedback_accepted"
            ),
            "admission_factor_ids": admitted_ids,
            "selected_factor_ids": selected,
        },
        "factors": {
            factor_id: copy.deepcopy(dict(factor_items)[factor_id])
            for factor_id in selected
        },
    }
    blocking_reasons = []
    rejection_reasons = []
    if configured_admission and not requested_admission_ids:
        blocking_reasons.append("预注册候选名单为空。")
    if duplicate_admission_ids:
        blocking_reasons.append("预注册候选名单包含重复因子。")
    if unknown_admission_ids:
        blocking_reasons.append("预注册候选名单包含因子库中不存在的因子。")
    if configured_admission and missing_consistency:
        blocking_reasons.append("部分预注册候选缺少冻结的语义一致性证据。")
    if configured_admission and stale_cache:
        blocking_reasons.append("部分预注册候选的缓存与冻结 Provider 不一致。")
    if duplicate_reference_cached_ids:
        blocking_reasons.append("冻结的缓存参考因子名单包含重复因子。")
    if unknown_reference_cached_ids:
        blocking_reasons.append("冻结的缓存参考因子名单包含因子库中不存在的因子。")
    if overlapping_reference_cached_ids:
        blocking_reasons.append("冻结的缓存参考因子不得与准入候选重叠。")
    if reference_missing_consistency:
        blocking_reasons.append("部分缓存参考因子缺少冻结的语义一致性证据。")
    if reference_stale_cache:
        blocking_reasons.append("部分缓存参考因子的缓存与冻结 Provider 不一致。")
    if reference_cached_errors:
        blocking_reasons.append("部分缓存参考因子无法读取验证期缓存。")
    if not selected:
        rejection_reasons.append(
            "没有同时通过准入、语义证据、Provider 缓存来源、RankIC 和功能去冗余的自定义因子。"
        )
    if errors:
        blocking_reasons.append("部分已接受因子无法读取验证期缓存。")
    report = {
        "status": (
            "FAIL"
            if blocking_reasons
            else "REJECTED"
            if rejection_reasons
            else "PASS"
        ),
        "sourceLibrary": str(factor_library.resolve()),
        "providerFingerprint": provider_fingerprint,
        "validationPeriod": [validation_start, validation_end],
        "correlationThreshold": correlation_threshold,
        "coverageThreshold": coverage_threshold,
        "rankICMinimumExclusive": rank_ic_min,
        "admissionMode": (
            "preregistered_ids" if configured_admission else "feedback_accepted"
        ),
        "admissionFactorIds": admitted_ids,
        "feedbackAcceptedFactorIds": [
            factor_id for factor_id, _ in feedback_accepted
        ],
        "duplicateAdmissionFactorIds": duplicate_admission_ids,
        "unknownAdmissionFactorIds": unknown_admission_ids,
        "requiredCacheRange": [
            requested_cache_start,
            requested_cache_end,
        ],
        "requiredCacheSessionRange": [effective_cache_start, effective_cache_end],
        "selectedFactorIds": selected,
        "excludedMissingConsistency": missing_consistency,
        "excludedStaleCache": stale_cache,
        "excludedLowCoverage": low_coverage,
        "excludedNonPositiveRankIC": nonpositive_rank_ic,
        "redundantFactors": redundant,
        "referenceFactorSource": "+".join(
            source
            for source, enabled in (
                ("declared_expressions", bool(reference_factor_expressions)),
                ("cached_library", bool(requested_reference_cached_ids)),
            )
            if enabled
        )
        or None,
        "referenceFactorNames": list(reference_factor_expressions or {}),
        "referenceCachedFactorIds": reference_cached_ids,
        "duplicateReferenceCachedFactorIds": duplicate_reference_cached_ids,
        "unknownReferenceCachedFactorIds": unknown_reference_cached_ids,
        "overlappingReferenceCachedFactorIds": overlapping_reference_cached_ids,
        "referenceCachedMissingConsistency": reference_missing_consistency,
        "referenceCachedStaleCache": reference_stale_cache,
        "referenceCachedErrors": reference_cached_errors,
        "referenceRedundantFactors": reference_redundant,
        "blockingReasons": blocking_reasons,
        "rejectionReasons": rejection_reasons,
        "factors": records,
        "correlations": {
            row: {
                column: _json_clean(correlations.loc[row, column])
                for column in correlations.columns
            }
            for row in correlations.index
        },
        "referenceCorrelations": {
            row: {
                column.removeprefix("reference::"): _json_clean(
                    reference_correlations.loc[row, column]
                )
                for column in reference_correlations.columns
            }
            for row in reference_correlations.index
        },
    }
    return report, selected_payload


def _resolve_trial_target(
    factors: dict[str, Any],
    factor_audit: dict[str, Any],
    settings: dict[str, Any],
) -> tuple[str, dict[str, Any]]:
    selected_ids = [str(item) for item in factor_audit.get("selectedFactorIds") or []]
    configured_target = str(settings.get("target_factor_id") or "")
    if configured_target:
        target_factor_id = configured_target
        target_selection = {
            "rule": "configured_factor_id",
            "eligibleFactorIds": selected_ids,
        }
    else:
        rule = str(settings.get("target_selection_rule") or "")
        if rule != "highest_validation_rank_ic":
            raise ValueError(
                "target_factor_id or target_selection_rule=highest_validation_rank_ic is required"
            )
        candidate_ids = [
            str(item)
            for item in (
                settings.get("target_candidate_factor_ids")
                or factor_audit.get("admissionFactorIds")
                or []
            )
        ]
        if not candidate_ids or len(candidate_ids) != len(set(candidate_ids)):
            raise ValueError("target candidate factor IDs must be non-empty and unique")
        missing_candidates = sorted(set(candidate_ids).difference(factors))
        if missing_candidates:
            raise ValueError(
                f"target candidate factors are absent from trial universe: {missing_candidates}"
            )
        admitted_ids = [
            str(item) for item in factor_audit.get("admissionFactorIds") or []
        ]
        if admitted_ids and set(candidate_ids) != set(admitted_ids):
            raise ValueError(
                "target candidate factors must match the frozen audit admission universe"
            )
        eligible_ids = [
            factor_id for factor_id in selected_ids if factor_id in candidate_ids
        ]
        if not eligible_ids:
            raise ValueError("no preregistered target candidate passed the selection gates")
        target_factor_id = eligible_ids[0]
        audit_records = {
            str(record.get("factorId")): record
            for record in factor_audit.get("factors") or []
        }
        target_selection = {
            "rule": rule,
            "candidateFactorIds": candidate_ids,
            "eligibleFactorIds": eligible_ids,
            "targetValidationRankIC": (
                audit_records.get(target_factor_id) or {}
            ).get("validationRankIC"),
            "tieBreak": "source_factor_order",
            "selectionPeriod": factor_audit.get("validationPeriod"),
        }

    if target_factor_id not in factors:
        raise ValueError(f"target factor is absent from trial universe: {target_factor_id}")
    if target_factor_id not in selected_ids:
        raise ValueError("target factor did not pass the frozen selection gates")
    return target_factor_id, target_selection


def build_frozen_trial_plan(
    factor_library: Path,
    factor_payload: dict[str, Any],
    factor_audit: dict[str, Any],
    historical_provider: Path,
    provider_fingerprint: str,
    market: str,
    required_start: str,
    required_end: str,
    backtest_config_path: Path,
    output_dir: Path,
    settings: dict[str, Any],
) -> tuple[dict[str, Any], Path]:
    """Freeze one homogeneous Alpha158_20 + single-factor trial per source factor."""
    required_session_start, required_session_end = _effective_session_range(
        QlibBinaryDailySource(historical_provider).calendar,
        required_start,
        required_end,
    )
    factors = factor_payload.get("factors") or {}
    expected_count = int(settings.get("expected_factor_count", 0))
    if (factor_payload.get("metadata") or {}).get("preparation_mode") != "trial_universe":
        raise ValueError("trial matrix requires a trial-universe prepared factor library")
    if not factors or len(factors) != expected_count:
        raise ValueError(
            f"trial-universe factor count mismatch: expected {expected_count}, got {len(factors)}"
        )
    target_factor_id, target_selection = _resolve_trial_target(
        factors,
        factor_audit,
        settings,
    )

    source_value = (factor_payload.get("metadata") or {}).get("source_library")
    source_path = Path(str(source_value or "")).expanduser().resolve()
    source_sha = str((factor_payload.get("metadata") or {}).get("source_library_sha256") or "")
    if not source_path.is_file() or not source_sha or _sha256(source_path) != source_sha:
        raise ValueError("trial-universe source-library binding is invalid")
    factor_audit_path = output_dir / "factor_audit.json"
    if not factor_audit_path.is_file():
        raise ValueError("factor audit must be persisted before freezing the trial plan")

    libraries_dir = output_dir / "trial_libraries"
    libraries_dir.mkdir(parents=True, exist_ok=False)
    trials = []
    target_library_path: Path | None = None
    for index, (factor_id, factor_info) in enumerate(factors.items(), start=1):
        if not _has_consistency_proof(factor_info):
            raise ValueError(f"trial factor lacks frozen consistency proof: {factor_id}")
        if not _cache_matches_provider(
            factor_info,
            provider_fingerprint,
            market=market,
            required_start=required_session_start,
            required_end=required_session_end,
        ):
            raise ValueError(f"trial factor cache binding is invalid: {factor_id}")
        trial_id = f"T{index:02d}_{factor_id[:8]}"
        library_path = libraries_dir / f"{trial_id}.json"
        payload = {
            "metadata": {
                "schema_version": "profit_single_factor_trial_v1",
                "source_library": str(factor_library.resolve()),
                "source_library_sha256": _sha256(factor_library),
                "factor_id": factor_id,
            },
            "factors": {factor_id: copy.deepcopy(factor_info)},
        }
        _write_json(library_path, payload)
        cache = factor_info.get("cache_location") or {}
        trials.append(
            {
                "trialId": trial_id,
                "factorId": factor_id,
                "factorName": factor_info.get("factor_name", factor_id),
                "factorExpressionSha256": hashlib.sha256(
                    str(factor_info.get("factor_expression") or "").encode("utf-8")
                ).hexdigest(),
                "factorLibraryPath": str(library_path.resolve()),
                "factorLibrarySha256": _sha256(library_path),
                "factorCachePath": str(Path(cache["result_h5_path"]).resolve()),
                "factorCacheSha256": cache.get("result_h5_sha256"),
            }
        )
        if factor_id == target_factor_id:
            target_library_path = library_path

    if target_library_path is None:
        raise ValueError("target single-factor library was not created")
    target_trial_id = next(
        trial["trialId"] for trial in trials if trial["factorId"] == target_factor_id
    )
    plan = {
        "schemaVersion": "profit_trial_plan_v2",
        "status": "FROZEN",
        "createdAt": datetime.now(timezone.utc).isoformat(),
        "historicalEvidenceOnly": True,
        "historicalEvidenceReason": "The 2022+ test interval was viewed before this matrix audit.",
        "baselineCandidate": "B0",
        "strategyContract": "Alpha158_20 + exactly one frozen custom factor",
        "providerPath": str(historical_provider.resolve()),
        "providerFingerprint": provider_fingerprint,
        "market": market,
        "requiredDateRange": [required_start, required_end],
        "requiredSessionDateRange": [
            required_session_start,
            required_session_end,
        ],
        "backtestConfigPath": str(backtest_config_path.resolve()),
        "backtestConfigSha256": _sha256(backtest_config_path),
        "preparedFactorLibraryPath": str(factor_library.resolve()),
        "preparedFactorLibrarySha256": _sha256(factor_library),
        "factorAuditPath": str(factor_audit_path.resolve()),
        "factorAuditSha256": _sha256(factor_audit_path),
        "sourceTrialLibraryPath": str(source_path),
        "sourceTrialLibrarySha256": source_sha,
        "expectedTrialCount": expected_count,
        "targetFactorId": target_factor_id,
        "targetSelection": target_selection,
        "selectionContract": {
            "validationPeriod": factor_audit.get("validationPeriod"),
            "correlationThreshold": factor_audit.get("correlationThreshold"),
            "coverageThreshold": factor_audit.get("coverageThreshold"),
            "rankICMinimumExclusive": factor_audit.get("rankICMinimumExclusive"),
            "admissionMode": factor_audit.get("admissionMode"),
            "admissionFactorIds": factor_audit.get("admissionFactorIds"),
        },
        "targetTrialId": target_trial_id,
        "c1AliasOf": target_trial_id,
        "trials": trials,
    }
    return plan, target_library_path


def _return_metrics(returns: pd.Series) -> dict[str, Any]:
    values = pd.to_numeric(returns, errors="coerce").dropna()
    if values.empty:
        return {}
    std = float(values.std(ddof=1))
    cumulative = values.cumsum()
    drawdown = cumulative - cumulative.cummax()
    return {
        "days": int(len(values)),
        "totalExcessReturn": float(values.sum()),
        "annualizedExcessReturn": float(values.mean() * 252.0),
        "informationRatio": float(values.mean() / std * math.sqrt(252.0)) if std > 0 else 0.0,
        "maxDrawdown": float(drawdown.min()),
        "positiveDayRate": float((values > 0).mean()),
    }


def analyze_daily_returns(
    daily: pd.DataFrame,
    cost_multipliers: Iterable[float] = (1.0, 1.5, 2.0),
) -> dict[str, Any]:
    work = daily.copy()
    if "date" in work.columns:
        work["date"] = pd.to_datetime(work["date"])
        work = work.set_index("date")
    else:
        work.index = pd.to_datetime(work.index)

    if {"portfolio_return", "benchmark_return", "cost"}.issubset(work.columns):
        sensitivity = {}
        for multiplier in cost_multipliers:
            returns = (
                work["portfolio_return"]
                - work["benchmark_return"]
                - float(multiplier) * work["cost"]
            )
            sensitivity[str(multiplier)] = _return_metrics(returns)
        standard_returns = (
            work["portfolio_return"] - work["benchmark_return"] - work["cost"]
        )
    elif "daily_excess_return" in work.columns:
        standard_returns = work["daily_excess_return"]
        sensitivity = {"1.0": _return_metrics(standard_returns)}
    else:
        raise ValueError("daily return file has no supported return columns")

    yearly = {
        str(year): _return_metrics(group)
        for year, group in standard_returns.groupby(standard_returns.index.year)
    }
    monthly = {
        str(period): _return_metrics(group)
        for period, group in standard_returns.groupby(standard_returns.index.to_period("M"))
    }
    recent_windows = {
        str(window): _return_metrics(standard_returns.tail(window))
        for window in (60, 120, 252)
        if len(standard_returns) >= window
    }
    return {
        "standard": _return_metrics(standard_returns),
        "yearly": yearly,
        "monthly": monthly,
        "recentWindows": recent_windows,
        "costSensitivity": sensitivity,
    }


def rolling_return_metrics(daily: pd.DataFrame) -> pd.DataFrame:
    work = daily.copy()
    if "date" in work.columns:
        work["date"] = pd.to_datetime(work["date"])
        work = work.set_index("date")
    else:
        work.index = pd.to_datetime(work.index)
    returns = work["daily_excess_return"].astype(float)
    frames = []
    for window in (60, 120, 252):
        rolling_mean = returns.rolling(window).mean() * 252.0
        rolling_std = returns.rolling(window).std(ddof=1)
        frames.append(
            pd.DataFrame(
                {
                    "date": returns.index,
                    "window": window,
                    "annualized_excess_return": rolling_mean,
                    "information_ratio": rolling_mean / (rolling_std * math.sqrt(252.0)),
                }
            ).dropna()
        )
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def _candidate_config(
    base_config: dict[str, Any],
    candidate_id: str,
    factor_source: str,
    factor_json: Path | None,
    output_dir: Path,
    provider: Path,
    provider_fingerprint: str,
    decision_filter: str,
    expected_factor_count: int | None = None,
    expected_factor_ids: list[str] | None = None,
    strict_custom: bool = False,
) -> dict[str, Any]:
    config = copy.deepcopy(base_config)
    config["data"]["provider_uri"] = str(provider)
    config["data"]["provider_strict"] = True
    config["data"]["provider_fingerprint"] = provider_fingerprint
    config["experiment"]["name"] = f"profit_validation_{candidate_id.lower()}"
    config["experiment"]["recorder"] = f"profit_validation_{candidate_id.lower()}"
    config["experiment"]["output_dir"] = str(output_dir)
    config["factor_source"]["type"] = factor_source
    config["factor_source"]["custom"]["json_files"] = [str(factor_json)] if factor_json else []
    config["factor_source"]["custom"]["decision_filter"] = decision_filter
    config["factor_source"]["custom"]["strict"] = strict_custom
    config["factor_source"]["custom"]["expected_factor_count"] = expected_factor_count
    config["factor_source"]["custom"]["expected_factor_ids"] = expected_factor_ids
    config.setdefault("llm", {})
    config["llm"]["cache_dir"] = str(output_dir.parent / "factor_cache")
    config["llm"]["cache_namespace"] = provider_fingerprint
    config["llm"]["auto_extract_cache"] = False
    return config


def _write_trial_ledger(path: Path, rows: list[dict[str, Any]]) -> None:
    fieldnames = [
        "candidate",
        "factor_id",
        "factor_name",
        "factor_source",
        "alias_of",
        "status",
        "started_at",
        "finished_at",
        "metrics_path",
        "error",
    ]
    with path.open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def _check(
    check_id: str,
    passed: bool,
    actual: Any,
    required: Any,
    reason: str,
) -> dict[str, Any]:
    return {
        "id": check_id,
        "status": "PASS" if passed else "FAIL",
        "actual": actual,
        "required": required,
        "reason": "" if passed else reason,
    }


def _incremental_bootstrap(
    baseline_path: str,
    combined_path: str,
    iterations: int = 1000,
    block_size: int = 20,
    seed: int = 42,
) -> dict[str, Any]:
    baseline = pd.read_csv(baseline_path, parse_dates=["date"]).set_index("date")
    combined = pd.read_csv(combined_path, parse_dates=["date"]).set_index("date")
    joined = baseline[["daily_excess_return"]].join(
        combined[["daily_excess_return"]],
        how="inner",
        lsuffix="_b0",
        rsuffix="_c1",
    ).dropna()
    delta = (
        joined["daily_excess_return_c1"] - joined["daily_excess_return_b0"]
    ).to_numpy(dtype=float)
    if len(delta) < block_size * 2:
        return {
            "status": "INSUFFICIENT",
            "days": len(delta),
            "iterations": iterations,
            "blockSize": block_size,
        }
    rng = np.random.default_rng(seed)
    starts = np.arange(0, len(delta) - block_size + 1)
    estimates = np.empty(iterations, dtype=float)
    blocks_needed = math.ceil(len(delta) / block_size)
    for index in range(iterations):
        sampled_starts = rng.choice(starts, size=blocks_needed, replace=True)
        sample = np.concatenate(
            [delta[start : start + block_size] for start in sampled_starts]
        )[: len(delta)]
        estimates[index] = float(sample.mean() * 252.0)
    return {
        "status": "PASS",
        "days": len(delta),
        "iterations": iterations,
        "blockSize": block_size,
        "annualizedDelta": float(delta.mean() * 252.0),
        "confidenceInterval90": [
            float(np.percentile(estimates, 5)),
            float(np.percentile(estimates, 95)),
        ],
    }


def _daily_sharpe(returns: pd.Series | np.ndarray) -> float | None:
    values = pd.to_numeric(pd.Series(returns), errors="coerce").dropna()
    if len(values) < 3:
        return None
    std = float(values.std(ddof=1))
    if std <= 0 or not math.isfinite(std):
        return None
    return float(values.mean() / std)


def deflated_sharpe_ratio(
    target_returns: pd.Series,
    trial_returns: pd.DataFrame,
    total_trials: int,
    minimum_observed_trials: int | None = None,
) -> dict[str, Any]:
    """DSR where declared trials set the penalty and observed trials set completeness."""
    required_observed_trials = (
        int(total_trials)
        if minimum_observed_trials is None
        else int(minimum_observed_trials)
    )
    target = pd.to_numeric(target_returns, errors="coerce").dropna()
    trial_sharpes = [
        value
        for value in (_daily_sharpe(trial_returns[column]) for column in trial_returns)
        if value is not None
    ]
    observed = _daily_sharpe(target)
    if observed is None or len(target) < 3 or len(trial_sharpes) < 2 or total_trials < 2:
        return {
            "status": "INSUFFICIENT",
            "days": int(len(target)),
            "observedTrialReturns": len(trial_sharpes),
            "declaredTrials": int(total_trials),
            "requiredObservedTrials": required_observed_trials,
        }
    if required_observed_trials < 2 or required_observed_trials > total_trials:
        return {
            "status": "INVALID",
            "days": int(len(target)),
            "observedTrialReturns": len(trial_sharpes),
            "declaredTrials": int(total_trials),
            "requiredObservedTrials": required_observed_trials,
            "reason": "required observed trials must be between 2 and declared trials",
        }

    trial_std = float(np.std(trial_sharpes, ddof=1))
    normal = NormalDist()
    euler_gamma = 0.5772156649015329
    max_z = (
        (1.0 - euler_gamma) * normal.inv_cdf(1.0 - 1.0 / total_trials)
        + euler_gamma
        * normal.inv_cdf(1.0 - 1.0 / (total_trials * math.e))
    )
    # Under the DSR null, the expected maximum is centered at zero skill.
    expected_max = trial_std * max_z
    skewness = float(target.skew())
    kurtosis = float(target.kurt()) + 3.0
    variance_term = (
        1.0
        - skewness * observed
        + ((kurtosis - 1.0) / 4.0) * observed * observed
    )
    if variance_term <= 0 or not math.isfinite(variance_term):
        return {
            "status": "INVALID",
            "days": int(len(target)),
            "observedTrialReturns": len(trial_sharpes),
            "declaredTrials": int(total_trials),
            "requiredObservedTrials": required_observed_trials,
            "varianceTerm": variance_term,
        }

    statistic = (
        (observed - expected_max)
        * math.sqrt(len(target) - 1.0)
        / math.sqrt(variance_term)
    )
    probability = float(normal.cdf(statistic))
    evidence_complete = len(trial_sharpes) >= required_observed_trials
    return {
        "status": "PASS" if evidence_complete else "LIMITED",
        "days": int(len(target)),
        "declaredTrials": int(total_trials),
        "observedTrialReturns": len(trial_sharpes),
        "requiredObservedTrials": required_observed_trials,
        "evidenceComplete": evidence_complete,
        "dailyObservedSharpe": observed,
        "dailyExpectedMaximumSharpe": expected_max,
        "annualizedObservedSharpe": observed * math.sqrt(252.0),
        "annualizedExpectedMaximumSharpe": expected_max * math.sqrt(252.0),
        "trialSharpeMean": float(np.mean(trial_sharpes)),
        "trialSharpeStd": trial_std,
        "skewness": skewness,
        "kurtosis": kurtosis,
        "statistic": statistic,
        "probability": probability,
    }


def probability_of_backtest_overfitting(
    trial_returns: pd.DataFrame,
    partitions: int = 16,
    minimum_strategies: int = 10,
) -> dict[str, Any]:
    """Estimate PBO with deterministic combinatorially symmetric CV."""
    matrix = trial_returns.apply(pd.to_numeric, errors="coerce").dropna(how="any")
    strategy_count = int(matrix.shape[1])
    if partitions < 2 or partitions % 2 != 0:
        return {
            "status": "INVALID",
            "reason": "partitions must be an even integer >= 2",
            "partitions": partitions,
        }
    if strategy_count < minimum_strategies:
        return {
            "status": "INSUFFICIENT",
            "reason": "too few complete strategy return series for reliable OOS ranks",
            "strategies": strategy_count,
            "minimumStrategies": minimum_strategies,
            "days": int(len(matrix)),
            "partitions": partitions,
        }
    block_size = len(matrix) // partitions
    if block_size < 3:
        return {
            "status": "INSUFFICIENT",
            "reason": "too few observations per CSCV partition",
            "strategies": strategy_count,
            "days": int(len(matrix)),
            "partitions": partitions,
        }

    usable_rows = block_size * partitions
    dropped_rows = len(matrix) - usable_rows
    usable_matrix = matrix.iloc[dropped_rows:]
    values = usable_matrix.to_numpy(dtype=float)
    blocks = np.arange(usable_rows).reshape(partitions, block_size)
    logits: list[float] = []
    half = partitions // 2
    all_blocks = set(range(partitions))
    for train_blocks in combinations(range(partitions), half):
        test_blocks = sorted(all_blocks.difference(train_blocks))
        train_index = blocks[list(train_blocks)].reshape(-1)
        test_index = blocks[test_blocks].reshape(-1)
        train = values[train_index]
        test = values[test_index]
        train_std = train.std(axis=0, ddof=1)
        test_std = test.std(axis=0, ddof=1)
        train_sharpe = np.divide(
            train.mean(axis=0),
            train_std,
            out=np.full(strategy_count, -np.inf),
            where=train_std > 0,
        )
        test_sharpe = np.divide(
            test.mean(axis=0),
            test_std,
            out=np.full(strategy_count, -np.inf),
            where=test_std > 0,
        )
        winner = int(np.argmax(train_sharpe))
        rank = float(pd.Series(test_sharpe).rank(method="average").iloc[winner])
        relative_rank = rank / (strategy_count + 1.0)
        logits.append(float(math.log(relative_rank / (1.0 - relative_rank))))

    pbo = float(np.mean(np.asarray(logits) < 0.0))
    return {
        "status": "PASS",
        "probability": pbo,
        "strategies": strategy_count,
        "days": int(len(matrix)),
        "usableDays": usable_rows,
        "droppedDays": int(dropped_rows),
        "usedDateRange": [
            _json_clean(usable_matrix.index[0]),
            _json_clean(usable_matrix.index[-1]),
        ],
        "droppedDateRange": (
            [_json_clean(matrix.index[0]), _json_clean(matrix.index[dropped_rows - 1])]
            if dropped_rows
            else None
        ),
        "partitions": partitions,
        "combinations": len(logits),
        "medianLogit": float(np.median(logits)),
    }


def deduplicate_trial_returns(
    trial_returns: pd.DataFrame,
) -> tuple[pd.DataFrame, dict[str, str]]:
    """Keep one representative for identical realized return paths."""
    kept: list[str] = []
    aliases: dict[str, str] = {}
    for column in trial_returns.columns:
        duplicate = next(
            (
                representative
                for representative in kept
                if trial_returns[column].equals(trial_returns[representative])
            ),
            None,
        )
        if duplicate is None:
            kept.append(str(column))
        else:
            aliases[str(column)] = duplicate
    return trial_returns[kept].copy(), aliases


def _candidate_return_matrix(candidates: dict[str, Any]) -> pd.DataFrame:
    series = []
    for candidate_id, result in candidates.items():
        path_value = result.get("dailyPath") if result.get("status") == "completed" else None
        if not path_value:
            continue
        frame = pd.read_csv(path_value, parse_dates=["date"])
        if "daily_excess_return" not in frame:
            raise ValueError(f"candidate {candidate_id} has no daily_excess_return")
        series.append(
            frame.set_index("date")["daily_excess_return"].rename(candidate_id)
        )
    return pd.concat(series, axis=1, join="inner").dropna() if series else pd.DataFrame()


def _load_daily_pnl(path: str | Path, candidate_id: str) -> pd.DataFrame:
    required = {
        "date",
        "portfolio_return",
        "benchmark_return",
        "cost",
        "daily_excess_return",
    }
    frame = pd.read_csv(path)
    missing = sorted(required.difference(frame.columns))
    if missing:
        raise ValueError(f"candidate {candidate_id} is missing P&L columns: {missing}")
    ordered_columns = [
        "date",
        "portfolio_return",
        "benchmark_return",
        "cost",
        "daily_excess_return",
    ]
    frame = frame[ordered_columns].copy()
    frame["date"] = pd.to_datetime(frame["date"], errors="raise")
    if frame["date"].duplicated().any():
        raise ValueError(f"candidate {candidate_id} has duplicate dates")
    if not frame["date"].is_monotonic_increasing:
        raise ValueError(f"candidate {candidate_id} dates are not strictly ordered")
    numeric = sorted(required.difference({"date"}))
    frame[numeric] = frame[numeric].apply(pd.to_numeric, errors="coerce")
    if frame[numeric].isna().any().any():
        raise ValueError(f"candidate {candidate_id} has non-numeric or missing P&L values")
    expected_excess = (
        frame["portfolio_return"] - frame["benchmark_return"] - frame["cost"]
    )
    if not np.allclose(
        expected_excess.to_numpy(dtype=float),
        frame["daily_excess_return"].to_numpy(dtype=float),
        rtol=1e-9,
        atol=1e-12,
    ):
        raise ValueError(f"candidate {candidate_id} violates the excess-return identity")
    return frame.set_index("date")


def build_strict_trial_matrix(
    candidates: dict[str, Any],
    trial_plan: dict[str, Any],
    output_dir: Path | None = None,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Build complete C1-like incremental returns without date intersection."""
    baseline_id = str(trial_plan.get("baselineCandidate") or "B0")
    trials = trial_plan.get("trials") or []
    expected_ids = [str(trial["trialId"]) for trial in trials]
    if not expected_ids or len(expected_ids) != len(set(expected_ids)):
        raise ValueError("trial plan must contain unique ordered trial IDs")
    baseline_result = candidates.get(baseline_id) or {}
    if baseline_result.get("status") != "completed" or not baseline_result.get("dailyPath"):
        raise ValueError(f"baseline candidate {baseline_id} is incomplete")
    baseline = _load_daily_pnl(baseline_result["dailyPath"], baseline_id)

    matrix = pd.DataFrame(index=baseline.index)
    result_bindings: dict[str, Any] = {}
    for trial in trials:
        trial_id = str(trial["trialId"])
        result = candidates.get(trial_id) or {}
        if result.get("status") != "completed" or not result.get("dailyPath"):
            raise ValueError(f"planned trial {trial_id} is incomplete")
        daily = _load_daily_pnl(result["dailyPath"], trial_id)
        if not daily.index.equals(baseline.index):
            raise ValueError(f"planned trial {trial_id} dates do not match {baseline_id}")
        matrix[trial_id] = (
            daily["daily_excess_return"] - baseline["daily_excess_return"]
        )
        result_bindings[trial_id] = {
            "factorId": trial.get("factorId"),
            "dailyPath": str(Path(result["dailyPath"]).resolve()),
            "dailySha256": _sha256(Path(result["dailyPath"])),
        }

    if matrix.isna().any().any() or list(matrix.columns) != expected_ids:
        raise ValueError("trial matrix is incomplete or column order changed")

    manifest = {
        "status": "PASS",
        "baselineCandidate": baseline_id,
        "baselineDailyPath": str(Path(baseline_result["dailyPath"]).resolve()),
        "baselineDailySha256": _sha256(Path(baseline_result["dailyPath"])),
        "expectedTrialIds": expected_ids,
        "actualTrialIds": list(matrix.columns),
        "trialCount": len(expected_ids),
        "rows": int(len(matrix)),
        "dateRange": [matrix.index.min().date().isoformat(), matrix.index.max().date().isoformat()],
        "targetTrialId": trial_plan.get("targetTrialId"),
        "resultBindings": result_bindings,
    }
    if output_dir is not None:
        output_dir.mkdir(parents=True, exist_ok=True)
        matrix_path = output_dir / "trial_matrix.csv"
        matrix.rename_axis("date").to_csv(matrix_path)
        manifest.update(
            {
                "path": str(matrix_path.resolve()),
                "sha256": _sha256(matrix_path),
            }
        )
        _write_json(output_dir / "trial_matrix_manifest.json", manifest)
    return matrix, manifest


def _verify_trial_count_evidence(
    project_root: Path,
    factor_payload: dict[str, Any],
    settings: dict[str, Any],
) -> tuple[dict[str, Any], Path | None]:
    expected = int(settings.get("total_trials", 0))
    evidence_value = settings.get("trial_count_evidence")
    if not evidence_value:
        return {
            "status": "MISSING",
            "expectedTrials": expected,
            "reason": "trial-count evidence path is not configured",
        }, None

    evidence_path = _resolve(project_root, evidence_value)
    result = {
        "status": "MISSING",
        "path": str(evidence_path),
        "expectedTrials": expected,
    }
    if not evidence_path.is_file():
        result["reason"] = "trial-count evidence file is unavailable"
        return result, evidence_path

    try:
        payload = json.loads(evidence_path.read_text(encoding="utf-8"))
        actual = int((payload.get("summary") or {}).get("libraryFactorCount", 0))
        evidence_library_sha = str(
            (((payload.get("inputs") or {}).get("library") or {}).get("sha256") or "")
        )
        factor_metadata = factor_payload.get("metadata") or {}
        factor_source_sha = str(
            factor_metadata.get("trial_universe_library_sha256")
            or factor_metadata.get("upstream_source_library_sha256")
            or factor_metadata.get("source_library_sha256")
            or ""
        )
        count_matches = expected >= 2 and actual == expected
        source_matches = bool(factor_source_sha) and factor_source_sha == evidence_library_sha
        result.update(
            {
                "status": "PASS" if count_matches and source_matches else "FAIL",
                "sha256": _sha256(evidence_path),
                "actualTrials": actual,
                "countMatches": count_matches,
                "factorSourceLibrarySha256": factor_source_sha,
                "evidenceLibrarySha256": evidence_library_sha,
                "sourceBindingMatches": source_matches,
                "sourceAuditStatus": payload.get("status"),
            }
        )
        if result["status"] != "PASS":
            result["reason"] = "trial count or source-library binding does not match"
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        result.update({"status": "INVALID", "reason": str(exc)})
    return result, evidence_path


def _verify_historical_trial_audit(
    project_root: Path,
    settings: dict[str, Any],
    admission_factor_ids: Iterable[str] | None = None,
) -> tuple[dict[str, Any], Path | None]:
    try:
        declared_trials = int(settings.get("total_trials", 0))
        planned_new_trials = int(settings.get("planned_new_trials", -1))
    except (TypeError, ValueError) as exc:
        return {"status": "INVALID", "reason": str(exc)}, None
    planned_ids = [str(item) for item in settings.get("planned_new_trial_ids") or []]
    expected_admission_ids = [str(item) for item in admission_factor_ids or []]
    planned_breakdown = settings.get("planned_new_evaluations") or {}
    audit_value = settings.get("historical_trial_audit")
    expected_sha = str(settings.get("historical_trial_audit_sha256") or "")
    if not audit_value:
        return {
            "status": "MISSING",
            "declaredTrials": declared_trials,
            "plannedNewTrials": planned_new_trials,
            "plannedNewTrialIds": planned_ids,
            "reason": "historical trial audit path is not configured",
        }, None

    audit_path = _resolve(project_root, audit_value)
    try:
        audit_path.relative_to(project_root.resolve())
    except ValueError:
        return {
            "status": "INVALID",
            "path": str(audit_path),
            "reason": "historical trial audit must stay inside the project root",
        }, audit_path
    result = {
        "status": "MISSING",
        "path": str(audit_path),
        "declaredTrials": declared_trials,
        "plannedNewTrials": planned_new_trials,
        "plannedNewTrialIds": planned_ids,
        "plannedNewEvaluations": planned_breakdown,
        "admissionFactorIds": expected_admission_ids,
        "expectedSha256": expected_sha,
    }
    if not audit_path.is_file():
        result["reason"] = "historical trial audit file is unavailable"
        return result, audit_path

    try:
        actual_sha = _sha256(audit_path)
        payload = json.loads(audit_path.read_text(encoding="utf-8"))
        summary = payload.get("summary") or {}
        historical_floor = int(
            summary.get("declaredRevealedDecisionOpportunityFloor", 0)
        )
        minimum_declared = historical_floor + planned_new_trials
        schema_matches = payload.get("schemaVersion") == TRIAL_HISTORY_AUDIT_SCHEMA
        source_passed = payload.get("status") == "PASS"
        sha_matches = bool(expected_sha) and actual_sha == expected_sha
        audit_id_matches = bool(payload.get("auditId")) and (
            payload.get("auditId") == trial_history_audit_id(payload)
        )
        issues = payload.get("issues") or []
        unresolved_count = int(summary.get("unresolvedIssueCount", -1))
        issues_resolved = unresolved_count == 0 and not any(
            item.get("severity") == "BLOCKING"
            for item in issues
            if isinstance(item, dict)
        )
        trials = payload.get("trials") or []
        counted_trials = [
            item
            for item in trials
            if isinstance(item, dict) and item.get("countedForPenalty") is True
        ]
        counted_keys = [
            (str(item.get("stage") or ""), str(item.get("evaluationKey") or ""))
            for item in counted_trials
        ]
        trial_counts_match = (
            len(trials) == int(summary.get("trialRecordCount", -1))
            and len(counted_trials) == int(summary.get("countedTrialRecordCount", -1))
            and len(counted_trials) == historical_floor
            and all(stage and key for stage, key in counted_keys)
            and len(counted_keys) == len(set(counted_keys))
            and not any(item.get("aliasOf") for item in counted_trials)
        )
        runs = payload.get("runs") or []
        run_counts_match = len(runs) == int(summary.get("manifestCount", -1))
        inputs = payload.get("inputs") or {}
        input_files = inputs.get("files") or []
        input_paths = [str(item.get("path") or "") for item in input_files]
        inputs_match = (
            len(input_files) == int(inputs.get("fileCount", -1))
            and len(input_paths) == len(set(input_paths))
            and all(input_paths)
            and bool(inputs.get("fingerprint"))
            and canonical_sha256(input_files) == inputs.get("fingerprint")
        )
        actual_inputs_match = inputs_match
        if actual_inputs_match:
            for item in input_files:
                input_path = (project_root / str(item.get("path") or "")).resolve()
                try:
                    input_path.relative_to(project_root.resolve())
                except ValueError:
                    actual_inputs_match = False
                    break
                if (
                    not input_path.is_file()
                    or _sha256(input_path) != str(item.get("sha256") or "")
                ):
                    actual_inputs_match = False
                    break
        policy = payload.get("policy") or {}
        code_bindings = {
            "auditCodeSha256": project_root
            / "quantaalpha"
            / "validation"
            / "trial_history.py",
            "entrypointSha256": project_root
            / "scripts"
            / "audit_profit_trial_history.py",
            "verifierCodeSha256": project_root
            / "quantaalpha"
            / "validation"
            / "profit.py",
            "factorFunctionLibrarySha256": project_root
            / "quantaalpha"
            / "factors"
            / "coder"
            / "function_lib.py",
            "factorParserSha256": project_root
            / "quantaalpha"
            / "factors"
            / "coder"
            / "expr_parser.py",
            "factorCalculatorSha256": project_root
            / "quantaalpha"
            / "backtest"
            / "custom_factor_calculator.py",
            "factorPreparationSha256": project_root
            / "scripts"
            / "prepare_profit_factor_library.py",
            "reconciliationSha256": project_root
            / "configs"
            / "profit_trial_history_reconciliation_v1.json",
        }
        code_matches = all(
            path.is_file()
            and bool(policy.get(field))
            and _sha256(path) == policy.get(field)
            for field, path in code_bindings.items()
        )
        artifact_root = audit_path.parent.resolve()
        derived = payload.get("derivedArtifacts") or {}
        required_artifacts = {"runs", "trials", "issues", "report"}
        artifacts_match = set(derived) == required_artifacts
        if artifacts_match:
            for item in derived.values():
                artifact_path = (artifact_root / str(item.get("path") or "")).resolve()
                if (
                    artifact_path.parent != artifact_root
                    or not artifact_path.is_file()
                    or _sha256(artifact_path) != str(item.get("sha256") or "")
                ):
                    artifacts_match = False
                    break
        planned_ids_valid = (
            len(planned_ids) == len(set(planned_ids))
            and all(planned_ids)
            and sorted(planned_ids) == sorted(expected_admission_ids)
        )
        try:
            planned_admission = int(planned_breakdown.get("admission", -1))
            planned_matrix = int(planned_breakdown.get("formal_matrix", -1))
            planned_factor_only = int(planned_breakdown.get("factor_only", -1))
            matrix_trials = int(settings.get("matrix_trials", -1))
        except (TypeError, ValueError):
            planned_admission = planned_matrix = planned_factor_only = matrix_trials = -1
        planned_breakdown_valid = (
            planned_admission == len(planned_ids)
            and planned_matrix == matrix_trials
            and matrix_trials >= 2
            and planned_factor_only == len(planned_ids)
            and planned_new_trials
            == planned_admission + planned_matrix + planned_factor_only
        )
        revealed_stages = {
            "MINING_REVEALED",
            "ADMISSION_REVEALED",
            "STRATEGY_REVEALED",
        }
        planned_ids_are_new = not any(
            str(item.get("factorId") or "") in set(planned_ids)
            and item.get("stage") in revealed_stages
            for item in trials
            if isinstance(item, dict)
        )
        declared_ids = {
            str(item.get("factorId") or "")
            for item in trials
            if isinstance(item, dict) and item.get("stage") == "DECLARED"
        }
        planned_ids_declared = bool(planned_ids) and set(planned_ids).issubset(
            declared_ids
        )
        floor_valid = historical_floor >= 2 and planned_new_trials >= 1
        count_sufficient = floor_valid and declared_trials >= minimum_declared
        passed = all(
            (
                schema_matches,
                source_passed,
                sha_matches,
                audit_id_matches,
                issues_resolved,
                trial_counts_match,
                run_counts_match,
                inputs_match,
                actual_inputs_match,
                code_matches,
                artifacts_match,
                planned_ids_valid,
                planned_breakdown_valid,
                planned_ids_are_new,
                planned_ids_declared,
                count_sufficient,
            )
        )
        result.update(
            {
                "status": "PASS" if passed else "FAIL",
                "sha256": actual_sha,
                "schemaVersion": payload.get("schemaVersion"),
                "schemaMatches": schema_matches,
                "sourceAuditStatus": payload.get("status"),
                "sourceAuditPassed": source_passed,
                "shaMatches": sha_matches,
                "auditId": payload.get("auditId"),
                "auditIdMatches": audit_id_matches,
                "issuesResolved": issues_resolved,
                "trialCountsMatch": trial_counts_match,
                "runCountsMatch": run_counts_match,
                "inputsMatch": inputs_match,
                "actualInputsMatch": actual_inputs_match,
                "codeMatches": code_matches,
                "artifactsMatch": artifacts_match,
                "plannedIdsValid": planned_ids_valid,
                "plannedBreakdownValid": planned_breakdown_valid,
                "plannedIdsAreNew": planned_ids_are_new,
                "plannedIdsDeclared": planned_ids_declared,
                "historicalTrialFloor": historical_floor,
                "minimumDeclaredTrials": minimum_declared,
                "countSufficient": count_sufficient,
                "inputFingerprint": inputs.get("fingerprint"),
            }
        )
        if not passed:
            result["reason"] = (
                "historical trial audit integrity, planned IDs, or declared trial floor does not match"
            )
    except (OSError, TypeError, ValueError, AttributeError, json.JSONDecodeError) as exc:
        result.update({"status": "INVALID", "reason": str(exc)})
    return result, audit_path


def _multiple_testing_audit(
    candidates: dict[str, Any],
    settings: dict[str, Any],
) -> dict[str, Any]:
    required = bool(settings.get("required", False))
    if not required and not settings.get("enabled", False):
        return {"status": "NOT_REQUIRED", "required": False}

    historical_evidence = settings.get("historicalTrialEvidence") or {}
    if settings.get("historical_trial_audit") and not historical_evidence:
        return {
            "status": "BLOCKED",
            "required": required,
            "reason": "历史试验审计已配置但未完成绑定校验。",
            "historicalTrialEvidence": {},
        }
    if historical_evidence and historical_evidence.get("status") != "PASS":
        return {
            "status": "BLOCKED",
            "required": required,
            "reason": historical_evidence.get("reason")
            or "历史试验计数审计未通过。",
            "historicalTrialEvidence": historical_evidence,
        }

    matrix_evidence = settings.get("trialMatrix") or {}
    if matrix_evidence:
        if matrix_evidence.get("status") != "PASS":
            return {
                "status": "BLOCKED",
                "required": required,
                "reason": matrix_evidence.get("reason")
                or "严格收益矩阵未通过完整性检查。",
                "trialMatrix": matrix_evidence,
            }
        matrix_path = Path(str(matrix_evidence.get("path") or ""))
        expected_sha = str(matrix_evidence.get("sha256") or "")
        expected_ids = [str(item) for item in matrix_evidence.get("expectedTrialIds") or []]
        target_trial_id = str(matrix_evidence.get("targetTrialId") or "")
        declared_trials = int(settings.get("total_trials", 0))
        matrix_trials = int(settings.get("matrix_trials", declared_trials))
        if (
            not matrix_path.is_file()
            or not expected_sha
            or _sha256(matrix_path) != expected_sha
            or not expected_ids
            or matrix_trials < 2
            or declared_trials < matrix_trials
            or len(expected_ids) != matrix_trials
            or target_trial_id not in expected_ids
        ):
            return {
                "status": "BLOCKED",
                "required": required,
                "reason": "收益矩阵文件、哈希、目标或预注册试验数不匹配。",
                "trialMatrix": matrix_evidence,
            }
        try:
            returns = pd.read_csv(matrix_path, parse_dates=["date"]).set_index("date")
            if (
                returns.index.duplicated().any()
                or not returns.index.is_monotonic_increasing
                or list(returns.columns) != expected_ids
            ):
                raise ValueError("trial matrix dates or ordered columns changed")
            returns = returns.apply(pd.to_numeric, errors="coerce")
            if returns.isna().any().any():
                raise ValueError("trial matrix contains missing or non-numeric values")
        except (OSError, TypeError, ValueError) as exc:
            return {
                "status": "BLOCKED",
                "required": required,
                "reason": str(exc),
                "trialMatrix": matrix_evidence,
            }

        dsr = deflated_sharpe_ratio(
            returns[target_trial_id],
            returns,
            declared_trials,
            minimum_observed_trials=matrix_trials,
        )
        pbo_settings = settings.get("pbo") or {}
        pbo_returns, pbo_aliases = deduplicate_trial_returns(returns)
        pbo = probability_of_backtest_overfitting(
            pbo_returns,
            partitions=int(pbo_settings.get("partitions", 16)),
            minimum_strategies=int(pbo_settings.get("minimum_strategies", 10)),
        )
        dsr_min = float(settings.get("dsr_probability_min", 0.95))
        pbo_required = bool(pbo_settings.get("required", False))
        pbo_max = float(pbo_settings.get("probability_max", 0.5))
        dsr_passed = dsr.get("status") == "PASS" and dsr.get("probability", 0.0) >= dsr_min
        pbo_passed = (
            not pbo_required
            or (pbo.get("status") == "PASS" and pbo.get("probability", 1.0) <= pbo_max)
        )
        complete = (
            dsr.get("status") == "PASS"
            and (not pbo_required or pbo.get("status") == "PASS")
        )
        status = "PASS" if dsr_passed and pbo_passed else "FAIL" if complete else "INSUFFICIENT"
        return {
            "status": status,
            "required": required,
            "reason": "" if status == "PASS" else (
                "多重试验调整后的统计证据未达到门槛。"
                if status == "FAIL"
                else "多重试验收益序列不足。"
            ),
            "target": f"{target_trial_id} incremental daily excess return versus B0",
            "declaredTrialCount": declared_trials,
            "matrixTrialCount": matrix_trials,
            "availableIncrementalTrials": list(returns.columns),
            "historicalTrialEvidence": historical_evidence,
            "trialMatrix": matrix_evidence,
            "deflatedSharpe": {
                **dsr,
                "gateStatus": (
                    "PASS"
                    if dsr_passed
                    else "FAIL"
                    if dsr.get("status") == "PASS"
                    else "NOT_EVALUABLE"
                ),
            },
            "deflatedSharpeRequired": f">= {dsr_min}",
            "pbo": {
                **pbo,
                "gateStatus": (
                    "NOT_REQUIRED"
                    if not pbo_required
                    else "PASS"
                    if pbo_passed
                    else "FAIL"
                    if pbo.get("status") == "PASS"
                    else "NOT_EVALUABLE"
                ),
            },
            "pboReturnAliases": pbo_aliases,
            "pboUniqueStrategies": list(pbo_returns.columns),
            "pboRequired": pbo_required,
            "pboThreshold": f"<= {pbo_max}",
        }

    evidence = settings.get("trialCountEvidence") or {}
    total_trials = int(evidence.get("actualTrials") or settings.get("total_trials", 0))
    try:
        returns = _candidate_return_matrix(candidates)
    except Exception as exc:
        return {
            "status": "INSUFFICIENT",
            "required": required,
            "reason": str(exc),
            "trialCountEvidence": evidence,
        }
    if "B0" not in returns or "C1" not in returns or total_trials < 2:
        return {
            "status": "INSUFFICIENT",
            "required": required,
            "reason": "B0/C1 returns or verified trial count is unavailable",
            "availableStrategies": list(returns.columns),
            "trialCountEvidence": evidence,
        }

    incremental = returns.drop(columns="B0").subtract(returns["B0"], axis=0)
    dsr = deflated_sharpe_ratio(
        incremental["C1"],
        incremental,
        total_trials,
    )
    pbo_settings = settings.get("pbo") or {}
    pbo_returns, pbo_aliases = deduplicate_trial_returns(incremental)
    pbo = probability_of_backtest_overfitting(
        pbo_returns,
        partitions=int(pbo_settings.get("partitions", 16)),
        minimum_strategies=int(pbo_settings.get("minimum_strategies", 10)),
    )
    dsr_min = float(settings.get("dsr_probability_min", 0.95))
    pbo_required = bool(pbo_settings.get("required", False))
    pbo_max = float(pbo_settings.get("probability_max", 0.5))
    evidence_valid = evidence.get("status") == "PASS"
    dsr_passed = dsr.get("status") == "PASS" and dsr.get("probability", 0.0) >= dsr_min
    pbo_passed = (
        not pbo_required
        or (pbo.get("status") == "PASS" and pbo.get("probability", 1.0) <= pbo_max)
    )
    if evidence_valid and dsr_passed and pbo_passed:
        status = "PASS"
        reason = ""
    elif dsr.get("status") in {"INSUFFICIENT", "LIMITED", "INVALID"} or (
        pbo_required and pbo.get("status") != "PASS"
    ) or not evidence_valid:
        status = "INSUFFICIENT"
        reason = "多重试验收益序列或试验计数证据不完整。"
    else:
        status = "FAIL"
        reason = "多重试验调整后的统计证据未达到门槛。"
    return {
        "status": status,
        "required": required,
        "reason": reason,
        "historicalTrialEvidence": historical_evidence,
        "trialCountEvidence": evidence,
        "target": "C1 minus B0 daily excess return",
        "availableStrategies": list(returns.columns),
        "availableIncrementalTrials": list(incremental.columns),
        "deflatedSharpe": {
            **dsr,
            "gateStatus": (
                "PASS"
                if dsr_passed
                else "FAIL"
                if dsr.get("status") == "PASS"
                else "NOT_EVALUABLE"
            ),
        },
        "deflatedSharpeRequired": f">= {dsr_min}",
        "pbo": {
            **pbo,
            "gateStatus": (
                "NOT_REQUIRED"
                if not pbo_required
                else "PASS"
                if pbo_passed
                else "FAIL"
                if pbo.get("status") == "PASS"
                else "NOT_EVALUABLE"
            ),
        },
        "pboReturnAliases": pbo_aliases,
        "pboUniqueStrategies": list(pbo_returns.columns),
        "pboRequired": pbo_required,
        "pboThreshold": f"<= {pbo_max}",
    }


def _historical_promotion_checks(
    candidates: dict[str, Any],
    promotion: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, Any] | None, dict[str, Any]]:
    required_candidates = ("B0", "F1", "C1")
    checks = [
        _check(
            "candidate_completion",
            all(candidates.get(item, {}).get("status") == "completed" for item in required_candidates),
            {
                item: candidates.get(item, {}).get("status", "missing")
                for item in required_candidates
            },
            "B0/F1/C1 completed",
            "B0、F1、C1 未全部完成。",
        )
    ]
    if checks[-1]["status"] == "FAIL":
        return checks, None, {
            "status": "NOT_RUN",
            "required": bool(
                (promotion.get("multiple_testing") or {}).get("required", False)
            ),
        }

    baseline_analysis = candidates["B0"]["analysis"]
    combined_analysis = candidates["C1"]["analysis"]
    baseline = baseline_analysis.get("standard") or {}
    combined = combined_analysis.get("standard") or {}
    recent_window = str(int(promotion.get("recent_window_days", 252)))
    recent = combined_analysis.get("recentWindows", {}).get(recent_window) or {}
    latest_year = str(promotion.get("latest_complete_year", "2025"))
    latest = combined_analysis.get("yearly", {}).get(latest_year) or {}
    cost_key = str(float(promotion.get("cost_multiplier", 2.0)))
    stressed = combined_analysis.get("costSensitivity", {}).get(cost_key) or {}

    checks.extend(
        [
            _check(
                "recent_window_positive",
                recent.get("annualizedExcessReturn", -math.inf)
                > float(promotion.get("recent_annualized_excess_min", 0.0)),
                recent.get("annualizedExcessReturn"),
                f"> {promotion.get('recent_annualized_excess_min', 0.0)}",
                f"C1 最近 {recent_window} 个交易日税费后超额收益未为正。",
            ),
            _check(
                "latest_complete_year",
                latest.get("annualizedExcessReturn", -math.inf)
                >= float(promotion.get("latest_year_annualized_excess_min", 0.02)),
                latest.get("annualizedExcessReturn"),
                f">= {promotion.get('latest_year_annualized_excess_min', 0.02)}",
                f"C1 {latest_year} 年税费后超额收益未达到门槛。",
            ),
            _check(
                "information_ratio",
                combined.get("informationRatio", -math.inf)
                >= float(promotion.get("information_ratio_min", 0.5)),
                combined.get("informationRatio"),
                f">= {promotion.get('information_ratio_min', 0.5)}",
                "C1 信息比率未达到门槛。",
            ),
            _check(
                "max_drawdown",
                combined.get("maxDrawdown", -math.inf)
                >= -abs(float(promotion.get("max_drawdown_abs_max", 0.15))),
                combined.get("maxDrawdown"),
                f">= {-abs(float(promotion.get('max_drawdown_abs_max', 0.15)))}",
                "C1 最大回撤超过门槛。",
            ),
            _check(
                "cost_stress",
                stressed.get("annualizedExcessReturn", -math.inf)
                > float(promotion.get("stressed_annualized_excess_min", 0.0)),
                stressed.get("annualizedExcessReturn"),
                f"> {promotion.get('stressed_annualized_excess_min', 0.0)} at {cost_key}x cost",
                f"C1 在 {cost_key}x 成本下税费后超额收益未为正。",
            ),
        ]
    )

    stability_years = [
        str(year)
        for year in promotion.get(
            "stability_years",
            ["2022", "2023", "2024", latest_year],
        )
    ]
    yearly = combined_analysis.get("yearly", {})
    positive_years = sum(
        (yearly.get(year) or {}).get("annualizedExcessReturn", -math.inf) > 0
        for year in stability_years
    )
    required_positive_years = int(promotion.get("positive_years_min", 3))
    checks.append(
        _check(
            "yearly_stability",
            positive_years >= required_positive_years,
            {"positive": positive_years, "years": stability_years},
            f">= {required_positive_years} positive years",
            "C1 完整年度正超额数量不足。",
        )
    )

    latest_months = [
        metrics
        for month, metrics in combined_analysis.get("monthly", {}).items()
        if month.startswith(f"{latest_year}-")
    ]
    positive_month_total = sum(
        max(float(metrics.get("totalExcessReturn") or 0.0), 0.0)
        for metrics in latest_months
    )
    max_month = max(
        (max(float(metrics.get("totalExcessReturn") or 0.0), 0.0) for metrics in latest_months),
        default=0.0,
    )
    month_share = max_month / positive_month_total if positive_month_total > 0 else 1.0
    max_month_share = float(promotion.get("latest_year_max_positive_month_share", 0.8))
    checks.append(
        _check(
            "latest_year_month_concentration",
            bool(latest_months) and month_share <= max_month_share,
            month_share,
            f"<= {max_month_share}",
            f"C1 {latest_year} 年收益过度集中于单月。",
        )
    )

    arr_delta = combined.get("annualizedExcessReturn", -math.inf) - baseline.get(
        "annualizedExcessReturn",
        0.0,
    )
    ir_delta = combined.get("informationRatio", -math.inf) - baseline.get(
        "informationRatio",
        0.0,
    )
    drawdown_delta = combined.get("maxDrawdown", -math.inf) - baseline.get(
        "maxDrawdown",
        0.0,
    )
    arr_improvement = float(promotion.get("annualized_excess_improvement_min", 0.02))
    ir_improvement = float(promotion.get("information_ratio_improvement_min", 0.2))
    checks.extend(
        [
            _check(
                "incremental_value",
                arr_delta >= arr_improvement or ir_delta >= ir_improvement,
                {"annualizedExcessDelta": arr_delta, "informationRatioDelta": ir_delta},
                {
                    "annualizedExcessDelta": f">= {arr_improvement}",
                    "orInformationRatioDelta": f">= {ir_improvement}",
                },
                "C1 相对 B0 未达到实质改善门槛。",
            ),
            _check(
                "non_degradation",
                arr_delta
                >= -abs(float(promotion.get("annualized_excess_degradation_max", 0.01)))
                and ir_delta
                >= -abs(float(promotion.get("information_ratio_degradation_max", 0.1)))
                and drawdown_delta
                >= -abs(float(promotion.get("drawdown_degradation_max", 0.02))),
                {
                    "annualizedExcessDelta": arr_delta,
                    "informationRatioDelta": ir_delta,
                    "maxDrawdownDelta": drawdown_delta,
                },
                "all degradation limits",
                "C1 相对 B0 存在明显退化。",
            ),
        ]
    )

    bootstrap = _incremental_bootstrap(
        candidates["B0"]["dailyPath"],
        candidates["C1"]["dailyPath"],
        iterations=int(promotion.get("bootstrap_iterations", 1000)),
        block_size=int(promotion.get("bootstrap_block_days", 20)),
        seed=int(promotion.get("bootstrap_seed", 42)),
    )
    lower_bound = (bootstrap.get("confidenceInterval90") or [-math.inf])[0]
    checks.append(
        _check(
            "incremental_bootstrap",
            bootstrap.get("status") == "PASS"
            and lower_bound > float(promotion.get("bootstrap_lower_bound_min", 0.0)),
            bootstrap,
            f"90% lower bound > {promotion.get('bootstrap_lower_bound_min', 0.0)}",
            "C1 增量收益的不确定性区间没有可靠地高于零。",
        )
    )
    multiple_testing = _multiple_testing_audit(
        candidates,
        promotion.get("multiple_testing") or {},
    )
    if multiple_testing.get("required", False):
        checks.append(
            {
                "id": "multiple_testing_adjustment",
                "status": multiple_testing["status"],
                "actual": multiple_testing,
                "required": "verified trial count, DSR gate, and required PBO gate",
                "reason": multiple_testing.get("reason", ""),
            }
        )
    return checks, bootstrap, multiple_testing


def _decision(
    run_id: str,
    protocol_version: str,
    data_audit: dict[str, Any],
    factor_audit: dict[str, Any],
    candidates: dict[str, Any],
    promotion: dict[str, Any],
    walk_forward: dict[str, Any],
    audit_only: bool = False,
    historical_evidence_only: bool = False,
) -> dict[str, Any]:
    reasons: list[str] = []
    factor_rejected = factor_audit.get("status") == "REJECTED"
    if data_audit["status"] != "PASS":
        reasons.extend(data_audit.get("blockingReasons", []))
    if factor_audit["status"] not in {"PASS", "REJECTED"}:
        reasons.extend(factor_audit.get("blockingReasons", []))
    if reasons:
        status = "BLOCKED"
        checks: list[dict[str, Any]] = []
        bootstrap = None
        multiple_testing = {"status": "NOT_RUN", "required": False}
    elif factor_rejected:
        status = "REJECTED"
        reasons.extend(factor_audit.get("rejectionReasons", []))
        checks = []
        bootstrap = None
        multiple_testing = {"status": "NOT_RUN", "required": False}
    elif audit_only:
        status = "RESEARCH_ONLY"
        reasons.append("本次仅执行数据与因子审计，未运行候选训练。")
        checks = []
        bootstrap = None
        multiple_testing = {"status": "NOT_RUN", "required": False}
    else:
        checks, bootstrap, multiple_testing = _historical_promotion_checks(
            candidates,
            promotion,
        )
        failed = [check["reason"] for check in checks if check["status"] == "FAIL"]
        insufficient = [
            check["reason"]
            for check in checks
            if check["status"] == "INSUFFICIENT"
        ]
        completion_failed = any(
            check["id"] == "candidate_completion" and check["status"] == "FAIL"
            for check in checks
        )
        technical_blocked = [
            check.get("reason", "")
            for check in checks
            if check.get("status") == "BLOCKED"
        ]
        if completion_failed:
            status = "BLOCKED"
            reasons.extend(failed)
            reasons.extend(technical_blocked)
        elif failed:
            status = "REJECTED"
            reasons.extend(failed)
            reasons.extend(technical_blocked)
        elif technical_blocked:
            status = "BLOCKED"
            reasons.extend(technical_blocked)
        elif insufficient:
            status = "RESEARCH_ONLY"
            reasons.extend(insufficient)
        elif walk_forward.get("required", True) and walk_forward.get("status") != "PASS":
            status = "RESEARCH_ONLY"
            reasons.append("盈利导向季度走步验证尚未通过。")
        else:
            status = "PAPER_CANDIDATE"

    if historical_evidence_only and status == "PAPER_CANDIDATE":
        status = "RESEARCH_ONLY"
        reasons.append("该协议复用了已查看的历史区间，只能作为研究证据。")

    return {
        "runId": run_id,
        "protocolVersion": protocol_version,
        "status": status,
        "reasons": list(dict.fromkeys(reasons)),
        "paperEligible": status == "PAPER_CANDIDATE",
        "historicalGateChecks": checks,
        "incrementalBootstrap": bootstrap,
        "multipleTesting": multiple_testing,
        "walkForward": walk_forward,
        "createdAt": datetime.now(timezone.utc).isoformat(),
    }


def _render_report(
    run_id: str,
    data_audit: dict[str, Any],
    factor_audit: dict[str, Any],
    candidate_results: dict[str, Any],
    decision: dict[str, Any],
) -> str:
    lines = [
        f"# 收益验证结果：{run_id}",
        "",
        f"- 决策：`{decision['status']}`",
        f"- 数据审计：`{data_audit['status']}`",
        f"- 因子审计：`{factor_audit['status']}`",
        f"- 可进入模拟盘：`{str(decision['paperEligible']).lower()}`",
        "",
        "## 阻塞原因",
        "",
    ]
    lines.extend(f"- {reason}" for reason in decision["reasons"])
    lines.extend(["", "## 入选因子", ""])
    if factor_audit.get("selectedFactorIds"):
        lines.extend(f"- `{factor_id}`" for factor_id in factor_audit["selectedFactorIds"])
    else:
        lines.append("- 无")
    lines.extend(
        [
            "",
            "## 候选历史诊断",
            "",
            "| 候选 | 年化税费后超额 | IR | 最大回撤 | 2025 年化超额 |",
            "| --- | ---: | ---: | ---: | ---: |",
        ]
    )
    candidate_ids = ["B0", "F1", "C1"]
    if "A0" in candidate_results:
        candidate_ids.append("A0")
    for candidate_id in candidate_ids:
        result = candidate_results.get(candidate_id, {})
        standard = result.get("analysis", {}).get("standard", {})
        year_2025 = result.get("analysis", {}).get("yearly", {}).get("2025", {})
        if not standard:
            lines.append(f"| {candidate_id} | 未完成 | - | - | - |")
            continue
        lines.append(
            "| {id} | {arr:.2%} | {ir:.3f} | {mdd:.2%} | {y:.2%} |".format(
                id=candidate_id,
                arr=standard.get("annualizedExcessReturn", 0),
                ir=standard.get("informationRatio", 0),
                mdd=standard.get("maxDrawdown", 0),
                y=year_2025.get("annualizedExcessReturn", 0),
            )
        )
    multiple_testing = decision.get("multipleTesting") or {}
    lines.extend(
        [
            "",
            "## 多重试验审计",
            "",
            f"- 状态：`{multiple_testing.get('status', 'NOT_RUN')}`",
            f"- DSR：`{(multiple_testing.get('deflatedSharpe') or {}).get('probability', 'N/A')}`",
            f"- PBO：`{(multiple_testing.get('pbo') or {}).get('probability', 'N/A')}`",
        ]
    )
    lines.extend(
        [
            "",
            "该报告是已冻结协议下的诊断结果，不是收益承诺。只有 `PAPER_CANDIDATE` 或 `PAPER_VALIDATED` 状态可以生成模型模拟盘信号。",
            "",
        ]
    )
    return "\n".join(lines)


def _configured_reference_factor_expressions(
    base_config: dict[str, Any],
    validation: dict[str, Any],
) -> dict[str, str]:
    references: dict[str, str] = {}
    factor_source = base_config.get("factor_source") or {}
    official_source = (
        (factor_source.get("combined") or {}).get("official_source")
        or factor_source.get("type")
    )
    if official_source == "alpha158_20":
        references.update(FactorLoader.ALPHA158_20_FACTORS)

    configured = validation.get("reference_factor_expressions") or {}
    if not isinstance(configured, dict):
        raise ValueError("validation.reference_factor_expressions must be a mapping")
    for raw_name, raw_expression in configured.items():
        name = str(raw_name).strip()
        expression = str(raw_expression).strip()
        if not name or not expression:
            raise ValueError("reference factor names and expressions must be non-empty")
        if name in references and references[name] != expression:
            raise ValueError(f"reference factor conflicts with official source: {name}")
        references[name] = expression
    return references


def _deterministic_process_contract() -> dict[str, str]:
    hash_seed = os.environ.get("PYTHONHASHSEED")
    if hash_seed != "0":
        raise RuntimeError(
            "profit validation requires PYTHONHASHSEED=0; "
            "use scripts/run_profit_validation.py"
        )
    return {
        "pythonHashSeed": hash_seed,
        "strategySignalOrder": "score_desc_instrument_asc",
    }


def _protocol_at_least(value: str, minimum: tuple[int, int]) -> bool:
    try:
        parts = tuple(int(item) for item in str(value).split(".")[:2])
    except ValueError:
        return False
    return (parts + (0, 0))[:2] >= minimum


def run_profit_validation(
    project_root: Path,
    config_path: Path,
    audit_only: bool = False,
) -> dict[str, Any]:
    process_contract = _deterministic_process_contract()
    project_root = project_root.resolve()
    config_path = config_path.resolve()
    config = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    protocol_version = str(config.get("protocol_version", "1.1"))
    run_id = config.get("run_id") or datetime.now().strftime("profit_%Y%m%d_%H%M%S")
    output_root = _resolve(project_root, config["output_root"])
    run_dir = output_root / run_id
    if run_dir.exists():
        raise FileExistsError(f"validation run already exists: {run_dir}")
    run_dir.mkdir(parents=True)

    historical_provider = _resolve(project_root, config["historical_provider"])
    recent_provider = _resolve(project_root, config["recent_provider"])
    factor_library = _resolve(project_root, config["factor_library"])
    backtest_config_path = _resolve(project_root, config["backtest_config"])
    validation = config.get("validation") or {}
    base_config = yaml.safe_load(backtest_config_path.read_text(encoding="utf-8")) or {}
    historical_contract = provider_contract_fingerprint(
        historical_provider,
        validation.get("market", "csi300"),
    )
    recent_contract = provider_contract_fingerprint(
        recent_provider,
        validation.get("market", "csi300"),
    )

    factor_payload = json.loads(factor_library.read_text(encoding="utf-8"))
    promotion_config = copy.deepcopy(config.get("promotion") or {})
    multiple_testing_settings = promotion_config.setdefault("multiple_testing", {})
    trial_matrix_settings = copy.deepcopy(config.get("trial_matrix") or {})
    trial_matrix_enabled = bool(trial_matrix_settings.get("enabled", False))
    trial_count_path = None
    historical_trial_audit_path = None
    historical_trial_evidence: dict[str, Any] | None = None
    historical_trial_reveal_allowed = True
    multiple_testing_enabled = bool(
        multiple_testing_settings.get("enabled")
        or multiple_testing_settings.get("required")
    )
    historical_trial_audit_required = multiple_testing_enabled and _protocol_at_least(
        protocol_version,
        (2, 9),
    )
    if multiple_testing_enabled and (
        multiple_testing_settings.get("historical_trial_audit")
        or historical_trial_audit_required
    ):
        historical_trial_evidence, historical_trial_audit_path = (
            _verify_historical_trial_audit(
                project_root,
                multiple_testing_settings,
                validation.get("admission_factor_ids") or [],
            )
        )
        multiple_testing_settings["historicalTrialEvidence"] = (
            historical_trial_evidence
        )
        historical_trial_reveal_allowed = (
            historical_trial_evidence.get("status") == "PASS"
        )
    if (
        not trial_matrix_enabled
        and multiple_testing_enabled
    ):
        trial_count_evidence, trial_count_path = _verify_trial_count_evidence(
            project_root,
            factor_payload,
            multiple_testing_settings,
        )
        multiple_testing_settings["trialCountEvidence"] = trial_count_evidence

    input_files = [
        config_path,
        backtest_config_path,
        factor_library,
        project_root / "quantaalpha" / "validation" / "profit.py",
        project_root / "quantaalpha" / "backtest" / "factor_loader.py",
        project_root / "quantaalpha" / "backtest" / "custom_factor_calculator.py",
        project_root / "quantaalpha" / "backtest" / "runner.py",
        project_root / "quantaalpha" / "factors" / "coder" / "expr_parser.py",
        project_root / "quantaalpha" / "factors" / "coder" / "function_lib.py",
        project_root / "scripts" / "run_profit_validation.py",
    ]
    if trial_count_path and trial_count_path.is_file():
        input_files.append(trial_count_path)
    if historical_trial_audit_path and historical_trial_audit_path.is_file():
        input_files.append(historical_trial_audit_path)
    preparation_audit = factor_library.with_suffix(".audit.json")
    if preparation_audit.is_file():
        input_files.append(preparation_audit)
    selection_evidence = config.get("selection_evidence")
    if selection_evidence:
        selection_path = _resolve(project_root, selection_evidence)
        if selection_path.is_file():
            input_files.append(selection_path)
    source_library = (factor_payload.get("metadata") or {}).get("source_library")
    if source_library:
        source_path = _resolve(project_root, source_library)
        if source_path.is_file():
            input_files.append(source_path)
            try:
                source_payload = json.loads(source_path.read_text(encoding="utf-8"))
                upstream = (source_payload.get("metadata") or {}).get("source_library")
                if upstream:
                    upstream_path = _resolve(project_root, upstream)
                    if upstream_path.is_file():
                        input_files.append(upstream_path)
            except json.JSONDecodeError:
                pass
    semantic_evidence = (factor_payload.get("metadata") or {}).get(
        "semantic_evidence"
    )
    if semantic_evidence and semantic_evidence != "configured_llm":
        evidence_path = _resolve(project_root, semantic_evidence)
        if evidence_path.exists():
            input_files.append(evidence_path)
    if historical_trial_reveal_allowed:
        for factor_info in (factor_payload.get("factors") or {}).values():
            cache_path_value = (factor_info.get("cache_location") or {}).get(
                "result_h5_path"
            )
            if cache_path_value:
                cache_path = _resolve(project_root, cache_path_value)
                if cache_path.exists():
                    input_files.append(cache_path)
    input_files = list(dict.fromkeys(path.resolve() for path in input_files))
    manifest = {
        "runId": run_id,
        "protocolVersion": protocol_version,
        "createdAt": datetime.now(timezone.utc).isoformat(),
        "projectRoot": str(project_root),
        "inputs": {
            _manifest_key(path, project_root): {
                "path": str(path),
                "sha256": _sha256(path),
            }
            for path in input_files
        },
        "providers": {
            "historical": historical_contract,
            "recent": recent_contract,
        },
        "auditOnly": audit_only,
        "processContract": process_contract,
    }
    _write_json(run_dir / "manifest.json", manifest)

    data_audit = audit_data_providers(
        historical_provider,
        recent_provider,
        validation.get("required_start", "2016-01-01"),
        validation.get("required_end", "2025-12-26"),
        validation.get("market", "csi300"),
        int(validation.get("data_sample_size", 20)),
    )
    configured_provider = _resolve(
        project_root,
        (base_config.get("data") or {}).get("provider_uri", ""),
    )
    configuration_checks = [
        {
            "id": "backtest_market_binding",
            "status": "PASS"
            if (base_config.get("data") or {}).get("market")
            == validation.get("market", "csi300")
            else "FAIL",
            "details": {
                "validation": validation.get("market", "csi300"),
                "backtest": (base_config.get("data") or {}).get("market"),
            },
        },
        {
            "id": "backtest_label_binding",
            "status": "PASS"
            if (base_config.get("dataset") or {}).get("label")
            == validation.get("label", "Ref($close, -2) / Ref($close, -1) - 1")
            else "FAIL",
            "details": {
                "validation": validation.get(
                    "label", "Ref($close, -2) / Ref($close, -1) - 1"
                ),
                "backtest": (base_config.get("dataset") or {}).get("label"),
            },
        },
        {
            "id": "backtest_provider_binding",
            "status": "PASS" if configured_provider == historical_provider else "FAIL",
            "details": {
                "validation": str(historical_provider),
                "backtest": str(configured_provider),
            },
        },
    ]
    data_audit["configurationChecks"] = configuration_checks
    data_audit["checks"].extend(configuration_checks)
    if any(check["status"] == "FAIL" for check in configuration_checks):
        data_audit["status"] = "FAIL"
        data_audit["blockingReasons"].append(
            "收益验证配置与回测配置的数据市场、标签或 Provider 不一致。"
        )
    segment_embargo = audit_segment_embargo(
        historical_provider,
        (base_config.get("dataset") or {}).get("segments") or {},
        int(validation.get("label_horizon_trading_days", 2)),
    )
    data_audit["segmentEmbargo"] = segment_embargo
    data_audit["checks"].append(
        {
            "id": "dataset_segment_embargo",
            "status": segment_embargo["status"],
            "details": segment_embargo,
        }
    )
    if segment_embargo["status"] != "PASS":
        data_audit["status"] = "FAIL"
        data_audit["blockingReasons"].append(
            "数据集分段未给未来标签保留足够的交易日隔离。"
        )
    _write_json(run_dir / "data_audit.json", data_audit)

    if historical_trial_reveal_allowed:
        factor_audit, selected_library = audit_factor_library(
            factor_library,
            historical_provider,
            validation.get("market", "csi300"),
            validation.get("label", "Ref($close, -2) / Ref($close, -1) - 1"),
            validation.get("validation_start", "2021-01-01"),
            validation.get("validation_end", "2021-12-31"),
            float(validation.get("correlation_threshold", 0.7)),
            float(validation.get("factor_coverage_min", 0.5)),
            float(validation.get("rank_ic_min", 0.0)),
            validation.get("required_start"),
            validation.get("required_end"),
            validation.get("admission_factor_ids"),
            _configured_reference_factor_expressions(base_config, validation),
            validation.get("reference_cached_factor_ids"),
        )
    else:
        reason = (
            (historical_trial_evidence or {}).get("reason")
            or "历史试验审计未通过，未读取候选缓存或计算 RankIC。"
        )
        factor_audit = {
            "status": "BLOCKED",
            "blockingReasons": [reason],
            "admissionFactorIds": [
                str(item)
                for item in validation.get("admission_factor_ids") or []
            ],
            "selectedFactorIds": [],
            "factors": [],
            "historicalTrialEvidence": historical_trial_evidence or {},
        }
        selected_library = {
            "metadata": {
                "status": "BLOCKED",
                "reason": reason,
            },
            "factors": {},
        }
    _write_json(run_dir / "factor_audit.json", factor_audit)
    selected_library_path = run_dir / "selected_factors.json"
    _write_json(selected_library_path, selected_library)
    audit_library_path = run_dir / "audit_all_factors.json"
    if historical_trial_reveal_allowed:
        audit_library_path.write_bytes(factor_library.read_bytes())

    trial_plan: dict[str, Any] | None = None
    target_library_path = selected_library_path
    if (
        trial_matrix_enabled
        and data_audit["status"] == "PASS"
        and factor_audit["status"] == "PASS"
    ):
        try:
            trial_plan, target_library_path = build_frozen_trial_plan(
                factor_library,
                factor_payload,
                factor_audit,
                historical_provider,
                historical_contract["fingerprint"],
                validation.get("market", "csi300"),
                validation.get("required_start", "2016-01-01"),
                validation.get("required_end", "2025-12-26"),
                backtest_config_path,
                run_dir,
                trial_matrix_settings,
            )
            trial_plan_path = run_dir / "trial_plan.json"
            _write_json(trial_plan_path, trial_plan)
            manifest["trialPlan"] = {
                "path": str(trial_plan_path.resolve()),
                "sha256": _sha256(trial_plan_path),
                "targetTrialId": trial_plan["targetTrialId"],
                "expectedTrialCount": trial_plan["expectedTrialCount"],
            }
            for frozen_path in [
                trial_plan_path,
                *[
                    Path(trial["factorLibraryPath"])
                    for trial in trial_plan["trials"]
                ],
            ]:
                manifest["inputs"][_manifest_key(frozen_path, project_root)] = {
                    "path": str(frozen_path.resolve()),
                    "sha256": _sha256(frozen_path),
                }
            _write_json(run_dir / "manifest.json", manifest)
        except Exception as exc:
            factor_audit["status"] = "FAIL"
            factor_audit.setdefault("blockingReasons", []).append(
                f"冻结同质试验计划失败：{exc}"
            )
            factor_audit["trialPlan"] = {"status": "FAIL", "reason": str(exc)}
            _write_json(run_dir / "factor_audit.json", factor_audit)

    candidate_results: dict[str, Any] = {}
    trials: list[dict[str, Any]] = []
    audits_passed = data_audit["status"] == "PASS" and factor_audit["status"] == "PASS"
    should_run_candidates = (
        historical_trial_reveal_allowed
        and
        not audit_only
        and (audits_passed or not bool(config.get("stop_on_audit_failure", False)))
    )
    if should_run_candidates:
        candidates: list[dict[str, Any]] = [
            {
                "candidate_id": "B0",
                "factor_source": "alpha158_20",
                "factor_library": None,
                "decision_filter": "accepted",
                "factor_id": "",
                "factor_name": "Alpha158_20",
                "expected_factor_count": None,
                "strict_custom": False,
            }
        ]
        if trial_plan:
            candidates.extend(
                {
                    "candidate_id": item["trialId"],
                    "factor_source": "combined",
                    "factor_library": Path(item["factorLibraryPath"]),
                    "decision_filter": "all",
                    "factor_id": item["factorId"],
                    "factor_name": item["factorName"],
                    "expected_factor_count": 1,
                    "strict_custom": True,
                }
                for item in trial_plan["trials"]
            )
            candidates.append(
                {
                    "candidate_id": "F1",
                    "factor_source": "custom",
                    "factor_library": target_library_path,
                    "decision_filter": "all",
                    "factor_id": trial_plan["targetFactorId"],
                    "factor_name": "Target factor only",
                    "expected_factor_count": 1,
                    "strict_custom": True,
                }
            )
        else:
            candidates.extend(
                [
                    {
                        "candidate_id": "F1",
                        "factor_source": "custom",
                        "factor_library": selected_library_path,
                        "decision_filter": "accepted",
                        "factor_id": "",
                        "factor_name": "Selected custom pool",
                        "expected_factor_count": None,
                        "strict_custom": False,
                    },
                    {
                        "candidate_id": "C1",
                        "factor_source": "combined",
                        "factor_library": selected_library_path,
                        "decision_filter": "accepted",
                        "factor_id": "",
                        "factor_name": "Selected combined pool",
                        "expected_factor_count": None,
                        "strict_custom": False,
                    },
                ]
            )
            if bool(config.get("include_audit_candidate", True)):
                candidates.append(
                    {
                        "candidate_id": "A0",
                        "factor_source": "combined",
                        "factor_library": audit_library_path,
                        "decision_filter": "all",
                        "factor_id": "",
                        "factor_name": "Audit pool",
                        "expected_factor_count": None,
                        "strict_custom": False,
                    }
                )
        provider_fingerprint = historical_contract["fingerprint"]
        for candidate in candidates:
            candidate_id = candidate["candidate_id"]
            factor_source = candidate["factor_source"]
            candidate_library = candidate["factor_library"]
            decision_filter = candidate["decision_filter"]
            started_at = datetime.now(timezone.utc).isoformat()
            candidate_dir = run_dir / candidate_id
            candidate_dir.mkdir()
            candidate_config = _candidate_config(
                base_config,
                candidate_id,
                factor_source,
                candidate_library,
                candidate_dir,
                historical_provider,
                provider_fingerprint,
                decision_filter,
                expected_factor_count=candidate["expected_factor_count"],
                expected_factor_ids=(
                    [candidate["factor_id"]]
                    if candidate["strict_custom"] and candidate["factor_id"]
                    else None
                ),
                strict_custom=candidate["strict_custom"],
            )
            candidate_config_path = candidate_dir / "config.yaml"
            candidate_config_path.write_text(
                yaml.safe_dump(candidate_config, sort_keys=False, allow_unicode=True),
                encoding="utf-8",
            )
            trial = {
                "candidate": candidate_id,
                "factor_id": candidate["factor_id"],
                "factor_name": candidate["factor_name"],
                "factor_source": factor_source,
                "alias_of": "",
                "status": "running",
                "started_at": started_at,
                "finished_at": "",
                "metrics_path": "",
                "error": "",
            }
            try:
                runner = BacktestRunner(str(candidate_config_path))
                metrics = runner.run(
                    factor_source=factor_source,
                    factor_json=[str(candidate_library)] if candidate_library else None,
                    experiment_name=f"profit_validation_{run_id}_{candidate_id}",
                    output_name=candidate_id.lower(),
                    skip_uncached=bool(config.get("skip_uncached", True)),
                )
                metrics_path = candidate_dir / f"{candidate_id.lower()}_backtest_metrics.json"
                daily_path = candidate_dir / f"{candidate_id.lower()}_cumulative_excess.csv"
                daily = pd.read_csv(daily_path)
                analysis = analyze_daily_returns(
                    daily,
                    config.get("cost_multipliers", [1.0, 1.5, 2.0]),
                )
                rolling = rolling_return_metrics(daily)
                rolling.to_csv(candidate_dir / "rolling_metrics.csv", index=False)
                candidate_result = {
                    "status": "completed",
                    "factorId": candidate["factor_id"] or None,
                    "factorName": candidate["factor_name"],
                    "metrics": metrics,
                    "analysis": analysis,
                    "metricsPath": str(metrics_path),
                    "dailyPath": str(daily_path),
                    "configPath": str(candidate_config_path),
                    "configSha256": _sha256(candidate_config_path),
                }
                candidate_results[candidate_id] = candidate_result
                _write_json(candidate_dir / "analysis.json", candidate_result)
                trial["status"] = "completed"
                trial["metrics_path"] = str(metrics_path)
            except Exception as exc:
                trial["status"] = "failed"
                trial["error"] = str(exc)
                candidate_results[candidate_id] = {
                    "status": "failed",
                    "error": str(exc),
                }
            finally:
                trial["finished_at"] = datetime.now(timezone.utc).isoformat()
                trials.append(trial)
                _write_trial_ledger(run_dir / "trial_ledger.csv", trials)
                gc.collect()

        if trial_plan:
            alias_id = trial_plan["c1AliasOf"]
            alias_source = candidate_results.get(alias_id) or {}
            alias_completed = alias_source.get("status") == "completed"
            candidate_results["C1"] = {
                **copy.deepcopy(alias_source),
                "status": "completed" if alias_completed else "failed",
                "aliasOf": alias_id,
                "factorId": trial_plan["targetFactorId"],
            }
            trials.append(
                {
                    "candidate": "C1",
                    "factor_id": trial_plan["targetFactorId"],
                    "factor_name": "Target combined strategy alias",
                    "factor_source": "combined",
                    "alias_of": alias_id,
                    "status": "completed" if alias_completed else "failed",
                    "started_at": "",
                    "finished_at": datetime.now(timezone.utc).isoformat(),
                    "metrics_path": alias_source.get("metricsPath", ""),
                    "error": "" if alias_completed else "target trial is incomplete",
                }
            )
            _write_trial_ledger(run_dir / "trial_ledger.csv", trials)
            try:
                _, trial_matrix_manifest = build_strict_trial_matrix(
                    candidate_results,
                    trial_plan,
                    run_dir,
                )
            except Exception as exc:
                trial_matrix_manifest = {
                    "status": "BLOCKED",
                    "reason": str(exc),
                    "expectedTrialIds": [
                        item["trialId"] for item in trial_plan["trials"]
                    ],
                    "targetTrialId": trial_plan["targetTrialId"],
                }
                _write_json(
                    run_dir / "trial_matrix_manifest.json",
                    trial_matrix_manifest,
                )
            multiple_testing_settings["trialMatrix"] = trial_matrix_manifest
    else:
        _write_trial_ledger(run_dir / "trial_ledger.csv", trials)

    walk_forward_config = config.get("walk_forward") or {}
    walk_forward = {
        "status": "NOT_RUN",
        "required": bool(walk_forward_config.get("required", True)),
    }
    result_path_value = walk_forward_config.get("result_path")
    if result_path_value:
        result_path = _resolve(project_root, result_path_value)
        if result_path.exists():
            loaded_walk_forward = json.loads(result_path.read_text(encoding="utf-8"))
            expected_binding = {
                "protocolVersion": protocol_version,
                "providerFingerprint": historical_contract["fingerprint"],
                "factorLibrarySha256": _sha256(factor_library),
                "candidate": "C1",
            }
            if trial_plan:
                trial_plan_path = run_dir / "trial_plan.json"
                expected_binding.update(
                    {
                        "targetFactorId": trial_plan["targetFactorId"],
                        "targetTrialId": trial_plan["targetTrialId"],
                        "trialPlanSha256": _sha256(trial_plan_path),
                    }
                )
            binding_matches = all(
                loaded_walk_forward.get(key) == value
                for key, value in expected_binding.items()
            )
            walk_forward = {
                **loaded_walk_forward,
                "status": loaded_walk_forward.get("status")
                if binding_matches
                else "INVALID",
                "required": bool(walk_forward_config.get("required", True)),
                "bindingMatches": binding_matches,
                "expectedBinding": expected_binding,
                "resultPath": str(result_path),
            }

    decision = _decision(
        run_id,
        protocol_version,
        data_audit,
        factor_audit,
        candidate_results,
        promotion_config,
        walk_forward,
        audit_only=audit_only,
        historical_evidence_only=bool(
            trial_matrix_settings.get("historical_evidence_only", False)
        ),
    )
    _write_json(run_dir / "candidate_metrics.json", candidate_results)
    _write_json(run_dir / "decision.json", decision)
    (run_dir / "report.md").write_text(
        _render_report(run_id, data_audit, factor_audit, candidate_results, decision),
        encoding="utf-8",
    )
    return {
        "runId": run_id,
        "runDir": str(run_dir),
        "decision": decision,
        "dataAudit": data_audit["status"],
        "factorAudit": factor_audit["status"],
        "candidates": candidate_results,
    }
