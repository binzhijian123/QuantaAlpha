import hashlib
import json
import os
import select
import signal
import sqlite3
import struct
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo


BACKEND_ROOT = Path(__file__).resolve().parents[1] / "frontend-v2" / "backend"
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from market_data.intraday_capture import run_capture_cycle
from market_data.intraday_shadow import (
    IntradayShadowCoordinator,
    _rank_signals,
)
from market_data.intraday_shadow_v2 import (
    IntradayShadowCoordinator as V2IntradayShadowCoordinator,
)
from market_data.intraday_shadow_v3 import (
    IntradayShadowCoordinator as V3IntradayShadowCoordinator,
)
from market_data.intraday_shadow_v4 import (
    IntradayShadowCoordinator as V4IntradayShadowCoordinator,
)
from market_data.intraday_shadow_v5 import (
    IntradayShadowCoordinator as V5IntradayShadowCoordinator,
)
from market_data.intraday_shadow_v6 import (
    IntradayShadowCoordinator as V6IntradayShadowCoordinator,
)
from market_data.intraday_shadow_v7 import (
    IntradayShadowCoordinator as V7IntradayShadowCoordinator,
    PROTOCOL_ID as V7_PROTOCOL_ID,
)
from market_data.intraday_shadow_v8 import (
    IntradayShadowCoordinator as V8IntradayShadowCoordinator,
    PROTOCOL_ID as V8_PROTOCOL_ID,
)
from market_data.intraday_shadow_v9 import (
    IntradayShadowCoordinator as V9IntradayShadowCoordinator,
    PROTOCOL_ID as V9_PROTOCOL_ID,
)
from market_data.providers.a_stock_data_intraday_v3 import (
    REALTIME_CONTRACT_SHA256,
)
from quantaalpha.utils.qlib_provider import provider_contract_fingerprint


CHINA_TZ = ZoneInfo("Asia/Shanghai")
BASE_DATES = (
    "2026-08-04",
    "2026-08-05",
    "2026-08-06",
    "2026-08-07",
    "2026-08-10",
    "2026-08-11",
    "2026-08-12",
    "2026-08-13",
)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _sidecar(path: Path) -> None:
    path.with_suffix(path.suffix + ".sha256").write_text(
        f"{_sha(path)}  {path.name}\n", encoding="ascii"
    )


def _symbols() -> list[str]:
    return [f"{600000 + index:06d}.SH" for index in range(300)]


def _write_feature(path: Path, values: list[float], start: int = 0) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(struct.pack(f"<{len(values) + 1}f", float(start), *values))


def _write_provider(root: Path, dates: tuple[str, ...]) -> Path:
    provider = root
    (provider / "calendars").mkdir(parents=True, exist_ok=True)
    (provider / "instruments").mkdir(parents=True, exist_ok=True)
    (provider / "calendars" / "day.txt").write_text(
        "\n".join(dates) + "\n", encoding="utf-8"
    )
    (provider / "calendars" / "day_future.txt").write_text(
        "\n".join((*BASE_DATES, "2026-08-14", "2026-08-17", "2026-08-18"))
        + "\n",
        encoding="utf-8",
    )
    instruments = [
        f"SH{symbol[:6]}\t2026-08-04\t2026-08-18" for symbol in _symbols()
    ]
    (provider / "instruments" / "csi300.txt").write_text(
        "\n".join(instruments) + "\n", encoding="utf-8"
    )
    for symbol in [*_symbols(), "000300.SH"]:
        code, exchange = symbol.split(".")
        directory = provider / "features" / f"{exchange.lower()}{code}"
        values = [10.0 + index / 100 for index in range(len(dates))]
        _write_feature(directory / "open.day.bin", values)
        _write_feature(directory / "close.day.bin", values)
        _write_feature(directory / "factor.day.bin", [1.0] * len(dates))
    return provider


class _QuoteClient:
    def __init__(
        self,
        price_shift: float = 0.0,
        reverse_turnover: bool = False,
        reverse_returns: bool = False,
    ):
        self.price_shift = price_shift
        self.reverse_turnover = reverse_turnover
        self.reverse_returns = reverse_returns

    def realtime_quotes(self, codes: list[str], **kwargs):
        now = kwargs["now"]
        result = {}
        for index, code in enumerate(codes):
            if code == "000300.SH":
                previous = 4000.0
                price = 4000.0 + self.price_shift
                turnover = 1.0
            else:
                symbol_index = int(code[:6]) - 600000
                previous = 10.0 + index / 1000
                decline_rank = 299 - symbol_index if self.reverse_returns else symbol_index
                decline = 0.001 + decline_rank / 100_000
                price = previous * (1.0 - decline) + self.price_shift
                turnover = float(
                    symbol_index + 1
                    if self.reverse_turnover
                    else 300 - symbol_index
                )
            result[code] = {
                "symbol": code,
                "name": code,
                "price": price,
                "previousClose": previous,
                "open": previous,
                "high": max(previous, price),
                "low": min(previous, price),
                "change": price - previous,
                "changePercent": (price / previous - 1.0) * 100.0,
                "amountCny": 100_000_000.0,
                "turnoverPercent": turnover,
                "quoteAsOf": now.isoformat(),
                "tradeDate": now.date().isoformat(),
                "isStale": False,
                "staleReason": None,
                "source": "a-stock-data/tencent-public",
            }
        return result


class _DroppingQuoteClient(_QuoteClient):
    def __init__(self, dropped: set[str], **kwargs):
        super().__init__(**kwargs)
        self.dropped = dropped

    def realtime_quotes(self, codes: list[str], **kwargs):
        result = super().realtime_quotes(codes, **kwargs)
        for symbol in self.dropped:
            result.pop(symbol, None)
        return result


class _AgingQuoteClient(_QuoteClient):
    def __init__(self, aged: set[str], **kwargs):
        super().__init__(**kwargs)
        self.aged = aged

    def realtime_quotes(self, codes: list[str], **kwargs):
        result = super().realtime_quotes(codes, **kwargs)
        old_quote_time = kwargs["now"] - timedelta(seconds=121)
        for symbol in self.aged:
            result[symbol]["quoteAsOf"] = old_quote_time.isoformat()
        return result


class _BenchmarkWithoutTurnoverQuoteClient(_QuoteClient):
    def realtime_quotes(self, codes: list[str], **kwargs):
        result = super().realtime_quotes(codes, **kwargs)
        result["000300.SH"]["turnoverPercent"] = None
        return result


class _ContractQuoteClient(_QuoteClient):
    def realtime_quotes(self, codes: list[str], **kwargs):
        result = super().realtime_quotes(codes, **kwargs)
        for quote in result.values():
            quote["realtimeContractSha256"] = REALTIME_CONTRACT_SHA256
        return result


class _MissingTurnoverQuoteClient(_QuoteClient):
    def __init__(self, missing: set[str], **kwargs):
        super().__init__(**kwargs)
        self.missing = missing

    def realtime_quotes(self, codes: list[str], **kwargs):
        result = super().realtime_quotes(codes, **kwargs)
        for symbol in self.missing:
            result[symbol]["turnoverPercent"] = None
        return result


def _capture_pair(
    provider: Path,
    capture: Path,
    trade_date: str,
    *,
    decision_client: _QuoteClient | None = None,
    execution_client: _QuoteClient | None = None,
) -> None:
    for clock, client in (
        ("14:50:03", decision_client or _QuoteClient()),
        (
            "14:55:03",
            execution_client
            or _QuoteClient(price_shift=0.01, reverse_turnover=True),
        ),
    ):
        run_capture_cycle(
            provider,
            capture,
            poll_seconds=60,
            now=datetime.fromisoformat(f"{trade_date}T{clock}+08:00"),
            client=client,
        )


