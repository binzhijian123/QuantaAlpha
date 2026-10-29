import unittest

import pandas as pd

from quantaalpha.backtest.runner import (
    BacktestRunner,
    cross_sectional_rank_normalize,
    filter_market_frame_by_selector,
    normalize_market_index,
    validate_dataset_segment_boundaries,
)


class FakeDataset:
    def __init__(self, frames):
        self.frames = frames

    def prepare(self, segment, col_set="label"):
        return self.frames[segment]


class FakePosition:
    def __init__(self, symbols):
        self.symbols = list(symbols)

    def get_stock_list(self):
        return self.symbols


class NormalizeMarketIndexTests(unittest.TestCase):
    def test_normalizes_unnamed_datetime_instrument_index(self):
        index = pd.MultiIndex.from_arrays(
            [pd.to_datetime(["2025-01-02", "2025-01-03"]), ["SH600000", "SH600000"]]
        )
        frame = pd.DataFrame({"factor": [1.0, 2.0]}, index=index)

        result = normalize_market_index(frame, "factor")

        self.assertEqual(result.index.names, ["datetime", "instrument"])
        self.assertEqual(result.index[0], (pd.Timestamp("2025-01-02"), "SH600000"))

    def test_swaps_qlib_instrument_datetime_index(self):
        index = pd.MultiIndex.from_arrays(
            [["SH600000", "SZ000001"], pd.to_datetime(["2025-01-02", "2025-01-02"])],
            names=["instrument", "datetime"],
        )
        frame = pd.DataFrame({"factor": [1.0, 2.0]}, index=index)

        result = normalize_market_index(frame, "qlib")

        self.assertEqual(result.index.names, ["datetime", "instrument"])
        self.assertEqual(set(result.index.get_level_values("instrument")), {"SH600000", "SZ000001"})

    def test_rejects_duplicate_market_rows(self):
        index = pd.MultiIndex.from_tuples(
            [("2025-01-02", "SH600000"), ("2025-01-02", "SH600000")]
        )
        frame = pd.DataFrame({"factor": [1.0, 2.0]}, index=index)

        with self.assertRaisesRegex(ValueError, "duplicate"):
            normalize_market_index(frame, "factor")

    def test_filters_yaml_list_date_range(self):
        index = pd.MultiIndex.from_product(
            [
                pd.to_datetime(["2020-12-31", "2021-01-04", "2022-01-04"]),
                ["SH600000"],
            ],
            names=["datetime", "instrument"],
        )
        frame = pd.DataFrame({"factor": [1.0, 2.0, 3.0]}, index=index)

        result = filter_market_frame_by_selector(
            frame, ["2021-01-01", "2021-12-31"]
        )

        self.assertEqual(list(result["factor"]), [2.0])
        self.assertEqual(
            list(result.index.get_level_values("datetime")),
            [pd.Timestamp("2021-01-04")],
        )

    def test_rejects_dataset_segment_leakage(self):
        index = pd.MultiIndex.from_product(
            [pd.to_datetime(["2020-12-31", "2021-01-04"]), ["SH600000"]],
            names=["datetime", "instrument"],
        )
        leaked = pd.DataFrame({"LABEL0": [0.0, 0.0]}, index=index)
        dataset = FakeDataset({"train": leaked, "valid": leaked})

        with self.assertRaisesRegex(ValueError, "outside configured range"):
            validate_dataset_segment_boundaries(
                dataset,
                {
                    "train": ["2020-01-01", "2020-12-31"],
                    "valid": ["2021-01-01", "2021-12-31"],
                },
            )

    def test_precomputed_rank_normalization_matches_qlib_scale(self):
        index = pd.MultiIndex.from_product(
            [pd.to_datetime(["2021-01-04"]), ["A", "B"]],
            names=["datetime", "instrument"],
        )
        frame = pd.DataFrame({"factor": [10.0, 20.0]}, index=index)

        result = cross_sectional_rank_normalize(frame, ["factor"], "datetime")

        self.assertAlmostEqual(result.loc[(pd.Timestamp("2021-01-04"), "A"), "factor"], 0.0)
        self.assertAlmostEqual(result.loc[(pd.Timestamp("2021-01-04"), "B"), "factor"], 1.73)

    def test_paper_execution_contract_freezes_topk_dropout_parameters(self):
        runner = BacktestRunner.__new__(BacktestRunner)
        runner.config = {
            "backtest": {
                "strategy": {
                    "class": "TopkDropoutStrategy",
                    "kwargs": {
                        "topk": 50,
                        "n_drop": 1,
                        "risk_degree": 0.95,
                    },
                }
            }
        }

        contract = runner._paper_execution_contract()

        self.assertEqual(contract["topK"], 50)
        self.assertEqual(contract["nDrop"], 1)
        self.assertEqual(contract["riskDegree"], 0.95)
        self.assertEqual(contract["methodBuy"], "top")
        self.assertEqual(contract["methodSell"], "bottom")
        self.assertEqual(contract["version"], 1)
        self.assertEqual(contract["holdThreshold"], 1)

    def test_paper_execution_contract_rejects_nondeterministic_methods(self):
        runner = BacktestRunner.__new__(BacktestRunner)
        runner.config = {
            "backtest": {
                "strategy": {
                    "class": "TopkDropoutStrategy",
                    "kwargs": {"topk": 50, "n_drop": 1, "method_buy": "random"},
                }
            }
        }

        with self.assertRaisesRegex(ValueError, "deterministic"):
            runner._paper_execution_contract()

    def test_paper_execution_contract_supports_multi_session_hold_period(self):
        runner = BacktestRunner.__new__(BacktestRunner)
        runner.config = {
            "backtest": {
                "strategy": {
                    "class": "TopkDropoutStrategy",
                    "kwargs": {"topk": 50, "n_drop": 1, "hold_thresh": 21},
                }
            }
        }

        contract = runner._paper_execution_contract()

        self.assertEqual(contract["version"], 2)
        self.assertEqual(contract["holdThreshold"], 21)

    def test_paper_execution_contract_rejects_nonpositive_hold_period(self):
        runner = BacktestRunner.__new__(BacktestRunner)
        runner.config = {
            "backtest": {
                "strategy": {
                    "class": "TopkDropoutStrategy",
                    "kwargs": {"topk": 50, "n_drop": 1, "hold_thresh": 0},
                }
            }
        }

        with self.assertRaisesRegex(ValueError, "positive"):
            runner._paper_execution_contract()

    def test_qlib_strategy_spec_preserves_all_frozen_kwargs(self):
        prediction = object()
        strategy = {
            "class": "TopkDropoutStrategy",
            "module_path": "qlib.contrib.strategy",
            "kwargs": {
                "signal": "<PRED>",
                "topk": 50,
                "n_drop": 1,
                "hold_thresh": 126,
                "risk_degree": 0.95,
                "method_buy": "top",
                "method_sell": "bottom",
                "only_tradable": False,
                "forbid_all_trade_at_limit": True,
            },
        }

        resolved = BacktestRunner._qlib_strategy_spec(strategy, prediction)

        self.assertEqual(resolved["class"], strategy["class"])
        self.assertEqual(resolved["module_path"], strategy["module_path"])
        self.assertIs(resolved["kwargs"]["signal"], prediction)
        self.assertEqual(
            {key: value for key, value in resolved["kwargs"].items() if key != "signal"},
            {key: value for key, value in strategy["kwargs"].items() if key != "signal"},
        )
        self.assertEqual(strategy["kwargs"]["signal"], "<PRED>")

    def test_strategy_signal_breaks_ties_by_instrument_deterministically(self):
        index = pd.MultiIndex.from_tuples(
            [
                (pd.Timestamp("2026-08-03"), "B"),
                (pd.Timestamp("2026-08-03"), "A"),
                (pd.Timestamp("2026-08-03"), "C"),
                (pd.Timestamp("2026-08-04"), "A"),
            ],
            names=["datetime", "instrument"],
        )
        signal = pd.DataFrame({"score": [1.0, 1.0, 2.0, float("nan")]}, index=index)
        shuffled = signal.iloc[[2, 0, 3, 1]]

        first = BacktestRunner._deterministic_strategy_signal(signal)
        second = BacktestRunner._deterministic_strategy_signal(shuffled)

        pd.testing.assert_frame_equal(first.sort_index(), second.sort_index())
        daily = first.xs(pd.Timestamp("2026-08-03"), level="datetime")["score"]
        self.assertGreater(daily["C"], daily["A"])
        self.assertGreater(daily["A"], daily["B"])
        self.assertTrue(
            pd.isna(first.loc[(pd.Timestamp("2026-08-04"), "A"), "score"])
        )

    def test_strategy_signal_rejects_multiple_columns(self):
        index = pd.MultiIndex.from_tuples(
            [(pd.Timestamp("2026-08-03"), "A")],
            names=["datetime", "instrument"],
        )
        signal = pd.DataFrame({"score": [1.0], "other": [2.0]}, index=index)

        with self.assertRaisesRegex(RuntimeError, "exactly one column"):
            BacktestRunner._deterministic_strategy_signal(signal)

    def test_position_count_series_audits_realized_holdings(self):
        result = BacktestRunner._position_count_series(
            {
                pd.Timestamp("2026-08-03"): FakePosition(["A", "B"]),
                pd.Timestamp("2026-08-04"): FakePosition(["A", "B", "C"]),
            }
        )

        self.assertEqual(result.to_dict(), {
            pd.Timestamp("2026-08-03"): 2,
            pd.Timestamp("2026-08-04"): 3,
        })

    def test_position_count_series_rejects_unauditable_payload(self):
        with self.assertRaisesRegex(RuntimeError, "date mapping"):
            BacktestRunner._position_count_series([])


if __name__ == "__main__":
    unittest.main()
