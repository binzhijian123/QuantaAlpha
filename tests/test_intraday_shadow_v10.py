"""Synthetic temporary evidence only; never produces production acceptance."""
import copy
from contextlib import redirect_stderr, redirect_stdout
import gzip
import importlib.util
import io
import json
import sqlite3
from pathlib import Path
import sys
import tempfile
import unittest
from datetime import datetime
from unittest.mock import patch

BACKEND = Path(__file__).resolve().parents[1] / "frontend-v2/backend"
sys.path.insert(0, str(BACKEND))
from market_data import intraday_shadow_v10 as v10
from market_data import intraday_capture as capture
from market_data import intraday_calibration as calibration
from market_data import intraday_source_qualification_v4 as qualification
from market_data.intraday_shadow_v5 import IntradayShadowCoordinator as V5

spec = importlib.util.spec_from_file_location("shadow_test_helpers", Path(__file__).with_name("test_intraday_shadow.py"))
helpers = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helpers)
cli_spec = importlib.util.spec_from_file_location("v10_verify_cli", BACKEND.parents[1] / "scripts/verify_intraday_v10_declaration.py")
verify_cli = importlib.util.module_from_spec(cli_spec)
cli_spec.loader.exec_module(verify_cli)


def write_json(path, payload):
    path.write_text(json.dumps(payload, sort_keys=True))
    helpers._sidecar(path)


def envelope(path, payload):
    payload = copy.deepcopy(payload)
    payload.pop("contentSha256", None)
    payload["contentSha256"] = capture._sha256_bytes(capture._canonical_json_bytes(payload))
    path.write_bytes(gzip.compress(capture._canonical_json_bytes(payload), mtime=0))
    helpers._sidecar(path)
    return path


class Client(helpers._QuoteClient):
    def realtime_quotes(self, *args, **kwargs):
        rows = super().realtime_quotes(*args, **kwargs)
        for row in rows.values():
            row["realtimeContractSha256"] = v10.REALTIME_CONTRACT_SHA256
        return rows


