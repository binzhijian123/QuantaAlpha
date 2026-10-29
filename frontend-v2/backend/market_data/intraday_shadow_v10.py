"""Inactive V10 gate: production V4 evidence on the unchanged V5 causal engine.

This module has no default declaration and does not select or activate a run.
All added evidence checks precede the parent's first SQLite/filesystem write.
"""
from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
import re
import sqlite3
from typing import Any, Mapping

from quantaalpha.utils.provider_lineage import verify_append_only_provider

from . import intraday_calibration as calibration
from . import intraday_capture as capture
from . import intraday_capture_v4 as capture_v4
from . import intraday_source_qualification_v4 as qualification
from .intraday_shadow import PROJECT_ROOT, SHANGHAI_TZ, _resolve, load_intraday_shadow_declaration
from .intraday_shadow_v2 import _load_quality_contract
from .intraday_shadow_v4 import QUALITY_EXTENSION_CONTRACT
from .intraday_shadow_v5 import CAUSALITY_CONTRACT, IntradayShadowCoordinator as V5Coordinator
from .providers.a_stock_data_intraday_v4 import (
    REALTIME_CONTRACT_SHA256, RESPONSE_TIME_CONTRACT,
)

PROTOCOL_ID = "intraday-shadow-t1-reversal-proxy-v10-response-timed"
SOURCE_RELIABILITY_BASE_CONTRACT = {
    "version": 2,
    "realtimeContractSha256": REALTIME_CONTRACT_SHA256,
    "sourceContract": RESPONSE_TIME_CONTRACT,
    "requireRealtimeContractForEveryQuote": True,
    "prohibitCrossSourceFieldMerge": True,
}
WINDOWS = [["09:30:00", "11:30:00"], ["13:00:00", "15:00:00"]]
QUALITY_GATES = {
    "maxFutureQuoteLeadSeconds": 5, "maxQuoteAgeSeconds": 120,
    "maximumConsecutiveMissingSlots": 3, "maximumOrphanSnapshots": 0,
    "minimumBenchmarkCoverageRatio": .9, "minimumMedianCoreFieldCoverageRatio": .95,
    "minimumMedianUsableCoverageRatio": .95, "minimumP10UsableCoverageRatio": .9,
    "minimumSlotCoverageRatio": .9,
}
EXECUTION = {
    "initialCash": 1000000., "riskDegree": .95, "priceField": "price",
    "settlement": "T+1", "lotSize": 100, "buyCostRate": .0005,
    "sellCostRate": .0015, "minimumCost": 5., "slippageBps": 10.,
    "limitThreshold": .095, "growthLimitThreshold": .195,
    "starMinimumOrderSize": 200, "starOrderIncrement": 1,
}
TIMING = {"decisionSlot": "14:50:00", "executionSlot": "14:55:00",
          "freezeAfter": "14:56:00", "freezeDeadline": "15:05:30"}
REQUIRED_IMPLEMENTATION_FILES = {
    *[f"frontend-v2/backend/market_data/intraday_shadow{suffix}.py"
      for suffix in ("", "_v2", "_v3", "_v4", "_v5", "_v10")],
    "quantaalpha/utils/provider_lineage.py", "quantaalpha/utils/qlib_provider.py",
}
HISTORICAL_DECLARATIONS_ROOT = PROJECT_ROOT / "configs"


def _overlaps(left: Path, right: Path) -> bool:
    return left == right or left in right.parents or right in left.parents


