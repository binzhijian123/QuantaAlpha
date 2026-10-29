import json
from pathlib import Path
import struct
import tempfile
import unittest

from scripts.verify_shadow_provider_session import verify_session, FIELDS


class ShadowProviderSessionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "calendars").mkdir()
        (self.root / "instruments").mkdir()
        (self.root / "calendars/day.txt").write_text("2026-10-21\n2026-10-22\n")
        (self.root / "instruments/csi300.txt").write_text("".join(f"SH{600000+i}\t2026-10-21\t2026-10-22\n" for i in range(300)))
        for code in ["sh000300", *[f"sh{600000+i}" for i in range(300)]]:
            folder = self.root / "features" / code
            folder.mkdir(parents=True)
            for field in FIELDS:
                value = {"high": 12, "low": 8, "factor": 1, "volume": 100}.get(field, 10)
                (folder / f"{field}.day.bin").write_bytes(struct.pack("<3f", 0, value, value))

    def test_complete_session_passes_without_claiming_provenance_or_acceptance(self):
        report = verify_session(self.root, "2026-10-22")
        self.assertEqual(report["status"], "PASS")
        self.assertEqual(report["validEquityCount"], 300)
        self.assertTrue(report["benchmarkValid"])
        self.assertFalse(report["autoShadowAccepted"])
        self.assertFalse(report["provenanceVerified"])
        self.assertNotIn('"price":', json.dumps(report))

    def test_benchmark_lag_blocks_otherwise_complete_equities(self):
        path = self.root / "features/sh000300/close.day.bin"
        path.write_bytes(struct.pack("<2f", 0, 10))
        report = verify_session(self.root, "2026-10-22")
        self.assertEqual(report["status"], "FAIL")
        self.assertEqual(report["validEquityCount"], 300)
        self.assertFalse(report["benchmarkValid"])
        self.assertEqual(report["failures"][0]["reasons"], ["close:MISSING_SESSION_VALUE"])

    def test_nan_bad_offset_and_bad_ohlc_fail(self):
        path = self.root / "features/sh600000/close.day.bin"
        for raw in (struct.pack("<3f", 0, 10, float("nan")), struct.pack("<3f", .5, 10, 10), struct.pack("<3f", 0, 10, 20)):
            path.write_bytes(raw)
            self.assertEqual(verify_session(self.root, "2026-10-22")["status"], "FAIL")

    def test_non_current_session_rejected_and_no_files_written(self):
        before = {str(p): p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        with self.assertRaises(ValueError): verify_session(self.root, "2026-10-21")
        verify_session(self.root, "2026-10-22")
        self.assertEqual(before, {str(p): p.read_bytes() for p in self.root.rglob("*") if p.is_file()})

    def test_calendar_whitespace_uses_same_index_as_provider_context(self):
        (self.root / "calendars/day.txt").write_text(" 2026-10-21 \n\n 2026-10-22 \n")
        self.assertEqual(verify_session(self.root, "2026-10-22")["status"], "PASS")
        (self.root / "features/sh000300/close.day.bin").write_bytes(struct.pack("<3f", 0, 10, float("nan")))
        self.assertFalse(verify_session(self.root, "2026-10-22")["benchmarkValid"])
