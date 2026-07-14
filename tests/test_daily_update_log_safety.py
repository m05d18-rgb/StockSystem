"""
daily_update.py save_run_log() 相關的回歸測試，對應這次修的三個問題：

1. save_run_log() 用 Path.write_text() 直接覆寫 latest.json，CPython 內部是
   open(mode='w') 先截斷再寫入，讀取端可能讀到空檔/半截 JSON。改成寫暫存檔
   +os.replace() 的原子寫入。
2. run() 的 finally 區塊呼叫 save_run_log() 本身沒有 try/except 保護，拋例外
   會讓後面的 set_daily_meta() 完全沒機會執行。改成包住這個呼叫，記錄
   logWriteError 但讓 set_daily_meta 依然執行。
3. daily_update_logs/ 目錄沒有任何清理機制，永久增長。加上依檔名排序只保留
   最近 N 筆的 _prune_old_run_logs()。

全程用暫時替換 LOG_DIR 到 tmp 目錄的方式隔離，不會動到正式的
daily_update_logs/ 目錄或裡面的真實歷史紀錄。

執行方式：
  python -m unittest tests.test_daily_update_log_safety -v
"""
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import daily_update as daily_update_module
from daily_update import run, save_run_log


class SaveRunLogAtomicWriteTests(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = Path(tempfile.mkdtemp(prefix="daily_update_logs_test_"))
        self.patcher = patch.object(daily_update_module, "LOG_DIR", self.tmp_dir)
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_writes_timestamped_file_and_latest_json(self):
        path = save_run_log({"ok": True, "symbols": ["2330"]})
        self.assertTrue(path.exists())
        self.assertTrue((self.tmp_dir / "latest.json").exists())
        with open(self.tmp_dir / "latest.json", "r", encoding="utf-8") as fh:
            content = json.load(fh)
        self.assertEqual(content["symbols"], ["2330"])

    def test_no_leftover_tmp_file_after_write(self):
        save_run_log({"ok": True, "symbols": []})
        tmp_leftovers = list(self.tmp_dir.glob("*.tmp"))
        self.assertEqual(tmp_leftovers, [])

    def test_latest_json_is_never_left_truncated_even_if_replace_is_interrupted(self):
        # 模擬 os.replace 之前的寫入過程本身沒問題，但驗證用的是 temp+replace
        # 語意：只要 os.replace 沒被呼叫，latest.json 應該維持完全不存在或
        # 完全是舊內容，不會出現半截檔案。
        save_run_log({"ok": True, "symbols": ["2330"], "round": 1})
        original_replace = os.replace
        call_count = {"n": 0}

        def failing_replace(src, dst):
            call_count["n"] += 1
            if str(dst).endswith("latest.json"):
                raise OSError("simulated onedrive lock")
            return original_replace(src, dst)

        with patch.object(daily_update_module.os, "replace", side_effect=failing_replace):
            with self.assertRaises(OSError):
                save_run_log({"ok": True, "symbols": ["2330"], "round": 2})

        with open(self.tmp_dir / "latest.json", "r", encoding="utf-8") as fh:
            content = json.load(fh)
        self.assertEqual(content["round"], 1)


class PruneOldRunLogsTests(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = Path(tempfile.mkdtemp(prefix="daily_update_logs_test_"))
        self.patcher = patch.object(daily_update_module, "LOG_DIR", self.tmp_dir)
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_keeps_only_the_most_recent_n_files(self):
        for i in range(5):
            stamp = f"2026070{i}_120000"
            (self.tmp_dir / f"daily_update_{stamp}.json").write_text("{}", encoding="utf-8")
        daily_update_module._prune_old_run_logs(keep=3)
        remaining = sorted(p.name for p in self.tmp_dir.glob("daily_update_*.json"))
        self.assertEqual(len(remaining), 3)
        self.assertEqual(remaining, [
            "daily_update_20260702_120000.json",
            "daily_update_20260703_120000.json",
            "daily_update_20260704_120000.json",
        ])

    def test_does_not_touch_latest_json(self):
        (self.tmp_dir / "latest.json").write_text('{"marker": true}', encoding="utf-8")
        for i in range(10):
            (self.tmp_dir / f"daily_update_2026070{i}_120000.json").write_text("{}", encoding="utf-8")
        daily_update_module._prune_old_run_logs(keep=2)
        self.assertTrue((self.tmp_dir / "latest.json").exists())


class SaveRunLogExceptionDoesNotBlockMetaWriteTests(unittest.TestCase):
    def test_save_run_log_exception_still_lets_set_daily_meta_run(self):
        fake_result = {
            "ok": True, "updatedRows": {}, "priceFetchErrors": {}, "market": {},
            "model": {}, "trainingSymbols": [], "trainingSymbolCount": 0,
            "portfolioSymbols": [], "portfolioSymbolCount": 0,
            "predictions": [], "predictionErrors": [], "updatedOutcomes": {},
        }
        with patch.object(daily_update_module, "load_sinopac_symbols", side_effect=RuntimeError("no sinopac")), \
             patch.object(daily_update_module.backend, "full_daily_update", return_value=fake_result), \
             patch.object(daily_update_module, "backup_database", return_value={"ok": True, "skipped": True}), \
             patch.object(daily_update_module, "save_daily_brain_v2_snapshots", return_value={"saved": 0, "total": 0, "errors": []}), \
             patch.object(daily_update_module, "run_daily_data_integrity_check", return_value={"ok": True, "notified": False}), \
             patch.object(daily_update_module, "save_run_log", side_effect=OSError("disk full")), \
             patch.object(daily_update_module, "set_daily_meta") as mock_set_meta:
            result = run()

        mock_set_meta.assert_called_once()
        self.assertEqual(result.get("logWriteError"), "disk full")
        called_status, called_payload, called_log_path = mock_set_meta.call_args[0]
        self.assertEqual(called_log_path, "")


if __name__ == "__main__":
    unittest.main()
