from __future__ import annotations

import re
import time
from typing import Any, Iterable

import pandas as pd
import requests

from .a_stock_data import normalize_stock_code


EASTMONEY_FINANCE_BASE = (
    "https://emweb.securities.eastmoney.com/PC_HSF10/NewFinanceAnalysis"
)
STATEMENT_CONTRACTS = {
    "income_quarterly": {
        "date_path": "lrbDateAjaxNew",
        "detail_path": "lrbAjaxNew",
        "date_type": "2",
        "detail_date_type": "0",
        "report_type": "2",
        "required_value": "DEDUCT_PARENT_NETPROFIT",
    },
    "balance_report": {
        "date_path": "zcfzbDateAjaxNew",
        "detail_path": "zcfzbAjaxNew",
        "date_type": "0",
        "detail_date_type": "0",
        "report_type": "1",
        "required_value": "TOTAL_PARENT_EQUITY",
    },
}
REQUIRED_METADATA_FIELDS = {
    "REPORT_DATE",
    "NOTICE_DATE",
    "UPDATE_DATE",
    "SECURITY_CODE",
}


class EastmoneyFinancialError(RuntimeError):
    """Eastmoney transport or response-contract error."""


def to_eastmoney_finance_code(code: str) -> str:
    digits, exchange, _ = normalize_stock_code(code)
    return f"{exchange}{digits}"


def _date_series(frame: pd.DataFrame, field: str) -> pd.Series:
    return pd.to_datetime(frame[field], errors="coerce").dt.normalize()


