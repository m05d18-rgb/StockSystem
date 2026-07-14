"""
server.py line_config_status() 的回歸測試，對應這次修的問題：LINE 推播的
連續失敗計數(line_notify.read_line_failure_state)已經在 line_notify.py
裡持久化，但 line_config_status()(/api/settings/line 的資料來源)之前沒有
把它一併回傳，設定頁完全看不到「已經連續失敗過幾次」。

執行方式：
  python -m unittest tests.test_line_config_status -v
"""
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server


FAKE_CONFIG = {"channelAccessToken": "fake-token-1234567890", "targetId": "fake-target", "enabled": True}


class LineConfigStatusFailureStateTests(unittest.TestCase):
    def test_surfaces_zero_failures_by_default(self):
        with patch.object(server, "read_line_config", return_value=FAKE_CONFIG), \
             patch.object(server, "read_line_failure_state", return_value={
                 "consecutiveFailures": 0, "lastError": "", "lastFailureAt": "",
             }):
            status = server.line_config_status()
        self.assertEqual(status["consecutiveFailures"], 0)
        self.assertEqual(status["lastFailureError"], "")

    def test_surfaces_consecutive_failure_details(self):
        with patch.object(server, "read_line_config", return_value=FAKE_CONFIG), \
             patch.object(server, "read_line_failure_state", return_value={
                 "consecutiveFailures": 5,
                 "lastError": "LINE push failed: HTTP 429 rate limited",
                 "lastFailureAt": "2026-07-03 10:00:00",
             }):
            status = server.line_config_status()
        self.assertEqual(status["consecutiveFailures"], 5)
        self.assertIn("429", status["lastFailureError"])
        self.assertEqual(status["lastFailureAt"], "2026-07-03 10:00:00")


if __name__ == "__main__":
    unittest.main()
