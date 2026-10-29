"""Opt-in, declaration-bound V4 collection/calibration; never trading acceptance."""
from __future__ import annotations

from datetime import date, datetime, time
import hashlib
import json
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from market_data import intraday_capture as _capture
from market_data import intraday_capture_v4 as _capture_v4
from market_data.intraday_calibration import (
    calibration_implementation_contract, load_calibration_declaration,
    read_verified_calibration_report,
)

DECLARATION_KEY = "QA_SHADOW_RECOVERY_DECLARATION"
_REQUIRED_FILES = {
    "frontend-v2/backend/local_app.py", "frontend-v2/backend/system_settings.py",
    "frontend-v2/backend/shadow_recovery.py",
}
_WINDOWS = [["09:30:00", "11:30:00"], ["13:00:00", "15:00:00"]]
_QUALITY_GATES = {
    "maxFutureQuoteLeadSeconds": 5, "maxQuoteAgeSeconds": 120,
    "maximumConsecutiveMissingSlots": 3, "maximumOrphanSnapshots": 0,
    "minimumBenchmarkCoverageRatio": 0.9, "minimumMedianCoreFieldCoverageRatio": 0.95,
    "minimumMedianUsableCoverageRatio": 0.95, "minimumP10UsableCoverageRatio": 0.9,
    "minimumSlotCoverageRatio": 0.9,
}
_CALIBRATION_REPORT_GATES = {
    "slotCoverage", "medianUsableCoverage", "p10UsableCoverage", "medianCoreFieldCoverage",
    "benchmarkCoverage", "longestMissingRun", "forcedCapture", "quoteTimes", "priceRanges",
    "integrity", "orphanSnapshots", "singleBinding", "frozenBinding", "calibrationImplementation",
}
_TZ = ZoneInfo("Asia/Shanghai")


class RecoveryError(ValueError):
    """Only fixed codes are disclosed through recovery status."""


def _now() -> datetime:
    return datetime.now(_TZ)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _inside(root: Path, value: Any) -> Path:
    if not isinstance(value, str) or not value:
        raise RecoveryError("RECOVERY_PATH_INVALID")
    path = Path(value).expanduser()
    path = (path if path.is_absolute() else root / path).resolve()
    if not path.is_relative_to(root) or path == root:
        raise RecoveryError("RECOVERY_PATH_OUTSIDE_PROJECT")
    return path


def _overlap(left: Path, right: Path) -> bool:
    return left == right or left.is_relative_to(right) or right.is_relative_to(left)


def _predeclared(payload: dict[str, Any], trade_day: date) -> None:
    created = datetime.fromisoformat(str(payload["createdAt"]))
    if (created.tzinfo is None or created.astimezone(_TZ).date() >= trade_day
            or created > _now()):
        raise RecoveryError("RECOVERY_DECLARATION_NOT_PREDECLARED")


