from __future__ import annotations

import json
import math
import re
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd
import requests


# Endpoint strategy adapted from simonlin1212/a-stock-data (Apache-2.0).
TENCENT_KLINE_URL = "https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get"
TENCENT_ADJUSTED_KLINE_URL = "https://proxy.finance.qq.com/ifzqgtimg/appstock/app/fqkline/get"
THS_KLINE_BASE_URL = "https://d.10jqka.com.cn/v6/line"
TENCENT_QUOTE_URL = "https://qt.gtimg.cn/q="
SINA_QUOTE_URL = "https://hq.sinajs.cn/list="
SINA_QUOTE_REFERER = "https://finance.sina.com.cn/"
EASTMONEY_QUOTE_URLS = (
    "https://push2delay.eastmoney.com/api/qt/ulist.np/get",
    "https://18.push2.eastmoney.com/api/qt/ulist.np/get",
    "https://82.push2.eastmoney.com/api/qt/ulist.np/get",
)
EASTMONEY_QUOTE_REFERER = "https://quote.eastmoney.com/"
EASTMONEY_QUOTE_TOKEN = "fa5fd1943c7b386f172d6893dbbd1d0c"
TENCENT_PAGE_SIZE = 640
MAX_HISTORY_PAGES = 40
MAX_QUOTE_BATCH_SIZE = 60
MAX_EASTMONEY_QUOTE_BATCH_SIZE = 500
CHINA_TZ = ZoneInfo("Asia/Shanghai")


def _tencent_volume_multiplier(canonical_code: str) -> float:
    digits, exchange = canonical_code.split(".")
    # Tencent reports STAR Market volume in shares; other supported boards use lots.
    return 1.0 if exchange == "SH" and digits.startswith(("688", "689")) else 100.0


def normalize_stock_code(code: str) -> tuple[str, str, str]:
    raw = code.strip().upper()
    match = re.fullmatch(r"(SH|SZ|BJ)(\d{6})", raw)
    if match:
        exchange, digits = match.groups()
    else:
        match = re.fullmatch(r"(\d{6})(?:\.(SH|SZ|BJ))?", raw)
        if not match:
            raise ValueError(f"unsupported A-share code: {code}")
        digits, exchange = match.groups()
        if exchange is None:
            if digits.startswith(("6", "9")):
                exchange = "SH"
            elif digits.startswith(("0", "2", "3")):
                exchange = "SZ"
            elif digits.startswith(("4", "8")):
                exchange = "BJ"
            else:
                raise ValueError(f"cannot infer exchange for code: {code}")
    if exchange == "BJ":
        raise ValueError("a-stock-data daily adapter does not yet support Beijing Stock Exchange codes")
    return digits, exchange, f"{exchange.lower()}{digits}"


def to_qlib_code(code: str) -> str:
    digits, exchange, _ = normalize_stock_code(code)
    return f"{exchange.lower()}{digits}"


def default_window(lookback_trading_days: int) -> tuple[str, str]:
    end = date.today()
    start = end - timedelta(days=max(lookback_trading_days * 3, 10))
    return start.isoformat(), end.isoformat()


def _extract_json_object(text: str) -> dict[str, Any]:
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end < start:
        raise ValueError("response does not contain a JSON object")
    payload = json.loads(text[start : end + 1])
    if not isinstance(payload, dict):
        raise ValueError("response JSON is not an object")
    return payload


def _quote_number(values: list[str], index: int) -> float | None:
    if index >= len(values) or not values[index]:
        return None
    try:
        value = float(values[index])
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def _quote_timestamp(raw: str) -> str | None:
    try:
        return datetime.strptime(raw, "%Y%m%d%H%M%S").replace(tzinfo=CHINA_TZ).isoformat()
    except (TypeError, ValueError):
        return None


def _sina_quote_timestamp(date_value: str, time_value: str) -> str | None:
    try:
        return (
            datetime.strptime(f"{date_value} {time_value}", "%Y-%m-%d %H:%M:%S")
            .replace(tzinfo=CHINA_TZ)
            .isoformat()
        )
    except (TypeError, ValueError):
        return None


