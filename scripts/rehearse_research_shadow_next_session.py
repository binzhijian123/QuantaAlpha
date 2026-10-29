#!/usr/bin/env python3
"""Run an isolated next-session research-shadow engineering rehearsal."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from quantaalpha.trading.research_shadow import DEFAULT_DECLARATION
from quantaalpha.trading.research_shadow_rehearsal import (
    run_research_shadow_rehearsal,
)


def _write_report(path: Path, report: dict) -> None:
    path = path.expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Rehearse the frozen paired research-shadow run on one synthetic "
            "next session without writing production state."
        )
    )
    parser.add_argument("--declaration", default=str(DEFAULT_DECLARATION))
    parser.add_argument("--trade-date")
    parser.add_argument("--report")
    parser.add_argument("--keep-workspace", action="store_true")
    parser.add_argument("--allow-full-copy", action="store_true")
    args = parser.parse_args()

    report = run_research_shadow_rehearsal(
        Path(args.declaration),
        trade_date=args.trade_date,
        keep_workspace=args.keep_workspace,
        allow_full_copy=args.allow_full_copy,
        progress=lambda message: print(f"[rehearsal] {message}", file=sys.stderr),
    )
    if args.report:
        _write_report(Path(args.report), report)
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
