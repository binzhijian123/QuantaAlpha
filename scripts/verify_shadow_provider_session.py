#!/usr/bin/env python3
"""Read-only daily-bar completeness audit; not a provenance or release approval."""
from __future__ import annotations

import argparse
from datetime import date
import hashlib
import json
import math
from pathlib import Path
import struct
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "frontend-v2/backend"))
from market_data.intraday_capture import _provider_context, BENCHMARK_SYMBOL

FIELDS = ("open", "high", "low", "close", "volume", "factor")


def _value(path: Path, session_index: int) -> tuple[float, str]:
    raw = path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    if len(raw) < 8 or len(raw) % 4:
        raise ValueError("INVALID_FEATURE_BINARY")
    start = struct.unpack_from("<f", raw)[0]
    if not math.isfinite(start) or start < 0 or not start.is_integer():
        raise ValueError("INVALID_FEATURE_OFFSET")
    index = session_index - int(start) + 1
    if index < 1 or index * 4 + 4 > len(raw):
        raise ValueError("MISSING_SESSION_VALUE")
    value = struct.unpack_from("<f", raw, index * 4)[0]
    if not math.isfinite(value):
        raise ValueError("NONFINITE_SESSION_VALUE")
    return value, digest


def verify_session(provider: Path, session: str) -> dict:
    if date.fromisoformat(session).isoformat() != session:
        raise ValueError("INVALID_SESSION_DATE")
    provider = provider.expanduser().resolve()
    context = _provider_context(provider)
    # Auditing the current frozen pool is meaningful only on its exact as-of day.
    if session != context["providerLatestDate"]:
        raise ValueError("SESSION_MUST_MATCH_PROVIDER_AS_OF")
    calendar = [line.strip() for line in (provider / "calendars/day.txt").read_text().splitlines() if line.strip()]
    session_index = calendar.index(session)
    failures = []
    checked = []
    valid_equities = 0
    benchmark_valid = False
    for symbol in context["symbols"]:
        code = (symbol[-2:] + symbol[:6]).lower()
        values = {}
        reasons = []
        for field in FIELDS:
            path = provider / "features" / code / f"{field}.day.bin"
            try:
                value, digest = _value(path, session_index)
                values[field] = value
                checked.append((f"features/{code}/{field}.day.bin", digest))
            except FileNotFoundError:
                reasons.append(f"{field}:MISSING_FEATURE_FILE")
            except ValueError as exc:
                reasons.append(f"{field}:{exc}")
        if len(values) == len(FIELDS):
            if min(values[k] for k in ("open", "high", "low", "close", "factor")) <= 0:
                reasons.append("NONPOSITIVE_PRICE_OR_FACTOR")
            if values["volume"] < 0:
                reasons.append("NEGATIVE_VOLUME")
            if not values["low"] <= min(values["open"], values["close"]) <= max(values["open"], values["close"]) <= values["high"]:
                reasons.append("INCONSISTENT_OHLC")
        if reasons:
            failures.append({"symbol": symbol, "reasons": reasons})
        elif symbol == BENCHMARK_SYMBOL:
            benchmark_valid = True
        else:
            valid_equities += 1
    body = {
        "kind": "shadow_daily_session_completeness", "schemaVersion": 1,
        "status": "PASS" if not failures else "FAIL", "session": session,
        "providerAsOf": context["providerLatestDate"],
        "universeSymbolsSha256": context["symbolsSha256"],
        "requiredSymbolCount": 301, "validEquityCount": valid_equities,
        "benchmarkValid": benchmark_valid, "fields": list(FIELDS),
        "checkedFeatureDigest": hashlib.sha256(json.dumps(sorted(checked), separators=(",", ":")).encode()).hexdigest(),
        "failures": failures, "priceValuesIncluded": False,
        "provenanceVerified": False, "lineageVerified": False,
        "autoShadowAccepted": False, "profitEvidenceEligible": False,
    }
    body["reportSha256"] = hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return body


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", type=Path, required=True)
    parser.add_argument("--session", required=True)
    args = parser.parse_args()
    try:
        report = verify_session(args.provider, args.session)
    except Exception:
        print(json.dumps({"status": "FAIL", "reason": "INVALID_OR_UNREADABLE_PROVIDER", "autoShadowAccepted": False}))
        return 1
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
