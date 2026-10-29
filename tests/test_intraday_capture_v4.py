"""V4 provenance tests use isolated temporary providers and no network."""
from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from tests.test_intraday_capture import _FakeClient, _write_provider, CHINA_TZ
from datetime import datetime
from market_data import intraday_capture as base
from market_data import intraday_capture_v3 as v3
from market_data import intraday_capture_v4 as v4
from market_data.providers.a_stock_data_intraday_v4 import (
    REALTIME_CONTRACT_SHA256, ResponseTimedRealtimeClient,
)

NOW = datetime(2026, 8, 12, 10, 0, tzinfo=CHINA_TZ)


class FixtureClient(ResponseTimedRealtimeClient):
    """Replace transports only; quote filtering/tagging remains the real V4 code."""
    def __init__(self):
        super().__init__(timeout=8, tdx_ping_timeout=5, tdx_query_timeout=8,
                         clock=lambda: NOW)
        self.fake = _FakeClient()

    def _tencent_realtime_quotes(self, requested):
        return self.fake.realtime_quotes([s for s, _ in requested], now=NOW)

    def _easy_tdx_realtime_quotes(self, requested):
        return {}

    def _eastmoney_realtime_quotes(self, requested, **kwargs):
        return {}

    def _sina_realtime_quotes(self, requested):
        return {}


class ForgedClient(FixtureClient):
    def realtime_quotes(self, codes, **kwargs):
        return {"600000.SH": {"realtimeContractSha256": REALTIME_CONTRACT_SHA256}}


