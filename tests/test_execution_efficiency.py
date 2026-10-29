import unittest

import pandas as pd

from quantaalpha.validation.execution_efficiency import execution_cost_check


class ExecutionEfficiencyTest(unittest.TestCase):
    def test_cost_reduction_uses_annualized_realized_cost(self):
        baseline = pd.DataFrame({"cost": [0.001, 0.001]})
        candidate = pd.DataFrame({"cost": [0.0004, 0.0004]})

        check = execution_cost_check(baseline, candidate, 0.5)

        self.assertEqual(check["status"], "PASS")
        self.assertAlmostEqual(check["actual"]["reduction"], 0.6)


if __name__ == "__main__":
    unittest.main()
