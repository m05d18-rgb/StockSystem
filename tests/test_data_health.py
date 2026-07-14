"""
ModelBackend.data_health() 的回歸測試：正式股票分析只看資料更新狀態，
獨立模型的健康檢查成功、失敗、過期或未執行都不能封鎖雷達與持股提醒。

執行方式：
  python -m unittest tests.test_data_health -v
"""
import datetime as dt
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ml_backend import backend


TODAY = dt.date.today().isoformat()
YESTERDAY = (dt.date.today() - dt.timedelta(days=1)).isoformat()


def _meta(**overrides):
    base = {
        "last_system_health_status": "success",
        "last_system_health_at": f"{TODAY} 09:00:00",
        "last_system_health_error": "",
        "last_daily_job_status": "success",
        "last_daily_job_at": f"{TODAY} 08:00:00",
        "last_daily_job_error": "",
        "last_data_update": f"{TODAY} 08:50:00",
    }
    base.update(overrides)
    return base


class ModelHealthIsolationTests(unittest.TestCase):
    def test_fresh_success_today_passes_through_to_daily_job_check(self):
        result = backend.data_health(_meta())
        self.assertTrue(result["ok"])
        self.assertEqual(result["mode"], "normal")

    def test_fresh_model_failure_does_not_block_fresh_market_data(self):
        meta = _meta(last_system_health_status="failed", last_system_health_error="model.pkl 損壞")
        result = backend.data_health(meta)
        self.assertTrue(result["ok"])
        self.assertEqual(result["mode"], "normal")
        self.assertNotIn("model.pkl", result["reason"])

    def test_stale_model_health_does_not_block_fresh_market_data(self):
        meta = _meta(last_system_health_at=f"{YESTERDAY} 09:00:00")
        result = backend.data_health(meta)
        self.assertTrue(result["ok"])
        self.assertEqual(result["mode"], "normal")

    def test_stale_model_failure_does_not_block_fresh_market_data(self):
        meta = _meta(
            last_system_health_status="failed",
            last_system_health_at=f"{YESTERDAY} 09:00:00",
            last_system_health_error="舊的暫時性錯誤",
        )
        result = backend.data_health(meta)
        self.assertTrue(result["ok"])
        self.assertEqual(result["mode"], "normal")
        self.assertNotIn("舊的暫時性錯誤", result["reason"])

    def test_model_never_checked_does_not_block_fresh_market_data(self):
        meta = _meta(last_system_health_status="", last_system_health_at="")
        result = backend.data_health(meta)
        self.assertTrue(result["ok"])
        self.assertEqual(result["mode"], "normal")


class DailyJobAndDataFreshnessTests(unittest.TestCase):
    # 2026-07-08：data_health 改看「今日價格資料是否新鮮(last_data_update)」,
    # 不再要求「完整每日更新(舊版含重訓)今天成功」。重訓已是純參考、移到收盤後
    # 14:30,盤中賣出/觀察決策只需要資料新鮮;完整每日 job 沒在窗口跑到(排程時機/
    # 重啟)不該把盤中決策鎖進 observe_only 一整天。

    def test_fresh_daily_success_today_passes(self):
        result = backend.data_health(_meta())
        self.assertTrue(result["ok"])
        self.assertEqual(result["mode"], "normal")

    def test_stale_daily_job_but_fresh_data_is_ok(self):
        # 完整每日更新停在兩天前,但今天有更新過價格資料(盤中即時/缺口修復)
        # → 資料新鮮,不鎖 observe_only(這正是本次修的核心痛點)。
        two_days_ago = (dt.date.today() - dt.timedelta(days=2)).isoformat()
        meta = _meta(
            last_daily_job_at=f"{two_days_ago} 08:55:30",
            last_data_update=f"{TODAY} 09:20:00",
        )
        result = backend.data_health(meta)
        self.assertTrue(result["ok"])
        self.assertEqual(result["mode"], "normal")

    def test_stale_daily_and_stale_data_blocks(self):
        # 完整每日更新沒跑、今天也沒更新過任何價格資料 → 資料真的過期,observe_only。
        two_days_ago = (dt.date.today() - dt.timedelta(days=2)).isoformat()
        meta = _meta(
            last_daily_job_at=f"{two_days_ago} 08:55:30",
            last_data_update=f"{YESTERDAY} 13:30:00",
        )
        result = backend.data_health(meta)
        self.assertFalse(result["ok"])
        self.assertEqual(result["mode"], "observe_only")
        self.assertIn("價格資料", result["reason"])

    def test_daily_failed_today_blocks_even_if_data_touched(self):
        # 每日更新「今天」明確失敗 → 真的抓資料出問題,即使 last_data_update 是今天
        # 也示警 observe_only(保留原本的今日失敗偵測)。
        meta = _meta(
            last_daily_job_status="failed",
            last_daily_job_at=f"{TODAY} 08:40:00",
            last_daily_job_error="database is locked",
            last_data_update=f"{TODAY} 08:50:00",
        )
        result = backend.data_health(meta)
        self.assertFalse(result["ok"])
        self.assertEqual(result["mode"], "observe_only")
        self.assertIn("database is locked", result["reason"])

    def test_stale_daily_failed_yesterday_but_fresh_data_is_ok(self):
        # 昨天失敗的每日更新不該被永久相信;今天資料若新鮮 → ok(失敗偵測只認今天)。
        meta = _meta(
            last_daily_job_status="failed",
            last_daily_job_at=f"{YESTERDAY} 08:55:30",
            last_daily_job_error="舊的暫時性錯誤",
            last_data_update=f"{TODAY} 09:00:00",
        )
        result = backend.data_health(meta)
        self.assertTrue(result["ok"])


if __name__ == "__main__":
    unittest.main()
