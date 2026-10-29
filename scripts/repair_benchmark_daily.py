#!/usr/bin/env python3
"""Collect and audit by default; explicit --promote creates an isolated Provider."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
for path in (ROOT, ROOT / 'frontend-v2/backend'):
    sys.path.insert(0, str(path))
from market_data.benchmark_daily_repair import (
    collect_observations, audit_observations, build_repaired_provider,
)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-provider', type=Path, required=True)
    parser.add_argument('--artifact-root', type=Path, required=True,
                        help='New directory for fresh source observations; existing evidence is never reused.')
    parser.add_argument('--output-provider', type=Path)
    parser.add_argument('--promote', action='store_true')
    args = parser.parse_args(argv)
    if args.promote != bool(args.output_provider):
        parser.error('--promote and --output-provider must be supplied together')
    try:
        observations = collect_observations(args.base_provider, args.artifact_root)
        audit = audit_observations(args.base_provider, observations)
        result = {'status': audit['status'], 'targetDate': audit['targetDate'],
                  'promoted': False, 'artifacts': str(observations)}
        if args.promote:
            promoted = build_repaired_provider(args.base_provider, args.output_provider, observations)
            result.update(provider=promoted['provider'],
                          fingerprint=promoted['contract']['fingerprint'], promoted=True)
    except Exception:
        # Source responses may be large; never reflect transport exception bodies.
        print(json.dumps({'status': 'FAIL', 'error': 'BENCHMARK_REPAIR_REJECTED',
                          'promoted': False}))
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0



if __name__ == '__main__':
    raise SystemExit(main())
