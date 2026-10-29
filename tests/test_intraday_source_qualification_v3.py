import sys
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


BACKEND_ROOT = Path(__file__).resolve().parents[1] / "frontend-v2" / "backend"
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from market_data.intraday_source_qualification_v3 import (
    build_source_qualification_report,
)


class IntradaySourceQualificationV3Tests(unittest.TestCase):
    def _reference_and_quotes(self):
        symbols = [f"{index:06d}.SZ" for index in range(1, 301)] + [
            "000300.SH"
        ]
        reference_rows = []
        quotes = {}
        for index, symbol in enumerate(symbols, start=1):
            reference_turnover = None if symbol == "000300.SH" else 0.43
            reference_rows.append(
                {
                    "symbol": symbol,
                    "price": 10.0,
                    "previousClose": 10.1,
                    "turnoverPercent": reference_turnover,
                    "tradeDate": "2026-08-14",
                    "source": "a-stock-data/tencent-public",
                }
            )
            quotes[symbol] = {
                "symbol": symbol,
                "price": 10.001,
                "previousClose": 10.101,
                "turnoverPercent": (
                    None if symbol == "000300.SH" else 0.431
                ),
                "tradeDate": "2026-08-14",
                "source": "a-stock-data/easy-tdx-public",
            }
        reference = {
            "contentSha256": "a" * 64,
            "captureSlot": "2026-08-14T15:00:00+08:00",
            "quoteTradeDate": "2026-08-14",
            "universe": {
                "symbols": symbols,
                "symbolsSha256": "b" * 64,
            },
            "quotes": reference_rows,
        }
        return reference, quotes

    def test_complete_independent_source_passes_without_quote_values(self):
        reference, quotes = self._reference_and_quotes()
        report = build_source_qualification_report(
            reference_snapshot=reference,
            reference_path=Path("/tmp/reference.json.gz"),
            reference_archive_sha256="c" * 64,
            quotes=quotes,
            measured_at=datetime(
                2026, 8, 14, 22, 0, tzinfo=ZoneInfo("Asia/Shanghai")
            ),
            query_elapsed_seconds=5.25,
        )

        self.assertEqual(report["status"], "PASS")
        self.assertTrue(all(report["gateResults"].values()))
        self.assertEqual(report["observed"]["coreValidQuoteCount"], 301)
        self.assertFalse(report["profitEvidenceEligible"])
        self.assertFalse(report["returnValuesIncluded"])
        self.assertFalse(report["individualQuoteValuesIncluded"])
        self.assertNotIn("symbols", report)

    def test_missing_quote_fails_closed(self):
        reference, quotes = self._reference_and_quotes()
        quotes.pop(next(iter(quotes)))
        report = build_source_qualification_report(
            reference_snapshot=reference,
            reference_path=Path("/tmp/reference.json.gz"),
            reference_archive_sha256="c" * 64,
            quotes=quotes,
            measured_at=datetime(
                2026, 8, 14, 22, 0, tzinfo=ZoneInfo("Asia/Shanghai")
            ),
            query_elapsed_seconds=5.25,
        )

        self.assertEqual(report["status"], "FAIL")
        self.assertFalse(report["gateResults"]["exactUniverse"])
        self.assertFalse(report["gateResults"]["allCoreFields"])
        self.assertEqual(report["observed"]["missingQuoteCount"], 1)


if __name__ == "__main__":
    unittest.main()
