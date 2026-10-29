"""Zero-LLM preflight for the QuantaAlpha paper-validation workflow."""

from __future__ import annotations

import hashlib
import json
import os
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd
import yaml

from quantaalpha.pipeline.evolution.controller import EvolutionConfig, EvolutionController
from quantaalpha.pipeline.evolution.trajectory import StrategyTrajectory
from quantaalpha.utils.qlib_provider import (
    calendar_bounds,
    provider_contract_fingerprint,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def resolve_path(value: str | Path) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else PROJECT_ROOT / path


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_yaml(path: Path) -> dict[str, Any]:
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"YAML root must be an object: {path}")
    return payload


def _dotenv_values() -> dict[str, str]:
    path = PROJECT_ROOT / ".env"
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip("'").strip('"')
    return values


def _effective_llm_configuration(
    dotenv: dict[str, str] | None = None,
    environ: dict[str, str] | None = None,
) -> dict[str, str | None]:
    """Resolve credentials with the same precedence as run.sh."""
    project_values = _dotenv_values() if dotenv is None else dotenv
    process_values = os.environ if environ is None else environ
    return {
        "chat_model": project_values.get("CHAT_MODEL")
        or process_values.get("CHAT_MODEL"),
        "api_key": project_values.get("OPENAI_API_KEY")
        or process_values.get("OPENAI_API_KEY"),
        "base_url": project_values.get("OPENAI_BASE_URL")
        or process_values.get("OPENAI_BASE_URL"),
    }


def probe_llm_auth(base_url: str, api_key: str) -> dict[str, Any]:
    """Validate OpenAI-compatible credentials without generating tokens."""
    url = f"{base_url.rstrip('/')}/models"
    request = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {api_key}",
            "User-Agent": "QuantaAlpha-paper-preflight/1.0",
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            status = int(response.status)
        return {"ok": 200 <= status < 300, "httpStatus": status, "endpoint": url}
    except urllib.error.HTTPError as exc:
        return {"ok": False, "httpStatus": int(exc.code), "endpoint": url}
    except Exception as exc:
        return {
            "ok": False,
            "httpStatus": None,
            "endpoint": url,
            "errorType": type(exc).__name__,
        }


def simulate_schedule(experiment_config: dict[str, Any]) -> dict[str, Any]:
    """Exercise the real controller without calling an LLM or running a backtest."""
    planning = experiment_config["planning"]
    evolution = experiment_config["evolution"]
    controller = EvolutionController(
        EvolutionConfig(
            num_directions=int(planning["num_directions"]),
            max_rounds=int(evolution["max_rounds"]),
            mutation_enabled=bool(evolution["mutation_enabled"]),
            crossover_enabled=bool(evolution["crossover_enabled"]),
            crossover_size=int(evolution["crossover_size"]),
            crossover_n=int(evolution["crossover_n"]),
            prefer_diverse_crossover=bool(
                evolution.get("prefer_diverse_crossover", True)
            ),
            parent_selection_strategy=str(
                evolution.get("parent_selection_strategy", "best")
            ),
            paper_fidelity_mode=bool(
                evolution.get("paper_fidelity_mode", False)
            ),
            parallel_enabled=True,
            fresh_start=True,
        )
    )
    # A schedule dry run must never invoke the mutation/crossover LLM prompts.
    controller.mutation_op.generate_mutation_prompt_suffix = lambda parent: "preflight"
    controller.crossover_op.generate_crossover_prompt_suffix = (
        lambda parents: "preflight"
    )
    controller.mutation_op.build_localized_replay_task = (
        lambda parent, strict=True: {
            "contract": "paper_v3_localized_prefix_replay",
            "target_step": "factor_propose",
            "target_step_index": 0,
            "frozen_prefix": [],
            "inherited_checkpoints": {},
            "parent_checkpoint": None,
            "refinement_instruction": "preflight",
            "strategy_suffix": "preflight",
        }
    )
    controller.crossover_op.build_validated_segment_recombination_task = (
        lambda parents, strict=True: {
            "contract": "paper_v3_validated_segment_recombination",
            "parent_ids": [parent.trajectory_id for parent in parents],
            "selected_segments": [],
            "strategy_suffix": "preflight",
        }
    )

    phases: list[str] = []
    task_counts: list[int] = []
    safety_limit = int(evolution["max_rounds"]) + 2
    while not controller.is_complete():
        tasks = controller.get_all_tasks_for_current_phase()
        if not tasks:
            raise RuntimeError(
                f"controller produced no tasks before completion: {controller.get_current_state()}"
            )
        phase = tasks[0]["phase"]
        round_idx = int(tasks[0]["round_idx"])
        if any(task["phase"] != phase or int(task["round_idx"]) != round_idx for task in tasks):
            raise RuntimeError("controller mixed phases or rounds in one parallel batch")
        phases.append(phase.value)
        task_counts.append(len(tasks))

        for index, task in enumerate(tasks):
            parent_ids = [
                trajectory.trajectory_id
                for trajectory in task.get("parent_trajectories", [])
            ]
            trajectory = StrategyTrajectory(
                trajectory_id=f"preflight-{round_idx}-{index}",
                direction_id=int(task["direction_id"]),
                round_idx=round_idx,
                phase=phase,
                hypothesis="preflight",
                backtest_metrics={"RankIC": 0.01 + index / 1000.0},
                parent_ids=parent_ids,
            )
            controller.report_task_complete(task, trajectory)
        controller.advance_phase_after_parallel_completion(tasks)
        if len(phases) > safety_limit:
            raise RuntimeError("controller schedule exceeded its configured round budget")

    return {
        "phaseSequence": phases,
        "taskCounts": task_counts,
        "rounds": len(phases),
        "llmCalls": 0,
    }


