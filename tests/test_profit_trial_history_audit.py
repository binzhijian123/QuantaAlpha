import csv
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from quantaalpha.validation.trial_history import audit_id, file_sha256
from scripts.audit_profit_trial_history import (
    collect_trial_history,
    write_trial_history_audit,
)


class ProfitTrialHistoryAuditTests(unittest.TestCase):
    @staticmethod
    def _write_json(path: Path, payload: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload), encoding="utf-8")

    @staticmethod
    def _return_text(rows: list[tuple[str, float, float, float]]) -> str:
        lines = [
            "date,portfolio_return,benchmark_return,cost,daily_excess_return,cumulative_excess_return"
        ]
        cumulative = 0.0
        for date, portfolio, benchmark, cost in rows:
            excess = portfolio - benchmark - cost
            cumulative += excess
            lines.append(
                f"{date},{portfolio},{benchmark},{cost},{excess},{cumulative}"
            )
        return "\n".join(lines) + "\n"

    @classmethod
    def _write_result(
        cls,
        run_dir: Path,
        candidate: str,
        rows: list[tuple[str, float, float, float]],
    ) -> tuple[Path, Path]:
        result_dir = run_dir / candidate
        result_dir.mkdir(parents=True, exist_ok=True)
        prefix = candidate.lower()
        metrics_path = result_dir / f"{prefix}_backtest_metrics.json"
        cls._write_json(
            metrics_path,
            {
                "experiment_name": f"test_{candidate}",
                "factor_source": "alpha158_20" if candidate == "B0" else "combined",
                "num_factors": 20 if candidate == "B0" else 21,
                "metrics": {"information_ratio": 0.1},
                "config": {
                    "test_range": "2022-01-03 ~ 2022-01-04",
                    "market": "csi300",
                    "benchmark": "SH000300",
                    "effective_strategy": {
                        "class": "TopkDropoutStrategy",
                        "kwargs": {"topk": 50, "n_drop": 1, "hold_thresh": 1},
                    },
                },
            },
        )
        return_path = result_dir / f"{prefix}_cumulative_excess.csv"
        return_path.write_text(cls._return_text(rows), encoding="utf-8")
        return metrics_path, return_path

    @classmethod
    def _build_fixture(cls, root: Path, reconcile_orphan: bool = True) -> dict:
        factorlib = root / "data" / "factorlib"
        configs = root / "configs"
        results = root / "data" / "results" / "profit_validation"
        audit_code = root / "quantaalpha" / "validation" / "trial_history.py"
        verifier_code = root / "quantaalpha" / "validation" / "profit.py"
        function_library = (
            root / "quantaalpha" / "factors" / "coder" / "function_lib.py"
        )
        factor_parser = root / "quantaalpha" / "factors" / "coder" / "expr_parser.py"
        factor_calculator = (
            root
            / "quantaalpha"
            / "backtest"
            / "custom_factor_calculator.py"
        )
        factor_preparation = root / "scripts" / "prepare_profit_factor_library.py"
        entrypoint = root / "scripts" / "audit_profit_trial_history.py"
        audit_code.parent.mkdir(parents=True, exist_ok=True)
        entrypoint.parent.mkdir(parents=True, exist_ok=True)
        audit_code.write_text("# fixture audit code\n", encoding="utf-8")
        verifier_code.write_text("# fixture verifier code\n", encoding="utf-8")
        function_library.parent.mkdir(parents=True, exist_ok=True)
        function_library.write_text(
            "# fixture factor function library\n", encoding="utf-8"
        )
        factor_parser.write_text("# fixture factor parser\n", encoding="utf-8")
        factor_calculator.parent.mkdir(parents=True, exist_ok=True)
        factor_calculator.write_text(
            "# fixture factor calculator\n", encoding="utf-8"
        )
        factor_preparation.write_text(
            "# fixture factor preparation\n", encoding="utf-8"
        )
        entrypoint.write_text("# fixture audit entrypoint\n", encoding="utf-8")
        cls._write_json(
            factorlib / "all_factors_library_a.json",
            {
                "factors": {
                    "a": {
                        "factor_id": "a",
                        "factor_name": "A",
                        "factor_expression": "$close / Ref($close, 1)",
                        "feedback": {"decision": False},
                    },
                    "b": {
                        "factor_id": "b",
                        "factor_name": "B",
                        "factor_expression": "Mean($volume, 5)",
                        "feedback": {"decision": True},
                    },
                }
            },
        )
        cls._write_json(
            configs / "profit_candidates_v1.json",
            {
                "metadata": {"protocol_version": "1"},
                "factors": {
                    "c1": {
                        "factor_id": "c1",
                        "factor_name": "Candidate 1",
                        "factor_expression": "Std($close, 20)",
                    },
                    "c2": {
                        "factor_id": "c2",
                        "factor_name": "Candidate 2",
                        "factor_expression": "Mean($close, 20)",
                    },
                },
            },
        )

        full_run = results / "profit_20260101_000000"
        audit_run = results / "profit_20260102_000000"
        provider = {
            "fingerprint": "provider-fingerprint",
            "files": {
                "calendar": {"sha256": "calendar-sha"},
                "instruments": {"sha256": "instruments-sha"},
            },
        }
        manifest_inputs = {
            "configs/profit_validation_v1.yaml": {"sha256": "selection-sha"},
            "configs/backtest_profit_validation_v1.yaml": {
                "sha256": "backtest-sha"
            },
        }
        for run_dir, audit_only in ((full_run, False), (audit_run, True)):
            cls._write_json(
                run_dir / "manifest.json",
                {
                    "runId": run_dir.name,
                    "protocolVersion": "1",
                    "auditOnly": audit_only,
                    "providers": {"historical": provider},
                    "inputs": manifest_inputs,
                },
            )
            cls._write_json(
                run_dir / "factor_audit.json",
                {
                    "status": "PASS",
                    "admissionFactorIds": ["c1"],
                    "factors": [
                        {
                            "factorId": "c1",
                            "factorName": "Candidate 1",
                            "coverage": 1.0,
                            "validationRankIC": 0.02,
                            "selected": True,
                        }
                    ],
                },
            )

        b0_metrics, _ = cls._write_result(
            full_run,
            "B0",
            [("2022-01-03", 0.01, 0.0, 0.001), ("2022-01-04", 0.0, 0.0, 0.0)],
        )
        t1_metrics, t1_return = cls._write_result(
            full_run,
            "T1_c1",
            [("2022-01-03", 0.02, 0.0, 0.001), ("2022-01-04", -0.01, 0.0, 0.0)],
        )
        with (full_run / "trial_ledger.csv").open(
            "w", encoding="utf-8", newline=""
        ) as file:
            fields = [
                "candidate",
                "factor_id",
                "factor_name",
                "factor_source",
                "alias_of",
                "status",
                "started_at",
                "finished_at",
                "metrics_path",
                "error",
            ]
            writer = csv.DictWriter(file, fieldnames=fields)
            writer.writeheader()
            writer.writerows(
                [
                    {
                        "candidate": "B0",
                        "status": "completed",
                        "metrics_path": str(b0_metrics),
                    },
                    {
                        "candidate": "T1_c1",
                        "factor_id": "c1",
                        "factor_name": "Candidate 1",
                        "status": "completed",
                        "metrics_path": str(t1_metrics),
                    },
                    {
                        "candidate": "C1",
                        "factor_id": "c1",
                        "factor_name": "Candidate 1 alias",
                        "alias_of": "T1_c1",
                        "status": "completed",
                        "metrics_path": str(t1_metrics),
                    },
                ]
            )
        cls._write_json(
            full_run / "trial_plan.json",
            {
                "trials": [
                    {
                        "trialId": "T1_c1",
                        "factorId": "c1",
                        "factorExpressionSha256": hashlib.sha256(
                            "Std($close,20)".encode()
                        ).hexdigest(),
                    }
                ]
            },
        )

        replay_dir = full_run / "T1_c1_replay"
        replay_dir.mkdir(parents=True)
        replay_return = replay_dir / "t1_c1_replay_cumulative_excess.csv"
        replay_return.write_bytes(
            b"\xef\xbb\xbf" + t1_return.read_text(encoding="utf-8").replace("\n", "\r\n").encode()
        )
        replay_metrics = replay_dir / "t1_c1_replay_backtest_metrics.json"
        replay_payload = json.loads(t1_metrics.read_text(encoding="utf-8"))
        replay_payload["experiment_name"] = "test_T1_c1_replay"
        cls._write_json(replay_metrics, replay_payload)
        source_position = t1_return.with_name("t1_c1_position_counts.csv")
        replay_position = replay_dir / "t1_c1_replay_position_counts.csv"
        source_position.write_text("date,count\n2022-01-03,50\n", encoding="utf-8")
        replay_position.write_bytes(source_position.read_bytes())
        replay_program = replay_dir / "replay.py"
        replay_program.write_text("print('deterministic replay')\n", encoding="utf-8")

        orphan_aliases = []
        if reconcile_orphan:
            orphan_aliases.append(
                {
                    "path": str(replay_return.relative_to(root)),
                    "alias_of": str(t1_return.relative_to(root)),
                    "required_raw_sha256": file_sha256(replay_return),
                    "source_metrics_path": str(t1_metrics.relative_to(root)),
                    "replay_metrics_path": str(replay_metrics.relative_to(root)),
                    "source_position_path": str(source_position.relative_to(root)),
                    "replay_position_path": str(replay_position.relative_to(root)),
                    "required_position_sha256": file_sha256(source_position),
                    "replay_program_path": str(replay_program.relative_to(root)),
                    "required_replay_program_sha256": file_sha256(replay_program),
                    "reason": "fixture deterministic replay",
                }
            )
        cls._write_json(
            configs / "profit_trial_history_reconciliation_v1.json",
            {
                "schema_version": "profit_trial_history_reconciliation_v1",
                "created_before_next_candidate": True,
                "orphan_return_aliases": orphan_aliases,
                "incomplete_trials": [],
            },
        )
        return {
            "replayReturn": replay_return,
            "t1Metrics": t1_metrics,
            "auditCode": audit_code,
            "entrypoint": entrypoint,
        }

    def test_strict_audit_builds_crosswalk_and_semantically_deduplicates_replay(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixture = self._build_fixture(root)

            payload, _ = collect_trial_history(
                root,
                fixture["entrypoint"],
                fixture["auditCode"],
            )
            summary = payload["summary"]

            self.assertEqual(payload["status"], "PASS")
            self.assertEqual(summary["penaltyStageCounts"]["MINING_REVEALED"], 2)
            self.assertEqual(summary["uniqueNormalizedMiningHypothesisCount"], 2)
            self.assertEqual(summary["penaltyStageCounts"]["ADMISSION_REVEALED"], 1)
            self.assertEqual(summary["penaltyStageCounts"]["STRATEGY_REVEALED"], 1)
            self.assertEqual(summary["declaredRevealedDecisionOpportunityFloor"], 4)
            self.assertEqual(summary["minimumDeclaredTrialsForOneAdditionalReveal"], 5)
            self.assertEqual(summary["orphanReturnCount"], 1)
            self.assertEqual(summary["reconciledOrphanReturnCount"], 1)
            self.assertEqual(summary["unresolvedIssueCount"], 0)
            self.assertEqual(summary["stageCounts"]["DECLARED"], 1)

            output_dir = root / "audit_output"
            result = write_trial_history_audit(
                root,
                output_dir,
                fixture["entrypoint"],
                fixture["auditCode"],
            )
            self.assertEqual(result["status"], "PASS")
            written = json.loads((output_dir / "summary.json").read_text())
            self.assertEqual(written["auditId"], audit_id(written))
            for artifact in written["derivedArtifacts"].values():
                self.assertEqual(
                    artifact["sha256"],
                    file_sha256(output_dir / artifact["path"]),
                )
            second_output = root / "audit_output_2"
            second = write_trial_history_audit(
                root,
                second_output,
                fixture["entrypoint"],
                fixture["auditCode"],
            )
            self.assertEqual(second["auditId"], result["auditId"])
            self.assertEqual(
                (second_output / "summary.json").read_bytes(),
                (output_dir / "summary.json").read_bytes(),
            )
            with self.assertRaises(FileExistsError):
                write_trial_history_audit(
                    root,
                    output_dir,
                    fixture["entrypoint"],
                    fixture["auditCode"],
                )

    def test_unreconciled_orphan_blocks_authoritative_audit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixture = self._build_fixture(root, reconcile_orphan=False)

            payload, _ = collect_trial_history(
                root,
                fixture["entrypoint"],
                fixture["auditCode"],
            )

            self.assertEqual(payload["status"], "BLOCKED")
            self.assertEqual(payload["summary"]["unresolvedIssueCount"], 1)
            self.assertIn(
                "UNRESOLVED_ORPHAN_RETURN",
                {issue["code"] for issue in payload["issues"]},
            )

    def test_invalid_same_run_alias_blocks_audit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixture = self._build_fixture(root)
            ledger = next(root.glob("data/results/profit_validation/profit_*/trial_ledger.csv"))
            with ledger.open(encoding="utf-8") as file:
                rows = list(csv.DictReader(file))
            rows[-1]["metrics_path"] = rows[0]["metrics_path"]
            with ledger.open("w", encoding="utf-8", newline="") as file:
                writer = csv.DictWriter(file, fieldnames=list(rows[0]))
                writer.writeheader()
                writer.writerows(rows)

            payload, _ = collect_trial_history(
                root,
                fixture["entrypoint"],
                fixture["auditCode"],
            )

            self.assertEqual(payload["status"], "BLOCKED")
            self.assertIn(
                "INVALID_LEDGER_ALIAS",
                {issue["code"] for issue in payload["issues"]},
            )

    def test_duplicate_manifest_run_id_blocks_audit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixture = self._build_fixture(root)
            source = next(
                root.glob(
                    "data/results/profit_validation/profit_20260101_000000/manifest.json"
                )
            )
            duplicate = (
                root
                / "data"
                / "results"
                / "profit_validation"
                / "profit_20260103_000000"
                / "manifest.json"
            )
            duplicate.parent.mkdir(parents=True)
            duplicate.write_bytes(source.read_bytes())

            payload, _ = collect_trial_history(
                root,
                fixture["entrypoint"],
                fixture["auditCode"],
            )

            self.assertEqual(payload["status"], "BLOCKED")
            self.assertIn(
                "DUPLICATE_RUN_ID",
                {issue["code"] for issue in payload["issues"]},
            )

    def test_protocol_2_9_candidate_identity_is_deterministic(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixture = self._build_fixture(root)
            namespace = "profit-v2.9-test"
            factor_name = "LowVolatility36MMonthlyProxy"
            expression = "-MONTHLY_RETURN_VOLATILITY($close, 36, 36)"
            factor_id = hashlib.sha256(
                f"{namespace}:{factor_name}:{expression}".encode("utf-8")
            ).hexdigest()[:16]
            registry_path = root / "configs" / "profit_candidates_v2_9_test.json"
            self._write_json(
                registry_path,
                {
                    "metadata": {
                        "protocol_version": "2.9",
                        "factor_id_namespace": namespace,
                        "admission_factor_ids": [factor_id],
                    },
                    "factors": {
                        factor_id: {
                            "factor_id": factor_id,
                            "factor_name": factor_name,
                            "factor_expression": expression,
                        }
                    },
                },
            )

            valid, _ = collect_trial_history(
                root,
                fixture["entrypoint"],
                fixture["auditCode"],
            )
            self.assertEqual(valid["status"], "PASS")

            payload = json.loads(registry_path.read_text(encoding="utf-8"))
            payload["factors"][factor_id]["factor_id"] = "forged-id"
            self._write_json(registry_path, payload)
            invalid, _ = collect_trial_history(
                root,
                fixture["entrypoint"],
                fixture["auditCode"],
            )
            self.assertEqual(invalid["status"], "BLOCKED")
            self.assertIn(
                "CANDIDATE_IDENTITY",
                {issue["code"] for issue in invalid["issues"]},
            )


if __name__ == "__main__":
    unittest.main()
