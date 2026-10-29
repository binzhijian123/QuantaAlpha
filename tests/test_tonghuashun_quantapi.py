import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = PROJECT_ROOT / "frontend-v2" / "backend"
for path in (PROJECT_ROOT, BACKEND_ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from market_data.providers.tonghuashun_quantapi import (
    QUANTAPI_ACCESS_TOKEN_URL,
    QUANTAPI_SERVICE_URLS,
    TonghuashunQuantApiClient,
    TonghuashunQuantApiError,
    summarize_quantapi_response,
)


class _Response:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class _Session:
    def __init__(self, *payloads):
        self.payloads = list(payloads)
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return _Response(self.payloads.pop(0))


def _token_payload(token="access-secret"):
    return {
        "errorcode": 0,
        "errmsg": "Success",
        "data": {"access_token": token, "expire_in": 604800},
    }


def _basic_request():
    return {
        "codes": "300033.SZ,600030.SH",
        "indipara": [
            {"indicator": "ths_roe_stock", "indiparams": ["20241231"]}
        ],
    }


class TonghuashunQuantApiClientTests(unittest.TestCase):
    def test_missing_refresh_token_fails_before_network(self):
        session = _Session()
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(TonghuashunQuantApiError, "IFIND_REFRESH_TOKEN"):
                TonghuashunQuantApiClient(session=session)
        self.assertEqual(session.calls, [])

    def test_preflight_uses_current_token_endpoint_and_returns_no_secret(self):
        refresh_token = "refresh-secret"
        access_token = "access-secret"
        session = _Session(_token_payload(access_token))
        result = TonghuashunQuantApiClient(
            refresh_token=refresh_token, session=session
        ).preflight()

        self.assertEqual(session.calls[0][0], QUANTAPI_ACCESS_TOKEN_URL)
        self.assertEqual(session.calls[0][1]["headers"]["refresh_token"], refresh_token)
        rendered = str(result)
        self.assertNotIn(refresh_token, rendered)
        self.assertNotIn(access_token, rendered)
        self.assertFalse(result["tokenPersisted"])
        self.assertEqual(result["accessTokenExpiresInSeconds"], 604800)

    def test_basic_request_uses_access_token_and_returns_tables(self):
        response = {
            "errorcode": 0,
            "errmsg": "Success",
            "tables": [
                {
                    "thscode": "300033.SZ",
                    "table": {"ths_roe_stock": [12.3]},
                }
            ],
            "dataVol": 1,
        }
        session = _Session(_token_payload(), response)
        client = TonghuashunQuantApiClient(
            refresh_token="refresh-secret", session=session
        )

        result = client.request("basic_data_service", _basic_request())

        self.assertEqual(result, response)
        self.assertEqual(session.calls[1][0], QUANTAPI_SERVICE_URLS["basic_data_service"])
        self.assertEqual(session.calls[1][1]["headers"]["access_token"], "access-secret")
        self.assertNotIn("refresh_token", session.calls[1][1]["headers"])

    def test_api_error_redacts_refresh_and_access_tokens(self):
        refresh_token = "refresh-secret"
        access_token = "access-secret"
        session = _Session(
            _token_payload(access_token),
            {
                "errorcode": -1,
                "errmsg": f"invalid {refresh_token} and {access_token}",
            },
        )
        client = TonghuashunQuantApiClient(
            refresh_token=refresh_token, session=session
        )

        with self.assertRaises(TonghuashunQuantApiError) as caught:
            client.request("basic_data_service", _basic_request())

        rendered = str(caught.exception)
        self.assertNotIn(refresh_token, rendered)
        self.assertNotIn(access_token, rendered)

    def test_unsupported_service_fails_before_network(self):
        session = _Session()
        client = TonghuashunQuantApiClient(refresh_token="token", session=session)
        with self.assertRaisesRegex(TonghuashunQuantApiError, "unsupported"):
            client.request("real_time_quotation", _basic_request())
        self.assertEqual(session.calls, [])

    def test_request_payload_rejects_embedded_credentials(self):
        session = _Session()
        client = TonghuashunQuantApiClient(refresh_token="token", session=session)
        payload = {**_basic_request(), "access_token": "must-not-be-here"}
        with self.assertRaisesRegex(TonghuashunQuantApiError, "must not contain credentials"):
            client.request("basic_data_service", payload)
        self.assertEqual(session.calls, [])

    def test_date_sequence_requires_dates_before_network(self):
        session = _Session()
        client = TonghuashunQuantApiClient(refresh_token="token", session=session)
        with self.assertRaisesRegex(TonghuashunQuantApiError, "startdate"):
            client.request("date_sequence", _basic_request())
        self.assertEqual(session.calls, [])

    def test_summary_exposes_shape_but_not_values(self):
        payload = {
            "errorcode": 0,
            "tables": [
                {
                    "thscode": "300033.SZ",
                    "table": {
                        "time": ["2024-01-01", "2024-01-02"],
                        "ths_roe_stock": [12.3, 13.4],
                    },
                }
            ],
            "datatype": {"ths_roe_stock": "double"},
            "dataVol": 4,
        }

        summary = summarize_quantapi_response(payload)

        rendered = str(summary)
        self.assertEqual(summary["tableCount"], 1)
        self.assertEqual(summary["tables"][0]["rowCount"], 2)
        self.assertEqual(
            summary["tables"][0]["fields"], ["ths_roe_stock", "time"]
        )
        self.assertNotIn("12.3", rendered)
        self.assertNotIn("2024-01-01", rendered)
        self.assertFalse(summary["valuesExposed"])


if __name__ == "__main__":
    unittest.main()
