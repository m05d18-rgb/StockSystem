"""
盤中進場訊號推播(notify_intraday_entry_triggers)的回歸測試。

功能：候選股 canBuy 當日首次 False→True 時推 LINE(每日上限2則、每檔每日
去重、stale報價跳過、額度讓位/超限降級桌面通知、送達才寫去重狀態)。

隔離鐵律：去重狀態存在正式 model_meta 表，測試一律 patch
INTRADAY_ENTRY_NOTIFY_STATE_KEY 成 __test_*__ 假前綴 key 並在 setUp/tearDown
清理，絕不觸碰正式的 intraday_entry_notify_state(2026-07-03 test_auto_schedule
借用真實 job_id 污染正式去重 key、害使用者收到重複 LINE 的同類事故)。
LINE/桌面通知全部 mock，不打真實 API。

執行方式：
  python -m unittest tests.test_intraday_entry_notify -v
"""
import json
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server as server_module
import line_notify

TEST_STATE_KEY = "__test_intraday_entry_notify_state__"
TEST_BUDGET_KEY = "__test_radar_lot_budget__"
# #169 進場推播現在也會查跨路徑 LINE 閘門(整併 I)，這個共用 state key 也要隔離，
# 否則跨路徑去重/總量會滲進來把該推的 LINE 擋掉(2026-07-04 整併時同步補上)。
TEST_LINE_BUDGET_KEY = "__test_intraday_line_budget_state__"


def _quote_state(can_buy, setup_type="breakout", current=100.0):
    return {"canBuy": can_buy, "setupType": setup_type, "currentPrice": current}


def _candidate(symbol, name="測試股", score=80, trigger=101.0, stop=93.0):
    return {"symbol": symbol, "name": name, "score": score, "buyTrigger": trigger, "stopPrice": stop}


