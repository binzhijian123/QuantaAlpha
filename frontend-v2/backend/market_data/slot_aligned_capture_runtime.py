"""Minute-grid capture scheduling without changing the frozen collector.

Only real current-slot captures are dispatched. A slow lock/network can still
miss a slot; no scheduler can manufacture the lost observation afterwards.
"""
from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta
import hashlib
import json
from pathlib import Path
import sys
from typing import Any
from zoneinfo import ZoneInfo

from .intraday_capture import (
    list_verified_slot_records, read_capture_status, update_capture_calibration_status,
)
from .intraday_calibration import finalize_calibration_report, load_calibration_declaration
from .intraday_capture_health import summarize_latest_capture_day

TZ = ZoneInfo('Asia/Shanghai')
SCHEDULER_CONTRACT = {
    'pollSeconds': 60, 'phaseSeconds': 5, 'timezone': 'Asia/Shanghai',
    'noForce': True, 'noInjectedNow': True, 'noHistoricalBackfill': True,
    'overrunPolicy': 'current-slot-only',
    'dateGuard': 'optional-recoveryTradeDate-exact-real-date',
    'startupPolicy': 'next-real-grid-tick',
}
_ROOT = Path(__file__).resolve().parents[3]
_BOUND_PATHS = (
    Path(__file__).resolve(), _ROOT / 'frontend-v2/backend/app.py',
    Path(__file__).with_name('intraday_capture.py'),
    Path(__file__).with_name('intraday_capture_v4.py'),
    Path(__file__).with_name('intraday_calibration.py'),
    Path(__file__).with_name('intraday_capture_health.py'),
)


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


_LOADED_HASHES = {path: _sha(path) for path in _BOUND_PATHS}


def capture_scheduler_implementation_contract():
    files = []
    for path, expected in _LOADED_HASHES.items():
        if _sha(path) != expected:
            raise RuntimeError('CAPTURE_SCHEDULER_IMPLEMENTATION_DRIFT')
        files.append({'path': str(path), 'sha256': expected})
    payload = {'version': 1, 'files': sorted(files, key=lambda row: row['path']),
               'scheduler': dict(SCHEDULER_CONTRACT)}
    payload['sha256'] = hashlib.sha256(json.dumps(
        payload, sort_keys=True, ensure_ascii=True, separators=(',', ':')).encode()).hexdigest()
    return payload


def _slot(current):
    return current.replace(second=0, microsecond=0)


def next_grid_tick(current):
    """Startup and already-observed slots wait for the next real :05 tick."""
    tick = _slot(current) + timedelta(seconds=5)
    return tick if current <= tick else tick + timedelta(minutes=1)


class _DateWindowClosed(Exception):
    def __init__(self, phase):
        self.phase = phase


