"""No network: realistic float32 Providers and retained API-return fixtures."""
from __future__ import annotations
from datetime import datetime
import importlib.util
import json
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch
import numpy as np
import pandas as pd
from tests.test_intraday_capture import CHINA_TZ
from market_data import benchmark_daily_repair as repair
from market_data import investment_data_daily_extension as extension

NOW = datetime(2026, 10, 23, 12, tzinfo=CHINA_TZ)


class RepairTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.base, self.output, self.artifacts = (self.root / name for name in ('base', 'output', 'source'))
        self.base.mkdir()
        self.artifacts.mkdir()
        self.clock = patch.object(repair, 'now', return_value=NOW)
        self.clock.start(); self.addCleanup(self.clock.stop)
        self.days = pd.bdate_range(end='2026-10-22', periods=40).strftime('%Y-%m-%d').tolist()
        for name in ('calendars', 'instruments', 'features/sh000300'):
            (self.base / name).mkdir(parents=True)
        (self.base / 'calendars/day.txt').write_text('\n'.join(self.days) + '\n')
        lines = ''.join(f'SH{i:06d}\t{self.days[0]}\t{self.days[-1]}\n' for i in range(600000, 600300))
        (self.base / 'instruments/csi300.txt').write_text(lines)
        (self.base / 'instruments/all.txt').write_text(lines)
        (self.base / repair.MANIFEST).write_text(json.dumps({'kind': 'official_index_membership_overlay',
                                                          'membership': {'asOfDate': self.days[-1]}}))
        self.raw = []
        columns = {field: [] for field in extension.FIELDS}
        for index, day in enumerate(self.days):
            close = 100.0 + index
            record = {'datetime': day, 'open': close, 'high': close + 1, 'low': close - 1,
                      'close': close, 'vol': 100, 'amount': 1_000_000.0}
            self.raw.append(record)
            values = dict(open=close, high=close + 1, low=close - 1, close=close,
                          volume=100.0, amount=1000.0, vwap=100.0, factor=1.0,
                          adjclose=close, change=0.0 if not index else close/(close-1)-1)
            for field in columns:
                columns[field].append(values[field] if index < 39 else np.nan)
        for field, values in columns.items():
            (self.base / f'features/sh000300/{field}.day.bin').write_bytes(np.array([0] + values, dtype='<f4').tobytes())
        # Runtime fingerprint covers all ten fields for all 300 stocks.
        for index in range(600000, 600300):
            directory = self.base / f'features/sh{index:06d}'
            directory.mkdir()
            for field in extension.FIELDS:
                (directory / f'{field}.day.bin').write_bytes(np.array([0] + [1] * 40, dtype='<f4').tobytes())
        self.payloads = {}
        for host in repair.HOSTS:
            for kind in ('raw', 'qfq'):
                self.payloads[f'{host}_{kind}'] = {
                    'host': host, 'adjustment': 'NONE' if kind == 'raw' else 'QFQ',
                    'requestedAt': NOW.isoformat(), 'receivedAt': NOW.isoformat(),
                    'fetchCalls': ['NONE'] if kind == 'raw' else ['NONE', 'QFQ'],
                    'syntheticAllowed': False, 'records': [dict(row) for row in self.raw]}
        self.payloads['tencent'] = {'requestedAt': NOW.isoformat(), 'receivedAt': NOW.isoformat(),
            'records': [dict(date=row['datetime'], open=row['open'], high=row['high'], low=row['low'],
                             close=row['close'], volume=row['vol']*100, amount=row['amount']) for row in self.raw]}
        self.write_sources()

    def write_sources(self):
        artifacts = {key: repair._store(self.artifacts, key + '.json', value) for key, value in self.payloads.items()}
        repair._store(self.artifacts, 'observations.json', {
            'schemaVersion': 1, 'kind': 'fresh_benchmark_repair_observations',
            'startedAt': NOW.isoformat(), 'completedAt': NOW.isoformat(),
            'window': self.days, 'implementation': repair.implementation_contract(), 'artifacts': artifacts})

    def test_audit_and_promote_exact_cells_runtime_contract(self):
        before = repair.tree(self.base)
        originals = {field: (self.base / f'features/sh000300/{field}.day.bin').read_bytes() for field in extension.FIELDS}
        result = repair.build_repaired_provider(self.base, self.output, self.artifacts)
        self.assertEqual(result['status'], 'PASS')
        self.assertEqual(repair.tree(self.base), before)
        self.assertEqual(result['contract']['dailyFeatureContract']['files'], 3010)
        for field in extension.FIELDS:
            updated = (self.output / f'features/sh000300/{field}.day.bin').read_bytes()
            self.assertEqual(updated[:-4], originals[field][:-4])
            self.assertTrue(np.isfinite(struct.unpack('<f', updated[-4:])[0]))
        manifest = json.loads((self.output / repair.MANIFEST).read_text())
        self.assertEqual(manifest['repairType'], 'benchmark_missing_session_repair')
        self.assertEqual(len(manifest['cellEdits']), 10)
        self.assertEqual((self.output / repair.INHERITED).read_bytes(), (self.base / repair.MANIFEST).read_bytes())
        changed = {name for name, value in before.items() if repair.sha(self.output / name) != value}
        self.assertEqual(changed, set(manifest['cellEdits']) | {repair.MANIFEST})
        self.assertFalse(result['profitEvidenceEligible'])

    def test_tail_missing_is_extended_only_one_cell(self):
        for field in extension.FIELDS:
            path = self.base / f'features/sh000300/{field}.day.bin'
            path.write_bytes(path.read_bytes()[:-4])
        before = (self.base / 'features/sh000300/close.day.bin').read_bytes()
        repair.build_repaired_provider(self.base, self.output, self.artifacts)
        updated = (self.output / 'features/sh000300/close.day.bin').read_bytes()
        self.assertEqual(updated[:-4], before)

    def test_source_disagreement_missing_nan_duplicate_and_units_fail(self):
        key = f'{repair.HOSTS[0]}_qfq'
        original = json.loads(json.dumps(self.payloads))
        changes = [lambda p: p[key]['records'][-1].update(close=140),
                   lambda p: p[key]['records'].pop(),
                   lambda p: p[key]['records'][-1].update(close=None),
                   lambda p: p[key]['records'].append(p[key]['records'][0]),
                   lambda p: p['tencent']['records'][-1].update(volume=100)]
        for change in changes:
            self.payloads = json.loads(json.dumps(original)); change(self.payloads); self.write_sources()
            with self.assertRaises(ValueError):
                repair.build_repaired_provider(self.base, self.output, self.artifacts)
            self.assertFalse(self.output.exists())

    def test_current_session_is_never_used(self):
        for key, payload in self.payloads.items():
            row = dict(payload['records'][-1])
            row['date' if key == 'tencent' else 'datetime'] = '2026-10-23'
            row['close'] = 9999999
            payload['records'].append(row)
        self.write_sources()
        audit = repair.audit_observations(self.base, self.artifacts)
        self.assertEqual(audit['targetDate'], '2026-10-22')
        self.assertEqual(audit['row']['close'], 139)
        with patch.object(repair, 'now', return_value=datetime(2026,10,22,18,tzinfo=CHINA_TZ)):
            with self.assertRaisesRegex(ValueError, 'strictly earlier'):
                repair.audit_observations(self.base, self.artifacts)

    def test_finite_inf_and_missing_historical_cell_rejected(self):
        path = self.base / 'features/sh000300/close.day.bin'
        original = path.read_bytes()
        for value in (1.0, float('inf')):
            path.write_bytes(original[:-4] + struct.pack('<f', value))
            with self.assertRaises(ValueError):
                repair.build_repaired_provider(self.base, self.output, self.artifacts)
        path.write_bytes(original[:8] + struct.pack('<f', float('nan')) + original[12:])
        with self.assertRaisesRegex(ValueError, '39 historical'):
            repair.audit_observations(self.base, self.artifacts)

    def test_original_audit_gate_not_relaxed(self):
        path = self.base / 'features/sh000300/change.day.bin'
        data = bytearray(path.read_bytes())
        data[4*20:4*21] = struct.pack('<f', .123)
        path.write_bytes(data)
        with self.assertRaisesRegex(ValueError, 'Original benchmark mapping audit failed'):
            repair.build_repaired_provider(self.base, self.output, self.artifacts)
        self.assertFalse(self.output.exists())

    def test_artifact_and_implementation_hash_drift(self):
        path = self.artifacts / f'{repair.HOSTS[0]}_raw.json'
        path.write_text(path.read_text() + ' ')
        with self.assertRaisesRegex(ValueError, 'hash/path'):
            repair.audit_observations(self.base, self.artifacts)
        self.write_sources()
        contract = repair.implementation_contract()
        contract['version'] = 'wrong'
        with patch.object(repair, 'implementation_contract', return_value=contract):
            with self.assertRaisesRegex(ValueError, 'implementation/window'):
                repair.audit_observations(self.base, self.artifacts)

    def test_no_synthetic_fallback_sequence(self):
        client = repair.ServerOnlyMacClient(repair.HOSTS[0])
        with patch.object(repair.MacClient, '_fetch_kline_pages', return_value=[]):
            for fq in (repair.Adjust.NONE, repair.Adjust.QFQ):
                client._fetch_kline_pages(1, '000300', repair.Period.DAILY, 0, 50, 1, fq)
            with self.assertRaisesRegex(ValueError, 'fallback rejected'):
                client._fetch_kline_pages(1, '000300', repair.Period.DAILY, 0, 50, 1, repair.Adjust.NONE)
        with self.assertRaisesRegex(ValueError, 'recomputation'):
            client._local_recompute_qfq(None, 1, '000300')

    def test_nonempty_output_and_links_rejected(self):
        self.output.mkdir()
        with self.assertRaises(ValueError):
            repair.build_repaired_provider(self.base, self.output, self.artifacts)
        self.output.rmdir()
        (self.base / 'escape').symlink_to(self.artifacts)
        with self.assertRaisesRegex(ValueError, 'symbolic'):
            repair.build_repaired_provider(self.base, self.output, self.artifacts)


    def test_sdk_actual_bad_qfq_public_api_fallback_is_rejected(self):
        from easy_tdx.mac.models import MacBar
        bar = MacBar(datetime=NOW.replace(tzinfo=None), open=-1, high=-1, low=-1,
                     close=-1, vol=1, amount=1)
        client = repair.ServerOnlyMacClient(repair.HOSTS[0])
        with patch.object(repair.MacClient, '_fetch_kline_pages', return_value=[bar]):
            client.get_stock_kline(1, '000300', period=repair.Period.DAILY,
                                   count=50, adjust=repair.Adjust.NONE)
            with self.assertRaisesRegex(ValueError, 'fallback rejected'):
                client.get_stock_kline(1, '000300', period=repair.Period.DAILY,
                                       count=50, adjust=repair.Adjust.QFQ)
        self.assertEqual(client.fetch_calls, ['NONE', 'QFQ'])

    def test_cli_default_does_not_promote(self):
        from contextlib import redirect_stdout
        import io
        spec = importlib.util.spec_from_file_location('repair_cli', Path(__file__).resolve().parents[1] / 'scripts/repair_benchmark_daily.py')
        cli = importlib.util.module_from_spec(spec); spec.loader.exec_module(cli)
        with patch.object(cli, 'collect_observations', return_value=self.artifacts) as collect, patch.object(cli, 'build_repaired_provider') as build, redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(['--base-provider', str(self.base), '--artifact-root', str(self.artifacts)]), 0)
            collect.assert_called_once(); build.assert_not_called()
        with redirect_stdout(io.StringIO()), patch('sys.stderr', new_callable=io.StringIO):
            with self.assertRaises(SystemExit):
                cli.main(['--base-provider', str(self.base), '--artifact-root', str(self.artifacts), '--output-provider', str(self.output)])

    def test_evidence_cannot_be_collected_inside_base(self):
        with self.assertRaisesRegex(ValueError, 'outside the Provider'):
            repair.collect_observations(self.base, self.base / 'evidence')
        self.assertFalse((self.base / 'evidence').exists())

    def test_feature_runtime_and_stock_completeness(self):
        repair.build_repaired_provider(self.base, self.output, self.artifacts)
        path = self.output / 'features/sh600000/close.day.bin'
        path.write_bytes(path.read_bytes()[:-4] + struct.pack('<f', 999))
        with self.assertRaisesRegex(RuntimeError, 'feature tree'):
            repair.provider_contract_fingerprint(self.output)



    def test_fresh_collection_real_sdk_and_tencent_parsers_without_network(self):
        from easy_tdx.mac.models import MacBar
        from unittest.mock import Mock
        bars = [MacBar(datetime=datetime.fromisoformat(row['datetime']),
                       open=row['open'], high=row['high'], low=row['low'], close=row['close'],
                       vol=row['vol'], amount=row['amount']) for row in self.raw]
        rows = [[row['datetime'], str(row['open']), str(row['close']), str(row['high']), str(row['low']),
                 row['vol'], {}, 0, row['amount']/10000] for row in self.raw]
        response = Mock(text='kline_day=' + json.dumps({'data': {'sh000300': {'day': rows}}}),
                        url='https://web.ifzq.gtimg.cn/appstock/app/kline/mkline', status_code=200)
        destination = self.root / 'fresh'
        with patch.object(repair.MacClient, '__enter__', lambda client: client), patch.object(repair.MacClient, '__exit__', return_value=False), patch.object(repair.MacClient, '_execute', return_value=bars) as execute, patch('requests.Session.get', return_value=response) as get:
            repair.collect_observations(self.base, destination)
        self.assertEqual(execute.call_count, 4)
        self.assertEqual(get.call_count, 1)
        self.assertEqual(repair.audit_observations(self.base, destination)['status'], 'PASS')
        tencent = json.loads((destination/'tencent.json').read_text())
        self.assertEqual(tencent['http'][0]['body'], response.text)
        self.assertEqual(json.loads((destination/f'{repair.HOSTS[0]}_qfq.json').read_text())['fetchCalls'], ['NONE', 'QFQ'])

    def test_validation_failure_after_copy_publishes_nothing(self):
        path = self.base / 'features/sh600000/close.day.bin'
        path.write_bytes(path.read_bytes()[:-4] + struct.pack('<f', float('nan')))
        before = repair.tree(self.base)
        with self.assertRaisesRegex(ValueError, 'stock features must be complete'):
            repair.build_repaired_provider(self.base, self.output, self.artifacts)
        self.assertFalse(self.output.exists())
        self.assertFalse(list(self.root.glob('.output.building-*')))
        self.assertEqual(repair.tree(self.base), before)



    def test_cli_limits_success_output_and_sanitizes_failure(self):
        from contextlib import redirect_stdout
        import io
        spec = importlib.util.spec_from_file_location('repair_cli_compact', Path(__file__).resolve().parents[1] / 'scripts/repair_benchmark_daily.py')
        cli = importlib.util.module_from_spec(spec); spec.loader.exec_module(cli)
        args = ['--base-provider', str(self.base), '--artifact-root', str(self.artifacts),
                '--promote', '--output-provider', str(self.output)]
        output = io.StringIO()
        with patch.object(cli, 'collect_observations', return_value=self.artifacts), patch.object(cli, 'audit_observations', return_value={'status':'PASS', 'targetDate':self.days[-1]}), patch.object(cli, 'build_repaired_provider', return_value={'provider':str(self.output), 'contract':{'fingerprint':'abc', 'large':'PRIVATE_BODY'*10000}}), redirect_stdout(output):
            self.assertEqual(cli.main(args), 0)
        self.assertLess(len(output.getvalue()), 1000)
        self.assertNotIn('PRIVATE_BODY', output.getvalue())
        self.assertEqual(json.loads(output.getvalue())['fingerprint'], 'abc')
        output = io.StringIO()
        with patch.object(cli, 'collect_observations', side_effect=ValueError('PRIVATE_HTTP_BODY')), redirect_stdout(output):
            self.assertEqual(cli.main(args), 1)
        self.assertNotIn('PRIVATE_HTTP_BODY', output.getvalue())
        self.assertEqual(json.loads(output.getvalue())['error'], 'BENCHMARK_REPAIR_REJECTED')



    def test_day_factor_spread_gate_rejects_when_other_metrics_pass(self):
        path = self.base / 'features/sh000300/factor.day.bin'
        data = np.frombuffer(path.read_bytes(), dtype='<f4').copy()
        # Each factor remains within 2e-6 of close/raw_close. An outlier's
        # spread from the median exceeds 2e-6: only the original day gate fails.
        data[1:40] = 1 + 1.5e-6
        data[2] = 1 - 1.5e-6
        path.write_bytes(data.tobytes())
        observed = []
        original = extension.audit_symbol_mapping
        def record(*args, **kwargs):
            result = original(*args, **kwargs)
            observed.append(result[0])
            return result
        with patch.object(extension, 'audit_symbol_mapping', side_effect=record):
            with self.assertRaisesRegex(ValueError, 'Original benchmark mapping audit failed'):
                repair.build_repaired_provider(self.base, self.output, self.artifacts)
        self.assertEqual(len(observed), 1)
        audit = observed[0]
        self.assertEqual(audit['adjustmentKey'], 'day')
        self.assertEqual(audit['metrics']['dayIdentityRelativeError']['count'], 156)
        self.assertLess(audit['metrics']['factorRelativeError']['maximum'], 2e-6)
        self.assertGreater(audit['dayFactorRelativeSpread'], 2e-6)
        self.assertEqual(audit['failures'], ['day payload has a non-constant base factor'])
        self.assertFalse(self.output.exists())

    def test_day_identity_failure_never_promotes(self):
        for host in repair.HOSTS:
            self.payloads[f'{host}_qfq']['records'][10]['close'] += .001
        self.write_sources()
        with self.assertRaisesRegex(ValueError, 'raw/QFQ closed values disagree'):
            repair.build_repaired_provider(self.base, self.output, self.artifacts)
        self.assertFalse(self.output.exists())
        # Independently prove the unchanged original helper evaluates its day
        # identity branch and refuses the same candidate's nonidentity.
        raw = repair._frame(self.payloads[f'{repair.HOSTS[0]}_raw'], self.days, True)
        adjusted = repair._frame(self.payloads[f'{repair.HOSTS[0]}_qfq'], self.days, True)
        audit, _ = extension.audit_symbol_mapping(self.base, repair.CODE, raw, adjusted,
                                                  self.days, self.days, self.days[-2])
        self.assertEqual(audit['adjustmentKey'], 'day')
        self.assertEqual(audit['metrics']['dayIdentityRelativeError']['count'], 156)
        self.assertIn('day payload is not identical to raw prices', audit['failures'])
        self.assertEqual(audit['status'], 'FAIL')



    def test_tencent_string_ohlc_normalized_to_float64_and_promotes(self):
        for row in self.payloads['tencent']['records']:
            for field in repair.SOURCE_FIELDS:
                row[field] = str(row[field])
        self.write_sources()
        frame = repair._frame(self.payloads['tencent'], self.days)
        for field in repair.SOURCE_FIELDS:
            self.assertEqual(str(frame[field].dtype), 'float64')
        self.assertEqual(repair.audit_observations(self.base, self.artifacts)['status'], 'PASS')
        result = repair.build_repaired_provider(self.base, self.output, self.artifacts)
        self.assertEqual(result['status'], 'PASS')

    def test_tencent_bad_numeric_string_rejects_without_promotion(self):
        before = repair.tree(self.base)
        self.payloads['tencent']['records'][15]['close'] = 'not-a-number'
        self.write_sources()
        with self.assertRaises(ValueError):
            repair.build_repaired_provider(self.base, self.output, self.artifacts)
        self.assertFalse(self.output.exists())
        self.assertEqual(repair.tree(self.base), before)


if __name__ == '__main__':
    unittest.main()
