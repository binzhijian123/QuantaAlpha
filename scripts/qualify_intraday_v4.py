#!/usr/bin/env python3
"""Collect isolated V4 engineering evidence; never selects a trading Provider.

Run through scripts/qualify_intraday_v4.sh for the repo-owned conda bootstrap.
No clock/date override exists: close comparisons require a real same-day close.
"""
from __future__ import annotations

import argparse
import gzip
import json
import sys
from datetime import datetime, time
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = PROJECT_ROOT / "frontend-v2/backend"
for directory in (PROJECT_ROOT, BACKEND_ROOT):
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))

from market_data.intraday_capture import (  # noqa: E402
    CHINA_TZ, _canonical_json_bytes, _immutable_write, _provider_context,
    _sha256_bytes, read_verified_snapshot,
)
from market_data.intraday_capture_v4 import run_capture_cycle  # noqa: E402
from market_data.intraday_source_qualification_v4 import (  # noqa: E402
    REALTIME_CONTRACT_SHA256, build_source_qualification_report,
    persist_source_qualification_report,
)
from market_data.providers.a_stock_data_intraday_v4 import ResponseTimedRealtimeClient  # noqa: E402
from quantaalpha.utils.qlib_provider import provider_contract_fingerprint  # noqa: E402

DEFAULT_OUTPUT = PROJECT_ROOT / "data/market/a_share/intraday/v4_qualification"
PROTECTED_OUTPUT = PROJECT_ROOT / "data/market/a_share/intraday/a_stock_data_csi300"


def _now() -> datetime:
    return datetime.now(CHINA_TZ)


def _provider_metadata(context: dict[str, Any]) -> dict[str, Any]:
    return {"path": context["providerPath"], "latestDate": context["providerLatestDate"],
            "calendarPath": context["calendarPath"], "calendarSha256": context["calendarSha256"],
            "csi300Path": context["csi300Path"], "csi300Sha256": context["csi300Sha256"]}


def _universe_metadata(context: dict[str, Any]) -> dict[str, Any]:
    return {"name": "CSI300_PLUS_BENCHMARK", "asOf": context["providerLatestDate"],
            "symbolsSha256": context["symbolsSha256"], "symbols": context["symbols"]}


def _safe_output(path: Path) -> Path:
    output = path.expanduser().resolve()
    if output == PROTECTED_OUTPUT or PROTECTED_OUTPUT in output.parents:
        raise ValueError("the frozen production capture directory is protected")
    return output


def _write_envelope(output: Path, label: str, body: dict[str, Any]) -> Path:
    digest = _sha256_bytes(_canonical_json_bytes(body))
    payload = {**body, "contentSha256": digest}
    archive = gzip.compress(_canonical_json_bytes(payload), mtime=0)
    path = output / "raw_closes" / body["quoteTradeDate"] / f"{label}_{digest[:12]}.json.gz"
    _immutable_write(path.with_suffix(path.suffix + ".sha256"),
                     f"{_sha256_bytes(archive)}  {path.name}\n".encode("ascii"))
    _immutable_write(path, archive)
    return path


def _diagnostic(output: Path, payload: dict[str, Any]) -> Path:
    raw = _canonical_json_bytes(payload)
    path = output / "diagnostics" / f"diagnostic_{_sha256_bytes(raw)[:20]}.json"
    _immutable_write(path, raw)
    return path


def collect_realtime(provider: Path, output: Path) -> tuple[int, dict[str, Any]]:
    context = _provider_context(provider)
    fingerprint = provider_contract_fingerprint(provider)["fingerprint"]
    result = run_capture_cycle(provider, output / "capture")
    latest = result.get("latest") or {}
    path = latest.get("file")
    summary = {"stage": "REALTIME_COLLECTION_ONLY", "status": result.get("status"),
               "qualificationPassed": False, "profitEvidenceEligible": False,
               "providerFingerprint": fingerprint, "snapshotFile": path,
               "requiredQuoteCount": 301, "usableQuoteCount": latest.get("usableQuoteCount", 0),
               "requestedQuoteCount": len(context["symbols"])}
    if str(result.get("status", "")).startswith("WAITING"):
        return 2, summary
    if not path:
        return 1, summary
    snapshot = read_verified_snapshot(path)
    if (_provider_context(provider) != context
            or provider_contract_fingerprint(provider)["fingerprint"] != fingerprint):
        raise ValueError("Provider changed during realtime collection")
    summary["usableQuoteCount"] = snapshot.get("coverage", {}).get("usableQuoteCount", 0)
    summary["returnedQuoteCount"] = len(snapshot.get("quotes", []))
    summary["tradeDate"] = snapshot.get("quoteTradeDate")
    # This command collects evidence, never issues a source qualification PASS.
    if (snapshot.get("provider") != _provider_metadata(context)
            or snapshot.get("universe") != _universe_metadata(context)
            or summary["usableQuoteCount"] != 301 or summary["returnedQuoteCount"] != 301
            or summary["tradeDate"] != _now().date().isoformat()
            or any(q.get("realtimeContractSha256") != REALTIME_CONTRACT_SHA256 for q in snapshot["quotes"])):
        summary["status"] = "INCOMPLETE_OR_INVALID_REALTIME_EVIDENCE"
        return 1, summary
    summary["status"] = "REALTIME_STAGE_COLLECTED"
    return 0, summary