def _eastmoney_quote_timestamp(value: Any) -> str | None:
    if isinstance(value, bool):
        return None
    try:
        timestamp = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(timestamp) or timestamp <= 0:
        return None
    try:
        return datetime.fromtimestamp(timestamp, tz=CHINA_TZ).isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def _eastmoney_number(value: Any, *, divisor: float = 1.0) -> float | None:
    if isinstance(value, bool) or value in (None, "", "-"):
        return None
    try:
        numeric = float(value) / divisor
    except (TypeError, ValueError, ZeroDivisionError):
        return None
    return numeric if math.isfinite(numeric) else None


def _usable_realtime_quote(
    quote: dict[str, Any] | None,
    *,
    expected_trade_date: str | None = None,
    max_quote_age_seconds: int | None = None,
    now: datetime | None = None,
) -> bool:
    if not quote or quote.get("isStale"):
        return False
    price = quote.get("price")
    if not bool(
        isinstance(price, (int, float))
        and not isinstance(price, bool)
        and math.isfinite(float(price))
        and float(price) > 0
    ):
        return False
    if expected_trade_date and quote.get("tradeDate") != expected_trade_date:
        return False
    if max_quote_age_seconds is not None:
        quote_as_of = quote.get("quoteAsOf")
        try:
            quote_time = datetime.fromisoformat(str(quote_as_of)).astimezone(CHINA_TZ)
        except (TypeError, ValueError):
            return False
        reference_time = (now or datetime.now(CHINA_TZ)).astimezone(CHINA_TZ)
        age_seconds = (reference_time - quote_time).total_seconds()
        if age_seconds > max_quote_age_seconds or age_seconds < -5:
            return False
    return True


