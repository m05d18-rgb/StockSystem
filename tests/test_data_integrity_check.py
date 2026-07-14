"""
data_integrity_check.py 的回歸測試，對應這次修的兩個 bug：

1. check_symbol() 只看「陣列最後 N 筆」/「總筆數」，抓不到「抓取管線持續
   故障、資料停在很久以前，但歷史累積筆數夠多、欄位齊全」這種最典型的
   斷更情境——實測 250 筆完整歷史但最新一筆停在 31 天前，舊版邏輯回傳
   ok=True。正常情況改以全市場最後完整交易日判斷；個股落後該交易日就
   標示斷更或停止交易。只有全市場參考日期暫時不可用時，才退回
   calendar_days_between() 與 MARKET_DATA_MAX_STALE_DAYS 的曆日門檻。

2. 「完全沒有價格資料」跟「有資料但來源都不是官方(price_source 未通過
   驗證)」原本共用同一句訊息，讓人誤判成沒抓過資料，實際上兩者的修復
   方式不同。

全部用 patch.object(backend, ...) 餵假資料，不觸碰真實資料庫。

執行方式：
  python -m unittest tests.test_data_integrity_check -v
"""
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ml_backend import MARKET_DATA_MAX_STALE_DAYS, backend
import data_integrity_check
from data_integrity_check import check_model_freshness, check_symbol, run


def _price_row(date, close=100.0, price_source="TWSE"):
    return {
        "date": date, "open": close, "high": close, "low": close, "close": close,
        "volume": 1_000_000, "price_source": price_source,
    }


class CheckSymbolStalenessTests(unittest.TestCase):
    def test_stale_beyond_threshold_flags_issue(self):
        rows = [_price_row(f"2026-05-{d:02d}") for d in range(1, 26)]
        with patch.object(backend, "load_price_rows", return_value=rows), \
             patch.object(data_integrity_check, "today_key", return_value="2026-07-02"):
            result = check_symbol("TEST")
        self.assertFalse(result["ok"])
        self.assertTrue(any("疑似抓取管線斷更" in issue for issue in result["issues"]))

    def test_fresh_within_threshold_does_not_flag_issue(self):
        # model_data_quality 有自己一套 120 筆歷史窗的覆蓋率門檻，跟這裡要
        # 驗證的新鮮度判斷是兩件事，mock 掉讓這個測試只看新鮮度邏輯本身。
        rows = [_price_row(f"2026-06-{d:02d}") for d in range(20, 30)]
        with patch.object(backend, "load_price_rows", return_value=rows), \
             patch.object(backend, "model_data_quality", return_value={"ok": True, "missing": []}), \
             patch.object(data_integrity_check, "today_key", return_value="2026-07-02"):
            result = check_symbol("TEST")
        self.assertTrue(result["ok"])
        self.assertFalse(any("疑似抓取管線斷更" in issue for issue in result["issues"]))

    def test_exactly_at_threshold_is_not_flagged(self):
        # calendar_days_between("2026-06-26", "2026-07-02") == 6 == MARKET_DATA_MAX_STALE_DAYS，
        # 門檻是 "> 門檻" 才算過期，剛好等於門檻不該被標記。
        self.assertEqual(MARKET_DATA_MAX_STALE_DAYS, 6)
        rows = [_price_row("2026-06-26")]
        with patch.object(backend, "load_price_rows", return_value=rows), \
             patch.object(data_integrity_check, "today_key", return_value="2026-07-02"):
            result = check_symbol("TEST")
        self.assertFalse(any("疑似抓取管線斷更" in issue for issue in result["issues"]))

    def test_complete_market_date_is_used_instead_of_calendar_age(self):
        rows = [_price_row("2026-07-03")]
        with patch.object(backend, "load_price_rows", return_value=rows), \
             patch.object(backend, "model_data_quality", return_value={"ok": True, "missing": []}), \
             patch.object(data_integrity_check, "today_key", return_value="2026-07-13"):
            result = check_symbol("TEST", expected_latest_date="2026-07-09")

        self.assertFalse(result["ok"])
        issue = next(issue for issue in result["issues"] if "疑似抓取管線斷更" in issue)
        self.assertIn("全市場最後完整交易日 2026-07-09", issue)
        self.assertNotIn("距今 10 天", issue)


class CheckSymbolMissingDataMessageTests(unittest.TestCase):
    def test_no_rows_at_all_reports_completely_missing(self):
        with patch.object(backend, "load_price_rows", return_value=[]):
            result = check_symbol("TEST")
        self.assertFalse(result["ok"])
        self.assertEqual(result["issues"], ["完全沒有價格資料"])

    def test_rows_exist_but_none_verified_reports_distinct_message(self):
        rows = [_price_row("2026-06-30", price_source="unknown_scraper")]
        with patch.object(backend, "load_price_rows", return_value=rows):
            result = check_symbol("TEST")
        self.assertFalse(result["ok"])
        self.assertEqual(len(result["issues"]), 1)
        self.assertIn("來源皆非官方", result["issues"][0])
        self.assertIn("1 筆", result["issues"][0])
        self.assertNotEqual(result["issues"][0], "完全沒有價格資料")


