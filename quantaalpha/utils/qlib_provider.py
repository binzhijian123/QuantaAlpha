"""Select a Qlib provider that covers a runtime configuration's dates."""

import hashlib
import json
import os
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, Iterable, Optional, Sequence, Tuple


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DOTENV_PATH = PROJECT_ROOT / ".env"


def _load_dotenv_dict() -> Dict[str, str]:
    values: Dict[str, str] = {}
    if not DOTENV_PATH.exists():
        return values
    for line in DOTENV_PATH.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def _expand_path(path: str) -> Path:
    return Path(os.path.expandvars(path)).expanduser()


def _configured_provider(config: Dict[str, Any]) -> Optional[Path]:
    configured = (config.get("data") or {}).get("provider_uri")
    if configured:
        return _expand_path(str(configured))
    configured = (config.get("qlib_init") or {}).get("provider_uri")
    return _expand_path(str(configured)) if configured else None


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _daily_extension_feature_contract(
    provider: Path,
    extension: Dict[str, Any],
) -> Dict[str, Any]:
    scope = extension.get("scope") or {}
    market = str(scope.get("market") or "")
    extension_end = str(scope.get("extensionCalendarEnd") or "")
    benchmark = str(scope.get("benchmark") or "").upper()
    fields = [str(value) for value in extension.get("featureFields", []) if value]
    expected = extension.get("featureTree") or {}
    if not market or not extension_end or not benchmark or not fields:
        raise RuntimeError("frozen daily extension manifest has an incomplete feature contract")
    instrument_path = provider / "instruments" / f"{market}.txt"
    codes = set()
    for line in instrument_path.read_text(encoding="utf-8").splitlines():
        parts = line.split("\t")
        if len(parts) >= 3 and parts[1] <= extension_end <= parts[2]:
            codes.add(parts[0].strip().lower())
    codes.add(benchmark.lower())
    entries = []
    for code in sorted(codes):
        for field in fields:
            path = provider / "features" / code / f"{field}.day.bin"
            if not path.exists():
                raise RuntimeError(f"frozen daily extension feature is missing: {path}")
            entries.append((f"features/{code}/{field}.day.bin", _sha256_file(path)))
    digest = hashlib.sha256()
    for relative, file_digest in sorted(entries):
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(file_digest.encode("ascii"))
    actual_tree = digest.hexdigest()
    if int(expected.get("files", -1)) != len(entries):
        raise RuntimeError("frozen daily extension feature count does not match its manifest")
    if expected.get("outputTreeSha256") != actual_tree:
        raise RuntimeError("frozen daily extension feature tree does not match its manifest")
    all_path = provider / "instruments" / "all.txt"
    expected_all = scope.get("allInstrumentSha256")
    if expected_all and _sha256_file(all_path) != expected_all:
        raise RuntimeError("frozen daily extension all-market contract was modified")
    return {
        "files": len(entries),
        "outputTreeSha256": actual_tree,
        "allInstrumentSha256": _sha256_file(all_path),
    }


