import gzip
import hashlib
import json
import stat
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


BACKEND_ROOT = Path(__file__).resolve().parents[1] / "frontend-v2" / "backend"
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from market_data.intraday_capture import (
    capture_implementation_contract,
    capture_schedule,
    list_verified_slot_records,
    read_capture_status,
    run_capture_cycle,
)


CHINA_TZ = ZoneInfo("Asia/Shanghai")


def _write_provider(root: Path) -> Path:
    provider = root / "provider"
    (provider / "calendars").mkdir(parents=True)
    (provider / "instruments").mkdir(parents=True)
    (provider / "calendars" / "day.txt").write_text(
        "2026-08-10\n2026-08-11\n", encoding="utf-8"
    )
    (provider / "calendars" / "day_future.txt").write_text(
        "2026-08-10\n2026-08-11\n2026-08-12\n2026-08-13\n2026-08-14\n",
        encoding="utf-8",
    )
    lines = [
        f"SH{600000 + index:06d}\t2020-01-01\t2026-12-31"
        for index in range(300)
    ]
    (provider / "instruments" / "csi300.txt").write_text(
        "\n".join(lines) + "\n", encoding="utf-8"
    )
    return provider


class _FakeClient:
    def __init__(self, trade_date: str = "2026-08-12", limit: int | None = None):
        self.trade_date = trade_date
        self.limit = limit
        self.calls = 0

    def realtime_quotes(self, codes: list[str], **kwargs):
        self.calls += 1
        selected = codes if self.limit is None else codes[: self.limit]
        reference = kwargs.get("now") or datetime(
            2026, 8, 12, 10, 0, tzinfo=CHINA_TZ
        )
        quote_as_of = reference.replace(
            year=int(self.trade_date[:4]),
            month=int(self.trade_date[5:7]),
            day=int(self.trade_date[8:10]),
        ).isoformat()
        return {
            code: {
                "symbol": code,
                "name": code,
                "price": 10.0,
                "previousClose": 9.9,
                "open": 9.95,
                "high": 10.1,
                "low": 9.9,
                "change": 0.1,
                "changePercent": 1.01,
                "amountCny": 1_000_000.0,
                "turnoverPercent": 0.5,
                "quoteAsOf": quote_as_of,
                "tradeDate": self.trade_date,
                "isStale": False,
                "staleReason": None,
                "source": "a-stock-data/tencent-public",
            }
            for code in selected
        }


class _ErrorClient:
    def realtime_quotes(self, codes: list[str], **kwargs):
        raise RuntimeError("public quote route unavailable")


class _UnexpectedClient(_FakeClient):
    def realtime_quotes(self, codes: list[str], **kwargs):
        result = super().realtime_quotes(codes, **kwargs)
        result["999999.SH"] = {
            **next(iter(result.values())),
            "symbol": "999999.SH",
        }
        return result


class _MismatchClient(_FakeClient):
    def realtime_quotes(self, codes: list[str], **kwargs):
        result = super().realtime_quotes(codes, **kwargs)
        result[codes[0]]["symbol"] = "999999.SH"
        return result


class _MixedDateClient(_FakeClient):
    def realtime_quotes(self, codes: list[str], **kwargs):
        result = super().realtime_quotes(codes, **kwargs)
        for code in codes[1:]:
            result[code]["tradeDate"] = "2026-08-11"
            result[code]["quoteAsOf"] = "2026-08-11T15:00:00+08:00"
        return result


class _OldTimeCurrentDateClient(_FakeClient):
    def realtime_quotes(self, codes: list[str], **kwargs):
        result = super().realtime_quotes(codes, **kwargs)
        for quote in result.values():
            quote["quoteAsOf"] = "2026-08-12T10:00:00+08:00"
        return result


class _SequenceClock:
    def __init__(self, values: list[datetime]):
        self.values = iter(values)

    def __call__(self) -> datetime:
        return next(self.values)


