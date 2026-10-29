"""Independent TCP fallback for strategy-critical intraday quote fields."""

from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime
from importlib import metadata as importlib_metadata
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo

from .a_stock_data import _usable_realtime_quote, normalize_stock_code
from .a_stock_data_intraday_v2 import (
    BENCHMARK_SYMBOL,
    CoreFieldRealtimeClient,
    _core_quote_usable,
    _finite_number,
)


CHINA_TZ = ZoneInfo("Asia/Shanghai")
MAX_TDX_QUOTE_BATCH_SIZE = 80
INDEPENDENT_TRANSPORT_FALLBACK_CONTRACT = {
    "version": 1,
    "sourceOrder": ["tencent", "easy_tdx", "eastmoney", "sina"],
    "equityCoreFields": ["price", "previousClose", "turnoverPercent"],
    "benchmarkCoreFields": ["price", "previousClose"],
    "easyTdxTransport": "TCP/7709",
    "easyTdxPackageVersion": "1.20.6",
}
REALTIME_CONTRACT_SHA256 = hashlib.sha256(
    json.dumps(
        INDEPENDENT_TRANSPORT_FALLBACK_CONTRACT,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
).hexdigest()


def _require_easy_tdx_package_version() -> None:
    expected = INDEPENDENT_TRANSPORT_FALLBACK_CONTRACT["easyTdxPackageVersion"]
    try:
        installed = importlib_metadata.version("easy-tdx")
    except importlib_metadata.PackageNotFoundError as exc:
        raise RuntimeError(f"easy-tdx {expected} is not installed") from exc
    if installed != expected:
        raise RuntimeError(
            f"easy-tdx package version mismatch: expected {expected}, got {installed}"
        )


def _tdx_quote_timestamp(date_value: Any, time_value: Any) -> str | None:
    date_number = _finite_number(date_value)
    time_number = _finite_number(time_value)
    if date_number is None or time_number is None:
        return None
    date_text = str(int(date_number))
    time_text = str(int(time_number)).zfill(6)
    if len(date_text) != 8 or len(time_text) != 6:
        return None
    try:
        value = datetime.strptime(date_text + time_text, "%Y%m%d%H%M%S")
    except ValueError:
        return None
    return value.replace(tzinfo=CHINA_TZ).isoformat()


def normalize_easy_tdx_quote_rows(
    rows: Sequence[Mapping[str, Any]],
    requested_symbols: set[str],
) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        code = str(row.get("code") or "").zfill(6)
        try:
            market = int(row.get("market"))
        except (TypeError, ValueError):
            continue
        if len(code) != 6 or not code.isdigit() or market not in {0, 1}:
            continue
        symbol = f"{code}.{'SH' if market == 1 else 'SZ'}"
        if symbol not in requested_symbols:
            continue

        price = _finite_number(row.get("close"))
        previous_close = _finite_number(row.get("pre_close"))
        volume = _finite_number(row.get("vol"))
        amount = _finite_number(row.get("amount"))
        turnover = _finite_number(row.get("turnover"))
        quote_as_of = _tdx_quote_timestamp(
            row.get("server_update_date"),
            row.get("server_update_time"),
        )
        change = (
            price - previous_close
            if price is not None and previous_close is not None
            else None
        )
        change_percent = (
            change / previous_close * 100.0
            if change is not None and previous_close not in (None, 0.0)
            else None
        )
        stale = bool(
            price is not None
            and price > 0
            and previous_close is not None
            and math.isclose(price, previous_close, rel_tol=0, abs_tol=1e-12)
            and (volume is None or volume == 0)
        )
        result[symbol] = {
            "symbol": symbol,
            "name": str(row.get("name") or ""),
            "price": price,
            "previousClose": previous_close,
            "open": _finite_number(row.get("open")),
            "high": _finite_number(row.get("high")),
            "low": _finite_number(row.get("low")),
            "change": change,
            "changePercent": change_percent,
            "amountCny": amount,
            "turnoverPercent": turnover,
            "quoteAsOf": quote_as_of,
            "tradeDate": quote_as_of[:10] if quote_as_of else None,
            "isStale": stale,
            "staleReason": (
                "成交量为零且现价等于昨收，可能未开盘、停牌或代码已失效"
                if stale
                else None
            ),
            "source": "a-stock-data/easy-tdx-public",
        }
    return result


class IndependentTransportRealtimeClient(CoreFieldRealtimeClient):
    """Use an independently transported TCP quote before HTTP-only fallbacks."""

    core_field_contract = INDEPENDENT_TRANSPORT_FALLBACK_CONTRACT

    def __init__(
        self,
        *args: Any,
        tdx_hosts: Sequence[str] | None = None,
        tdx_ping_timeout: float = 2.0,
        tdx_query_timeout: float = 4.0,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self.tdx_hosts = tuple(tdx_hosts) if tdx_hosts is not None else None
        self.tdx_ping_timeout = min(5.0, max(0.5, float(tdx_ping_timeout)))
        self.tdx_query_timeout = min(8.0, max(1.0, float(tdx_query_timeout)))

    def _easy_tdx_realtime_quotes(
        self,
        requested: list[tuple[str, str]],
    ) -> dict[str, dict[str, Any]]:
        _require_easy_tdx_package_version()
        try:
            from easy_tdx import MacClient
            from easy_tdx.codec.bitmap import FieldBit, PresetField
            from easy_tdx.config import get_mac_hosts
        except ImportError as exc:
            raise RuntimeError("easy-tdx 1.20.6 is not installed") from exc

        hosts = list(self.tdx_hosts or get_mac_hosts())
        ranked = MacClient.ping_all(
            hosts=hosts,
            timeout=self.tdx_ping_timeout,
        )
        if not ranked:
            raise RuntimeError("no easy-tdx MAC quote host is reachable")

        fields = (
            PresetField.BASIC
            + PresetField.VOLUME
            + FieldBit.SERVER_UPDATE_DATE
            + FieldBit.SERVER_UPDATE_TIME
        )
        stocks = [
            (1 if symbol.endswith(".SH") else 0, symbol[:6])
            for symbol, _ in requested
        ]
        rows: list[dict[str, Any]] = []
        best_host = str(ranked[0][0])
        with MacClient(
            best_host,
            timeout=self.tdx_query_timeout,
            auto_reconnect=False,
            heartbeat_interval=0,
        ) as client:
            for offset in range(0, len(stocks), MAX_TDX_QUOTE_BATCH_SIZE):
                frame = client.get_stock_quotes(
                    stocks[offset : offset + MAX_TDX_QUOTE_BATCH_SIZE],
                    fields,
                )
                rows.extend(frame.to_dict(orient="records"))
        result = normalize_easy_tdx_quote_rows(
            rows,
            {symbol for symbol, _ in requested},
        )
        if requested and not result:
            raise RuntimeError("easy-tdx returned no quote rows")
        return result

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
            symbol = f"{digits}.{exchange}"
            if symbol in seen:
                continue
            seen.add(symbol)
            requested.append((symbol, market_code))
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
                ingest(self._easy_tdx_realtime_quotes(missing))
            except Exception as exc:
                errors.append(f"EasyTdx={exc}")

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
                f"a-stock-data independent-transport real-time sources failed: {detail}"
            )
        for quote in result.values():
            quote["realtimeContractSha256"] = REALTIME_CONTRACT_SHA256
        return result
