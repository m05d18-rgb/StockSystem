"""
server.py should_run_portable_daily_update()/daily_update_worker() 的回歸測試，
對應這次修的 bug：

舊版只判斷 last_daily_update_attempt_date() != today(今天有沒有嘗試過)，
只要今天第一次嘗試失敗(實際發生過：database is locked)，daily_update_
worker 整天都不會再自動重試，要等隔天 08:30 才有下一次機會。改成判斷
「今天有沒有成功過」(latest_daily_update_health().ok)，還沒成功就依
AUTO_SCHEDULE_MAX_RETRIES 上限重試，重試次數持久化在 model_meta(跟
auto_schedule_* 排程重試機制共用同一組函式)。

另外(2026-07-03稽核新增)：完整每日更新沒有任何可中斷的檢查點，且重訓
本身是CPU密集的純Python迴圈，會透過GIL拖慢所有其他請求——實測07-01卡住
的更新在開盤期間補跑，庫存/報價查詢回應時間從1-2秒拖到12-19秒、持續近
30分鐘。修法：加開盤時段(09:00-13:30)保護，該時段內完全不觸發，遞延到
收盤後再跑。

執行方式：
  python -m unittest tests.test_daily_update_retry -v
"""
import os
import sys
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server


def _time_struct(hour, minute):
    return time.strptime(f"2026-07-03 {hour:02d}:{minute:02d}:00", "%Y-%m-%d %H:%M:%S")


class ShouldRunPortableDailyUpdateTests(unittest.TestCase):
    def test_weekend_never_runs_daily_update_or_monster_scan(self):
        now = time.strptime("2026-07-11 08:35:00", "%Y-%m-%d %H:%M:%S")
        with patch("server.read_finmind_token", return_value="token"):
            self.assertFalse(server.should_run_portable_daily_update(now))

    def test_before_0830_does_not_run(self):
        now = _time_struct(8, 0)
        with patch("server.read_finmind_token", return_value="token"):
            self.assertFalse(server.should_run_portable_daily_update(now))

    def test_no_token_does_not_run(self):
        now = _time_struct(9, 0)
        with patch("server.read_finmind_token", return_value=""):
            self.assertFalse(server.should_run_portable_daily_update(now))

    def test_already_succeeded_today_does_not_run(self):
        now = _time_struct(9, 0)
        with patch("server.read_finmind_token", return_value="token"), \
             patch("server.latest_daily_update_health", return_value={"ok": True}):
            self.assertFalse(server.should_run_portable_daily_update(now))

    def test_first_attempt_today_runs_even_if_not_succeeded(self):
        # 08:35：在開盤前的寬限窗口，不受開盤時段保護影響。
        now = _time_struct(8, 35)
        with patch("server.read_finmind_token", return_value="token"), \
             patch("server.latest_daily_update_health", return_value={"ok": False}), \
             patch("server.last_daily_update_attempt_date", return_value="2026-07-02"):
            self.assertTrue(server.should_run_portable_daily_update(now))

    def test_failed_attempt_today_retries_until_max_retries(self):
        now = _time_struct(8, 35)
        with patch("server.read_finmind_token", return_value="token"), \
             patch("server.latest_daily_update_health", return_value={"ok": False}), \
             patch("server.last_daily_update_attempt_date", return_value="2026-07-03"), \
             patch("server.auto_schedule_attempt_count", return_value=1):
            # 已經失敗過 1 次(< AUTO_SCHEDULE_MAX_RETRIES=3)，還要繼續重試。
            self.assertTrue(server.should_run_portable_daily_update(now))

    def test_stops_retrying_after_max_retries_reached(self):
        now = _time_struct(8, 35)
        with patch("server.read_finmind_token", return_value="token"), \
             patch("server.latest_daily_update_health", return_value={"ok": False}), \
             patch("server.last_daily_update_attempt_date", return_value="2026-07-03"), \
             patch("server.auto_schedule_attempt_count", return_value=server.AUTO_SCHEDULE_MAX_RETRIES):
            self.assertFalse(server.should_run_portable_daily_update(now))

    def test_does_not_run_during_market_hours_even_with_pending_retries(self):
        # 對應2026-07-03實測案例：07-01卡住的每日更新在開盤期間(09:00-13:30)
        # 補跑，透過GIL拖慢庫存/報價查詢近30分鐘。開盤時段內即使還有重試
        # 名額也不該觸發，遞延到收盤後再跑。
        for hour, minute in [(9, 0), (10, 30), (13, 29)]:
            now = _time_struct(hour, minute)
            with patch("server.read_finmind_token", return_value="token"), \
                 patch("server.latest_daily_update_health", return_value={"ok": False}), \
                 patch("server.last_daily_update_attempt_date", return_value="2026-07-02"), \
                 patch("server.auto_schedule_attempt_count", return_value=0):
                self.assertFalse(
                    server.should_run_portable_daily_update(now),
                    f"不應該在 {hour:02d}:{minute:02d} 開盤時段觸發",
                )

    def test_resumes_after_market_close(self):
        now = _time_struct(13, 30)
        with patch("server.read_finmind_token", return_value="token"), \
             patch("server.latest_daily_update_health", return_value={"ok": False}), \
             patch("server.last_daily_update_attempt_date", return_value="2026-07-02"), \
             patch("server.auto_schedule_attempt_count", return_value=0):
            self.assertTrue(server.should_run_portable_daily_update(now))

    def test_still_blocked_during_market_hours_even_on_first_attempt_today(self):
        # 就算是「今天第一次嘗試」(原本永遠放行)，開盤時段的保護仍然優先。
        now = _time_struct(9, 30)
        with patch("server.read_finmind_token", return_value="token"), \
             patch("server.latest_daily_update_health", return_value={"ok": False}), \
             patch("server.last_daily_update_attempt_date", return_value="2026-07-02"):
            self.assertFalse(server.should_run_portable_daily_update(now))


if __name__ == "__main__":
    unittest.main()
