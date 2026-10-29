"""Freeze a non-profit qualification of the independent intraday quote source."""

from __future__ import annotations

import json
import math
import time
from datetime import datetime
from importlib import metadata as importlib_metadata
from pathlib import Path
from typing import Any, Mapping

from .intraday_capture import (
    BENCHMARK_SYMBOL,
    CHINA_TZ,
    _canonical_json_bytes,
    _immutable_write,
    _sha256_bytes,
    _sha256_file,
    read_verified_snapshot,
)
from .providers.a_stock_data_intraday_v3 import (
    INDEPENDENT_TRANSPORT_FALLBACK_CONTRACT,
    REALTIME_CONTRACT_SHA256,
    IndependentTransportRealtimeClient,
)


QUALIFICATION_SCHEMA_VERSION = 1
QUALIFICATION_THRESHOLDS = {
    "requiredQuoteCount": 301,
    "maximumPriceAbsoluteError": 0.01,
    "maximumPreviousCloseAbsoluteError": 0.01,
    "maximumTurnoverAbsoluteErrorVsRoundedReference": 0.02,
}
EXPECTED_SOURCE = "a-stock-data/easy-tdx-public"


def _finite_number(value: Any, *, positive: bool = False) -> bool:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    number = float(value)
    if not math.isfinite(number):
        return False
    return number > 0 if positive else number >= 0


def _implementation_contract() -> dict[str, Any]:
    project_root = Path(__file__).resolve().parents[3]
    files = [
        Path(__file__).resolve(),
        project_root
        / "frontend-v2/backend/market_data/providers/a_stock_data_intraday_v3.py",
        project_root / "frontend-v2/backend/market_data/intraday_capture_v3.py",
        project_root / "requirements.txt",
        project_root / "frontend-v2/backend/requirements.txt",
    ]
    records = [
        {
            "path": str(path.relative_to(project_root)),
            "sha256": _sha256_file(path),
        }
        for path in files
    ]
    contract = {
        "version": 1,
        "files": records,
        "easyTdxPackageVersion": importlib_metadata.version("easy-tdx"),
        "realtimeContractSha256": REALTIME_CONTRACT_SHA256,
    }
    return {
        **contract,
        "contractSha256": _sha256_bytes(_canonical_json_bytes(contract)),
    }