class Preflight:
    def __init__(self) -> None:
        self.checks: list[dict[str, Any]] = []

    def check(
        self,
        check_id: str,
        passed: bool,
        *,
        expected: Any,
        actual: Any,
        evidence: str,
        hard: bool = True,
    ) -> None:
        self.checks.append(
            {
                "id": check_id,
                "status": "PASS" if passed else ("FAIL" if hard else "WARN"),
                "hard": hard,
                "expected": expected,
                "actual": actual,
                "evidence": evidence,
            }
        )

    def hard_failures(self) -> list[dict[str, Any]]:
        return [
            check
            for check in self.checks
            if check["hard"] and check["status"] == "FAIL"
        ]

    def warnings(self) -> list[dict[str, Any]]:
        return [check for check in self.checks if check["status"] == "WARN"]


def _check_sources(preflight: Preflight, config: dict[str, Any]) -> None:
    paper = config["sources"]["paper"]
    official = config["sources"]["official_code"]
    for check_id, path_value, expected_hash in (
        ("paper_pdf", paper["path"], paper["sha256"]),
        (
            "official_code_archive",
            official["archive_path"],
            official["archive_sha256"],
        ),
    ):
        path = resolve_path(path_value)
        actual_hash = sha256_file(path) if path.exists() else None
        preflight.check(
            check_id,
            actual_hash == expected_hash,
            expected=expected_hash,
            actual=actual_hash,
            evidence=str(path),
        )


