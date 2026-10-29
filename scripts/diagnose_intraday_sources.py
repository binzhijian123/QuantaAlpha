#!/usr/bin/env python3
"""Read-only diagnostics through the frozen V3 quote fallback path."""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime
import json
from pathlib import Path
import sys
from typing import Any, Callable
from zoneinfo import ZoneInfo

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "frontend-v2" / "backend"))

from market_data.intraday_capture import read_verified_snapshot
from market_data.providers.a_stock_data import _usable_realtime_quote
from market_data.providers.a_stock_data_intraday_v2 import _core_quote_usable
from market_data.providers.a_stock_data_intraday_v3 import IndependentTransportRealtimeClient

CHINA_TZ = ZoneInfo("Asia/Shanghai")


def summarize_rows(rows, *, expected_day: str, reference: datetime) -> dict[str, Any]:
    reasons: Counter[str] = Counter()
    for symbol, quote in rows.items():
        options = dict(expected_trade_date=expected_day, max_quote_age_seconds=120, now=reference)
        if _core_quote_usable(quote, symbol=symbol, **options):
            reasons["usable"] += 1
        elif quote.get("isStale"):
            reasons["flaggedStale"] += 1
        elif quote.get("tradeDate") != expected_day:
            reasons["wrongTradeDate"] += 1
        else:
            try:
                timestamp = datetime.fromisoformat(str(quote.get("quoteAsOf")))
                if timestamp.tzinfo is None:
                    raise ValueError("timestamp lacks timezone")
                age = (reference - timestamp).total_seconds()
            except (TypeError, ValueError):
                reasons["invalidTimestamp"] += 1
                continue
            if age < -5:
                reasons["futureQuote"] += 1
            elif age > 120:
                reasons["expiredQuote"] += 1
            elif not _usable_realtime_quote(quote, **options):
                reasons["invalidPrice"] += 1
            else:
                reasons["missingCoreFields"] += 1
    return {"rows": len(rows), "reasons": dict(sorted(reasons.items()))}


class DiagnosticClient(IndependentTransportRealtimeClient):
    """Observe source boundaries without changing accepted rows or fallbacks."""

    def __init__(self, *, clock: Callable[[], datetime] | None = None, **kwargs):
        super().__init__(**kwargs)
        self.clock = clock or (lambda: datetime.now(CHINA_TZ))
        self.source_reports: list[dict[str, Any]] = []

    def _observe(self, name, source, requested, **kwargs):
        started = self.clock()
        report = {"source": name, "requested": len(requested), "startedAt": started.isoformat()}
        try:
            rows = source(requested, **kwargs)
        except Exception as exc:
            failed = self.clock()
            report.update({
                "errorType": type(exc).__name__, "failedAt": failed.isoformat(),
                "durationSeconds": (failed - started).total_seconds(),
            })
            raise
        else:
            received = self.clock()
            report.update({
                "receivedAt": received.isoformat(),
                "durationSeconds": (received - started).total_seconds(),
                "atRequestStart": summarize_rows(rows, expected_day=self.expected_day, reference=self.reference),
                "atResponse": summarize_rows(rows, expected_day=self.expected_day, reference=received),
            })
            return rows
        finally:
            self.source_reports.append(report)

    def _tencent_realtime_quotes(self, requested):
        return self._observe("tencent", super()._tencent_realtime_quotes, requested)

    def _easy_tdx_realtime_quotes(self, requested):
        return self._observe("easy_tdx", super()._easy_tdx_realtime_quotes, requested)

    def _eastmoney_realtime_quotes(self, requested, **kwargs):
        return self._observe("eastmoney", super()._eastmoney_realtime_quotes, requested, **kwargs)

    def _sina_realtime_quotes(self, requested):
        return self._observe("sina", super()._sina_realtime_quotes, requested)

    def diagnose(self, symbols):
        self.source_reports = []
        self.reference = self.clock()
        self.expected_day = self.reference.astimezone(CHINA_TZ).date().isoformat()
        error_type = None
        result = {}
        try:
            result = self.realtime_quotes(
                symbols, expected_trade_date=self.expected_day,
                max_quote_age_seconds=120, now=self.reference,
            )
        except Exception as exc:
            error_type = type(exc).__name__
        return {
            "kind": "intraday_source_diagnostic", "schemaVersion": 1,
            "usageRealm": "DIAGNOSTIC_ONLY", "evidenceEligible": False,
            "startedAt": self.reference.isoformat(), "completedAt": self.clock().isoformat(),
            "requested": len(set(symbols)), "returned": len(result),
            "errorType": error_type, "sources": self.source_reports,
            "limitations": [
                "Only invoked fallback sources are observed; Eastmoney may filter rows internally.",
                "A probe describes its own requests, not the cause of historical failed slots.",
                "No raw quote, credential, exception body, snapshot, or ledger is stored.",
            ],
        }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reference-snapshot", required=True, type=Path)
    args = parser.parse_args()
    snapshot = read_verified_snapshot(args.reference_snapshot)
    report = DiagnosticClient(
        timeout=8.0, tdx_ping_timeout=2.0, tdx_query_timeout=4.0,
    ).diagnose(snapshot["universe"]["symbols"])
    print(json.dumps(report, indent=2, ensure_ascii=True))
    return 1 if report["errorType"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
