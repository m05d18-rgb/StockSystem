"""
sinopac_backend.py 設定檔讀寫的回歸測試，對應這次修的問題：

  1. load_config() 之前完全沒有 try/except，設定檔一旦損毀(寫到一半被
     中斷/手動編輯壞掉)，json.loads 會讓 JSONDecodeError 一路傳到
     holdings()/quotes()/下單流程等呼叫點，永豐功能整個連續失效。現在
     比照 load_share_overrides() 的防禦模式，視為未設定回傳 {}。
  2. save_config() 之前直接 CONFIG_PATH.write_text(...) 覆寫，寫入過程中
     被中斷(OneDrive同步鎖檔/程式崩潰/磁碟滿)會讓檔案停在半截JSON。改成
     temp+os.replace 原子寫入，比照 daily_update.py 的 save_run_log。

全部把 CONFIG_PATH 換成暫存目錄下的假路徑，不觸碰真實 sinopac_api.json。

執行方式：
  python -m unittest tests.test_sinopac_config_atomicity -v
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sinopac_backend
from sinopac_backend import SinoPacBackend


class LoadConfigCorruptionTests(unittest.TestCase):
    def test_corrupted_json_returns_empty_dict_instead_of_raising(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            fake_path = Path(tmp_dir) / "sinopac_api.json"
            fake_path.write_text("{ this is not valid json", encoding="utf-8")
            with patch.object(sinopac_backend, "CONFIG_PATH", fake_path):
                backend = SinoPacBackend()
                result = backend.load_config()
        self.assertEqual(result, {})

    def test_missing_file_still_returns_empty_dict(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            fake_path = Path(tmp_dir) / "does_not_exist.json"
            with patch.object(sinopac_backend, "CONFIG_PATH", fake_path):
                backend = SinoPacBackend()
                result = backend.load_config()
        self.assertEqual(result, {})

    def test_valid_json_still_loads_normally(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            fake_path = Path(tmp_dir) / "sinopac_api.json"
            fake_path.write_text(json.dumps({"apiKey": "abc12345", "secretKey": "xyz12345"}), encoding="utf-8")
            with patch.object(sinopac_backend, "CONFIG_PATH", fake_path):
                backend = SinoPacBackend()
                result = backend.load_config()
        self.assertEqual(result["apiKey"], "abc12345")


class SaveConfigAtomicWriteTests(unittest.TestCase):
    def test_save_config_writes_no_leftover_temp_file(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            fake_path = Path(tmp_dir) / "sinopac_api.json"
            with patch.object(sinopac_backend, "CONFIG_PATH", fake_path), \
                 patch.object(SinoPacBackend, "status", return_value={"ok": True}):
                backend = SinoPacBackend()
                backend.save_config({"apiKey": "abcd1234", "secretKey": "efgh5678"})
            self.assertTrue(fake_path.exists())
            temp_path = fake_path.with_name(f"{fake_path.name}.tmp")
            self.assertFalse(temp_path.exists(), "atomic write 後不該留下 .tmp 暫存檔")
            saved = json.loads(fake_path.read_text(encoding="utf-8"))
            self.assertEqual(saved["apiKey"], "abcd1234")

    def test_save_config_preserves_existing_ca_fields_when_not_resubmitted(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            fake_path = Path(tmp_dir) / "sinopac_api.json"
            fake_path.write_text(json.dumps({
                "apiKey": "old-key1", "secretKey": "old-secret1",
                "caPath": "C:\\ca.pfx", "caPassword": "oldpw123", "personId": "A123456789",
            }), encoding="utf-8")
            with patch.object(sinopac_backend, "CONFIG_PATH", fake_path), \
                 patch.object(SinoPacBackend, "status", return_value={"ok": True}):
                backend = SinoPacBackend()
                backend.save_config({"apiKey": "new-key12", "secretKey": "new-secret1"})
            saved = json.loads(fake_path.read_text(encoding="utf-8"))
            self.assertEqual(saved["apiKey"], "new-key12")
            self.assertEqual(saved["caPassword"], "oldpw123")
            self.assertEqual(saved["personId"], "A123456789")


if __name__ == "__main__":
    unittest.main()
