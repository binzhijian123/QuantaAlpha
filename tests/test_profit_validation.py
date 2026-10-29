import hashlib
import json
import math
import os
import tempfile
import unittest
from pathlib import Path
from statistics import NormalDist
from unittest.mock import patch

import numpy as np
import pandas as pd

from quantaalpha.backtest.custom_factor_calculator import CustomFactorCalculator
from quantaalpha.validation.profit import (
    _cache_matches_provider,
    _configured_reference_factor_expressions,
    _decision,
    _deterministic_process_contract,
    _effective_session_range,
    _factor_cache_code_contract,
    _has_consistency_proof,
    _multiple_testing_audit,
    _resolve_trial_target,
    _verify_historical_trial_audit,
    _verify_trial_count_evidence,
    analyze_daily_returns,
    audit_factor_library,
    build_strict_trial_matrix,
    deduplicate_trial_returns,
    deflated_sharpe_ratio,
    probability_of_backtest_overfitting,
    run_profit_validation,
    select_factor_pool,
)
from quantaalpha.validation.trial_history import audit_id, canonical_sha256


class FunctionalRedundancyTests(unittest.TestCase):
    def test_profit_validation_requires_deterministic_hash_seed(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "PYTHONHASHSEED=0"):
                _deterministic_process_contract()

        with patch.dict(os.environ, {"PYTHONHASHSEED": "0"}, clear=True):
            self.assertEqual(
                _deterministic_process_contract(),
                {
                    "pythonHashSeed": "0",
                    "strategySignalOrder": "score_desc_instrument_asc",
                },
            )

    def test_higher_validation_rank_ic_wins_correlated_group(self):
        correlations = pd.DataFrame(
            [
                [1.0, 0.99, 0.10],
                [0.99, 1.0, 0.05],
                [0.10, 0.05, 1.0],
            ],
            index=["a", "b", "c"],
            columns=["a", "b", "c"],
        )
        selected, rejected = select_factor_pool(
            ["a", "b", "c"],
            {"a": 0.01, "b": 0.03, "c": 0.02},
            correlations,
            0.7,
        )

        self.assertEqual(selected, ["b", "c"])
        self.assertEqual(rejected, {"a": "b"})

    def test_configured_references_extend_official_factor_set(self):
        references = _configured_reference_factor_expressions(
            {
                "factor_source": {
                    "combined": {"official_source": "alpha158_20"}
                }
            },
            {
                "reference_factor_expressions": {
                    "prior_book_to_price": "$pit_book_to_price"
                }
            },
        )

        self.assertIn("ROC1", references)
        self.assertEqual(references["prior_book_to_price"], "$pit_book_to_price")

    def test_configured_reference_contract_rejects_non_mapping(self):
        with self.assertRaisesRegex(ValueError, "must be a mapping"):
            _configured_reference_factor_expressions(
                {"factor_source": {"type": "alpha158_20"}},
                {"reference_factor_expressions": ["$pit_book_to_price"]},
            )


