"""Build an immutable paper-v3 factor pool from a mined factor library."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd
import yaml

from quantaalpha.validation.factor_pool import select_paper_factor_pool


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _resolve(value: str | Path) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else PROJECT_ROOT / path


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )


def _normalize_factor_series(value: Any, source: Path) -> pd.Series:
    if isinstance(value, pd.DataFrame):
        if value.shape[1] != 1:
            raise ValueError(f"Factor HDF must have one column: {source}")
        value = value.iloc[:, 0]
    if not isinstance(value, pd.Series):
        raise ValueError(f"Factor HDF must contain a Series: {source}")
    if not isinstance(value.index, pd.MultiIndex):
        raise ValueError(f"Factor HDF must use a MultiIndex: {source}")
    names = list(value.index.names)
    if names == ["instrument", "datetime"]:
        value = value.reorder_levels(["datetime", "instrument"])
    elif names != ["datetime", "instrument"]:
        raise ValueError(f"Unexpected factor index names {names}: {source}")

    datetimes = pd.to_datetime(
        value.index.get_level_values("datetime"),
        errors="raise",
    )
    instruments = (
        value.index.get_level_values("instrument")
        .astype(str)
        .str.upper()
    )
    value.index = pd.MultiIndex.from_arrays(
        [datetimes, instruments],
        names=["datetime", "instrument"],
    )
    if value.index.has_duplicates:
        raise ValueError(f"Factor HDF contains duplicate rows: {source}")
    return pd.to_numeric(value.sort_index(), errors="coerce")


def _build_validation_label(
    factor_data_path: Path,
    start_time: str,
    end_time: str,
) -> pd.Series:
    panel = pd.read_hdf(factor_data_path, key="data")
    if "$close" not in panel:
        raise ValueError(f"$close is missing from {factor_data_path}")
    close = pd.to_numeric(panel["$close"], errors="coerce").sort_index()
    grouped = close.groupby(level="instrument", sort=False)
    label = grouped.shift(-2) / grouped.shift(-1) - 1.0
    dates = label.index.get_level_values("datetime")
    label = label.loc[
        (dates >= pd.Timestamp(start_time))
        & (dates <= pd.Timestamp(end_time))
    ]
    label.name = "Ref($close, -2) / Ref($close, -1) - 1"
    if label.empty or label.index.has_duplicates:
        raise ValueError("Validation label is empty or has duplicate rows")
    return label


def build_paper_factor_pool(
    protocol_config_path: str | Path,
    library_path: str | Path,
    output_dir: str | Path,
) -> dict[str, Any]:
    protocol_path = _resolve(protocol_config_path).resolve()
    library_path = _resolve(library_path).resolve()
    output_dir = _resolve(output_dir).resolve()
    if output_dir.exists():
        raise FileExistsError(f"Output already exists: {output_dir}")
    temporary = output_dir.with_name(f".{output_dir.name}.tmp")
    if temporary.exists():
        raise FileExistsError(f"Temporary output already exists: {temporary}")

    protocol = yaml.safe_load(protocol_path.read_text(encoding="utf-8"))
    library = json.loads(library_path.read_text(encoding="utf-8"))
    factors = library.get("factors")
    if not isinstance(factors, dict) or not factors:
        raise ValueError("Factor library is empty")

    validation_start, validation_end = protocol["paper_protocol"][
        "effective_segments"
    ]["valid"]
    factor_data_path = _resolve(
        protocol["runtime"]["factor_data"]["path"]
    ).resolve()
    label = _build_validation_label(
        factor_data_path,
        validation_start,
        validation_end,
    )

    eligible: dict[str, dict[str, Any]] = {}
    preselection: list[dict[str, Any]] = []
    for factor_id, entry in sorted(factors.items()):
        quality = entry.get("quality_gate") or {}
        if not (
            quality.get("evaluated")
            and quality.get("passed")
            and quality.get("consistency_passed")
        ):
            preselection.append(
                {
                    "factor": factor_id,
                    "status": "REJECTED",
                    "reason": "quality_gate_evidence_missing_or_failed",
                }
            )
            continue
        eligible[factor_id] = entry
    if not eligible:
        raise ValueError("No factors have complete passing quality-gate evidence")

    series: dict[str, pd.Series] = {}
    factor_sources: dict[str, dict[str, Any]] = {}
    for factor_id, entry in eligible.items():
        source_value = (entry.get("cache_location") or {}).get(
            "result_h5_path"
        )
        if not source_value:
            raise ValueError(f"Factor {factor_id} has no result_h5_path")
        source = _resolve(source_value).resolve()
        if not source.is_file():
            raise FileNotFoundError(f"Factor cache does not exist: {source}")
        value = _normalize_factor_series(pd.read_hdf(source), source)
        series[factor_id] = value.reindex(label.index)
        factor_sources[factor_id] = {
            "path": str(source),
            "sha256": _sha256(source),
            "size": source.stat().st_size,
        }

    factor_panel = pd.DataFrame(series, index=label.index)
    assumptions = protocol["replication_assumptions"]["factor_pool"]
    selection = select_paper_factor_pool(
        factor_panel,
        label,
        max_abs_correlation=float(assumptions["max_abs_correlation"]),
        max_factors=int(assumptions["max_factors"]),
        max_fraction=float(assumptions["max_fraction"]),
        min_cross_section_size=int(
            assumptions["min_cross_section_size"]
        ),
        min_valid_days=int(assumptions["min_valid_days"]),
    )
    if selection["correlationMethod"] != assumptions["correlation_method"]:
        raise ValueError("Factor-pool correlation contract mismatch")

    selected_ids = selection["selectedFactors"]
    if not selected_ids:
        raise ValueError("Factor-pool admission selected no usable factors")
    selected_library = {
        "metadata": {
            "version": "paper_v3_selected_pool_v1",
            "created_at": datetime.now().astimezone().isoformat(),
            "source_library": str(library_path),
            "source_library_sha256": _sha256(library_path),
            "validation_segment": [validation_start, validation_end],
            "selected_factors": len(selected_ids),
            "protocol_id": protocol["protocol"]["id"],
        },
        "factors": {
            factor_id: factors[factor_id] for factor_id in selected_ids
        },
    }
    result = {
        "schemaVersion": 1,
        "createdAt": datetime.now().astimezone().isoformat(),
        "protocolId": protocol["protocol"]["id"],
        "validationSegment": [validation_start, validation_end],
        "label": protocol["paper_protocol"]["label"],
        "sourceLibrary": {
            "path": str(library_path),
            "sha256": _sha256(library_path),
            "totalFactors": len(factors),
            "eligibleFactors": len(eligible),
        },
        "preselectionDecisions": preselection,
        "selection": selection,
    }
    manifest = {
        "protocol": {
            "path": str(protocol_path),
            "sha256": _sha256(protocol_path),
        },
        "factorData": {
            "path": str(factor_data_path),
            "sha256": _sha256(factor_data_path),
        },
        "sourceLibrary": result["sourceLibrary"],
        "factorSources": factor_sources,
    }

    temporary.mkdir(parents=True)
    _write_json(temporary / "selection.json", result)
    _write_json(temporary / "selected_factor_library.json", selected_library)
    _write_json(temporary / "manifest.json", manifest)
    (temporary / "report.md").write_text(
        "\n".join(
            [
                "# QuantaAlpha 论文 v3 最终因子池",
                "",
                f"- 验证区间：`{validation_start}` 至 `{validation_end}`",
                f"- 原始因子：`{len(factors)}`",
                f"- 质量门槛合格：`{len(eligible)}`",
                f"- 池上限：`{selection['poolLimit']}`",
                f"- 最终入选：`{selection['selectedCount']}`",
                "",
                "本步骤只使用验证集进行因子池准入；测试集未参与排序或去冗余。",
                "",
            ]
        ),
        encoding="utf-8",
    )
    temporary.replace(output_dir)
    return {
        **result,
        "outputDir": str(output_dir),
        "selectedLibrary": str(output_dir / "selected_factor_library.json"),
    }