class IntradayCaptureTest(unittest.TestCase):
    def test_schedule_uses_provider_trading_dates_and_market_windows(self):
        dates = ["2026-08-12", "2026-08-13"]
        waiting = capture_schedule(
            datetime(2026, 8, 12, 8, 0, tzinfo=CHINA_TZ), dates
        )
        self.assertFalse(waiting["active"])
        self.assertEqual(waiting["nextWindowAt"], "2026-08-12T09:15:00+08:00")

        morning = capture_schedule(
            datetime(2026, 8, 12, 10, 0, tzinfo=CHINA_TZ), dates
        )
        self.assertTrue(morning["active"])

        lunch = capture_schedule(
            datetime(2026, 8, 12, 12, 0, tzinfo=CHINA_TZ), dates
        )
        self.assertFalse(lunch["active"])
        self.assertEqual(lunch["nextWindowAt"], "2026-08-12T13:00:00+08:00")

        closed = capture_schedule(
            datetime(2026, 8, 12, 16, 0, tzinfo=CHINA_TZ), dates
        )
        self.assertEqual(closed["nextWindowAt"], "2026-08-13T09:15:00+08:00")

    def test_closed_market_writes_status_without_requesting_quotes(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            provider = _write_provider(root)
            client = _FakeClient()
            status = run_capture_cycle(
                provider,
                root / "capture",
                now=datetime(2026, 8, 12, 2, 0, tzinfo=CHINA_TZ),
                client=client,
            )

            self.assertEqual(status["status"], "WAITING_MARKET")
            self.assertEqual(client.calls, 0)
            self.assertEqual(status["nextCaptureAt"], "2026-08-12T09:15:00+08:00")
            self.assertFalse((root / "capture" / "snapshots").exists())

    def test_capture_writes_immutable_hashed_snapshot(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            provider = _write_provider(root)
            output = root / "capture"
            now = datetime(2026, 8, 12, 10, 0, 15, tzinfo=CHINA_TZ)
            status = run_capture_cycle(provider, output, now=now, client=_FakeClient())

            self.assertEqual(status["status"], "CAPTURED")
            latest = status["latest"]
            self.assertEqual(latest["requestedCount"], 301)
            self.assertEqual(latest["quoteCount"], 301)
            self.assertEqual(latest["currentTradeDateQuoteCount"], 301)
            self.assertEqual(latest["usableQuoteCount"], 301)
            self.assertEqual(latest["rawCoverageRatio"], 1.0)
            self.assertEqual(latest["currentTradeDateCoverageRatio"], 1.0)
            self.assertEqual(latest["usableCoverageRatio"], 1.0)
            self.assertEqual(latest["coverageRatio"], 1.0)

            snapshot = Path(latest["file"])
            sidecar = snapshot.with_suffix(snapshot.suffix + ".sha256")
            self.assertEqual(stat.S_IMODE(snapshot.stat().st_mode), 0o444)
            self.assertEqual(stat.S_IMODE(sidecar.stat().st_mode), 0o444)
            archive_hash = hashlib.sha256(snapshot.read_bytes()).hexdigest()
            self.assertEqual(sidecar.read_text(encoding="ascii").split()[0], archive_hash)

            with gzip.open(snapshot, "rt", encoding="utf-8") as handle:
                payload = json.load(handle)
            self.assertEqual(payload["usageRealm"], "INTRADAY_RESEARCH_RAW")
            self.assertFalse(payload["evidenceEligible"])
            self.assertFalse(payload["trainingEligible"])
            self.assertFalse(payload["officialDailyEligible"])
            self.assertEqual(payload["captureRequestedAt"], now.isoformat())
            self.assertEqual(payload["responseReceivedAt"], now.isoformat())
            self.assertEqual(payload["availableAt"], now.isoformat())
            self.assertEqual(len(payload["universe"]["symbols"]), 301)
            self.assertEqual(len(payload["quotes"]), 301)
            self.assertEqual(read_capture_status(output)["latest"], latest)
            records = list_verified_slot_records(output, "2026-08-12")
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0][1]["status"], "CAPTURED")
            self.assertEqual(records[0][1]["snapshot"]["contentSha256"], payload["contentSha256"])

    def test_same_capture_slot_is_reused_without_another_request(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            provider = _write_provider(root)
            output = root / "capture"
            client = _FakeClient()
            now = datetime(2026, 8, 12, 10, 0, 15, tzinfo=CHINA_TZ)
            first = run_capture_cycle(provider, output, now=now, client=client)
            second = run_capture_cycle(
                provider,
                output,
                now=datetime(2026, 8, 12, 10, 0, 45, tzinfo=CHINA_TZ),
                client=_ErrorClient(),
            )

            self.assertEqual(first["status"], "CAPTURED")
            self.assertEqual(second["status"], "REUSED")
            self.assertEqual(client.calls, 1)
            self.assertEqual(second["latest"]["file"], first["latest"]["file"])

    def test_current_session_guard_does_not_persist_previous_day_quotes(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            provider = _write_provider(root)
            output = root / "capture"
            status = run_capture_cycle(
                provider,
                output,
                now=datetime(2026, 8, 12, 9, 16, tzinfo=CHINA_TZ),
                client=_FakeClient(trade_date="2026-08-11"),
            )

            self.assertEqual(status["status"], "WAITING_QUOTES")
            self.assertFalse((output / "snapshots").exists())

    def test_partial_snapshot_records_missing_symbols_without_claiming_completeness(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            provider = _write_provider(root)
            output = root / "capture"
            status = run_capture_cycle(
                provider,
                output,
                now=datetime(2026, 8, 12, 10, 1, tzinfo=CHINA_TZ),
                client=_FakeClient(limit=2),
            )

            self.assertEqual(status["status"], "CAPTURED")
            self.assertEqual(status["latest"]["quoteCount"], 2)
            self.assertEqual(status["latest"]["rawCoverageRatio"], 2 / 301)
            self.assertEqual(status["latest"]["currentTradeDateCoverageRatio"], 2 / 301)
            self.assertEqual(status["latest"]["usableCoverageRatio"], 2 / 301)
            self.assertEqual(status["latest"]["coverageRatio"], 2 / 301)
            with gzip.open(status["latest"]["file"], "rt", encoding="utf-8") as handle:
                payload = json.load(handle)
            self.assertEqual(len(payload["coverage"]["missingSymbols"]), 299)

    def test_unexpected_upstream_symbols_are_recorded_but_not_counted(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            provider = _write_provider(root)
            status = run_capture_cycle(
                provider,
                root / "capture",
                now=datetime(2026, 8, 12, 10, 2, tzinfo=CHINA_TZ),
                client=_UnexpectedClient(),
            )

            self.assertEqual(status["status"], "CAPTURED")
            self.assertEqual(status["latest"]["quoteCount"], 301)
            with gzip.open(status["latest"]["file"], "rt", encoding="utf-8") as handle:
                payload = json.load(handle)
            self.assertEqual(payload["coverage"]["unexpectedSymbols"], ["999999.SH"])

    def test_coverage_ratio_counts_only_current_usable_quotes(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            provider = _write_provider(root)
            status = run_capture_cycle(
                provider,
                root / "capture",
                now=datetime(2026, 8, 12, 10, 4, tzinfo=CHINA_TZ),
                client=_MixedDateClient(),
            )

            self.assertEqual(status["status"], "CAPTURED")
            latest = status["latest"]
            self.assertEqual(latest["quoteCount"], 301)
            self.assertEqual(latest["currentTradeDateQuoteCount"], 1)
            self.assertEqual(latest["usableQuoteCount"], 1)
            self.assertEqual(latest["rawCoverageRatio"], 1.0)
            self.assertEqual(latest["currentTradeDateCoverageRatio"], 1 / 301)
            self.assertEqual(latest["usableCoverageRatio"], 1 / 301)
            self.assertEqual(latest["coverageRatio"], 1 / 301)

    def test_slot_records_form_an_immutable_hash_chain(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            provider = _write_provider(root)
            output = root / "capture"
            for minute in (0, 1):
                status = run_capture_cycle(
                    provider,
                    output,
                    now=datetime(2026, 8, 12, 10, minute, tzinfo=CHINA_TZ),
                    client=_FakeClient(),
                )
                self.assertEqual(status["status"], "CAPTURED")

            records = list_verified_slot_records(output, "2026-08-12")
            self.assertEqual([item[1]["sequence"] for item in records], [1, 2])
            self.assertIsNone(records[0][1]["previousRecordSha256"])
            self.assertEqual(
                records[1][1]["previousRecordSha256"],
                records[0][1]["recordSha256"],
            )
            self.assertEqual(stat.S_IMODE(records[0][0].stat().st_mode), 0o444)

    def test_corrupt_mutable_status_is_recovered_without_stopping_capture(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            provider = _write_provider(root)
            output = root / "capture"
            output.mkdir()
            (output / "status.json").write_text("not-json", encoding="utf-8")

            status = run_capture_cycle(
                provider,
                output,
                now=datetime(2026, 8, 12, 2, 0, tzinfo=CHINA_TZ),
                client=_FakeClient(),
            )

            self.assertEqual(status["status"], "WAITING_MARKET")
            self.assertIn("statusRecoveryError", status)
            self.assertEqual(read_capture_status(output)["status"], "WAITING_MARKET")

    def test_same_day_provider_binding_drift_is_recorded_and_rejected(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            provider = _write_provider(root)
            output = root / "capture"
            first = run_capture_cycle(
                provider,
                output,
                now=datetime(2026, 8, 12, 10, 0, tzinfo=CHINA_TZ),
                client=_FakeClient(),
            )
            self.assertEqual(first["status"], "CAPTURED")
            instrument_path = provider / "instruments" / "csi300.txt"
            lines = instrument_path.read_text(encoding="utf-8").splitlines()
            instrument_path.write_text("\n".join(reversed(lines)) + "\n", encoding="utf-8")

            second = run_capture_cycle(
                provider,
                output,
                now=datetime(2026, 8, 12, 10, 1, tzinfo=CHINA_TZ),
                client=_FakeClient(),
            )

            self.assertEqual(second["status"], "ERROR")
            self.assertIn("binding drifted", second["lastError"])
            records = list_verified_slot_records(output, "2026-08-12")
            self.assertEqual([record[1]["status"] for record in records], ["CAPTURED", "ERROR"])

    def test_current_date_quote_older_than_120_seconds_is_not_usable(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            provider = _write_provider(root)
            status = run_capture_cycle(
                provider,
                root / "capture",
                now=datetime(2026, 8, 12, 10, 5, tzinfo=CHINA_TZ),
                client=_OldTimeCurrentDateClient(),
            )

            self.assertEqual(status["status"], "CAPTURED")
            self.assertEqual(status["latest"]["currentTradeDateQuoteCount"], 301)
            self.assertEqual(status["latest"]["usableQuoteCount"], 0)
            self.assertEqual(status["latest"]["usableCoverageRatio"], 0.0)

    def test_available_time_is_after_the_network_response(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            provider = _write_provider(root)
            clock = _SequenceClock(
                [
                    datetime(2026, 8, 12, 10, 0, 0, tzinfo=CHINA_TZ),
                    datetime(2026, 8, 12, 10, 0, 1, tzinfo=CHINA_TZ),
                    datetime(2026, 8, 12, 10, 0, 3, tzinfo=CHINA_TZ),
                    datetime(2026, 8, 12, 10, 0, 4, tzinfo=CHINA_TZ),
                ]
            )
            status = run_capture_cycle(
                provider,
                root / "capture",
                client=_FakeClient(),
                clock=clock,
            )

            with gzip.open(status["latest"]["file"], "rt", encoding="utf-8") as handle:
                payload = json.load(handle)
            self.assertEqual(payload["captureRequestedAt"], "2026-08-12T10:00:01+08:00")
            self.assertEqual(payload["responseReceivedAt"], "2026-08-12T10:00:03+08:00")
            self.assertEqual(payload["availableAt"], "2026-08-12T10:00:03+08:00")
            records = list_verified_slot_records(root / "capture", "2026-08-12")
            self.assertEqual(records[0][1]["committedAt"], "2026-08-12T10:00:04+08:00")

    def test_slot_record_deletion_breaks_the_sequence(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            provider = _write_provider(root)
            output = root / "capture"
            for minute in (0, 1):
                run_capture_cycle(
                    provider,
                    output,
                    now=datetime(2026, 8, 12, 10, minute, tzinfo=CHINA_TZ),
                    client=_FakeClient(),
                )
            records = list_verified_slot_records(output, "2026-08-12")
            records[0][0].chmod(0o644)
            records[0][0].unlink()

            with self.assertRaisesRegex(ValueError, "sequence is not contiguous"):
                list_verified_slot_records(output, "2026-08-12")

    def test_mismatched_quote_symbol_fails_closed(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            provider = _write_provider(root)
            status = run_capture_cycle(
                provider,
                root / "capture",
                now=datetime(2026, 8, 12, 10, 3, tzinfo=CHINA_TZ),
                client=_MismatchClient(),
            )

            self.assertEqual(status["status"], "ERROR")
            self.assertIn("quote symbol mismatch", status["lastError"])
            self.assertFalse((root / "capture" / "snapshots").exists())

    def test_errors_are_persisted_without_creating_snapshot(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            provider = _write_provider(root)
            output = root / "capture"
            status = run_capture_cycle(
                provider,
                output,
                now=datetime(2026, 8, 12, 10, 0, tzinfo=CHINA_TZ),
                client=_ErrorClient(),
            )

            self.assertEqual(status["status"], "ERROR")
            self.assertEqual(status["consecutiveErrors"], 1)
            self.assertIn("public quote route unavailable", status["lastError"])
            self.assertFalse((output / "snapshots").exists())

    def test_implementation_contract_binds_collector_and_adapter(self):
        contract = capture_implementation_contract()
        self.assertEqual(contract["version"], 1)
        self.assertEqual(len(contract["files"]), 2)
        self.assertEqual(len(contract["sha256"]), 64)
        self.assertTrue(all(len(item["sha256"]) == 64 for item in contract["files"]))


if __name__ == "__main__":
    unittest.main()
