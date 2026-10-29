"""
QuantaAlpha Backend API
FastAPI-based REST + WebSocket API for factor mining and backtesting.

Integrates with the core QuantaAlpha CLI to launch experiments
and reads factor library JSON for the factor browsing API.
"""

import asyncio
import glob
import json
import os
import re
import signal
import subprocess
import sys
import time
import uuid
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timedelta
from importlib import metadata as importlib_metadata
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional
from zoneinfo import ZoneInfo

import yaml
from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Resolve project root (two levels up from this file: frontend-v2/backend/)
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
# Ensure import quantaalpha is available (when backend is started from frontend-v2 directory, repo root is not in sys.path)
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
DOTENV_PATH = PROJECT_ROOT / ".env"
FRONTEND_DIST = PROJECT_ROOT / "frontend-v2" / "dist"
INTRADAY_PREFLIGHT_REPORT_PATH = (
    PROJECT_ROOT / "data" / "results" / "intraday-shadow-readiness-latest.json"
)
INTRADAY_PREFLIGHT_MAX_AGE_SECONDS = 900

# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------


@asynccontextmanager
async def _app_lifespan(_: FastAPI):
    background_tasks: dict[str, asyncio.Task[Any]] = {}
    try:
        if _research_shadow_auto_config()["enabled"]:
            background_tasks[RESEARCH_SHADOW_WORKER] = asyncio.create_task(
                _research_shadow_watch_loop(),
                name=RESEARCH_SHADOW_WORKER,
            )
        if _intraday_capture_config()["enabled"]:
            background_tasks[INTRADAY_CAPTURE_WORKER] = asyncio.create_task(
                _intraday_capture_loop(),
                name=INTRADAY_CAPTURE_WORKER,
            )
        if _intraday_shadow_config()["enabled"]:
            background_tasks[INTRADAY_SHADOW_WORKER] = asyncio.create_task(
                _intraday_shadow_loop(),
                name=INTRADAY_SHADOW_WORKER,
            )
        for name, task in background_tasks.items():
            _register_background_worker(name, task)
        yield
    finally:
        for task in background_tasks.values():
            task.cancel()
        for name, task in background_tasks.items():
            with suppress(asyncio.CancelledError, Exception):
                await task
            _mark_background_worker_stopped(name)
        _BACKGROUND_TASKS.clear()


