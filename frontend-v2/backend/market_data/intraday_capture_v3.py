"""Versioned capture entry point with an independent TCP core-field fallback."""

from __future__ import annotations

from typing import Any

from .intraday_capture import run_capture_cycle as _run_capture_cycle
from .providers.a_stock_data_intraday_v3 import IndependentTransportRealtimeClient


def run_capture_cycle(
    *args: Any,
    client: Any | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    quote_client = (
        client
        if client is not None
        else IndependentTransportRealtimeClient(
            timeout=8.0,
            tdx_ping_timeout=2.0,
            tdx_query_timeout=4.0,
        )
    )
    return _run_capture_cycle(*args, client=quote_client, **kwargs)
