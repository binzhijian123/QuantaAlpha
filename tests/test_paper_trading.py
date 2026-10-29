import hashlib
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from quantaalpha.trading.paper import (
    PAPER_CAUSAL_EXECUTION_MODEL_VERSION,
    DailyBar,
    PaperTradingConfig,
    PaperTradingEngine,
)


def _bar(symbol: str, trade_date: str, open_price: float, close: float | None = None, previous: float | None = None):
    return DailyBar(
        symbol=symbol,
        trade_date=trade_date,
        open=open_price,
        high=max(open_price + 0.2, close or open_price),
        low=min(open_price - 0.2, close or open_price),
        close=close or open_price,
        volume=1_000_000,
        previous_close=previous,
        factor=1.0,
        listing_sessions=100,
    )


def _benchmark(
    trade_date: str,
    close: float = 100.0,
    previous: float = 100.0,
) -> DailyBar:
    return _bar("000300.SH", trade_date, close, close=close, previous=previous)


def _model_source(
    current: list[str],
    keep: list[str],
    sell: list[str],
    buy: list[str],
    top_k: int,
    n_drop: int = 1,
    risk_degree: float = 0.95,
    ref: str = "snapshot-test",
    hold_threshold: int = 1,
    holding_sessions: dict[str, int] | None = None,
    sell_candidates: list[str] | None = None,
    hold_blocked: list[str] | None = None,
) -> dict:
    desired = [*keep, *buy]
    version = 1 if hold_threshold == 1 else 2
    holding_sessions = holding_sessions or {symbol: 1 for symbol in current}
    sell_candidates = sell if sell_candidates is None else sell_candidates
    hold_blocked = [] if hold_blocked is None else hold_blocked
    plan = {
        "currentSymbols": current,
        "keepSymbols": keep,
        "sellSymbols": sell,
        "buySymbols": buy,
        "desiredSymbols": desired,
        "topK": top_k,
        "nDrop": n_drop,
        "riskDegree": risk_degree,
        "currentSymbolsSha256": hashlib.sha256(
            "\n".join(sorted(current)).encode("utf-8")
        ).hexdigest(),
    }
    if version >= 2:
        plan.update(
            {
                "currentHoldingSessions": holding_sessions,
                "sellCandidates": sell_candidates,
                "holdBlockedSymbols": hold_blocked,
                "currentPositionsSha256": hashlib.sha256(
                    "\n".join(
                        f"{symbol}:{holding_sessions[symbol]}"
                        for symbol in sorted(holding_sessions)
                    ).encode("utf-8")
                ).hexdigest(),
            }
        )
    return {
        "type": "model_snapshot",
        "ref": ref,
        "executionMode": "topk_dropout",
        "executionContract": {
            "version": version,
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
        },
        "executionPlan": plan,
    }


class PaperTradingEngineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp.name) / "paper.sqlite3"
        self.engine = PaperTradingEngine(self.db_path)
        self.engine.ensure_account(initial_cash=100_000)

    def tearDown(self):
        self.temp.cleanup()

    def _causal_engine(self) -> PaperTradingEngine:
        engine = PaperTradingEngine(
            Path(self.temp.name) / "causal.sqlite3",
            PaperTradingConfig(
                execution_model_version=PAPER_CAUSAL_EXECUTION_MODEL_VERSION,
                growth_limit_threshold=0.195,
                star_minimum_order_size=200,
                star_order_increment=1,
                require_listing_sessions=True,
            ),
        )
        engine.ensure_account(initial_cash=100_000)
        return engine

    def test_target_weight_executes_next_day_and_persists(self):
        self.engine.submit_targets(
            "2025-01-02",
            {"600000.SH": 0.5},
            source={"type": "manual", "ref": "snapshot-test"},
        )
        result = self.engine.execute_pending(
            {"600000.SH": _bar("600000.SH", "2025-01-03", 10.0, 10.1, 9.8)},
            "2025-01-03",
        )

        self.assertEqual(result["positions"][0]["quantity"], 5000)
        self.assertEqual(result["positions"][0]["availableQuantity"], 0)
        self.assertEqual(result["positions"][0]["holdingSessions"], 1)
        self.assertEqual(result["fills"][0]["side"], "buy")
        self.assertGreater(result["account"]["totalValue"], 100_000)

        reopened = PaperTradingEngine(self.db_path).get_state()
        self.assertEqual(reopened["positions"][0]["quantity"], 5000)
        self.assertEqual(reopened["positions"][0]["holdingSessions"], 1)
        self.assertEqual(reopened["account"]["lastTradeDate"], "2025-01-03")
        self.assertEqual(reopened["latestSignal"]["source"]["ref"], "snapshot-test")

    def test_adjusted_price_factor_enforces_real_board_lot_and_display(self):
        self.engine.submit_targets("2025-01-02", {"600000.SH": 0.5})
        adjusted_bar = DailyBar(
            symbol="600000.SH",
            trade_date="2025-01-03",
            open=5.0,
            high=5.2,
            low=4.8,
            close=5.05,
            volume=1_000_000,
            previous_close=4.9,
            factor=0.5,
        )

        result = self.engine.execute_pending(
            {"600000.SH": adjusted_bar},
            "2025-01-03",
        )

        position = result["positions"][0]
        self.assertEqual(position["quantity"], 5000)
        self.assertAlmostEqual(position["lastPrice"], 10.1)
        self.assertEqual(result["orders"][0]["quantity"], 5000)
        self.assertEqual(result["fills"][0]["quantity"], 5000)
        self.assertAlmostEqual(result["fills"][0]["price"], 10.01)
        self.assertAlmostEqual(result["fills"][0]["gross_amount"], 50_050.0)

        with self.engine._connect() as connection:
            stored = connection.execute(
                "SELECT quantity, factor FROM positions WHERE symbol = '600000.SH'"
            ).fetchone()
        self.assertAlmostEqual(stored["quantity"], 10_000.0)
        self.assertAlmostEqual(stored["factor"], 0.5)

    def test_full_target_rebalance_sells_existing_position(self):
        self.engine.submit_targets("2025-01-02", {"600000.SH": 0.5})
        self.engine.execute_pending(
            {"600000.SH": _bar("600000.SH", "2025-01-03", 10.0, 10.1, 9.8)},
            "2025-01-03",
        )
        self.engine.submit_targets("2025-01-03", {})
        result = self.engine.execute_pending(
            {"600000.SH": _bar("600000.SH", "2025-01-06", 11.0, 11.0, 10.1)},
            "2025-01-06",
        )

        self.assertEqual(result["positions"], [])
        self.assertEqual(result["fills"][0]["side"], "sell")
        self.assertGreater(result["account"]["cash"], 100_000)

    def test_one_price_limit_up_rejects_buy(self):
        self.engine.submit_targets("2025-01-02", {"600000.SH": 1.0})
        locked = DailyBar(
            symbol="600000.SH",
            trade_date="2025-01-03",
            open=11.0,
            high=11.0,
            low=11.0,
            close=11.0,
            volume=1_000_000,
            previous_close=10.0,
        )

        result = self.engine.execute_pending({"600000.SH": locked}, "2025-01-03")

        self.assertEqual(result["positions"], [])
        self.assertEqual(result["orders"][0]["status"], "rejected")
        self.assertIn("涨停", result["orders"][0]["reason"])

    def test_suspended_bar_leaves_rejected_order(self):
        self.engine.submit_targets("2025-01-02", {"600000.SH": 0.5})
        suspended = DailyBar(
            symbol="600000.SH",
            trade_date="2025-01-03",
            open=10.0,
            high=10.0,
            low=10.0,
            close=10.0,
            volume=0,
            previous_close=10.0,
        )

        result = self.engine.execute_pending({"600000.SH": suspended}, "2025-01-03")

        self.assertEqual(result["orders"][0]["status"], "rejected")
        self.assertIn("停牌", result["orders"][0]["reason"])

    def test_account_can_only_run_once_per_trade_date(self):
        self.engine.submit_targets("2025-01-02", {"600000.SH": 0.2})
        bars = {"600000.SH": _bar("600000.SH", "2025-01-03", 10.0, previous=9.8)}
        self.engine.execute_pending(bars, "2025-01-03")
        self.engine.submit_targets("2025-01-02", {"600000.SH": 0.3})

        with self.assertRaisesRegex(ValueError, "already ran"):
            self.engine.execute_pending(bars, "2025-01-03")

    def test_cannot_trade_on_signal_date(self):
        self.engine.submit_targets("2025-01-02", {"600000.SH": 0.2})
        with self.assertRaisesRegex(ValueError, "after signal"):
            self.engine.execute_pending(
                {"600000.SH": _bar("600000.SH", "2025-01-02", 10.0)},
                "2025-01-02",
            )

    def test_target_below_one_lot_is_recorded_as_rejected(self):
        self.engine.submit_targets("2025-01-02", {"600000.SH": 0.001})
        result = self.engine.execute_pending(
            {"600000.SH": _bar("600000.SH", "2025-01-03", 100.0)},
            "2025-01-03",
        )

        self.assertEqual(result["positions"], [])
        self.assertEqual(result["orders"][0]["status"], "rejected")
        self.assertIn("不足一手", result["orders"][0]["reason"])

    def test_topk_dropout_initial_buy_uses_post_sell_cash_risk_budget(self):
        source = _model_source(
            current=[],
            keep=[],
            sell=[],
            buy=["600000.SH", "000001.SZ"],
            top_k=2,
        )
        self.engine.submit_targets(
            "2025-01-02",
            {"600000.SH": 0.475, "000001.SZ": 0.475},
            source=source,
        )

        result = self.engine.execute_pending(
            {
                "600000.SH": _bar("600000.SH", "2025-01-03", 10.0),
                "000001.SZ": _bar("000001.SZ", "2025-01-03", 10.0),
            },
            "2025-01-03",
            benchmark_bar=_benchmark("2025-01-03", close=101.0, previous=100.0),
        )

        self.assertEqual(
            {position["symbol"]: position["quantity"] for position in result["positions"]},
            {"000001.SZ": 4700, "600000.SH": 4700},
        )
        self.assertEqual(len(result["fills"]), 2)
        validation = self.engine.get_state(
            market_calendar=["2025-01-03"]
        )["paperValidation"]
        self.assertEqual(validation["observationSessions"], 1)
        self.assertAlmostEqual(validation["benchmarkReturn"], 0.01)
        self.assertAlmostEqual(
            validation["afterCostExcessReturn"],
            (1 + validation["strategyReturn"]) / 1.01 - 1,
        )
        self.assertEqual(validation["stage"], "ENGINEERING")
        self.assertFalse(validation["automaticPromotionAllowed"])

    def test_topk_dropout_hold_period_matches_qlib_and_persists(self):
        self.engine.submit_targets(
            "2025-01-02",
            {"600000.SH": 0.475, "000001.SZ": 0.475},
            source=_model_source(
                current=[],
                keep=[],
                sell=[],
                buy=["600000.SH", "000001.SZ"],
                top_k=2,
                hold_threshold=2,
            ),
        )
        first = self.engine.execute_pending(
            {
                "600000.SH": _bar("600000.SH", "2025-01-03", 10.0),
                "000001.SZ": _bar("000001.SZ", "2025-01-03", 10.0),
            },
            "2025-01-03",
            benchmark_bar=_benchmark("2025-01-03"),
        )
        first_sessions = {
            position["symbol"]: position["holdingSessions"]
            for position in first["positions"]
        }
        self.assertEqual(first_sessions, {"000001.SZ": 1, "600000.SH": 1})

        self.engine.submit_targets(
            "2025-01-03",
            {"600000.SH": 0.475, "000002.SZ": 0.475},
            source=_model_source(
                current=["000001.SZ", "600000.SH"],
                keep=["600000.SH"],
                sell=[],
                buy=["000002.SZ"],
                top_k=2,
                hold_threshold=2,
                holding_sessions=first_sessions,
                sell_candidates=["000001.SZ"],
                hold_blocked=["000001.SZ"],
            ),
        )
        second = self.engine.execute_pending(
            {
                "000001.SZ": _bar("000001.SZ", "2025-01-06", 10.0),
                "000002.SZ": _bar("000002.SZ", "2025-01-06", 10.0),
                "600000.SH": _bar("600000.SH", "2025-01-06", 10.0),
            },
            "2025-01-06",
            benchmark_bar=_benchmark("2025-01-06", close=101.0),
        )
        second_sessions = {
            position["symbol"]: position["holdingSessions"]
            for position in second["positions"]
        }
        self.assertEqual(
            second_sessions,
            {"000001.SZ": 2, "000002.SZ": 1, "600000.SH": 2},
        )
        self.assertEqual(len(second["positions"]), 3)
        reopened_sessions = {
            position["symbol"]: position["holdingSessions"]
            for position in PaperTradingEngine(self.db_path).get_state()["positions"]
        }
        self.assertEqual(reopened_sessions, second_sessions)

        self.engine.submit_targets(
            "2025-01-06",
            {"000002.SZ": 0.475, "600000.SH": 0.475},
            source=_model_source(
                current=["000001.SZ", "000002.SZ", "600000.SH"],
                keep=["000002.SZ", "600000.SH"],
                sell=["000001.SZ"],
                buy=[],
                top_k=2,
                hold_threshold=2,
                holding_sessions=second_sessions,
                sell_candidates=["000001.SZ"],
            ),
        )
        third = self.engine.execute_pending(
            {
                "000001.SZ": _bar("000001.SZ", "2025-01-07", 10.0),
                "000002.SZ": _bar("000002.SZ", "2025-01-07", 10.0),
                "600000.SH": _bar("600000.SH", "2025-01-07", 10.0),
            },
            "2025-01-07",
            benchmark_bar=_benchmark("2025-01-07", close=102.0, previous=101.0),
        )

        self.assertEqual(
            [position["symbol"] for position in third["positions"]],
            ["000002.SZ", "600000.SH"],
        )
        self.assertEqual(third["fills"][0]["symbol"], "000001.SZ")
        self.assertEqual(third["fills"][0]["side"], "sell")

    def test_version_two_plan_rejects_changed_holding_sessions_before_run(self):
        self.engine.submit_targets(
            "2025-01-02",
            {"600000.SH": 0.95},
            source=_model_source(
                current=[],
                keep=[],
                sell=[],
                buy=["600000.SH"],
                top_k=1,
                hold_threshold=2,
            ),
        )
        self.engine.execute_pending(
            {"600000.SH": _bar("600000.SH", "2025-01-03", 10.0)},
            "2025-01-03",
            benchmark_bar=_benchmark("2025-01-03"),
        )
        source = _model_source(
            current=["600000.SH"],
            keep=["600000.SH"],
            sell=[],
            buy=[],
            top_k=1,
            hold_threshold=2,
            holding_sessions={"600000.SH": 1},
        )
        source["executionPlan"]["currentHoldingSessions"]["600000.SH"] = 2
        self.engine.submit_targets(
            "2025-01-03",
            {"600000.SH": 0.95},
            source=source,
        )

        with self.assertRaisesRegex(ValueError, "持有交易日已变化"):
            self.engine.execute_pending(
                {"600000.SH": _bar("600000.SH", "2025-01-06", 10.0)},
                "2025-01-06",
                benchmark_bar=_benchmark("2025-01-06"),
            )

        self.assertIsNotNone(self.engine.next_pending_signal())

    def test_topk_dropout_missing_factor_fails_before_run(self):
        self.engine.submit_targets(
            "2025-01-02",
            {"600000.SH": 0.95},
            source=_model_source(
                current=[],
                keep=[],
                sell=[],
                buy=["600000.SH"],
                top_k=1,
            ),
        )
        bar_without_factor = DailyBar(
            symbol="600000.SH",
            trade_date="2025-01-03",
            open=10.0,
            high=10.2,
            low=9.8,
            close=10.1,
            volume=1_000_000,
            previous_close=9.9,
        )

        with self.assertRaisesRegex(ValueError, "缺少 Qlib factor"):
            self.engine.execute_pending(
                {"600000.SH": bar_without_factor},
                "2025-01-03",
                benchmark_bar=_benchmark("2025-01-03"),
            )

        self.assertIsNotNone(self.engine.next_pending_signal())
        self.assertEqual(self.engine.get_state()["orders"], [])

    def test_topk_dropout_replaces_only_planned_bottom_position(self):
        initial_source = _model_source(
            current=[],
            keep=[],
            sell=[],
            buy=["600000.SH", "000001.SZ"],
            top_k=2,
        )
        self.engine.submit_targets(
            "2025-01-02",
            {"600000.SH": 0.475, "000001.SZ": 0.475},
            source=initial_source,
        )
        first = self.engine.execute_pending(
            {
                "600000.SH": _bar("600000.SH", "2025-01-03", 10.0),
                "000001.SZ": _bar("000001.SZ", "2025-01-03", 10.0),
            },
            "2025-01-03",
            benchmark_bar=_benchmark("2025-01-03"),
        )
        retained_quantity = next(
            position["quantity"] for position in first["positions"]
            if position["symbol"] == "600000.SH"
        )
        source = _model_source(
            current=["000001.SZ", "600000.SH"],
            keep=["600000.SH"],
            sell=["000001.SZ"],
            buy=["000002.SZ"],
            top_k=2,
        )
        self.engine.submit_targets(
            "2025-01-03",
            {"600000.SH": 0.475, "000002.SZ": 0.475},
            source=source,
        )

        result = self.engine.execute_pending(
            {
                "600000.SH": _bar("600000.SH", "2025-01-06", 10.0),
                "000001.SZ": _bar("000001.SZ", "2025-01-06", 10.0),
                "000002.SZ": _bar("000002.SZ", "2025-01-06", 10.0),
            },
            "2025-01-06",
            benchmark_bar=_benchmark("2025-01-06", close=101.0, previous=100.0),
        )

        positions = {position["symbol"]: position for position in result["positions"]}
        self.assertEqual(positions["600000.SH"]["quantity"], retained_quantity)
        self.assertNotIn("000001.SZ", positions)
        self.assertIn("000002.SZ", positions)
        latest_fills = result["fills"][:2]
        self.assertEqual({(fill["symbol"], fill["side"]) for fill in latest_fills}, {
            ("000001.SZ", "sell"),
            ("000002.SZ", "buy"),
        })

    def test_topk_dropout_keeps_positions_without_rebalancing(self):
        initial_source = _model_source(
            current=[],
            keep=[],
            sell=[],
            buy=["600000.SH", "000001.SZ"],
            top_k=2,
        )
        self.engine.submit_targets(
            "2025-01-02",
            {"600000.SH": 0.475, "000001.SZ": 0.475},
            source=initial_source,
        )
        first = self.engine.execute_pending(
            {
                "600000.SH": _bar("600000.SH", "2025-01-03", 10.0),
                "000001.SZ": _bar("000001.SZ", "2025-01-03", 10.0),
            },
            "2025-01-03",
            benchmark_bar=_benchmark("2025-01-03"),
        )
        original = {
            position["symbol"]: position["quantity"] for position in first["positions"]
        }
        source = _model_source(
            current=["000001.SZ", "600000.SH"],
            keep=["000001.SZ", "600000.SH"],
            sell=[],
            buy=[],
            top_k=2,
        )
        self.engine.submit_targets(
            "2025-01-03",
            {"600000.SH": 0.475, "000001.SZ": 0.475},
            source=source,
        )

        result = self.engine.execute_pending(
            {
                "600000.SH": _bar("600000.SH", "2025-01-06", 11.0),
                "000001.SZ": _bar("000001.SZ", "2025-01-06", 9.0),
            },
            "2025-01-06",
            benchmark_bar=_benchmark("2025-01-06", close=102.0, previous=100.0),
        )

        self.assertEqual(
            {position["symbol"]: position["quantity"] for position in result["positions"]},
            original,
        )
        self.assertEqual(len(result["orders"]), 2)
        self.assertEqual(len(result["fills"]), 2)
        validation = self.engine.get_state(
            market_calendar=["2025-01-03", "2025-01-06"]
        )["paperValidation"]
        self.assertEqual(validation["observationSessions"], 2)
        self.assertAlmostEqual(validation["benchmarkReturn"], 0.02)
        self.assertTrue(validation["dataComplete"])

    def test_topk_dropout_rejects_stale_position_snapshot_before_run(self):
        self.engine.submit_targets(
            "2025-01-02",
            {"600000.SH": 0.95},
            source=_model_source(
                current=[],
                keep=[],
                sell=[],
                buy=["600000.SH"],
                top_k=1,
            ),
        )
        self.engine.execute_pending(
            {"600000.SH": _bar("600000.SH", "2025-01-03", 10.0)},
            "2025-01-03",
            benchmark_bar=_benchmark("2025-01-03"),
        )
        source = _model_source(
            current=[],
            keep=[],
            sell=[],
            buy=["000001.SZ"],
            top_k=1,
        )
        signal = self.engine.submit_targets(
            "2025-01-03",
            {"000001.SZ": 0.95},
            source=source,
        )

        with self.assertRaisesRegex(ValueError, "持仓已变化"):
            self.engine.execute_pending(
                {
                    "600000.SH": _bar("600000.SH", "2025-01-06", 10.0),
                    "000001.SZ": _bar("000001.SZ", "2025-01-06", 10.0),
                },
                "2025-01-06",
                benchmark_bar=_benchmark("2025-01-06", close=101.0),
            )

        pending = self.engine.next_pending_signal()
        self.assertEqual(pending["signalId"], signal["signalId"])
        self.assertEqual(len(self.engine.get_state()["orders"]), 1)

    def test_legacy_topk_dropout_forbids_all_trades_at_daily_limit(self):
        self.engine.submit_targets(
            "2025-01-02",
            {"600000.SH": 0.95},
            source=_model_source(
                current=[],
                keep=[],
                sell=[],
                buy=["600000.SH"],
                top_k=1,
            ),
        )
        self.engine.execute_pending(
            {"600000.SH": _bar("600000.SH", "2025-01-03", 10.0)},
            "2025-01-03",
            benchmark_bar=_benchmark("2025-01-03"),
        )
        source = _model_source(
            current=["600000.SH"],
            keep=[],
            sell=["600000.SH"],
            buy=["000001.SZ"],
            top_k=1,
        )
        self.engine.submit_targets(
            "2025-01-03",
            {"000001.SZ": 0.95},
            source=source,
        )

        result = self.engine.execute_pending(
            {
                "600000.SH": _bar(
                    "600000.SH", "2025-01-06", 10.5, close=11.0, previous=10.0
                ),
                "000001.SZ": _bar(
                    "000001.SZ", "2025-01-06", 10.0, previous=10.0
                ),
            },
            "2025-01-06",
            benchmark_bar=_benchmark("2025-01-06", close=101.0),
        )

        sell_order = next(
            order for order in result["orders"]
            if order["symbol"] == "600000.SH" and order["side"] == "sell"
        )
        self.assertEqual(sell_order["status"], "rejected")
        self.assertIn("禁止全部交易", sell_order["reason"])
        self.assertEqual(
            {position["symbol"] for position in result["positions"]},
            {"600000.SH", "000001.SZ"},
        )

    def test_causal_open_model_does_not_use_same_session_close_limit(self):
        engine = self._causal_engine()
        engine.submit_targets(
            "2025-01-02",
            {"600000.SH": 0.95},
            source=_model_source(
                current=[], keep=[], sell=[], buy=["600000.SH"], top_k=1
            ),
        )
        bar = DailyBar(
            symbol="600000.SH",
            trade_date="2025-01-03",
            open=10.0,
            high=11.0,
            low=9.8,
            close=11.0,
            volume=1_000_000,
            previous_close=10.0,
            factor=1.0,
            listing_sessions=100,
        )

        result = engine.execute_pending(
            {"600000.SH": bar},
            "2025-01-03",
            benchmark_bar=_benchmark("2025-01-03"),
        )

        self.assertEqual(result["orders"][0]["status"], "filled")
        self.assertEqual(result["positions"][0]["symbol"], "600000.SH")

    def test_causal_open_limit_threshold_depends_on_board(self):
        engine = self._causal_engine()
        main_limit = _bar("600000.SH", "2025-01-03", 10.96, previous=10.0)
        growth_ten_percent = _bar(
            "300001.SZ", "2025-01-03", 11.0, previous=10.0
        )
        star_ten_percent = _bar(
            "688001.SH", "2025-01-03", 11.0, previous=10.0
        )
        growth_limit = _bar(
            "300001.SZ", "2025-01-03", 11.96, previous=10.0
        )
        star_limit = _bar(
            "688001.SH", "2025-01-03", 11.96, previous=10.0
        )

        self.assertIn("禁止全部交易", engine._topk_dropout_blocked_reason(main_limit))
        self.assertIsNone(engine._topk_dropout_blocked_reason(growth_ten_percent))
        self.assertIsNone(engine._topk_dropout_blocked_reason(star_ten_percent))
        self.assertIn("禁止全部交易", engine._topk_dropout_blocked_reason(growth_limit))
        self.assertIn("禁止全部交易", engine._topk_dropout_blocked_reason(star_limit))

    def test_causal_open_model_rejects_unsupported_listing_window(self):
        engine = self._causal_engine()
        new_listing = DailyBar(
            **{
                **_bar("600000.SH", "2025-01-03", 10.0, previous=9.8).__dict__,
                "listing_sessions": 5,
            }
        )
        unknown_listing = DailyBar(
            **{
                **_bar("600000.SH", "2025-01-03", 10.0, previous=9.8).__dict__,
                "listing_sessions": None,
            }
        )

        self.assertIn("前五个交易日", engine._topk_dropout_blocked_reason(new_listing))
        self.assertIn("日龄未知", engine._topk_dropout_blocked_reason(unknown_listing))

    def test_causal_fill_price_is_independent_of_later_daily_range(self):
        engine = self._causal_engine()
        narrow = DailyBar(
            "600000.SH", "2025-01-03", 10.0, 10.0, 10.0, 10.0,
            1_000_000, 9.9, 1.0, 100,
        )
        wide = DailyBar(
            "600000.SH", "2025-01-03", 10.0, 20.0, 1.0, 18.0,
            2_000_000, 9.9, 1.0, 100,
        )

        self.assertAlmostEqual(engine._fill_price(narrow, "buy"), 10.01)
        self.assertEqual(
            engine._fill_price(narrow, "buy"),
            engine._fill_price(wide, "buy"),
        )
        self.assertEqual(
            engine._fill_price(narrow, "sell"),
            engine._fill_price(wide, "sell"),
        )

    def test_causal_star_order_uses_200_share_minimum_and_one_share_increment(self):
        engine = self._causal_engine()
        engine.submit_targets(
            "2025-01-02",
            {"688001.SH": 0.95},
            source=_model_source(
                current=[], keep=[], sell=[], buy=["688001.SH"], top_k=1
            ),
        )

        result = engine.execute_pending(
            {"688001.SH": _bar("688001.SH", "2025-01-03", 100.0, previous=100.0)},
            "2025-01-03",
            benchmark_bar=_benchmark("2025-01-03"),
        )

        quantity = result["positions"][0]["quantity"]
        self.assertGreaterEqual(quantity, 200)
        self.assertEqual(quantity, 950)
        self.assertNotEqual(quantity % 100, 0)

    def test_model_execution_without_benchmark_fails_before_run(self):
        self.engine.submit_targets(
            "2025-01-02",
            {"600000.SH": 0.95},
            source=_model_source(
                current=[], keep=[], sell=[], buy=["600000.SH"], top_k=1
            ),
        )

        with self.assertRaisesRegex(ValueError, "缺少沪深300基准"):
            self.engine.execute_pending(
                {"600000.SH": _bar("600000.SH", "2025-01-03", 10.0)},
                "2025-01-03",
            )

        state = self.engine.get_state(market_calendar=["2025-01-03"])
        self.assertIsNotNone(state["pendingSignal"])
        self.assertEqual(state["orders"], [])
        self.assertEqual(state["equityCurve"], [])

    def test_forward_metric_milestones_do_not_auto_promote(self):
        calendar: list[str] = []
        previous_trade_date = "2024-12-31"
        for index in range(120):
            trade_date = (date(2025, 1, 2) + timedelta(days=index)).isoformat()
            calendar.append(trade_date)
            if index == 0:
                source = _model_source(
                    current=[],
                    keep=[],
                    sell=[],
                    buy=["600000.SH"],
                    top_k=1,
                )
            else:
                source = _model_source(
                    current=["600000.SH"],
                    keep=["600000.SH"],
                    sell=[],
                    buy=[],
                    top_k=1,
                )
            self.engine.submit_targets(
                previous_trade_date,
                {"600000.SH": 0.95},
                source=source,
            )
            self.engine.execute_pending(
                {"600000.SH": _bar("600000.SH", trade_date, 10.0)},
                trade_date,
                benchmark_bar=_benchmark(
                    trade_date,
                    close=100.0 + index,
                    previous=99.0 + index,
                ),
            )
            previous_trade_date = trade_date
            if index == 19:
                at_20 = self.engine.get_state(
                    market_calendar=calendar
                )["paperValidation"]
            if index == 59:
                at_60 = self.engine.get_state(
                    market_calendar=calendar
                )["paperValidation"]

        at_120 = self.engine.get_state(
            market_calendar=calendar
        )["paperValidation"]
        self.assertTrue(at_20["engineeringGateReached"])
        self.assertEqual(at_20["stage"], "FORWARD_OBSERVATION")
        self.assertEqual(at_60["stage"], "EARLY_METRIC_FAIL")
        self.assertFalse(at_60["earlyMetricGatePassed"])
        self.assertEqual(at_120["stage"], "STRONG_METRIC_FAIL")
        self.assertFalse(at_120["strongMetricGatePassed"])
        self.assertFalse(at_120["automaticPromotionAllowed"])

    def test_calendar_gap_invalidates_forward_metrics(self):
        self.engine.submit_targets(
            "2025-01-02",
            {"600000.SH": 0.95},
            source=_model_source(
                current=[], keep=[], sell=[], buy=["600000.SH"], top_k=1
            ),
        )
        self.engine.execute_pending(
            {"600000.SH": _bar("600000.SH", "2025-01-03", 10.0)},
            "2025-01-03",
            benchmark_bar=_benchmark("2025-01-03"),
        )
        self.engine.submit_targets(
            "2025-01-03",
            {"600000.SH": 0.95},
            source=_model_source(
                current=["600000.SH"],
                keep=["600000.SH"],
                sell=[],
                buy=[],
                top_k=1,
            ),
        )
        self.engine.execute_pending(
            {"600000.SH": _bar("600000.SH", "2025-01-06", 10.0)},
            "2025-01-06",
            benchmark_bar=_benchmark("2025-01-06", close=101.0),
        )

        validation = self.engine.get_state(
            market_calendar=["2025-01-03", "2025-01-04", "2025-01-06"]
        )["paperValidation"]
        self.assertFalse(validation["calendarComplete"])
        self.assertEqual(validation["missingMarketSessions"], 1)
        self.assertFalse(validation["dataComplete"])
        self.assertEqual(validation["stage"], "INVALID")

    def test_drawdown_stop_is_reported_without_auto_promotion(self):
        self.engine.submit_targets(
            "2025-01-02",
            {"600000.SH": 0.95},
            source=_model_source(
                current=[], keep=[], sell=[], buy=["600000.SH"], top_k=1
            ),
        )
        self.engine.execute_pending(
            {"600000.SH": _bar("600000.SH", "2025-01-03", 10.0)},
            "2025-01-03",
            benchmark_bar=_benchmark("2025-01-03"),
        )
        self.engine.submit_targets(
            "2025-01-03",
            {"600000.SH": 0.95},
            source=_model_source(
                current=["600000.SH"],
                keep=["600000.SH"],
                sell=[],
                buy=[],
                top_k=1,
            ),
        )
        self.engine.execute_pending(
            {
                "600000.SH": _bar(
                    "600000.SH", "2025-01-06", 10.0, close=8.0
                )
            },
            "2025-01-06",
            benchmark_bar=_benchmark("2025-01-06"),
        )

        validation = self.engine.get_state(
            market_calendar=["2025-01-03", "2025-01-06"]
        )["paperValidation"]
        self.assertLessEqual(validation["maxDrawdown"], -0.15)
        self.assertTrue(validation["drawdownWarningTriggered"])
        self.assertTrue(validation["drawdownStopTriggered"])
        self.assertEqual(validation["stage"], "STOPPED")
        self.assertFalse(validation["automaticPromotionAllowed"])

    def test_forward_account_rejects_snapshot_or_manual_lineage_changes(self):
        self.engine.submit_targets(
            "2025-01-02",
            {"600000.SH": 0.95},
            source=_model_source(
                current=[],
                keep=[],
                sell=[],
                buy=["600000.SH"],
                top_k=1,
                ref="snapshot-a",
            ),
        )
        self.engine.execute_pending(
            {"600000.SH": _bar("600000.SH", "2025-01-03", 10.0)},
            "2025-01-03",
            benchmark_bar=_benchmark("2025-01-03"),
        )

        with self.assertRaisesRegex(ValueError, "策略快照已变化"):
            self.engine.submit_targets(
                "2025-01-03",
                {"600000.SH": 0.95},
                source=_model_source(
                    current=["600000.SH"],
                    keep=["600000.SH"],
                    sell=[],
                    buy=[],
                    top_k=1,
                    ref="snapshot-b",
                ),
            )
        with self.assertRaisesRegex(ValueError, "模型前向观察"):
            self.engine.submit_targets("2025-01-03", {"600000.SH": 0.5})

    def test_only_one_pending_signal_is_allowed_per_account(self):
        self.engine.submit_targets("2025-01-02", {"600000.SH": 0.5})

        with self.assertRaisesRegex(ValueError, "pending signal"):
            self.engine.submit_targets("2025-01-03", {"000001.SZ": 0.5})


if __name__ == "__main__":
    unittest.main()
