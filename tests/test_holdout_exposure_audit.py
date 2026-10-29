import csv
import json
import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quantaalpha.validation.holdout_exposure import (  # noqa: E402
    _extract_config_intervals,
    collect_holdout_exposure,
    write_holdout_exposure_audit,
)


class HoldoutExposureAuditTests(unittest.TestCase):
    def test_yaml_date_objects_are_normalized(self):
        import yaml

        payload = yaml.safe_load(
            "dataset:\n"
            "  segments:\n"
            "    train: [2008-01-01, 2014-12-31]\n"
            "    valid: [2015-01-01, 2016-12-31]\n"
            "    test: [2017-01-01, 2020-08-01]\n"
        )

        self.assertEqual(
            _extract_config_intervals(payload),
            [
                ("test", "2017-01-01", "2020-08-01"),
                ("train", "2008-01-01", "2014-12-31"),
                ("valid", "2015-01-01", "2016-12-31"),
            ],
        )

    def _project(self, root: Path) -> Path:
        (root / "configs").mkdir()
        results = root / "data" / "results" / "legacy" / "run"
        results.mkdir(parents=True)
        provider = root / "provider"
        (provider / "calendars").mkdir(parents=True)
        (provider / "instruments").mkdir()
        dates = [f"2020-01-{day:02d}" for day in range(1, 21)]
        (provider / "calendars" / "day.txt").write_text(
            "\n".join(dates) + "\n", encoding="utf-8"
        )
        (provider / "instruments" / "csi300.txt").write_text(
            "SH600000\t2020-01-01\t2020-01-20\n", encoding="utf-8"
        )
        config = (
            "dataset:\n"
            "  segments:\n"
            "    train: [2020-01-01, 2020-01-04]\n"
            "    valid: [2020-01-05, 2020-01-06]\n"
            "    test: [2020-01-07, 2020-01-10]\n"
        )
        (results / "conf.yaml").write_text(config, encoding="utf-8")
        (results / "ret.pkl").write_bytes(b"opaque-local-result")
        with (results / "sample_cumulative_excess.csv").open(
            "w", encoding="utf-8", newline=""
        ) as stream:
            writer = csv.DictWriter(
                stream, fieldnames=["date", "daily_excess_return"]
            )
            writer.writeheader()
            for day in range(7, 11):
                writer.writerow(
                    {"date": f"2020-01-{day:02d}", "daily_excess_return": "0.0"}
                )
        return provider

    def test_collect_finds_only_late_qualifying_gap(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            provider = self._project(root)

            payload, _, issues = collect_holdout_exposure(
                root,
                provider,
                minimum_training_sessions=3,
                minimum_holdout_sessions=5,
            )

        self.assertFalse(issues)
        self.assertEqual(payload["status"], "HISTORICAL_HOLDOUT_AVAILABLE")
        self.assertEqual(
            payload["unexposedIntervals"],
            [
                {
                    "start": "2020-01-11",
                    "end": "2020-01-20",
                    "sessions": 10,
                    "priorEligibleSessions": 10,
                    "trainingSessionsRequired": 3,
                    "holdoutSessionsRequired": 5,
                    "qualifies": True,
                }
            ],
        )

    def test_writer_reports_no_holdout_without_overwriting(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            provider = self._project(root)
            output = root / "audit"

            result = write_holdout_exposure_audit(
                root,
                provider,
                output,
                minimum_training_sessions=12,
                minimum_holdout_sessions=12,
            )
            payload = json.loads((output / "summary.json").read_text())

            self.assertEqual(result["status"], "NO_DEFENSIBLE_HISTORICAL_HOLDOUT")
            self.assertEqual(payload["blockingIssues"], 0)
            self.assertTrue((output / "summary.sha256").is_file())
            self.assertTrue((output / "exposure_sources.csv").is_file())
            with self.assertRaises(FileExistsError):
                write_holdout_exposure_audit(root, provider, output)


if __name__ == "__main__":
    unittest.main()
