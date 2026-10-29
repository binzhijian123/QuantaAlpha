from __future__ import annotations

import csv
import hashlib
import json
import math
import struct
from collections import defaultdict
from pathlib import Path
from typing import Any


AUDIT_SCHEMA = "profit_trial_history_audit_v2"
RECONCILIATION_SCHEMA = "profit_trial_history_reconciliation_v1"
RETURN_COLUMNS = (
    "date",
    "portfolio_return",
    "benchmark_return",
    "cost",
    "daily_excess_return",
    "cumulative_excess_return",
)


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_sha256(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def audit_id(payload: dict[str, Any]) -> str:
    material = dict(payload)
    material.pop("auditId", None)
    return canonical_sha256(material)


def _load_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("JSON root must be an object")
    return payload


def _factor_items(payload: dict[str, Any]) -> list[dict[str, Any]]:
    factors = payload.get("factors") or {}
    if isinstance(factors, dict):
        return [item for item in factors.values() if isinstance(item, dict)]
    if isinstance(factors, list):
        return [item for item in factors if isinstance(item, dict)]
    return []


def _normalize_expression(value: Any) -> str:
    return "".join(str(value or "").split())


def _protocol_at_least(value: Any, minimum: tuple[int, int]) -> bool:
    try:
        parts = tuple(int(item) for item in str(value).split(".")[:2])
    except ValueError:
        return False
    return (parts + (0, 0))[:2] >= minimum


def _finite(value: Any) -> bool:
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def _canonical_float(value: float) -> bytes:
    normalized = 0.0 if value == 0.0 else float(value)
    return struct.pack(">d", normalized)


def _semantic_return(path: Path) -> tuple[dict[str, Any], list[str], list[float]]:
    raw_sha = file_sha256(path)
    full_digest = hashlib.sha256()
    daily_digest = hashlib.sha256()
    dates: list[str] = []
    daily_excess: list[float] = []
    running_cumulative = 0.0
    with path.open("r", encoding="utf-8-sig", newline="") as file:
        reader = csv.DictReader(file)
        if tuple(reader.fieldnames or ()) != RETURN_COLUMNS:
            raise ValueError("unexpected cumulative-return columns")
        for row_number, row in enumerate(reader, start=2):
            date = str(row.get("date") or "")
            if not date or (dates and date <= dates[-1]):
                raise ValueError(f"dates are missing, duplicated, or unsorted at row {row_number}")
            values: list[float] = []
            for column in RETURN_COLUMNS[1:]:
                try:
                    number = float(row[column])
                except (KeyError, TypeError, ValueError) as exc:
                    raise ValueError(f"invalid {column} at row {row_number}") from exc
                if not math.isfinite(number):
                    raise ValueError(f"non-finite {column} at row {row_number}")
                values.append(number)
            portfolio, benchmark, cost, excess, cumulative = values
            expected_excess = portfolio - benchmark - cost
            if not math.isclose(excess, expected_excess, rel_tol=1e-10, abs_tol=1e-12):
                raise ValueError(f"daily excess identity failed at row {row_number}")
            running_cumulative += excess
            if not math.isclose(
                cumulative,
                running_cumulative,
                rel_tol=1e-10,
                abs_tol=1e-9,
            ):
                raise ValueError(f"cumulative excess identity failed at row {row_number}")
            date_bytes = date.encode("ascii")
            full_digest.update(struct.pack(">H", len(date_bytes)))
            full_digest.update(date_bytes)
            daily_digest.update(struct.pack(">H", len(date_bytes)))
            daily_digest.update(date_bytes)
            for number in values:
                full_digest.update(_canonical_float(number))
            daily_digest.update(_canonical_float(excess))
            dates.append(date)
            daily_excess.append(excess)
    if not dates:
        raise ValueError("cumulative-return file is empty")
    return (
        {
            "rawSha256": raw_sha,
            "semanticSha256": full_digest.hexdigest(),
            "dailySemanticSha256": daily_digest.hexdigest(),
            "rows": len(dates),
            "dateStart": dates[0],
            "dateEnd": dates[-1],
        },
        dates,
        daily_excess,
    )


def _incremental_sha256(
    dates: list[str],
    values: list[float],
    baseline_dates: list[str],
    baseline_values: list[float],
) -> str:
    if dates != baseline_dates or len(values) != len(baseline_values):
        raise ValueError("candidate and B0 dates do not align")
    digest = hashlib.sha256()
    for date, value, baseline in zip(dates, values, baseline_values):
        encoded_date = date.encode("ascii")
        digest.update(struct.pack(">H", len(encoded_date)))
        digest.update(encoded_date)
        digest.update(_canonical_float(value - baseline))
    return digest.hexdigest()


def _cumulative_path(metrics_path: Path) -> Path | None:
    suffix = "_backtest_metrics.json"
    if metrics_path.name.endswith(suffix):
        return metrics_path.with_name(
            metrics_path.name[: -len(suffix)] + "_cumulative_excess.csv"
        )
    candidates = sorted(metrics_path.parent.glob("*_cumulative_excess.csv"))
    return candidates[0] if len(candidates) == 1 else None


def collect_strict_trial_history(
    project_root: Path,
    reconciliation_path: Path,
    entrypoint_path: Path,
    audit_code_path: Path | None = None,
) -> tuple[dict[str, Any], dict[str, list[dict[str, Any]]]]:
    project_root = project_root.resolve()
    reconciliation_path = reconciliation_path.resolve()
    entrypoint_path = entrypoint_path.resolve()
    audit_code_path = (audit_code_path or Path(__file__)).resolve()
    input_hashes: dict[str, str] = {}
    issues: list[dict[str, Any]] = []

    def issue(severity: str, code: str, message: str, evidence: str = "") -> None:
        issues.append(
            {
                "severity": severity,
                "code": code,
                "message": message,
                "evidence": evidence,
            }
        )

    def relative(path: Path) -> str:
        resolved = path.resolve()
        try:
            return str(resolved.relative_to(project_root))
        except ValueError as exc:
            raise ValueError(f"path escapes project root: {resolved}") from exc

    def resolve(value: str | Path) -> Path:
        path = Path(value).expanduser()
        resolved = path.resolve() if path.is_absolute() else (project_root / path).resolve()
        relative(resolved)
        return resolved

    def record(path: Path) -> str:
        key = relative(path)
        digest = input_hashes.get(key)
        if digest is None:
            digest = file_sha256(path)
            input_hashes[key] = digest
        return digest

    code_hashes: dict[str, str] = {}
    for label, source_path in (
        ("auditCodeSha256", audit_code_path),
        ("entrypointSha256", entrypoint_path),
        (
            "verifierCodeSha256",
            project_root / "quantaalpha" / "validation" / "profit.py",
        ),
        (
            "factorFunctionLibrarySha256",
            project_root / "quantaalpha" / "factors" / "coder" / "function_lib.py",
        ),
        (
            "factorParserSha256",
            project_root / "quantaalpha" / "factors" / "coder" / "expr_parser.py",
        ),
        (
            "factorCalculatorSha256",
            project_root
            / "quantaalpha"
            / "backtest"
            / "custom_factor_calculator.py",
        ),
        (
            "factorPreparationSha256",
            project_root / "scripts" / "prepare_profit_factor_library.py",
        ),
    ):
        try:
            code_hashes[label] = record(source_path)
        except (OSError, ValueError) as exc:
            issue("BLOCKING", "AUDIT_CODE_UNAVAILABLE", str(exc))
            code_hashes[label] = ""

    try:
        reconciliation = _load_json(reconciliation_path)
        reconciliation_sha = record(reconciliation_path)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        reconciliation = {}
        reconciliation_sha = ""
        issue("BLOCKING", "RECONCILIATION_UNAVAILABLE", str(exc))
    if reconciliation.get("schema_version") != RECONCILIATION_SCHEMA:
        issue("BLOCKING", "RECONCILIATION_SCHEMA", "reconciliation schema does not match")
    if reconciliation.get("created_before_next_candidate") is not True:
        issue("BLOCKING", "RECONCILIATION_TIMING", "reconciliation was not frozen before the next candidate")

    trials: list[dict[str, Any]] = []
    factor_contracts: dict[str, dict[str, str]] = {}
    seen_factor_ids: set[str] = set()
    library_paths = sorted(
        (project_root / "data" / "factorlib").glob("all_factors_library*.json")
    )
    for path in library_paths:
        try:
            payload = _load_json(path)
            source_sha = record(path)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            issue("BLOCKING", "MINING_LIBRARY_INVALID", str(exc), relative(path))
            continue
        for factor in _factor_items(payload):
            factor_id = str(factor.get("factor_id") or "")
            expression = _normalize_expression(factor.get("factor_expression"))
            feedback = factor.get("feedback") or {}
            if not factor_id or not expression or factor_id in seen_factor_ids:
                issue("BLOCKING", "MINING_FACTOR_IDENTITY", "missing or duplicate mined factor identity", relative(path))
                continue
            seen_factor_ids.add(factor_id)
            expression_sha = hashlib.sha256(expression.encode("utf-8")).hexdigest()
            hypothesis_key = canonical_sha256(
                {"expressionSha256": expression_sha, "stage": "MINING"}
            )
            revealed = isinstance(feedback, dict) and "decision" in feedback
            if not revealed:
                issue("BLOCKING", "MINING_OUTCOME_MISSING", f"mined factor {factor_id} has no feedback decision", relative(path))
            factor_contracts[factor_id] = {
                "expressionSha256": expression_sha,
                "hypothesisKey": hypothesis_key,
            }
            trials.append(
                {
                    "trialId": f"MINING:{factor_id}",
                    "stage": "MINING_REVEALED" if revealed else "DECLARED",
                    "runId": "",
                    "factorId": factor_id,
                    "factorName": str(factor.get("factor_name") or ""),
                    "hypothesisKey": hypothesis_key,
                    "evaluationKey": canonical_sha256(
                        {
                            "factorId": factor_id,
                            "sourceSha256": source_sha,
                            "feedbackDecision": feedback.get("decision"),
                        }
                    ),
                    "providerFingerprint": "",
                    "selectionContractSha256": "",
                    "strategyContractSha256": "",
                    "returnRawSha256": "",
                    "returnSemanticSha256": "",
                    "incrementalSemanticSha256": "",
                    "aliasOf": "",
                    "countedForPenalty": revealed,
                    "countedForPbo": False,
                    "evidencePaths": [relative(path)],
                    "reason": "LLM/mining feedback outcome was revealed.",
                }
            )

    registered: dict[str, dict[str, Any]] = {}
    registry_paths = sorted((project_root / "configs").glob("profit_candidates_*.json"))
    for path in registry_paths:
        try:
            payload = _load_json(path)
            record(path)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            issue("BLOCKING", "CANDIDATE_REGISTRY_INVALID", str(exc), relative(path))
            continue
        metadata = payload.get("metadata") or {}
        strict_identity = _protocol_at_least(metadata.get("protocol_version"), (2, 9))
        factors_payload = payload.get("factors") or {}
        if strict_identity:
            namespace = str(metadata.get("factor_id_namespace") or "")
            admission_ids = [
                str(item) for item in metadata.get("admission_factor_ids") or []
            ]
            registry_ids = (
                [str(item) for item in factors_payload]
                if isinstance(factors_payload, dict)
                else []
            )
            if not namespace or not registry_ids or admission_ids != registry_ids:
                issue(
                    "BLOCKING",
                    "CANDIDATE_IDENTITY",
                    "protocol 2.9+ requires a namespace and admission IDs matching factor-key order",
                    relative(path),
                )
        factor_entries = (
            list(factors_payload.items())
            if isinstance(factors_payload, dict)
            else [("", item) for item in _factor_items(payload)]
        )
        for registry_key, factor in factor_entries:
            if not isinstance(factor, dict):
                issue(
                    "BLOCKING",
                    "CANDIDATE_IDENTITY",
                    "candidate factor must be an object",
                    relative(path),
                )
                continue
            factor_id = str(factor.get("factor_id") or "")
            raw_expression = str(factor.get("factor_expression") or "")
            expression = _normalize_expression(raw_expression)
            if not factor_id or not expression:
                issue("BLOCKING", "CANDIDATE_IDENTITY", "candidate id/expression is missing", relative(path))
                continue
            if strict_identity:
                factor_name = str(factor.get("factor_name") or "")
                expected_id = hashlib.sha256(
                    (
                        f"{metadata.get('factor_id_namespace')}:"
                        f"{factor_name}:{raw_expression}"
                    ).encode("utf-8")
                ).hexdigest()[:16]
                if (
                    not factor_name
                    or str(registry_key) != factor_id
                    or factor_id != expected_id
                ):
                    issue(
                        "BLOCKING",
                        "CANDIDATE_IDENTITY",
                        f"protocol 2.9+ candidate {factor_id or registry_key} has a non-deterministic identity",
                        relative(path),
                    )
            expression_sha = hashlib.sha256(expression.encode("utf-8")).hexdigest()
            factor_metadata = factor.get("metadata") or {}
            hypothesis_key = canonical_sha256(
                {
                    "expressionSha256": expression_sha,
                    "predictionDirection": factor_metadata.get("prediction_direction"),
                    "stage": "REGISTERED_CANDIDATE",
                }
            )
            prior = registered.get(factor_id)
            if prior and prior["expressionSha256"] != expression_sha:
                issue("BLOCKING", "CANDIDATE_CONFLICT", f"candidate {factor_id} has conflicting expressions", relative(path))
                continue
            registered.setdefault(
                factor_id,
                {
                    "factorName": str(factor.get("factor_name") or ""),
                    "expressionSha256": expression_sha,
                    "hypothesisKey": hypothesis_key,
                    "paths": [],
                },
            )["paths"].append(relative(path))
            factor_contracts.setdefault(
                factor_id,
                {"expressionSha256": expression_sha, "hypothesisKey": hypothesis_key},
            )

    manifest_paths = sorted(
        (project_root / "data" / "results" / "profit_validation").glob(
            "profit_*/manifest.json"
        )
    )
    run_records: list[dict[str, Any]] = []
    run_contexts: dict[str, dict[str, Any]] = {}
    seen_run_ids: set[str] = set()
    admission_keys: dict[str, str] = {}
    referenced_returns: set[Path] = set()
    used_incomplete_overrides: set[tuple[str, str]] = set()
    incomplete_overrides = {
        (str(item.get("run_id") or ""), str(item.get("candidate") or "")): item
        for item in reconciliation.get("incomplete_trials") or []
        if isinstance(item, dict)
    }

    for manifest_path in manifest_paths:
        run_dir = manifest_path.parent.resolve()
        try:
            manifest = _load_json(manifest_path)
            manifest_sha = record(manifest_path)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            issue("BLOCKING", "MANIFEST_INVALID", str(exc), relative(manifest_path))
            continue
        run_id = str(manifest.get("runId") or run_dir.name)
        if run_id in seen_run_ids:
            issue("BLOCKING", "DUPLICATE_RUN_ID", f"duplicate run id {run_id}", relative(manifest_path))
            continue
        seen_run_ids.add(run_id)
        provider_value = (manifest.get("providers") or {}).get("historical") or {}
        provider = provider_value if isinstance(provider_value, dict) else {}
        provider_fingerprint = str(
            provider.get("fingerprint")
            or (provider_value if isinstance(provider_value, str) else "")
        )
        provider_files = {
            key: str((value or {}).get("sha256") or "")
            for key, value in sorted((provider.get("files") or {}).items())
        }
        effective_data_sha = canonical_sha256(
            {"providerFingerprint": provider_fingerprint, "files": provider_files}
        )
        declared_inputs = manifest.get("inputs") or {}
        validation_entries = [
            (key, value)
            for key, value in declared_inputs.items()
            if key.startswith("configs/profit_validation") and key.endswith((".yaml", ".yml"))
        ]
        backtest_entries = [
            (key, value)
            for key, value in declared_inputs.items()
            if key.startswith("configs/backtest_profit_validation") and key.endswith((".yaml", ".yml"))
        ]
        selection_sha = str((validation_entries[0][1] or {}).get("sha256") or "") if validation_entries else ""
        declared_backtest_sha = str((backtest_entries[0][1] or {}).get("sha256") or "") if backtest_entries else ""
        run_record = {
            "runId": run_id,
            "protocolVersion": str(manifest.get("protocolVersion") or ""),
            "auditOnly": bool(manifest.get("auditOnly", False)),
            "manifestPath": relative(manifest_path),
            "manifestSha256": manifest_sha,
            "providerFingerprint": provider_fingerprint,
            "effectiveDataSha256": effective_data_sha,
            "selectionContractSha256": selection_sha,
            "declaredBacktestConfigSha256": declared_backtest_sha,
        }
        run_records.append(run_record)
        context = {
            **run_record,
            "runDir": run_dir,
            "manifest": manifest,
        }
        run_contexts[run_id] = context

        factor_audit_path = run_dir / "factor_audit.json"
        if factor_audit_path.is_file():
            try:
                factor_audit = _load_json(factor_audit_path)
                record(factor_audit_path)
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                issue("BLOCKING", "FACTOR_AUDIT_INVALID", str(exc), relative(factor_audit_path))
                factor_audit = {}
            factor_rows = {
                str(item.get("factorId") or ""): item
                for item in factor_audit.get("factors") or []
                if isinstance(item, dict)
            }
            explicit_admission_ids = factor_audit.get("admissionFactorIds") or []
            legacy_selected_ids = factor_audit.get("selectedFactorIds") or []
            context["selectedFactorIds"] = [str(item) for item in legacy_selected_ids]
            admission_ids = explicit_admission_ids or legacy_selected_ids
            for factor_id_value in admission_ids:
                factor_id = str(factor_id_value)
                factor_row = factor_rows.get(factor_id)
                if factor_id not in registered and factor_id not in factor_contracts:
                    issue("BLOCKING", "UNREGISTERED_ADMISSION", f"admission candidate {factor_id} is not registered", relative(factor_audit_path))
                if factor_row is None:
                    issue("BLOCKING", "ADMISSION_ROW_MISSING", f"admission candidate {factor_id} has no factor row", relative(factor_audit_path))
                    continue
                result_fields = {
                    "coverage": factor_row.get("coverage"),
                    "validationRankIC": factor_row.get("validationRankIC"),
                    "error": factor_row.get("error"),
                    "selected": factor_row.get("selected"),
                    "redundantWith": factor_row.get("redundantWith"),
                    "referenceRedundantWith": factor_row.get("referenceRedundantWith"),
                    "auditStatus": factor_audit.get("status"),
                }
                revealed = _finite(result_fields["coverage"]) or _finite(
                    result_fields["validationRankIC"]
                )
                stage = "ADMISSION_REVEALED" if revealed else "INVALID_REVEALED"
                hypothesis_key = (registered.get(factor_id) or factor_contracts.get(factor_id) or {}).get(
                    "hypothesisKey", canonical_sha256({"factorId": factor_id})
                )
                evaluation_key = canonical_sha256(
                    {
                        "factorId": factor_id,
                        "providerFingerprint": provider_fingerprint,
                        "effectiveDataSha256": effective_data_sha,
                        "selectionContractSha256": selection_sha,
                        "result": result_fields,
                    }
                )
                duplicate_of = admission_keys.get(evaluation_key, "")
                trial_id = f"ADMISSION:{run_id}:{factor_id}"
                if not duplicate_of:
                    admission_keys[evaluation_key] = trial_id
                trials.append(
                    {
                        "trialId": trial_id,
                        "stage": stage,
                        "runId": run_id,
                        "factorId": factor_id,
                        "factorName": str(factor_row.get("factorName") or ""),
                        "hypothesisKey": hypothesis_key,
                        "evaluationKey": evaluation_key,
                        "providerFingerprint": provider_fingerprint,
                        "selectionContractSha256": selection_sha,
                        "strategyContractSha256": "",
                        "returnRawSha256": "",
                        "returnSemanticSha256": "",
                        "incrementalSemanticSha256": "",
                        "aliasOf": duplicate_of,
                        "countedForPenalty": bool(revealed and not duplicate_of),
                        "countedForPbo": False,
                        "evidencePaths": [relative(factor_audit_path)],
                        "reason": (
                            "Coverage or RankIC was revealed."
                            if revealed
                            else "Technical/invalid admission produced no coverage or RankIC."
                        ),
                    }
                )

    strategy_keys: dict[str, str] = {}
    for run_id, context in sorted(run_contexts.items()):
        run_dir: Path = context["runDir"]
        ledger_path = run_dir / "trial_ledger.csv"
        if not ledger_path.is_file():
            continue
        try:
            record(ledger_path)
            with ledger_path.open("r", encoding="utf-8-sig", newline="") as file:
                ledger = list(csv.DictReader(file))
        except (OSError, ValueError) as exc:
            issue("BLOCKING", "LEDGER_INVALID", str(exc), relative(ledger_path))
            continue
        by_candidate: dict[str, dict[str, str]] = {}
        for row in ledger:
            candidate = str(row.get("candidate") or "")
            if not candidate or candidate in by_candidate:
                issue("BLOCKING", "LEDGER_CANDIDATE_IDENTITY", "missing or duplicate ledger candidate", relative(ledger_path))
                continue
            by_candidate[candidate] = {key: str(value or "") for key, value in row.items()}

        plan_path = run_dir / "trial_plan.json"
        plan_by_trial: dict[str, dict[str, Any]] = {}
        plan_by_factor: dict[str, dict[str, Any]] = {}
        if plan_path.is_file():
            try:
                plan = _load_json(plan_path)
                record(plan_path)
                for item in plan.get("trials") or []:
                    if not isinstance(item, dict):
                        continue
                    plan_by_trial[str(item.get("trialId") or "")] = item
                    plan_by_factor[str(item.get("factorId") or "")] = item
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                issue("BLOCKING", "TRIAL_PLAN_INVALID", str(exc), relative(plan_path))

        for candidate, row in by_candidate.items():
            status = row.get("status", "").lower()
            if status not in {"completed", "running"}:
                continue
            if status == "running":
                override = incomplete_overrides.get((run_id, candidate))
                if (
                    not override
                    or override.get("expected_status") != "running"
                    or str(override.get("factor_id") or "") != row.get("factor_id")
                    or (override.get("metrics_must_be_absent") and row.get("metrics_path"))
                ):
                    issue("BLOCKING", "UNRESOLVED_INCOMPLETE_TRIAL", f"running trial {run_id}/{candidate} is not reconciled", relative(ledger_path))
                else:
                    used_incomplete_overrides.add((run_id, candidate))
                trials.append(
                    {
                        "trialId": f"STRATEGY:{run_id}:{candidate}",
                        "stage": "INCOMPLETE_NO_REVEALED_RETURN",
                        "runId": run_id,
                        "factorId": row.get("factor_id", ""),
                        "factorName": row.get("factor_name", ""),
                        "hypothesisKey": "",
                        "evaluationKey": "",
                        "providerFingerprint": context["providerFingerprint"],
                        "selectionContractSha256": context["selectionContractSha256"],
                        "strategyContractSha256": "",
                        "returnRawSha256": "",
                        "returnSemanticSha256": "",
                        "incrementalSemanticSha256": "",
                        "aliasOf": "",
                        "countedForPenalty": False,
                        "countedForPbo": False,
                        "evidencePaths": [relative(ledger_path)],
                        "reason": str((override or {}).get("reason") or "Unresolved incomplete trial."),
                    }
                )

        completed_direct = {
            candidate: row
            for candidate, row in by_candidate.items()
            if row.get("status", "").lower() == "completed" and not row.get("alias_of")
        }
        parsed_results: dict[str, dict[str, Any]] = {}
        for candidate, row in completed_direct.items():
            metrics_value = row.get("metrics_path", "")
            try:
                metrics_path = resolve(metrics_value)
                if not metrics_path.is_file():
                    raise ValueError("metrics file is unavailable")
                metrics = _load_json(metrics_path)
                record(metrics_path)
                cumulative_path = _cumulative_path(metrics_path)
                if cumulative_path is None or not cumulative_path.is_file():
                    raise ValueError("cumulative return file is unavailable")
                relative(cumulative_path)
                record(cumulative_path)
                return_evidence, dates, daily_values = _semantic_return(cumulative_path)
                referenced_returns.add(cumulative_path.resolve())
                parsed_results[candidate] = {
                    "metricsPath": metrics_path,
                    "metrics": metrics,
                    "cumulativePath": cumulative_path.resolve(),
                    "returnEvidence": return_evidence,
                    "dates": dates,
                    "dailyValues": daily_values,
                }
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                issue("BLOCKING", "COMPLETED_RESULT_INVALID", str(exc), f"{run_id}/{candidate}")

        baseline = parsed_results.get("B0")
        if parsed_results and baseline is None:
            issue("BLOCKING", "BASELINE_MISSING", f"completed run {run_id} has no B0 result", relative(ledger_path))

        for candidate, row in by_candidate.items():
            if row.get("status", "").lower() != "completed":
                continue
            alias_of = row.get("alias_of", "")
            trial_id = f"STRATEGY:{run_id}:{candidate}"
            if alias_of:
                target = by_candidate.get(alias_of)
                valid_alias = (
                    target is not None
                    and target.get("status", "").lower() == "completed"
                    and not target.get("alias_of")
                    and target.get("metrics_path") == row.get("metrics_path")
                )
                if not valid_alias:
                    issue("BLOCKING", "INVALID_LEDGER_ALIAS", f"invalid alias {run_id}/{candidate}->{alias_of}", relative(ledger_path))
                trials.append(
                    {
                        "trialId": trial_id,
                        "stage": "STRATEGY_ALIAS",
                        "runId": run_id,
                        "factorId": row.get("factor_id", ""),
                        "factorName": row.get("factor_name", ""),
                        "hypothesisKey": "",
                        "evaluationKey": "",
                        "providerFingerprint": context["providerFingerprint"],
                        "selectionContractSha256": context["selectionContractSha256"],
                        "strategyContractSha256": "",
                        "returnRawSha256": "",
                        "returnSemanticSha256": "",
                        "incrementalSemanticSha256": "",
                        "aliasOf": f"STRATEGY:{run_id}:{alias_of}",
                        "countedForPenalty": False,
                        "countedForPbo": False,
                        "evidencePaths": [relative(ledger_path)],
                        "reason": "Explicit same-run ledger alias.",
                    }
                )
                continue
            result = parsed_results.get(candidate)
            if result is None:
                continue
            metrics = result["metrics"]
            strategy_contract_sha = canonical_sha256(metrics.get("config") or {})
            factor_id = row.get("factor_id", "")
            if not factor_id and candidate != "B0":
                selected_ids = context.get("selectedFactorIds") or []
                factor_id = "|".join(selected_ids) if selected_ids else f"legacy:{candidate}"
            plan_item = plan_by_trial.get(candidate) or plan_by_factor.get(factor_id) or {}
            expression_sha = str(
                plan_item.get("factorExpressionSha256")
                or (factor_contracts.get(factor_id) or {}).get("expressionSha256")
                or ""
            )
            role = "BASELINE" if candidate == "B0" else "FACTOR_ONLY" if candidate == "F1" else "COMBINED"
            incremental_sha = ""
            if candidate != "B0" and baseline is not None:
                try:
                    incremental_sha = _incremental_sha256(
                        result["dates"],
                        result["dailyValues"],
                        baseline["dates"],
                        baseline["dailyValues"],
                    )
                except ValueError as exc:
                    issue("BLOCKING", "INCREMENTAL_RETURN_INVALID", str(exc), f"{run_id}/{candidate}")
            evaluation_key = canonical_sha256(
                {
                    "factorId": factor_id,
                    "expressionSha256": expression_sha,
                    "role": role,
                    "providerFingerprint": context["providerFingerprint"],
                    "effectiveDataSha256": context["effectiveDataSha256"],
                    "strategyContractSha256": strategy_contract_sha,
                    "incrementalSemanticSha256": incremental_sha,
                }
            )
            duplicate_of = strategy_keys.get(evaluation_key, "") if candidate != "B0" else ""
            if candidate != "B0" and not duplicate_of:
                strategy_keys[evaluation_key] = trial_id
            contract = factor_contracts.get(factor_id) or {}
            trials.append(
                {
                    "trialId": trial_id,
                    "stage": "STRATEGY_BASELINE" if candidate == "B0" else "STRATEGY_REVEALED",
                    "runId": run_id,
                    "factorId": factor_id,
                    "factorName": row.get("factor_name", ""),
                    "hypothesisKey": contract.get("hypothesisKey", ""),
                    "evaluationKey": evaluation_key,
                    "providerFingerprint": context["providerFingerprint"],
                    "selectionContractSha256": context["selectionContractSha256"],
                    "strategyContractSha256": strategy_contract_sha,
                    "returnRawSha256": result["returnEvidence"]["rawSha256"],
                    "returnSemanticSha256": result["returnEvidence"]["dailySemanticSha256"],
                    "incrementalSemanticSha256": incremental_sha,
                    "aliasOf": duplicate_of,
                    "countedForPenalty": bool(candidate != "B0" and incremental_sha and not duplicate_of),
                    "countedForPbo": False,
                    "evidencePaths": [
                        relative(result["metricsPath"]),
                        relative(result["cumulativePath"]),
                        relative(ledger_path),
                    ],
                    "reason": (
                        "Baseline is evidence but not a searched strategy."
                        if candidate == "B0"
                        else "Duplicate semantic evaluation of " + duplicate_of
                        if duplicate_of
                        else "Distinct revealed strategy evaluation."
                    ),
                }
            )

    for key in sorted(set(incomplete_overrides) - used_incomplete_overrides):
        issue("BLOCKING", "UNUSED_INCOMPLETE_OVERRIDE", f"incomplete override does not match a running row: {key[0]}/{key[1]}")

    all_returns = {
        path.resolve()
        for path in (project_root / "data" / "results" / "profit_validation").glob(
            "profit_*/**/*_cumulative_excess.csv"
        )
    }
    orphan_paths = sorted(all_returns - referenced_returns)
    orphan_overrides: dict[Path, dict[str, Any]] = {}
    for item in reconciliation.get("orphan_return_aliases") or []:
        if not isinstance(item, dict):
            continue
        try:
            override_path = resolve(str(item.get("path") or ""))
        except ValueError as exc:
            issue("BLOCKING", "ORPHAN_OVERRIDE_PATH", str(exc))
            continue
        if override_path in orphan_overrides:
            issue("BLOCKING", "DUPLICATE_ORPHAN_OVERRIDE", "orphan return is reconciled more than once", relative(override_path))
            continue
        orphan_overrides[override_path] = item
    used_orphan_overrides: set[Path] = set()
    for orphan_path in orphan_paths:
        override = orphan_overrides.get(orphan_path)
        if not override:
            issue("BLOCKING", "UNRESOLVED_ORPHAN_RETURN", "orphan return has no explicit reconciliation", relative(orphan_path))
            continue
        used_orphan_overrides.add(orphan_path)
        try:
            source_path = resolve(str(override.get("alias_of") or ""))
            if source_path not in referenced_returns:
                raise ValueError("orphan alias target is not a referenced direct result")
            orphan_evidence, orphan_dates, orphan_values = _semantic_return(orphan_path)
            source_evidence, source_dates, source_values = _semantic_return(source_path)
            record(orphan_path)
            record(source_path)
            if orphan_evidence["rawSha256"] != str(override.get("required_raw_sha256") or ""):
                raise ValueError("orphan raw SHA does not match reconciliation")
            if orphan_evidence["semanticSha256"] != source_evidence["semanticSha256"]:
                raise ValueError("orphan and source semantic returns differ")
            if orphan_dates != source_dates or orphan_values != source_values:
                raise ValueError("orphan and source daily returns differ")
            source_metrics_path = resolve(str(override.get("source_metrics_path") or ""))
            replay_metrics_path = resolve(str(override.get("replay_metrics_path") or ""))
            source_metrics = _load_json(source_metrics_path)
            replay_metrics = _load_json(replay_metrics_path)
            record(source_metrics_path)
            record(replay_metrics_path)
            for key in ("factor_source", "num_factors", "metrics", "config"):
                if source_metrics.get(key) != replay_metrics.get(key):
                    raise ValueError(f"orphan replay metrics differ in {key}")
            source_position = resolve(str(override.get("source_position_path") or ""))
            replay_position = resolve(str(override.get("replay_position_path") or ""))
            required_position_sha = str(override.get("required_position_sha256") or "")
            if record(source_position) != required_position_sha or record(replay_position) != required_position_sha:
                raise ValueError("orphan position-count SHA does not match")
            replay_program = resolve(str(override.get("replay_program_path") or ""))
            if record(replay_program) != str(override.get("required_replay_program_sha256") or ""):
                raise ValueError("orphan replay program SHA does not match")
            trials.append(
                {
                    "trialId": "REPLAY_ALIAS:" + relative(orphan_path),
                    "stage": "STRATEGY_REPLAY_ALIAS",
                    "runId": orphan_path.parents[1].name,
                    "factorId": "",
                    "factorName": "",
                    "hypothesisKey": "",
                    "evaluationKey": "",
                    "providerFingerprint": "",
                    "selectionContractSha256": "",
                    "strategyContractSha256": canonical_sha256(source_metrics.get("config") or {}),
                    "returnRawSha256": orphan_evidence["rawSha256"],
                    "returnSemanticSha256": orphan_evidence["dailySemanticSha256"],
                    "incrementalSemanticSha256": "",
                    "aliasOf": relative(source_path),
                    "countedForPenalty": False,
                    "countedForPbo": False,
                    "evidencePaths": [relative(orphan_path), relative(replay_program), relative(reconciliation_path)],
                    "reason": str(override.get("reason") or "Explicit deterministic replay alias."),
                }
            )
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            issue("BLOCKING", "INVALID_ORPHAN_RECONCILIATION", str(exc), relative(orphan_path))
    for path in sorted(set(orphan_overrides) - used_orphan_overrides):
        issue("BLOCKING", "UNUSED_ORPHAN_OVERRIDE", "orphan override does not match an orphan return", relative(path))

    for factor_id, candidate in sorted(registered.items()):
        if not any(
            trial["factorId"] == factor_id and trial["stage"] in {"ADMISSION_REVEALED", "INVALID_REVEALED"}
            for trial in trials
        ):
            trials.append(
                {
                    "trialId": f"DECLARED:{factor_id}",
                    "stage": "DECLARED",
                    "runId": "",
                    "factorId": factor_id,
                    "factorName": candidate["factorName"],
                    "hypothesisKey": candidate["hypothesisKey"],
                    "evaluationKey": "",
                    "providerFingerprint": "",
                    "selectionContractSha256": "",
                    "strategyContractSha256": "",
                    "returnRawSha256": "",
                    "returnSemanticSha256": "",
                    "incrementalSemanticSha256": "",
                    "aliasOf": "",
                    "countedForPenalty": False,
                    "countedForPbo": False,
                    "evidencePaths": candidate["paths"],
                    "reason": "Registered candidate with no revealed admission outcome.",
                }
            )

    trials.sort(key=lambda row: row["trialId"])
    run_records.sort(key=lambda row: row["runId"])
    issues.sort(key=lambda row: (row["severity"], row["code"], row["evidence"], row["message"]))
    blocking_count = sum(item["severity"] == "BLOCKING" for item in issues)
    stage_counts: dict[str, int] = defaultdict(int)
    penalty_stage_counts: dict[str, int] = defaultdict(int)
    for trial in trials:
        stage_counts[trial["stage"]] += 1
        if trial["countedForPenalty"]:
            penalty_stage_counts[trial["stage"]] += 1
    counted_trials = sum(bool(trial["countedForPenalty"]) for trial in trials)
    unique_mining_hypotheses = len(
        {
            trial["hypothesisKey"]
            for trial in trials
            if trial["stage"] == "MINING_REVEALED"
        }
    )
    input_files = [
        {"path": path, "sha256": digest}
        for path, digest in sorted(input_hashes.items())
    ]
    input_fingerprint = canonical_sha256(input_files)
    summary = {
        "manifestCount": len(run_records),
        "factorLibraryCount": len(library_paths),
        "candidateRegistryCount": len(registry_paths),
        "trialRecordCount": len(trials),
        "stageCounts": dict(sorted(stage_counts.items())),
        "penaltyStageCounts": dict(sorted(penalty_stage_counts.items())),
        "uniqueNormalizedMiningHypothesisCount": unique_mining_hypotheses,
        "countedTrialRecordCount": counted_trials,
        "declaredRevealedDecisionOpportunityFloor": counted_trials,
        "minimumDeclaredTrialsForOneAdditionalReveal": counted_trials + 1,
        "referencedReturnCount": len(referenced_returns),
        "orphanReturnCount": len(orphan_paths),
        "reconciledOrphanReturnCount": len(used_orphan_overrides),
        "unresolvedIssueCount": blocking_count,
        "warningCount": sum(item["severity"] == "WARNING" for item in issues),
    }
    payload = {
        "schemaVersion": AUDIT_SCHEMA,
        "status": "PASS" if blocking_count == 0 else "BLOCKED",
        "policy": {
            "penaltyUnit": "Each distinct revealed mining outcome, admission evaluation, or formal strategy evaluation is counted once; baselines, invalid/no-result attempts, explicit aliases, deterministic replays, and exact semantic evaluation duplicates are excluded.",
            "strategySemanticIdentity": "factor ID + expression + role + Provider/effective-data contract + effective strategy contract + aligned incremental daily-excess semantic SHA256 versus same-run B0",
            "pboBoundary": "Historical heterogeneous records are not a PBO matrix. PBO remains restricted to the current frozen aligned trial matrix.",
            "trueCountCaveat": "This is a reproducible anti-understatement floor over auditable local evidence, not proof of statistical independence or an exact lifetime search count. Unrecorded historical work can only increase the declared count.",
            "auditCodeSha256": code_hashes["auditCodeSha256"],
            "entrypointSha256": code_hashes["entrypointSha256"],
            "verifierCodeSha256": code_hashes["verifierCodeSha256"],
            "factorFunctionLibrarySha256": code_hashes[
                "factorFunctionLibrarySha256"
            ],
            "factorParserSha256": code_hashes["factorParserSha256"],
            "factorCalculatorSha256": code_hashes["factorCalculatorSha256"],
            "factorPreparationSha256": code_hashes["factorPreparationSha256"],
            "reconciliationSha256": reconciliation_sha,
        },
        "summary": summary,
        "runs": run_records,
        "trials": trials,
        "issues": issues,
        "inputs": {
            "fileCount": len(input_files),
            "fingerprint": input_fingerprint,
            "files": input_files,
        },
    }
    tables = {"runs": run_records, "trials": trials, "issues": issues}
    return payload, tables


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fieldnames = list(rows[0]) if rows else []
    with path.open("w", encoding="utf-8", newline="") as file:
        if not fieldnames:
            return
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    key: json.dumps(value, ensure_ascii=False, sort_keys=True)
                    if isinstance(value, (dict, list))
                    else value
                    for key, value in row.items()
                }
            )


