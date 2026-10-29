#!/usr/bin/env python3
"""Derive an isolated Provider using one observed official CSI300 membership list.

This changes membership only on the list's as-of date, never infers an earlier
effective date, and never changes the supplied base Provider.
"""
from __future__ import annotations

import argparse
from datetime import date, datetime
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import struct
import tempfile
from typing import Any
from zoneinfo import ZoneInfo


SOURCE_URL = "https://oss-ch.csindex.com.cn/static/html/csindex/public/uploads/file/autofile/cons/000300cons.xls"
EXPECTED_XLS_SHA256 = "3416697f85f6842629fdeacea38f082e5d99b92f63f3a0ec34b235e43a657d89"
MANIFEST_NAME = "quantaalpha_extension.manifest.json"
REQUIRED_FIELDS = ("open", "high", "low", "close", "vwap", "volume", "factor")
DATE_FIELD = "日期Date"
INDEX_FIELD = "指数代码 Index Code"
CODE_FIELD = "成份券代码Constituent Code"
EXCHANGE_FIELD = "交易所Exchange"
ENGLISH_EXCHANGE_FIELD = "交易所英文名称Exchange(Eng)"
EXCHANGES = {
    "上海证券交易所": "SH", "Shanghai Stock Exchange": "SH",
    "深圳证券交易所": "SZ", "Shenzhen Stock Exchange": "SZ",
}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _iso_date(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("membership dates must be explicit text dates")
    if re.fullmatch(r"\d{8}", value):
        value = f"{value[:4]}-{value[4:6]}-{value[6:]}"
    return date.fromisoformat(value).isoformat()


def read_membership(path: Path) -> tuple[str, list[str]]:
    records = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(records, list) or len(records) != 300:
        raise ValueError("official CSI300 records must contain exactly 300 rows")
    dates: set[str] = set()
    symbols: list[str] = []
    for record in records:
        if not isinstance(record, dict) or record.get(INDEX_FIELD) != "000300":
            raise ValueError("every record must explicitly identify index 000300")
        dates.add(_iso_date(record.get(DATE_FIELD)))
        code = record.get(CODE_FIELD)
        if not isinstance(code, str) or not re.fullmatch(r"\d{6}", code):
            raise ValueError("constituent codes must be explicit six-digit strings")
        supplied = [record.get(field) for field in (EXCHANGE_FIELD, ENGLISH_EXCHANGE_FIELD)
                    if record.get(field)]
        exchanges = {EXCHANGES.get(value) for value in supplied}
        if not supplied or None in exchanges or len(exchanges) != 1:
            raise ValueError("every constituent must have one explicit supported exchange")
        symbols.append(f"{next(iter(exchanges))}{code}")
    if len(dates) != 1 or len(set(symbols)) != 300:
        raise ValueError("membership dates must agree and 300 constituents must be unique")
    return next(iter(dates)), sorted(symbols)


def _read_xls_records(path: Path) -> list[dict[str, Any]]:
    try:
        import pandas as pd
        return pd.read_excel(path, dtype=str).to_dict(orient="records")
    except ImportError:
        raise ValueError("official XLS re-parsing requires pandas and xlrd in the current Python environment") from None
    except Exception:
        raise ValueError("official XLS could not be parsed; membership records were not verified") from None


def _tree_hashes(root: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ValueError("base Provider must not contain symbolic links")
        if path.is_file():
            result[path.relative_to(root).as_posix()] = sha256_file(path)
    return result


def _contract_hash(values: dict[str, str]) -> str:
    return hashlib.sha256(json.dumps(values, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _validate_features(source: Path, symbols: list[str], date_index: int) -> None:
    for symbol in symbols:
        for field in REQUIRED_FIELDS:
            path = source / "features" / symbol.lower() / f"{field}.day.bin"
            if not path.is_file() or path.stat().st_size < 8 or path.stat().st_size % 4:
                raise ValueError(f"required feature is missing or malformed: {symbol}/{field}")
            with path.open("rb") as stream:
                start = struct.unpack("<f", stream.read(4))[0]
                if not math.isfinite(start) or start < 0 or not start.is_integer():
                    raise ValueError(f"invalid feature start index: {symbol}/{field}")
                offset = date_index - int(start)
                if offset < 0 or offset >= path.stat().st_size // 4 - 1:
                    raise ValueError(f"feature does not cover as-of date: {symbol}/{field}")
                stream.seek(4 * (offset + 1))
                value = struct.unpack("<f", stream.read(4))[0]
            if not math.isfinite(value) or (value < 0 if field == "volume" else value <= 0):
                raise ValueError(f"feature is unusable on as-of date: {symbol}/{field}")


def _benchmark_coverage(source: Path, calendar: list[str]) -> dict[str, Any]:
    """Report inherited benchmark coverage; membership derivation does not repair it."""
    ends: list[int] = []
    missing: list[str] = []
    for field in REQUIRED_FIELDS:
        path = source / "features/sh000300" / f"{field}.day.bin"
        if not path.is_file() or path.stat().st_size < 8 or path.stat().st_size % 4:
            missing.append(field)
            continue
        with path.open("rb") as stream:
            start = struct.unpack("<f", stream.read(4))[0]
        if not math.isfinite(start) or start < 0 or not start.is_integer():
            missing.append(field)
            continue
        ends.append(int(start) + path.stat().st_size // 4 - 2)
    end = min(ends) if ends and not missing else -1
    return {"symbol": "SH000300", "missingOrMalformedFields": missing,
            "allRequiredFieldsCoverAsOf": not missing and end >= len(calendar) - 1,
            "latestCoveredDate": calendar[min(end, len(calendar) - 1)] if end >= 0 else None,
            "featureValuesModified": False,
            "scope": "Coverage only; does not validate benchmark values or establish full Provider business eligibility."}


def build_current_membership_provider(
    source: Path | str,
    destination: Path | str,
    membership: Path | str,
    xls: Path | str,
    *,
    expected_xls_sha256: str = EXPECTED_XLS_SHA256,
) -> dict[str, Any]:
    source = Path(source).expanduser().resolve()
    destination = Path(destination).expanduser().absolute()
    membership = Path(membership).expanduser().resolve()
    xls = Path(xls).expanduser().resolve()
    if not source.is_dir() or os.path.lexists(destination):
        raise ValueError("base must exist and destination must be a new path")
    if source == destination.resolve() or source in destination.resolve().parents:
        raise ValueError("destination cannot be inside the base Provider")
    if (source / MANIFEST_NAME).exists():
        raise ValueError("use an unextended base; existing extension provenance cannot be overwritten")
    if not re.fullmatch(r"[0-9a-f]{64}", expected_xls_sha256):
        raise ValueError("expected XLS SHA256 must be an explicit lowercase digest")
    if sha256_file(xls) != expected_xls_sha256:
        raise ValueError("official XLS SHA256 mismatch")
    records_hash = sha256_file(membership)
    as_of, symbols = read_membership(membership)
    if _read_xls_records(xls) != json.loads(membership.read_text(encoding="utf-8")):
        raise ValueError("parsed membership JSON does not exactly match the official XLS records")
    calendar_path = source / "calendars/day.txt"
    calendar = calendar_path.read_text(encoding="utf-8").splitlines()
    if len(calendar) < 2 or calendar != sorted(set(calendar)):
        raise ValueError("base calendar must be sorted, unique, and have a prior trading date")
    if any(_iso_date(day) != day for day in calendar) or calendar[-1] != as_of:
        raise ValueError("membership as-of must equal the base Provider's latest trading date")
    previous_date = calendar[-2]
    instrument_path = source / "instruments/csi300.txt"
    output_lines: list[str] = []
    active: list[str] = []
    for line in instrument_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        if (len(parts) != 3 or not re.fullmatch(r"(?:SH|SZ)[A-Z0-9]{6}", parts[0])
                or _iso_date(parts[1]) != parts[1] or _iso_date(parts[2]) != parts[2]
                or parts[1] > parts[2] or parts[2] > as_of):
            raise ValueError("invalid or forward-dated base membership interval")
        if parts[1] <= as_of <= parts[2]:
            if not re.fullmatch(r"(?:SH|SZ)\d{6}", parts[0]):
                raise ValueError("active constituent codes must contain six digits")
            active.append(parts[0])
            if parts[1] <= previous_date:
                output_lines.append(f"{parts[0]}\t{parts[1]}\t{previous_date}")
        else:
            output_lines.append(line)
    if len(active) != 300 or len(set(active)) != 300:
        raise ValueError("base Provider must have 300 unambiguous active constituents")
    _validate_features(source, symbols, len(calendar) - 1)
    source_tree = _tree_hashes(source)
    output_lines.extend(f"{symbol}\t{as_of}\t{as_of}" for symbol in symbols)
    output = ("\n".join(output_lines) + "\n").encode("utf-8")
    release_path = source / "qlib_bin.manifest.json"
    release = json.loads(release_path.read_text(encoding="utf-8")) if release_path.exists() else None
    manifest: dict[str, Any] = {
        "schemaVersion": 1, "kind": "official_index_membership_overlay",
        "createdAt": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(),
        "sourceProvider": str(source), "sourceTreeSha256": _contract_hash(source_tree),
        "sourceFileCount": len(source_tree),
        "baseReleaseArchive": release,
        "releaseArchiveScope": "The preserved qlib_bin.manifest.json describes only the unmodified base archive, not this derived Provider tree.",
        "derivedProviderRequiresOverlayManifest": True,
        "inheritedBenchmarkCoverage": _benchmark_coverage(source, calendar),
        "fullProviderBusinessEligibility": "NOT_ESTABLISHED_BY_MEMBERSHIP_DERIVATION",
        "membership": {
            "indexCode": "000300", "asOfDate": as_of, "constituentCount": 300,
            "sourceUrl": SOURCE_URL, "xlsPath": str(xls), "xlsSha256": expected_xls_sha256,
            "recordsPath": str(membership), "recordsSha256": records_hash,
            "recordsExtraction": "Builder independently re-parsed the hash-verified XLS with pandas.read_excel(dtype=str) and compared every record exactly with the supplied JSON.",
            "recordsExtractionVerifiedByBuilder": True,
            "observedAsOfOnly": True, "effectiveChangeDateVerified": False,
            "earlierMembershipChangesInferred": False,
        },
        "changedMembershipDateOnly": as_of, "historyPreservedThrough": previous_date,
        "removedAtAsOf": sorted(set(active) - set(symbols)),
        "addedAtAsOf": sorted(set(symbols) - set(active)),
        "sourceInstrumentsSha256": source_tree["instruments/csi300.txt"],
        "derivedInstrumentsSha256": hashlib.sha256(output).hexdigest(),
        "featureValuesModified": False, "sourceProviderModified": False,
        "profitEvidenceEligible": False,
    }
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f".{destination.name}.building-", dir=destination.parent))
    try:
        shutil.copytree(source, temporary, dirs_exist_ok=True)
        temporary.chmod(temporary.stat().st_mode | 0o700)
        target = temporary / "instruments/csi300.txt"
        target.chmod(0o644)
        target.write_bytes(output)
        target.chmod(0o444)
        (temporary / MANIFEST_NAME).write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        (temporary / MANIFEST_NAME).chmod(0o444)
        copied_tree = _tree_hashes(temporary)
        unchanged = {k: v for k, v in copied_tree.items() if k not in {"instruments/csi300.txt", MANIFEST_NAME}}
        if unchanged != {k: v for k, v in source_tree.items() if k != "instruments/csi300.txt"}:
            raise ValueError("copied Provider changed files outside membership and provenance")
        if _tree_hashes(source) != source_tree:
            raise ValueError("base Provider changed during construction")
        if sha256_file(membership) != records_hash or sha256_file(xls) != expected_xls_sha256:
            raise ValueError("membership evidence changed during construction")
        if os.path.lexists(destination):
            raise ValueError("destination appeared during construction; refusing overwrite")
        os.rename(temporary, destination)
    finally:
        if temporary.exists():
            temporary.chmod(temporary.stat().st_mode | 0o700)
            for path in temporary.rglob("*"):
                if path.is_dir() and not path.is_symlink():
                    path.chmod(path.stat().st_mode | 0o700)
            shutil.rmtree(temporary)
    return {"status": "BUILT", "providerPath": str(destination), "manifest": manifest}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--destination", required=True, type=Path)
    parser.add_argument("--membership", required=True, type=Path)
    parser.add_argument("--xls", required=True, type=Path)
    parser.add_argument("--xls-sha256", default=EXPECTED_XLS_SHA256)
    args = parser.parse_args()
    result = build_current_membership_provider(args.source, args.destination, args.membership,
                                               args.xls, expected_xls_sha256=args.xls_sha256)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