def _write_declaration(root: Path, provider: Path, capture: Path) -> Path:
    report = root / "calibration.json"
    report.write_text(
        json.dumps(
            {
                "status": "PASS",
                "finalized": True,
                "reportSha256": "a" * 64,
                "profitEvidenceEligible": False,
                "featureValuesIncluded": False,
                "returnValuesIncluded": False,
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    _sidecar(report)
    implementation = BACKEND_ROOT / "market_data" / "intraday_shadow.py"
    declaration = root / "declaration.json"
    payload = {
        "schemaVersion": 1,
        "protocolId": "intraday-shadow-test-v1",
        "runId": "intraday_shadow_test_v1",
        "declaredAt": "2026-08-13T16:00:00+08:00",
        "usageRealm": "INTRADAY_SHADOW",
        "formalPaperEligible": False,
        "automaticPromotionAllowed": False,
        "historicalReturnValuesUsedForSelection": False,
        "paperReference": {
            "doi": "10.2139/ssrn.6736959",
            "fullMethodVerified": False,
            "implementationKind": "MECHANISM_PROXY",
        },
        "firstEligibleSignalDate": "2026-08-14",
        "captureRoot": str(capture),
        "databasePath": str(root / "run" / "ledger.sqlite3"),
        "calibration": {
            "reportPath": str(report),
            "fileSha256": _sha(report),
            "reportSha256": "a" * 64,
        },
        "providerRoot": {
            "path": str(provider),
            "fingerprint": provider_contract_fingerprint(provider, "csi300")[
                "fingerprint"
            ],
        },
        "strategy": {
            "candidateFormula": "max(-rawReturn, 0) * turnoverPercent",
            "baselineFormula": "max(-rawReturn, 0)",
            "eligibleReturnDirection": "NEGATIVE_ONLY",
            "mechanismProxy": True,
            "paperReplication": False,
            "topK": 50,
        },
        "timing": {
            "decisionSlot": "14:50:00",
            "executionSlot": "14:55:00",
            "freezeAfter": "15:06:00",
            "freezeDeadline": "15:20:00",
        },
        "execution": {
            "initialCash": 1_000_000,
            "riskDegree": 0.95,
            "priceField": "price",
            "settlement": "T+1",
            "lotSize": 100,
            "buyCostRate": 0.0005,
            "sellCostRate": 0.0015,
            "minimumCost": 5.0,
            "slippageBps": 10.0,
            "limitThreshold": 0.095,
            "growthLimitThreshold": 0.195,
            "starMinimumOrderSize": 200,
            "starOrderIncrement": 1,
        },
        "implementation": {
            "files": [{"path": str(implementation), "sha256": _sha(implementation)}]
        },
    }
    declaration.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    _sidecar(declaration)
    return declaration


def _write_v2_declaration(root: Path, provider: Path, capture: Path) -> Path:
    declaration = _write_declaration(root, provider, capture)
    payload = json.loads(declaration.read_text(encoding="utf-8"))
    payload["protocolId"] = "intraday-shadow-test-v2"
    payload["runId"] = "intraday_shadow_test_v2"
    payload["databasePath"] = str(root / "run-v2" / "ledger.sqlite3")
    payload["quality"] = {
        "version": 1,
        "minimumCoreFieldCoverageRatio": 0.95,
        "requiredUniverseSize": 301,
        "maximumQuoteAgeSeconds": 120,
        "maximumFutureQuoteLeadSeconds": 5,
        "requireBenchmarkUsable": True,
        "requireExecutionQuotesForAllTargets": True,
    }
    implementation_v2 = BACKEND_ROOT / "market_data" / "intraday_shadow_v2.py"
    payload["implementation"]["files"].append(
        {"path": str(implementation_v2), "sha256": _sha(implementation_v2)}
    )
    declaration.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    _sidecar(declaration)
    return declaration


def _write_v3_declaration(root: Path, provider: Path, capture: Path) -> Path:
    declaration = _write_v2_declaration(root, provider, capture)
    payload = json.loads(declaration.read_text(encoding="utf-8"))
    payload["protocolId"] = "intraday-shadow-test-v3"
    payload["runId"] = "intraday_shadow_test_v3"
    payload["databasePath"] = str(root / "run-v3" / "ledger.sqlite3")
    payload["qualityExtension"] = {
        "requireDecisionQuotesForAllTargets": True,
    }
    implementation_v3 = BACKEND_ROOT / "market_data" / "intraday_shadow_v3.py"
    payload["implementation"]["files"].append(
        {"path": str(implementation_v3), "sha256": _sha(implementation_v3)}
    )
    declaration.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    _sidecar(declaration)
    return declaration


def _write_v4_declaration(root: Path, provider: Path, capture: Path) -> Path:
    declaration = _write_v3_declaration(root, provider, capture)
    payload = json.loads(declaration.read_text(encoding="utf-8"))
    payload["protocolId"] = "intraday-shadow-test-v4"
    payload["runId"] = "intraday_shadow_test_v4"
    payload["databasePath"] = str(root / "run-v4" / "ledger.sqlite3")
    payload["qualityExtension"] = {
        "requireDecisionQuotesForAllTargets": True,
        "requireCoreFieldAwareFallback": True,
        "benchmarkTurnoverRequired": False,
    }
    for implementation in (
        BACKEND_ROOT / "market_data" / "intraday_shadow_v4.py",
        BACKEND_ROOT / "market_data" / "intraday_capture_v2.py",
        BACKEND_ROOT
        / "market_data"
        / "providers"
        / "a_stock_data_intraday_v2.py",
    ):
        payload["implementation"]["files"].append(
            {"path": str(implementation), "sha256": _sha(implementation)}
        )
    declaration.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    _sidecar(declaration)
    return declaration


def _write_v5_declaration(root: Path, provider: Path, capture: Path) -> Path:
    declaration = _write_v4_declaration(root, provider, capture)
    payload = json.loads(declaration.read_text(encoding="utf-8"))
    payload["protocolId"] = "intraday-shadow-test-v5"
    payload["runId"] = "intraday_shadow_test_v5"
    payload["databasePath"] = str(root / "run-v5" / "ledger.sqlite3")
    payload["timing"].update(
        {
            "freezeAfter": "14:56:00",
            "freezeDeadline": "15:05:30",
        }
    )
    payload["causality"] = {
        "version": 1,
        "decisionIntentRequired": True,
        "decisionFreezeAfter": "14:50:00",
        "decisionFreezeDeadline": "14:54:30",
        "executionBindingAfter": "14:56:00",
        "executionBindingDeadline": "15:05:30",
        "requireNoPriorPendingSession": True,
        "requirePortfolioFingerprint": True,
    }
    implementation_v5 = BACKEND_ROOT / "market_data" / "intraday_shadow_v5.py"
    payload["implementation"]["files"].append(
        {"path": str(implementation_v5), "sha256": _sha(implementation_v5)}
    )
    declaration.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    _sidecar(declaration)
    return declaration


def _write_v6_declaration(root: Path, provider: Path, capture: Path) -> Path:
    declaration = _write_v5_declaration(root, provider, capture)
    payload = json.loads(declaration.read_text(encoding="utf-8"))
    payload["protocolId"] = "intraday-shadow-test-v6"
    payload["runId"] = "intraday_shadow_test_v6"
    payload["databasePath"] = str(root / "run-v6" / "ledger.sqlite3")

    report_without_hash = {
        "schemaVersion": 1,
        "kind": "intraday_source_qualification_report",
        "phase": "SOURCE_RELIABILITY_QUALIFICATION_ONLY",
        "profitEvidenceEligible": False,
        "status": "PASS",
        "decision": "INDEPENDENT_SOURCE_QUALIFIED",
        "candidate": {
            "source": "a-stock-data/easy-tdx-public",
            "transport": "TCP/7709",
            "packageVersion": "1.20.6",
            "realtimeContractSha256": REALTIME_CONTRACT_SHA256,
            "prohibitCrossSourceFieldMerge": True,
        },
    }
    report_hash = hashlib.sha256(
        json.dumps(
            report_without_hash,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()
    report_payload = {**report_without_hash, "reportSha256": report_hash}
    report = root / f"source_qualification_v3_{report_hash[:12]}.json"
    report.write_text(
        json.dumps(
            report_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ),
        encoding="utf-8",
    )
    _sidecar(report)
    payload["sourceReliability"] = {
        "version": 1,
        "sourceOrder": ["tencent", "easy_tdx", "eastmoney", "sina"],
        "equityCoreFields": ["price", "previousClose", "turnoverPercent"],
        "benchmarkCoreFields": ["price", "previousClose"],
        "independentFallbackTransport": "TCP/7709",
        "easyTdxPackageVersion": "1.20.6",
        "realtimeContractSha256": REALTIME_CONTRACT_SHA256,
        "requireRealtimeContractForEveryQuote": True,
        "prohibitCrossSourceFieldMerge": True,
        "qualification": {
            "reportPath": str(report),
            "fileSha256": _sha(report),
            "reportSha256": report_hash,
        },
    }
    implementation_v6 = BACKEND_ROOT / "market_data" / "intraday_shadow_v6.py"
    payload["implementation"]["files"].append(
        {"path": str(implementation_v6), "sha256": _sha(implementation_v6)}
    )
    declaration.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    _sidecar(declaration)
    return declaration


def _write_v7_declaration(root: Path, provider: Path, capture: Path) -> Path:
    declaration = _write_v6_declaration(root, provider, capture)
    payload = json.loads(declaration.read_text(encoding="utf-8"))
    payload["protocolId"] = V7_PROTOCOL_ID
    payload["runId"] = "intraday_shadow_test_v7"
    payload["databasePath"] = str(root / "run-v7" / "ledger.sqlite3")
    implementation_v7 = BACKEND_ROOT / "market_data" / "intraday_shadow_v7.py"
    payload["implementation"]["files"].append(
        {"path": str(implementation_v7), "sha256": _sha(implementation_v7)}
    )
    declaration.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    _sidecar(declaration)
    return declaration


def _write_v8_declaration(root: Path, provider: Path, capture: Path) -> Path:
    declaration = _write_v7_declaration(root, provider, capture)
    payload = json.loads(declaration.read_text(encoding="utf-8"))
    payload["protocolId"] = V8_PROTOCOL_ID
    payload["runId"] = "intraday_shadow_test_v8"
    payload["databasePath"] = str(root / "run-v8" / "ledger.sqlite3")
    implementation_v8 = BACKEND_ROOT / "market_data" / "intraday_shadow_v8.py"
    payload["implementation"]["files"].append(
        {"path": str(implementation_v8), "sha256": _sha(implementation_v8)}
    )
    declaration.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    _sidecar(declaration)
    return declaration


def _write_v9_declaration(root: Path, provider: Path, capture: Path) -> Path:
    declaration = _write_v8_declaration(root, provider, capture)
    payload = json.loads(declaration.read_text(encoding="utf-8"))
    payload["protocolId"] = V9_PROTOCOL_ID
    payload["runId"] = "intraday_shadow_test_v9"
    payload["databasePath"] = str(root / "run-v9" / "ledger.sqlite3")
    implementation_v9 = BACKEND_ROOT / "market_data" / "intraday_shadow_v9.py"
    payload["implementation"]["files"].append(
        {"path": str(implementation_v9), "sha256": _sha(implementation_v9)}
    )
    declaration.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    _sidecar(declaration)
    return declaration


class IntradayShadowTests(unittest.TestCase):
    def test_candidate_and_baseline_use_only_negative_decision_rows(self):
        snapshot = {
            "quoteTradeDate": "2026-08-14",
            "quotes": [
                {
                    "symbol": f"{600000 + index:06d}.SH",
                    "price": 9.0 + index / 1000,
                    "previousClose": 10.0,
                    "turnoverPercent": index + 1,
                    "tradeDate": "2026-08-14",
                    "isStale": False,
                }
                for index in range(60)
            ]
            + [
                {
                    "symbol": "000001.SZ",
                    "price": 11.0,
                    "previousClose": 10.0,
                    "turnoverPercent": 1000.0,
                    "tradeDate": "2026-08-14",
                    "isStale": False,
                }
            ],
        }

        ranked = _rank_signals(snapshot, top_k=50)

        self.assertEqual(len(ranked["C1"]), 50)
        self.assertEqual(len(ranked["B0"]), 50)
        self.assertNotIn("000001.SZ", {row["symbol"] for row in ranked["C1"]})
        self.assertNotEqual(
            [row["symbol"] for row in ranked["C1"]],
            [row["symbol"] for row in ranked["B0"]],
        )

    def test_v2_rejects_decision_snapshot_below_95_percent_coverage(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            provider = _write_provider(root / "provider", BASE_DATES)
            capture = root / "capture"
            dropped = set(_symbols()[-20:])
            _capture_pair(
                provider,
                capture,
                "2026-08-14",
                decision_client=_DroppingQuoteClient(dropped),
            )
            declaration = _write_v2_declaration(root, provider, capture)
            coordinator = V2IntradayShadowCoordinator(declaration)

            with self.assertRaisesRegex(ValueError, "95% quality gate"):
                coordinator.freeze_session(
                    "2026-08-14",
                    now=datetime(2026, 8, 14, 15, 7, tzinfo=CHINA_TZ),
                )

            state = coordinator.get_state()
            self.assertEqual(state["status"], "STAGED")
            self.assertEqual(state["pairedEvidence"]["frozenSessions"], 0)

    def test_v2_rejects_missing_execution_quote_for_ranked_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            provider = _write_provider(root / "provider", BASE_DATES)
            capture = root / "capture"
            dropped = set(_symbols()[-15:])
            _capture_pair(
                provider,
                capture,
                "2026-08-14",
                execution_client=_DroppingQuoteClient(
                    dropped,
                    price_shift=0.01,
                    reverse_turnover=True,
                ),
            )
            declaration = _write_v2_declaration(root, provider, capture)
            coordinator = V2IntradayShadowCoordinator(declaration)

            with self.assertRaisesRegex(ValueError, "lacks usable target quotes"):
                coordinator.freeze_session(
                    "2026-08-14",
                    now=datetime(2026, 8, 14, 15, 7, tzinfo=CHINA_TZ),
                )

            self.assertEqual(
                coordinator.get_state()["pairedEvidence"]["frozenSessions"],
                0,
            )

    def test_v2_rejects_missing_benchmark_even_with_high_coverage(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            provider = _write_provider(root / "provider", BASE_DATES)
            capture = root / "capture"
            _capture_pair(
                provider,
                capture,
                "2026-08-14",
                decision_client=_DroppingQuoteClient({"000300.SH"}),
            )
            declaration = _write_v2_declaration(root, provider, capture)
            coordinator = V2IntradayShadowCoordinator(declaration)

            with self.assertRaisesRegex(ValueError, "usable benchmark quote"):
                coordinator.freeze_session(
                    "2026-08-14",
                    now=datetime(2026, 8, 14, 15, 7, tzinfo=CHINA_TZ),
                )

    def test_v2_requires_frozen_quality_contract_before_ledger_creation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            provider = _write_provider(root / "provider", BASE_DATES)
            declaration = _write_declaration(root, provider, root / "capture")

            with self.assertRaisesRegex(ValueError, "quality contract is missing"):
                V2IntradayShadowCoordinator(declaration)

            self.assertFalse((root / "run" / "ledger.sqlite3").exists())

    def test_v2_full_quality_pair_freezes_normally(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            provider = _write_provider(root / "provider", BASE_DATES)
            capture = root / "capture"
            _capture_pair(provider, capture, "2026-08-14")
            declaration = _write_v2_declaration(root, provider, capture)
            coordinator = V2IntradayShadowCoordinator(declaration)

            state = coordinator.freeze_session(
                "2026-08-14",
                now=datetime(2026, 8, 14, 15, 7, tzinfo=CHINA_TZ),
            )

            self.assertEqual(state["status"], "AWAITING_OFFICIAL_DAILY")
            self.assertEqual(state["pairedEvidence"]["frozenSessions"], 1)

    def test_v2_quality_gated_pair_settles_with_official_factor(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            provider = _write_provider(root / "provider", BASE_DATES)
            successor = _write_provider(
                root / "successor",
                (*BASE_DATES, "2026-08-14"),
            )
            capture = root / "capture"
            _capture_pair(provider, capture, "2026-08-14")
            declaration = _write_v2_declaration(root, provider, capture)
            coordinator = V2IntradayShadowCoordinator(declaration)
            coordinator.freeze_session(
                "2026-08-14",
                now=datetime(2026, 8, 14, 15, 7, tzinfo=CHINA_TZ),
            )

            with patch(
                "market_data.intraday_shadow.discover_latest_provider",
                return_value=successor,
            ):
                settled = coordinator.settle_ready_sessions(
                    now=datetime(2026, 8, 15, 0, 40, tzinfo=CHINA_TZ)
                )
            state = coordinator.get_state()

            self.assertEqual(settled, ["2026-08-14"])
            self.assertEqual(state["status"], "ACTIVE")
            self.assertEqual(state["pairedEvidence"]["observationSessions"], 1)
            self.assertEqual(len(state["arms"]["C1"]["positions"]), 50)
            self.assertEqual(len(state["arms"]["B0"]["positions"]), 50)

    def test_v3_rejects_aged_decision_quote_for_ranked_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            provider = _write_provider(root / "provider", BASE_DATES)
            capture = root / "capture"
            _capture_pair(
                provider,
                capture,
                "2026-08-14",
                decision_client=_AgingQuoteClient({"600100.SH"}),
            )
            declaration = _write_v3_declaration(root, provider, capture)
            coordinator = V3IntradayShadowCoordinator(declaration)

            with self.assertRaisesRegex(
                ValueError,
                "decision snapshot lacks usable target quotes",
            ):
                coordinator.freeze_session(
                    "2026-08-14",
                    now=datetime(2026, 8, 14, 15, 7, tzinfo=CHINA_TZ),
                )

            state = coordinator.get_state()
            self.assertEqual(state["status"], "STAGED")
            self.assertEqual(state["pairedEvidence"]["frozenSessions"], 0)

    def test_v3_requires_frozen_decision_target_contract_before_ledger_creation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            provider = _write_provider(root / "provider", BASE_DATES)
            declaration = _write_v2_declaration(root, provider, root / "capture")

            with self.assertRaisesRegex(
                ValueError,
                "decision-target contract is missing",
            ):
                V3IntradayShadowCoordinator(declaration)

            self.assertFalse((root / "run-v2" / "ledger.sqlite3").exists())

    def test_v3_full_quality_pair_settles_with_official_factor(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            provider = _write_provider(root / "provider", BASE_DATES)
            successor = _write_provider(
                root / "successor",
                (*BASE_DATES, "2026-08-14"),
            )
            capture = root / "capture"
            _capture_pair(provider, capture, "2026-08-14")
            declaration = _write_v3_declaration(root, provider, capture)
            coordinator = V3IntradayShadowCoordinator(declaration)
            coordinator.freeze_session(
                "2026-08-14",
                now=datetime(2026, 8, 14, 15, 7, tzinfo=CHINA_TZ),
            )

            with patch(
                "market_data.intraday_shadow.discover_latest_provider",
                return_value=successor,
            ):
                settled = coordinator.settle_ready_sessions(
                    now=datetime(2026, 8, 15, 0, 40, tzinfo=CHINA_TZ)
                )
            state = coordinator.get_state()

            self.assertEqual(settled, ["2026-08-14"])
            self.assertEqual(state["status"], "ACTIVE")
            self.assertEqual(state["pairedEvidence"]["observationSessions"], 1)
            self.assertEqual(len(state["arms"]["C1"]["positions"]), 50)
            self.assertEqual(len(state["arms"]["B0"]["positions"]), 50)

    def test_v4_accepts_benchmark_without_turnover_without_mutating_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            provider = _write_provider(root / "provider", BASE_DATES)
            capture = root / "capture"
            _capture_pair(
                provider,
                capture,
                "2026-08-14",
                decision_client=_BenchmarkWithoutTurnoverQuoteClient(),
                execution_client=_BenchmarkWithoutTurnoverQuoteClient(
                    price_shift=0.01,
                    reverse_turnover=True,
                ),
            )
            declaration = _write_v4_declaration(root, provider, capture)
            coordinator = V4IntradayShadowCoordinator(declaration)
            _, decision = coordinator._snapshot_at(
                "2026-08-14",
                "14:50:00",
                available_by=datetime(2026, 8, 14, 15, 7, tzinfo=CHINA_TZ),
            )

            usable = coordinator._quality_quote_map(decision, label="decision")
            state = coordinator.freeze_session(
                "2026-08-14",
                now=datetime(2026, 8, 14, 15, 7, tzinfo=CHINA_TZ),
            )

            self.assertIsNone(usable["000300.SH"]["turnoverPercent"])
            benchmark = next(
                quote
                for quote in decision["quotes"]
                if quote["symbol"] == "000300.SH"
            )
            self.assertIsNone(benchmark["turnoverPercent"])
            self.assertEqual(state["status"], "AWAITING_OFFICIAL_DAILY")
            self.assertEqual(state["pairedEvidence"]["frozenSessions"], 1)

    def test_v4_rejects_insufficient_equity_core_field_coverage(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            provider = _write_provider(root / "provider", BASE_DATES)
            capture = root / "capture"
            missing = set(_symbols()[:16])
            _capture_pair(
                provider,
                capture,
                "2026-08-14",
                decision_client=_MissingTurnoverQuoteClient(missing),
            )
            declaration = _write_v4_declaration(root, provider, capture)
            coordinator = V4IntradayShadowCoordinator(declaration)

            with self.assertRaisesRegex(ValueError, "failed the 95% quality gate"):
                coordinator.freeze_session(
                    "2026-08-14",
                    now=datetime(2026, 8, 14, 15, 7, tzinfo=CHINA_TZ),
                )

            self.assertEqual(
                coordinator.get_state()["pairedEvidence"]["frozenSessions"],
                0,
            )

    def test_v4_requires_frozen_core_field_contract_before_ledger_creation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            provider = _write_provider(root / "provider", BASE_DATES)
            declaration = _write_v3_declaration(root, provider, root / "capture")

            with self.assertRaisesRegex(ValueError, "v4 quality extension drifted"):
                V4IntradayShadowCoordinator(declaration)

            self.assertFalse((root / "run-v3" / "ledger.sqlite3").exists())

    def test_future_only_freeze_is_idempotent_and_binds_both_snapshots(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            provider = _write_provider(root / "provider", BASE_DATES)
            capture = root / "capture"
            _capture_pair(provider, capture, "2026-08-14")
            declaration = _write_declaration(root, provider, capture)
            coordinator = IntradayShadowCoordinator(declaration)

            with self.assertRaisesRegex(ValueError, "pre-declaration"):
                coordinator.freeze_session(
                    "2026-08-13",
                    now=datetime(2026, 8, 13, 15, 7, tzinfo=CHINA_TZ),
                )
            first = coordinator.freeze_session(
                "2026-08-14",
                now=datetime(2026, 8, 14, 15, 7, tzinfo=CHINA_TZ),
            )
            second = coordinator.freeze_session(
                "2026-08-14",
                now=datetime(2026, 8, 14, 15, 8, tzinfo=CHINA_TZ),
            )

            self.assertEqual(first["pairedEvidence"]["frozenSessions"], 1)
            self.assertEqual(second["pairedEvidence"]["frozenSessions"], 1)
            self.assertEqual(first["status"], "AWAITING_OFFICIAL_DAILY")
            self.assertEqual(len(first["latestSignals"]["C1"]), 50)
            self.assertEqual(first["audit"]["events"], 2)
            self.assertEqual(first["latestSession"]["status"], "FROZEN_AWAITING_OFFICIAL")

    def test_official_factor_settlement_is_paired_and_after_cost(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            provider = _write_provider(root / "provider", BASE_DATES)
            successor = _write_provider(
                root / "successor", (*BASE_DATES, "2026-08-14")
            )
            capture = root / "capture"
            _capture_pair(provider, capture, "2026-08-14")
            declaration = _write_declaration(root, provider, capture)
            coordinator = IntradayShadowCoordinator(declaration)
            coordinator.freeze_session(
                "2026-08-14",
                now=datetime(2026, 8, 14, 15, 7, tzinfo=CHINA_TZ),
            )

            with patch(
                "market_data.intraday_shadow.discover_latest_provider",
                return_value=successor,
            ):
                settled = coordinator.settle_ready_sessions(
                    now=datetime(2026, 8, 15, 0, 40, tzinfo=CHINA_TZ)
                )
            state = coordinator.get_state()

            self.assertEqual(settled, ["2026-08-14"])
            self.assertEqual(state["status"], "ACTIVE")
            self.assertEqual(state["pairedEvidence"]["observationSessions"], 1)
            self.assertEqual(len(state["arms"]["C1"]["positions"]), 50)
            self.assertEqual(len(state["arms"]["B0"]["positions"]), 50)
            self.assertLess(state["arms"]["C1"]["account"]["totalReturn"], 0)
            self.assertEqual(state["audit"]["events"], 3)

    def test_pair_rolls_back_when_second_arm_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            provider = _write_provider(root / "provider", BASE_DATES)
            successor = _write_provider(
                root / "successor", (*BASE_DATES, "2026-08-14")
            )
            capture = root / "capture"
            _capture_pair(provider, capture, "2026-08-14")
            declaration = _write_declaration(root, provider, capture)
            coordinator = IntradayShadowCoordinator(declaration)
            coordinator.freeze_session(
                "2026-08-14",
                now=datetime(2026, 8, 14, 15, 7, tzinfo=CHINA_TZ),
            )
            original = coordinator._settle_arm

            def fail_second(connection, **kwargs):
                if kwargs["arm"] == "B0":
                    raise RuntimeError("injected pair failure")
                return original(connection, **kwargs)

            with patch(
                "market_data.intraday_shadow.discover_latest_provider",
                return_value=successor,
            ), patch.object(coordinator, "_settle_arm", side_effect=fail_second):
                with self.assertRaisesRegex(RuntimeError, "injected pair failure"):
                    coordinator.settle_ready_sessions(
                        now=datetime(2026, 8, 15, 0, 40, tzinfo=CHINA_TZ)
                    )

            state = coordinator.get_state()
            self.assertEqual(state["pairedEvidence"]["observationSessions"], 0)
            self.assertEqual(state["status"], "AWAITING_OFFICIAL_DAILY")
            self.assertEqual(state["arms"]["C1"]["positions"], [])
            self.assertEqual(state["arms"]["B0"]["positions"], [])

    @unittest.skipUnless(hasattr(os, "fork"), "requires POSIX fork")
    def test_pair_recovers_atomically_after_sigkill_between_arms(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            provider = _write_provider(root / "provider", BASE_DATES)
            successor = _write_provider(
                root / "successor", (*BASE_DATES, "2026-08-14")
            )
            capture = root / "capture"
            _capture_pair(provider, capture, "2026-08-14")
            declaration = _write_declaration(root, provider, capture)
            coordinator = IntradayShadowCoordinator(declaration)
            coordinator.freeze_session(
                "2026-08-14",
                now=datetime(2026, 8, 14, 15, 7, tzinfo=CHINA_TZ),
            )

            read_fd, write_fd = os.pipe()
            child_pid = os.fork()
            if child_pid == 0:
                os.close(read_fd)
                try:
                    child = IntradayShadowCoordinator(declaration)
                    original = child._settle_arm

                    def pause_before_second_arm(connection, **kwargs):
                        if kwargs["arm"] == "B0":
                            os.write(write_fd, b"B")
                            while True:
                                signal.pause()
                        return original(connection, **kwargs)

                    with patch(
                        "market_data.intraday_shadow.discover_latest_provider",
                        return_value=successor,
                    ), patch.object(
                        child,
                        "_settle_arm",
                        side_effect=pause_before_second_arm,
                    ):
                        child.settle_ready_sessions(
                            now=datetime(2026, 8, 15, 0, 40, tzinfo=CHINA_TZ)
                        )
                except BaseException:
                    os._exit(91)
                finally:
                    os.close(write_fd)
                os._exit(92)

            os.close(write_fd)
            wait_status = None
            try:
                ready, _, _ = select.select([read_fd], [], [], 20)
                self.assertTrue(ready, "child did not reach the paired transaction boundary")
                self.assertEqual(os.read(read_fd, 1), b"B")
                os.kill(child_pid, signal.SIGKILL)
                _, wait_status = os.waitpid(child_pid, 0)
            finally:
                os.close(read_fd)
                if wait_status is None:
                    try:
                        os.kill(child_pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    _, wait_status = os.waitpid(child_pid, 0)

            self.assertTrue(os.WIFSIGNALED(wait_status))
            self.assertEqual(os.WTERMSIG(wait_status), signal.SIGKILL)

            recovered = IntradayShadowCoordinator(declaration)
            state = recovered.get_state()
            self.assertEqual(state["status"], "AWAITING_OFFICIAL_DAILY")
            self.assertEqual(state["pairedEvidence"]["observationSessions"], 0)
            self.assertEqual(state["arms"]["C1"]["positions"], [])
            self.assertEqual(state["arms"]["B0"]["positions"], [])
            backup = recovered.run_dir / f".{recovered.db_path.name}.2026-08-14.bak"
            self.assertTrue(backup.is_file())
            with sqlite3.connect(recovered.db_path) as connection:
                self.assertEqual(
                    connection.execute("PRAGMA integrity_check").fetchone()[0],
                    "ok",
                )
                self.assertEqual(
                    connection.execute("PRAGMA journal_mode").fetchone()[0],
                    "wal",
                )
                self.assertEqual(
                    connection.execute("PRAGMA synchronous").fetchone()[0],
                    2,
                )
                self.assertEqual(
                    connection.execute("SELECT COUNT(*) FROM orders").fetchone()[0],
                    0,
                )
                self.assertEqual(
                    connection.execute("SELECT COUNT(*) FROM nav_history").fetchone()[0],
                    0,
                )

            with patch(
                "market_data.intraday_shadow.discover_latest_provider",
                return_value=successor,
            ):
                settled = recovered.settle_ready_sessions(
                    now=datetime(2026, 8, 15, 0, 41, tzinfo=CHINA_TZ)
                )
            final_state = recovered.get_state()

            self.assertEqual(settled, ["2026-08-14"])
            self.assertEqual(final_state["status"], "ACTIVE")
            self.assertEqual(final_state["pairedEvidence"]["observationSessions"], 1)
            self.assertEqual(len(final_state["arms"]["C1"]["positions"]), 50)
            self.assertEqual(len(final_state["arms"]["B0"]["positions"]), 50)
            self.assertEqual(final_state["audit"]["events"], 3)
            self.assertFalse(backup.exists())

    def test_missing_exact_execution_slot_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            provider = _write_provider(root / "provider", BASE_DATES)
            capture = root / "capture"
            run_capture_cycle(
                provider,
                capture,
                poll_seconds=60,
                now=datetime.fromisoformat("2026-08-14T14:50:03+08:00"),
                client=_QuoteClient(),
            )
            coordinator = IntradayShadowCoordinator(
                _write_declaration(root, provider, capture)
            )

            with self.assertRaisesRegex(ValueError, "exactly one .*14:55:00"):
                coordinator.freeze_session(
                    "2026-08-14",
                    now=datetime(2026, 8, 14, 15, 7, tzinfo=CHINA_TZ),
                )

    def test_late_arriving_snapshot_cannot_be_used_retroactively(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            provider = _write_provider(root / "provider", BASE_DATES)
            capture = root / "capture"
            late = datetime(2026, 8, 14, 15, 21, tzinfo=CHINA_TZ)
            for clock, client in (
                ("14:50:03", _QuoteClient()),
                ("14:55:03", _QuoteClient(price_shift=0.01)),
            ):
                run_capture_cycle(
                    provider,
                    capture,
                    poll_seconds=60,
                    now=datetime.fromisoformat(f"2026-08-14T{clock}+08:00"),
                    clock=lambda: late,
                    client=client,
                )
            coordinator = IntradayShadowCoordinator(
                _write_declaration(root, provider, capture)
            )

            with self.assertRaisesRegex(ValueError, "not available by freeze time"):
                coordinator.freeze_session(
                    "2026-08-14",
                    now=datetime(2026, 8, 14, 15, 7, tzinfo=CHINA_TZ),
                )

    def test_missed_deadline_is_permanent_and_cannot_be_backfilled(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            provider = _write_provider(root / "provider", BASE_DATES)
            capture = root / "capture"
            coordinator = IntradayShadowCoordinator(
                _write_declaration(root, provider, capture)
            )

            status = coordinator.run_cycle(
                poll_seconds=60,
                now=datetime(2026, 8, 14, 15, 21, tzinfo=CHINA_TZ),
            )
            self.assertEqual(status["status"], "MISSED_SESSION")
            self.assertEqual(status["actions"], ["MISSED:2026-08-14"])
            self.assertEqual(status["evidenceCheckpoint"]["count"], 1)

            _capture_pair(provider, capture, "2026-08-14")
            with self.assertRaisesRegex(ValueError, "MISSED_SESSION"):
                coordinator.freeze_session(
                    "2026-08-14",
                    now=datetime(2026, 8, 14, 15, 7, tzinfo=CHINA_TZ),
                )

    def test_restart_detects_an_unrecorded_prior_trading_day(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            provider = _write_provider(root / "provider", BASE_DATES)
            coordinator = IntradayShadowCoordinator(
                _write_declaration(root, provider, root / "capture")
            )

            status = coordinator.run_cycle(
                poll_seconds=60,
                now=datetime(2026, 8, 17, 9, 0, tzinfo=CHINA_TZ),
            )

            self.assertEqual(status["status"], "MISSED_SESSION")
            self.assertEqual(status["actions"], ["MISSED:2026-08-14"])
            self.assertEqual(
                status["state"]["pairedEvidence"]["missedSessions"][0]["trade_date"],
                "2026-08-14",
            )

    def test_t_plus_one_blocks_same_day_inventory_and_allows_prior_inventory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            provider = _write_provider(root / "provider", BASE_DATES)
            successor = _write_provider(
                root / "successor", (*BASE_DATES, "2026-08-14", "2026-08-17")
            )
            capture = root / "capture"
            _capture_pair(provider, capture, "2026-08-14")
            coordinator = IntradayShadowCoordinator(
                _write_declaration(root, provider, capture)
            )
            coordinator.freeze_session(
                "2026-08-14",
                now=datetime(2026, 8, 14, 15, 7, tzinfo=CHINA_TZ),
            )
            with patch(
                "market_data.intraday_shadow.discover_latest_provider",
                return_value=successor,
            ):
                coordinator.settle_ready_sessions(
                    now=datetime(2026, 8, 15, 0, 40, tzinfo=CHINA_TZ)
                )

            _capture_pair(
                provider,
                capture,
                "2026-08-17",
                decision_client=_QuoteClient(
                    reverse_turnover=True, reverse_returns=True
                ),
                execution_client=_QuoteClient(
                    price_shift=0.02,
                    reverse_turnover=True,
                    reverse_returns=True,
                ),
            )
            coordinator.freeze_session(
                "2026-08-17",
                now=datetime(2026, 8, 17, 15, 7, tzinfo=CHINA_TZ),
            )
            state = coordinator.get_state()
            held = {row["symbol"] for row in state["arms"]["C1"]["positions"]}
            desired = {row["symbol"] for row in state["latestSignals"]["C1"]}
            to_sell = sorted(held - desired)
            self.assertGreater(len(to_sell), 1)
            same_day_inventory = to_sell[0]
            with sqlite3.connect(coordinator.db_path) as connection:
                connection.execute(
                    "UPDATE positions SET acquired_date='2026-08-17' "
                    "WHERE arm='C1' AND symbol=?",
                    (same_day_inventory,),
                )

            with patch(
                "market_data.intraday_shadow.discover_latest_provider",
                return_value=successor,
            ):
                coordinator.settle_ready_sessions(
                    now=datetime(2026, 8, 18, 0, 40, tzinfo=CHINA_TZ)
                )

            with sqlite3.connect(coordinator.db_path) as connection:
                blocked = connection.execute(
                    "SELECT status, reason FROM orders WHERE trade_date='2026-08-17' "
                    "AND arm='C1' AND symbol=? AND side='sell'",
                    (same_day_inventory,),
                ).fetchone()
                prior_day_sells = connection.execute(
                    "SELECT COUNT(*) FROM orders WHERE trade_date='2026-08-17' "
                    "AND arm='C1' AND side='sell' AND status='filled'"
                ).fetchone()[0]
            self.assertEqual(blocked, ("rejected", "T+1 可卖数量不足"))
            self.assertGreater(prior_day_sells, 0)

    def test_evidence_checkpoint_detects_manifest_tampering(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            provider = _write_provider(root / "provider", BASE_DATES)
            coordinator = IntradayShadowCoordinator(
                _write_declaration(root, provider, root / "capture")
            )
            status = coordinator.run_cycle(
                poll_seconds=60,
                now=datetime(2026, 8, 13, 17, 0, tzinfo=CHINA_TZ),
            )
            self.assertEqual(status["evidenceCheckpoint"]["status"], "CREATED")
            manifest = next(coordinator.evidence_dir.glob("checkpoint_*/manifest.json"))
            os.chmod(manifest, 0o644)
            manifest.write_text(
                manifest.read_text(encoding="utf-8") + " ", encoding="utf-8"
            )

            with self.assertRaisesRegex(ValueError, "file hash mismatch"):
                coordinator.get_state()

    def test_implementation_or_declaration_drift_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            provider = _write_provider(root / "provider", BASE_DATES)
            capture = root / "capture"
            declaration = _write_declaration(root, provider, capture)
            payload = json.loads(declaration.read_text(encoding="utf-8"))
            payload["strategy"]["candidateFormula"] = "changed"
            declaration.write_text(json.dumps(payload), encoding="utf-8")
            _sidecar(declaration)

            with self.assertRaisesRegex(ValueError, "formula drifted"):
                IntradayShadowCoordinator(declaration)

    def test_invalid_execution_unit_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            provider = _write_provider(root / "provider", BASE_DATES)
            declaration = _write_declaration(root, provider, root / "capture")
            payload = json.loads(declaration.read_text(encoding="utf-8"))
            payload["execution"]["lotSize"] = 0
            declaration.write_text(
                json.dumps(payload, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            _sidecar(declaration)

            with self.assertRaisesRegex(ValueError, "lotSize must be a positive integer"):
                IntradayShadowCoordinator(declaration)

    def test_v5_freezes_decision_before_binding_execution_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            provider = _write_provider(root / "provider", BASE_DATES)
            capture = root / "capture"
            run_capture_cycle(
                provider,
                capture,
                poll_seconds=60,
                now=datetime.fromisoformat("2026-08-14T14:50:03+08:00"),
                client=_QuoteClient(),
            )
            coordinator = V5IntradayShadowCoordinator(
                _write_v5_declaration(root, provider, capture)
            )

            decision = coordinator.run_cycle(
                poll_seconds=60,
                now=datetime(2026, 8, 14, 14, 50, 20, tzinfo=CHINA_TZ),
            )
            with sqlite3.connect(coordinator.db_path) as connection:
                decision_rows = connection.execute(
                    "SELECT COUNT(*) FROM decision_intents"
                ).fetchone()[0]
                session_rows = connection.execute(
                    "SELECT COUNT(*) FROM signal_sessions"
                ).fetchone()[0]
            self.assertEqual(
                decision["status"], "DECISION_FROZEN_AWAITING_EXECUTION"
            )
            self.assertEqual(
                decision["actions"], ["DECISION_FROZEN:2026-08-14"]
            )
            self.assertEqual((decision_rows, session_rows), (1, 0))
            self.assertEqual(decision["state"]["audit"]["events"], 2)

            run_capture_cycle(
                provider,
                capture,
                poll_seconds=60,
                now=datetime.fromisoformat("2026-08-14T14:55:03+08:00"),
                client=_QuoteClient(price_shift=0.01, reverse_turnover=True),
            )
            execution = coordinator.run_cycle(
                poll_seconds=60,
                now=datetime(2026, 8, 14, 14, 56, 20, tzinfo=CHINA_TZ),
            )
            with sqlite3.connect(coordinator.db_path) as connection:
                intent_status = connection.execute(
                    "SELECT status FROM decision_intents"
                ).fetchone()[0]
                session_rows = connection.execute(
                    "SELECT COUNT(*) FROM signal_sessions"
                ).fetchone()[0]
                signal_rows = connection.execute(
                    "SELECT COUNT(*) FROM signals"
                ).fetchone()[0]
            self.assertEqual(execution["status"], "AWAITING_OFFICIAL_DAILY")
            self.assertEqual(
                execution["actions"], ["EXECUTION_BOUND:2026-08-14"]
            )
            self.assertEqual(intent_status, "EXECUTION_BOUND")
            self.assertEqual((session_rows, signal_rows), (1, 100))
            self.assertEqual(execution["state"]["audit"]["events"], 4)
            self.assertTrue(execution["evidenceCheckpoint"]["upToDate"])

    def test_v6_rejects_snapshot_without_realtime_source_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            provider = _write_provider(root / "provider", BASE_DATES)
            capture = root / "capture"
            run_capture_cycle(
                provider,
                capture,
                poll_seconds=60,
                now=datetime.fromisoformat("2026-08-14T14:50:03+08:00"),
                client=_QuoteClient(),
            )
            coordinator = V6IntradayShadowCoordinator(
                _write_v6_declaration(root, provider, capture)
            )

            with self.assertRaisesRegex(ValueError, "source contract is missing"):
                coordinator.freeze_decision_intent(
                    "2026-08-14",
                    now=datetime(2026, 8, 14, 14, 50, 20, tzinfo=CHINA_TZ),
                )

            self.assertEqual(
                coordinator.get_state()["pairedEvidence"]["decisionIntentSessions"],
                0,
            )

    def test_v6_runs_two_phase_flow_with_qualified_source_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            provider = _write_provider(root / "provider", BASE_DATES)
            capture = root / "capture"
            run_capture_cycle(
                provider,
                capture,
                poll_seconds=60,
                now=datetime.fromisoformat("2026-08-14T14:50:03+08:00"),
                client=_ContractQuoteClient(),
            )
            declaration = _write_v6_declaration(root, provider, capture)
            coordinator = V6IntradayShadowCoordinator(declaration)

            decision = coordinator.run_cycle(
                poll_seconds=60,
                now=datetime(2026, 8, 14, 14, 50, 20, tzinfo=CHINA_TZ),
            )
            self.assertEqual(
                decision["status"], "DECISION_FROZEN_AWAITING_EXECUTION"
            )
            self.assertEqual(
                decision["state"]["sourceReliability"][
                    "realtimeContractSha256"
                ],
                REALTIME_CONTRACT_SHA256,
            )

            run_capture_cycle(
                provider,
                capture,
                poll_seconds=60,
                now=datetime.fromisoformat("2026-08-14T14:55:03+08:00"),
                client=_ContractQuoteClient(
                    price_shift=0.01,
                    reverse_turnover=True,
                ),
            )
            execution = coordinator.run_cycle(
                poll_seconds=60,
                now=datetime(2026, 8, 14, 14, 56, 20, tzinfo=CHINA_TZ),
            )
            self.assertEqual(execution["status"], "AWAITING_OFFICIAL_DAILY")
            self.assertEqual(
                execution["actions"], ["EXECUTION_BOUND:2026-08-14"]
            )

    def test_v7_starts_a_new_two_phase_ledger_with_the_v6_source_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            provider = _write_provider(root / "provider", BASE_DATES)
            capture = root / "capture"
            _capture_pair(
                provider,
                capture,
                "2026-08-14",
                decision_client=_ContractQuoteClient(),
                execution_client=_ContractQuoteClient(price_shift=0.01),
            )
            coordinator = V7IntradayShadowCoordinator(
                _write_v7_declaration(root, provider, capture)
            )

            decision = coordinator.run_cycle(
                poll_seconds=60,
                now=datetime(2026, 8, 14, 14, 50, 20, tzinfo=CHINA_TZ),
            )
            execution = coordinator.run_cycle(
                poll_seconds=60,
                now=datetime(2026, 8, 14, 14, 56, 20, tzinfo=CHINA_TZ),
            )

            self.assertEqual(
                decision["actions"], ["DECISION_FROZEN:2026-08-14"]
            )
            self.assertEqual(
                execution["actions"], ["EXECUTION_BOUND:2026-08-14"]
            )
            self.assertEqual(execution["state"]["runId"], "intraday_shadow_test_v7")
            self.assertEqual(execution["status"], "AWAITING_OFFICIAL_DAILY")

    def test_v8_starts_a_new_two_phase_ledger_with_the_v6_source_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            provider = _write_provider(root / "provider", BASE_DATES)
            capture = root / "capture"
            _capture_pair(
                provider,
                capture,
                "2026-08-14",
                decision_client=_ContractQuoteClient(),
                execution_client=_ContractQuoteClient(price_shift=0.01),
            )
            coordinator = V8IntradayShadowCoordinator(
                _write_v8_declaration(root, provider, capture)
            )

            decision = coordinator.run_cycle(
                poll_seconds=60,
                now=datetime(2026, 8, 14, 14, 50, 20, tzinfo=CHINA_TZ),
            )
            execution = coordinator.run_cycle(
                poll_seconds=60,
                now=datetime(2026, 8, 14, 14, 56, 20, tzinfo=CHINA_TZ),
            )

            self.assertEqual(
                decision["actions"], ["DECISION_FROZEN:2026-08-14"]
            )
            self.assertEqual(
                execution["actions"], ["EXECUTION_BOUND:2026-08-14"]
            )
            self.assertEqual(execution["state"]["runId"], "intraday_shadow_test_v8")
            self.assertEqual(execution["status"], "AWAITING_OFFICIAL_DAILY")

    def test_v9_starts_a_new_two_phase_ledger_with_the_v6_source_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            provider = _write_provider(root / "provider", BASE_DATES)
            capture = root / "capture"
            _capture_pair(
                provider,
                capture,
                "2026-08-14",
                decision_client=_ContractQuoteClient(),
                execution_client=_ContractQuoteClient(price_shift=0.01),
            )
            coordinator = V9IntradayShadowCoordinator(
                _write_v9_declaration(root, provider, capture)
            )

            decision = coordinator.run_cycle(
                poll_seconds=60,
                now=datetime(2026, 8, 14, 14, 50, 20, tzinfo=CHINA_TZ),
            )
            execution = coordinator.run_cycle(
                poll_seconds=60,
                now=datetime(2026, 8, 14, 14, 56, 20, tzinfo=CHINA_TZ),
            )

            self.assertEqual(
                decision["actions"], ["DECISION_FROZEN:2026-08-14"]
            )
            self.assertEqual(
                execution["actions"], ["EXECUTION_BOUND:2026-08-14"]
            )
            self.assertEqual(execution["state"]["runId"], "intraday_shadow_test_v9")
            self.assertEqual(execution["status"], "AWAITING_OFFICIAL_DAILY")

    def test_v5_execution_requires_prior_decision_intent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            provider = _write_provider(root / "provider", BASE_DATES)
            capture = root / "capture"
            _capture_pair(provider, capture, "2026-08-14")
            coordinator = V5IntradayShadowCoordinator(
                _write_v5_declaration(root, provider, capture)
            )

            with self.assertRaisesRegex(ValueError, "without a prior decision intent"):
                coordinator.freeze_session(
                    "2026-08-14",
                    now=datetime(2026, 8, 14, 14, 56, 20, tzinfo=CHINA_TZ),
                )

    def test_v5_waits_without_error_while_exact_snapshots_are_in_flight(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            provider = _write_provider(root / "provider", BASE_DATES)
            capture = root / "capture"
            coordinator = V5IntradayShadowCoordinator(
                _write_v5_declaration(root, provider, capture)
            )

            waiting_decision = coordinator.run_cycle(
                poll_seconds=60,
                now=datetime(2026, 8, 14, 14, 50, 10, tzinfo=CHINA_TZ),
            )
            self.assertEqual(waiting_decision["status"], "STAGED")
            self.assertEqual(waiting_decision["consecutiveErrors"], 0)
            self.assertEqual(
                waiting_decision["actions"],
                ["WAITING_DECISION_SNAPSHOT:2026-08-14"],
            )

            run_capture_cycle(
                provider,
                capture,
                poll_seconds=60,
                now=datetime.fromisoformat("2026-08-14T14:50:20+08:00"),
                client=_QuoteClient(),
            )
            frozen = coordinator.run_cycle(
                poll_seconds=60,
                now=datetime(2026, 8, 14, 14, 51, 10, tzinfo=CHINA_TZ),
            )
            self.assertEqual(
                frozen["status"], "DECISION_FROZEN_AWAITING_EXECUTION"
            )

            waiting_execution = coordinator.run_cycle(
                poll_seconds=60,
                now=datetime(2026, 8, 14, 14, 56, 10, tzinfo=CHINA_TZ),
            )
            self.assertEqual(
                waiting_execution["status"],
                "DECISION_FROZEN_AWAITING_EXECUTION",
            )
            self.assertEqual(waiting_execution["consecutiveErrors"], 0)
            self.assertEqual(
                waiting_execution["actions"],
                ["WAITING_EXECUTION_SNAPSHOT:2026-08-14"],
            )

    def test_v5_missed_decision_deadline_is_permanent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            provider = _write_provider(root / "provider", BASE_DATES)
            capture = root / "capture"
            coordinator = V5IntradayShadowCoordinator(
                _write_v5_declaration(root, provider, capture)
            )

            status = coordinator.run_cycle(
                poll_seconds=60,
                now=datetime(2026, 8, 14, 14, 55, 10, tzinfo=CHINA_TZ),
            )
            self.assertEqual(status["status"], "MISSED_SESSION")
            self.assertEqual(
                status["actions"],
                ["MISSED:2026-08-14:DECISION_INTENT_DEADLINE_PASSED"],
            )
            self.assertEqual(
                status["state"]["pairedEvidence"]["missedSessions"][0]["reason"],
                "DECISION_INTENT_DEADLINE_PASSED",
            )

            run_capture_cycle(
                provider,
                capture,
                poll_seconds=60,
                now=datetime.fromisoformat("2026-08-14T14:50:03+08:00"),
                client=_QuoteClient(),
            )
            with self.assertRaisesRegex(ValueError, "MISSED_SESSION"):
                coordinator.freeze_decision_intent(
                    "2026-08-14",
                    now=datetime(2026, 8, 14, 14, 50, 20, tzinfo=CHINA_TZ),
                )

    def test_v5_missed_execution_deadline_preserves_intent_without_fill(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            provider = _write_provider(root / "provider", BASE_DATES)
            capture = root / "capture"
            run_capture_cycle(
                provider,
                capture,
                poll_seconds=60,
                now=datetime.fromisoformat("2026-08-14T14:50:03+08:00"),
                client=_QuoteClient(),
            )
            coordinator = V5IntradayShadowCoordinator(
                _write_v5_declaration(root, provider, capture)
            )
            coordinator.run_cycle(
                poll_seconds=60,
                now=datetime(2026, 8, 14, 14, 50, 20, tzinfo=CHINA_TZ),
            )

            status = coordinator.run_cycle(
                poll_seconds=60,
                now=datetime(2026, 8, 14, 15, 6, 0, tzinfo=CHINA_TZ),
            )
            with sqlite3.connect(coordinator.db_path) as connection:
                intent = connection.execute(
                    "SELECT status FROM decision_intents"
                ).fetchone()[0]
                sessions = connection.execute(
                    "SELECT COUNT(*) FROM signal_sessions"
                ).fetchone()[0]
            self.assertEqual(status["status"], "MISSED_SESSION")
            self.assertEqual(intent, "DECISION_FROZEN")
            self.assertEqual(sessions, 0)
            self.assertEqual(
                status["state"]["pairedEvidence"]["missedSessions"][0]["reason"],
                "EXECUTION_BINDING_DEADLINE_PASSED",
            )

    def test_v5_rejects_portfolio_drift_between_decision_and_execution(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            provider = _write_provider(root / "provider", BASE_DATES)
            capture = root / "capture"
            _capture_pair(provider, capture, "2026-08-14")
            coordinator = V5IntradayShadowCoordinator(
                _write_v5_declaration(root, provider, capture)
            )
            coordinator.freeze_decision_intent(
                "2026-08-14",
                now=datetime(2026, 8, 14, 14, 50, 20, tzinfo=CHINA_TZ),
            )
            with sqlite3.connect(coordinator.db_path) as connection:
                connection.execute("UPDATE accounts SET cash = cash - 1 WHERE arm='C1'")

            with self.assertRaisesRegex(ValueError, "portfolio changed"):
                coordinator.freeze_session(
                    "2026-08-14",
                    now=datetime(2026, 8, 14, 14, 56, 20, tzinfo=CHINA_TZ),
                )

    def test_v5_restart_reuses_the_same_frozen_intent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            provider = _write_provider(root / "provider", BASE_DATES)
            capture = root / "capture"
            _capture_pair(provider, capture, "2026-08-14")
            declaration = _write_v5_declaration(root, provider, capture)
            first = V5IntradayShadowCoordinator(declaration)
            first.freeze_decision_intent(
                "2026-08-14",
                now=datetime(2026, 8, 14, 14, 50, 20, tzinfo=CHINA_TZ),
            )
            frozen = first.get_state()["latestDecisionIntent"]

            restarted = V5IntradayShadowCoordinator(declaration)
            restarted.freeze_decision_intent(
                "2026-08-14",
                now=datetime(2026, 8, 14, 14, 51, 20, tzinfo=CHINA_TZ),
            )
            restarted.freeze_session(
                "2026-08-14",
                now=datetime(2026, 8, 14, 14, 56, 20, tzinfo=CHINA_TZ),
            )
            state = restarted.get_state()

            self.assertEqual(
                state["latestDecisionIntent"]["decisionSnapshotSha256"],
                frozen["decisionSnapshotSha256"],
            )
            self.assertEqual(
                state["latestDecisionIntent"]["targetsSha256"],
                frozen["targetsSha256"],
            )
            self.assertEqual(
                state["latestDecisionIntent"]["status"], "EXECUTION_BOUND"
            )

    def test_v5_restart_reconciles_crash_after_session_insert(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            provider = _write_provider(root / "provider", BASE_DATES)
            capture = root / "capture"
            _capture_pair(provider, capture, "2026-08-14")
            declaration = _write_v5_declaration(root, provider, capture)
            coordinator = V5IntradayShadowCoordinator(declaration)
            coordinator.freeze_decision_intent(
                "2026-08-14",
                now=datetime(2026, 8, 14, 14, 50, 20, tzinfo=CHINA_TZ),
            )

            V4IntradayShadowCoordinator.freeze_session(
                coordinator,
                "2026-08-14",
                now=datetime(2026, 8, 14, 14, 56, 20, tzinfo=CHINA_TZ),
            )
            with sqlite3.connect(coordinator.db_path) as connection:
                self.assertEqual(
                    connection.execute(
                        "SELECT status FROM decision_intents"
                    ).fetchone()[0],
                    "DECISION_FROZEN",
                )

            restarted = V5IntradayShadowCoordinator(declaration)
            with patch(
                "market_data.intraday_shadow.discover_latest_provider",
                return_value=provider,
            ):
                status = restarted.run_cycle(
                    poll_seconds=60,
                    now=datetime(2026, 8, 14, 14, 57, 20, tzinfo=CHINA_TZ),
                )

            self.assertEqual(
                status["actions"], ["EXECUTION_BOUND:2026-08-14"]
            )
            self.assertEqual(
                status["state"]["latestDecisionIntent"]["status"],
                "EXECUTION_BOUND",
            )
            self.assertEqual(status["state"]["audit"]["events"], 4)

    def test_v5_blocks_new_intent_until_prior_session_is_finalized(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            provider = _write_provider(root / "provider", BASE_DATES)
            capture = root / "capture"
            for trade_date in ("2026-08-14", "2026-08-17"):
                _capture_pair(provider, capture, trade_date)
            coordinator = V5IntradayShadowCoordinator(
                _write_v5_declaration(root, provider, capture)
            )
            coordinator.freeze_decision_intent(
                "2026-08-14",
                now=datetime(2026, 8, 14, 14, 50, 20, tzinfo=CHINA_TZ),
            )
            coordinator.freeze_session(
                "2026-08-14",
                now=datetime(2026, 8, 14, 14, 56, 20, tzinfo=CHINA_TZ),
            )

            with self.assertRaisesRegex(ValueError, "prior session is not finalized"):
                coordinator.freeze_decision_intent(
                    "2026-08-17",
                    now=datetime(2026, 8, 17, 14, 50, 20, tzinfo=CHINA_TZ),
                )


if __name__ == "__main__":
    unittest.main()