app = FastAPI(title="QuantaAlpha API", version="2.0.0", lifespan=_app_lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000", "http://127.0.0.1:3000",
        "http://localhost:3001", "http://127.0.0.1:3001",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount(
    "/assets",
    StaticFiles(directory=FRONTEND_DIST / "assets", check_dir=False),
    name="frontend-assets",
)

# ========================== Pydantic Models ==========================


class MiningStartRequest(BaseModel):
    """Request to start a factor mining experiment."""
    direction: str = Field(..., description="Research direction, e.g. '价量因子挖掘'")
    numDirections: int = Field(2, ge=1, le=10, description="Initial exploration branches")
    maxRounds: int = Field(3, ge=1, le=20, description="Total evolution rounds")
    maxLoops: int = Field(2, ge=1, le=10, description="Workflow loops per evolution task")
    factorsPerHypothesis: int = Field(1, ge=1, le=5, description="Factors generated per hypothesis")
    librarySuffix: Optional[str] = Field(None, description="Factor library file suffix")
    qualityGateEnabled: bool = Field(True, description="Enable quality gate checks")
    parallelEnabled: bool = Field(False, description="Enable parallel execution within evolution phases")


class BacktestStartRequest(BaseModel):
    """Request to start an independent backtest."""
    factorJson: str = Field(..., description="Path to factor library JSON")
    factorSource: str = Field("custom", description="custom | combined")
    configPath: Optional[str] = Field(None, description="Path to backtest config")


class PaperTarget(BaseModel):
    symbol: str = Field(..., description="A-share symbol, for example 600000.SH")
    targetWeight: float = Field(..., ge=0, le=1)


class PaperSignalRequest(BaseModel):
    signalDate: str
    targets: List[PaperTarget] = Field(default_factory=list)


class PaperStrategySignalRequest(BaseModel):
    signalDate: Optional[str] = None
    topK: Optional[int] = Field(None, ge=1, le=100)
    exposure: Optional[float] = Field(None, gt=0, le=1)


class PaperRunRequest(BaseModel):
    tradeDate: Optional[str] = None


class PaperResetRequest(BaseModel):
    initialCash: float = Field(1_000_000, gt=0)


class SystemConfigUpdate(BaseModel):
    """Partial update to system configuration (.env)."""
    QLIB_DATA_DIR: Optional[str] = None
    QLIB_PROVIDER_URI: Optional[str] = None
    DATA_RESULTS_DIR: Optional[str] = None
    OPENAI_API_KEY: Optional[str] = None
    OPENAI_BASE_URL: Optional[str] = None
    CHAT_MODEL: Optional[str] = None
    REASONING_MODEL: Optional[str] = None


class ApiResponse(BaseModel):
    success: bool
    data: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    message: Optional[str] = None


# ========================== In-Memory State ==========================

tasks: Dict[str, Dict[str, Any]] = {}
ws_connections: Dict[str, List[WebSocket]] = {}  # task_id -> list of WS

ENABLE_LOG_DIAGNOSTICS = os.getenv("ENABLE_LOG_DIAGNOSTICS", "true").lower() not in {"0", "false", "no", "off"}

_SECRET_PATTERNS = (
    re.compile(r"sk-[A-Za-z0-9_-]{12,}"),
    re.compile(r"(?i)(api[_-]?key\s*[=:]\s*)([A-Za-z0-9_-]{12,})"),
)

_DIAGNOSTIC_RULES = [
    {
        "code": "missing_daily_pv",
        "severity": "error",
        "title": "缺少 daily_pv.h5",
        "patterns": ("daily_pv.h5",),
        "message": "因子执行目录没有找到 daily_pv.h5，因子值无法计算。",
        "suggestion": "检查数据软链接和 FACTOR_CoSTEER_DATA_FOLDER 配置，确保 daily_pv.h5 会被链接到工作目录。",
    },
    {
        "code": "missing_timeout",
        "severity": "error",
        "title": "缺少 timeout 命令",
        "patterns": ("timeout: command not found",),
        "message": "本机环境缺少 GNU timeout，子任务超时控制会失败。",
        "suggestion": "在 conda 环境安装 coreutils，或确保 PATH 中有 GNU timeout。",
    },
    {
        "code": "missing_libomp",
        "severity": "error",
        "title": "LightGBM 缺少 libomp",
        "patterns": ("libomp.dylib",),
        "message": "LightGBM 依赖的 OpenMP 动态库没有加载成功，Qlib 回测会中断。",
        "suggestion": "在 quantaalpha 环境中安装 llvm-openmp，然后重启后端并重新运行实验。",
    },
    {
        "code": "qlib_no_result",
        "severity": "error",
        "title": "Qlib 没有产出结果文件",
        "patterns": ("No result file found",),
        "message": "回测步骤没有生成预期结果文件，通常是更早的模型初始化或依赖错误导致。",
        "suggestion": "查看本诊断列表中更早出现的错误，例如 libomp、数据路径或模型依赖问题。",
    },
    {
        "code": "missing_report_artifact",
        "severity": "error",
        "title": "缺少回测报告 artifact",
        "patterns": ("report_normal_1day.pkl",),
        "message": "Qlib 没有生成或无法读取 portfolio_analysis/report_normal_1day.pkl。",
        "suggestion": "优先检查同一任务中更早的 Qlib 或 LightGBM 错误。",
    },
    {
        "code": "unknown_factor_variable",
        "severity": "error",
        "title": "因子表达式包含未知字段",
        "patterns": ("Unknown variable(s)",),
        "message": "因子表达式使用了数据中不存在的字段。",
        "suggestion": "只使用 $open、$close、$high、$low、$volume；日收益用 TS_PCTCHANGE($close, 1)。",
    },
    {
        "code": "llm_json_parse",
        "severity": "warning",
        "title": "LLM 输出 JSON 需要修复",
        "patterns": ("JSON fix failed", "JSON parse failed", "Unterminated string"),
        "message": "模型输出的 JSON 不完整或格式不合法，系统会尝试续写或修复，因此会变慢。",
        "suggestion": "如果频繁出现，可以减少每次生成因子数，或换更稳定的模型/开启更严格 JSON 模式。",
    },
    {
        "code": "factor_execution_timeout",
        "severity": "error",
        "title": "因子执行超时",
        "patterns": ("Execution timeout error", "TimeoutExpired"),
        "message": "单个因子计算超过了配置的超时时间。",
        "suggestion": "减少因子复杂度、缩短窗口，或适当提高 FACTOR_CoSTEER_FILE_BASED_EXECUTION_TIMEOUT。",
    },
    {
        "code": "python_module_missing",
        "severity": "error",
        "title": "Python 依赖缺失",
        "patterns": ("ModuleNotFoundError", "No module named"),
        "message": "当前 conda 环境缺少运行所需的 Python 包。",
        "suggestion": "根据日志中的模块名安装依赖，然后重启后端。",
    },
    {
        "code": "llm_api_error",
        "severity": "error",
        "title": "LLM API 调用失败",
        "patterns": ("AuthenticationError", "RateLimitError", "APIConnectionError", "litellm."),
        "message": "模型接口调用出现认证、限流或网络错误。",
        "suggestion": "检查 API key、base URL、模型名和网络状态；必要时降低并发或稍后重试。",
    },
]


# ========================== Utility Helpers ==========================

def _gen_id() -> str:
    return str(uuid.uuid4())[:8]


def _now() -> str:
    return datetime.now().isoformat()


PROCESS_INSTANCE_ID = uuid.uuid4().hex
PROCESS_STARTED_AT = _now()
PROCESS_STARTED_MONOTONIC = time.monotonic()
RESEARCH_SHADOW_WORKER = "researchShadowWatcher"
INTRADAY_CAPTURE_WORKER = "intradayCapture"
INTRADAY_SHADOW_WORKER = "intradayShadow"
BACKGROUND_WORKER_STARTUP_GRACE_SECONDS = 60.0
_BACKGROUND_TASKS: Dict[str, asyncio.Task[Any]] = {}
_BACKGROUND_WORKER_STATE: Dict[str, Dict[str, Any]] = {}


def _register_background_worker(name: str, task: asyncio.Task[Any]) -> None:
    now_monotonic = time.monotonic()
    _BACKGROUND_TASKS[name] = task
    _BACKGROUND_WORKER_STATE[name] = {
        "status": "STARTING",
        "registeredAt": _now(),
        "registeredMonotonic": now_monotonic,
        "lastHeartbeatAt": _now(),
        "lastHeartbeatMonotonic": now_monotonic,
        "consecutiveErrors": 0,
        "lastError": None,
    }


def _mark_background_worker_running(name: str, max_run_seconds: float) -> None:
    now_monotonic = time.monotonic()
    state = _BACKGROUND_WORKER_STATE.setdefault(name, {})
    state.update(
        {
            "status": "RUNNING",
            "lastStartedAt": _now(),
            "lastStartedMonotonic": now_monotonic,
            "lastHeartbeatAt": _now(),
            "lastHeartbeatMonotonic": now_monotonic,
            "maxRunSeconds": float(max_run_seconds),
        }
    )


def _mark_background_worker_idle(
    name: str,
    sleep_seconds: float,
    *,
    error: str | None = None,
) -> None:
    now_monotonic = time.monotonic()
    sleep_seconds = max(1.0, float(sleep_seconds))
    idle_grace_seconds = max(60.0, min(300.0, sleep_seconds / 2.0))
    state = _BACKGROUND_WORKER_STATE.setdefault(name, {})
    previous_errors = int(state.get("consecutiveErrors") or 0)
    state.update(
        {
            "status": "DEGRADED" if error else "IDLE",
            "lastCompletedAt": _now(),
            "lastHeartbeatAt": _now(),
            "lastHeartbeatMonotonic": now_monotonic,
            "nextDueAt": (
                datetime.now() + timedelta(seconds=sleep_seconds)
            ).isoformat(),
            "idleDeadlineMonotonic": (
                now_monotonic + sleep_seconds + idle_grace_seconds
            ),
            "idleGraceSeconds": idle_grace_seconds,
            "consecutiveErrors": previous_errors + 1 if error else 0,
            "lastError": _sanitize_log_line(error)[:1000] if error else None,
        }
    )


def _mark_background_worker_disabled(name: str) -> None:
    now_monotonic = time.monotonic()
    state = _BACKGROUND_WORKER_STATE.setdefault(name, {})
    state.update(
        {
            "status": "DISABLED",
            "lastHeartbeatAt": _now(),
            "lastHeartbeatMonotonic": now_monotonic,
            "consecutiveErrors": 0,
            "lastError": None,
        }
    )


def _mark_background_worker_stopped(name: str) -> None:
    state = _BACKGROUND_WORKER_STATE.setdefault(name, {})
    state.update(
        {
            "status": "STOPPED",
            "lastHeartbeatAt": _now(),
            "lastHeartbeatMonotonic": time.monotonic(),
        }
    )


def _background_worker_readiness(
    name: str,
    *,
    enabled: bool,
    now_monotonic: float,
) -> Dict[str, Any]:
    task = _BACKGROUND_TASKS.get(name)
    state = _BACKGROUND_WORKER_STATE.get(name) or {}
    process_age = max(0.0, now_monotonic - PROCESS_STARTED_MONOTONIC)
    status = str(state.get("status") or "STARTING")
    task_alive = bool(task is not None and not task.done())
    detail: Dict[str, Any] = {
        "name": name,
        "enabled": enabled,
        "ready": True,
        "status": "DISABLED" if not enabled else status,
        "taskAlive": task_alive,
        "reason": None,
        "lastStartedAt": state.get("lastStartedAt"),
        "lastCompletedAt": state.get("lastCompletedAt"),
        "lastHeartbeatAt": state.get("lastHeartbeatAt"),
        "nextDueAt": state.get("nextDueAt"),
        "consecutiveErrors": int(state.get("consecutiveErrors") or 0),
        "lastError": state.get("lastError"),
    }
    if not enabled:
        return detail
    if task is None:
        detail["ready"] = process_age <= BACKGROUND_WORKER_STARTUP_GRACE_SECONDS
        detail["reason"] = (
            "STARTING"
            if detail["ready"]
            else "TASK_NOT_REGISTERED"
        )
        return detail
    if task.done():
        detail["ready"] = False
        detail["reason"] = "TASK_EXITED"
        try:
            task_error = task.exception()
        except (asyncio.CancelledError, Exception) as exc:
            task_error = exc
        if task_error:
            detail["lastError"] = _sanitize_log_line(str(task_error))[:1000]
        return detail

    if status == "RUNNING":
        started = float(state.get("lastStartedMonotonic") or now_monotonic)
        max_run = float(state.get("maxRunSeconds") or 0.0)
        run_age = max(0.0, now_monotonic - started)
        detail["runAgeSeconds"] = round(run_age, 3)
        detail["maxRunSeconds"] = max_run
        if max_run <= 0 or run_age > max_run:
            detail["ready"] = False
            detail["reason"] = "RUN_TIMEOUT"
        return detail

    if status in {"IDLE", "DEGRADED"}:
        heartbeat = float(state.get("lastHeartbeatMonotonic") or now_monotonic)
        deadline = float(state.get("idleDeadlineMonotonic") or heartbeat)
        heartbeat_age = max(0.0, now_monotonic - heartbeat)
        detail["heartbeatAgeSeconds"] = round(heartbeat_age, 3)
        detail["idleGraceSeconds"] = float(state.get("idleGraceSeconds") or 0.0)
        if now_monotonic > deadline:
            detail["ready"] = False
            detail["reason"] = "HEARTBEAT_STALE"
        return detail

    if status in {"STARTING", "DISABLED"}:
        heartbeat = float(
            state.get("lastHeartbeatMonotonic")
            or state.get("registeredMonotonic")
            or PROCESS_STARTED_MONOTONIC
        )
        transition_age = max(0.0, now_monotonic - heartbeat)
        detail["transitionAgeSeconds"] = round(transition_age, 3)
        if transition_age > BACKGROUND_WORKER_STARTUP_GRACE_SECONDS:
            detail["ready"] = False
            detail["reason"] = "STARTUP_TIMEOUT"
        else:
            detail["reason"] = "STARTING"
        return detail

    detail["ready"] = False
    detail["reason"] = "INVALID_WORKER_STATE"
    return detail


def _runtime_readiness_payload(
    now_monotonic: float | None = None,
) -> Dict[str, Any]:
    now_monotonic = time.monotonic() if now_monotonic is None else now_monotonic
    config_error = None
    try:
        research_enabled = bool(_research_shadow_auto_config()["enabled"])
        intraday_enabled = bool(_intraday_capture_config()["enabled"])
        intraday_shadow_enabled = bool(_intraday_shadow_config()["enabled"])
    except Exception as exc:
        research_enabled = True
        intraday_enabled = True
        intraday_shadow_enabled = True
        config_error = _sanitize_log_line(str(exc))[:1000]
    workers = {
        RESEARCH_SHADOW_WORKER: _background_worker_readiness(
            RESEARCH_SHADOW_WORKER,
            enabled=research_enabled,
            now_monotonic=now_monotonic,
        ),
        INTRADAY_CAPTURE_WORKER: _background_worker_readiness(
            INTRADAY_CAPTURE_WORKER,
            enabled=intraday_enabled,
            now_monotonic=now_monotonic,
        ),
        INTRADAY_SHADOW_WORKER: _background_worker_readiness(
            INTRADAY_SHADOW_WORKER,
            enabled=intraday_shadow_enabled,
            now_monotonic=now_monotonic,
        ),
    }
    ready = config_error is None and all(
        worker["ready"] for worker in workers.values()
    )
    return {
        "status": "healthy" if ready else "unhealthy",
        "ready": ready,
        "timestamp": _now(),
        "pid": os.getpid(),
        "instanceId": PROCESS_INSTANCE_ID,
        "startedAt": PROCESS_STARTED_AT,
        "processAgeSeconds": round(
            max(0.0, now_monotonic - PROCESS_STARTED_MONOTONIC),
            3,
        ),
        "configError": config_error,
        "workers": workers,
    }


def _sanitize_log_line(line: str) -> str:
    """Mask likely API keys before logs are stored or sent to the UI."""
    sanitized = line
    sanitized = _SECRET_PATTERNS[0].sub(lambda m: f"{m.group(0)[:6]}...{m.group(0)[-4:]}", sanitized)
    sanitized = _SECRET_PATTERNS[1].sub(lambda m: f"{m.group(1)}***", sanitized)
    return sanitized


def _line_matches_rule(line: str, patterns: tuple[str, ...]) -> bool:
    lowered = line.lower()
    return any(pattern.lower() in lowered for pattern in patterns)


def _diagnostic_from_line(line: str, source: str) -> Optional[Dict[str, Any]]:
    if not ENABLE_LOG_DIAGNOSTICS:
        return None
    for rule in _DIAGNOSTIC_RULES:
        if not _line_matches_rule(line, rule["patterns"]):
            continue

        if rule["code"] == "python_module_missing":
            lowered = line.lower()
            is_optional_skip = (
                "(optional:" in lowered
                and (" is skipped" in lowered or " are skipped" in lowered)
            )
            if is_optional_skip:
                continue

        return {
            "id": _gen_id(),
            "timestamp": _now(),
            "lastSeen": _now(),
            "severity": rule["severity"],
            "code": rule["code"],
            "title": rule["title"],
            "message": rule["message"],
            "suggestion": rule["suggestion"],
            "source": source,
            "count": 1,
            "example": _sanitize_log_line(line)[:500],
        }
    return None


def _record_diagnostic(task: Dict[str, Any], line: str, source: str) -> Optional[Dict[str, Any]]:
    diagnostic = _diagnostic_from_line(line, source)
    if diagnostic is None:
        return None

    diagnostics = task.setdefault("diagnostics", [])
    for existing in diagnostics:
        if existing.get("code") == diagnostic["code"] and existing.get("source") == source:
            existing["count"] = int(existing.get("count", 1)) + 1
            existing["lastSeen"] = diagnostic["lastSeen"]
            existing["example"] = diagnostic["example"]
            return existing

    diagnostics.append(diagnostic)
    diagnostics.sort(key=lambda d: {"error": 0, "warning": 1, "info": 2}.get(d.get("severity", "info"), 3))
    return diagnostic


def _load_experiment_config() -> Dict[str, Any]:
    base_config_path = PROJECT_ROOT / "configs" / "experiment.yaml"
    with open(base_config_path, "r", encoding="utf-8") as _f:
        return yaml.safe_load(_f) or {}


def _apply_mining_overrides(run_cfg: Dict[str, Any], req: MiningStartRequest) -> Dict[str, Any]:
    planning_cfg = run_cfg.setdefault("planning", {})
    evolution_cfg = run_cfg.setdefault("evolution", {})
    exec_cfg = run_cfg.setdefault("execution", {})
    factor_cfg = run_cfg.setdefault("factor", {})
    qg = run_cfg.setdefault("quality_gate", {})

    num_directions = int(req.numDirections)
    max_rounds = int(req.maxRounds)
    max_loops = int(req.maxLoops)
    factors_per_hypothesis = int(req.factorsPerHypothesis)
    steps_per_loop = int(exec_cfg.get("steps_per_loop", 5) or 5)
    configured_step_n = exec_cfg.get("step_n")
    estimated_steps = int(configured_step_n) if configured_step_n is not None else max_loops * steps_per_loop

    # numDirections=1 should mean "run the provided direction directly", not "spend an extra LLM call re-planning one direction".
    planning_enabled = num_directions > 1
    parallel_enabled = bool(req.parallelEnabled)
    quality_gate_enabled = bool(req.qualityGateEnabled)

    planning_cfg["enabled"] = planning_enabled
    planning_cfg["num_directions"] = num_directions
    evolution_cfg["max_rounds"] = max_rounds
    evolution_cfg["parallel_enabled"] = parallel_enabled
    exec_cfg["max_loops"] = max_loops
    exec_cfg["parallel_execution"] = parallel_enabled
    factor_cfg["factors_per_hypothesis"] = factors_per_hypothesis

    if quality_gate_enabled:
        qg["complexity_enabled"] = True
        qg["redundancy_enabled"] = True
        qg["consistency_enabled"] = True
    else:
        qg["consistency_enabled"] = False
        qg["complexity_enabled"] = False
        qg["redundancy_enabled"] = False

    return {
        "numDirections": num_directions,
        "planningEnabled": planning_enabled,
        "maxRounds": max_rounds,
        "maxLoops": max_loops,
        "stepsPerLoop": steps_per_loop,
        "estimatedStepsPerTask": estimated_steps,
        "factorsPerHypothesis": factors_per_hypothesis,
        "parallelEnabled": parallel_enabled,
        "qualityGateEnabled": quality_gate_enabled,
        "yaml": {
            "planning.num_directions": num_directions,
            "planning.enabled": planning_enabled,
            "evolution.max_rounds": max_rounds,
            "evolution.parallel_enabled": parallel_enabled,
            "execution.max_loops": max_loops,
            "execution.parallel_execution": parallel_enabled,
            "factor.factors_per_hypothesis": factors_per_hypothesis,
            "quality_gate.enabled": quality_gate_enabled,
            "quality_gate.consistency_enabled": qg["consistency_enabled"],
        },
    }


def _load_dotenv_dict() -> Dict[str, str]:
    """Parse the .env file into a dict (simple key=value, ignoring comments)."""
    env: Dict[str, str] = {}
    if DOTENV_PATH.exists():
        for line in DOTENV_PATH.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if "=" in stripped:
                key, _, val = stripped.partition("=")
                env[key.strip()] = val.strip()
    return env


def _find_factor_jsons() -> List[str]:
    """Find all factor library JSON files in data/factorlib/."""
    factorlib_dir = PROJECT_ROOT / "data" / "factorlib"
    pattern = str(factorlib_dir / "all_factors_library*.json")
    results = sorted(glob.glob(pattern), key=os.path.getmtime, reverse=True)

    old_pattern = str(PROJECT_ROOT / "all_factors_library*.json")
    old_results = sorted(glob.glob(old_pattern), key=os.path.getmtime, reverse=True)

    seen = set(results)
    for r in old_results:
        if r not in seen:
            results.append(r)
    return results


def _load_factor_library(path: str) -> Dict[str, Any]:
    """Load and parse a factor library JSON file."""
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _classify_quality(backtest_results: Dict[str, Any]) -> str:
    """Classify factor quality based on backtest metrics."""
    if not backtest_results:
        return "low"
    # Use information ratio or IC-related metrics
    ic = None
    for key in ["1day.excess_return_without_cost.information_ratio",
                 "1day.excess_return_with_cost.information_ratio"]:
        if key in backtest_results:
            ic = backtest_results[key]
            break
    if ic is None:
        # Try to find any IC-like metric
        for key, val in backtest_results.items():
            if "information_ratio" in key and isinstance(val, (int, float)):
                ic = val
                break
    if ic is None:
        return "medium"
    if ic > 0.5:
        return "high"
    if ic > 0.1:
        return "medium"
    return "low"


async def _broadcast(task_id: str, message: Dict[str, Any]):
    """Send a JSON message to all WebSocket clients for a task."""
    if task_id not in ws_connections:
        return
    dead: List[WebSocket] = []
    for ws in ws_connections[task_id]:
        try:
            await ws.send_json(message)
        except Exception:
            dead.append(ws)
    for ws in dead:
        ws_connections[task_id].remove(ws)


async def _record_and_broadcast_diagnostic(task_id: str, task: Dict[str, Any], line: str, source: str):
    diagnostic = _record_diagnostic(task, line, source)
    if diagnostic is None:
        return
    await _broadcast(task_id, {
        "type": "diagnostic",
        "taskId": task_id,
        "data": diagnostic,
        "timestamp": _now(),
    })


# ========================== Mining Process ==========================

async def _run_mining(task_id: str, req: MiningStartRequest):
    """
    Launch the actual QuantaAlpha mining experiment as a subprocess
    and stream its output over WebSocket.
    """
    task = tasks[task_id]
    try:
        # Build the command
        env = os.environ.copy()
        # Load .env into env
        dotenv = _load_dotenv_dict()
        env.update(dotenv)

        # Use experiment_id as suffix to guarantee isolation
        experiment_id = f"exp_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        env["EXPERIMENT_ID"] = experiment_id
        
        # Enforce unique library suffix if not provided
        if not req.librarySuffix:
            req.librarySuffix = experiment_id
            # Update task config so frontend knows the suffix
            task["config"]["librarySuffix"] = req.librarySuffix
            
        env["FACTOR_LIBRARY_SUFFIX"] = req.librarySuffix

        results_base = dotenv.get("DATA_RESULTS_DIR", str(PROJECT_ROOT / "data" / "results"))
        env["WORKSPACE_PATH"] = f"{results_base}/workspace_{experiment_id}"
        env["PICKLE_CACHE_FOLDER_PATH_STR"] = f"{results_base}/pickle_cache_{experiment_id}"

        os.makedirs(env["WORKSPACE_PATH"], exist_ok=True)
        os.makedirs(env["PICKLE_CACHE_FOLDER_PATH_STR"], exist_ok=True)
        full_log_path = Path(env["WORKSPACE_PATH"]) / "run.log"
        task["fullLogPath"] = str(full_log_path)

        # Qlib symlink
        qlib_data = dotenv.get("QLIB_DATA_DIR", "")
        if qlib_data:
            qlib_symlink_dir = Path.home() / ".qlib" / "qlib_data"
            qlib_symlink_dir.mkdir(parents=True, exist_ok=True)
            cn_data_link = qlib_symlink_dir / "cn_data"
            if not cn_data_link.exists() or os.readlink(str(cn_data_link)) != qlib_data:
                if cn_data_link.is_symlink():
                    cn_data_link.unlink()
                cn_data_link.symlink_to(qlib_data)

        # Build a temporary config with frontend parameter overrides
        base_config_path = PROJECT_ROOT / "configs" / "experiment.yaml"
        config_path_to_use = str(base_config_path)

        try:
            run_cfg = _load_experiment_config()
            effective_config = _apply_mining_overrides(run_cfg, req)

            # Write to a temporary file so the original is untouched
            tmp_dir = Path(env.get("WORKSPACE_PATH", "/tmp"))
            tmp_dir.mkdir(parents=True, exist_ok=True)
            tmp_cfg = tmp_dir / "experiment_override.yaml"
            with open(tmp_cfg, "w", encoding="utf-8") as _f:
                yaml.safe_dump(run_cfg, _f, allow_unicode=True, default_flow_style=False)
            config_path_to_use = str(tmp_cfg)
            effective_config["overrideConfigPath"] = config_path_to_use
            task["effectiveConfig"] = effective_config
        except Exception as cfg_err:
            # Fall back to original config if anything fails
            import traceback
            traceback.print_exc()

        # Build CLI args
        cmd = [
            sys.executable, "-m", "quantaalpha.cli", "mine",
            "--direction", req.direction,
            "--config_path", config_path_to_use,
        ]

        task["status"] = "running"
        task["progress"]["phase"] = "planning"
        task["progress"]["message"] = "正在启动实验..."
        task["updatedAt"] = _now()

        await _broadcast(task_id, {
            "type": "progress",
            "taskId": task_id,
            "data": task["progress"],
            "timestamp": _now(),
        })
        config_summary = task.get("effectiveConfig") or {}
        config_log = {
            "id": _gen_id(),
            "timestamp": _now(),
            "level": "info",
            "message": (
                "生效配置: "
                f"初始探索分支={config_summary.get('numDirections', req.numDirections)}, "
                f"进化轮次={config_summary.get('maxRounds', req.maxRounds)}, "
                f"每任务循环={config_summary.get('maxLoops', req.maxLoops)}, "
                f"每假设因子={config_summary.get('factorsPerHypothesis', req.factorsPerHypothesis)}, "
                f"并行执行={'on' if config_summary.get('parallelEnabled', req.parallelEnabled) else 'off'}, "
                f"质量检查={'on' if config_summary.get('qualityGateEnabled', req.qualityGateEnabled) else 'off'}"
            ),
        }
        task["logs"].append(config_log)
        await _broadcast(task_id, {
            "type": "log",
            "taskId": task_id,
            "data": config_log,
            "timestamp": _now(),
        })
        await _broadcast(task_id, {
            "type": "result",
            "taskId": task_id,
            "data": {"status": task["status"], "effectiveConfig": task.get("effectiveConfig")},
            "timestamp": _now(),
        })

        # Launch subprocess
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=str(PROJECT_ROOT),
            env=env,
        )
        task["pid"] = proc.pid

        # Stream stdout line by line
        line_count = 0
        current_phase = "planning"
        run_log_file = full_log_path.open("a", encoding="utf-8")

        # Noisy patterns to suppress (shared with backtest)
        _MINING_NOISE = (
            "field data contains nan",
            "common_infra",
            "PyTorch models are skipped",
            "UserWarning: pkg_resources",
            "FutureWarning",
            "UserWarning",
            "Training until validation scores",
            "Did not meet early stopping",
        )

        try:
            while True:
                line_bytes = await proc.stdout.readline()
                if not line_bytes:
                    break
                line = _sanitize_log_line(line_bytes.decode("utf-8", errors="replace").rstrip())
                if not line:
                    continue
                run_log_file.write(line + "\n")
                run_log_file.flush()
                await _record_and_broadcast_diagnostic(task_id, task, line, "mining")
                line_count += 1

                # Skip noisy warnings
                if any(p in line for p in _MINING_NOISE):
                    continue

                # Detect phase from log messages
                new_phase = current_phase
                if "factor_propose" in line:
                    new_phase = "evolving"
                elif "factor_backtest" in line or "backtest" in line.lower():
                    new_phase = "backtesting"
                elif "feedback" in line:
                    new_phase = "analyzing"
                elif "factor_calculate" in line:
                    new_phase = "evolving"
                elif "规划" in line or "planning" in line.lower():
                    new_phase = "planning"
                elif "进化完成" in line or "程序执行完成" in line:
                    new_phase = "completed"

                if new_phase != current_phase:
                    current_phase = new_phase
                    task["progress"]["phase"] = current_phase
                    task["progress"]["message"] = line[:200]
                    task["progress"]["timestamp"] = _now()
                    await _broadcast(task_id, {
                        "type": "progress",
                        "taskId": task_id,
                        "data": task["progress"],
                        "timestamp": _now(),
                    })

                # Send log every line (throttle to avoid flooding)
                if line_count % 3 == 0 or "INFO" in line or "ERROR" in line or "WARNING" in line:
                    level = "info"
                    if "ERROR" in line or "Error" in line:
                        level = "error"
                    elif "WARNING" in line or "Warning" in line:
                        level = "warning"
                    elif "完成" in line or "success" in line.lower():
                        level = "success"

                    log_entry = {
                        "id": _gen_id(),
                        "timestamp": _now(),
                        "level": level,
                        "message": line[:500],
                    }
                    task["logs"].append(log_entry)
                    # Keep only last 500 logs in memory
                    if len(task["logs"]) > 500:
                        task["logs"] = task["logs"][-500:]

                    await _broadcast(task_id, {
                        "type": "log",
                        "taskId": task_id,
                        "data": log_entry,
                        "timestamp": _now(),
                    })

                # Extract metrics from log lines like "RankIC=0.0016"
                if "RankIC=" in line:
                    try:
                        rank_ic_str = line.split("RankIC=")[1].split(",")[0].split(")")[0]
                        task["metrics"]["rankIc"] = float(rank_ic_str)
                        await _broadcast(task_id, {
                            "type": "metrics",
                            "taskId": task_id,
                            "data": task["metrics"],
                            "timestamp": _now(),
                        })
                    except Exception:
                        pass
                
                # Check for factor saving to update top factors list
                if "已保存" in line or "因子" in line:
                    _update_mining_metrics(task)
                    if task.get("metrics"):
                         await _broadcast(task_id, {
                            "type": "result",
                            "taskId": task_id,
                            "data": {
                                "status": task["status"],
                                "metrics": task["metrics"],
                                "diagnostics": task.get("diagnostics", []),
                                "effectiveConfig": task.get("effectiveConfig"),
                            },
                            "timestamp": _now(),
                        })
        finally:
            run_log_file.close()

        exit_code = await proc.wait()
        task["pid"] = None

        if exit_code == 0:
            task["status"] = "completed"
            task["progress"]["phase"] = "completed"
            task["progress"]["progress"] = 100
            task["progress"]["message"] = "实验完成"
        else:
            task["status"] = "failed"
            task["progress"]["message"] = f"实验失败 (exit code: {exit_code})"

        task["updatedAt"] = _now()

        # Load final factor count from the library JSON
        # Prefer the library file matching the librarySuffix for this experiment
        _update_mining_metrics(task)

        await _broadcast(task_id, {
            "type": "result",
            "taskId": task_id,
            "data": {
                "status": task["status"],
                "metrics": task["metrics"],
                "diagnostics": task.get("diagnostics", []),
                "effectiveConfig": task.get("effectiveConfig"),
            },
            "timestamp": _now(),
        })

    except Exception as e:
        task["status"] = "failed"
        task["progress"]["message"] = f"Error: {str(e)}"
        task["updatedAt"] = _now()
        _record_diagnostic(task, str(e), "mining")
        await _broadcast(task_id, {
            "type": "error",
            "taskId": task_id,
            "data": {"error": str(e), "diagnostics": task.get("diagnostics", [])},
            "timestamp": _now(),
        })