def _check_experiment_profile(
    preflight: Preflight,
    protocol: dict[str, Any],
    replication_assumptions: dict[str, Any],
    experiment_config: dict[str, Any],
    config_path: Path,
) -> dict[str, Any] | None:
    planning = experiment_config.get("planning") or {}
    evolution = experiment_config.get("evolution") or {}
    quality = experiment_config.get("quality_gate") or {}
    factor = experiment_config.get("factor") or {}
    complexity = factor.get("complexity") or {}
    execution = experiment_config.get("execution") or {}
    llm = experiment_config.get("llm") or {}

    comparisons = {
        "planning_directions": (
            protocol["planning_directions"],
            planning.get("num_directions"),
        ),
        "phase_rounds": (
            protocol["expected_phase_rounds"],
            evolution.get("max_rounds"),
        ),
        "factors_per_hypothesis": (
            protocol["factors_per_hypothesis"],
            factor.get("factors_per_hypothesis"),
        ),
        "symbol_length": (
            protocol["complexity"]["symbol_length_threshold"],
            complexity.get("symbol_length_threshold"),
        ),
        "base_features": (
            protocol["complexity"]["base_features_threshold"],
            complexity.get("base_features_threshold"),
        ),
        "free_args_ratio": (
            protocol["complexity"]["free_args_ratio_threshold"],
            complexity.get("free_args_ratio_threshold"),
        ),
        "paper_fidelity_mode": (
            True,
            evolution.get("paper_fidelity_mode"),
        ),
        "steps_per_trajectory": (
            5,
            execution.get("step_n"),
        ),
    }
    if protocol.get("failed_loop_retry_budget") is not None:
        comparisons["failed_loop_retry_budget"] = (
            protocol["failed_loop_retry_budget"],
            execution.get("failed_loop_retry_budget"),
        )
    for check_id, (expected, actual) in comparisons.items():
        preflight.check(
            f"experiment_{check_id}",
            actual == expected,
            expected=expected,
            actual=actual,
            evidence=str(config_path),
        )

    required_gates = {
        "consistency_enabled": True,
        "consistency_require_pass": True,
        "fail_closed_on_error": True,
        "complexity_enabled": True,
        "redundancy_enabled": True,
    }
    actual_gates = {key: quality.get(key) for key in required_gates}
    preflight.check(
        "experiment_quality_gates",
        actual_gates == required_gates,
        expected=required_gates,
        actual=actual_gates,
        evidence=str(config_path),
    )

    if protocol.get("strict_json_mode") is not None:
        strict_runtime = {
            "jsonModeStrict": llm.get("json_mode_strict"),
            "consistencyStrictMode": quality.get(
                "consistency_strict_mode"
            ),
            "exactFactorCount": factor.get(
                "require_exact_factor_count"
            ),
        }
        expected_strict_runtime = {
            "jsonModeStrict": bool(protocol["strict_json_mode"]),
            "consistencyStrictMode": True,
            "exactFactorCount": True,
        }
        preflight.check(
            "experiment_strict_generation",
            strict_runtime == expected_strict_runtime,
            expected=expected_strict_runtime,
            actual=strict_runtime,
            evidence=str(config_path),
        )

    expected_variant_roles = protocol.get("variant_roles")
    if expected_variant_roles is not None:
        variant_contract = factor.get("variant_contract") or {}
        roles = variant_contract.get("roles") or []
        actual_variant_contract = {
            "enabled": variant_contract.get("enabled"),
            "protocolVersion": variant_contract.get(
                "protocol_version"
            ),
            "roles": [
                {
                    "id": role.get("id"),
                    "variationAxis": role.get("variation_axis"),
                }
                for role in roles
                if isinstance(role, dict)
            ],
            "allowedFeatures": variant_contract.get(
                "allowed_features"
            ),
        }
        expected_variant_contract = {
            "enabled": True,
            "protocolVersion": protocol.get(
                "variant_protocol_version"
            ),
            "roles": expected_variant_roles,
            "allowedFeatures": protocol.get("raw_features"),
        }
        preflight.check(
            "experiment_variant_contract",
            actual_variant_contract == expected_variant_contract,
            expected=expected_variant_contract,
            actual=actual_variant_contract,
            evidence=str(config_path),
        )

    try:
        schedule = simulate_schedule(experiment_config)
    except Exception as exc:
        preflight.check(
            "controller_schedule",
            False,
            expected=[
                "original",
                "mutation",
                "crossover",
                "mutation",
                "crossover",
                "mutation",
                "crossover",
                "mutation",
                "crossover",
                "mutation",
                "crossover",
            ],
            actual=str(exc),
            evidence="EvolutionController dry run",
        )
        return None

    expected_sequence = ["original"] + ["mutation", "crossover"] * int(
        protocol["evolution_cycles"]
    )
    preflight.check(
        "controller_schedule",
        schedule["phaseSequence"] == expected_sequence,
        expected=expected_sequence,
        actual=schedule["phaseSequence"],
        evidence="EvolutionController dry run; no LLM calls",
    )
    expected_attempts = int(
        replication_assumptions.get("expected_factor_attempts", 0)
    )
    actual_attempts = sum(schedule["taskCounts"]) * int(
        factor["factors_per_hypothesis"]
    )
    preflight.check(
        "candidate_factor_budget",
        (
            evolution.get("crossover_n")
            == replication_assumptions.get("crossover_children_per_phase")
            and actual_attempts == expected_attempts
        ),
        expected={
            "crossoverChildrenPerPhase": replication_assumptions.get(
                "crossover_children_per_phase"
            ),
            "attemptedFactors": expected_attempts,
        },
        actual={
            "crossoverChildrenPerPhase": evolution.get("crossover_n"),
            "attemptedFactors": actual_attempts,
        },
        evidence=(
            "locked replication assumption; official default and paper's "
            "approximately-150 factor scale"
        ),
    )
    return schedule


