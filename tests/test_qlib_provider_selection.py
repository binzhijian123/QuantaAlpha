import tempfile
import unittest
from pathlib import Path

from quantaalpha.utils.qlib_provider import (
    provider_contract_fingerprint,
    required_date_bounds,
    select_provider_for_config,
)


def _provider(root: Path, name: str, dates: list[str]) -> Path:
    provider = root / name
    calendar = provider / "calendars" / "day.txt"
    calendar.parent.mkdir(parents=True)
    calendar.write_text("\n".join(dates) + "\n", encoding="utf-8")
    return provider


class QlibProviderSelectionTest(unittest.TestCase):
    def test_required_dates_are_collected_recursively(self):
        config = {
            "data": {"start_time": "2016-01-01", "end_time": "2023-12-31"},
            "dataset": {"segments": {"test": ["2022-01-01", "2023-12-31"]}},
        }
        self.assertEqual(required_date_bounds(config), ("2016-01-01", "2023-12-31"))

    def test_recent_config_prefers_recent_provider(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            recent = _provider(root, "recent", ["2024-01-02", "2026-07-15"])
            history = _provider(root, "history", ["2005-01-04", "2026-01-09"])
            config = {"data": {"start_time": "2024-01-02", "end_time": "2026-07-15"}}
            self.assertEqual(select_provider_for_config(config, "recent.yaml", [recent, history]), recent)

    def test_historical_config_falls_back_to_history_provider(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            recent = _provider(root, "recent", ["2024-01-02", "2026-07-15"])
            history = _provider(root, "history", ["2005-01-04", "2026-01-09"])
            config = {"data": {"start_time": "2016-01-01", "end_time": "2023-12-31"}}
            self.assertEqual(select_provider_for_config(config, "history.yaml", [recent, history]), history)

    def test_strict_provider_never_falls_back_to_another_candidate(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            configured = _provider(root, "configured", ["2024-01-02", "2024-12-31"])
            fallback = _provider(root, "fallback", ["2016-01-04", "2026-07-24"])
            config = {
                "data": {
                    "provider_uri": str(configured),
                    "provider_strict": True,
                    "start_time": "2016-01-01",
                    "end_time": "2026-07-22",
                }
            }
            with self.assertRaises(RuntimeError):
                select_provider_for_config(config, "strict.yaml", [fallback])

    def test_provider_contract_changes_with_point_in_time_universe(self):
        with tempfile.TemporaryDirectory() as tmp:
            provider = _provider(Path(tmp), "provider", ["2026-07-23", "2026-07-24"])
            instruments = provider / "instruments" / "csi300.txt"
            instruments.parent.mkdir(parents=True)
            instruments.write_text(
                "SH600000\t2026-07-23\t2026-07-24\n",
                encoding="utf-8",
            )
            before = provider_contract_fingerprint(provider)["fingerprint"]
            instruments.write_text(
                "SH600000\t2026-07-23\t2026-07-24\n"
                "SZ000001\t2026-07-24\t2026-07-24\n",
                encoding="utf-8",
            )
            after = provider_contract_fingerprint(provider)["fingerprint"]
            self.assertNotEqual(before, after)

    def test_provider_contract_binds_extension_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            provider = _provider(Path(tmp), "provider", ["2026-07-23", "2026-07-24"])
            instruments = provider / "instruments" / "csi300.txt"
            instruments.parent.mkdir(parents=True)
            instruments.write_text(
                "SH600000\t2026-07-23\t2026-07-24\n",
                encoding="utf-8",
            )
            before = provider_contract_fingerprint(provider)
            (provider / "quantaalpha_extension.manifest.json").write_text(
                '{"kind":"point_in_time_fundamentals","batchId":"v1"}',
                encoding="utf-8",
            )
            after = provider_contract_fingerprint(provider)

            self.assertNotEqual(before["fingerprint"], after["fingerprint"])
            self.assertEqual(after["extension"]["batchId"], "v1")
            self.assertEqual(after["strength"], "contract_and_extension")

    def test_frozen_market_extension_rejects_all_market_after_base_end(self):
        with tempfile.TemporaryDirectory() as tmp:
            provider = _provider(
                Path(tmp),
                "provider",
                ["2026-08-04", "2026-08-05", "2026-08-06"],
            )
            (provider / "quantaalpha_extension.manifest.json").write_text(
                '{"kind":"frozen_market_daily_prices","scope":'
                '{"allowedMarkets":["csi300"],"baseCalendarEnd":"2026-08-04"}}',
                encoding="utf-8",
            )
            all_config = {
                "data": {
                    "market": "all",
                    "start_time": "2026-08-04",
                    "end_time": "2026-08-06",
                }
            }
            with self.assertRaisesRegex(RuntimeError, "outside frozen extension scope"):
                select_provider_for_config(all_config, "all.yaml", [provider])

            missing_market_config = {
                "data": {
                    "start_time": "2026-08-04",
                    "end_time": "2026-08-06",
                }
            }
            with self.assertRaisesRegex(RuntimeError, "market must be declared"):
                select_provider_for_config(
                    missing_market_config,
                    "missing-market.yaml",
                    [provider],
                )

            csi300_config = {
                "data": {
                    "market": "csi300",
                    "start_time": "2026-08-04",
                    "end_time": "2026-08-06",
                }
            }
            self.assertEqual(
                select_provider_for_config(csi300_config, "csi300.yaml", [provider]),
                provider,
            )


if __name__ == "__main__":
    unittest.main()
