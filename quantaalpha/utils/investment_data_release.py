"""Install immutable Qlib providers from official investment_data releases."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tarfile
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date
from pathlib import Path, PurePosixPath
from typing import Any, Dict, Optional

from quantaalpha.utils.qlib_provider import provider_contract_fingerprint


PROJECT_ROOT = Path(__file__).resolve().parents[2]
RELEASE_API = "https://api.github.com/repos/chenditc/investment_data/releases"
ARCHIVE_NAME = "qlib_bin.tar.gz"
MANIFEST_NAME = "qlib_bin.manifest.json"
REQUIRED_MANIFEST_KEYS = {
    "release_tag",
    "target_trade_date",
    "archive_size_bytes",
    "archive_sha256",
}
METADATA_REQUEST_ATTEMPTS = 3
METADATA_RETRY_DELAYS_SECONDS = (1.0, 2.0)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _validated_release_manifest(
    payload: Dict[str, Any],
    expected_tag: Optional[str] = None,
) -> Dict[str, Any]:
    manifest = dict(payload)
    missing = REQUIRED_MANIFEST_KEYS.difference(manifest)
    if missing:
        raise RuntimeError(f"release manifest is missing keys: {sorted(missing)}")
    release_tag = str(manifest["release_tag"])
    target_date = str(manifest["target_trade_date"])
    if expected_tag and release_tag != expected_tag:
        raise RuntimeError(
            f"release tag mismatch: expected {expected_tag}, got {release_tag}"
        )
    parsed_dates = {}
    for field, value in (
        ("release_tag", release_tag),
        ("target_trade_date", target_date),
    ):
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            raise RuntimeError(f"{field} must be an ISO date")
        try:
            parsed_dates[field] = date.fromisoformat(value)
        except ValueError as exc:
            raise RuntimeError(f"{field} must be a valid date") from exc
    if parsed_dates["target_trade_date"] > parsed_dates["release_tag"]:
        raise RuntimeError("target_trade_date cannot be later than release_tag")
    archive_size = manifest["archive_size_bytes"]
    if not isinstance(archive_size, int) or archive_size <= 0:
        raise RuntimeError("archive_size_bytes must be a positive integer")
    archive_sha = str(manifest["archive_sha256"]).removeprefix("sha256:")
    if not re.fullmatch(r"[0-9a-f]{64}", archive_sha):
        raise RuntimeError("archive_sha256 must be a lowercase SHA256 digest")
    manifest["archive_sha256"] = f"sha256:{archive_sha}"
    return manifest


def load_release_manifest(path: Path, expected_tag: Optional[str] = None) -> Dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"invalid release manifest {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"invalid release manifest {path}: expected an object")
    return _validated_release_manifest(payload, expected_tag=expected_tag)


def verify_release_assets(source_dir: Path, expected_tag: Optional[str] = None) -> Dict[str, Any]:
    source_dir = source_dir.expanduser().resolve()
    manifest_path = source_dir / MANIFEST_NAME
    archive_path = source_dir / ARCHIVE_NAME
    manifest = load_release_manifest(manifest_path, expected_tag=expected_tag)
    if not archive_path.is_file():
        raise RuntimeError(f"release archive is missing: {archive_path}")
    actual_size = archive_path.stat().st_size
    if actual_size != manifest["archive_size_bytes"]:
        raise RuntimeError(
            f"release archive size mismatch: expected {manifest['archive_size_bytes']}, "
            f"got {actual_size}"
        )
    expected_sha = str(manifest["archive_sha256"]).removeprefix("sha256:")
    actual_sha = sha256_file(archive_path)
    if actual_sha != expected_sha:
        raise RuntimeError(
            f"release archive SHA256 mismatch: expected {expected_sha}, got {actual_sha}"
        )
    return {
        "sourceDir": str(source_dir),
        "manifestPath": str(manifest_path),
        "manifestSha256": sha256_file(manifest_path),
        "archivePath": str(archive_path),
        "archiveSizeBytes": actual_size,
        "archiveSha256": actual_sha,
        "manifest": manifest,
    }


def _request_json(url: str) -> Dict[str, Any]:
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "QuantaAlpha-investment-data-installer",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    last_error: Exception | None = None
    for attempt in range(METADATA_REQUEST_ATTEMPTS):
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            last_error = exc
            if 400 <= exc.code < 500 and exc.code not in {408, 429}:
                break
        except Exception as exc:
            last_error = exc
        if attempt < METADATA_REQUEST_ATTEMPTS - 1:
            time.sleep(METADATA_RETRY_DELAYS_SECONDS[attempt])
    raise RuntimeError(
        f"failed to query official release metadata after "
        f"{attempt + 1} attempt(s): {last_error}"
    ) from last_error


def _release_metadata(release_tag: Optional[str] = None) -> Dict[str, Any]:
    endpoint = (
        f"{RELEASE_API}/tags/{urllib.parse.quote(release_tag, safe='')}"
        if release_tag
        else f"{RELEASE_API}/latest"
    )
    metadata = _request_json(endpoint)
    actual_tag = str(metadata.get("tag_name") or "")
    if not actual_tag or (release_tag and actual_tag != release_tag):
        raise RuntimeError(
            f"official release metadata returned unexpected tag {actual_tag!r}"
        )
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", actual_tag):
        raise RuntimeError(f"official release tag is not an ISO date: {actual_tag!r}")
    try:
        date.fromisoformat(actual_tag)
    except ValueError as exc:
        raise RuntimeError(
            f"official release tag is not a valid date: {actual_tag!r}"
        ) from exc
    return metadata


def query_official_release_tag(release_tag: Optional[str] = None) -> str:
    """Read only release metadata without downloading or hashing large assets."""
    return str(_release_metadata(release_tag)["tag_name"])


def query_official_release_manifest(
    release_tag: Optional[str] = None,
) -> Dict[str, Any]:
    """Read and validate the small manifest without downloading the archive."""
    metadata = _release_metadata(release_tag)
    actual_tag = str(metadata["tag_name"])
    assets = {
        str(asset.get("name")): str(asset.get("browser_download_url"))
        for asset in metadata.get("assets", [])
    }
    manifest_url = assets.get(MANIFEST_NAME)
    if not manifest_url:
        raise RuntimeError(f"official release {actual_tag} is missing {MANIFEST_NAME}")
    payload = _request_json(manifest_url)
    if not isinstance(payload, dict):
        raise RuntimeError("official release manifest must be a JSON object")
    return _validated_release_manifest(payload, expected_tag=actual_tag)


def _download(url: str, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{os.getpid()}.part")
    if temporary.exists():
        temporary.unlink()
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "QuantaAlpha-investment-data-installer"},
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response, temporary.open("wb") as stream:
            shutil.copyfileobj(response, stream, length=1024 * 1024)
        os.replace(temporary, destination)
    except Exception as exc:
        temporary.unlink(missing_ok=True)
        raise RuntimeError(f"failed to download {destination.name}: {exc}") from exc


def download_official_release(
    release_root: Path,
    release_tag: Optional[str] = None,
) -> Path:
    metadata = _release_metadata(release_tag)
    actual_tag = str(metadata.get("tag_name") or "")
    assets = {
        str(asset.get("name")): str(asset.get("browser_download_url"))
        for asset in metadata.get("assets", [])
    }
    missing = {ARCHIVE_NAME, MANIFEST_NAME}.difference(assets)
    if missing:
        raise RuntimeError(f"official release is missing assets: {sorted(missing)}")
    source_dir = release_root.expanduser().resolve() / (
        f"investment_data_{actual_tag.replace('-', '')}"
    )
    source_dir.mkdir(parents=True, exist_ok=True)
    for asset_name in (MANIFEST_NAME, ARCHIVE_NAME):
        destination = source_dir / asset_name
        if not destination.exists():
            _download(assets[asset_name], destination)
    verify_release_assets(source_dir, expected_tag=actual_tag)
    return source_dir


def _validated_tar_members(archive_path: Path) -> list[tarfile.TarInfo]:
    with tarfile.open(archive_path, "r:gz") as archive:
        members = archive.getmembers()
    if not members:
        raise RuntimeError("release archive is empty")
    paths = set()
    for member in members:
        relative = PurePosixPath(member.name)
        if (
            relative.is_absolute()
            or ".." in relative.parts
            or not relative.parts
            or relative.parts[0] != "qlib_bin"
        ):
            raise RuntimeError(f"unsafe release archive path: {member.name}")
        if not (member.isdir() or member.isfile()):
            raise RuntimeError(f"unsupported release archive entry: {member.name}")
        if member.name in paths:
            raise RuntimeError(f"duplicate release archive path: {member.name}")
        paths.add(member.name)
    return members


def verify_provider_tree(provider: Path, archive_path: Path) -> Dict[str, Any]:
    """Prove that every installed data file still matches the verified archive."""
    provider = provider.expanduser().resolve()
    members = _validated_tar_members(archive_path)
    expected_paths = set()
    tree_digest = hashlib.sha256()
    total_bytes = 0
    file_count = 0
    with tarfile.open(archive_path, "r:gz") as archive:
        for member in members:
            if not member.isfile():
                continue
            relative = PurePosixPath(*PurePosixPath(member.name).parts[1:])
            target = provider.joinpath(*relative.parts)
            if not target.is_file() or target.is_symlink():
                raise RuntimeError(f"installed Provider file is missing: {relative}")
            if target.stat().st_size != member.size:
                raise RuntimeError(f"installed Provider file size differs: {relative}")
            expected_paths.add(relative.as_posix())
            source = archive.extractfile(member)
            if source is None:
                raise RuntimeError(f"cannot read release archive file: {member.name}")
            file_digest = hashlib.sha256()
            with source, target.open("rb") as installed:
                while True:
                    source_chunk = source.read(1024 * 1024)
                    installed_chunk = installed.read(1024 * 1024)
                    if source_chunk != installed_chunk:
                        raise RuntimeError(
                            f"installed Provider file content differs: {relative}"
                        )
                    if not source_chunk:
                        break
                    file_digest.update(installed_chunk)
            tree_digest.update(relative.as_posix().encode("utf-8"))
            tree_digest.update(b"\0")
            tree_digest.update(file_digest.hexdigest().encode("ascii"))
            total_bytes += member.size
            file_count += 1

    actual_paths = {
        path.relative_to(provider).as_posix()
        for path in provider.rglob("*")
        if path.is_file() and path.name != MANIFEST_NAME
    }
    if actual_paths != expected_paths:
        missing = sorted(expected_paths - actual_paths)[:5]
        extra = sorted(actual_paths - expected_paths)[:5]
        raise RuntimeError(
            f"installed Provider file set differs: missing={missing}, extra={extra}"
        )
    return {
        "files": file_count,
        "bytes": total_bytes,
        "treeSha256": tree_digest.hexdigest(),
    }


def validate_installed_provider(
    provider: Path,
    expected_manifest: Dict[str, Any],
) -> Dict[str, Any]:
    provider = provider.expanduser().resolve()
    required = (
        provider / "calendars" / "day.txt",
        provider / "instruments" / "all.txt",
        provider / "instruments" / "csi300.txt",
        provider / "features",
        provider / MANIFEST_NAME,
    )
    missing = [str(path) for path in required if not path.exists()]
    if missing:
        raise RuntimeError(f"installed Provider is incomplete: {missing}")
    installed_manifest = load_release_manifest(
        provider / MANIFEST_NAME,
        expected_tag=str(expected_manifest["release_tag"]),
    )
    if installed_manifest != expected_manifest:
        raise RuntimeError("installed Provider manifest differs from the release manifest")
    dates = [
        value.strip()
        for value in (provider / "calendars" / "day.txt").read_text(
            encoding="utf-8"
        ).splitlines()
        if value.strip()
    ]
    if not dates or dates != sorted(set(dates)):
        raise RuntimeError("installed Provider day calendar is empty or not strictly ordered")
    target_date = str(expected_manifest["target_trade_date"])
    if dates[-1] != target_date:
        raise RuntimeError(
            f"installed Provider ends on {dates[-1]}, expected {target_date}"
        )
    active_csi300 = 0
    for line in (provider / "instruments" / "csi300.txt").read_text(
        encoding="utf-8"
    ).splitlines():
        parts = line.split("\t")
        if len(parts) >= 3 and parts[1] <= target_date <= parts[2]:
            active_csi300 += 1
    if active_csi300 != 300:
        raise RuntimeError(
            f"installed Provider has {active_csi300} active CSI300 members on {target_date}"
        )
    contract = provider_contract_fingerprint(provider, market="csi300")
    return {
        "providerPath": str(provider),
        "calendarStart": dates[0],
        "calendarEnd": dates[-1],
        "calendarSessions": len(dates),
        "activeCsi300": active_csi300,
        "providerFingerprint": contract["fingerprint"],
        "providerContractStrength": contract["strength"],
    }


def install_release_provider(source_dir: Path, output_provider: Path) -> Dict[str, Any]:
    assets = verify_release_assets(source_dir)
    manifest = assets["manifest"]
    output_provider = output_provider.expanduser().resolve()
    if output_provider.exists():
        provider = validate_installed_provider(output_provider, manifest)
        provider["archiveTree"] = verify_provider_tree(
            output_provider, Path(assets["archivePath"])
        )
        return {"status": "REUSED", "assets": assets, "provider": provider}
    output_provider.parent.mkdir(parents=True, exist_ok=True)
    temporary_root = Path(
        tempfile.mkdtemp(prefix=f".{output_provider.name}.", dir=output_provider.parent)
    )
    try:
        archive_path = Path(assets["archivePath"])
        members = _validated_tar_members(archive_path)
        with tarfile.open(archive_path, "r:gz") as archive:
            archive.extractall(temporary_root, members=members)
        extracted = temporary_root / "qlib_bin"
        shutil.copy2(Path(assets["manifestPath"]), extracted / MANIFEST_NAME)
        provider = validate_installed_provider(extracted, manifest)
        provider["archiveTree"] = verify_provider_tree(extracted, archive_path)
        os.replace(extracted, output_provider)
        provider["providerPath"] = str(output_provider)
        return {"status": "INSTALLED", "assets": assets, "provider": provider}
    finally:
        shutil.rmtree(temporary_root, ignore_errors=True)


def switch_runtime_provider(
    provider: Path,
    dotenv_path: Path = PROJECT_ROOT / ".env",
    qlib_link: Path = Path("~/.qlib/qlib_data/cn_data"),
) -> Dict[str, str]:
    provider = provider.expanduser().resolve()
    if not provider.is_dir():
        raise RuntimeError(f"Provider does not exist: {provider}")
    qlib_link = qlib_link.expanduser()
    if qlib_link.exists() and not qlib_link.is_symlink():
        raise RuntimeError(f"refusing to replace real Qlib directory: {qlib_link}")

    dotenv_path = dotenv_path.expanduser().resolve()
    lines = dotenv_path.read_text(encoding="utf-8").splitlines() if dotenv_path.exists() else []
    replacements = {
        "QLIB_DATA_DIR": str(provider),
        "QLIB_PROVIDER_URI": str(provider),
    }
    found = set()
    updated = []
    for line in lines:
        key = line.split("=", 1)[0].strip() if "=" in line else ""
        if key in replacements:
            updated.append(f"{key}={replacements[key]}")
            found.add(key)
        else:
            updated.append(line)
    for key in replacements:
        if key not in found:
            updated.append(f"{key}={replacements[key]}")
    dotenv_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_env = dotenv_path.with_name(f".{dotenv_path.name}.{os.getpid()}.tmp")
    temporary_env.write_text("\n".join(updated) + "\n", encoding="utf-8")
    os.replace(temporary_env, dotenv_path)

    qlib_link.parent.mkdir(parents=True, exist_ok=True)
    temporary_link = qlib_link.with_name(f".{qlib_link.name}.{os.getpid()}.tmp")
    temporary_link.unlink(missing_ok=True)
    temporary_link.symlink_to(provider, target_is_directory=True)
    os.replace(temporary_link, qlib_link)
    return {
        "dotenvPath": str(dotenv_path),
        "qlibLink": str(qlib_link),
        "providerPath": str(provider),
    }
