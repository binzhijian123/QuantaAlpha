import unittest

import pandas as pd

from quantaalpha.validation.standard_baseline import (
    evaluate_standard_baseline,
    validate_daily_pair,
)


def _metrics(annualized, information_ratio, max_drawdown, total=0.01):
    return {
        "annualizedExcessReturn": annualized,
        "informationRatio": information_ratio,
        "maxDrawdown": max_drawdown,
        "totalExcessReturn": total,
    }


class StandardBaselineValidationTest(unittest.TestCase):
    def setUp(self):
        self.promotion = {
            "recent_window_days": 252,
            "recent_annualized_excess_min": 0.0,
            "latest_complete_year": "2025",
            "latest_year_annualized_excess_min": 0.02,
            "information_ratio_min": 0.5,
            "max_drawdown_abs_max": 0.15,
            "cost_multiplier": 2.0,
            "stressed_annualized_excess_min": 0.0,
            "stability_years": ["2022", "2023", "2024", "2025"],
            "positive_years_min": 3,
            "latest_year_max_positive_month_share": 0.8,
            "annualized_excess_improvement_min": 0.02,
            "information_ratio_improvement_min": 0.2,
            "annualized_excess_degradation_max": 0.01,
            "information_ratio_degradation_max": 0.1,
            "drawdown_degradation_max": 0.02,
            "bootstrap_lower_bound_min": 0.0,
        }

    def _analysis(self, annualized, information_ratio, max_drawdown):
        yearly = {
            year: _metrics(0.04, 0.4, -0.05)
            for year in ("2022", "2023", "2024", "2025")
        }
        monthly = {
            f"2025-{month:02d}": _metrics(0.03, 0.2, -0.01, total=0.01)
            for month in range(1, 13)
        }
        return {
            "standard": _metrics(annualized, information_ratio, max_drawdown),
            "yearly": yearly,
            "monthly": monthly,
            "recentWindows": {"252": _metrics(0.05, 0.5, -0.05)},
            "costSensitivity": {"2.0": _metrics(0.02, 0.3, -0.08)},
        }

    def test_all_frozen_checks_can_pass(self):
        baseline = self._analysis(0.01, 0.1, -0.12)
        candidate = self._analysis(0.05, 0.6, -0.10)
        bootstrap = {
            "status": "PASS",
            "confidenceInterval90": [0.01, 0.07],
        }

        checks = evaluate_standard_baseline(
            baseline, candidate, self.promotion, bootstrap
        )

        self.assertTrue(checks)
        self.assertTrue(all(check["status"] == "PASS" for check in checks))

    def test_failed_recent_and_incremental_evidence_rejects(self):
        baseline = self._analysis(0.02, 0.3, -0.10)
        candidate = self._analysis(0.021, 0.31, -0.10)
        candidate["recentWindows"]["252"]["annualizedExcessReturn"] = -0.01
        bootstrap = {
            "status": "PASS",
            "confidenceInterval90": [-0.01, 0.02],
        }

        checks = evaluate_standard_baseline(
            baseline, candidate, self.promotion, bootstrap
        )
        failed = {check["id"] for check in checks if check["status"] == "FAIL"}

        self.assertIn("recent_window_positive", failed)
        self.assertIn("incremental_value", failed)
        self.assertIn("incremental_bootstrap", failed)

    def test_daily_pair_requires_exact_dates(self):
        baseline = pd.DataFrame(index=pd.to_datetime(["2025-01-02", "2025-01-03"]))
        candidate = pd.DataFrame(index=pd.to_datetime(["2025-01-02", "2025-01-06"]))

        with self.assertRaisesRegex(ValueError, "exactly identical"):
            validate_daily_pair(baseline, candidate)


if __name__ == "__main__":
    unittest.main()
