"""Verify append-only lineage between immutable Qlib daily Providers."""

from __future__ import annotations

import bisect
import hashlib
import json
import math
import os
import re
import threading
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, Sequence
from zoneinfo import ZoneInfo

import numpy as np

from quantaalpha.utils.qlib_provider import provider_contract_fingerprint


SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
LINEAGE_KIND = "qlib_append_only_provider_lineage"
LINEAGE_VERSION = 1
LINEAGE_ALGORITHM = "calendar-membership-feature-prefix-v1"
DEFAULT_FIELDS = ("open", "high", "low", "close", "vwap", "volume", "factor")
_FEATURE_TREE_CACHE: dict[tuple[str, str], str] = {}
_FEATURE_TREE_CACHE_LOCK = threading.Lock()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_calendar(provider: Path) -> list[str]:
    path = provider / "calendars" / "day.txt"
    if not path.is_file():
        raise FileNotFoundError(f"Provider calendar is missing: {path}")
    dates = [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if not dates or dates != sorted(set(dates)):
        raise ValueError(f"Provider calendar is empty, duplicated, or unsorted: {path}")
    return dates


def _read_intervals(provider: Path, market: str) -> list[tuple[str, str, str]]:
    path = provider / "instruments" / f"{market}.txt"
    if not path.is_file():
        raise FileNotFoundError(f"Provider instrument history is missing: {path}")
    intervals: list[tuple[str, str, str]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) < 3 or not parts[0].strip() or parts[1] > parts[2]:
            raise ValueError(f"Invalid instrument interval at {path}:{line_number}")
        intervals.append((parts[0].strip().upper(), parts[1], parts[2]))
    if not intervals:
        raise ValueError(f"Provider instrument history is empty: {path}")
    return intervals


def _membership_iterator(
    intervals: Iterable[tuple[str, str, str]],
    calendar: Sequence[str],
) -> Iterator[tuple[str, tuple[str, ...]]]:
    additions: dict[int, list[str]] = {}
    removals: dict[int, list[str]] = {}
    for code, start, end in intervals:
        start_index = bisect.bisect_left(calendar, start)
        end_index = bisect.bisect_right(calendar, end)
        if start_index >= len(calendar) or end_index <= 0 or start_index >= end_index:
            continue
        additions.setdefault(max(0, start_index), []).append(code)
        removals.setdefault(min(len(calendar), end_index), []).append(code)

    counts: Counter[str] = Counter()
    for index, date in enumerate(calendar):
        for code in removals.get(index, ()):
            counts[code] -= 1
            if counts[code] <= 0:
                counts.pop(code, None)
        for code in additions.get(index, ()):
            counts[code] += 1
        yield date, tuple(sorted(counts))


def _compare_membership_history(
    parent: Path,
    successor: Path,
    market: str,
    calendar: Sequence[str],
) -> tuple[str, set[str], int]:
    parent_rows = _membership_iterator(_read_intervals(parent, market), calendar)
    successor_rows = _membership_iterator(_read_intervals(successor, market), calendar)
    digest = hashlib.sha256()
    instruments: set[str] = set()
    observations = 0
    for (date, parent_codes), (_, successor_codes) in zip(parent_rows, successor_rows):
        if parent_codes != successor_codes:
            parent_set = set(parent_codes)
            successor_set = set(successor_codes)
            raise ValueError(
                "Provider market membership history changed on "
                f"{date}: removed={sorted(parent_set - successor_set)[:5]}, "
                f"added={sorted(successor_set - parent_set)[:5]}"
            )
        digest.update(date.encode("ascii"))
        digest.update(b"\0")
        digest.update("\n".join(parent_codes).encode("ascii"))
        digest.update(b"\0")
        instruments.update(parent_codes)
        observations += len(parent_codes)
    return digest.hexdigest(), instruments, observations


def _qlib_code(value: str) -> str:
    normalized = value.strip().upper()
    if "." in normalized:
        digits, exchange = normalized.split(".", 1)
        if digits.isdigit() and exchange in {"SH", "SZ", "BJ"}:
            return f"{exchange.lower()}{digits}"
    if normalized[:2] in {"SH", "SZ", "BJ"} and normalized[2:].isdigit():
        return normalized.lower()
    if re.fullmatch(r"[A-Z][A-Z0-9_]+", normalized):
        return normalized.lower()
    raise ValueError(f"Unsupported Qlib instrument code: {value}")


def _aligned_feature_prefix(path: Path, calendar_sessions: int) -> np.ndarray:
    result = np.full(calendar_sessions, np.nan, dtype="<f4")
    if not path.is_file():
        return result
    binary = np.fromfile(path, dtype="<f4")
    if len(binary) < 1 or not math.isfinite(float(binary[0])):
        raise ValueError(f"Invalid Qlib feature file: {path}")
    start_value = float(binary[0])
    start_index = int(start_value)
    if start_index < 0 or float(start_index) != start_value:
        raise ValueError(f"Invalid Qlib feature start index: {path}")
    values = binary[1:]
    overlap_start = min(calendar_sessions, start_index)
    overlap_end = min(calendar_sessions, start_index + len(values))
    if overlap_start < overlap_end:
        result[overlap_start:overlap_end] = values[
            overlap_start - start_index : overlap_end - start_index
        ]
    result[np.isnan(result)] = np.nan
    return result


def _first_difference(
    parent_values: np.ndarray,
    successor_values: np.ndarray,
) -> int | None:
    equal = (parent_values == successor_values) | (
        np.isnan(parent_values) & np.isnan(successor_values)
    )
    mismatches = np.flatnonzero(~equal)
    return int(mismatches[0]) if len(mismatches) else None


def _compare_feature_history(
    parent: Path,
    successor: Path,
    calendar: Sequence[str],
    instruments: Iterable[str],
    fields: Sequence[str],
) -> dict[str, Any]:
    digest = hashlib.sha256()
    checked_files = 0
    both_missing_files = 0
    non_null_values = 0
    for instrument in sorted(set(instruments)):
        code = _qlib_code(instrument)
        for field in fields:
            relative = Path("features") / code / f"{field}.day.bin"
            parent_path = parent / relative
            successor_path = successor / relative
            if not parent_path.exists() and not successor_path.exists():
                both_missing_files += 1
                parent_values = np.full(len(calendar), np.nan, dtype="<f4")
                successor_values = parent_values.copy()
            else:
                parent_values = _aligned_feature_prefix(parent_path, len(calendar))
                successor_values = _aligned_feature_prefix(successor_path, len(calendar))
            difference = _first_difference(parent_values, successor_values)
            if difference is not None:
                raise ValueError(
                    "Provider feature history changed at "
                    f"{relative.as_posix()} on {calendar[difference]}: "
                    f"{float(parent_values[difference])} != "
                    f"{float(successor_values[difference])}"
                )
            digest.update(relative.as_posix().encode("ascii"))
            digest.update(b"\0")
            digest.update(parent_values.tobytes(order="C"))
            checked_files += 1
            non_null_values += int(np.count_nonzero(~np.isnan(parent_values)))
    return {
        "featureHistorySha256": digest.hexdigest(),
        "checkedFeatureFiles": checked_files,
        "bothMissingFeatureFiles": both_missing_files,
        "checkedFeatureCells": checked_files * len(calendar),
        "checkedNonNullFeatureValues": non_null_values,
    }


def _feature_tree_paths(
    provider: Path,
    market: str,
    benchmark: str,
    fields: Sequence[str],
) -> list[tuple[str, Path]]:
    instruments = {code for code, _, _ in _read_intervals(provider, market)}
    instruments.add(_qlib_code(benchmark).upper())
    return [
        (
            (Path("features") / _qlib_code(instrument) / f"{field}.day.bin").as_posix(),
            provider
            / "features"
            / _qlib_code(instrument)
            / f"{field}.day.bin",
        )
        for instrument in sorted(instruments)
        for field in fields
    ]


def _feature_tree_metadata_sha(paths: Sequence[tuple[str, Path]]) -> str:
    digest = hashlib.sha256()
    for relative, path in paths:
        digest.update(relative.encode("ascii"))
        digest.update(b"\0")
        if path.is_file():
            stat = path.stat()
            digest.update(f"{stat.st_size}:{stat.st_mtime_ns}".encode("ascii"))
        else:
            digest.update(b"MISSING")
        digest.update(b"\0")
    return digest.hexdigest()


def provider_feature_tree_contract(
    provider_path: Path,
    *,
    market: str,
    benchmark: str,
    fields: Sequence[str],
) -> dict[str, Any]:
    provider = provider_path.expanduser().resolve()
    paths = _feature_tree_paths(provider, market, benchmark, fields)
    digest = hashlib.sha256()
    present = 0
    missing = 0
    total_bytes = 0
    for relative, path in paths:
        digest.update(relative.encode("ascii"))
        digest.update(b"\0")
        if path.is_file():
            file_sha = sha256_file(path)
            digest.update(file_sha.encode("ascii"))
            present += 1
            total_bytes += path.stat().st_size
        else:
            digest.update(b"MISSING")
            missing += 1
        digest.update(b"\0")
    return {
        "algorithm": "relevant-feature-files-sha256-v1",
        "sha256": digest.hexdigest(),
        "paths": len(paths),
        "presentFiles": present,
        "missingFiles": missing,
        "bytes": total_bytes,
    }


def validate_provider_feature_tree(
    provider_path: Path,
    expected: Mapping[str, Any],
    *,
    market: str,
    benchmark: str,
    fields: Sequence[str],
) -> None:
    provider = provider_path.expanduser().resolve()
    expected_sha = str(expected.get("sha256") or "")
    if (
        expected.get("algorithm") != "relevant-feature-files-sha256-v1"
        or not re.fullmatch(r"[0-9a-f]{64}", expected_sha)
    ):
        raise ValueError("Provider lineage proof lacks a valid feature-tree contract")
    paths = _feature_tree_paths(provider, market, benchmark, fields)
    metadata_sha = _feature_tree_metadata_sha(paths)
    cache_key = (str(provider), expected_sha)
    with _FEATURE_TREE_CACHE_LOCK:
        if _FEATURE_TREE_CACHE.get(cache_key) == metadata_sha:
            return
    actual = provider_feature_tree_contract(
        provider,
        market=market,
        benchmark=benchmark,
        fields=fields,
    )
    if actual != dict(expected):
        raise ValueError("Active Provider feature tree differs from its lineage proof")
    with _FEATURE_TREE_CACHE_LOCK:
        _FEATURE_TREE_CACHE[cache_key] = metadata_sha


def verify_append_only_provider(
    parent_provider: Path,
    successor_provider: Path,
    *,
    market: str = "csi300",
    benchmark: str = "000300.SH",
    fields: Sequence[str] = DEFAULT_FIELDS,
) -> dict[str, Any]:
    """Prove that successor preserves all parent history and only appends sessions."""
    parent = parent_provider.expanduser().resolve()
    successor = successor_provider.expanduser().resolve()
    if parent == successor:
        raise ValueError("Parent and successor Provider paths must differ")
    normalized_fields = tuple(dict.fromkeys(str(field).lower() for field in fields))
    if not normalized_fields:
        raise ValueError("At least one feature field is required")

    parent_calendar = _read_calendar(parent)
    successor_calendar = _read_calendar(successor)
    if len(successor_calendar) <= len(parent_calendar):
        raise ValueError("Successor Provider does not append a new trading session")
    if successor_calendar[: len(parent_calendar)] != parent_calendar:
        difference = next(
            (
                index
                for index, (left, right) in enumerate(
                    zip(parent_calendar, successor_calendar)
                )
                if left != right
            ),
            None,
        )
        detail = (
            f" at index {difference}: {parent_calendar[difference]} != "
            f"{successor_calendar[difference]}"
            if difference is not None
            else ""
        )
        raise ValueError(f"Successor calendar is not an exact parent prefix{detail}")

    parent_contract = provider_contract_fingerprint(parent, market)
    successor_contract = provider_contract_fingerprint(successor, market)
    membership_sha, instruments, membership_observations = _compare_membership_history(
        parent, successor, market, parent_calendar
    )
    instruments.add(_qlib_code(benchmark).upper())
    feature_result = _compare_feature_history(
        parent,
        successor,
        parent_calendar,
        instruments,
        normalized_fields,
    )
    appended = successor_calendar[len(parent_calendar) :]
    successor_feature_tree = provider_feature_tree_contract(
        successor,
        market=market,
        benchmark=benchmark,
        fields=normalized_fields,
    )
    return {
        "version": LINEAGE_VERSION,
        "kind": LINEAGE_KIND,
        "algorithm": LINEAGE_ALGORITHM,
        "status": "PASS",
        "createdAt": datetime.now(SHANGHAI_TZ).isoformat(),
        "market": market,
        "benchmark": benchmark,
        "fields": list(normalized_fields),
        "parentProviderPath": str(parent),
        "successorProviderPath": str(successor),
        "parentFingerprint": parent_contract["fingerprint"],
        "successorFingerprint": successor_contract["fingerprint"],
        "historyCutoff": parent_calendar[-1],
        "successorLatestDate": successor_calendar[-1],
        "parentCalendarSessions": len(parent_calendar),
        "successorCalendarSessions": len(successor_calendar),
        "appendedSessions": len(appended),
        "firstAppendedDate": appended[0],
        "calendarPrefixUnchanged": True,
        "membershipHistoryUnchanged": True,
        "historyUnchanged": True,
        "membershipHistorySha256": membership_sha,
        "checkedMembershipObservations": membership_observations,
        "checkedInstruments": len(instruments),
        "successorFeatureTree": successor_feature_tree,
        **feature_result,
    }


def write_lineage_proof(path: Path, proof: Mapping[str, Any]) -> dict[str, str]:
    """Atomically write an immutable proof and its SHA256 sidecar."""
    destination = path.expanduser().resolve()
    if destination.exists() or destination.with_suffix(destination.suffix + ".sha256").exists():
        raise FileExistsError(f"Refusing to overwrite immutable lineage proof: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(dict(proof), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, destination)
    digest = sha256_file(destination)
    sidecar = destination.with_suffix(destination.suffix + ".sha256")
    temporary_sidecar = sidecar.with_name(f".{sidecar.name}.{os.getpid()}.tmp")
    temporary_sidecar.write_text(digest + "\n", encoding="ascii")
    os.replace(temporary_sidecar, sidecar)
    return {"proofPath": str(destination), "proofSha256": digest}


def validate_lineage_chain(
    proof_dir: Path,
    *,
    root_fingerprint: str,
    successor_fingerprint: str,
    run_id: str,
    declaration_sha256: str,
    successor_provider_path: Path | None = None,
    verify_active_feature_tree: bool = False,
) -> list[dict[str, Any]]:
    """Validate a sidecar-bound proof chain from successor back to the frozen root."""
    directory = proof_dir.expanduser().resolve()
    current = successor_fingerprint
    expected_path = (
        successor_provider_path.expanduser().resolve()
        if successor_provider_path is not None
        else None
    )
    seen: set[str] = set()
    chain: list[dict[str, Any]] = []
    while current != root_fingerprint:
        if current in seen or len(seen) >= 3660:
            raise ValueError("Provider lineage proof chain contains a cycle or is too deep")
        seen.add(current)
        proof_path = directory / f"{current}.json"
        sidecar_path = proof_path.with_suffix(proof_path.suffix + ".sha256")
        if not proof_path.is_file() or not sidecar_path.is_file():
            raise ValueError(
                "Current Provider differs from the frozen Provider and lacks an "
                "append-only lineage proof"
            )
        expected_sha = sidecar_path.read_text(encoding="ascii").strip()
        if expected_sha != sha256_file(proof_path):
            raise ValueError("Provider lineage proof SHA256 sidecar mismatch")
        proof = json.loads(proof_path.read_text(encoding="utf-8"))
        required_fields = set(DEFAULT_FIELDS)
        if (
            proof.get("version") != LINEAGE_VERSION
            or proof.get("kind") != LINEAGE_KIND
            or proof.get("algorithm") != LINEAGE_ALGORITHM
            or proof.get("status") != "PASS"
            or proof.get("successorFingerprint") != current
            or proof.get("rootFingerprint") != root_fingerprint
            or proof.get("runId") != run_id
            or proof.get("declarationSha256") != declaration_sha256
            or proof.get("historyUnchanged") is not True
            or proof.get("calendarPrefixUnchanged") is not True
            or proof.get("membershipHistoryUnchanged") is not True
            or not required_fields.issubset(set(proof.get("fields") or []))
            or int(proof.get("appendedSessions") or 0) <= 0
        ):
            raise ValueError("Provider append-only lineage proof is invalid")
        if expected_path is not None:
            actual_path = Path(str(proof.get("successorProviderPath") or "")).expanduser().resolve()
            if actual_path != expected_path:
                raise ValueError("Provider lineage proof path does not match the active Provider")
            expected_path = None
        parent = str(proof.get("parentFingerprint") or "")
        if not parent or parent == current:
            raise ValueError("Provider lineage proof has an invalid parent fingerprint")
        chain.append(proof)
        current = parent
    if verify_active_feature_tree and chain:
        if successor_provider_path is None:
            raise ValueError("Active Provider path is required for feature-tree validation")
        active_proof = chain[0]
        validate_provider_feature_tree(
            successor_provider_path,
            active_proof.get("successorFeatureTree") or {},
            market=str(active_proof.get("market") or "csi300"),
            benchmark=str(active_proof.get("benchmark") or "000300.SH"),
            fields=tuple(str(field) for field in active_proof.get("fields") or ()),
        )
    return chain
