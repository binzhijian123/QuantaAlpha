import pickle
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd


BACKEND_ROOT = Path(__file__).resolve().parents[1] / "frontend-v2" / "backend"
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from market_data.sync_service import write_minimal_qlib_provider
from market_data.fundamentals import write_enriched_qlib_provider
from quantaalpha.trading.market_data import QlibBinaryDailySource
from quantaalpha.trading.signal import (
    PaperStrategySignalGenerator,
    build_model_signal_source,
    build_topk_dropout_plan,
    validate_topk_dropout_contract,
)


class _FakeBooster:
    def predict(self, features):
        return np.asarray(features)[:, 0]


class _FakeModel:
    def __init__(self):
        self.model = _FakeBooster()


def _execution_contract(
    top_k: int,
    n_drop: int,
    risk_degree: float = 0.95,
    hold_threshold: int = 1,
    version: int | None = None,
):
    return {
        "version": version or (1 if hold_threshold == 1 else 2),
        "type": "topk_dropout",
        "strategyClass": "TopkDropoutStrategy",
        "topK": top_k,
        "nDrop": n_drop,
        "riskDegree": risk_degree,
        "methodBuy": "top",
        "methodSell": "bottom",
        "holdThreshold": hold_threshold,
        "onlyTradable": False,
        "forbidAllTradeAtLimit": True,
    }


