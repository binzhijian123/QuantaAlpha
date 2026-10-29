import hashlib
import json
import sys
import unittest
from pathlib import Path

import pandas as pd
import requests


PROJECT_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = PROJECT_ROOT / "frontend-v2" / "backend"
for path in (PROJECT_ROOT, BACKEND_ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from market_data.providers.eastmoney_financials import (
    EastmoneyFinancialClient,
    audit_conservative_droe_structure,
    to_eastmoney_finance_code,
)


def _frame(rows, value_field):
    records = []
    for report_date, notice_date, update_date in rows:
        records.append(
            {
                "SECURITY_CODE": "600519",
                "REPORT_DATE": pd.Timestamp(report_date),
                "NOTICE_DATE": pd.Timestamp(notice_date),
                "UPDATE_DATE": pd.Timestamp(update_date),
                value_field: 1.0,
            }
        )
    return pd.DataFrame(records)


class _Client:
    def __init__(self, income, balance):
        self.income = income
        self.balance = balance

    def income_quarterly(self, code, **kwargs):
        return self.income.copy()

    def balance_report(self, code, **kwargs):
        return self.balance.copy()


class _Response:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class _RetrySession:
    def __init__(self):
        self.headers = {}
        self.calls = 0

    def get(self, url, **kwargs):
        self.calls += 1
        if self.calls == 1:
            raise requests.Timeout("temporary timeout")
        return _Response({"data": []})


class EastmoneyDroeSourceTests(unittest.TestCase):
    def test_code_normalization(self):
        self.assertEqual(to_eastmoney_finance_code("600519.SH"), "SH600519")
        self.assertEqual(to_eastmoney_finance_code("sz000001"), "SZ000001")

    def test_json_request_retries_one_transient_timeout(self):
        session = _RetrySession()
        client = EastmoneyFinancialClient(
            session=session, max_attempts=2, retry_delay=0
        )
        self.assertEqual(client._get_json("probe", {}), {"data": []})
        self.assertEqual(session.calls, 2)

    def test_audit_uses_latest_notice_update_and_six_month_deadline(self):
        report_dates = pd.date_range("2019-12-31", periods=13, freq="QE")
        income_rows = []
        balance_rows = []
        for report_date in report_dates:
            notice = report_date + pd.Timedelta(days=30)
            update = notice
            if report_date == pd.Timestamp("2021-03-31"):
                update = pd.Timestamp("2022-04-01")
            income_rows.append((report_date, notice, update))
            balance_rows.append((report_date, notice, notice))
        client = _Client(
            _frame(income_rows, "DEDUCT_PARENT_NETPROFIT"),
            _frame(balance_rows, "TOTAL_PARENT_EQUITY"),
        )

        result = audit_conservative_droe_structure(
            ["600519.SH"],
            start_report_date="2021-01-01",
            end_report_date="2021-12-31",
            selection_year=2021,
            minimum_ratio=0.5,
            client=client,
        )

        self.assertEqual(result["expectedReportPeriods"], 4)
        self.assertEqual(result["structuralDroEPeriods"], 4)
        self.assertEqual(result["timelyDroEPeriods"], 3)
        self.assertEqual(result["selectionYearTimelyRatio"], 0.75)
        self.assertEqual(result["status"], "PASS")
        self.assertFalse(result["financialValuesUsedForAudit"])
        self.assertFalse(result["returnsOrPricesRead"])

    def test_audit_fails_when_conservative_dates_destroy_coverage(self):
        report_dates = pd.date_range("2019-12-31", periods=13, freq="QE")
        income_rows = []
        balance_rows = []
        for report_date in report_dates:
            notice = report_date + pd.Timedelta(days=30)
            update = notice if report_date < pd.Timestamp("2021-01-01") else notice + pd.DateOffset(years=1)
            income_rows.append((report_date, notice, update))
            balance_rows.append((report_date, notice, update))
        client = _Client(
            _frame(income_rows, "DEDUCT_PARENT_NETPROFIT"),
            _frame(balance_rows, "TOTAL_PARENT_EQUITY"),
        )

        result = audit_conservative_droe_structure(
            ["600519.SH"],
            start_report_date="2021-01-01",
            end_report_date="2021-12-31",
            selection_year=2021,
            minimum_ratio=0.5,
            client=client,
        )

        self.assertEqual(result["status"], "FAIL")
        self.assertIn("minimumTimelyDroERatio", result["failedChecks"])
        self.assertIn(
            "minimumSelectionYearTimelyDroERatio", result["failedChecks"]
        )

    def test_missing_update_date_is_counted_and_never_timely(self):
        report_dates = pd.date_range("2019-12-31", periods=13, freq="QE")
        income_rows = []
        balance_rows = []
        for report_date in report_dates:
            notice = report_date + pd.Timedelta(days=30)
            update = pd.NaT if report_date == pd.Timestamp("2021-03-31") else notice
            income_rows.append((report_date, notice, update))
            balance_rows.append((report_date, notice, notice))
        client = _Client(
            _frame(income_rows, "DEDUCT_PARENT_NETPROFIT"),
            _frame(balance_rows, "TOTAL_PARENT_EQUITY"),
        )

        result = audit_conservative_droe_structure(
            ["600519.SH"],
            start_report_date="2021-01-01",
            end_report_date="2021-12-31",
            selection_year=2021,
            minimum_ratio=0.5,
            client=client,
        )

        self.assertEqual(result["invalidAvailabilityDateRows"], 1)
        self.assertEqual(result["timelyDroEPeriods"], 3)

    def test_missing_report_period_remains_in_coverage_denominator(self):
        report_dates = pd.date_range("2019-12-31", periods=13, freq="QE")
        income_rows = []
        balance_rows = []
        for report_date in report_dates:
            notice = report_date + pd.Timedelta(days=30)
            if report_date != pd.Timestamp("2021-06-30"):
                income_rows.append((report_date, notice, notice))
            balance_rows.append((report_date, notice, notice))
        client = _Client(
            _frame(income_rows, "DEDUCT_PARENT_NETPROFIT"),
            _frame(balance_rows, "TOTAL_PARENT_EQUITY"),
        )

        result = audit_conservative_droe_structure(
            ["600519.SH"],
            start_report_date="2021-01-01",
            end_report_date="2021-12-31",
            selection_year=2021,
            minimum_ratio=0.5,
            client=client,
        )

        self.assertEqual(result["expectedReportPeriods"], 4)
        self.assertEqual(result["structuralDroEPeriods"], 3)
        self.assertEqual(result["timelyDroEPeriods"], 3)

    def test_v2_3_registry_is_frozen_and_trial_count_increases(self):
        config = json.loads(
            (
                PROJECT_ROOT
                / "configs"
                / "profit_candidates_v2_3_eastmoney_droe1.json"
            ).read_text(encoding="utf-8")
        )
        metadata = config["metadata"]
        self.assertTrue(metadata["created_before_v2_3_structural_coverage_probe"])
        self.assertTrue(metadata["created_before_candidate_factor_derivation"])
        self.assertFalse(metadata["candidate_factor_values_viewed_before_registration"])
        self.assertFalse(metadata["candidate_forward_returns_viewed_before_registration"])
        self.assertEqual(metadata["current_state"], "SOURCE_METADATA_PROBE_PENDING")
        self.assertEqual(metadata["homogeneous_trial_count_after_v2_3"], 28)
        self.assertEqual(metadata["admission_factor_ids"], list(config["factors"]))
        self.assertIn("UPDATE_DATE", metadata["availability_policy"])

        factor_id, factor = next(iter(config["factors"].items()))
        expected = hashlib.sha256(
            (
                f"{metadata['factor_id_namespace']}:"
                f"{factor['factor_name']}:{factor['factor_expression']}"
            ).encode()
        ).hexdigest()[:16]
        self.assertEqual(factor_id, expected)
        self.assertEqual(factor["factor_expression"], "$pit_droe_1y")


if __name__ == "__main__":
    unittest.main()
