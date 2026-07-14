"""
ml_backend.py 對新股(上市未滿120個交易日)的回歸測試，對應這次修的問題：

build_features_for_rows() 對 len(rows)<120 直接 return []。quick_monster_
filter() 舊版吃到空 features 直接 return None，跟同一個函式裡其他拒絕
原因(例如 missing_verified_source)不一致地完全消失、不會被 scan_monster_
scores 的 errors 清單記錄到——使用者只看到候選數字變化，無法分辨是「真的
不夠強」還是「新股被硬性排除」。monster_score_for_symbol() 則是 raise
RuntimeError("... has no monster features")，訊息不夠明確。

修法：quick_monster_filter() 改成回傳跟其他拒絕原因同樣結構的
{"ok": False, "reason": "insufficient_history", ...} dict；
monster_score_for_symbol() 的例外訊息帶上明確原因與實際列數。

全部用合成K線(不足120天)，不碰資料庫/網路。

執行方式：
  python -m unittest tests.test_new_stock_insufficient_history -v
"""
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ml_backend import backend

TEST_SYMBOL = "TEST9903"


def make_short_history_rows(days=60):
    rows = []
    for i in range(days):
        close = 100.0 + i * 0.1
        rows.append({
            "symbol": TEST_SYMBOL,
            "date": f"2026-{(i // 28) + 1:02d}-{(i % 28) + 1:02d}",
            "open": close, "high": close * 1.01, "low": close * 0.99,
            "close": close, "volume": 1_000_000,
            "price_source": "FinMind TaiwanStockPrice",
        })
    return rows


class QuickMonsterFilterInsufficientHistoryTests(unittest.TestCase):
    def test_returns_structured_rejection_instead_of_bare_none(self):
        rows = make_short_history_rows(60)
        with patch.object(backend, "load_price_rows", return_value=rows), \
             patch.object(backend, "rows_with_verified_sources", return_value=rows), \
             patch.object(backend, "latest_complete_price_date", return_value=rows[-1]["date"]):
            result = backend.quick_monster_filter(TEST_SYMBOL)

        self.assertIsNotNone(result, "不該再回傳裸 None，應該回傳結構化的拒絕原因")
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "insufficient_history")
        self.assertEqual(result["symbol"], TEST_SYMBOL)
        self.assertEqual(result["rowCount"], 60)

    def test_shape_matches_other_rejection_reasons(self):
        # 跟 missing_verified_source 那條路徑回傳同樣的欄位集合，呼叫端
        # 才能用同一套邏輯處理各種拒絕原因，不需要另外特判 None。
        rows = make_short_history_rows(60)
        with patch.object(backend, "load_price_rows", return_value=rows), \
             patch.object(backend, "rows_with_verified_sources", return_value=rows), \
             patch.object(backend, "latest_complete_price_date", return_value=rows[-1]["date"]):
            result = backend.quick_monster_filter(TEST_SYMBOL)
        with patch.object(backend, "load_price_rows", return_value=[]), \
             patch.object(backend, "rows_with_verified_sources", return_value=[]), \
             patch.object(backend, "latest_complete_price_date", return_value="2026-07-13"), \
             patch.object(backend, "update_prices"):
            missing_source_result = backend.quick_monster_filter(TEST_SYMBOL)

        expected_keys = set(missing_source_result.keys())
        self.assertEqual(set(result.keys()) - {"rowCount"}, expected_keys)

    def test_sufficient_history_is_not_affected(self):
        rows = make_short_history_rows(150)
        with patch.object(backend, "load_price_rows", return_value=rows), \
             patch.object(backend, "rows_with_verified_sources", return_value=rows), \
             patch.object(backend, "latest_complete_price_date", return_value=rows[-1]["date"]):
            result = backend.quick_monster_filter(TEST_SYMBOL)
        self.assertIsNotNone(result)
        self.assertNotEqual(result.get("reason"), "insufficient_history")


class MonsterScoreForSymbolInsufficientHistoryTests(unittest.TestCase):
    def test_error_message_states_reason_and_row_count(self):
        rows = make_short_history_rows(60)
        quality = {"ok": True, "missing": [], "rows": 60}
        with patch.object(backend, "ensure_rule_analysis_rows", return_value=(rows, quality)):
            with self.assertRaises(RuntimeError) as ctx:
                backend.monster_score_for_symbol(TEST_SYMBOL)
        message = str(ctx.exception)
        self.assertIn("insufficient_history", message)
        self.assertIn("60", message)


if __name__ == "__main__":
    unittest.main()
