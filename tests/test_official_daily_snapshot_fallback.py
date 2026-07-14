import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ml_backend import backend


def _twse_after_payload(date="20260713", symbol="2330"):
    return {
        "stat": "OK",
        "date": date,
        "tables": [{
            "title": "每日收盤行情",
            "fields": [
                "證券代號", "證券名稱", "成交股數", "開盤價", "最高價",
                "最低價", "收盤價", "本益比",
            ],
            "data": [[symbol, "台積電", "12,345", "1,000", "1,020", "990", "1,010", "25.5"]],
        }],
    }


def _tpex_after_payload(date="20260713", symbol="6488"):
    return {
        "stat": "ok",
        "date": date,
        "tables": [{
            "title": "上櫃股票行情",
            "fields": ["代號", "名稱", "收盤", "開盤", "最高", "最低", "成交股數"],
            "data": [
                ["006201", "元大富櫃50", "45.9", "46", "47", "45", "100"],
                [symbol, "環球晶", "500", "490", "505", "485", "9,876"],
                ["6488P1", "測試權證", "1.2", "1", "1.3", "0.9", "999"],
            ],
        }],
    }


def _tpex_emerging_payload(date="1150713", symbol="7566", average="11.86"):
    return [{
        "Date": date,
        "SecuritiesCompanyCode": symbol,
        "PreviousAveragePrice": "11.98",
        "Highest": "12.10",
        "Lowest": "11.70",
        "Average": average,
        "LatestPrice": "11.90",
        "TransactionVolume": "159032",
    }]


class DatedAfterTradingSnapshotTests(unittest.TestCase):
    def test_twse_requires_exact_date_and_parses_daily_close_table(self):
        with patch.object(backend, "fetch_openapi_json", return_value=_twse_after_payload()), \
             patch("ml_backend.TWSE_AFTER_TRADING_MIN_ROWS", 1):
            rows = backend.fetch_twse_after_trading_snapshot_rows("2026-07-13")

        row = rows["2330"]
        self.assertEqual(row["date"], "2026-07-13")
        self.assertEqual(row["volume"], 12345)
        self.assertEqual(row["close"], 1010)
        self.assertEqual(row["per"], 25.5)
        self.assertIn("afterTrading", row["price_source"])

    def test_twse_rejects_a_response_for_another_session(self):
        with patch.object(backend, "fetch_openapi_json", return_value=_twse_after_payload("20260709")):
            with self.assertRaisesRegex(RuntimeError, "尚未提供 2026-07-13"):
                backend.fetch_twse_after_trading_snapshot_rows("2026-07-13")

    def test_tpex_keeps_only_four_digit_common_stock_codes(self):
        with patch.object(backend, "fetch_openapi_json", return_value=_tpex_after_payload()), \
             patch("ml_backend.TPEX_AFTER_TRADING_MIN_ROWS", 1):
            rows = backend.fetch_tpex_after_trading_snapshot_rows("2026-07-13")

        self.assertEqual(set(rows), {"6488"})
        self.assertEqual(rows["6488"]["volume"], 9876)
        self.assertIn("afterTrading", rows["6488"]["price_source"])

    def test_tpex_emerging_uses_official_weighted_average_as_daily_reference(self):
        with patch.object(backend, "fetch_openapi_json", return_value=_tpex_emerging_payload()), \
             patch("ml_backend.TPEX_EMERGING_MIN_ROWS", 1):
            rows = backend.fetch_tpex_emerging_official_latest()

        row = rows["7566"]
        self.assertEqual(row["date"], "2026-07-13")
        self.assertEqual(row["open"], 11.86)
        self.assertEqual(row["close"], 11.86)
        self.assertEqual(row["high"], 12.10)
        self.assertEqual(row["low"], 11.70)
        self.assertEqual(row["volume"], 159032)
        self.assertIn("weighted average", row["price_source"])

    def test_tpex_emerging_no_trade_carries_official_previous_average_with_zero_volume(self):
        payload = _tpex_emerging_payload(average="-")
        payload[0].update({"Highest": "-", "Lowest": "-", "TransactionVolume": "-"})
        with patch.object(backend, "fetch_openapi_json", return_value=payload), \
             patch("ml_backend.TPEX_EMERGING_MIN_ROWS", 1):
            rows = backend.fetch_tpex_emerging_official_latest()

        row = rows["7566"]
        self.assertEqual(row["open"], 11.98)
        self.assertEqual(row["high"], 11.98)
        self.assertEqual(row["low"], 11.98)
        self.assertEqual(row["close"], 11.98)
        self.assertEqual(row["volume"], 0)
        self.assertIn("no-trade previous average", row["price_source"])


