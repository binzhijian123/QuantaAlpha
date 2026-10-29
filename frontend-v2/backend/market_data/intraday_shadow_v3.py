"""Decision-target quality gate for the future-only intraday shadow run."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from .intraday_capture import BENCHMARK_SYMBOL
from .intraday_shadow import PROJECT_ROOT, _rank_signals, load_intraday_shadow_declaration
from .intraday_shadow_v2 import IntradayShadowCoordinator as V2IntradayShadowCoordinator


DEFAULT_DECLARATION = (
    PROJECT_ROOT / "configs" / "intraday_shadow_20260814_t1_reversal_v3.json"
)
DECISION_TARGET_CONTRACT = {"requireDecisionQuotesForAllTargets": True}


class IntradayShadowCoordinator(V2IntradayShadowCoordinator):
    """Require every ranked target to be usable at decision and execution time."""

    def __init__(self, declaration_path: Path | str = DEFAULT_DECLARATION):
        declaration, _ = load_intraday_shadow_declaration(declaration_path)
        extension = declaration.get("qualityExtension")
        if not isinstance(extension, Mapping):
            raise ValueError("intraday-shadow v3 decision-target contract is missing")
        if dict(extension) != DECISION_TARGET_CONTRACT:
            raise ValueError("intraday-shadow v3 decision-target contract drifted")
        self.quality_extension = dict(DECISION_TARGET_CONTRACT)
        super().__init__(declaration_path)

    def _quality_quote_map(
        self,
        snapshot: Mapping[str, Any],
        *,
        label: str,
    ) -> dict[str, dict[str, Any]]:
        usable = super()._quality_quote_map(snapshot, label=label)
        if label != "decision":
            return usable

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
