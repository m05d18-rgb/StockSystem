"""
repair_yahoo_degenerate_rows.py 的回歸測試，對應這次修的兩個高風險問題：

1. _reference_close() 在 FinMind 補值窗口全部無效(沒有任何乾淨 OHLC 列)時回傳
   None，導致 price_scale_is_plausible(candidate, None) 被 ml_backend.py 的通用
   契約(無參考值視為可接受)放行，讓尺度錯誤的 Yahoo 資料被無條件寫回 prices
   表。修法：reference_close 為 None 時直接視為不可信，該日期改走 still_bad
   (最終刪除)而不是接受寫入。

2. repair_symbol() 對 still_bad 的 DELETE 完全沒有備份機制。修法：加
   backup_rows_before_delete()，刪除前把完整列內容寫進一份 JSON Lines 備份檔。

全程用假股票代號(TEST9901)並 mock 掉 FinMind/Yahoo 網路呼叫，不會動到任何
真實股票的歷史資料；DELETE_BACKUP_PATH 在測試中被替換成暫存檔路徑，不會污染
正式的備份檔案。

執行方式：
  python -m unittest tests.test_repair_yahoo_degenerate_rows -v
"""
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import repair_yahoo_degenerate_rows as repair_module
from ml_backend import backend

TEST_SYMBOL = "TEST9901"
TEST_DATE = "2020-01-02"


class ReferenceCloseNoneFailsClosedTests(unittest.TestCase):
    def tearDown(self):
        with backend.connect() as conn:
            conn.execute("DELETE FROM prices WHERE symbol = ?", (TEST_SYMBOL,))
            conn.commit()

    def test_yahoo_row_rejected_when_no_finmind_reference_exists(self):
        # FinMind 整個修復窗口都沒有任何乾淨 OHLC 列 → sorted_finmind_dates 為空
        # → _reference_close 對任何日期都回傳 None。
        with patch.object(repair_module.backend, "fetch_finmind_dataset", return_value=[]), \
             patch.object(
                 repair_module.backend, "fetch_yahoo_fallback_price_rows",
                 return_value=[{
                     "date": TEST_DATE, "open": 999.0, "high": 999.0, "low": 999.0,
                     "close": 999.0, "volume": 100, "price_source": "Yahoo Finance fallback",
                 }],
             ), \
             patch.object(repair_module.backend, "upsert_price_rows") as mock_upsert:
            result = repair_module.repair_symbol(TEST_SYMBOL, [TEST_DATE], token="fake-token")

        self.assertEqual(result["fixedViaYahoo"], 0)
        self.assertEqual(result["deleted"], 1)
        mock_upsert.assert_not_called()

    def test_yahoo_row_accepted_when_finmind_reference_is_plausible(self):
        # 對照組：FinMind 有其他日期的乾淨參考收盤價，且 Yahoo 值尺度吻合時
        # 仍應正常接受，確認 fail-closed 修法沒有連帶擋掉正常情境。
        finmind_data = [
            {"date": "2020-01-01", "open": "100", "max": "101", "min": "99", "close": "100", "Trading_Volume": "1000"},
        ]
        with patch.object(repair_module.backend, "fetch_finmind_dataset", return_value=finmind_data), \
             patch.object(
                 repair_module.backend, "fetch_yahoo_fallback_price_rows",
                 return_value=[{
                     "date": TEST_DATE, "open": 100.5, "high": 101.0, "low": 99.5,
                     "close": 100.2, "volume": 100, "price_source": "Yahoo Finance fallback",
                 }],
             ), \
             patch.object(repair_module.backend, "upsert_price_rows") as mock_upsert:
            result = repair_module.repair_symbol(TEST_SYMBOL, [TEST_DATE], token="fake-token")

        self.assertEqual(result["fixedViaYahoo"], 1)
        self.assertEqual(result["deleted"], 0)
        mock_upsert.assert_called_once()


class BackupBeforeDeleteTests(unittest.TestCase):
    def setUp(self):
        self.backup_file = tempfile.NamedTemporaryFile(
            mode="w", suffix=".jsonl", delete=False, encoding="utf-8"
        )
        self.backup_file.close()
        os.unlink(self.backup_file.name)
        self.patcher = patch.object(repair_module, "DELETE_BACKUP_PATH", self.backup_file.name)
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()
        if os.path.exists(self.backup_file.name):
            os.unlink(self.backup_file.name)
        with backend.connect() as conn:
            conn.execute("DELETE FROM prices WHERE symbol = ?", (TEST_SYMBOL,))
            conn.commit()

    def test_delete_backs_up_row_contents_before_removing(self):
        backend.upsert_price_rows([{
            "symbol": TEST_SYMBOL, "date": TEST_DATE,
            "open": 50.0, "high": 51.0, "low": 49.0, "close": 50.5, "volume": 200,
            "price_source": "Yahoo Finance fallback",
        }])
        with patch.object(repair_module.backend, "fetch_finmind_dataset", return_value=[]), \
             patch.object(repair_module.backend, "fetch_yahoo_fallback_price_rows", return_value=[]):
            result = repair_module.repair_symbol(TEST_SYMBOL, [TEST_DATE], token="fake-token")

        self.assertEqual(result["deleted"], 1)
        with backend.connect() as conn:
            remaining = conn.execute(
                "SELECT COUNT(*) FROM prices WHERE symbol = ? AND date = ?", (TEST_SYMBOL, TEST_DATE)
            ).fetchone()[0]
        self.assertEqual(remaining, 0)

        self.assertTrue(os.path.exists(self.backup_file.name))
        with open(self.backup_file.name, "r", encoding="utf-8") as fh:
            lines = [json.loads(line) for line in fh if line.strip()]
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0]["symbol"], TEST_SYMBOL)
        self.assertEqual(lines[0]["date"], TEST_DATE)
        self.assertEqual(lines[0]["close"], 50.5)

    def test_no_backup_file_written_when_nothing_deleted(self):
        with patch.object(repair_module.backend, "fetch_finmind_dataset", return_value=[
            {"date": TEST_DATE, "open": "10", "max": "11", "min": "9", "close": "10", "Trading_Volume": "100"},
        ]), patch.object(repair_module.backend, "upsert_price_rows") as mock_upsert:
            result = repair_module.repair_symbol(TEST_SYMBOL, [TEST_DATE], token="fake-token")

        self.assertEqual(result["deleted"], 0)
        mock_upsert.assert_called_once()
        self.assertFalse(os.path.exists(self.backup_file.name))


if __name__ == "__main__":
    unittest.main()
