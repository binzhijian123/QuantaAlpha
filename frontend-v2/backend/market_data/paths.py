from __future__ import annotations

from pathlib import Path


def market_data_root(project_root: Path) -> Path:
    return project_root / "data" / "market" / "a_share"


def raw_provider_dir(project_root: Path, provider: str, batch_id: str) -> Path:
    return market_data_root(project_root) / "raw" / provider / batch_id


def staging_dir(project_root: Path, batch_id: str) -> Path:
    return market_data_root(project_root) / "staging" / batch_id


def qlib_provider_dir(project_root: Path, provider_name: str = "a_stock_data_daily_cn") -> Path:
    return market_data_root(project_root) / "qlib" / provider_name


def manifest_path(project_root: Path, batch_id: str) -> Path:
    return market_data_root(project_root) / "manifests" / f"{batch_id}.json"


def report_path(project_root: Path, batch_id: str) -> Path:
    return market_data_root(project_root) / "reports" / f"{batch_id}.json"
