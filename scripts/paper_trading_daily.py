#!/usr/bin/env python3
"""Execute the oldest pending paper signal on its next available trading day."""

import json
from pathlib import Path

from quantaalpha.trading.market_data import QlibBinaryDailySource
from quantaalpha.trading.paper import PAPER_BENCHMARK_SYMBOL, PaperTradingEngine
from quantaalpha.trading.signal import (
    PaperStrategySignalGenerator,
    build_model_signal_source,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    engine = PaperTradingEngine(PROJECT_ROOT / "data" / "trading" / "paper" / "paper_trading.sqlite3")
    engine.ensure_account()
    source = QlibBinaryDailySource()
    actions = []
    pending = engine.next_pending_signal()
    if pending is not None:
        account = engine.get_state()["account"]
        after_date = max(filter(None, [pending["signalDate"], account.get("lastTradeDate")]))
        trade_date = source.next_date_after(after_date)
        if trade_date is None:
            print(json.dumps({"status": "waiting", "signalDate": pending["signalDate"]}, ensure_ascii=False))
            return
        bars = source.get_bars(pending["symbols"], trade_date)
        benchmark_bar = source.get_bars([PAPER_BENCHMARK_SYMBOL], trade_date).get(
            PAPER_BENCHMARK_SYMBOL
        )
        if benchmark_bar is None:
            raise ValueError(f"{trade_date} 缺少沪深300基准行情")
        result = engine.execute_pending(
            bars,
            trade_date,
            benchmark_bar=benchmark_bar,
        )
        actions.append({"type": "execution", "latestRun": result["latestRun"]})

    latest_date = source.latest_dates(1)[-1]
    latest_signal = engine.latest_signal()
    if engine.next_pending_signal() is None and (
        latest_signal is None or latest_signal["signalDate"] < latest_date
    ):
        generator = PaperStrategySignalGenerator(
            PROJECT_ROOT / "data" / "trading" / "paper" / "strategy_snapshot.pkl",
            source=source,
        )
        current_symbols = [
            position["symbol"]
            for position in engine.get_state()["positions"]
            if position["quantity"] > 0
        ]
        generated = generator.generate_targets(
            latest_date,
            current_symbols=current_symbols,
        )
        signal = engine.submit_targets(
            latest_date,
            generated["targets"],
            source=build_model_signal_source(generated),
        )
        actions.append({"type": "signal", "signalId": signal["signalId"], "signalDate": latest_date})

    state = engine.get_state(market_calendar=source.calendar)
    print(json.dumps({"status": "completed" if actions else "idle", "actions": actions, "account": state["account"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
