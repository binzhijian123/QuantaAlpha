"""Response-timed capture with an isolated copy of the frozen collector core.

The original collector module and its process-local contract are never patched.
V4 snapshots bind the real provider chain, this wrapper, dependency pins, and
any injected transport-fixture subclass rather than claiming production identity.
"""
from __future__ import annotations

from contextvars import ContextVar
from importlib import metadata, util
import inspect
from pathlib import Path
import platform
import sys
from typing import Any

from . import intraday_capture as _base
from .providers import a_stock_data, a_stock_data_intraday_v2, a_stock_data_intraday_v3
from .providers import a_stock_data_intraday_v4 as _provider
from .providers.a_stock_data_intraday_v4 import ResponseTimedRealtimeClient

_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_PRODUCTION_CLIENT = ResponseTimedRealtimeClient
_QUOTE_METHOD = ResponseTimedRealtimeClient.realtime_quotes
_REALTIME_CONTRACT = _provider.REALTIME_CONTRACT_SHA256
_EXPECTED_PACKAGE_VERSION = str(_provider.RESPONSE_TIME_CONTRACT["easyTdxPackageVersion"])
_ACTIVE_CLIENT: ContextVar[tuple[ResponseTimedRealtimeClient, bool] | None] = ContextVar(
    "intraday_capture_v4_client", default=None,
)
_INJECTED_FILE_HASHES: dict[str, str] = {}


def _implementation_paths() -> tuple[Path, ...]:
    return tuple(sorted({
        Path(__file__).resolve(), Path(_base.__file__).resolve(),
        Path(a_stock_data.__file__).resolve(),
        Path(a_stock_data_intraday_v2.__file__).resolve(),
        Path(a_stock_data_intraday_v3.__file__).resolve(),
        Path(_provider.__file__).resolve(),
        _PROJECT_ROOT / "requirements.txt",
        _PROJECT_ROOT / "frontend-v2/backend/requirements.txt",
    }))


def _file_hashes() -> dict[str, str]:
    return {str(p.relative_to(_PROJECT_ROOT)): _base._sha256_file(p)
            for p in _implementation_paths()}


# Bind loaded files at import, so edits before the first capture are also refused.
_PROCESS_FILE_HASHES = _file_hashes()


def _verify_client(client: ResponseTimedRealtimeClient) -> None:
    if not isinstance(client, _PRODUCTION_CLIENT):
        raise TypeError("V4 capture requires a ResponseTimedRealtimeClient")
    if getattr(client.realtime_quotes, "__func__", None) is not _QUOTE_METHOD:
        raise TypeError("V4 capture refuses an overridden realtime_quotes method")
    if client.core_field_contract != _provider.RESPONSE_TIME_CONTRACT:
        raise RuntimeError("V4 client source contract drifted")


