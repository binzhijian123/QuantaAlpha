from __future__ import annotations

import fcntl
import gzip
import hashlib
import io
import json
import math
import os
import platform
import tempfile
from contextlib import contextmanager
from datetime import date, datetime, time, timedelta
from importlib import metadata
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Protocol
from zoneinfo import ZoneInfo

from .providers import a_stock_data as a_stock_data_module
from .providers.a_stock_data import AStockDataClient


CHINA_TZ = ZoneInfo("Asia/Shanghai")
SCHEMA_VERSION = 1
BENCHMARK_SYMBOL = "000300.SH"
MORNING_WINDOW = (time(9, 15), time(11, 31))
AFTERNOON_WINDOW = (time(13, 0), time(15, 1))
DEFAULT_OUTPUT_ROOT = Path("data/market/a_share/intraday/a_stock_data_csi300")
_PROCESS_IMPLEMENTATION_FILE_HASHES: dict[str, str] | None = None


class QuoteClient(Protocol):
    def realtime_quotes(
        self,
        codes: list[str],
        *,
        expected_trade_date: str | None = None,
        max_quote_age_seconds: int | None = None,
        now: datetime | None = None,
    ) -> dict[str, dict[str, Any]]: ...


def _canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_write(path: Path, payload: bytes, *, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_path = Path(handle.name)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary_path, mode)
        os.replace(temporary_path, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()


def _immutable_write(path: Path, payload: bytes) -> None:
    if path.exists():
        if path.read_bytes() != payload:
            raise ValueError(f"immutable intraday artifact already exists with other bytes: {path}")
        return
    _atomic_write(path, payload, mode=0o444)


def _write_status(output_root: Path, status: Mapping[str, Any]) -> None:
    _atomic_write(
        output_root / "status.json",
        json.dumps(status, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")
        + b"\n",
        mode=0o644,
    )


def read_capture_status(output_root: Path | str) -> dict[str, Any] | None:
    path = Path(output_root).expanduser().resolve() / "status.json"
    if not path.exists():
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("version") != SCHEMA_VERSION:
        raise ValueError("intraday capture status has an unsupported schema")
    return payload


def update_capture_calibration_status(
    output_root: Path | str,
    calibration: Mapping[str, Any],
) -> dict[str, Any]:
    output = Path(output_root).expanduser().resolve()
    with _capture_lock(output):
        status = read_capture_status(output)
        if status is None:
            raise ValueError("capture status does not exist")
        updated = {**status, "calibration": dict(calibration)}
        _write_status(output, updated)
        return updated


@contextmanager
def _capture_lock(output_root: Path) -> Iterator[None]:
    output_root.mkdir(parents=True, exist_ok=True)
    lock_path = output_root / ".capture.lock"
    with lock_path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _distribution_version(name: str) -> str:
    try:
        return metadata.version(name)
    except metadata.PackageNotFoundError:
        return "MISSING"


def capture_implementation_contract() -> dict[str, Any]:
    project_root = Path(__file__).resolve().parents[3]
    files = []
    current_file_hashes: dict[str, str] = {}
    for path in sorted(
        {
            Path(__file__).resolve(),
            Path(a_stock_data_module.__file__).resolve(),
        }
    ):
        try:
            display_path = str(path.relative_to(project_root))
        except ValueError:
            display_path = path.name
        file_hash = _sha256_file(path)
        current_file_hashes[display_path] = file_hash
        files.append({"path": display_path, "sha256": file_hash})
    global _PROCESS_IMPLEMENTATION_FILE_HASHES
    if _PROCESS_IMPLEMENTATION_FILE_HASHES is None:
        _PROCESS_IMPLEMENTATION_FILE_HASHES = current_file_hashes
    elif _PROCESS_IMPLEMENTATION_FILE_HASHES != current_file_hashes:
        raise RuntimeError(
            "intraday collector source changed in a running process; restart is required"
        )
    contract = {
        "version": 1,
        "files": files,
        "runtime": {
            "pythonImplementation": platform.python_implementation(),
            "pythonVersion": platform.python_version(),
            "requests": _distribution_version("requests"),
            "pandas": _distribution_version("pandas"),
        },
    }
    return {**contract, "sha256": _sha256_bytes(_canonical_json_bytes(contract))}


def _provider_context(provider_path: Path) -> dict[str, Any]:
    provider = provider_path.expanduser().resolve()
    day_path = provider / "calendars" / "day.txt"
    future_path = provider / "calendars" / "day_future.txt"
    instrument_path = provider / "instruments" / "csi300.txt"
    for path in (day_path, instrument_path):
        if not path.is_file():
            raise FileNotFoundError(f"required Provider file is missing: {path}")

    official_dates = [
        value.strip()
        for value in day_path.read_text(encoding="utf-8").splitlines()
        if value.strip()
    ]
    if not official_dates or official_dates != sorted(set(official_dates)):
        raise ValueError("Provider day calendar is empty or not strictly ordered")
    latest_date = official_dates[-1]

    symbols: set[str] = set()
    for line in instrument_path.read_text(encoding="utf-8").splitlines():
        parts = line.split("\t")
        if len(parts) < 3 or not (parts[1] <= latest_date <= parts[2]):
            continue
        qlib_symbol = parts[0].strip().upper()
        if len(qlib_symbol) != 8 or qlib_symbol[:2] not in {"SH", "SZ"}:
            raise ValueError(f"unsupported CSI300 instrument code: {qlib_symbol}")
        symbols.add(f"{qlib_symbol[2:]}.{qlib_symbol[:2]}")
    if len(symbols) != 300:
        raise ValueError(
            f"Provider must have 300 active CSI300 members on {latest_date}, got {len(symbols)}"
        )

    calendar_path = future_path if future_path.is_file() else day_path
    trading_dates = [
        value.strip()
        for value in calendar_path.read_text(encoding="utf-8").splitlines()
        if value.strip()
    ]
    if not trading_dates or trading_dates != sorted(set(trading_dates)):
        raise ValueError("Provider capture calendar is empty or not strictly ordered")

    requested_symbols = sorted(symbols | {BENCHMARK_SYMBOL})
    return {
        "providerPath": str(provider),
        "providerLatestDate": latest_date,
        "calendarPath": str(calendar_path),
        "calendarSha256": _sha256_file(calendar_path),
        "csi300Path": str(instrument_path),
        "csi300Sha256": _sha256_file(instrument_path),
        "tradingDates": trading_dates,
        "symbols": requested_symbols,
        "symbolsSha256": _sha256_bytes("\n".join(requested_symbols).encode("ascii")),
    }


def capture_schedule(now: datetime, trading_dates: list[str]) -> dict[str, Any]:
    local_now = now.astimezone(CHINA_TZ)
    today = local_now.date().isoformat()
    available_dates = sorted(set(trading_dates))
    if today in available_dates:
        local_time = local_now.time().replace(tzinfo=None)
        for start, end in (MORNING_WINDOW, AFTERNOON_WINDOW):
            if start <= local_time < end:
                return {
                    "active": True,
                    "status": "ACTIVE",
                    "nextWindowAt": None,
                }
            if local_time < start:
                return {
                    "active": False,
                    "status": "WAITING_MARKET",
                    "nextWindowAt": datetime.combine(
                        local_now.date(), start, tzinfo=CHINA_TZ
                    ).isoformat(),
                }

    next_date = next(
        (date.fromisoformat(value) for value in available_dates if value > today),
        None,
    )
    return {
        "active": False,
        "status": "WAITING_MARKET" if next_date else "CALENDAR_EXHAUSTED",
        "nextWindowAt": (
            datetime.combine(next_date, MORNING_WINDOW[0], tzinfo=CHINA_TZ).isoformat()
            if next_date
            else None
        ),
    }


def _capture_slot(now: datetime, poll_seconds: int) -> datetime:
    epoch_seconds = int(now.timestamp())
    slot_epoch = epoch_seconds - (epoch_seconds % poll_seconds)
    return datetime.fromtimestamp(slot_epoch, tz=CHINA_TZ)


def _usable_capture_quote(
    quote: Mapping[str, Any],
    *,
    trade_date: str,
    available_at: datetime,
    max_age_seconds: int = 120,
    max_future_lead_seconds: int = 5,
) -> bool:
    price = quote.get("price")
    if (
        quote.get("tradeDate") != trade_date
        or quote.get("isStale")
        or not isinstance(price, (int, float))
        or isinstance(price, bool)
        or not math.isfinite(float(price))
        or float(price) <= 0
    ):
        return False
    try:
        quote_time = datetime.fromisoformat(str(quote.get("quoteAsOf"))).astimezone(
            CHINA_TZ
        )
    except (TypeError, ValueError):
        return False
    age_seconds = (available_at - quote_time).total_seconds()
    return -max_future_lead_seconds <= age_seconds <= max_age_seconds


def _snapshot_summary(path: Path, payload: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "file": str(path),
        "contentSha256": str(payload["contentSha256"]),
        "archiveSha256": _sha256_file(path),
        "capturedAt": payload["capturedAt"],
        "captureSlot": payload["captureSlot"],
        "quoteAsOf": payload["quoteAsOfMax"],
        "quoteTradeDate": payload["quoteTradeDate"],
        "providerLatestDate": payload["provider"]["latestDate"],
        "requestedCount": payload["coverage"]["requestedCount"],
        "quoteCount": payload["coverage"]["quoteCount"],
        "currentTradeDateQuoteCount": payload["coverage"][
            "currentTradeDateQuoteCount"
        ],
        "usableQuoteCount": payload["coverage"]["usableQuoteCount"],
        "rawCoverageRatio": payload["coverage"]["rawCoverageRatio"],
        "currentTradeDateCoverageRatio": payload["coverage"][
            "currentTradeDateCoverageRatio"
        ],
        "usableCoverageRatio": payload["coverage"]["usableCoverageRatio"],
        "coverageRatio": payload["coverage"]["coverageRatio"],
        "sources": payload["sources"],
    }


def read_verified_snapshot(path: Path | str) -> dict[str, Any]:
    path = Path(path).expanduser().resolve()
    with gzip.open(path, "rb") as handle:
        raw = handle.read()
    payload = json.loads(raw.decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"intraday snapshot is not a JSON object: {path}")
    expected_hash = path.name.rsplit("_", 1)[-1].split(".", 1)[0]
    actual_hash = _sha256_bytes(_canonical_json_bytes({
        key: value for key, value in payload.items() if key != "contentSha256"
    }))
    if not actual_hash.startswith(expected_hash) or payload.get("contentSha256") != actual_hash:
        raise ValueError(f"intraday snapshot content hash mismatch: {path}")
    sidecar = path.with_suffix(path.suffix + ".sha256")
    if not sidecar.is_file():
        raise ValueError(f"intraday snapshot hash sidecar is missing: {sidecar}")
    recorded_archive_hash = sidecar.read_text(encoding="ascii").split()[0]
    if recorded_archive_hash != _sha256_file(path):
        raise ValueError(f"intraday snapshot archive hash mismatch: {path}")
    return payload


def read_verified_slot_record(path: Path | str) -> dict[str, Any]:
    path = Path(path).expanduser().resolve()
    raw = path.read_bytes()
    payload = json.loads(raw.decode("utf-8"))
    if not isinstance(payload, dict) or payload.get("kind") != "intraday_capture_slot":
        raise ValueError(f"intraday slot record is invalid: {path}")
    expected_hash = path.stem.rsplit("_", 1)[-1]
    actual_record_hash = _sha256_bytes(
        _canonical_json_bytes(
            {key: value for key, value in payload.items() if key != "recordSha256"}
        )
    )
    if (
        not actual_record_hash.startswith(expected_hash)
        or payload.get("recordSha256") != actual_record_hash
    ):
        raise ValueError(f"intraday slot record content hash mismatch: {path}")
    sidecar = path.with_suffix(path.suffix + ".sha256")
    if not sidecar.is_file():
        raise ValueError(f"intraday slot record sidecar is missing: {sidecar}")
    recorded_file_hash = sidecar.read_text(encoding="ascii").split()[0]
    if recorded_file_hash != _sha256_bytes(raw):
        raise ValueError(f"intraday slot record file hash mismatch: {path}")
    return payload


def list_verified_slot_records(
    output_root: Path | str,
    trade_date: str,
) -> list[tuple[Path, dict[str, Any]]]:
    root = Path(output_root).expanduser().resolve()
    records = []
    for path in sorted((root / "slots" / trade_date).glob("slot_*.json")):
        records.append((path, read_verified_slot_record(path)))
    previous_hash: str | None = None
    seen_slots: set[str] = set()
    for expected_sequence, (_, payload) in enumerate(records, start=1):
        if payload.get("tradeDate") != trade_date:
            raise ValueError("intraday slot record trade date mismatch")
        if payload.get("sequence") != expected_sequence:
            raise ValueError("intraday slot record sequence is not contiguous")
        if payload.get("previousRecordSha256") != previous_hash:
            raise ValueError("intraday slot record hash chain is broken")
        capture_slot = str(payload.get("captureSlot") or "")
        if capture_slot in seen_slots:
            raise ValueError(f"duplicate intraday capture slot: {capture_slot}")
        seen_slots.add(capture_slot)
        previous_hash = str(payload["recordSha256"])
    return records


def _slot_binding(
    *,
    provider: Mapping[str, Any],
    implementation: Mapping[str, Any],
    poll_seconds: int,
) -> dict[str, Any]:
    return {
        "providerPath": provider["providerPath"],
        "providerLatestDate": provider["providerLatestDate"],
        "calendarSha256": provider["calendarSha256"],
        "csi300Sha256": provider["csi300Sha256"],
        "symbolsSha256": provider["symbolsSha256"],
        "implementationSha256": implementation["sha256"],
        "pollSeconds": poll_seconds,
    }


def _commit_slot_record(
    *,
    output_root: Path,
    trade_date: str,
    capture_slot: datetime,
    status: str,
    request_started_at: datetime,
    response_received_at: datetime,
    committed_at: datetime,
    forced_capture: bool,
    binding: Mapping[str, Any],
    snapshot: Mapping[str, Any] | None,
    error: str | None,
) -> tuple[Path, dict[str, Any]]:
    records = list_verified_slot_records(output_root, trade_date)
    slot_text = capture_slot.isoformat()
    for path, payload in records:
        if payload.get("captureSlot") == slot_text:
            return path, payload
    previous_hash = records[-1][1]["recordSha256"] if records else None
    payload_without_hash = {
        "schemaVersion": SCHEMA_VERSION,
        "kind": "intraday_capture_slot",
        "usageRealm": "INTRADAY_RESEARCH_RAW",
        "evidenceEligible": False,
        "trainingEligible": False,
        "officialDailyEligible": False,
        "tradeDate": trade_date,
        "sequence": len(records) + 1,
        "captureSlot": slot_text,
        "status": status,
        "requestStartedAt": request_started_at.isoformat(),
        "responseReceivedAt": response_received_at.isoformat(),
        "availableAt": response_received_at.isoformat(),
        "committedAt": committed_at.isoformat(),
        "forcedCapture": forced_capture,
        "binding": dict(binding),
        "snapshot": dict(snapshot) if snapshot else None,
        "error": error[:1000] if error else None,
        "previousRecordSha256": previous_hash,
    }
    record_hash = _sha256_bytes(_canonical_json_bytes(payload_without_hash))
    payload = {**payload_without_hash, "recordSha256": record_hash}
    raw = _canonical_json_bytes(payload)
    slot_dir = output_root / "slots" / trade_date
    path = slot_dir / (
        f"slot_{len(records) + 1:06d}_{capture_slot:%H%M%S}_{record_hash[:12]}.json"
    )
    file_hash = _sha256_bytes(raw)
    _immutable_write(
        path.with_suffix(path.suffix + ".sha256"),
        f"{file_hash}  {path.name}\n".encode("ascii"),
    )
    _immutable_write(path, raw)
    return path, payload


def _slot_record_summary(path: Path, payload: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "file": str(path),
        "sequence": int(payload["sequence"]),
        "captureSlot": payload["captureSlot"],
        "status": payload["status"],
        "recordSha256": payload["recordSha256"],
        "previousRecordSha256": payload.get("previousRecordSha256"),
        "committedAt": payload["committedAt"],
    }


def _base_status(
    *,
    output_root: Path,
    poll_seconds: int,
    now: datetime,
    previous: Mapping[str, Any] | None,
) -> dict[str, Any]:
    return {
        "version": SCHEMA_VERSION,
        "usageRealm": "INTRADAY_RESEARCH_RAW",
        "evidenceEligible": False,
        "trainingEligible": False,
        "outputRoot": str(output_root),
        "pollSeconds": poll_seconds,
        "lastAttemptAt": now.isoformat(),
        "lastSuccessAt": (previous or {}).get("lastSuccessAt"),
        "nextCaptureAt": None,
        "consecutiveErrors": int((previous or {}).get("consecutiveErrors") or 0),
        "lastError": (previous or {}).get("lastError"),
        "latest": (previous or {}).get("latest"),
        "latestSlotRecord": (previous or {}).get("latestSlotRecord"),
        "calibration": (previous or {}).get("calibration"),
    }


def run_capture_cycle(
    provider_path: Path | str,
    output_root: Path | str = DEFAULT_OUTPUT_ROOT,
    *,
    poll_seconds: int = 60,
    force: bool = False,
    now: datetime | None = None,
    client: QuoteClient | None = None,
    clock: Callable[[], datetime] | None = None,
) -> dict[str, Any]:
    if poll_seconds < 30 or poll_seconds > 900:
        raise ValueError("poll_seconds must be between 30 and 900")
    wall_clock = clock or (lambda: datetime.now(CHINA_TZ))
    local_now = (now or wall_clock()).astimezone(CHINA_TZ)
    output = Path(output_root).expanduser().resolve()

    def current_time() -> datetime:
        if now is not None and clock is None:
            return local_now
        return wall_clock().astimezone(CHINA_TZ)

    with _capture_lock(output):
        status_recovery_error: str | None = None
        try:
            previous = read_capture_status(output)
        except Exception as exc:
            previous = None
            status_recovery_error = str(exc)[:1000]
        base = _base_status(
            output_root=output,
            poll_seconds=poll_seconds,
            now=local_now,
            previous=previous,
        )
        if status_recovery_error:
            base["statusRecoveryError"] = status_recovery_error
        provider: dict[str, Any] | None = None
        binding: dict[str, Any] | None = None
        slot: datetime | None = None
        request_started_at = local_now
        response_received_at = local_now
        try:
            provider = _provider_context(Path(provider_path))
            implementation = capture_implementation_contract()
            binding = _slot_binding(
                provider=provider,
                implementation=implementation,
                poll_seconds=poll_seconds,
            )
            schedule = capture_schedule(local_now, provider["tradingDates"])
            base["providerPath"] = provider["providerPath"]
            base["providerLatestDate"] = provider["providerLatestDate"]
            if not force and not schedule["active"]:
                status = {
                    **base,
                    "status": schedule["status"],
                    "nextCaptureAt": schedule["nextWindowAt"],
                }
                _write_status(output, status)
                return status

            slot = _capture_slot(local_now, poll_seconds)
            slot_text = slot.strftime("%H%M%S")
            trade_date = local_now.date().isoformat()
            snapshot_dir = output / "snapshots" / trade_date
            slot_records = list_verified_slot_records(output, trade_date)
            existing_record = next(
                (
                    (path, payload)
                    for path, payload in slot_records
                    if payload.get("captureSlot") == slot.isoformat()
                ),
                None,
            )
            if existing_record:
                record_path, record = existing_record
                snapshot_summary = record.get("snapshot")
                if snapshot_summary:
                    snapshot_path = Path(str(snapshot_summary["file"]))
                    payload = read_verified_snapshot(snapshot_path)
                    latest = _snapshot_summary(snapshot_path, payload)
                    reused_status = "REUSED"
                else:
                    latest = base.get("latest")
                    reused_status = str(record["status"])
                status = {
                    **base,
                    "status": reused_status,
                    "lastSuccessAt": (
                        latest["capturedAt"] if latest else base.get("lastSuccessAt")
                    ),
                    "nextCaptureAt": (slot + timedelta(seconds=poll_seconds)).isoformat(),
                    "consecutiveErrors": (
                        0 if reused_status == "REUSED" else base["consecutiveErrors"]
                    ),
                    "lastError": record.get("error"),
                    "latest": latest,
                    "latestSlotRecord": _slot_record_summary(record_path, record),
                }
                _write_status(output, status)
                return status

            if slot_records and slot_records[-1][1].get("binding") != binding:
                raise RuntimeError(
                    "intraday capture binding drifted within the trading day"
                )

            quote_client = client or AStockDataClient(timeout=8.0)
            requested = provider["symbols"]
            requested_set = set(requested)
            request_started_at = current_time()
            quotes = quote_client.realtime_quotes(
                requested,
                expected_trade_date=trade_date,
                max_quote_age_seconds=120,
                now=request_started_at,
            )
            response_received_at = current_time()
            if response_received_at < request_started_at:
                raise ValueError("intraday capture clock moved backwards during request")
            unexpected_symbols = sorted(set(quotes) - requested_set)
            quote_rows = []
            for symbol in requested:
                if symbol not in quotes:
                    continue
                quote = dict(quotes[symbol])
                if quote.get("symbol") != symbol:
                    raise ValueError(
                        f"quote symbol mismatch for {symbol}: {quote.get('symbol')}"
                    )
                quote_rows.append(quote)
            current_trade_date = trade_date
            current_rows = [
                quote for quote in quote_rows if quote.get("tradeDate") == current_trade_date
            ]
            usable_rows = [
                quote
                for quote in current_rows
                if _usable_capture_quote(
                    quote,
                    trade_date=current_trade_date,
                    available_at=response_received_at,
                )
            ]
            if not force and not current_rows:
                error = "upstream quotes have not switched to the current trade date"
                committed_at = current_time()
                record_path, record = _commit_slot_record(
                    output_root=output,
                    trade_date=trade_date,
                    capture_slot=slot,
                    status="WAITING_QUOTES",
                    request_started_at=request_started_at,
                    response_received_at=response_received_at,
                    committed_at=committed_at,
                    forced_capture=force,
                    binding=binding,
                    snapshot=None,
                    error=error,
                )
                status = {
                    **base,
                    "status": "WAITING_QUOTES",
                    "nextCaptureAt": (slot + timedelta(seconds=poll_seconds)).isoformat(),
                    "lastError": error,
                    "latestSlotRecord": _slot_record_summary(record_path, record),
                }
                _write_status(output, status)
                return status

            quote_times = sorted(
                str(quote["quoteAsOf"])
                for quote in quote_rows
                if quote.get("quoteAsOf")
            )
            quote_trade_dates = sorted(
                {str(quote["tradeDate"]) for quote in quote_rows if quote.get("tradeDate")}
            )
            returned_symbols = {str(quote["symbol"]) for quote in quote_rows}
            current_symbols = {str(quote["symbol"]) for quote in current_rows}
            usable_symbols = {str(quote["symbol"]) for quote in usable_rows}
            missing_symbols = sorted(requested_set - returned_symbols)
            non_current_trade_date_symbols = sorted(returned_symbols - current_symbols)
            unusable_current_trade_date_symbols = sorted(
                current_symbols - usable_symbols
            )
            raw_coverage_ratio = len(quote_rows) / len(requested)
            current_coverage_ratio = len(current_rows) / len(requested)
            usable_coverage_ratio = len(usable_rows) / len(requested)
            payload_without_hash = {
                "schemaVersion": SCHEMA_VERSION,
                "kind": "a_stock_data_csi300_intraday_snapshot",
                "usageRealm": "INTRADAY_RESEARCH_RAW",
                "evidenceEligible": False,
                "trainingEligible": False,
                "officialDailyEligible": False,
                "forcedCapture": force,
                "captureRequestedAt": request_started_at.isoformat(),
                "responseReceivedAt": response_received_at.isoformat(),
                "availableAt": response_received_at.isoformat(),
                "capturedAt": response_received_at.isoformat(),
                "captureSlot": slot.isoformat(),
                "quoteAsOfMin": quote_times[0] if quote_times else None,
                "quoteAsOfMax": quote_times[-1] if quote_times else None,
                "quoteTradeDate": quote_trade_dates[-1] if quote_trade_dates else None,
                "quoteTradeDates": quote_trade_dates,
                "sources": sorted(
                    {str(quote["source"]) for quote in quote_rows if quote.get("source")}
                ),
                "provider": {
                    "path": provider["providerPath"],
                    "latestDate": provider["providerLatestDate"],
                    "calendarPath": provider["calendarPath"],
                    "calendarSha256": provider["calendarSha256"],
                    "csi300Path": provider["csi300Path"],
                    "csi300Sha256": provider["csi300Sha256"],
                },
                "universe": {
                    "name": "CSI300_PLUS_BENCHMARK",
                    "asOf": provider["providerLatestDate"],
                    "symbolsSha256": provider["symbolsSha256"],
                    "symbols": requested,
                },
                "coverage": {
                    "requestedCount": len(requested),
                    "quoteCount": len(quote_rows),
                    "currentTradeDateQuoteCount": len(current_rows),
                    "usableQuoteCount": len(usable_rows),
                    "rawCoverageRatio": raw_coverage_ratio,
                    "currentTradeDateCoverageRatio": current_coverage_ratio,
                    "usableCoverageRatio": usable_coverage_ratio,
                    "coverageRatio": usable_coverage_ratio,
                    "missingSymbols": missing_symbols,
                    "nonCurrentTradeDateSymbols": non_current_trade_date_symbols,
                    "unusableCurrentTradeDateSymbols": (
                        unusable_current_trade_date_symbols
                    ),
                    "unexpectedSymbols": unexpected_symbols,
                },
                "implementation": implementation,
                "quotes": quote_rows,
            }
            content_hash = _sha256_bytes(_canonical_json_bytes(payload_without_hash))
            payload = {**payload_without_hash, "contentSha256": content_hash}
            raw = _canonical_json_bytes(payload)
            archive_buffer = io.BytesIO()
            with gzip.GzipFile(
                fileobj=archive_buffer,
                mode="wb",
                compresslevel=6,
                mtime=0,
            ) as handle:
                handle.write(raw)
            archive = archive_buffer.getvalue()
            snapshot_path = snapshot_dir / f"snapshot_{slot_text}_{content_hash[:12]}.json.gz"
            archive_hash = _sha256_bytes(archive)
            _immutable_write(
                snapshot_path.with_suffix(snapshot_path.suffix + ".sha256"),
                f"{archive_hash}  {snapshot_path.name}\n".encode("ascii"),
            )
            _immutable_write(snapshot_path, archive)
            snapshot_summary = _snapshot_summary(snapshot_path, payload)
            committed_at = current_time()
            record_path, record = _commit_slot_record(
                output_root=output,
                trade_date=trade_date,
                capture_slot=slot,
                status="CAPTURED",
                request_started_at=request_started_at,
                response_received_at=response_received_at,
                committed_at=committed_at,
                forced_capture=force,
                binding=binding,
                snapshot=snapshot_summary,
                error=None,
            )
            status = {
                **base,
                "status": "CAPTURED",
                "lastSuccessAt": response_received_at.isoformat(),
                "nextCaptureAt": (slot + timedelta(seconds=poll_seconds)).isoformat(),
                "consecutiveErrors": 0,
                "lastError": None,
                "latest": snapshot_summary,
                "latestSlotRecord": _slot_record_summary(record_path, record),
            }
            _write_status(output, status)
            return status
        except Exception as exc:
            error = str(exc)[:1000]
            record_summary = base.get("latestSlotRecord")
            if slot is not None and binding is not None:
                try:
                    response_received_at = max(response_received_at, current_time())
                    record_path, record = _commit_slot_record(
                        output_root=output,
                        trade_date=local_now.date().isoformat(),
                        capture_slot=slot,
                        status="ERROR",
                        request_started_at=request_started_at,
                        response_received_at=response_received_at,
                        committed_at=current_time(),
                        forced_capture=force,
                        binding=binding,
                        snapshot=None,
                        error=error,
                    )
                    record_summary = _slot_record_summary(record_path, record)
                except Exception as record_exc:
                    error = f"{error}; slot record failure: {record_exc}"[:1000]
            status = {
                **base,
                "status": "ERROR",
                "nextCaptureAt": (local_now + timedelta(seconds=poll_seconds)).isoformat(),
                "consecutiveErrors": base["consecutiveErrors"] + 1,
                "lastError": error,
                "latestSlotRecord": record_summary,
            }
            _write_status(output, status)
            return status
