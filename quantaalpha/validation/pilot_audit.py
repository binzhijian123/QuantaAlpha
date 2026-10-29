"""Audit a paper-v3.1 pilot without evaluating the historical test segment."""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from quantaalpha.validation.paper import load_yaml, resolve_path, sha256_file


SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"Expected a JSON object: {path}")
    return value


class PilotAudit:
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
    ) -> None:
        self.checks.append(
            {
                "id": check_id,
                "status": "PASS" if passed else "FAIL",
                "expected": expected,
                "actual": actual,
                "evidence": evidence,
            }
        )

    def failures(self) -> list[dict[str, Any]]:
        return [
            check for check in self.checks if check["status"] == "FAIL"
        ]


def _rank_ic(trajectory: dict[str, Any]) -> float | None:
    metrics = trajectory.get("backtest_metrics") or {}
    for key in ("RankIC", "Rank IC", "rank_ic"):
        value = metrics.get(key)
        if value is not None:
            try:
                return float(value)
            except (TypeError, ValueError):
                return None
    return None


def _variant_evidence(
    factors: list[dict[str, Any]],
    expected_roles: dict[str, str],
) -> dict[str, Any]:
    roles: list[str] = []
    axes: dict[str, str] = {}
    family_ids: list[str] = []
    complete = True
    gate_passed = True
    hashes_valid = True
    for factor in factors:
        quality_gate = factor.get("quality_gate") or {}
        contract = quality_gate.get("variant_contract") or {}
        role = contract.get("variantId")
        axis = contract.get("variationAxis")
        family_id = contract.get("familyId")
        roles.append(str(role or ""))
        axes[str(role or "")] = str(axis or "")
        family_ids.append(str(family_id or ""))
        complete = complete and bool(
            contract.get("protocolVersion")
            and family_id
            and role
            and contract.get("variantHypothesis")
            and contract.get("model")
        )
        gate_passed = gate_passed and bool(
            quality_gate.get("evaluated")
            and quality_gate.get("passed")
            and quality_gate.get("consistency_passed")
        )
        hashes_valid = hashes_valid and all(
            SHA256_RE.fullmatch(str(contract.get(key, ""))) is not None
            for key in (
                "familyHypothesisSha256",
                "promptSha256",
                "responseSha256",
            )
        )
    return {
        "roles": roles,
        "rolesValid": set(roles) == set(expected_roles)
        and len(roles) == len(expected_roles),
        "axesValid": axes == expected_roles,
        "singleFamily": len(set(family_ids)) == 1
        and bool(family_ids)
        and bool(family_ids[0]),
        "complete": complete,
        "qualityGatePassed": gate_passed,
        "hashesValid": hashes_valid,
    }


def _render_report(result: dict[str, Any]) -> str:
    lines = [
        "# QuantaAlpha 论文 v3.1 Pilot 审计",
        "",
        f"- 状态：`{result['status']}`",
        f"- 轨迹：`{result['summary']['trajectoryCount']}`",
        f"- 因子库：`{result['summary']['libraryFactorCount']}`",
        f"- 验证池：`{result['summary']['selectedFactorCount']}`",
        f"- 历史测试集参与评估：`{result['evidenceScope']['historicalTestEvaluated']}`",
        f"- 提供器包含历史测试日期：`{result['evidenceScope']['providerContainsHistoricalTestRows']}`",
        f"- 物理零读取保证：`{result['evidenceScope']['physicalZeroReadGuaranteed']}`",
        "",
        "| 检查 | 状态 | 证据 |",
        "| --- | --- | --- |",
    ]
    for check in result["checks"]:
        lines.append(
            f"| `{check['id']}` | `{check['status']}` | "
            f"{check['evidence']} |"
        )
    lines.extend(
        [
            "",
            "## 结论",
            "",
            result["conclusion"],
            "",
            "本审计仅使用训练/验证阶段产物进行评估、排序和回测。运行提供器"
            "包含 2022–2025 数据行，因此本审计不声称物理零读取。该历史区间"
            "已在更早运行中查看，不能再次作为独立样本外证据；通过后只能进入"
            "协议冻结后的前向模拟盘。",
            "",
        ]
    )
    return "\n".join(lines)


