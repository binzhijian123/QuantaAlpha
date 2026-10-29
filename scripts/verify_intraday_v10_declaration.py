#!/usr/bin/env python3
"""Qualify a V10 declaration read-only; never claims runtime readiness."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
for directory in (PROJECT_ROOT, PROJECT_ROOT / "frontend-v2/backend"):
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))


class _PublicArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise ValueError("Invalid CLI arguments.")


def main(argv: list[str] | None = None) -> int:
    status = {"status": "NOT_READY", "autoShadowAccepted": False,
              "runtimeNotTested": True, "message": "V10 declaration is not qualified."}
    try:
        parser = _PublicArgumentParser(description=__doc__)
        parser.add_argument("--declaration", required=True, type=Path)
        args = parser.parse_args(argv)
        from market_data.intraday_shadow_v10 import validate_v10_declaration
        validate_v10_declaration(args.declaration)
    except Exception:
        # Local paths, URLs, configuration values and upstream exception messages
        # are deliberately not echoed. The function API remains available for
        # trusted local debugging; this CLI only emits fixed public statements.
        print(json.dumps(status, sort_keys=True))
        return 1
    status.update(status="DECLARATION_QUALIFIED",
                  message="Declaration qualified; runtime has not been tested.")
    print(json.dumps(status, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
