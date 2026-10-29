"""Conservative inventory of historical periods already consumed by research."""

from __future__ import annotations

import bisect
import csv
import hashlib
import json
import re
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable

import yaml


AUDIT_SCHEMA = "historical_holdout_exposure_audit_v1"
SKIP_PARTS = {".git", "node_modules", "dist", "__pycache__"}
LEGACY_OUTCOME_NAMES = {"ret.pkl", "qlib_res.csv"}
DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")
SEGMENT_PATTERN = re.compile(
    r"(?m)^\s*(train|valid|test)\s*:\s*"
    r"\[\s*['\"]?(\d{4}-\d{2}-\d{2})['\"]?\s*,\s*"
    r"['\"]?(\d{4}-\d{2}-\d{2})['\"]?\s*\]"
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _tree_sha256(entries: Iterable[tuple[str, str]]) -> str:
    digest = hashlib.sha256()
    for relative, file_digest in sorted(entries):
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(file_digest.encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def _date_text(value: Any) -> str | None:
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    text = str(value or "")[:10]
    if not DATE_PATTERN.fullmatch(text):
        return None
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError:
        return None


def _pair(value: Any) -> tuple[str, str] | None:
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        return None
    start, end = _date_text(value[0]), _date_text(value[1])
    if not start or not end or start > end:
        return None
    return start, end


def _extract_config_intervals(payload: Any) -> list[tuple[str, str, str]]:
    intervals: set[tuple[str, str, str]] = set()

    def visit(value: Any, path: tuple[str, ...] = ()) -> None:
        if isinstance(value, dict):
            segments = value.get("segments")
            if isinstance(segments, dict):
                for role in ("train", "valid", "test"):
                    bounds = _pair(segments.get(role))
                    if bounds:
                        intervals.add((role, *bounds))

            path_text = ".".join(path).lower()
            if "backtest" in path_text or path and str(path[-1]).lower() == "backtest":
                start = _date_text(value.get("start_time"))
                end = _date_text(value.get("end_time"))
                if start and end and start <= end:
                    intervals.add(("backtest", start, end))

            for role, start_key, end_key in (
                ("train", "train_start", "train_end"),
                ("valid", "valid_start", "valid_end"),
                ("test", "test_start", "test_end"),
                ("selection", "validation_start", "validation_end"),
                ("selection", "selection_start", "selection_end"),
                ("backtest", "backtest_start", "backtest_end"),
            ):
                start = _date_text(value.get(start_key))
                end = _date_text(value.get(end_key))
                if start and end and start <= end:
                    intervals.add((role, start, end))

            for key, nested in value.items():
                visit(nested, (*path, str(key)))
        elif isinstance(value, list):
            for index, nested in enumerate(value):
                visit(nested, (*path, str(index)))

    visit(payload)
    return sorted(intervals)


def _extract_intervals_from_text(text: str) -> list[tuple[str, str, str]]:
    return sorted(
        {
            (match.group(1), match.group(2), match.group(3))
            for match in SEGMENT_PATTERN.finditer(text)
        }
    )


def _relative(path: Path, project_root: Path) -> str:
    return str(path.resolve().relative_to(project_root.resolve()))


def _iter_files(root: Path, patterns: tuple[str, ...]) -> Iterable[Path]:
    seen: set[Path] = set()
    for pattern in patterns:
        for path in root.rglob(pattern):
            if path in seen or any(part in SKIP_PARTS for part in path.parts):
                continue
            seen.add(path)
            yield path


def _return_date_bounds(path: Path) -> tuple[str, str, int]:
    dates: list[str] = []
    with path.open("r", encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        if "date" not in (reader.fieldnames or []):
            raise ValueError("return CSV has no date column")
        for row_number, row in enumerate(reader, start=2):
            current = _date_text(row.get("date"))
            if not current:
                raise ValueError(f"invalid date at row {row_number}")
            if dates and current <= dates[-1]:
                raise ValueError(f"dates are duplicated or unsorted at row {row_number}")
            dates.append(current)
    if not dates:
        raise ValueError("return CSV is empty")
    return dates[0], dates[-1], len(dates)


def _calendar_runs(calendar: list[str], mask: list[bool]) -> list[dict[str, Any]]:
    runs: list[dict[str, Any]] = []
    start: int | None = None
    for index, included in enumerate([*mask, False]):
        if included and start is None:
            start = index
        elif not included and start is not None:
            end = index - 1
            runs.append(
                {
                    "start": calendar[start],
                    "end": calendar[end],
                    "sessions": end - start + 1,
                    "startIndex": start,
                }
            )
            start = None
    return runs


def collect_holdout_exposure(
    project_root: Path,
    provider: Path,
    *,
    minimum_training_sessions: int = 504,
    minimum_holdout_sessions: int = 1008,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    project_root = project_root.resolve()
    provider = provider.resolve()
    config_roots = [project_root / "configs", project_root / "data" / "results"]
    source_rows: list[dict[str, Any]] = []
    issues: list[dict[str, Any]] = []
    config_hashes: list[tuple[str, str]] = []
    return_hashes: list[tuple[str, str]] = []
    legacy_hashes: list[tuple[str, str]] = []
    exposure_directories: set[Path] = set()
    config_files = 0
    config_fallback_files = 0

    for root in config_roots:
        if not root.exists():
            continue
        for path in sorted(_iter_files(root, ("*.yaml", "*.yml"))):
            config_files += 1
            relative = _relative(path, project_root)
            digest = _sha256(path)
            config_hashes.append((relative, digest))
            text = path.read_text(encoding="utf-8", errors="replace")
            parse_mode = "yaml"
            try:
                intervals = _extract_config_intervals(yaml.safe_load(text))
            except yaml.YAMLError:
                intervals = _extract_intervals_from_text(text)
                parse_mode = "regex_fallback"
                config_fallback_files += 1
            if intervals:
                exposure_directories.add(path.parent.resolve())
            elif parse_mode == "regex_fallback":
                issues.append(
                    {
                        "severity": "BLOCKING",
                        "code": "CONFIG_RANGE_UNRESOLVED",
                        "path": relative,
                        "message": "malformed YAML has no recoverable train/valid/test segment",
                    }
                )
            for role, start, end in intervals:
                source_rows.append(
                    {
                        "kind": "declared_config",
                        "role": role,
                        "start": start,
                        "end": end,
                        "sessions": "",
                        "sourcePath": relative,
                        "sourceSha256": digest,
                        "parseMode": parse_mode,
                    }
                )

    results_root = project_root / "data" / "results"
    return_files = sorted(_iter_files(results_root, ("*_cumulative_excess.csv",)))
    for path in return_files:
        relative = _relative(path, project_root)
        digest = _sha256(path)
        return_hashes.append((relative, digest))
        try:
            start, end, sessions = _return_date_bounds(path)
        except ValueError as exc:
            issues.append(
                {
                    "severity": "BLOCKING",
                    "code": "RETURN_RANGE_UNRESOLVED",
                    "path": relative,
                    "message": str(exc),
                }
            )
            continue
        source_rows.append(
            {
                "kind": "revealed_return",
                "role": "test",
                "start": start,
                "end": end,
                "sessions": sessions,
                "sourcePath": relative,
                "sourceSha256": digest,
                "parseMode": "csv_date_column",
            }
        )

    legacy_files = sorted(
        path
        for path in _iter_files(results_root, tuple(LEGACY_OUTCOME_NAMES))
        if path.name in LEGACY_OUTCOME_NAMES
    )
    for path in legacy_files:
        relative = _relative(path, project_root)
        digest = _sha256(path)
        legacy_hashes.append((relative, digest))
        current = path.parent.resolve()
        mapped = False
        while current != results_root.parent and current != current.parent:
            if current in exposure_directories:
                mapped = True
                break
            if current == results_root.resolve():
                break
            current = current.parent
        if not mapped:
            issues.append(
                {
                    "severity": "BLOCKING",
                    "code": "LEGACY_OUTCOME_RANGE_UNRESOLVED",
                    "path": relative,
                    "message": "legacy outcome has no adjacent declared segment",
                }
            )

    calendar_path = provider / "calendars" / "day.txt"
    instrument_path = provider / "instruments" / "csi300.txt"
    calendar = [
        line.strip()
        for line in calendar_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    membership_starts = [
        parts[1]
        for line in instrument_path.read_text(encoding="utf-8").splitlines()
        if len(parts := line.split("\t")) >= 3
    ]
    if not calendar or not membership_starts:
        raise ValueError("provider calendar or CSI300 membership is empty")
    eligible_start = min(membership_starts)
    eligible_calendar = [value for value in calendar if value >= eligible_start]
    exposed = [False] * len(eligible_calendar)
    for row in source_rows:
        left = bisect.bisect_left(eligible_calendar, row["start"])
        right = bisect.bisect_right(eligible_calendar, row["end"])
        for index in range(left, right):
            exposed[index] = True

    exposed_runs = _calendar_runs(eligible_calendar, exposed)
    unexposed_runs = _calendar_runs(eligible_calendar, [not value for value in exposed])
    qualifying: list[dict[str, Any]] = []
    for run in unexposed_runs:
        candidate = {
            **{key: value for key, value in run.items() if key != "startIndex"},
            "priorEligibleSessions": run["startIndex"],
            "trainingSessionsRequired": minimum_training_sessions,
            "holdoutSessionsRequired": minimum_holdout_sessions,
        }
        candidate["qualifies"] = bool(
            candidate["priorEligibleSessions"] >= minimum_training_sessions
            and candidate["sessions"] >= minimum_holdout_sessions
        )
        if candidate["qualifies"]:
            qualifying.append(candidate)
        run.clear()
        run.update(candidate)

    blocking = [item for item in issues if item["severity"] == "BLOCKING"]
    if blocking:
        status = "INDETERMINATE"
    elif qualifying:
        status = "HISTORICAL_HOLDOUT_AVAILABLE"
    else:
        status = "NO_DEFENSIBLE_HISTORICAL_HOLDOUT"

    inventory = {
        "configFiles": config_files,
        "configRegexFallbackFiles": config_fallback_files,
        "returnFiles": len(return_files),
        "legacyOutcomeFiles": len(legacy_files),
        "configTreeSha256": _tree_sha256(config_hashes),
        "returnTreeSha256": _tree_sha256(return_hashes),
        "legacyOutcomeTreeSha256": _tree_sha256(legacy_hashes),
    }
    payload: dict[str, Any] = {
        "schemaVersion": AUDIT_SCHEMA,
        "status": status,
        "provider": {
            "path": _relative(provider, project_root),
            "calendarSha256": _sha256(calendar_path),
            "csi300Sha256": _sha256(instrument_path),
            "calendarStart": calendar[0],
            "calendarEnd": calendar[-1],
            "csi300Start": eligible_start,
            "eligibleSessions": len(eligible_calendar),
        },
        "thresholds": {
            "minimumTrainingSessions": minimum_training_sessions,
            "minimumHoldoutSessions": minimum_holdout_sessions,
        },
        "inventory": inventory,
        "exposureSources": len(source_rows),
        "exposedIntervals": exposed_runs,
        "unexposedIntervals": unexposed_runs,
        "qualifyingHistoricalHoldouts": qualifying,
        "cleanForwardBoundaryExclusive": calendar[-1],
        "blockingIssues": len(blocking),
        "limitations": [
            "This inventory cannot prove the absence of deleted, external, or unrecorded experiments.",
            "A historical gap is eligible only when it also has enough prior CSI300 sessions for training.",
            "Future sessions strictly after the active Provider remain the strongest independent evidence.",
        ],
        "sourceCodeSha256": _sha256(Path(__file__)),
    }
    payload["auditId"] = hashlib.sha256(
        _canonical_json(payload).encode("utf-8")
    ).hexdigest()
    return payload, source_rows, issues


def write_holdout_exposure_audit(
    project_root: Path,
    provider: Path,
    output_dir: Path,
    *,
    minimum_training_sessions: int = 504,
    minimum_holdout_sessions: int = 1008,
) -> dict[str, Any]:
    output_dir = output_dir.resolve()
    if output_dir.exists():
        raise FileExistsError(f"audit output already exists: {output_dir}")
    payload, source_rows, issues = collect_holdout_exposure(
        project_root,
        provider,
        minimum_training_sessions=minimum_training_sessions,
        minimum_holdout_sessions=minimum_holdout_sessions,
    )
    output_dir.mkdir(parents=True)

    summary_path = output_dir / "summary.json"
    summary_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    summary_sha = _sha256(summary_path)
    (output_dir / "summary.sha256").write_text(
        f"{summary_sha}  summary.json\n", encoding="ascii"
    )

    source_fields = [
        "kind",
        "role",
        "start",
        "end",
        "sessions",
        "sourcePath",
        "sourceSha256",
        "parseMode",
    ]
    with (output_dir / "exposure_sources.csv").open(
        "w", encoding="utf-8", newline=""
    ) as stream:
        writer = csv.DictWriter(stream, fieldnames=source_fields)
        writer.writeheader()
        writer.writerows(sorted(source_rows, key=lambda row: tuple(str(row[k]) for k in source_fields)))

    issue_fields = ["severity", "code", "path", "message"]
    with (output_dir / "issues.csv").open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=issue_fields)
        writer.writeheader()
        writer.writerows(issues)

    lines = [
        "# Historical holdout exposure audit",
        "",
        f"- Status: `{payload['status']}`",
        f"- Audit ID: `{payload['auditId']}`",
        f"- Provider calendar: `{payload['provider']['calendarStart']}` to `{payload['provider']['calendarEnd']}`",
        f"- CSI300 history starts: `{payload['provider']['csi300Start']}`",
        f"- Declared/result exposure sources: `{payload['exposureSources']}`",
        f"- Parsed cumulative-return files: `{payload['inventory']['returnFiles']}`",
        f"- Legacy outcome files mapped to configs: `{payload['inventory']['legacyOutcomeFiles']}`",
        f"- Blocking issues: `{payload['blockingIssues']}`",
        "",
        "## Unexposed intervals",
        "",
    ]
    for item in payload["unexposedIntervals"]:
        lines.append(
            f"- `{item['start']}` to `{item['end']}`: {item['sessions']} sessions; "
            f"prior CSI300 sessions {item['priorEligibleSessions']}; qualifies `{str(item['qualifies']).lower()}`"
        )
    lines.extend(
        [
            "",
            "No historical interval may be called independent unless it appears in "
            "`qualifyingHistoricalHoldouts`. Sessions after the active Provider are not "
            "retroactive evidence and must be collected prospectively.",
        ]
    )
    (output_dir / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {
        "outputDir": str(output_dir),
        "status": payload["status"],
        "auditId": payload["auditId"],
        "summarySha256": summary_sha,
    }
