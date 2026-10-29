from __future__ import annotations

import hashlib
import json
import math
from datetime import date, datetime, time, timedelta
from pathlib import Path
from statistics import median
from typing import Any, Mapping

from .intraday_capture import (
    BENCHMARK_SYMBOL,
    CHINA_TZ,
    _canonical_json_bytes,
    _immutable_write,
    _sha256_bytes,
    _sha256_file,
    list_verified_slot_records,
    read_verified_snapshot,
)


CALIBRATION_SCHEMA_VERSION = 1
_PROCESS_CALIBRATION_SOURCE_SHA256: str | None = None


def _parse_datetime(value: Any, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value)).astimezone(CHINA_TZ)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid {field}: {value}") from exc
    return parsed


def _parse_time(value: str) -> time:
    try:
        return time.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"invalid calibration time: {value}") from exc


def _finite_number(value: Any, *, positive: bool = False) -> bool:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    numeric = float(value)
    if not math.isfinite(numeric):
        return False
    return numeric > 0 if positive else numeric >= 0


def calibration_implementation_contract() -> dict[str, Any]:
    source_path = Path(__file__).resolve()
    source_hash = _sha256_file(source_path)
    global _PROCESS_CALIBRATION_SOURCE_SHA256
    if _PROCESS_CALIBRATION_SOURCE_SHA256 is None:
        _PROCESS_CALIBRATION_SOURCE_SHA256 = source_hash
    elif _PROCESS_CALIBRATION_SOURCE_SHA256 != source_hash:
        raise RuntimeError(
            "intraday calibration source changed in a running process; restart is required"
        )
    contract = {
        "version": 1,
        "file": source_path.name,
        "sha256": source_hash,
    }
    return {
        **contract,
        "contractSha256": _sha256_bytes(_canonical_json_bytes(contract)),
    }


def load_calibration_declaration(
    declaration_path: Path | str,
) -> tuple[dict[str, Any], str]:
    path = Path(declaration_path).expanduser().resolve()
    raw = path.read_bytes()
    declaration_hash = _sha256_bytes(raw)
    sidecar = path.with_suffix(path.suffix + ".sha256")
    if not sidecar.is_file():
        raise ValueError(f"calibration declaration sidecar is missing: {sidecar}")
    if sidecar.read_text(encoding="ascii").split()[0] != declaration_hash:
        raise ValueError("calibration declaration SHA256 mismatch")
    payload = json.loads(raw.decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("calibration declaration must be a JSON object")
    if payload.get("schemaVersion") != CALIBRATION_SCHEMA_VERSION:
        raise ValueError("unsupported calibration declaration schema")
    if payload.get("phase") != "CALIBRATION_ONLY":
        raise ValueError("intraday calibration declaration must be CALIBRATION_ONLY")
    if payload.get("profitEvidenceEligible") is not False:
        raise ValueError("calibration declaration cannot be profit-evidence eligible")
    if payload.get("featureValuesPermitted") is not False:
        raise ValueError("calibration declaration cannot permit feature values")
    if payload.get("returnValuesPermitted") is not False:
        raise ValueError("calibration declaration cannot permit return values")
    return payload, declaration_hash


def _expected_slots(
    trade_date: str,
    windows: list[list[str]],
    poll_seconds: int,
) -> list[datetime]:
    day = date.fromisoformat(trade_date)
    slots: list[datetime] = []
    for window in windows:
        if not isinstance(window, list) or len(window) != 2:
            raise ValueError("each calibration window must contain start and end")
        cursor = datetime.combine(day, _parse_time(window[0]), tzinfo=CHINA_TZ)
        end = datetime.combine(day, _parse_time(window[1]), tzinfo=CHINA_TZ)
        if end < cursor:
            raise ValueError("calibration window end precedes start")
        while cursor <= end:
            slots.append(cursor)
            cursor += timedelta(seconds=poll_seconds)
    normalized = sorted(set(slots))
    if len(normalized) != len(slots):
        raise ValueError("calibration windows overlap")
    return normalized


def _percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, math.ceil(fraction * len(ordered)) - 1)
    return ordered[index]


def _longest_missing_run(expected: list[str], captured: set[str]) -> int:
    longest = 0
    current = 0
    for slot in expected:
        if slot in captured:
            current = 0
        else:
            current += 1
            longest = max(longest, current)
    return longest


