from __future__ import annotations

from datetime import datetime, time
from typing import Any, Mapping
from zoneinfo import ZoneInfo

from .providers.a_stock_data import AStockDataClient


CHINA_TZ = ZoneInfo("Asia/Shanghai")
BENCHMARK_SYMBOL = "000300.SH"
QUOTE_FRESHNESS_SECONDS = 120


def _market_phase(now: datetime) -> str:
    if now.weekday() >= 5:
        return "NON_TRADING_DAY"
    local_time = now.time().replace(tzinfo=None)
    if time(9, 15) <= local_time < time(9, 30):
        return "PRE_OPEN"
    if time(9, 30) <= local_time <= time(11, 30):
        return "MORNING"
    if time(11, 30) < local_time < time(13, 0):
        return "LUNCH"
    if time(13, 0) <= local_time <= time(15, 0):
        return "AFTERNOON"
    return "CLOSED"


def _parse_quote_time(value: object) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=CHINA_TZ)
    return parsed.astimezone(CHINA_TZ)


def build_paper_realtime_state(
    paper_state: Mapping[str, Any],
    official_latest_date: str,
    *,
    client: AStockDataClient | None = None,
    prefetched_quotes: Mapping[str, Mapping[str, Any]] | None = None,
    prefetched_error: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Build an in-memory quote overlay; it never writes prices or evidence."""
    local_now = (now or datetime.now(CHINA_TZ)).astimezone(CHINA_TZ)
    positions = list(paper_state.get("positions") or [])
    symbols = [str(position["symbol"]) for position in positions]
    requested_symbols = [*symbols, BENCHMARK_SYMBOL]
    retrieved_at = local_now.isoformat()

    if prefetched_quotes is not None:
        quotes = {
            symbol: dict(prefetched_quotes[symbol])
            for symbol in requested_symbols
            if symbol in prefetched_quotes
        }
        fetch_error = prefetched_error
    else:
        quote_client = client or AStockDataClient(timeout=8.0)
        try:
            quotes = quote_client.realtime_quotes(requested_symbols)
            fetch_error = None
        except Exception as exc:
            quotes = {}
            fetch_error = str(exc)

    quote_times = [
        parsed
        for quote in quotes.values()
        if (parsed := _parse_quote_time(quote.get("quoteAsOf"))) is not None
    ]
    latest_quote_time = max(quote_times) if quote_times else None
    quote_trade_date = latest_quote_time.date().isoformat() if latest_quote_time else None
    phase = _market_phase(local_now)
    active_phase = phase in {"PRE_OPEN", "MORNING", "AFTERNOON"}
    quote_age_by_symbol: dict[str, float | None] = {}
    delayed_symbols: list[str] = []
    for symbol in requested_symbols:
        quote_time = _parse_quote_time((quotes.get(symbol) or {}).get("quoteAsOf"))
        quote_age = (
            max(0.0, (local_now - quote_time).total_seconds())
            if quote_time is not None
            else None
        )
        quote_age_by_symbol[symbol] = quote_age
        if active_phase and (
            quote_age is None or quote_age > QUOTE_FRESHNESS_SECONDS
        ):
            delayed_symbols.append(symbol)
    delayed_symbol_set = set(delayed_symbols)
    age_seconds = (
        max(0.0, (local_now - latest_quote_time).total_seconds())
        if latest_quote_time is not None
        else None
    )
    delayed = bool(delayed_symbols)
    if fetch_error or not quotes:
        status = "UNAVAILABLE"
    elif delayed:
        status = "DELAYED"
    elif active_phase:
        status = "LIVE"
    else:
        status = "CLOSED"

    quote_sources = sorted(
        {
            str(quote["source"])
            for quote in quotes.values()
            if quote.get("source")
        }
    )
    if len(quote_sources) == 1:
        realtime_source = quote_sources[0]
    elif quote_sources:
        realtime_source = "a-stock-data/mixed"
    else:
        realtime_source = "a-stock-data/realtime-unavailable"

    account = dict(paper_state.get("account") or {})
    cash = float(account.get("cash") or 0.0)
    initial_cash = float(account.get("initialCash") or 0.0)
    official_total_value = float(account.get("totalValue") or cash)
    estimated_market_value = 0.0
    covered_positions = 0
    live_positions: list[dict[str, Any]] = []
    for position in positions:
        symbol = str(position["symbol"])
        quote = quotes.get(symbol) or {}
        live_price = quote.get("price")
        usable_live_price = bool(
            isinstance(live_price, (int, float))
            and live_price > 0
            and not quote.get("isStale")
            and symbol not in delayed_symbol_set
        )
        official_price = float(position.get("lastPrice") or 0.0)
        valuation_price = float(live_price) if usable_live_price else official_price
        quantity = float(position.get("quantity") or 0.0)
        market_value = quantity * valuation_price
        estimated_market_value += market_value
        covered_positions += int(usable_live_price)
        live_positions.append(
            {
                "symbol": symbol,
                "quantity": position.get("quantity"),
                "officialLastPrice": official_price,
                "livePrice": float(live_price) if usable_live_price else None,
                "valuationPrice": valuation_price,
                "marketValue": market_value,
                "priceSource": "realtime" if usable_live_price else "official_fallback",
                "quoteAsOf": quote.get("quoteAsOf"),
                "quoteAgeSeconds": quote_age_by_symbol.get(symbol),
                "isDelayed": symbol in delayed_symbol_set,
                "isStale": bool(quote.get("isStale")),
            }
        )

    estimated_total_value = cash + estimated_market_value
    if quote_trade_date is None:
        reconciliation_status = "QUOTE_UNAVAILABLE"
    elif quote_trade_date <= official_latest_date:
        reconciliation_status = "OFFICIAL_PROVIDER_AVAILABLE"
    else:
        reconciliation_status = "AWAITING_OFFICIAL_DAILY"

    return {
        "status": status,
        "source": realtime_source,
        "quoteSources": quote_sources,
        "retrievedAt": retrieved_at,
        "quoteAsOf": latest_quote_time.isoformat() if latest_quote_time else None,
        "quoteTradeDate": quote_trade_date,
        "marketPhase": phase,
        "ageSeconds": age_seconds,
        "delayed": delayed,
        "delayedSymbols": sorted(set(delayed_symbols)),
        "freshnessThresholdSeconds": QUOTE_FRESHNESS_SECONDS,
        "provisional": True,
        "persisted": False,
        "evidenceEligible": False,
        "pollIntervalSeconds": 30,
        "error": fetch_error,
        "officialLatestDate": official_latest_date,
        "reconciliationStatus": reconciliation_status,
        "account": {
            "cash": cash,
            "estimatedMarketValue": estimated_market_value,
            "estimatedTotalValue": estimated_total_value,
            "estimatedTotalReturn": (
                estimated_total_value / initial_cash - 1.0 if initial_cash > 0 else None
            ),
            "changeFromOfficial": estimated_total_value - official_total_value,
            "coveredPositions": covered_positions,
            "totalPositions": len(positions),
            "coverageComplete": covered_positions == len(positions),
        },
        "positions": live_positions,
        "benchmark": (
            {
                **quotes[BENCHMARK_SYMBOL],
                "quoteAgeSeconds": quote_age_by_symbol.get(BENCHMARK_SYMBOL),
                "isDelayed": BENCHMARK_SYMBOL in delayed_symbol_set,
            }
            if BENCHMARK_SYMBOL in quotes
            else None
        ),
        "quotes": [quotes[symbol] for symbol in sorted(quotes)],
    }
