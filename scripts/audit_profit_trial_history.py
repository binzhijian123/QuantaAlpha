#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quantaalpha.validation.trial_history import (  # noqa: E402
    collect_strict_trial_history,
    write_strict_trial_history_audit,
)


DEFAULT_RECONCILIATION = (
    PROJECT_ROOT / "configs" / "profit_trial_history_reconciliation_v1.json"
)


def collect_trial_history(
    project_root: Path,
    entrypoint_path: Path | None = None,
    audit_code_path: Path | None = None,
):
    return collect_strict_trial_history(
        project_root,
        Path(project_root) / "configs" / "profit_trial_history_reconciliation_v1.json",
        entrypoint_path or Path(__file__),
        audit_code_path,
    )


def write_trial_history_audit(
    project_root: Path,
    output_dir: Path,
    entrypoint_path: Path | None = None,
    audit_code_path: Path | None = None,
):
    return write_strict_trial_history_audit(
        project_root,
        Path(project_root) / "configs" / "profit_trial_history_reconciliation_v1.json",
        entrypoint_path or Path(__file__),
        output_dir,
        audit_code_path,
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build a strict crosswalk of revealed profit trials."
    )
    parser.add_argument("--project-root", default=str(PROJECT_ROOT))
    parser.add_argument("--reconciliation", default=str(DEFAULT_RECONCILIATION))
    parser.add_argument("--output-dir")
    args = parser.parse_args()
    project_root = Path(args.project_root).resolve()
    output_dir = (
        Path(args.output_dir).resolve()
        if args.output_dir
        else project_root
        / "data"
        / "results"
        / "profit_validation"
        / f"trial_history_audit_v2_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    )
    result = write_strict_trial_history_audit(
        project_root,
        Path(args.reconciliation),
        Path(__file__),
        output_dir,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
