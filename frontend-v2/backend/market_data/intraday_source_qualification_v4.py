"""Separate V4 engineering qualification; never changes a frozen trading run.

Inputs are immutable collector snapshots or raw close envelopes with the same
quotes/provider/universe/time fields. Raw envelopes must carry contentSha256 and
an archive .sha256 sidecar. Close comparison is not a realtime freshness proof.
"""
from __future__ import annotations

import gzip
import json
import math
import re
from datetime import datetime, time
from importlib import metadata
from pathlib import Path
from typing import Any, Mapping

from .intraday_capture import (
    BENCHMARK_SYMBOL, CHINA_TZ, _canonical_json_bytes, _immutable_write,
    _sha256_bytes, _sha256_file,
)
from .providers.a_stock_data_intraday_v4 import REALTIME_CONTRACT_SHA256

REQUIRED_GATES = {"evidenceIntegrity", "providerBinding", "exactUniverse", "realtimeV4",
                  "sameDayClose", "priceAgreement", "previousCloseAgreement",
                  "turnoverAgreement", "implementationBinding"}

THRESHOLDS = {
    "requiredQuoteCount": 301,
    "maximumPriceAbsoluteError": 0.01,
    "maximumPreviousCloseAbsoluteError": 0.01,
    "maximumTurnoverAbsoluteErrorVsRoundedReference": 0.02,
    "maxQuoteAgeSeconds": 120,
    "maxFutureQuoteLeadSeconds": 5,
}
TENCENT = "a-stock-data/tencent-public"
TCP = "a-stock-data/easy-tdx-public"
ALLOWED_SOURCES = {TENCENT, TCP, "a-stock-data/eastmoney-public", "a-stock-data/sina-public"}


def _digest(value: Any) -> str:
    return _sha256_bytes(_canonical_json_bytes(value))


def _timestamp(value: Any) -> datetime:
    parsed = datetime.fromisoformat(str(value))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("qualification timestamps must be timezone aware")
    return parsed.astimezone(CHINA_TZ)


def _number(value: Any, *, positive: bool = False) -> bool:
    try:
        return (isinstance(value, (int, float)) and not isinstance(value, bool)
                and math.isfinite(value) and (value > 0 if positive else value >= 0))
    except OverflowError:
        return False


def _read_evidence(path: Path | str) -> tuple[dict[str, Any], dict[str, str]]:
    path = Path(path).expanduser().resolve()
    archive = path.read_bytes()
    raw = gzip.decompress(archive) if path.suffix == ".gz" else archive
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError("qualification evidence must be an object")
    content_hash = _digest({k: v for k, v in payload.items() if k != "contentSha256"})
    sidecar = path.with_suffix(path.suffix + ".sha256")
    tokens = sidecar.read_text(encoding="ascii").split() if sidecar.is_file() else []
    archive_hash = _sha256_bytes(archive)
    if payload.get("contentSha256") != content_hash or not tokens or tokens[0] != archive_hash:
        raise ValueError("qualification evidence integrity mismatch")
    return payload, {"file": str(path), "contentSha256": content_hash,
                     "archiveSha256": archive_hash}


def _implementation_contract() -> dict[str, Any]:
    root = Path(__file__).resolve().parents[3]
    paths = [
        "frontend-v2/backend/market_data/intraday_source_qualification_v4.py",
        "frontend-v2/backend/market_data/intraday_capture_v4.py",
        "frontend-v2/backend/market_data/intraday_capture.py",
        "frontend-v2/backend/market_data/providers/a_stock_data_intraday_v4.py",
        "frontend-v2/backend/market_data/providers/a_stock_data_intraday_v3.py",
        "frontend-v2/backend/market_data/providers/a_stock_data_intraday_v2.py",
        "frontend-v2/backend/market_data/providers/a_stock_data.py",
        "requirements.txt", "frontend-v2/backend/requirements.txt",
    ]
    contract = {"files": [{"path": p, "sha256": _sha256_file(root / p)} for p in paths],
                "easyTdxPackageVersion": metadata.version("easy-tdx"),
                "realtimeContractSha256": REALTIME_CONTRACT_SHA256}
    return {**contract, "contractSha256": _digest(contract)}