class ModelEligibilityClassificationTests(unittest.TestCase):
    def test_model_sample_shortfall_is_not_reported_as_pipeline_failure(self):
        rows = [_price_row("2026-07-13")]
        quality = {
            "ok": False,
            "missing": ["priceRowsEnough", "marginSourceCoverageOk"],
        }
        with patch.object(backend, "load_price_rows", return_value=rows), \
             patch.object(backend, "model_data_quality", return_value=quality), \
             patch.object(data_integrity_check, "today_key", return_value="2026-07-13"):
            result = check_symbol("TEST", expected_latest_date="2026-07-13")

        self.assertTrue(result["ok"])
        self.assertEqual(result["issues"], [])
        self.assertFalse(result["modelEligible"])
        self.assertEqual(result["eligibilityIssues"], quality["missing"])

    def test_run_counts_ineligible_separately_from_broken_symbols(self):
        def fake_check(symbol, expected_latest_date=""):
            return {
                "symbol": symbol,
                "ok": True,
                "latestDate": expected_latest_date,
                "issues": [],
                "modelEligible": symbol != "NEW1",
                "eligibilityIssues": ["priceRowsEnough"] if symbol == "NEW1" else [],
            }

        healthy = {"ok": True, "issue": None}
        fresh_model = {
            "ok": True,
            "issue": None,
            "trainedAt": "2026-07-13 15:00:00",
            "ageDays": 0,
            "symbolCount": 1,
        }
        with patch.object(data_integrity_check, "check_symbol", side_effect=fake_check), \
             patch.object(backend, "latest_complete_price_date", return_value="2026-07-13"), \
             patch.object(data_integrity_check, "check_model_freshness", return_value=fresh_model), \
             patch.object(data_integrity_check, "check_model_gate", return_value=healthy), \
             patch.object(data_integrity_check, "check_prediction_settlement", return_value=healthy):
            result = run(symbols=["2330", "NEW1"])

        self.assertTrue(result["ok"])
        self.assertEqual(result["problemCount"], 0)
        self.assertEqual(result["modelIneligibleCount"], 1)
        self.assertEqual(result["modelIneligible"][0]["symbol"], "NEW1")


class CheckModelFreshnessLoadErrorTests(unittest.TestCase):
    """對應這次修的bug：check_model_freshness()原本讀backend._model_load_error
    (共享實例屬性，ThreadingHTTPServer下有TOCTOU競態)，改用
    load_model_with_error()回傳的tuple，這裡驗證回報的錯誤訊息確實來自
    這次呼叫本身。"""

    def test_load_failure_reports_error_from_this_call(self):
        with patch.object(backend, "load_model_with_error", return_value=(None, "這次真正的失敗原因")):
            result = check_model_freshness(stale_days=2)
        self.assertFalse(result["ok"])
        self.assertIn("這次真正的失敗原因", result["issue"])


class RunSingleSymbolFailureIsolationTests(unittest.TestCase):
    """對應這次修的 bug：run() 原本是 [check_symbol(s) for s in symbols]，
    任何一檔股票的例外(DB連線瞬斷/資料型別異常)沒接住會讓整個 list
    comprehension 中斷，當天所有股票的檢查結果全部消失。daily_update.py
    呼叫端又是整包 try/except，最後只回報一個籠統的 error，其他明明正常的
    股票也拿不到結果、LINE也不會收到「有問題」通知。"""

    def test_one_symbol_raising_does_not_abort_the_whole_batch(self):
        def fake_check_symbol(symbol, expected_latest_date=""):
            if symbol == "BOOM":
                raise RuntimeError("模擬DB連線瞬斷")
            return {"symbol": symbol, "ok": True, "latestDate": "2026-07-02", "issues": []}

        with patch.object(data_integrity_check, "check_symbol", side_effect=fake_check_symbol), \
             patch.object(backend, "latest_complete_price_date", return_value="2026-07-02"), \
             patch.object(data_integrity_check, "check_model_freshness", return_value={"ok": True, "trainedAt": "2026-07-02 00:00:00", "ageDays": 0, "symbolCount": 1, "issue": None}):
            result = run(symbols=["OK1", "BOOM", "OK2"], stale_days=2)

        self.assertEqual(result["total"], 3)
        self.assertEqual(result["problemCount"], 1)
        boom_result = next(p for p in result["problems"] if p["symbol"] == "BOOM")
        self.assertIn("模擬DB連線瞬斷", boom_result["issues"][0])
        # 另外兩檔正常股票的結果不該被 BOOM 的例外拖累消失
        ok_symbols = {r["symbol"] for r in result["problems"]}
        self.assertNotIn("OK1", ok_symbols)
        self.assertNotIn("OK2", ok_symbols)
        self.assertEqual(result["expectedLatestDate"], "2026-07-02")


if __name__ == "__main__":
    unittest.main()
