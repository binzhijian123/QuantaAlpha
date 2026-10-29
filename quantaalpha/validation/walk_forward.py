from __future__ import annotations

import gc
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
from quantaalpha.trading.market_data import QlibBinaryDailySource
from quantaalpha.utils.qlib_provider import provider_contract_fingerprint
from quantaalpha.validation.execution_efficiency import _backtest_prediction
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


def build_walk_forward_schedule(
    calendar: pd.DatetimeIndex,
    train_start: str,
    test_start: str,
    test_end: str,
    validation_sessions: int,
    label_horizon_sessions: int,
) -> list[dict[str, Any]]:
    calendar = pd.DatetimeIndex(calendar).sort_values().unique()
    test_dates = calendar[(calendar >= pd.Timestamp(test_start)) & (calendar <= pd.Timestamp(test_end))]
    if len(test_dates) == 0:
        raise ValueError("Walk-forward test calendar is empty")
    train_start_index = int(calendar.searchsorted(pd.Timestamp(train_start), side="left"))
    folds: list[dict[str, Any]] = []
    periods = test_dates.to_period("Q")
    for period in periods.unique():
        fold_dates = test_dates[periods == period]
        test_start_index = int(calendar.get_loc(fold_dates[0]))
        test_end_index = int(calendar.get_loc(fold_dates[-1]))
        valid_end_index = test_start_index - label_horizon_sessions - 1
        valid_start_index = valid_end_index - validation_sessions + 1
        train_end_index = valid_start_index - label_horizon_sessions - 1
        if train_end_index < train_start_index or valid_start_index < 0:
            raise ValueError(f"Insufficient history for walk-forward fold {period}")
        fold = {
            "id": f"WF_{str(period).replace('Q', '_Q')}",
            "train": [
                str(calendar[train_start_index].date()),
                str(calendar[train_end_index].date()),
            ],
            "valid": [
                str(calendar[valid_start_index].date()),
                str(calendar[valid_end_index].date()),
            ],
            "test": [str(fold_dates[0].date()), str(fold_dates[-1].date())],
            "testSessions": int(test_end_index - test_start_index + 1),
        }
        if train_end_index + label_horizon_sessions >= valid_start_index:
            raise AssertionError(f"Train/valid purge failed for {fold['id']}")
        if valid_end_index + label_horizon_sessions >= test_start_index:
            raise AssertionError(f"Valid/test purge failed for {fold['id']}")
        folds.append(fold)

    covered = []
    for fold in folds:
        covered.extend(
            calendar[
                (calendar >= pd.Timestamp(fold["test"][0]))
                & (calendar <= pd.Timestamp(fold["test"][1]))
            ].tolist()
        )
    if not pd.DatetimeIndex(covered).equals(test_dates):
        raise AssertionError("Walk-forward folds do not exactly cover the test calendar")
    return folds


