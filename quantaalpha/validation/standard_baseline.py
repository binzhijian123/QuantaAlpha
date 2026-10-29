from __future__ import annotations

import copy
import csv
import gc
import hashlib
import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import yaml

from quantaalpha.backtest.factor_loader import FactorLoader
from quantaalpha.backtest.runner import BacktestRunner
from quantaalpha.utils.qlib_provider import provider_contract_fingerprint
from quantaalpha.validation.profit import (
    _incremental_bootstrap,
    analyze_daily_returns,
    provider_summary,
)


def _resolve(project_root: Path, value: str | Path) -> Path:
    path = Path(value).expanduser()
    return path.resolve() if path.is_absolute() else (project_root / path).resolve()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _json_clean(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _json_clean(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_clean(item) for item in value]
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating, float)):
        return None if not math.isfinite(float(value)) else float(value)
    if isinstance(value, (pd.Timestamp, datetime, Path)):
        return str(value)
    return value


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(_json_clean(payload), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary.replace(path)


def _check(
    check_id: str,
    passed: bool,
    actual: Any,
    required: Any,
    reason: str,
) -> dict[str, Any]:
    return {
        "id": check_id,
        "status": "PASS" if passed else "FAIL",
        "actual": actual,
        "required": required,
        "reason": "" if passed else reason,
    }


def _factor_set_sha256(factors: dict[str, str]) -> str:
    payload = json.dumps(factors, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _load_daily(path: Path, candidate_id: str) -> pd.DataFrame:
    if not path.is_file():
        raise FileNotFoundError(f"{candidate_id} daily return file is missing: {path}")
    frame = pd.read_csv(path)
    required = {
        "date",
        "portfolio_return",
        "benchmark_return",
        "cost",
        "daily_excess_return",
    }
    missing = sorted(required - set(frame.columns))
    if missing:
        raise ValueError(f"{candidate_id} daily return columns are missing: {missing}")
    frame["date"] = pd.to_datetime(frame["date"], errors="coerce")
    if frame["date"].isna().any():
        raise ValueError(f"{candidate_id} contains invalid dates")
    if frame["date"].duplicated().any() or not frame["date"].is_monotonic_increasing:
        raise ValueError(f"{candidate_id} dates must be unique and increasing")
    numeric_columns = sorted(required - {"date"})
    numeric = frame[numeric_columns].apply(pd.to_numeric, errors="coerce")
    if not np.isfinite(numeric.to_numpy(dtype=float)).all():
        raise ValueError(f"{candidate_id} contains non-finite returns")
    expected = (
        numeric["portfolio_return"]
        - numeric["benchmark_return"]
        - numeric["cost"]
    )
    if not np.allclose(
        expected.to_numpy(dtype=float),
        numeric["daily_excess_return"].to_numpy(dtype=float),
        rtol=0.0,
        atol=1e-12,
    ):
        raise ValueError(f"{candidate_id} daily excess return identity failed")
    frame[numeric_columns] = numeric
    return frame.set_index("date")


def validate_daily_pair(
    baseline: pd.DataFrame,
    candidate: pd.DataFrame,
) -> dict[str, Any]:
    baseline_dates = pd.DatetimeIndex(baseline.index)
    candidate_dates = pd.DatetimeIndex(candidate.index)
    if not baseline_dates.equals(candidate_dates):
        raise ValueError("B0 and B1 daily return dates are not exactly identical")
    if len(baseline_dates) == 0:
        raise ValueError("B0 and B1 daily return files are empty")
    return {
        "status": "PASS",
        "rows": int(len(baseline_dates)),
        "start": str(baseline_dates.min().date()),
        "end": str(baseline_dates.max().date()),
        "exactDates": True,
    }


def evaluate_standard_baseline(
    baseline_analysis: dict[str, Any],
    candidate_analysis: dict[str, Any],
    promotion: dict[str, Any],
    bootstrap: dict[str, Any],
    baseline_label: str = "B0",
    candidate_label: str = "B1",
) -> list[dict[str, Any]]:
    baseline = baseline_analysis.get("standard") or {}
    candidate = candidate_analysis.get("standard") or {}
    recent_window = str(int(promotion.get("recent_window_days", 252)))
    recent = candidate_analysis.get("recentWindows", {}).get(recent_window) or {}
    latest_year = str(promotion.get("latest_complete_year", "2025"))
    latest = candidate_analysis.get("yearly", {}).get(latest_year) or {}
    cost_key = str(float(promotion.get("cost_multiplier", 2.0)))
    stressed = candidate_analysis.get("costSensitivity", {}).get(cost_key) or {}

    checks = [
        _check(
            "recent_window_positive",
            recent.get("annualizedExcessReturn", -math.inf)
            > float(promotion.get("recent_annualized_excess_min", 0.0)),
            recent.get("annualizedExcessReturn"),
            f"> {promotion.get('recent_annualized_excess_min', 0.0)}",
            f"{candidate_label} 最近 {recent_window} 个交易日税费后超额收益未为正。",
        ),
        _check(
            "latest_complete_year",
            latest.get("annualizedExcessReturn", -math.inf)
            >= float(promotion.get("latest_year_annualized_excess_min", 0.02)),
            latest.get("annualizedExcessReturn"),
            f">= {promotion.get('latest_year_annualized_excess_min', 0.02)}",
            f"{candidate_label} {latest_year} 年税费后超额收益未达到门槛。",
        ),
        _check(
            "information_ratio",
            candidate.get("informationRatio", -math.inf)
            >= float(promotion.get("information_ratio_min", 0.5)),
            candidate.get("informationRatio"),
            f">= {promotion.get('information_ratio_min', 0.5)}",
            f"{candidate_label} 信息比率未达到门槛。",
        ),
        _check(
            "max_drawdown",
            candidate.get("maxDrawdown", -math.inf)
            >= -abs(float(promotion.get("max_drawdown_abs_max", 0.15))),
            candidate.get("maxDrawdown"),
            f">= {-abs(float(promotion.get('max_drawdown_abs_max', 0.15)))}",
            f"{candidate_label} 最大回撤超过门槛。",
        ),
        _check(
            "cost_stress",
            stressed.get("annualizedExcessReturn", -math.inf)
            > float(promotion.get("stressed_annualized_excess_min", 0.0)),
            stressed.get("annualizedExcessReturn"),
            f"> {promotion.get('stressed_annualized_excess_min', 0.0)} at {cost_key}x cost",
            f"{candidate_label} 在 {cost_key}x 成本下税费后超额收益未为正。",
        ),
    ]

    stability_years = [str(year) for year in promotion.get("stability_years", [])]
    yearly = candidate_analysis.get("yearly", {})
    positive_years = sum(
        (yearly.get(year) or {}).get("annualizedExcessReturn", -math.inf) > 0
        for year in stability_years
    )
    required_positive_years = int(promotion.get("positive_years_min", 3))
    checks.append(
        _check(
            "yearly_stability",
            bool(stability_years) and positive_years >= required_positive_years,
            {"positive": positive_years, "years": stability_years},
            f">= {required_positive_years} positive years",
            f"{candidate_label} 完整年度正超额数量不足。",
        )
    )

    latest_months = [
        metrics
        for month, metrics in candidate_analysis.get("monthly", {}).items()
        if month.startswith(f"{latest_year}-")
    ]
    positive_month_returns = [
        max(float(metrics.get("totalExcessReturn") or 0.0), 0.0)
        for metrics in latest_months
    ]
    positive_month_total = sum(positive_month_returns)
    month_share = (
        max(positive_month_returns, default=0.0) / positive_month_total
        if positive_month_total > 0
        else 1.0
    )
    max_month_share = float(promotion.get("latest_year_max_positive_month_share", 0.8))
    checks.append(
        _check(
            "latest_year_month_concentration",
            bool(latest_months) and month_share <= max_month_share,
            month_share,
            f"<= {max_month_share}",
            f"{candidate_label} {latest_year} 年收益过度集中于单月。",
        )
    )

    arr_delta = candidate.get("annualizedExcessReturn", -math.inf) - baseline.get(
        "annualizedExcessReturn", 0.0
    )
    ir_delta = candidate.get("informationRatio", -math.inf) - baseline.get(
        "informationRatio", 0.0
    )
    drawdown_delta = candidate.get("maxDrawdown", -math.inf) - baseline.get(
        "maxDrawdown", 0.0
    )
    arr_improvement = float(promotion.get("annualized_excess_improvement_min", 0.02))
    ir_improvement = float(promotion.get("information_ratio_improvement_min", 0.2))
    checks.extend(
        [
            _check(
                "incremental_value",
                arr_delta >= arr_improvement or ir_delta >= ir_improvement,
                {
                    "annualizedExcessDelta": arr_delta,
                    "informationRatioDelta": ir_delta,
                },
                {
                    "annualizedExcessDelta": f">= {arr_improvement}",
                    "orInformationRatioDelta": f">= {ir_improvement}",
                },
                f"{candidate_label} 相对 {baseline_label} 未达到实质改善门槛。",
            ),
            _check(
                "non_degradation",
                arr_delta
                >= -abs(float(promotion.get("annualized_excess_degradation_max", 0.01)))
                and ir_delta
                >= -abs(float(promotion.get("information_ratio_degradation_max", 0.1)))
                and drawdown_delta
                >= -abs(float(promotion.get("drawdown_degradation_max", 0.02))),
                {
                    "annualizedExcessDelta": arr_delta,
                    "informationRatioDelta": ir_delta,
                    "maxDrawdownDelta": drawdown_delta,
                },
                "all degradation limits",
                f"{candidate_label} 相对 {baseline_label} 存在明显退化。",
            ),
        ]
    )

    lower_bound = (bootstrap.get("confidenceInterval90") or [-math.inf])[0]
    checks.append(
        _check(
            "incremental_bootstrap",
            bootstrap.get("status") == "PASS"
            and lower_bound > float(promotion.get("bootstrap_lower_bound_min", 0.0)),
            bootstrap,
            f"90% lower bound > {promotion.get('bootstrap_lower_bound_min', 0.0)}",
            f"{candidate_label} 增量收益的不确定性区间没有可靠地高于零。",
        )
    )
    return checks


def _write_ledger(path: Path, rows: list[dict[str, Any]]) -> None:
    fieldnames = [
        "candidate",
        "factor_source",
        "factor_count",
        "status",
        "started_at",
        "finished_at",
        "daily_path",
        "metrics_path",
        "error",
    ]
    with path.open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def _runtime_config(
    base: dict[str, Any],
    spec: dict[str, Any],
    provider: Path,
    provider_fingerprint: str,
    output_dir: Path,
    run_id: str,
) -> dict[str, Any]:
    runtime = copy.deepcopy(base)
    runtime["data"]["provider_uri"] = str(provider)
    runtime["data"]["provider_strict"] = True
    runtime["data"]["provider_fingerprint"] = provider_fingerprint
    runtime["factor_source"]["type"] = spec["factor_source"]
    runtime["factor_source"]["custom"]["json_files"] = []
    runtime["experiment"]["name"] = f"{run_id}_{spec['id'].lower()}"
    runtime["experiment"]["recorder"] = f"{run_id}_{spec['id'].lower()}"
    runtime["experiment"]["output_dir"] = str(output_dir)
    return runtime


def _contract_audit(
    project_root: Path,
    config: dict[str, Any],
    base_config_path: Path,
    base: dict[str, Any],
) -> tuple[dict[str, Any], Path, str]:
    frozen = config["frozen_contract"]
    provider = _resolve(project_root, base["data"]["provider_uri"])
    provider_contract = provider_contract_fingerprint(provider, base["data"]["market"])
    provider_fingerprint = provider_contract["fingerprint"]
    actual = {
        "base_backtest_sha256": _sha256(base_config_path),
        "provider_path": str(provider),
        "provider_fingerprint": provider_fingerprint,
        "market": base["data"]["market"],
        "data_start": str(base["data"]["start_time"]),
        "data_end": str(base["data"]["end_time"]),
        "label": base["dataset"]["label"],
        "segments": base["dataset"]["segments"],
        "model": base["model"],
        "strategy": base["backtest"]["strategy"],
        "backtest": base["backtest"]["backtest"],
        "random_seed": base.get("random_seed"),
        "baseline_factor_count": len(FactorLoader.ALPHA158_20_FACTORS),
        "candidate_factor_count": len(FactorLoader.ALPHA158_FACTORS),
        "baseline_factor_sha256": _factor_set_sha256(FactorLoader.ALPHA158_20_FACTORS),
        "candidate_factor_sha256": _factor_set_sha256(FactorLoader.ALPHA158_FACTORS),
    }
    checks = []
    for key, expected in frozen.items():
        checks.append(
            _check(
                f"frozen_{key}",
                actual.get(key) == expected,
                actual.get(key),
                expected,
                f"Frozen contract mismatch: {key}",
            )
        )
    summary = provider_summary(provider, base["data"]["market"])
    checks.extend(
        [
            _check(
                "provider_point_in_time_universe",
                bool(summary.get("hasPointInTimeHistory")),
                summary.get("hasPointInTimeHistory"),
                True,
                "Provider does not expose point-in-time universe intervals.",
            ),
            _check(
                "provider_calendar_coverage",
                summary["calendarStart"] <= str(base["data"]["start_time"])
                and summary["calendarEnd"] >= str(base["data"]["end_time"]),
                {
                    "start": summary["calendarStart"],
                    "end": summary["calendarEnd"],
                },
                {
                    "start": str(base["data"]["start_time"]),
                    "end": str(base["data"]["end_time"]),
                },
                "Provider calendar does not cover the frozen backtest range.",
            ),
        ]
    )
    return (
        {
            "status": "PASS" if all(item["status"] == "PASS" for item in checks) else "FAIL",
            "checks": checks,
            "actual": actual,
            "provider": summary,
        },
        provider,
        provider_fingerprint,
    )


def _report(decision: dict[str, Any], candidates: dict[str, Any]) -> str:
    lines = [
        f"# Alpha158 standard baseline validation {decision['runId']}",
        "",
        f"- Decision: `{decision['status']}`",
        f"- Historical gate passed: `{str(decision['historicalGatePassed']).lower()}`",
        "- Paper eligible: `false`",
        "",
    ]
    for candidate_id in ("B0", "B1"):
        candidate = candidates.get(candidate_id)
        if not candidate:
            continue
        metrics = candidate["analysis"]["standard"]
        lines.extend(
            [
                f"## {candidate_id} {candidate['factorSource']}",
                "",
                f"- Annualized after-cost excess: `{metrics['annualizedExcessReturn']:.6f}`",
                f"- Information ratio: `{metrics['informationRatio']:.6f}`",
                f"- Max drawdown: `{metrics['maxDrawdown']:.6f}`",
                "",
            ]
        )
    lines.extend(["## Checks", ""])
    for check in decision.get("checks", []):
        lines.append(f"- `{check['status']}` {check['id']}: {check.get('reason') or 'passed'}")
    lines.extend(
        [
            "",
            "This is historical research evidence only. It is not a profit guarantee or real-money authorization.",
            "",
        ]
    )
    return "\n".join(lines)


def run_standard_baseline_validation(
    project_root: Path,
    config_path: Path,
    audit_only: bool = False,
) -> dict[str, Any]:
    project_root = project_root.resolve()
    config_path = config_path.resolve()
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    base_config_path = _resolve(project_root, config["base_backtest_config"])
    base = yaml.safe_load(base_config_path.read_text(encoding="utf-8"))
    output_root = _resolve(project_root, config["output_root"])
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    run_id = str(config.get("run_id") or f"alpha158_{timestamp}")
    run_dir = output_root / run_id
    run_dir.mkdir(parents=True, exist_ok=False)

    audit, provider, provider_fingerprint = _contract_audit(
        project_root, config, base_config_path, base
    )
    _write_json(run_dir / "contract_audit.json", audit)

    specs = [config["comparison"]["baseline"], config["comparison"]["candidate"]]
    runtime_paths: dict[str, Path] = {}
    for spec in specs:
        trial_dir = run_dir / "trials" / spec["id"]
        trial_dir.mkdir(parents=True, exist_ok=False)
        runtime = _runtime_config(
            base,
            spec,
            provider,
            provider_fingerprint,
            trial_dir,
            run_id,
        )
        runtime_path = run_dir / "configs" / f"{spec['id']}.yaml"
        runtime_path.parent.mkdir(parents=True, exist_ok=True)
        runtime_path.write_text(
            yaml.safe_dump(runtime, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )
        runtime_paths[spec["id"]] = runtime_path

    source_paths = [
        config_path,
        base_config_path,
        Path(__file__).resolve(),
        project_root / "quantaalpha/backtest/factor_loader.py",
        project_root / "quantaalpha/backtest/runner.py",
        *runtime_paths.values(),
    ]
    manifest = {
        "runId": run_id,
        "protocolVersion": config["protocol_version"],
        "createdAt": datetime.now(timezone.utc).isoformat(),
        "historicalEvidenceOnly": True,
        "inputs": {str(path): _sha256(path) for path in source_paths},
        "providerPath": str(provider),
        "providerFingerprint": provider_fingerprint,
        "comparison": specs,
        "promotion": config["promotion"],
        "factorSets": {
            "B0": {
                "count": len(FactorLoader.ALPHA158_20_FACTORS),
                "sha256": _factor_set_sha256(FactorLoader.ALPHA158_20_FACTORS),
            },
            "B1": {
                "count": len(FactorLoader.ALPHA158_FACTORS),
                "sha256": _factor_set_sha256(FactorLoader.ALPHA158_FACTORS),
            },
        },
    }
    _write_json(run_dir / "manifest.json", manifest)

    if audit["status"] != "PASS" or audit_only:
        status = "BLOCKED" if audit["status"] != "PASS" else "RESEARCH_ONLY"
        reasons = [
            check["reason"]
            for check in audit["checks"]
            if check["status"] == "FAIL"
        ]
        if audit_only:
            reasons.append("Audit-only run; B0 and B1 returns were not opened.")
        decision = {
            "runId": run_id,
            "protocolVersion": config["protocol_version"],
            "status": status,
            "historicalGatePassed": False,
            "paperEligible": False,
            "checks": audit["checks"],
            "reasons": reasons,
        }
        _write_json(run_dir / "decision.json", decision)
        (run_dir / "report.md").write_text(_report(decision, {}), encoding="utf-8")
        return {"runId": run_id, "runDir": str(run_dir), "decision": decision}

    ledger_rows: list[dict[str, Any]] = []
    candidates: dict[str, Any] = {}
    failure: str | None = None
    for spec in specs:
        candidate_id = spec["id"]
        trial_dir = run_dir / "trials" / candidate_id
        output_name = candidate_id
        row = {
            "candidate": candidate_id,
            "factor_source": spec["factor_source"],
            "factor_count": spec["factor_count"],
            "status": "RUNNING",
            "started_at": datetime.now(timezone.utc).isoformat(),
            "finished_at": "",
            "daily_path": "",
            "metrics_path": "",
            "error": "",
        }
        ledger_rows.append(row)
        _write_ledger(run_dir / "trial_ledger.csv", ledger_rows)
        try:
            runner = BacktestRunner(str(runtime_paths[candidate_id]))
            runner.run(
                factor_source=spec["factor_source"],
                experiment_name=f"{run_id}_{candidate_id.lower()}",
                output_name=output_name,
            )
            daily_path = trial_dir / f"{output_name}_cumulative_excess.csv"
            metrics_path = trial_dir / f"{output_name}_backtest_metrics.json"
            daily = _load_daily(daily_path, candidate_id)
            metrics_payload = json.loads(metrics_path.read_text(encoding="utf-8"))
            if int(metrics_payload.get("num_factors", -1)) != int(spec["factor_count"]):
                raise ValueError(
                    f"{candidate_id} factor count mismatch: "
                    f"{metrics_payload.get('num_factors')} != {spec['factor_count']}"
                )
            analysis = analyze_daily_returns(
                daily,
                cost_multipliers=config.get("cost_multipliers", [1.0, 1.5, 2.0]),
            )
            candidates[candidate_id] = {
                "factorSource": spec["factor_source"],
                "factorCount": spec["factor_count"],
                "dailyPath": str(daily_path),
                "dailySha256": _sha256(daily_path),
                "metricsPath": str(metrics_path),
                "metricsSha256": _sha256(metrics_path),
                "analysis": analysis,
            }
            _write_json(trial_dir / "analysis.json", analysis)
            row.update(
                {
                    "status": "COMPLETE",
                    "finished_at": datetime.now(timezone.utc).isoformat(),
                    "daily_path": str(daily_path),
                    "metrics_path": str(metrics_path),
                }
            )
        except Exception as error:
            failure = f"{type(error).__name__}: {error}"
            row.update(
                {
                    "status": "FAILED",
                    "finished_at": datetime.now(timezone.utc).isoformat(),
                    "error": failure,
                }
            )
        finally:
            _write_ledger(run_dir / "trial_ledger.csv", ledger_rows)
            gc.collect()
        if failure:
            break

    if failure:
        decision = {
            "runId": run_id,
            "protocolVersion": config["protocol_version"],
            "status": "BLOCKED",
            "historicalGatePassed": False,
            "paperEligible": False,
            "checks": [],
            "reasons": [failure],
        }
    else:
        baseline_daily = _load_daily(Path(candidates["B0"]["dailyPath"]), "B0")
        candidate_daily = _load_daily(Path(candidates["B1"]["dailyPath"]), "B1")
        pair_audit = validate_daily_pair(baseline_daily, candidate_daily)
        _write_json(run_dir / "return_pair_audit.json", pair_audit)
        bootstrap = _incremental_bootstrap(
            candidates["B0"]["dailyPath"],
            candidates["B1"]["dailyPath"],
            iterations=int(config["promotion"].get("bootstrap_iterations", 1000)),
            block_size=int(config["promotion"].get("bootstrap_block_days", 20)),
            seed=int(config["promotion"].get("bootstrap_seed", 42)),
        )
        checks = evaluate_standard_baseline(
            candidates["B0"]["analysis"],
            candidates["B1"]["analysis"],
            config["promotion"],
            bootstrap,
        )
        historical_gate_passed = all(check["status"] == "PASS" for check in checks)
        reasons = [check["reason"] for check in checks if check["status"] == "FAIL"]
        if historical_gate_passed:
            reasons.append(
                "Historical gates passed, but this run remains research-only until a separately reviewed forward simulation status and inference-parity gate exist."
            )
        decision = {
            "runId": run_id,
            "protocolVersion": config["protocol_version"],
            "status": "RESEARCH_ONLY" if historical_gate_passed else "REJECTED",
            "historicalGatePassed": historical_gate_passed,
            "forwardTrialGatePassed": historical_gate_passed,
            "paperEligible": False,
            "checks": checks,
            "incrementalBootstrap": bootstrap,
            "multipleTesting": {
                "status": "INSUFFICIENT_FOR_PROFIT_CLAIM",
                "strategiesInRun": 2,
                "reason": "One preregistered standard-feature comparison cannot establish DSR/PBO; independent forward evidence is required.",
            },
            "reasons": reasons,
        }

    _write_json(run_dir / "candidates.json", candidates)
    _write_json(run_dir / "decision.json", decision)
    (run_dir / "report.md").write_text(
        _report(decision, candidates), encoding="utf-8"
    )
    return {"runId": run_id, "runDir": str(run_dir), "decision": decision}
