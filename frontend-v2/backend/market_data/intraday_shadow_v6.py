"""Independent-transport source contract for the future intraday shadow run."""

from __future__ import annotations

from importlib import metadata as importlib_metadata
from pathlib import Path
from typing import Any, Mapping

from .intraday_capture import _sha256_file
from .intraday_shadow import PROJECT_ROOT, _resolve, load_intraday_shadow_declaration
from .intraday_shadow_v5 import (
    IntradayShadowCoordinator as V5IntradayShadowCoordinator,
)
from .intraday_source_qualification_v3 import (
    EXPECTED_SOURCE,
    read_verified_source_qualification_report,
)
from .providers.a_stock_data_intraday_v3 import (
    INDEPENDENT_TRANSPORT_FALLBACK_CONTRACT,
    REALTIME_CONTRACT_SHA256,
)


DEFAULT_DECLARATION = (
    PROJECT_ROOT / "configs" / "intraday_shadow_20260817_t1_reversal_v6.json"
)
SOURCE_RELIABILITY_BASE_CONTRACT = {
    "version": 1,
    "sourceOrder": INDEPENDENT_TRANSPORT_FALLBACK_CONTRACT["sourceOrder"],
    "equityCoreFields": INDEPENDENT_TRANSPORT_FALLBACK_CONTRACT[
        "equityCoreFields"
    ],
    "benchmarkCoreFields": INDEPENDENT_TRANSPORT_FALLBACK_CONTRACT[
        "benchmarkCoreFields"
    ],
    "independentFallbackTransport": INDEPENDENT_TRANSPORT_FALLBACK_CONTRACT[
        "easyTdxTransport"
    ],
    "easyTdxPackageVersion": INDEPENDENT_TRANSPORT_FALLBACK_CONTRACT[
        "easyTdxPackageVersion"
    ],
    "realtimeContractSha256": REALTIME_CONTRACT_SHA256,
    "requireRealtimeContractForEveryQuote": True,
    "prohibitCrossSourceFieldMerge": True,
}


class IntradayShadowCoordinator(V5IntradayShadowCoordinator):
    """Require a qualified, independently transported fallback contract."""

    def __init__(self, declaration_path: Path | str = DEFAULT_DECLARATION):
        declaration, _ = load_intraday_shadow_declaration(declaration_path)
        reliability = declaration.get("sourceReliability")
        if not isinstance(reliability, Mapping):
            raise ValueError("intraday-shadow v6 source reliability contract is missing")
        base = {key: reliability.get(key) for key in SOURCE_RELIABILITY_BASE_CONTRACT}
        if base != SOURCE_RELIABILITY_BASE_CONTRACT:
            raise ValueError("intraday-shadow v6 source reliability contract drifted")
        if set(reliability) != {
            *SOURCE_RELIABILITY_BASE_CONTRACT,
            "qualification",
        }:
            raise ValueError("intraday-shadow v6 source reliability fields drifted")
        qualification = reliability.get("qualification")
        if not isinstance(qualification, Mapping) or set(qualification) != {
            "reportPath",
            "fileSha256",
            "reportSha256",
        }:
            raise ValueError("intraday-shadow v6 source qualification binding is invalid")
        report_path = _resolve(str(qualification["reportPath"]))
        if _sha256_file(report_path) != qualification["fileSha256"]:
            raise ValueError("intraday-shadow v6 source qualification file drifted")
        report = read_verified_source_qualification_report(report_path)
        candidate = report.get("candidate") or {}
        if (
            report.get("status") != "PASS"
            or report.get("decision") != "INDEPENDENT_SOURCE_QUALIFIED"
            or report.get("profitEvidenceEligible") is not False
            or report.get("reportSha256") != qualification["reportSha256"]
            or candidate.get("source") != EXPECTED_SOURCE
            or candidate.get("packageVersion")
            != SOURCE_RELIABILITY_BASE_CONTRACT["easyTdxPackageVersion"]
            or candidate.get("realtimeContractSha256")
            != REALTIME_CONTRACT_SHA256
            or candidate.get("prohibitCrossSourceFieldMerge") is not True
        ):
            raise ValueError("intraday-shadow v6 source qualification is not eligible")
        installed_version = importlib_metadata.version("easy-tdx")
        if installed_version != SOURCE_RELIABILITY_BASE_CONTRACT[
            "easyTdxPackageVersion"
        ]:
            raise ValueError("intraday-shadow v6 easy-tdx runtime version drifted")
        self.source_reliability = {
            **SOURCE_RELIABILITY_BASE_CONTRACT,
            "qualification": dict(qualification),
        }
        super().__init__(declaration_path)

    def _quality_quote_map(
        self,
        snapshot: Mapping[str, Any],
        *,
        label: str,
    ) -> dict[str, dict[str, Any]]:
        quotes = snapshot.get("quotes")
        if not isinstance(quotes, list) or not quotes:
            raise ValueError(f"intraday-shadow {label} snapshot has no quotes")
        if any(
            not isinstance(quote, Mapping)
            or quote.get("realtimeContractSha256") != REALTIME_CONTRACT_SHA256
            for quote in quotes
        ):
            raise ValueError(
                f"intraday-shadow {label} snapshot source contract is missing or drifted"
            )
        return super()._quality_quote_map(snapshot, label=label)

    def get_state(self) -> dict[str, Any]:
        state = super().get_state()
        state["sourceReliability"] = self.source_reliability
        return state
