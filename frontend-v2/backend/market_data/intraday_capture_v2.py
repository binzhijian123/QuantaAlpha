"""Versioned capture entry point using core-field-aware source fallback."""

from __future__ import annotations

from typing import Any

from .intraday_capture import run_capture_cycle as _run_capture_cycle
from .providers.a_stock_data_intraday_v2 import CoreFieldRealtimeClient


def run_capture_cycle(
    *args: Any,
    client: Any | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    quote_client = client if client is not None else CoreFieldRealtimeClient(timeout=8.0)
    return _run_capture_cycle(*args, client=quote_client, **kwargs)
