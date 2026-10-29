"""Core-field-aware real-time fallback for the intraday strategy."""

from __future__ import annotations

import math
from datetime import datetime
from typing import Any, Mapping

from .a_stock_data import (
    AStockDataClient,
    _usable_realtime_quote,
    normalize_stock_code,
)


BENCHMARK_SYMBOL = "000300.SH"
CORE_FIELD_FALLBACK_CONTRACT = {
    "version": 1,
    "sourceOrder": ["tencent", "eastmoney", "sina"],
    "equityCoreFields": ["price", "previousClose", "turnoverPercent"],
    "benchmarkCoreFields": ["price", "previousClose"],
}


def _finite_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _core_quote_usable(
    quote: Mapping[str, Any] | None,
    *,
    symbol: str,
    expected_trade_date: str | None,
    max_quote_age_seconds: int | None,
    now: datetime | None,
) -> bool:
    if not _usable_realtime_quote(
        dict(quote) if quote is not None else None,
        expected_trade_date=expected_trade_date,
        max_quote_age_seconds=max_quote_age_seconds,
        now=now,
    ):
        return False
    previous_close = _finite_number(quote.get("previousClose"))
    if previous_close is None or previous_close <= 0:
        return False
    if symbol == BENCHMARK_SYMBOL:
        return True
    turnover = _finite_number(quote.get("turnoverPercent"))
    return turnover is not None and turnover >= 0


class CoreFieldRealtimeClient(AStockDataClient):
    """Continue fallback until each quote has the fields needed by the strategy."""

    core_field_contract = CORE_FIELD_FALLBACK_CONTRACT

    def realtime_quotes(
        self,
        codes: list[str],
        *,
        expected_trade_date: str | None = None,
        max_quote_age_seconds: int | None = None,
        now: datetime | None = None,
    ) -> dict[str, dict[str, Any]]:
        requested: list[tuple[str, str]] = []
        seen: set[str] = set()
        for code in codes:
            digits, exchange, market_code = normalize_stock_code(code)
            canonical_code = f"{digits}.{exchange}"
            if canonical_code in seen:
                continue
            seen.add(canonical_code)
            requested.append((canonical_code, market_code))
        if not requested:
            return {}

        accepted: dict[str, dict[str, Any]] = {}
        best_effort: dict[str, dict[str, Any]] = {}
        errors: list[str] = []

        def ingest(rows: Mapping[str, Mapping[str, Any]]) -> None:
            for symbol, raw in rows.items():
                if symbol not in seen:
                    continue
                quote = dict(raw)
                if _usable_realtime_quote(
                    quote,
                    expected_trade_date=expected_trade_date,
                    max_quote_age_seconds=max_quote_age_seconds,
                    now=now,
                ):
                    best_effort.setdefault(symbol, quote)
                if _core_quote_usable(
                    quote,
                    symbol=symbol,
                    expected_trade_date=expected_trade_date,
                    max_quote_age_seconds=max_quote_age_seconds,
                    now=now,
                ):
                    accepted[symbol] = quote

        def unresolved() -> list[tuple[str, str]]:
            return [item for item in requested if item[0] not in accepted]

        try:
            ingest(self._tencent_realtime_quotes(requested))
        except Exception as exc:
            errors.append(f"Tencent={exc}")

        missing = unresolved()
        if missing:
            try:
                ingest(
                    self._eastmoney_realtime_quotes(
                        missing,
                        expected_trade_date=expected_trade_date,
                        max_quote_age_seconds=max_quote_age_seconds,
                        now=now,
                    )
                )
            except Exception as exc:
                errors.append(f"Eastmoney={exc}")

        missing = unresolved()
        if missing:
            try:
                ingest(self._sina_realtime_quotes(missing))
            except Exception as exc:
                errors.append(f"Sina={exc}")

        result = {
            symbol: accepted.get(symbol) or best_effort[symbol]
            for symbol, _ in requested
            if symbol in accepted or symbol in best_effort
        }
        if not result:
            detail = "all rows were unusable"
            if errors:
                detail = f"{detail}; {'; '.join(errors)}"
            raise RuntimeError(
                f"a-stock-data core-field real-time sources failed: {detail}"
            )
        return result