def collect_close(provider: Path, realtime_snapshot: Path, output: Path) -> tuple[int, dict[str, Any]]:
    current = _now()
    if current.time().replace(tzinfo=None) < time(15):
        return 2, {"stage": "CLOSE_COMPARISON", "status": "WAIT",
                   "reason": "WAIT_FOR_SAME_DAY_CLOSE", "profitEvidenceEligible": False}
    context = _provider_context(provider)
    fingerprint = provider_contract_fingerprint(provider)["fingerprint"]
    trade_date = current.date().isoformat()
    reference = read_verified_snapshot(realtime_snapshot)
    if (trade_date not in context["tradingDates"] or reference.get("quoteTradeDate") != trade_date
            or reference.get("provider") != _provider_metadata(context)
            or reference.get("universe") != _universe_metadata(context)):
        raise ValueError("realtime evidence does not bind the actual same-day Provider")
    client = ResponseTimedRealtimeClient(timeout=8.0, tdx_ping_timeout=5.0, tdx_query_timeout=8.0)
    requested = [(s, ("sh" if s.endswith(".SH") else "sz") + s[:6]) for s in context["symbols"]]
    paths = {}
    failures = []
    for label, fetch in (("reference", client._tencent_realtime_quotes),
                         ("candidate", client._easy_tdx_realtime_quotes)):
        started = _now()
        try:
            rows = fetch(requested)
        except Exception:
            rows = {}
            failures.append(label)
        received = _now()
        if received < started:
            raise ValueError("close source response clock moved backwards")
        # Preserve raw source flags/timestamps; do not manufacture freshness.
        body = {"schemaVersion": 1, "kind": "intraday_v4_raw_close_evidence",
                "profitEvidenceEligible": False, "quoteTradeDate": trade_date,
                "providerFingerprint": fingerprint, "provider": _provider_metadata(context),
                "universe": _universe_metadata(context),
                "captureRequestedAt": started.isoformat(), "responseReceivedAt": received.isoformat(),
                "availableAt": received.isoformat(), "quotes": list(rows.values())}
        paths[label] = _write_envelope(output, label, body)
    # Detect Provider drift during I/O rather than binding observations to stale context.
    if (_provider_context(provider) != context
            or provider_contract_fingerprint(provider)["fingerprint"] != fingerprint):
        raise ValueError("Provider changed during close collection")
    report = build_source_qualification_report(
        realtime_snapshot_path=realtime_snapshot, reference_snapshot_path=paths["reference"],
        candidate_snapshot_path=paths["candidate"], provider_fingerprint=fingerprint, measured_at=_now())
    if report["status"] == "PASS":
        path = persist_source_qualification_report(report, output)
        return 0, {"status": "PASS", "qualificationFile": str(path),
                   "reportSha256": report["reportSha256"], "profitEvidenceEligible": False}
    diagnostic = {"checkedAt": _now().isoformat(), "failedSourceStages": failures, "report": report}
    path = _diagnostic(output, diagnostic)
    return (2 if report["status"] == "WAIT" else 1), {
        "status": report["status"], "diagnosticFile": str(path),
        "failedGates": [k for k, v in report["gateResults"].items() if not v],
        "profitEvidenceEligible": False}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("realtime", "close"):
        command = sub.add_parser(name)
        command.add_argument("--provider", type=Path, required=True)
        command.add_argument("--output", type=Path, default=DEFAULT_OUTPUT,
                             help=f"isolated evidence directory (default: {DEFAULT_OUTPUT})")
        if name == "close":
            command.add_argument("--realtime-snapshot", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        output = _safe_output(args.output)
        provider = args.provider.expanduser().resolve()
        if args.command == "realtime":
            code, payload = collect_realtime(provider, output)
        else:
            code, payload = collect_close(provider, args.realtime_snapshot, output)
    except Exception:
        code, payload = 1, {"status": "FAIL", "reason": "COLLECTION_OR_INTEGRITY_ERROR",
                            "profitEvidenceEligible": False}
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
