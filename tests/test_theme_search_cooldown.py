"""
handle_ai_theme_search 冷卻時間的回歸測試，對應這次修的問題：

Perplexity 題材搜尋每次呼叫都是真實計費的外部 API 呼叫，原本完全沒有節流，
多分頁/多裝置各自按「手動掃描」會各自觸發，短時間內可能疊加非預期費用/
額度消耗。修法：跟 monster_scan_lock 同樣道理，用全域鎖+冷卻時間戳擋掉
短時間內的重複觸發。

用 patch.object 讓 perplexity_text 回傳假結果，不觸碰真實 Perplexity API。

執行方式：
  python -m unittest tests.test_theme_search_cooldown -v
"""
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server as server_module


class FakeRequest:
    def makefile(self, *args, **kwargs):
        import io
        return io.BytesIO()


class ThemeSearchCooldownTests(unittest.TestCase):
    def setUp(self):
        server_module.last_theme_search_at = 0.0

    def _make_handler(self):
        handler = server_module.StockHandler.__new__(server_module.StockHandler)
        handler.write_json = lambda payload, status=200: setattr(handler, "_response", (status, payload))
        return handler

    def test_first_call_within_cooldown_window_proceeds(self):
        handler = self._make_handler()
        with patch.object(server_module.backend, "connect") as mock_connect, \
             patch.object(server_module.StockHandler, "perplexity_text", return_value="假結果"):
            mock_connect.return_value.__enter__.return_value.execute.return_value.fetchall.return_value = []
            handler.handle_ai_theme_search()
        status, payload = handler._response
        self.assertTrue(payload.get("ok"))

    def test_second_call_immediately_after_is_blocked_by_cooldown(self):
        handler1 = self._make_handler()
        handler2 = self._make_handler()
        with patch.object(server_module.backend, "connect") as mock_connect, \
             patch.object(server_module.StockHandler, "perplexity_text", return_value="假結果") as mock_perplexity:
            mock_connect.return_value.__enter__.return_value.execute.return_value.fetchall.return_value = []
            handler1.handle_ai_theme_search()
            handler2.handle_ai_theme_search()
        status2, payload2 = handler2._response
        self.assertFalse(payload2.get("ok"))
        self.assertTrue(payload2.get("cooldown"))
        # 第二次呼叫應該在冷卻檢查就被擋掉，perplexity_text 只該被呼叫一次(第一次)。
        self.assertEqual(mock_perplexity.call_count, 1)

    def test_hot_sectors_are_capped_in_count_and_length(self):
        # hot_sectors目前來自FinMind官方股票產業分類統計(受控固定枚舉值)，
        # 但這是外部資料流進送給第三方Perplexity API的prompt的邊界，這裡
        # 驗證即使上游meta資料異常混入超過6筆或超長字串，也會被截斷，
        # 不會讓prompt長度/費用不受控。
        import json as json_module
        many_long_sectors = [f"測試產業{i}" * 10 for i in range(10)]
        handler = self._make_handler()
        with patch.object(server_module.backend, "connect") as mock_connect, \
             patch.object(server_module.StockHandler, "perplexity_text", return_value="假結果"):
            mock_connect.return_value.__enter__.return_value.execute.return_value.fetchall.return_value = [
                ("last_monster_hot_sectors", json_module.dumps(many_long_sectors)),
            ]
            handler.handle_ai_theme_search()
        status, payload = handler._response
        self.assertTrue(payload.get("ok"))
        grounded = payload["groundedHotSectors"]
        self.assertLessEqual(len(grounded), 6)
        for sector in grounded:
            self.assertLessEqual(len(sector), 20)

    def test_call_after_cooldown_window_elapses_proceeds(self):
        handler1 = self._make_handler()
        handler2 = self._make_handler()
        with patch.object(server_module.backend, "connect") as mock_connect, \
             patch.object(server_module.StockHandler, "perplexity_text", return_value="假結果") as mock_perplexity:
            mock_connect.return_value.__enter__.return_value.execute.return_value.fetchall.return_value = []
            handler1.handle_ai_theme_search()
            server_module.last_theme_search_at -= (server_module.THEME_SEARCH_COOLDOWN_SECONDS + 1)
            handler2.handle_ai_theme_search()
        status2, payload2 = handler2._response
        self.assertTrue(payload2.get("ok"))
        self.assertEqual(mock_perplexity.call_count, 2)


if __name__ == "__main__":
    unittest.main()