def build_source_qualification_report(
    *,
    reference_snapshot: Mapping[str, Any],
    reference_path: Path,
    reference_archive_sha256: str,
    quotes: Mapping[str, Mapping[str, Any]],
    measured_at: datetime,
    query_elapsed_seconds: float,
) -> dict[str, Any]:
    trade_date = str(reference_snapshot.get("quoteTradeDate") or "")
    universe = reference_snapshot.get("universe")
    reference_rows = reference_snapshot.get("quotes")
    if not trade_date or not isinstance(universe, Mapping):
        raise ValueError("reference snapshot trade date or universe is missing")
    if not isinstance(reference_rows, list):
        raise ValueError("reference snapshot quotes are missing")
    symbols = universe.get("symbols")
    if not isinstance(symbols, list) or not all(isinstance(item, str) for item in symbols):
        raise ValueError("reference snapshot universe symbols are invalid")
    if len(symbols) != len(set(symbols)):
        raise ValueError("reference snapshot universe contains duplicate symbols")

    reference_by_symbol = {
        str(row.get("symbol")): row
        for row in reference_rows
        if isinstance(row, Mapping) and isinstance(row.get("symbol"), str)
    }
    expected_symbols = set(symbols)
    observed_symbols = set(quotes)
    price_errors: list[float] = []
    previous_close_errors: list[float] = []
    turnover_errors: list[float] = []
    core_valid_count = 0
    source_valid_count = 0
    trade_date_valid_count = 0

    for symbol in symbols:
        reference = reference_by_symbol.get(symbol)
        quote = quotes.get(symbol)
        if not isinstance(reference, Mapping) or not isinstance(quote, Mapping):
            continue
        source_valid = quote.get("source") == EXPECTED_SOURCE
        trade_date_valid = quote.get("tradeDate") == trade_date
        price_valid = _finite_number(quote.get("price"), positive=True)
        previous_close_valid = _finite_number(
            quote.get("previousClose"), positive=True
        )
        turnover_valid = symbol == BENCHMARK_SYMBOL or _finite_number(
            quote.get("turnoverPercent")
        )
        source_valid_count += int(source_valid)
        trade_date_valid_count += int(trade_date_valid)
        core_valid_count += int(
            source_valid
            and trade_date_valid
            and price_valid
            and previous_close_valid
            and turnover_valid
        )

        if price_valid and _finite_number(reference.get("price"), positive=True):
            price_errors.append(
                abs(float(quote["price"]) - float(reference["price"]))
            )
        if previous_close_valid and _finite_number(
            reference.get("previousClose"), positive=True
        ):
            previous_close_errors.append(
                abs(
                    float(quote["previousClose"])
                    - float(reference["previousClose"])
                )
            )
        if (
            symbol != BENCHMARK_SYMBOL
            and turnover_valid
            and _finite_number(reference.get("turnoverPercent"))
        ):
            turnover_errors.append(
                abs(
                    float(quote["turnoverPercent"])
                    - float(reference["turnoverPercent"])
                )
            )

    max_price_error = max(price_errors, default=None)
    max_previous_close_error = max(previous_close_errors, default=None)
    max_turnover_error = max(turnover_errors, default=None)
    required_count = int(QUALIFICATION_THRESHOLDS["requiredQuoteCount"])
    reference_source_valid = all(
        isinstance(row, Mapping)
        and row.get("source") == "a-stock-data/tencent-public"
        and row.get("tradeDate") == trade_date
        for row in reference_rows
    )
    gate_results = {
        "referenceIntegrity": (
            reference_snapshot.get("contentSha256") is not None
            and len(reference_by_symbol) == required_count
            and len(symbols) == required_count
            and reference_source_valid
        ),
        "exactUniverse": observed_symbols == expected_symbols,
        "allCoreFields": core_valid_count == required_count,
        "singleIndependentSource": source_valid_count == required_count,
        "currentTradeDate": trade_date_valid_count == required_count,
        "priceAgreement": (
            max_price_error is not None
            and max_price_error
            <= float(QUALIFICATION_THRESHOLDS["maximumPriceAbsoluteError"])
        ),
        "previousCloseAgreement": (
            max_previous_close_error is not None
            and max_previous_close_error
            <= float(
                QUALIFICATION_THRESHOLDS[
                    "maximumPreviousCloseAbsoluteError"
                ]
            )
        ),
        "turnoverAgreement": (
            len(turnover_errors) == required_count - 1
            and max_turnover_error is not None
            and max_turnover_error
            <= float(
                QUALIFICATION_THRESHOLDS[
                    "maximumTurnoverAbsoluteErrorVsRoundedReference"
                ]
            )
        ),
    }
    status = "PASS" if all(gate_results.values()) else "FAIL"
    implementation = _implementation_contract()
    report_without_hash = {
        "schemaVersion": QUALIFICATION_SCHEMA_VERSION,
        "kind": "intraday_source_qualification_report",
        "phase": "SOURCE_RELIABILITY_QUALIFICATION_ONLY",
        "profitEvidenceEligible": False,
        "featureValuesIncluded": False,
        "returnValuesIncluded": False,
        "individualQuoteValuesIncluded": False,
        "symbolIdentifiersIncluded": False,
        "tradeDate": trade_date,
        "measuredAt": measured_at.astimezone(CHINA_TZ).isoformat(),
        "status": status,
        "reference": {
            "file": str(reference_path.resolve()),
            "contentSha256": reference_snapshot["contentSha256"],
            "archiveSha256": reference_archive_sha256,
            "captureSlot": reference_snapshot.get("captureSlot"),
            "quoteCount": len(reference_rows),
            "universeSymbolsSha256": universe.get("symbolsSha256"),
            "source": "a-stock-data/tencent-public",
        },
        "candidate": {
            "source": EXPECTED_SOURCE,
            "transport": INDEPENDENT_TRANSPORT_FALLBACK_CONTRACT[
                "easyTdxTransport"
            ],
            "packageVersion": implementation["easyTdxPackageVersion"],
            "realtimeContractSha256": REALTIME_CONTRACT_SHA256,
            "prohibitCrossSourceFieldMerge": True,
        },
        "thresholds": QUALIFICATION_THRESHOLDS,
        "observed": {
            "referenceQuoteCount": len(reference_by_symbol),
            "requestedQuoteCount": len(symbols),
            "returnedQuoteCount": len(quotes),
            "missingQuoteCount": len(expected_symbols - observed_symbols),
            "unexpectedQuoteCount": len(observed_symbols - expected_symbols),
            "coreValidQuoteCount": core_valid_count,
            "sourceValidQuoteCount": source_valid_count,
            "tradeDateValidQuoteCount": trade_date_valid_count,
            "priceComparisonCount": len(price_errors),
            "previousCloseComparisonCount": len(previous_close_errors),
            "turnoverComparisonCount": len(turnover_errors),
            "maximumPriceAbsoluteError": max_price_error,
            "maximumPreviousCloseAbsoluteError": max_previous_close_error,
            "maximumTurnoverAbsoluteErrorVsRoundedReference": max_turnover_error,
            "queryElapsedSeconds": round(float(query_elapsed_seconds), 6),
        },
        "gateResults": gate_results,
        "implementation": implementation,
        "decision": (
            "INDEPENDENT_SOURCE_QUALIFIED"
            if status == "PASS"
            else "DO_NOT_ACTIVATE_INDEPENDENT_SOURCE"
        ),
    }
    report_sha256 = _sha256_bytes(_canonical_json_bytes(report_without_hash))
    return {**report_without_hash, "reportSha256": report_sha256}