def _universe(snapshot: Mapping[str, Any]) -> list[str]:
    universe = snapshot.get("universe") or {}
    symbols = universe.get("symbols")
    if (not isinstance(symbols, list) or len(symbols) != 301
            or any(not isinstance(s, str) or not re.fullmatch(r"\d{6}\.(SH|SZ)", s) for s in symbols)
            or len(set(symbols)) != 301 or BENCHMARK_SYMBOL not in symbols):
        raise ValueError("qualification requires exactly 301 unique universe symbols")
    if universe.get("symbolsSha256") != _sha256_bytes("\n".join(symbols).encode("ascii")):
        raise ValueError("qualification universe hash mismatch")
    return symbols


def _verify_realtime_implementation(snapshot: Mapping[str, Any], implementation: Mapping[str, Any]) -> None:
    bound = snapshot.get("implementation")
    if not isinstance(bound, Mapping):
        raise ValueError("realtime collector implementation is missing")
    if bound.get("sha256") != _digest({k: v for k, v in bound.items() if k != "sha256"}):
        raise ValueError("realtime collector implementation hash mismatch")
    runtime = bound.get("runtime") or {}
    module = "market_data.providers.a_stock_data_intraday_v4"
    if (runtime.get("entrypointModule") != "market_data.intraday_capture_v4"
            or runtime.get("providerModule") != module
            or runtime.get("productionProviderModule") != module
            or runtime.get("providerClass") != "ResponseTimedRealtimeClient"
            or runtime.get("productionProviderClass") != "ResponseTimedRealtimeClient"
            or runtime.get("clientInjected") is not False
            or runtime.get("easyTdxPackageVersion") != "1.20.6"
            or runtime.get("realtimeContractSha256") != REALTIME_CONTRACT_SHA256
            or runtime.get("prohibitCrossSourceFieldMerge") is not True):
        raise ValueError("realtime observation did not use the production V4 collector")
    expected_files = {row["path"]: row["sha256"] for row in implementation["files"]
                      if not row["path"].endswith("intraday_source_qualification_v4.py")}
    actual_files = bound.get("files")
    if (not isinstance(actual_files, list) or len(actual_files) != len(expected_files)
            or {row["path"]: row["sha256"] for row in actual_files} != expected_files):
        raise ValueError("realtime collector file binding drifted")


def _rows(snapshot: Mapping[str, Any], symbols: list[str], trade_date: str,
          *, realtime: bool, source: str | None = None) -> dict[str, Mapping[str, Any]]:
    requested = _timestamp(snapshot.get("captureRequestedAt"))
    received = _timestamp(snapshot.get("responseReceivedAt"))
    available = _timestamp(snapshot.get("availableAt"))
    if not requested <= received == available or requested.date().isoformat() != trade_date or received.date().isoformat() != trade_date:
        raise ValueError("qualification causal clock or date mismatch")
    if realtime:
        t = requested.time().replace(tzinfo=None)
        if not (time(9, 30) <= t <= time(11, 30) or time(13) <= t <= time(15, 2)):
            raise ValueError("realtime qualification requires a market-session observation")
    elif requested.time().replace(tzinfo=None) < time(15):
        raise ValueError("close comparison cannot use an intraday request")
    rows = snapshot.get("quotes")
    if not isinstance(rows, list) or len(rows) != 301:
        raise ValueError("qualification requires 301 quote rows")
    by_symbol = {}
    for row in rows:
        if not isinstance(row, Mapping):
            raise ValueError("invalid quote row")
        symbol = row.get("symbol")
        if symbol not in symbols or symbol in by_symbol:
            raise ValueError("duplicate or unexpected quote symbol")
        if (row.get("tradeDate") != trade_date or row.get("isStale") is not False
                or not _number(row.get("price"), positive=True)
                or not _number(row.get("previousClose"), positive=True)
                or (symbol != BENCHMARK_SYMBOL and not _number(row.get("turnoverPercent")))):
            raise ValueError("invalid quote core fields, date or stale flag")
        timestamp = _timestamp(row.get("quoteAsOf"))
        if timestamp.date().isoformat() != trade_date:
            raise ValueError("quote timestamp date mismatch")
        if realtime:
            age = (received - timestamp).total_seconds()
            if not -5 <= age <= 120:
                raise ValueError("realtime quote outside freshness limits")
            if row.get("realtimeContractSha256") != REALTIME_CONTRACT_SHA256 or row.get("source") not in ALLOWED_SOURCES:
                raise ValueError("realtime V4 source contract mismatch")
        else:
            if timestamp.time().replace(tzinfo=None) < time(15) or (timestamp - received).total_seconds() > 5:
                raise ValueError("close quote timestamp is not a completed same-day close")
        if source is not None and row.get("source") != source:
            raise ValueError("qualification requires a single exact source")
        by_symbol[symbol] = row
    return by_symbol


