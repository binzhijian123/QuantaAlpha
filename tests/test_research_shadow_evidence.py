import hashlib
import json
import os
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from quantaalpha.trading.paper import PaperTradingEngine
from quantaalpha.trading.research_shadow_evidence import (
    ensure_evidence_checkpoint,
    validate_evidence_checkpoint_chain,
)
from quantaalpha.utils.qlib_provider import provider_contract_fingerprint


SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _canonical(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class _Coordinator:
    def __init__(self, state):
        self.state = deepcopy(state)

    def get_state(self):
        return deepcopy(self.state)


def _arm_state(engine: PaperTradingEngine, arm: str, snapshot_id: str) -> dict:
    state = engine.get_state(arm, market_calendar=["2026-08-11"])
    state.update(
        {
            "arm": arm,
            "label": arm,
            "strategy": {
                "snapshotId": snapshot_id,
                "providerFingerprint": "provider",
                "validationStatus": "RESEARCH_ONLY",
                "featureCount": 20,
                "executionContract": {"version": 2, "topK": 50},
            },
        }
    )
    return state


def _environment(root: Path) -> dict:
    provider = root / "provider"
    (provider / "calendars").mkdir(parents=True)
    (provider / "instruments").mkdir()
    (provider / "calendars" / "day.txt").write_text(
        "2026-08-11\n", encoding="utf-8"
    )
    (provider / "instruments" / "csi300.txt").write_text(
        "SH600000\t2026-08-11\t2026-08-11\n", encoding="utf-8"
    )
    provider_fingerprint = provider_contract_fingerprint(provider)["fingerprint"]

    run_dir = root / "run"
    snapshots = run_dir / "snapshots"
    snapshots.mkdir(parents=True)
    snapshot_paths = {arm: snapshots / f"{arm}.pkl" for arm in ("C1", "B0")}
    for arm, path in snapshot_paths.items():
        path.write_bytes(f"snapshot-{arm}".encode("ascii"))

    database = run_dir / "shadow.sqlite3"
    engine = PaperTradingEngine(database)
    engine.ensure_account("C1", "C1")
    engine.ensure_account("B0", "B0")
    with sqlite3.connect(database) as connection:
        connection.executescript(
            """
            CREATE TABLE shadow_metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE shadow_audit (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                event_type TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                previous_hash TEXT NOT NULL,
                event_hash TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL
            );
            """
        )

    declaration_path = root / "declaration.json"
    manifest_path = run_dir / "manifest.json"
    declaration = {
        "version": 1,
        "runId": "shadow-test-v1",
        "usageRealm": "RESEARCH_SHADOW",
        "formalPaperEligible": False,
        "providerPath": str(provider),
        "providerFingerprint": provider_fingerprint,
        "providerLineageProofDir": str(run_dir / "provider_lineage"),
        "databasePath": str(database),
        "manifestPath": str(manifest_path),
        "arms": {
            arm: {"snapshotPath": str(path)} for arm, path in snapshot_paths.items()
        },
    }
    declaration_path.write_text(
        json.dumps(declaration, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    manifest = {
        "runId": declaration["runId"],
        "usageRealm": "RESEARCH_SHADOW",
        "declarationSha256": _sha256(declaration_path),
        "arms": {
            arm: {"snapshotSha256": _sha256(path)}
            for arm, path in snapshot_paths.items()
        },
    }
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    state = {
        "runId": declaration["runId"],
        "usageRealm": "RESEARCH_SHADOW",
        "status": "WAITING_OFFICIAL_DAILY",
        "nextAction": "WAIT_FOR_NEXT_OFFICIAL_PROVIDER",
        "marketData": {
            "providerPath": str(provider),
            "latestDate": "2026-08-11",
            "frozenProviderFingerprint": provider_fingerprint,
        },
        "protocol": {"topK": 50},
        "pairedEvidence": {
            "observationSessions": 0,
            "datesAligned": True,
            "evidenceEligible": False,
        },
        "arms": {
            "C1": _arm_state(engine, "C1", "snapshot-c1"),
            "B0": _arm_state(engine, "B0", "snapshot-b0"),
        },
        "audit": {
            "valid": True,
            "events": 0,
            "head": None,
            "lastEvent": None,
            "lastEventAt": None,
        },
    }
    return {
        "declaration": declaration_path,
        "database": database,
        "state": state,
    }


def _append_audited_cash_change(environment: dict, cash: float) -> None:
    created_at = "2026-08-12T15:01:00+08:00"
    event_type = "TEST_AUDITED_CHANGE"
    payload_json = _canonical({"cash": cash})
    previous = "0" * 64
    event_hash = hashlib.sha256(
        (previous + "\0" + event_type + "\0" + payload_json + "\0" + created_at).encode(
            "utf-8"
        )
    ).hexdigest()
    with sqlite3.connect(environment["database"]) as connection:
        connection.execute("UPDATE accounts SET cash = ? WHERE id = 'C1'", (cash,))
        connection.execute(
            "INSERT INTO shadow_audit(event_type, payload_json, previous_hash, "
            "event_hash, created_at) VALUES (?, ?, ?, ?, ?)",
            (event_type, payload_json, previous, event_hash, created_at),
        )
    environment["state"]["arms"]["C1"]["account"].update(
        {
            "cash": cash,
            "totalValue": cash,
            "totalReturn": cash / 1_000_000.0 - 1.0,
        }
    )
    environment["state"]["audit"] = {
        "valid": True,
        "events": 1,
        "head": event_hash,
        "lastEvent": event_type,
        "lastEventAt": created_at,
    }


def _ensure(environment: dict):
    return ensure_evidence_checkpoint(
        environment["declaration"],
        now=datetime(2026, 8, 12, 15, 5, tzinfo=SHANGHAI_TZ),
        coordinator_factory=lambda _path: _Coordinator(environment["state"]),
    )


class ResearchShadowEvidenceTests(unittest.TestCase):
    def test_active_run_requires_frozen_execution_implementation(self):
        with tempfile.TemporaryDirectory() as tmp:
            environment = _environment(Path(tmp))
            environment["state"]["lifecycle"] = {"status": "ACTIVE"}

            with self.assertRaisesRegex(ValueError, "implementation is not frozen"):
                _ensure(environment)

    def test_checkpoint_explicitly_binds_frozen_execution_implementation(self):
        with tempfile.TemporaryDirectory() as tmp:
            environment = _environment(Path(tmp))
            implementation = {
                "version": 1,
                "files": [{"path": "paper.py", "sha256": "a" * 64}],
                "runtime": {"pythonVersion": "3.10.20"},
                "sha256": "b" * 64,
                "frozen": True,
            }
            environment["state"].update(
                {
                    "lifecycle": {"status": "ACTIVE"},
                    "executionImplementation": implementation,
                    "executionContract": {"sha256": "c" * 64, "frozen": True},
                }
            )

            summary = _ensure(environment)
            checkpoint = Path(summary["directory"]) / summary["latest"]["file"]
            payload = json.loads(checkpoint.read_text(encoding="utf-8"))

            self.assertEqual(
                payload["evidence"]["state"]["executionImplementation"],
                implementation,
            )

    def test_unchanged_state_reuses_one_sqlite_checkpoint(self):
        with tempfile.TemporaryDirectory() as tmp:
            environment = _environment(Path(tmp))

            created = _ensure(environment)
            reused = _ensure(environment)
            verified = validate_evidence_checkpoint_chain(environment["declaration"])

            self.assertEqual(created["status"], "CREATED")
            self.assertEqual(reused["status"], "REUSED")
            self.assertEqual(verified["count"], 1)
            sqlite_path = Path(verified["directory"]) / verified["latest"]["sqliteFile"]
            self.assertTrue(sqlite_path.is_file())
            self.assertEqual(os.stat(sqlite_path).st_mode & 0o222, 0)

    def test_audited_ledger_append_creates_a_linked_checkpoint(self):
        with tempfile.TemporaryDirectory() as tmp:
            environment = _environment(Path(tmp))
            first = _ensure(environment)
            _append_audited_cash_change(environment, 999_000.0)

            second = _ensure(environment)
            verified = validate_evidence_checkpoint_chain(environment["declaration"])

            self.assertEqual(first["count"], 1)
            self.assertEqual(second["status"], "CREATED")
            self.assertEqual(verified["count"], 2)
            self.assertEqual(verified["latest"]["auditEvents"], 1)

    def test_unaudited_ledger_change_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            environment = _environment(Path(tmp))
            _ensure(environment)
            with sqlite3.connect(environment["database"]) as connection:
                connection.execute("UPDATE accounts SET cash = 999000 WHERE id = 'C1'")
            environment["state"]["arms"]["C1"]["account"].update(
                {"cash": 999_000.0, "totalValue": 999_000.0, "totalReturn": -0.001}
            )

            with self.assertRaisesRegex(ValueError, "without an audit-chain advance"):
                _ensure(environment)

    def test_json_and_sqlite_tampering_are_detected(self):
        for artifact in ("json", "sqlite"):
            with self.subTest(artifact=artifact), tempfile.TemporaryDirectory() as tmp:
                environment = _environment(Path(tmp))
                summary = _ensure(environment)
                directory = Path(summary["directory"])
                filename = (
                    summary["latest"]["file"]
                    if artifact == "json"
                    else summary["latest"]["sqliteFile"]
                )
                path = directory / filename
                os.chmod(path, 0o644)
                with path.open("ab") as stream:
                    stream.write(b"tamper")

                with self.assertRaisesRegex(ValueError, "SHA256"):
                    validate_evidence_checkpoint_chain(environment["declaration"])

    def test_previous_link_tampering_is_detected_even_with_new_sidecar(self):
        with tempfile.TemporaryDirectory() as tmp:
            environment = _environment(Path(tmp))
            _ensure(environment)
            _append_audited_cash_change(environment, 999_000.0)
            summary = _ensure(environment)
            path = Path(summary["directory"]) / summary["latest"]["file"]
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["previousCheckpoint"]["sha256"] = "0" * 64
            os.chmod(path, 0o644)
            path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            sidecar = path.with_suffix(path.suffix + ".sha256")
            os.chmod(sidecar, 0o644)
            sidecar.write_text(_sha256(path) + "\n", encoding="ascii")

            with self.assertRaisesRegex(ValueError, "previous link"):
                validate_evidence_checkpoint_chain(environment["declaration"])

    def test_unreferenced_checkpoint_artifact_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            environment = _environment(Path(tmp))
            summary = _ensure(environment)
            orphan = Path(summary["directory"]) / "checkpoint_999999_orphan.sqlite3"
            orphan.write_bytes(b"orphan")

            with self.assertRaisesRegex(ValueError, "unexpected artifacts"):
                validate_evidence_checkpoint_chain(environment["declaration"])

    def test_concurrent_writers_do_not_duplicate_sequence(self):
        with tempfile.TemporaryDirectory() as tmp:
            environment = _environment(Path(tmp))
            with ThreadPoolExecutor(max_workers=2) as executor:
                results = list(executor.map(lambda _: _ensure(environment), range(2)))

            verified = validate_evidence_checkpoint_chain(environment["declaration"])
            self.assertEqual({result["status"] for result in results}, {"CREATED", "REUSED"})
            self.assertEqual(verified["count"], 1)


if __name__ == "__main__":
    unittest.main()