def _report(payload: dict[str, Any]) -> str:
    summary = payload["summary"]
    penalty = summary["penaltyStageCounts"]
    return "\n".join(
        [
            "# Profit trial history audit v2",
            "",
            f"- Status: `{payload['status']}`",
            f"- Mining outcomes counted: `{penalty.get('MINING_REVEALED', 0)}`",
            f"- Admission evaluations counted: `{penalty.get('ADMISSION_REVEALED', 0)}`",
            f"- Formal strategy evaluations counted: `{penalty.get('STRATEGY_REVEALED', 0)}`",
            f"- Declared revealed-decision-opportunity floor: `{summary['declaredRevealedDecisionOpportunityFloor']}`",
            f"- Minimum for one additional revealed evaluation: `{summary['minimumDeclaredTrialsForOneAdditionalReveal']}`",
            f"- Unresolved issues: `{summary['unresolvedIssueCount']}`",
            "",
            "This count is a reproducible anti-understatement floor, not proof that trials are statistically independent or that all lifetime research was recorded.",
            "PBO must still use the current frozen, aligned incremental-return matrix.",
            "",
        ]
    )


def write_strict_trial_history_audit(
    project_root: Path,
    reconciliation_path: Path,
    entrypoint_path: Path,
    output_dir: Path,
    audit_code_path: Path | None = None,
) -> dict[str, Any]:
    project_root = project_root.resolve()
    output_dir = output_dir.resolve()
    try:
        output_dir.relative_to(project_root)
    except ValueError as exc:
        raise ValueError("audit output must stay inside the project root") from exc
    if output_dir.exists():
        raise FileExistsError(f"audit output already exists: {output_dir}")
    payload, tables = collect_strict_trial_history(
        project_root,
        reconciliation_path,
        entrypoint_path,
        audit_code_path,
    )
    output_dir.mkdir(parents=True, exist_ok=False)
    derived_paths = {
        "runs": output_dir / "runs.csv",
        "trials": output_dir / "trials.csv",
        "issues": output_dir / "issues.csv",
        "report": output_dir / "report.md",
    }
    _write_csv(derived_paths["runs"], tables["runs"])
    _write_csv(derived_paths["trials"], tables["trials"])
    _write_csv(derived_paths["issues"], tables["issues"])
    derived_paths["report"].write_text(_report(payload), encoding="utf-8")
    payload["derivedArtifacts"] = {
        key: {"path": path.name, "sha256": file_sha256(path)}
        for key, path in sorted(derived_paths.items())
    }
    payload["auditId"] = audit_id(payload)
    summary_path = output_dir / "summary.json"
    summary_path.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    return {
        "status": payload["status"],
        "auditId": payload["auditId"],
        "outputDir": str(output_dir),
        "summaryPath": str(summary_path),
        "summarySha256": file_sha256(summary_path),
        "summary": payload["summary"],
    }