# ========================== API Endpoints ==========================

@app.get("/", include_in_schema=False)
async def root():
    index_path = FRONTEND_DIST / "index.html"
    if index_path.is_file():
        return FileResponse(index_path)
    return {"message": "QuantaAlpha API", "version": "2.0.0"}


@app.get("/api/health")
async def health_check():
    return {
        "status": "healthy",
        "timestamp": _now(),
        "pid": os.getpid(),
        "instanceId": PROCESS_INSTANCE_ID,
        "startedAt": PROCESS_STARTED_AT,
    }


@app.get("/api/health/ready")
async def readiness_check():
    payload = _runtime_readiness_payload()
    return JSONResponse(
        status_code=200 if payload["ready"] else 503,
        content=payload,
    )


# ---- Mining endpoints ----

@app.post("/api/v1/mining/start", response_model=ApiResponse)
async def start_mining(req: MiningStartRequest):
    """Start a new factor mining experiment."""
    task_id = _gen_id()
    effective_config = None
    try:
        preview_cfg = _load_experiment_config()
        effective_config = _apply_mining_overrides(preview_cfg, req)
    except Exception:
        effective_config = None

        task = {
        "taskId": task_id,
        "status": "running",
        "config": req.model_dump(),
        "effectiveConfig": effective_config,
        "progress": {
            "phase": "parsing",
            "currentRound": 0,
            "totalRounds": req.maxRounds or 1,
            "progress": 0,
            "message": "正在初始化实验...",
            "timestamp": _now(),
        },
        "logs": [],
        "diagnostics": [],
        "fullLogPath": None,
        "metrics": {
            "ic": 0, "icir": 0, "rankIc": 0, "rankIcir": 0,
            "annualReturn": 0, "sharpeRatio": 0, "maxDrawdown": 0,
            "totalFactors": 0, "highQualityFactors": 0,
            "mediumQualityFactors": 0, "lowQualityFactors": 0,
        },
        "result": None,
        "pid": None,
        "createdAt": _now(),
        "updatedAt": _now(),
    }
    tasks[task_id] = task

    # Launch the mining process in background
    asyncio.create_task(_run_mining(task_id, req))

    return ApiResponse(
        success=True,
        data={"taskId": task_id, "task": task},
        message="实验已启动",
    )


@app.get("/api/v1/mining/{task_id}", response_model=ApiResponse)
async def get_mining_status(task_id: str):
    """Get task status."""
    if task_id not in tasks:
        raise HTTPException(status_code=404, detail="Task not found")
    return ApiResponse(success=True, data={"task": tasks[task_id]})


@app.delete("/api/v1/mining/{task_id}", response_model=ApiResponse)
async def cancel_mining(task_id: str):
    """Cancel a running mining task."""
    if task_id not in tasks:
        raise HTTPException(status_code=404, detail="Task not found")
    task = tasks[task_id]
    if task.get("pid"):
        try:
            pid = task["pid"]
            # Try graceful termination first
            os.kill(pid, signal.SIGTERM)
            
            # Wait briefly for cleanup (0.5s)
            for _ in range(5):
                try:
                    os.kill(pid, 0) # Check if alive
                    await asyncio.sleep(0.1)
                except ProcessLookupError:
                    break
            
            # Force kill if still running
            try:
                os.kill(pid, 0)
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        except ProcessLookupError:
            pass
    task["status"] = "cancelled"
    task["updatedAt"] = _now()
    await _broadcast(task_id, {
        "type": "result",
        "taskId": task_id,
        "data": {"status": "cancelled"},
        "timestamp": _now(),
    })
    return ApiResponse(success=True, message="任务已取消")


@app.get("/api/v1/mining/tasks/list", response_model=ApiResponse)
async def list_tasks():
    """List all tasks."""
    task_list = sorted(tasks.values(), key=lambda t: t["createdAt"], reverse=True)
    return ApiResponse(success=True, data={"tasks": task_list})


# ---- Factor library endpoints ----