def _check_runtime_thresholds(
    preflight: Preflight,
    experiment_config: dict[str, Any],
) -> None:
    from quantaalpha.factors.proposal import AlphaAgentHypothesis2FactorExpression

    factor = experiment_config["factor"]
    complexity = factor["complexity"]
    duplication = factor["duplication"]
    quality = experiment_config["quality_gate"]
    constructor = AlphaAgentHypothesis2FactorExpression(
        consistency_enabled=quality["consistency_enabled"],
        complexity_enabled=quality["complexity_enabled"],
        redundancy_enabled=quality["redundancy_enabled"],
        factors_per_hypothesis=factor["factors_per_hypothesis"],
        generation_max_attempts=factor["generation_max_attempts"],
        require_exact_factor_count=bool(
            factor.get("require_exact_factor_count", False)
        ),
        variant_max_attempts=int(
            factor.get("variant_max_attempts", 2) or 2
        ),
        variant_contract=factor.get("variant_contract") or {},
        consistency_strict_mode=quality["consistency_strict_mode"],
        consistency_require_pass=quality["consistency_require_pass"],
        quality_gate_fail_closed_on_error=quality["fail_closed_on_error"],
        max_correction_attempts=quality["max_correction_attempts"],
        symbol_length_threshold=complexity["symbol_length_threshold"],
        base_features_threshold=complexity["base_features_threshold"],
        free_args_ratio_threshold=complexity["free_args_ratio_threshold"],
        unique_vars_ratio_threshold=complexity["unique_vars_ratio_threshold"],
        duplication_threshold=duplication["threshold"],
        factor_zoo_path=duplication["factor_zoo_path"],
    )
    regulator = constructor.factor_regulator
    actual = {
        "symbolLength": regulator.symbol_length_threshold,
        "baseFeatures": regulator.base_features_threshold,
        "freeArgsRatio": regulator.free_args_ratio_threshold,
        "uniqueVarsRatio": regulator.unique_vars_ratio_threshold,
        "duplication": regulator.duplication_threshold,
        "requireConsistent": constructor.quality_gate.consistency_checker.require_consistent,
        "complexityFailClosed": constructor.quality_gate.complexity_checker.fail_closed,
        "redundancyFailClosed": constructor.quality_gate.redundancy_checker.fail_closed,
        "exactFactorCount": constructor.require_exact_factor_count,
        "variantContractEnabled": constructor._variant_contract_enabled(),
        "variantRoles": [
            role.get("id") for role in constructor._variant_roles()
        ],
    }
    expected = {
        "symbolLength": complexity["symbol_length_threshold"],
        "baseFeatures": complexity["base_features_threshold"],
        "freeArgsRatio": complexity["free_args_ratio_threshold"],
        "uniqueVarsRatio": complexity["unique_vars_ratio_threshold"],
        "duplication": duplication["threshold"],
        "requireConsistent": quality["consistency_require_pass"],
        "complexityFailClosed": quality["fail_closed_on_error"],
        "redundancyFailClosed": quality["fail_closed_on_error"],
        "exactFactorCount": bool(
            factor.get("require_exact_factor_count", False)
        ),
        "variantContractEnabled": bool(
            (factor.get("variant_contract") or {}).get(
                "enabled",
                False,
            )
        ),
        "variantRoles": [
            role.get("id")
            for role in (
                (factor.get("variant_contract") or {}).get("roles")
                or []
            )
            if isinstance(role, dict)
        ],
    }
    preflight.check(
        "runtime_factor_constraints",
        actual == expected,
        expected=expected,
        actual=actual,
        evidence="AlphaAgentHypothesis2FactorExpression runtime instance",
    )