class PaperStrategySignalGeneratorTests(unittest.TestCase):
    def test_qlib_execution_bar_carries_adjustment_factor(self):
        rows = [
            {
                "date": trade_date,
                "code": "000001.SZ",
                "open": close,
                "high": close + 0.1,
                "low": close - 0.1,
                "close": close,
                "volume": 1000,
                "amount": close * 1000,
            }
            for trade_date, close in (("2025-01-02", 5.0), ("2025-01-03", 5.1))
        ]
        with tempfile.TemporaryDirectory() as tmp:
            provider = Path(tmp) / "provider"
            write_minimal_qlib_provider(pd.DataFrame(rows), provider)
            np.asarray([0.0, 0.5, 0.6], dtype="<f").tofile(
                provider / "features" / "sz000001" / "factor.day.bin"
            )
            bar = QlibBinaryDailySource(provider).get_bars(
                ["000001.SZ"], "2025-01-03"
            )["000001.SZ"]

        self.assertAlmostEqual(bar.factor, 0.6)
        self.assertAlmostEqual(bar.open, 5.1, places=6)
        self.assertAlmostEqual(bar.previous_close, 5.0, places=6)
        self.assertEqual(bar.listing_sessions, 2)

    def test_frozen_model_generates_ranked_equal_weight_targets(self):
        rows = []
        dates = pd.date_range("2026-07-13", periods=3, freq="D").strftime("%Y-%m-%d")
        for code, closes in {
            "000001.SZ": [10.0, 10.0, 12.0],
            "600000.SH": [10.0, 10.0, 11.0],
            "000002.SZ": [10.0, 10.0, 9.0],
        }.items():
            for trade_date, close in zip(dates, closes):
                rows.append(
                    {
                        "date": trade_date,
                        "code": code,
                        "open": close,
                        "high": close + 0.1,
                        "low": close - 0.1,
                        "close": close,
                        "volume": 1000,
                        "amount": close * 1000,
                    }
                )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            provider = root / "provider"
            snapshot = root / "strategy.pkl"
            write_minimal_qlib_provider(pd.DataFrame(rows), provider)
            with snapshot.open("wb") as file:
                pickle.dump(
                    {
                        "version": 1,
                        "model": _FakeModel(),
                        "featureColumns": ["MOMENTUM"],
                        "factors": [
                            {
                                "name": "MOMENTUM",
                                "expression": "$return",
                                "source": "custom",
                            }
                        ],
                        "backtestMetrics": {"information_ratio": 1.0},
                        "providerPath": str(provider.resolve()),
                        "profitValidation": {"status": "PAPER_CANDIDATE"},
                        "executionContract": _execution_contract(2, 1, 0.8),
                    },
                    file,
                )
            generator = PaperStrategySignalGenerator(snapshot, QlibBinaryDailySource(provider))
            result = generator.generate_targets("2026-07-15", top_k=2, exposure=0.8)

        self.assertEqual(list(result["targets"]), ["000001.SZ", "600000.SH"])
        self.assertEqual(set(result["targets"].values()), {0.4})
        self.assertEqual(result["validScoreCount"], 3)
        self.assertAlmostEqual(result["ranking"][0]["score"], 1.73)
        source = build_model_signal_source(result)
        self.assertEqual(source["executionMode"], "topk_dropout")
        self.assertEqual(source["executionPlan"]["buySymbols"], ["000001.SZ", "600000.SH"])
        self.assertEqual(source["executionContract"]["nDrop"], 1)

    def test_legacy_snapshot_is_visible_but_cannot_generate_signals(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            provider = root / "provider"
            snapshot = root / "strategy.pkl"
            rows = [
                {
                    "date": "2026-07-15",
                    "code": "000001.SZ",
                    "open": 10.0,
                    "high": 10.1,
                    "low": 9.9,
                    "close": 10.0,
                    "volume": 1000,
                    "amount": 10000,
                }
            ]
            write_minimal_qlib_provider(pd.DataFrame(rows), provider)
            with snapshot.open("wb") as file:
                pickle.dump(
                    {
                        "version": 1,
                        "model": _FakeModel(),
                        "featureColumns": ["CLOSE"],
                        "factors": [{"name": "CLOSE", "expression": "$close"}],
                        "providerPath": str(provider.resolve()),
                    },
                    file,
                )

            generator = PaperStrategySignalGenerator(snapshot, QlibBinaryDailySource(provider))
            self.assertFalse(generator.describe()["eligibleForPaper"])
            with self.assertRaisesRegex(ValueError, "未通过收益验证"):
                generator.generate_targets("2026-07-15", top_k=1)

    def test_signal_generator_loads_frozen_point_in_time_fields(self):
        rows = []
        panel_rows = []
        dates = pd.date_range("2026-07-13", periods=3, freq="D").strftime("%Y-%m-%d")
        for code, quality in {
            "000001.SZ": 8.0,
            "600000.SH": 12.0,
            "000002.SZ": 5.0,
        }.items():
            for trade_date in dates:
                rows.append(
                    {
                        "date": trade_date,
                        "code": code,
                        "open": 10.0,
                        "high": 10.1,
                        "low": 9.9,
                        "close": 10.0,
                        "volume": 1000,
                        "amount": 10000,
                    }
                )
                panel_rows.append(
                    {
                        "date": trade_date,
                        "code": code,
                        "pit_q_dt_roe": quality,
                    }
                )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            base = root / "base"
            provider = root / "enriched"
            snapshot = root / "strategy.pkl"
            source_manifest = root / "source.json"
            source_manifest.write_text('{"batchId":"signal-test"}', encoding="utf-8")
            write_minimal_qlib_provider(pd.DataFrame(rows), base)
            write_enriched_qlib_provider(
                base,
                provider,
                pd.DataFrame(panel_rows),
                ("pit_q_dt_roe",),
                batch_id="signal-test",
                source_manifest=source_manifest,
                as_of_audit={"status": "PASS"},
            )
            with snapshot.open("wb") as file:
                pickle.dump(
                    {
                        "version": 1,
                        "model": _FakeModel(),
                        "featureColumns": ["QUALITY"],
                        "factors": [
                            {
                                "name": "QUALITY",
                                "expression": "$pit_q_dt_roe",
                                "source": "custom",
                            }
                        ],
                        "providerPath": str(provider.resolve()),
                        "profitValidation": {"status": "PAPER_CANDIDATE"},
                        "executionContract": _execution_contract(1, 1),
                    },
                    file,
                )
            result = PaperStrategySignalGenerator(
                snapshot, QlibBinaryDailySource(provider)
            ).generate_targets("2026-07-15", top_k=1)

        self.assertEqual(result["targets"], {"600000.SH": 0.95})

    def test_topk_dropout_plan_matches_qlib_top_bottom_selection(self):
        scores = pd.Series(
            {"A": 1.0, "B": 0.9, "C": 0.8, "D": 0.7},
            name="score",
        )
        plan = build_topk_dropout_plan(scores, ["B", "D"], top_k=2, n_drop=1)

        self.assertEqual(plan["keepSymbols"], ["B"])
        self.assertEqual(plan["sellSymbols"], ["D"])
        self.assertEqual(plan["buySymbols"], ["A"])
        self.assertEqual(plan["desiredSymbols"], ["B", "A"])

    def test_topk_dropout_does_not_replace_with_a_worse_new_candidate(self):
        scores = pd.Series({"A": 1.0, "B": 0.9, "C": 0.8}, name="score")
        plan = build_topk_dropout_plan(scores, ["A", "B"], top_k=2, n_drop=1)

        self.assertEqual(plan["keepSymbols"], ["A", "B"])
        self.assertEqual(plan["sellSymbols"], [])
        self.assertEqual(plan["buySymbols"], [])

    def test_topk_dropout_recovers_temporary_position_overflow_without_buying(self):
        scores = pd.Series({"A": 1.0, "B": 0.9, "C": 0.8}, name="score")
        plan = build_topk_dropout_plan(scores, ["A", "B", "C"], top_k=2, n_drop=1)

        self.assertEqual(plan["keepSymbols"], ["A", "B"])
        self.assertEqual(plan["sellSymbols"], ["C"])
        self.assertEqual(plan["buySymbols"], [])
        self.assertEqual(plan["desiredSymbols"], ["A", "B"])

    def test_topk_dropout_holds_young_sell_candidate_but_keeps_qlib_buy(self):
        scores = pd.Series(
            {"A": 1.0, "B": 0.9, "C": 0.8, "D": 0.7},
            name="score",
        )

        plan = build_topk_dropout_plan(
            scores,
            ["B", "D"],
            top_k=2,
            n_drop=1,
            hold_threshold=21,
            holding_sessions={"B": 21, "D": 20},
        )

        self.assertEqual(plan["sellCandidates"], ["D"])
        self.assertEqual(plan["sellSymbols"], [])
        self.assertEqual(plan["holdBlockedSymbols"], ["D"])
        self.assertEqual(plan["keepSymbols"], ["B"])
        self.assertEqual(plan["buySymbols"], ["A"])
        self.assertEqual(plan["desiredSymbols"], ["B", "A"])

    def test_snapshot_execution_parameters_cannot_be_overridden(self):
        rows = []
        for code in ("000001.SZ", "600000.SH"):
            for trade_date in ("2026-07-14", "2026-07-15"):
                rows.append(
                    {
                        "date": trade_date,
                        "code": code,
                        "open": 10,
                        "high": 10.1,
                        "low": 9.9,
                        "close": 10,
                        "volume": 1000,
                        "amount": 10000,
                    }
                )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            provider = root / "provider"
            snapshot = root / "strategy.pkl"
            write_minimal_qlib_provider(pd.DataFrame(rows), provider)
            with snapshot.open("wb") as file:
                pickle.dump(
                    {
                        "version": 1,
                        "model": _FakeModel(),
                        "featureColumns": ["CLOSE"],
                        "factors": [{"name": "CLOSE", "expression": "$close"}],
                        "providerPath": str(provider.resolve()),
                        "profitValidation": {"status": "PAPER_CANDIDATE"},
                        "executionContract": _execution_contract(1, 1, 0.95),
                    },
                    file,
                )
            generator = PaperStrategySignalGenerator(
                snapshot, QlibBinaryDailySource(provider)
            )
            with self.assertRaisesRegex(ValueError, "冻结策略"):
                generator.generate_targets("2026-07-15", top_k=2, exposure=0.95)

    def test_zero_drop_contract_is_rejected_as_unsupported(self):
        with self.assertRaisesRegex(ValueError, "topK/nDrop"):
            validate_topk_dropout_contract(_execution_contract(1, 0))

    def test_version_two_contract_supports_frozen_hold_period(self):
        contract = validate_topk_dropout_contract(
            _execution_contract(50, 1, hold_threshold=21)
        )

        self.assertEqual(contract["version"], 2)
        self.assertEqual(contract["holdThreshold"], 21)

    def test_version_one_contract_cannot_claim_multi_session_hold(self):
        with self.assertRaisesRegex(ValueError, "版本 1"):
            validate_topk_dropout_contract(
                _execution_contract(50, 1, hold_threshold=21, version=1)
            )


if __name__ == "__main__":
    unittest.main()
