"""
即時主力動向(realtime_flow_staging)新鮮度檢查 + 清理機制的回歸測試。

對應 2026-07-04 稽核發現：collector 斷線(on_session_down)且當天重啟次數
已達上限時可能整段時間沒再啟動，realtime_flow_staging 裡當天早上的舊
資料仍然符合 WHERE date=今天，原本完全沒有新鮮度檢查，會被原封不動當成
「現在」的主力動向顯示，讓交易者誤信已經是好幾小時前的資料。另外這張表
也完全沒有清理機制，歷史日期的列會無限累積。

執行方式：
  python -m unittest tests.test_realtime_flow_freshness -v
"""
import datetime as dt
import os
import sqlite3
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server
import realtime_tick_collector as collector_module


class RealtimeFlowIsStaleTests(unittest.TestCase):
    def test_fresh_update_is_not_stale(self):
        now = dt.datetime(2026, 7, 4, 13, 0, 0)
        updated_at = (now - dt.timedelta(seconds=30)).strftime("%Y-%m-%d %H:%M:%S")
        self.assertFalse(server.realtime_flow_is_stale(updated_at, now))

    def test_update_older_than_threshold_is_stale(self):
        now = dt.datetime(2026, 7, 4, 13, 0, 0)
        updated_at = (now - dt.timedelta(seconds=200)).strftime("%Y-%m-%d %H:%M:%S")
        self.assertTrue(server.realtime_flow_is_stale(updated_at, now))

    def test_boundary_at_exactly_threshold_is_not_stale(self):
        now = dt.datetime(2026, 7, 4, 13, 0, 0)
        updated_at = (now - dt.timedelta(seconds=server.REALTIME_FLOW_STALE_SECONDS)).strftime("%Y-%m-%d %H:%M:%S")
        self.assertFalse(server.realtime_flow_is_stale(updated_at, now))

    def test_missing_timestamp_is_not_stale_but_absent(self):
        # None 代表這檔根本沒有 flow 資料(不在訂閱池)，不是「舊資料」，
        # 呼叫端本來就不會把 stale 旗標套用在完全沒有資料的情況上。
        self.assertFalse(server.realtime_flow_is_stale(None))

    def test_unparseable_timestamp_fails_safe_to_stale(self):
        # 解析失敗寧可當作不新鮮(多顯示警告)，不能讓壞掉的時間字串
        # 冒充新鮮資料。
        self.assertTrue(server.realtime_flow_is_stale("not-a-timestamp"))


class RealtimeFlowStalenessMergedIntoStateTests(unittest.TestCase):
    def test_stale_flow_row_sets_stale_flag_on_state(self):
        now = dt.datetime(2026, 7, 4, 13, 0, 0)
        stale_updated_at = (now - dt.timedelta(hours=3)).strftime("%Y-%m-%d %H:%M:%S")
        by_code = {"9999": {}}
        flow_by_code = {"9999": {"moneyFlow": 100.0, "largeOrderFlow": 5000.0, "tickCount": 12, "updatedAt": stale_updated_at}}
        for code, state in by_code.items():
            flow = flow_by_code.get(code)
            state["realtimeMoneyFlow"] = flow.get("moneyFlow") if flow else None
            state["realtimeLargeOrderFlow"] = flow.get("largeOrderFlow") if flow else None
            state["realtimeTickCount"] = flow.get("tickCount") if flow else None
            state["realtimeFlowStale"] = server.realtime_flow_is_stale(flow.get("updatedAt"), now) if flow else False
            state["realtimeFlowUpdatedAt"] = flow.get("updatedAt") if flow else None
        self.assertTrue(by_code["9999"]["realtimeFlowStale"])
        self.assertEqual(by_code["9999"]["realtimeFlowUpdatedAt"], stale_updated_at)

    def test_code_without_flow_row_is_not_marked_stale(self):
        by_code = {"8888": {}}
        flow_by_code = {}
        for code, state in by_code.items():
            flow = flow_by_code.get(code)
            state["realtimeFlowStale"] = server.realtime_flow_is_stale(flow.get("updatedAt")) if flow else False
        self.assertFalse(by_code["8888"]["realtimeFlowStale"])


def _make_memory_backend_connection():
    conn = sqlite3.connect(":memory:")
    conn.execute("""
        CREATE TABLE realtime_flow_staging (
            symbol TEXT NOT NULL,
            date TEXT NOT NULL,
            realtime_money_flow REAL,
            realtime_large_order_flow REAL,
            tick_count INTEGER NOT NULL DEFAULT 0,
            source TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (symbol, date)
        )
    """)
    return conn


class _ConnCtx:
    """模擬 backend.connect() 的 with-context 行為(commit/close)，
    包住一個共用的 in-memory 連線，不碰真正的正式資料庫。"""

    def __init__(self, conn):
        self._conn = conn

    def __enter__(self):
        return self._conn

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None:
            self._conn.commit()
        return False


class CleanupOldFlowRowsTests(unittest.TestCase):
    def setUp(self):
        self.conn = _make_memory_backend_connection()
        rows = [
            ("ZTESTFLOW1", "2026-06-01", 1.0, 1.0, 1, "test", "2026-06-01 09:00:00"),  # 遠早於保留期，該刪
            ("ZTESTFLOW2", "2026-06-19", 1.0, 1.0, 1, "test", "2026-06-19 09:00:00"),  # cutoff 前一天，該刪
            ("ZTESTFLOW3", "2026-06-20", 1.0, 1.0, 1, "test", "2026-06-20 09:00:00"),  # 剛好等於cutoff，保留(< 而非 <=)
            ("ZTESTFLOW4", "2026-07-03", 1.0, 1.0, 1, "test", "2026-07-03 09:00:00"),  # 近期，保留
        ]
        self.conn.executemany(
            "INSERT INTO realtime_flow_staging VALUES (?, ?, ?, ?, ?, ?, ?)", rows
        )
        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    def test_rows_older_than_retention_are_deleted_recent_rows_kept(self):
        with patch.object(collector_module, "backend") as mock_backend, \
             patch.object(collector_module, "taipei_now", return_value=dt.datetime(2026, 7, 4, 8, 0, 0, tzinfo=collector_module.TAIPEI_TZ)):
            mock_backend.connect.return_value = _ConnCtx(self.conn)
            deleted = collector_module.cleanup_old_flow_rows(retention_days=14)
        self.assertEqual(deleted, 2, "cutoff=2026-06-20，早於這天的2筆該被刪掉")
        remaining = {row[0] for row in self.conn.execute("SELECT symbol FROM realtime_flow_staging").fetchall()}
        self.assertEqual(remaining, {"ZTESTFLOW3", "ZTESTFLOW4"})

    def test_cleanup_failure_does_not_raise(self):
        with patch.object(collector_module, "backend") as mock_backend:
            mock_backend.connect.side_effect = RuntimeError("db locked")
            deleted = collector_module.cleanup_old_flow_rows(retention_days=14)
        self.assertEqual(deleted, 0, "清理失敗要吞掉例外、回傳0，不能讓開機流程中止")


if __name__ == "__main__":
    unittest.main()
