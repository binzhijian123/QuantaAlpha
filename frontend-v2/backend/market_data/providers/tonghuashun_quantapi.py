from __future__ import annotations

import os
from typing import Any, Iterable, Mapping

import requests


QUANTAPI_BASE_URL = "https://quantapi.51ifind.com/api/v1"
QUANTAPI_ACCESS_TOKEN_URL = f"{QUANTAPI_BASE_URL}/get_access_token"
QUANTAPI_SERVICE_URLS = {
    "basic_data_service": f"{QUANTAPI_BASE_URL}/basic_data_service",
    "date_sequence": f"{QUANTAPI_BASE_URL}/date_sequence",
}


class TonghuashunQuantApiError(RuntimeError):
    """QuantAPI transport, authentication, or response-contract error."""


def _non_empty_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _contains_sensitive_key(value: Any) -> bool:
    if isinstance(value, Mapping):
        for key, item in value.items():
            normalized = str(key).lower().replace("-", "_")
            if any(part in normalized for part in ("token", "password", "secret")):
                return True
            if _contains_sensitive_key(item):
                return True
    elif isinstance(value, (list, tuple)):
        return any(_contains_sensitive_key(item) for item in value)
    return False


def _validate_indipara(value: Any) -> None:
    if not isinstance(value, list) or not value:
        raise TonghuashunQuantApiError("indipara must be a non-empty list")
    for item in value:
        if not isinstance(item, dict) or not _non_empty_string(item.get("indicator")):
            raise TonghuashunQuantApiError(
                "each indipara item must contain a non-empty indicator"
            )
        if "indiparams" in item and not isinstance(item["indiparams"], list):
            raise TonghuashunQuantApiError("indiparams must be a list when present")


def validate_quantapi_payload(service: str, payload: Mapping[str, Any]) -> None:
    if service not in QUANTAPI_SERVICE_URLS:
        raise TonghuashunQuantApiError(f"unsupported QuantAPI service: {service}")
    if not isinstance(payload, Mapping):
        raise TonghuashunQuantApiError("QuantAPI request payload must be an object")
    if _contains_sensitive_key(payload):
        raise TonghuashunQuantApiError(
            "request payload must not contain credentials; configure IFIND_REFRESH_TOKEN instead"
        )
    if not _non_empty_string(payload.get("codes")):
        raise TonghuashunQuantApiError("codes must be a non-empty comma-separated string")
    _validate_indipara(payload.get("indipara"))
    if service == "date_sequence":
        for field in ("startdate", "enddate"):
            if not _non_empty_string(payload.get(field)):
                raise TonghuashunQuantApiError(f"{field} is required for date_sequence")
        functionpara = payload.get("functionpara")
        if functionpara is not None and not isinstance(functionpara, dict):
            raise TonghuashunQuantApiError("functionpara must be an object when present")


def _table_shape(table: Any) -> tuple[list[str], int]:
    if isinstance(table, dict):
        fields = sorted(str(key) for key in table)
        lengths = [len(value) for value in table.values() if isinstance(value, list)]
        return fields, max(lengths, default=(1 if table else 0))
    if isinstance(table, list):
        fields = sorted(
            {
                str(key)
                for row in table
                if isinstance(row, dict)
                for key in row
            }
        )
        return fields, len(table)
    return [], 0