@app.get("/api/v1/factors", response_model=ApiResponse)
async def get_factors(
    quality: Optional[str] = Query(None, description="Filter by quality: high/medium/low"),
    search: Optional[str] = Query(None, description="Search by factor name"),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    library: Optional[str] = Query(None, description="Specific library file name"),
):
    """Get factors from the factor library JSON."""
    # Find the most recent factor library
    if library:
        lib_path = str(PROJECT_ROOT / "data" / "factorlib" / library)
        # Fallback: check if file exists at project root (legacy location)
        if not Path(lib_path).exists():
            alt = str(PROJECT_ROOT / library)
            if Path(alt).exists():
                lib_path = alt
    else:
        jsons = _find_factor_jsons()
        if not jsons:
            return ApiResponse(
                success=True,
                data={"factors": [], "total": 0, "limit": limit, "offset": offset,
                      "libraries": []},
            )
        lib_path = jsons[0]

    try:
        raw = _load_factor_library(lib_path)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to read factor library: {e}")

    factors_dict = raw.get("factors", {})
    metadata = raw.get("metadata", {})

    # Convert dict to list with quality classification
    factors_list: List[Dict[str, Any]] = []
    for factor_id, factor_info in factors_dict.items():
        if not isinstance(factor_info, dict):
            continue
        bt = factor_info.get("backtest_results", {})
        q = _classify_quality(bt)
        # Extract metrics with proper fallbacks
        # Try specific keys first, then standard ones
        ic = bt.get("IC", bt.get("1day.excess_return_without_cost.information_coefficient", 0))
        icir = bt.get("ICIR", bt.get("1day.excess_return_without_cost.information_coefficient_ir", 0))
        rank_ic = bt.get("Rank IC", bt.get("rank_ic", bt.get("1day.excess_return_without_cost.rank_ic", 0)))
        rank_icir = bt.get("Rank ICIR", bt.get("rank_ic_ir", bt.get("1day.excess_return_without_cost.rank_ic_ir", 0)))
        
        factor_entry = {
            "factorId": factor_info.get("factor_id", factor_id),
            "factorName": factor_info.get("factor_name", "Unknown"),
            "factorExpression": factor_info.get("factor_expression", ""),
            "factorDescription": factor_info.get("factor_description", ""),
            "factorFormulation": factor_info.get("factor_formulation", ""),
            "quality": q,
            "backtestResults": bt,
            # Extract key metrics
            "ic": ic,
            "icir": icir,
            "rankIc": rank_ic,
            "rankIcir": rank_icir,
            "annualReturn": bt.get("1day.excess_return_with_cost.annualized_return", 
                                  bt.get("1day.excess_return_without_cost.annualized_return", 0)),
            "maxDrawdown": bt.get("1day.excess_return_with_cost.max_drawdown", 
                                 bt.get("1day.excess_return_without_cost.max_drawdown", 0)),
            "sharpeRatio": bt.get("1day.excess_return_with_cost.information_ratio", 
                                bt.get("1day.excess_return_without_cost.information_ratio", 0)),
            "round": factor_info.get("evolution_metadata", {}).get("round", 0)
            if isinstance(factor_info.get("evolution_metadata"), dict) else 0,
            "direction": factor_info.get("evolution_metadata", {}).get("direction_index", "")
            if isinstance(factor_info.get("evolution_metadata"), dict) else "",
            "createdAt": factor_info.get("added_at", ""),
        }
        factors_list.append(factor_entry)

    # Apply filters
    if quality:
        factors_list = [f for f in factors_list if f["quality"] == quality]
    if search:
        search_lower = search.lower()
        factors_list = [
            f for f in factors_list
            if search_lower in f["factorName"].lower()
            or search_lower in f.get("factorDescription", "").lower()
            or search_lower in f.get("factorExpression", "").lower()
        ]

    total = len(factors_list)
    paginated = factors_list[offset: offset + limit]

    # Available library files
    all_libs = [Path(p).name for p in _find_factor_jsons()]

    return ApiResponse(
        success=True,
        data={
            "factors": paginated,
            "total": total,
            "limit": limit,
            "offset": offset,
            "metadata": metadata,
            "libraries": all_libs,
        },
    )


# ---- Factor cache endpoints ----
# IMPORTANT: These must be registered BEFORE /api/v1/factors/{factor_id}
# otherwise FastAPI matches "cache-status" as a factor_id parameter.

@app.get("/api/v1/factors/cache-status", response_model=ApiResponse)
async def get_cache_status(
    library: Optional[str] = Query(None, description="Factor library JSON filename"),
):
    """Check cache status of factors in the specified factor library."""
    if library:
        lib_path = str(PROJECT_ROOT / "data" / "factorlib" / library)
        if not Path(lib_path).exists():
            alt = str(PROJECT_ROOT / library)
            if Path(alt).exists():
                lib_path = alt
    else:
        jsons = _find_factor_jsons()
        if not jsons:
            return ApiResponse(success=True, data={
                "total": 0, "h5_cached": 0, "md5_cached": 0,
                "need_compute": 0, "factors": [],
            })
        lib_path = jsons[0]

    if not Path(lib_path).exists():
        raise HTTPException(status_code=404, detail=f"Factor library not found: {library}")

    # Import from core library
    from quantaalpha.factors.library import FactorLibraryManager
    result = FactorLibraryManager.check_cache_status(lib_path)
    return ApiResponse(success=True, data=result)


@app.post("/api/v1/factors/warm-cache", response_model=ApiResponse)
async def warm_cache(
    library: Optional[str] = Query(None, description="Factor library JSON filename"),
):
    """Batch sync from result.h5 to MD5 cache directory."""
    if library:
        lib_path = str(PROJECT_ROOT / "data" / "factorlib" / library)
        if not Path(lib_path).exists():
            alt = str(PROJECT_ROOT / library)
            if Path(alt).exists():
                lib_path = alt
    else:
        jsons = _find_factor_jsons()
        if not jsons:
            return ApiResponse(success=False, error="未找到因子库文件")
        lib_path = jsons[0]

    if not Path(lib_path).exists():
        raise HTTPException(status_code=404, detail=f"Factor library not found: {library}")

    from quantaalpha.factors.library import FactorLibraryManager
    result = FactorLibraryManager.warm_cache_from_json(lib_path)
    # Build a clear message
    parts = []
    if result['synced']:
        parts.append(f"新同步 {result['synced']} 个")
    if result.get('already_cached'):
        parts.append(f"已有缓存 {result['already_cached']} 个")
    if result.get('no_source'):
        parts.append(f"无H5源 {result['no_source']} 个(回测时从表达式计算)")
    if result['failed']:
        parts.append(f"失败 {result['failed']} 个")
    msg = "，".join(parts) if parts else "无需操作"
    return ApiResponse(
        success=True,
        data=result,
        message=msg,
    )


# ---- Factor library list endpoint (must be BEFORE {factor_id} route) ----

@app.get("/api/v1/factors/libraries", response_model=ApiResponse)
async def list_factor_libraries():
    """List all factor library JSON files in the project root."""
    libs = [Path(p).name for p in _find_factor_jsons()]
    return ApiResponse(success=True, data={"libraries": libs})


@app.get("/api/v1/factors/{factor_id}", response_model=ApiResponse)
async def get_factor_detail(factor_id: str):
    """Get full detail of a single factor."""
    jsons = _find_factor_jsons()
    for lib_path in jsons:
        try:
            raw = _load_factor_library(lib_path)
            factors = raw.get("factors", {})
            if factor_id in factors:
                info = factors[factor_id]
                return ApiResponse(success=True, data={"factor": info})
        except Exception:
            continue
    raise HTTPException(status_code=404, detail="Factor not found")


# ---- Backtest endpoints ----

@app.post("/api/v1/backtest/start", response_model=ApiResponse)
async def start_backtest(req: BacktestStartRequest):
    """Start an independent backtest."""
    task_id = _gen_id()
    config_path = req.configPath or str(PROJECT_ROOT / "configs" / "backtest.yaml")

    task = {
        "taskId": task_id,
        "status": "running",
        "type": "backtest",
        "config": {**req.model_dump(), "configPath": config_path, "outputName": f"backtest_{task_id}"},
        "progress": {
            "phase": "backtesting",
            "currentRound": 0,
            "totalRounds": 1,
            "progress": 0,
            "message": "正在启动回测...",
            "timestamp": _now(),
        },
        "logs": [],
        "metrics": {},
        "result": None,
        "pid": None,
        "createdAt": _now(),
        "updatedAt": _now(),
    }
    tasks[task_id] = task

    # Launch backtest in background
    asyncio.create_task(_run_backtest(task_id, req, config_path))
    return ApiResponse(
        success=True,
        data={"taskId": task_id, "task": task},
        message="回测已启动",
    )


@app.get("/api/v1/backtest/{task_id}", response_model=ApiResponse)
async def get_backtest_status(task_id: str):
    """Get backtest task status and results."""
    if task_id not in tasks:
        raise HTTPException(status_code=404, detail="Task not found")
    return ApiResponse(success=True, data={"task": tasks[task_id]})


@app.delete("/api/v1/backtest/{task_id}", response_model=ApiResponse)
async def cancel_backtest(task_id: str):
    """Cancel a running backtest task."""
    if task_id not in tasks:
        raise HTTPException(status_code=404, detail="Task not found")
    task = tasks[task_id]
    if task.get("pid"):
        try:
            os.kill(task["pid"], signal.SIGTERM)
        except ProcessLookupError:
            pass
    task["status"] = "cancelled"
    task["updatedAt"] = _now()
    await _broadcast(task_id, {
        "type": "result",
        "taskId": task_id,
        "data": {"status": "cancelled"},
        "timestamp": _now(),
    })
    return ApiResponse(success=True, message="回测已取消")


async def _run_backtest(task_id: str, req: BacktestStartRequest, config_path: str):
    """Run the independent backtest (V2) as a subprocess."""
    task = tasks[task_id]
    try:
        env = os.environ.copy()
        dotenv = _load_dotenv_dict()
        env.update(dotenv)

        # --- Resolve factor JSON path ---
        # Frontend sends just the filename (e.g. "all_factors_library_test3hjback.json")
        # We need to resolve it to the full path under data/factorlib/
        factor_json_input = req.factorJson
        factor_json_path = Path(factor_json_input)
        if not factor_json_path.is_absolute():
            # Check data/factorlib/ first
            candidate = PROJECT_ROOT / "data" / "factorlib" / factor_json_input
            if candidate.exists():
                factor_json_path = candidate
            else:
                # Try as relative to project root
                candidate2 = PROJECT_ROOT / factor_json_input
                if candidate2.exists():
                    factor_json_path = candidate2
                else:
                    factor_json_path = candidate  # will fail with a clear error message
        factor_json_str = str(factor_json_path)

        # --- Find the correct Python executable ---
        # Prefer the conda env that has qlib installed
        conda_env = dotenv.get("CONDA_ENV_NAME", "quantaalpha")
        python_bin = sys.executable  # fallback

        # Dynamically detect conda base path (portable, no hardcoded paths)
        conda_prefixes = [os.path.expanduser(f"~/.conda/envs/{conda_env}")]
        try:
            import subprocess as _sp
            conda_base = _sp.check_output(
                ["conda", "info", "--base"], text=True, timeout=5
            ).strip()
            conda_prefixes.insert(0, os.path.join(conda_base, "envs", conda_env))
        except Exception:
            pass
        # Also check CONDA_PREFIX if we're already in the right env
        if os.environ.get("CONDA_PREFIX"):
            conda_prefixes.insert(0, os.environ["CONDA_PREFIX"])

        for prefix in conda_prefixes:
            candidate_bin = Path(prefix) / "bin" / "python"
            if candidate_bin.exists():
                python_bin = str(candidate_bin)
                break

        # Build CLI command
        cmd = [
            python_bin, "-m", "quantaalpha.backtest.run_backtest",
            "-c", config_path,
            "--factor-source", req.factorSource,
            "--factor-json", factor_json_str,
            "--output-name", task["config"]["outputName"],
            "--skip-uncached",
            "-v",
        ]

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=str(PROJECT_ROOT),
            env=env,
        )
        task["pid"] = proc.pid

        # Noisy warnings from Qlib / dependencies that can be safely suppressed
        _NOISY_PATTERNS = (
            "field data contains nan",
            "common_infra",
            "PyTorch models are skipped",
            "UserWarning: pkg_resources",
            "Training until validation scores",
            "FutureWarning",
            "UserWarning",
            "Did not meet early stopping",
            "num_leaves is set=",
        )

        # --- Stream stdout ---
        log_entry = None
        while True:
            line_bytes = await proc.stdout.readline()
            if not line_bytes:
                break
            line = line_bytes.decode("utf-8", errors="replace").rstrip()
            if not line:
                continue

            # Skip noisy repeated warnings
            if any(p in line for p in _NOISY_PATTERNS):
                continue

            level = "info"
            if "ERROR" in line or "Error" in line:
                level = "error"
            elif "WARNING" in line or "Warning" in line:
                level = "warning"
            elif "完成" in line or "success" in line.lower() or "✓" in line:
                level = "success"

            log_entry = {
                "id": _gen_id(),
                "timestamp": _now(),
                "level": level,
                "message": line[:500],
            }
            task["logs"].append(log_entry)
            if len(task["logs"]) > 2000:
                task["logs"] = task["logs"][-2000:]

            # Broadcast log to WebSocket
            await _broadcast(task_id, {
                "type": "log",
                "taskId": task_id,
                "data": log_entry,
                "timestamp": _now(),
            })

            # Update progress for meaningful lines
            if any(kw in line for kw in ["因子", "回测", "模型", "训练", "完成", "加载",
                                          "[1/4]", "[2/4]", "[3/4]", "[4/4]", "结果"]):
                task["progress"]["message"] = line[:200]

                # Estimate progress from run_backtest step markers
                if "[1/4]" in line:
                    task["progress"]["progress"] = 15
                elif "[2/4]" in line:
                    task["progress"]["progress"] = 35
                elif "[3/4]" in line:
                    task["progress"]["progress"] = 55
                elif "[4/4]" in line:
                    task["progress"]["progress"] = 75
                elif "结果已保存" in line or "回测结果" in line:
                    task["progress"]["progress"] = 95

                task["progress"]["timestamp"] = _now()
                await _broadcast(task_id, {
                    "type": "progress",
                    "taskId": task_id,
                    "data": task["progress"],
                    "timestamp": _now(),
                })

        # --- Process exit ---
        exit_code = await proc.wait()
        task["pid"] = None
        task["updatedAt"] = _now()

        # A zero process exit is necessary but not sufficient: the task owns one
        # exact result file and must contain real portfolio metrics.
        if exit_code == 0:
            result_error = _load_backtest_results(task)
            if result_error is None:
                task["status"] = "completed"
                task["progress"]["phase"] = "completed"
                task["progress"]["progress"] = 100
                task["progress"]["message"] = "回测完成"
            else:
                task["status"] = "failed"
                task["progress"]["phase"] = "failed"
                task["progress"]["message"] = result_error
        else:
            task["status"] = "failed"
            task["progress"]["phase"] = "failed"
            task["progress"]["message"] = f"回测失败 (exit code: {exit_code})"

        await _broadcast(task_id, {
            "type": "result",
            "taskId": task_id,
            "data": {
                "status": task["status"],
                "metrics": task.get("metrics", {}),
            },
            "timestamp": _now(),
        })

    except Exception as e:
        import traceback
        traceback.print_exc()
        task["status"] = "failed"
        task["progress"]["message"] = str(e)
        task["updatedAt"] = _now()
        await _broadcast(task_id, {
            "type": "error",
            "taskId": task_id,
            "data": {"error": str(e)},
            "timestamp": _now(),
        })


