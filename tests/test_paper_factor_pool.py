import unittest

import numpy as np
import pandas as pd

from quantaalpha.validation.factor_pool import select_paper_factor_pool


def _panel(days: int = 8, instruments: int = 8):
    dates = pd.date_range("2021-01-04", periods=days, freq="B")
    codes = [f"S{index:02d}" for index in range(instruments)]
    index = pd.MultiIndex.from_product(
        [dates, codes],
        names=["datetime", "instrument"],
    )
    base = np.tile(np.arange(instruments, dtype=float), days)
    label = pd.Series(base, index=index, name="label")
    factors = pd.DataFrame(
        {
            "alpha": base,
            "alpha_duplicate": base * 2,
            "diverse": np.tile(
                np.array([0, 3, 1, 6, 2, 7, 5, 4], dtype=float),
                days,
            ),
            "constant": 1.0,
        },
        index=index,
    )
    return factors, label


class PaperFactorPoolTest(unittest.TestCase):
    def test_rankic_order_correlation_rejection_and_audit(self):
        factors, label = _panel()
        result = select_paper_factor_pool(
            factors,
            label,
            max_fraction=1.0,
            min_cross_section_size=5,
            min_valid_days=5,
        )
        decisions = {
            item["factor"]: item for item in result["decisions"]
        }
        self.assertEqual(result["selectedFactors"][0], "alpha")
        self.assertEqual(
            decisions["alpha_duplicate"]["reason"],
            "correlation_threshold",
        )
        self.assertEqual(
            decisions["constant"]["reason"],
            "insufficient_valid_rankic_days",
        )
        self.assertIn("diverse", result["selectedFactors"])

    def test_pool_is_capped_at_half_of_mined_factors(self):
        factors, label = _panel()
        result = select_paper_factor_pool(
            factors,
            label,
            min_cross_section_size=5,
            min_valid_days=5,
        )
        self.assertEqual(result["candidateCount"], 4)
        self.assertEqual(result["poolLimit"], 2)
        self.assertLessEqual(result["selectedCount"], 2)

    def test_ties_are_resolved_by_factor_name(self):
        factors, label = _panel()
        factors = factors[["alpha"]].rename(columns={"alpha": "zeta"})
        factors["beta"] = factors["zeta"]
        result = select_paper_factor_pool(
            factors,
            label,
            max_fraction=1.0,
            min_cross_section_size=5,
            min_valid_days=5,
        )
        self.assertEqual(result["decisions"][0]["factor"], "beta")

    def test_invalid_index_contract_fails_closed(self):
        factors, label = _panel()
        factors.index = factors.index.set_names(["date", "instrument"])
        with self.assertRaisesRegex(ValueError, "index names"):
            select_paper_factor_pool(factors, label)

    def test_non_overlapping_panel_fails_closed(self):
        factors, label = _panel()
        label.index = pd.MultiIndex.from_product(
            [
                pd.date_range("2022-01-03", periods=8, freq="B"),
                [f"S{index:02d}" for index in range(8)],
            ],
            names=["datetime", "instrument"],
        )
        with self.assertRaisesRegex(ValueError, "do not overlap"):
            select_paper_factor_pool(factors, label)


if __name__ == "__main__":
    unittest.main()