def _snapshot_quality(
    payload: Mapping[str, Any],
    *,
    trade_date: str,
    max_quote_age_seconds: int,
    max_future_quote_lead_seconds: int,
) -> dict[str, Any]:
    available_at = _parse_datetime(payload.get("availableAt"), "availableAt")
    request_started_at = _parse_datetime(
        payload.get("captureRequestedAt"), "captureRequestedAt"
    )
    response_received_at = _parse_datetime(
        payload.get("responseReceivedAt"), "responseReceivedAt"
    )
    if request_started_at > response_received_at or response_received_at != available_at:
        raise ValueError("snapshot causal availability timestamps are inconsistent")

    quotes = payload.get("quotes")
    if not isinstance(quotes, list):
        raise ValueError("snapshot quotes must be a list")
    requested_count = int(payload.get("coverage", {}).get("requestedCount") or 0)
    if requested_count <= 0:
        raise ValueError("snapshot requested count is invalid")

    usable_symbols: set[str] = set()
    core_symbols: set[str] = set()
    source_counts: dict[str, int] = {}
    quote_time_errors = 0
    range_errors = 0
    for quote in quotes:
        if not isinstance(quote, dict):
            continue
        symbol = str(quote.get("symbol") or "")
        if quote.get("tradeDate") != trade_date:
            continue
        if not _finite_number(quote.get("price"), positive=True) or quote.get(
            "isStale"
        ):
            continue
        try:
            quote_time = _parse_datetime(quote.get("quoteAsOf"), "quoteAsOf")
        except ValueError:
            quote_time_errors += 1
            continue
        age_seconds = (available_at - quote_time).total_seconds()
        if (
            age_seconds > max_quote_age_seconds
            or age_seconds < -max_future_quote_lead_seconds
        ):
            quote_time_errors += 1
            continue
        usable_symbols.add(symbol)
        source = str(quote.get("source") or "unknown")
        source_counts[source] = source_counts.get(source, 0) + 1

        previous_close = quote.get("previousClose")
        open_price = quote.get("open")
        high = quote.get("high")
        low = quote.get("low")
        amount = quote.get("amountCny")
        if all(
            (
                _finite_number(previous_close, positive=True),
                _finite_number(open_price, positive=True),
                _finite_number(high, positive=True),
                _finite_number(low, positive=True),
                _finite_number(amount),
            )
        ):
            core_symbols.add(symbol)
            price = float(quote["price"])
            if not (
                float(low) <= price <= float(high)
                and float(low) <= float(open_price) <= float(high)
            ):
                range_errors += 1

    usable_ratio = len(usable_symbols) / requested_count
    core_ratio = len(core_symbols) / requested_count
    declared = payload.get("coverage", {})
    declared_usable = int(declared.get("usableQuoteCount") or 0)
    declared_ratio = float(declared.get("usableCoverageRatio") or 0.0)
    coverage_recompute_error = declared_usable != len(usable_symbols) or not math.isclose(
        declared_ratio, usable_ratio, rel_tol=0, abs_tol=1e-12
    )
    return {
        "usableCount": len(usable_symbols),
        "usableCoverageRatio": usable_ratio,
        "coreFieldCount": len(core_symbols),
        "coreFieldCoverageRatio": core_ratio,
        "benchmarkUsable": BENCHMARK_SYMBOL in usable_symbols,
        "quoteTimeErrors": quote_time_errors,
        "rangeErrors": range_errors,
        "coverageRecomputeError": coverage_recompute_error,
        "sourceCounts": dict(sorted(source_counts.items())),
    }


