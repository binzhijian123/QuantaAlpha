"""Temporary synthetic production-bound fixtures; no network or real clock override."""
from contextlib import redirect_stderr, redirect_stdout
import copy
from datetime import datetime
import fcntl
import importlib.util
import io
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

from scripts import watch_intraday_v4_qualification as watch

spec = importlib.util.spec_from_file_location("watch_shadow_helpers", Path(__file__).with_name("test_intraday_shadow.py"))
helpers = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helpers)


class StopWatch(BaseException):
    pass


class Client(helpers._QuoteClient):
    def __init__(self, incomplete=False):
        super().__init__()
        self.incomplete = incomplete

    def realtime_quotes(self, *args, **kwargs):
        rows = super().realtime_quotes(*args, **kwargs)
        for row in rows.values():
            row["realtimeContractSha256"] = watch.qualification.REALTIME_CONTRACT_SHA256
        if self.incomplete:
            rows.pop(next(iter(rows)))
        return rows


class QualificationWatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.provider = helpers._write_provider(self.root / "provider", helpers.BASE_DATES)
        self.capture = self.root / "capture"
        self.output = self.root / "qualification"
        self.day = "2026-08-14"
        self.impl = watch.production.capture_implementation_contract()
        self.now = datetime.fromisoformat(self.day + "T15:10:00+08:00")
        self.clock_patch = patch.object(watch, "_now", return_value=self.now)
        self.fake_now = self.clock_patch.start()
        self.addCleanup(self.clock_patch.stop)
        self.close_patch = patch.object(watch.qualify, "collect_close", return_value=(0, {"status": "PASS", "reportSha256": "a" * 64}))
        self.close = self.close_patch.start()
        self.addCleanup(self.close_patch.stop)
        self.original_umask = os.umask(0o077)
        self.addCleanup(os.umask, self.original_umask)

    def capture_at(self, clock="09:30:03", incomplete=False, day=None):
        stamp = datetime.fromisoformat((day or self.day) + "T" + clock + "+08:00")
        with patch.object(watch.capture, "capture_implementation_contract", return_value=self.impl):
            return watch.capture.run_capture_cycle(self.provider, self.capture, now=stamp, client=Client(incomplete))

    def run_watch(self, **kwargs):
        args = dict(provider=self.provider, capture_root=self.capture, trade_date=self.day,
                    output=self.output, poll_seconds=1)
        args.update(kwargs)
        return watch.watch(**args)

    def state(self):
        return watch._read(self.output / "watch_status.json")

    def test_first_eligible_snapshot_is_fixed_and_original_close_called_once(self):
        first = self.capture_at()
        self.capture_at("09:31:03")
        original_clock = watch.qualify._now
        code, result = self.run_watch()
        self.assertEqual(code, 0)
        self.assertEqual(result["status"], "PASS")
        self.close.assert_called_once_with(self.provider, Path(first["latest"]["file"]), self.output)
        self.assertIs(watch.qualify._now, original_clock)
        self.assertEqual(self.state()["selected"]["snapshotFile"], first["latest"]["file"])
        self.assertNotIn("quotes", json.dumps(result))
        self.run_watch()
        self.assertEqual(self.close.call_count, 1)
        self.assertEqual(stat.S_IMODE((self.output / "watch_status.json").stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE((self.output / "watch_pid.json").stat().st_mode), 0o600)

    def test_incomplete_first_snapshot_preserved_and_next_exact301_selected(self):
        failed = self.capture_at(incomplete=True)
        successful = self.capture_at("09:31:03")
        original = Path(failed["latest"]["file"]).read_bytes()
        code, _ = self.run_watch()
        self.assertEqual(code, 0)
        self.assertEqual(self.state()["selected"]["snapshotFile"], successful["latest"]["file"])
        self.assertEqual(Path(failed["latest"]["file"]).read_bytes(), original)

    def test_1510_boundary_waits_with_bounded_sleep(self):
        self.capture_at()
        self.fake_now.side_effect = [self.now.replace(minute=9, second=59),
                                     self.now.replace(minute=9, second=59), self.now, self.now, self.now, self.now]
        with patch.object(watch.clock, "sleep") as sleep:
            code, _ = self.run_watch(poll_seconds=60)
        self.assertEqual(code, 0)
        sleep.assert_called_once_with(60)
        self.close.assert_called_once()

    def test_future_date_only_waits_without_reading_capture_or_collecting(self):
        self.fake_now.return_value = self.now.replace(hour=10)
        with patch.object(watch.clock, "sleep", side_effect=StopWatch), \
             patch.object(watch.capture, "list_verified_slot_records") as records:
            with self.assertRaises(StopWatch):
                self.run_watch(trade_date="2026-08-17")
        records.assert_not_called()
        self.close.assert_not_called()
        self.assertEqual(self.state()["status"], "WAITING_REALTIME")
        self.assertEqual(self.state()["binding"]["tradeDate"], "2026-08-17")

    def test_cross_day_pending_becomes_missed_without_backfill(self):
        self.capture_at()
        self.fake_now.return_value = self.now.replace(hour=14)
        with patch.object(watch.clock, "sleep", side_effect=StopWatch):
            with self.assertRaises(StopWatch):
                self.run_watch()
        self.fake_now.return_value = self.now.replace(day=15)
        code, result = self.run_watch()
        self.assertEqual(code, 1)
        self.assertEqual(result["status"], "MISSED")
        self.assertEqual(self.state()["status"], "MISSED")
        self.close.assert_not_called()

    def test_cross_day_immediately_before_attempt_never_calls_close(self):
        self.capture_at()
        self.fake_now.side_effect = [self.now, self.now, self.now.replace(day=15)]
        code, result = self.run_watch()
        self.assertEqual(code, 1)
        self.assertEqual(result["status"], "MISSED")
        self.close.assert_not_called()

    def test_cross_day_after_durable_attempt_before_call_never_calls_close(self):
        self.capture_at()
        self.fake_now.side_effect = [self.now, self.now, self.now, self.now.replace(day=15)]
        code, result = self.run_watch()
        self.assertEqual(code, 1)
        self.assertEqual(result["status"], "MISSED")
        self.assertIn("attemptedAt", self.state())
        self.close.assert_not_called()

    def test_attempt_directory_fsync_failure_is_unknown_without_network(self):
        self.capture_at()
        real_fsync = os.fsync
        def fail_attempt_directory(descriptor):
            if stat.S_ISDIR(os.fstat(descriptor).st_mode) and (self.output / "watch_status.json").exists():
                if self.state()["status"] == "ATTEMPT_STARTED":
                    raise OSError("private failure must not appear")
            return real_fsync(descriptor)
        with patch.object(watch.os, "fsync", side_effect=fail_attempt_directory):
            code, result = self.run_watch()
        self.assertEqual(code, 1)
        self.assertEqual(result["status"], "UNKNOWN")
        self.close.assert_not_called()
        self.assertNotIn("private failure", json.dumps(result))
        self.run_watch()
        self.close.assert_not_called()

    def test_past_new_date_does_not_create_output(self):
        self.fake_now.return_value = self.now.replace(day=15)
        code, result = self.run_watch()
        self.assertEqual(code, 1)
        self.assertEqual(result["status"], "MISSED")
        self.assertFalse(self.output.exists())
        self.close.assert_not_called()

    def test_attempt_started_crash_recovers_unknown_never_retries(self):
        self.capture_at()
        self.close.side_effect = StopWatch
        with self.assertRaises(StopWatch):
            self.run_watch()
        self.assertEqual(self.state()["status"], "ATTEMPT_STARTED")
        self.close.side_effect = None
        self.close.return_value = (0, {"status": "PASS"})
        code, result = self.run_watch()
        self.assertEqual(code, 1)
        self.assertEqual(result["status"], "UNKNOWN")
        self.assertEqual(self.state()["status"], "UNKNOWN")
        self.assertEqual(self.close.call_count, 1)

    def test_failed_close_keeps_raw_original_and_never_retries(self):
        self.capture_at()
        raw = self.output / "failed-raw-evidence.json"
        def fail(*args):
            raw.write_bytes(b"original source failure evidence")
            return 1, {"status": "FAIL", "diagnosticFile": str(raw)}
        self.close.side_effect = fail
        code, result = self.run_watch()
        self.assertEqual(code, 1)
        self.assertFalse(result["qualificationPassed"])
        before = raw.read_bytes()
        self.run_watch()
        self.assertEqual(self.close.call_count, 1)
        self.assertEqual(raw.read_bytes(), before)

    def test_collection_error_is_redacted_and_terminal(self):
        self.capture_at()
        self.close.side_effect = RuntimeError("PRIVATE_TOKEN=must-not-appear")
        code, result = self.run_watch()
        self.assertEqual(code, 1)
        self.assertEqual(result["status"], "UNKNOWN")
        self.assertNotIn("PRIVATE_TOKEN", json.dumps(result))
        self.assertNotIn("PRIVATE_TOKEN", (self.output / "watch_status.json").read_text())
        self.run_watch()
        self.assertEqual(self.close.call_count, 1)

    def test_restart_cannot_change_provider_capture_or_production_binding(self):
        self.capture_at()
        self.fake_now.return_value = self.now.replace(hour=14)
        with patch.object(watch.clock, "sleep", side_effect=StopWatch):
            with self.assertRaises(StopWatch):
                self.run_watch()
        previous = (self.output / "watch_status.json").read_bytes()
        code, result = self.run_watch(capture_root=self.root / "different-capture")
        self.assertEqual(code, 1)
        self.assertEqual(result["reason"], "WATCH_BINDING_CHANGED")
        self.assertEqual((self.output / "watch_status.json").read_bytes(), previous)
        changed = copy.deepcopy(self.impl)
        changed["sha256"] = "f" * 64
        with patch.object(watch.production, "capture_implementation_contract", return_value=changed):
            _, result = self.run_watch()
        self.assertEqual(result["reason"], "WATCH_BINDING_CHANGED")
        self.assertEqual((self.output / "watch_status.json").read_bytes(), previous)
        self.close.assert_not_called()

    def test_duplicate_process_lock_refuses_without_status_write(self):
        self.output.mkdir()
        with (self.output / "watch.lock").open("a+") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            code, result = self.run_watch()
        self.assertEqual(code, 2)
        self.assertEqual(result["status"], "ALREADY_RUNNING")
        self.assertFalse((self.output / "watch_status.json").exists())
        self.close.assert_not_called()

    def test_no_formal_snapshot_fails_without_calling_close(self):
        code, result = self.run_watch()
        self.assertEqual(code, 1)
        self.assertEqual(result["reason"], "WATCH_NO_ELIGIBLE_REALTIME")
        self.close.assert_not_called()

    def test_misbound_or_after_session_snapshot_is_not_selected(self):
        result = self.capture_at()
        original = watch.capture.read_verified_snapshot
        for change in ({"provider": {"path": "different"}},
                       {"captureRequestedAt": self.day + "T15:01:00+08:00"}):
            with self.subTest(change=change):
                binding = watch._binding(self.provider, self.capture, self.day, self.output)
                with patch.object(watch.capture, "read_verified_snapshot", side_effect=lambda path: {**original(path), **change}):
                    self.assertIsNone(watch._select(self.capture, self.day, binding, self.now))
        self.assertTrue(Path(result["latest"]["file"]).exists())

    def test_bad_poll_or_clock_override_cli_is_fixed_redacted_and_stdout_private(self):
        code, _ = self.run_watch(poll_seconds=0)
        self.assertEqual(code, 1)
        self.assertFalse(self.output.exists())
        log = self.root / "stdout.log"
        with log.open("w") as stream, redirect_stdout(stream), redirect_stderr(io.StringIO()):
            code = watch.main(["--now", "PRIVATE_TOKEN=never-output"])
        self.assertEqual(code, 1)
        self.assertNotIn("PRIVATE_TOKEN", log.read_text())
        self.assertEqual(stat.S_IMODE(log.stat().st_mode), 0o600)
        self.close.assert_not_called()


if __name__ == "__main__":
    unittest.main()