def _check_algorithm_fidelity(preflight: Preflight) -> None:
    """Fail closed until the three paper-specific algorithm contracts exist."""
    from quantaalpha.pipeline.evolution.crossover import CrossoverOperator
    from quantaalpha.pipeline.evolution.mutation import MutationOperator
    from quantaalpha.pipeline.factor_mining import (
        _suppress_unsafe_rdagent_settings_logging,
    )
    from quantaalpha.factors.proposal import (
        AlphaAgentHypothesis2FactorExpression,
    )
    from quantaalpha.llm.config import LLMSettings
    from quantaalpha.validation.factor_pool import select_paper_factor_pool

    secret_log_suppression = _suppress_unsafe_rdagent_settings_logging()
    preflight.check(
        "rdagent_credential_log_suppression",
        secret_log_suppression,
        expected=True,
        actual=secret_log_suppression,
        evidence=(
            "quantaalpha/pipeline/factor_mining.py disables upstream "
            "LITELLM_SETTINGS serialization before any factor worker runs"
        ),
    )

    trajectory_fields = set(StrategyTrajectory.__dataclass_fields__)
    mutation_method = getattr(
        MutationOperator,
        "build_localized_replay_task",
        None,
    )
    mutation_actual = {
        "localizedReplayMethod": callable(mutation_method),
        "structuredStepOutputs": "step_outputs" in trajectory_fields,
    }
    preflight.check(
        "mutation_localized_prefix_replay",
        all(mutation_actual.values()),
        expected={
            "localizedReplayMethod": True,
            "structuredStepOutputs": True,
        },
        actual=mutation_actual,
        evidence=(
            "quantaalpha/pipeline/evolution/mutation.py and trajectory.py; "
            "paper requires locating the failed step, freezing the valid prefix, "
            "and regenerating only the suffix"
        ),
    )

    crossover_method = getattr(
        CrossoverOperator,
        "build_validated_segment_recombination_task",
        None,
    )
    crossover_actual = {
        "segmentRecombinationMethod": callable(crossover_method),
        "structuredStepOutputs": "step_outputs" in trajectory_fields,
    }
    preflight.check(
        "crossover_validated_segment_recombination",
        all(crossover_actual.values()),
        expected={
            "segmentRecombinationMethod": True,
            "structuredStepOutputs": True,
        },
        actual=crossover_actual,
        evidence=(
            "quantaalpha/pipeline/evolution/crossover.py and trajectory.py; "
            "paper requires explicit reuse and recombination of validated "
            "trajectory segments"
        ),
    )

    index = pd.MultiIndex.from_product(
        [
            pd.date_range("2021-01-04", periods=3, freq="B"),
            ["A", "B", "C"],
        ],
        names=["datetime", "instrument"],
    )
    label = pd.Series([0.0, 1.0, 2.0] * 3, index=index)
    panel = pd.DataFrame(
        {
            "alpha": [0.0, 1.0, 2.0] * 3,
            "duplicate": [0.0, 2.0, 4.0] * 3,
        },
        index=index,
    )
    pool_result = select_paper_factor_pool(
        panel,
        label,
        max_fraction=1.0,
        min_cross_section_size=3,
        min_valid_days=3,
    )
    duplicate_decision = next(
        item
        for item in pool_result["decisions"]
        if item["factor"] == "duplicate"
    )
    pool_passed = (
        pool_result["selectedFactors"] == ["alpha"]
        and duplicate_decision["reason"] == "correlation_threshold"
    )
    preflight.check(
        "factor_pool_rankic_correlation_gate",
        pool_passed,
        expected={
            "validationSort": "2021 mean daily cross-sectional RankIC",
            "maxAbsCorrelation": 0.7,
            "maxFactors": 150,
            "maxFractionOfMinedFactors": 0.5,
        },
        actual={
            "method": pool_result["method"],
            "selectedFactors": pool_result["selectedFactors"],
            "duplicateDecision": duplicate_decision["reason"],
        },
        evidence=(
            "quantaalpha/validation/factor_pool.py deterministic runtime smoke"
        ),
    )

    structured_generation = {
        "strictJsonSetting": (
            "structured_json_mode_strict"
            in LLMSettings.model_fields
        ),
        "variantFamilyMethod": callable(
            getattr(
                AlphaAgentHypothesis2FactorExpression,
                "_generate_variant_family_response",
                None,
            )
        ),
    }
    preflight.check(
        "structured_variant_generation",
        all(structured_generation.values()),
        expected={
            "strictJsonSetting": True,
            "variantFamilyMethod": True,
        },
        actual=structured_generation,
        evidence=(
            "quantaalpha/llm/config.py and "
            "quantaalpha/factors/proposal.py"
        ),
    )


