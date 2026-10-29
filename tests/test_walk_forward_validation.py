import unittest

import pandas as pd

from quantaalpha.validation.walk_forward import build_walk_forward_schedule


class WalkForwardScheduleTest(unittest.TestCase):
    def test_quarterly_schedule_is_purged_and_contiguous(self):
        calendar = pd.bdate_range("2017-01-02", "2022-12-30")

        folds = build_walk_forward_schedule(
            calendar,
            train_start="2017-01-02",
            test_start="2021-01-04",
            test_end="2022-12-30",
            validation_sessions=252,
            label_horizon_sessions=2,
        )

        self.assertEqual(len(folds), 8)
        covered = []
        for fold in folds:
            train_end = calendar.get_loc(pd.Timestamp(fold["train"][1]))
            valid_start = calendar.get_loc(pd.Timestamp(fold["valid"][0]))
            valid_end = calendar.get_loc(pd.Timestamp(fold["valid"][1]))
            test_start = calendar.get_loc(pd.Timestamp(fold["test"][0]))
            self.assertLess(train_end + 2, valid_start)
            self.assertLess(valid_end + 2, test_start)
            covered.extend(
                calendar[
                    (calendar >= pd.Timestamp(fold["test"][0]))
                    & (calendar <= pd.Timestamp(fold["test"][1]))
                ]
            )
        expected = calendar[(calendar >= "2021-01-04") & (calendar <= "2022-12-30")]
        self.assertTrue(pd.DatetimeIndex(covered).equals(expected))


if __name__ == "__main__":
    unittest.main()