def read_verified_source_qualification_report(path: Path | str) -> dict[str, Any]:
    report_path = Path(path).expanduser().resolve()
    raw = report_path.read_bytes()
    payload = json.loads(raw.decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("source qualification report must be a JSON object")
    actual_hash = _sha256_bytes(
        _canonical_json_bytes(
            {key: value for key, value in payload.items() if key != "reportSha256"}
        )
    )
    if (
        payload.get("reportSha256") != actual_hash
        or not actual_hash.startswith(report_path.stem.rsplit("_", 1)[-1])
    ):
        raise ValueError("source qualification report content SHA256 mismatch")
    sidecar = report_path.with_suffix(report_path.suffix + ".sha256")
    if (
        not sidecar.is_file()
        or sidecar.read_text(encoding="ascii").split()[0] != _sha256_bytes(raw)
    ):
        raise ValueError("source qualification report file SHA256 mismatch")
    return payload


def qualify_independent_source(
    reference_snapshot_path: Path | str,
    output_root: Path | str,
    *,
    client: IndependentTransportRealtimeClient | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    reference_path = Path(reference_snapshot_path).expanduser().resolve()
    reference = read_verified_snapshot(reference_path)
    trade_date = str(reference.get("quoteTradeDate") or "")
    report_dir = Path(output_root).expanduser().resolve() / "audits" / trade_date
    existing = sorted(report_dir.glob("source_qualification_v3_*.json"))
    if existing:
        if len(existing) != 1:
            raise ValueError("multiple source qualification reports exist")
        payload = read_verified_source_qualification_report(existing[0])
        if payload.get("reference", {}).get("contentSha256") != reference.get(
            "contentSha256"
        ):
            raise ValueError("frozen source qualification uses another reference")
        return payload

    symbols = list(reference.get("universe", {}).get("symbols") or [])
    requested = [
        (symbol, f"{'sh' if symbol.endswith('.SH') else 'sz'}{symbol[:6]}")
        for symbol in symbols
    ]
    quote_client = client or IndependentTransportRealtimeClient(
        timeout=8.0,
        tdx_ping_timeout=2.0,
        tdx_query_timeout=4.0,
    )
    started = time.monotonic()
    quotes = quote_client._easy_tdx_realtime_quotes(requested)
    elapsed = time.monotonic() - started
    report = build_source_qualification_report(
        reference_snapshot=reference,
        reference_path=reference_path,
        reference_archive_sha256=_sha256_file(reference_path),
        quotes=quotes,
        measured_at=now or datetime.now(CHINA_TZ),
        query_elapsed_seconds=elapsed,
    )
    if report["status"] != "PASS":
        raise ValueError(
            "independent source qualification failed; no report was frozen: "
            + json.dumps(report["gateResults"], sort_keys=True)
        )
    raw = _canonical_json_bytes(report)
    report_path = report_dir / f"source_qualification_v3_{report['reportSha256'][:12]}.json"
    file_hash = _sha256_bytes(raw)
    _immutable_write(
        report_path.with_suffix(report_path.suffix + ".sha256"),
        f"{file_hash}  {report_path.name}\n".encode("ascii"),
    )
    _immutable_write(report_path, raw)
    return report
