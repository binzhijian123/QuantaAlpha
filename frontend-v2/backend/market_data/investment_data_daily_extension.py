from __future__ import annotations

import hashlib
import json
import math
import re
import shutil
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from quantaalpha.utils.qlib_provider import provider_contract_fingerprint

from .paths import manifest_path, raw_provider_dir, report_path
from .providers.a_stock_data import AStockDataClient, normalize_stock_code, to_qlib_code


RAW_PROVIDER_NAME = "a_stock_data_investment_daily"
EXTENSION_MANIFEST = "quantaalpha_extension.manifest.json"
FIELDS = (
    "open",
    "high",
    "low",
    "close",
    "volume",
    "vwap",
    "amount",
    "factor",
    "change",
    "adjclose",
)
PRICE_FIELDS = ("open", "high", "low", "close")
DEFAULT_BENCHMARK = "000300.SH"
EXPECTED_CSI300_SIZE = 300

AUDIT_THRESHOLDS = {
    "minimumCommonSessions": 20,
    "minimumAdjustmentRegimeSessions": 3,
    "minimumDirectAdjustmentRegimeSessionsWithForwardEvidence": 2,
    "minimumOverlapCoverage": 0.95,
    "qfqReplayMedianRelativeError": 0.002,
    "qfqReplayP95RelativeError": 0.006,
    "qfqReplayMaximumRelativeError": 0.0125,
    "factorMaximumRelativeError": 2e-6,
    "volumeMaximumRelativeError": 1e-4,
    "amountMaximumRelativeError": 2e-6,
    "vwapMaximumRelativeError": 2e-6,
    "adjcloseRatioMaximumRelativeError": 2e-6,
    "changeMaximumAbsoluteError": 2e-6,
    "dayIdentityMaximumRelativeError": 2e-6,
    "carriedAdjustmentMaximumRelativeSpread": 2e-6,
}


@dataclass(frozen=True)
class InvestmentDataDailyExtensionRequest:
    base_provider: Path
    end_date: str
    output_provider: Path | None = None
    market: str = "csi300"
    benchmark: str = DEFAULT_BENCHMARK
    batch_id: str | None = None
    overlap_sessions: int = 40
    request_interval_seconds: float = 0.02
    snapshot_rechecks: int = 2
    require_closed_session: bool = True
    forward_adjustment_evidence_dates: tuple[str, ...] = ()
    source_snapshot_dir: Path | None = None


def _adjustment_evidence_policy(
    anchor_date: str,
    forward_dates: list[str],
) -> dict[str, Any]:
    allow_forward = bool(forward_dates)
    return {
        "algorithmVersion": "tencent-forward-adjustment-continuity-v1",
        "mode": (
            "base_overlap_plus_forward_source_continuity"
            if allow_forward
            else "base_overlap_only"
        ),
        "forwardEvidenceApproved": allow_forward,
        "minimumDirectBaseSessions": (
            AUDIT_THRESHOLDS["minimumDirectAdjustmentRegimeSessionsWithForwardEvidence"]
            if allow_forward
            else AUDIT_THRESHOLDS["minimumAdjustmentRegimeSessions"]
        ),
        "minimumEffectiveSessions": AUDIT_THRESHOLDS[
            "minimumAdjustmentRegimeSessions"
        ],
        "minimumForwardSessions": 2 if allow_forward else 0,
        "anchorDate": anchor_date,
        "forwardDates": list(forward_dates) if allow_forward else [],
        "continuityMetric": "tencent_qfq_raw_ohlc_ratio_relative_to_base_anchor",
        "evidenceType": "tencent_forward_continuity" if allow_forward else None,
        "maximumRelativeSpread": AUDIT_THRESHOLDS[
            "carriedAdjustmentMaximumRelativeSpread"
        ],
        "doesNotReplaceBaseReplayErrorChecks": True,
        "doesNotIncreaseCommonSessions": True,
    }


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _json_bytes(payload: Any) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    temporary.replace(path)


def _canonical_code(code: str) -> str:
    digits, exchange, _ = normalize_stock_code(code)
    return f"{digits}.{exchange}"


def _qlib_instrument(code: str) -> str:
    digits, exchange, _ = normalize_stock_code(code)
    return f"{exchange}{digits}"