def _quarter_ordinal(value: pd.Timestamp) -> int:
    return int(value.year * 4 + (value.month - 1) // 3)


class EastmoneyFinancialClient:
    """Small dependency-free client for Eastmoney's public statement routes."""

    def __init__(
        self,
        timeout: float = 20.0,
        request_interval: float = 0.05,
        max_attempts: int = 3,
        retry_delay: float = 0.5,
        session: requests.Session | None = None,
    ):
        self.timeout = timeout
        self.request_interval = max(float(request_interval), 0.0)
        self.max_attempts = max(int(max_attempts), 1)
        self.retry_delay = max(float(retry_delay), 0.0)
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": "Mozilla/5.0"})

    def _get_json(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        url = f"{EASTMONEY_FINANCE_BASE}/{path}"
        last_error: Exception | None = None
        for attempt in range(1, self.max_attempts + 1):
            try:
                response = self.session.get(url, params=params, timeout=self.timeout)
                response.raise_for_status()
                payload = response.json()
                if not isinstance(payload, dict):
                    raise ValueError("response JSON is not an object")
                return payload
            except Exception as exc:
                last_error = exc
                if attempt < self.max_attempts and self.retry_delay:
                    time.sleep(self.retry_delay * attempt)
        raise EastmoneyFinancialError(
            f"Eastmoney request failed for {path} after {self.max_attempts} attempts: {last_error}"
        ) from last_error

    def company_type(self, code: str) -> str:
        symbol = to_eastmoney_finance_code(code)
        last_error: Exception | None = None
        response_text: str | None = None
        for attempt in range(1, self.max_attempts + 1):
            try:
                response = self.session.get(
                    f"{EASTMONEY_FINANCE_BASE}/Index",
                    params={"type": "web", "code": symbol.lower()},
                    timeout=self.timeout,
                )
                response.raise_for_status()
                response_text = response.text
                break
            except Exception as exc:
                last_error = exc
                if attempt < self.max_attempts and self.retry_delay:
                    time.sleep(self.retry_delay * attempt)
        if response_text is None:
            raise EastmoneyFinancialError(
                f"Eastmoney company-type request failed for {symbol} "
                f"after {self.max_attempts} attempts: {last_error}"
            ) from last_error
        match = re.search(
            r'id=["\']hidctype["\'][^>]*value=["\']([^"\']+)', response_text
        )
        if not match:
            raise EastmoneyFinancialError(
                f"Eastmoney omitted company type for {symbol}"
            )
        return match.group(1)

    def statement(
        self,
        code: str,
        statement: str,
        *,
        start_report_date: str | pd.Timestamp | None = None,
        end_report_date: str | pd.Timestamp | None = None,
    ) -> pd.DataFrame:
        if statement not in STATEMENT_CONTRACTS:
            raise EastmoneyFinancialError(f"unsupported statement: {statement}")
        contract = STATEMENT_CONTRACTS[statement]
        symbol = to_eastmoney_finance_code(code)
        company_type = self.company_type(code)
        date_payload = self._get_json(
            contract["date_path"],
            {
                "companyType": company_type,
                "reportDateType": contract["date_type"],
                "code": symbol,
            },
        )
        date_rows = date_payload.get("data")
        if not isinstance(date_rows, list) or not date_rows:
            raise EastmoneyFinancialError(
                f"Eastmoney returned no report dates for {symbol} {statement}"
            )
        start = (
            pd.Timestamp(start_report_date).normalize()
            if start_report_date is not None
            else None
        )
        end = (
            pd.Timestamp(end_report_date).normalize()
            if end_report_date is not None
            else None
        )
        dates = []
        for row in date_rows:
            if not isinstance(row, dict) or not row.get("REPORT_DATE"):
                raise EastmoneyFinancialError(
                    f"Eastmoney returned malformed report dates for {symbol} {statement}"
                )
            report_date = pd.to_datetime(row["REPORT_DATE"], errors="coerce")
            if pd.isna(report_date):
                raise EastmoneyFinancialError(
                    f"Eastmoney returned an invalid report date for {symbol} {statement}"
                )
            report_date = report_date.normalize()
            if start is not None and report_date < start:
                continue
            if end is not None and report_date > end:
                continue
            dates.append(report_date.strftime("%Y-%m-%d"))
        if not dates:
            raise EastmoneyFinancialError(
                f"Eastmoney returned no report dates inside the requested window "
                f"for {symbol} {statement}"
            )

        batches = [dates[index : index + 5] for index in range(0, len(dates), 5)]
        rows: list[dict[str, Any]] = []
        for batch in batches:
            payload = self._get_json(
                contract["detail_path"],
                {
                    "companyType": company_type,
                    "reportDateType": contract["detail_date_type"],
                    "reportType": contract["report_type"],
                    "dates": ",".join(batch),
                    "code": symbol,
                },
            )
            detail_rows = payload.get("data")
            if not isinstance(detail_rows, list):
                raise EastmoneyFinancialError(
                    f"Eastmoney returned malformed details for {symbol} {statement}"
                )
            if any(not isinstance(row, dict) for row in detail_rows):
                raise EastmoneyFinancialError(
                    f"Eastmoney returned non-object rows for {symbol} {statement}"
                )
            rows.extend(detail_rows)
            if self.request_interval:
                time.sleep(self.request_interval)

        frame = pd.DataFrame.from_records(rows)
        required = REQUIRED_METADATA_FIELDS | {contract["required_value"]}
        missing = sorted(required - set(frame.columns))
        if missing:
            raise EastmoneyFinancialError(
                f"Eastmoney omitted required fields for {symbol} {statement}: {missing}"
            )
        for field in ("REPORT_DATE", "NOTICE_DATE", "UPDATE_DATE"):
            frame[field] = _date_series(frame, field)
        if frame["REPORT_DATE"].isna().any():
            raise EastmoneyFinancialError(
                f"Eastmoney returned invalid report dates for {symbol} {statement}"
            )
        if frame.duplicated(subset=["REPORT_DATE"], keep=False).any():
            raise EastmoneyFinancialError(
                f"Eastmoney returned duplicate report periods for {symbol} {statement}"
            )
        return frame.sort_values("REPORT_DATE").reset_index(drop=True)

    def income_quarterly(
        self,
        code: str,
        *,
        start_report_date: str | pd.Timestamp | None = None,
        end_report_date: str | pd.Timestamp | None = None,
    ) -> pd.DataFrame:
        return self.statement(
            code,
            "income_quarterly",
            start_report_date=start_report_date,
            end_report_date=end_report_date,
        )

    def balance_report(
        self,
        code: str,
        *,
        start_report_date: str | pd.Timestamp | None = None,
        end_report_date: str | pd.Timestamp | None = None,
    ) -> pd.DataFrame:
        return self.statement(
            code,
            "balance_report",
            start_report_date=start_report_date,
            end_report_date=end_report_date,
        )


def audit_conservative_droe_structure(
    codes: Iterable[str],
    *,
    start_report_date: str,
    end_report_date: str,
    selection_year: int = 2021,
    minimum_ratio: float = 0.5,
    client: EastmoneyFinancialClient | None = None,
) -> dict[str, Any]:
    """Audit date/field viability without using or reporting financial values."""
    source = client or EastmoneyFinancialClient()
    start = pd.Timestamp(start_report_date).normalize()
    end = pd.Timestamp(end_report_date).normalize()
    if end < start:
        raise ValueError("end_report_date must be on or after start_report_date")
    expected_report_dates = pd.date_range(start=start, end=end, freq="QE")

    code_results = []
    total_expected = 0
    total_structural = 0
    total_timely = 0
    selection_expected = 0
    selection_timely = 0
    update_after_notice = 0
    invalid_availability_dates = 0

    for code in codes:
        income = source.income_quarterly(
            code,
            start_report_date=start - pd.DateOffset(months=12),
            end_report_date=end,
        )
        balance = source.balance_report(
            code,
            start_report_date=start - pd.DateOffset(months=15),
            end_report_date=end,
        )
        income_dates = {
            _quarter_ordinal(row.REPORT_DATE): row
            for row in income.itertuples(index=False)
        }
        balance_dates = {
            _quarter_ordinal(row.REPORT_DATE): row
            for row in balance.itertuples(index=False)
        }
        update_after_notice += int((income["UPDATE_DATE"] > income["NOTICE_DATE"]).sum())
        update_after_notice += int((balance["UPDATE_DATE"] > balance["NOTICE_DATE"]).sum())
        invalid_availability_dates += int(
            income[["NOTICE_DATE", "UPDATE_DATE"]].isna().any(axis=1).sum()
        )
        invalid_availability_dates += int(
            balance[["NOTICE_DATE", "UPDATE_DATE"]].isna().any(axis=1).sum()
        )

        expected = 0
        structural = 0
        timely = 0
        code_selection_expected = 0
        code_selection_timely = 0
        for report_date in expected_report_dates:
            expected += 1
            if report_date.year == selection_year:
                code_selection_expected += 1
            quarter = _quarter_ordinal(report_date)
            required_rows = (
                income_dates.get(quarter),
                balance_dates.get(quarter - 1),
                income_dates.get(quarter - 4),
                balance_dates.get(quarter - 5),
            )
            if any(item is None for item in required_rows):
                continue
            structural += 1
            if any(
                pd.isna(item.NOTICE_DATE) or pd.isna(item.UPDATE_DATE)
                for item in required_rows
            ):
                continue
            availability = max(
                max(item.NOTICE_DATE, item.UPDATE_DATE) for item in required_rows
            )
            deadline = report_date + pd.DateOffset(months=6)
            if availability <= deadline:
                timely += 1
                if report_date.year == selection_year:
                    code_selection_timely += 1

        total_expected += expected
        total_structural += structural
        total_timely += timely
        selection_expected += code_selection_expected
        selection_timely += code_selection_timely
        code_results.append(
            {
                "code": code,
                "expectedReportPeriods": expected,
                "structuralDroEPeriods": structural,
                "timelyDroEPeriods": timely,
                "timelyRatio": timely / expected if expected else 0.0,
                "selectionYearExpected": code_selection_expected,
                "selectionYearTimely": code_selection_timely,
                "selectionYearTimelyRatio": (
                    code_selection_timely / code_selection_expected
                    if code_selection_expected
                    else 0.0
                ),
            }
        )

    timely_ratio = total_timely / total_expected if total_expected else 0.0
    selection_ratio = (
        selection_timely / selection_expected if selection_expected else 0.0
    )
    failed_checks = []
    if timely_ratio < minimum_ratio:
        failed_checks.append("minimumTimelyDroERatio")
    if selection_ratio < minimum_ratio:
        failed_checks.append("minimumSelectionYearTimelyDroERatio")
    return {
        "status": "PASS" if not failed_checks else "FAIL",
        "source": "eastmoney_public_current_snapshot",
        "codes": code_results,
        "codeCount": len(code_results),
        "startReportDate": start.strftime("%Y-%m-%d"),
        "endReportDate": end.strftime("%Y-%m-%d"),
        "expectedReportPeriods": total_expected,
        "structuralDroEPeriods": total_structural,
        "timelyDroEPeriods": total_timely,
        "timelyDroERatio": timely_ratio,
        "selectionYear": selection_year,
        "selectionYearExpected": selection_expected,
        "selectionYearTimely": selection_timely,
        "selectionYearTimelyRatio": selection_ratio,
        "updateAfterNoticeRows": update_after_notice,
        "invalidAvailabilityDateRows": invalid_availability_dates,
        "minimumRatio": minimum_ratio,
        "failedChecks": failed_checks,
        "financialValuesUsedForAudit": False,
        "financialValuesExposed": False,
        "returnsOrPricesRead": False,
        "artifactsWritten": False,
    }
