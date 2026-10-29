"""Explicit r4 recovery binding; V4 quote semantics remain unchanged."""
from __future__ import annotations

from datetime import date
import json
import os
from pathlib import Path
from typing import Any

import shadow_recovery as base

DECLARATION_KEY = "QA_SHADOW_RECOVERY_R4_DECLARATION"
RUNTIME_MODE = "SLOT_ALIGNED_CAPTURE_R4"
ENTRYPOINT = "recovery_r4_app"
REQUIRED_FILES = frozenset({
    *base._REQUIRED_FILES,
    "frontend-v2/backend/app.py",
    "frontend-v2/backend/recovery_r4_app.py",
    "frontend-v2/backend/shadow_recovery_r4.py",
    "frontend-v2/backend/market_data/slot_aligned_capture_runtime.py",
    "scripts/freeze_shadow_recovery.py", "scripts/freeze_shadow_recovery_r4.py",
    "scripts/run_recovery_r4_backend_supervised.sh",
})
_LOADED_SHA = base._sha(Path(__file__).resolve())
_BASE_LOADED_SHA = base._sha(Path(base.__file__).resolve())


def scheduler_contract() -> dict[str, Any]:
    from market_data.slot_aligned_capture_runtime import capture_scheduler_implementation_contract
    return capture_scheduler_implementation_contract()


def metadata(scheduler_sha: str) -> dict[str, Any]:
    return {"runtimeMode": RUNTIME_MODE, "runtimeEntrypoint": ENTRYPOINT,
            "schedulerImplementationSha256": scheduler_sha}


def _output_isolated(root: Path, output: Path, own_declaration: Path | None = None) -> None:
    # A direct new capture directory cannot alias watch output or a ledger tree.
    parent = root / "data/market/a_share/intraday"
    if output.parent != parent or output.name in {"v4_qualification_watch", "v4_qualification"}:
        raise base.RecoveryError("RECOVERY_R4_CAPTURE_DIRECTORY_INVALID")
    for path in (root / "configs").glob("*.json"):
        if own_declaration is not None and path.resolve() == own_declaration:
            continue
        prior = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(prior, dict):
            continue
        for key in ("outputRoot", "captureRoot"):
            if prior.get(key) and base._overlap(output, base._inside(root, prior[key])):
                raise base.RecoveryError("RECOVERY_R4_CAPTURE_DIRECTORY_REUSED")


