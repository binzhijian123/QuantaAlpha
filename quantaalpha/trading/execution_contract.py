from __future__ import annotations

import hashlib
import math
from typing import Any, Iterable, Mapping


def _integer(value: Any, field: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"策略快照的 {field} 必须是整数")
    try:
        result = int(value)
        numeric = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"策略快照的 {field} 必须是整数") from exc
    if not math.isfinite(numeric) or numeric != result:
        raise ValueError(f"策略快照的 {field} 必须是整数")
    return result


def validate_topk_dropout_contract(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("策略快照缺少执行契约")
    version = value.get("version")
    if (
        type(version) is not int
        or version not in {1, 2}
        or value.get("type") != "topk_dropout"
    ):
        raise ValueError("策略快照执行契约版本或类型不受支持")
    if value.get("strategyClass") != "TopkDropoutStrategy":
        raise ValueError("策略快照执行类不是 TopkDropoutStrategy")
    if value.get("methodBuy") != "top" or value.get("methodSell") != "bottom":
        raise ValueError("模拟盘只支持确定性的 TopkDropout top/bottom 规则")

    try:
        top_k = _integer(value.get("topK", 0), "topK")
        n_drop = _integer(value.get("nDrop", -1), "nDrop")
        risk_degree = float(value.get("riskDegree", 0))
        hold_threshold = _integer(
            value.get("holdThreshold", 1), "holdThreshold"
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("策略快照的执行参数无效") from exc
    if top_k < 1 or n_drop < 1 or n_drop > top_k:
        raise ValueError("策略快照的 topK/nDrop 无效")
    if not math.isfinite(risk_degree) or not 0 < risk_degree <= 1:
        raise ValueError("策略快照的 riskDegree 无效")
    if hold_threshold < 1:
        raise ValueError("策略快照的 holdThreshold 必须是正整数")
    if version == 1 and hold_threshold != 1:
        raise ValueError("版本 1 模拟盘执行契约只支持 1 日最短持有期")
    if bool(value.get("onlyTradable", False)):
        raise ValueError("模拟盘信号阶段不支持 onlyTradable=true")
    if value.get("forbidAllTradeAtLimit") is not True:
        raise ValueError("模拟盘要求 forbidAllTradeAtLimit=true")
    return {
        **value,
        "version": version,
        "topK": top_k,
        "nDrop": n_drop,
        "riskDegree": risk_degree,
        "holdThreshold": hold_threshold,
    }


def symbols_sha256(symbols: Iterable[str]) -> str:
    return hashlib.sha256(
        "\n".join(sorted(str(symbol) for symbol in symbols)).encode("utf-8")
    ).hexdigest()


def position_sessions_sha256(sessions: Mapping[str, int]) -> str:
    return hashlib.sha256(
        "\n".join(
            f"{symbol}:{int(sessions[symbol])}" for symbol in sorted(sessions)
        ).encode("utf-8")
    ).hexdigest()
