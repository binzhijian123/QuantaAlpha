"""Independent V10 entrypoint tests: no network, real ledger or project .env IO."""
from __future__ import annotations
import asyncio
from contextlib import asynccontextmanager, contextmanager
from datetime import timedelta
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

BACKEND = Path(__file__).resolve().parents[1] / 'frontend-v2/backend'
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))
import v10_runtime as runtime


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        for relative in runtime.REQUIRED_RUNTIME_FILES:
            destination = self.root / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            source = BACKEND.parents[1] / relative
            destination.write_bytes(source.read_bytes() if source.exists() else b'# maintenance fixture\n')
        self.selected = self.root / 'configs/forward_v10.json'
        self.provider = self.root / 'provider'; self.provider.mkdir()
        self.capture = self.root / 'capture'
        self.declaration = {'runId': 'unit_forward_v10', 'runtime': dict(runtime.RUNTIME_CONTRACT),
            'captureContract': {'pollSeconds':60}, 'captureRoot':str(self.capture),
            'databasePath':str(self.root/'run/forward.sqlite3'),
            'implementation': {'files':[{'path': relative, 'sha256':runtime._sha(self.root/relative)}
                                       for relative in runtime.REQUIRED_RUNTIME_FILES]}}
        self.values = {}
        self.tasks, self.worker_state = {}, {}
        self.route_called = Mock()
        self.app = FastAPI()
        for path, method in (('/api/v1/research-shadow/state','GET'),
                            ('/api/v1/research-shadow/run','POST'),
                            ('/api/v1/paper-trading/account','GET'),
                            ('/api/v1/paper-trading/realtime','GET'),
                            ('/api/v1/paper-trading/signals','POST'),
                            ('/api/v1/paper-trading/account/reset','POST')):
            async def old_engine():
                self.route_called()
                raise AssertionError('old engine route must not execute')
            self.app.add_api_route(path, old_engine, methods=[method])
        self.saved_requests = []
        self.saved_lock_depths = []
        @self.app.put('/api/v1/system/config')
        async def save_config(request: Request):
            self.saved_requests.append(await request.json())
            self.saved_lock_depths.append(self.lock_depth)
            return {'success': True}
        self.backend = SimpleNamespace(PROJECT_ROOT=self.root, app=self.app,
            _runtime_readiness_payload=lambda **kwargs:{'pid':1234,'instanceId':'unit-instance',
                'startedAt':'unit-started','status':'healthy','ready':True,'workers':{}},
            _load_dotenv_dict=lambda:dict(self.values),
            INTRADAY_CAPTURE_WORKER='capture_worker', INTRADAY_SHADOW_WORKER='shadow_worker',
            _BACKGROUND_TASKS=self.tasks,
            _background_worker_readiness=self.readiness,
            _register_background_worker=self.register,
            _mark_background_worker_disabled=lambda name:self.mark(name, 'DISABLED'),
            _mark_background_worker_running=lambda name, maximum:self.mark(name, 'RUNNING'),
            _mark_background_worker_idle=lambda name, poll, error=None:self.mark(name,'DEGRADED' if error else 'IDLE'),
            _mark_background_worker_stopped=lambda name:self.mark(name,'STOPPED'),
            _intraday_capture_loop=self.capture_loop, _intraday_shadow_loop=self.shadow_loop)
        @self.app.get('/api/health/ready')
        async def old_readiness():
            from fastapi.responses import JSONResponse
            payload = self.backend._runtime_readiness_payload()
            return JSONResponse(status_code=200 if payload['ready'] else 503, content=payload)
        self.lock_depth = 0
        self.maintenance_status = None
        self.maintenance = SimpleNamespace(runtime_lock=self.lock,
            resolve_current_provider=Mock(side_effect=lambda path:self.provider),
            read_status=lambda path:self.maintenance_status, run_cycle=self.maintenance_cycle,
            bootstrap_root_provider=Mock(return_value={'status':'ROOT_SELECTED'}))
        self.loader = patch.object(runtime, '_maintenance', return_value=self.maintenance)
        self.maintenance_mock = self.loader.start(); self.addCleanup(self.loader.stop)
        self.validator = patch.object(runtime, 'validate_v10_declaration', side_effect=self.validate)
        self.validation_mock = self.validator.start(); self.addCleanup(self.validator.stop)

    def validate(self, path):
        if Path(path) != self.selected or not path.exists():
            raise ValueError('SECRET raw declaration error')
        return {'declaration': self.declaration,
                'declarationSha256':hashlib.sha256(json.dumps(self.declaration,sort_keys=True).encode()).hexdigest(),
                'captureImplementation': {'runtime':{'providerClass':'ResponseTimedRealtimeClient'}}}

    def configure(self):
        self.selected.parent.mkdir(parents=True,exist_ok=True)
        self.selected.write_text('{}')
        self.values[runtime.DECLARATION_KEY] = str(self.selected)
        return runtime.install_v10_runtime(self.backend)

    @contextmanager
    def lock(self, path):
        self.assertEqual(self.lock_depth,0)
        self.lock_depth += 1
        try:
            yield
        finally:
            self.lock_depth -= 1

    def mark(self, name, state):
        self.worker_state[name] = state

    def register(self,name,task):
        self.tasks[name] = task; self.mark(name,'STARTING')

    def readiness(self,name,*,enabled,now_monotonic):
        task = self.tasks.get(name)
        return {'enabled':enabled, 'taskAlive':bool(task and not task.done()),
                'ready':self.worker_state.get(name) not in {'DEGRADED','STOPPED'},
                'status':self.worker_state.get(name,'STARTING'),
                'lastHeartbeatAt':runtime._now().isoformat(), 'consecutiveErrors':0}

    async def capture_loop(self):
        self.mark('capture_worker','IDLE')
        await asyncio.Event().wait()

    async def shadow_loop(self):
        self.mark('shadow_worker','IDLE')
        await asyncio.Event().wait()

    def maintenance_cycle(self,path,**kwargs):
        self.assertEqual(self.lock_depth,0) # run_cycle itself owns the lock
        validated = self.validate(path)
        self.maintenance_status = {'runId':self.declaration['runId'],
            'declarationSha256':validated['declarationSha256'], 'checkedAt':runtime._now().isoformat(),
            'status':'NO_NEW_SESSION','runtimeConsistent':True}
        return self.maintenance_status

    def test_no_optin_ignores_all_legacy_flags_and_does_no_maintenance_io(self):
        self.values.update(QUANTAALPHA_INTRADAY_CAPTURE_ENABLED='true',
                           QUANTAALPHA_INTRADAY_SHADOW_ENABLED='true',QUANTAALPHA_RESEARCH_SHADOW_ENABLED='true')
        adapter = runtime.install_v10_runtime(self.backend)
        with TestClient(self.app) as client:
            status = client.get('/api/v1/v10-runtime/status')
            self.assertEqual(status.status_code,200)
            data = status.json()['data']
            self.assertFalse(data['configured']); self.assertFalse(data['captureEnabled'])
            self.assertFalse(data['shadowEnabled']); self.assertFalse(data['autoShadowAccepted'])
            self.assertEqual(data['stage'],'DISABLED')
            self.assertEqual(client.get('/api/v1/v10-runtime/health').status_code,200)
            self.assertEqual(client.get('/api/v1/v10-runtime/ready').status_code,503)
            self.assertFalse(self.tasks)
        self.assertFalse(self.backend._research_shadow_auto_config()['enabled'])
        self.assertFalse(self.backend._intraday_capture_config()['enabled'])
        self.assertFalse(self.backend._intraday_shadow_config()['enabled'])
        self.maintenance_mock.assert_not_called(); self.validation_mock.assert_not_called()
        self.assertFalse(self.capture.exists())

    def test_invalid_unknown_declaration_503_without_fallback(self):
        self.values[runtime.DECLARATION_KEY] = str(self.selected)
        adapter = runtime.install_v10_runtime(self.backend)
        with TestClient(self.app) as client:
            response = client.get('/api/v1/v10-runtime/status')
            self.assertEqual(response.status_code,503)
            self.assertNotIn('SECRET',response.text)
            self.assertFalse(response.json()['data']['captureEnabled'])
            self.assertFalse(self.tasks)
            self.assertEqual(client.get('/api/v1/shadow-recovery/status').json()['data']['mode'],'FORWARD_RUNTIME')
        self.assertFalse(self.backend._intraday_capture_config()['enabled'])
        self.assertFalse(self.capture.exists())

    def test_old_read_and_write_routes_are_blocked_before_construction(self):
        runtime.install_v10_runtime(self.backend)
        with TestClient(self.app) as client:
            for method,path in (('get','/api/v1/research-shadow/state'),('post','/api/v1/research-shadow/run'),
                                ('get','/api/v1/paper-trading/account'),('get','/api/v1/paper-trading/realtime'),
                                ('post','/api/v1/paper-trading/signals'),('post','/api/v1/paper-trading/account/reset')):
                self.assertEqual(getattr(client,method)(path).status_code,409)
        self.route_called.assert_not_called()

    def test_provider_config_guard_also_applies_to_invalid_configuration(self):
        self.values[runtime.DECLARATION_KEY] = str(self.selected)
        runtime.install_v10_runtime(self.backend)
        with TestClient(self.app) as client:
            for key in ('QLIB_DATA_DIR','QLIB_PROVIDER_URI'):
                self.assertEqual(client.put('/api/v1/system/config',json={key:str(self.provider)}).status_code,409)
            self.assertEqual(client.put('/api/v1/system/config',json={'CHAT_MODEL':'custom'}).status_code,503)
            self.assertFalse(self.saved_requests)

    def test_contract_and_process_hash_pin_failclosed(self):
        adapter = self.configure()
        self.assertIsNotNone(adapter.validate())
        self.declaration['runtime']['capturePollSeconds'] = 30
        self.assertIsNone(adapter.validate())
        self.declaration['runtime']['capturePollSeconds'] = 60
        self.declaration['runId'] = 'changed_v10'
        self.assertIsNone(adapter.validate())
        self.values[runtime.DECLARATION_KEY] = str(self.root/'other.json')
        self.assertIsNone(adapter.validate())
        self.assertEqual(adapter.error,'V10_DECLARATION_SELECTION_CHANGED')

    def test_current_discovery_failure_disables_actions(self):
        adapter = self.configure()
        self.maintenance.resolve_current_provider.side_effect = ValueError('unaccepted latest provider')
        self.assertFalse(adapter.capture_config()['enabled'])
        self.assertFalse(adapter.shadow_config()['enabled'])
        with patch.object(runtime.capture_v4,'run_capture_cycle') as capture:
            with self.assertRaisesRegex(RuntimeError,'PROVIDER_UNVERIFIED'):
                adapter.capture_cycle(self.provider,self.capture)
            capture.assert_not_called()

    def test_health_includes_maintenance_and_lifecycle_cancels_all_workers(self):
        adapter = self.configure()
        with TestClient(self.app) as client:
            # Synchronize one request after the startup maintenance thread ran.
            for _ in range(20):
                result = client.get('/api/v1/v10-runtime/status').json()['data']
                if result['ready']:
                    break
            self.assertTrue(result['ready'])
            self.assertEqual(set(result['workers']),{'capture','shadow','maintenance'})
            self.assertTrue(all(worker['taskAlive'] for worker in result['workers'].values()))
            self.assertTrue(all('cycle' in worker for worker in result['workers'].values()))
            self.assertFalse(result['autoShadowAccepted'])
            self.assertEqual(result['runId'],'unit_forward_v10')
            response = client.get('/api/v1/v10-runtime/ready')
            self.assertEqual(response.status_code,200)
            self.assertEqual(response.json()['status'],'healthy')
            self.assertEqual(response.json()['pid'],1234)
            self.assertEqual(response.json()['instanceId'],'unit-instance')
            self.assertEqual(client.get('/api/health/ready').status_code,200)
            self.maintenance_status['status'] = 'BLOCKED'
            self.assertEqual(client.get('/api/v1/v10-runtime/ready').status_code,503)
            self.assertEqual(client.get('/api/health/ready').status_code,503)
        self.assertFalse(self.tasks)
        self.assertEqual(set(self.worker_state.values()),{'STOPPED'})

    def test_permanent_clock_fault_or_unverifiable_scheduler_is_not_healthy(self):
        self.configure()
        with TestClient(self.app) as client:
            for _ in range(20):
                if client.get('/api/v1/v10-runtime/status').json()['data']['ready']:
                    break
            self.assertEqual(client.get('/api/v1/v10-runtime/ready').status_code, 200)
            scheduler = self.backend._slot_aligned_capture_runtime
            scheduler.clock_fault, scheduler.phase = True, 'CLOCK_INVALID'
            response = client.get('/api/v1/v10-runtime/ready')
            self.assertEqual(response.status_code, 503)
            self.assertEqual(client.get('/api/health/ready').status_code, 503)
            state = client.get('/api/v1/v10-runtime/status').json()['data']
            self.assertFalse(state['workers']['capture']['ready'])
            self.assertEqual(state['workers']['capture']['reason'], 'V10_CAPTURE_SCHEDULER_UNVERIFIED')
            with patch.object(scheduler, 'status', side_effect=RuntimeError('SECRET scheduler detail')):
                response = client.get('/api/v1/v10-runtime/ready')
                self.assertEqual(response.status_code, 503)
                self.assertNotIn('SECRET', response.text)
            with patch.object(scheduler, 'status', return_value=None):
                self.assertEqual(client.get('/api/v1/v10-runtime/ready').status_code, 503)
            del self.backend._slot_aligned_capture_runtime
            self.assertEqual(client.get('/api/v1/v10-runtime/ready').status_code, 503)

    def test_cycles_hold_shared_lock_and_reject_stale_provider_selection(self):
        adapter = self.configure()
        self.maintenance_cycle(self.selected)
        def capture(*args,**kwargs):
            self.assertEqual(self.lock_depth,1)
            return {'status':'CAPTURED'}
        with patch.object(runtime.capture_v4,'run_capture_cycle',side_effect=capture) as call:
            self.assertEqual(adapter.capture_cycle(self.provider,self.capture,poll_seconds=60)['status'],'CAPTURED')
            with self.assertRaisesRegex(RuntimeError,'CAPTURE_PROVIDER_CHANGED'):
                adapter.capture_cycle(self.root/'stale',self.capture)
            self.assertEqual(call.call_count,1)
        from market_data import intraday_shadow_v10
        coordinator = Mock()
        coordinator.run_cycle.side_effect=lambda **kwargs:self.assertEqual(self.lock_depth,1) or {'status':'WAITING'}
        def construct(path):
            self.assertEqual(self.lock_depth,1)
            return coordinator
        with patch.object(intraday_shadow_v10,'IntradayShadowCoordinator',side_effect=construct):
            self.assertEqual(adapter.coordinator().run_cycle(poll_seconds=60)['status'],'WAITING')

    def test_stale_or_wrong_run_maintenance_result_is_not_ready(self):
        adapter = self.configure()
        self.maintenance_cycle(self.selected)
        validated = adapter.validate()
        self.maintenance_status['checkedAt'] = (adapter.started_at-timedelta(seconds=1)).isoformat()
        self.assertFalse(adapter._maintenance_state(validated)[1])
        self.maintenance_status['checkedAt'] = runtime._now().isoformat()
        self.maintenance_status['runId'] = 'old_v9'
        self.assertFalse(adapter._maintenance_state(validated)[1])

    def test_entrypoint_import_does_not_import_recovery_or_local_app(self):
        code = "import sys; import v10_app; assert 'local_app' not in sys.modules; assert 'shadow_recovery' not in sys.modules; print('V10_IMPORT_OK')"
        result = subprocess.run([sys.executable,'-c',code],cwd=BACKEND,text=True,capture_output=True,timeout=30)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertIn('V10_IMPORT_OK',result.stdout)



    def test_capture_cannot_run_before_maintenance_ready_or_with_custom_runtime(self):
        adapter = self.configure()
        with patch.object(runtime.capture_v4,'run_capture_cycle') as capture:
            with self.assertRaisesRegex(RuntimeError,'MAINTENANCE_NOT_READY'):
                adapter.capture_cycle(self.provider,self.capture)
            self.maintenance_cycle(self.selected)
            for options in ({'force':True},{'client':object()},{'clock':lambda:None},{'poll_seconds':30}):
                with self.assertRaisesRegex(RuntimeError,'RUNTIME_CONTRACT_CHANGED'):
                    adapter.capture_cycle(self.provider,self.capture,**options)
            capture.assert_not_called()

    def test_implementation_drift_disabled_binding_never_uses_old_implementations(self):
        adapter = self.configure()
        self.assertIsNotNone(adapter.validate())
        path = self.root/'frontend-v2/backend/system_settings.py'
        path.write_bytes(path.read_bytes()+b'\n# drift')
        self.assertFalse(adapter.capture_config()['enabled'])
        self.assertFalse(adapter.shadow_config()['enabled'])
        runner,binding = adapter.runtime_binding()
        self.assertFalse(binding['enabled'])
        self.assertEqual(runner(self.provider,self.capture)['status'],'DISABLED')
        self.assertFalse(self.capture.exists())

    def test_bootstrap_failure_blocks_ready_without_run_cycle(self):
        adapter = self.configure()
        self.maintenance.bootstrap_root_provider.return_value = {'status':'BLOCKED'}
        with patch.object(self.maintenance,'run_cycle') as cycle:
            with TestClient(self.app) as client:
                for _ in range(20):
                    response = client.get('/api/v1/v10-runtime/ready')
                    if self.worker_state.get(runtime.MAINTENANCE_WORKER) == 'DEGRADED':
                        break
                self.assertEqual(response.status_code,503)
                self.assertFalse(response.json()['data']['workers']['maintenance']['ready'])
            cycle.assert_not_called()
        self.assertFalse(self.tasks)

    def test_optin_added_after_start_requires_restart(self):
        adapter = runtime.install_v10_runtime(self.backend)
        self.values[runtime.DECLARATION_KEY] = str(self.selected)
        self.assertFalse(adapter.capture_config()['enabled'])
        self.assertEqual(adapter.error,'V10_DECLARATION_SELECTION_CHANGED')
        self.assertFalse(adapter.shadow_config()['enabled'])
        self.maintenance_mock.assert_not_called()



    def test_loaded_runtime_and_entrypoint_baselines_are_not_current_disk_only(self):
        adapter = self.configure()
        with patch.object(runtime, '_LOADED_RUNTIME_SHA256', '0'*64):
            self.assertIsNone(adapter.validate())
        adapter.entrypoint_sha256 = '0'*64
        self.assertIsNone(adapter.validate())
        self.assertEqual(adapter.error,'V10_DECLARATION_INVALID')



    def test_loaded_dependency_cannot_be_relabelled_before_first_validate(self):
        adapter = self.configure()
        relative = 'frontend-v2/backend/system_settings.py'
        path = self.root/relative
        path.write_bytes(path.read_bytes()+b'\n# replaced before first validation')
        for row in self.declaration['implementation']['files']:
            if row['path'] == relative:
                row['sha256'] = runtime._sha(path)
        self.assertIsNone(adapter.validate())
        self.assertEqual(adapter.error,'V10_DECLARATION_INVALID')

    def test_shutdown_callback_blocks_late_maintenance_commit(self):
        adapter = self.configure()
        self.assertTrue(adapter.should_continue())
        adapter.stopping = True
        self.assertFalse(adapter.should_continue())
        adapter.stopping = False
        self.values[runtime.DECLARATION_KEY] = 'changed.json'
        self.assertFalse(adapter.should_continue())



    def test_valid_model_config_save_uses_shared_lock_and_preserves_body(self):
        self.configure()
        with TestClient(self.app) as client:
            response = client.put('/api/v1/system/config',json={'CHAT_MODEL':'custom','OPENAI_API_KEY':'unit-placeholder'})
            self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(self.saved_requests[-1]['CHAT_MODEL'],'custom')
        self.assertEqual(self.saved_lock_depths[-1],1)
        self.assertEqual(self.lock_depth,0)

    def test_cancelled_lock_acquisition_eventually_releases(self):
        import threading
        released = threading.Event()
        acquired = threading.Event()
        exits = []
        @contextmanager
        def manager():
            acquired.set()
            released.wait(timeout=2)
            try:
                yield
            finally:
                exits.append(True)
        async def request():
            async with runtime._offthread_lock(manager()):
                raise AssertionError('cancelled request may not enter handler')
        async def scenario():
            task = asyncio.create_task(request())
            await asyncio.to_thread(acquired.wait,1)
            task.cancel()
            released.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
        asyncio.run(scenario())
        self.assertEqual(exits,[True])


if __name__ == '__main__':
    unittest.main()
