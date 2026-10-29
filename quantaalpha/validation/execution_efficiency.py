from __future__ import annotations

import copy
import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import yaml

from quantaalpha.validation.profit import _incremental_bootstrap, analyze_daily_returns
from quantaalpha.validation.standard_baseline import (
    _check,
    _load_daily,
    _resolve,
    _sha256,
    _write_json,
    evaluate_standard_baseline,
    validate_daily_pair,
)


def _filter_invalid_price_signals(
    prediction: pd.Series,
    stock_list: list[str],
    start_time: str,
    end_time: str,
) -> pd.Series:
    from qlib.data import D

    filtered = prediction.copy()
    price_data = D.features(
        stock_list,
        ["$close"],
        start_time=start_time,
        end_time=end_time,
        freq="day",
    )
    invalid_indices = price_data.index[
        (price_data["$close"] == 0) | price_data["$close"].isna()
    ]
    invalid_set = {(datetime_value, instrument) for instrument, datetime_value in invalid_indices}
    for index in filtered.index:
        if index in invalid_set:
            filtered.loc[index] = np.nan
    return filtered


def _backtest_prediction(
    runtime: dict[str, Any],
    prediction: pd.Series,
    n_drop: int,
    output_path: Path,
) -> pd.DataFrame:
    from qlib.backtest import backtest as qlib_backtest
    from qlib.data import D

    backtest_config = runtime["backtest"]["backtest"]
    strategy_config = copy.deepcopy(runtime["backtest"]["strategy"])
    strategy_config["kwargs"]["n_drop"] = int(n_drop)
    market = runtime["data"]["market"]
    instruments = D.instruments(market)
    stock_list = D.list_instruments(
        instruments,
        start_time=backtest_config["start_time"],
        end_time=backtest_config["end_time"],
        as_list=True,
    )
    filtered_prediction = _filter_invalid_price_signals(
        prediction,
        stock_list,
        backtest_config["start_time"],
        backtest_config["end_time"],
    )
    portfolio_metric_dict, _ = qlib_backtest(
        executor={
            "class": "SimulatorExecutor",
            "module_path": "qlib.backtest.executor",
            "kwargs": {
                "time_per_step": "day",
                "generate_portfolio_metrics": True,
                "verbose": False,
                "indicator_config": {"show_indicator": False},
            },
        },
        strategy={
            "class": strategy_config["class"],
            "module_path": strategy_config["module_path"],
            "kwargs": {
                "signal": filtered_prediction,
                "topk": int(strategy_config["kwargs"]["topk"]),
                "n_drop": int(strategy_config["kwargs"]["n_drop"]),
            },
        },
        start_time=backtest_config["start_time"],
        end_time=backtest_config["end_time"],
        account=backtest_config["account"],
        benchmark=backtest_config["benchmark"],
        exchange_kwargs={"codes": stock_list, **backtest_config["exchange_kwargs"]},
    )
    if not portfolio_metric_dict or "1day" not in portfolio_metric_dict:
        raise RuntimeError("Qlib did not return one-day portfolio metrics")
    report_df, _ = portfolio_metric_dict["1day"]
    required = {"return", "bench", "cost"}
    missing = sorted(required - set(report_df.columns))
    if missing:
        raise RuntimeError(f"Qlib portfolio metrics are missing columns: {missing}")
    daily = pd.DataFrame(index=report_df.index)
    daily["portfolio_return"] = report_df["return"].replace(
        [np.inf, -np.inf], np.nan
    ).fillna(0)
    daily["benchmark_return"] = report_df["bench"].replace(
        [np.inf, -np.inf], np.nan
    ).fillna(0)
    daily["cost"] = report_df["cost"].replace([np.inf, -np.inf], np.nan).fillna(0)
    daily["daily_excess_return"] = (
        daily["portfolio_return"] - daily["benchmark_return"] - daily["cost"]
    )
    daily["cumulative_excess_return"] = daily["daily_excess_return"].cumsum()
    daily.index.name = "date"
    output_path.parent.mkdir(parents=True, exist_ok=False)
    daily.to_csv(output_path)
    return daily


