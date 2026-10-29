"""Provider-maintenance successor for the future-only intraday shadow protocol."""

from __future__ import annotations

from pathlib import Path

from .intraday_shadow import PROJECT_ROOT, load_intraday_shadow_declaration
from .intraday_shadow_v6 import (
    IntradayShadowCoordinator as V6IntradayShadowCoordinator,
)


PROTOCOL_ID = "intraday-shadow-t1-reversal-proxy-v8-provider-maintenance"
DEFAULT_DECLARATION = (
    PROJECT_ROOT / "configs" / "intraday_shadow_20260910_t1_reversal_v8.json"
)


class IntradayShadowCoordinator(V6IntradayShadowCoordinator):
    """Start clean evidence after V7 missed a causal session."""

    def __init__(self, declaration_path: Path | str = DEFAULT_DECLARATION):
        declaration, _ = load_intraday_shadow_declaration(declaration_path)
        if declaration.get("protocolId") != PROTOCOL_ID:
            raise ValueError("intraday-shadow v8 protocol identifier drifted")
        super().__init__(declaration_path)
