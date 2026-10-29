#!/usr/bin/env python3
"""Build the paper-validation factor panel from the locked Qlib provider."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd
import yaml


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quantaalpha.utils.qlib_provider import provider_contract_fingerprint


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def resolve_path(value: str) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else PROJECT_ROOT / path


def build_factor_data(config_path: Path) -> dict:
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    protocol = config["paper_protocol"]
    runtime = config["runtime"]
    factor_data = runtime["factor_data"]
    provider = resolve_path(runtime["provider_uri"]).resolve()
    output_path = resolve_path(factor_data["path"]).resolve()
    manifest_path = resolve_path(factor_data["manifest_path"]).resolve()

    provider_contract = provider_contract_fingerprint(provider, protocol["market"])
    expected_fingerprint = runtime.get("provider_fingerprint")
    if expected_fingerprint and provider_contract["fingerprint"] != expected_fingerprint:
        raise RuntimeError(
            "Qlib provider fingerprint changed: "
            f"{provider_contract['fingerprint']} != {expected_fingerprint}"
        )

    import qlib
    from qlib.data import D

    qlib.init(provider_uri=str(provider), region="cn")
    fields = list(protocol["raw_features"])
    instruments = D.instruments(protocol["market"])
    panel = D.features(
        instruments,
        fields,
        start_time=factor_data["start_time"],
        end_time=factor_data["end_time"],
        freq="day",
    )
    if panel.empty:
        raise RuntimeError("Qlib returned an empty paper-validation factor panel")

    if list(panel.index.names) == ["instrument", "datetime"]:
        panel = panel.swaplevel()
    panel = panel.sort_index()
    panel.index = panel.index.set_names(["datetime", "instrument"])
    if panel.index.has_duplicates:
        raise RuntimeError("paper-validation factor panel contains duplicate rows")
    if list(panel.columns) != fields:
        raise RuntimeError(
            f"factor panel fields differ from the paper protocol: {list(panel.columns)}"
        )

    coverage = panel.notna().mean()
    missing_fields = [field for field in fields if coverage[field] < 0.95]
    if missing_fields:
        raise RuntimeError(
            f"factor panel fields have less than 95% coverage: {missing_fields}"
        )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    temp_output = output_path.with_suffix(".tmp.h5")
    panel.astype("float32").to_hdf(temp_output, key="data", mode="w", format="fixed")
    temp_output.replace(output_path)

    dates = panel.index.get_level_values("datetime")
    active_counts = panel.groupby(level="datetime").size()
    manifest = {
        "schemaVersion": 1,
        "createdAt": datetime.now().astimezone().isoformat(),
        "configPath": str(config_path.resolve()),
        "configSha256": sha256_file(config_path),
        "dataPath": str(output_path),
        "dataSha256": sha256_file(output_path),
        "provider": provider_contract,
        "market": protocol["market"],
        "fields": fields,
        "startTime": dates.min().date().isoformat(),
        "endTime": dates.max().date().isoformat(),
        "rows": int(len(panel)),
        "uniqueInstruments": int(
            panel.index.get_level_values("instrument").nunique()
        ),
        "activeInstrumentsPerDay": {
            "minimum": int(active_counts.min()),
            "median": float(active_counts.median()),
            "maximum": int(active_counts.max()),
        },
        "fieldCoverage": {
            field: float(value) for field, value in coverage.items()
        },
    }
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    temp_manifest = manifest_path.with_suffix(".tmp.json")
    temp_manifest.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temp_manifest.replace(manifest_path)
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--config",
        default="configs/paper_validation_v3.yaml",
        help="Paper-validation protocol YAML",
    )
    args = parser.parse_args()
    config_path = resolve_path(args.config)
    manifest = build_factor_data(config_path)
    print(
        json.dumps(
            {
                "dataPath": manifest["dataPath"],
                "dataSha256": manifest["dataSha256"],
                "rows": manifest["rows"],
                "dates": [manifest["startTime"], manifest["endTime"]],
                "fields": manifest["fields"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
