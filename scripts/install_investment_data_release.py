#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quantaalpha.utils.investment_data_release import (
    download_official_release,
    install_release_provider,
    load_release_manifest,
    switch_runtime_provider,
)


def _path(value: str) -> Path:
    path = Path(value).expanduser()
    return path.resolve() if path.is_absolute() else (PROJECT_ROOT / path).resolve()


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Download, verify, and immutably install an official chenditc/"
            "investment_data Qlib release. Existing Provider directories are never overwritten."
        )
    )
    parser.add_argument(
        "--release-tag",
        help="Official YYYY-MM-DD release tag. Omit to use the latest official release.",
    )
    parser.add_argument(
        "--source-dir",
        help="Use an existing directory containing qlib_bin.tar.gz and its manifest.",
    )
    parser.add_argument(
        "--release-root",
        default="data/market/a_share/releases",
        help="Directory used for downloaded immutable release assets.",
    )
    parser.add_argument(
        "--output-provider",
        help="New Provider path. Defaults to data/market/a_share/qlib/investment_data_YYYYMMDD_cn.",
    )
    parser.add_argument(
        "--switch",
        action="store_true",
        help="After validation, update .env and ~/.qlib/qlib_data/cn_data.",
    )
    args = parser.parse_args()

    source_dir = (
        _path(args.source_dir)
        if args.source_dir
        else download_official_release(_path(args.release_root), args.release_tag)
    )
    manifest = load_release_manifest(
        source_dir / "qlib_bin.manifest.json",
        expected_tag=args.release_tag,
    )
    release_slug = str(manifest["release_tag"]).replace("-", "")
    output_provider = (
        _path(args.output_provider)
        if args.output_provider
        else PROJECT_ROOT
        / "data"
        / "market"
        / "a_share"
        / "qlib"
        / f"investment_data_{release_slug}_cn"
    )
    result = install_release_provider(source_dir, output_provider)
    if args.switch:
        result["runtimeSwitch"] = switch_runtime_provider(output_provider)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
