"""Explicit, inactive-by-default V10 runtime; historical entrypoints stay frozen."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, suppress
from datetime import datetime
import hashlib
import importlib
import os
from pathlib import Path
import time
from typing import Any
from zoneinfo import ZoneInfo

from fastapi.responses import JSONResponse
from market_data import intraday_capture_v4 as capture_v4
from market_data import slot_aligned_capture_runtime as aligned_capture
from market_data.intraday_shadow_v10 import validate_v10_declaration

TZ = ZoneInfo('Asia/Shanghai')
DECLARATION_KEY = 'QA_V10_DECLARATION'
RUNTIME_CONTRACT = {'mode': 'FORWARD_RUNTIME', 'capturePollSeconds': 60,
                    'shadowPollSeconds': 60, 'maintenancePollSeconds': 900,
                    'captureScheduler': dict(aligned_capture.SCHEDULER_CONTRACT)}
MAINTENANCE_WORKER = 'v10_provider_maintenance'
REQUIRED_RUNTIME_FILES = {
    'frontend-v2/backend/app.py', 'frontend-v2/backend/v10_app.py',
    'frontend-v2/backend/v10_runtime.py', 'frontend-v2/backend/system_settings.py',
    'frontend-v2/backend/market_data/v10_provider_maintenance.py',
    'frontend-v2/backend/market_data/slot_aligned_capture_runtime.py',
}


def _now():
    return datetime.now(TZ)


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


# Pin the code image at import, rather than comparing two reads of today's disk.
_LOADED_RUNTIME_SHA256 = _sha(__file__)


def _maintenance():
    # No maintenance import, discovery, directory or lock on the disabled path.
    return importlib.import_module('market_data.v10_provider_maintenance')


@asynccontextmanager
async def _offthread_lock(manager):
    """Do not block the event loop, or orphan an acquired flock on cancellation."""
    acquisition = asyncio.create_task(asyncio.to_thread(manager.__enter__))
    try:
        await asyncio.shield(acquisition)
    except asyncio.CancelledError:
        # The underlying thread cannot be cancelled. Wait for it to acquire,
        # then release rather than leaving a lock held after this request ends.
        try:
            await acquisition
        finally:
            if (acquisition.done() and not acquisition.cancelled()
                    and acquisition.exception() is None):
                await asyncio.to_thread(manager.__exit__, None, None, None)
        raise
    try:
        yield
    finally:
        await asyncio.shield(asyncio.to_thread(manager.__exit__, None, None, None))


class V10Runtime:
    def __init__(self, backend, *, entrypoint_sha256=None):
        self.backend = backend
        self.entrypoint_sha256 = entrypoint_sha256
        self.root = Path(backend.PROJECT_ROOT).resolve()
        self.started_at = _now()
        self.selected = self._selection()
        self.declaration_sha = None
        self.error = None
        self.original_readiness = getattr(backend, '_runtime_readiness_payload', None)
        self.bootstrap_complete = False
        self.stopping = False
        self.loaded_files = {relative: _sha(self.root / relative) for relative in (
            'frontend-v2/backend/app.py', 'frontend-v2/backend/system_settings.py')}

    def _selection(self):
        value = self.backend._load_dotenv_dict().get(DECLARATION_KEY)
        return str(value).strip() if value else None

    @property
    def configured(self):
        try:
            return bool(self.selected or self._selection())
        except Exception:
            return True

    def validate(self):
        try:
            selection = self._selection()
            if selection != self.selected:
                self.error = 'V10_DECLARATION_SELECTION_CHANGED'
                return None
            if not selection:
                self.error = None
                return None
            path = Path(selection).expanduser()
            path = (self.root / path).resolve() if not path.is_absolute() else path.resolve()
            if self.root not in path.parents:
                raise ValueError('declaration outside root')
            validated = validate_v10_declaration(path)
            declaration = validated['declaration']
            digest = validated['declarationSha256']
            if declaration.get('runtime') != RUNTIME_CONTRACT:
                raise ValueError('runtime contract drift')
            if self.declaration_sha is not None and digest != self.declaration_sha:
                raise ValueError('declaration changed in process')
            files = {}
            for row in declaration['implementation']['files']:
                item = Path(row['path']).expanduser()
                item = (self.root / item).resolve() if not item.is_absolute() else item.resolve()
                if item in files:
                    raise ValueError('duplicate implementation binding')
                files[item] = row['sha256']
            for relative in REQUIRED_RUNTIME_FILES:
                file = self.root / relative
                if files.get(file) != _sha(file):
                    raise ValueError('runtime implementation binding drift')
                if relative in self.loaded_files and files[file] != self.loaded_files[relative]:
                    raise ValueError('loaded dependency implementation drift')
            # The current process must actually be running the bound adapter.
            if files[self.root / 'frontend-v2/backend/v10_runtime.py'] != _LOADED_RUNTIME_SHA256:
                raise ValueError('loaded runtime is not the declared implementation')
            if (self.entrypoint_sha256 is not None
                    and files[self.root / 'frontend-v2/backend/v10_app.py'] != self.entrypoint_sha256):
                raise ValueError('loaded entrypoint is not the declared implementation')
            if declaration.get('captureContract', {}).get('pollSeconds') != 60:
                raise ValueError('capture poll contract drift')
            aligned_capture.capture_scheduler_implementation_contract()
            self.declaration_sha = digest
            self.error = None
            return {**validated, 'path': path}
        except Exception:
            self.error = 'V10_DECLARATION_INVALID'
            return None

    def current(self, validated):
        try:
            provider = Path(_maintenance().resolve_current_provider(validated['path'])).resolve()
            return provider
        except Exception:
            self.error = 'V10_PROVIDER_UNVERIFIED'
            return None

    def active(self):
        validated = self.validate()
        if validated is None:
            return None, None
        return validated, self.current(validated)

    def capture_config(self):
        validated, provider = self.active()
        return {'enabled': bool(validated and provider), 'pollSeconds': 60,
                'maxCycleSeconds': 300, 'providerPath': str(provider) if provider else None,
                'outputRoot': str(self._resolve(validated['declaration']['captureRoot'])) if validated else str(self.root / 'data/market/a_share/intraday/v10_disabled'),
                'calibrationEnabled': False, 'calibrationDeclaration': ''}

    def shadow_config(self):
        validated, provider = self.active()
        return {'enabled': bool(validated and provider), 'pollSeconds': 60,
                'maxCycleSeconds': 600,
                'declaration': str(validated['path']) if validated else ''}

    @staticmethod
    def research_config():
        return {'enabled': False, 'pollSeconds': 900, 'maxCycleSeconds': 1800}

    def _resolve(self, value):
        path = Path(value).expanduser()
        return (self.root / path).resolve() if not path.is_absolute() else path.resolve()

    def _maintenance_state(self, validated):
        try:
            status = _maintenance().read_status(validated['path']) or {}
            checked = datetime.fromisoformat(str(status.get('checkedAt')))
            current = _now()
            ready = (status.get('runId') == validated['declaration']['runId']
                     and status.get('declarationSha256') == validated['declarationSha256']
                     and status.get('status') in {'NO_NEW_SESSION', 'ADVANCED'}
                     and status.get('runtimeConsistent') is True
                     and checked.tzinfo is not None
                     and self.started_at <= checked <= current
                     and (current - checked).total_seconds() <= 1200)
            return status, bool(ready)
        except Exception:
            return {}, False

    def _require_cycle(self):
        validated = self.validate()
        if validated is None:
            raise RuntimeError(self.error or 'V10_RUNTIME_DISABLED')
        provider = self.current(validated)
        if provider is None:
            raise RuntimeError('V10_PROVIDER_UNVERIFIED')
        _, maintained = self._maintenance_state(validated)
        if not maintained:
            raise RuntimeError('V10_MAINTENANCE_NOT_READY')
        return validated, provider

    def capture_cycle(self, *args, **kwargs):
        validated = self.validate()
        if validated is None:
            return {'status': 'DISABLED', 'errorCode': self.error}
        with _maintenance().runtime_lock(validated['path']):
            validated, provider = self._require_cycle()
            # app's config/effective-provider selection happened before this lock.
            # A maintenance switch in between must never send stale source binding.
            supplied = kwargs.get('provider_path')
            if supplied is None and args:
                supplied = args[0]
            if supplied is None or Path(supplied).resolve() != provider:
                raise RuntimeError('V10_CAPTURE_PROVIDER_CHANGED')
            expected_root = self._resolve(validated['declaration']['captureRoot'])
            supplied_root = kwargs.get('output_root')
            if supplied_root is None and len(args) > 1:
                supplied_root = args[1]
            if supplied_root is None or Path(supplied_root).resolve() != expected_root:
                raise RuntimeError('V10_CAPTURE_ROOT_CHANGED')
            if (kwargs.get('poll_seconds', 60) != 60 or kwargs.get('force', False)
                    or kwargs.get('client') is not None or kwargs.get('clock') is not None):
                raise RuntimeError('V10_CAPTURE_RUNTIME_CONTRACT_CHANGED')
            return capture_v4.run_capture_cycle(*args, **kwargs)

    def runtime_binding(self):
        validated = self.validate()
        if validated is None:
            return self.capture_cycle, {'enabled': False, 'recoveryError': self.error,
                                        'entrypointModule': __name__}
        return self.capture_cycle, dict(validated['captureImplementation']['runtime'])

    def effective_provider(self, config, *, now=None):
        validated, provider = self.active()
        if not validated or provider is None:
            raise RuntimeError(self.error or 'V10_RUNTIME_DISABLED')
        if Path(config.get('providerPath') or '').resolve() != provider:
            raise RuntimeError('V10_CAPTURE_PROVIDER_CHANGED')
        return provider

    def coordinator(self):
        # Delay constructor/SQLite I/O until the shared lock is held.
        return _LockedCoordinator(self)

    def _worker(self, name, enabled):
        detail = self.backend._background_worker_readiness(
            name, enabled=enabled, now_monotonic=time.monotonic())
        safe = {key: detail.get(key) for key in (
            'enabled', 'ready', 'status', 'taskAlive', 'reason', 'lastStartedAt',
            'lastCompletedAt', 'lastHeartbeatAt', 'nextDueAt', 'consecutiveErrors')}
        # Startup grace is process readiness, not proof a worker is alive.
        safe['ready'] = bool(enabled and detail.get('ready') and detail.get('taskAlive'))
        safe['cycle'] = {key: detail.get(key) for key in (
            'status', 'lastStartedAt', 'lastCompletedAt', 'nextDueAt', 'consecutiveErrors')}
        return safe

    def status(self):
        validated, provider = self.active()
        enabled = bool(validated and provider)
        workers = {
            'capture': self._worker(self.backend.INTRADAY_CAPTURE_WORKER, enabled),
            'shadow': self._worker(self.backend.INTRADAY_SHADOW_WORKER, enabled),
            'maintenance': self._worker(MAINTENANCE_WORKER, bool(validated)),
        }
        try:
            scheduler = self.backend._slot_aligned_capture_runtime
            scheduler_state = scheduler.status()
            if not isinstance(scheduler_state, dict):
                raise ValueError('scheduler status is unavailable')
            scheduler_verified = (
                self.backend._intraday_capture_loop == scheduler.loop
                and isinstance(scheduler_state.get('phase'), str)
                and bool(scheduler_state['phase'])
                and scheduler_state['phase'] not in {'CLOCK_INVALID', 'UNKNOWN'}
                and not scheduler.clock_fault)
        except Exception:
            scheduler_state, scheduler_verified = {}, False
        workers['capture']['schedulerPhase'] = scheduler_state.get('phase', 'UNKNOWN')
        workers['capture']['ready'] = workers['capture']['ready'] and scheduler_verified
        if enabled and not scheduler_verified:
            workers['capture']['reason'] = 'V10_CAPTURE_SCHEDULER_UNVERIFIED'
        maintenance_state, maintained = self._maintenance_state(validated) if validated else ({}, False)
        workers['maintenance']['businessStatus'] = maintenance_state.get('status', 'UNKNOWN')
        workers['maintenance']['ready'] = workers['maintenance']['ready'] and maintained
        healthy = enabled and all(worker['ready'] for worker in workers.values())
        stage = ('DECLARATION_INVALID' if self.error else 'DISABLED') if not enabled else (
            'FORWARD_RUNNING_UNACCEPTED' if healthy else 'WAITING_RUNTIME')
        return {'mode': 'FORWARD_RUNTIME', 'configured': self.configured,
                'enabled': self.configured, 'shadowEnabled': enabled,
                'captureEnabled': enabled, 'maintenanceEnabled': bool(validated),
                'workers': workers, 'runId': validated['declaration']['runId'] if validated else None,
                'declarationSha256': validated['declarationSha256'] if validated else None,
                'autoShadowAccepted': False, 'accepted': False, 'stage': stage,
                'errorCode': self.error, 'error': self.error,
                'ready': bool(healthy), 'checkedAt': _now().isoformat()}

    def readiness_payload(self, now_monotonic=None):
        # Keep the frozen supervisor identity contract while adding maintenance.
        if self.original_readiness is not None:
            payload = self.original_readiness(now_monotonic=now_monotonic)
        else:
            payload = {'pid': os.getpid(),
                       'instanceId': getattr(self.backend, 'PROCESS_INSTANCE_ID', 'v10'),
                       'startedAt': self.started_at.isoformat(), 'workers': {}}
        status = self.status()
        ready = (not status['configured'] and not status['errorCode']) or status['ready']
        maintenance = dict(status['workers']['maintenance'])
        if not status['configured']:
            maintenance['ready'] = True
        payload = {**payload, 'workers': {**payload.get('workers', {}),
                    MAINTENANCE_WORKER: maintenance},
                   'status': 'healthy' if ready else 'unhealthy', 'ready': bool(ready),
                   'runId': status['runId'], 'declarationSha256': status['declarationSha256'],
                   'autoShadowAccepted': False, 'mode': 'FORWARD_RUNTIME',
                   'configError': status['errorCode'], 'v10': status}
        return payload

    def preflight(self, **kwargs):
        status = self.status()
        failed = [name for name, worker in status['workers'].items() if not worker['ready']]
        return {'configured': status['configured'], 'ready': status['ready'],
                'status': 'READY' if status['ready'] else 'NOT_READY' if status['configured'] else 'DISABLED',
                'checkedAt': status['checkedAt'], 'ageSeconds': 0, 'maxAgeSeconds': 900,
                'passedChecks': 3-len(failed), 'totalChecks': 3, 'failedChecks': failed,
                'runId': status['runId'], 'declarationSha256': status['declarationSha256']}

    def should_continue(self):
        return not self.stopping and self.validate() is not None

    async def maintenance_loop(self):
        while True:
            validated = self.validate()
            if validated is None:
                self.backend._mark_background_worker_disabled(MAINTENANCE_WORKER)
                await asyncio.sleep(60)
                continue
            self.backend._mark_background_worker_running(MAINTENANCE_WORKER, 1800)
            error = None
            try:
                # Bootstrap and run_cycle each own their lock. No outer flock.
                if not self.bootstrap_complete:
                    selected = await asyncio.to_thread(_maintenance().bootstrap_root_provider,
                        validated['path'], enabled=True, should_continue=self.should_continue)
                    if selected.get('status') not in {'ROOT_SELECTED', 'ALREADY_INITIALIZED'}:
                        raise RuntimeError('V10_MAINTENANCE_BOOTSTRAP_BLOCKED')
                    self.bootstrap_complete = True
                result = await asyncio.to_thread(_maintenance().run_cycle,
                    validated['path'], enabled=True, poll_seconds=900,
                    should_continue=self.should_continue)
                if (result.get('status') not in {'NO_NEW_SESSION', 'ADVANCED'}
                        or result.get('runtimeConsistent') is not True):
                    error = 'V10_MAINTENANCE_BLOCKED'
            except Exception:
                error = 'V10_MAINTENANCE_FAILED'
            self.backend._mark_background_worker_idle(MAINTENANCE_WORKER, 900, error=error)
            await asyncio.sleep(900)

    @asynccontextmanager
    async def lifespan(self, app):
        # Keep the aligned capture and frozen shadow workers registered while a
        # blocked Provider pointer recovers; their live configs remain disabled.
        # Never start the old daily-research worker.
        self.stopping = False
        tasks = {}
        if self.validate() is not None:
            for name, loop in (
                (self.backend.INTRADAY_CAPTURE_WORKER, self.backend._intraday_capture_loop),
                (self.backend.INTRADAY_SHADOW_WORKER, self.backend._intraday_shadow_loop),
                (MAINTENANCE_WORKER, self.maintenance_loop),
            ):
                task = asyncio.create_task(loop(), name=name)
                tasks[name] = task
                self.backend._register_background_worker(name, task)
        try:
            yield
        finally:
            self.stopping = True
            for task in tasks.values():
                task.cancel()
            for name, task in tasks.items():
                with suppress(asyncio.CancelledError, Exception):
                    await task
                self.backend._mark_background_worker_stopped(name)
                self.backend._BACKGROUND_TASKS.pop(name, None)


class _LockedCoordinator:
    def __init__(self, runtime):
        self.runtime = runtime

    def _call(self, method, *args, **kwargs):
        runtime = self.runtime
        validated = runtime.validate()
        if validated is None:
            raise RuntimeError(runtime.error or 'V10_RUNTIME_DISABLED')
        with _maintenance().runtime_lock(validated['path']):
            validated, _ = runtime._require_cycle()
            from market_data.intraday_shadow_v10 import IntradayShadowCoordinator
            coordinator = IntradayShadowCoordinator(validated['path'])
            return getattr(coordinator, method)(*args, **kwargs)

    def run_cycle(self, *args, **kwargs):
        return self._call('run_cycle', *args, **kwargs)

    def get_state(self):
        return self._call('get_state')

    def read_automation_status(self):
        return self._call('read_automation_status')

    @property
    def declaration(self):
        validated = self.runtime.validate()
        if validated is None:
            raise RuntimeError(self.runtime.error or 'V10_RUNTIME_DISABLED')
        return validated['declaration']

    @property
    def run_id(self):
        return self.declaration['runId']


def install_v10_runtime(backend, *, entrypoint_sha256=None):
    if getattr(backend, '_v10_runtime_adapter', None) is not None:
        return backend._v10_runtime_adapter
    runtime = V10Runtime(backend, entrypoint_sha256=entrypoint_sha256)
    backend._v10_runtime_adapter = runtime
    backend._research_shadow_auto_config = runtime.research_config
    backend._intraday_capture_config = runtime.capture_config
    backend._intraday_capture_runtime_binding = runtime.runtime_binding
    backend._intraday_capture_effective_provider_path = runtime.effective_provider
    aligned_capture.install_slot_aligned_capture_runtime(backend)
    backend._intraday_shadow_config = runtime.shadow_config
    backend._intraday_shadow_coordinator = runtime.coordinator
    backend._intraday_preflight_summary = runtime.preflight
    backend._runtime_readiness_payload = runtime.readiness_payload
    backend.app.router.lifespan_context = runtime.lifespan

    @backend.app.middleware('http')
    async def runtime_guard(request, call_next):
        path = request.url.path
        if path.startswith(('/api/v1/research-shadow/', '/api/v1/paper-trading/')):
            return JSONResponse(status_code=409, content={'success': False,
                'errorCode': 'HISTORICAL_ENGINE_DISABLED_IN_V10'})
        if request.method == 'PUT' and path == '/api/v1/system/config' and runtime.configured:
            try:
                payload = await request.json()
                if not isinstance(payload, dict):
                    raise ValueError('invalid payload')
            except Exception:
                return JSONResponse(status_code=400, content={'success': False,
                    'errorCode': 'V10_INVALID_CONFIG_REQUEST'})
            def provider_changed():
                current = backend._load_dotenv_dict()
                return any(key in payload and payload[key] != current.get(key, '')
                           for key in ('QLIB_DATA_DIR', 'QLIB_PROVIDER_URI'))
            if provider_changed():
                return JSONResponse(status_code=409, content={'success': False,
                    'errorCode': 'V10_PROVIDER_CONFIG_LOCKED'})
            validated = await asyncio.to_thread(runtime.validate)
            if validated is None:
                return JSONResponse(status_code=503, content={'success': False,
                    'errorCode': runtime.error or 'V10_RUNTIME_UNVERIFIED'})
            try:
                async with _offthread_lock(_maintenance().runtime_lock(validated['path'])):
                    if not await asyncio.to_thread(runtime.should_continue):
                        return JSONResponse(status_code=503, content={'success': False,
                            'errorCode': 'V10_RUNTIME_UNVERIFIED'})
                    if provider_changed():
                        return JSONResponse(status_code=409, content={'success': False,
                            'errorCode': 'V10_PROVIDER_CONFIG_LOCKED'})
                    # Reuse the installed handler under the same lock as the
                    # maintenance .env read/replace transaction, including API-only edits.
                    return await call_next(request)
            except Exception:
                return JSONResponse(status_code=503, content={'success': False,
                    'errorCode': 'V10_CONFIG_SAVE_UNAVAILABLE'})

        return await call_next(request)

    async def status():
        result = await asyncio.to_thread(runtime.status)
        return JSONResponse(status_code=503 if result['errorCode'] else 200,
                            content={'success': not bool(result['errorCode']), 'data': result})

    async def ready():
        payload = await asyncio.to_thread(runtime.readiness_payload)
        result = payload.pop('v10')
        # This endpoint requires an opted-in, operational V10 runtime. Service
        # liveness with all workers disabled is separately visible in /health.
        payload.update(ready=result['ready'], status='healthy' if result['ready'] else 'unhealthy')
        return JSONResponse(status_code=200 if result['ready'] else 503,
                            content={**payload, 'success': result['ready'], 'data': result})

    async def health():
        payload = await asyncio.to_thread(runtime.readiness_payload)
        result = payload.pop('v10')
        return JSONResponse(status_code=200 if payload['ready'] else 503,
                            content={**payload, 'success': payload['ready'], 'data': result})

    backend.app.add_api_route('/api/v1/v10-runtime/status', status, methods=['GET'])
    backend.app.add_api_route('/api/v1/shadow-recovery/status', status, methods=['GET'])
    backend.app.add_api_route('/api/v1/v10-runtime/health', health, methods=['GET'])
    backend.app.add_api_route('/api/v1/v10-runtime/ready', ready, methods=['GET'])
    backend.app.openapi_schema = None
    return runtime
