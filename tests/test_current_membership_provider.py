import hashlib
import json
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch

from scripts.build_current_membership_provider import (
    CODE_FIELD, DATE_FIELD, ENGLISH_EXCHANGE_FIELD, EXCHANGE_FIELD, INDEX_FIELD,
    MANIFEST_NAME, REQUIRED_FIELDS, build_current_membership_provider,
)


DATES = ["2026-10-20", "2026-10-21", "2026-10-22"]


def _hashes(root):
    return {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in root.rglob("*") if p.is_file()}


def _active(path, day):
    return {code for code, start, end in
            (line.split("\t") for line in path.read_text().splitlines())
            if start <= day <= end}


class CurrentMembershipProviderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "base"
        self.destination = self.root / "derived"
        (self.source / "calendars").mkdir(parents=True)
        (self.source / "instruments").mkdir()
        (self.source / "calendars/day.txt").write_text("\n".join(DATES) + "\n")
        (self.source / "calendars/day_future.txt").write_text("2026-10-23\n2026-10-26\n")
        self.base_symbols = [f"SH{600000 + n}" for n in range(300)]
        intervals = [f"{code}\t{DATES[0]}\t{DATES[-1]}" for code in self.base_symbols]
        intervals.append("SZ002294\t2026-10-01\t2026-10-19")
        (self.source / "instruments/csi300.txt").write_text("\n".join(intervals) + "\n")
        (self.source / "qlib_bin.manifest.json").write_text(json.dumps({
            "archive_sha256": "sha256:" + "a" * 64, "target_trade_date": DATES[-1],
        }))
        for code in [*self.base_symbols, "SZ002294", "SH000300"]:
            directory = self.source / "features" / code.lower()
            directory.mkdir(parents=True)
            for field in REQUIRED_FIELDS:
                (directory / f"{field}.day.bin").write_bytes(struct.pack("<4f", 0, 10, 11, 12))
        self.new_symbols = self.base_symbols[:-1] + ["SZ002294"]
        self.records = [{
            DATE_FIELD: "20261022", INDEX_FIELD: "000300", CODE_FIELD: code[2:],
            EXCHANGE_FIELD: "上海证券交易所" if code.startswith("SH") else "深圳证券交易所",
            ENGLISH_EXCHANGE_FIELD: "Shanghai Stock Exchange" if code.startswith("SH") else "Shenzhen Stock Exchange",
        } for code in self.new_symbols]
        self.membership = self.root / "records.json"
        self._write_records()
        self.xls = self.root / "official.xls"
        self.xls.write_bytes(b"fixture official XLS; parser provenance supplied by caller")
        self.xls_hash = hashlib.sha256(self.xls.read_bytes()).hexdigest()
        self.xls_parser = patch("scripts.build_current_membership_provider._read_xls_records",
                                return_value=json.loads(json.dumps(self.records)))
        self.xls_parser.start()
        self.addCleanup(self.xls_parser.stop)

    def _write_records(self):
        self.membership.write_text(json.dumps(self.records, ensure_ascii=False))

    def _build(self, **kwargs):
        return build_current_membership_provider(self.source, self.destination, self.membership,
                                                 self.xls, expected_xls_sha256=self.xls_hash, **kwargs)

    def test_isolated_asof_membership_preserves_history_source_and_feature_bytes(self):
        before = _hashes(self.source)
        result = self._build()
        self.assertEqual(result["status"], "BUILT")
        self.assertEqual(_hashes(self.source), before)
        original = self.source / "instruments/csi300.txt"
        derived = self.destination / "instruments/csi300.txt"
        for day in ["2026-10-01", "2026-10-19", *DATES[:-1]]:
            self.assertEqual(_active(original, day), _active(derived, day))
        self.assertEqual(_active(derived, DATES[-1]), set(self.new_symbols))
        copied = _hashes(self.destination)
        for name, digest in before.items():
            if name != "instruments/csi300.txt":
                self.assertEqual(copied[name], digest)
        manifest = json.loads((self.destination / MANIFEST_NAME).read_text())
        self.assertEqual(manifest["removedAtAsOf"], [self.base_symbols[-1]])
        self.assertEqual(manifest["addedAtAsOf"], ["SZ002294"])
        self.assertEqual(manifest["membership"]["asOfDate"], DATES[-1])
        self.assertFalse(manifest["membership"]["effectiveChangeDateVerified"])
        self.assertTrue(manifest["membership"]["recordsExtractionVerifiedByBuilder"])
        self.assertTrue(manifest["derivedProviderRequiresOverlayManifest"])
        self.assertEqual(manifest["sourceInstrumentsSha256"], before["instruments/csi300.txt"])
        self.assertEqual(manifest["derivedInstrumentsSha256"], copied["instruments/csi300.txt"])

    def test_refuses_existing_destination_and_nested_destination(self):
        self.destination.mkdir()
        sentinel = self.destination / "keep"
        sentinel.write_bytes(b"preserve")
        with self.assertRaises(ValueError):
            self._build()
        self.assertEqual(sentinel.read_bytes(), b"preserve")
        with self.assertRaises(ValueError):
            build_current_membership_provider(self.source, self.source / "nested", self.membership,
                                              self.xls, expected_xls_sha256=self.xls_hash)
        self.assertFalse((self.source / "nested").exists())

    def test_rejects_unverified_xls_without_output(self):
        with self.assertRaisesRegex(ValueError, "XLS SHA256"):
            build_current_membership_provider(self.source, self.destination, self.membership,
                                              self.xls, expected_xls_sha256="0" * 64)
        self.assertFalse(self.destination.exists())

    def test_rejects_json_not_matching_xls_and_unavailable_parser(self):
        with patch("scripts.build_current_membership_provider._read_xls_records", return_value=[]):
            with self.assertRaisesRegex(ValueError, "exactly match"):
                self._build()
        with patch("scripts.build_current_membership_provider._read_xls_records",
                   side_effect=ValueError("requires pandas and xlrd")):
            with self.assertRaisesRegex(ValueError, "requires pandas and xlrd"):
                self._build()
        self.assertFalse(self.destination.exists())

    def test_rejects_duplicate_date_index_and_exchange_errors(self):
        original = json.loads(json.dumps(self.records))
        changes = [
            lambda: self.records.__setitem__(1, dict(self.records[0])),
            lambda: self.records[0].__setitem__(DATE_FIELD, "20261021"),
            lambda: self.records[0].__setitem__(INDEX_FIELD, "000905"),
            lambda: self.records[0].__setitem__(EXCHANGE_FIELD, "unknown"),
            lambda: self.records[0].__setitem__(ENGLISH_EXCHANGE_FIELD, "Shenzhen Stock Exchange"),
        ]
        for change in changes:
            self.records = json.loads(json.dumps(original))
            change()
            self._write_records()
            with self.subTest(records_sha=hashlib.sha256(self.membership.read_bytes()).hexdigest()):
                with self.assertRaises(ValueError):
                    self._build()
                self.assertFalse(self.destination.exists())

    def test_rejects_earlier_snapshot_and_missing_new_constituent_features(self):
        for row in self.records:
            row[DATE_FIELD] = "20261021"
        self._write_records()
        with patch("scripts.build_current_membership_provider._read_xls_records", return_value=self.records):
            with self.assertRaisesRegex(ValueError, "latest trading date"):
                self._build()
        for row in self.records:
            row[DATE_FIELD] = "20261022"
        self._write_records()
        (self.source / "features/sz002294/factor.day.bin").unlink()
        with self.assertRaisesRegex(ValueError, "required feature"):
            self._build()
        self.assertFalse(self.destination.exists())

    def test_rejects_nan_on_asof_instead_of_dropping_a_member(self):
        (self.source / "features/sz002294/close.day.bin").write_bytes(struct.pack("<4f", 0, 10, 11, float("nan")))
        with self.assertRaisesRegex(ValueError, "unusable on as-of"):
            self._build()
        self.assertFalse(self.destination.exists())

    def test_copy_failure_removes_only_new_temporary_directory(self):
        before = _hashes(self.source)
        with patch("scripts.build_current_membership_provider.shutil.copytree", side_effect=OSError("fixture copy failure")):
            with self.assertRaises(OSError):
                self._build()
        self.assertEqual(_hashes(self.source), before)
        self.assertFalse(self.destination.exists())
        self.assertEqual(list(self.root.glob(".derived.building-*")), [])

    def test_refuses_existing_extension_without_losing_its_provenance(self):
        (self.source / MANIFEST_NAME).write_text('{"kind":"prior-extension"}')
        with self.assertRaisesRegex(ValueError, "existing extension provenance"):
            self._build()
        self.assertFalse(self.destination.exists())

    def test_preserves_inactive_legacy_alphanumeric_history_verbatim(self):
        path = self.source / "instruments/csi300.txt"
        legacy = "SHT00018\t2005-04-08\t2007-01-03\n"
        path.write_text(path.read_text() + legacy)
        source_before = path.read_bytes()
        self._build()
        self.assertEqual(path.read_bytes(), source_before)
        self.assertIn(legacy, (self.destination / "instruments/csi300.txt").read_text())
        self.assertEqual(_active(self.destination / "instruments/csi300.txt", "2006-01-04"), {"SHT00018"})
        self.assertEqual(_active(self.destination / "instruments/csi300.txt", DATES[-1]), set(self.new_symbols))

    def test_rejects_alphanumeric_code_in_current_active_membership(self):
        path = self.source / "instruments/csi300.txt"
        path.write_text(path.read_text().replace(self.base_symbols[0], "SHT00018"))
        with self.assertRaisesRegex(ValueError, "active constituent codes must contain six digits"):
            self._build()
        self.assertFalse(self.destination.exists())


if __name__ == "__main__":
    unittest.main()
