import copy
import gzip
import json
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

BACKEND = Path(__file__).resolve().parents[1] / "frontend-v2/backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from market_data.intraday_capture import _canonical_json_bytes, _sha256_bytes
from market_data.intraday_source_qualification_v4 import (
    REALTIME_CONTRACT_SHA256, TCP, TENCENT, build_source_qualification_report,
    persist_source_qualification_report, read_verified_source_qualification_report,
)

TZ = ZoneInfo("Asia/Shanghai")
NOW = datetime(2026, 10, 23, 15, 5, tzinfo=TZ)
MODULE = "market_data.intraday_source_qualification_v4"


class SourceQualificationV4Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.symbols = [f"{i:06d}.SZ" for i in range(1, 301)] + ["000300.SH"]
        self.universe = {"symbols": self.symbols,
                         "symbolsSha256": _sha256_bytes("\n".join(self.symbols).encode("ascii"))}
        self.impl = {"files": [{"path": "fixture.py", "sha256": "a" * 64}],
                     "easyTdxPackageVersion": "1.20.6",
                     "realtimeContractSha256": REALTIME_CONTRACT_SHA256}
        self.impl["contractSha256"] = _sha256_bytes(_canonical_json_bytes(self.impl))
        self.patcher = patch(MODULE + "._implementation_contract", return_value=self.impl)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        self.runtime = {"entrypointModule": "market_data.intraday_capture_v4",
                        "providerModule": "market_data.providers.a_stock_data_intraday_v4",
                        "productionProviderModule": "market_data.providers.a_stock_data_intraday_v4",
                        "providerClass": "ResponseTimedRealtimeClient",
                        "productionProviderClass": "ResponseTimedRealtimeClient",
                        "clientInjected": False, "easyTdxPackageVersion": "1.20.6",
                        "realtimeContractSha256": REALTIME_CONTRACT_SHA256,
                        "prohibitCrossSourceFieldMerge": True}
        self.bound = {"files": self.impl["files"], "runtime": self.runtime}
        self.bound["sha256"] = _sha256_bytes(_canonical_json_bytes(self.bound))
        self.data = {"realtime": self.snapshot(TENCENT, "14:00:00", realtime=True),
                     "reference": self.snapshot(TENCENT, "15:00:00"),
                     "candidate": self.snapshot(TCP, "15:00:00")}

    def snapshot(self, source, clock, realtime=False):
        stamp = f"2026-10-23T{clock}+08:00"
        return {"quoteTradeDate": "2026-10-23", "provider": {"calendarSha256": "b" * 64},
                "universe": copy.deepcopy(self.universe), "captureRequestedAt": stamp,
                "responseReceivedAt": stamp, "availableAt": stamp,
                "implementation": copy.deepcopy(self.bound),
                "quotes": [{"symbol": s, "source": source, "tradeDate": "2026-10-23",
                            "quoteAsOf": stamp, "isStale": False, "price": 10.,
                            "previousClose": 9., "turnoverPercent": None if s == "000300.SH" else 0.4,
                            **({"realtimeContractSha256": REALTIME_CONTRACT_SHA256} if realtime else {})}
                           for s in self.symbols]}

    def write(self, label, payload):
        body = copy.deepcopy(payload)
        body["contentSha256"] = _sha256_bytes(_canonical_json_bytes(body))
        path = self.root / (label + ".json.gz")
        archive = gzip.compress(_canonical_json_bytes(body), mtime=0)
        path.write_bytes(archive)
        path.with_suffix(path.suffix + ".sha256").write_text(_sha256_bytes(archive) + "  " + path.name)
        return path

    def build(self, data=None, **updates):
        args = {label + "_snapshot_path": self.write(label, payload)
                for label, payload in (data or self.data).items()}
        args.update(provider_fingerprint="c" * 64, measured_at=NOW)
        args.update(updates)
        return build_source_qualification_report(**args)

    def test_complete_evidence_passes_redacted_and_roundtrips_idempotently(self):
        report = self.build()
        self.assertEqual(report["status"], "PASS")
        self.assertFalse(report["profitEvidenceEligible"])
        encoded = json.dumps(report)
        self.assertNotIn("000300.SH", encoded)
        self.assertNotIn('"quotes"', encoded)
        self.assertFalse(report["closeComparisonProvesRealtimeFreshness"])
        path = persist_source_qualification_report(report, self.root / "out")
        self.assertEqual(read_verified_source_qualification_report(path), report)
        self.assertEqual(persist_source_qualification_report(report, self.root / "out"), path)

    def test_injected_client_or_runtime_file_drift_cannot_qualify(self):
        for change in ("injected", "file", "entrypoint"):
            data = copy.deepcopy(self.data)
            bound = data["realtime"]["implementation"]
            if change == "injected": bound["runtime"]["clientInjected"] = True
            elif change == "file": bound["files"][0]["sha256"] = "e" * 64
            else: bound["runtime"]["entrypointModule"] = "market_data.intraday_capture_v3"
            bound["sha256"] = _sha256_bytes(_canonical_json_bytes({k: v for k, v in bound.items() if k != "sha256"}))
            self.assertEqual(self.build(data)["status"], "FAIL")

    def test_missing_evidence_waits_and_cannot_persist(self):
        report = self.build(candidate_snapshot_path=None)
        self.assertEqual(report["status"], "WAIT")
        with self.assertRaises(ValueError): persist_source_qualification_report(report, self.root / "out")

    def test_300_duplicate_and_extra_quotes_fail(self):
        for label in self.data:
            for change in ("missing", "duplicate", "extra"):
                with self.subTest(label=label, change=change):
                    data = copy.deepcopy(self.data)
                    rows = data[label]["quotes"]
                    if change == "missing": rows.pop()
                    elif change == "duplicate": rows[-1] = copy.deepcopy(rows[0])
                    else: rows.append(copy.deepcopy(rows[0]))
                    self.assertEqual(self.build(data)["status"], "FAIL")

    def test_dates_stale_missing_core_naive_times_and_source_fail(self):
        for label in self.data:
            changes = [{"tradeDate": "2026-10-22"}, {"isStale": True},
                       {"price": 0}, {"previousClose": None}, {"turnoverPercent": None},
                       {"quoteAsOf": "2026-10-23T15:00:00"},
                       {"quoteAsOf": "2026-10-22T15:00:00+08:00"}, {"source": "unknown"}]
            for change in changes:
                with self.subTest(label=label, change=change):
                    data = copy.deepcopy(self.data)
                    data[label]["quotes"][0].update(change)
                    self.assertEqual(self.build(data)["status"], "FAIL")

    def test_benchmark_requires_no_turnover_but_must_exist(self):
        self.assertEqual(self.build()["status"], "PASS")
        data = copy.deepcopy(self.data)
        data["candidate"]["quotes"][-1]["previousClose"] = None
        self.assertEqual(self.build(data)["status"], "FAIL")

    def test_intraday_price_comparison_rejected_even_when_prices_equal(self):
        for label in ("reference", "candidate"):
            data = copy.deepcopy(self.data)
            data[label] = self.snapshot(TENCENT if label == "reference" else TCP, "14:59:59")
            self.assertEqual(self.build(data)["status"], "FAIL")

    def test_post_close_raw_comparison_does_not_relax_realtime_age(self):
        data = copy.deepcopy(self.data)
        data["realtime"]["responseReceivedAt"] = "2026-10-23T14:02:01+08:00"
        data["realtime"]["availableAt"] = data["realtime"]["responseReceivedAt"]
        self.assertEqual(self.build(data)["status"], "FAIL")

    def test_realtime_age_and_future_boundaries(self):
        for stamp, valid in [("13:58:00", True), ("13:57:59", False),
                             ("14:00:05", True), ("14:00:06", False)]:
            data = copy.deepcopy(self.data)
            data["realtime"]["quotes"][0]["quoteAsOf"] = f"2026-10-23T{stamp}+08:00"
            self.assertEqual(self.build(data)["status"] == "PASS", valid)

    def test_clock_reversal_future_availability_and_v3_mixture_fail(self):
        for change in ("backward", "future", "v3"):
            data = copy.deepcopy(self.data)
            if change == "backward": data["realtime"]["captureRequestedAt"] = "2026-10-23T14:00:01+08:00"
            elif change == "future": data["candidate"]["availableAt"] = "2026-10-23T16:00:00+08:00"
            else: data["realtime"]["quotes"][0]["realtimeContractSha256"] = "a" * 64
            self.assertEqual(self.build(data)["status"], "FAIL")

    def test_universe_provider_and_package_drift_fail(self):
        data = copy.deepcopy(self.data)
        data["candidate"]["universe"]["symbolsSha256"] = "d" * 64
        self.assertEqual(self.build(data)["status"], "FAIL")
        data = copy.deepcopy(self.data)
        data["candidate"]["provider"]["calendarSha256"] = "d" * 64
        self.assertEqual(self.build(data)["status"], "FAIL")
        with patch(MODULE + "._implementation_contract", return_value={**self.impl, "easyTdxPackageVersion": "0"}):
            self.assertEqual(self.build()["status"], "FAIL")

    def test_comparison_thresholds_fail_closed(self):
        for field, value in [("price", 10.011), ("previousClose", 9.011), ("turnoverPercent", 0.421)]:
            data = copy.deepcopy(self.data)
            data["candidate"]["quotes"][0][field] = value
            self.assertEqual(self.build(data)["status"], "FAIL")

    def test_archive_and_content_hash_tampering_fail(self):
        report = self.build()
        path = self.root / "candidate.json.gz"
        archive = path.read_bytes() + b"tampered"
        path.write_bytes(archive)
        result = build_source_qualification_report(
            realtime_snapshot_path=self.root / "realtime.json.gz",
            reference_snapshot_path=self.root / "reference.json.gz", candidate_snapshot_path=path,
            provider_fingerprint="c" * 64, measured_at=NOW)
        self.assertEqual(result["status"], "FAIL")
        output = persist_source_qualification_report(report, self.root / "out")
        payload = json.loads(output.read_text())
        payload["providerFingerprint"] = "d" * 64
        output.chmod(0o600)
        output.write_text(json.dumps(payload))
        with self.assertRaises(ValueError): read_verified_source_qualification_report(output)

    def test_conflicting_second_report_not_overwritten(self):
        first = self.build()
        persist_source_qualification_report(first, self.root / "out")
        second = self.build(measured_at=datetime(2026, 10, 23, 15, 6, tzinfo=TZ))
        with self.assertRaises(ValueError): persist_source_qualification_report(second, self.root / "out")


if __name__ == "__main__":
    unittest.main()
