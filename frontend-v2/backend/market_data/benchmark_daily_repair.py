"""Closed-session, benchmark-only repair into a new immutable Provider.

Two TCP endpoints corroborate the same protocol, not independent upstreams.
This tool never changes the supplied Provider or accepts a precomputed PASS.
"""
from __future__ import annotations

from datetime import datetime
import hashlib
from importlib import metadata
import inspect
import json
import math
import os
from pathlib import Path
import shutil
import struct
import tempfile
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
from easy_tdx.mac.client import MacClient
from easy_tdx.mac.enums import Adjust, Period

from quantaalpha.utils.qlib_provider import provider_contract_fingerprint
from . import investment_data_daily_extension as extension
from .providers.a_stock_data import AStockDataClient

TZ = ZoneInfo('Asia/Shanghai')
HOSTS = ('123.60.47.136', '121.37.207.165')
CODE = '000300.SH'
MANIFEST = 'quantaalpha_extension.manifest.json'
INHERITED = 'benchmark_repair.inherited_membership.manifest.json'
SOURCE_FIELDS = ('open', 'high', 'low', 'close', 'volume', 'amount')
UNITS = {'volumeMultiplier': 100, 'amountMultiplier': 1,
         'scope': 'easy-tdx 1.20.6 MAC DAILY 000300.SH only',
         'qualification': 'fixed conversion corroborated against complete Tencent raw window',
         'priceAbsoluteTolerance': .01, 'volumeRelativeTolerance': 1e-4,
         'amountRelativeTolerance': 2e-6}


def now():
    return datetime.now(TZ)