def build_source_qualification_report(
    *, realtime_snapshot_path: Path | str | None,
    reference_snapshot_path: Path | str | None,
    candidate_snapshot_path: Path | str | None,
    provider_fingerprint: str,
    measured_at: datetime,
) -> dict[str, Any]:
    """Build a redacted report. Invalid/incomplete inputs never yield PASS.

    provider_fingerprint is the independently verified Provider fingerprint supplied
    by the caller; the three evidence envelopes must bind identical Provider metadata.
    """
    checked_at = _timestamp(measured_at)
    gates = {"evidenceIntegrity": False, "providerBinding": False,
             "exactUniverse": False, "realtimeV4": False, "sameDayClose": False,
             "priceAgreement": False, "previousCloseAgreement": False,
             "turnoverAgreement": False, "implementationBinding": False}
    evidence = {}
    observed = {}
    implementation = None
    trade_date = None
    paths = {"realtime": realtime_snapshot_path, "reference": reference_snapshot_path,
             "candidate": candidate_snapshot_path}
    missing = any(p is None for p in paths.values())
    failure = None
    try:
        implementation = _implementation_contract()
        gates["implementationBinding"] = implementation["easyTdxPackageVersion"] == "1.20.6"
        if missing:
            failure = "MISSING_EVIDENCE"
        else:
            snapshots = {}
            for label, path in paths.items():
                snapshots[label], evidence[label] = _read_evidence(path)
            gates["evidenceIntegrity"] = True
            rt, ref, candidate = (snapshots[k] for k in ("realtime", "reference", "candidate"))
            trade_date = ref.get("quoteTradeDate")
            if not trade_date or any(s.get("quoteTradeDate") != trade_date for s in snapshots.values()) or checked_at.date().isoformat() != trade_date:
                raise ValueError("qualification observations must share the measured trade date")
            if any(_timestamp(s.get("availableAt")) > checked_at for s in snapshots.values()):
                raise ValueError("evidence became available after measurement")
            providers = [s.get("provider") for s in snapshots.values()]
            if (not re.fullmatch(r"[0-9a-f]{64}", provider_fingerprint)
                    or not all(isinstance(p, Mapping) and p for p in providers)
                    or len({_digest(p) for p in providers}) != 1
                    or any(s.get("providerFingerprint", provider_fingerprint) != provider_fingerprint for s in snapshots.values())):
                raise ValueError("provider binding mismatch")
            gates["providerBinding"] = True
            symbols = _universe(ref)
            if any(_universe(s) != symbols for s in (rt, candidate)):
                raise ValueError("qualification universes differ")
            gates["exactUniverse"] = True
            _verify_realtime_implementation(rt, implementation)
            _rows(rt, symbols, trade_date, realtime=True)
            gates["realtimeV4"] = True
            reference_rows = _rows(ref, symbols, trade_date, realtime=False, source=TENCENT)
            candidate_rows = _rows(candidate, symbols, trade_date, realtime=False, source=TCP)
            gates["sameDayClose"] = True
            price_error = max(abs(float(reference_rows[s]["price"]) - float(candidate_rows[s]["price"])) for s in symbols)
            previous_error = max(abs(float(reference_rows[s]["previousClose"]) - float(candidate_rows[s]["previousClose"])) for s in symbols)
            turnover_error = max(abs(float(reference_rows[s]["turnoverPercent"]) - float(candidate_rows[s]["turnoverPercent"])) for s in symbols if s != BENCHMARK_SYMBOL)
            observed = {"requiredQuoteCount": 301, "realtimeQuoteCount": 301,
                        "referenceQuoteCount": 301, "candidateQuoteCount": 301,
                        "maximumPriceAbsoluteError": price_error,
                        "maximumPreviousCloseAbsoluteError": previous_error,
                        "maximumTurnoverAbsoluteErrorVsRoundedReference": turnover_error}
            gates["priceAgreement"] = price_error <= THRESHOLDS["maximumPriceAbsoluteError"]
            gates["previousCloseAgreement"] = previous_error <= THRESHOLDS["maximumPreviousCloseAbsoluteError"]
            gates["turnoverAgreement"] = turnover_error <= THRESHOLDS["maximumTurnoverAbsoluteErrorVsRoundedReference"]
    except (OSError, ValueError, TypeError, KeyError, EOFError, OverflowError, metadata.PackageNotFoundError):
        # Never place raw upstream content, quote identifiers or error bodies here.
        failure = "INVALID_OR_UNAVAILABLE_EVIDENCE"
    status = "PASS" if all(gates.values()) else ("WAIT" if missing else "FAIL")
    body = {"schemaVersion": 1, "kind": "intraday_source_qualification_v4_report",
            "phase": "SOURCE_RELIABILITY_QUALIFICATION_ONLY", "status": status,
            "decision": "V4_SOURCE_QUALIFIED" if status == "PASS" else "DO_NOT_ACTIVATE_V4",
            "profitEvidenceEligible": False, "individualQuoteValuesIncluded": False,
            "symbolIdentifiersIncluded": False, "returnValuesIncluded": False,
            "tradeDate": trade_date, "measuredAt": checked_at.isoformat(),
            "providerFingerprint": provider_fingerprint,
            "universeSymbolsSha256": None,
            "realtimeContractSha256": REALTIME_CONTRACT_SHA256,
            "thresholds": dict(THRESHOLDS), "evidence": evidence,
            "observed": observed, "gateResults": gates,
            "failureCategory": failure, "implementation": implementation,
            "closeComparisonProvesRealtimeFreshness": False}
    if gates["exactUniverse"]:
        body["universeSymbolsSha256"] = ref["universe"]["symbolsSha256"]
    return {**body, "reportSha256": _digest(body)}