class ShadowRecoveryR4(base.ShadowRecovery):
    """Retain the old adapter operations with an explicit, fail-closed r4 schema."""

    def validate(self) -> dict[str, Any] | None:
        try:
            if not self.declaration_value or os.environ.get(DECLARATION_KEY) != self.declaration_value:
                raise base.RecoveryError("RECOVERY_R4_EXPLICIT_SELECTOR_REQUIRED")
            if (self.original_research_config().get("enabled")
                    or self.original_shadow_config().get("enabled")):
                raise base.RecoveryError("RECOVERY_REQUIRES_TRADING_WORKERS_DISABLED")
            path = base._inside(self.root, self.declaration_value)
            digest = base._sha(path)
            if path.with_suffix(path.suffix + ".sha256").read_text(encoding="ascii").split() != [digest, path.name]:
                raise base.RecoveryError("RECOVERY_DECLARATION_HASH_INVALID")
            if self.accepted_declaration_sha not in (None, digest):
                raise base.RecoveryError("RECOVERY_DECLARATION_CHANGED_IN_PROCESS")
            declaration = json.loads(path.read_text(encoding="utf-8"))
            contract = scheduler_contract()
            if (declaration.get("schemaVersion") != 4
                    or {key: declaration.get(key) for key in metadata(contract["sha256"])} != metadata(contract["sha256"])
                    or declaration.get("phase") != "CALIBRATION_ONLY"
                    or declaration.get("profitEvidenceEligible") is not False
                    or declaration.get("createdBeforeTradeDate") is not True
                    or type(declaration.get("pollSeconds")) is not int
                    or declaration["pollSeconds"] != 60):
                raise base.RecoveryError("RECOVERY_R4_CONTRACT_INVALID")
            target = date.fromisoformat(str(declaration["tradeDate"]))
            base._predeclared(declaration, target)
            provider = base._inside(self.root, declaration["providerPath"])
            output = base._inside(self.root, declaration["outputRoot"])
            calibration_path = base._inside(self.root, declaration["calibrationDeclaration"])
            _output_isolated(self.root, output, path)
            old_outputs = {self.root / "data/market/a_share/intraday/a_stock_data_csi300"}
            original = self.original_capture_config()
            if original.get("outputRoot"):
                old_outputs.add(Path(original["outputRoot"]).resolve())
            for old_path in (self.root / "configs").glob("intraday_shadow*.json"):
                prior = json.loads(old_path.read_text(encoding="utf-8"))
                if prior.get("captureRoot"):
                    old_outputs.add(base._inside(self.root, prior["captureRoot"]))
                if (prior.get("providerRoot") or {}).get("path") and provider == base._inside(self.root, prior["providerRoot"]["path"]):
                    raise base.RecoveryError("RECOVERY_REQUIRES_INDEPENDENT_DIRECTORIES")
            if any(base._overlap(output, old) for old in old_outputs) or base._overlap(output, provider):
                raise base.RecoveryError("RECOVERY_REQUIRES_INDEPENDENT_DIRECTORIES")
            recorded = {}
            for entry in declaration["recoveryImplementation"]["files"]:
                source = base._inside(self.root, entry["path"])
                name = str(source.relative_to(self.root))
                if name in recorded or base._sha(source) != entry["sha256"]:
                    raise base.RecoveryError("RECOVERY_IMPLEMENTATION_DRIFT")
                recorded[name] = entry["sha256"]
            if not REQUIRED_FILES.issubset(recorded):
                raise base.RecoveryError("RECOVERY_IMPLEMENTATION_INCOMPLETE")
            if (recorded["frontend-v2/backend/shadow_recovery_r4.py"] != _LOADED_SHA
                    or recorded["frontend-v2/backend/shadow_recovery.py"] != _BASE_LOADED_SHA
                    or getattr(self.backend, "_recovery_r4_entrypoint_sha", None)
                    != recorded["frontend-v2/backend/recovery_r4_app.py"]):
                raise base.RecoveryError("RECOVERY_R4_LOADED_IMPLEMENTATION_DRIFT")
            runtime = getattr(self.backend, "_slot_aligned_capture_runtime", None)
            if runtime is None or self.backend._intraday_capture_loop != runtime.loop:
                raise base.RecoveryError("RECOVERY_R4_SCHEDULER_NOT_INSTALLED")
            for item in contract["files"]:
                name = str(base._inside(self.root, item["path"]).relative_to(self.root))
                if recorded.get(name) != item["sha256"]:
                    raise base.RecoveryError("RECOVERY_R4_SCHEDULER_BINDING_DRIFT")
            implementation = base._capture_v4.capture_implementation_contract()
            if (implementation["sha256"] != declaration["captureImplementationSha256"]
                    or implementation["runtime"].get("clientInjected") is not False):
                raise base.RecoveryError("RECOVERY_CAPTURE_IMPLEMENTATION_DRIFT")
            context = base._capture._provider_context(provider)
            binding = base._capture._slot_binding(provider=context, implementation=implementation, poll_seconds=60)
            if binding != declaration["providerBinding"] or target.isoformat() not in context["tradingDates"]:
                raise base.RecoveryError("RECOVERY_PROVIDER_BINDING_DRIFT")
            calibration, calibration_sha = base.load_calibration_declaration(calibration_path)
            if calibration_sha != declaration["calibrationDeclarationSha256"]:
                raise base.RecoveryError("RECOVERY_CALIBRATION_DECLARATION_DRIFT")
            base._predeclared(calibration, target)
            details, capture_contract = calibration["calibration"], calibration["captureContract"]
            if ({key: calibration.get(key) for key in metadata(contract["sha256"])} != metadata(contract["sha256"])
                    or details["tradeDate"] != target.isoformat()
                    or details["continuousSessionWindows"] != base._WINDOWS
                    or details["expectedSlotCount"] != 242 or details["finalizeAfter"] != "15:05:00"
                    or details["qualityGates"] != base._QUALITY_GATES
                    or any(type(value) is bool for value in details["qualityGates"].values())
                    or capture_contract["pollSeconds"] != 60 or capture_contract["requestedSymbolCount"] != 301
                    or capture_contract["binding"] != binding
                    or calibration["calibrationImplementationSha256"] != base.calibration_implementation_contract()["contractSha256"]):
                raise base.RecoveryError("RECOVERY_CALIBRATION_CONTRACT_INVALID")
            self.accepted_declaration_sha = digest
            self.error = None
            return {"declaration": declaration, "provider": provider, "output": output,
                    "calibrationPath": calibration_path, "calibrationSha": calibration_sha,
                    "calibration": calibration, "binding": binding, "implementation": implementation}
        except base.RecoveryError as exc:
            self.error = str(exc)
        except Exception:
            self.error = "RECOVERY_R4_BINDING_VERIFICATION_FAILED"
        return None

    def capture_config(self) -> dict[str, Any]:
        config = super().capture_config()
        verified = self.validate()
        return {**config, "recoveryTradeDate": verified["declaration"]["tradeDate"] if verified else None,
                "recoveryRuntimeMode": RUNTIME_MODE}

    def runtime_binding(self):
        verified = self.validate()
        if verified is None:
            return super().runtime_binding()
        def guarded_capture(*args, **kwargs):
            checked = self.validate()
            if checked is None:
                raise base.RecoveryError(self.error or "RECOVERY_R4_BINDING_INVALID")
            today = base._now().astimezone(base._TZ).date().isoformat()
            if today != checked["declaration"]["tradeDate"]:
                return {"status": "WAITING_CALIBRATION_DATE" if today < checked["declaration"]["tradeDate"]
                        else "CALIBRATION_DATE_ENDED", "autoShadowAccepted": False}
            if any(key in kwargs for key in ("now", "clock", "client")) or kwargs.get("force", False):
                raise base.RecoveryError("RECOVERY_R4_CAPTURE_OVERRIDE_FORBIDDEN")
            if type(kwargs.get("poll_seconds", 60)) is not int or kwargs.get("poll_seconds", 60) != 60:
                raise base.RecoveryError("RECOVERY_R4_POLL_SECONDS_DRIFT")
            if len(args) != 2 or Path(args[0]).resolve() != checked["provider"] or Path(args[1]).resolve() != checked["output"]:
                raise base.RecoveryError("RECOVERY_EFFECTIVE_CONFIG_DRIFT")
            return base._capture_v4.run_capture_cycle(*args, **kwargs)
        return guarded_capture, dict(verified["implementation"]["runtime"])

    def status(self) -> dict[str, Any]:
        result = super().status()
        runtime = getattr(self.backend, "_slot_aligned_capture_runtime", None)
        result.update(recoveryVersion=4, runtimeEntrypoint=ENTRYPOINT, schedulerMode=RUNTIME_MODE,
                      captureScheduledToday=result.get("tradeDate") == base._now().date().isoformat())
        result["verifiedGates"]["slotAlignedScheduler"] = self.error is None
        if runtime is not None:
            try:
                status = runtime.status()
                result["scheduler"] = {key: status.get(key) for key in
                    ("phase", "lastAttemptedSlot", "lastObservedAt", "errorCode")}
            except Exception:
                result["scheduler"] = {"phase": "UNKNOWN", "errorCode": "RECOVERY_R4_SCHEDULER_STATUS_UNAVAILABLE"}
        return result


def install_shadow_recovery_r4(backend: Any) -> ShadowRecoveryR4:
    existing = getattr(backend, "_shadow_recovery_r4_adapter", None)
    if existing is not None:
        return existing
    adapter = ShadowRecoveryR4(backend, os.environ.get(DECLARATION_KEY, ""))
    backend._shadow_recovery_r4_adapter = adapter
    backend._intraday_capture_config = adapter.capture_config
    backend._intraday_capture_runtime_binding = adapter.runtime_binding
    backend._intraday_capture_effective_provider_path = adapter.effective_provider
    backend._research_shadow_auto_config = lambda: adapter.disabled_trading_config(adapter.original_research_config)
    backend._intraday_shadow_config = lambda: adapter.disabled_trading_config(adapter.original_shadow_config)
    backend.app.router.routes[:] = [route for route in backend.app.router.routes
        if getattr(route, "path", None) != "/api/v1/shadow-recovery/status"]
    @backend.app.get("/api/v1/shadow-recovery/status")
    async def recovery_status():
        return {"success": True, "data": adapter.status()}
    backend.app.openapi_schema = None
    return adapter