def sha(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def json_bytes(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(',', ':'),
                      allow_nan=False).encode()


def tree(root):
    entries = {}
    for path in sorted(Path(root).rglob('*')):
        if path.is_symlink():
            raise ValueError('Provider symbolic links are forbidden')
        if path.is_file():
            if path.stat().st_nlink != 1:
                raise ValueError('Provider hard links are forbidden')
            entries[path.relative_to(root).as_posix()] = sha(path)
    return entries


class ServerOnlyMacClient(MacClient):
    """Retain the SDK's public API; prohibit every QFQ fallback path."""
    def __init__(self, host):
        super().__init__(host=host, port=7709, timeout=8, auto_reconnect=False,
                         heartbeat_interval=0)
        self.fetch_calls = []

    def _fetch_kline_pages(self, market, code, period, start, count, times, fq):
        expected = (Adjust.NONE, Adjust.QFQ)
        index = len(self.fetch_calls)
        if (index >= 2 or fq != expected[index] or market != 1 or code != '000300'
                or period != Period.DAILY or start != 0 or count != 50 or times != 1):
            raise ValueError('Unexpected TCP fetch or SDK QFQ fallback rejected')
        self.fetch_calls.append(fq.name)
        return super()._fetch_kline_pages(market, code, period, start, count, times, fq)

    def _local_recompute_qfq(self, *args, **kwargs):
        raise ValueError('Local QFQ recomputation is forbidden')


def implementation_contract():
    if metadata.version('easy-tdx') != '1.20.6':
        raise ValueError('easy-tdx must be exactly 1.20.6')
    import easy_tdx
    package = Path(easy_tdx.__file__).resolve().parent
    files = {str(path.resolve()): sha(path) for path in sorted(package.rglob('*.py'))}
    for module in (inspect.getmodule(AStockDataClient), extension,
                   inspect.getmodule(provider_contract_fingerprint)):
        path = Path(module.__file__).resolve()
        files[str(path)] = sha(path)
    files[str(Path(__file__).resolve())] = sha(__file__)
    cli = Path(__file__).resolve().parents[3] / 'scripts/repair_benchmark_daily.py'
    if cli.is_file():
        files[str(cli)] = sha(cli)
    dist = metadata.distribution('easy-tdx')
    for relative in dist.files or ():
        if str(relative).endswith(('/RECORD', '/METADATA')):
            path = Path(dist.locate_file(relative)).resolve()
            files[str(path)] = sha(path)
    result = {'package': 'easy-tdx', 'version': '1.20.6', 'files': files,
              'clientClass': f'{ServerOnlyMacClient.__module__}.{ServerOnlyMacClient.__name__}',
              'publicKlineSourceSha256': hashlib.sha256(inspect.getsource(MacClient.get_stock_kline).encode()).hexdigest(),
              'timezone': 'Asia/Shanghai', 'hosts': list(HOSTS), 'port': 7709,
              'request': {'market': 1, 'code': '000300', 'period': 'DAILY', 'count': 50,
                          'start': 0, 'times': 1, 'adjustments': ['NONE', 'QFQ']},
              'units': dict(UNITS), 'auditThresholds': dict(extension.AUDIT_THRESHOLDS)}
    result['sha256'] = hashlib.sha256(json_bytes(result)).hexdigest()
    return result


def closed_window(base):
    calendar = (Path(base) / 'calendars/day.txt').read_text().splitlines()
    if len(calendar) < 40 or calendar != sorted(set(calendar)):
        raise ValueError('Provider requires a sorted unique calendar with 40 sessions')
    if any(datetime.strptime(day, '%Y-%m-%d').date().isoformat() != day for day in calendar):
        raise ValueError('Invalid calendar date')
    if calendar[-1] >= now().astimezone(TZ).date().isoformat():
        raise ValueError('Repair session must be strictly earlier than today')
    return calendar, calendar[-40:]


def _store(root, name, payload):
    path = root / name
    path.write_bytes(json_bytes(payload))
    path.with_name(path.name + '.sha256').write_text(f'{sha(path)}  {path.name}\n')
    return {'file': path.name, 'sha256': sha(path)}


def collect_observations(base, artifact_root):
    """Fresh acquisition only. Raw API returns and Tencent HTTP bodies are retained."""
    _, window = closed_window(base)
    artifact_root = Path(artifact_root).absolute()
    resolved = artifact_root.resolve()
    base_path = Path(base).resolve()
    if (base_path == resolved or base_path in resolved.parents or resolved in base_path.parents
            or artifact_root.parent.resolve() != artifact_root.parent):
        raise ValueError('Observation directory must be outside the Provider on a canonical path')
    artifact_root.mkdir(parents=True, exist_ok=False)
    contract = implementation_contract()
    started = now().isoformat()
    artifacts = {}
    for host in HOSTS:
        with ServerOnlyMacClient(host) as client:
            for name, adjust in (('raw', Adjust.NONE), ('qfq', Adjust.QFQ)):
                requested = now().isoformat()
                frame = client.get_stock_kline(1, '000300', period=Period.DAILY,
                                              count=50, adjust=adjust)
                received = now().isoformat()
                # pandas preserves the SDK's datetime and binary-decoded numbers.
                records = json.loads(frame.to_json(orient='records', date_format='iso', double_precision=15))
                key = f'{host}_{name}'
                artifacts[key] = _store(artifact_root, key + '.json', {
                    'source': 'easy_tdx.mac.client.MacClient.get_stock_kline',
                    'host': host, 'adjustment': adjust.name, 'requestedAt': requested,
                    'receivedAt': received, 'records': records,
                    'fetchCalls': list(client.fetch_calls), 'syntheticAllowed': False})
            if client.fetch_calls != ['NONE', 'QFQ']:
                raise ValueError('Incomplete server fetch sequence')
    client = AStockDataClient(timeout=8)
    http = []
    original_get = client.session.get

    def record_get(*args, **kwargs):
        requested = now().isoformat()
        response = original_get(*args, **kwargs)
        http.append({'url': response.url, 'requestedAt': requested,
                     'receivedAt': now().isoformat(), 'body': response.text,
                     'status': response.status_code})
        return response

    client.session.get = record_get
    requested = now().isoformat()
    frame = client.tencent_raw_history(CODE, window[0], window[-1])
    artifacts['tencent'] = _store(artifact_root, 'tencent.json', {
        'source': 'AStockDataClient.tencent_raw_history', 'requestedAt': requested,
        'receivedAt': now().isoformat(), 'http': http,
        'records': json.loads(frame.to_json(orient='records', date_format='iso', double_precision=15))})
    if implementation_contract() != contract:
        raise ValueError('Implementation drift during acquisition')
    manifest = {'schemaVersion': 1, 'kind': 'fresh_benchmark_repair_observations',
                'startedAt': started, 'completedAt': now().isoformat(),
                'window': window, 'implementation': contract, 'artifacts': artifacts}
    _store(artifact_root, 'observations.json', manifest)
    return artifact_root


def _load_observations(root, window):
    root = Path(root).resolve()
    path = root / 'observations.json'
    sidecar = path.with_name(path.name + '.sha256').read_text().split()
    if sidecar != [sha(path), path.name]:
        raise ValueError('Observation manifest hash mismatch')
    manifest = json.loads(path.read_text())
    if (manifest.get('kind') != 'fresh_benchmark_repair_observations'
            or manifest.get('window') != window
            or manifest.get('implementation') != implementation_contract()):
        raise ValueError('Observation implementation/window mismatch')
    start, end = (datetime.fromisoformat(manifest[key]) for key in ('startedAt', 'completedAt'))
    if (start.tzinfo is None or end.tzinfo is None or not start <= end <= now()
            or end.astimezone(TZ).date() != now().astimezone(TZ).date()):
        raise ValueError('Observation acquisition must be fresh today with aware timestamps')
    expected = {f'{host}_{kind}' for host in HOSTS for kind in ('raw', 'qfq')} | {'tencent'}
    if set(manifest.get('artifacts', {})) != expected:
        raise ValueError('Five source observations are required')
    result = {}
    for key, artifact in manifest['artifacts'].items():
        path = (root / artifact['file']).resolve()
        if path.parent != root or path.is_symlink() or sha(path) != artifact['sha256']:
            raise ValueError('Source artifact hash/path mismatch')
        if path.with_name(path.name + '.sha256').read_text().split() != [sha(path), path.name]:
            raise ValueError('Source sidecar mismatch')
        payload = json.loads(path.read_text())
        requested, received = (datetime.fromisoformat(payload[name]) for name in ('requestedAt', 'receivedAt'))
        if requested.tzinfo is None or received.tzinfo is None or not start <= requested <= received <= end:
            raise ValueError('Invalid source observation timestamps')
        if key != 'tencent':
            host, kind = key.rsplit('_', 1)
            if (payload.get('host') != host or payload.get('adjustment') != ('NONE' if kind == 'raw' else 'QFQ')
                    or payload.get('fetchCalls') != (['NONE'] if kind == 'raw' else ['NONE', 'QFQ'])
                    or payload.get('syntheticAllowed') is not False):
                raise ValueError('Unverified server-only source sequence')
        result[key] = payload
    return result, manifest


def _frame(payload, window, tcp=False):
    work = pd.DataFrame(payload['records'])
    date_column = 'datetime' if tcp else 'date'
    work['date'] = pd.to_datetime(work[date_column], errors='raise').dt.strftime('%Y-%m-%d')
    if work['date'].duplicated().any():
        raise ValueError('Source contains duplicate dates')
    # Current/unrequested bars are retained in raw evidence, never used in repair.
    work = work[work['date'].isin(window)].sort_values('date').reset_index(drop=True)
    if work['date'].tolist() != window:
        raise ValueError('Source does not contain exactly the closed 40-session window')
    if tcp:
        work['volume'] = pd.to_numeric(work['vol']) * UNITS['volumeMultiplier']
        work['amount'] = pd.to_numeric(work['amount']) * UNITS['amountMultiplier']
    # Tencent's real parser retains textual OHLC. Normalize the stored columns,
    # not merely a temporary array: later comparisons and auditing use work.
    for field in SOURCE_FIELDS:
        work[field] = pd.to_numeric(work[field], errors='raise').astype('float64')
    numbers = work[list(SOURCE_FIELDS)].to_numpy(dtype='float64')
    if not np.isfinite(numbers).all():
        raise ValueError('Nonfinite source value')
    if ((work[['open', 'high', 'low', 'close']] <= 0).any().any()
            or (work[['volume', 'amount']] <= 0).any().any()
            or (work['low'] > work[['open', 'close']].min(axis=1)).any()
            or (work['high'] < work[['open', 'close']].max(axis=1)).any()):
        raise ValueError('Invalid source OHLC/volume/amount')
    if 'corporate_action' in work and work['corporate_action'].map(extension._action_payload).map(bool).any():
        raise ValueError('Benchmark source reports unexpected corporate actions')
    work['corporate_action'] = [{} for _ in window]
    # The accepted closed benchmark payload is raw-identical. Preserve the
    # original helper's stricter day-identity/factor/action gates.
    work['adjustment_key'] = 'day'
    work['synthetic'] = False
    return work


def audit_observations(base, artifact_root):
    base = Path(base).resolve()
    calendar, window = closed_window(base)
    payloads, source = _load_observations(artifact_root, window)
    frames = {key: _frame(payload, window, key != 'tencent') for key, payload in payloads.items()}
    tcp_keys = [f'{host}_{kind}' for host in HOSTS for kind in ('raw', 'qfq')]
    reference = frames[tcp_keys[0]][list(SOURCE_FIELDS)].to_numpy(dtype='<f8').tobytes()
    for key in tcp_keys[1:]:
        if frames[key][list(SOURCE_FIELDS)].to_numpy(dtype='<f8').tobytes() != reference:
            raise ValueError('TCP host/raw/QFQ closed values disagree')
    raw, tencent = frames[tcp_keys[0]], frames['tencent']
    for field in SOURCE_FIELDS:
        error = np.abs(raw[field].to_numpy() - tencent[field].to_numpy())
        tolerance = .01 if field in extension.PRICE_FIELDS else (
            np.abs(tencent[field].to_numpy()) * (1e-4 if field == 'volume' else 2e-6))
        if (error > tolerance).any():
            raise ValueError(f'Tencent fixed-unit corroboration failed: {field}')
    local = extension._load_base_frame(base, CODE, calendar, window)
    if not np.isfinite(local.iloc[:-1][list(extension.FIELDS)].to_numpy(dtype='float64')).all():
        raise ValueError('All 39 historical benchmark rows must be complete')
    if not local.iloc[-1][list(extension.FIELDS)].isna().all():
        raise ValueError('Only an entirely missing benchmark session may be repaired')
    audits, rows = {}, []
    for host in HOSTS:
        audit, context = extension.audit_symbol_mapping(
            base, CODE, frames[f'{host}_raw'], frames[f'{host}_qfq'], calendar, window, window[-2])
        counts = {name: metric['count'] for name, metric in audit['metrics'].items()}
        required = {'factorRelativeError': 39, 'volumeRelativeError': 39,
                    'amountRelativeError': 39, 'vwapRelativeError': 39,
                    'adjcloseRatioRelativeError': 39, 'changeAbsoluteError': 38,
                    'qfqReplayRelativeError': 156, 'qfqAdjustmentRegimeRelativeError': 156,
                    'dayIdentityRelativeError': 156}
        if (audit['status'] != 'PASS' or audit['commonSessions'] != 39
                or audit['localValidSessions'] != 39 or audit['overlapCoverage'] != 1
                or any(counts.get(name) != count for name, count in required.items())):
            raise ValueError(f'Original benchmark mapping audit failed: {host}')
        panel = extension.build_extension_rows(CODE, frames[f'{host}_raw'], frames[f'{host}_qfq'], [window[-1]], context)
        if panel['syntheticAdjustment'].any() or panel['noBar'].any():
            raise ValueError('Synthetic or missing adjusted target is forbidden')
        values = panel.iloc[0][list(extension.FIELDS)].to_numpy(dtype='<f4')
        if not np.isfinite(values).all():
            raise ValueError('Nonfinite float32 repair values')
        rows.append(values)
        audits[host] = audit
    if rows[0].tobytes() != rows[1].tobytes():
        raise ValueError('Independent host repair rows differ')
    return {'status': 'PASS', 'window': window, 'targetDate': window[-1],
            'units': dict(UNITS), 'auditThresholds': dict(extension.AUDIT_THRESHOLDS),
            'audits': audits, 'source': source,
            'sourceManifestSha256': sha(Path(artifact_root) / 'observations.json'),
            'row': dict(zip(extension.FIELDS, map(float, rows[0])))}


def build_repaired_provider(base, output, artifact_root):
    """Re-audit frozen observations and promote only the ten permitted cells."""
    base, output = Path(base).resolve(), Path(output).absolute()
    resolved = output.resolve()
    if (os.path.lexists(output) or base == resolved or base in resolved.parents
            or resolved in base.parents or output.parent.resolve() != output.parent):
        raise ValueError('Output must be a new nonoverlapping canonical path')
    before = tree(base)
    audit = audit_observations(base, artifact_root)
    inherited = json.loads((base / MANIFEST).read_text())
    if inherited.get('kind') != 'official_index_membership_overlay':
        raise ValueError('Repair requires the membership-derived Provider')
    if (base / INHERITED).exists():
        raise ValueError('Inherited provenance destination already exists')
    calendar, _ = closed_window(base)
    index = len(calendar) - 1
    edits = {}
    for field in extension.FIELDS:
        relative = f'features/sh000300/{field}.day.bin'
        content = (base / relative).read_bytes()
        if len(content) < 8 or len(content) % 4:
            raise ValueError('Malformed benchmark feature file')
        start = struct.unpack('<f', content[:4])[0]
        if not math.isfinite(start) or start < 0 or not start.is_integer():
            raise ValueError('Invalid benchmark feature header')
        offset = (index - int(start) + 1) * 4
        if offset < 4 or offset > len(content):
            raise ValueError('Repair may not bridge missing sessions')
        if offset < len(content) and not math.isnan(struct.unpack('<f', content[offset:offset+4])[0]):
            raise ValueError('Finite/infinite target value cannot be overwritten')
        value = struct.pack('<f', audit['row'][field])
        updated = content[:offset] + value + content[offset+4:]
        edits[relative] = (updated, {'offsetBytes': offset, 'oldFileSha256': before[relative],
                                    'newFileSha256': hashlib.sha256(updated).hexdigest(),
                                    'oldCellHex': content[offset:offset+4].hex(), 'newCellHex': value.hex()})
    if tree(base) != before:
        raise ValueError('Base changed during audit')
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f'.{output.name}.building-', dir=output.parent))
    try:
        shutil.copytree(base, temporary, dirs_exist_ok=True)
        temporary.chmod(0o700)
        for relative, (updated, _) in edits.items():
            target = temporary / relative
            target.chmod(0o600)
            target.write_bytes(updated)
            target.chmod(0o444)
        saved = temporary / INHERITED
        saved.write_bytes((base / MANIFEST).read_bytes())
        saved.chmod(0o444)
        entries = tree(temporary)
        for relative, digest in before.items():
            if relative not in edits and entries.get(relative) != digest:
                raise ValueError('Unexpected copied Provider change')
        codes = set()
        for line in (temporary / 'instruments/csi300.txt').read_text().splitlines():
            code, start, end = line.split('\t')
            if start <= calendar[-1] <= end:
                codes.add(code.lower())
        if len(codes) != 300:
            raise ValueError('Expected 300 current members')
        for code in sorted(codes):
            stock = extension._load_base_frame(temporary, code.upper(), calendar, [calendar[-1]])
            if not np.isfinite(stock[list(extension.FIELDS)].to_numpy(dtype='float64')).all():
                raise ValueError('Current stock features must be complete')
        codes.add('sh000300')
        feature_entries = [(f'features/{code}/{field}.day.bin', sha(temporary / f'features/{code}/{field}.day.bin'))
                           for code in sorted(codes) for field in extension.FIELDS]
        digest = hashlib.sha256()
        for relative, file_hash in sorted(feature_entries):
            digest.update(relative.encode()); digest.update(b'\0'); digest.update(file_hash.encode('ascii'))
        manifest = {'schemaVersion': 1, 'kind': 'frozen_market_daily_prices',
                    'repairType': 'benchmark_missing_session_repair', 'appendOnly': False,
                    'createdAt': now().isoformat(), 'profitEvidenceEligible': False,
                    'sourceProviderModified': False, 'baseProvider': str(base),
                    'baseTree': before, 'baseTreeSha256': hashlib.sha256(json_bytes(before)).hexdigest(),
                    'inheritedMembership': {'file': INHERITED, 'sha256': sha(saved),
                        'scope': 'Preserved ancestor provenance; original featureValuesModified refers only to its derivation.'},
                    'audit': audit, 'cellEdits': {key: val[1] for key, val in edits.items()},
                    'scope': {'market': 'csi300', 'benchmark': 'SH000300',
                              'baseCalendarEnd': calendar[-2], 'extensionCalendarStart': calendar[-1],
                              'extensionCalendarEnd': calendar[-1], 'repairDate': calendar[-1],
                              'allowedMarkets': ['csi300'], 'frozenUniverseSize': 300,
                              'allInstrumentSha256': sha(temporary / 'instruments/all.txt')},
                    'featureFields': list(extension.FIELDS),
                    'featureTree': {'files': len(feature_entries), 'outputTreeSha256': digest.hexdigest()},
                    'outputTreeBeforeRepairManifest': entries,
                    'scopeLimit': 'Benchmark repair only; does not establish auto-shadow or profit acceptance.'}
        target = temporary / MANIFEST
        target.chmod(0o600)
        target.write_bytes(json_bytes(manifest))
        target.chmod(0o444)
        actual = tree(temporary)
        expected = dict(before)
        expected.update({relative: detail['newFileSha256'] for relative, (_, detail) in edits.items()})
        expected[INHERITED], expected[MANIFEST] = sha(saved), sha(target)
        if actual != expected or tree(base) != before:
            raise ValueError('Full tree verification failed')
        readback = extension._load_base_frame(temporary, CODE, calendar, [calendar[-1]])
        if readback[list(extension.FIELDS)].to_numpy(dtype='<f4').tobytes() != np.array(list(audit['row'].values()), dtype='<f4').tobytes():
            raise ValueError('Repair readback differs')
        contract = provider_contract_fingerprint(temporary)
        if not contract['dailyFeatureContract'] or implementation_contract() != audit['source']['implementation']:
            raise ValueError('Runtime feature/implementation verification failed')
        if os.path.lexists(output):
            raise ValueError('Output appeared during promotion')
        temporary.rename(output)
        return {'status': 'PASS', 'provider': str(output),
                'contract': provider_contract_fingerprint(output), 'targetDate': calendar[-1],
                'profitEvidenceEligible': False}
    except BaseException:
        # Copied Providers may use read-only directories. Only our staging copy
        # is made writable for cleanup; the original Provider is never chmod'ed.
        temporary.chmod(0o700)
        for directory, directories, _ in os.walk(temporary):
            for name in directories:
                (Path(directory) / name).chmod(0o700)
        shutil.rmtree(temporary)
        raise
