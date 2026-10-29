"""Virtual real-clock traces; no service, network or historical evidence writes."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

BACKEND = Path(__file__).resolve().parents[1] / 'frontend-v2/backend'
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))
from market_data import slot_aligned_capture_runtime as aligned
from market_data.intraday_capture import capture_schedule

TZ = ZoneInfo('Asia/Shanghai')


class Clock:
    def __init__(self, text='2026-10-30T09:29:58+08:00'):
        self.current = datetime.fromisoformat(text)
        self.jitter = 0.
        self.sleeps = []

    def __call__(self):
        return self.current

    def advance(self, seconds):
        self.current += timedelta(seconds=seconds)

    async def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.advance(seconds+self.jitter)
        await asyncio.sleep(0)


async def inline(function, *args, **kwargs):
    return function(*args, **kwargs)


class SchedulerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.clock = Clock()
        self.records, self.calls, self.markers, self.alerts = [], [], [], []
        self.config_cost, self.preparation, self.network = 0., 0., 4.
        self.known_days = ['2026-10-30', '2026-11-02']
        self.config = {'enabled':True, 'pollSeconds':60, 'maxCycleSeconds':300,
            'providerPath':'/fixture/provider', 'outputRoot':'/fixture/output',
            'calibrationEnabled':False, 'calibrationDeclaration':'/fixture/calibration.json',
            'recoveryTradeDate':'2026-10-30'}
        self.latest = None
        self.calibration_calls = []
        self.backend = SimpleNamespace(INTRADAY_CAPTURE_WORKER='capture',
            _intraday_capture_config=self.get_config,
            _intraday_capture_runtime_binding=Mock(return_value=(self.runner,{})),
            _intraday_capture_effective_provider_path=Mock(side_effect=lambda config:config['providerPath']),
            _mark_background_worker_disabled=lambda name:self.markers.append(('DISABLED',)),
            _mark_background_worker_running=lambda name,maximum:self.markers.append(('RUNNING',maximum)),
            _mark_background_worker_idle=lambda name,delay,error=None:self.markers.append(('IDLE',delay,error)),
            _intraday_capture_alert_events=self.capture_alerts,
            _intraday_calibration_refresh_required=lambda **kwargs:kwargs['enabled'] and not kwargs['calibration'].get('finalized'),
            _intraday_temporal_close_alert=lambda health:('capture.incomplete',self.clock().date().isoformat()),
            _emit_fixed_runtime_alert=lambda name,subject_date=None:self.alerts.append((name,subject_date)),
            _sanitize_log_line=lambda line:line.replace('SECRET','***'))
        self.patches = [
            patch.object(aligned,'read_capture_status',side_effect=lambda root:self.latest),
            patch.object(aligned,'list_verified_slot_records',side_effect=lambda root,day:[(None,r) for r in self.records if r['captureSlot'][:10]==day]),
            patch.object(aligned,'summarize_latest_capture_day',return_value={'status':'COMPLETE'}),
            patch.object(aligned,'load_calibration_declaration',return_value=({'calibration':{'tradeDate':'2026-10-30'}},'cal-sha')),
            patch.object(aligned,'finalize_calibration_report',side_effect=self.finalize),
            patch.object(aligned,'update_capture_calibration_status',side_effect=self.update_calibration),
        ]
        self.mocks = [p.start() for p in self.patches]
        for p in self.patches:
            self.addCleanup(p.stop)
        self.adapter = self.new_adapter()

    def new_adapter(self):
        return aligned.SlotAlignedCaptureRuntime(self.backend,wall_clock=self.clock,
                                                  sleep=self.clock.sleep,run_sync=inline)

    def get_config(self):
        self.clock.advance(self.config_cost)
        return dict(self.config)

    def capture_alerts(self,previous,current=None,failed=False):
        return ['capture.failure'] if failed else []

    def finalize(self,declaration,output):
        self.calibration_calls.append(self.clock())
        finished = self.clock().strftime('%H:%M:%S') >= '15:05:00'
        return {'status':'PASS' if finished else 'IN_PROGRESS','finalized':finished}

    def update_calibration(self,output,calibration):
        self.latest = {**self.latest,'calibration':calibration}
        return self.latest

    def runner(self,provider,output,**kwargs):
        self.assertEqual(kwargs,{'poll_seconds':60}) # No now/clock/force injection.
        self.assertEqual(provider,self.config['providerPath'])
        core_entry = self.clock()
        slot = core_entry.replace(second=0,microsecond=0)
        schedule = capture_schedule(core_entry,self.known_days)
        if not schedule['active']:
            self.latest = {'status':schedule['status'],'nextCaptureAt':schedule['nextWindowAt']}
            return self.latest
        old = next((record for record in self.records if record['captureSlot']==slot.isoformat()),None)
        if old:
            self.latest = {'status':'REUSED','latestSlotRecord':old,
                           'nextCaptureAt':(slot+timedelta(minutes=1)).isoformat()}
            return self.latest
        self.clock.advance(self.preparation)
        requested = self.clock()
        self.clock.advance(self.network)
        received = self.clock()
        self.calls.append({'slot':slot,'requested':requested,'received':received})
        record = {'captureSlot':slot.isoformat(),'status':'CAPTURED'}
        self.records.append(record)
        self.latest = {'status':'CAPTURED','latestSlotRecord':record,
                       'nextCaptureAt':(slot+timedelta(minutes=1)).isoformat()}
        return self.latest

    async def iterate(self):
        delay = await self.adapter.tick()
        await self.clock.sleep(delay)
        return delay

    async def test_startup_real_tick_04_05_and_tail58(self):
        for wall,expected in [('10:12:04','10:12:05'),('10:12:05','10:12:05'),('10:12:58','10:13:05')]:
            with self.subTest(wall=wall):
                self.clock.current = datetime.fromisoformat('2026-10-30T'+wall+'+08:00')
                self.records.clear(); self.calls.clear(); self.adapter = self.new_adapter()
                await self.iterate()
                if not self.calls:
                    await self.iterate()
                self.assertEqual(self.calls[0]['requested'].strftime('%H:%M:%S'),expected)

    async def test_old_boundary_bug_reproduces_and_new_grid_captures_current_minute(self):
        # A slow pre-core stage can retain the old entry-clock slot even on :05 cadence.
        self.clock.current = datetime.fromisoformat('2026-10-30T10:12:59.995+08:00')
        self.adapter.next_due = datetime.fromisoformat('2026-10-30T10:12:05+08:00')
        self.preparation, self.network = .012650,4.209367
        await self.iterate()
        self.assertEqual(self.calls[0]['slot'].strftime('%H:%M'),'10:12')
        self.assertEqual(self.calls[0]['requested'].isoformat(),'2026-10-30T10:13:00.007650+08:00')
        # The relative loop would dispatch at ~10:14:00 and miss 10:13.
        old_next_entry = datetime.fromisoformat('2026-10-30T10:14:00.040+08:00')
        self.assertEqual(old_next_entry.replace(second=0,microsecond=0).strftime('%H:%M'),'10:14')
        self.preparation = 0.
        await self.iterate()
        self.assertEqual([c['slot'].strftime('%H:%M') for c in self.calls],['10:12','10:13'])
        self.assertEqual(self.calls[1]['requested'].strftime('%H:%M:%S'),'10:13:05')

    async def test_two_complete_days_keep_all_242_expected_slots_with_jitter(self):
        self.clock.jitter = .03
        self.config_cost = .03
        for day in ('2026-10-30','2026-11-02'):
            self.clock.current = datetime.fromisoformat(day+'T09:29:58+08:00')
            self.config['recoveryTradeDate'] = day
            self.records.clear(); self.calls.clear(); self.adapter = self.new_adapter()
            while self.clock().strftime('%H:%M:%S') < '15:01:05':
                await self.iterate()
            expected = []
            for start,end in (('09:30','11:30'),('13:00','15:00')):
                tick = datetime.fromisoformat(day+'T'+start+':00+08:00')
                last = datetime.fromisoformat(day+'T'+end+':00+08:00')
                while tick<=last:
                    expected.append(tick.isoformat()); tick+=timedelta(minutes=1)
            self.assertEqual([r['captureSlot'] for r in self.records],expected)
            self.assertEqual(len(self.calls),242)
            offsets = [c['requested'].second+c['requested'].microsecond/1e6 for c in self.calls]
            self.assertGreaterEqual(min(offsets),5.)
            self.assertLess(max(offsets),5.25) # Does not accumulate 30ms per cycle.

    async def test_overrun_forward_jump_never_backfills_past_minutes(self):
        self.clock.current = datetime.fromisoformat('2026-10-30T10:12:05+08:00')
        self.network = 130.
        await self.iterate()
        self.network = 4.
        await self.iterate()
        self.assertEqual([c['slot'].strftime('%H:%M') for c in self.calls],['10:12','10:14'])
        self.clock.advance(180.)
        await self.iterate()
        self.assertEqual(self.calls[-1]['slot'].strftime('%H:%M'),'10:18')
        self.assertFalse(any(c['slot'].strftime('%H:%M')=='10:13' for c in self.calls))

    async def test_future_and_past_target_never_open_output_or_bind_collector(self):
        for target,phase in [('2026-11-02','WAITING_CALIBRATION_DATE'),('2026-10-29','CALIBRATION_DATE_ENDED')]:
            self.config['recoveryTradeDate'] = target
            self.adapter = self.new_adapter()
            await self.iterate()
            self.assertEqual(self.adapter.status()['phase'],phase)
        self.backend._intraday_capture_runtime_binding.assert_not_called()
        for mock in (self.mocks[0],self.mocks[1],self.mocks[3],self.mocks[4],self.mocks[5]):
            mock.assert_not_called()
        self.assertFalse(self.calls)

    async def test_calendar_lunch_and_close_keep_no_quote_and_close_audit_once(self):
        self.clock.current = datetime.fromisoformat('2026-10-30T11:31:05+08:00')
        await self.iterate(); await self.iterate()
        self.assertFalse(self.calls)
        self.assertEqual(self.mocks[2].call_count,1)
        self.assertEqual(len(self.alerts),1)
        self.clock.current = datetime.fromisoformat('2026-10-30T13:00:05+08:00')
        await self.iterate()
        self.assertEqual(len(self.calls),1)
        self.clock.current = datetime.fromisoformat('2026-10-30T15:01:05+08:00')
        await self.iterate()
        self.assertEqual(self.mocks[2].call_count,2)
        self.known_days.clear()
        self.clock.current = datetime.fromisoformat('2026-10-30T15:02:05+08:00')
        await self.iterate()
        self.assertEqual(self.latest['status'],'CALENDAR_EXHAUSTED')
        self.assertEqual(len(self.calls),1)

    async def test_rollback_same_minute_or_across_day_latches_without_capture(self):
        for rollback in (timedelta(seconds=1),timedelta(days=1)):
            self.clock.current = datetime.fromisoformat('2026-10-30T10:12:05+08:00')
            self.records.clear(); self.calls.clear(); self.adapter = self.new_adapter()
            await self.iterate()
            self.clock.current = self.adapter.last_wall - rollback
            await self.adapter.tick()
            count = len(self.calls)
            self.clock.advance(86400.)
            await self.adapter.tick()
            self.assertEqual(len(self.calls),count)
            self.assertEqual(self.adapter.status()['phase'],'CLOCK_INVALID')

    async def test_restart_existing_slot_is_not_recaptured(self):
        self.clock.current = datetime.fromisoformat('2026-10-30T10:12:05+08:00')
        await self.adapter.tick()
        self.adapter = self.new_adapter()
        # A restarted loop at an already-recorded grid tick waits to the next.
        self.clock.current = datetime.fromisoformat('2026-10-30T10:12:05+08:00')
        await self.iterate()
        self.assertEqual(len(self.calls),1)
        self.assertEqual(self.adapter.status()['phase'],'WAITING_NEXT_SLOT')

    async def test_calibration_finalize_after_close_and_errors_preserved(self):
        self.config['calibrationEnabled'] = True
        self.clock.current = datetime.fromisoformat('2026-10-30T15:04:05+08:00')
        await self.iterate(); await self.iterate()
        self.assertEqual(len(self.calibration_calls),2)
        self.assertTrue(self.latest['calibration']['finalized'])
        self.assertEqual(self.latest['calibration']['status'],'PASS')
        self.mocks[4].side_effect = ValueError('SECRET calibration failed')
        self.clock.current = datetime.fromisoformat('2026-10-30T15:06:05+08:00')
        await self.iterate()
        self.assertEqual(self.latest['calibration']['status'],'ERROR')
        self.assertIn('*** calibration failed',self.markers[-1][-1])

    async def test_disabled_and_errors_keep_worker_and_alert_semantics(self):
        self.config['enabled'] = False
        await self.iterate()
        self.assertEqual(self.markers[-1],('DISABLED',))
        self.assertFalse(self.calls)
        self.config['enabled'] = True
        self.config['providerPath'] = None
        self.clock.current = datetime.fromisoformat('2026-10-30T10:12:05+08:00')
        await self.iterate()
        self.assertIn('QLIB_DATA_DIR',self.markers[-1][-1])
        self.assertIn(('capture.failure',None),self.alerts)

    async def test_config_and_implementation_drift_failclosed(self):
        self.clock.current = datetime.fromisoformat('2026-10-30T10:12:05+08:00')
        self.config['pollSeconds'] = 30
        await self.iterate()
        self.assertIn('POLL_DRIFT',self.adapter.error)
        self.config['pollSeconds'] = 60
        real = aligned.capture_scheduler_implementation_contract
        with patch.object(aligned,'capture_scheduler_implementation_contract',side_effect=RuntimeError('CAPTURE_SCHEDULER_IMPLEMENTATION_DRIFT')):
            await self.iterate()
        self.assertIn('IMPLEMENTATION_DRIFT',self.adapter.error)
        self.assertFalse(self.calls)

    async def test_cancellation_does_not_start_a_second_cycle(self):
        self.clock.current = datetime.fromisoformat('2026-10-30T10:12:05+08:00')
        async def cancel_sleep(delay):
            raise asyncio.CancelledError()
        self.adapter.sleep = cancel_sleep
        with self.assertRaises(asyncio.CancelledError):
            await self.adapter.loop()
        self.assertEqual(len(self.calls),1)

    async def test_contract_and_install_are_process_bound_and_idempotent(self):
        contract = aligned.capture_scheduler_implementation_contract()
        self.assertEqual(contract['scheduler']['phaseSeconds'],5)
        self.assertEqual(len(contract['sha256']),64)
        self.assertTrue(any(row['path'].endswith('slot_aligned_capture_runtime.py') for row in contract['files']))
        adapter = aligned.install_slot_aligned_capture_runtime(self.backend)
        self.assertIs(aligned.install_slot_aligned_capture_runtime(self.backend),adapter)
        self.assertEqual(self.backend._intraday_capture_loop,adapter.loop)
        with patch.object(aligned,'_sha',return_value='0'*64):
            with self.assertRaisesRegex(RuntimeError,'IMPLEMENTATION_DRIFT'):
                aligned.capture_scheduler_implementation_contract()



    async def test_cross_date_preflight_or_executor_queue_never_dispatches(self):
        for location in ('preflight','executor'):
            with self.subTest(location=location):
                self.clock.current = datetime.fromisoformat('2026-10-30T23:59:59+08:00')
                self.records.clear(); self.calls.clear(); self.adapter = self.new_adapter()
                self.adapter.next_due = datetime.fromisoformat('2026-10-30T23:59:05+08:00')
                self.config['calibrationEnabled'] = True
                if location == 'preflight':
                    def delayed_provider(config):
                        self.clock.advance(7)
                        return config['providerPath']
                    self.backend._intraday_capture_effective_provider_path.side_effect = delayed_provider
                else:
                    self.backend._intraday_capture_effective_provider_path.side_effect = lambda config:config['providerPath']
                    async def delayed_executor(function,*args,**kwargs):
                        if getattr(function,'__name__','') == '_guarded_call':
                            self.clock.advance(7)
                        return function(*args,**kwargs)
                    self.adapter.run_sync = delayed_executor
                await self.adapter.tick()
                self.assertEqual(self.adapter.status()['phase'],'CALIBRATION_DATE_ENDED')
                self.assertFalse(self.calls)
                self.mocks[4].assert_not_called()
                self.mocks[5].assert_not_called()

    async def test_date_change_inside_frozen_operation_is_degraded_not_rewritten(self):
        self.clock.current = datetime.fromisoformat('2026-10-30T23:59:55+08:00')
        self.adapter.next_due = datetime.fromisoformat('2026-10-30T23:59:05+08:00')
        self.config['calibrationEnabled'] = True
        def already_inflight(*args,**kwargs):
            self.clock.advance(10)
            return {'status':'CAPTURED'}
        self.backend._intraday_capture_runtime_binding.return_value = (already_inflight,{})
        await self.adapter.tick()
        self.assertIn('DATE_CHANGED_DURING_CYCLE',self.adapter.error)
        self.mocks[4].assert_not_called()
        self.mocks[5].assert_not_called()


if __name__ == '__main__':
    unittest.main()
