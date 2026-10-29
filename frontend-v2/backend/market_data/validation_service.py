from __future__ import annotations

from typing import Iterable

import pandas as pd

from .models import ValidationIssue


REQUIRED_COLUMNS = {"date", "code", "open", "high", "low", "close", "volume", "amount"}
NUMERIC_COLUMNS = ["open", "high", "low", "close", "volume", "amount"]
PRICE_COLUMNS = ["open", "high", "low", "close"]


def normalize_history_frame(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    for col in NUMERIC_COLUMNS:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    if "date" in df.columns:
        df["date"] = pd.to_datetime(df["date"], errors="coerce").dt.strftime("%Y-%m-%d")
    if "code" in df.columns:
        df["code"] = df["code"].astype(str).str.upper()
    return df


def validate_history_frame(
    df: pd.DataFrame,
    code: str,
    expected_dates: Iterable[str] | None = None,
) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    missing = sorted(REQUIRED_COLUMNS - set(df.columns))
    if missing:
        return [ValidationIssue(code=code, level="error", message="缺少关键字段", details={"missing": missing})]

    if df.empty:
        return [ValidationIssue(code=code, level="error", message="数据源返回空数据")]

    duplicate_dates = df[df.duplicated(subset=["date"], keep=False)]["date"].dropna().unique().tolist()
    if duplicate_dates:
        issues.append(
            ValidationIssue(
                code=code,
                level="error",
                message="同一股票存在重复日期",
                details={"dates": duplicate_dates[:10], "count": len(duplicate_dates)},
            )
        )

    numeric_na = df[NUMERIC_COLUMNS].isna()
    fully_blank_rows = numeric_na.all(axis=1)
    partially_blank_rows = numeric_na.any(axis=1) & ~fully_blank_rows
    if partially_blank_rows.any():
        invalid_numeric: dict[str, int] = {}
        for col in NUMERIC_COLUMNS:
            count = int(df.loc[partially_blank_rows, col].isna().sum())
            if count:
                invalid_numeric[col] = count
        issues.append(ValidationIssue(code=code, level="error", message="数值字段存在部分缺失值", details=invalid_numeric))
    if fully_blank_rows.any():
        issues.append(
            ValidationIssue(
                code=code,
                level="warning",
                message="存在整行空行情，保留为 NaN 以维持 Qlib 日历对齐",
                details={"rows": int(fully_blank_rows.sum()), "sampleDates": df.loc[fully_blank_rows, "date"].head(5).tolist()},
            )
        )

    if (df[PRICE_COLUMNS] < 0).any().any():
        issues.append(ValidationIssue(code=code, level="error", message="价格字段存在负数"))

    bad_range = df[
        (df["high"] < df["low"])
        | (df["open"] > df["high"])
        | (df["open"] < df["low"])
        | (df["close"] > df["high"])
        | (df["close"] < df["low"])
    ]
    if not bad_range.empty:
        issues.append(
            ValidationIssue(
                code=code,
                level="error",
                message="OHLC 价格区间不一致",
                details={"rows": int(len(bad_range)), "sampleDates": bad_range["date"].head(5).tolist()},
            )
        )

    if expected_dates is not None:
        expected = set(expected_dates)
        unexpected = sorted(set(df["date"].dropna()) - expected)
        if unexpected:
            issues.append(
                ValidationIssue(
                    code=code,
                    level="warning",
                    message="存在不在请求窗口内的日期",
                    details={"dates": unexpected[:10], "count": len(unexpected)},
                )
            )

    return issues