class OfficialSnapshotFallbackSelectionTests(unittest.TestCase):
    def test_lagging_latest_openapi_is_upgraded_by_exact_date_tables(self):
        def fake_fetch(url):
            if url.endswith("STOCK_DAY_ALL"):
                return [{
                    "Code": "2330", "Date": "1150709", "ClosingPrice": "980",
                    "OpeningPrice": "970", "HighestPrice": "985", "LowestPrice": "965",
                    "TradeVolume": "10,000",
                }]
            if url.endswith("BWIBBU_ALL"):
                return []
            if url.endswith("tpex_mainboard_daily_close_quotes"):
                return [{
                    "SecuritiesCompanyCode": "6488", "Date": "1150709", "Close": "480",
                    "Open": "475", "High": "485", "Low": "470", "TradingShares": "8,000",
                }]
            if url.endswith("tpex_mainboard_peratio_analysis"):
                return []
            if url.endswith("tpex_esb_latest_statistics"):
                return _tpex_emerging_payload()
            if "MI_INDEX" in url:
                return _twse_after_payload()
            if "stk_quote_result.php" in url:
                return _tpex_after_payload()
            raise AssertionError(f"unexpected URL: {url}")

        with patch.object(backend, "fetch_openapi_json", side_effect=fake_fetch), \
             patch("ml_backend.TWSE_AFTER_TRADING_MIN_ROWS", 1), \
             patch("ml_backend.TPEX_AFTER_TRADING_MIN_ROWS", 1), \
             patch("ml_backend.TPEX_EMERGING_MIN_ROWS", 1):
            rows, errors = backend.fetch_official_daily_snapshot_rows(target_date="2026-07-13")

        self.assertEqual(errors, [])
        self.assertEqual(rows["2330"]["date"], "2026-07-13")
        self.assertEqual(rows["6488"]["date"], "2026-07-13")
        self.assertEqual(rows["7566"]["date"], "2026-07-13")
        self.assertIn("afterTrading", rows["2330"]["price_source"])
        self.assertIn("afterTrading", rows["6488"]["price_source"])
        self.assertIn("weighted average", rows["7566"]["price_source"])

    def test_incomplete_fallback_does_not_replace_last_complete_rows(self):
        def fake_fetch(url):
            if url.endswith("STOCK_DAY_ALL"):
                return [{
                    "Code": "2330", "Date": "1150709", "ClosingPrice": "980",
                    "OpeningPrice": "970", "HighestPrice": "985", "LowestPrice": "965",
                    "TradeVolume": "10,000",
                }]
            if url.endswith("BWIBBU_ALL") or url.endswith("tpex_mainboard_daily_close_quotes") \
                    or url.endswith("tpex_mainboard_peratio_analysis"):
                return []
            if url.endswith("tpex_esb_latest_statistics"):
                return _tpex_emerging_payload()
            if "MI_INDEX" in url:
                return _twse_after_payload()
            if "stk_quote_result.php" in url:
                return _tpex_after_payload()
            raise AssertionError(f"unexpected URL: {url}")

        with patch.object(backend, "fetch_openapi_json", side_effect=fake_fetch), \
             patch("ml_backend.TWSE_AFTER_TRADING_MIN_ROWS", 2), \
             patch("ml_backend.TPEX_AFTER_TRADING_MIN_ROWS", 2):
            rows, errors = backend.fetch_official_daily_snapshot_rows(target_date="2026-07-13")

        self.assertEqual(rows["2330"]["date"], "2026-07-09")
        self.assertGreaterEqual(len(errors), 2)


if __name__ == "__main__":
    unittest.main()