def execution_cost_check(
    baseline_daily: pd.DataFrame,
    candidate_daily: pd.DataFrame,
    minimum_reduction: float,
) -> dict[str, Any]:
    baseline_cost = float(baseline_daily["cost"].mean() * 252.0)
    candidate_cost = float(candidate_daily["cost"].mean() * 252.0)
    reduction = 1.0 - candidate_cost / baseline_cost if baseline_cost > 0 else -math.inf
    return _check(
        "execution_cost_reduction",
        reduction >= minimum_reduction,
        {
            "baselineAnnualizedCost": baseline_cost,
            "candidateAnnualizedCost": candidate_cost,
            "reduction": reduction,
        },
        f">= {minimum_reduction}",
        "低换手候选没有达到预注册的成本降幅。",
    )


def _report(decision: dict[str, Any], candidates: dict[str, Any]) -> str:
    lines = [
        f"# Execution efficiency validation {decision['runId']}",
        "",
        f"- Decision: `{decision['status']}`",
        f"- Historical gate passed: `{str(decision['historicalGatePassed']).lower()}`",
        "- Paper eligible: `false`",
        "",
    ]
    for candidate_id in ("E0", "E1"):
        item = candidates.get(candidate_id)
        if not item:
            continue
        metrics = item["analysis"]["standard"]
        lines.extend(
            [
                f"## {candidate_id} n_drop={item['nDrop']}",
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
            "This is historical execution research only. It does not authorize a paper or real-money strategy.",
            "",
        ]
    )
    return "\n".join(lines)