class PreregisteredSelectionTests(unittest.TestCase):
    def test_false_feedback_factor_can_be_explicitly_admitted(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            index = pd.MultiIndex.from_product(
                [
                    pd.date_range("2021-01-04", periods=10, freq="B"),
                    [f"SH600{number:03d}" for number in range(11)],
                ],
                names=["datetime", "instrument"],
            )
            values = np.tile(np.arange(11, dtype=float), 10)
            cache_path = root / "candidate.h5"
            pd.Series(values, index=index, name="factor").to_hdf(
                cache_path,
                key="data",
                mode="w",
            )
            expression = "RANK($close)"
            factor_id = "candidate"
            library_path = root / "library.json"
            library_path.write_text(
                json.dumps(
                    {
                        "factors": {
                            factor_id: {
                                "factor_name": "Candidate",
                                "factor_expression": expression,
                                "feedback": {"decision": False},
                                "quality_gate": {
                                    "consistency_enabled": True,
                                    "consistency_passed": True,
                                    "consistency_check": {"is_consistent": True},
                                },
                                "cache_location": {
                                    "result_h5_path": str(cache_path),
                                    "result_h5_sha256": hashlib.sha256(
                                        cache_path.read_bytes()
                                    ).hexdigest(),
                                    "provider_fingerprint": "provider-a",
                                    "market": "csi300",
                                    "date_range": ["2021-01-04", "2021-01-15"],
                                    "expression_sha256": hashlib.sha256(
                                        expression.encode("utf-8")
                                    ).hexdigest(),
                                },
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            label = pd.DataFrame({"LABEL0": values}, index=index)
            with (
                patch(
                    "quantaalpha.validation.profit.provider_contract_fingerprint",
                    return_value={"fingerprint": "provider-a"},
                ),
                patch(
                    "quantaalpha.validation.profit._validation_label_panel",
                    return_value=label,
                ),
                patch(
                    "quantaalpha.validation.profit.QlibBinaryDailySource"
                ) as source_type,
            ):
                source_type.return_value.calendar = list(
                    pd.date_range("2021-01-04", "2021-01-15", freq="B")
                )
                report, selected = audit_factor_library(
                    library_path,
                    root,
                    "csi300",
                    "label",
                    "2021-01-04",
                    "2021-01-15",
                    0.7,
                    required_cache_start="2021-01-01",
                    required_cache_end="2021-01-17",
                    admission_factor_ids=[factor_id],
                )

        self.assertEqual(report["status"], "PASS")
        self.assertEqual(report["admissionMode"], "preregistered_ids")
        self.assertEqual(report["selectedFactorIds"], [factor_id])
        self.assertFalse(report["factors"][0]["feedbackDecision"])
        self.assertTrue(report["factors"][0]["admitted"])
        self.assertEqual(report["requiredCacheRange"], ["2021-01-01", "2021-01-17"])
        self.assertEqual(
            report["requiredCacheSessionRange"],
            ["2021-01-04", "2021-01-15"],
        )
        self.assertIn(factor_id, selected["factors"])

    def test_target_is_deterministically_first_selected_candidate(self):
        factors = {"a": {}, "b": {}, "old": {}}
        audit = {
            "selectedFactorIds": ["b", "a"],
            "admissionFactorIds": ["a", "b"],
            "validationPeriod": ["2021-01-04", "2021-12-29"],
            "factors": [
                {"factorId": "a", "validationRankIC": 0.01},
                {"factorId": "b", "validationRankIC": 0.03},
            ],
        }
        target, evidence = _resolve_trial_target(
            factors,
            audit,
            {
                "target_selection_rule": "highest_validation_rank_ic",
                "target_candidate_factor_ids": ["a", "b"],
            },
        )

        self.assertEqual(target, "b")
        self.assertEqual(evidence["rule"], "highest_validation_rank_ic")
        self.assertEqual(evidence["targetValidationRankIC"], 0.03)

    def test_cached_library_reference_participates_in_redundancy_gate(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            dates = pd.date_range("2021-01-04", periods=10, freq="B")
            instruments = [f"SH{index:06d}" for index in range(11)]
            index = pd.MultiIndex.from_product(
                [dates, instruments], names=["datetime", "instrument"]
            )
            values = np.tile(np.arange(11, dtype=float), 10)

            factors = {}
            for factor_id, expression in (
                ("candidate", "RANK($close)"),
                ("reference", "RANK($open)"),
            ):
                cache_path = root / f"{factor_id}.h5"
                pd.Series(values, index=index, name="factor").to_hdf(
                    cache_path,
                    key="data",
                    mode="w",
                )
                factors[factor_id] = {
                    "factor_name": factor_id.title(),
                    "factor_expression": expression,
                    "feedback": {"decision": False},
                    "quality_gate": {
                        "consistency_enabled": True,
                        "consistency_passed": True,
                        "consistency_check": {"is_consistent": True},
                    },
                    "cache_location": {
                        "result_h5_path": str(cache_path),
                        "result_h5_sha256": hashlib.sha256(
                            cache_path.read_bytes()
                        ).hexdigest(),
                        "provider_fingerprint": "provider-a",
                        "market": "csi300",
                        "date_range": ["2021-01-04", "2021-01-15"],
                        "expression_sha256": hashlib.sha256(
                            expression.encode("utf-8")
                        ).hexdigest(),
                    },
                }

            library_path = root / "library.json"
            library_path.write_text(
                json.dumps({"factors": factors}), encoding="utf-8"
            )
            label = pd.DataFrame({"LABEL0": values}, index=index)
            with (
                patch(
                    "quantaalpha.validation.profit.provider_contract_fingerprint",
                    return_value={"fingerprint": "provider-a"},
                ),
                patch(
                    "quantaalpha.validation.profit._validation_label_panel",
                    return_value=label,
                ),
                patch(
                    "quantaalpha.validation.profit.QlibBinaryDailySource"
                ) as source_type,
            ):
                source_type.return_value.calendar = list(dates)
                report, selected = audit_factor_library(
                    library_path,
                    root,
                    "csi300",
                    "label",
                    "2021-01-04",
                    "2021-01-15",
                    0.7,
                    admission_factor_ids=["candidate"],
                    reference_cached_factor_ids=["reference"],
                )

        self.assertEqual(report["status"], "REJECTED")
        self.assertEqual(report["referenceFactorSource"], "cached_library")
        self.assertEqual(report["referenceCachedFactorIds"], ["reference"])
        self.assertEqual(
            report["referenceRedundantFactors"]["candidate"]["referenceFactor"],
            "cached::reference",
        )
        self.assertEqual(selected["factors"], {})

    def test_target_candidate_universe_must_match_admission(self):
        with self.assertRaisesRegex(ValueError, "match the frozen audit admission"):
            _resolve_trial_target(
                {"a": {}, "b": {}},
                {
                    "selectedFactorIds": ["a"],
                    "admissionFactorIds": ["a", "b"],
                },
                {
                    "target_selection_rule": "highest_validation_rank_ic",
                    "target_candidate_factor_ids": ["a"],
                },
            )


class ReturnAnalysisTests(unittest.TestCase):
    def test_cost_sensitivity_uses_saved_gross_components(self):
        daily = pd.DataFrame(
            {
                "date": pd.date_range("2025-01-02", periods=4),
                "portfolio_return": [0.01, -0.002, 0.004, 0.001],
                "benchmark_return": [0.002, -0.001, 0.001, 0.0],
                "cost": [0.001, 0.001, 0.001, 0.001],
                "daily_excess_return": [0.007, -0.002, 0.002, 0.0],
            }
        )
        result = analyze_daily_returns(daily, [1.0, 2.0])

        self.assertGreater(
            result["costSensitivity"]["1.0"]["annualizedExcessReturn"],
            result["costSensitivity"]["2.0"]["annualizedExcessReturn"],
        )
        self.assertIn("2025", result["yearly"])
        self.assertIn("2025-01", result["monthly"])


class ProvenanceGateTests(unittest.TestCase):
    def test_required_cache_range_uses_provider_sessions(self):
        self.assertEqual(
            _effective_session_range(
                pd.date_range("2021-01-04", "2021-01-08", freq="B"),
                "2021-01-01",
                "2021-01-10",
            ),
            ("2021-01-04", "2021-01-08"),
        )

    def test_required_cache_range_rejects_empty_session_window(self):
        with self.assertRaisesRegex(ValueError, "no sessions"):
            _effective_session_range(
                pd.date_range("2021-01-04", "2021-01-08", freq="B"),
                "2021-01-09",
                "2021-01-10",
            )

    def test_consistency_requires_persisted_checker_evidence(self):
        factor = {
            "quality_gate": {
                "consistency_enabled": True,
                "consistency_passed": True,
            }
        }
        self.assertFalse(_has_consistency_proof(factor))
        factor["quality_gate"]["consistency_check"] = {"is_consistent": True}
        self.assertTrue(_has_consistency_proof(factor))

    def test_cache_requires_matching_provider_fingerprint(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache_path = Path(tmp) / "result.h5"
            cache_path.write_bytes(b"factor-cache")
            expression = "RANK($close)"
            factor = {
                "factor_expression": expression,
                "cache_location": {
                    "result_h5_path": str(cache_path),
                    "result_h5_sha256": hashlib.sha256(b"factor-cache").hexdigest(),
                    "provider_fingerprint": "provider-a",
                    "date_range": ["2014-01-01", "2026-07-24"],
                    "expression_sha256": hashlib.sha256(
                        expression.encode("utf-8")
                    ).hexdigest(),
                }
            }
            self.assertTrue(_cache_matches_provider(factor, "provider-a"))
            self.assertFalse(_cache_matches_provider(factor, "provider-b"))
            factor["factor_expression"] = "RANK($open)"
            self.assertFalse(_cache_matches_provider(factor, "provider-a"))
            factor["factor_expression"] = expression
            cache_path.write_bytes(b"tampered")
            self.assertFalse(_cache_matches_provider(factor, "provider-a"))

    def test_cache_requires_real_h5_date_and_market_coverage(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache_path = Path(tmp) / "result.h5"
            index = pd.MultiIndex.from_product(
                [pd.date_range("2021-01-04", "2021-01-08", freq="B"), ["SH600000"]],
                names=["datetime", "instrument"],
            )
            pd.Series(np.arange(len(index)), index=index, name="factor").to_hdf(
                cache_path,
                key="data",
                mode="w",
            )
            expression = "RANK($close)"
            factor = {
                "factor_expression": expression,
                "cache_location": {
                    "result_h5_path": str(cache_path),
                    "result_h5_sha256": hashlib.sha256(cache_path.read_bytes()).hexdigest(),
                    "provider_fingerprint": "provider-a",
                    "market": "csi300",
                    "date_range": ["2021-01-04", "2021-01-08"],
                    "expression_sha256": hashlib.sha256(
                        expression.encode("utf-8")
                    ).hexdigest(),
                },
            }
            self.assertTrue(
                _cache_matches_provider(
                    factor,
                    "provider-a",
                    market="csi300",
                    required_start="2021-01-04",
                    required_end="2021-01-08",
                )
            )
            self.assertFalse(
                _cache_matches_provider(
                    factor,
                    "provider-a",
                    market="csi500",
                    required_start="2021-01-04",
                    required_end="2021-01-08",
                )
            )

    def test_required_cache_code_binding_rejects_operator_drift(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache_path = Path(tmp) / "result.h5"
            cache_path.write_bytes(b"factor-cache")
            expression = "RANK($close)"
            factor = {
                "factor_expression": expression,
                "metadata": {"cache_code_binding_required": True},
                "cache_location": {
                    "result_h5_path": str(cache_path),
                    "result_h5_sha256": hashlib.sha256(
                        cache_path.read_bytes()
                    ).hexdigest(),
                    "provider_fingerprint": "provider-a",
                    "date_range": ["2014-01-01", "2026-07-24"],
                    "expression_sha256": hashlib.sha256(
                        expression.encode("utf-8")
                    ).hexdigest(),
                    **_factor_cache_code_contract(),
                },
            }

            self.assertTrue(_cache_matches_provider(factor, "provider-a"))
            factor["cache_location"]["function_library_sha256"] = "0" * 64
            self.assertFalse(_cache_matches_provider(factor, "provider-a"))
            self.assertFalse(
                _cache_matches_provider(
                    factor,
                    "provider-a",
                    market="csi300",
                    required_start="2021-01-04",
                    required_end="2021-01-11",
                )
            )

    def test_factor_cache_namespace_prevents_cross_provider_collision(self):
        left = CustomFactorCalculator(cache_namespace="provider-a")
        right = CustomFactorCalculator(cache_namespace="provider-b")
        self.assertNotEqual(
            left._get_cache_key("RANK($close)"),
            right._get_cache_key("RANK($close)"),
        )


class PromotionDecisionTests(unittest.TestCase):
    @staticmethod
    def _analysis() -> dict:
        monthly = {
            f"2025-{month:02d}": {"totalExcessReturn": 0.003}
            for month in range(1, 13)
        }
        return {
            "standard": {
                "annualizedExcessReturn": 0.08,
                "informationRatio": 0.8,
                "maxDrawdown": -0.08,
            },
            "recentWindows": {"252": {"annualizedExcessReturn": 0.06}},
            "yearly": {
                year: {"annualizedExcessReturn": 0.03}
                for year in ("2022", "2023", "2024", "2025")
            },
            "monthly": monthly,
            "costSensitivity": {"2.0": {"annualizedExcessReturn": 0.04}},
        }

    def test_only_full_historical_and_walk_forward_pass_is_paper_candidate(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dates = pd.date_range("2022-01-04", periods=100, freq="B")
            baseline_path = root / "b0.csv"
            combined_path = root / "c1.csv"
            pd.DataFrame(
                {"date": dates, "daily_excess_return": [0.0] * len(dates)}
            ).to_csv(baseline_path, index=False)
            pd.DataFrame(
                {"date": dates, "daily_excess_return": [0.001] * len(dates)}
            ).to_csv(combined_path, index=False)

            baseline_analysis = self._analysis()
            baseline_analysis["standard"] = {
                "annualizedExcessReturn": 0.04,
                "informationRatio": 0.5,
                "maxDrawdown": -0.1,
            }
            candidates = {
                "B0": {
                    "status": "completed",
                    "analysis": baseline_analysis,
                    "dailyPath": str(baseline_path),
                },
                "F1": {"status": "completed", "analysis": self._analysis()},
                "C1": {
                    "status": "completed",
                    "analysis": self._analysis(),
                    "dailyPath": str(combined_path),
                },
            }
            decision = _decision(
                "run",
                "1.2",
                {"status": "PASS"},
                {"status": "PASS"},
                candidates,
                {
                    "bootstrap_iterations": 100,
                    "bootstrap_block_days": 5,
                },
                {"status": "PASS", "required": True},
            )
            self.assertEqual(decision["status"], "PAPER_CANDIDATE")
            self.assertTrue(decision["paperEligible"])

            decision_without_walk = _decision(
                "run",
                "1.2",
                {"status": "PASS"},
                {"status": "PASS"},
                candidates,
                {
                    "bootstrap_iterations": 100,
                    "bootstrap_block_days": 5,
                },
                {"status": "NOT_RUN", "required": True},
            )
            self.assertEqual(decision_without_walk["status"], "RESEARCH_ONLY")
            self.assertFalse(decision_without_walk["paperEligible"])

    def test_incomplete_multiple_testing_evidence_stays_research_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dates = pd.date_range("2022-01-04", periods=100, freq="B")
            candidates = {}
            for candidate, value in (("B0", 0.0), ("F1", 0.0008), ("C1", 0.001)):
                path = root / f"{candidate.lower()}.csv"
                pd.DataFrame(
                    {"date": dates, "daily_excess_return": [value] * len(dates)}
                ).to_csv(path, index=False)
                analysis = self._analysis()
                if candidate == "B0":
                    analysis["standard"] = {
                        "annualizedExcessReturn": 0.04,
                        "informationRatio": 0.5,
                        "maxDrawdown": -0.1,
                    }
                candidates[candidate] = {
                    "status": "completed",
                    "analysis": analysis,
                    "dailyPath": str(path),
                }

            decision = _decision(
                "run",
                "1.3",
                {"status": "PASS"},
                {"status": "PASS"},
                candidates,
                {
                    "bootstrap_iterations": 100,
                    "bootstrap_block_days": 5,
                    "multiple_testing": {
                        "enabled": True,
                        "required": True,
                        "total_trials": 15,
                        "trialCountEvidence": {
                            "status": "PASS",
                            "actualTrials": 15,
                        },
                        "dsr_probability_min": 0.95,
                    },
                },
                {"status": "PASS", "required": True},
            )
            self.assertEqual(decision["status"], "RESEARCH_ONLY")
            self.assertFalse(decision["paperEligible"])
            self.assertEqual(decision["multipleTesting"]["status"], "INSUFFICIENT")

    def test_incomplete_candidate_run_is_blocked_not_rejected(self):
        decision = _decision(
            "run",
            "1.2",
            {"status": "PASS"},
            {"status": "PASS"},
            {"B0": {"status": "failed"}},
            {},
            {"status": "NOT_RUN", "required": True},
        )
        self.assertEqual(decision["status"], "BLOCKED")
        self.assertFalse(decision["paperEligible"])

    def test_definitive_gate_failure_rejects_despite_later_technical_block(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dates = pd.date_range("2022-01-04", periods=100, freq="B")
            candidates = {}
            for candidate, value in (("B0", 0.0), ("F1", 0.0008), ("C1", 0.001)):
                path = root / f"{candidate.lower()}.csv"
                pd.DataFrame(
                    {"date": dates, "daily_excess_return": [value] * len(dates)}
                ).to_csv(path, index=False)
                analysis = self._analysis()
                if candidate == "B0":
                    analysis["standard"] = {
                        "annualizedExcessReturn": 0.04,
                        "informationRatio": 0.5,
                        "maxDrawdown": -0.1,
                    }
                if candidate == "C1":
                    analysis["standard"]["informationRatio"] = 0.1
                candidates[candidate] = {
                    "status": "completed",
                    "analysis": analysis,
                    "dailyPath": str(path),
                }

            decision = _decision(
                "run",
                "2.4",
                {"status": "PASS"},
                {"status": "PASS"},
                candidates,
                {
                    "bootstrap_iterations": 100,
                    "bootstrap_block_days": 5,
                    "multiple_testing": {
                        "enabled": True,
                        "required": True,
                        "total_trials": 20,
                        "trialMatrix": {"status": "PASS"},
                    },
                },
                {"status": "NOT_RUN", "required": True},
            )

        self.assertEqual(decision["status"], "REJECTED")
        self.assertIn("C1 信息比率未达到门槛。", decision["reasons"])
        self.assertIn("收益矩阵文件、哈希、目标或预注册试验数不匹配。", decision["reasons"])

    def test_preregistered_factor_rejection_is_not_a_technical_block(self):
        decision = _decision(
            "run",
            "1.5",
            {"status": "PASS"},
            {
                "status": "REJECTED",
                "blockingReasons": [],
                "rejectionReasons": ["no candidate passed the frozen gate"],
            },
            {},
            {},
            {"status": "NOT_RUN", "required": True},
        )

        self.assertEqual(decision["status"], "REJECTED")
        self.assertEqual(decision["reasons"], ["no candidate passed the frozen gate"])

    def test_historical_only_evidence_cannot_promote_to_paper_candidate(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dates = pd.date_range("2022-01-04", periods=100, freq="B")
            baseline_path = root / "b0.csv"
            combined_path = root / "c1.csv"
            pd.DataFrame(
                {"date": dates, "daily_excess_return": [0.0] * len(dates)}
            ).to_csv(baseline_path, index=False)
            pd.DataFrame(
                {"date": dates, "daily_excess_return": [0.001] * len(dates)}
            ).to_csv(combined_path, index=False)
            candidates = {
                "B0": {
                    "status": "completed",
                    "analysis": {
                        **self._analysis(),
                        "standard": {
                            "annualizedExcessReturn": 0.04,
                            "informationRatio": 0.5,
                            "maxDrawdown": -0.1,
                        },
                    },
                    "dailyPath": str(baseline_path),
                },
                "F1": {"status": "completed", "analysis": self._analysis()},
                "C1": {
                    "status": "completed",
                    "analysis": self._analysis(),
                    "dailyPath": str(combined_path),
                },
            }
            decision = _decision(
                "run",
                "1.5",
                {"status": "PASS"},
                {"status": "PASS"},
                candidates,
                {"bootstrap_iterations": 100, "bootstrap_block_days": 5},
                {"status": "PASS", "required": True},
                historical_evidence_only=True,
            )

        self.assertEqual(decision["status"], "RESEARCH_ONLY")
        self.assertFalse(decision["paperEligible"])


class MultipleTestingTests(unittest.TestCase):
    def test_identical_realized_paths_are_aliased_for_pbo(self):
        returns = pd.DataFrame(
            {
                "a": [0.1, -0.1, 0.2],
                "b": [0.1, -0.1, 0.2],
                "c": [0.2, -0.1, 0.1],
            }
        )

        unique, aliases = deduplicate_trial_returns(returns)

        self.assertEqual(list(unique.columns), ["a", "c"])
        self.assertEqual(aliases, {"b": "a"})

    def test_strict_matrix_audit_uses_unique_paths_for_pbo(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "matrix.csv"
            days = 160
            noise = np.sin(np.arange(days)) * 0.001
            frame = pd.DataFrame(
                {
                    "date": pd.date_range("2022-01-04", periods=days, freq="B"),
                    **{
                        f"T{index:02d}": noise + index * 0.00001
                        for index in range(12)
                    },
                }
            )
            frame["T01"] = frame["T00"]
            frame.to_csv(path, index=False)
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            expected_ids = [f"T{index:02d}" for index in range(12)]

            result = _multiple_testing_audit(
                {},
                {
                    "enabled": True,
                    "required": True,
                    "total_trials": 12,
                    "dsr_probability_min": 0.0,
                    "trialMatrix": {
                        "status": "PASS",
                        "path": str(path),
                        "sha256": digest,
                        "expectedTrialIds": expected_ids,
                        "targetTrialId": "T11",
                    },
                    "pbo": {
                        "required": True,
                        "partitions": 16,
                        "minimum_strategies": 10,
                        "probability_max": 1.0,
                    },
                },
            )

        self.assertEqual(result["pbo"]["strategies"], 11)
        self.assertEqual(result["pboReturnAliases"], {"T01": "T00"})
        self.assertEqual(result["deflatedSharpe"]["gateStatus"], "PASS")
        self.assertEqual(result["pbo"]["gateStatus"], "PASS")

    def test_multiple_testing_separates_computation_from_failed_gates(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "matrix.csv"
            rng = np.random.default_rng(11)
            frame = pd.DataFrame(
                {
                    "date": pd.date_range("2022-01-04", periods=160, freq="B"),
                    **{
                        f"T{index:02d}": rng.normal(0.0, 0.01, 160)
                        for index in range(12)
                    },
                }
            )
            frame.to_csv(path, index=False)
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            expected_ids = [f"T{index:02d}" for index in range(12)]

            result = _multiple_testing_audit(
                {},
                {
                    "enabled": True,
                    "required": True,
                    "total_trials": 12,
                    "dsr_probability_min": 1.0,
                    "trialMatrix": {
                        "status": "PASS",
                        "path": str(path),
                        "sha256": digest,
                        "expectedTrialIds": expected_ids,
                        "targetTrialId": "T11",
                    },
                    "pbo": {
                        "required": True,
                        "partitions": 16,
                        "minimum_strategies": 10,
                        "probability_max": 0.0,
                    },
                },
            )

        self.assertEqual(result["status"], "FAIL")
        self.assertEqual(result["deflatedSharpe"]["status"], "PASS")
        self.assertEqual(result["deflatedSharpe"]["gateStatus"], "FAIL")
        self.assertEqual(result["pbo"]["status"], "PASS")
        self.assertGreater(result["pbo"]["probability"], 0.0)
        self.assertEqual(result["pbo"]["gateStatus"], "FAIL")

    def test_declared_trials_penalize_complete_smaller_homogeneous_matrix(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "matrix.csv"
            rng = np.random.default_rng(17)
            expected_ids = [f"T{index:02d}" for index in range(12)]
            frame = pd.DataFrame(
                {
                    "date": pd.date_range("2022-01-04", periods=160, freq="B"),
                    **{
                        factor_id: rng.normal(0.0, 0.01, 160)
                        for factor_id in expected_ids
                    },
                }
            )
            frame.to_csv(path, index=False)
            result = _multiple_testing_audit(
                {},
                {
                    "enabled": True,
                    "required": True,
                    "matrix_trials": 12,
                    "total_trials": 15,
                    "dsr_probability_min": 0.95,
                    "trialMatrix": {
                        "status": "PASS",
                        "path": str(path),
                        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                        "expectedTrialIds": expected_ids,
                        "targetTrialId": "T11",
                    },
                    "pbo": {
                        "required": True,
                        "partitions": 16,
                        "minimum_strategies": 10,
                        "probability_max": 0.05,
                    },
                },
            )

        self.assertIn(result["status"], {"PASS", "FAIL"})
        self.assertEqual(result["declaredTrialCount"], 15)
        self.assertEqual(result["matrixTrialCount"], 12)
        self.assertEqual(result["deflatedSharpe"]["status"], "PASS")
        self.assertEqual(result["deflatedSharpe"]["declaredTrials"], 15)
        self.assertEqual(result["deflatedSharpe"]["requiredObservedTrials"], 12)
        self.assertTrue(result["deflatedSharpe"]["evidenceComplete"])

    def test_trial_count_evidence_binds_count_and_source_library(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            evidence_path = root / "audit.json"
            evidence_path.write_text(
                json.dumps(
                    {
                        "status": "RESEARCH_ONLY",
                        "summary": {"libraryFactorCount": 15},
                        "inputs": {"library": {"sha256": "source-sha"}},
                    }
                ),
                encoding="utf-8",
            )
            evidence, resolved = _verify_trial_count_evidence(
                root,
                {"metadata": {"trial_universe_library_sha256": "source-sha"}},
                {"total_trials": 15, "trial_count_evidence": str(evidence_path)},
            )
            self.assertEqual(evidence["status"], "PASS")
            self.assertEqual(evidence["actualTrials"], 15)
            self.assertEqual(resolved, evidence_path.resolve())

            mismatched, _ = _verify_trial_count_evidence(
                root,
                {"metadata": {"trial_universe_library_sha256": "other-sha"}},
                {"total_trials": 15, "trial_count_evidence": str(evidence_path)},
            )
            self.assertEqual(mismatched["status"], "FAIL")

    def test_historical_trial_audit_binds_hash_and_declared_floor(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            code_paths = {
                "auditCodeSha256": root
                / "quantaalpha"
                / "validation"
                / "trial_history.py",
                "entrypointSha256": root
                / "scripts"
                / "audit_profit_trial_history.py",
                "verifierCodeSha256": root
                / "quantaalpha"
                / "validation"
                / "profit.py",
                "factorFunctionLibrarySha256": root
                / "quantaalpha"
                / "factors"
                / "coder"
                / "function_lib.py",
                "factorParserSha256": root
                / "quantaalpha"
                / "factors"
                / "coder"
                / "expr_parser.py",
                "factorCalculatorSha256": root
                / "quantaalpha"
                / "backtest"
                / "custom_factor_calculator.py",
                "factorPreparationSha256": root
                / "scripts"
                / "prepare_profit_factor_library.py",
                "reconciliationSha256": root
                / "configs"
                / "profit_trial_history_reconciliation_v1.json",
            }
            for field, path in code_paths.items():
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(field, encoding="utf-8")
            audit_dir = root / "audit"
            audit_dir.mkdir()
            derived = {}
            for name in ("runs", "trials", "issues", "report"):
                suffix = ".md" if name == "report" else ".csv"
                path = audit_dir / f"{name}{suffix}"
                path.write_text(name, encoding="utf-8")
                derived[name] = {
                    "path": path.name,
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                }
            trials = [
                {
                    "trialId": f"trial-{index}",
                    "stage": stage,
                    "factorId": f"old-{index}",
                    "evaluationKey": f"evaluation-{index}",
                    "aliasOf": "",
                    "countedForPenalty": True,
                }
                for index, stage in enumerate(
                    ["MINING_REVEALED", "ADMISSION_REVEALED", "STRATEGY_REVEALED"]
                )
            ]
            trials.append(
                {
                    "trialId": "declared-new1",
                    "stage": "DECLARED",
                    "factorId": "new1",
                    "evaluationKey": "",
                    "aliasOf": "",
                    "countedForPenalty": False,
                }
            )
            input_files = [
                {
                    "path": str(path.relative_to(root)),
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                }
                for path in code_paths.values()
            ]
            payload = {
                "schemaVersion": "profit_trial_history_audit_v2",
                "status": "PASS",
                "policy": {
                    field: hashlib.sha256(path.read_bytes()).hexdigest()
                    for field, path in code_paths.items()
                },
                "summary": {
                    "manifestCount": 1,
                    "trialRecordCount": 4,
                    "countedTrialRecordCount": 3,
                    "declaredRevealedDecisionOpportunityFloor": 3,
                    "unresolvedIssueCount": 0,
                },
                "runs": [{"runId": "historical-run"}],
                "trials": trials,
                "issues": [],
                "inputs": {
                    "fileCount": len(input_files),
                    "fingerprint": canonical_sha256(input_files),
                    "files": input_files,
                },
                "derivedArtifacts": derived,
            }
            payload["auditId"] = audit_id(payload)
            audit_path = audit_dir / "summary.json"
            audit_path.write_text(json.dumps(payload), encoding="utf-8")
            digest = hashlib.sha256(audit_path.read_bytes()).hexdigest()
            settings = {
                "matrix_trials": 2,
                "total_trials": 7,
                "planned_new_trials": 4,
                "planned_new_trial_ids": ["new1"],
                "planned_new_evaluations": {
                    "admission": 1,
                    "formal_matrix": 2,
                    "factor_only": 1,
                },
                "historical_trial_audit": str(audit_path),
                "historical_trial_audit_sha256": digest,
            }

            evidence, resolved = _verify_historical_trial_audit(
                root,
                settings,
                ["new1"],
            )
            self.assertEqual(evidence["status"], "PASS")
            self.assertEqual(evidence["historicalTrialFloor"], 3)
            self.assertEqual(evidence["minimumDeclaredTrials"], 7)
            self.assertEqual(resolved, audit_path.resolve())

            underdeclared, _ = _verify_historical_trial_audit(
                root,
                {**settings, "total_trials": 6},
                ["new1"],
            )
            self.assertEqual(underdeclared["status"], "FAIL")
            self.assertFalse(underdeclared["countSufficient"])

            wrong_hash, _ = _verify_historical_trial_audit(
                root,
                {**settings, "historical_trial_audit_sha256": "0" * 64},
                ["new1"],
            )
            self.assertEqual(wrong_hash["status"], "FAIL")
            self.assertFalse(wrong_hash["shaMatches"])

            wrong_ids, _ = _verify_historical_trial_audit(
                root,
                settings,
                ["different-candidate"],
            )
            self.assertEqual(wrong_ids["status"], "FAIL")
            self.assertFalse(wrong_ids["plannedIdsValid"])

            wrong_breakdown, _ = _verify_historical_trial_audit(
                root,
                {
                    **settings,
                    "planned_new_evaluations": {
                        "admission": 1,
                        "formal_matrix": 1,
                        "factor_only": 1,
                    },
                },
                ["new1"],
            )
            self.assertEqual(wrong_breakdown["status"], "FAIL")
            self.assertFalse(wrong_breakdown["plannedBreakdownValid"])

            forged_path = audit_dir / "forged.json"
            forged_path.write_text(
                json.dumps(
                    {
                        "schemaVersion": "profit_trial_history_audit_v2",
                        "status": "PASS",
                        "summary": {"declaredRevealedDecisionOpportunityFloor": 3},
                    }
                ),
                encoding="utf-8",
            )
            forged, _ = _verify_historical_trial_audit(
                root,
                {
                    **settings,
                    "historical_trial_audit": str(forged_path),
                    "historical_trial_audit_sha256": hashlib.sha256(
                        forged_path.read_bytes()
                    ).hexdigest(),
                },
                ["new1"],
            )
            self.assertEqual(forged["status"], "FAIL")
            self.assertFalse(forged["auditIdMatches"])

            code_paths["entrypointSha256"].write_text(
                "entrypoint changed after audit",
                encoding="utf-8",
            )
            drifted, _ = _verify_historical_trial_audit(
                root,
                settings,
                ["new1"],
            )
            self.assertEqual(drifted["status"], "FAIL")
            self.assertFalse(drifted["actualInputsMatch"])

    def test_failed_historical_audit_stops_before_factor_cache_or_backtest(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            factor_cache = root / "cache" / "candidate.h5"
            factor_cache.parent.mkdir(parents=True)
            factor_cache.write_bytes(b"unrevealed-candidate-values")
            factor_library = root / "data" / "factorlib" / "candidate.json"
            factor_library.parent.mkdir(parents=True)
            factor_library.write_text(
                json.dumps(
                    {
                        "factors": {
                            "new1": {
                                "factor_id": "new1",
                                "factor_expression": "RANK($close)",
                                "cache_location": {
                                    "result_h5_path": str(factor_cache)
                                },
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            provider = root / "provider"
            provider.mkdir()
            backtest_path = root / "configs" / "backtest.yaml"
            backtest_path.parent.mkdir(parents=True)
            backtest_path.write_text(
                """
data:
  provider_uri: provider
  market: csi300
dataset:
  label: Ref($close, -22) / Ref($close, -1) - 1
  segments: {}
""".strip(),
                encoding="utf-8",
            )
            config_path = root / "configs" / "validation.yaml"
            config_path.write_text(
                f"""
protocol_version: "2.9"
run_id: blocked-history
output_root: output
factor_library: {factor_library.relative_to(root)}
historical_provider: provider
recent_provider: provider
backtest_config: {backtest_path.relative_to(root)}
stop_on_audit_failure: false
trial_matrix:
  enabled: true
validation:
  market: csi300
  label: Ref($close, -22) / Ref($close, -1) - 1
  admission_factor_ids: [new1]
promotion:
  multiple_testing:
    enabled: true
    required: true
    historical_trial_audit: missing-audit.json
""".strip(),
                encoding="utf-8",
            )
            for relative in (
                "quantaalpha/validation/profit.py",
                "quantaalpha/backtest/factor_loader.py",
                "quantaalpha/backtest/custom_factor_calculator.py",
                "quantaalpha/backtest/runner.py",
                "quantaalpha/factors/coder/expr_parser.py",
                "quantaalpha/factors/coder/function_lib.py",
                "scripts/run_profit_validation.py",
            ):
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(relative, encoding="utf-8")

            import quantaalpha.validation.profit as profit_module

            original_sha256 = profit_module._sha256

            def guarded_sha256(path):
                if Path(path).resolve() == factor_cache.resolve():
                    raise AssertionError("candidate cache was read before history gate")
                return original_sha256(Path(path))

            with (
                patch.dict(os.environ, {"PYTHONHASHSEED": "0"}),
                patch(
                    "quantaalpha.validation.profit.provider_contract_fingerprint",
                    return_value={"fingerprint": "provider-fp", "files": {}},
                ),
                patch(
                    "quantaalpha.validation.profit.audit_data_providers",
                    return_value={
                        "status": "PASS",
                        "checks": [],
                        "blockingReasons": [],
                    },
                ),
                patch(
                    "quantaalpha.validation.profit.audit_segment_embargo",
                    return_value={"status": "PASS"},
                ),
                patch(
                    "quantaalpha.validation.profit._verify_historical_trial_audit",
                    return_value=(
                        {"status": "FAIL", "reason": "frozen audit mismatch"},
                        None,
                    ),
                ),
                patch(
                    "quantaalpha.validation.profit.audit_factor_library"
                ) as factor_audit,
                patch("quantaalpha.validation.profit.BacktestRunner") as runner,
                patch("quantaalpha.validation.profit._sha256", side_effect=guarded_sha256),
            ):
                result = run_profit_validation(root, config_path, audit_only=False)

            self.assertEqual(result["decision"]["status"], "BLOCKED")
            self.assertEqual(result["factorAudit"], "BLOCKED")
            self.assertEqual(result["candidates"], {})
            factor_audit.assert_not_called()
            runner.assert_not_called()

    def test_failed_historical_trial_audit_blocks_multiple_testing(self):
        result = _multiple_testing_audit(
            {},
            {
                "enabled": True,
                "required": True,
                "historicalTrialEvidence": {
                    "status": "FAIL",
                    "reason": "declared trial floor is too low",
                },
            },
        )

        self.assertEqual(result["status"], "BLOCKED")
        self.assertIn("too low", result["reason"])

    def test_dsr_is_deterministic_and_marks_missing_trials_limited(self):
        rng = np.random.default_rng(42)
        trial_returns = pd.DataFrame(
            {
                f"trial_{index}": rng.normal(index * 0.00001, 0.01, 504)
                for index in range(3)
            }
        )
        target = pd.Series(rng.normal(0.001, 0.01, 504))

        first = deflated_sharpe_ratio(target, trial_returns, total_trials=15)
        second = deflated_sharpe_ratio(target, trial_returns, total_trials=15)

        self.assertEqual(first["status"], "LIMITED")
        self.assertEqual(first["observedTrialReturns"], 3)
        self.assertEqual(first["requiredObservedTrials"], 15)
        self.assertAlmostEqual(first["probability"], second["probability"], places=15)
        self.assertGreaterEqual(first["probability"], 0.0)
        self.assertLessEqual(first["probability"], 1.0)

    def test_dsr_can_require_complete_matrix_and_penalize_more_declared_trials(self):
        rng = np.random.default_rng(21)
        trial_returns = pd.DataFrame(
            {
                f"trial_{index}": rng.normal(index * 0.00001, 0.01, 504)
                for index in range(4)
            }
        )
        target = trial_returns["trial_3"]

        result = deflated_sharpe_ratio(
            target,
            trial_returns,
            total_trials=15,
            minimum_observed_trials=4,
        )

        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["declaredTrials"], 15)
        self.assertEqual(result["requiredObservedTrials"], 4)
        self.assertEqual(result["observedTrialReturns"], 4)
        self.assertTrue(result["evidenceComplete"])

    def test_dsr_expected_maximum_is_centered_at_zero_skill(self):
        rng = np.random.default_rng(7)
        trial_returns = pd.DataFrame(
            {
                f"trial_{index}": rng.normal(0.001 + index * 0.0001, 0.01, 504)
                for index in range(4)
            }
        )
        target = trial_returns["trial_3"]
        result = deflated_sharpe_ratio(target, trial_returns, total_trials=4)
        normal = NormalDist()
        max_z = (
            (1.0 - 0.5772156649015329) * normal.inv_cdf(1.0 - 1.0 / 4)
            + 0.5772156649015329
            * normal.inv_cdf(1.0 - 1.0 / (4 * math.e))
        )

        self.assertAlmostEqual(
            result["dailyExpectedMaximumSharpe"],
            result["trialSharpeStd"] * max_z,
            places=15,
        )

    def test_pbo_is_zero_for_stably_ordered_strategies(self):
        days = 160
        common_noise = np.sin(np.arange(days)) * 0.001
        trial_returns = pd.DataFrame(
            {
                f"strategy_{index}": common_noise + index * 0.0001
                for index in range(12)
            }
        )

        result = probability_of_backtest_overfitting(trial_returns)

        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["combinations"], 12870)

    def test_pbo_drops_only_earliest_remainder_dates(self):
        dates = pd.date_range("2022-01-03", periods=163, freq="B")
        noise = np.sin(np.arange(len(dates))) * 0.001
        trial_returns = pd.DataFrame(
            {
                f"strategy_{index}": noise + index * 0.0001
                for index in range(12)
            },
            index=dates,
        )

        result = probability_of_backtest_overfitting(trial_returns)

        self.assertEqual(result["droppedDays"], 3)
        self.assertEqual(result["usedDateRange"][0], dates[3].isoformat())
        self.assertEqual(result["usedDateRange"][1], dates[-1].isoformat())
        self.assertEqual(result["droppedDateRange"][1], dates[2].isoformat())
        self.assertEqual(result["probability"], 0.0)

    def test_pbo_rejects_too_few_strategy_series(self):
        returns = pd.DataFrame(np.ones((160, 3)), columns=["a", "b", "c"])
        result = probability_of_backtest_overfitting(returns)
        self.assertEqual(result["status"], "INSUFFICIENT")
        self.assertEqual(result["strategies"], 3)


class StrictTrialMatrixTests(unittest.TestCase):
    @staticmethod
    def _write_daily(path: Path, dates: pd.DatetimeIndex, excess: np.ndarray) -> None:
        benchmark = np.full(len(dates), 0.001)
        cost = np.full(len(dates), 0.0002)
        portfolio = excess + benchmark + cost
        pd.DataFrame(
            {
                "date": dates,
                "portfolio_return": portfolio,
                "benchmark_return": benchmark,
                "cost": cost,
                "daily_excess_return": excess,
            }
        ).to_csv(path, index=False)

    def test_matrix_requires_exact_dates_and_writes_incremental_returns(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dates = pd.date_range("2022-01-04", periods=8, freq="B")
            baseline_path = root / "b0.csv"
            trial_path = root / "t1.csv"
            self._write_daily(baseline_path, dates, np.full(len(dates), 0.001))
            self._write_daily(trial_path, dates, np.full(len(dates), 0.0015))
            candidates = {
                "B0": {"status": "completed", "dailyPath": str(baseline_path)},
                "T01": {"status": "completed", "dailyPath": str(trial_path)},
            }
            plan = {
                "baselineCandidate": "B0",
                "targetTrialId": "T01",
                "trials": [{"trialId": "T01", "factorId": "factor-1"}],
            }

            matrix, manifest = build_strict_trial_matrix(candidates, plan, root / "out")

            self.assertEqual(list(matrix.columns), ["T01"])
            self.assertTrue(np.allclose(matrix["T01"], 0.0005))
            self.assertEqual(manifest["status"], "PASS")
            self.assertTrue(Path(manifest["path"]).is_file())

    def test_matrix_rejects_silent_date_intersection(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dates = pd.date_range("2022-01-04", periods=8, freq="B")
            baseline_path = root / "b0.csv"
            trial_path = root / "t1.csv"
            self._write_daily(baseline_path, dates, np.full(len(dates), 0.001))
            self._write_daily(trial_path, dates[1:], np.full(len(dates) - 1, 0.0015))
            candidates = {
                "B0": {"status": "completed", "dailyPath": str(baseline_path)},
                "T01": {"status": "completed", "dailyPath": str(trial_path)},
            }
            plan = {
                "baselineCandidate": "B0",
                "targetTrialId": "T01",
                "trials": [{"trialId": "T01", "factorId": "factor-1"}],
            }

            with self.assertRaisesRegex(ValueError, "dates do not match"):
                build_strict_trial_matrix(candidates, plan)


if __name__ == "__main__":
    unittest.main()
