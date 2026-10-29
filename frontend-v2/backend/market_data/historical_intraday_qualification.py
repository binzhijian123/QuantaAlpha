from __future__ import annotations

import math
import time as time_module
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol

import pandas as pd

from .intraday_capture import read_verified_snapshot


MINUTE_FIELDS = (
    "ts_code",
    "trade_time",
    "open",
    "close",
    "high",
    "low",
    "vol",
    "amount",
)
PREMARKET_FIELDS = (
    "trade_date",
    "ts_code",
    "total_share",
    "float_share",
    "pre_close",
    "up_limit",
    "down_limit",
)
DEFAULT_SAMPLE_CODES = ("600519.SH", "000001.SZ", "300750.SZ")
DEFAULT_MINIMUM_COMMON_SESSIONS = 252
DEFAULT_MAX_TURNOVER_DIFFERENCE = 0.02


class TushareQueryClient(Protocol):
    def query(
        self,
        api_name: str,
        *,
        params: dict[str, Any] | None = None,
        fields: tuple[str, ...] = (),
    ) -> pd.DataFrame: ...


@dataclass(frozen=True)
class TushareIntradayQualificationRequest:
    codes: tuple[str, ...]
    start_date: date
    end_date: date
    reference_date: date
    reference_snapshot: Path
    minimum_common_sessions: int = DEFAULT_MINIMUM_COMMON_SESSIONS
    chunk_days: int = 120
    request_interval_seconds: float = 0.2
    max_turnover_difference: float = DEFAULT_MAX_TURNOVER_DIFFERENCE

    def __post_init__(self) -> None:
        normalized_codes = tuple(dict.fromkeys(code.strip().upper() for code in self.codes))
        if not normalized_codes or any(
            len(code) != 9 or code[-3:] not in {".SH", ".SZ"}
            for code in normalized_codes
        ):
            raise ValueError("qualification codes must be unique A-share TS codes")
        if normalized_codes != self.codes:
            raise ValueError("qualification codes must already be normalized and unique")
        if self.start_date > self.end_date:
            raise ValueError("qualification start date is after end date")
        if not self.start_date <= self.reference_date <= self.end_date:
            raise ValueError("reference date must be inside the requested date range")
        if self.minimum_common_sessions <= 0:
            raise ValueError("minimum common sessions must be positive")
        if not 1 <= self.chunk_days <= 160:
            raise ValueError("chunk days must be between 1 and 160")
        if self.request_interval_seconds < 0:
            raise ValueError("request interval cannot be negative")
        if not math.isfinite(self.max_turnover_difference) or self.max_turnover_difference <= 0:
            raise ValueError("maximum turnover difference must be positive and finite")


def _clock_range(start: time, end: time) -> tuple[time, ...]:
    anchor = date(2000, 1, 1)
    cursor = datetime.combine(anchor, start)
    finish = datetime.combine(anchor, end)
    values: list[time] = []
    while cursor <= finish:
        values.append(cursor.time())
        cursor += timedelta(minutes=5)
    return tuple(values)


# Regular five-minute bars must be present. A separate 09:30 auction bar is
# accepted and included when supplied, but is not assumed before the live probe.
EXPECTED_THROUGH_DECISION = frozenset(
    _clock_range(time(9, 35), time(11, 30))
    + _clock_range(time(13, 5), time(14, 50))
)


def _date_chunks(start: date, end: date, chunk_days: int) -> list[tuple[date, date]]:
    chunks: list[tuple[date, date]] = []
    cursor = start
    while cursor <= end:
        chunk_end = min(end, cursor + timedelta(days=chunk_days - 1))
        chunks.append((cursor, chunk_end))
        cursor = chunk_end + timedelta(days=1)
    return chunks


def _require_columns(frame: pd.DataFrame, fields: tuple[str, ...], name: str) -> None:
    missing = sorted(set(fields) - set(frame.columns))
    if missing:
        raise ValueError(f"{name} response is missing fields: {missing}")


def _numeric_column(
    frame: pd.DataFrame,
    field: str,
    *,
    positive: bool,
) -> pd.Series:
    values = pd.to_numeric(frame[field], errors="coerce")
    finite = values.map(lambda value: math.isfinite(float(value)) if pd.notna(value) else False)
    valid_range = values > 0 if positive else values >= 0
    if not bool((finite & valid_range).all()):
        qualifier = "positive" if positive else "non-negative"
        raise ValueError(f"{field} must contain only finite {qualifier} values")
    return values.astype(float)


