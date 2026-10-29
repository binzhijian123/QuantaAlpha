"""Read-only temporal health summaries for immutable intraday capture slots."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Sequence
from zoneinfo import ZoneInfo

from .intraday_capture import list_verified_slot_records


CHINA_TZ = ZoneInfo("Asia/Shanghai")
DEFAULT_CONTINUOUS_WINDOWS = (
    ("09:30:00", "11:30:00"),
    ("13:00:00", "15:00:00"),
)
CRITICAL_SLOT_TIMES = {
    "decision": "14:50:00",
    "execution": "14:55:00",
}


def _parse_time(value: str) -> time:
    return time.fromisoformat(value)


def _expected_slots(
    trade_date: str,
    poll_seconds: int,
    windows: Sequence[tuple[str, str]],
) -> list[datetime]:
    if poll_seconds <= 0:
        raise ValueError("intraday temporal health poll_seconds must be positive")
    day = date.fromisoformat(trade_date)
    slots: list[datetime] = []
    for start_text, end_text in windows:
        cursor = datetime.combine(day, _parse_time(start_text), tzinfo=CHINA_TZ)
        end = datetime.combine(day, _parse_time(end_text), tzinfo=CHINA_TZ)
        if end < cursor:
            raise ValueError("intraday temporal health window end precedes start")
        while cursor <= end:
            slots.append(cursor)
            cursor += timedelta(seconds=poll_seconds)
    normalized = sorted(set(slots))
    if len(normalized) != len(slots):
        raise ValueError("intraday temporal health windows overlap")
    return normalized


def _latest_recorded_trade_date(output_root: Path) -> str | None:
    slots_root = output_root / "slots"
    if not slots_root.is_dir():
        return None
    candidates = []
    for path in slots_root.iterdir():
        if not path.is_dir() or not any(path.glob("slot_*.json")):
            continue
        try:
            date.fromisoformat(path.name)
        except ValueError:
            continue
        candidates.append(path.name)
    return max(candidates) if candidates else None


def _local_now(now: datetime | None) -> datetime:
    value = now or datetime.now(CHINA_TZ)
    if value.tzinfo is None:
        return value.replace(tzinfo=CHINA_TZ)
    return value.astimezone(CHINA_TZ)


def _longest_unavailable_run(
    expected: Sequence[datetime],
    due: set[str],
    record_status: dict[str, str],
    poll_seconds: int,
) -> int:
    longest = 0
    current = 0
    previous: datetime | None = None
    step = timedelta(seconds=poll_seconds)
    for slot in expected:
        slot_text = slot.isoformat()
        if slot_text not in due:
            continue
        if previous is not None and slot - previous != step:
            current = 0
        if record_status.get(slot_text) == "CAPTURED":
            current = 0
        else:
            current += 1
            longest = max(longest, current)
        previous = slot
    return longest


def summarize_latest_capture_day(
    output_root: Path | str,
    *,
    poll_seconds: int,
    now: datetime | None = None,
    windows: Sequence[tuple[str, str]] = DEFAULT_CONTINUOUS_WINDOWS,
) -> dict[str, Any] | None:
    """Verify the latest slot chain and summarize only slots already due."""
    root = Path(output_root).expanduser().resolve()
    trade_date = _latest_recorded_trade_date(root)
    if trade_date is None:
        return None

    local_now = _local_now(now)
    expected = _expected_slots(trade_date, poll_seconds, windows)
    expected_text = {slot.isoformat() for slot in expected}
    records = list_verified_slot_records(root, trade_date)
    record_status: dict[str, str] = {}
    for _, payload in records:
        capture_slot = str(payload.get("captureSlot") or "")
        status = str(payload.get("status") or "")
        if status not in {"CAPTURED", "ERROR"}:
            raise ValueError(f"unsupported intraday slot status: {status}")
        record_status[capture_slot] = status

    completion_delay = timedelta(seconds=poll_seconds)
    due = {
        slot.isoformat()
        for slot in expected
        if slot + completion_delay <= local_now
        or slot.isoformat() in record_status
    }
    captured = sum(record_status.get(slot) == "CAPTURED" for slot in due)
    errors = sum(record_status.get(slot) == "ERROR" for slot in due)
    missing = len(due) - captured - errors
    observed = sum(slot in record_status for slot in expected_text)
    extra = sum(slot not in expected_text for slot in record_status)
    unavailable = errors + missing

    if not due:
        status = "PENDING"
    elif unavailable:
        status = "DEGRADED"
    elif len(due) < len(expected):
        status = "IN_PROGRESS"
    else:
        status = "COMPLETE"

    critical: dict[str, str] = {}
    for name, slot_time in CRITICAL_SLOT_TIMES.items():
        slot_text = datetime.combine(
            date.fromisoformat(trade_date),
            _parse_time(slot_time),
            tzinfo=CHINA_TZ,
        ).isoformat()
        critical[name] = record_status.get(
            slot_text,
            "MISSING" if slot_text in due else "PENDING",
        )

    due_count = len(due)
    return {
        "version": 1,
        "tradeDate": trade_date,
        "asOf": local_now.isoformat(),
        "status": status,
        "integrityValid": True,
        "expectedSlotCount": len(expected),
        "dueSlotCount": due_count,
        "futureSlotCount": len(expected) - due_count,
        "observedSlotCount": observed,
        "capturedSlotCount": captured,
        "errorSlotCount": errors,
        "missingSlotCount": missing,
        "extraSlotCount": extra,
        "recordCoverageRatio": (
            (captured + errors) / due_count if due_count else 0.0
        ),
        "successfulSlotCoverageRatio": (
            captured / due_count if due_count else 0.0
        ),
        "longestConsecutiveUnavailableSlots": _longest_unavailable_run(
            expected,
            due,
            record_status,
            poll_seconds,
        ),
        "criticalSlots": critical,
        "latestRecordSha256": records[-1][1]["recordSha256"] if records else None,
    }
