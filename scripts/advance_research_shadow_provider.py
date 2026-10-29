#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quantaalpha.trading.research_shadow_automation import (
    advance_official_provider,
    resolve_project_path,
)
from scripts.verify_research_shadow_provider_lineage import (
    DEFAULT_DECLARATION,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Install the latest official investment_data release, prove append-only "
            "lineage, and switch runtime only after the proof passes."
        )
    )
    parser.add_argument("--release-tag", help="Official YYYY-MM-DD release tag.")
    parser.add_argument("--declaration", default=str(DEFAULT_DECLARATION))
    parser.add_argument(
        "--release-root",
        default="data/market/a_share/releases",
    )
    args = parser.parse_args()

    result = advance_official_provider(
        resolve_project_path(args.declaration),
        release_tag=args.release_tag,
        release_root=resolve_project_path(args.release_root),
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