def _is_market_timestamp(value: datetime) -> bool:
    stamp = value.time()
    if stamp.second or stamp.microsecond or stamp.minute % 5:
        return False
    return time(9, 30) <= stamp <= time(11, 30) or time(13, 0) <= stamp <= time(15, 0)


def normalize_tushare_intraday_symbol(
    code: str,
    minute_frame: pd.DataFrame,
    premarket_frame: pd.DataFrame,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Build decision-time rows without filling or using end-of-day fields."""

    _require_columns(minute_frame, MINUTE_FIELDS, "stk_mins")
    _require_columns(premarket_frame, PREMARKET_FIELDS, "stk_premarket")
    minutes = minute_frame.loc[:, MINUTE_FIELDS].copy()
    premarket = premarket_frame.loc[:, PREMARKET_FIELDS].copy()

    if not minutes.empty and set(minutes["ts_code"].astype(str)) != {code}:
        raise ValueError(f"stk_mins returned another symbol while requesting {code}")
    if not premarket.empty and set(premarket["ts_code"].astype(str)) != {code}:
        raise ValueError(f"stk_premarket returned another symbol while requesting {code}")

    minutes["trade_time"] = pd.to_datetime(minutes["trade_time"], errors="coerce")
    if minutes["trade_time"].isna().any():
        raise ValueError("stk_mins contains invalid trade_time values")
    if minutes["trade_time"].duplicated().any():
        raise ValueError("stk_mins contains duplicate trade_time rows")
    if not minutes["trade_time"].map(_is_market_timestamp).all():
        raise ValueError("stk_mins contains timestamps outside the five-minute A-share sessions")
    for field in ("open", "close", "high", "low"):
        minutes[field] = _numeric_column(minutes, field, positive=True)
    minutes["vol"] = _numeric_column(minutes, "vol", positive=False)
    minutes["amount"] = _numeric_column(minutes, "amount", positive=False)
    invalid_ohlc = (
        (minutes["high"] < minutes[["open", "close", "low"]].max(axis=1))
        | (minutes["low"] > minutes[["open", "close", "high"]].min(axis=1))
    )
    if invalid_ohlc.any():
        raise ValueError("stk_mins contains inconsistent OHLC values")
    minutes["trade_date"] = minutes["trade_time"].dt.date
    minutes["clock"] = minutes["trade_time"].dt.time

    premarket["trade_date"] = pd.to_datetime(
        premarket["trade_date"].astype(str), format="%Y%m%d", errors="coerce"
    ).dt.date
    if premarket["trade_date"].isna().any():
        raise ValueError("stk_premarket contains invalid trade_date values")
    if premarket["trade_date"].duplicated().any():
        raise ValueError("stk_premarket contains duplicate trade dates")
    premarket["float_share"] = _numeric_column(
        premarket, "float_share", positive=True
    )
    premarket["pre_close"] = _numeric_column(premarket, "pre_close", positive=True)
    premarket_by_date = premarket.set_index("trade_date")

    rows: list[dict[str, Any]] = []
    invalid_dates: list[dict[str, Any]] = []
    auction_bar_dates = 0
    for trade_date, day_frame in minutes.groupby("trade_date", sort=True):
        clocks = set(day_frame["clock"])
        missing_regular = sorted(EXPECTED_THROUGH_DECISION - clocks)
        decision = day_frame.loc[day_frame["clock"] == time(14, 50)]
        execution = day_frame.loc[day_frame["clock"] == time(14, 55)]
        reasons: list[str] = []
        if missing_regular:
            reasons.append("missing_regular_five_minute_bars")
        if len(decision) != 1:
            reasons.append("decision_bar_count_not_one")
        if len(execution) != 1:
            reasons.append("execution_bar_count_not_one")
        if trade_date not in premarket_by_date.index:
            reasons.append("missing_premarket_row")
        if reasons:
            invalid_dates.append(
                {
                    "tradeDate": trade_date.isoformat(),
                    "reasons": reasons,
                    "missingRegularBarCount": len(missing_regular),
                }
            )
            continue

        through_decision = day_frame.loc[
            ((day_frame["clock"] >= time(9, 30)) & (day_frame["clock"] <= time(11, 30)))
            | ((day_frame["clock"] >= time(13, 0)) & (day_frame["clock"] <= time(14, 50)))
        ]
        has_auction_bar = time(9, 30) in clocks
        auction_bar_dates += int(has_auction_bar)
        premarket_row = premarket_by_date.loc[trade_date]
        cumulative_volume = float(through_decision["vol"].sum())
        float_share = float(premarket_row["float_share"])
        rows.append(
            {
                "trade_date": trade_date,
                "code": code,
                "decision_close": float(decision.iloc[0]["close"]),
                "execution_close": float(execution.iloc[0]["close"]),
                "pre_close": float(premarket_row["pre_close"]),
                "float_share_10k": float_share,
                "cumulative_volume_shares": cumulative_volume,
                "turnover_percent": cumulative_volume / (float_share * 10_000.0) * 100.0,
                "bars_through_decision": int(len(through_decision)),
                "has_auction_bar": has_auction_bar,
            }
        )

    daily = pd.DataFrame(
        rows,
        columns=(
            "trade_date",
            "code",
            "decision_close",
            "execution_close",
            "pre_close",
            "float_share_10k",
            "cumulative_volume_shares",
            "turnover_percent",
            "bars_through_decision",
            "has_auction_bar",
        ),
    )
    summary = {
        "code": code,
        "minuteRows": int(len(minutes)),
        "premarketRows": int(len(premarket)),
        "minuteDates": int(minutes["trade_date"].nunique()),
        "validSessions": int(len(daily)),
        "invalidSessions": int(len(invalid_dates)),
        "auctionBarSessions": auction_bar_dates,
        "barsThroughDecisionMin": (
            int(daily["bars_through_decision"].min()) if not daily.empty else 0
        ),
        "barsThroughDecisionMax": (
            int(daily["bars_through_decision"].max()) if not daily.empty else 0
        ),
        "invalidDateExamples": invalid_dates[:20],
    }
    return daily, summary


def _reference_crosscheck(
    daily_by_code: Mapping[str, pd.DataFrame],
    payload: Mapping[str, Any],
    request: TushareIntradayQualificationRequest,
) -> dict[str, Any]:
    capture_slot = datetime.fromisoformat(str(payload.get("captureSlot")))
    if capture_slot.date() != request.reference_date:
        raise ValueError("reference snapshot date does not match qualification request")
    quotes = payload.get("quotes")
    if not isinstance(quotes, list):
        raise ValueError("reference snapshot quotes must be a list")
    quote_by_code = {
        str(row.get("symbol")): row for row in quotes if isinstance(row, Mapping)
    }
    comparisons: list[dict[str, Any]] = []
    for code in request.codes:
        quote = quote_by_code.get(code)
        if quote is None:
            comparisons.append({"code": code, "passed": False, "reason": "missing_quote"})
            continue
        daily = daily_by_code[code]
        selected = daily.loc[daily["trade_date"] == request.reference_date]
        if len(selected) != 1:
            comparisons.append(
                {"code": code, "passed": False, "reason": "missing_reconstructed_session"}
            )
            continue
        row = selected.iloc[0]
        reference_previous_close = float(quote.get("previousClose"))
        reference_turnover = float(quote.get("turnoverPercent"))
        previous_close_difference = abs(float(row["pre_close"]) - reference_previous_close)
        turnover_difference = abs(float(row["turnover_percent"]) - reference_turnover)
        passed = (
            str(quote.get("tradeDate")) == request.reference_date.isoformat()
            and str(quote.get("source")) == "a-stock-data/tencent-public"
            and previous_close_difference <= 0.011
            and turnover_difference <= request.max_turnover_difference
        )
        comparisons.append(
            {
                "code": code,
                "passed": passed,
                "reconstructedPreviousClose": float(row["pre_close"]),
                "referencePreviousClose": reference_previous_close,
                "previousCloseAbsoluteDifference": previous_close_difference,
                "reconstructedTurnoverPercent": float(row["turnover_percent"]),
                "referenceTurnoverPercent": reference_turnover,
                "turnoverAbsoluteDifference": turnover_difference,
                "maxTurnoverAbsoluteDifference": request.max_turnover_difference,
                "referenceSource": quote.get("source"),
            }
        )
    return {
        "status": "PASS" if all(row["passed"] for row in comparisons) else "FAIL",
        "referenceDate": request.reference_date.isoformat(),
        "snapshotContentSha256": payload.get("contentSha256"),
        "comparisons": comparisons,
    }


def qualify_tushare_intraday_sample(
    client: TushareQueryClient,
    request: TushareIntradayQualificationRequest,
    *,
    snapshot_loader: Callable[[Path], Mapping[str, Any]] = read_verified_snapshot,
    sleep: Callable[[float], None] = time_module.sleep,
) -> dict[str, Any]:
    """Run a bounded sample probe; never writes or promotes market data."""

    chunks = _date_chunks(request.start_date, request.end_date, request.chunk_days)
    daily_by_code: dict[str, pd.DataFrame] = {}
    symbol_summaries: list[dict[str, Any]] = []
    queries: list[dict[str, Any]] = []
    row_limit_hit = False

    def pause() -> None:
        if request.request_interval_seconds:
            sleep(request.request_interval_seconds)

    for code in request.codes:
        minute_frames: list[pd.DataFrame] = []
        for chunk_start, chunk_end in chunks:
            frame = client.query(
                "stk_mins",
                params={
                    "ts_code": code,
                    "freq": "5min",
                    "start_date": f"{chunk_start.isoformat()} 00:00:00",
                    "end_date": f"{chunk_end.isoformat()} 23:59:59",
                },
                fields=MINUTE_FIELDS,
            )
            row_limit_hit = row_limit_hit or len(frame) >= 8_000
            minute_frames.append(frame)
            queries.append(
                {
                    "apiName": "stk_mins",
                    "code": code,
                    "startDate": chunk_start.isoformat(),
                    "endDate": chunk_end.isoformat(),
                    "rows": int(len(frame)),
                }
            )
            pause()
        premarket = client.query(
            "stk_premarket",
            params={
                "ts_code": code,
                "start_date": request.start_date.strftime("%Y%m%d"),
                "end_date": request.end_date.strftime("%Y%m%d"),
            },
            fields=PREMARKET_FIELDS,
        )
        row_limit_hit = row_limit_hit or len(premarket) >= 8_000
        queries.append(
            {
                "apiName": "stk_premarket",
                "code": code,
                "startDate": request.start_date.isoformat(),
                "endDate": request.end_date.isoformat(),
                "rows": int(len(premarket)),
            }
        )
        pause()
        minutes = pd.concat(minute_frames, ignore_index=True) if minute_frames else pd.DataFrame()
        daily, summary = normalize_tushare_intraday_symbol(code, minutes, premarket)
        daily_by_code[code] = daily
        symbol_summaries.append(summary)

    session_sets = [set(frame["trade_date"]) for frame in daily_by_code.values()]
    common_sessions = sorted(set.intersection(*session_sets)) if session_sets else []
    reference_payload = snapshot_loader(request.reference_snapshot)
    crosscheck = _reference_crosscheck(daily_by_code, reference_payload, request)
    coverage_passed = (
        len(common_sessions) >= request.minimum_common_sessions
        and all(
            summary["validSessions"] >= request.minimum_common_sessions
            for summary in symbol_summaries
        )
    )
    sample_passed = coverage_passed and not row_limit_hit and crosscheck["status"] == "PASS"
    return {
        "schemaVersion": 1,
        "kind": "tushare_historical_intraday_sample_qualification",
        "status": "SAMPLE_PASS" if sample_passed else "FAIL",
        "samplePassed": sample_passed,
        "sourceQualified": False,
        "adapterEligible": False,
        "profitEvidenceEligible": False,
        "writesMarketData": False,
        "request": {
            "codes": list(request.codes),
            "startDate": request.start_date.isoformat(),
            "endDate": request.end_date.isoformat(),
            "minimumCommonSessions": request.minimum_common_sessions,
            "chunkDays": request.chunk_days,
            "referenceSnapshot": str(request.reference_snapshot.expanduser().resolve()),
        },
        "coverage": {
            "passed": coverage_passed,
            "commonValidSessions": len(common_sessions),
            "firstCommonSession": common_sessions[0].isoformat() if common_sessions else None,
            "lastCommonSession": common_sessions[-1].isoformat() if common_sessions else None,
            "symbols": symbol_summaries,
        },
        "transport": {
            "queryCount": len(queries),
            "rowLimitHit": row_limit_hit,
            "queries": queries,
        },
        "referenceCrosscheck": crosscheck,
        "remainingSourceGates": [
            "current_stk_premarket_contract_covers_only_about_two_years_not_four_complete_years",
            "usage_terms_review",
            "full_point_in_time_csi300_capacity_probe",
            "immutable_raw_manifest_before_adapter",
            "profit_protocol_runs_only_after_source_contract_freeze",
        ],
    }