def _verify_run_isolation(declaration_path: Path | str,
                          declaration: Mapping[str, Any], digest: str) -> None:
    """Reject aliases/old ledgers before SQLite PRAGMA or directory creation."""
    own_declaration = _resolve(str(declaration_path))
    run_id = declaration.get("runId")
    if not isinstance(run_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*_v10", run_id):
        raise ValueError("V10 requires a separate runId ending in _v10")
    database = _resolve(declaration["databasePath"])
    run_dir = database.parent
    capture_root = _resolve(declaration["captureRoot"])
    for path in HISTORICAL_DECLARATIONS_ROOT.glob("intraday_shadow*.json"):
        if path.resolve() == own_declaration:
            continue
        historical = json.loads(path.read_text(encoding="utf-8"))
        if historical.get("runId") == run_id:
            raise ValueError("V10 runId is already declared by another run")
        old_database = _resolve(historical["databasePath"])
        old_capture = _resolve(historical["captureRoot"])
        if any(_overlaps(new, old) for new in (run_dir, capture_root)
               for old in (old_database.parent, old_capture)):
            raise ValueError("V10 paths overlap a historical run or capture tree")
    if not database.exists():
        if any(Path(str(database) + suffix).exists() for suffix in ("-wal", "-shm", "-journal")):
            raise ValueError("V10 database has unowned SQLite sidecars")
        return
    expected = {"runId": run_id, "declarationSha256": digest,
                "usageRealm": "INTRADAY_SHADOW", "formalPaperEligible": "false"}
    try:
        # immutable avoids creating even read-only WAL/SHM files. If the ownership
        # rows exist only in an uncheckpointed WAL, this deliberately fails closed.
        with sqlite3.connect(database.as_uri() + "?mode=ro&immutable=1", uri=True) as connection:
            rows = connection.execute("SELECT key, value FROM shadow_metadata").fetchall()
        metadata = dict(rows)
    except (sqlite3.Error, OSError, ValueError):
        raise ValueError("V10 target database ownership cannot be verified read-only") from None
    if len(rows) != len(metadata) or metadata != expected:
        raise ValueError("V10 target database metadata belongs to another declaration")


def _time(value: Any) -> datetime:
    parsed = datetime.fromisoformat(str(value))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("V10 evidence timestamps must be timezone aware")
    return parsed.astimezone(SHANGHAI_TZ)


def _provider_metadata(context: Mapping[str, Any]) -> dict[str, Any]:
    return {"path": context["providerPath"], "latestDate": context["providerLatestDate"],
            **{key: context[key] for key in
               ("calendarPath", "calendarSha256", "csi300Path", "csi300Sha256")}}


def _verify_universe(snapshot: Mapping[str, Any], context: Mapping[str, Any]) -> None:
    quotes = snapshot.get("quotes") or []
    quoted = [row.get("symbol") for row in quotes if isinstance(row, Mapping)]
    if (qualification._universe(snapshot) != context["symbols"]
            or snapshot["universe"]["symbolsSha256"] != context["symbolsSha256"]
            or len(quoted) != 301 or set(quoted) != set(context["symbols"])):
        raise ValueError("V10 snapshot differs from the frozen exact universe")


def _verify_production(snapshot: Mapping[str, Any], implementation: Mapping[str, Any]) -> None:
    if snapshot.get("implementation") != implementation:
        raise ValueError("V10 requires the current production V4 capture implementation")
    quotes = snapshot.get("quotes")
    if (not isinstance(quotes, list) or len(quotes) != 301
            or any(not isinstance(row, Mapping)
                   or row.get("realtimeContractSha256") != REALTIME_CONTRACT_SHA256
                   for row in quotes)):
        raise ValueError("V10 requires the V4 realtime contract on every quote")
    requested, received, available, captured = (
        _time(snapshot.get(key)) for key in
        ("captureRequestedAt", "responseReceivedAt", "availableAt", "capturedAt")
    )
    if not requested <= received == available == captured:
        raise ValueError("V10 snapshot request/response/availability clock moved backwards or drifted")
    for row in quotes:
        # Check ALL rows, including those the inherited coverage gate may omit.
        # Quote age/lead and trade-date constraints remain the V5/V4 quality gate.
        _time(row.get("quoteAsOf"))


def _bound_report(binding: Mapping[str, Any], reader: Any) -> dict[str, Any]:
    if not isinstance(binding, Mapping) or set(binding) != {
            "reportPath", "fileSha256", "reportSha256"}:
        raise ValueError("V10 evidence report binding is invalid")
    path = _resolve(binding["reportPath"])
    if capture._sha256_file(path) != binding["fileSha256"]:
        raise ValueError("V10 evidence report file drifted")
    report = reader(path)
    if report.get("reportSha256") != binding["reportSha256"]:
        raise ValueError("V10 evidence report content drifted")
    return report


def _verify_qualification(binding: Mapping[str, Any], context: Mapping[str, Any],
                          fingerprint: str, declared_at: datetime,
                          implementation: Mapping[str, Any]) -> dict[str, Any]:
    report = _bound_report(binding, qualification.read_verified_source_qualification_report)
    if (report.get("providerFingerprint") != fingerprint
            or report.get("universeSymbolsSha256") != context["symbolsSha256"]
            or _time(report["measuredAt"]) > declared_at):
        raise ValueError("V10 source qualification root/time/universe mismatch")
    evidence = report.get("evidence") or {}
    if set(evidence) != {"realtime", "reference", "candidate"}:
        raise ValueError("V10 qualification requires all three original observations")
    paths = {}
    for label, bound in evidence.items():
        path = _resolve(bound["file"])
        snapshot, observed = qualification._read_evidence(path)
        if observed != bound or snapshot.get("provider") != _provider_metadata(context):
            raise ValueError("V10 qualification evidence/provider drifted")
        _verify_universe(snapshot, context)
        if label == "realtime":
            _verify_production(snapshot, implementation)
        paths[label + "_snapshot_path"] = path
    # Recompute with the ORIGINAL measurement time, never a fresh convenient time.
    rebuilt = qualification.build_source_qualification_report(
        **paths, provider_fingerprint=fingerprint, measured_at=_time(report["measuredAt"]))
    if rebuilt != report or rebuilt["status"] != "PASS":
        raise ValueError("V10 qualification no longer reproduces its frozen PASS")
    return report


def _verify_calibration(binding: Mapping[str, Any], context: Mapping[str, Any],
                        declared_at: datetime, implementation: Mapping[str, Any]) -> dict[str, Any]:
    report = _bound_report(binding, calibration.read_verified_calibration_report)
    if (report.get("status") != "PASS" or report.get("finalized") is not True
            or _time(report["evaluatedAt"]) > declared_at):
        raise ValueError("V10 requires a previously finalized calibration PASS")
    declaration_path = _resolve(report["declaration"]["file"])
    declaration, digest = calibration.load_calibration_declaration(declaration_path)
    settings = declaration["calibration"]
    day = str(report["tradeDate"])
    if (digest != report["declaration"]["sha256"]
            or settings.get("tradeDate") != day
            or _time(declaration.get("createdAt")).date().isoformat() >= day
            or context["providerLatestDate"] > _time(declaration.get("createdAt")).date().isoformat()
            or declaration.get("createdBeforeCurrentTradeDateQuotesObserved") is not True
            or settings.get("continuousSessionWindows") != WINDOWS
            or settings.get("expectedSlotCount") != 242
            or settings.get("finalizeAfter") != "15:05:00"
            or settings.get("qualityGates") != QUALITY_GATES
            or declaration["captureContract"].get("pollSeconds") != 60):
        raise ValueError("V10 calibration predeclaration or fixed quality contract drifted")
    expected_slots = [t.isoformat() for t in calibration._expected_slots(day, WINDOWS, 60)]
    manifest = report.get("sourceManifest") or []
    if (report.get("expected", {}).get("slots") != expected_slots
            or report.get("expected", {}).get("slotCount") != 242
            or len(manifest) != 242
            or [item.get("slot") for item in manifest] != expected_slots
            or report.get("observed", {}).get("capturedExpectedSlotCount") != 242
            or report.get("observed", {}).get("slotCoverageRatio") != 1):
        raise ValueError("V10 requires every one of the 242 calibration slots")
    root = _resolve(manifest[0]["snapshot"]).parents[2]
    expected_binding = capture._slot_binding(provider=context, implementation=implementation,
                                             poll_seconds=60)
    if (declaration["captureContract"].get("binding") != expected_binding
            or report["expected"].get("binding") != expected_binding):
        raise ValueError("V10 calibration root/implementation binding drifted")
    for item in manifest:
        path = _resolve(item["snapshot"])
        record = _resolve(item["slotRecord"])
        if (path.parent != root / "snapshots" / day
                or record.parent != root / "slots" / day):
            raise ValueError("V10 calibration evidence escaped its capture tree")
        snapshot = capture.read_verified_snapshot(path)
        if snapshot.get("provider") != _provider_metadata(context):
            raise ValueError("V10 calibration snapshot provider drifted")
        _verify_universe(snapshot, context)
        _verify_production(snapshot, implementation)
    rebuilt = calibration.build_calibration_report(declaration_path, root,
                                                   now=_time(report["evaluatedAt"]))
    if rebuilt != report or rebuilt["status"] != "PASS":
        raise ValueError("V10 calibration no longer reproduces its frozen PASS")
    return report


def validate_v10_declaration(declaration_path: Path | str) -> dict[str, Any]:
    """Read-only declaration qualification; no coordinator or runtime acceptance.

    Rechecks the frozen source files and reports without creating directories,
    SQLite databases, lineage proofs, status artifacts, or SQLite sidecars.
    """
    declaration, declaration_sha = load_intraday_shadow_declaration(declaration_path)
    if (declaration.get("protocolId") != PROTOCOL_ID
            or declaration.get("strategy", {}).get("topK") != 50
            or declaration.get("execution") != EXECUTION
            or declaration.get("timing") != TIMING):
        raise ValueError("V10 protocol or preserved execution contract drifted")
    # These are the unchanged pre-write requirements of the V5 -> V4 -> V2
    # initializer chain, included so this public check cannot claim qualification
    # for a declaration that the inherited initializer would subsequently reject.
    _load_quality_contract(declaration)
    if declaration.get("qualityExtension") != QUALITY_EXTENSION_CONTRACT:
        raise ValueError("intraday-shadow v4 quality extension drifted")
    if declaration.get("causality") != CAUSALITY_CONTRACT:
        raise ValueError("intraday-shadow v5 causality contract drifted")
    _verify_run_isolation(declaration_path, declaration, declaration_sha)
    bound_files = {_resolve(row["path"]) for row in declaration["implementation"]["files"]}
    if not {PROJECT_ROOT / name for name in REQUIRED_IMPLEMENTATION_FILES} <= bound_files:
        raise ValueError("V10 implementation must bind its coordinator and inherited engine")
    reliability = declaration.get("sourceReliability")
    if (not isinstance(reliability, Mapping)
            or set(reliability) != {*SOURCE_RELIABILITY_BASE_CONTRACT,
                                    "qualification", "captureImplementationSha256"}
            or {key: reliability.get(key) for key in SOURCE_RELIABILITY_BASE_CONTRACT}
            != SOURCE_RELIABILITY_BASE_CONTRACT):
        raise ValueError("V10 response-timed source contract drifted")
    implementation = capture_v4.capture_implementation_contract()
    if reliability["captureImplementationSha256"] != implementation["sha256"]:
        raise ValueError("V10 production capture implementation drifted")
    context = capture._provider_context(_resolve(declaration["providerRoot"]["path"]))
    declared_at = _time(declaration["declaredAt"])
    if declared_at > datetime.now(SHANGHAI_TZ):
        raise ValueError("V10 declaration cannot be future dated")
    source_report = _verify_qualification(reliability["qualification"], context,
        declaration["providerRoot"]["fingerprint"], declared_at, implementation)
    calibration_report = _verify_calibration(declaration["calibration"], context,
        declared_at, implementation)
    if source_report["tradeDate"] > calibration_report["tradeDate"]:
        raise ValueError("V10 qualification must precede or share calibration day")
    return {"declaration": declaration, "declarationSha256": declaration_sha,
            "providerContext": context, "captureImplementation": implementation,
            "sourceQualificationReport": source_report, "calibrationReport": calibration_report}


class IntradayShadowCoordinator(V5Coordinator):
    """Require independently revalidated V4 source and full-session calibration."""

    def __init__(self, declaration_path: Path | str):
        validated = validate_v10_declaration(declaration_path)
        self.source_reliability = dict(validated["declaration"]["sourceReliability"])
        self.production_capture = validated["captureImplementation"]
        self.frozen_context = validated["providerContext"]
        super().__init__(declaration_path)

    def _quality_quote_map(self, snapshot: Mapping[str, Any], *, label: str):
        # Also protects the V5 intent recovery path that reloads a frozen snapshot.
        _verify_production(snapshot, capture_v4.capture_implementation_contract())
        _verify_universe(snapshot, self.frozen_context)
        return super()._quality_quote_map(snapshot, label=label)

    def _verify_provider(self, provider_path: Path):
        provider = provider_path.expanduser().resolve()
        root = _resolve(self.declaration["providerRoot"]["path"])
        fresh = None
        if provider != root:
            # Cached lineage metadata alone cannot certify unchanged factor bytes.
            fresh = verify_append_only_provider(root, provider, market="csi300",
                benchmark=capture.BENCHMARK_SYMBOL, fields=("factor",))
            if fresh["parentFingerprint"] != self.declaration["providerRoot"]["fingerprint"]:
                raise ValueError("V10 provider lineage has the wrong frozen root")
        contract, proof = super()._verify_provider(provider)
        if fresh is not None and proof.get("successorFeatureTree") != fresh["successorFeatureTree"]:
            raise ValueError("V10 provider factor tree differs from its frozen lineage proof")
        return contract, proof

    def _snapshot_at(self, trade_date: str, slot: str, available_by: datetime):
        record, snapshot = super()._snapshot_at(trade_date, slot, available_by=available_by)
        context = capture._provider_context(_resolve(snapshot["provider"]["path"]))
        if (snapshot["provider"] != _provider_metadata(context)
                or context["symbolsSha256"] != self.frozen_context["symbolsSha256"]):
            raise ValueError("V10 snapshot provider metadata/universe drifted")
        implementation = capture_v4.capture_implementation_contract()
        if implementation != self.production_capture:
            raise ValueError("V10 capture implementation changed after initialization")
        if record.get("binding") != capture._slot_binding(provider=context,
                implementation=implementation, poll_seconds=60):
            raise ValueError("V10 slot binding differs from the actual provider")
        _verify_universe(snapshot, self.frozen_context)
        _verify_production(snapshot, implementation)
        self._verify_provider(Path(context["providerPath"]))
        return record, snapshot

    def get_state(self):
        state = super().get_state()
        state["sourceReliability"] = self.source_reliability
        return state
