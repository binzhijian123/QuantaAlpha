"""Quality-gated v2 coordinator for the future-only intraday shadow run."""

from __future__ import annotations

import math
from datetime import date, datetime
from pathlib import Path
from typing import Any, Mapping

from .intraday_capture import BENCHMARK_SYMBOL, _usable_capture_quote
from .intraday_shadow import (
    PROJECT_ROOT,
    SHANGHAI_TZ,
    IntradayShadowCoordinator as V1IntradayShadowCoordinator,
    _parse_clock,
    _rank_signals,
    load_intraday_shadow_declaration,
)


DEFAULT_DECLARATION = (
    PROJECT_ROOT / "configs" / "intraday_shadow_20260814_t1_reversal_v2.json"
)
QUALITY_CONTRACT_VERSION = 1


def _load_quality_contract(declaration: Mapping[str, Any]) -> dict[str, Any]:
    quality = declaration.get("quality")
    if not isinstance(quality, Mapping):
        raise ValueError("intraday-shadow v2 quality contract is missing")
    expected = {
        "version": QUALITY_CONTRACT_VERSION,
        "minimumCoreFieldCoverageRatio": 0.95,
        "requiredUniverseSize": 301,
        "maximumQuoteAgeSeconds": 120,
        "maximumFutureQuoteLeadSeconds": 5,
        "requireBenchmarkUsable": True,
        "requireExecutionQuotesForAllTargets": True,
    }
    if dict(quality) != expected:
        raise ValueError("intraday-shadow v2 quality contract drifted")
    return expected


