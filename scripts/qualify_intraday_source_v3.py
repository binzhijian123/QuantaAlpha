#!/usr/bin/env python3
"""Freeze the independent intraday source qualification report."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = PROJECT_ROOT / "frontend-v2" / "backend"
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from market_data.intraday_source_qualification_v3 import (  # noqa: E402
    qualify_independent_source,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--reference-snapshot",
        type=Path,
        default=PROJECT_ROOT
        / "data/market/a_share/intraday/a_stock_data_csi300/snapshots/2026-08-14"
        / "snapshot_150000_3856f839b91b.json.gz",
    )
    parser.add_argument(
        "--output-root",
        type=Path,
        default=PROJECT_ROOT
        / "data/market/a_share/intraday/a_stock_data_csi300",
    )
    args = parser.parse_args()
    report = qualify_independent_source(
        args.reference_snapshot,
        args.output_root,
    )
    summary = {
        "status": report["status"],
        "decision": report["decision"],
        "tradeDate": report["tradeDate"],
        "reportSha256": report["reportSha256"],
        "observed": report["observed"],
        "profitEvidenceEligible": False,
    }
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