def _load_backtest_results(task: Dict[str, Any]) -> Optional[str]:
    """Load this task's result file, returning an error message on failure."""
    try:
        config_path = task.get("config", {}).get("configPath") or str(
            PROJECT_ROOT / "configs" / "backtest.yaml"
        )
        with open(config_path, "r") as f:
            bt_config = yaml.safe_load(f)
        output_dir_raw = bt_config.get("experiment", {}).get(
            "output_dir", "data/results/backtest_v2_results"
        )
        # Resolve relative output_dir against PROJECT_ROOT (run_backtest runs with cwd=PROJECT_ROOT)
        output_dir = Path(output_dir_raw)
        if not output_dir.is_absolute():
            output_dir = PROJECT_ROOT / output_dir
        output_name = task.get("config", {}).get("outputName")
        if not output_name:
            return "回测失败：任务缺少结果文件标识"
        metrics_file = output_dir / f"{output_name}_backtest_metrics.json"
        if not metrics_file.exists():
            return f"回测失败：未生成任务结果文件 {metrics_file.name}"

        with open(metrics_file, "r", encoding="utf-8") as f:
            metrics_data = json.load(f)
            # The JSON has a nested structure: { metrics: {...}, config: {...}, ... }
            # Flatten: put the inner metrics dict at the top level for the frontend,
            # but also keep meta fields like experiment_name and elapsed_seconds.
        inner_metrics = metrics_data.get("metrics", {})
        required_metrics = {"annualized_return", "max_drawdown", "information_ratio"}
        missing_metrics = sorted(required_metrics - set(inner_metrics))
        if missing_metrics:
            return "回测失败：结果缺少策略指标 " + ", ".join(missing_metrics)
        flat = {**inner_metrics}
            # Carry over useful metadata
        for key in ("experiment_name", "factor_source", "num_factors",
                    "config", "elapsed_seconds"):
            if key in metrics_data:
                flat[f"__{key}"] = metrics_data[key]
            
            # Load cumulative excess return data from CSV
        csv_path = metrics_file.with_name(metrics_file.name.replace("_backtest_metrics.json", "_cumulative_excess.csv"))
        if csv_path.exists():
            import pandas as pd
            df = pd.read_csv(csv_path)
            if 'date' in df.columns and 'cumulative_excess_return' in df.columns:
                cumulative_data = df[['date', 'cumulative_excess_return']].to_dict('records')
                flat["cumulative_curve"] = [
                    {"date": r["date"], "value": r["cumulative_excess_return"]}
                    for r in cumulative_data
                ]

        task["metrics"] = flat
        return None
    except Exception as e:
        return f"回测失败：无法读取任务结果 ({e})"


# ---- Internal daily paper trading endpoints ----

PAPER_TRADING_DB = PROJECT_ROOT / "data" / "trading" / "paper" / "paper_trading.sqlite3"
PAPER_STRATEGY_SNAPSHOT = PROJECT_ROOT / "data" / "trading" / "paper" / "strategy_snapshot.pkl"


def _paper_engine():
    from quantaalpha.trading.paper import PaperTradingEngine

    engine = PaperTradingEngine(PAPER_TRADING_DB)
    engine.ensure_account()
    return engine


def _paper_source():
    from quantaalpha.trading.market_data import QlibBinaryDailySource

    return QlibBinaryDailySource()


def _paper_strategy(source=None):
    from quantaalpha.trading.signal import PaperStrategySignalGenerator

    return PaperStrategySignalGenerator(PAPER_STRATEGY_SNAPSHOT, source=source)


def _paper_state_with_market(engine, source) -> Dict[str, Any]:
    state = engine.get_state(market_calendar=source.calendar)
    dates = source.latest_dates(2)
    state["marketData"] = {
        "providerPath": str(source.provider_path),
        "previousDate": dates[-2] if len(dates) > 1 else None,
        "latestDate": dates[-1],
    }
    try:
        state["strategy"] = _paper_strategy(source).describe()
    except Exception as exc:
        state["strategy"] = {"available": False, "error": str(exc)}
    return state


@app.get("/api/v1/paper-trading/account", response_model=ApiResponse)
async def get_paper_trading_account():
    try:
        engine = _paper_engine()
        source = _paper_source()
        return ApiResponse(success=True, data=_paper_state_with_market(engine, source))
    except Exception as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.get("/api/v1/paper-trading/realtime", response_model=ApiResponse)
async def get_paper_trading_realtime():
    try:
        from market_data.realtime_service import build_paper_realtime_state

        engine = _paper_engine()
        source = _paper_source()
        paper_state = engine.get_state()
        latest_date = source.latest_dates(1)[-1]
        live_state = await asyncio.to_thread(
            build_paper_realtime_state,
            paper_state,
            latest_date,
        )
        return ApiResponse(success=True, data=live_state)
    except Exception as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.post("/api/v1/paper-trading/signals", response_model=ApiResponse)