class IntradayEntryNotifyTests(unittest.TestCase):
    def setUp(self):
        self._patchers = [
            patch.object(server_module, "INTRADAY_ENTRY_NOTIFY_STATE_KEY", TEST_STATE_KEY),
            patch.object(server_module, "RADAR_LOT_BUDGET_META_KEY", TEST_BUDGET_KEY),
            patch.object(server_module, "INTRADAY_LINE_BUDGET_STATE_KEY", TEST_LINE_BUDGET_KEY),
            # 這些測試驗的是「LINE 送不出去→退回桌面」的 fallback 機制,預設把
            # LINE_SELL_ONLY 關掉讓桌面照舊觸發;sell-only 靜音另有專門測試覆蓋。
            patch.object(line_notify, "LINE_SELL_ONLY", False),
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
            conn.execute("DELETE FROM model_meta WHERE key IN (?, ?, ?)", (TEST_STATE_KEY, TEST_BUDGET_KEY, TEST_LINE_BUDGET_KEY))
            conn.commit()

    def _set_budget(self, value):
        with server_module.backend.connect() as conn:
            server_module.backend.set_meta(conn, TEST_BUDGET_KEY, str(value))

    def _read_state(self):
        with server_module.backend.connect() as conn:
            row = conn.execute("SELECT value FROM model_meta WHERE key = ?", (TEST_STATE_KEY,)).fetchone()
        return json.loads(row[0]) if row else None

    def test_flip_to_can_buy_sends_line_and_marks_dedup(self):
        previous = {"ZTEST1": _quote_state(False)}
        current = {"ZTEST1": _quote_state(True)}
        with patch.object(server_module, "send_line_message_via_api",
                          return_value={"ok": True, "sent": True}) as mock_line:
            result = server_module.notify_intraday_entry_triggers(
                previous, current, {}, [_candidate("ZTEST1")]
            )
        self.assertEqual(result["channel"], "line")
        self.assertEqual(result["notified"], 1)
        mock_line.assert_called_once()
        message = mock_line.call_args[0][0]
        self.assertIn("ZTEST1", message)
        self.assertIn("突破", message)
        self.assertIn("觸發 101.00", message)
        self.assertIn("停損 93.00", message)
        state = self._read_state()
        self.assertIn("ZTEST1", state["symbols"])
        self.assertEqual(state["lineCount"], 1)

    def test_stale_quotes_skip_entire_round(self):
        previous = {"ZTEST1": _quote_state(False)}
        current = {"ZTEST1": _quote_state(True)}
        with patch.object(server_module, "send_line_message_via_api") as mock_line:
            result = server_module.notify_intraday_entry_triggers(
                previous, current, {"stale": True}, [_candidate("ZTEST1")]
            )
        self.assertEqual(result["skipped"], "stale_quotes")
        mock_line.assert_not_called()

    def test_no_previous_round_skips(self):
        # 伺服器剛啟動第一輪沒有上一輪資料：不通知，避免重啟風暴
        current = {"ZTEST1": _quote_state(True)}
        with patch.object(server_module, "send_line_message_via_api") as mock_line:
            result = server_module.notify_intraday_entry_triggers({}, current, {}, [_candidate("ZTEST1")])
        self.assertEqual(result["skipped"], "no_previous_round")
        mock_line.assert_not_called()

    def test_already_can_buy_previous_round_not_notified(self):
        previous = {"ZTEST1": _quote_state(True)}
        current = {"ZTEST1": _quote_state(True)}
        with patch.object(server_module, "send_line_message_via_api") as mock_line:
            result = server_module.notify_intraday_entry_triggers(
                previous, current, {}, [_candidate("ZTEST1")]
            )
        self.assertEqual(result["notified"], 0)
        mock_line.assert_not_called()

    def test_same_symbol_same_day_deduped(self):
        previous = {"ZTEST1": _quote_state(False)}
        current = {"ZTEST1": _quote_state(True)}
        with patch.object(server_module, "send_line_message_via_api",
                          return_value={"ok": True, "sent": True}) as mock_line:
            server_module.notify_intraday_entry_triggers(previous, current, {}, [_candidate("ZTEST1")])
            # 同一檔盤中抖動再翻正一次：已通知過，不再送
            result = server_module.notify_intraday_entry_triggers(previous, current, {}, [_candidate("ZTEST1")])
        self.assertEqual(result["skipped"], "already_notified")
        self.assertEqual(mock_line.call_count, 1)

    def test_line_failure_does_not_mark_dedup(self):
        # LINE 網路失敗：不寫去重狀態，下一輪 30 秒迴圈自動重試
        previous = {"ZTEST1": _quote_state(False)}
        current = {"ZTEST1": _quote_state(True)}
        with patch.object(server_module, "send_line_message_via_api",
                          side_effect=RuntimeError("LINE push failed: DNS")):
            with self.assertRaises(RuntimeError):
                server_module.notify_intraday_entry_triggers(previous, current, {}, [_candidate("ZTEST1")])
        self.assertIsNone(self._read_state(), "送出失敗不能標記已通知")

    def test_daily_line_cap_falls_back_to_desktop(self):
        with server_module.backend.connect() as conn:
            server_module.backend.set_meta(conn, TEST_STATE_KEY, json.dumps({
                "date": server_module.scheduler_today(server_module.taipei_localtime()),
                "symbols": ["ZTEST0"],
                "lineCount": server_module.INTRADAY_ENTRY_NOTIFY_DAILY_LINE_CAP,
            }))
        previous = {"ZTEST1": _quote_state(False)}
        current = {"ZTEST1": _quote_state(True)}
        with patch.object(server_module, "send_line_message_via_api") as mock_line, \
             patch.object(server_module, "send_windows_desktop_notification",
                          return_value={"ok": True, "sent": True}) as mock_desktop:
            result = server_module.notify_intraday_entry_triggers(
                previous, current, {}, [_candidate("ZTEST1")]
            )
        self.assertEqual(result["channel"], "desktop")
        mock_line.assert_not_called()
        mock_desktop.assert_called_once()
        state = self._read_state()
        self.assertIn("ZTEST1", state["symbols"])

    def test_quota_suppressed_falls_back_to_desktop_and_marks(self):
        previous = {"ZTEST1": _quote_state(False)}
        current = {"ZTEST1": _quote_state(True)}
        with patch.object(server_module, "send_line_message_via_api",
                          return_value={"ok": True, "sent": False, "suppressed": True, "reason": "額度保留"}), \
             patch.object(server_module, "send_windows_desktop_notification",
                          return_value={"ok": True, "sent": True}) as mock_desktop:
            result = server_module.notify_intraday_entry_triggers(
                previous, current, {}, [_candidate("ZTEST1")]
            )
        self.assertEqual(result["channel"], "desktop")
        mock_desktop.assert_called_once()
        self.assertIn("ZTEST1", self._read_state()["symbols"])

    def test_over_budget_symbol_not_notified_but_tracked_as_pending(self):
        # 一張預算10萬：現價200元(一張20萬)買不起→整輪只有它時不發，但要
        # 記進 pendingOverBudget 以便之後價格跌回預算內時能補推播(2026-07-04
        # 稽核修復：舊版完全不寫狀態，之後即使變便宜也永遠不會再通知，因為
        # canBuy 只翻正一次、之後不再觸發 diff)。
        self._set_budget(100000)
        previous = {"ZTEST1": _quote_state(False)}
        current = {"ZTEST1": _quote_state(True, current=200.0)}
        with patch.object(server_module, "send_line_message_via_api") as mock_line:
            result = server_module.notify_intraday_entry_triggers(
                previous, current, {}, [_candidate("ZTEST1")]
            )
        self.assertEqual(result["skipped"], "over_budget")
        self.assertEqual(result["overBudget"], 1)
        mock_line.assert_not_called()
        state = self._read_state()
        self.assertNotIn("ZTEST1", state["symbols"], "沒送達不算已通知")
        self.assertIn("ZTEST1", state["pendingOverBudget"], "要記住這檔還在等預算回落")

    def test_over_budget_candidate_notified_once_price_drops_within_budget(self):
        # 承上：股價之後真的跌回預算內、但 canBuy 從未掉回 false(沒有新的
        # 翻正事件)——舊版永遠不會再檢查這檔，新版每輪都會重新檢查
        # pendingOverBudget 裡的候選是否已經買得起。
        self._set_budget(100000)
        previous = {"ZTEST1": _quote_state(False)}
        current_expensive = {"ZTEST1": _quote_state(True, current=200.0)}
        with patch.object(server_module, "send_line_message_via_api") as mock_line:
            server_module.notify_intraday_entry_triggers(previous, current_expensive, {}, [_candidate("ZTEST1")])
            mock_line.assert_not_called()
            # canBuy 全程維持 true，只是價格緩跌到預算內；上一輪快照本身
            # canBuy 也是 true(不是翻正事件)
            current_affordable = {"ZTEST1": _quote_state(True, current=95.0)}
            mock_line.return_value = {"ok": True, "sent": True}
            result = server_module.notify_intraday_entry_triggers(
                current_expensive, current_affordable, {}, [_candidate("ZTEST1")]
            )
        self.assertEqual(result["notified"], 1, "價格跌回預算內應該補推播，即使canBuy沒有重新翻正")
        state = self._read_state()
        self.assertIn("ZTEST1", state["symbols"])
        self.assertNotIn("ZTEST1", state["pendingOverBudget"], "已經補推播過，不該還留在待補清單")

    def test_pending_over_budget_cleared_when_can_buy_drops_false(self):
        # 待補清單裡的訊號如果 canBuy 掉回 false，代表這個訊號本身已經
        # 失效，不該繼續留著等預算回落——之後重新翻正才算全新訊號。
        self._set_budget(100000)
        previous = {"ZTEST1": _quote_state(False)}
        expensive = {"ZTEST1": _quote_state(True, current=200.0)}
        with patch.object(server_module, "send_line_message_via_api") as mock_line:
            server_module.notify_intraday_entry_triggers(previous, expensive, {}, [_candidate("ZTEST1")])
            state = self._read_state()
            self.assertIn("ZTEST1", state["pendingOverBudget"])
            dropped = {"ZTEST1": _quote_state(False, current=195.0)}
            server_module.notify_intraday_entry_triggers(expensive, dropped, {}, [_candidate("ZTEST1")])
        state = self._read_state()
        self.assertNotIn("ZTEST1", state["pendingOverBudget"], "canBuy掉回false後訊號失效，待補清單要清掉")

    def test_independent_reflip_same_day_notifies_twice(self):
        # 2026-07-04 稽核修復：同一檔股票同一天 false→true→false→true 兩次
        # 獨立翻正(中間經歷過完整的false週期，不是單純抖動)，各自都是合理的
        # 進場訊號，都該推播——舊版去重用「今天出現過」整天鎖死，第二次會
        # 被誤判成已通知過而永久漏推。
        first_false = {"ZTEST1": _quote_state(False)}
        first_true = {"ZTEST1": _quote_state(True)}
        with patch.object(server_module, "send_line_message_via_api",
                          return_value={"ok": True, "sent": True}) as mock_line:
            result1 = server_module.notify_intraday_entry_triggers(
                first_false, first_true, {}, [_candidate("ZTEST1")]
            )
            # canBuy 掉回 false：這次的訊號結束
            back_to_false = {"ZTEST1": _quote_state(False)}
            server_module.notify_intraday_entry_triggers(first_true, back_to_false, {}, [_candidate("ZTEST1")])
            # 全新的一次翻正
            second_true = {"ZTEST1": _quote_state(True)}
            result2 = server_module.notify_intraday_entry_triggers(
                back_to_false, second_true, {}, [_candidate("ZTEST1")]
            )
        self.assertEqual(result1["notified"], 1)
        self.assertEqual(result2["notified"], 1, "經歷過完整false週期的第二次翻正要當成全新訊號推播")
        self.assertEqual(mock_line.call_count, 2)

    def test_budget_filters_expensive_keeps_affordable(self):
        self._set_budget(100000)
        previous = {"ZTEST1": _quote_state(False), "ZTEST2": _quote_state(False)}
        current = {
            "ZTEST1": _quote_state(True, current=88.0),   # 一張8.8萬 買得起
            "ZTEST2": _quote_state(True, current=250.0),  # 一張25萬 買不起
        }
        with patch.object(server_module, "send_line_message_via_api",
                          return_value={"ok": True, "sent": True}) as mock_line:
            result = server_module.notify_intraday_entry_triggers(
                previous, current, {}, [_candidate("ZTEST1"), _candidate("ZTEST2")]
            )
        self.assertEqual(result["notified"], 1)
        message = mock_line.call_args[0][0]
        self.assertIn("ZTEST1", message)
        self.assertNotIn("ZTEST2", message, "超出預算的不能出現在推播裡")
        state = self._read_state()
        self.assertIn("ZTEST1", state["symbols"])
        self.assertNotIn("ZTEST2", state["symbols"])

    def test_no_budget_set_notifies_everything(self):
        previous = {"ZTEST1": _quote_state(False)}
        current = {"ZTEST1": _quote_state(True, current=999.0)}
        with patch.object(server_module, "send_line_message_via_api",
                          return_value={"ok": True, "sent": True}) as mock_line:
            result = server_module.notify_intraday_entry_triggers(
                previous, current, {}, [_candidate("ZTEST1")]
            )
        self.assertEqual(result["notified"], 1)
        mock_line.assert_called_once()

    def test_multiple_flips_merged_into_one_message_top5_by_score(self):
        codes = [f"ZTEST{i}" for i in range(1, 8)]  # 7檔同輪翻正
        previous = {c: _quote_state(False) for c in codes}
        current = {c: _quote_state(True) for c in codes}
        candidates = [_candidate(c, score=i * 10) for i, c in enumerate(codes, start=1)]
        with patch.object(server_module, "send_line_message_via_api",
                          return_value={"ok": True, "sent": True}) as mock_line:
            result = server_module.notify_intraday_entry_triggers(previous, current, {}, candidates)
        self.assertEqual(result["notified"], 7)
        self.assertEqual(mock_line.call_count, 1, "同輪多檔必須合併成一則")
        message = mock_line.call_args[0][0]
        self.assertIn("另有 2 檔", message)
        self.assertIn("ZTEST7", message, "分數最高的要列在訊息裡")
        state = self._read_state()
        self.assertEqual(len(state["symbols"]), 7, "沒列進訊息的也要標已通知(已含在「另有N檔」裡)")

    def test_sell_only_mode_mutes_buy_signal_desktop(self):
        # 使用者 2026-07-07 回饋:「LINE 只發賣出」下,進場訊號 LINE 被 sell-only 擋掉後,
        # 不該再退回改用 Windows 桌面彈窗冒出來(等於從另一個門違反只賣提醒)。
        # sell-only 時 send_line 回 suppressed → desktop_notify_buy_signal 應直接靜音、不彈窗。
        previous = {"ZTEST1": _quote_state(False)}
        current = {"ZTEST1": _quote_state(True)}
        with patch.object(line_notify, "LINE_SELL_ONLY", True), \
             patch.object(server_module, "send_line_message_via_api",
                          return_value={"ok": True, "sent": False, "suppressed": True, "reason": "只發賣出"}), \
             patch.object(server_module, "send_windows_desktop_notification") as mock_desktop:
            server_module.notify_intraday_entry_triggers(
                previous, current, {}, [_candidate("ZTEST1")]
            )
        mock_desktop.assert_not_called()


if __name__ == "__main__":
    unittest.main()
