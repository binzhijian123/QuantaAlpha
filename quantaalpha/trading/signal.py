from __future__ import annotations

import hashlib
import math
import pickle
import re
from pathlib import Path
from typing import Any, Iterable, Mapping

import numpy as np
import pandas as pd

from quantaalpha.backtest.custom_factor_calculator import CustomFactorCalculator
from quantaalpha.trading.execution_contract import (
    position_sessions_sha256,
    symbols_sha256,
    validate_topk_dropout_contract,
)
from quantaalpha.trading.market_data import QlibBinaryDailySource
from quantaalpha.utils.qlib_provider import provider_contract_fingerprint
from quantaalpha.utils.provider_lineage import validate_lineage_chain


def build_model_signal_source(generated: dict[str, Any]) -> dict[str, Any]:
    strategy = generated["strategy"]
    return {
        "type": "model_snapshot",
        "ref": strategy["snapshotId"],
        "topK": generated["topK"],
        "exposure": generated["exposure"],
        "validScoreCount": generated["validScoreCount"],
        "ranking": generated["ranking"],
        "executionMode": "topk_dropout",
        "executionContract": generated["executionContract"],
        "executionPlan": generated["executionPlan"],
    }


def build_research_shadow_signal_source(
    generated: dict[str, Any], run_id: str, arm: str
) -> dict[str, Any]:
    source = build_model_signal_source(generated)
    return {
        **source,
        "usageRealm": "RESEARCH_SHADOW",
        "runId": run_id,
        "arm": arm,
        "formalPaperEligible": False,
        "automaticPromotionAllowed": False,
    }


def build_topk_dropout_plan(
    scores: pd.Series,
    current_symbols: Iterable[str],
    top_k: int,
    n_drop: int,
    hold_threshold: int = 1,
    holding_sessions: Mapping[str, int] | None = None,
) -> dict[str, Any]:
    """Replicate Qlib TopkDropout's deterministic top/bottom stock selection."""
    if top_k < 1 or n_drop < 1 or n_drop > top_k:
        raise ValueError("invalid TopkDropout top_k/n_drop")
    try:
        normalized_hold_threshold = int(hold_threshold)
        numeric_hold_threshold = float(hold_threshold)
    except (TypeError, ValueError) as exc:
        raise ValueError("hold_threshold must be a positive integer") from exc
    if (
        isinstance(hold_threshold, bool)
        or not math.isfinite(numeric_hold_threshold)
        or numeric_hold_threshold != normalized_hold_threshold
        or normalized_hold_threshold < 1
    ):
        raise ValueError("hold_threshold must be positive")
    hold_threshold = normalized_hold_threshold
    ranked = pd.to_numeric(scores, errors="coerce").replace(
        [np.inf, -np.inf], np.nan
    )
    if ranked.index.has_duplicates:
        raise ValueError("model scores contain duplicate symbols")
    ranked.index = ranked.index.map(str)
    current = list(dict.fromkeys(str(symbol) for symbol in current_symbols))
    if len(current) > top_k + n_drop:
        raise ValueError(
            "current model portfolio has "
            f"{len(current)} positions, exceeding recovery limit topK+nDrop={top_k + n_drop}"
        )
    if holding_sessions is None:
        if current and hold_threshold > 1:
            raise ValueError("holding sessions are required for a multi-session hold threshold")
        normalized_sessions = {symbol: 1 for symbol in current}
    else:
        raw_sessions = {str(symbol): value for symbol, value in holding_sessions.items()}
        if set(raw_sessions) != set(current):
            raise ValueError("holding sessions must match current symbols exactly")
        normalized_sessions: dict[str, int] = {}
        for symbol in current:
            try:
                sessions = int(raw_sessions[symbol])
                numeric_sessions = float(raw_sessions[symbol])
            except (TypeError, ValueError) as exc:
                raise ValueError(f"invalid holding sessions for {symbol}") from exc
            if (
                isinstance(raw_sessions[symbol], bool)
                or not math.isfinite(numeric_sessions)
                or numeric_sessions != sessions
                or sessions < 1
            ):
                raise ValueError(f"holding sessions for {symbol} must be positive")
            normalized_sessions[symbol] = sessions

    last = ranked.reindex(current).sort_values(ascending=False).index
    candidate_count = n_drop + top_k - len(last)
    today = ranked[~ranked.index.isin(last)].sort_values(ascending=False).index[
        :candidate_count
    ]
    combined = ranked.reindex(last.union(pd.Index(today))).sort_values(
        ascending=False
    ).index
    bottom = combined[-n_drop:] if n_drop else pd.Index([])
    sell_candidates = [str(symbol) for symbol in last[last.isin(bottom)]]
    buy_count = len(sell_candidates) + top_k - len(last)
    buy = today[:buy_count]
    sell_symbols = [
        symbol
        for symbol in sell_candidates
        if normalized_sessions[symbol] >= hold_threshold
    ]
    hold_blocked_symbols = [
        symbol
        for symbol in sell_candidates
        if normalized_sessions[symbol] < hold_threshold
    ]
    buy_symbols = [str(symbol) for symbol in buy]
    keep_symbols = [
        symbol for symbol in current if symbol not in set(sell_candidates)
    ]
    return {
        "currentSymbols": current,
        "currentHoldingSessions": normalized_sessions,
        "keepSymbols": keep_symbols,
        "sellCandidates": sell_candidates,
        "sellSymbols": sell_symbols,
        "holdBlockedSymbols": hold_blocked_symbols,
        "buySymbols": buy_symbols,
        "desiredSymbols": [*keep_symbols, *buy_symbols],
    }