def run_execution_efficiency_validation(
    project_root: Path,
    config_path: Path,
    audit_only: bool = False,
) -> dict[str, Any]:
    import qlib

    project_root = project_root.resolve()
    config_path = config_path.resolve()
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    runtime_path = _resolve(project_root, config["baseline_runtime_config"])
    prediction_path = _resolve(project_root, config["prediction_path"])
    frozen_daily_path = _resolve(project_root, config["frozen_baseline_daily"])
    source_manifest_path = _resolve(project_root, config["source_manifest"])
    runtime = yaml.safe_load(runtime_path.read_text(encoding="utf-8"))
    provider = _resolve(project_root, runtime["data"]["provider_uri"])
    from quantaalpha.utils.qlib_provider import provider_contract_fingerprint

    actual_provider_fingerprint = provider_contract_fingerprint(
        provider, runtime["data"]["market"]
    )["fingerprint"]
    frozen = config["frozen_contract"]
    actual = {
        "runtime_config_sha256": _sha256(runtime_path),
        "prediction_sha256": _sha256(prediction_path),
        "frozen_baseline_daily_sha256": _sha256(frozen_daily_path),
        "source_manifest_sha256": _sha256(source_manifest_path),
        "provider_path": str(provider),
        "provider_fingerprint": actual_provider_fingerprint,
        "topk": int(runtime["backtest"]["strategy"]["kwargs"]["topk"]),
        "baseline_n_drop": int(runtime["backtest"]["strategy"]["kwargs"]["n_drop"]),
    }
    audit_checks = [
        _check(
            f"frozen_{key}",
            actual.get(key) == expected,
            actual.get(key),
            expected,
            f"Frozen execution contract mismatch: {key}",
        )
        for key, expected in frozen.items()
    ]
    audit = {
        "status": "PASS" if all(item["status"] == "PASS" for item in audit_checks) else "FAIL",
        "checks": audit_checks,
        "actual": actual,
    }

    output_root = _resolve(project_root, config["output_root"])
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    run_id = str(config.get("run_id") or f"execution_{timestamp}")
    run_dir = output_root / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    _write_json(run_dir / "contract_audit.json", audit)
    manifest_inputs = [
        config_path,
        runtime_path,
        prediction_path,
        frozen_daily_path,
        source_manifest_path,
        Path(__file__).resolve(),
    ]
    _write_json(
        run_dir / "manifest.json",
        {
            "runId": run_id,
            "protocolVersion": config["protocol_version"],
            "createdAt": datetime.now(timezone.utc).isoformat(),
            "historicalEvidenceOnly": True,
            "inputs": {str(path): _sha256(path) for path in manifest_inputs},
            "comparison": config["comparison"],
            "promotion": config["promotion"],
        },
    )

    if audit["status"] != "PASS" or audit_only:
        decision = {
            "runId": run_id,
            "protocolVersion": config["protocol_version"],
            "status": "BLOCKED" if audit["status"] != "PASS" else "RESEARCH_ONLY",
            "historicalGatePassed": False,
            "paperEligible": False,
            "checks": audit_checks,
            "reasons": [
                *[
                    check["reason"]
                    for check in audit_checks
                    if check["status"] == "FAIL"
                ],
                *(["Audit-only run; execution returns were not opened."] if audit_only else []),
            ],
        }
        _write_json(run_dir / "decision.json", decision)
        (run_dir / "report.md").write_text(_report(decision, {}), encoding="utf-8")
        return {"runId": run_id, "runDir": str(run_dir), "decision": decision}

    qlib.init(provider_uri=str(provider), region=runtime["data"].get("region", "cn"))
    prediction = pd.read_pickle(prediction_path)
    if isinstance(prediction, pd.DataFrame):
        if prediction.shape[1] != 1:
            raise ValueError("Frozen prediction must contain exactly one score column")
        prediction = prediction.iloc[:, 0]
    if not isinstance(prediction, pd.Series):
        raise TypeError(f"Unsupported prediction type: {type(prediction)}")

    candidates: dict[str, Any] = {}
    daily_frames: dict[str, pd.DataFrame] = {}
    for spec in (config["comparison"]["baseline"], config["comparison"]["candidate"]):
        candidate_id = spec["id"]
        daily_path = run_dir / "trials" / candidate_id / "daily.csv"
        daily = _backtest_prediction(runtime, prediction, int(spec["n_drop"]), daily_path)
        daily_frames[candidate_id] = daily
        analysis = analyze_daily_returns(
            daily, config.get("cost_multipliers", [1.0, 1.5, 2.0])
        )
        candidates[candidate_id] = {
            "nDrop": int(spec["n_drop"]),
            "dailyPath": str(daily_path),
            "dailySha256": _sha256(daily_path),
            "analysis": analysis,
        }
        _write_json(daily_path.parent / "analysis.json", analysis)

    frozen_daily = _load_daily(frozen_daily_path, "frozen B0")
    e0_daily = _load_daily(Path(candidates["E0"]["dailyPath"]), "E0")
    pair_audit = validate_daily_pair(frozen_daily, e0_daily)
    parity_columns = ["portfolio_return", "benchmark_return", "cost", "daily_excess_return"]
    parity = all(
        np.allclose(
            frozen_daily[column].to_numpy(dtype=float),
            e0_daily[column].to_numpy(dtype=float),
            rtol=0.0,
            atol=1e-12,
        )
        for column in parity_columns
    )
    pair_audit["exactBaselineValues"] = parity
    pair_audit["status"] = "PASS" if parity else "FAIL"
    _write_json(run_dir / "baseline_parity_audit.json", pair_audit)
    if not parity:
        decision = {
            "runId": run_id,
            "protocolVersion": config["protocol_version"],
            "status": "BLOCKED",
            "historicalGatePassed": False,
            "paperEligible": False,
            "checks": [],
            "reasons": ["Frozen E0 replay does not match the source B0 daily returns."],
        }
    else:
        e1_daily = _load_daily(Path(candidates["E1"]["dailyPath"]), "E1")
        validate_daily_pair(e0_daily, e1_daily)
        bootstrap = _incremental_bootstrap(
            candidates["E0"]["dailyPath"],
            candidates["E1"]["dailyPath"],
            iterations=int(config["promotion"].get("bootstrap_iterations", 1000)),
            block_size=int(config["promotion"].get("bootstrap_block_days", 20)),
            seed=int(config["promotion"].get("bootstrap_seed", 42)),
        )
        checks = evaluate_standard_baseline(
            candidates["E0"]["analysis"],
            candidates["E1"]["analysis"],
            config["promotion"],
            bootstrap,
            baseline_label="E0",
            candidate_label="E1",
        )
        checks.append(
            execution_cost_check(
                e0_daily,
                e1_daily,
                float(config["promotion"].get("minimum_cost_reduction", 0.5)),
            )
        )
        historical_gate_passed = all(check["status"] == "PASS" for check in checks)
        decision = {
            "runId": run_id,
            "protocolVersion": config["protocol_version"],
            "status": "RESEARCH_ONLY" if historical_gate_passed else "REJECTED",
            "historicalGatePassed": historical_gate_passed,
            "forwardTrialGatePassed": historical_gate_passed,
            "paperEligible": False,
            "checks": checks,
            "incrementalBootstrap": bootstrap,
            "reasons": [
                check["reason"] for check in checks if check["status"] == "FAIL"
            ],
        }

    _write_json(run_dir / "candidates.json", candidates)
    _write_json(run_dir / "decision.json", decision)
    (run_dir / "report.md").write_text(
        _report(decision, candidates), encoding="utf-8"
    )
    return {"runId": run_id, "runDir": str(run_dir), "decision": decision}
