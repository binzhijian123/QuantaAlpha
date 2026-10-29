import hashlib
import json
import tempfile
import unittest
from pathlib import Path

import yaml

from quantaalpha.backtest.factor_loader import FactorLoader
from quantaalpha.backtest.runner import BacktestRunner


class FactorDecisionFilterTests(unittest.TestCase):
    def _library(self, root: Path) -> Path:
        path = root / "factors.json"
        path.write_text(
            json.dumps(
                {
                    "factors": {
                        "accepted": {
                            "factor_name": "ACCEPTED",
                            "factor_expression": "$close",
                            "feedback": {"decision": True},
                        },
                        "rejected": {
                            "factor_name": "REJECTED",
                            "factor_expression": "$open",
                            "feedback": {"decision": False},
                        },
                        "legacy": {
                            "factor_name": "LEGACY",
                            "factor_expression": "$volume",
                        },
                    }
                }
            ),
            encoding="utf-8",
        )
        return path

    @staticmethod
    def _config(path: Path, decision_filter: str) -> dict:
        return {
            "factor_source": {
                "type": "custom",
                "custom": {
                    "json_files": [str(path)],
                    "decision_filter": decision_filter,
                },
            }
        }

    def test_accepted_filter_is_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._library(Path(tmp))
            _, factors = FactorLoader(self._config(path, "accepted")).load_factors()

        self.assertEqual([factor["factor_id"] for factor in factors], ["accepted"])
        self.assertIs(factors[0]["feedback_decision"], True)

    def test_all_filter_is_explicit_audit_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._library(Path(tmp))
            _, factors = FactorLoader(self._config(path, "all")).load_factors()

        self.assertEqual(
            [factor["factor_id"] for factor in factors],
            ["accepted", "rejected", "legacy"],
        )

    def test_invalid_filter_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._library(Path(tmp))
            with self.assertRaises(ValueError):
                FactorLoader(self._config(path, "best")).load_factors()

    def test_strict_mode_rejects_missing_library(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = self._config(Path(tmp) / "missing.json", "all")
            config["factor_source"]["custom"]["strict"] = True
            with self.assertRaises(FileNotFoundError):
                FactorLoader(config).load_factors()

    def test_strict_mode_rejects_factor_count_mismatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._library(Path(tmp))
            config = self._config(path, "accepted")
            config["factor_source"]["custom"].update(
                {"strict": True, "expected_factor_count": 2}
            )
            with self.assertRaises(ValueError):
                FactorLoader(config).load_factors()

    def test_strict_mode_accepts_exact_factor_count(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._library(Path(tmp))
            config = self._config(path, "accepted")
            config["factor_source"]["custom"].update(
                {"strict": True, "expected_factor_count": 1}
            )
            _, factors = FactorLoader(config).load_factors()

        self.assertEqual([factor["factor_id"] for factor in factors], ["accepted"])

    def test_strict_mode_rejects_factor_id_mismatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._library(Path(tmp))
            config = self._config(path, "accepted")
            config["factor_source"]["custom"].update(
                {
                    "strict": True,
                    "expected_factor_count": 1,
                    "expected_factor_ids": ["different"],
                }
            )
            with self.assertRaisesRegex(ValueError, "factor IDs mismatch"):
                FactorLoader(config).load_factors()


class StrictRuntimeContractTests(unittest.TestCase):
    def test_runtime_rechecks_cache_binding_and_hash(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cache_path = root / "result.h5"
            cache_path.write_bytes(b"frozen-cache")
            config_path = root / "config.yaml"
            config_path.write_text(
                yaml.safe_dump(
                    {
                        "data": {
                            "provider_fingerprint": "provider-a",
                            "market": "csi300",
                        },
                        "factor_source": {
                            "custom": {
                                "strict": True,
                                "expected_factor_ids": ["factor-a"],
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            expression = "RANK($close)"
            factors = [
                {
                    "factor_id": "factor-a",
                    "factor_name": "FACTOR_A",
                    "factor_expression": expression,
                    "cache_location": {
                        "result_h5_path": str(cache_path),
                        "result_h5_sha256": hashlib.sha256(
                            cache_path.read_bytes()
                        ).hexdigest(),
                        "provider_fingerprint": "provider-a",
                        "market": "csi300",
                        "expression_sha256": hashlib.sha256(
                            expression.encode("utf-8")
                        ).hexdigest(),
                    },
                }
            ]
            runner = BacktestRunner(str(config_path))
            runner._validate_strict_custom_factor_contract(factors)

            cache_path.write_bytes(b"tampered")
            with self.assertRaisesRegex(ValueError, "cache hash mismatch"):
                runner._validate_strict_custom_factor_contract(factors)


if __name__ == "__main__":
    unittest.main()
