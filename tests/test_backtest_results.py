import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path


def _load_backend_app():
    app_path = Path(__file__).resolve().parents[1] / "frontend-v2" / "backend" / "app.py"
    spec = importlib.util.spec_from_file_location("quantaalpha_backend_results_test", app_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load backend app from {app_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


BACKEND_APP = _load_backend_app()


class BacktestResultLoadingTests(unittest.TestCase):
    def test_loads_only_task_owned_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp)
            config_path = output_dir / "backtest.yaml"
            config_path.write_text(
                f"experiment:\n  output_dir: {output_dir}\n", encoding="utf-8"
            )
            valid = {
                "metrics": {
                    "annualized_return": 0.1,
                    "max_drawdown": -0.2,
                    "information_ratio": 0.5,
                }
            }
            (output_dir / "backtest_owned_backtest_metrics.json").write_text(
                json.dumps(valid), encoding="utf-8"
            )
            (output_dir / "backtest_other_backtest_metrics.json").write_text(
                json.dumps({"metrics": {"annualized_return": 99}}), encoding="utf-8"
            )
            task = {
                "config": {"configPath": str(config_path), "outputName": "backtest_owned"},
                "metrics": {},
            }

            error = BACKEND_APP._load_backtest_results(task)

            self.assertIsNone(error)
            self.assertEqual(task["metrics"]["annualized_return"], 0.1)

    def test_empty_metrics_are_a_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp)
            config_path = output_dir / "backtest.yaml"
            config_path.write_text(
                f"experiment:\n  output_dir: {output_dir}\n", encoding="utf-8"
            )
            (output_dir / "backtest_empty_backtest_metrics.json").write_text(
                json.dumps({"metrics": {}}), encoding="utf-8"
            )
            task = {
                "config": {"configPath": str(config_path), "outputName": "backtest_empty"},
                "metrics": {},
            }

            error = BACKEND_APP._load_backtest_results(task)

            self.assertIn("缺少策略指标", error)
            self.assertEqual(task["metrics"], {})


if __name__ == "__main__":
    unittest.main()
