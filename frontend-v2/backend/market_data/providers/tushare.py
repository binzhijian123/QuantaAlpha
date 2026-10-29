from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Iterable

import pandas as pd
import requests

from .a_stock_data import normalize_stock_code


TUSHARE_API_URL = "https://api.tushare.pro"


class TushareApiError(RuntimeError):
    """Tushare transport or API error with credentials removed."""


@dataclass(frozen=True)
class TushareQuery:
    api_name: str
    params: dict[str, Any]
    fields: tuple[str, ...]


def to_tushare_code(code: str) -> str:
    digits, exchange, _ = normalize_stock_code(code)
    return f"{digits}.{exchange}"


def to_tushare_date(value: str) -> str:
    parsed = pd.Timestamp(value)
    if pd.isna(parsed):
        raise ValueError(f"invalid date: {value}")
    return parsed.strftime("%Y%m%d")


class TushareProClient:
    """Small Tushare Pro HTTP client that never persists or logs its token."""

    def __init__(
        self,
        token: str | None = None,
        timeout: float = 30.0,
        session: requests.Session | None = None,
        api_url: str = TUSHARE_API_URL,
    ):
        resolved_token = (token or os.getenv("TUSHARE_TOKEN") or "").strip()
        if not resolved_token:
            raise TushareApiError(
                "TUSHARE_TOKEN is not configured; set it in the local environment before syncing"
            )
        self._token = resolved_token
        self.timeout = timeout
        self.session = session or requests.Session()
        self.api_url = api_url

    def _redact(self, value: Any) -> str:
        return str(value).replace(self._token, "<redacted>")

    def query_raw(
        self,
        api_name: str,
        *,
        params: dict[str, Any] | None = None,
        fields: Iterable[str] = (),
    ) -> dict[str, Any]:
        requested_fields = tuple(str(field).strip() for field in fields if str(field).strip())
        request_payload = {
            "api_name": api_name,
            "token": self._token,
            "params": dict(params or {}),
            "fields": ",".join(requested_fields),
        }
        try:
            response = self.session.post(
                self.api_url,
                json=request_payload,
                timeout=self.timeout,
            )
            response.raise_for_status()
            payload = response.json()
        except Exception as exc:
            raise TushareApiError(
                f"Tushare request failed for {api_name}: {self._redact(exc)}"
            ) from exc

        if not isinstance(payload, dict):
            raise TushareApiError(f"Tushare returned a non-object payload for {api_name}")
        if payload.get("code") != 0:
            raise TushareApiError(
                f"Tushare API rejected {api_name}: {self._redact(payload.get('msg') or 'unknown error')}"
            )
        data = payload.get("data")
        if not isinstance(data, dict):
            raise TushareApiError(f"Tushare returned no data object for {api_name}")
        response_fields = data.get("fields")
        items = data.get("items")
        if not isinstance(response_fields, list) or not isinstance(items, list):
            raise TushareApiError(f"Tushare returned an invalid table for {api_name}")
        missing_fields = sorted(set(requested_fields) - set(response_fields))
        if missing_fields:
            raise TushareApiError(
                f"Tushare omitted requested fields for {api_name}: {missing_fields}"
            )
        if any(not isinstance(row, list) or len(row) != len(response_fields) for row in items):
            raise TushareApiError(f"Tushare returned malformed rows for {api_name}")
        return payload

    def query(
        self,
        api_name: str,
        *,
        params: dict[str, Any] | None = None,
        fields: Iterable[str] = (),
    ) -> pd.DataFrame:
        payload = self.query_raw(api_name, params=params, fields=fields)
        data = payload["data"]
        return pd.DataFrame(data["items"], columns=data["fields"])

    def preflight(self, trade_date: str) -> dict[str, Any]:
        frame = self.query(
            "trade_cal",
            params={
                "exchange": "SSE",
                "start_date": to_tushare_date(trade_date),
                "end_date": to_tushare_date(trade_date),
            },
            fields=("exchange", "cal_date", "is_open"),
        )
        return {
            "status": "PASS",
            "endpoint": self.api_url,
            "tradeCalendarRows": int(len(frame)),
            "tokenConfigured": True,
        }