class IntradayShadowCoordinator(V1IntradayShadowCoordinator):
    """Add pre-freeze data quality gates while preserving the v1 ledger engine."""

    def __init__(self, declaration_path: Path | str = DEFAULT_DECLARATION):
        declaration, _ = load_intraday_shadow_declaration(declaration_path)
        self.quality = _load_quality_contract(declaration)
        super().__init__(declaration_path)

    def _quality_quote_map(
        self,
        snapshot: Mapping[str, Any],
        *,
        label: str,
    ) -> dict[str, dict[str, Any]]:
        trade_date = str(snapshot.get("quoteTradeDate") or "")
        try:
            available_at = datetime.fromisoformat(str(snapshot["availableAt"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(
                f"intraday-shadow {label} snapshot has invalid availability time"
            ) from exc
        if available_at.tzinfo is None:
            raise ValueError(
                f"intraday-shadow {label} snapshot availability lacks timezone"
            )
        available_at = available_at.astimezone(SHANGHAI_TZ)

        universe = snapshot.get("universe") or {}
        symbols = universe.get("symbols") or []
        if (
            not isinstance(symbols, list)
            or len(symbols) != int(self.quality["requiredUniverseSize"])
            or len(set(symbols)) != len(symbols)
            or BENCHMARK_SYMBOL not in symbols
        ):
            raise ValueError(
                f"intraday-shadow {label} snapshot universe does not meet the quality contract"
            )
        symbol_set = set(symbols)

        quote_map: dict[str, dict[str, Any]] = {}
        usable: dict[str, dict[str, Any]] = {}
        for raw in snapshot.get("quotes") or []:
            if not isinstance(raw, Mapping):
                continue
            symbol = str(raw.get("symbol") or "")
            if not symbol or symbol not in symbol_set:
                raise ValueError(
                    f"intraday-shadow {label} snapshot contains an unexpected symbol"
                )
            if symbol in quote_map:
                raise ValueError(
                    f"intraday-shadow {label} snapshot contains duplicate quotes"
                )
            quote = dict(raw)
            quote_map[symbol] = quote
            if not _usable_capture_quote(
                quote,
                trade_date=trade_date,
                available_at=available_at,
                max_age_seconds=int(self.quality["maximumQuoteAgeSeconds"]),
                max_future_lead_seconds=int(
                    self.quality["maximumFutureQuoteLeadSeconds"]
                ),
            ):
                continue
            try:
                previous_close = float(quote["previousClose"])
                turnover = float(quote["turnoverPercent"])
            except (KeyError, TypeError, ValueError):
                continue
            if (
                not math.isfinite(previous_close)
                or previous_close <= 0
                or not math.isfinite(turnover)
                or turnover < 0
            ):
                continue
            usable[symbol] = quote

        coverage = snapshot.get("coverage") or {}
        required_count = int(self.quality["requiredUniverseSize"])
        minimum_ratio = float(self.quality["minimumCoreFieldCoverageRatio"])
        try:
            requested_count = int(coverage["requestedCount"])
            reported_usable_count = int(coverage["usableQuoteCount"])
            reported_usable_ratio = float(coverage["usableCoverageRatio"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(
                f"intraday-shadow {label} snapshot lacks valid coverage metadata"
            ) from exc
        if requested_count != required_count:
            raise ValueError(
                f"intraday-shadow {label} snapshot requested-count drifted"
            )
        minimum_count = math.ceil(required_count * minimum_ratio)
        if (
            reported_usable_count < minimum_count
            or not math.isfinite(reported_usable_ratio)
            or reported_usable_ratio < minimum_ratio
            or len(usable) < minimum_count
        ):
            raise ValueError(
                f"intraday-shadow {label} snapshot failed the 95% quality gate"
            )
        if self.quality["requireBenchmarkUsable"] and BENCHMARK_SYMBOL not in usable:
            raise ValueError(
                f"intraday-shadow {label} snapshot lacks a usable benchmark quote"
            )
        return usable

    def freeze_session(
        self,
        trade_date: str,
        *,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        parsed = date.fromisoformat(trade_date).isoformat()
        if parsed < str(self.declaration["firstEligibleSignalDate"]):
            raise ValueError(
                "intraday-shadow forbids pre-declaration or calibration-day signals"
            )
        local_now = (now or datetime.now(SHANGHAI_TZ)).astimezone(SHANGHAI_TZ)
        timing = self.declaration["timing"]
        freeze_after = datetime.combine(
            date.fromisoformat(parsed),
            _parse_clock(timing["freezeAfter"], "freezeAfter"),
            SHANGHAI_TZ,
        )
        freeze_deadline = datetime.combine(
            date.fromisoformat(parsed),
            _parse_clock(timing["freezeDeadline"], "freezeDeadline"),
            SHANGHAI_TZ,
        )
        if local_now < freeze_after:
            raise ValueError("intraday-shadow signal cannot freeze before freezeAfter")
        if local_now > freeze_deadline or local_now.date().isoformat() != parsed:
            raise ValueError(
                "intraday-shadow signal freeze deadline passed; backfill is forbidden"
            )

        _, decision = self._snapshot_at(
            parsed,
            timing["decisionSlot"],
            available_by=local_now,
        )
        _, execution = self._snapshot_at(
            parsed,
            timing["executionSlot"],
            available_by=local_now,
        )
        if decision.get("universe") != execution.get("universe"):
            raise ValueError("intraday-shadow decision and execution universes differ")
        if decision.get("implementation") != execution.get("implementation"):
            raise ValueError(
                "intraday-shadow capture implementation drifted within session"
            )
        self._quality_quote_map(decision, label="decision")
        execution_usable = self._quality_quote_map(execution, label="execution")
        ranked = _rank_signals(
            decision,
            top_k=int(self.declaration["strategy"]["topK"]),
        )
        required_execution_symbols = {BENCHMARK_SYMBOL}
        for arm in ranked.values():
            required_execution_symbols.update(row["symbol"] for row in arm)
        missing_targets = sorted(required_execution_symbols - set(execution_usable))
        if (
            self.quality["requireExecutionQuotesForAllTargets"]
            and missing_targets
        ):
            raise ValueError(
                "intraday-shadow execution snapshot lacks usable target quotes: "
                f"{missing_targets[:5]}"
            )
        return super().freeze_session(parsed, now=local_now)