def _train_predictions(
    runtime_path: Path,
    folds: list[dict[str, Any]],
    run_dir: Path,
    expected_factor_count: int,
) -> tuple[pd.Series, list[dict[str, Any]]]:
    import lightgbm as lgb
    from qlib.contrib.model.gbdt import LGBModel
    from qlib.data.dataset import DatasetH
    from qlib.data.dataset.handler import DataHandlerLP
    from qlib.workflow import R

    runner = BacktestRunner(str(runtime_path))
    runner._init_qlib()
    factor_expressions, custom_factors = runner._load_factors()
    if custom_factors or len(factor_expressions) != expected_factor_count:
        raise ValueError(
            "Walk-forward factor contract mismatch: "
            f"qlib={len(factor_expressions)}, custom={len(custom_factors)}"
        )
    base_dataset = runner._create_dataset(factor_expressions, None)
    model_params = runner.config["model"]["params"]
    prediction_parts: list[pd.Series] = []
    fold_results: list[dict[str, Any]] = []
    experiment_name = f"{run_dir.name}_walk_forward"

    with R.start(experiment_name=experiment_name, recorder_name=run_dir.name):
        for fold in folds:
            dataset = DatasetH(
                handler=base_dataset.handler,
                segments={
                    "train": tuple(fold["train"]),
                    "valid": tuple(fold["valid"]),
                    "test": tuple(fold["test"]),
                },
            )
            probe = LGBModel(**model_params)
            probe.fit(dataset, verbose_eval=0)
            best_iteration = int(
                probe.model.best_iteration
                or probe.model.current_iteration()
                or model_params.get("num_boost_round", 1)
            )
            train_frame, valid_frame = dataset.prepare(
                ["train", "valid"],
                col_set=["feature", "label"],
                data_key=DataHandlerLP.DK_L,
            )
            refit_frame = pd.concat([train_frame, valid_frame], axis=0).sort_index()
            x_refit = refit_frame["feature"]
            y_refit = refit_frame["label"].to_numpy().reshape(-1)
            refit_params = dict(probe.params)
            for key in ("early_stopping_round", "early_stopping_rounds", "early_stopping"):
                refit_params.pop(key, None)
            booster = lgb.train(
                refit_params,
                lgb.Dataset(x_refit.to_numpy(), label=y_refit),
                num_boost_round=best_iteration,
            )
            test_features = dataset.prepare(
                "test", col_set="feature", data_key=DataHandlerLP.DK_I
            )
            prediction = pd.Series(
                booster.predict(test_features.to_numpy()),
                index=test_features.index,
                name="score",
            )
            prediction_dates = pd.DatetimeIndex(
                prediction.index.get_level_values("datetime")
            )
            if (
                prediction_dates.min() != pd.Timestamp(fold["test"][0])
                or prediction_dates.max() != pd.Timestamp(fold["test"][1])
                or prediction_dates.nunique() != int(fold["testSessions"])
            ):
                raise ValueError(f"Prediction dates escaped fold {fold['id']}")
            fold_dir = run_dir / "folds" / fold["id"]
            fold_dir.mkdir(parents=True, exist_ok=False)
            model_path = fold_dir / "model.txt"
            prediction_path = fold_dir / "prediction.pkl"
            booster.save_model(str(model_path))
            prediction.to_pickle(prediction_path)
            fold_result = {
                **fold,
                "status": "PASS",
                "bestIteration": best_iteration,
                "trainRows": int(len(train_frame)),
                "validRows": int(len(valid_frame)),
                "testRows": int(len(prediction)),
                "modelPath": str(model_path),
                "modelSha256": _sha256(model_path),
                "predictionPath": str(prediction_path),
                "predictionSha256": _sha256(prediction_path),
            }
            fold_results.append(fold_result)
            prediction_parts.append(prediction)
            del probe, booster, dataset, train_frame, valid_frame, refit_frame
            gc.collect()

    stitched = pd.concat(prediction_parts).sort_index()
    if stitched.index.has_duplicates:
        raise ValueError("Walk-forward stitched prediction has duplicate rows")
    actual_dates = pd.DatetimeIndex(stitched.index.get_level_values("datetime")).unique()
    if len(actual_dates) != sum(int(fold["testSessions"]) for fold in folds):
        raise AssertionError("Walk-forward stitched prediction has missing test dates")
    return stitched, fold_results


def _report(decision: dict[str, Any], candidates: dict[str, Any]) -> str:
    lines = [
        f"# Walk-forward validation {decision['runId']}",
        "",
        f"- Decision: `{decision['status']}`",
        f"- Historical gate passed: `{str(decision['historicalGatePassed']).lower()}`",
        "- Paper eligible: `false`",
        "",
    ]
    for candidate_id in ("S0", "W1"):
        item = candidates.get(candidate_id)
        if not item:
            continue
        metrics = item["analysis"]["standard"]
        lines.extend(
            [
                f"## {candidate_id}",
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
            "This is historical walk-forward research only. It is not a profit guarantee or real-money authorization.",
            "",
        ]
    )
    return "\n".join(lines)