def calendar_bounds(provider_uri: Path) -> Optional[Tuple[str, str, int]]:
    calendar_path = provider_uri / "calendars" / "day.txt"
    if not calendar_path.exists():
        return None
    dates = [line.strip() for line in calendar_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if not dates:
        return None
    return dates[0], dates[-1], len(dates)


def _iter_date_values(value: Any) -> Iterable[str]:
    if isinstance(value, dict):
        for nested in value.values():
            yield from _iter_date_values(nested)
    elif isinstance(value, (list, tuple)):
        for nested in value:
            yield from _iter_date_values(nested)
    elif isinstance(value, datetime):
        yield value.date().isoformat()
    elif isinstance(value, date):
        yield value.isoformat()
    elif isinstance(value, str) and len(value) >= 10 and value[:4].isdigit() and value[4] == "-":
        yield value[:10]


def required_date_bounds(config: Dict[str, Any]) -> Optional[Tuple[str, str]]:
    date_values = list(_iter_date_values(config))
    if not date_values:
        return None
    return min(date_values), max(date_values)


def candidate_provider_paths(config: Dict[str, Any]) -> list[Path]:
    dotenv = _load_dotenv_dict()
    candidates: list[str] = []
    for key in ("QLIB_DATA_DIR", "QLIB_PROVIDER_URI"):
        if os.environ.get(key):
            candidates.append(os.environ[key])
        if dotenv.get(key):
            candidates.append(dotenv[key])

    configured = (config.get("qlib_init") or {}).get("provider_uri")
    if configured:
        candidates.append(str(configured))
    configured = (config.get("data") or {}).get("provider_uri")
    if configured:
        candidates.append(str(configured))

    candidates.append(str(PROJECT_ROOT / "cn_data"))
    candidates.append("~/.qlib/qlib_data/cn_data")

    paths: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        path = _expand_path(candidate)
        key = str(path)
        if key not in seen:
            seen.add(key)
            paths.append(path)
    return paths


def provider_contract_fingerprint(
    provider_uri: Path,
    market: str = "csi300",
) -> Dict[str, Any]:
    """Fingerprint immutable provider provenance and the point-in-time universe."""
    provider = provider_uri.expanduser().resolve()
    paths = {
        "calendar": provider / "calendars" / "day.txt",
        "instruments": provider / "instruments" / f"{market}.txt",
        "releaseManifest": provider / "qlib_bin.manifest.json",
        "extensionManifest": provider / "quantaalpha_extension.manifest.json",
    }
    digest = hashlib.sha256()
    files: Dict[str, Dict[str, Any]] = {}
    for label, path in paths.items():
        if not path.exists():
            if label not in {"releaseManifest", "extensionManifest"}:
                raise FileNotFoundError(f"provider contract file is missing: {path}")
            continue
        content = path.read_bytes()
        file_digest = hashlib.sha256(content).hexdigest()
        digest.update(label.encode("utf-8"))
        digest.update(b"\0")
        digest.update(file_digest.encode("ascii"))
        files[label] = {
            "path": str(path),
            "sha256": file_digest,
            "size": len(content),
        }

    release = None
    manifest_path = paths["releaseManifest"]
    if manifest_path.exists():
        release = json.loads(manifest_path.read_text(encoding="utf-8"))
        archive_digest = str(release.get("archive_sha256", "")).removeprefix("sha256:")
        if archive_digest:
            digest.update(b"\0archive\0")
            digest.update(archive_digest.encode("ascii"))

    extension = None
    daily_feature_contract = None
    extension_manifest_path = paths["extensionManifest"]
    if extension_manifest_path.exists():
        extension = json.loads(extension_manifest_path.read_text(encoding="utf-8"))
        if extension.get("kind") == "frozen_market_daily_prices":
            daily_feature_contract = _daily_extension_feature_contract(provider, extension)
            digest.update(b"\0daily-feature-tree\0")
            digest.update(daily_feature_contract["outputTreeSha256"].encode("ascii"))

    return {
        "fingerprint": digest.hexdigest(),
        "market": market,
        "providerPath": str(provider),
        "files": files,
        "release": release,
        "extension": extension,
        "dailyFeatureContract": daily_feature_contract,
        "strength": (
            "release_archive_contract_and_extension"
            if release and release.get("archive_sha256") and extension
            else "release_archive_and_contract"
            if release and release.get("archive_sha256")
            else "contract_and_extension"
            if extension
            else "contract_files_only"
        ),
    }


def provider_scope_violation(
    provider_uri: Path,
    config: Dict[str, Any],
    required: Optional[Tuple[str, str]] = None,
) -> Optional[str]:
    """Return a reason when a scoped extension cannot satisfy the requested market."""
    manifest_path = provider_uri.expanduser() / "quantaalpha_extension.manifest.json"
    if not manifest_path.exists():
        return None
    try:
        extension = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return f"invalid extension manifest: {exc}"
    if extension.get("kind") != "frozen_market_daily_prices":
        return None
    scope = extension.get("scope") or {}
    base_end = str(scope.get("baseCalendarEnd") or "")
    if not required or not base_end or required[1] <= base_end:
        return None
    data = config.get("data") or {}
    qlib_init = config.get("qlib_init") or {}
    requested_market = data.get("market") or qlib_init.get("market") or config.get("market")
    if not requested_market:
        return (
            "market must be declared when a frozen daily extension is requested "
            f"after {base_end}"
        )
    allowed_markets = {
        str(value).lower() for value in scope.get("allowedMarkets", []) if value
    }
    if str(requested_market).lower() not in allowed_markets:
        return (
            f"market {requested_market} is outside frozen extension scope "
            f"{sorted(allowed_markets)} after {base_end}"
        )
    return None


def select_provider_for_config(
    config: Dict[str, Any],
    config_name: str,
    candidates: Optional[Sequence[Path]] = None,
) -> Path:
    """Return the first configured provider that fully covers all config dates."""
    required = required_date_bounds(config)
    inspected: list[str] = []
    configured = _configured_provider(config)
    strict = bool(
        (config.get("data") or {}).get("provider_strict")
        or (config.get("qlib_init") or {}).get("provider_strict")
    )
    if strict:
        if configured is None:
            raise RuntimeError(
                f"{config_name} enables provider_strict but does not configure provider_uri"
            )
        providers: Sequence[Path] = [configured]
    else:
        providers = candidates or candidate_provider_paths(config)

    for provider in providers:
        provider = provider.expanduser()
        bounds = calendar_bounds(provider)
        if bounds is None:
            inspected.append(f"{provider} (missing calendars/day.txt)")
            continue

        start, end, count = bounds
        scope_violation = provider_scope_violation(provider, config, required)
        if scope_violation:
            inspected.append(
                f"{provider} ({start}..{end}, {count} days; {scope_violation})"
            )
            continue
        inspected.append(f"{provider} ({start}..{end}, {count} days)")
        if required is None or (start <= required[0] and end >= required[1]):
            return provider

    required_text = "unknown" if required is None else f"{required[0]}..{required[1]}"
    raise RuntimeError(
        f"No Qlib provider covers {config_name} required date range {required_text}. "
        f"Checked: {'; '.join(inspected)}. "
        "Refresh a provider with enough historical data or adjust the backtest config date segments."
    )