def _read_calendar(provider: Path, name: str = "day.txt") -> list[str]:
    path = provider / "calendars" / name
    if not path.exists():
        raise FileNotFoundError(f"provider calendar is missing: {path}")
    dates = [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if not dates or dates != sorted(set(dates)):
        raise ValueError(f"provider calendar is empty, duplicated, or unsorted: {path}")
    return dates


def load_frozen_universe(provider: Path, market: str, as_of: str) -> list[str]:
    path = provider / "instruments" / f"{market}.txt"
    if not path.exists():
        raise FileNotFoundError(f"market instrument file is missing: {path}")
    codes = []
    for line in path.read_text(encoding="utf-8").splitlines():
        parts = line.split("\t")
        if len(parts) < 3 or not (parts[1] <= as_of <= parts[2]):
            continue
        codes.append(_canonical_code(parts[0]))
    unique = sorted(set(codes))
    if len(unique) != len(codes):
        raise ValueError(f"{market} contains duplicate active instruments on {as_of}")
    if market.lower() == "csi300" and len(unique) != EXPECTED_CSI300_SIZE:
        raise ValueError(
            f"csi300 must contain {EXPECTED_CSI300_SIZE} instruments on {as_of}, got {len(unique)}"
        )
    return unique


def _read_field(
    provider: Path,
    code: str,
    field: str,
    calendar_index: dict[str, int],
    dates: list[str],
) -> np.ndarray:
    path = provider / "features" / to_qlib_code(code) / f"{field}.day.bin"
    if not path.exists():
        raise FileNotFoundError(f"base feature is missing: {path}")
    binary = np.fromfile(path, dtype="<f")
    if len(binary) < 2:
        raise ValueError(f"base feature is empty: {path}")
    start_index = int(binary[0])
    values = binary[1:]
    result = np.full(len(dates), np.nan, dtype="float64")
    for offset, date in enumerate(dates):
        index = calendar_index[date] - start_index
        if 0 <= index < len(values):
            result[offset] = float(values[index])
    return result


def _load_base_frame(
    provider: Path,
    code: str,
    calendar: list[str],
    dates: list[str],
) -> pd.DataFrame:
    index = {date: position for position, date in enumerate(calendar)}
    data: dict[str, Any] = {"date": dates}
    for field in FIELDS:
        data[field] = _read_field(provider, code, field, index, dates)
    return pd.DataFrame(data)


def _action_payload(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {"unparsed": value}
        return parsed if isinstance(parsed, dict) else {"value": parsed}
    return {}


def _prepare_source_frame(frame: pd.DataFrame, name: str) -> pd.DataFrame:
    if frame.empty:
        raise ValueError(f"{name} source frame is empty")
    required = {"date", "open", "high", "low", "close"}
    missing = sorted(required - set(frame.columns))
    if missing:
        raise ValueError(f"{name} source frame is missing fields: {missing}")
    work = frame.copy()
    work["date"] = pd.to_datetime(work["date"], errors="coerce").dt.strftime("%Y-%m-%d")
    if work["date"].isna().any():
        raise ValueError(f"{name} source frame contains invalid dates")
    for field in ("open", "high", "low", "close", "volume", "amount", "source_volume"):
        if field in work:
            work[field] = pd.to_numeric(work[field], errors="coerce")
    if "corporate_action" not in work:
        work["corporate_action"] = [{} for _ in range(len(work))]
    work["corporate_action"] = work["corporate_action"].map(_action_payload)
    duplicate_dates = work.loc[work.duplicated("date", keep=False), "date"].unique()
    for date in duplicate_dates:
        group = work[work["date"] == date]
        normalized = {
            _json_bytes(
                {
                    key: (_action_payload(value) if key == "corporate_action" else value)
                    for key, value in row.items()
                    if key not in {"source"}
                }
            )
            for row in group.to_dict("records")
        }
        if len(normalized) > 1:
            raise ValueError(f"{name} contains conflicting duplicate rows for {date}")
    return work.drop_duplicates("date", keep="last").sort_values("date").reset_index(drop=True)


def _relative_errors(actual: pd.Series | np.ndarray, predicted: pd.Series | np.ndarray) -> np.ndarray:
    actual_values = np.asarray(actual, dtype="float64")
    predicted_values = np.asarray(predicted, dtype="float64")
    valid = np.isfinite(actual_values) & np.isfinite(predicted_values)
    if not valid.any():
        return np.asarray([], dtype="float64")
    denominator = np.maximum(np.abs(actual_values[valid]), 1e-12)
    return np.abs(predicted_values[valid] - actual_values[valid]) / denominator


def _metric_summary(values: np.ndarray) -> dict[str, Any]:
    finite = np.asarray(values, dtype="float64")
    finite = finite[np.isfinite(finite)]
    if not len(finite):
        return {"count": 0, "median": None, "p95": None, "maximum": None}
    return {
        "count": int(len(finite)),
        "median": float(np.median(finite)),
        "p95": float(np.quantile(finite, 0.95)),
        "maximum": float(np.max(finite)),
    }


def _maximum(summary: dict[str, Any]) -> float:
    value = summary.get("maximum")
    return float(value) if value is not None else math.inf


def _summary_value(summary: dict[str, Any], key: str) -> float:
    value = summary.get(key)
    return float(value) if value is not None else math.inf


def _forward_adjustment_evidence(
    raw: pd.DataFrame,
    adjusted: pd.DataFrame,
    anchor_date: str,
    forward_dates: list[str],
) -> dict[str, Any]:
    requested_dates = [
        anchor_date,
        *sorted({date for date in forward_dates if date > anchor_date}),
    ]
    raw_by_date = raw.set_index("date")
    adjusted_by_date = adjusted.set_index("date")
    missing_dates = [
        date
        for date in requested_dates
        if date not in raw_by_date.index or date not in adjusted_by_date.index
    ]
    action_dates = [
        date
        for date in requested_dates
        if date in raw_by_date.index
        and bool(_action_payload(raw_by_date.loc[date, "corporate_action"]))
    ]
    synthetic_dates = [
        date
        for date in requested_dates
        if date in adjusted_by_date.index
        and "synthetic" in adjusted_by_date.columns
        and bool(adjusted_by_date.loc[date, "synthetic"])
    ]
    ratio_errors: list[float] = []
    unstable_dates: list[str] = []
    stable_dates: list[str] = []
    per_date_maximum: dict[str, float] = {}
    if anchor_date in raw_by_date.index and anchor_date in adjusted_by_date.index:
        anchor_ratios: dict[str, float] = {}
        for field in PRICE_FIELDS:
            raw_value = float(raw_by_date.loc[anchor_date, field])
            adjusted_value = float(adjusted_by_date.loc[anchor_date, field])
            if not math.isfinite(raw_value) or raw_value <= 0 or not math.isfinite(adjusted_value):
                anchor_ratios = {}
                break
            anchor_ratios[field] = adjusted_value / raw_value
        if anchor_ratios and all(value > 0 and math.isfinite(value) for value in anchor_ratios.values()):
            for date in requested_dates:
                if date in missing_dates or date in action_dates or date in synthetic_dates:
                    continue
                date_errors = []
                for field in PRICE_FIELDS:
                    raw_value = float(raw_by_date.loc[date, field])
                    adjusted_value = float(adjusted_by_date.loc[date, field])
                    if not math.isfinite(raw_value) or raw_value <= 0 or not math.isfinite(adjusted_value):
                        date_errors = [math.inf]
                        break
                    ratio = adjusted_value / raw_value
                    error = abs(ratio - anchor_ratios[field]) / max(
                        abs(anchor_ratios[field]), 1e-12
                    )
                    date_errors.append(error)
                maximum = float(max(date_errors)) if date_errors else math.inf
                per_date_maximum[date] = maximum
                ratio_errors.extend(date_errors)
                if maximum <= AUDIT_THRESHOLDS["carriedAdjustmentMaximumRelativeSpread"]:
                    stable_dates.append(date)
                else:
                    unstable_dates.append(date)

    failures = []
    if len(requested_dates) < AUDIT_THRESHOLDS["minimumAdjustmentRegimeSessions"]:
        failures.append("forward adjustment evidence chain has fewer than three sessions")
    if missing_dates:
        failures.append("forward adjustment evidence is missing raw or qfq rows")
    if action_dates:
        failures.append("forward adjustment evidence crosses a corporate action")
    if synthetic_dates:
        failures.append("synthetic qfq rows cannot be forward adjustment evidence")
    if unstable_dates:
        failures.append("forward qfq/raw ratios are unstable against the base anchor")
    if len(stable_dates) != len(requested_dates):
        failures.append("forward adjustment evidence chain is incomplete")
    return {
        "status": "PASS" if not failures else "FAIL",
        "anchorDate": anchor_date,
        "requestedDates": requested_dates,
        "stableDates": stable_dates,
        "missingDates": missing_dates,
        "corporateActionDates": action_dates,
        "syntheticDates": synthetic_dates,
        "unstableDates": unstable_dates,
        "perDateMaximumRelativeSpread": per_date_maximum,
        "qfqRawRatioRelativeSpread": _metric_summary(np.asarray(ratio_errors)),
        "failures": list(dict.fromkeys(failures)),
    }


def audit_symbol_mapping(
    base_provider: Path,
    code: str,
    raw_frame: pd.DataFrame,
    adjusted_frame: pd.DataFrame,
    calendar: list[str],
    overlap_dates: list[str],
    anchor_date: str,
    *,
    forward_adjustment_evidence_dates: list[str] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    forward_adjustment_evidence_dates = forward_adjustment_evidence_dates or []
    allow_forward_adjustment_evidence = bool(forward_adjustment_evidence_dates)
    raw = _prepare_source_frame(raw_frame, "Tencent raw")
    adjusted = _prepare_source_frame(adjusted_frame, "Tencent adjusted")
    base = _load_base_frame(base_provider, code, calendar, overlap_dates)
    raw_columns = {
        column: f"raw_{column}"
        for column in raw.columns
        if column != "date"
    }
    adjusted_columns = {
        column: f"adjusted_{column}"
        for column in adjusted.columns
        if column != "date"
    }
    merged = base.merge(
        raw.rename(columns=raw_columns),
        on="date",
        how="left",
    ).merge(
        adjusted.rename(columns=adjusted_columns),
        on="date",
        how="left",
    )
    local_valid = int(merged["close"].notna().sum())
    common = merged[
        merged[["close", "raw_close", "adjusted_close"]].notna().all(axis=1)
    ].copy()
    if anchor_date not in set(common["date"]):
        raise ValueError(f"{code} has no common raw/qfq/base anchor on {anchor_date}")
    anchor = common[common["date"] == anchor_date].iloc[-1]
    price_scale = float(anchor["close"]) / float(anchor["adjusted_close"])
    if not math.isfinite(price_scale) or price_scale <= 0:
        raise ValueError(f"{code} produced an invalid qfq anchor scale")

    price_errors = []
    for field in PRICE_FIELDS:
        price_errors.extend(
            _relative_errors(common[field], common[f"adjusted_{field}"] * price_scale)
        )
    price_summary = _metric_summary(np.asarray(price_errors))
    action_dates = sorted(
        raw.loc[
            raw["corporate_action"].map(_action_payload).map(bool)
            & (raw["date"] <= anchor_date),
            "date",
        ].tolist()
    )
    last_action_date = action_dates[-1] if action_dates else None
    adjustment_regime = (
        common[common["date"] >= last_action_date]
        if last_action_date
        else common
    )
    regime_price_errors = []
    for field in PRICE_FIELDS:
        regime_price_errors.extend(
            _relative_errors(
                adjustment_regime[field],
                adjustment_regime[f"adjusted_{field}"] * price_scale,
            )
        )
    regime_price_summary = _metric_summary(np.asarray(regime_price_errors))
    forward_evidence = {
        "status": "NOT_ENABLED",
        "anchorDate": anchor_date,
        "requestedDates": [],
        "stableDates": [],
        "missingDates": [],
        "corporateActionDates": [],
        "syntheticDates": [],
        "unstableDates": [],
        "perDateMaximumRelativeSpread": {},
        "qfqRawRatioRelativeSpread": _metric_summary(np.asarray([])),
        "failures": [],
    }
    if allow_forward_adjustment_evidence:
        forward_evidence = _forward_adjustment_evidence(
            raw,
            adjusted,
            anchor_date,
            forward_adjustment_evidence_dates,
        )

    factor_errors = _relative_errors(
        common["factor"],
        common["close"] / common["raw_close"],
    )
    volume_errors = _relative_errors(
        common["volume"],
        common["raw_volume"] / (100.0 * common["factor"]),
    )
    amount_errors = _relative_errors(common["amount"], common["raw_amount"] / 1000.0)
    vwap_errors = _relative_errors(
        common["vwap"],
        common["amount"] * 10.0 / common["volume"],
    )
    adjclose_ratio = common["adjclose"] / common["close"]
    adjclose_to_close = float(np.nanmedian(adjclose_ratio))
    adjclose_ratio_errors = _relative_errors(
        adjclose_ratio,
        np.full(len(adjclose_ratio), adjclose_to_close),
    )

    raw_changes = raw[["date", "close"]].copy()
    raw_changes["predicted_change"] = raw_changes["close"].pct_change(fill_method=None)
    change_check = common[["date", "change"]].merge(
        raw_changes[["date", "predicted_change"]], on="date", how="left"
    )
    change_values = np.abs(
        change_check["change"].to_numpy(dtype="float64")
        - change_check["predicted_change"].to_numpy(dtype="float64")
    )
    change_values = change_values[np.isfinite(change_values)]

    adjustment_keys = sorted(set(adjusted.get("adjustment_key", pd.Series(dtype=str)).dropna()))
    if len(adjustment_keys) != 1:
        raise ValueError(f"{code} has inconsistent Tencent adjustment keys: {adjustment_keys}")
    adjustment_key = adjustment_keys[0]
    day_identity_summary = {"count": 0, "median": None, "p95": None, "maximum": None}
    day_factor_spread = 0.0
    day_actions = 0
    if adjustment_key == "day":
        identity_errors = []
        for field in PRICE_FIELDS:
            identity_errors.extend(
                _relative_errors(common[f"raw_{field}"], common[f"adjusted_{field}"])
            )
        day_identity_summary = _metric_summary(np.asarray(identity_errors))
        factor_median = float(np.nanmedian(common["factor"]))
        day_factor_spread = float(
            np.nanmax(np.abs(common["factor"] / factor_median - 1.0))
        )
        day_actions = sum(bool(_action_payload(value)) for value in raw["corporate_action"])

    summaries = {
        "qfqReplayRelativeError": price_summary,
        "qfqAdjustmentRegimeRelativeError": regime_price_summary,
        "factorRelativeError": _metric_summary(factor_errors),
        "volumeRelativeError": _metric_summary(volume_errors),
        "amountRelativeError": _metric_summary(amount_errors),
        "vwapRelativeError": _metric_summary(vwap_errors),
        "adjcloseRatioRelativeError": _metric_summary(adjclose_ratio_errors),
        "changeAbsoluteError": _metric_summary(change_values),
        "dayIdentityRelativeError": day_identity_summary,
        "forwardQfqRawRatioRelativeSpread": forward_evidence[
            "qfqRawRatioRelativeSpread"
        ],
    }
    coverage = len(common) / local_valid if local_valid else 0.0
    failures = []
    direct_regime_sessions = int(len(adjustment_regime))
    effective_regime_dates = set(adjustment_regime["date"].tolist())
    forward_evidence_used = False
    if len(common) < AUDIT_THRESHOLDS["minimumCommonSessions"]:
        failures.append("insufficient common overlap sessions")
    if direct_regime_sessions < AUDIT_THRESHOLDS["minimumAdjustmentRegimeSessions"]:
        if not allow_forward_adjustment_evidence:
            failures.append("insufficient post-corporate-action adjustment regime")
        elif direct_regime_sessions < AUDIT_THRESHOLDS[
            "minimumDirectAdjustmentRegimeSessionsWithForwardEvidence"
        ]:
            failures.append(
                "insufficient direct base overlap for forward adjustment evidence"
            )
        elif forward_evidence["status"] != "PASS":
            failures.append("forward adjustment evidence did not pass")
        else:
            forward_evidence_used = True
            effective_regime_dates.update(forward_evidence["stableDates"])
    if len(effective_regime_dates) < AUDIT_THRESHOLDS["minimumAdjustmentRegimeSessions"]:
        failures.append("insufficient effective post-corporate-action adjustment regime")
    if coverage < AUDIT_THRESHOLDS["minimumOverlapCoverage"]:
        failures.append("overlap coverage below threshold")
    if _summary_value(regime_price_summary, "median") > AUDIT_THRESHOLDS["qfqReplayMedianRelativeError"]:
        failures.append("qfq adjustment-regime median error above threshold")
    if _summary_value(regime_price_summary, "p95") > AUDIT_THRESHOLDS["qfqReplayP95RelativeError"]:
        failures.append("qfq adjustment-regime p95 error above threshold")
    if _maximum(regime_price_summary) > AUDIT_THRESHOLDS["qfqReplayMaximumRelativeError"]:
        failures.append("qfq adjustment-regime maximum error above threshold")
    for label, threshold_name in (
        ("factorRelativeError", "factorMaximumRelativeError"),
        ("volumeRelativeError", "volumeMaximumRelativeError"),
        ("amountRelativeError", "amountMaximumRelativeError"),
        ("vwapRelativeError", "vwapMaximumRelativeError"),
        ("adjcloseRatioRelativeError", "adjcloseRatioMaximumRelativeError"),
        ("changeAbsoluteError", "changeMaximumAbsoluteError"),
    ):
        if _maximum(summaries[label]) > AUDIT_THRESHOLDS[threshold_name]:
            failures.append(f"{label} above threshold")
    if adjustment_key == "day":
        if _maximum(day_identity_summary) > AUDIT_THRESHOLDS["dayIdentityMaximumRelativeError"]:
            failures.append("day payload is not identical to raw prices")
        if day_factor_spread > AUDIT_THRESHOLDS["dayIdentityMaximumRelativeError"]:
            failures.append("day payload has a non-constant base factor")
        if day_actions:
            failures.append("day payload cannot cover corporate actions")

    audit = {
        "status": "PASS" if not failures else "FAIL",
        "code": code,
        "adjustmentKey": adjustment_key,
        "anchorDate": anchor_date,
        "lastCorporateActionDate": last_action_date,
        "adjustmentRegimeSessions": direct_regime_sessions,
        "baseAdjustmentRegimeDates": adjustment_regime["date"].tolist(),
        "baseAdjustmentRegimeSessions": direct_regime_sessions,
        "adjustmentEvidenceMode": (
            "base_overlap_plus_forward_source_continuity"
            if forward_evidence_used
            else "base_overlap_only"
        ),
        "adjustmentEvidenceSessions": int(len(effective_regime_dates)),
        "directAdjustmentRegimeSessions": direct_regime_sessions,
        "forwardAdjustmentEvidenceEnabled": allow_forward_adjustment_evidence,
        "forwardAdjustmentEvidenceUsed": forward_evidence_used,
        "forwardAdjustmentEvidenceDates": [
            date
            for date in forward_evidence["stableDates"]
            if date > anchor_date
        ],
        "forwardAdjustmentEvidenceSessions": sum(
            date > anchor_date for date in forward_evidence["stableDates"]
        ),
        "forwardAdjustmentEvidence": forward_evidence,
        "commonSessions": int(len(common)),
        "localValidSessions": local_valid,
        "overlapCoverage": float(coverage),
        "dayFactorRelativeSpread": day_factor_spread,
        "dayCorporateActionRows": day_actions,
        "metrics": summaries,
        "failures": failures,
    }
    context = {
        "priceScale": price_scale,
        "adjcloseToClose": adjclose_to_close,
        "adjustmentKey": adjustment_key,
        "lastFactor": float(anchor["factor"]),
    }
    return audit, context


def _complete_adjusted_targets(
    raw_frame: pd.DataFrame,
    adjusted_frame: pd.DataFrame,
    target_dates: list[str],
    explicit_no_bar_dates: set[str] | None = None,
) -> pd.DataFrame:
    explicit_no_bar_dates = explicit_no_bar_dates or set()
    raw = _prepare_source_frame(raw_frame, "Tencent raw")
    adjusted = _prepare_source_frame(adjusted_frame, "Tencent adjusted")
    adjusted["synthetic"] = False
    raw_by_date = raw.set_index("date")
    adjusted_by_date = adjusted.set_index("date")
    synthetic_rows = []
    for target_date in target_dates:
        if target_date not in raw_by_date.index:
            # The caller owns suspension evidence and will either emit a factor-only
            # no-bar row or reject the missing source row.
            continue
        if target_date in adjusted_by_date.index:
            continue
        raw_row = raw_by_date.loc[target_date]
        if bool(_action_payload(raw_row["corporate_action"])):
            raise ValueError(
                f"Tencent qfq is missing corporate-action date {target_date}"
            )
        common_dates = sorted(
            date
            for date in set(raw_by_date.index) & set(adjusted_by_date.index)
            if date < target_date
        )
        if len(common_dates) < 2:
            raise ValueError(f"Tencent qfq has no stable history before {target_date}")
        evidence_dates = common_dates[-2:]
        ratios: dict[str, float] = {}
        for field in PRICE_FIELDS:
            values = np.asarray(
                [
                    float(adjusted_by_date.loc[date, field])
                    / float(raw_by_date.loc[date, field])
                    for date in evidence_dates
                ],
                dtype="float64",
            )
            median = float(np.median(values))
            spread = float(np.max(np.abs(values / median - 1.0)))
            if spread > AUDIT_THRESHOLDS["carriedAdjustmentMaximumRelativeSpread"]:
                raise ValueError(
                    f"Tencent qfq/raw ratio is unstable before {target_date}"
                )
            ratios[field] = median
        actions_since_evidence = raw[
            (raw["date"] > evidence_dates[-1]) & (raw["date"] <= target_date)
        ]["corporate_action"].map(_action_payload).map(bool)
        if actions_since_evidence.any():
            raise ValueError(
                f"Tencent qfq cannot be carried across a corporate action on {target_date}"
            )
        synthetic_rows.append(
            {
                "date": target_date,
                "code": raw_row.get("code"),
                **{field: float(raw_row[field]) * ratios[field] for field in PRICE_FIELDS},
                "source_volume": raw_row.get("source_volume"),
                "corporate_action": {},
                "adjustment_key": adjusted["adjustment_key"].iloc[-1],
                "source": "tencent_qfq_ratio_carried",
                "synthetic": True,
            }
        )
    if synthetic_rows:
        adjusted = pd.concat([adjusted, pd.DataFrame(synthetic_rows)], ignore_index=True)
    return adjusted.sort_values("date").reset_index(drop=True)


def build_extension_rows(
    code: str,
    raw_frame: pd.DataFrame,
    adjusted_frame: pd.DataFrame,
    target_dates: list[str],
    context: dict[str, Any],
    explicit_no_bar_dates: set[str] | None = None,
) -> pd.DataFrame:
    explicit_no_bar_dates = explicit_no_bar_dates or set()
    raw = _prepare_source_frame(raw_frame, "Tencent raw")
    adjusted = _complete_adjusted_targets(
        raw,
        adjusted_frame,
        target_dates,
        explicit_no_bar_dates,
    )
    raw["predicted_change"] = raw["close"].pct_change(fill_method=None)
    raw_by_date = raw.set_index("date")
    adjusted_by_date = adjusted.set_index("date")
    rows = []
    previous_factor = float(context["lastFactor"])
    for target_date in target_dates:
        if target_date not in raw_by_date.index:
            if target_date not in explicit_no_bar_dates:
                raise ValueError(
                    f"{code} is missing {target_date} without independent suspension evidence"
                )
            rows.append(
                {
                    "date": target_date,
                    "code": _canonical_code(code),
                    **{field: np.nan for field in FIELDS if field != "factor"},
                    "factor": previous_factor,
                    "syntheticAdjustment": False,
                    "noBar": True,
                }
            )
            continue
        if target_date not in adjusted_by_date.index:
            raise ValueError(f"{code} is missing adjusted target date {target_date}")
        raw_row = raw_by_date.loc[target_date]
        adjusted_row = adjusted_by_date.loc[target_date]
        normalized = {
            field: float(adjusted_row[field]) * float(context["priceScale"])
            for field in PRICE_FIELDS
        }
        factor = normalized["close"] / float(raw_row["close"])
        volume = float(raw_row["volume"]) / (100.0 * factor)
        amount = float(raw_row["amount"]) / 1000.0
        vwap = amount * 10.0 / volume
        row = {
            "date": target_date,
            "code": _canonical_code(code),
            **normalized,
            "volume": volume,
            "vwap": vwap,
            "amount": amount,
            "factor": factor,
            "change": float(raw_row["predicted_change"]),
            "adjclose": normalized["close"] * float(context["adjcloseToClose"]),
            "syntheticAdjustment": bool(adjusted_row.get("synthetic", False)),
            "noBar": False,
        }
        numeric = np.asarray([row[field] for field in FIELDS], dtype="float64")
        if not np.isfinite(numeric).all():
            raise ValueError(f"{code} produced non-finite target values on {target_date}")
        if not (
            row["low"] <= row["open"] <= row["high"]
            and row["low"] <= row["close"] <= row["high"]
            and row["volume"] >= 0
            and row["amount"] >= 0
            and row["factor"] > 0
        ):
            raise ValueError(f"{code} produced invalid target values on {target_date}")
        previous_factor = factor
        rows.append(row)
    return pd.DataFrame(rows)


def _frame_snapshot_digest(frame: pd.DataFrame, target_dates: list[str]) -> str:
    work = _prepare_source_frame(frame, "snapshot")
    by_date = work.set_index("date")
    payload = []
    for date in target_dates:
        if date not in by_date.index:
            payload.append({"date": date, "row": None})
            continue
        row = by_date.loc[date].to_dict()
        row["corporate_action"] = _action_payload(row.get("corporate_action"))
        payload.append({"date": date, "row": row})
    return hashlib.sha256(_json_bytes(payload)).hexdigest()


def _artifact_frame(path: Path, frame: pd.DataFrame) -> dict[str, Any]:
    path.parent.mkdir(parents=True, exist_ok=True)
    work = frame.copy()
    if "corporate_action" in work:
        work["corporate_action"] = work["corporate_action"].map(
            lambda value: json.dumps(_action_payload(value), ensure_ascii=True, sort_keys=True)
        )
    work.to_csv(path, index=False)
    return {"path": str(path), "sha256": _sha256(path), "rows": int(len(work))}


def _read_snapshot_artifact(path: Path, name: str) -> tuple[pd.DataFrame, dict[str, Any]]:
    if not path.is_file():
        raise FileNotFoundError(f"{name} snapshot artifact is missing: {path}")
    frame = pd.read_csv(path)
    if frame.empty:
        raise ValueError(f"{name} snapshot artifact is empty: {path}")
    return frame, {"path": str(path), "sha256": _sha256(path), "rows": int(len(frame))}


def _retry(call: Callable[[], pd.DataFrame], label: str) -> pd.DataFrame:
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            return call()
        except Exception as exc:
            last_error = exc
            if attempt < 2:
                time.sleep(0.5 * (attempt + 1))
    assert last_error is not None
    raise RuntimeError(f"{label} failed after three attempts: {last_error}") from last_error


def _tree_digest(entries: list[tuple[str, str]]) -> str:
    digest = hashlib.sha256()
    for relative, file_digest in sorted(entries):
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(file_digest.encode("ascii"))
    return digest.hexdigest()


def _directory_tree_contract(root: Path) -> dict[str, Any]:
    if not root.is_dir():
        raise FileNotFoundError(f"source artifact directory is missing: {root}")
    entries = [
        (path.relative_to(root).as_posix(), _sha256(path))
        for path in sorted(root.rglob("*"))
        if path.is_file()
    ]
    return {"files": len(entries), "treeSha256": _tree_digest(entries)}


def _extend_market_file(
    source: Path,
    destination: Path,
    codes: set[str],
    base_end: str,
    target_end: str,
) -> int:
    lines = []
    extended = set()
    for line in source.read_text(encoding="utf-8").splitlines():
        parts = line.split("\t")
        if len(parts) >= 3 and parts[1] <= base_end <= parts[2]:
            code = _canonical_code(parts[0])
            if code in codes:
                parts[2] = target_end
                line = "\t".join(parts)
                extended.add(code)
        lines.append(line)
    if extended != codes:
        missing = sorted(codes - extended)
        raise ValueError(f"market file did not extend every frozen instrument: {missing[:10]}")
    destination.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return len(extended)


def _feature_tree_digests(
    base: Path,
    build: Path,
    codes: list[str],
    appended_count: int,
) -> dict[str, Any]:
    prefix_entries = []
    extension_entries = []
    output_entries = []
    for code in sorted(codes):
        qlib_code = to_qlib_code(code)
        for field in FIELDS:
            relative = f"features/{qlib_code}/{field}.day.bin"
            source = base / relative
            destination = build / relative
            source_size = source.stat().st_size
            if destination.stat().st_size != source_size + appended_count * 4:
                raise RuntimeError(f"unexpected appended feature size: {relative}")
            with destination.open("rb") as file:
                prefix = file.read(source_size)
                extension = file.read()
            prefix_digest = hashlib.sha256(prefix).hexdigest()
            source_digest = _sha256(source)
            if prefix_digest != source_digest:
                raise RuntimeError(f"base feature prefix changed: {relative}")
            prefix_entries.append((relative, prefix_digest))
            extension_entries.append((relative, hashlib.sha256(extension).hexdigest()))
            output_entries.append((relative, _sha256(destination)))
    return {
        "files": len(output_entries),
        "prefixTreeSha256": _tree_digest(prefix_entries),
        "extensionTreeSha256": _tree_digest(extension_entries),
        "outputTreeSha256": _tree_digest(output_entries),
    }


def build_immutable_provider(
    base_provider: Path,
    output_provider: Path,
    panel: pd.DataFrame,
    market_codes: list[str],
    target_dates: list[str],
    *,
    market: str,
    benchmark: str,
    batch_id: str,
    source_manifest: Path,
    created_at: str,
    audit_summary: dict[str, Any],
) -> dict[str, Any]:
    base = base_provider.expanduser().resolve()
    output = output_provider.expanduser().resolve()
    if base == output:
        raise ValueError("daily extension must be written to a new provider")
    if output.exists():
        raise FileExistsError(f"refusing to overwrite provider: {output}")
    calendar = _read_calendar(base)
    base_end = calendar[-1]
    expected_targets = [date for date in target_dates if date > base_end]
    if expected_targets != target_dates or target_dates != sorted(set(target_dates)):
        raise ValueError("target dates must be unique and strictly after the base calendar")
    work = panel.copy()
    work["code"] = work["code"].map(_canonical_code)
    work["date"] = pd.to_datetime(work["date"]).dt.strftime("%Y-%m-%d")
    required_codes = sorted(set(market_codes) | {_canonical_code(benchmark)})
    if sorted(work["code"].unique()) != required_codes:
        raise ValueError("extension panel does not contain the frozen universe plus benchmark")
    if len(work) != len(required_codes) * len(target_dates):
        raise ValueError("extension panel is not rectangular")
    if work.duplicated(["code", "date"]).any():
        raise ValueError("extension panel contains duplicate code/date rows")
    if sorted(work["date"].unique()) != target_dates:
        raise ValueError("extension panel target dates do not match the requested dates")
    missing_fields = sorted(set(FIELDS) - set(work.columns))
    if missing_fields:
        raise ValueError(f"extension panel is missing fields: {missing_fields}")
    if audit_summary.get("status") != "PASS":
        raise RuntimeError("daily extension audit must pass before provider promotion")

    base_contract = provider_contract_fingerprint(base, market)
    if not source_manifest.exists():
        raise FileNotFoundError(f"daily extension source manifest is missing: {source_manifest}")
    try:
        source_contract = json.loads(source_manifest.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError("daily extension source manifest is invalid JSON") from exc
    if source_contract.get("schemaVersion") != "quantaalpha-market-data-source-v2":
        raise ValueError("daily extension source manifest has the wrong schema version")
    if source_contract.get("kind") != "frozen_market_daily_prices":
        raise ValueError("daily extension source manifest has the wrong kind")
    source_audit = source_contract.get("audit") or {}
    if source_audit.get("status") != "PASS":
        raise RuntimeError("daily extension source manifest audit did not pass")
    if source_contract.get("baseProviderFingerprint") != base_contract["fingerprint"]:
        raise RuntimeError("daily extension source manifest base fingerprint mismatch")
    if (source_contract.get("request") or {}).get("targetDates") != target_dates:
        raise RuntimeError("daily extension source manifest target dates mismatch")
    source_summary = {key: value for key, value in source_audit.items() if key != "perSymbol"}
    if source_summary != audit_summary:
        raise RuntimeError("daily extension source manifest audit summary mismatch")
    source_policy = source_audit.get("adjustmentEvidencePolicy")
    build_policy = audit_summary.get("adjustmentEvidencePolicy")
    if not isinstance(source_policy, dict) or source_policy != build_policy:
        raise RuntimeError("daily extension adjustment evidence policy mismatch")
    request_forward_dates = (source_contract.get("request") or {}).get(
        "forwardAdjustmentEvidenceDates"
    )
    if request_forward_dates != source_policy.get("forwardDates"):
        raise RuntimeError("daily extension request evidence approval mismatch")
    source_artifacts = source_contract.get("artifacts") or {}
    artifact_root_value = source_artifacts.get("root")
    if not artifact_root_value:
        raise ValueError("daily extension source artifact root is missing")
    artifact_root = Path(str(artifact_root_value)).expanduser().resolve()
    actual_artifacts = _directory_tree_contract(artifact_root)
    if actual_artifacts != {
        "files": source_artifacts.get("files"),
        "treeSha256": source_artifacts.get("treeSha256"),
    }:
        raise RuntimeError("daily extension source artifact tree mismatch")
    source_replay = (source_contract.get("source") or {}).get("snapshotReplay")
    if source_replay:
        replay_report = Path(str(source_replay.get("reportPath") or "")).expanduser()
        if not replay_report.is_file():
            raise FileNotFoundError("daily extension replay report is missing")
        if _sha256(replay_report) != source_replay.get("reportSha256"):
            raise RuntimeError("daily extension replay report hash mismatch")
    build = output.with_name(f".{output.name}_build_{batch_id}")
    if build.exists():
        raise FileExistsError(f"stale provider build path exists: {build}")
    build.parent.mkdir(parents=True, exist_ok=True)
    try:
        # Existing feature files will be appended, so hard links are unsafe here.
        shutil.copytree(base, build, copy_function=shutil.copy2)
        (build / "calendars" / "day.txt").write_text(
            "\n".join([*calendar, *target_dates]) + "\n",
            encoding="utf-8",
        )
        extended_count = _extend_market_file(
            base / "instruments" / f"{market}.txt",
            build / "instruments" / f"{market}.txt",
            set(market_codes),
            base_end,
            target_dates[-1],
        )
        base_last_index = len(calendar) - 1
        indexed = work.set_index(["code", "date"])
        for code in required_codes:
            for field in FIELDS:
                source = base / "features" / to_qlib_code(code) / f"{field}.day.bin"
                destination = build / "features" / to_qlib_code(code) / f"{field}.day.bin"
                binary = np.fromfile(source, dtype="<f")
                if len(binary) < 2 or int(binary[0]) + len(binary) - 2 != base_last_index:
                    raise ValueError(f"base feature does not end at the base calendar: {source}")
                extension = np.asarray(
                    [indexed.loc[(code, date), field] for date in target_dates],
                    dtype="<f",
                )
                with destination.open("ab") as file:
                    file.write(extension.tobytes())

        feature_tree = _feature_tree_digests(
            base,
            build,
            required_codes,
            len(target_dates),
        )
        extension_manifest = {
            "schemaVersion": "quantaalpha-provider-extension-v2",
            "kind": "frozen_market_daily_prices",
            "batchId": batch_id,
            "createdAt": created_at,
            "baseProvider": str(base),
            "baseProviderFingerprint": base_contract["fingerprint"],
            "sourceManifest": str(source_manifest.resolve()),
            "sourceManifestSha256": _sha256(source_manifest),
            "source": {
                "provider": "a-stock-data/tencent-public",
                "credentialsRequired": False,
                "serviceLevelAgreement": False,
            },
            "scope": {
                "allowedMarkets": [market],
                "market": market,
                "universeAsOf": base_end,
                "baseCalendarEnd": base_end,
                "extensionCalendarEnd": target_dates[-1],
                "frozenInstrumentCount": len(market_codes),
                "benchmark": _qlib_instrument(benchmark),
                "allMarketExtended": False,
                "allInstrumentSha256": _sha256(build / "instruments" / "all.txt"),
            },
            "calendarExtension": target_dates,
            "featureFields": list(FIELDS),
            "featureTree": feature_tree,
            "normalizationContract": {
                "ohlc": "tencent_qfq_ohlc * base_close_anchor / tencent_qfq_close_anchor",
                "factor": "normalized_close / tencent_raw_close",
                "volume": "tencent_raw_shares / (100 * factor)",
                "amount": "tencent_raw_cny / 1000",
                "vwap": "amount * 10 / volume",
                "change": "tencent_raw_close / previous_valid_tencent_raw_close - 1",
                "adjclose": "normalized_close * median(base_adjclose / base_close)",
            },
            "audit": audit_summary,
        }
        _write_json(build / EXTENSION_MANIFEST, extension_manifest)
        build_contract = provider_contract_fingerprint(build, market)
        if build_contract["fingerprint"] == base_contract["fingerprint"]:
            raise RuntimeError("daily extension provider fingerprint did not change")
        if _read_calendar(build)[-len(target_dates) :] != target_dates:
            raise RuntimeError("built provider calendar extension is invalid")
        current_base_contract = provider_contract_fingerprint(base, market)
        if current_base_contract["fingerprint"] != base_contract["fingerprint"]:
            raise RuntimeError("base provider changed during immutable extension build")
        build.replace(output)
    except Exception:
        shutil.rmtree(build, ignore_errors=True)
        raise
    return {
        "path": str(output),
        "fingerprint": build_contract["fingerprint"],
        "baseFingerprint": base_contract["fingerprint"],
        "extensionManifest": str(output / EXTENSION_MANIFEST),
        "extendedMarketInstruments": extended_count,
        "featureTree": feature_tree,
    }


def _fetch_pair(
    client: AStockDataClient,
    code: str,
    start_date: str,
    end_date: str,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    raw = _retry(
        lambda: client.tencent_raw_history(code, start_date, end_date),
        f"Tencent raw {code}",
    )
    adjusted = _retry(
        lambda: client.tencent_adjusted_history(code, start_date, end_date),
        f"Tencent qfq {code}",
    )
    return raw, adjusted


def sync_investment_data_daily_extension(
    project_root: Path,
    request: InvestmentDataDailyExtensionRequest,
    *,
    client: AStockDataClient | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    base = request.base_provider.expanduser().resolve()
    output = request.output_provider.expanduser().resolve() if request.output_provider else None
    if not re.fullmatch(r"[a-zA-Z0-9_.-]+", request.market):
        raise ValueError("market contains unsupported characters")
    if request.overlap_sessions < AUDIT_THRESHOLDS["minimumCommonSessions"]:
        raise ValueError("overlap_sessions is below the audit minimum")
    if request.snapshot_rechecks not in {1, 2}:
        raise ValueError("snapshot_rechecks must be one or two")
    if request.forward_adjustment_evidence_dates and request.snapshot_rechecks != 2:
        raise ValueError("forward adjustment evidence requires two source snapshots")
    if request.source_snapshot_dir and request.snapshot_rechecks != 2:
        raise ValueError("snapshot replay requires two source snapshots")
    if output and output.exists():
        raise FileExistsError(f"refusing to overwrite provider: {output}")

    calendar = _read_calendar(base)
    future_calendar = _read_calendar(base, "day_future.txt")
    base_end = calendar[-1]
    end_date = pd.Timestamp(request.end_date).strftime("%Y-%m-%d")
    target_dates = [date for date in future_calendar if base_end < date <= end_date]
    if not target_dates:
        raise ValueError(f"no new provider sessions between {base_end} and {end_date}")
    if target_dates != sorted(set(target_dates)):
        raise ValueError("future calendar produced invalid target dates")
    approved_forward_dates = [
        pd.Timestamp(date).strftime("%Y-%m-%d")
        for date in request.forward_adjustment_evidence_dates
    ]
    if approved_forward_dates and approved_forward_dates != target_dates:
        raise ValueError(
            "forward adjustment evidence dates must exactly match the target dates"
        )

    shanghai_now = now or datetime.now(ZoneInfo("Asia/Shanghai"))
    if shanghai_now.tzinfo is None:
        shanghai_now = shanghai_now.replace(tzinfo=ZoneInfo("Asia/Shanghai"))
    today = shanghai_now.astimezone(ZoneInfo("Asia/Shanghai")).date().isoformat()
    if end_date > today:
        raise ValueError("daily extension cannot publish future sessions")
    current_session = target_dates[-1] == today
    if current_session and request.require_closed_session:
        local_time = shanghai_now.astimezone(ZoneInfo("Asia/Shanghai")).time()
        if (local_time.hour, local_time.minute) < (16, 0):
            raise RuntimeError("current-session bars cannot be accepted before 16:00 Asia/Shanghai")
        if request.snapshot_rechecks != 2:
            raise RuntimeError("current-session bars require two identical source snapshots")

    batch_id = request.batch_id or (
        f"a_stock_data_investment_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}"
    )
    if not re.fullmatch(r"[a-zA-Z0-9_.-]+", batch_id):
        raise ValueError("batch_id contains unsupported characters")
    new_raw_root = raw_provider_dir(project_root, RAW_PROVIDER_NAME, batch_id)
    replay_root = (
        request.source_snapshot_dir.expanduser().resolve()
        if request.source_snapshot_dir
        else None
    )
    raw_root = replay_root or new_raw_root
    manifest_file = manifest_path(project_root, batch_id)
    report_file = report_path(project_root, batch_id)
    if new_raw_root.exists() or manifest_file.exists() or report_file.exists():
        raise FileExistsError(f"refusing to reuse immutable batch: {batch_id}")
    if replay_root:
        if not replay_root.is_dir():
            raise FileNotFoundError(f"snapshot replay directory is missing: {replay_root}")
    else:
        raw_root.mkdir(parents=True)

    started_at = datetime.now(timezone.utc).isoformat()
    base_contract = provider_contract_fingerprint(base, request.market)
    market_codes = load_frozen_universe(base, request.market, base_end)
    benchmark = _canonical_code(request.benchmark)
    fetch_codes = sorted(set(market_codes) | {benchmark})
    overlap_dates = calendar[-request.overlap_sessions :]
    snapshot_dates = sorted(set([*overlap_dates, *target_dates]))
    fetch_start = overlap_dates[0]
    source_client = None if replay_root else (client or AStockDataClient())
    adjustment_evidence_policy = _adjustment_evidence_policy(
        base_end,
        approved_forward_dates,
    )
    source_frames: dict[str, tuple[pd.DataFrame, pd.DataFrame]] = {}
    artifacts: dict[str, Any] = {}
    snapshot_failures: dict[str, str] = {}
    source_replay: dict[str, Any] | None = None
    if replay_root:
        replay_report_path = report_path(project_root, replay_root.name)
        if not replay_report_path.is_file():
            raise FileNotFoundError(
                f"snapshot replay report is missing: {replay_report_path}"
            )
        replay_report = json.loads(replay_report_path.read_text(encoding="utf-8"))
        if Path(str(replay_report.get("rawDir") or "")).expanduser().resolve() != replay_root:
            raise RuntimeError("snapshot replay report raw directory mismatch")
        if replay_report.get("baseProviderFingerprint") != base_contract["fingerprint"]:
            raise RuntimeError("snapshot replay report base fingerprint mismatch")
        if replay_report.get("targetDates") != target_dates:
            raise RuntimeError("snapshot replay report target dates mismatch")
        if replay_report.get("provider") != "a-stock-data/tencent-public":
            raise RuntimeError("snapshot replay report provider mismatch")
        source_replay = {
            "batchId": replay_root.name,
            "reportPath": str(replay_report_path),
            "reportSha256": _sha256(replay_report_path),
            "reportStatus": replay_report.get("status"),
            "capturedAt": replay_report.get("finishedAt"),
        }

    try:
        for code in fetch_codes:
            code_key = to_qlib_code(code)
            if replay_root:
                raw, raw_artifact = _read_snapshot_artifact(
                    raw_root / "snapshot_1" / "raw" / f"{code_key}.csv",
                    f"snapshot 1 raw {code}",
                )
                adjusted, adjusted_artifact = _read_snapshot_artifact(
                    raw_root / "snapshot_1" / "adjusted" / f"{code_key}.csv",
                    f"snapshot 1 adjusted {code}",
                )
                raw_check, raw_check_artifact = _read_snapshot_artifact(
                    raw_root / "snapshot_2" / "raw" / f"{code_key}.csv",
                    f"snapshot 2 raw {code}",
                )
                adjusted_check, adjusted_check_artifact = _read_snapshot_artifact(
                    raw_root / "snapshot_2" / "adjusted" / f"{code_key}.csv",
                    f"snapshot 2 adjusted {code}",
                )
                code_artifacts = {
                    "snapshot1": {
                        "raw": raw_artifact,
                        "adjusted": adjusted_artifact,
                    },
                    "snapshot2": {
                        "raw": raw_check_artifact,
                        "adjusted": adjusted_check_artifact,
                    },
                }
            else:
                assert source_client is not None
                raw, adjusted = _fetch_pair(
                    source_client, code, fetch_start, end_date
                )
                code_artifacts = {
                    "snapshot1": {
                        "raw": _artifact_frame(
                            raw_root / "snapshot_1" / "raw" / f"{code_key}.csv",
                            raw,
                        ),
                        "adjusted": _artifact_frame(
                            raw_root
                            / "snapshot_1"
                            / "adjusted"
                            / f"{code_key}.csv",
                            adjusted,
                        ),
                    }
                }
            if request.snapshot_rechecks == 2 and not replay_root:
                time.sleep(max(request.request_interval_seconds, 0.0))
                raw_check, adjusted_check = _fetch_pair(
                    source_client, code, fetch_start, end_date
                )
                code_artifacts["snapshot2"] = {
                    "raw": _artifact_frame(
                        raw_root / "snapshot_2" / "raw" / f"{code_key}.csv",
                        raw_check,
                    ),
                    "adjusted": _artifact_frame(
                        raw_root / "snapshot_2" / "adjusted" / f"{code_key}.csv",
                        adjusted_check,
                    ),
                }
            if request.snapshot_rechecks == 2:
                if _frame_snapshot_digest(raw, snapshot_dates) != _frame_snapshot_digest(
                    raw_check, snapshot_dates
                ):
                    snapshot_failures[code] = (
                        "raw overlap or target rows changed between snapshots"
                    )
                elif _frame_snapshot_digest(
                    adjusted, snapshot_dates
                ) != _frame_snapshot_digest(adjusted_check, snapshot_dates):
                    snapshot_failures[code] = (
                        "adjusted overlap or target rows changed between snapshots"
                    )
            source_frames[code] = (raw, adjusted)
            artifacts[code] = code_artifacts
            if not replay_root:
                time.sleep(max(request.request_interval_seconds, 0.0))

        benchmark_dates = set(source_frames[benchmark][0]["date"].astype(str))
        missing_benchmark_dates = sorted(set(target_dates) - benchmark_dates)
        if missing_benchmark_dates:
            raise RuntimeError(
                f"benchmark is missing target sessions: {missing_benchmark_dates}"
            )
        if snapshot_failures:
            raise RuntimeError(
                f"current-session source snapshots were not stable: {snapshot_failures}"
            )
        target_actions: dict[str, list[str]] = {}
        for code, (raw, _) in source_frames.items():
            prepared_raw = _prepare_source_frame(raw, "Tencent raw")
            action_dates = prepared_raw.loc[
                prepared_raw["date"].isin(target_dates)
                & prepared_raw["corporate_action"].map(_action_payload).map(bool),
                "date",
            ].tolist()
            if action_dates:
                target_actions[code] = action_dates
        if target_actions:
            raise RuntimeError(
                "target corporate actions require a separately audited cross-action extension: "
                f"{target_actions}"
            )

        symbol_audits: dict[str, Any] = {}
        contexts: dict[str, Any] = {}
        panels = []
        for code in fetch_codes:
            raw, adjusted = source_frames[code]
            audit, context = audit_symbol_mapping(
                base,
                code,
                raw,
                adjusted,
                calendar,
                overlap_dates,
                base_end,
                forward_adjustment_evidence_dates=approved_forward_dates,
            )
            symbol_audits[code] = audit
            contexts[code] = context
        failed_audits = {
            code: audit["failures"]
            for code, audit in symbol_audits.items()
            if audit["status"] != "PASS"
        }
        if failed_audits:
            raise RuntimeError(f"source mapping audit failed: {failed_audits}")

        for code in fetch_codes:
            raw, adjusted = source_frames[code]
            panels.append(
                build_extension_rows(
                    code,
                    raw,
                    adjusted,
                    target_dates,
                    contexts[code],
                )
            )
        panel = pd.concat(panels, ignore_index=True)
        synthetic_adjustments = int(panel["syntheticAdjustment"].sum())
        adjustment_keys = {
            code: contexts[code]["adjustmentKey"] for code in fetch_codes
        }
        adjustment_evidence_modes = {
            code: symbol_audits[code]["adjustmentEvidenceMode"]
            for code in fetch_codes
        }
        artifact_entries = []
        for code_artifacts in artifacts.values():
            for snapshot in code_artifacts.values():
                for artifact in snapshot.values():
                    artifact_path = Path(artifact["path"])
                    artifact_entries.append(
                        (artifact_path.relative_to(raw_root).as_posix(), artifact["sha256"])
                    )
        audit_summary = {
            "status": "PASS",
            "thresholds": AUDIT_THRESHOLDS,
            "frozenInstrumentCount": len(market_codes),
            "fetchedInstrumentCount": len(fetch_codes),
            "targetDates": target_dates,
            "overlapStart": overlap_dates[0],
            "overlapEnd": overlap_dates[-1],
            "snapshotRechecks": request.snapshot_rechecks,
            "snapshotFailures": snapshot_failures,
            "targetCorporateActions": target_actions,
            "sourceCaptureMode": (
                "immutable_snapshot_replay" if replay_root else "live_double_snapshot"
            ),
            "adjustmentEvidencePolicy": adjustment_evidence_policy,
            "adjustmentEvidenceModeCounts": {
                str(key): int(value)
                for key, value in pd.Series(
                    adjustment_evidence_modes
                ).value_counts().items()
            },
            "syntheticAdjustmentRows": synthetic_adjustments,
            "adjustmentKeyCounts": {
                str(key): int(value)
                for key, value in pd.Series(adjustment_keys).value_counts().items()
            },
            "perSymbol": symbol_audits,
        }
        source_manifest_payload = {
            "schemaVersion": "quantaalpha-market-data-source-v2",
            "kind": "frozen_market_daily_prices",
            "batchId": batch_id,
            "createdAt": started_at,
            "baseProvider": str(base),
            "baseProviderFingerprint": base_contract["fingerprint"],
            "source": {
                "provider": "a-stock-data/tencent-public",
                "credentialsRequired": False,
                "rawEndpoint": "Tencent newfqkline",
                "adjustedEndpoint": "Tencent fqkline qfq",
                "captureMode": (
                    "immutable_snapshot_replay"
                    if replay_root
                    else "live_double_snapshot"
                ),
                "snapshotReplay": source_replay,
            },
            "request": {
                "market": request.market,
                "benchmark": _qlib_instrument(benchmark),
                "endDate": end_date,
                "overlapSessions": request.overlap_sessions,
                "targetDates": target_dates,
                "universeAsOf": base_end,
                "forwardAdjustmentEvidenceDates": approved_forward_dates,
                "sourceSnapshotDir": str(replay_root) if replay_root else None,
            },
            "normalizationContract": {
                "ohlc": "tencent_qfq_ohlc * base_close_anchor / tencent_qfq_close_anchor",
                "factor": "normalized_close / tencent_raw_close",
                "volume": "tencent_raw_shares / (100 * factor)",
                "amount": "tencent_raw_cny / 1000",
                "vwap": "amount * 10 / volume",
                "change": "tencent_raw_close / previous_valid_tencent_raw_close - 1",
                "adjclose": "normalized_close * median(base_adjclose / base_close)",
            },
            "artifacts": {
                "root": str(raw_root),
                "files": len(artifact_entries),
                "treeSha256": _tree_digest(artifact_entries),
            },
            "audit": audit_summary,
        }
        _write_json(manifest_file, source_manifest_payload)

        provider_result = None
        if output:
            _write_json(
                report_file,
                {
                    "status": "AUDIT_PASS_BUILD_PENDING",
                    "batchId": batch_id,
                    "baseProvider": str(base),
                    "baseProviderFingerprint": base_contract["fingerprint"],
                    "targetDates": target_dates,
                    "adjustmentEvidencePolicy": adjustment_evidence_policy,
                    "sourceReplay": source_replay,
                    "manifestPath": str(manifest_file),
                    "intendedOutputProvider": str(output),
                    "startedAt": started_at,
                },
            )
            provider_result = build_immutable_provider(
                base,
                output,
                panel,
                market_codes,
                target_dates,
                market=request.market,
                benchmark=benchmark,
                batch_id=batch_id,
                source_manifest=manifest_file,
                created_at=started_at,
                audit_summary={
                    key: value
                    for key, value in audit_summary.items()
                    if key != "perSymbol"
                },
            )
        result = {
            "status": "PASS",
            "batchId": batch_id,
            "provider": "a-stock-data/tencent-public",
            "credentialsRequired": False,
            "baseProvider": str(base),
            "baseProviderFingerprint": base_contract["fingerprint"],
            "market": request.market,
            "universeAsOf": base_end,
            "targetDates": target_dates,
            "adjustmentEvidencePolicy": adjustment_evidence_policy,
            "sourceReplay": source_replay,
            "codes": len(fetch_codes),
            "rawDir": str(raw_root),
            "manifestPath": str(manifest_file),
            "reportPath": str(report_file),
            "outputProvider": provider_result,
            "audit": {
                key: value for key, value in audit_summary.items() if key != "perSymbol"
            },
            "startedAt": started_at,
            "finishedAt": datetime.now(timezone.utc).isoformat(),
        }
        _write_json(report_file, result)
        return result
    except Exception as exc:
        failure = {
            "status": "FAIL",
            "batchId": batch_id,
            "provider": "a-stock-data/tencent-public",
            "credentialsRequired": False,
            "baseProvider": str(base),
            "baseProviderFingerprint": base_contract["fingerprint"],
            "market": request.market,
            "targetDates": target_dates,
            "adjustmentEvidencePolicy": adjustment_evidence_policy,
            "sourceReplay": source_replay,
            "rawDir": str(raw_root),
            "manifestPath": str(manifest_file) if manifest_file.exists() else None,
            "reportPath": str(report_file),
            "outputProvider": (
                {
                    "path": str(output),
                    "publicationState": "UNCONFIRMED_REPORT_FAILURE",
                }
                if output and output.exists()
                else None
            ),
            "reason": str(exc),
            "startedAt": started_at,
            "finishedAt": datetime.now(timezone.utc).isoformat(),
        }
        _write_json(report_file, failure)
        return failure
