"""
market_data_quality 的回歸測試，對應這次修的 bug：

之前只檢查 MarketContext.close(key, latest_date) <= 0 是否為正數，
沒有比對「找到的那一筆資料」實際日期跟查詢日期差了幾天。row_index 用
bisect_right 找「最近一筆 <= latest_date 的資料」，就算那筆資料是好幾天前
的舊資料，只要收盤價 > 0 一樣會被判定成新鮮——導致某個大盤來源(TAIEX/
OTC/USDTWD)連續好幾天抓不到資料時，predict_symbol 會完全沒有察覺，
直接拿過期的大盤資料餵給 market_gate/adjust_probability_for_market。

修法：新增 MarketContext.latest_available_date() 取得實際命中的那筆資料
日期，market_data_quality 用 calendar_days_between() 比對跟查詢日期的
日曆天數差，超過 MARKET_DATA_MAX_STALE_DAYS(6天，容忍週末/連續假期)才
判定為過期。

全部用 patch.object(backend, "load_market_rows", ...) 餵假資料，不觸碰
真實資料庫。

執行方式：
  python -m unittest tests.test_market_data_quality -v
"""
import datetime as dt
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ml_backend import MARKET_DATA_MAX_STALE_DAYS, MarketContext, backend, calendar_days_between


def _consecutive_rows(end_date, count, close=100.0):
    end = dt.date.fromisoformat(end_date)
    return [
        {"date": (end - dt.timedelta(days=offset)).isoformat(), "close": close}
        for offset in range(count - 1, -1, -1)
    ]


def _fresh_market_rows(latest_date, overrides=None):
    overrides = overrides or {}
    rows = {
        "TAIEX": _consecutive_rows(latest_date, 25),
        "OTC": _consecutive_rows(latest_date, 25),
        "NASDAQ": _consecutive_rows(latest_date, 25),
        "SP500": _consecutive_rows(latest_date, 25),
        "USDTWD": _consecutive_rows(latest_date, 25),
    }
    rows.update(overrides)
    return rows


class CalendarDaysBetweenTests(unittest.TestCase):
    def test_same_date_is_zero(self):
        self.assertEqual(calendar_days_between("2026-07-02", "2026-07-02"), 0)

    def test_counts_calendar_days_regardless_of_order(self):
        self.assertEqual(calendar_days_between("2026-06-26", "2026-07-02"), 6)
        self.assertEqual(calendar_days_between("2026-07-02", "2026-06-26"), 6)

    def test_invalid_input_returns_zero_instead_of_raising(self):
        self.assertEqual(calendar_days_between(None, "2026-07-02"), 0)
        self.assertEqual(calendar_days_between("not-a-date", "2026-07-02"), 0)


class MarketContextLatestAvailableDateTests(unittest.TestCase):
    def test_returns_date_of_row_actually_used_by_close(self):
        context = MarketContext({"TAIEX": _consecutive_rows("2026-06-25", 10)})
        # 查詢日期(latest_date)比資料最後一天晚很多，row_index 只能往回找到 06-25
        self.assertEqual(context.latest_available_date("TAIEX", "2026-07-02"), "2026-06-25")

    def test_returns_none_when_no_rows_at_or_before_date(self):
        context = MarketContext({"TAIEX": _consecutive_rows("2026-07-02", 5)})
        self.assertIsNone(context.latest_available_date("TAIEX", "2026-01-01"))

    def test_returns_none_for_missing_key(self):
        context = MarketContext({})
        self.assertIsNone(context.latest_available_date("TAIEX", "2026-07-02"))


class MarketDataQualityStalenessTests(unittest.TestCase):
    LATEST_DATE = "2026-07-02"

    def test_all_sources_fresh_is_ok(self):
        with patch.object(backend, "load_market_rows", return_value=_fresh_market_rows(self.LATEST_DATE)):
            result = backend.market_data_quality(self.LATEST_DATE)
        self.assertTrue(result["ok"])
        self.assertEqual(result["missing"], [])

    def test_gap_within_weekend_style_tolerance_is_still_ok(self):
        # TAIEX 最後一筆資料是查詢日期 3 天前(例如週五收盤、週一查詢)，
        # 在 MARKET_DATA_MAX_STALE_DAYS 容忍範圍內，不該被判定過期。
        overrides = {"TAIEX": _consecutive_rows("2026-06-29", 25)}
        with patch.object(backend, "load_market_rows", return_value=_fresh_market_rows(self.LATEST_DATE, overrides)):
            result = backend.market_data_quality(self.LATEST_DATE)
        self.assertTrue(result["ok"])

    def test_source_stale_beyond_threshold_is_flagged(self):
        stale_date = (dt.date.fromisoformat(self.LATEST_DATE) - dt.timedelta(days=MARKET_DATA_MAX_STALE_DAYS + 1)).isoformat()
        overrides = {"TAIEX": _consecutive_rows(stale_date, 25)}
        with patch.object(backend, "load_market_rows", return_value=_fresh_market_rows(self.LATEST_DATE, overrides)):
            result = backend.market_data_quality(self.LATEST_DATE)
        self.assertFalse(result["ok"])
        self.assertTrue(any(item.startswith("TAIEX(stale") for item in result["missing"]))

    def test_zero_close_still_reported_as_plain_missing_not_stale(self):
        # 既有的「收盤價<=0」判斷不能因為這次改動被覆蓋掉或改變訊息格式。
        overrides = {"TAIEX": _consecutive_rows(self.LATEST_DATE, 25, close=0.0)}
        with patch.object(backend, "load_market_rows", return_value=_fresh_market_rows(self.LATEST_DATE, overrides)):
            result = backend.market_data_quality(self.LATEST_DATE)
        self.assertFalse(result["ok"])
        self.assertIn("TAIEX", result["missing"])

    def test_multiple_stale_sources_all_reported(self):
        stale_date = (dt.date.fromisoformat(self.LATEST_DATE) - dt.timedelta(days=MARKET_DATA_MAX_STALE_DAYS + 3)).isoformat()
        overrides = {
            "TAIEX": _consecutive_rows(stale_date, 25),
            "USDTWD": _consecutive_rows(stale_date, 25),
        }
        with patch.object(backend, "load_market_rows", return_value=_fresh_market_rows(self.LATEST_DATE, overrides)):
            result = backend.market_data_quality(self.LATEST_DATE)
        self.assertFalse(result["ok"])
        self.assertTrue(any(item.startswith("TAIEX(stale") for item in result["missing"]))
        self.assertTrue(any(item.startswith("USDTWD(stale") for item in result["missing"]))


if __name__ == "__main__":
    unittest.main()
