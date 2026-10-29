"""Deterministic paper-v3 factor-pool admission on a validation panel."""

from __future__ import annotations

import math
from typing import Any

import numpy as np
import pandas as pd


EXPECTED_INDEX_NAMES = ["datetime", "instrument"]


def _validate_panel(
    factor_values: pd.DataFrame,
    label: pd.Series,
) -> tuple[pd.DataFrame, pd.Series]:
    if not isinstance(factor_values, pd.DataFrame) or factor_values.empty:
        raise ValueError("factor_values must be a non-empty DataFrame")
    if not isinstance(label, pd.Series) or label.empty:
        raise ValueError("label must be a non-empty Series")
    if not isinstance(factor_values.index, pd.MultiIndex):
        raise ValueError("factor_values must use a MultiIndex")
    if not isinstance(label.index, pd.MultiIndex):
        raise ValueError("label must use a MultiIndex")
    if list(factor_values.index.names) != EXPECTED_INDEX_NAMES:
        raise ValueError(
            f"factor_values index names must be {EXPECTED_INDEX_NAMES}"
        )
    if list(label.index.names) != EXPECTED_INDEX_NAMES:
        raise ValueError(f"label index names must be {EXPECTED_INDEX_NAMES}")
    if factor_values.index.has_duplicates or label.index.has_duplicates:
        raise ValueError("factor_values and label indices must be unique")
    if factor_values.columns.has_duplicates:
        raise ValueError("factor names must be unique")

    common_index = factor_values.index.intersection(label.index).sort_values()
    if common_index.empty:
        raise ValueError("factor_values and label do not overlap")
    aligned_factors = factor_values.reindex(common_index).apply(
        pd.to_numeric,
        errors="coerce",
    )
    aligned_label = pd.to_numeric(
        label.reindex(common_index),
        errors="coerce",
    )
    aligned_factors = aligned_factors.replace([np.inf, -np.inf], np.nan)
    aligned_label = aligned_label.replace([np.inf, -np.inf], np.nan)
    return aligned_factors, aligned_label


def _daily_spearman(
    left: pd.Series,
    right: pd.Series,
    *,
    min_cross_section_size: int,
) -> pd.Series:
    joined = pd.concat(
        [left.rename("left"), right.rename("right")],
        axis=1,
    )

    def correlation(frame: pd.DataFrame) -> float:
        valid = frame.dropna()
        if len(valid) < min_cross_section_size:
            return math.nan
        if (
            valid["left"].nunique(dropna=True) < 2
            or valid["right"].nunique(dropna=True) < 2
        ):
            return math.nan
        left_rank = valid["left"].rank(method="average")
        right_rank = valid["right"].rank(method="average")
        return float(left_rank.corr(right_rank))

    return joined.groupby(level="datetime", sort=True).apply(
        correlation,
        include_groups=False,
    )


