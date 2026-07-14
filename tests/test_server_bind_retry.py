"""
server.py bind_server_with_retry() 的回歸測試，對應這次修的問題：

原本 __main__ 區塊直接裸呼叫 ThreadingHTTPServer(...)，完全沒有 try/except。
在雙啟動路徑(start_server.bat / launch_stock_system.ps1)各自獨立判斷「要不要
啟動」的 TOCTOU 競態窗口下，若兩邊剛好都判定要啟動、其中一個搶先 bind 成功，
稍後真正想啟動的那個會因 port 已被占用丟出 OSError(WinError 10048)，整個
process 直接以未處理例外崩潰退出，且沒有任何重試——使用者以為系統啟動了，
實際上完全沒有伺服器在跑。

修法：抽成 bind_server_with_retry()，bind 失敗時重試數次(間隔 delay_seconds)，
只有連續失敗到達上限才真的往外拋例外。

執行方式：
  python -m unittest tests.test_server_bind_retry -v
"""
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server as server_module


class BindServerWithRetryTests(unittest.TestCase):
    def test_returns_server_immediately_on_first_success(self):
        sentinel = object()
        with patch.object(server_module, "ThreadingHTTPServer", return_value=sentinel) as mock_ctor, \
             patch.object(server_module.time, "sleep") as mock_sleep:
            result = server_module.bind_server_with_retry(8008, "handler")
        self.assertIs(result, sentinel)
        mock_ctor.assert_called_once_with(("127.0.0.1", 8008), "handler")
        mock_sleep.assert_not_called()

    def test_binds_loopback_only_for_cloudflare_origin(self):
        sentinel = object()
        with patch.object(server_module, "ThreadingHTTPServer", return_value=sentinel) as mock_ctor:
            server_module.bind_server_with_retry(8008, "handler")
        bind_address = mock_ctor.call_args.args[0]
        self.assertEqual(bind_address, ("127.0.0.1", 8008))
        self.assertNotEqual(bind_address[0], "0.0.0.0")

    def test_retries_after_transient_bind_failure_then_succeeds(self):
        sentinel = object()
        with patch.object(
            server_module, "ThreadingHTTPServer",
            side_effect=[OSError("port in use"), OSError("port in use"), sentinel],
        ) as mock_ctor, patch.object(server_module.time, "sleep") as mock_sleep:
            result = server_module.bind_server_with_retry(8008, "handler", attempts=5, delay_seconds=1)
        self.assertIs(result, sentinel)
        self.assertEqual(mock_ctor.call_count, 3)
        self.assertEqual(mock_sleep.call_count, 2)

    def test_raises_after_exhausting_all_attempts(self):
        with patch.object(server_module, "ThreadingHTTPServer", side_effect=OSError("port in use")) as mock_ctor, \
             patch.object(server_module.time, "sleep") as mock_sleep:
            with self.assertRaises(OSError):
                server_module.bind_server_with_retry(8008, "handler", attempts=3, delay_seconds=1)
        self.assertEqual(mock_ctor.call_count, 3)
        self.assertEqual(mock_sleep.call_count, 2)


if __name__ == "__main__":
    unittest.main()