class IntradayCaptureV4Tests(unittest.TestCase):
    def test_contract_binds_real_source_chain_and_dependency_files(self):
        contract = v4.capture_implementation_contract()
        paths = {row["path"] for row in contract["files"]}
        self.assertTrue({
            "frontend-v2/backend/market_data/intraday_capture.py",
            "frontend-v2/backend/market_data/intraday_capture_v4.py",
            "frontend-v2/backend/market_data/providers/a_stock_data.py",
            "frontend-v2/backend/market_data/providers/a_stock_data_intraday_v2.py",
            "frontend-v2/backend/market_data/providers/a_stock_data_intraday_v3.py",
            "frontend-v2/backend/market_data/providers/a_stock_data_intraday_v4.py",
            "requirements.txt", "frontend-v2/backend/requirements.txt",
        }.issubset(paths))
        runtime = contract["runtime"]
        self.assertEqual(runtime["entrypointModule"], v4.__name__)
        self.assertEqual(runtime["providerModule"], ResponseTimedRealtimeClient.__module__)
        self.assertEqual(runtime["providerClass"], "ResponseTimedRealtimeClient")
        self.assertFalse(runtime["clientInjected"])
        self.assertEqual(runtime["easyTdxPackageVersion"], "1.20.6")
        self.assertEqual(runtime["realtimeContractSha256"], REALTIME_CONTRACT_SHA256)
        self.assertEqual(runtime["timeouts"], {"http": 8.0, "tcpHandshake": 5.0, "tcpQuery": 8.0})
        self.assertEqual(contract["sourceContract"]["freshnessReference"], "source-response-received-at")
        payload = {k: v for k, v in contract.items() if k != "sha256"}
        self.assertEqual(contract["sha256"], base._sha256_bytes(base._canonical_json_bytes(payload)))

    def test_snapshot_and_slot_bind_actual_fixture_and_v4_provider_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            result = v4.run_capture_cycle(_write_provider(root), root / "capture",
                                          client=FixtureClient(), clock=lambda: NOW)
            self.assertEqual(result["status"], "CAPTURED")
            snapshot = base.read_verified_snapshot(result["latest"]["file"])
            contract = snapshot["implementation"]
            self.assertEqual(snapshot["coverage"]["usableQuoteCount"], 301)
            self.assertTrue(all(q["realtimeContractSha256"] == REALTIME_CONTRACT_SHA256
                                for q in snapshot["quotes"]))
            self.assertTrue(contract["runtime"]["clientInjected"])
            self.assertEqual(contract["runtime"]["providerClass"], "FixtureClient")
            self.assertEqual(contract["runtime"]["providerModule"], FixtureClient.__module__)
            self.assertIn("tests/test_intraday_capture_v4.py", {f["path"] for f in contract["files"]})
            records = base.list_verified_slot_records(root / "capture", "2026-08-12")
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0][1]["binding"]["implementationSha256"], contract["sha256"])
            self.assertFalse(snapshot["evidenceEligible"])
            self.assertFalse(snapshot["trainingEligible"])
        # No fixture-specific state leaks into a later production contract.
        self.assertFalse(v4.capture_implementation_contract()["runtime"]["clientInjected"])

    def test_base_and_v3_globals_and_contract_stay_unchanged(self):
        original = base.capture_implementation_contract
        before = original()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            v4.run_capture_cycle(_write_provider(root), root / "capture",
                                 client=FixtureClient(), clock=lambda: NOW)
        self.assertIs(base.capture_implementation_contract, original)
        self.assertIs(v3._run_capture_cycle, base.run_capture_cycle)
        self.assertIs(v3._run_capture_cycle.__globals__["capture_implementation_contract"], original)
        self.assertIsNot(v4._CORE.run_capture_cycle, base.run_capture_cycle)
        self.assertEqual(base.capture_implementation_contract(), before)

    def test_modified_file_hash_refuses_capture_before_any_output(self):
        original_hash = base._sha256_file
        wrapper_path = Path(v4.__file__).resolve()
        def changed(path):
            return "0" * 64 if Path(path).resolve() == wrapper_path else original_hash(path)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            provider = _write_provider(root)
            with patch.object(base, "_sha256_file", side_effect=changed):
                with self.assertRaisesRegex(RuntimeError, "source changed"):
                    v4.run_capture_cycle(provider, root / "capture", client=FixtureClient(), clock=lambda: NOW)
            self.assertFalse((root / "capture").exists())

    def test_dependency_or_provider_alias_drift_fails_closed(self):
        with patch.object(v4.metadata, "version", return_value="1.20.5"):
            with self.assertRaisesRegex(RuntimeError, "package version"):
                v4.capture_implementation_contract()
        with patch.object(v4, "ResponseTimedRealtimeClient", object):
            with self.assertRaisesRegex(RuntimeError, "runtime binding"):
                v4.capture_implementation_contract()

    def test_arbitrary_and_forged_public_client_methods_are_rejected(self):
        for client in (_FakeClient(), ForgedClient()):
            with self.subTest(client=type(client).__name__), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                with self.assertRaises(TypeError):
                    v4.run_capture_cycle(_write_provider(root), root / "capture", client=client, clock=lambda: NOW)
                self.assertFalse((root / "capture").exists())

    def test_legacy_snapshot_is_not_reused_or_relabelled_as_v4(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            provider = _write_provider(root)
            output = root / "capture"
            old = base.run_capture_cycle(provider, output, client=_FakeClient(), now=NOW)
            original_status = (output / "status.json").read_bytes()
            with self.assertRaisesRegex(RuntimeError, "different implementation"):
                v4.run_capture_cycle(provider, output, client=FixtureClient(), clock=lambda: NOW)
            self.assertEqual((output / "status.json").read_bytes(), original_status)
            self.assertEqual(len(base.list_verified_slot_records(output, "2026-08-12")), 1)
            self.assertEqual(base.read_verified_snapshot(old["latest"]["file"])["implementation"]["version"], 1)

    def test_source_drift_during_fetch_cannot_commit_a_captured_snapshot(self):
        state = {"changed": False}
        class DriftingFixture(FixtureClient):
            def _tencent_realtime_quotes(self, requested):
                result = super()._tencent_realtime_quotes(requested)
                state["changed"] = True
                return result
        original_hash = base._sha256_file
        wrapper_path = Path(v4.__file__).resolve()
        def changed(path):
            if state["changed"] and Path(path).resolve() == wrapper_path:
                return "0" * 64
            return original_hash(path)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.object(base, "_sha256_file", side_effect=changed):
                with self.assertRaisesRegex(RuntimeError, "source changed"):
                    v4.run_capture_cycle(_write_provider(root), root / "capture",
                                         client=DriftingFixture(), clock=lambda: NOW)
            records = base.list_verified_slot_records(root / "capture", "2026-08-12")
            self.assertEqual([r[1]["status"] for r in records], ["ERROR"])
            self.assertFalse(list((root / "capture").glob("snapshots/**/*.json.gz")))

    def test_quote_proxy_refuses_missing_or_wrong_v4_contract(self):
        for row in ({}, {"realtimeContractSha256": "0" * 64}):
            with self.subTest(row=row):
                fake = Mock()
                fake.realtime_quotes.return_value = {"600000.SH": row}
                with self.assertRaisesRegex(RuntimeError, "unbound quote"):
                    v4._VerifiedQuoteClient(fake).realtime_quotes(["600000.SH"])


if __name__ == "__main__":
    unittest.main()
