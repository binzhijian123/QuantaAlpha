import contextlib
import io
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch

from scripts import qualify_intraday_v4 as cli
from market_data.intraday_source_qualification_v4 import _read_evidence

MODULE = "scripts.qualify_intraday_v4"


class QualifyIntradayV4CliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.provider = self.root / "provider"
        self.output = self.root / "isolated"
        self.symbols = [f"{i:06d}.SZ" for i in range(1, 301)] + ["000300.SH"]
        self.context = {"providerPath": str(self.provider), "providerLatestDate": "2026-10-22",
                        "calendarPath": "calendar", "calendarSha256": "a" * 64,
                        "csi300Path": "csi300", "csi300Sha256": "b" * 64,
                        "symbols": self.symbols,
                        "symbolsSha256": cli._sha256_bytes("\n".join(self.symbols).encode("ascii")),
                        "tradingDates": ["2026-10-22", "2026-10-23"]}
        self.now = datetime(2026, 10, 23, 15, 1, tzinfo=cli.CHINA_TZ)
        self.snapshot = {"quoteTradeDate": "2026-10-23", "provider": cli._provider_metadata(self.context),
                         "universe": cli._universe_metadata(self.context),
                         "coverage": {"usableQuoteCount": 301},
                         "quotes": [{"symbol": s, "isStale": False,
                                     "quoteAsOf": "2026-10-23T15:00:00+08:00",
                                     "realtimeContractSha256": cli.REALTIME_CONTRACT_SHA256}
                                    for s in self.symbols]}
        for name, value in (("_provider_context", self.context),
                            ("provider_contract_fingerprint", {"fingerprint": "c" * 64}),
                            ("read_verified_snapshot", self.snapshot), ("_now", self.now)):
            p = patch(MODULE + "." + name, return_value=value)
            p.start()
            self.addCleanup(p.stop)

    def report(self, status="FAIL"):
        return {"status": status, "gateResults": {"allCoreFields": status == "PASS"},
                "profitEvidenceEligible": False, "reportSha256": "d" * 64}

    def test_preclose_is_wait_without_provider_read_or_network(self):
        with patch(MODULE + "._now", return_value=self.now.replace(hour=14, minute=59)), \
             patch(MODULE + ".ResponseTimedRealtimeClient") as client, \
             patch(MODULE + "._provider_context") as provider:
            code, payload = cli.collect_close(self.provider, self.root / "rt.gz", self.output)
        self.assertEqual(code, 2)
        self.assertEqual(payload["status"], "WAIT")
        client.assert_not_called()
        provider.assert_not_called()
        self.assertFalse(self.output.exists())

    def test_stale_date_or_wrong_provider_rejected_before_network(self):
        for change in ({"quoteTradeDate": "2026-10-22"}, {"provider": {"path": "another"}}):
            with patch(MODULE + ".read_verified_snapshot", return_value={**self.snapshot, **change}), \
                 patch(MODULE + ".ResponseTimedRealtimeClient") as client:
                with self.assertRaises(ValueError):
                    cli.collect_close(self.provider, self.root / "rt.gz", self.output)
                client.assert_not_called()

    def test_raw_sources_preserve_stale_and_time_flags_and_fail_not_persist(self):
        raw_quote = {"symbol": self.symbols[0], "isStale": True,
                     "quoteAsOf": "2026-10-22T14:00:00+08:00", "price": 10}
        client = Mock()
        client._tencent_realtime_quotes.return_value = {self.symbols[0]: raw_quote}
        client._easy_tdx_realtime_quotes.return_value = {}
        with patch(MODULE + ".ResponseTimedRealtimeClient", return_value=client), \
             patch(MODULE + ".build_source_qualification_report", return_value=self.report()) as build, \
             patch(MODULE + ".persist_source_qualification_report") as persist:
            code, summary = cli.collect_close(self.provider, self.root / "rt.gz", self.output)
        self.assertEqual(code, 1)
        self.assertEqual(summary["status"], "FAIL")
        persist.assert_not_called()
        kwargs = build.call_args.kwargs
        reference, _ = _read_evidence(kwargs["reference_snapshot_path"])
        self.assertEqual(reference["quotes"], [raw_quote])
        self.assertEqual(reference["providerFingerprint"], "c" * 64)
        requested = client._tencent_realtime_quotes.call_args.args[0]
        self.assertEqual(len(requested), 301)
        self.assertEqual(requested[-1], ("000300.SH", "sh000300"))
        self.assertTrue(Path(summary["diagnosticFile"]).is_file())
        self.assertNotIn("quotes", summary)

    def test_network_exception_is_redacted_and_candidate_still_queried(self):
        client = Mock()
        client._tencent_realtime_quotes.side_effect = RuntimeError("SECRET_TOKEN=never-output")
        client._easy_tdx_realtime_quotes.return_value = {}
        with patch(MODULE + ".ResponseTimedRealtimeClient", return_value=client), \
             patch(MODULE + ".build_source_qualification_report", return_value=self.report()):
            code, summary = cli.collect_close(self.provider, self.root / "rt.gz", self.output)
        self.assertEqual(code, 1)
        client._easy_tdx_realtime_quotes.assert_called_once()
        self.assertNotIn("SECRET", json.dumps(summary))
        self.assertNotIn("SECRET", Path(summary["diagnosticFile"]).read_text())

    def test_only_builder_pass_can_persist(self):
        client = Mock()
        client._tencent_realtime_quotes.return_value = {}
        client._easy_tdx_realtime_quotes.return_value = {}
        with patch(MODULE + ".ResponseTimedRealtimeClient", return_value=client), \
             patch(MODULE + ".build_source_qualification_report", return_value=self.report("PASS")), \
             patch(MODULE + ".persist_source_qualification_report", return_value=self.output / "report.json") as persist:
            code, summary = cli.collect_close(self.provider, self.root / "rt.gz", self.output)
        self.assertEqual(code, 0)
        self.assertEqual(summary["status"], "PASS")
        persist.assert_called_once()

    def test_provider_drift_during_io_is_rejected(self):
        client = Mock()
        client._tencent_realtime_quotes.return_value = {}
        client._easy_tdx_realtime_quotes.return_value = {}
        with patch(MODULE + ".ResponseTimedRealtimeClient", return_value=client), \
             patch(MODULE + "._provider_context", side_effect=[self.context, {**self.context, "calendarSha256": "e" * 64}]), \
             patch(MODULE + ".build_source_qualification_report") as builder:
            with self.assertRaises(ValueError):
                cli.collect_close(self.provider, self.root / "rt.gz", self.output)
            builder.assert_not_called()

    def test_realtime_300_is_not_complete_or_qualification(self):
        snapshot = {**self.snapshot, "coverage": {"usableQuoteCount": 300}, "quotes": self.snapshot["quotes"][:-1]}
        with patch(MODULE + ".run_capture_cycle", return_value={"status": "CAPTURED", "latest": {"file": "snapshot.gz"}}) as run, \
             patch(MODULE + ".read_verified_snapshot", return_value=snapshot):
            code, summary = cli.collect_realtime(self.provider, self.output)
        self.assertEqual(code, 1)
        self.assertFalse(summary["qualificationPassed"])
        self.assertEqual(summary["usableQuoteCount"], 300)
        self.assertNotIn("client", run.call_args.kwargs)

    def test_realtime_complete_only_collects_stage(self):
        with patch(MODULE + ".run_capture_cycle", return_value={"status": "CAPTURED", "latest": {"file": "snapshot.gz"}}):
            code, summary = cli.collect_realtime(self.provider, self.output)
        self.assertEqual(code, 0)
        self.assertEqual(summary["status"], "REALTIME_STAGE_COLLECTED")
        self.assertFalse(summary["qualificationPassed"])

    def test_realtime_market_wait_and_exception_exit_codes(self):
        with patch(MODULE + ".run_capture_cycle", return_value={"status": "WAITING_MARKET"}):
            self.assertEqual(cli.collect_realtime(self.provider, self.output)[0], 2)
        stream = io.StringIO()
        with patch(MODULE + ".collect_realtime", side_effect=RuntimeError("SECRET")), contextlib.redirect_stdout(stream):
            self.assertEqual(cli.main(["realtime", "--provider", str(self.provider)]), 1)
        self.assertNotIn("SECRET", stream.getvalue())

    def test_waiting_market_never_reuses_cached_snapshot_as_fresh_collection(self):
        with patch(MODULE + ".run_capture_cycle", return_value={"status": "WAITING_MARKET", "latest": {"file": "old.gz"}}), \
             patch(MODULE + ".read_verified_snapshot") as read:
            code, summary = cli.collect_realtime(self.provider, self.output)
        self.assertEqual(code, 2)
        self.assertFalse(summary["qualificationPassed"])
        read.assert_not_called()

    def test_frozen_capture_output_refused(self):
        with self.assertRaises(ValueError): cli._safe_output(cli.PROTECTED_OUTPUT)
        with self.assertRaises(ValueError): cli._safe_output(cli.PROTECTED_OUTPUT / "new")
        self.assertEqual(cli._safe_output(self.output), self.output.resolve())


if __name__ == "__main__":
    unittest.main()
