"""Independent r4 entrypoint: explicit process opt-in and a bound new scheduler."""
import os
from pathlib import Path

import app as backend
from system_settings import install_settings_routes
from shadow_recovery_r4 import install_shadow_recovery_r4, base

backend._recovery_r4_entrypoint_sha = base._sha(Path(__file__).resolve())
settings = install_settings_routes(backend)
recovery = install_shadow_recovery_r4(backend)
try:
    from market_data.slot_aligned_capture_runtime import install_slot_aligned_capture_runtime
    scheduler = install_slot_aligned_capture_runtime(backend)
except Exception:
    scheduler = None
    async def _unavailable_capture_loop():
        raise RuntimeError("RECOVERY_R4_SCHEDULER_UNAVAILABLE")
    backend._intraday_capture_loop = _unavailable_capture_loop
_original_readiness = backend._runtime_readiness_payload


def _r4_readiness(*args, **kwargs):
    result = _original_readiness(*args, **kwargs)
    verified = recovery.validate()
    result["recoveryR4"] = {"bindingVerified": verified is not None,
        "tradeDate": verified["declaration"]["tradeDate"] if verified else None,
        "runtimeEntrypoint": "recovery_r4_app", "accepted": False,
        "error": recovery.error}
    try:
        if scheduler is None or getattr(backend, "_slot_aligned_capture_runtime", None) is not scheduler:
            raise ValueError("missing scheduler")
        scheduler_state = scheduler.status()
        phase = scheduler_state["phase"]
        if not isinstance(phase, str) or not phase:
            raise ValueError("unavailable scheduler state")
        result["recoveryR4"]["schedulerPhase"] = phase
        scheduler_error = "RECOVERY_R4_SCHEDULER_CLOCK_INVALID" if phase == "CLOCK_INVALID" else None
    except Exception:
        scheduler_error = "RECOVERY_R4_SCHEDULER_STATUS_UNAVAILABLE"
    if verified is None:
        result.update(ready=False, status="unhealthy", configError=recovery.error)
    if scheduler_error:
        result.update(ready=False, status="unhealthy", configError=scheduler_error)
        result["recoveryR4"]["error"] = scheduler_error
    return result


backend._runtime_readiness_payload = _r4_readiness
app = backend.app

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=os.environ.get("BACKEND_HOST", "127.0.0.1"),
                port=int(os.environ.get("BACKEND_PORT", "8012")), log_level="info")