def run_walk_forward_validation(
    project_root: Path,
    config_path: Path,
    audit_only: bool = False,
) -> dict[str, Any]:
    project_root = project_root.resolve()
    config_path = config_path.resolve()
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    runtime_path = _resolve(project_root, config["baseline_runtime_config"])
    baseline_daily_path = _resolve(project_root, config["baseline_daily"])
    source_manifest_path = _resolve(project_root, config["source_manifest"])
    source_decision_path = _resolve(project_root, config["source_decision"])
    runtime = yaml.safe_load(runtime_path.read_text(encoding="utf-8"))
    provider = _resolve(project_root, runtime["data"]["provider_uri"])
    provider_fingerprint = provider_contract_fingerprint(
        provider, runtime["data"]["market"]
    )["fingerprint"]
    source = QlibBinaryDailySource(provider)
    schedule_config = config["schedule"]
    folds = build_walk_forward_schedule(
        pd.to_datetime(source.calendar),
        schedule_config["train_start"],
        schedule_config["test_start"],
        schedule_config["test_end"],
        int(schedule_config["validation_sessions"]),
        int(schedule_config["label_horizon_sessions"]),
    )

    frozen = config["frozen_contract"]
    actual = {
        "runtime_config_sha256": _sha256(runtime_path),
        "baseline_daily_sha256": _sha256(baseline_daily_path),
        "source_manifest_sha256": _sha256(source_manifest_path),
        "source_decision_sha256": _sha256(source_decision_path),
        "provider_path": str(provider),
        "provider_fingerprint": provider_fingerprint,
        "factor_source": runtime["factor_source"]["type"],
        "factor_count": len(FactorLoader.ALPHA158_20_FACTORS)
        if runtime["factor_source"]["type"] == "alpha158_20"
        else None,
        "n_drop": int(config["n_drop"]),
        "fold_count": len(folds),
    }
    checks = [
        _check(
            f"frozen_{key}",
            actual.get(key) == expected,
            actual.get(key),
            expected,
            f"Frozen walk-forward contract mismatch: {key}",
        )
        for key, expected in frozen.items()
    ]
    audit = {
        "status": "PASS" if all(check["status"] == "PASS" for check in checks) else "FAIL",
        "checks": checks,
        "actual": actual,
    }

    output_root = _resolve(project_root, config["output_root"])
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    run_id = str(config.get("run_id") or f"walk_forward_{timestamp}")
    run_dir = output_root / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    _write_json(run_dir / "contract_audit.json", audit)
    _write_json(run_dir / "fold_plan.json", {"folds": folds})
    inputs = [
        config_path,
        runtime_path,
        baseline_daily_path,
        source_manifest_path,
        source_decision_path,
        Path(__file__).resolve(),
    ]
    _write_json(
        run_dir / "manifest.json",
        {
            "runId": run_id,
            "protocolVersion": config["protocol_version"],
            "createdAt": datetime.now(timezone.utc).isoformat(),
            "historicalEvidenceOnly": True,
            "inputs": {str(path): _sha256(path) for path in inputs},
            "foldPlanSha256": _sha256(run_dir / "fold_plan.json"),
            "schedule": schedule_config,
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
            "checks": checks,
            "reasons": [
                *[
                    check["reason"]
                    for check in checks
                    if check["status"] == "FAIL"
                ],
                *(["Audit-only run; walk-forward returns were not opened."] if audit_only else []),
            ],
        }
        _write_json(run_dir / "decision.json", decision)
        (run_dir / "report.md").write_text(_report(decision, {}), encoding="utf-8")
        return {"runId": run_id, "runDir": str(run_dir), "decision": decision}

    prediction, fold_results = _train_predictions(
        runtime_path, folds, run_dir, int(config["factor_count"])
    )
    prediction_path = run_dir / "walk_forward_prediction.pkl"
    prediction.to_pickle(prediction_path)
    _write_json(
        run_dir / "walk_forward_training.json",
        {
            "status": "PASS",
            "folds": fold_results,
            "predictionPath": str(prediction_path),
            "predictionSha256": _sha256(prediction_path),
            "rows": int(len(prediction)),
        },
    )
    candidate_daily_path = run_dir / "trials" / "W1" / "daily.csv"
    candidate_daily = _backtest_prediction(
        runtime, prediction, int(config["n_drop"]), candidate_daily_path
    )
    baseline_daily = _load_daily(baseline_daily_path, "S0")
    candidate_daily = _load_daily(candidate_daily_path, "W1")
    pair_audit = validate_daily_pair(baseline_daily, candidate_daily)
    _write_json(run_dir / "return_pair_audit.json", pair_audit)
    baseline_analysis = analyze_daily_returns(
        baseline_daily, config.get("cost_multipliers", [1.0, 1.5, 2.0])
    )
    candidate_analysis = analyze_daily_returns(
        candidate_daily, config.get("cost_multipliers", [1.0, 1.5, 2.0])
    )
    candidates = {
        "S0": {
            "dailyPath": str(baseline_daily_path),
            "dailySha256": _sha256(baseline_daily_path),
            "analysis": baseline_analysis,
        },
        "W1": {
            "dailyPath": str(candidate_daily_path),
            "dailySha256": _sha256(candidate_daily_path),
            "predictionPath": str(prediction_path),
            "predictionSha256": _sha256(prediction_path),
            "analysis": candidate_analysis,
        },
    }
    bootstrap = _incremental_bootstrap(
        str(baseline_daily_path),
        str(candidate_daily_path),
        iterations=int(config["promotion"].get("bootstrap_iterations", 1000)),
        block_size=int(config["promotion"].get("bootstrap_block_days", 20)),
        seed=int(config["promotion"].get("bootstrap_seed", 42)),
    )
    performance_checks = evaluate_standard_baseline(
        baseline_analysis,
        candidate_analysis,
        config["promotion"],
        bootstrap,
        baseline_label="S0",
        candidate_label="W1",
    )
    historical_gate_passed = all(check["status"] == "PASS" for check in performance_checks)
    decision = {
        "runId": run_id,
        "protocolVersion": config["protocol_version"],
        "status": "RESEARCH_ONLY" if historical_gate_passed else "REJECTED",
        "historicalGatePassed": historical_gate_passed,
        "forwardTrialGatePassed": historical_gate_passed,
        "paperEligible": False,
        "checks": performance_checks,
        "incrementalBootstrap": bootstrap,
        "reasons": [
            check["reason"]
            for check in performance_checks
            if check["status"] == "FAIL"
        ],
    }
    _write_json(run_dir / "candidates.json", candidates)
    _write_json(run_dir / "decision.json", decision)
    (run_dir / "report.md").write_text(
        _report(decision, candidates), encoding="utf-8"
    )
    return {"runId": run_id, "runDir": str(run_dir), "decision": decision}