def build_calibration_report(
    declaration_path: Path | str,
    output_root: Path | str,
    *,
    trade_date: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    declaration, declaration_hash = load_calibration_declaration(declaration_path)
    calibration = declaration["calibration"]
    target_date = trade_date or str(calibration["tradeDate"])
    if target_date != calibration["tradeDate"]:
        raise ValueError("trade date does not match the frozen calibration declaration")
    poll_seconds = int(declaration["captureContract"]["pollSeconds"])
    expected_datetimes = _expected_slots(
        target_date,
        calibration["continuousSessionWindows"],
        poll_seconds,
    )
    expected_slots = [value.isoformat() for value in expected_datetimes]
    declared_expected_count = int(calibration.get("expectedSlotCount") or 0)
    if declared_expected_count != len(expected_slots):
        raise ValueError(
            "calibration expected slot count does not match its frozen windows"
        )
    expected_set = set(expected_slots)
    root = Path(output_root).expanduser().resolve()
    implementation = calibration_implementation_contract()
    gates = calibration["qualityGates"]
    integrity_errors: list[str] = []

    try:
        records = list_verified_slot_records(root, target_date)
    except Exception as exc:
        records = []
        integrity_errors.append(str(exc))

    records_by_slot = {
        str(payload.get("captureSlot")): (path, payload)
        for path, payload in records
        if str(payload.get("captureSlot")) in expected_set
    }
    captured_slots: set[str] = set()
    forced_records = 0
    failed_slot_statuses: dict[str, int] = {}
    usable_ratios: list[float] = []
    core_ratios: list[float] = []
    benchmark_usable = 0
    quote_time_errors = 0
    range_errors = 0
    source_counts: dict[str, int] = {}
    binding_hashes: set[str] = set()
    snapshot_manifest: list[dict[str, Any]] = []
    referenced_snapshots: set[Path] = {
        Path(str(payload["snapshot"]["file"])).expanduser().resolve()
        for _, payload in records
        if payload.get("snapshot")
    }

    for slot_text in expected_slots:
        item = records_by_slot.get(slot_text)
        if not item:
            continue
        record_path, record = item
        binding = record.get("binding")
        binding_hashes.add(_sha256_bytes(_canonical_json_bytes(binding)))
        if record.get("forcedCapture"):
            forced_records += 1
        if record.get("status") != "CAPTURED" or not record.get("snapshot"):
            key = str(record.get("status") or "UNKNOWN")
            failed_slot_statuses[key] = failed_slot_statuses.get(key, 0) + 1
            continue
        snapshot_path = Path(str(record["snapshot"]["file"])).expanduser().resolve()
        try:
            payload = read_verified_snapshot(snapshot_path)
            if payload.get("captureSlot") != slot_text:
                raise ValueError("snapshot capture slot does not match commit record")
            if payload.get("availableAt") != record.get("availableAt"):
                raise ValueError("snapshot availability does not match commit record")
            if _parse_datetime(record.get("committedAt"), "committedAt") < _parse_datetime(
                record.get("availableAt"), "record availableAt"
            ):
                raise ValueError("slot record was committed before data became available")
            if record["snapshot"].get("contentSha256") != payload.get(
                "contentSha256"
            ):
                raise ValueError("slot record snapshot content SHA256 mismatch")
            if record["snapshot"].get("archiveSha256") != _sha256_file(snapshot_path):
                raise ValueError("slot record snapshot archive SHA256 mismatch")
            if payload.get("forcedCapture"):
                forced_records += 1
            if payload.get("implementation", {}).get("sha256") != record.get(
                "binding", {}
            ).get("implementationSha256"):
                raise ValueError("snapshot implementation does not match slot binding")
            quality = _snapshot_quality(
                payload,
                trade_date=target_date,
                max_quote_age_seconds=int(gates["maxQuoteAgeSeconds"]),
                max_future_quote_lead_seconds=int(
                    gates["maxFutureQuoteLeadSeconds"]
                ),
            )
        except Exception as exc:
            integrity_errors.append(f"{slot_text}: {exc}")
            continue
        if quality["coverageRecomputeError"]:
            integrity_errors.append(
                f"{slot_text}: snapshot usable coverage does not recompute exactly"
            )
        captured_slots.add(slot_text)
        usable_ratios.append(quality["usableCoverageRatio"])
        core_ratios.append(quality["coreFieldCoverageRatio"])
        benchmark_usable += int(quality["benchmarkUsable"])
        quote_time_errors += int(quality["quoteTimeErrors"])
        range_errors += int(quality["rangeErrors"])
        for source, count in quality["sourceCounts"].items():
            source_counts[source] = source_counts.get(source, 0) + int(count)
        snapshot_manifest.append(
            {
                "slot": slot_text,
                "slotRecord": str(record_path),
                "slotRecordSha256": record["recordSha256"],
                "snapshot": str(snapshot_path),
                "contentSha256": payload["contentSha256"],
                "archiveSha256": _sha256_file(snapshot_path),
                "usableCoverageRatio": quality["usableCoverageRatio"],
                "coreFieldCoverageRatio": quality["coreFieldCoverageRatio"],
                "benchmarkUsable": quality["benchmarkUsable"],
            }
        )

    all_snapshot_paths = {
        path.resolve()
        for path in (root / "snapshots" / target_date).glob("snapshot_*.json.gz")
    }
    orphan_snapshots = sorted(str(path) for path in all_snapshot_paths - referenced_snapshots)
    expected_count = len(expected_slots)
    slot_coverage = len(captured_slots) / expected_count if expected_count else 0.0
    benchmark_ratio = benchmark_usable / expected_count if expected_count else 0.0
    median_usable = median(usable_ratios) if usable_ratios else 0.0
    p10_usable = _percentile(usable_ratios, 0.10)
    median_core = median(core_ratios) if core_ratios else 0.0
    longest_missing = _longest_missing_run(expected_slots, captured_slots)

    expected_binding = declaration["captureContract"]["binding"]
    observed_binding_hash = next(iter(binding_hashes), None)
    expected_binding_hash = _sha256_bytes(_canonical_json_bytes(expected_binding))
    gate_results = {
        "slotCoverage": slot_coverage >= float(gates["minimumSlotCoverageRatio"]),
        "medianUsableCoverage": median_usable
        >= float(gates["minimumMedianUsableCoverageRatio"]),
        "p10UsableCoverage": p10_usable
        >= float(gates["minimumP10UsableCoverageRatio"]),
        "medianCoreFieldCoverage": median_core
        >= float(gates["minimumMedianCoreFieldCoverageRatio"]),
        "benchmarkCoverage": benchmark_ratio
        >= float(gates["minimumBenchmarkCoverageRatio"]),
        "longestMissingRun": longest_missing
        <= int(gates["maximumConsecutiveMissingSlots"]),
        "forcedCapture": forced_records == 0,
        "quoteTimes": quote_time_errors == 0,
        "priceRanges": range_errors == 0,
        "integrity": not integrity_errors,
        "orphanSnapshots": len(orphan_snapshots)
        <= int(gates["maximumOrphanSnapshots"]),
        "singleBinding": len(binding_hashes) == 1,
        "frozenBinding": observed_binding_hash == expected_binding_hash,
        "calibrationImplementation": implementation["contractSha256"]
        == declaration["calibrationImplementationSha256"],
    }

    finalize_at = datetime.combine(
        date.fromisoformat(target_date),
        _parse_time(calibration["finalizeAfter"]),
        tzinfo=CHINA_TZ,
    )
    checked_at = (now or datetime.now(CHINA_TZ)).astimezone(CHINA_TZ)
    finalized = checked_at >= finalize_at
    status = "PASS" if finalized and all(gate_results.values()) else (
        "FAIL" if finalized else "IN_PROGRESS"
    )
    source_manifest = sorted(snapshot_manifest, key=lambda item: item["slot"])
    source_manifest_sha = _sha256_bytes(_canonical_json_bytes(source_manifest))
    report_without_hash = {
        "schemaVersion": CALIBRATION_SCHEMA_VERSION,
        "kind": "intraday_capture_calibration_report",
        "protocolId": declaration["protocolId"],
        "phase": "CALIBRATION_ONLY",
        "profitEvidenceEligible": False,
        "featureValuesIncluded": False,
        "returnValuesIncluded": False,
        "tradeDate": target_date,
        "status": status,
        "finalized": finalized,
        "evaluatedAt": (finalize_at if finalized else checked_at).isoformat(),
        "declaration": {
            "file": str(Path(declaration_path).expanduser().resolve()),
            "sha256": declaration_hash,
        },
        "calibrationImplementation": implementation,
        "expected": {
            "slotCount": expected_count,
            "slots": expected_slots,
            "binding": expected_binding,
        },
        "observed": {
            "committedRecordCount": len(records),
            "capturedExpectedSlotCount": len(captured_slots),
            "slotCoverageRatio": slot_coverage,
            "medianUsableCoverageRatio": median_usable,
            "p10UsableCoverageRatio": p10_usable,
            "medianCoreFieldCoverageRatio": median_core,
            "benchmarkCoverageRatio": benchmark_ratio,
            "longestConsecutiveMissingSlots": longest_missing,
            "forcedRecordCount": forced_records,
            "quoteTimeErrorCount": quote_time_errors,
            "rangeErrorCount": range_errors,
            "bindingVariantCount": len(binding_hashes),
            "failedSlotStatuses": dict(sorted(failed_slot_statuses.items())),
            "sourceCounts": dict(sorted(source_counts.items())),
            "orphanSnapshots": orphan_snapshots,
            "integrityErrors": integrity_errors,
        },
        "qualityGates": gates,
        "gateResults": gate_results,
        "sourceManifestSha256": source_manifest_sha,
        "sourceManifest": source_manifest,
        "decision": (
            "DATA_CALIBRATION_PASS"
            if status == "PASS"
            else "DATA_CALIBRATION_FAIL"
            if status == "FAIL"
            else "WAIT_FOR_SESSION_CLOSE"
        ),
        "nextAction": (
            "Freeze one new candidate and an independent A/B run before the next session; do not modify the active DeltaES1/B0 run."
            if status == "PASS"
            else "Do not create intraday factors or trades from this calibration day."
        ),
    }
    report_hash = _sha256_bytes(_canonical_json_bytes(report_without_hash))
    return {**report_without_hash, "reportSha256": report_hash}


def read_verified_calibration_report(path: Path | str) -> dict[str, Any]:
    path = Path(path).expanduser().resolve()
    raw = path.read_bytes()
    payload = json.loads(raw.decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("calibration report must be a JSON object")
    actual_report_hash = _sha256_bytes(
        _canonical_json_bytes(
            {key: value for key, value in payload.items() if key != "reportSha256"}
        )
    )
    expected_prefix = path.stem.rsplit("_", 1)[-1]
    if (
        payload.get("reportSha256") != actual_report_hash
        or not actual_report_hash.startswith(expected_prefix)
    ):
        raise ValueError("calibration report content SHA256 mismatch")
    sidecar = path.with_suffix(path.suffix + ".sha256")
    if not sidecar.is_file() or sidecar.read_text(encoding="ascii").split()[
        0
    ] != _sha256_bytes(raw):
        raise ValueError("calibration report file SHA256 mismatch")
    return payload


def calibration_report_summary(path: Path, payload: Mapping[str, Any]) -> dict[str, Any]:
    observed = payload["observed"]
    return {
        "status": payload["status"],
        "finalized": bool(payload["finalized"]),
        "decision": payload["decision"],
        "tradeDate": payload["tradeDate"],
        "file": str(path),
        "reportSha256": payload["reportSha256"],
        "slotCoverageRatio": observed["slotCoverageRatio"],
        "medianUsableCoverageRatio": observed["medianUsableCoverageRatio"],
        "capturedExpectedSlotCount": observed["capturedExpectedSlotCount"],
        "expectedSlotCount": payload["expected"]["slotCount"],
        "profitEvidenceEligible": False,
    }


def finalize_calibration_report(
    declaration_path: Path | str,
    output_root: Path | str,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    report = build_calibration_report(
        declaration_path,
        output_root,
        now=now,
    )
    if not report["finalized"]:
        return {
            **calibration_report_summary(Path(""), report),
            "file": None,
        }
    root = Path(output_root).expanduser().resolve()
    report_dir = root / "audits" / report["tradeDate"]
    existing = sorted(report_dir.glob("calibration_*.json"))
    if existing:
        if len(existing) != 1:
            raise ValueError("multiple calibration reports exist for one trade date")
        payload = read_verified_calibration_report(existing[0])
        if payload["reportSha256"] != report["reportSha256"]:
            raise ValueError("frozen calibration report no longer matches source records")
        return calibration_report_summary(existing[0], payload)

    raw = _canonical_json_bytes(report)
    path = report_dir / f"calibration_{report['reportSha256'][:12]}.json"
    file_hash = hashlib.sha256(raw).hexdigest()
    _immutable_write(
        path.with_suffix(path.suffix + ".sha256"),
        f"{file_hash}  {path.name}\n".encode("ascii"),
    )
    _immutable_write(path, raw)
    return calibration_report_summary(path, report)
