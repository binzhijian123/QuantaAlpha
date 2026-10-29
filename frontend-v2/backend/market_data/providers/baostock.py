from __future__ import annotations

import contextlib
import importlib
import importlib.metadata
import io
import signal
import threading
import time
from typing import Any, Iterable

import pandas as pd

from .a_stock_data import normalize_stock_code


class BaoStockApiError(RuntimeError):
    """BaoStock transport or response error."""


class _SocketReadGuard:
    def __init__(self, connection: Any):
        self._connection = connection

    def recv(self, *args: Any, **kwargs: Any) -> bytes:
        chunk = self._connection.recv(*args, **kwargs)
        if chunk == b"":
            raise ConnectionError("BaoStock server closed the connection")
        return chunk

    def __getattr__(self, name: str) -> Any:
        return getattr(self._connection, name)


def to_baostock_code(code: str) -> str:
    digits, exchange, _ = normalize_stock_code(code)
    return f"{exchange.lower()}.{digits}"


def from_baostock_code(code: str) -> str:
    raw = str(code).strip().lower()
    parts = raw.split(".")
    if len(parts) != 2 or parts[0] not in {"sh", "sz"}:
        raise ValueError(f"unsupported BaoStock code: {code}")
    return f"{parts[1]}.{parts[0].upper()}"


def to_baostock_date(value: str) -> str:
    parsed = pd.Timestamp(value)
    if pd.isna(parsed):
        raise ValueError(f"invalid date: {value}")
    return parsed.strftime("%Y-%m-%d")


