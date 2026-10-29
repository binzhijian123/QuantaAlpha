from __future__ import annotations

import math
import re
from pathlib import Path
from typing import Iterable

import numpy as np
import pandas as pd

from quantaalpha.trading.paper import DailyBar, normalize_symbol
from quantaalpha.utils.qlib_provider import calendar_bounds, candidate_provider_paths


def discover_latest_provider() -> Path:
    candidates: list[tuple[str, Path]] = []
    for provider in candidate_provider_paths({}):
        bounds = calendar_bounds(provider)
        if bounds is not None:
            candidates.append((bounds[1], provider))
    if not candidates:
        raise RuntimeError("no readable Qlib provider is configured")
    return max(candidates, key=lambda item: item[0])[1]


class QlibBinaryDailySource:
    """Read the small set of daily fields needed by the paper engine."""

    def __init__(self, provider_path: str | Path | None = None):
        self.provider_path = Path(provider_path) if provider_path else discover_latest_provider()
        calendar_file = self.provider_path / "calendars" / "day.txt"
        self.calendar = [line.strip() for line in calendar_file.read_text(encoding="utf-8").splitlines() if line.strip()]
        if not self.calendar:
            raise RuntimeError(f"empty Qlib calendar: {calendar_file}")
        self._date_index = {value: index for index, value in enumerate(self.calendar)}

    def latest_dates(self, count: int = 2) -> list[str]:
        return self.calendar[-max(1, count):]

    def next_date_after(self, signal_date: str) -> str | None:
        return next((value for value in self.calendar if value > signal_date), None)

    def available_symbols(self, as_of: str | None = None) -> list[str]:
        effective_date = as_of or self.calendar[-1]
        instrument_dir = self.provider_path / "instruments"
        instrument_file = instrument_dir / "csi300.txt"
        if not instrument_file.exists():
            instrument_file = instrument_dir / "all.txt"
        if not instrument_file.exists():
            raise RuntimeError(f"missing Qlib instrument file: {instrument_dir}")

        symbols: list[str] = []
        for line in instrument_file.read_text(encoding="utf-8").splitlines():
            parts = line.split("\t")
            if len(parts) < 3 or not (parts[1] <= effective_date <= parts[2]):
                continue
            symbols.append(normalize_symbol(parts[0].upper()))
        return sorted(set(symbols))

    def history_frame(
        self,
        end_date: str,
        start_date: str | None = None,
        symbols: Iterable[str] | None = None,
        fields: Iterable[str] | None = None,
    ) -> pd.DataFrame:
        if end_date not in self._date_index:
            raise ValueError(f"history end date is not available: {end_date}")
        start_date = start_date or self.calendar[0]
        if start_date not in self._date_index:
            start_date = next((value for value in self.calendar if value >= start_date), "")
        if not start_date or start_date > end_date:
            raise ValueError(f"invalid history window: {start_date} to {end_date}")

        start_index = self._date_index[start_date]
        end_index = self._date_index[end_date]
        dates = pd.to_datetime(self.calendar[start_index : end_index + 1])
        selected = list(symbols) if symbols is not None else self.available_symbols(end_date)
        selected_fields = list(
            dict.fromkeys(fields or ("open", "high", "low", "close", "volume", "vwap"))
        )
        invalid_fields = [
            field
            for field in selected_fields
            if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", str(field))
        ]
        if invalid_fields:
            raise ValueError(f"invalid Qlib fields: {invalid_fields}")
        frames: list[pd.DataFrame] = []
        for raw_symbol in selected:
            symbol = normalize_symbol(raw_symbol)
            values = {
                f"${field}": self._field_slice(symbol, field, start_index, end_index)
                for field in selected_fields
            }
            frame = pd.DataFrame(values, index=dates)
            frame["instrument"] = symbol
            frame.index.name = "datetime"
            frames.append(frame.set_index("instrument", append=True))
        if not frames:
            return pd.DataFrame()
        return pd.concat(frames).sort_index()

    def get_bars(self, symbols: Iterable[str], trade_date: str) -> dict[str, DailyBar]:
        if trade_date not in self._date_index:
            raise ValueError(f"trade date is not available in provider calendar: {trade_date}")
        index = self._date_index[trade_date]
        bars: dict[str, DailyBar] = {}
        for raw_symbol in symbols:
            symbol = normalize_symbol(raw_symbol)
            fields = {name: self._value(symbol, name, index) for name in ("open", "high", "low", "close", "volume")}
            previous_close = self._value(symbol, "close", index - 1) if index > 0 else None
            factor = self._value(symbol, "factor", index)
            listing_sessions = self._listing_sessions(symbol, index)
            if all(value is None for value in fields.values()):
                continue
            bars[symbol] = DailyBar(
                symbol=symbol,
                trade_date=trade_date,
                open=float(fields["open"] or math.nan),
                high=float(fields["high"] or math.nan),
                low=float(fields["low"] or math.nan),
                close=float(fields["close"] or math.nan),
                volume=float(fields["volume"] or 0.0),
                previous_close=previous_close,
                factor=factor,
                listing_sessions=listing_sessions,
            )
        return bars

    def _listing_sessions(self, symbol: str, calendar_index: int) -> int | None:
        digits, exchange = symbol.split(".")
        for field in ("open", "close"):
            feature_file = (
                self.provider_path
                / "features"
                / f"{exchange.lower()}{digits}"
                / f"{field}.day.bin"
            )
            if not feature_file.exists():
                continue
            values = np.fromfile(feature_file, dtype="<f")
            if len(values) < 2:
                continue
            finite_offsets = np.flatnonzero(np.isfinite(values[1:]))
            if not len(finite_offsets):
                continue
            first_calendar_index = int(values[0]) + int(finite_offsets[0])
            if calendar_index < first_calendar_index:
                return None
            return calendar_index - first_calendar_index + 1
        return None

    def _value(self, symbol: str, field: str, calendar_index: int) -> float | None:
        digits, exchange = symbol.split(".")
        feature_file = self.provider_path / "features" / f"{exchange.lower()}{digits}" / f"{field}.day.bin"
        if not feature_file.exists():
            return None
        values = np.fromfile(feature_file, dtype="<f")
        if len(values) < 2:
            return None
        start_index = int(values[0])
        offset = calendar_index - start_index
        if offset < 0 or offset >= len(values) - 1:
            return None
        value = float(values[offset + 1])
        return value if math.isfinite(value) else None

    def _field_slice(self, symbol: str, field: str, start_index: int, end_index: int) -> np.ndarray:
        result = np.full(end_index - start_index + 1, np.nan, dtype="float64")
        digits, exchange = symbol.split(".")
        feature_file = self.provider_path / "features" / f"{exchange.lower()}{digits}" / f"{field}.day.bin"
        if not feature_file.exists():
            return result
        values = np.fromfile(feature_file, dtype="<f")
        if len(values) < 2:
            return result

        field_start = int(values[0])
        field_end = field_start + len(values) - 2
        overlap_start = max(start_index, field_start)
        overlap_end = min(end_index, field_end)
        if overlap_start > overlap_end:
            return result
        source_start = overlap_start - field_start + 1
        source_end = overlap_end - field_start + 2
        target_start = overlap_start - start_index
        target_end = overlap_end - start_index + 1
        result[target_start:target_end] = values[source_start:source_end]
        return result