class PaperStrategySignalGenerator:
    """Generate target weights from a frozen, backtest-verified model snapshot."""

    def __init__(self, snapshot_path: str | Path, source: QlibBinaryDailySource | None = None):
        self.snapshot_path = Path(snapshot_path)
        self.source = source or QlibBinaryDailySource()

    def describe(self) -> dict[str, Any]:
        payload = self._load_snapshot()
        return self._describe_payload(payload)

    def _describe_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        metrics = payload.get("backtestMetrics", {})
        eligibility = self._paper_eligibility(payload)
        return {
            "available": True,
            "eligibleForPaper": eligibility["eligible"],
            "validationStatus": eligibility["status"],
            "validationReasons": eligibility["reasons"],
            "snapshotPath": str(self.snapshot_path),
            "snapshotId": self._snapshot_id(),
            "createdAt": payload.get("createdAt"),
            "factorSource": payload.get("factorSource"),
            "factorJson": payload.get("factorJson", []),
            "factorCount": len(payload.get("factors", [])),
            "featureCount": len(payload.get("featureColumns", [])),
            "trainingSegments": payload.get("trainingSegments", {}),
            "backtestMetrics": metrics,
            "trainingProviderPath": payload.get("providerPath"),
            "signalProviderPath": str(self.source.provider_path),
            "executionContract": payload.get("executionContract"),
        }

    def generate_targets(
        self,
        signal_date: str | None = None,
        top_k: int | None = None,
        exposure: float | None = None,
        current_symbols: Iterable[str] = (),
        current_holding_sessions: Mapping[str, int] | None = None,
    ) -> dict[str, Any]:
        payload = self._load_snapshot()
        eligibility = self._paper_eligibility(payload)
        if not eligibility["eligible"]:
            raise ValueError("策略未通过收益验证：" + "；".join(eligibility["reasons"]))
        contract = validate_topk_dropout_contract(payload.get("executionContract"))
        frozen_top_k = contract["topK"]
        frozen_exposure = contract["riskDegree"]
        if top_k is not None and top_k != frozen_top_k:
            raise ValueError(
                f"请求持仓数量 {top_k} 与冻结策略 {frozen_top_k} 不一致"
            )
        if exposure is not None and not math.isclose(
            exposure, frozen_exposure, rel_tol=0, abs_tol=1e-12
        ):
            raise ValueError(
                f"请求目标仓位 {exposure} 与冻结策略 {frozen_exposure} 不一致"
            )
        top_k = frozen_top_k
        exposure = frozen_exposure
        signal_date = signal_date or self.source.latest_dates(1)[-1]
        if signal_date not in self.source.calendar:
            raise ValueError(f"signal date is not available in local market data: {signal_date}")

        raw_fields = {"close"}
        for factor in payload["factors"]:
            raw_fields.update(
                re.findall(r"\$([A-Za-z][A-Za-z0-9_]*)", factor.get("expression", ""))
            )
        raw_fields.discard("return")
        history = self.source.history_frame(signal_date, fields=sorted(raw_fields))
        if history.empty:
            raise ValueError("local market data is empty")
        calculator = CustomFactorCalculator(history)
        calculated: dict[str, pd.Series] = {}
        for factor in payload["factors"]:
            name = factor.get("name", "")
            expression = factor.get("expression", "")
            if not name or not expression:
                raise ValueError("strategy snapshot contains an invalid factor definition")
            result = calculator.calculate_factor(name, expression)
            if result is None:
                raise ValueError(f"failed to calculate strategy factor: {name}")
            calculated[name] = result

        features = pd.DataFrame(calculated).fillna(0.0)
        datetime_level = features.index.names[0] if features.index.names[0] else 0
        for column in features.columns:
            if np.isinf(features[column].to_numpy()).any():
                features[column] = features.groupby(level=datetime_level)[column].transform(
                    lambda values: values.replace(
                        [np.inf, -np.inf], values[~np.isinf(values)].mean()
                    )
                )
        try:
            latest = features.xs(pd.Timestamp(signal_date), level="datetime").copy()
        except KeyError as exc:
            raise ValueError(f"no feature rows are available on {signal_date}") from exc
        latest = latest.groupby(level=0).last()
        latest = (latest.rank(pct=True) - 0.5) * 3.46

        feature_columns = payload["featureColumns"]
        missing = sorted(set(feature_columns) - set(latest.columns))
        if missing:
            raise ValueError("strategy features are missing: " + ", ".join(missing))
        model = payload["model"]
        booster = getattr(model, "model", None)
        if booster is None:
            raise ValueError("strategy snapshot does not contain a fitted model")
        scores = pd.Series(
            booster.predict(latest[feature_columns]),
            index=latest.index,
            name="score",
        ).replace([np.inf, -np.inf], np.nan).dropna().sort_values(ascending=False)
        if len(scores) < top_k:
            raise ValueError(f"only {len(scores)} valid scores are available; top_k={top_k}")

        plan = build_topk_dropout_plan(
            scores,
            current_symbols,
            top_k,
            contract["nDrop"],
            contract["holdThreshold"],
            current_holding_sessions,
        )
        selected = scores.head(top_k)
        target_weight = exposure / top_k
        targets = {
            str(symbol): target_weight for symbol in plan["desiredSymbols"]
        }
        plan = {
            **plan,
            "topK": top_k,
            "nDrop": contract["nDrop"],
            "riskDegree": exposure,
            "currentSymbolsSha256": symbols_sha256(plan["currentSymbols"]),
        }
        if contract["version"] >= 2:
            plan["currentPositionsSha256"] = position_sessions_sha256(
                plan["currentHoldingSessions"]
            )
        return {
            "signalDate": signal_date,
            "targets": targets,
            "ranking": [
                {
                    "symbol": str(symbol),
                    "score": float(score),
                    "targetWeight": targets.get(str(symbol), 0.0),
                }
                for symbol, score in selected.items()
            ],
            "validScoreCount": len(scores),
            "topK": top_k,
            "exposure": exposure,
            "executionPlan": plan,
            "executionContract": contract,
            "strategy": self._describe_payload(payload),
        }

    def _load_snapshot(self) -> dict[str, Any]:
        if not self.snapshot_path.exists():
            raise FileNotFoundError(f"paper strategy snapshot not found: {self.snapshot_path}")
        with self.snapshot_path.open("rb") as file:
            payload = pickle.load(file)
        if not isinstance(payload, dict) or payload.get("version") != 1:
            raise ValueError("unsupported paper strategy snapshot")
        required = {"model", "featureColumns", "factors"}
        missing = sorted(required - set(payload))
        if missing:
            raise ValueError("paper strategy snapshot is incomplete: " + ", ".join(missing))
        if not payload["featureColumns"] or not payload["factors"]:
            raise ValueError("paper strategy snapshot has no features")
        return payload

    def _paper_eligibility(self, payload: dict[str, Any]) -> dict[str, Any]:
        validation = payload.get("profitValidation") or {}
        status = validation.get("status", "RESEARCH_ONLY")
        recorded_reasons = [str(reason) for reason in validation.get("reasons", []) if reason]
        reasons: list[str] = []
        if status not in {"PAPER_CANDIDATE", "PAPER_VALIDATED"}:
            reasons.extend(recorded_reasons)
            reasons.append(f"验证状态为 {status}，未达到 PAPER_CANDIDATE")

        training_provider = payload.get("providerPath")
        if not training_provider:
            reasons.append("快照缺少训练数据提供器来源")
        else:
            expected = Path(training_provider).expanduser().resolve()
            actual = self.source.provider_path.expanduser().resolve()
            if expected != actual:
                reasons.append(f"训练与信号数据提供器不一致：{expected} != {actual}")

        try:
            validate_topk_dropout_contract(payload.get("executionContract"))
        except (TypeError, ValueError) as exc:
            reasons.append(str(exc))

        return {
            "eligible": not reasons,
            "status": status,
            "reasons": list(dict.fromkeys(reasons)),
        }

    def _snapshot_id(self) -> str:
        digest = hashlib.sha256()
        with self.snapshot_path.open("rb") as file:
            for chunk in iter(lambda: file.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()[:16]


class ResearchShadowSignalGenerator(PaperStrategySignalGenerator):
    """Generate signals only for an explicitly isolated research-shadow run."""

    def _describe_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        description = super()._describe_payload(payload)
        description.update(
            {
                "eligibleForPaper": False,
                "eligibleForResearchShadow": not description["validationReasons"],
                "formalPaperEligible": False,
                "usageRealm": "RESEARCH_SHADOW",
                "historicalStatus": (payload.get("profitValidation") or {}).get(
                    "historicalStatus", "REJECTED"
                ),
                "automaticPromotionAllowed": False,
                "declarationSha256": payload.get("declarationSha256"),
                "providerFingerprint": (
                    payload.get("providerContract") or {}
                ).get("fingerprint"),
            }
        )
        return description

    def _paper_eligibility(self, payload: dict[str, Any]) -> dict[str, Any]:
        reasons: list[str] = []
        validation = payload.get("profitValidation") or {}
        if validation.get("status") != "RESEARCH_ONLY":
            reasons.append("研究影子快照必须保持 RESEARCH_ONLY")
        if validation.get("historicalStatus") != "REJECTED":
            reasons.append("研究影子快照必须保留历史 REJECTED 结论")
        if payload.get("usageRealm") != "RESEARCH_SHADOW":
            reasons.append("快照未绑定 RESEARCH_SHADOW 使用域")
        if payload.get("paperEligible") is not False:
            reasons.append("研究影子快照不得具备正式模拟盘资格")
        if payload.get("automaticPromotionAllowed") is not False:
            reasons.append("研究影子快照不得允许自动晋级")

        declaration_path = Path(str(payload.get("declarationPath") or ""))
        expected_declaration_sha = str(payload.get("declarationSha256") or "")
        if not declaration_path.is_file() or not expected_declaration_sha:
            reasons.append("研究影子快照缺少冻结声明")
        elif self._file_sha256(declaration_path) != expected_declaration_sha:
            reasons.append("研究影子冻结声明哈希不匹配")

        snapshot_contract = payload.get("providerContract") or {}
        expected_fingerprint = str(snapshot_contract.get("fingerprint") or "")
        market = str(snapshot_contract.get("market") or "csi300")
        if not expected_fingerprint:
            reasons.append("研究影子快照缺少 Provider 指纹")
        else:
            try:
                actual_contract = provider_contract_fingerprint(
                    self.source.provider_path, market
                )
                actual_fingerprint = actual_contract["fingerprint"]
                if actual_fingerprint != expected_fingerprint:
                    self._validate_successor_proof(
                        payload,
                        expected_fingerprint,
                        actual_fingerprint,
                    )
            except (OSError, RuntimeError, ValueError) as exc:
                reasons.append(str(exc))

        try:
            validate_topk_dropout_contract(payload.get("executionContract"))
        except (TypeError, ValueError) as exc:
            reasons.append(str(exc))

        return {
            "eligible": not reasons,
            "status": "RESEARCH_ONLY",
            "reasons": list(dict.fromkeys(reasons)),
        }

    def _validate_successor_proof(
        self,
        payload: dict[str, Any],
        base_fingerprint: str,
        successor_fingerprint: str,
    ) -> None:
        proof_dir = Path(str(payload.get("providerLineageProofDir") or ""))
        validate_lineage_chain(
            proof_dir,
            root_fingerprint=base_fingerprint,
            successor_fingerprint=successor_fingerprint,
            run_id=str(payload.get("runId") or ""),
            declaration_sha256=str(payload.get("declarationSha256") or ""),
            successor_provider_path=self.source.provider_path,
            verify_active_feature_tree=True,
        )

    @staticmethod
    def _file_sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as file:
            for chunk in iter(lambda: file.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
