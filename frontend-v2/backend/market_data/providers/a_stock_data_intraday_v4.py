"""Candidate V4 client: validate quote freshness after receiving each source."""

from __future__ import annotations

from datetime import datetime
import hashlib
import json
from typing import Callable

from .a_stock_data import _usable_realtime_quote, normalize_stock_code
from .a_stock_data_intraday_v2 import _core_quote_usable
from .a_stock_data_intraday_v3 import (
    CHINA_TZ,
    INDEPENDENT_TRANSPORT_FALLBACK_CONTRACT,
    IndependentTransportRealtimeClient,
)

RESPONSE_TIME_CONTRACT = {
    **INDEPENDENT_TRANSPORT_FALLBACK_CONTRACT,
    "version": 2,
    "freshnessReference": "source-response-received-at",
    "revalidateAtReturn": True,
    "requireTimezoneAwareQuote": True,
    "rejectBackwardClock": True,
}
REALTIME_CONTRACT_SHA256 = hashlib.sha256(json.dumps(
    RESPONSE_TIME_CONTRACT, sort_keys=True, ensure_ascii=True, separators=(",", ":"),
).encode("utf-8")).hexdigest()


class ResponseTimedRealtimeClient(IndependentTransportRealtimeClient):
    """Keep frozen V3 transports, with a separately identifiable timing contract.

    ``now`` is the caller's request-start lower bound, not a fixed quote reference.
    Tests should inject a clock shared with the collector to simulate elapsed I/O.
    """

    core_field_contract = RESPONSE_TIME_CONTRACT

    def __init__(self, *args, clock: Callable[[], datetime] | None = None, **kwargs):
        super().__init__(*args, **kwargs)
        self.clock = clock or (lambda: datetime.now(CHINA_TZ))

    def realtime_quotes(
        self, codes, *, expected_trade_date=None, max_quote_age_seconds=None, now=None,
    ):
        requested = {}
        for code in codes:
            digits, exchange, market = normalize_stock_code(code)
            requested[f"{digits}.{exchange}"] = market
        if not requested:
            return {}

        previous_time = now

        def receive_time():
            nonlocal previous_time
            current = self.clock()
            if current.tzinfo is None or (
                previous_time is not None and previous_time.tzinfo is None
            ):
                raise ValueError("intraday response clock must have a timezone")
            if previous_time is not None and current < previous_time:
                raise ValueError("intraday response clock moved backwards")
            previous_time = current
            return current

        def usable(quote, reference, *, symbol, core=False):
            try:
                timestamp = datetime.fromisoformat(str(quote.get("quoteAsOf")))
                if timestamp.tzinfo is None:
                    return False
            except (TypeError, ValueError):
                return False
            options = dict(expected_trade_date=expected_trade_date,
                           max_quote_age_seconds=max_quote_age_seconds, now=reference)
            if core:
                return _core_quote_usable(quote, symbol=symbol, **options)
            return _usable_realtime_quote(quote, **options)

        reference = receive_time()
        accepted = {}
        best_effort = {}
        errors = []
        sources = (
            ("tencent", self._tencent_realtime_quotes),
            ("easy_tdx", self._easy_tdx_realtime_quotes),
            ("eastmoney", lambda missing: self._eastmoney_realtime_quotes(
                missing, expected_trade_date=expected_trade_date,
                max_quote_age_seconds=max_quote_age_seconds, now=None,
            )),
            ("sina", self._sina_realtime_quotes),
        )
        for name, fetch in sources:
            missing = [(s, market) for s, market in requested.items() if s not in accepted]
            if not missing:
                break
            try:
                rows = fetch(missing)
            except Exception as exc:
                errors.append(f"{name}={type(exc).__name__}")
                rows = {}
            # Clock errors must not be swallowed as an ordinary source outage.
            reference = receive_time()
            missing_symbols = {s for s, _ in missing}
            for symbol, raw in rows.items():
                if symbol not in missing_symbols:
                    continue
                quote = dict(raw)
                if usable(quote, reference, symbol=symbol):
                    best_effort[symbol] = quote
                if usable(quote, reference, symbol=symbol, core=True):
                    accepted[symbol] = quote

        reference = receive_time()
        result = {}
        for symbol in requested:
            quote = accepted.get(symbol) or best_effort.get(symbol)
            if quote is not None and usable(quote, reference, symbol=symbol):
                result[symbol] = {**quote, "realtimeContractSha256": REALTIME_CONTRACT_SHA256}
        if not result:
            detail = "; ".join(errors) or "all rows were unusable"
            raise RuntimeError(f"response-timed real-time sources failed: {detail}")
        return result
