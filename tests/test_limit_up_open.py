"""
漲停打開警示(detect_limit_up_open / notify_limit_up_open)的回歸測試。

功能：妖股候選盤中最高摸到漲停、但現價已回落離開漲停 = 漲停打開(賣壓湧現、
動能失敗的短線出場/避開訊號)，當天每檔第一次偵測到推 LINE 一次(每日上限、
每檔去重、先標記再送)。

隔離鐵律：去重狀態存正式 model_meta，測試一律 patch LIMIT_UP_OPEN_NOTIFY_STATE_KEY
成 __test_*__ 假 key 並在 setUp/tearDown 清理，絕不觸碰正式的 limit_up_open_notify_state
(比照 2026-07-03 借用真實 job_id 污染去重 key 害使用者收到重複 LINE 的事故)。
LINE 全部 mock，不打真實 API。

執行方式：
  python -m unittest tests.test_limit_up_open -v
"""
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server as server_module

TEST_STATE_KEY = "__test_limit_up_open_notify_state__"
TEST_LINE_BUDGET_KEY = "__test_intraday_line_budget_state__"


class DetectLimitUpOpenTests(unittest.TestCase):
    """純判斷函式，無 DB、無網路。ref=100 → 漲停≈110(touch門檻109.8/open門檻109.4)。"""

    def test_touched_and_opened_is_true(self):
        # 最高摸到漲停110、現價回落到108(+8%)：漲停打開
        self.assertTrue(server_module.detect_limit_up_open(100, 110, 108))

    def test_still_locked_is_false(self):
        # 最高110、現價還在110(仍鎖漲停)：還沒打開
        self.assertFalse(server_module.detect_limit_up_open(100, 110, 110))

    def test_never_touched_limit_is_false(self):
        # 最高只到105(+5%)、現價103：從沒摸到漲停，不算打開
        self.assertFalse(server_module.detect_limit_up_open(100, 105, 103))

    def test_just_inside_thresholds_is_true(self):
        # 最高109.85(略過touch門檻109.8)、現價109.3(略低於open門檻109.4)：清楚成立
        self.assertTrue(server_module.detect_limit_up_open(100, 109.85, 109.3))

    def test_touched_but_barely_off_is_false(self):
        # 摸到漲停但現價109.5(只離0.5%，未達open門檻109.4)：還沒明顯打開
        self.assertFalse(server_module.detect_limit_up_open(100, 110, 109.5))

    def test_invalid_prices_return_false(self):
        self.assertFalse(server_module.detect_limit_up_open(0, 110, 108))
        self.assertFalse(server_module.detect_limit_up_open(100, 0, 108))
        self.assertFalse(server_module.detect_limit_up_open(100, 110, 0))
        self.assertFalse(server_module.detect_limit_up_open(None, None, None))

    def test_high_below_current_is_dirty_data_false(self):
        # 當日最高不可能低於現價，視為髒資料
        self.assertFalse(server_module.detect_limit_up_open(100, 108, 110))


class NotifyLimitUpOpenTests(unittest.TestCase):
    def setUp(self):
        # 整併 I 後，②漲停打開也會查跨路徑 LINE 閘門，這個共用 key 也要隔離。
        self._patchers = [
            patch.object(server_module, "LIMIT_UP_OPEN_NOTIFY_STATE_KEY", TEST_STATE_KEY),
            patch.object(server_module, "INTRADAY_LINE_BUDGET_STATE_KEY", TEST_LINE_BUDGET_KEY),
        ]
        for p in self._patchers:
            p.start()
        self._cleanup()

    def tearDown(self):
        self._cleanup()
        for p in self._patchers:
            p.stop()

    def _cleanup(self):
        with server_module.backend.connect() as conn:
            conn.execute("DELETE FROM model_meta WHERE key IN (?, ?)", (TEST_STATE_KEY, TEST_LINE_BUDGET_KEY))
            conn.commit()

    def _quotes(self, opened=True):
        # ZTEST9001 漲停打開(110→108)、ZTEST9002 正常(未摸漲停)
        return {
            "ZTEST9001": {"referencePrice": 100, "highPrice": 110, "currentPrice": 108 if opened else 110},
            "ZTEST9002": {"referencePrice": 50, "highPrice": 52, "currentPrice": 51},
        }

    def _candidates(self):
        return [{"symbol": "ZTEST9001", "name": "測試妖股"}, {"symbol": "ZTEST9002", "name": "普通股"}]

    def test_fresh_open_notifies_once_and_dedupes(self):
        with patch.object(server_module, "send_line_message_via_api") as mock_send:
            first = server_module.notify_limit_up_open(self._quotes(), self._candidates())
            self.assertTrue(first["notified"])
            self.assertEqual(first["fresh"], ["ZTEST9001"])
            self.assertTrue(first["line"])
            mock_send.assert_called_once()
            # 同一檔第二輪不再重推(去重)
            second = server_module.notify_limit_up_open(self._quotes(), self._candidates())
            self.assertFalse(second["notified"])
            mock_send.assert_called_once()

    def test_no_open_does_not_notify(self):
        with patch.object(server_module, "send_line_message_via_api") as mock_send:
            result = server_module.notify_limit_up_open(self._quotes(opened=False), self._candidates())
            self.assertFalse(result["notified"])
            mock_send.assert_not_called()

    def test_daily_line_cap_marks_but_skips_send(self):
        # 先把 lineCount 推到上限，再來一檔新的漲停打開：仍標記去重但不送 LINE
        today = server_module.scheduler_today(server_module.taipei_localtime())
        server_module._write_limit_up_open_notify_state({
            "date": today, "symbols": [], "lineCount": server_module.LIMIT_UP_OPEN_NOTIFY_DAILY_LINE_CAP,
        })
        with patch.object(server_module, "send_line_message_via_api") as mock_send:
            result = server_module.notify_limit_up_open(self._quotes(), self._candidates())
        self.assertTrue(result["notified"])
        self.assertFalse(result["line"], "超過每日上限不送 LINE")
        mock_send.assert_not_called()
        # 但仍寫入去重，避免下一輪重推
        state = server_module._read_limit_up_open_notify_state(today)
        self.assertIn("ZTEST9001", state["symbols"])


if __name__ == "__main__":
    unittest.main()