def capture_implementation_contract() -> dict[str, Any]:
    """Return a verified contract, including the actual active client identity."""
    hashes = _file_hashes()
    if hashes != _PROCESS_FILE_HASHES:
        raise RuntimeError("V4 collector source changed in a running process; restart is required")
    if (ResponseTimedRealtimeClient is not _PRODUCTION_CLIENT
            or _provider.ResponseTimedRealtimeClient is not _PRODUCTION_CLIENT
            or _PRODUCTION_CLIENT.realtime_quotes is not _QUOTE_METHOD
            or _provider.REALTIME_CONTRACT_SHA256 != _REALTIME_CONTRACT
            or _base._sha256_bytes(_base._canonical_json_bytes(_provider.RESPONSE_TIME_CONTRACT))
            != _REALTIME_CONTRACT):
        raise RuntimeError("V4 provider runtime binding drifted")
    package_version = metadata.version("easy-tdx")
    if package_version != _EXPECTED_PACKAGE_VERSION:
        raise RuntimeError("V4 easy-tdx package version drifted")

    active = _ACTIVE_CLIENT.get()
    client, injected = active if active is not None else (None, False)
    actual_class = type(client) if client is not None else _PRODUCTION_CLIENT
    if client is not None:
        _verify_client(client)
    files = dict(hashes)
    if actual_class is not _PRODUCTION_CLIENT:
        fixture_path = Path(inspect.getfile(actual_class)).resolve()
        fixture_hash = _base._sha256_file(fixture_path)
        fixture_key = str(fixture_path)
        if _INJECTED_FILE_HASHES.setdefault(fixture_key, fixture_hash) != fixture_hash:
            raise RuntimeError("injected V4 client source changed in a running process")
        try:
            fixture_key = str(fixture_path.relative_to(_PROJECT_ROOT))
        except ValueError:
            pass
        files[fixture_key] = fixture_hash

    contract = {
        "version": 2,
        "files": [{"path": p, "sha256": digest} for p, digest in sorted(files.items())],
        "runtime": {
            "pythonImplementation": platform.python_implementation(),
            "pythonVersion": platform.python_version(),
            "requests": _base._distribution_version("requests"),
            "pandas": _base._distribution_version("pandas"),
            "entrypointModule": __name__,
            "providerModule": actual_class.__module__,
            "providerClass": actual_class.__qualname__,
            "productionProviderModule": _PRODUCTION_CLIENT.__module__,
            "productionProviderClass": _PRODUCTION_CLIENT.__qualname__,
            "clientInjected": injected,
            "easyTdxPackageVersion": package_version,
            "realtimeContractSha256": _REALTIME_CONTRACT,
            "sourceOrder": list(_provider.RESPONSE_TIME_CONTRACT["sourceOrder"]),
            "prohibitCrossSourceFieldMerge": True,
            "timeouts": {
                "http": float(client.timeout) if client is not None else 8.0,
                "tcpHandshake": float(client.tdx_ping_timeout) if client is not None else 5.0,
                "tcpQuery": float(client.tdx_query_timeout) if client is not None else 8.0,
            },
        },
        "sourceContract": dict(_provider.RESPONSE_TIME_CONTRACT),
    }
    return {**contract, "sha256": _base._sha256_bytes(_base._canonical_json_bytes(contract))}


def _isolated_core():
    module_name = "market_data._intraday_capture_core_v4"
    spec = util.spec_from_file_location(module_name, _base.__file__)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load isolated V4 collector core")
    module = util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.capture_implementation_contract = capture_implementation_contract
    sys.modules[module_name] = module
    return module


_CORE = _isolated_core()


class _VerifiedQuoteClient:
    def __init__(self, client: ResponseTimedRealtimeClient):
        self.client = client

    def realtime_quotes(self, codes, **kwargs):
        capture_implementation_contract()
        quotes = self.client.realtime_quotes(codes, **kwargs)
        # Verify before the isolated core can persist a snapshot or CAPTURED slot.
        capture_implementation_contract()
        if any(not isinstance(row, dict)
               or row.get("realtimeContractSha256") != _REALTIME_CONTRACT
               for row in quotes.values()):
            raise RuntimeError("V4 capture received an unbound quote source contract")
        return quotes


def _verify_snapshot_contract(path: str | Path, expected_sha: str) -> None:
    snapshot = _base.read_verified_snapshot(path)
    if (snapshot.get("implementation", {}).get("sha256") != expected_sha
            or any(row.get("realtimeContractSha256") != _REALTIME_CONTRACT
                   for row in snapshot.get("quotes", []))):
        raise RuntimeError("V4 capture refuses a snapshot with a different implementation or source contract")


def run_capture_cycle(*args: Any, client: Any | None = None, **kwargs: Any) -> dict[str, Any]:
    """Run the unchanged collector algorithm with V4-only provenance checks."""
    actual = client if client is not None else ResponseTimedRealtimeClient(
        timeout=8.0, tdx_ping_timeout=5.0, tdx_query_timeout=8.0,
    )
    _verify_client(actual)
    token = _ACTIVE_CLIENT.set((actual, client is not None))
    try:
        contract = capture_implementation_contract()
        output_root = (args[1] if len(args) > 1
                       else kwargs.get("output_root", _base.DEFAULT_OUTPUT_ROOT))
        previous = _base.read_capture_status(output_root)
        if previous and previous.get("latest"):
            _verify_snapshot_contract(previous["latest"]["file"], contract["sha256"])
        result = _CORE.run_capture_cycle(*args, client=_VerifiedQuoteClient(actual), **kwargs)
        capture_implementation_contract()
        if result.get("latest"):
            _verify_snapshot_contract(result["latest"]["file"], contract["sha256"])
        return result
    finally:
        _ACTIVE_CLIENT.reset(token)
