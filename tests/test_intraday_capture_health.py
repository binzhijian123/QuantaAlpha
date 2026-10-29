import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


BACKEND_ROOT = Path(__file__).resolve().parents[1] / "frontend-v2" / "backend"
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from market_data.intraday_capture import _commit_slot_record
from market_data.intraday_capture_health import summarize_latest_capture_day


CHINA_TZ = ZoneInfo("Asia/Shanghai")
TEST_WINDOWS = (("09:30:00", "09:32:00"),)


def _record(root: Path, minute: int, status: str) -> None:
    value = datetime(2026, 8, 14, 9, minute, 15, tzinfo=CHINA_TZ)
    slot = value.replace(second=0)
    _commit_slot_record(
        output_root=root,
        trade_date="2026-08-14",
        capture_slot=slot,
        status=status,
        request_started_at=value,
        response_received_at=value,
        committed_at=value,
        forced_capture=False,
        binding={},
        snapshot=None,
        error="upstream unavailable" if status == "ERROR" else None,
    )


class IntradayCaptureHealthTests(unittest.TestCase):
    def test_no_records_has_no_temporal_summary(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            result = summarize_latest_capture_day(
                Path(temp_dir),
                poll_seconds=60,
                now=datetime(2026, 8, 14, 9, 33, tzinfo=CHINA_TZ),
                windows=TEST_WINDOWS,
            )

        self.assertIsNone(result)

    def test_only_due_slots_are_counted_during_session(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _record(root, 30, "CAPTURED")
            result = summarize_latest_capture_day(
                root,
                poll_seconds=60,
                now=datetime(2026, 8, 14, 9, 31, 20, tzinfo=CHINA_TZ),
                windows=TEST_WINDOWS,
            )

        self.assertEqual(result["status"], "IN_PROGRESS")
        self.assertEqual(result["dueSlotCount"], 1)
        self.assertEqual(result["futureSlotCount"], 2)
        self.assertEqual(result["capturedSlotCount"], 1)
        self.assertEqual(result["missingSlotCount"], 0)

    def test_errors_and_missing_due_slots_are_persistently_degraded(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _record(root, 30, "CAPTURED")
            _record(root, 31, "ERROR")
            result = summarize_latest_capture_day(
                root,
                poll_seconds=60,
                now=datetime(2026, 8, 14, 9, 33, tzinfo=CHINA_TZ),
                windows=TEST_WINDOWS,
            )

        self.assertEqual(result["status"], "DEGRADED")
        self.assertEqual(result["expectedSlotCount"], 3)
        self.assertEqual(result["dueSlotCount"], 3)
        self.assertEqual(result["capturedSlotCount"], 1)
        self.assertEqual(result["errorSlotCount"], 1)
        self.assertEqual(result["missingSlotCount"], 1)
        self.assertEqual(result["longestConsecutiveUnavailableSlots"], 2)
        self.assertAlmostEqual(result["successfulSlotCoverageRatio"], 1 / 3)

    def test_all_due_slots_complete(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            for minute in (30, 31, 32):
                _record(root, minute, "CAPTURED")
            result = summarize_latest_capture_day(
                root,
                poll_seconds=60,
                now=datetime(2026, 8, 14, 9, 33, tzinfo=CHINA_TZ),
                windows=TEST_WINDOWS,
            )

        self.assertEqual(result["status"], "COMPLETE")
        self.assertEqual(result["capturedSlotCount"], 3)
        self.assertEqual(result["errorSlotCount"], 0)
        self.assertEqual(result["missingSlotCount"], 0)
        self.assertEqual(result["successfulSlotCoverageRatio"], 1.0)

    def test_deleted_record_is_rejected_as_a_broken_chain(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _record(root, 30, "CAPTURED")
            _record(root, 31, "CAPTURED")
            first = sorted((root / "slots" / "2026-08-14").glob("slot_*.json"))[0]
            first.chmod(0o644)
            first.unlink()

            with self.assertRaisesRegex(ValueError, "sequence is not contiguous"):
                summarize_latest_capture_day(
                    root,
                    poll_seconds=60,
                    now=datetime(2026, 8, 14, 9, 33, tzinfo=CHINA_TZ),
                    windows=TEST_WINDOWS,
                )


if __name__ == "__main__":
    unittest.main()