def read_verified_source_qualification_report(path: Path | str) -> dict[str, Any]:
    path = Path(path).expanduser().resolve()
    payload = json.loads(path.read_bytes())
    if (not isinstance(payload, dict) or payload.get("kind") != "intraday_source_qualification_v4_report"
            or payload.get("status") != "PASS" or payload.get("profitEvidenceEligible") is not False
            or not isinstance(payload.get("gateResults"), dict) or set(payload["gateResults"]) != REQUIRED_GATES
            or payload.get("thresholds") != THRESHOLDS
            or payload.get("implementation") != _implementation_contract()
            or not all(v is True for v in payload["gateResults"].values())
            or payload.get("realtimeContractSha256") != REALTIME_CONTRACT_SHA256):
        raise ValueError("invalid V4 qualification report")
    actual = _digest({k: v for k, v in payload.items() if k != "reportSha256"})
    sidecar = path.with_suffix(path.suffix + ".sha256")
    sidecar_tokens = sidecar.read_text(encoding="ascii").split() if sidecar.is_file() else []
    if (payload.get("reportSha256") != actual
            or path.name != f"source_qualification_v4_{actual[:12]}.json"
            or not sidecar_tokens or sidecar_tokens[0] != _sha256_file(path)):
        raise ValueError("V4 qualification report integrity mismatch")
    return payload


def persist_source_qualification_report(report: Mapping[str, Any], output_root: Path | str) -> Path:
    if (report.get("status") != "PASS" or report.get("profitEvidenceEligible") is not False
            or not isinstance(report.get("gateResults"), Mapping) or set(report["gateResults"]) != REQUIRED_GATES
            or report.get("thresholds") != THRESHOLDS
            or report.get("implementation") != _implementation_contract()
            or not all(v is True for v in report["gateResults"].values())
            or report.get("reportSha256") != _digest({k: v for k, v in report.items() if k != "reportSha256"})):
        raise ValueError("only an intact PASS qualification may be frozen")
    root = Path(output_root).expanduser().resolve() / "audits" / str(report["tradeDate"])
    existing = list(root.glob("source_qualification_v4_*.json"))
    path = root / f"source_qualification_v4_{report['reportSha256'][:12]}.json"
    if existing and (len(existing) != 1 or existing[0] != path):
        raise ValueError("another V4 qualification is already frozen for this date")
    raw = _canonical_json_bytes(dict(report))
    _immutable_write(path.with_suffix(path.suffix + ".sha256"), f"{_sha256_bytes(raw)}  {path.name}\n".encode("ascii"))
    _immutable_write(path, raw)
    read_verified_source_qualification_report(path)
    return path