def _check_backtest_profile(
    preflight: Preflight,
    protocol: dict[str, Any],
    backtest_config: dict[str, Any],
    config_path: Path,
) -> None:
    dataset = backtest_config.get("dataset") or {}
    backtest = (backtest_config.get("backtest") or {}).get("backtest") or {}
    strategy = (backtest_config.get("backtest") or {}).get("strategy") or {}
    strategy_kwargs = strategy.get("kwargs") or {}
    exchange = backtest.get("exchange_kwargs") or {}
    actual = {
        "market": (backtest_config.get("data") or {}).get("market"),
        "label": dataset.get("label"),
        "segments": dataset.get("segments"),
        "topk": strategy_kwargs.get("topk"),
        "n_drop": strategy_kwargs.get("n_drop"),
        "deal_price": exchange.get("deal_price"),
        "limit_threshold": exchange.get("limit_threshold"),
        "open_cost": exchange.get("open_cost"),
        "close_cost": exchange.get("close_cost"),
        "test": [backtest.get("start_time"), backtest.get("end_time")],
    }
    expected = {
        "market": protocol["market"],
        "label": protocol["label"],
        "segments": protocol["effective_segments"],
        **protocol["strategy"],
        "test": protocol["effective_segments"]["test"],
    }
    preflight.check(
        "backtest_protocol",
        actual == expected,
        expected=expected,
        actual=actual,
        evidence=str(config_path),
    )