class SlotAlignedCaptureRuntime:
    def __init__(self, backend, *, wall_clock=None, sleep=None, run_sync=None):
        # Dependency injection is solely for virtual-clock unit tests. The public
        # installer always uses actual time; no now/clock enters the collector API.
        self.backend = backend
        self.wall_clock = wall_clock or (lambda: datetime.now(TZ))
        self.sleep = sleep or asyncio.sleep
        self.run_sync = run_sync or asyncio.to_thread
        self.previous_cycle = None
        self.close_health_evaluated = False
        self.last_wall = None
        self.last_attempted_slot = None
        self.next_due = None
        self.clock_fault = False
        self.phase = 'STARTING'
        self.error = None
        self._expected_contract = capture_scheduler_implementation_contract()['sha256']

    def _time(self):
        current = self.wall_clock()
        if current.tzinfo is None or current.utcoffset() is None:
            raise RuntimeError('CAPTURE_SCHEDULER_CLOCK_UNAWARE')
        current = current.astimezone(TZ)
        if self.clock_fault or (self.last_wall is not None and current < self.last_wall):
            self.clock_fault = True
            raise RuntimeError('CAPTURE_SCHEDULER_CLOCK_MOVED_BACKWARDS')
        self.last_wall = current
        return current

    def status(self):
        return {'phase': self.phase, 'errorCode': self.error,
                'lastObservedAt': self.last_wall.isoformat() if self.last_wall else None,
                'lastAttemptedSlot': self.last_attempted_slot.isoformat() if self.last_attempted_slot else None,
                'nextDueAt': self.next_due.isoformat() if self.next_due else None}

    def _idle(self, delay, error=None):
        delay = max(.001, min(60., delay))
        self.error = error
        self.backend._mark_background_worker_idle(
            self.backend.INTRADAY_CAPTURE_WORKER, delay, error=error)
        return delay

    async def _emit(self, events):
        for event in events:
            name, day = event if isinstance(event, tuple) else (event, None)
            try:
                await self.run_sync(self.backend._emit_fixed_runtime_alert, name, subject_date=day)
            except Exception:
                print('[intraday-capture] alert delivery failed', file=sys.stderr, flush=True)

    def _failure_events(self):
        events = self.backend._intraday_capture_alert_events(self.previous_cycle, failed=True)
        self.previous_cycle = {'status': 'ERROR'}
        return events

    def _date_guard(self, config, current):
        target = config.get('recoveryTradeDate')
        if target is not None and current.date().isoformat() != target:
            raise _DateWindowClosed('WAITING_CALIBRATION_DATE' if current.date().isoformat() < target
                                    else 'CALIBRATION_DATE_ENDED')

    def _guarded_call(self, config, function, *args, **kwargs):
        # Runs in the actual worker thread, after executor queueing. No artificial
        # timestamp or outer .capture.lock is passed into the frozen collector.
        fresh = self.backend._intraday_capture_config()
        if any(fresh.get(key) != config.get(key) for key in (
                'enabled', 'pollSeconds', 'providerPath', 'outputRoot', 'recoveryTradeDate',
                'calibrationEnabled', 'calibrationDeclaration')):
            raise RuntimeError('CAPTURE_SCHEDULER_CONFIGURATION_CHANGED')
        if capture_scheduler_implementation_contract()['sha256'] != self._expected_contract:
            raise RuntimeError('CAPTURE_SCHEDULER_IMPLEMENTATION_DRIFT')
        self._date_guard(config, self._time())
        result = function(*args, **kwargs)
        # A frozen operation already in flight cannot be stopped safely. Mark a
        # boundary overrun degraded without rewriting its historical output.
        try:
            self._date_guard(config, self._time())
        except _DateWindowClosed:
            raise RuntimeError('CAPTURE_SCHEDULER_DATE_CHANGED_DURING_CYCLE') from None
        return result

    async def _cycle(self, config, runner, provider):
        """Preserve one frozen-loop cycle's calibration, close audit and alerts."""
        if self.previous_cycle is None:
            self.previous_cycle = await self.run_sync(read_capture_status, config['outputRoot'])
        status = await self.run_sync(self._guarded_call, config, runner,
                                     provider, config['outputRoot'], poll_seconds=60)
        calibration = status.get('calibration') or {}
        configured_date = ''
        if config['calibrationEnabled']:
            declaration, _ = await self.run_sync(
                load_calibration_declaration, config['calibrationDeclaration'])
            configured_date = str(declaration['calibration']['tradeDate'])
        should_calibrate = self.backend._intraday_calibration_refresh_required(
            enabled=config['calibrationEnabled'], calibration=calibration,
            configured_trade_date=configured_date)
        calibration_error = None
        if should_calibrate:
            try:
                calibration = await self.run_sync(self._guarded_call, config, finalize_calibration_report,
                    config['calibrationDeclaration'], config['outputRoot'])
            except _DateWindowClosed:
                raise
            except Exception as exc:
                calibration_error = self.backend._sanitize_log_line(str(exc))[:1000]
                calibration = {
                    'status': 'ERROR', 'finalized': False,
                    'decision': 'CALIBRATION_OPERATIONAL_ERROR',
                    'tradeDate': configured_date or None, 'file': None, 'reportSha256': None,
                    'slotCoverageRatio': 0., 'medianUsableCoverageRatio': 0.,
                    'capturedExpectedSlotCount': 0, 'expectedSlotCount': 242,
                    'profitEvidenceEligible': False, 'error': calibration_error,
                }
            status = await self.run_sync(self._guarded_call, config, update_capture_calibration_status,
                                         config['outputRoot'], calibration)
        capture_error = calibration_error or (
            str(status.get('lastError') or 'unknown error') if status.get('status') == 'ERROR' else None)
        events = self.backend._intraday_capture_alert_events(self.previous_cycle, status)
        if status.get('status') != 'WAITING_MARKET':
            self.close_health_evaluated = False
        elif not self.close_health_evaluated:
            self.close_health_evaluated = True
            try:
                health = await self.run_sync(summarize_latest_capture_day,
                    config['outputRoot'], poll_seconds=60)
                event = self.backend._intraday_temporal_close_alert(health)
                if event is not None:
                    events.append(event)
            except Exception:
                print('[intraday-capture] temporal close audit failed', file=sys.stderr, flush=True)
        self.previous_cycle = status
        return status, capture_error, events

    async def tick(self):
        """One bounded dispatch decision. Sleeps are computed from absolute ticks."""
        config = None
        try:
            config = await self.run_sync(self.backend._intraday_capture_config)
            if not config['enabled']:
                self.phase, self.error = 'DISABLED', None
                self.previous_cycle = {'status': 'DISABLED'}
                self.next_due = None
                self.backend._mark_background_worker_disabled(self.backend.INTRADAY_CAPTURE_WORKER)
                return 60.
            current = self._time()
            if config['pollSeconds'] != 60:
                raise RuntimeError('CAPTURE_SCHEDULER_POLL_DRIFT')
            if capture_scheduler_implementation_contract()['sha256'] != self._expected_contract:
                raise RuntimeError('CAPTURE_SCHEDULER_IMPLEMENTATION_DRIFT')
            # Waiting for a future calibration date never opens the output tree,
            # binds a quote client or calls the frozen collector/finalizer.
            target = config.get('recoveryTradeDate')
            if target is not None:
                if not isinstance(target, str) or date.fromisoformat(target).isoformat() != target:
                    raise RuntimeError('CAPTURE_SCHEDULER_TRADE_DATE_INVALID')
                today = current.date().isoformat()
                if today != target:
                    self.phase = 'WAITING_CALIBRATION_DATE' if today < target else 'CALIBRATION_DATE_ENDED'
                    self.next_due = None
                    return self._idle(60.)
            if self.next_due is None:
                self.next_due = next_grid_tick(current)
            if current < self.next_due:
                self.phase = 'WAITING_GRID_TICK'
                return self._idle((self.next_due-current).total_seconds())
            # Forward wall jumps and long cycles schedule only the present minute.
            # Earlier missing observations remain missing.
            current_slot = _slot(current)
            phase_at = current_slot + timedelta(seconds=5)
            if current < phase_at:
                self.next_due = phase_at
                self.phase = 'WAITING_GRID_TICK'
                return self._idle((phase_at-current).total_seconds())
            self.backend._mark_background_worker_running(
                self.backend.INTRADAY_CAPTURE_WORKER, config['maxCycleSeconds'])
            if not config['providerPath']:
                raise RuntimeError('QLIB_DATA_DIR is not configured')
            # Revalidate every iteration, rather than caching a binding for life.
            runner, _ = await self.run_sync(self.backend._intraday_capture_runtime_binding)
            provider = await self.run_sync(self.backend._intraday_capture_effective_provider_path, config)
            records_day = current.date().isoformat()
            records = await self.run_sync(list_verified_slot_records,
                config['outputRoot'], records_day)
            observed = {record.get('captureSlot') for _, record in records}
            current = self._time()
            if target is not None and current.date().isoformat() != target:
                self.phase = ('WAITING_CALIBRATION_DATE' if current.date().isoformat() < target
                              else 'CALIBRATION_DATE_ENDED')
                self.next_due = None
                return self._idle(60.)
            current_slot = _slot(current)
            # A lock/preflight can itself pass the next :05. Re-check real time;
            # do not intentionally call the core with an old planned timestamp.
            if current < current_slot + timedelta(seconds=5):
                self.next_due = current_slot + timedelta(seconds=5)
                self.phase = 'WAITING_GRID_TICK'
                return self._idle((self.next_due-current).total_seconds())
            if (current_slot.isoformat() in observed
                    or current_slot == self.last_attempted_slot):
                self.next_due = current_slot + timedelta(seconds=65)
                self.phase = 'WAITING_NEXT_SLOT'
                return self._idle((self.next_due-current).total_seconds())
            fresh_config = await self.run_sync(self.backend._intraday_capture_config)
            if any(fresh_config.get(key) != config.get(key) for key in (
                    'enabled', 'pollSeconds', 'providerPath', 'outputRoot', 'recoveryTradeDate',
                    'calibrationEnabled', 'calibrationDeclaration')):
                raise RuntimeError('CAPTURE_SCHEDULER_CONFIGURATION_CHANGED')
            if capture_scheduler_implementation_contract()['sha256'] != self._expected_contract:
                raise RuntimeError('CAPTURE_SCHEDULER_IMPLEMENTATION_DRIFT')
            current = self._time()
            self._date_guard(config, current)
            if current.date().isoformat() != records_day:
                self.phase = 'RESCHEDULE_CURRENT_DATE'
                return self._idle(.001)
            current_slot = _slot(current)
            if current < current_slot + timedelta(seconds=5):
                self.next_due = current_slot + timedelta(seconds=5)
                self.phase = 'WAITING_GRID_TICK'
                return self._idle((self.next_due-current).total_seconds())
            if current_slot.isoformat() in observed or current_slot == self.last_attempted_slot:
                self.next_due = current_slot + timedelta(seconds=65)
                self.phase = 'WAITING_NEXT_SLOT'
                return self._idle((self.next_due-current).total_seconds())
            self.last_attempted_slot = current_slot
            self.next_due = current_slot + timedelta(seconds=65)
            self.phase = 'CAPTURING'
            _, error, events = await self._cycle(config, runner, provider)
            await self._emit(events)
            if error:
                error = self.backend._sanitize_log_line(error)[:1000]
                print('[intraday-capture] '+error, file=sys.stderr, flush=True)
            current = self._time()
            self.phase = 'CYCLE_ERROR' if error else 'WAITING_NEXT_SLOT'
            # If the previous cycle's request crossed a boundary, next_due can
            # already be due in the CURRENT minute. Dispatch afresh after yielding.
            return self._idle((self.next_due-current).total_seconds(), error)
        except _DateWindowClosed as closed:
            self.phase = closed.phase
            self.next_due = None
            return self._idle(60.)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            error = self.backend._sanitize_log_line(str(exc))[:1000]
            self.phase = 'CLOCK_INVALID' if self.clock_fault else 'CYCLE_ERROR'
            events = self._failure_events()
            await self._emit(events)
            print('[intraday-capture] '+error, file=sys.stderr, flush=True)
            # No tight retry after config/hash/preflight failure. A failed actual
            # attempt remains final for that minute, matching immutable slot rules.
            return self._idle(60., error)

    async def loop(self):
        # A cancelled to_thread operation is not restarted: no wait_for timeout,
        # detached successor loop or overlapping capture thread is created.
        while True:
            delay = await self.tick()
            await self.sleep(delay)


def install_slot_aligned_capture_runtime(backend):
    adapter = getattr(backend, '_slot_aligned_capture_runtime', None)
    if adapter is not None:
        return adapter
    adapter = SlotAlignedCaptureRuntime(backend)
    backend._slot_aligned_capture_runtime = adapter
    backend._intraday_capture_loop = adapter.loop
    return adapter