class V10Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temp.name).resolve()
        cls.provider = helpers._write_provider(cls.root / "provider", helpers.BASE_DATES[:-1])
        cls.context = capture._provider_context(cls.provider)
        cls.impl = v10.capture_v4.capture_implementation_contract()
        cls.calib_root = cls.root / "calibration-captures"
        cls.calib_declaration = cls.root / "calibration-declaration.json"
        binding = capture._slot_binding(provider=cls.context, implementation=cls.impl, poll_seconds=60)
        cls.calib_payload = {
            "schemaVersion": 1, "protocolId": "synthetic_v4_calibration",
            "phase": "CALIBRATION_ONLY", "profitEvidenceEligible": False,
            "featureValuesPermitted": False, "returnValuesPermitted": False,
            "createdAt": "2026-08-12T16:00:00+08:00",
            "createdBeforeCurrentTradeDateQuotesObserved": True,
            "calibrationImplementationSha256": calibration.calibration_implementation_contract()["contractSha256"],
            "captureContract": {"binding": binding, "pollSeconds": 60},
            "calibration": {"tradeDate": "2026-08-13", "continuousSessionWindows": v10.WINDOWS,
                "expectedSlotCount": 242, "finalizeAfter": "15:05:00", "qualityGates": v10.QUALITY_GATES},
        }
        write_json(cls.calib_declaration, cls.calib_payload)
        # Explicit fixture substitution: exercise the real collector/chain/calibrator
        # without network calls. These files exist only in this temporary test tree.
        with patch.object(capture, "capture_implementation_contract", return_value=cls.impl):
            for stamp in calibration._expected_slots("2026-08-13", v10.WINDOWS, 60):
                result = capture.run_capture_cycle(cls.provider, cls.calib_root, now=stamp, client=Client())
                if result["status"] != "CAPTURED":
                    raise AssertionError(result["status"])
        summary = calibration.finalize_calibration_report(cls.calib_declaration, cls.calib_root,
            now=datetime.fromisoformat("2026-08-13T15:05:00+08:00"))
        cls.calib_report_path = Path(summary["file"])
        cls.calib_report = calibration.read_verified_calibration_report(cls.calib_report_path)
        if cls.calib_report["status"] != "PASS":
            raise AssertionError(cls.calib_report["observed"])
        records = capture.list_verified_slot_records(cls.calib_root, "2026-08-13")
        rt_path = Path(records[0][1]["snapshot"]["file"])
        close = capture.read_verified_snapshot(Path(records[-1][1]["snapshot"]["file"]))
        reference = envelope(cls.root / "reference.json.gz", close)
        for row in close["quotes"]:
            row["source"] = qualification.TCP
        candidate = envelope(cls.root / "candidate.json.gz", close)
        fingerprint = helpers.provider_contract_fingerprint(cls.provider, "csi300")["fingerprint"]
        cls.qual_report = qualification.build_source_qualification_report(
            realtime_snapshot_path=rt_path, reference_snapshot_path=reference,
            candidate_snapshot_path=candidate, provider_fingerprint=fingerprint,
            measured_at=datetime.fromisoformat("2026-08-13T15:05:00+08:00"))
        if cls.qual_report["status"] != "PASS":
            raise AssertionError(cls.qual_report)
        cls.qual_path = qualification.persist_source_qualification_report(cls.qual_report, cls.root / "qualification")

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def setUp(self):
        self.case = Path(tempfile.mkdtemp(dir=self.root))
        self.capture_root = self.case / "trading-captures"
        self.path = helpers._write_v5_declaration(self.case, self.provider, self.capture_root)
        self.payload = json.loads(self.path.read_text())
        self.payload.update(protocolId=v10.PROTOCOL_ID, runId="synthetic_v10")
        self.payload["databasePath"] = str(self.case / "v10" / "ledger.sqlite3")
        self.payload["calibration"] = self.bind(self.calib_report_path, self.calib_report)
        self.payload["sourceReliability"] = {
            **copy.deepcopy(v10.SOURCE_RELIABILITY_BASE_CONTRACT),
            "captureImplementationSha256": self.impl["sha256"],
            "qualification": self.bind(self.qual_path, self.qual_report),
        }
        module = Path(v10.__file__).resolve()
        self.payload["implementation"]["files"].append({"path": str(module), "sha256": helpers._sha(module)})
        bound = {Path(row["path"]).resolve() for row in self.payload["implementation"]["files"]}
        project = BACKEND.parents[1]
        for name in sorted(v10.REQUIRED_IMPLEMENTATION_FILES):
            path = project / name
            if path not in bound:
                self.payload["implementation"]["files"].append({"path": str(path), "sha256": helpers._sha(path)})
        self.save()

    def bind(self, path, report):
        return {"reportPath": str(path), "fileSha256": helpers._sha(path), "reportSha256": report["reportSha256"]}

    def save(self):
        write_json(self.path, self.payload)

    def reject(self, pattern=None):
        with self.assertRaisesRegex(ValueError, pattern or ".+"):
            v10.IntradayShadowCoordinator(self.path)
        self.assertFalse(Path(self.payload["databasePath"]).parent.exists())

    def test_complete_242_and_three_source_evidence_initializes_v5_engine(self):
        coordinator = v10.IntradayShadowCoordinator(self.path)
        self.assertIsInstance(coordinator, V5)
        self.assertTrue(coordinator.db_path.is_file())
        self.assertEqual(coordinator.causality, helpers.json.loads(self.path.read_text())["causality"])
        self.assertEqual(coordinator.get_state()["sourceReliability"], self.payload["sourceReliability"])

    def test_old_protocol_and_changed_costs_fail_before_database(self):
        for key, value in [("protocolId", "v9"), ("execution", {**v10.EXECUTION, "buyCostRate": 0.})]:
            original = self.payload[key]
            self.payload[key] = value
            self.save()
            self.reject()
            self.payload[key] = original

    def test_v3_source_or_unbound_coordinator_is_rejected(self):
        self.payload["sourceReliability"]["realtimeContractSha256"] = "0" * 64
        self.save()
        self.reject("source contract")
        self.payload["sourceReliability"]["realtimeContractSha256"] = v10.REALTIME_CONTRACT_SHA256
        self.payload["implementation"]["files"] = [row for row in self.payload["implementation"]["files"] if row["path"] != str(Path(v10.__file__).resolve())]
        self.save()
        self.reject("bind its coordinator")

    def test_qualification_rebuild_uses_original_measurement_time(self):
        with patch.object(qualification, "build_source_qualification_report", wraps=qualification.build_source_qualification_report) as build:
            v10.IntradayShadowCoordinator(self.path)
        self.assertEqual(build.call_args.kwargs["measured_at"].isoformat(), self.qual_report["measuredAt"])
        self.assertEqual(set(build.call_args.kwargs), {"realtime_snapshot_path", "reference_snapshot_path", "candidate_snapshot_path", "provider_fingerprint", "measured_at"})

    def test_qualification_cannot_reuse_a_self_reported_pass(self):
        wrong = copy.deepcopy(self.qual_report)
        wrong["status"] = "FAIL"
        with patch.object(qualification, "build_source_qualification_report", return_value=wrong):
            self.reject("reproduces")

    def test_missing_242nd_slot_cannot_use_legacy_ninety_percent_pass(self):
        wrong = copy.deepcopy(self.calib_report)
        wrong["sourceManifest"].pop()
        with patch.object(calibration, "read_verified_calibration_report", return_value=wrong):
            self.reject("242")

    def test_recomputed_calibration_must_match_frozen_report(self):
        wrong = copy.deepcopy(self.calib_report)
        wrong["sourceManifestSha256"] = "0" * 64
        with patch.object(calibration, "build_calibration_report", return_value=wrong):
            self.reject("reproduces")

    def test_own_calibration_capture_can_be_reused_without_mutating_evidence(self):
        before = self.tree_bytes(self.calib_root)
        self.payload["captureRoot"] = str(self.calib_root)
        self.save()
        coordinator = v10.IntradayShadowCoordinator(self.path)
        self.assertEqual(coordinator.capture_root, self.calib_root)
        self.assertEqual(self.tree_bytes(self.calib_root), before)

    @staticmethod
    def tree_bytes(root):
        return {str(path.relative_to(root)): path.read_bytes()
                for path in root.rglob("*") if path.is_file()}

    def test_real_old_v9_database_path_rejected_without_touching_old_run(self):
        old = json.loads((BACKEND.parents[1] / "configs/intraday_shadow_20260910_t1_reversal_v9.json").read_text())
        target = Path(old["databasePath"])
        before = self.tree_bytes(target.parent)
        self.payload["databasePath"] = str(target)
        self.save()
        with self.assertRaisesRegex(ValueError, "overlap"):
            v10.IntradayShadowCoordinator(self.path)
        self.assertEqual(self.tree_bytes(target.parent), before)

    def test_historical_run_dir_parent_child_alias_and_capture_overlap_rejected(self):
        catalog = self.case / "configs"
        catalog.mkdir()
        historical_dir = self.case / "legacy"
        historical_dir.mkdir()
        database = historical_dir / "ledger.sqlite3"
        with sqlite3.connect(database) as db:
            db.execute("CREATE TABLE protected(value)")
            db.execute("INSERT INTO protected VALUES ('keep')")
        old_capture = self.case / "old-captures"
        old_capture.mkdir()
        (old_capture / "evidence.json").write_text("preserve")
        old = {"runId": "legacy_v9", "databasePath": str(database), "captureRoot": str(old_capture)}
        write_json(catalog / "intraday_shadow_old.json", old)
        alias = self.case / "legacy-alias"
        alias.symlink_to(historical_dir, target_is_directory=True)
        before = self.tree_bytes(historical_dir)
        before_capture = self.tree_bytes(old_capture)
        original = copy.deepcopy(self.payload)
        cases = [
            {"databasePath": str(database)},
            {"databasePath": str(alias / "ledger.sqlite3")},
            {"databasePath": str(historical_dir / "child/new.sqlite3")},
            {"databasePath": str(self.case / "ancestor.sqlite3")},
            {"captureRoot": str(old_capture / "child")},
            {"captureRoot": str(historical_dir / "capture")},
        ]
        with patch.object(v10, "HISTORICAL_DECLARATIONS_ROOT", catalog):
            for updates in cases:
                with self.subTest(updates=updates):
                    self.payload = {**original, **updates}
                    self.save()
                    with self.assertRaisesRegex(ValueError, "overlap"):
                        v10.IntradayShadowCoordinator(self.path)
                    self.assertEqual(self.tree_bytes(historical_dir), before)
                    self.assertEqual(self.tree_bytes(old_capture), before_capture)
                    self.assertFalse(Path(self.payload["databasePath"] + "-wal").exists())
                    self.assertFalse(Path(self.payload["databasePath"] + "-shm").exists())

    def test_arbitrary_existing_sqlite_rejected_before_pragma_or_schema_change(self):
        target = Path(self.payload["databasePath"])
        target.parent.mkdir()
        with sqlite3.connect(target) as db:
            db.execute("CREATE TABLE protected(value)")
            db.execute("INSERT INTO protected VALUES ('untouched')")
        before = self.tree_bytes(target.parent)
        with self.assertRaisesRegex(ValueError, "ownership"):
            v10.IntradayShadowCoordinator(self.path)
        self.assertEqual(self.tree_bytes(target.parent), before)

    def test_existing_v10_requires_exact_declaration_metadata_and_reopens(self):
        coordinator = v10.IntradayShadowCoordinator(self.path)
        # Finish any constructor WAL so immutable read sees a complete main DB.
        with sqlite3.connect(coordinator.db_path) as db:
            db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        before_validation = self.tree_bytes(coordinator.run_dir)
        validated = v10.validate_v10_declaration(self.path)
        self.assertEqual(validated["declarationSha256"], coordinator.declaration_sha256)
        self.assertEqual(self.tree_bytes(coordinator.run_dir), before_validation)
        reopened = v10.IntradayShadowCoordinator(self.path)
        self.assertEqual(reopened.run_id, coordinator.run_id)
        self.assertEqual(reopened.declaration_sha256, coordinator.declaration_sha256)
        before = self.tree_bytes(coordinator.run_dir)
        self.payload["runId"] = "another_v10"
        self.save()
        with self.assertRaisesRegex(ValueError, "metadata"):
            v10.IntradayShadowCoordinator(self.path)
        self.assertEqual(self.tree_bytes(coordinator.run_dir), before)

    def test_metadata_only_in_wal_fails_closed_without_changing_sidecars(self):
        target = Path(self.payload["databasePath"])
        target.parent.mkdir()
        connection = sqlite3.connect(target)
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA wal_autocheckpoint=0")
            connection.execute("CREATE TABLE shadow_metadata(key TEXT PRIMARY KEY,value TEXT)")
            connection.executemany("INSERT INTO shadow_metadata VALUES (?,?)", {
                "runId": self.payload["runId"], "declarationSha256": helpers._sha(self.path),
                "usageRealm": "INTRADAY_SHADOW", "formalPaperEligible": "false"}.items())
            connection.commit()
            before = self.tree_bytes(target.parent)
            self.assertIn(target.name + "-wal", before)
            with self.assertRaisesRegex(ValueError, "ownership"):
                v10.IntradayShadowCoordinator(self.path)
            self.assertEqual(self.tree_bytes(target.parent), before)
        finally:
            connection.close()

    def test_run_id_requires_v10_and_must_not_duplicate_another_declaration(self):
        self.payload["runId"] = "legacy_v9"
        self.save()
        self.reject("ending in _v10")
        self.payload["runId"] = "duplicate_v10"
        self.save()
        catalog = self.case / "configs"
        catalog.mkdir()
        write_json(catalog / "intraday_shadow_other.json", {
            "runId": "duplicate_v10", "databasePath": str(self.case / "other/db.sqlite3"),
            "captureRoot": str(self.case / "other-capture")})
        with patch.object(v10, "HISTORICAL_DECLARATIONS_ROOT", catalog):
            self.reject("already declared")

    def test_public_validator_valid_and_invalid_are_read_only_without_coordinator(self):
        before = self.tree_bytes(self.case)
        with patch.object(V5, "__init__", side_effect=AssertionError("no coordinator may be created")):
            result = v10.validate_v10_declaration(self.path)
        self.assertEqual(result["declarationSha256"], helpers._sha(self.path))
        self.assertEqual(result["providerContext"], self.context)
        self.assertEqual(result["captureImplementation"], self.impl)
        self.assertEqual(result["sourceQualificationReport"], self.qual_report)
        self.assertEqual(result["calibrationReport"], self.calib_report)
        self.assertEqual(self.tree_bytes(self.case), before)
        for key in ("quality", "qualityExtension", "causality"):
            original = self.payload[key]
            self.payload[key] = {}
            self.save()
            before_invalid = self.tree_bytes(self.case)
            with self.assertRaises(ValueError):
                v10.validate_v10_declaration(self.path)
            self.assertEqual(self.tree_bytes(self.case), before_invalid)
            self.payload[key] = original
        self.assertFalse(Path(self.payload["databasePath"]).parent.exists())

    def test_cli_reports_declaration_only_and_never_echoes_error_details(self):
        before = self.tree_bytes(self.case)
        output, error = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(error):
            code = verify_cli.main(["--declaration", str(self.path)])
        self.assertEqual(code, 0)
        result = json.loads(output.getvalue())
        self.assertEqual(result["status"], "DECLARATION_QUALIFIED")
        self.assertFalse(result["autoShadowAccepted"])
        self.assertTrue(result["runtimeNotTested"])
        self.assertEqual(error.getvalue(), "")
        self.assertEqual(self.tree_bytes(self.case), before)
        secret = "private-api-token-must-not-appear"
        for args, cause in [(["--declaration", str(self.path)], ValueError(secret)),
                            (["--unrecognized-" + secret], None)]:
            output, error = io.StringIO(), io.StringIO()
            with patch.object(v10, "validate_v10_declaration", side_effect=cause), redirect_stdout(output), redirect_stderr(error):
                code = verify_cli.main(args)
            self.assertEqual(code, 1)
            result = json.loads(output.getvalue())
            self.assertEqual(result["status"], "NOT_READY")
            self.assertFalse(result["autoShadowAccepted"])
            self.assertTrue(result["runtimeNotTested"])
            self.assertNotIn(secret, output.getvalue() + error.getvalue())
            self.assertEqual(self.tree_bytes(self.case), before)

    def test_future_only_and_causal_v5_timing_remain_required(self):
        self.payload["firstEligibleSignalDate"] = "2026-08-13"
        self.save()
        self.reject("future-only")

    def test_quote_contract_and_exact_universe_gate(self):
        coordinator = v10.IntradayShadowCoordinator(self.path)
        path = self.qual_report["evidence"]["realtime"]["file"]
        snapshot = capture.read_verified_snapshot(path)
        coordinator._quality_quote_map(snapshot, label="fixture")
        wrong = copy.deepcopy(snapshot)
        wrong["quotes"][0].pop("realtimeContractSha256")
        with self.assertRaisesRegex(ValueError, "every quote"):
            coordinator._quality_quote_map(wrong, label="fixture")
        wrong = copy.deepcopy(snapshot)
        wrong["universe"]["symbols"].reverse()
        with self.assertRaises(ValueError):
            coordinator._quality_quote_map(wrong, label="fixture")

    def test_quality_rejects_naive_timestamp_in_every_clock_and_quote(self):
        coordinator = v10.IntradayShadowCoordinator(self.path)
        snapshot = capture.read_verified_snapshot(self.qual_report["evidence"]["realtime"]["file"])
        for key in ("captureRequestedAt", "responseReceivedAt", "availableAt", "capturedAt"):
            with self.subTest(clock=key):
                wrong = copy.deepcopy(snapshot)
                wrong[key] = wrong[key].removesuffix("+08:00")
                with self.assertRaisesRegex(ValueError, "timezone aware"):
                    coordinator._quality_quote_map(wrong, label="fixture")
        for index in range(len(snapshot["quotes"])):
            with self.subTest(quote=index):
                wrong = copy.deepcopy(snapshot)
                wrong["quotes"][index]["quoteAsOf"] = wrong["quotes"][index]["quoteAsOf"].removesuffix("+08:00")
                with self.assertRaisesRegex(ValueError, "timezone aware"):
                    coordinator._quality_quote_map(wrong, label="fixture")
        wrong = copy.deepcopy(snapshot)
        for key in ("captureRequestedAt", "responseReceivedAt", "availableAt", "capturedAt"):
            wrong[key] = wrong[key].removesuffix("+08:00")
        for row in wrong["quotes"]:
            row["quoteAsOf"] = row["quoteAsOf"].removesuffix("+08:00")
        with self.assertRaisesRegex(ValueError, "timezone aware"):
            coordinator._quality_quote_map(wrong, label="fixture")

    def test_quality_rejects_backward_or_disagreeing_snapshot_clocks(self):
        coordinator = v10.IntradayShadowCoordinator(self.path)
        snapshot = capture.read_verified_snapshot(self.qual_report["evidence"]["realtime"]["file"])
        for key, clock in [
            ("captureRequestedAt", "09:30:01"),
            ("responseReceivedAt", "09:29:59"),
            ("availableAt", "09:29:59"),
            ("availableAt", "09:30:01"),
            ("capturedAt", "09:29:59"),
            ("capturedAt", "09:30:01"),
        ]:
            with self.subTest(clock=key, value=clock):
                wrong = copy.deepcopy(snapshot)
                wrong[key] = "2026-08-13T" + clock + "+08:00"
                with self.assertRaisesRegex(ValueError, "clock moved backwards or drifted"):
                    coordinator._quality_quote_map(wrong, label="fixture")

    def test_snapshot_accepts_verified_append_successor_and_rechecks_factor_bytes(self):
        coordinator = v10.IntradayShadowCoordinator(self.path)
        successor = helpers._write_provider(self.case / "successor", helpers.BASE_DATES)
        with patch.object(capture, "capture_implementation_contract", return_value=self.impl):
            capture.run_capture_cycle(successor, self.capture_root,
                now=datetime.fromisoformat("2026-08-14T14:50:00+08:00"), client=Client())
        result = coordinator._snapshot_at("2026-08-14", "14:50:00", datetime.fromisoformat("2026-08-14T14:51:00+08:00"))
        self.assertEqual(result[1]["provider"]["path"], str(successor))
        self.assertTrue(list(coordinator.lineage_dir.glob("*.json")))
        # A change to the APPENDED part also fails, even though the root prefix
        # still matches and the metadata fingerprint is unchanged.
        helpers._write_feature(successor / "features/sh600000/factor.day.bin", [1.] * (len(helpers.BASE_DATES) - 1) + [2.])
        with self.assertRaisesRegex(ValueError, "factor tree"):
            coordinator._snapshot_at("2026-08-14", "14:50:00", datetime.fromisoformat("2026-08-14T14:51:00+08:00"))
        helpers._write_feature(successor / "features/sh600000/factor.day.bin", [2.] * len(helpers.BASE_DATES))
        with self.assertRaises(ValueError):
            coordinator._snapshot_at("2026-08-14", "14:50:00", datetime.fromisoformat("2026-08-14T14:51:00+08:00"))

    def test_snapshot_metadata_is_checked_against_actual_provider(self):
        coordinator = v10.IntradayShadowCoordinator(self.path)
        with patch.object(capture, "capture_implementation_contract", return_value=self.impl):
            capture.run_capture_cycle(self.provider, self.capture_root,
                now=datetime.fromisoformat("2026-08-14T14:50:00+08:00"), client=Client())
        real = capture._provider_context
        def changed(path):
            return {**real(path), "csi300Sha256": "0" * 64}
        with patch.object(capture, "_provider_context", side_effect=changed):
            with self.assertRaisesRegex(ValueError, "metadata"):
                coordinator._snapshot_at("2026-08-14", "14:50:00", datetime.fromisoformat("2026-08-14T14:51:00+08:00"))

    def test_v10_keeps_two_phase_intent_binding_and_evidence_checkpoint(self):
        coordinator = v10.IntradayShadowCoordinator(self.path)
        with patch.object(capture, "capture_implementation_contract", return_value=self.impl):
            capture.run_capture_cycle(self.provider, self.capture_root,
                now=datetime.fromisoformat("2026-08-14T14:50:03+08:00"), client=Client())
        decision = coordinator.run_cycle(poll_seconds=60,
            now=datetime.fromisoformat("2026-08-14T14:50:20+08:00"))
        self.assertEqual(decision["status"], "DECISION_FROZEN_AWAITING_EXECUTION")
        with sqlite3.connect(coordinator.db_path) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM signal_sessions").fetchone()[0], 0)
        with patch.object(capture, "capture_implementation_contract", return_value=self.impl):
            capture.run_capture_cycle(self.provider, self.capture_root,
                now=datetime.fromisoformat("2026-08-14T14:55:03+08:00"), client=Client(price_shift=.01))
        execution = coordinator.run_cycle(poll_seconds=60,
            now=datetime.fromisoformat("2026-08-14T14:56:20+08:00"))
        self.assertEqual(execution["status"], "AWAITING_OFFICIAL_DAILY")
        with sqlite3.connect(coordinator.db_path) as db:
            self.assertEqual(db.execute("SELECT status FROM decision_intents").fetchone()[0], "EXECUTION_BOUND")
            self.assertEqual(db.execute("SELECT COUNT(*) FROM signals").fetchone()[0], 100)
        self.assertTrue(execution["evidenceCheckpoint"]["upToDate"])


if __name__ == "__main__":
    unittest.main()