def _check_provider_and_factor_data(
    preflight: Preflight,
    config: dict[str, Any],
) -> None:
    protocol = config["paper_protocol"]
    runtime = config["runtime"]
    provider = resolve_path(runtime["provider_uri"]).resolve()
    expected_fingerprint = runtime["provider_fingerprint"]
    try:
        contract = provider_contract_fingerprint(provider, protocol["market"])
        bounds = calendar_bounds(provider)
    except Exception as exc:
        preflight.check(
            "provider_contract",
            False,
            expected=expected_fingerprint,
            actual=str(exc),
            evidence=str(provider),
        )
        return

    preflight.check(
        "provider_contract",
        contract["fingerprint"] == expected_fingerprint,
        expected=expected_fingerprint,
        actual=contract["fingerprint"],
        evidence=str(provider),
    )
    required_end = runtime["factor_data"]["end_time"]
    preflight.check(
        "provider_calendar",
        bool(bounds and bounds[0] <= "2016-01-01" and bounds[1] >= required_end),
        expected=f"coverage through {required_end}",
        actual=bounds,
        evidence=str(provider / "calendars/day.txt"),
    )

    factor_data = runtime["factor_data"]
    data_path = resolve_path(factor_data["path"])
    manifest_path = resolve_path(factor_data["manifest_path"])
    if not data_path.exists() or not manifest_path.exists():
        preflight.check(
            "factor_data_manifest",
            False,
            expected="versioned HDF5 and manifest",
            actual={
                "dataExists": data_path.exists(),
                "manifestExists": manifest_path.exists(),
            },
            evidence=str(manifest_path),
        )
        return

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    actual_hash = sha256_file(data_path)
    expected_fields = list(protocol["raw_features"])
    manifest_provider = (manifest.get("provider") or {}).get("fingerprint")
    manifest_ok = (
        manifest.get("dataSha256") == actual_hash
        and manifest.get("fields") == expected_fields
        and manifest_provider == expected_fingerprint
        and manifest.get("startTime")
        <= protocol["declared_segments"]["train"][0]
        and manifest.get("endTime") >= factor_data["end_time"]
    )
    preflight.check(
        "factor_data_manifest",
        manifest_ok,
        expected={
            "sha256": actual_hash,
            "fields": expected_fields,
            "providerFingerprint": expected_fingerprint,
            "coverage": [
                protocol["declared_segments"]["train"][0],
                factor_data["end_time"],
            ],
        },
        actual={
            "sha256": manifest.get("dataSha256"),
            "fields": manifest.get("fields"),
            "providerFingerprint": manifest_provider,
            "dates": [manifest.get("startTime"), manifest.get("endTime")],
            "rows": manifest.get("rows"),
        },
        evidence=str(manifest_path),
    )

    try:
        panel = pd.read_hdf(data_path, key="data")
        panel_ok = (
            list(panel.columns) == expected_fields
            and list(panel.index.names) == ["datetime", "instrument"]
            and not panel.index.has_duplicates
        )
        panel_actual: Any = {
            "shape": list(panel.shape),
            "columns": list(panel.columns),
            "indexNames": list(panel.index.names),
            "duplicates": int(panel.index.duplicated().sum()),
        }
    except Exception as exc:
        panel_ok = False
        panel_actual = str(exc)
    preflight.check(
        "factor_data_readable",
        panel_ok,
        expected={
            "columns": expected_fields,
            "indexNames": ["datetime", "instrument"],
            "duplicates": 0,
        },
        actual=panel_actual,
        evidence=str(data_path),
    )


def _source_manifest(config: dict[str, Any]) -> dict[str, Any]:
    digest = hashlib.sha256()
    files: dict[str, Any] = {}
    for value in config["runtime"]["critical_source_files"]:
        path = resolve_path(value)
        file_hash = sha256_file(path)
        digest.update(value.encode("utf-8"))
        digest.update(b"\0")
        digest.update(file_hash.encode("ascii"))
        files[value] = {"sha256": file_hash, "size": path.stat().st_size}
    return {"sha256": digest.hexdigest(), "files": files}


def _render_report(result: dict[str, Any]) -> str:
    failed = [check for check in result["checks"] if check["status"] == "FAIL"]
    warnings = [check for check in result["checks"] if check["status"] == "WARN"]
    lines = [
        "# QuantaAlpha 论文 v3 验证预检",
        "",
        f"- 运行：`{result['runId']}`",
        f"- 状态：`{result['status']}`",
        f"- LLM 调用：`{result['llmCalls']}`",
        f"- 硬失败：`{len(failed)}`",
        f"- 声明一致性警告：`{len(warnings)}`",
        "",
        "## 检查结果",
        "",
        "| 检查 | 状态 | 证据 |",
        "| --- | --- | --- |",
    ]
    for check in result["checks"]:
        lines.append(
            f"| `{check['id']}` | `{check['status']}` | {check['evidence']} |"
        )
    lines.extend(
        [
            "",
            "## 结论",
            "",
            result["summary"],
            "",
            "本预检只检查实验启动条件，不证明论文收益有效，也没有消耗 LLM token。",
            "",
        ]
    )
    return "\n".join(lines)


