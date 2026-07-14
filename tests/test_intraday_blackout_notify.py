"""
①-a 盤中即時報價失明告警(notify_intraday_quote_blackout)的回歸測試。

功能:永豐盤中報價端中斷(daily token/CA 憑證過期最常見)導致 quotes 清空、
health 轉 observe_only 時,在盤中確認窗(09:30-13:15)發一則 critical LINE,
每日去重一次,避免每 30 秒洗版。純告警,不碰任何買賣閘門/評分/門檻。

隔離鐵律:去重狀態存在正式 model_meta 表,測試一律 patch
INTRADAY_BLACKOUT_NOTIFY_STATE_KEY 成 __test_*__ 假前綴 key 並在 setUp/tearDown
清理,絕不觸碰正式的 intraday_blackout_notify_state(2026-07-03 test_auto_schedule
借用真實 job_id 污染正式去重 key、害使用者收到重複 LINE 的同類事故)。
LINE API 全部 mock,不打真實通知。

執行方式:
  python -m unittest tests.test_intraday_blackout_notify -v
"""
import json
import os
import sys
import unittest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server as server_module

TEST_STATE_KEY = "__test_intraday_blackout_notify_state__"
TEST_DATE = "2026-07-07"


def _blackout_status(error="Sinopac API Key / Secret Key is not configured", quote_count=0, daily_ok=True):
    """模擬 except 路徑(永豐 raise)或整批空報價下 update_monster_intraday_quotes
    產生的 status:health.ok=False、報價端失明。"""
    return {
        "error": error,
        "health": {
            "ok": False,
            "mode": "observe_only",
            "reason": f"盤中報價更新失敗:{error}" if error else "盤中報價尚未取得,暫停買進/賣出決策",
            "daily": {"ok": daily_ok},
            "quoteCount": quote_count,
        },
    }


class IntradayBlackoutNotifyTests(unittest.TestCase):
    def setUp(self):
        self._patchers = [
            patch.object(server_module, "INTRADAY_BLACKOUT_NOTIFY_STATE_KEY", TEST_STATE_KEY),
            patch.object(server_module, "monster_buy_confirm_window", return_value=True),
            patch.object(server_module, "scheduler_today", return_value=TEST_DATE),
        ]
        for p in self._patchers:
            p.start()
        self._delete_test_key()

    def tearDown(self):
        self._delete_test_key()
        for p in self._patchers:
            p.stop()

    def _delete_test_key(self):
        with server_module.backend.connect() as conn:
            conn.execute("DELETE FROM model_meta WHERE key = ?", (TEST_STATE_KEY,))
            conn.commit()

    def _read_state(self):
        with server_module.backend.connect() as conn:
            row = conn.execute("SELECT value FROM model_meta WHERE key = ?", (TEST_STATE_KEY,)).fetchone()
        return json.loads(row[0]) if row else None

    def test_blackout_in_window_sends_once_and_dedups(self):
        send = MagicMock(return_value={"sent": True})
        with patch.object(server_module, "send_line_message_via_api", send):
            first = server_module.notify_intraday_quote_blackout(_blackout_status())
            second = server_module.notify_intraday_quote_blackout(_blackout_status())
        self.assertEqual(first.get("notified"), 1)
        self.assertEqual(send.call_count, 1, "第二輪應被每日去重,不重發")
        self.assertEqual(second.get("skipped"), "already_notified")
        state = self._read_state()
        self.assertTrue(state and state.get("notified") is True)
        # 訊息是 critical 級(不受 LINE_SELL_ONLY 抑制)且點名永豐重登
        _, kwargs = send.call_args
        self.assertEqual(kwargs.get("priority"), "critical")
        self.assertIn("永豐", send.call_args[0][0])

    def test_daily_not_ready_is_not_blackout(self):
        # 每日資料未就緒(晨間 observe-only)是另一條已知路徑,不在這裡告警。
        send = MagicMock(return_value={"sent": True})
        with patch.object(server_module, "send_line_message_via_api", send):
            result = server_module.notify_intraday_quote_blackout(
                _blackout_status(error="", quote_count=0, daily_ok=False)
            )
        self.assertEqual(result.get("skipped"), "daily_not_ready")
        send.assert_not_called()

    def test_outside_confirm_window_skips(self):
        send = MagicMock(return_value={"sent": True})
        with patch.object(server_module, "monster_buy_confirm_window", return_value=False), \
             patch.object(server_module, "send_line_message_via_api", send):
            result = server_module.notify_intraday_quote_blackout(_blackout_status())
        self.assertEqual(result.get("skipped"), "outside_confirm_window")
        send.assert_not_called()

    def test_healthy_status_skips(self):
        send = MagicMock(return_value={"sent": True})
        healthy = {"error": "", "health": {"ok": True, "quoteCount": 50, "daily": {"ok": True}}}
        with patch.object(server_module, "send_line_message_via_api", send):
            result = server_module.notify_intraday_quote_blackout(healthy)
        self.assertEqual(result.get("skipped"), "healthy")
        send.assert_not_called()

    def test_line_network_failure_not_marked_retries_next_round(self):
        # LINE 真的網路失敗(既非 sent 也非 suppressed/disabled)→ 不標記已告警,
        # 下一輪自動重試(比照 notify_intraday_entry_triggers 的送達才落地語意)。
        send = MagicMock(return_value={"sent": False})
        with patch.object(server_module, "send_line_message_via_api", send):
            first = server_module.notify_intraday_quote_blackout(_blackout_status())
            self.assertIsNone(self._read_state(), "網路失敗不該寫入已告警狀態")
            second = server_module.notify_intraday_quote_blackout(_blackout_status())
        self.assertEqual(first.get("notified"), 0)
        self.assertEqual(send.call_count, 2, "第一輪失敗,第二輪應重試再送一次")

    def test_suppressed_counts_as_notified(self):
        # 月底額度保留池讓位(suppressed)也算今日已告警,不每 30 秒重試洗版。
        send = MagicMock(return_value={"sent": False, "suppressed": True})
        with patch.object(server_module, "send_line_message_via_api", send):
            server_module.notify_intraday_quote_blackout(_blackout_status())
            server_module.notify_intraday_quote_blackout(_blackout_status())
        self.assertEqual(send.call_count, 1, "suppressed 也要每日去重")
        state = self._read_state()
        self.assertTrue(state and state.get("notified") is True)

    def test_empty_quotes_without_error_is_blackout(self):
        # 登入成功卻整批空報價(quoteCount<=0、無 exception)的成功路徑子情況,
        # 同樣算失明要告警。
        send = MagicMock(return_value={"sent": True})
        with patch.object(server_module, "send_line_message_via_api", send):
            result = server_module.notify_intraday_quote_blackout(
                _blackout_status(error="", quote_count=0, daily_ok=True)
            )
        self.assertEqual(result.get("notified"), 1)
        send.assert_called_once()


if __name__ == "__main__":
    unittest.main()