async def submit_paper_trading_signal(req: PaperSignalRequest):
    try:
        from quantaalpha.trading.paper import normalize_symbol

        engine = _paper_engine()
        source = _paper_source()
        latest_date = source.latest_dates(1)[-1]
        if req.signalDate > latest_date:
            raise ValueError(f"信号日期晚于本地行情日期 {latest_date}")
        targets: Dict[str, float] = {}
        for target in req.targets:
            symbol = normalize_symbol(target.symbol)
            if symbol in targets:
                raise ValueError(f"目标组合包含重复股票 {symbol}")
            targets[symbol] = target.targetWeight
        signal = engine.submit_targets(req.signalDate, targets, source={"type": "manual"})
        return ApiResponse(success=True, data={"signal": signal}, message="目标组合已冻结")
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.post("/api/v1/paper-trading/signals/generate", response_model=ApiResponse)
async def generate_paper_trading_signal(req: PaperStrategySignalRequest):
    try:
        engine = _paper_engine()
        if engine.next_pending_signal() is not None:
            raise ValueError("已有待执行目标组合，请先完成撮合")
        source = _paper_source()
        latest_date = source.latest_dates(1)[-1]
        signal_date = req.signalDate or latest_date
        if signal_date > latest_date:
            raise ValueError(f"信号日期晚于本地行情日期 {latest_date}")
        generator = _paper_strategy(source)
        current_positions = [
            position
            for position in engine.get_state()["positions"]
            if position["quantity"] > 0
        ]
        current_symbols = [position["symbol"] for position in current_positions]
        current_holding_sessions = {
            position["symbol"]: position["holdingSessions"]
            for position in current_positions
        }
        generated = await asyncio.to_thread(
            generator.generate_targets,
            signal_date,
            req.topK,
            req.exposure,
            current_symbols,
            current_holding_sessions,
        )
        from quantaalpha.trading.signal import build_model_signal_source

        signal = engine.submit_targets(
            generated["signalDate"],
            generated["targets"],
            source=build_model_signal_source(generated),
        )
        state = _paper_state_with_market(engine, source)
        state["generatedSignal"] = {"signal": signal, "ranking": generated["ranking"]}
        return ApiResponse(success=True, data=state, message="策略信号已生成并冻结")
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.post("/api/v1/paper-trading/run", response_model=ApiResponse)
async def run_paper_trading(req: PaperRunRequest):
    try:
        from quantaalpha.trading.paper import PAPER_BENCHMARK_SYMBOL

        engine = _paper_engine()
        source = _paper_source()
        pending = engine.next_pending_signal()
        if pending is None:
            raise ValueError("没有待执行的目标组合")
        account_state = engine.get_state()["account"]
        after_date = max(filter(None, [pending["signalDate"], account_state.get("lastTradeDate")]))
        trade_date = req.tradeDate or source.next_date_after(after_date)
        if trade_date is None:
            raise ValueError(f"{pending['signalDate']} 之后还没有新的交易日行情")
        bars = source.get_bars(pending["symbols"], trade_date)
        benchmark_bar = source.get_bars([PAPER_BENCHMARK_SYMBOL], trade_date).get(
            PAPER_BENCHMARK_SYMBOL
        )
        if benchmark_bar is None:
            raise ValueError(f"{trade_date} 缺少沪深300基准行情")
        executed = engine.execute_pending(
            bars,
            trade_date,
            benchmark_bar=benchmark_bar,
        )
        state = _paper_state_with_market(engine, source)
        state["latestRun"] = executed["latestRun"]
        return ApiResponse(success=True, data=state, message=f"已按 {trade_date} 行情完成模拟撮合")
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.post("/api/v1/paper-trading/account/reset", response_model=ApiResponse)
async def reset_paper_trading_account(req: PaperResetRequest):
    try:
        engine = _paper_engine()
        source = _paper_source()
        engine.reset_account(initial_cash=req.initialCash)
        return ApiResponse(
            success=True,
            data=_paper_state_with_market(engine, source),
            message="模拟账户已重置",
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


# ---- Isolated paired research-shadow endpoints ----

from quantaalpha.trading.research_shadow import (
    DEFAULT_DECLARATION as RESEARCH_SHADOW_DECLARATION,
)


def _research_shadow_coordinator():
    from quantaalpha.trading.research_shadow import ResearchShadowCoordinator

    return ResearchShadowCoordinator(RESEARCH_SHADOW_DECLARATION)


def _research_shadow_auto_config() -> dict[str, Any]:
    values = _load_dotenv_dict()
    enabled_value = os.getenv(
        "RESEARCH_SHADOW_AUTO_ENABLED",
        values.get("RESEARCH_SHADOW_AUTO_ENABLED", "false"),
    )
    poll_value = os.getenv(
        "RESEARCH_SHADOW_AUTO_POLL_SECONDS",
        values.get("RESEARCH_SHADOW_AUTO_POLL_SECONDS", "900"),
    )
    max_cycle_value = os.getenv(
        "RESEARCH_SHADOW_AUTO_MAX_CYCLE_SECONDS",
        values.get("RESEARCH_SHADOW_AUTO_MAX_CYCLE_SECONDS", "7200"),
    )
    try:
        poll_seconds = int(poll_value)
    except (TypeError, ValueError):
        poll_seconds = 900
    try:
        max_cycle_seconds = int(max_cycle_value)
    except (TypeError, ValueError):
        max_cycle_seconds = 7200
    return {
        "enabled": str(enabled_value).lower() in {"1", "true", "yes", "on"},
        "pollSeconds": min(86_400, max(300, poll_seconds)),
        "maxCycleSeconds": min(21_600, max(900, max_cycle_seconds)),
    }


def _official_provider_freshness_status(
    payload: Mapping[str, Any] | None,
) -> str | None:
    if not isinstance(payload, Mapping):
        return None
    result = payload.get("result")
    if not isinstance(result, Mapping):
        return None
    update = result.get("providerUpdate")
    if not isinstance(update, Mapping):
        return None
    freshness = update.get("officialDataFreshness")
    if not isinstance(freshness, Mapping):
        return None
    value = str(freshness.get("status") or "").strip()
    return value or None


def _research_shadow_alert_events(
    previous: Mapping[str, Any] | None,
    current: Mapping[str, Any] | None = None,
    *,
    failed: bool = False,
) -> List[str]:
    previous_status = str((previous or {}).get("status") or "")
    if failed:
        return (
            []
            if previous_status == "ERROR"
            else ["OFFICIAL_PROVIDER_UPDATE_ERROR"]
        )
    events: List[str] = []
    current_status = str((current or {}).get("status") or "")
    if previous_status == "ERROR" and current_status != "ERROR":
        events.append("OFFICIAL_PROVIDER_UPDATE_RECOVERED")
    if current_status == "REVIEW_REQUIRED" and previous_status != "REVIEW_REQUIRED":
        events.append("RESEARCH_SHADOW_REVIEW_REQUIRED")
    previous_freshness = _official_provider_freshness_status(previous)
    current_freshness = _official_provider_freshness_status(current)
    if current_freshness == "OVERDUE" and previous_freshness != "OVERDUE":
        events.append("OFFICIAL_PROVIDER_OVERDUE")
    elif (
        previous_freshness == "OVERDUE"
        and current_freshness is not None
        and current_freshness != "OVERDUE"
    ):
        events.append("OFFICIAL_PROVIDER_FRESHNESS_RECOVERED")
    return events


async def _research_shadow_watch_loop() -> None:
    from quantaalpha.trading.research_shadow_automation import read_automation_status
    from quantaalpha.trading.unattended_provider_automation import (
        run_cycle_and_record,
    )

    previous_cycle: Mapping[str, Any] | None = None
    while True:
        config = _research_shadow_auto_config()
        if not config["enabled"]:
            _mark_background_worker_disabled(RESEARCH_SHADOW_WORKER)
            previous_cycle = {"status": "DISABLED"}
            await asyncio.sleep(60)
            continue
        _mark_background_worker_running(
            RESEARCH_SHADOW_WORKER,
            config["maxCycleSeconds"],
        )
        cycle_error = None
        alert_events: List[str] = []
        try:
            if previous_cycle is None:
                previous_cycle = await asyncio.to_thread(
                    read_automation_status,
                    RESEARCH_SHADOW_DECLARATION,
                )
            cycle_result = await asyncio.to_thread(
                run_cycle_and_record,
                RESEARCH_SHADOW_DECLARATION,
                poll_seconds=config["pollSeconds"],
            )
            alert_events = _research_shadow_alert_events(
                previous_cycle,
                cycle_result,
            )
            previous_cycle = cycle_result
        except Exception as exc:
            cycle_error = _sanitize_log_line(str(exc))[:1000]
            alert_events = _research_shadow_alert_events(
                previous_cycle,
                failed=True,
            )
            previous_cycle = {"status": "ERROR"}
            print(
                "[research-shadow-auto] " + cycle_error,
                file=sys.stderr,
                flush=True,
            )
        for event_name in alert_events:
            try:
                await asyncio.to_thread(_emit_fixed_runtime_alert, event_name)
            except Exception:
                print(
                    f"[research-shadow-auto] alert delivery failed: {event_name}",
                    file=sys.stderr,
                    flush=True,
                )
        _mark_background_worker_idle(
            RESEARCH_SHADOW_WORKER,
            config["pollSeconds"],
            error=cycle_error,
        )
        await asyncio.sleep(config["pollSeconds"])


def _intraday_capture_config() -> dict[str, Any]:
    values = _load_dotenv_dict()
    enabled_value = os.getenv(
        "QUANTAALPHA_INTRADAY_CAPTURE_ENABLED",
        values.get("QUANTAALPHA_INTRADAY_CAPTURE_ENABLED", "false"),
    )
    poll_value = os.getenv(
        "QUANTAALPHA_INTRADAY_CAPTURE_SECONDS",
        values.get("QUANTAALPHA_INTRADAY_CAPTURE_SECONDS", "60"),
    )
    max_cycle_value = os.getenv(
        "QUANTAALPHA_INTRADAY_CAPTURE_MAX_CYCLE_SECONDS",
        values.get("QUANTAALPHA_INTRADAY_CAPTURE_MAX_CYCLE_SECONDS", "300"),
    )
    # Runtime Provider switches update the project .env without mutating this process.
    provider_value = values.get("QLIB_DATA_DIR") or os.getenv("QLIB_DATA_DIR", "")
    output_value = os.getenv(
        "QUANTAALPHA_INTRADAY_CAPTURE_DIR",
        values.get(
            "QUANTAALPHA_INTRADAY_CAPTURE_DIR",
            str(PROJECT_ROOT / "data/market/a_share/intraday/a_stock_data_csi300"),
        ),
    )
    calibration_enabled_value = os.getenv(
        "QUANTAALPHA_INTRADAY_CALIBRATION_ENABLED",
        values.get("QUANTAALPHA_INTRADAY_CALIBRATION_ENABLED", "false"),
    )
    calibration_declaration_value = os.getenv(
        "QUANTAALPHA_INTRADAY_CALIBRATION_DECLARATION",
        values.get(
            "QUANTAALPHA_INTRADAY_CALIBRATION_DECLARATION",
            str(
                PROJECT_ROOT
                / "configs/intraday_capture_calibration_v2_20260813.json"
            ),
        ),
    )
    try:
        poll_seconds = int(poll_value)
    except (TypeError, ValueError):
        poll_seconds = 60
    try:
        max_cycle_seconds = int(max_cycle_value)
    except (TypeError, ValueError):
        max_cycle_seconds = 300
    output_root = Path(output_value).expanduser()
    if not output_root.is_absolute():
        output_root = PROJECT_ROOT / output_root
    calibration_declaration = Path(calibration_declaration_value).expanduser()
    if not calibration_declaration.is_absolute():
        calibration_declaration = PROJECT_ROOT / calibration_declaration
    return {
        "enabled": str(enabled_value).lower() in {"1", "true", "yes", "on"},
        "pollSeconds": min(900, max(30, poll_seconds)),
        "maxCycleSeconds": min(1800, max(120, max_cycle_seconds)),
        "providerPath": str(Path(provider_value).expanduser()) if provider_value else None,
        "outputRoot": str(output_root.resolve()),
        "calibrationEnabled": str(calibration_enabled_value).lower()
        in {"1", "true", "yes", "on"},
        "calibrationDeclaration": str(calibration_declaration.resolve()),
    }


def _intraday_calibration_refresh_required(
    *,
    enabled: bool,
    calibration: Mapping[str, Any],
    configured_trade_date: str,
) -> bool:
    if not enabled:
        return False
    calibration_trade_date = str(calibration.get("tradeDate") or "")
    return (
        calibration_trade_date != configured_trade_date
        or not bool(calibration.get("finalized"))
    )


def _intraday_capture_effective_provider_path(
    config: Mapping[str, Any],
    *,
    now: datetime | None = None,
) -> str | None:
    configured = config.get("providerPath")
    if not configured:
        return None
    from market_data.intraday_capture import list_verified_slot_records

    shanghai = ZoneInfo("Asia/Shanghai")
    local_now = (now or datetime.now(shanghai)).astimezone(shanghai)
    records = list_verified_slot_records(
        Path(str(config["outputRoot"])),
        local_now.date().isoformat(),
    )
    if not records:
        return str(configured)
    binding = records[0][1].get("binding") or {}
    bound_provider = str(binding.get("providerPath") or "").strip()
    if not bound_provider:
        raise RuntimeError("intraday capture slot binding lacks providerPath")
    return bound_provider


def _intraday_capture_alert_events(
    previous: Mapping[str, Any] | None,
    current: Mapping[str, Any] | None = None,
    *,
    failed: bool = False,
) -> List[str]:
    previous_status = str((previous or {}).get("status") or "")
    current_status = str((current or {}).get("status") or "")
    current_failed = failed or current_status == "ERROR"
    if current_failed:
        return [] if previous_status == "ERROR" else ["INTRADAY_CAPTURE_ERROR"]
    if previous_status == "ERROR":
        return ["INTRADAY_CAPTURE_RECOVERED"]
    return []


def _intraday_temporal_close_alert(
    temporal_health: Mapping[str, Any] | None,
) -> tuple[str, str] | None:
    health = temporal_health or {}
    if health.get("status") != "DEGRADED":
        return None
    try:
        expected_slots = int(health.get("expectedSlotCount") or 0)
        due_slots = int(health.get("dueSlotCount") or 0)
    except (TypeError, ValueError):
        return None
    if expected_slots <= 0 or due_slots != expected_slots:
        return None
    trade_date = str(health.get("tradeDate") or "")
    try:
        parsed_trade_date = datetime.strptime(trade_date, "%Y-%m-%d").date()
    except ValueError:
        return None
    if parsed_trade_date.isoformat() != trade_date:
        return None
    return "INTRADAY_TEMPORAL_DEGRADED", trade_date


def _intraday_capture_runtime_binding():
    from market_data.intraday_capture_v3 import run_capture_cycle
    from market_data.providers.a_stock_data_intraday_v3 import (
        INDEPENDENT_TRANSPORT_FALLBACK_CONTRACT,
        REALTIME_CONTRACT_SHA256,
        IndependentTransportRealtimeClient,
    )

    bound_provider = run_capture_cycle.__globals__.get(
        "IndependentTransportRealtimeClient"
    )
    expected_version = str(
        INDEPENDENT_TRANSPORT_FALLBACK_CONTRACT["easyTdxPackageVersion"]
    )
    installed_version = importlib_metadata.version("easy-tdx")
    contract = {
        "version": 1,
        "entrypointModule": run_capture_cycle.__module__,
        "providerModule": IndependentTransportRealtimeClient.__module__,
        "providerClass": IndependentTransportRealtimeClient.__name__,
        "easyTdxPackageVersion": installed_version,
        "realtimeContractSha256": REALTIME_CONTRACT_SHA256,
        "sourceOrder": list(
            INDEPENDENT_TRANSPORT_FALLBACK_CONTRACT["sourceOrder"]
        ),
        "prohibitCrossSourceFieldMerge": True,
    }
    if (
        contract["entrypointModule"] != "market_data.intraday_capture_v3"
        or contract["providerModule"]
        != "market_data.providers.a_stock_data_intraday_v3"
        or contract["providerClass"] != "IndependentTransportRealtimeClient"
        or bound_provider is not IndependentTransportRealtimeClient
        or installed_version != expected_version
    ):
        raise RuntimeError("intraday capture V3 runtime binding drifted")
    return run_capture_cycle, contract


async def _intraday_capture_loop() -> None:
    from market_data.intraday_calibration import (
        finalize_calibration_report,
        load_calibration_declaration,
    )
    from market_data.intraday_capture import (
        read_capture_status,
        update_capture_calibration_status,
    )
    from market_data.intraday_capture_health import summarize_latest_capture_day

    run_capture_cycle, _ = _intraday_capture_runtime_binding()
    previous_cycle: Mapping[str, Any] | None = None
    close_health_evaluated = False
    while True:
        config = _intraday_capture_config()
        started_at = asyncio.get_running_loop().time()
        if not config["enabled"]:
            _mark_background_worker_disabled(INTRADAY_CAPTURE_WORKER)
            previous_cycle = {"status": "DISABLED"}
            await asyncio.sleep(60)
            continue
        _mark_background_worker_running(
            INTRADAY_CAPTURE_WORKER,
            config["maxCycleSeconds"],
        )
        capture_error = None
        alert_events: List[str | tuple[str, str]] = []
        if not config["providerPath"]:
            capture_error = "QLIB_DATA_DIR is not configured"
            alert_events = _intraday_capture_alert_events(
                previous_cycle,
                failed=True,
            )
            previous_cycle = {"status": "ERROR"}
            print(
                "[intraday-capture] " + capture_error,
                file=sys.stderr,
                flush=True,
            )
        else:
            try:
                if previous_cycle is None:
                    previous_cycle = await asyncio.to_thread(
                        read_capture_status,
                        config["outputRoot"],
                    )
                effective_provider_path = await asyncio.to_thread(
                    _intraday_capture_effective_provider_path,
                    config,
                )
                status = await asyncio.to_thread(
                    run_capture_cycle,
                    effective_provider_path,
                    config["outputRoot"],
                    poll_seconds=config["pollSeconds"],
                )
                calibration = status.get("calibration") or {}
                configured_calibration_date = ""
                if config["calibrationEnabled"]:
                    configured_declaration, _ = load_calibration_declaration(
                        config["calibrationDeclaration"]
                    )
                    configured_calibration_date = str(
                        configured_declaration["calibration"]["tradeDate"]
                    )
                should_calibrate = _intraday_calibration_refresh_required(
                    enabled=config["calibrationEnabled"],
                    calibration=calibration,
                    configured_trade_date=configured_calibration_date,
                )
                calibration_error = None
                if should_calibrate:
                    try:
                        calibration = await asyncio.to_thread(
                            finalize_calibration_report,
                            config["calibrationDeclaration"],
                            config["outputRoot"],
                        )
                    except Exception as exc:
                        calibration_error = str(exc)[:1000]
                        calibration = {
                            "status": "ERROR",
                            "finalized": False,
                            "decision": "CALIBRATION_OPERATIONAL_ERROR",
                            "tradeDate": configured_calibration_date or None,
                            "file": None,
                            "reportSha256": None,
                            "slotCoverageRatio": 0.0,
                            "medianUsableCoverageRatio": 0.0,
                            "capturedExpectedSlotCount": 0,
                            "expectedSlotCount": 242,
                            "profitEvidenceEligible": False,
                            "error": calibration_error,
                        }
                    status = await asyncio.to_thread(
                        update_capture_calibration_status,
                        config["outputRoot"],
                        calibration,
                    )
                capture_error = (
                    calibration_error
                    or str(status.get("lastError") or "unknown error")
                    if status.get("status") == "ERROR"
                    else calibration_error
                )
                alert_events = _intraday_capture_alert_events(
                    previous_cycle,
                    status,
                )
                current_status = str(status.get("status") or "")
                if current_status != "WAITING_MARKET":
                    close_health_evaluated = False
                elif not close_health_evaluated:
                    close_health_evaluated = True
                    try:
                        temporal_health = await asyncio.to_thread(
                            summarize_latest_capture_day,
                            config["outputRoot"],
                            poll_seconds=config["pollSeconds"],
                        )
                        temporal_alert = _intraday_temporal_close_alert(
                            temporal_health
                        )
                        if temporal_alert is not None:
                            alert_events.append(temporal_alert)
                    except Exception:
                        print(
                            "[intraday-capture] temporal close audit failed",
                            file=sys.stderr,
                            flush=True,
                        )
                previous_cycle = status
            except Exception as exc:
                capture_error = str(exc)
                alert_events = _intraday_capture_alert_events(
                    previous_cycle,
                    failed=True,
                )
                previous_cycle = {"status": "ERROR"}
            if capture_error:
                print(
                    "[intraday-capture] "
                    + _sanitize_log_line(capture_error)[:1000],
                    file=sys.stderr,
                    flush=True,
                )
        for alert_event in alert_events:
            if isinstance(alert_event, tuple):
                event_name, subject_date = alert_event
            else:
                event_name, subject_date = alert_event, None
            try:
                await asyncio.to_thread(
                    _emit_fixed_runtime_alert,
                    event_name,
                    subject_date=subject_date,
                )
            except Exception:
                print(
                    f"[intraday-capture] alert delivery failed: {event_name}",
                    file=sys.stderr,
                    flush=True,
                )
        elapsed = asyncio.get_running_loop().time() - started_at
        sleep_seconds = max(1.0, config["pollSeconds"] - elapsed)
        _mark_background_worker_idle(
            INTRADAY_CAPTURE_WORKER,
            sleep_seconds,
            error=capture_error,
        )
        await asyncio.sleep(sleep_seconds)


def _intraday_shadow_config() -> dict[str, Any]:
    values = _load_dotenv_dict()
    enabled_value = os.getenv(
        "QUANTAALPHA_INTRADAY_SHADOW_ENABLED",
        values.get("QUANTAALPHA_INTRADAY_SHADOW_ENABLED", "false"),
    )
    poll_value = os.getenv(
        "QUANTAALPHA_INTRADAY_SHADOW_POLL_SECONDS",
        values.get("QUANTAALPHA_INTRADAY_SHADOW_POLL_SECONDS", "60"),
    )
    max_cycle_value = os.getenv(
        "QUANTAALPHA_INTRADAY_SHADOW_MAX_CYCLE_SECONDS",
        values.get("QUANTAALPHA_INTRADAY_SHADOW_MAX_CYCLE_SECONDS", "600"),
    )
    declaration_value = values.get(
        "QUANTAALPHA_INTRADAY_SHADOW_DECLARATION"
    ) or os.getenv(
        "QUANTAALPHA_INTRADAY_SHADOW_DECLARATION",
        str(
            PROJECT_ROOT
            / "configs/intraday_shadow_20260817_t1_reversal_v6.json"
        ),
    )
    try:
        poll_seconds = int(poll_value)
    except (TypeError, ValueError):
        poll_seconds = 60
    try:
        max_cycle_seconds = int(max_cycle_value)
    except (TypeError, ValueError):
        max_cycle_seconds = 600
    declaration = Path(declaration_value).expanduser()
    if not declaration.is_absolute():
        declaration = PROJECT_ROOT / declaration
    return {
        "enabled": str(enabled_value).lower() in {"1", "true", "yes", "on"},
        "pollSeconds": min(900, max(30, poll_seconds)),
        "maxCycleSeconds": min(3600, max(120, max_cycle_seconds)),
        "declaration": str(declaration.resolve()),
    }


def _intraday_preflight_summary(
    *,
    report_path: Path = INTRADAY_PREFLIGHT_REPORT_PATH,
    now: datetime | None = None,
    configured: bool | None = None,
    max_age_seconds: int = INTRADAY_PREFLIGHT_MAX_AGE_SECONDS,
) -> dict[str, Any]:
    if configured is None:
        values = _load_dotenv_dict()
        configured_value = os.getenv(
            "QA_BACKEND_INTRADAY_PREFLIGHT_ENABLED",
            values.get("QA_BACKEND_INTRADAY_PREFLIGHT_ENABLED", "false"),
        )
        configured = str(configured_value).lower() in {"1", "true", "yes", "on"}
    base = {
        "configured": bool(configured),
        "ready": False,
        "status": "DISABLED" if not configured else "STARTING",
        "checkedAt": None,
        "ageSeconds": None,
        "maxAgeSeconds": int(max_age_seconds),
        "passedChecks": 0,
        "totalChecks": 0,
        "failedChecks": [],
    }
    if not configured or not report_path.is_file():
        return base

    try:
        payload = json.loads(report_path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or payload.get("version") != 1:
            raise ValueError("invalid report envelope")
        report_status = str(payload.get("status") or "")
        report_ready = payload.get("ready")
        checked_at_text = str(payload.get("checkedAt") or "")
        checked_at = datetime.fromisoformat(checked_at_text)
        if checked_at.tzinfo is None:
            raise ValueError("readiness timestamp lacks timezone")
        current = (now or datetime.now(ZoneInfo("Asia/Shanghai"))).astimezone(
            ZoneInfo("Asia/Shanghai")
        )
        checked_at = checked_at.astimezone(ZoneInfo("Asia/Shanghai"))
        raw_age = (current - checked_at).total_seconds()
        if raw_age < -60:
            raise ValueError("readiness timestamp is in the future")
        checks = payload.get("checks") or []
        if not isinstance(checks, list) or any(
            not isinstance(item, dict) or not isinstance(item.get("passed"), bool)
            for item in checks
        ):
            raise ValueError("invalid readiness checks")
        passed = sum(item["passed"] for item in checks)
        failed = [
            str(item.get("name") or "unknown")[:80]
            for item in checks
            if not item["passed"]
        ]
        if not checks and payload.get("fatalError"):
            failed = ["fatal"]
        contract_valid = (
            report_status == "READY"
            and report_ready is True
            and bool(checks)
            and passed == len(checks)
        ) or (
            report_status == "NOT_READY"
            and report_ready is False
            and bool(failed)
        )
        if not contract_valid:
            raise ValueError("inconsistent readiness verdict")
    except (OSError, TypeError, ValueError):
        return {**base, "status": "INVALID"}

    age_seconds = max(0, int(raw_age))
    stale = age_seconds > max_age_seconds
    return {
        **base,
        "ready": bool(report_ready) and not stale,
        "status": "STALE" if stale else report_status,
        "checkedAt": checked_at.isoformat(),
        "ageSeconds": age_seconds,
        "passedChecks": passed,
        "totalChecks": len(checks),
        "failedChecks": failed,
    }


def _intraday_shadow_coordinator():
    config = _intraday_shadow_config()
    declaration_path = Path(config["declaration"])
    try:
        declaration = json.loads(declaration_path.read_text(encoding="utf-8"))
        protocol_id = str(declaration.get("protocolId") or "")
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError("intraday-shadow declaration cannot be selected") from exc
    if protocol_id == "intraday-shadow-t1-reversal-proxy-v9-provider-maintenance":
        from market_data.intraday_shadow_v9 import IntradayShadowCoordinator
    elif protocol_id == "intraday-shadow-t1-reversal-proxy-v8-provider-maintenance":
        from market_data.intraday_shadow_v8 import IntradayShadowCoordinator
    elif protocol_id == "intraday-shadow-t1-reversal-proxy-v7-rebased-provider":
        from market_data.intraday_shadow_v7 import IntradayShadowCoordinator
    elif protocol_id == "intraday-shadow-t1-reversal-proxy-v6-independent-transport":
        from market_data.intraday_shadow_v6 import IntradayShadowCoordinator
    elif protocol_id == "intraday-shadow-t1-reversal-proxy-v5-causal-intent":
        from market_data.intraday_shadow_v5 import IntradayShadowCoordinator
    else:
        raise RuntimeError(
            f"unsupported intraday-shadow protocol for runtime: {protocol_id}"
        )
    return IntradayShadowCoordinator(declaration_path)


def _intraday_shadow_alert_events(
    previous_status: str | None,
    *,
    cycle_status: str | None = None,
    actions: List[str] | None = None,
    failed: bool = False,
) -> List[str]:
    if failed:
        return [] if previous_status == "ERROR" else ["INTRADAY_SHADOW_ERROR"]
    events: List[str] = []
    if previous_status == "ERROR":
        events.append("INTRADAY_SHADOW_RECOVERED")
    action_values = [str(value) for value in actions or []]
    if any(value.startswith("DECISION_FROZEN:") for value in action_values):
        events.append("INTRADAY_DECISION_INTENT_FROZEN")
    if any(value.startswith("EXECUTION_BOUND:") for value in action_values):
        events.append("INTRADAY_EXECUTION_PRICE_BOUND")
    if any(value.startswith("FROZEN:") for value in action_values):
        events.append("INTRADAY_SIGNAL_FROZEN")
    if any(value.startswith("FINALIZED:") for value in action_values):
        events.append("INTRADAY_SESSION_FINALIZED")
    if cycle_status == "MISSED_SESSION" and previous_status != "MISSED_SESSION":
        events.append("INTRADAY_SESSION_MISSED")
    if cycle_status == "RISK_STOPPED" and previous_status != "RISK_STOPPED":
        events.append("INTRADAY_RISK_STOPPED")
    return events


def _emit_fixed_runtime_alert(
    event_name: str,
    *,
    subject_date: str | None = None,
) -> None:
    alert_script = PROJECT_ROOT / "scripts/research_shadow_supervisor_alert.py"
    command = [
        sys.executable,
        str(alert_script),
        event_name,
        "--supervisor-pid",
        str(os.getppid()),
        "--child-pid",
        str(os.getpid()),
    ]
    if subject_date is not None:
        command.extend(["--subject-date", subject_date])
    completed = subprocess.run(
        command,
        cwd=PROJECT_ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"fixed runtime alert failed: {event_name}")


async def _intraday_shadow_loop() -> None:
    previous_business_status: str | None = None
    while True:
        config = _intraday_shadow_config()
        if not config["enabled"]:
            _mark_background_worker_disabled(INTRADAY_SHADOW_WORKER)
            previous_business_status = "DISABLED"
            await asyncio.sleep(60)
            continue
        _mark_background_worker_running(
            INTRADAY_SHADOW_WORKER,
            config["maxCycleSeconds"],
        )
        cycle_error = None
        alert_events: List[str] = []
        try:
            coordinator = await asyncio.to_thread(_intraday_shadow_coordinator)
            cycle_result = await asyncio.to_thread(
                coordinator.run_cycle,
                poll_seconds=config["pollSeconds"],
            )
            cycle_status = str(cycle_result.get("status") or "UNKNOWN")
            alert_events = _intraday_shadow_alert_events(
                previous_business_status,
                cycle_status=cycle_status,
                actions=cycle_result.get("actions") or [],
            )
            previous_business_status = cycle_status
        except Exception as exc:
            cycle_error = _sanitize_log_line(str(exc))[:1000]
            alert_events = _intraday_shadow_alert_events(
                previous_business_status,
                failed=True,
            )
            previous_business_status = "ERROR"
            print(
                "[intraday-shadow] " + cycle_error,
                file=sys.stderr,
                flush=True,
            )
        for event_name in alert_events:
            try:
                await asyncio.to_thread(_emit_fixed_runtime_alert, event_name)
            except Exception:
                print(
                    f"[intraday-shadow] alert delivery failed: {event_name}",
                    file=sys.stderr,
                    flush=True,
                )
        _mark_background_worker_idle(
            INTRADAY_SHADOW_WORKER,
            config["pollSeconds"],
            error=cycle_error,
        )
        await asyncio.sleep(config["pollSeconds"])


@app.get("/api/v1/research-shadow/state", response_model=ApiResponse)
async def get_research_shadow_state():
    try:
        coordinator = _research_shadow_coordinator()
        state = await asyncio.to_thread(coordinator.get_state)
        return ApiResponse(success=True, data=state)
    except Exception as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.get("/api/v1/research-shadow/automation", response_model=ApiResponse)
async def get_research_shadow_automation():
    try:
        from quantaalpha.trading.research_shadow_automation import (
            read_automation_status,
        )

        config = _research_shadow_auto_config()
        status = await asyncio.to_thread(
            read_automation_status, RESEARCH_SHADOW_DECLARATION
        )
        data = status or {
            "version": 1,
            "runId": json.loads(
                RESEARCH_SHADOW_DECLARATION.read_text(encoding="utf-8")
            )["runId"],
            "enabled": config["enabled"],
            "status": "STARTING" if config["enabled"] else "DISABLED",
            "lastCheckedAt": None,
            "lastSuccessAt": None,
            "nextCheckAt": None,
            "pollSeconds": config["pollSeconds"],
            "consecutiveErrors": 0,
            "lastError": None,
            "result": None,
            "evidenceCheckpoint": None,
        }
        data["enabled"] = config["enabled"]
        if not config["enabled"]:
            data["status"] = "DISABLED"
            data["nextCheckAt"] = None
        data["configuredEnabled"] = config["enabled"]
        data["configuredPollSeconds"] = config["pollSeconds"]
        data["configuredMaxCycleSeconds"] = config["maxCycleSeconds"]
        return ApiResponse(success=True, data=data)
    except Exception as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.get("/api/v1/market-data/intraday-capture/status", response_model=ApiResponse)
async def get_intraday_capture_status():
    try:
        from market_data.intraday_capture import read_capture_status
        from market_data.intraday_capture_health import summarize_latest_capture_day

        config = _intraday_capture_config()
        status = await asyncio.to_thread(
            read_capture_status,
            config["outputRoot"],
        )
        data = status or {
            "version": 1,
            "usageRealm": "INTRADAY_RESEARCH_RAW",
            "evidenceEligible": False,
            "trainingEligible": False,
            "status": "STARTING" if config["enabled"] else "DISABLED",
            "lastAttemptAt": None,
            "lastSuccessAt": None,
            "nextCaptureAt": None,
            "consecutiveErrors": 0,
            "lastError": None,
            "latest": None,
        }
        data["enabled"] = config["enabled"]
        data["configuredEnabled"] = config["enabled"]
        data["configuredPollSeconds"] = config["pollSeconds"]
        data["configuredMaxCycleSeconds"] = config["maxCycleSeconds"]
        data["configuredProviderPath"] = config["providerPath"]
        data["configuredOutputRoot"] = config["outputRoot"]
        data["configuredCalibrationEnabled"] = config["calibrationEnabled"]
        data["configuredCalibrationDeclaration"] = config[
            "calibrationDeclaration"
        ]
        _, runtime_contract = _intraday_capture_runtime_binding()
        data["runtimeContract"] = runtime_contract
        if not config["enabled"]:
            data["status"] = "DISABLED"
            data["nextCaptureAt"] = None
        try:
            data["temporalHealth"] = await asyncio.to_thread(
                summarize_latest_capture_day,
                config["outputRoot"],
                poll_seconds=config["pollSeconds"],
            )
        except Exception:
            data["temporalHealth"] = {
                "version": 1,
                "status": "INVALID",
                "integrityValid": False,
                "reason": "SLOT_CHAIN_INVALID",
            }
        return ApiResponse(success=True, data=data)
    except Exception as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.get("/api/v1/intraday-shadow/state", response_model=ApiResponse)
async def get_intraday_shadow_state():
    try:
        coordinator = await asyncio.to_thread(_intraday_shadow_coordinator)
        state = await asyncio.to_thread(coordinator.get_state)
        state["quality"] = dict(coordinator.declaration["quality"])
        state["qualityExtension"] = dict(
            coordinator.declaration.get("qualityExtension") or {}
        )
        state["supersedes"] = dict(coordinator.declaration.get("supersedes") or {})
        return ApiResponse(success=True, data=state)
    except Exception as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.get("/api/v1/intraday-shadow/automation", response_model=ApiResponse)
async def get_intraday_shadow_automation():
    try:
        config = _intraday_shadow_config()
        coordinator = await asyncio.to_thread(_intraday_shadow_coordinator)
        status = await asyncio.to_thread(coordinator.read_automation_status)
        data = status or {
            "version": 1,
            "runId": coordinator.run_id,
            "enabled": config["enabled"],
            "status": "STARTING" if config["enabled"] else "DISABLED",
            "lastCheckedAt": None,
            "lastSuccessAt": None,
            "nextCheckAt": None,
            "pollSeconds": config["pollSeconds"],
            "consecutiveErrors": 0,
            "lastError": None,
            "actions": [],
            "state": None,
        }
        data["enabled"] = config["enabled"]
        data["configuredEnabled"] = config["enabled"]
        data["configuredPollSeconds"] = config["pollSeconds"]
        data["configuredMaxCycleSeconds"] = config["maxCycleSeconds"]
        data["configuredDeclaration"] = config["declaration"]
        data["quality"] = dict(coordinator.declaration["quality"])
        data["qualityExtension"] = dict(
            coordinator.declaration.get("qualityExtension") or {}
        )
        data["causality"] = dict(
            coordinator.declaration.get("causality") or {}
        )
        data["supersedes"] = dict(coordinator.declaration.get("supersedes") or {})
        data["preflight"] = await asyncio.to_thread(_intraday_preflight_summary)
        if not config["enabled"]:
            data["status"] = "DISABLED"
            data["nextCheckAt"] = None
        return ApiResponse(success=True, data=data)
    except Exception as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.get("/api/v1/research-shadow/realtime", response_model=ApiResponse)
async def get_research_shadow_realtime():
    try:
        from market_data.providers.a_stock_data import AStockDataClient
        from market_data.realtime_service import (
            BENCHMARK_SYMBOL,
            build_paper_realtime_state,
        )

        coordinator = _research_shadow_coordinator()
        arms = {
            arm: coordinator.engine.get_state(arm) for arm in ("C1", "B0")
        }
        symbols = [BENCHMARK_SYMBOL]
        for state in arms.values():
            symbols.extend(position["symbol"] for position in state["positions"])
        try:
            quotes = await asyncio.to_thread(
                AStockDataClient(timeout=8.0).realtime_quotes,
                list(dict.fromkeys(symbols)),
            )
            quote_error = None
        except Exception as exc:
            quotes = {}
            quote_error = str(exc)
        latest_date = coordinator.source.latest_dates(1)[-1]
        live_arms = {
            arm: build_paper_realtime_state(
                state,
                latest_date,
                prefetched_quotes=quotes,
                prefetched_error=quote_error,
            )
            for arm, state in arms.items()
        }
        return ApiResponse(
            success=True,
            data={
                "runId": coordinator.declaration["runId"],
                "usageRealm": "RESEARCH_SHADOW",
                "formalPaperEligible": False,
                "arms": live_arms,
            },
        )
    except Exception as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.post("/api/v1/research-shadow/signals/generate", response_model=ApiResponse)
async def generate_research_shadow_signal():
    try:
        coordinator = _research_shadow_coordinator()
        state = await asyncio.to_thread(coordinator.generate_latest_signal)
        return ApiResponse(
            success=True,
            data=state,
            message="C1/B0 最新收盘信号已配对冻结",
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.post("/api/v1/research-shadow/run", response_model=ApiResponse)
async def run_research_shadow_pair():
    try:
        coordinator = _research_shadow_coordinator()
        state = await asyncio.to_thread(coordinator.execute_ready_pair)
        return ApiResponse(
            success=True,
            data=state,
            message="C1/B0 已按唯一允许的下一交易日开盘完成配对撮合",
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


# ---- System config endpoints ----

@app.get("/api/v1/system/config", response_model=ApiResponse)
async def get_system_config():
    """Read current system configuration from .env and experiment.yaml."""
    dotenv = _load_dotenv_dict()

    # Read experiment.yaml for display
    exp_yaml_path = PROJECT_ROOT / "configs" / "experiment.yaml"
    exp_yaml_content = ""
    if exp_yaml_path.exists():
        exp_yaml_content = exp_yaml_path.read_text(encoding="utf-8")

    # Mask credentials for security before returning config to the frontend.
    masked_env = {}
    for k, v in dotenv.items():
        upper_key = k.upper()
        is_secret = (
            "KEY" in upper_key
            or "SECRET" in upper_key
            or "PASSWORD" in upper_key
            or upper_key == "TOKEN"
            or upper_key.endswith("_TOKEN")
            or "REFRESH_TOKEN" in upper_key
        )
        if is_secret and v:
            masked_env[k] = v[:8] + "..." + v[-4:] if len(v) > 12 else "***"
        else:
            masked_env[k] = v

    return ApiResponse(
        success=True,
        data={
            "env": masked_env,
            "experimentYaml": exp_yaml_content,
            "factorLibraries": [Path(p).name for p in _find_factor_jsons()],
        },
    )


@app.put("/api/v1/system/config", response_model=ApiResponse)
async def update_system_config(update: SystemConfigUpdate):
    """Update .env configuration (non-secret fields only)."""
    if not DOTENV_PATH.exists():
        raise HTTPException(status_code=404, detail=".env file not found")

    content = DOTENV_PATH.read_text(encoding="utf-8")
    updates = {k: v for k, v in update.model_dump().items() if v is not None}
    if "QLIB_DATA_DIR" in updates and "QLIB_PROVIDER_URI" not in updates:
        updates["QLIB_PROVIDER_URI"] = updates["QLIB_DATA_DIR"]

    import re
    for key, val in updates.items():
        # Replace existing line or append
        pattern = rf"^{re.escape(key)}\s*=.*$"
        replacement = f"{key}={val}"
        new_content, n = re.subn(pattern, replacement, content, flags=re.MULTILINE)
        if n > 0:
            content = new_content
        else:
            content += f"\n{replacement}\n"

    DOTENV_PATH.write_text(content, encoding="utf-8")

    qlib_data = updates.get("QLIB_DATA_DIR")
    if qlib_data:
        qlib_symlink_dir = Path.home() / ".qlib" / "qlib_data"
        qlib_symlink_dir.mkdir(parents=True, exist_ok=True)
        cn_data_link = qlib_symlink_dir / "cn_data"
        if cn_data_link.is_symlink():
            cn_data_link.unlink()
            cn_data_link.symlink_to(qlib_data)
        elif not cn_data_link.exists():
            cn_data_link.symlink_to(qlib_data)

    return ApiResponse(success=True, message="配置已更新")


# ---- WebSocket endpoint ----

@app.websocket("/ws/mining/{task_id}")
async def ws_mining(websocket: WebSocket, task_id: str):
    """WebSocket for real-time experiment updates."""
    await websocket.accept()

    if task_id not in ws_connections:
        ws_connections[task_id] = []
    ws_connections[task_id].append(websocket)

    # Send current state immediately
    if task_id in tasks:
        try:
            await websocket.send_json({
                "type": "progress",
                "taskId": task_id,
                "data": tasks[task_id].get("progress", {}),
                "timestamp": _now(),
            })
            for diagnostic in tasks[task_id].get("diagnostics", []):
                await websocket.send_json({
                    "type": "diagnostic",
                    "taskId": task_id,
                    "data": diagnostic,
                    "timestamp": _now(),
                })
            # Send recent logs
            for log in tasks[task_id].get("logs", [])[-20:]:
                await websocket.send_json({
                    "type": "log",
                    "taskId": task_id,
                    "data": log,
                    "timestamp": _now(),
                })
        except Exception:
            pass

    try:
        while True:
            data = await websocket.receive_text()
            # Heartbeat
            if data == "ping":
                await websocket.send_json({
                    "type": "heartbeat",
                    "timestamp": _now(),
                })
    except WebSocketDisconnect:
        if task_id in ws_connections:
            try:
                ws_connections[task_id].remove(websocket)
            except ValueError:
                pass


# ========================== Entry Point ==========================

def _update_mining_metrics(task: Dict[str, Any]):
    """
    Update mining task metrics from the generated factor library.
    Calculates best factor stats and extracts top 10 factors.
    """
    jsons = _find_factor_jsons()
    # Prefer library with matching suffix if configured
    target_lib = None
    config = task.get("config", {})
    suffix = config.get("librarySuffix")
    
    if suffix:
        candidate = PROJECT_ROOT / "data" / "factorlib" / f"all_factors_library_{suffix}.json"
        # Fix: If suffix is specified, we ONLY look at this file.
        # If it doesn't exist yet, it means no factors have been mined yet for this task.
        if candidate.exists():
            target_lib = str(candidate)
        else:
            # Task specific file not found -> assume empty state
            return
            
    elif jsons:
        # No suffix provided, fallback to latest existing library (legacy behavior)
        target_lib = jsons[0]
        
    if not target_lib:
        return

    # Check modification time
    try:
        mtime = os.path.getmtime(target_lib)
        created_at_str = task.get("createdAt")
        if created_at_str:
            created_at_dt = datetime.fromisoformat(created_at_str)
            # Add a small buffer (e.g. 1 second) to avoid race conditions where file is created immediately
            if mtime < created_at_dt.timestamp():
                # File is older than the task -> ignore it
                return
    except Exception:
        pass

    try:
        lib = _load_factor_library(target_lib)
        factors = lib.get("factors", {})
        
        # 1. Update basic stats
        total = len(factors)
        task["metrics"]["totalFactors"] = total
        
        high = medium = low = 0
        factor_list = []
        
        for f_id, f_info in factors.items():
            # Check if this factor was created after task start
            # If we are using a shared library file (unlikely with new logic, but possible if user forces it),
            # we must ensure we don't display old factors.
            try:
                added_at_str = f_info.get("added_at", "")
                created_at_str = task.get("createdAt", "")
                if added_at_str and created_at_str:
                    # Parse timestamps
                    # added_at usually in isoformat
                    added_at_dt = datetime.fromisoformat(added_at_str)
                    created_at_dt = datetime.fromisoformat(created_at_str)
                    if added_at_dt < created_at_dt:
                        continue
            except Exception:
                pass # If date parsing fails, be permissive or conservative? Permissive for now.

            bt = f_info.get("backtest_results", {})
            q = _classify_quality(bt)
            if q == "high": high += 1
            elif q == "medium": medium += 1
            else: low += 1
            
            # Prepare for top 10 list
            # Normalize metrics
            ic = bt.get("IC", bt.get("1day.excess_return_without_cost.information_coefficient", 0))
            icir = bt.get("ICIR", bt.get("1day.excess_return_without_cost.information_coefficient_ir", 0))
            rank_ic = bt.get("Rank IC", bt.get("rank_ic", bt.get("1day.excess_return_without_cost.rank_ic", 0)))
            rank_icir = bt.get("Rank ICIR", bt.get("rank_ic_ir", bt.get("1day.excess_return_without_cost.rank_ic_ir", 0)))
            
            annual_ret = bt.get("1day.excess_return_without_cost.annualized_return", 0)
            max_dd = bt.get("1day.excess_return_with_cost.max_drawdown", 
                                    bt.get("1day.excess_return_without_cost.max_drawdown", 0))
            
            # Calmar Ratio = Annual Return / Max Drawdown (absolute value)
            # Avoid division by zero
            cr = 0
            if max_dd < 0:
                cr = annual_ret / abs(max_dd)
            elif max_dd > 0:
                cr = annual_ret / max_dd
            
            factor_list.append({
                "factorName": f_info.get("factor_name", f_id),
                "factorExpression": f_info.get("factor_expression", ""),
                "rankIc": rank_ic,
                "rankIcir": rank_icir,
                "ic": ic,
                "icir": icir,
                "annualReturn": annual_ret,
                "sharpeRatio": bt.get("1day.excess_return_with_cost.information_ratio", 
                                    bt.get("1day.excess_return_without_cost.information_ratio", 0)),
                "maxDrawdown": max_dd,
                "calmarRatio": cr
            })

        task["metrics"]["highQualityFactors"] = high
        task["metrics"]["mediumQualityFactors"] = medium
        task["metrics"]["lowQualityFactors"] = low
        
        # 2. Find best factor
        if factor_list:
            # Sort by RankIC desc
            factor_list.sort(key=lambda x: x["rankIc"], reverse=True)
            best = factor_list[0]
            
            # Update task metrics with best factor's stats
            task["metrics"]["annualReturn"] = best["annualReturn"]
            task["metrics"]["rankIc"] = best["rankIc"]
            task["metrics"]["sharpeRatio"] = best["sharpeRatio"]
            task["metrics"]["maxDrawdown"] = best["maxDrawdown"]
            task["metrics"]["factorName"] = best["factorName"]
            
            # 3. Top 10 Factors
            task["metrics"]["top10Factors"] = factor_list[:10]
            
    except Exception:
        pass # Best effort

if __name__ == "__main__":
    import uvicorn
    host = os.environ.get("BACKEND_HOST", "127.0.0.1")
    port = int(os.environ.get("BACKEND_PORT", "8000"))
    uvicorn.run(app, host=host, port=port, log_level="info")