def summarize_quantapi_response(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Return response schema and volume without exposing source values."""
    tables = payload.get("tables")
    table_items = tables if isinstance(tables, list) else []
    summaries = []
    for item in table_items:
        if not isinstance(item, dict):
            summaries.append({"fields": [], "rowCount": 0})
            continue
        fields, row_count = _table_shape(item.get("table"))
        summary: dict[str, Any] = {"fields": fields, "rowCount": row_count}
        if _non_empty_string(item.get("thscode")):
            summary["thscode"] = item["thscode"]
        summaries.append(summary)
    return {
        "status": "PASS",
        "errorcode": payload.get("errorcode"),
        "tableCount": len(table_items),
        "tables": summaries,
        "datatype": payload.get("datatype"),
        "dataVol": payload.get("dataVol"),
        "valuesExposed": False,
        "tokenPersisted": False,
    }


class TonghuashunQuantApiClient:
    """Minimal read-only HTTP client that keeps QuantAPI credentials in memory."""

    def __init__(
        self,
        refresh_token: str | None = None,
        timeout: float = 30.0,
        session: requests.Session | None = None,
        access_token_url: str = QUANTAPI_ACCESS_TOKEN_URL,
        service_urls: Mapping[str, str] | None = None,
    ):
        resolved_token = (refresh_token or os.getenv("IFIND_REFRESH_TOKEN") or "").strip()
        if not resolved_token:
            raise TonghuashunQuantApiError(
                "IFIND_REFRESH_TOKEN is not configured; set it only in the local environment"
            )
        self._refresh_token = resolved_token
        self._access_token: str | None = None
        self.timeout = timeout
        self.session = session or requests.Session()
        self.access_token_url = access_token_url
        self.service_urls = dict(service_urls or QUANTAPI_SERVICE_URLS)

    def _redact(self, value: Any, extra_secrets: Iterable[str] = ()) -> str:
        result = str(value).replace(self._refresh_token, "<redacted>")
        secrets = tuple(extra_secrets) + ((self._access_token,) if self._access_token else ())
        for secret in secrets:
            if secret:
                result = result.replace(secret, "<redacted>")
        return result

    def _post(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        json_payload: Mapping[str, Any] | None = None,
        operation: str,
    ) -> dict[str, Any]:
        try:
            kwargs: dict[str, Any] = {
                "headers": dict(headers),
                "timeout": self.timeout,
            }
            if json_payload is not None:
                kwargs["json"] = dict(json_payload)
            response = self.session.post(url, **kwargs)
            response.raise_for_status()
            payload = response.json()
        except Exception as exc:
            raise TonghuashunQuantApiError(
                f"QuantAPI request failed for {operation}: {self._redact(exc)}"
            ) from exc
        if not isinstance(payload, dict):
            raise TonghuashunQuantApiError(
                f"QuantAPI returned a non-object payload for {operation}"
            )
        code = payload.get("errorcode")
        if str(code) != "0":
            message = payload.get("errmsg") or payload.get("message") or "unknown error"
            raise TonghuashunQuantApiError(
                f"QuantAPI rejected {operation}: {self._redact(message)}"
            )
        return payload

    def get_access_token(self) -> tuple[str, dict[str, Any]]:
        if self._access_token:
            return self._access_token, {}
        payload = self._post(
            self.access_token_url,
            headers={
                "Content-Type": "application/json",
                "refresh_token": self._refresh_token,
            },
            operation="get_access_token",
        )
        data = payload.get("data")
        access_token = data.get("access_token") if isinstance(data, dict) else None
        if not _non_empty_string(access_token):
            raise TonghuashunQuantApiError(
                "QuantAPI returned no access_token for get_access_token"
            )
        self._access_token = access_token.strip()
        metadata = {
            key: value
            for key, value in data.items()
            if key not in {"access_token", "refresh_token"}
        }
        return self._access_token, metadata

    def preflight(self) -> dict[str, Any]:
        _, metadata = self.get_access_token()
        result: dict[str, Any] = {
            "status": "PASS",
            "endpoint": self.access_token_url,
            "refreshTokenConfigured": True,
            "accessTokenReceived": True,
            "tokenPersisted": False,
        }
        if "expire_in" in metadata:
            result["accessTokenExpiresInSeconds"] = metadata["expire_in"]
        return result

    def request(self, service: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        validate_quantapi_payload(service, payload)
        if service not in self.service_urls:
            raise TonghuashunQuantApiError(f"unsupported QuantAPI service: {service}")
        access_token, _ = self.get_access_token()
        response = self._post(
            self.service_urls[service],
            headers={
                "Content-Type": "application/json",
                "access_token": access_token,
            },
            json_payload=payload,
            operation=service,
        )
        if not isinstance(response.get("tables"), list):
            raise TonghuashunQuantApiError(
                f"QuantAPI returned no tables list for {service}"
            )
        return response