class BaoStockClient:
    """Validated wrapper around the anonymous BaoStock Python client."""

    def __init__(
        self,
        module: Any | None = None,
        retries: int = 3,
        socket_timeout_seconds: float = 30.0,
    ):
        if retries < 1:
            raise ValueError("retries must be at least 1")
        if socket_timeout_seconds <= 0:
            raise ValueError("socket_timeout_seconds must be positive")
        if module is None:
            try:
                import baostock as module
            except ImportError as exc:
                raise BaoStockApiError(
                    "baostock is not installed; install the project requirements before syncing"
                ) from exc
        self._module = module
        self.retries = retries
        self.socket_timeout_seconds = socket_timeout_seconds
        self._logged_in = False
        try:
            self.package_version = importlib.metadata.version("baostock")
        except importlib.metadata.PackageNotFoundError:
            self.package_version = str(getattr(module, "__version__", "unknown"))

    def _baostock_context(self) -> Any | None:
        module_name = str(getattr(self._module, "__name__", ""))
        if module_name != "baostock":
            return None
        try:
            return importlib.import_module("baostock.common.context")
        except ImportError:
            return None

    def _configure_socket_timeout(self) -> None:
        context = self._baostock_context()
        connection = getattr(context, "default_socket", None) if context else None
        if connection is None:
            return
        setter = getattr(connection, "settimeout", None)
        if callable(setter):
            setter(self.socket_timeout_seconds)
        if not isinstance(connection, _SocketReadGuard):
            setattr(context, "default_socket", _SocketReadGuard(connection))

    def _reset_connection(self) -> None:
        self._logged_in = False
        context = self._baostock_context()
        connection = getattr(context, "default_socket", None) if context else None
        closer = getattr(connection, "close", None)
        if callable(closer):
            with contextlib.suppress(Exception):
                closer()
        if context is not None and hasattr(context, "default_socket"):
            delattr(context, "default_socket")

    @contextlib.contextmanager
    def _request_deadline(self):
        supports_alarm = all(
            hasattr(signal, name) for name in ("SIGALRM", "ITIMER_REAL")
        )
        if not supports_alarm or threading.current_thread() is not threading.main_thread():
            yield
            return
        active_timer = signal.getitimer(signal.ITIMER_REAL)
        if active_timer[0] > 0:
            yield
            return

        previous_handler = signal.getsignal(signal.SIGALRM)

        def timeout_handler(signum: int, frame: Any) -> None:
            del signum, frame
            raise BaoStockApiError(
                f"BaoStock request timed out after {self.socket_timeout_seconds:g}s"
            )

        signal.signal(signal.SIGALRM, timeout_handler)
        signal.setitimer(signal.ITIMER_REAL, self.socket_timeout_seconds)
        try:
            yield
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, previous_handler)

    def login(self) -> None:
        if self._logged_in:
            return
        try:
            with self._request_deadline():
                with contextlib.redirect_stdout(io.StringIO()):
                    response = self._module.login()
        except Exception as exc:
            raise BaoStockApiError(f"BaoStock login failed: {exc}") from exc
        if str(getattr(response, "error_code", "")) != "0":
            raise BaoStockApiError(
                "BaoStock login failed: "
                f"{getattr(response, 'error_msg', 'unknown error')}"
            )
        self._logged_in = True
        self._configure_socket_timeout()

    def close(self) -> None:
        if not self._logged_in:
            return
        try:
            with self._request_deadline():
                with contextlib.redirect_stdout(io.StringIO()):
                    response = self._module.logout()
        except Exception as exc:
            self._logged_in = False
            raise BaoStockApiError(f"BaoStock logout failed: {exc}") from exc
        self._logged_in = False
        if str(getattr(response, "error_code", "")) != "0":
            raise BaoStockApiError(
                "BaoStock logout failed: "
                f"{getattr(response, 'error_msg', 'unknown error')}"
            )

    def __enter__(self) -> "BaoStockClient":
        self.login()
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.close()

    def _query_once(
        self,
        method_name: str,
        *,
        required_fields: Iterable[str],
        params: dict[str, Any],
    ) -> dict[str, Any]:
        self.login()
        method = getattr(self._module, method_name, None)
        if method is None:
            raise BaoStockApiError(f"BaoStock method is unavailable: {method_name}")
        try:
            with self._request_deadline():
                response = method(**params)
        except Exception as exc:
            raise BaoStockApiError(
                f"BaoStock request failed for {method_name}: {exc}"
            ) from exc
        if str(getattr(response, "error_code", "")) != "0":
            raise BaoStockApiError(
                f"BaoStock rejected {method_name}: "
                f"{getattr(response, 'error_msg', 'unknown error')}"
            )
        fields = list(getattr(response, "fields", None) or [])
        missing = sorted(set(required_fields) - set(fields))
        if missing:
            raise BaoStockApiError(
                f"BaoStock omitted requested fields for {method_name}: {missing}"
            )
        rows: list[list[Any]] = []
        while response.next():
            row = response.get_row_data()
            if not isinstance(row, list) or len(row) != len(fields):
                raise BaoStockApiError(
                    f"BaoStock returned a malformed row for {method_name}"
                )
            rows.append(row)
        return {
            "code": 0,
            "msg": "success",
            "data": {"fields": fields, "items": rows},
        }

    def query_raw(
        self,
        method_name: str,
        *,
        required_fields: Iterable[str],
        params: dict[str, Any],
    ) -> dict[str, Any]:
        last_error: Exception | None = None
        for attempt in range(self.retries):
            try:
                return self._query_once(
                    method_name,
                    required_fields=required_fields,
                    params=params,
                )
            except Exception as exc:
                last_error = exc
                self._reset_connection()
                if attempt < self.retries - 1:
                    time.sleep(0.5 * (2**attempt))
        assert last_error is not None
        raise last_error

    def query_annual_history_raw(
        self,
        method_name: str,
        *,
        code: str,
        start_year: int,
        end_year: int,
        required_fields: Iterable[str],
        request_interval_seconds: float = 0.0,
    ) -> dict[str, Any]:
        if end_year < start_year:
            raise ValueError("end_year must be on or after start_year")
        fields: list[str] | None = None
        rows: list[list[Any]] = []
        requests = 0
        for year in range(start_year, end_year + 1):
            payload = self.query_raw(
                method_name,
                required_fields=required_fields,
                params={
                    "code": to_baostock_code(code),
                    "year": year,
                    "quarter": 4,
                },
            )
            table = payload["data"]
            response_fields = list(table["fields"])
            if fields is None:
                fields = response_fields
            elif fields != response_fields:
                raise BaoStockApiError(
                    f"BaoStock changed fields within {method_name} history"
                )
            rows.extend(table["items"])
            requests += 1
            if request_interval_seconds > 0 and year < end_year:
                time.sleep(request_interval_seconds)
        return {
            "code": 0,
            "msg": "success",
            "data": {"fields": fields or list(required_fields), "items": rows},
            "source": {
                "method": method_name,
                "reportFrequency": "annual",
                "quarter": 4,
                "startYear": start_year,
                "endYear": end_year,
                "requestCount": requests,
                "packageVersion": self.package_version,
            },
        }

    def query_quarterly_history_raw(
        self,
        method_name: str,
        *,
        code: str,
        start_year: int,
        end_year: int,
        required_fields: Iterable[str],
        request_interval_seconds: float = 0.0,
    ) -> dict[str, Any]:
        if end_year < start_year:
            raise ValueError("end_year must be on or after start_year")
        fields: list[str] | None = None
        rows: list[list[Any]] = []
        requests = 0
        total_requests = (end_year - start_year + 1) * 4
        for year in range(start_year, end_year + 1):
            for quarter in range(1, 5):
                payload = self.query_raw(
                    method_name,
                    required_fields=required_fields,
                    params={
                        "code": to_baostock_code(code),
                        "year": year,
                        "quarter": quarter,
                    },
                )
                table = payload["data"]
                response_fields = list(table["fields"])
                if fields is None:
                    fields = response_fields
                elif fields != response_fields:
                    raise BaoStockApiError(
                        f"BaoStock changed fields within {method_name} history"
                    )
                rows.extend(table["items"])
                requests += 1
                if request_interval_seconds > 0 and requests < total_requests:
                    time.sleep(request_interval_seconds)
        return {
            "code": 0,
            "msg": "success",
            "data": {"fields": fields or list(required_fields), "items": rows},
            "source": {
                "method": method_name,
                "reportFrequency": "quarterly",
                "quarters": [1, 2, 3, 4],
                "startYear": start_year,
                "endYear": end_year,
                "requestCount": requests,
                "packageVersion": self.package_version,
            },
        }

    def query_daily_valuation_raw(
        self,
        *,
        code: str,
        start_date: str,
        end_date: str,
        required_fields: Iterable[str],
    ) -> dict[str, Any]:
        fields = tuple(required_fields)
        payload = self.query_raw(
            "query_history_k_data_plus",
            required_fields=fields,
            params={
                "code": to_baostock_code(code),
                "fields": ",".join(fields),
                "start_date": to_baostock_date(start_date),
                "end_date": to_baostock_date(end_date),
                "frequency": "d",
                "adjustflag": "3",
            },
        )
        payload["source"] = {
            "method": "query_history_k_data_plus",
            "frequency": "d",
            "adjustflag": "3",
            "startDate": to_baostock_date(start_date),
            "endDate": to_baostock_date(end_date),
            "requestCount": 1,
            "packageVersion": self.package_version,
        }
        return payload

    def preflight(self, as_of_date: str) -> dict[str, Any]:
        date_value = to_baostock_date(as_of_date)
        try:
            payload = self.query_raw(
                "query_trade_dates",
                required_fields=("calendar_date", "is_trading_day"),
                params={"start_date": date_value, "end_date": date_value},
            )
            return {
                "status": "PASS",
                "provider": "baostock",
                "packageVersion": self.package_version,
                "tradeCalendarRows": len(payload["data"]["items"]),
                "credentialsRequired": False,
            }
        finally:
            self.close()