class AStockDataClient:
    """Daily A-share adapter based on the public routes documented by a-stock-data.

    Tencent is the primary full-history route. Tonghuashun's public recent-day
    route is used only if Tencent fails. No account or API key is involved.
    """

    def __init__(self, timeout: float = 15.0, session: requests.Session | None = None):
        self.timeout = timeout
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": "Mozilla/5.0"})

    def history_quotation(self, code: str, start_date: str, end_date: str) -> pd.DataFrame:
        digits, exchange, market_code = normalize_stock_code(code)
        canonical_code = f"{digits}.{exchange}"
        try:
            return self._tencent_history(market_code, canonical_code, start_date, end_date)
        except Exception as tencent_error:
            try:
                return self._ths_recent_history(digits, canonical_code, start_date, end_date)
            except Exception as ths_error:
                raise RuntimeError(
                    "a-stock-data daily sources failed for "
                    f"{canonical_code}: Tencent={tencent_error}; Tonghuashun={ths_error}"
                ) from ths_error

    def realtime_quotes(
        self,
        codes: list[str],
        *,
        expected_trade_date: str | None = None,
        max_quote_age_seconds: int | None = None,
        now: datetime | None = None,
    ) -> dict[str, dict[str, Any]]:
        """Fetch provisional quotes without mutating any daily Provider."""
        requested: list[tuple[str, str]] = []
        seen: set[str] = set()
        for code in codes:
            digits, exchange, market_code = normalize_stock_code(code)
            canonical_code = f"{digits}.{exchange}"
            if canonical_code in seen:
                continue
            seen.add(canonical_code)
            requested.append((canonical_code, market_code))

        if not requested:
            return {}

        result: dict[str, dict[str, Any]] = {}
        errors: list[str] = []
        try:
            result.update(self._tencent_realtime_quotes(requested))
        except Exception as exc:
            errors.append(f"Tencent={exc}")

        missing = [
            item
            for item in requested
            if not _usable_realtime_quote(
                result.get(item[0]),
                expected_trade_date=expected_trade_date,
                max_quote_age_seconds=max_quote_age_seconds,
                now=now,
            )
        ]
        if missing:
            try:
                fallback = self._sina_realtime_quotes(missing)
                for canonical_code, quote in fallback.items():
                    if _usable_realtime_quote(
                        quote,
                        expected_trade_date=expected_trade_date,
                        max_quote_age_seconds=max_quote_age_seconds,
                        now=now,
                    ):
                        result[canonical_code] = quote
            except Exception as exc:
                errors.append(f"Sina={exc}")

        missing = [
            item
            for item in requested
            if not _usable_realtime_quote(
                result.get(item[0]),
                expected_trade_date=expected_trade_date,
                max_quote_age_seconds=max_quote_age_seconds,
                now=now,
            )
        ]
        if missing:
            try:
                fallback = self._eastmoney_realtime_quotes(
                    missing,
                    expected_trade_date=expected_trade_date,
                    max_quote_age_seconds=max_quote_age_seconds,
                    now=now,
                )
                for canonical_code, quote in fallback.items():
                    if _usable_realtime_quote(
                        quote,
                        expected_trade_date=expected_trade_date,
                        max_quote_age_seconds=max_quote_age_seconds,
                        now=now,
                    ):
                        result[canonical_code] = quote
            except Exception as exc:
                errors.append(f"Eastmoney={exc}")

        unresolved = [
            canonical_code
            for canonical_code, _ in requested
            if not _usable_realtime_quote(
                result.get(canonical_code),
                expected_trade_date=expected_trade_date,
                max_quote_age_seconds=max_quote_age_seconds,
                now=now,
            )
        ]
        if len(unresolved) == len(requested):
            detail = "all rows were unusable"
            if errors:
                detail = f"{detail}; {'; '.join(errors)}"
            raise RuntimeError(f"a-stock-data real-time sources failed: {detail}")
        return result

    def _tencent_realtime_quotes(
        self,
        requested: list[tuple[str, str]],
    ) -> dict[str, dict[str, Any]]:
        result: dict[str, dict[str, Any]] = {}

        for offset in range(0, len(requested), MAX_QUOTE_BATCH_SIZE):
            batch = requested[offset : offset + MAX_QUOTE_BATCH_SIZE]
            response = self.session.get(
                TENCENT_QUOTE_URL + ",".join(market_code for _, market_code in batch),
                timeout=self.timeout,
            )
            response.raise_for_status()
            try:
                response.encoding = "gbk"
            except Exception:
                pass
            text = response.text
            canonical_by_market = {market: canonical for canonical, market in batch}
            for line in text.strip().split(";"):
                if not line.strip() or "=" not in line or '"' not in line:
                    continue
                market_code = line.split("=", 1)[0].rsplit("_", 1)[-1].lower()
                canonical_code = canonical_by_market.get(market_code)
                if canonical_code is None:
                    continue
                values = line.split('"', 2)[1].split("~")
                if len(values) < 53:
                    continue

                price = _quote_number(values, 3)
                previous_close = _quote_number(values, 4)
                amount_wan = _quote_number(values, 37)
                quote_as_of = _quote_timestamp(values[30] if len(values) > 30 else "")
                stale = bool(
                    price is not None
                    and price > 0
                    and previous_close is not None
                    and math.isclose(price, previous_close, rel_tol=0, abs_tol=1e-12)
                    and (amount_wan is None or amount_wan == 0)
                )
                result[canonical_code] = {
                    "symbol": canonical_code,
                    "name": values[1],
                    "price": price,
                    "previousClose": previous_close,
                    "open": _quote_number(values, 5),
                    "high": _quote_number(values, 33),
                    "low": _quote_number(values, 34),
                    "change": _quote_number(values, 31),
                    "changePercent": _quote_number(values, 32),
                    "amountCny": amount_wan * 10_000.0 if amount_wan is not None else None,
                    "turnoverPercent": _quote_number(values, 38),
                    "quoteAsOf": quote_as_of,
                    "tradeDate": quote_as_of[:10] if quote_as_of else None,
                    "isStale": stale,
                    "staleReason": (
                        "成交额为零且现价等于昨收，可能未开盘、停牌或代码已失效"
                        if stale
                        else None
                    ),
                    "source": "a-stock-data/tencent-public",
                }

        if requested and not result:
            raise RuntimeError("Tencent returned no usable real-time quote rows")
        return result

    def _sina_realtime_quotes(
        self,
        requested: list[tuple[str, str]],
    ) -> dict[str, dict[str, Any]]:
        result: dict[str, dict[str, Any]] = {}
        for offset in range(0, len(requested), MAX_QUOTE_BATCH_SIZE):
            batch = requested[offset : offset + MAX_QUOTE_BATCH_SIZE]
            response = self.session.get(
                SINA_QUOTE_URL + ",".join(market_code for _, market_code in batch),
                headers={"Referer": SINA_QUOTE_REFERER},
                timeout=self.timeout,
            )
            response.raise_for_status()
            try:
                response.encoding = "gbk"
            except Exception:
                pass
            canonical_by_market = {market: canonical for canonical, market in batch}
            for match in re.finditer(
                r'var\s+hq_str_([a-z]{2}\d{6})="([^"]*)"',
                response.text,
                flags=re.IGNORECASE,
            ):
                market_code = match.group(1).lower()
                canonical_code = canonical_by_market.get(market_code)
                if canonical_code is None:
                    continue
                values = match.group(2).split(",")
                if len(values) < 32 or not values[0]:
                    continue

                price = _quote_number(values, 3)
                previous_close = _quote_number(values, 2)
                amount_cny = _quote_number(values, 9)
                quote_as_of = _sina_quote_timestamp(values[30], values[31])
                change = (
                    price - previous_close
                    if price is not None and previous_close is not None
                    else None
                )
                change_percent = (
                    change / previous_close * 100.0
                    if change is not None and previous_close not in (None, 0.0)
                    else None
                )
                stale = bool(
                    price is not None
                    and price > 0
                    and previous_close is not None
                    and math.isclose(price, previous_close, rel_tol=0, abs_tol=1e-12)
                    and (amount_cny is None or amount_cny == 0)
                )
                result[canonical_code] = {
                    "symbol": canonical_code,
                    "name": values[0],
                    "price": price,
                    "previousClose": previous_close,
                    "open": _quote_number(values, 1),
                    "high": _quote_number(values, 4),
                    "low": _quote_number(values, 5),
                    "change": change,
                    "changePercent": change_percent,
                    "amountCny": amount_cny,
                    "turnoverPercent": None,
                    "quoteAsOf": quote_as_of,
                    "tradeDate": quote_as_of[:10] if quote_as_of else None,
                    "isStale": stale,
                    "staleReason": (
                        "成交额为零且现价等于昨收，可能未开盘、停牌或代码已失效"
                        if stale
                        else None
                    ),
                    "source": "a-stock-data/sina-public",
                }

        if requested and not result:
            raise RuntimeError("Sina returned no usable real-time quote rows")
        return result

    def _eastmoney_realtime_quotes(
        self,
        requested: list[tuple[str, str]],
        *,
        expected_trade_date: str | None = None,
        max_quote_age_seconds: int | None = None,
        now: datetime | None = None,
    ) -> dict[str, dict[str, Any]]:
        result: dict[str, dict[str, Any]] = {}
        errors: list[str] = []
        for offset in range(0, len(requested), MAX_EASTMONEY_QUOTE_BATCH_SIZE):
            batch = requested[offset : offset + MAX_EASTMONEY_QUOTE_BATCH_SIZE]
            unresolved = list(batch)
            for endpoint in EASTMONEY_QUOTE_URLS:
                if not unresolved:
                    break
                secid_to_canonical = {
                    ("1" if canonical.endswith(".SH") else "0", canonical[:6]): canonical
                    for canonical, _ in unresolved
                }
                params = {
                    "fltt": "1",
                    "invt": "2",
                    "fields": (
                        "f2,f3,f4,f5,f6,f8,f12,f13,f14,f15,f16,f17,f18,f124"
                    ),
                    "secids": ",".join(
                        f"{market}.{digits}"
                        for market, digits in secid_to_canonical
                    ),
                    "ut": EASTMONEY_QUOTE_TOKEN,
                }
                try:
                    response = self.session.get(
                        endpoint,
                        params=params,
                        headers={"Referer": EASTMONEY_QUOTE_REFERER},
                        timeout=self.timeout,
                    )
                    response.raise_for_status()
                    payload = response.json()
                    if not isinstance(payload, dict) or payload.get("rc") != 0:
                        raise RuntimeError("Eastmoney returned an unsuccessful payload")
                    rows = (payload.get("data") or {}).get("diff") or []
                    if isinstance(rows, dict):
                        rows = list(rows.values())
                    if not isinstance(rows, list):
                        raise RuntimeError("Eastmoney quote rows are not a list")
                    for row in rows:
                        if not isinstance(row, dict):
                            continue
                        market_value = row.get("f13")
                        market = "" if market_value is None else str(market_value)
                        digits = str(row.get("f12") or "").zfill(6)
                        canonical_code = secid_to_canonical.get((market, digits))
                        if canonical_code is None:
                            continue
                        price = _eastmoney_number(row.get("f2"), divisor=100.0)
                        previous_close = _eastmoney_number(
                            row.get("f18"), divisor=100.0
                        )
                        amount_cny = _eastmoney_number(row.get("f6"))
                        quote_as_of = _eastmoney_quote_timestamp(row.get("f124"))
                        stale = bool(
                            price is not None
                            and price > 0
                            and previous_close is not None
                            and math.isclose(
                                price,
                                previous_close,
                                rel_tol=0,
                                abs_tol=1e-12,
                            )
                            and (amount_cny is None or amount_cny == 0)
                        )
                        quote = {
                            "symbol": canonical_code,
                            "name": str(row.get("f14") or ""),
                            "price": price,
                            "previousClose": previous_close,
                            "open": _eastmoney_number(row.get("f17"), divisor=100.0),
                            "high": _eastmoney_number(row.get("f15"), divisor=100.0),
                            "low": _eastmoney_number(row.get("f16"), divisor=100.0),
                            "change": _eastmoney_number(row.get("f4"), divisor=100.0),
                            "changePercent": _eastmoney_number(
                                row.get("f3"), divisor=100.0
                            ),
                            "amountCny": amount_cny,
                            "turnoverPercent": _eastmoney_number(
                                row.get("f8"), divisor=100.0
                            ),
                            "quoteAsOf": quote_as_of,
                            "tradeDate": quote_as_of[:10] if quote_as_of else None,
                            "isStale": stale,
                            "staleReason": (
                                "成交额为零且现价等于昨收，可能未开盘、停牌或代码已失效"
                                if stale
                                else None
                            ),
                            "source": "a-stock-data/eastmoney-public",
                        }
                        if _usable_realtime_quote(
                            quote,
                            expected_trade_date=expected_trade_date,
                            max_quote_age_seconds=max_quote_age_seconds,
                            now=now,
                        ):
                            result[canonical_code] = quote
                    unresolved = [
                        item for item in unresolved if item[0] not in result
                    ]
                except Exception as exc:
                    errors.append(f"{endpoint}={exc}")

        if requested and not result:
            detail = "; ".join(errors) if errors else "no quote rows"
            raise RuntimeError(f"Eastmoney returned no usable real-time quote rows: {detail}")
        return result

    def tencent_raw_history(self, code: str, start_date: str, end_date: str) -> pd.DataFrame:
        """Fetch Tencent raw bars without falling back to another provider."""
        digits, exchange, market_code = normalize_stock_code(code)
        return self._tencent_history(
            market_code,
            f"{digits}.{exchange}",
            start_date,
            end_date,
        )

    def tencent_adjusted_history(
        self,
        code: str,
        start_date: str,
        end_date: str,
    ) -> pd.DataFrame:
        """Fetch Tencent's qfq series for audited investment_data extensions."""
        digits, exchange, market_code = normalize_stock_code(code)
        canonical_code = f"{digits}.{exchange}"
        start_ts = pd.Timestamp(start_date).normalize()
        end_ts = pd.Timestamp(end_date).normalize()
        if end_ts < start_ts:
            raise ValueError("end_date must be on or after start_date")

        response = self.session.get(
            TENCENT_ADJUSTED_KLINE_URL,
            params={
                "param": (
                    f"{market_code},day,{start_ts.strftime('%Y-%m-%d')},"
                    f"{end_ts.strftime('%Y-%m-%d')},{TENCENT_PAGE_SIZE},qfq"
                )
            },
            timeout=self.timeout,
        )
        response.raise_for_status()
        payload = _extract_json_object(response.text)
        stock_payload = (payload.get("data") or {}).get(market_code) or {}
        if isinstance(stock_payload.get("qfqday"), list):
            adjustment_key = "qfqday"
        elif isinstance(stock_payload.get("day"), list):
            adjustment_key = "day"
        else:
            raise RuntimeError("Tencent returned no adjusted daily bars")

        records: list[dict[str, Any]] = []
        for row in stock_payload[adjustment_key]:
            if not isinstance(row, list) or len(row) < 6:
                continue
            records.append(
                {
                    "date": row[0],
                    "code": canonical_code,
                    "open": row[1],
                    "close": row[2],
                    "high": row[3],
                    "low": row[4],
                    "source_volume": row[5],
                    "corporate_action": row[6] if len(row) > 6 and isinstance(row[6], dict) else {},
                    "adjustment_key": adjustment_key,
                    "source": "tencent_qfq" if adjustment_key == "qfqday" else "tencent_day_identity",
                }
            )
        frame = pd.DataFrame.from_records(records)
        if frame.empty:
            raise RuntimeError("Tencent returned no adjusted daily bars")
        frame["date"] = pd.to_datetime(frame["date"], errors="coerce")
        for field in ("open", "high", "low", "close", "source_volume"):
            frame[field] = pd.to_numeric(frame[field], errors="coerce")
        frame = frame[(frame["date"] >= start_ts) & (frame["date"] <= end_ts)]
        frame = frame.dropna(subset=["date"])
        frame = frame.drop_duplicates(subset=["date"], keep="last").sort_values("date")
        if frame.empty:
            raise RuntimeError("Tencent returned no adjusted bars inside the requested window")
        frame["date"] = frame["date"].dt.strftime("%Y-%m-%d")
        return frame.reset_index(drop=True)

    def _tencent_history(
        self,
        market_code: str,
        canonical_code: str,
        start_date: str,
        end_date: str,
    ) -> pd.DataFrame:
        start_ts = pd.Timestamp(start_date).normalize()
        end_ts = pd.Timestamp(end_date).normalize()
        if end_ts < start_ts:
            raise ValueError("end_date must be on or after start_date")

        pages: list[pd.DataFrame] = []
        cursor = end_ts
        previous_earliest: pd.Timestamp | None = None
        for _ in range(MAX_HISTORY_PAGES):
            page = self._fetch_tencent_page(market_code, canonical_code, start_ts, cursor)
            if page.empty:
                break
            pages.append(page)
            earliest = pd.to_datetime(page["date"]).min().normalize()
            if earliest <= start_ts:
                break
            if previous_earliest is not None and earliest >= previous_earliest:
                raise RuntimeError("Tencent history pagination did not advance")
            previous_earliest = earliest
            cursor = earliest - pd.Timedelta(days=1)

        if not pages:
            raise RuntimeError("Tencent returned no daily bars")
        frame = pd.concat(pages, ignore_index=True)
        frame["date"] = pd.to_datetime(frame["date"])
        frame = frame[(frame["date"] >= start_ts) & (frame["date"] <= end_ts)]
        frame = frame.drop_duplicates(subset=["date"], keep="last").sort_values("date")
        if frame.empty:
            raise RuntimeError("Tencent returned no daily bars inside the requested window")
        frame["date"] = frame["date"].dt.strftime("%Y-%m-%d")
        return frame.reset_index(drop=True)

    def _fetch_tencent_page(
        self,
        market_code: str,
        canonical_code: str,
        start_ts: pd.Timestamp,
        end_ts: pd.Timestamp,
    ) -> pd.DataFrame:
        params = {
            "_var": "kline_day",
            "param": (
                f"{market_code},day,{start_ts.strftime('%Y-%m-%d')},"
                f"{end_ts.strftime('%Y-%m-%d')},{TENCENT_PAGE_SIZE},"
            ),
        }
        response = self.session.get(TENCENT_KLINE_URL, params=params, timeout=self.timeout)
        response.raise_for_status()
        payload = _extract_json_object(response.text)
        stock_payload = (payload.get("data") or {}).get(market_code) or {}
        rows = stock_payload.get("day") or []
        if not isinstance(rows, list):
            raise ValueError("Tencent daily payload has an invalid row collection")

        records: list[dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, list) or len(row) < 9:
                continue
            records.append(
                {
                    "date": row[0],
                    "code": canonical_code,
                    "open": row[1],
                    "close": row[2],
                    "high": row[3],
                    "low": row[4],
                    "volume": float(row[5]) * _tencent_volume_multiplier(canonical_code),
                    # Tencent reports amount in CNY 10,000.
                    "amount": float(row[8]) * 10000.0,
                    "source_volume": float(row[5]),
                    "source_amount_10k_cny": float(row[8]),
                    "corporate_action": row[6] if isinstance(row[6], dict) else {},
                    "source": "tencent",
                }
            )
        return pd.DataFrame.from_records(records)

    def _ths_recent_history(
        self,
        digits: str,
        canonical_code: str,
        start_date: str,
        end_date: str,
    ) -> pd.DataFrame:
        referer = f"https://stockpage.10jqka.com.cn/{digits}/"
        headers = {"Referer": referer}
        last_response = self.session.get(
            f"{THS_KLINE_BASE_URL}/hs_{digits}/01/last.js",
            headers=headers,
            timeout=self.timeout,
        )
        last_response.raise_for_status()
        payload = _extract_json_object(last_response.text)
        raw_rows = str(payload.get("data") or "").split(";")

        records: list[dict[str, Any]] = []
        for raw_row in raw_rows:
            row = raw_row.split(",")
            if len(row) < 7:
                continue
            records.append(
                {
                    "date": row[0],
                    "code": canonical_code,
                    "open": row[1],
                    "high": row[2],
                    "low": row[3],
                    "close": row[4],
                    "volume": row[5],
                    "amount": row[6],
                    "source": "tonghuashun_public_fallback",
                }
            )

        today_response = self.session.get(
            f"{THS_KLINE_BASE_URL}/hs_{digits}/01/today.js",
            headers=headers,
            timeout=self.timeout,
        )
        today_response.raise_for_status()
        today_payload = _extract_json_object(today_response.text)
        today_values = next(iter(today_payload.values()), {})
        if isinstance(today_values, dict) and today_values.get("1"):
            records.append(
                {
                    "date": today_values.get("1"),
                    "code": canonical_code,
                    "open": today_values.get("7"),
                    "high": today_values.get("8"),
                    "low": today_values.get("9"),
                    "close": today_values.get("11"),
                    "volume": today_values.get("13"),
                    "amount": today_values.get("19"),
                    "source": "tonghuashun_public_fallback",
                }
            )

        frame = pd.DataFrame.from_records(records)
        if frame.empty:
            raise RuntimeError("Tonghuashun returned no recent daily bars")
        frame["date"] = pd.to_datetime(frame["date"], errors="coerce")
        start_ts = pd.Timestamp(start_date).normalize()
        end_ts = pd.Timestamp(end_date).normalize()
        frame = frame[(frame["date"] >= start_ts) & (frame["date"] <= end_ts)]
        frame = frame.drop_duplicates(subset=["date"], keep="last").sort_values("date")
        if frame.empty:
            raise RuntimeError("Tonghuashun returned no bars inside the requested window")
        frame["date"] = frame["date"].dt.strftime("%Y-%m-%d")
        return frame.reset_index(drop=True)
