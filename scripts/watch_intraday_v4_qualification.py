#!/usr/bin/env python3
"""One-day, one-attempt V4 close qualification watcher; no historical backfill."""
from __future__ import annotations

import argparse
from datetime import date, datetime, time
import fcntl
import json
import os
from pathlib import Path
import stat
import sys
import time as clock
import uuid
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
for directory in (PROJECT_ROOT, PROJECT_ROOT / "frontend-v2/backend"):
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))

from scripts import qualify_intraday_v4 as qualify
from market_data import intraday_capture as capture
from market_data import intraday_capture_v4 as production
from market_data import intraday_source_qualification_v4 as qualification

_LOADED_SHA = capture._sha256_file(Path(__file__))
_TERMINAL = {"PASS", "FAIL", "MISSED", "UNKNOWN"}


def _now() -> datetime:
    return datetime.now(capture.CHINA_TZ)


def _timestamp(value: Any) -> datetime:
    parsed = datetime.fromisoformat(str(value))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("WATCH_CLOCK_INVALID")
    return parsed.astimezone(capture.CHINA_TZ)


def _session(value: datetime) -> bool:
    local = value.time().replace(tzinfo=None)
    return time(9, 30) <= local <= time(11, 30) or time(13) <= local <= time(15)


def _write(path: Path, payload: dict[str, Any]) -> None:
    raw = capture._canonical_json_bytes(payload)
    body = {**payload, "statusSha256": capture._sha256_bytes(raw)}
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(capture._canonical_json_bytes(body))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def _read(path: Path) -> dict[str, Any]:
    if path.is_symlink():
        raise ValueError("WATCH_STATUS_ALIAS")
    body = json.loads(path.read_bytes())
    expected = body.pop("statusSha256")
    if expected != capture._sha256_bytes(capture._canonical_json_bytes(body)):
        raise ValueError("WATCH_STATUS_HASH_MISMATCH")
    return body


def _binding(provider: Path, capture_root: Path, trade_date: str, output: Path) -> dict[str, Any]:
    context = capture._provider_context(provider)
    if trade_date not in context["tradingDates"]:
        raise ValueError("WATCH_DATE_NOT_IN_CALENDAR")
    implementation = production.capture_implementation_contract()
    if capture._sha256_file(Path(__file__)) != _LOADED_SHA:
        raise ValueError("WATCH_LOADED_CODE_CHANGED")
    return {"providerPath": str(provider),
            "providerFingerprint": qualify.provider_contract_fingerprint(provider)["fingerprint"],
            "providerContext": context, "captureRoot": str(capture_root),
            "tradeDate": trade_date, "outputRoot": str(output),
            "productionImplementationSha256": implementation["sha256"],
            "watchImplementationSha256": _LOADED_SHA,
            "closeCollectorSha256": capture._sha256_file(Path(qualify.__file__)),
            "qualificationImplementation": qualification._implementation_contract()}


def _select(capture_root: Path, target: str, binding: dict[str, Any],
            current: datetime) -> dict[str, str] | None:
    context = binding["providerContext"]
    implementation = production.capture_implementation_contract()
    expected_binding = capture._slot_binding(provider=context, implementation=implementation, poll_seconds=60)
    records = capture.list_verified_slot_records(capture_root, target)
    for record_path, record in sorted(records, key=lambda pair: pair[1]["captureSlot"]):
        if record.get("status") != "CAPTURED" or record.get("forcedCapture") is not False:
            continue
        slot = _timestamp(record["captureSlot"])
        if slot.date().isoformat() != target or not _session(slot):
            continue
        try:
            snapshot_path = Path(record["snapshot"]["file"]).expanduser().resolve()
            if snapshot_path.parent != capture_root / "snapshots" / target:
                raise ValueError("WATCH_SNAPSHOT_OUTSIDE_CAPTURE")
            snapshot = capture.read_verified_snapshot(snapshot_path)
            received = _timestamp(snapshot["responseReceivedAt"])
            requested = _timestamp(snapshot["captureRequestedAt"])
            if (record.get("binding") != expected_binding
                    or record["snapshot"]["contentSha256"] != snapshot["contentSha256"]
                    or record["snapshot"]["archiveSha256"] != capture._sha256_file(snapshot_path)
                    or snapshot.get("implementation") != implementation
                    or snapshot.get("forcedCapture") is not False
                    or snapshot.get("usageRealm") != "INTRADAY_RESEARCH_RAW"
                    or snapshot.get("captureSlot") != record["captureSlot"]
                    or snapshot.get("quoteTradeDate") != target
                    or snapshot.get("provider") != qualify._provider_metadata(context)
                    or snapshot.get("universe") != qualify._universe_metadata(context)
                    or snapshot.get("coverage", {}).get("usableQuoteCount") != 301
                    or not _session(requested)
                    or not requested <= received == _timestamp(snapshot["availableAt"]) == _timestamp(snapshot["capturedAt"])
                    or not received <= _timestamp(record["committedAt"]) <= current
                    or _timestamp(record["availableAt"]) != received):
                raise ValueError("WATCH_SNAPSHOT_NOT_ELIGIBLE")
            symbols = qualification._universe(snapshot)
            qualification._verify_realtime_implementation(snapshot, binding["qualificationImplementation"])
            qualification._rows(snapshot, symbols, target, realtime=True)
        except (KeyError, TypeError, ValueError, OSError):
            # Keep every failed original. Eligibility is predeclared and contains
            # no close observations, returns, or selection based on a PASS result.
            continue
        return {"snapshotFile": str(snapshot_path), "recordFile": str(record_path.resolve()),
                "recordSha256": record["recordSha256"],
                "snapshotContentSha256": snapshot["contentSha256"],
                "snapshotArchiveSha256": record["snapshot"]["archiveSha256"]}
    return None