def audit_paper_validation_pilot(
    config_path: str | Path,
    trajectory_pool_path: str | Path,
    library_path: str | Path,
    pool_dir: str | Path,
    output_dir: str | Path,
) -> dict[str, Any]:
    config_path = resolve_path(config_path).resolve()
    trajectory_pool_path = resolve_path(trajectory_pool_path).resolve()
    library_path = resolve_path(library_path).resolve()
    pool_dir = resolve_path(pool_dir).resolve()
    output_dir = resolve_path(output_dir).resolve()

    config = load_yaml(config_path)
    acceptance = config["pilot_acceptance"]
    contract = config["experiment_contract"]
    expected_roles = {
        str(role["id"]): str(role["variationAxis"])
        for role in contract["variant_roles"]
    }

    trajectory_payload = _load_json(trajectory_pool_path)
    library_payload = _load_json(library_path)
    selection_payload = _load_json(pool_dir / "selection.json")
    selected_payload = _load_json(
        pool_dir / "selected_factor_library.json"
    )

    trajectories = list(
        (trajectory_payload.get("trajectories") or {}).values()
    )
    library_factors = library_payload.get("factors") or {}
    selection = selection_payload.get("selection") or {}
    selected_factors = selected_payload.get("factors") or {}
    audit = PilotAudit()

    expected_trajectory_count = int(
        acceptance["expected_trajectories"]
    )
    audit.check(
        "trajectory_count",
        len(trajectories) == expected_trajectory_count,
        expected=expected_trajectory_count,
        actual=len(trajectories),
        evidence=str(trajectory_pool_path),
    )

    factors_per_trajectory = int(
        acceptance["factors_per_trajectory"]
    )
    factor_counts = [
        len(trajectory.get("factors") or [])
        for trajectory in trajectories
    ]
    audit.check(
        "complete_factor_families",
        bool(trajectories)
        and all(
            count == factors_per_trajectory
            for count in factor_counts
        ),
        expected=f"{factors_per_trajectory} factors per trajectory",
        actual=factor_counts,
        evidence="trajectory_pool.json factors",
    )

    variant_evidence = [
        _variant_evidence(
            trajectory.get("factors") or [],
            expected_roles,
        )
        for trajectory in trajectories
    ]
    audit.check(
        "variant_lineage",
        bool(variant_evidence)
        and all(
            item["rolesValid"]
            and item["axesValid"]
            and item["singleFamily"]
            and item["complete"]
            and item["hashesValid"]
            for item in variant_evidence
        ),
        expected={
            "roles": expected_roles,
            "singleFamilyPerTrajectory": True,
            "sha256Evidence": True,
        },
        actual=variant_evidence,
        evidence="per-factor quality_gate.variant_contract",
    )
    audit.check(
        "factor_quality_gates",
        bool(variant_evidence)
        and all(
            item["qualityGatePassed"]
            for item in variant_evidence
        ),
        expected=True,
        actual=[
            item["qualityGatePassed"] for item in variant_evidence
        ],
        evidence="per-factor consistency/complexity/redundancy audit",
    )

    rank_ics = [_rank_ic(trajectory) for trajectory in trajectories]
    require_positive_rankic = bool(
        acceptance["require_positive_trajectory_rankic"]
    )
    rankic_passed = bool(rank_ics) and all(
        value is not None
        and (value > 0 if require_positive_rankic else True)
        for value in rank_ics
    )
    audit.check(
        "trajectory_rankic",
        rankic_passed,
        expected="all RankIC > 0" if require_positive_rankic else "RankIC present",
        actual=rank_ics,
        evidence="trajectory backtest_metrics on train/valid workflow",
    )

    minimum_library_factors = int(
        acceptance["minimum_library_factors"]
    )
    audit.check(
        "library_yield",
        len(library_factors) >= minimum_library_factors,
        expected=f">= {minimum_library_factors}",
        actual=len(library_factors),
        evidence=str(library_path),
    )

    selected_count = int(selection.get("selectedCount", 0) or 0)
    minimum_selected = int(
        acceptance["minimum_selected_factors"]
    )
    audit.check(
        "validation_pool_nonempty",
        selected_count >= minimum_selected
        and selected_count == len(selected_factors),
        expected=f">= {minimum_selected} selected factors",
        actual={
            "selectionCount": selected_count,
            "selectedLibraryCount": len(selected_factors),
        },
        evidence=str(pool_dir / "selection.json"),
    )

    selected_ids = set(selection.get("selectedFactors") or [])
    selected_decisions = [
        decision
        for decision in (selection.get("decisions") or [])
        if decision.get("factor") in selected_ids
    ]
    minimum_rankic = float(
        acceptance["minimum_selected_mean_rankic"]
    )
    selected_rank_ics = [
        decision.get("meanRankIC")
        for decision in selected_decisions
    ]
    audit.check(
        "selected_validation_rankic",
        len(selected_decisions) == selected_count
        and all(
            value is not None and float(value) > minimum_rankic
            for value in selected_rank_ics
        ),
        expected=f"all selected meanRankIC > {minimum_rankic}",
        actual=selected_rank_ics,
        evidence="2021 validation-only factor-pool decisions",
    )

    feedback_values = [
        (factor.get("feedback") or {}).get("decision")
        for factor in selected_factors.values()
    ]
    require_positive_feedback = bool(
        acceptance["require_positive_feedback"]
    )
    feedback_passed = (
        bool(feedback_values)
        and all(value is True for value in feedback_values)
        if require_positive_feedback
        else True
    )
    audit.check(
        "selected_feedback",
        feedback_passed,
        expected=(
            "all selected source feedback decisions are true"
            if require_positive_feedback
            else "not required"
        ),
        actual=feedback_values,
        evidence="selected factor library feedback",
    )

    failures = audit.failures()
    status = (
        "FORWARD_SIMULATION_CANDIDATE"
        if not failures
        else "RESEARCH_ONLY"
    )
    conclusion = (
        "训练/验证门禁全部通过，可以冻结协议后进入前向模拟盘；"
        "这不证明未来收益，也不恢复历史测试集的独立性。"
        if not failures
        else "训练/验证门禁未全部通过，维持 RESEARCH_ONLY，禁止生成可交易信号。"
    )
    factor_data_end = str(
        config["runtime"]["factor_data"]["end_time"]
    )
    historical_test_start = str(
        config["paper_protocol"]["effective_segments"]["test"][0]
    )
    provider_contains_historical_test = (
        factor_data_end >= historical_test_start
    )
    result = {
        "schemaVersion": 2,
        "createdAt": datetime.now().astimezone().isoformat(),
        "status": status,
        "conclusion": conclusion,
        "protocolId": config["protocol"]["id"],
        "evidenceScope": {
            "segments": ["train", "valid"],
            "historicalTestEvaluated": False,
            "historicalTestAccessed": False,
            "historicalTestAccessDefinition": (
                "not used in evaluation, ranking, or backtest evidence"
            ),
            "providerContainsHistoricalTestRows": (
                provider_contains_historical_test
            ),
            "physicalZeroReadGuaranteed": False,
            "historicalTestIndependent": False,
        },
        "summary": {
            "trajectoryCount": len(trajectories),
            "libraryFactorCount": len(library_factors),
            "selectedFactorCount": selected_count,
            "failureIds": [check["id"] for check in failures],
        },
        "checks": audit.checks,
        "inputs": {
            "config": {
                "path": str(config_path),
                "sha256": sha256_file(config_path),
            },
            "trajectoryPool": {
                "path": str(trajectory_pool_path),
                "sha256": sha256_file(trajectory_pool_path),
            },
            "library": {
                "path": str(library_path),
                "sha256": sha256_file(library_path),
            },
            "selection": {
                "path": str(pool_dir / "selection.json"),
                "sha256": sha256_file(pool_dir / "selection.json"),
            },
        },
    }

    output_dir.mkdir(parents=True, exist_ok=False)
    (output_dir / "audit.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (output_dir / "report.md").write_text(
        _render_report(result),
        encoding="utf-8",
    )
    result["outputDir"] = str(output_dir)
    return result
