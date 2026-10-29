"""Core-field fallback contract for the future-only intraday shadow run."""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any, Mapping

from .intraday_capture import BENCHMARK_SYMBOL
from .intraday_shadow import PROJECT_ROOT, _rank_signals, load_intraday_shadow_declaration
from .intraday_shadow_v2 import IntradayShadowCoordinator as V2IntradayShadowCoordinator
from .intraday_shadow_v3 import IntradayShadowCoordinator as V3IntradayShadowCoordinator


DEFAULT_DECLARATION = (
    PROJECT_ROOT / "configs" / "intraday_shadow_20260814_t1_reversal_v4.json"
)
QUALITY_EXTENSION_CONTRACT = {
    "requireDecisionQuotesForAllTargets": True,
    "requireCoreFieldAwareFallback": True,
    "benchmarkTurnoverRequired": False,
}


class IntradayShadowCoordinator(V3IntradayShadowCoordinator):
    """Preserve v3 target gates while making benchmark turnover explicitly optional."""

    def __init__(self, declaration_path: Path | str = DEFAULT_DECLARATION):
        declaration, _ = load_intraday_shadow_declaration(declaration_path)
        extension = declaration.get("qualityExtension")
        if not isinstance(extension, Mapping):
            raise ValueError("intraday-shadow v4 quality extension is missing")
        if dict(extension) != QUALITY_EXTENSION_CONTRACT:
            raise ValueError("intraday-shadow v4 quality extension drifted")
        self.quality_extension = dict(QUALITY_EXTENSION_CONTRACT)
        V2IntradayShadowCoordinator.__init__(self, declaration_path)

    def _quality_quote_map(
        self,
        snapshot: Mapping[str, Any],
        *,
        label: str,
    ) -> dict[str, dict[str, Any]]:
        normalized_quotes: list[Any] = []
        original_benchmark: dict[str, Any] | None = None
        for raw in snapshot.get("quotes") or []:
            if not isinstance(raw, Mapping):
                normalized_quotes.append(raw)
                continue
            quote = dict(raw)
            if quote.get("symbol") == BENCHMARK_SYMBOL:
                original_benchmark = dict(quote)
                try:
                    turnover = float(quote.get("turnoverPercent"))
                except (TypeError, ValueError):
                    turnover = math.nan
                if not math.isfinite(turnover) or turnover < 0:
                    # V2 validates one common field set. V4 uses a transient zero only
                    # for that validator; the persisted and returned quote stays unchanged.
                    quote["turnoverPercent"] = 0.0
            normalized_quotes.append(quote)

        normalized = {**dict(snapshot), "quotes": normalized_quotes}
        usable = V2IntradayShadowCoordinator._quality_quote_map(
            self,
            normalized,
            label=label,
        )
        if original_benchmark is not None and BENCHMARK_SYMBOL in usable:
            usable[BENCHMARK_SYMBOL] = original_benchmark

        if label == "decision":
            ranked = _rank_signals(
                snapshot,
                top_k=int(self.declaration["strategy"]["topK"]),
            )
            required_symbols = {BENCHMARK_SYMBOL}
            for arm in ranked.values():
                required_symbols.update(row["symbol"] for row in arm)
            missing_targets = sorted(required_symbols - set(usable))
            if missing_targets:
                raise ValueError(
                    "intraday-shadow decision snapshot lacks usable target quotes: "
                    f"{missing_targets[:5]}"
                )
        return usable