class ShadowRecovery:
    def __init__(self, backend: Any, declaration_value: str):
        self.backend = backend
        self.root = Path(backend.PROJECT_ROOT).resolve()
        self.declaration_value = declaration_value
        self.original_capture_config = backend._intraday_capture_config
        self.original_runtime_binding = backend._intraday_capture_runtime_binding
        self.original_effective_provider = backend._intraday_capture_effective_provider_path
        self.original_research_config = backend._research_shadow_auto_config
        self.original_shadow_config = backend._intraday_shadow_config
        self.accepted_declaration_sha: str | None = None
        self.error: str | None = None

    def validate(self) -> dict[str, Any] | None:
        """Revalidate the entire selected binding before each collection cycle."""
        try:
            values = self.backend._load_dotenv_dict()
            if values.get(DECLARATION_KEY) != self.declaration_value:
                raise RecoveryError("RECOVERY_DECLARATION_SELECTION_CHANGED")
            if (self.original_research_config().get("enabled")
                    or self.original_shadow_config().get("enabled")):
                raise RecoveryError("RECOVERY_REQUIRES_TRADING_WORKERS_DISABLED")
            path = _inside(self.root, self.declaration_value)
            digest = _sha(path)
            sidecar = path.with_suffix(path.suffix + ".sha256")
            if sidecar.read_text(encoding="ascii").split() != [digest, path.name]:
                raise RecoveryError("RECOVERY_DECLARATION_HASH_INVALID")
            if self.accepted_declaration_sha not in (None, digest):
                raise RecoveryError("RECOVERY_DECLARATION_CHANGED_IN_PROCESS")
            declaration = json.loads(path.read_text(encoding="utf-8"))
            if (declaration.get("schemaVersion") != 1
                    or declaration.get("phase") != "CALIBRATION_ONLY"
                    or declaration.get("profitEvidenceEligible") is not False
                    or declaration.get("createdBeforeTradeDate") is not True
                    or type(declaration.get("pollSeconds")) is not int
                    or declaration["pollSeconds"] != 60):
                raise RecoveryError("RECOVERY_DECLARATION_CONTRACT_INVALID")
            target = date.fromisoformat(str(declaration["tradeDate"]))
            _predeclared(declaration, target)
            provider = _inside(self.root, declaration["providerPath"])
            output = _inside(self.root, declaration["outputRoot"])
            calibration_path = _inside(self.root, declaration["calibrationDeclaration"])
            old_capture = self.original_capture_config()
            legacy_outputs = {self.root / "data/market/a_share/intraday/a_stock_data_csi300"}
            if old_capture.get("outputRoot"):
                legacy_outputs.add(Path(old_capture["outputRoot"]).resolve())
            historical_providers: set[Path] = set()
            for old_path in (self.root / "configs").glob("intraday_shadow*.json"):
                prior = json.loads(old_path.read_text(encoding="utf-8"))
                if prior.get("captureRoot"):
                    legacy_outputs.add(_inside(self.root, prior["captureRoot"]))
                if (prior.get("providerRoot") or {}).get("path"):
                    historical_providers.add(_inside(self.root, prior["providerRoot"]["path"]))
            if (any(_overlap(output, old) for old in legacy_outputs)
                    or provider in historical_providers or _overlap(output, provider)):
                raise RecoveryError("RECOVERY_REQUIRES_INDEPENDENT_DIRECTORIES")

            files = declaration["recoveryImplementation"]["files"]
            if not isinstance(files, list):
                raise RecoveryError("RECOVERY_IMPLEMENTATION_INVALID")
            recorded: dict[str, str] = {}
            for entry in files:
                file_path = _inside(self.root, entry["path"])
                rel = str(file_path.relative_to(self.root))
                if rel in recorded or _sha(file_path) != entry["sha256"]:
                    raise RecoveryError("RECOVERY_IMPLEMENTATION_DRIFT")
                recorded[rel] = entry["sha256"]
            if not _REQUIRED_FILES.issubset(recorded):
                raise RecoveryError("RECOVERY_IMPLEMENTATION_INCOMPLETE")
            if recorded["frontend-v2/backend/shadow_recovery.py"] != _sha(Path(__file__).resolve()):
                raise RecoveryError("RECOVERY_LOADED_IMPLEMENTATION_DRIFT")

            implementation = _capture_v4.capture_implementation_contract()
            if (implementation["sha256"] != declaration["captureImplementationSha256"]
                    or implementation["runtime"].get("clientInjected") is not False):
                raise RecoveryError("RECOVERY_CAPTURE_IMPLEMENTATION_DRIFT")
            provider_context = _capture._provider_context(provider)
            binding = _capture._slot_binding(provider=provider_context,
                                             implementation=implementation, poll_seconds=60)
            if binding != declaration["providerBinding"]:
                raise RecoveryError("RECOVERY_PROVIDER_BINDING_DRIFT")
            if target.isoformat() not in provider_context["tradingDates"]:
                raise RecoveryError("RECOVERY_DATE_NOT_IN_FROZEN_CALENDAR")

            calibration, calibration_sha = load_calibration_declaration(calibration_path)
            if calibration_sha != declaration["calibrationDeclarationSha256"]:
                raise RecoveryError("RECOVERY_CALIBRATION_DECLARATION_DRIFT")
            _predeclared(calibration, target)
            details = calibration["calibration"]
            capture_contract = calibration["captureContract"]
            if (details["tradeDate"] != target.isoformat()
                    or details["continuousSessionWindows"] != _WINDOWS
                    or details["expectedSlotCount"] != 242
                    or details["finalizeAfter"] != "15:05:00"
                    or details["qualityGates"] != _QUALITY_GATES
                    or any(type(value) is bool for value in details["qualityGates"].values())
                    or capture_contract["pollSeconds"] != 60
                    or capture_contract["requestedSymbolCount"] != 301
                    or capture_contract["binding"] != binding
                    or calibration["calibrationImplementationSha256"]
                    != calibration_implementation_contract()["contractSha256"]):
                raise RecoveryError("RECOVERY_CALIBRATION_CONTRACT_INVALID")
            if self.accepted_declaration_sha not in (None, digest):
                raise RecoveryError("RECOVERY_DECLARATION_CHANGED_IN_PROCESS")
            self.accepted_declaration_sha = digest
            self.error = None
            return {"declaration": declaration, "provider": provider, "output": output,
                    "calibrationPath": calibration_path, "calibrationSha": calibration_sha,
                    "calibration": calibration,
                    "binding": binding, "implementation": implementation}
        except RecoveryError as exc:
            self.error = str(exc)
        except Exception:
            # Do not reflect file contents, exception bodies, hashes, or local paths.
            self.error = "RECOVERY_BINDING_VERIFICATION_FAILED"
        return None

    def capture_config(self) -> dict[str, Any]:
        try:
            original = dict(self.original_capture_config())
        except Exception:
            original = {"pollSeconds": 60, "maxCycleSeconds": 300,
                        "calibrationDeclaration": ""}
        verified = self.validate()
        if verified is None:
            return {**original, "enabled": False, "calibrationEnabled": False,
                    "providerPath": None,
                    "outputRoot": str(self.root / "data/market/a_share/intraday/recovery_disabled"),
                    "recoveryError": self.error}
        return {**original, "enabled": True, "providerPath": str(verified["provider"]),
                "outputRoot": str(verified["output"]), "pollSeconds": 60,
                "calibrationEnabled": True,
                "calibrationDeclaration": str(verified["calibrationPath"]),
                "recoveryError": None}

    def runtime_binding(self):
        verified = self.validate()
        if verified is None:
            error = self.error or "RECOVERY_BINDING_INVALID"
            def disabled_capture(*args, **kwargs):
                return {"status": "DISABLED", "recoveryError": error}
            return disabled_capture, {"version": 2, "enabled": False,
                                      "entrypointModule": __name__, "providerModule": None,
                                      "providerClass": None, "realtimeContractSha256": None,
                                      "recoveryError": error}
        return _capture_v4.run_capture_cycle, dict(verified["implementation"]["runtime"])

    def effective_provider(self, config, *, now=None):
        verified = self.validate()
        if verified is None:
            raise RecoveryError(self.error or "RECOVERY_BINDING_INVALID")
        if (Path(str(config.get("providerPath"))).resolve() != verified["provider"]
                or Path(str(config.get("outputRoot"))).resolve() != verified["output"]):
            raise RecoveryError("RECOVERY_EFFECTIVE_CONFIG_DRIFT")
        path = self.original_effective_provider(config, now=now)
        if not path or Path(path).resolve() != verified["provider"]:
            raise RecoveryError("RECOVERY_FIRST_SLOT_PROVIDER_DRIFT")
        return str(verified["provider"])

    def disabled_trading_config(self, original) -> dict[str, Any]:
        try:
            config = dict(original())
        except Exception:
            config = {"recoveryError": "RECOVERY_ORIGINAL_CONFIG_UNAVAILABLE"}
        # Clamp even an invalid opt-in before lifespan can start either trader.
        return {**config, "enabled": False, "recoveryMode": "CALIBRATION_ONLY"}

    def status(self) -> dict[str, Any]:
        verified = self.validate()
        result = {"enabled": True, "accepted": False,
                  "mode": "CALIBRATION_ONLY", "stage": "RECOVERY_INVALID",
                  "captureEnabled": verified is not None, "shadowEnabled": False,
                  "researchEnabled": False, "autoShadowAccepted": False,
                  "profitEvidenceEligible": False, "tradeDate": None,
                  "verifiedGates": {"declarationAndImplementation": verified is not None,
                                    "providerBinding": verified is not None,
                                    "calibrationDeclaration": verified is not None,
                                    "tradingDisabled": True},
                  "error": self.error}
        if verified is None:
            return result
        target = verified["declaration"]["tradeDate"]
        result["tradeDate"] = target
        current = _now().astimezone(_TZ)
        finalize_at = datetime.combine(date.fromisoformat(target), time(15, 5), tzinfo=_TZ)
        result["stage"] = ("WAITING_CALIBRATION" if current.date().isoformat() < target
                           else "CALIBRATION_IN_PROGRESS" if current < finalize_at
                           else "CALIBRATION_REVIEW_REQUIRED")
        try:
            status = _capture.read_capture_status(verified["output"]) or {}
            result["lastCaptureStatus"] = status.get("status")
            calibration = status.get("calibration") or {}
            if status.get("status") == "ERROR":
                result["stage"] = "CAPTURE_ERROR"
            if calibration.get("finalized"):
                result["stage"] = "CALIBRATION_REVIEW_REQUIRED"
                report_path = _inside(self.root, calibration["file"])
                if not report_path.is_relative_to(verified["output"]):
                    raise RecoveryError("RECOVERY_CALIBRATION_REPORT_OUTSIDE_OUTPUT")
                report = read_verified_calibration_report(report_path)
                evaluated = datetime.fromisoformat(str(report.get("evaluatedAt")))
                if (evaluated.tzinfo is None or not finalize_at <= evaluated <= current
                        or report.get("tradeDate") != target
                        or report.get("declaration", {}).get("sha256") != verified["calibrationSha"]
                        or report.get("expected", {}).get("binding") != verified["binding"]):
                    raise RecoveryError("RECOVERY_CALIBRATION_REPORT_BINDING_INVALID")
                if (report.get("status") == "PASS" and report.get("finalized") is True
                        and report.get("profitEvidenceEligible") is False
                        and report.get("phase") == "CALIBRATION_ONLY"
                        and report.get("kind") == "intraday_capture_calibration_report"
                        and report.get("schemaVersion") == 1
                        and report.get("qualityGates") == _QUALITY_GATES
                        and set(report.get("gateResults") or {}) == _CALIBRATION_REPORT_GATES
                        and all(value is True for value in report["gateResults"].values())
                        and report.get("observed", {}).get("capturedExpectedSlotCount") == 242
                        and report.get("expected", {}).get("slotCount") == 242):
                    result["stage"] = "WAIT_FORWARD_RUN"
                    result["verifiedGates"]["fullDayCalibration"] = True
                else:
                    result["stage"] = "CALIBRATION_NOT_ACCEPTED"
        except Exception:
            result["stage"] = "CALIBRATION_REVIEW_REQUIRED"
            result["error"] = "RECOVERY_CAPTURE_STATUS_UNVERIFIED"
        return result