def run_preflight(
    config_path: str | Path,
    *,
    run_id: str | None = None,
) -> dict[str, Any]:
    config_path = resolve_path(config_path).resolve()
    config = load_yaml(config_path)
    protocol = config["paper_protocol"]
    experiment_contract = config.get("experiment_contract") or protocol
    runtime = config["runtime"]
    experiment_path = resolve_path(runtime["experiment_config"]).resolve()
    backtest_path = resolve_path(runtime["backtest_config"]).resolve()
    experiment_config = load_yaml(experiment_path)
    backtest_config = load_yaml(backtest_path)
    preflight = Preflight()

    _check_sources(preflight, config)
    schedule = _check_experiment_profile(
        preflight,
        experiment_contract,
        config.get("replication_assumptions") or {},
        experiment_config,
        experiment_path,
    )
    _check_runtime_thresholds(preflight, experiment_config)
    _check_algorithm_fidelity(preflight)
    _check_backtest_profile(preflight, protocol, backtest_config, backtest_path)
    _check_provider_and_factor_data(preflight, config)

    llm_configuration = _effective_llm_configuration()
    chat_model = llm_configuration["chat_model"]
    api_key = llm_configuration["api_key"]
    base_url = llm_configuration["base_url"]
    llm_ready = bool(
        api_key and base_url and chat_model
    )
    preflight.check(
        "llm_configuration",
        llm_ready,
        expected="API key, base URL and chat model configured",
        actual={"configured": llm_ready, "chatModel": chat_model},
        evidence=".env (secret values not recorded)",
    )
    auth_probe = (
        probe_llm_auth(str(base_url), str(api_key))
        if llm_ready
        else {"ok": False, "httpStatus": None, "endpoint": None}
    )
    preflight.check(
        "llm_authentication",
        bool(auth_probe["ok"]),
        expected="HTTP 2xx from the OpenAI-compatible /models endpoint",
        actual=auth_probe,
        evidence="credential probe; no generation request and no secret recorded",
    )
    accepted_backbones = config["claim_parity"]["accepted_backbones"]
    preflight.check(
        "paper_claim_backbone",
        chat_model in accepted_backbones,
        expected=accepted_backbones,
        actual=chat_model,
        evidence=".env CHAT_MODEL",
        hard=False,
    )
    preflight.check(
        "paper_undocumented_parameters",
        False,
        expected="all paper run parameters documented",
        actual=config["claim_parity"]["documented_gap"],
        evidence="arXiv v3 Appendix B and official repository defaults",
        hard=False,
    )

    source_manifest = _source_manifest(config)
    failures = preflight.hard_failures()
    warnings = preflight.warnings()
    if failures:
        status = "BLOCKED"
        summary = "阶段 0 未通过，禁止启动付费因子挖掘。"
    elif warnings:
        status = "READY_FOR_PILOT"
        summary = (
            "阶段 0 已通过，可以启动小规模算法验证；"
            "当前模型与论文主结果模型不同，因此不能声称精确复现论文数值。"
        )
    else:
        status = "PAPER_REPRO_READY"
        summary = "阶段 0 已通过，可以启动锁定配置的论文复现实验。"

    run_id = run_id or datetime.now().strftime("paper_preflight_%Y%m%d_%H%M%S")
    output_dir = resolve_path(config["output"]["root"]) / run_id
    output_dir.mkdir(parents=True, exist_ok=False)
    result = {
        "schemaVersion": 1,
        "runId": run_id,
        "createdAt": datetime.now().astimezone().isoformat(),
        "status": status,
        "summary": summary,
        "protocolId": config["protocol"]["id"],
        "paper": config["sources"]["paper"],
        "officialCode": config["sources"]["official_code"],
        "configuredBackbone": chat_model,
        "llmCalls": 0,
        "schedule": schedule,
        "checks": preflight.checks,
        "hardFailureIds": [check["id"] for check in failures],
        "warningIds": [check["id"] for check in warnings],
        "sourceManifest": source_manifest,
    }
    (output_dir / "preflight.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    manifest = {
        "runId": run_id,
        "protocolConfig": {
            "path": str(config_path),
            "sha256": sha256_file(config_path),
        },
        "experimentConfig": {
            "path": str(experiment_path),
            "sha256": sha256_file(experiment_path),
        },
        "backtestConfig": {
            "path": str(backtest_path),
            "sha256": sha256_file(backtest_path),
        },
        "source": source_manifest,
    }
    (output_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (output_dir / "report.md").write_text(
        _render_report(result),
        encoding="utf-8",
    )
    result["outputDir"] = str(output_dir.resolve())
    return result
