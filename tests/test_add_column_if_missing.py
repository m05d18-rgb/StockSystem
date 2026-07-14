"""
ml_backend.py add_column_if_missing() 的回歸測試，對應這次修的 bug：

init_db() 對 prices/brain_v2_snapshots/monster_scores/strategy_signals
四張表的欄位遷移採「PRAGMA table_info 檢查→ALTER TABLE」兩步判斷，中間
沒有互斥保護。不同 OS 行程在同一次新增欄位的部署後幾乎同時啟動時，兩邊
都會各自讀到「欄位不存在」再各自執行 ALTER TABLE，後執行的那個會拋出
sqlite3.OperationalError: duplicate column name，這個錯誤不含 "locked"，
會被 LockedConnection 的重試機制直接放行拋出，讓整個 ml_backend 模組
初始化失敗。

修法：把「欄位已存在」(不管是自己加的還是別的行程搶先加的)視為成功，
只有其他原因的 OperationalError 才真的往外拋。

用真實的記憶體 SQLite 資料庫測試(不觸碰正式資料庫)。

執行方式：
  python -m unittest tests.test_add_column_if_missing -v
"""
import os
import sqlite3
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ml_backend import add_column_if_missing


class AddColumnIfMissingTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")

    def tearDown(self):
        self.conn.close()

    def test_adds_column_when_missing(self):
        add_column_if_missing(self.conn, "t", set(), "new_col", "REAL")
        columns = {row[1] for row in self.conn.execute("PRAGMA table_info(t)").fetchall()}
        self.assertIn("new_col", columns)

    def test_skips_when_already_in_existing_columns_set(self):
        # existing_columns 是呼叫端事先查好的欄位集合，已經包含該欄位時
        # 根本不該嘗試 ALTER TABLE。
        add_column_if_missing(self.conn, "t", {"new_col"}, "new_col", "REAL")
        # 沒有真的加欄位(因為 existing_columns 說已經有了)，但也不該拋例外。

    def test_race_duplicate_column_error_is_swallowed(self):
        # 模擬「另一個行程搶先加好了，但我們查 existing_columns 時還沒看到」
        # 的競態：existing_columns 沒有這個欄位，但 ALTER TABLE 執行時
        # 已經存在(直接手動先加一次來模擬)。
        self.conn.execute("ALTER TABLE t ADD COLUMN raced_col REAL")
        try:
            add_column_if_missing(self.conn, "t", set(), "raced_col", "REAL")
        except sqlite3.OperationalError:
            self.fail("duplicate column name 這種競態不該讓 add_column_if_missing 往外拋例外")

    def test_other_operational_errors_still_raise(self):
        with self.assertRaises(sqlite3.OperationalError):
            add_column_if_missing(self.conn, "nonexistent_table", set(), "col", "REAL")


if __name__ == "__main__":
    unittest.main()
