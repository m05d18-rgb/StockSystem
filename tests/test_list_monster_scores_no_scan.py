"""
ml_backend.py list_monster_scores() 的回歸測試，對應這次修的問題：

當 monster_scores 表當天還沒有掃描紀錄(scan_date 為 NULL，例如新部署/DB
剛重建/連續 database is locked 導致每日更新從未跑完一輪)時，舊版在還沒
離開 `with self.connect() as conn:` 區塊前就呼叫 self.listed_symbols()，
這個函式在 stock_info 表為空時會同步觸發 update_stock_info() 打 FinMind
API(urlopen timeout=35秒)。SQLite 連線的交易(即使只是唯讀 SELECT)在這段
網路 I/O 期間持續開著，放大 database is locked 的發生機率。

修法：把 listed_symbols()/liquid_monster_universe() 的呼叫搬到 with 區塊
外面，確保連線的交易已經 commit(sqlite3.Connection 當 context manager
使用時，__exit__ 做的是 commit/rollback，不是真的關閉連線，但重點是
不再讓這段網路 I/O 跟一個尚未結束的資料庫交易綁在一起)。

用一個會記錄「有沒有先呼叫過 __exit__」的假連線包裝器測試呼叫順序，
不觸碰正式資料庫、不打真實 FinMind API。

執行方式：
  python -m unittest tests.test_list_monster_scores_no_scan -v
"""
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ml_backend import backend


class _FakeCursor:
    def __init__(self, rows):
        self._rows = rows

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return self._rows


class _FakeConnectionNoScanDate:
    """模擬 monster_scores 表為空(scan_date=NULL)、model_meta 表也是空的情境。"""

    def __init__(self, call_log):
        self.call_log = call_log
        self.exited = False
        self.row_factory = None

    def execute(self, sql, params=()):
        sql_upper = sql.strip().upper()
        if "MAX(SCAN_DATE)" in sql_upper:
            return _FakeCursor([(None,)])
        if "FROM MODEL_META" in sql_upper:
            return _FakeCursor([])
        raise AssertionError(f"未預期在 scan_date 為 None 時還執行了主查詢: {sql}")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.exited = True
        self.call_log.append("conn_exit")
        return False


class ListMonsterScoresNoScanDateTests(unittest.TestCase):
    def test_listed_symbols_called_after_connection_context_exits(self):
        call_log = []
        fake_conn = _FakeConnectionNoScanDate(call_log)

        def fake_listed_symbols():
            call_log.append("listed_symbols")
            self.assertTrue(fake_conn.exited, "listed_symbols() 應該在連線的 with 區塊結束後才呼叫")
            return ["2330", "2317"]

        def fake_liquid_universe():
            call_log.append("liquid_universe")
            self.assertTrue(fake_conn.exited, "liquid_monster_universe() 應該在連線的 with 區塊結束後才呼叫")
            return ["2330"]

        with patch.object(backend, "connect", return_value=fake_conn), \
             patch.object(backend, "listed_symbols", side_effect=fake_listed_symbols), \
             patch.object(backend, "liquid_monster_universe", side_effect=fake_liquid_universe), \
             patch.object(backend, "buy_signal_threshold", return_value=0.45):
            result = backend.list_monster_scores(limit=80)

        self.assertEqual(call_log, ["conn_exit", "listed_symbols", "liquid_universe"])
        self.assertTrue(result["ok"])
        self.assertIsNone(result["scanDate"])
        self.assertEqual(result["universeTotal"], 2)
        self.assertEqual(result["liquidUniverse"], 1)
        self.assertEqual(result["candidates"], [])

    def test_real_backend_no_scan_date_shape_is_unaffected(self):
        # 對照真實 backend(有正式掃描資料)確認回傳結構沒有被這次重構動到，
        # 只是搬動了呼叫時機。
        result = backend.list_monster_scores(limit=5)
        self.assertIn("ok", result)
        self.assertIn("scanDate", result)
        self.assertIn("candidates", result)


if __name__ == "__main__":
    unittest.main()