def install_shadow_recovery(backend: Any) -> ShadowRecovery | None:
    """Expose status; patch runtime only after explicit declaration opt-in."""
    if hasattr(backend, "_shadow_recovery_adapter"):
        return backend._shadow_recovery_adapter
    declaration_value = backend._load_dotenv_dict().get(DECLARATION_KEY)
    recovery = ShadowRecovery(backend, declaration_value) if declaration_value else None
    backend._shadow_recovery_adapter = recovery

    @backend.app.get("/api/v1/shadow-recovery/status")
    async def recovery_status():
        if recovery is not None:
            return {"success": True, "data": recovery.status()}
        try:
            shadow_enabled = bool(backend._intraday_shadow_config().get("enabled"))
            error = None
        except Exception:
            shadow_enabled = False
            error = "SHADOW_CONFIG_UNAVAILABLE"
        return {"success": True, "data": {
            "enabled": False, "stage": "DISABLED", "tradeDate": None,
            "accepted": False, "autoShadowAccepted": False, "captureEnabled": False,
            "shadowEnabled": shadow_enabled, "error": error,
        }}

    backend.app.openapi_schema = None
    if recovery is None:
        return None
    backend._intraday_capture_config = recovery.capture_config
    backend._intraday_capture_runtime_binding = recovery.runtime_binding
    backend._intraday_capture_effective_provider_path = recovery.effective_provider
    backend._research_shadow_auto_config = lambda: recovery.disabled_trading_config(recovery.original_research_config)
    backend._intraday_shadow_config = lambda: recovery.disabled_trading_config(recovery.original_shadow_config)
    return recovery
