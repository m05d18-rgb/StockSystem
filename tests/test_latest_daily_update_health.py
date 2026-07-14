"""
server.py latest_daily_update_health() 的回歸測試，對應這次修的 bug：

跟 ml_backend.py data_health() 是同一個 bug 的兩份獨立複製(server.py 這裡
是給 /api/monster-intraday 的 intraday_flow_health() 用的獨立實作)：舊版
只判斷 status != "success"，沒比對 last_daily_job_at 是不是今天。真實踩過：
last_daily_job_status='success' 但 last_daily_job_at 停在兩天前，之後每次
嘗試都失敗，這個函式仍然回傳 ok=True，讓正式模型持續對盤中妖股報價發出
「一切正常」的訊號。

用 patch.object(backend, "connect", ...) 餵假的 model_meta 列，不觸碰真實
資料庫。

執行方式：
  python -m unittest tests.test_latest_daily_update_health -v
"""
import contextlib
import datetime as dt
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server as server_module
from ml_backend import backend


TODAY = dt.date.today().strftime("%Y-%m-%d")
TWO_DAYS_AGO = (dt.date.today() - dt.timedelta(days=2)).strftime("%Y-%m-%d")


def _fake_connect(rows):
    conn = MagicMock()
    conn.execute.return_value.fetchall.return_value = rows

    @contextlib.contextmanager
    def _cm():
        yield conn

    return _cm()


class LatestDailyUpdateHealthTests(unittest.TestCase):
    def test_fresh_success_today_is_ok(self):
        rows = [("last_daily_job_status", "success"), ("last_daily_job_at", f"{TODAY} 08:55:30")]
        with patch.object(backend, "connect", return_value=_fake_connect(rows)):
            result = server_module.latest_daily_update_health()
        self.assertTrue(result["ok"])
        self.assertEqual(result["mode"], "normal")

    def test_stale_success_two_days_ago_is_not_trusted(self):
        rows = [("last_daily_job_status", "success"), ("last_daily_job_at", f"{TWO_DAYS_AGO} 08:55:30")]
        with patch.object(backend, "connect", return_value=_fake_connect(rows)):
            result = server_module.latest_daily_update_health()
        self.assertFalse(result["ok"])
        self.assertEqual(result["mode"], "observe_only")

    def test_fresh_failed_today_is_observe_only_with_error_reason(self):
        rows = [
            ("last_daily_job_status", "failed"),
            ("last_daily_job_at", f"{TODAY} 08:55:30"),
            ("last_daily_job_error", "FinMind 額度用盡"),
        ]
        with patch.object(backend, "connect", return_value=_fake_connect(rows)):
            result = server_module.latest_daily_update_health()
        self.assertFalse(result["ok"])
        self.assertIn("FinMind 額度用盡", result["reason"])

    def test_stale_quote_health_is_observe_only_even_when_prices_exist(self):
        with patch.object(server_module, "latest_daily_update_health", return_value={"ok": True}):
            result = server_module.intraday_flow_health(
                {"2330": {"currentPrice": 100}}, quote_stale=True, quote_ok=True,
            )
        self.assertFalse(result["ok"])
        self.assertEqual(result["mode"], "observe_only")
        self.assertIn("報價已過期", result["reason"])

    def test_market_daily_data_health_failure_is_observe_only(self):
        with patch.object(server_module, "latest_daily_update_health", return_value={"ok": True}):
            result = server_module.intraday_flow_health(
                {"2330": {"currentPrice": 100}},
                radar_data_health={"ok": False, "mode": "observe_only", "reason": "個股日K未通過新鮮度檢查"},
            )
        self.assertFalse(result["ok"])
        self.assertEqual(result["mode"], "observe_only")
        self.assertIn("個股日K", result["reason"])


if __name__ == "__main__":
    unittest.main()