def _summary(status: str, reason: str, **extra: Any) -> dict[str, Any]:
    return {"status": status, "reason": reason, "qualificationPassed": status == "PASS",
            "autoShadowAccepted": False, "profitEvidenceEligible": False, **extra}


def watch(provider: Path, capture_root: Path, trade_date: str, output: Path,
          *, poll_seconds: int = 15) -> tuple[int, dict[str, Any]]:
    """Wait for one real date and make at most one original collect_close call."""
    if not isinstance(poll_seconds, int) or isinstance(poll_seconds, bool) or not 1 <= poll_seconds <= 60:
        return 1, _summary("FAIL", "WATCH_CONFIGURATION_INVALID")
    try:
        target = date.fromisoformat(trade_date)
        if target.isoformat() != trade_date:
            raise ValueError("WATCH_DATE_INVALID")
        provider, capture_root = provider.expanduser().resolve(), capture_root.expanduser().resolve()
        lexical_output = output.expanduser().absolute()
        output = lexical_output.resolve()
        if (lexical_output != output or output == capture_root
                or output in capture_root.parents or capture_root in output.parents
                or output == provider or output in provider.parents or provider in output.parents):
            raise ValueError("WATCH_OUTPUT_OVERLAP")
        qualify._safe_output(output)
        binding = _binding(provider, capture_root, trade_date, output)
        current = _timestamp(_now())
        status_path = output / "watch_status.json"
        if current.date() > target and not status_path.exists():
            return 1, _summary("MISSED", "WATCH_DATE_ALREADY_PASSED")
        output.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor = os.open(output / "watch.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    except Exception:
        return 1, _summary("FAIL", "WATCH_CONFIGURATION_OR_BINDING_INVALID")
    with os.fdopen(descriptor, "a+") as lock:
        attempt_started = False
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 2, _summary("ALREADY_RUNNING", "WATCH_ALREADY_RUNNING")
        try:
            if status_path.exists():
                state = _read(status_path)
                if state.get("binding") != binding:
                    return 1, _summary("FAIL", "WATCH_BINDING_CHANGED")
                if state["status"] == "ATTEMPT_STARTED":
                    state.update(status="UNKNOWN", reason="WATCH_PRIOR_ATTEMPT_OUTCOME_UNKNOWN")
                    _write(status_path, state)
                if state["status"] in _TERMINAL:
                    return (0 if state["status"] == "PASS" else 1), _summary(state["status"], state["reason"])
            else:
                state = {"schemaVersion": 1, "binding": binding, "status": "WAITING_REALTIME",
                         "reason": "WATCH_WAITING_FOR_REALTIME", "selected": None,
                         "createdAt": current.isoformat(), "autoShadowAccepted": False,
                         "profitEvidenceEligible": False}
                _write(status_path, state)
            _write(output / "watch_pid.json", {"pid": os.getpid(), "tradeDate": trade_date,
                    "startedAt": current.isoformat(), "bindingSha256": capture._sha256_bytes(capture._canonical_json_bytes(binding))})
            previous = current
            while True:
                current = _timestamp(_now())
                if current < previous:
                    raise ValueError("WATCH_CLOCK_BACKWARDS")
                previous = current
                if _binding(provider, capture_root, trade_date, output) != binding:
                    raise ValueError("WATCH_BINDING_CHANGED")
                if current.date() > target:
                    state.update(status="MISSED", reason="WATCH_DATE_MISSED_NO_BACKFILL")
                    _write(status_path, state)
                    return 1, _summary("MISSED", state["reason"])
                if current.date() == target:
                    selected = _select(capture_root, trade_date, binding, current)
                    if state["selected"] is not None and selected != state["selected"]:
                        raise ValueError("WATCH_SELECTED_EVIDENCE_CHANGED")
                    if state["selected"] is None and selected is not None:
                        state.update(selected=selected, status="WAITING_CLOSE", reason="WATCH_WAITING_FOR_1510")
                        _write(status_path, state)
                    if current.time().replace(tzinfo=None) >= time(15, 10):
                        if state["selected"] is None:
                            state.update(status="FAIL", reason="WATCH_NO_ELIGIBLE_REALTIME")
                            _write(status_path, state)
                            return 1, _summary("FAIL", state["reason"])
                        ready = _timestamp(_now())
                        if ready < current:
                            raise ValueError("WATCH_CLOCK_BACKWARDS")
                        if ready.date() > target:
                            state.update(status="MISSED", reason="WATCH_DATE_MISSED_NO_BACKFILL")
                            _write(status_path, state)
                            return 1, _summary("MISSED", state["reason"])
                        state.update(status="ATTEMPT_STARTED", reason="WATCH_SINGLE_CLOSE_ATTEMPT",
                                     attemptedAt=ready.isoformat())
                        _write(status_path, state)
                        attempt_started = True
                        calling = _timestamp(_now())
                        if calling < ready:
                            raise ValueError("WATCH_CLOCK_BACKWARDS")
                        if calling.date() > target:
                            state.update(status="MISSED", reason="WATCH_DATE_MISSED_NO_BACKFILL")
                            _write(status_path, state)
                            return 1, _summary("MISSED", state["reason"])
                        # No replacement clock is passed or installed. The original
                        # close collector obtains both actual network timestamps.
                        code, result = qualify.collect_close(provider, Path(state["selected"]["snapshotFile"]), output)
                        done = _timestamp(_now())
                        if done < calling:
                            raise ValueError("WATCH_CLOCK_BACKWARDS")
                        passed = code == 0 and result.get("status") == "PASS" and done.date() == target
                        missed = done.date() > target
                        state.update(status="MISSED" if missed else "PASS" if passed else "FAIL",
                                     reason="WATCH_DATE_MISSED_NO_BACKFILL" if missed else
                                     "WATCH_QUALIFICATION_PASSED" if passed else "WATCH_CLOSE_ATTEMPT_NOT_PASSED",
                                     completedAt=done.isoformat())
                        for key in ("qualificationFile", "reportSha256", "diagnosticFile"):
                            if key in result:
                                state[key] = result[key]
                        _write(status_path, state)
                        return (0 if passed else 1), _summary(state["status"], state["reason"])
                clock.sleep(60 if current.date() < target else poll_seconds)
        except Exception:
            # ATTEMPT_STARTED is durable before any network access. Failures do
            # not erase raw evidence or authorize a second collection attempt.
            uncertain = attempt_started or ("state" in locals() and state.get("status") == "ATTEMPT_STARTED")
            status = "UNKNOWN" if uncertain else "FAIL"
            reason = "WATCH_ATTEMPT_OUTCOME_UNKNOWN" if uncertain else "WATCH_INTEGRITY_OR_COLLECTION_ERROR"
            if "state" in locals():
                state.update(status=status, reason=reason)
                try:
                    _write(status_path, state)
                except Exception:
                    pass
            return 1, _summary(status, reason)
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise ValueError("WATCH_ARGUMENTS_INVALID")


def main(argv: list[str] | None = None) -> int:
    os.umask(0o077)
    try:
        if stat.S_ISREG(os.fstat(sys.stdout.fileno()).st_mode):
            os.fchmod(sys.stdout.fileno(), 0o600)
    except (OSError, AttributeError, ValueError):
        pass
    try:
        parser = _Parser(description=__doc__)
        for name in ("provider", "capture-root", "output"):
            parser.add_argument("--" + name, type=Path, required=True)
        parser.add_argument("--trade-date", required=True)
        parser.add_argument("--poll-seconds", type=int, default=15)
        args = parser.parse_args(argv)
        code, result = watch(args.provider, args.capture_root, args.trade_date, args.output, poll_seconds=args.poll_seconds)
    except Exception:
        code, result = 1, _summary("FAIL", "WATCH_ARGUMENTS_OR_COLLECTION_INVALID")
    print(json.dumps(result, sort_keys=True))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