def select_paper_factor_pool(
    factor_values: pd.DataFrame,
    label: pd.Series,
    *,
    max_abs_correlation: float = 0.7,
    max_factors: int = 150,
    max_fraction: float = 0.5,
    min_cross_section_size: int = 30,
    min_valid_days: int = 60,
) -> dict[str, Any]:
    """Apply the paper's validation-only greedy RankIC admission rule.

    Pairwise output redundancy is measured as mean daily cross-sectional
    Spearman correlation. This choice is explicit because the paper only says
    "absolute correlation on the 2021 validation panel" without publishing the
    aggregation formula.
    """
    if not 0 < max_abs_correlation < 1:
        raise ValueError("max_abs_correlation must be between 0 and 1")
    if max_factors < 1:
        raise ValueError("max_factors must be positive")
    if not 0 < max_fraction <= 1:
        raise ValueError("max_fraction must be in (0, 1]")
    if min_cross_section_size < 2:
        raise ValueError("min_cross_section_size must be at least 2")
    if min_valid_days < 1:
        raise ValueError("min_valid_days must be positive")

    factors, aligned_label = _validate_panel(factor_values, label)
    factor_names = [str(column) for column in factors.columns]
    if len(set(factor_names)) != len(factor_names):
        raise ValueError("factor names must remain unique when converted to strings")
    factors.columns = factor_names

    ranking: list[dict[str, Any]] = []
    for factor_name in factor_names:
        daily_rank_ic = _daily_spearman(
            factors[factor_name],
            aligned_label,
            min_cross_section_size=min_cross_section_size,
        ).dropna()
        mean_rank_ic = (
            float(daily_rank_ic.mean())
            if len(daily_rank_ic) >= min_valid_days
            else None
        )
        ranking.append(
            {
                "factor": factor_name,
                "meanRankIC": mean_rank_ic,
                "validDays": int(len(daily_rank_ic)),
                "validObservations": int(
                    pd.concat(
                        [factors[factor_name], aligned_label],
                        axis=1,
                    )
                    .dropna()
                    .shape[0]
                ),
            }
        )

    ranked = sorted(
        ranking,
        key=lambda item: (
            item["meanRankIC"] is None,
            -item["meanRankIC"] if item["meanRankIC"] is not None else 0.0,
            item["factor"],
        ),
    )
    candidate_count = len(ranked)
    pool_limit = min(
        max_factors,
        int(math.floor(candidate_count * max_fraction)),
    )
    selected: list[str] = []
    decisions: list[dict[str, Any]] = []

    for item in ranked:
        factor_name = item["factor"]
        if item["meanRankIC"] is None:
            decisions.append(
                {
                    **item,
                    "status": "REJECTED",
                    "reason": "insufficient_valid_rankic_days",
                    "maxAbsCorrelation": None,
                    "correlatedWith": None,
                }
            )
            continue
        if len(selected) >= pool_limit:
            decisions.append(
                {
                    **item,
                    "status": "REJECTED",
                    "reason": "pool_size_cap",
                    "maxAbsCorrelation": None,
                    "correlatedWith": None,
                }
            )
            continue

        maximum = -1.0
        maximum_parent: str | None = None
        insufficient_overlap = False
        for selected_name in selected:
            daily_correlation = _daily_spearman(
                factors[factor_name],
                factors[selected_name],
                min_cross_section_size=min_cross_section_size,
            ).dropna()
            if len(daily_correlation) < min_valid_days:
                insufficient_overlap = True
                maximum_parent = selected_name
                break
            absolute_correlation = abs(float(daily_correlation.mean()))
            if absolute_correlation > maximum:
                maximum = absolute_correlation
                maximum_parent = selected_name

        if insufficient_overlap:
            decisions.append(
                {
                    **item,
                    "status": "REJECTED",
                    "reason": "insufficient_pairwise_correlation_days",
                    "maxAbsCorrelation": None,
                    "correlatedWith": maximum_parent,
                }
            )
            continue
        if maximum >= max_abs_correlation:
            decisions.append(
                {
                    **item,
                    "status": "REJECTED",
                    "reason": "correlation_threshold",
                    "maxAbsCorrelation": maximum,
                    "correlatedWith": maximum_parent,
                }
            )
            continue

        selected.append(factor_name)
        decisions.append(
            {
                **item,
                "status": "SELECTED",
                "reason": "rankic_and_correlation_pass",
                "maxAbsCorrelation": maximum if maximum >= 0 else None,
                "correlatedWith": maximum_parent,
            }
        )

    return {
        "method": "paper_v3_validation_rankic_greedy",
        "correlationMethod": "mean_daily_cross_sectional_spearman",
        "candidateCount": candidate_count,
        "poolLimit": pool_limit,
        "selectedCount": len(selected),
        "selectedFactors": selected,
        "parameters": {
            "maxAbsCorrelation": max_abs_correlation,
            "maxFactors": max_factors,
            "maxFraction": max_fraction,
            "minCrossSectionSize": min_cross_section_size,
            "minValidDays": min_valid_days,
        },
        "decisions": decisions,
    }

