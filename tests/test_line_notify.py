"""
line_notify.py send_line_message() 的回歸測試，對應這次修的 bug：

只接住 HTTPError 並包成清楚的「LINE push failed: HTTP {code} ...」訊息，
DNS 失敗/連線逾時/connection reset 這類網路層例外(URLError/socket.timeout)
會用 urllib 原始的 `<urlopen error ...>` 字串往外拋，沒有「LINE push
failed」這個好搜尋的前綴，容易在排查 log 時被忽略、誤以為 LINE 沒出過問題。

全部用 patch.object(line_notify, "urlopen", ...) 餵假的網路例外，不會真的
打 LINE API。

執行方式：
  python -m unittest tests.test_line_notify -v
"""
import os
import socket
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError, URLError

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import line_notify


FAKE_CONFIG = {"channelAccessToken": "fake-token-1234567890", "targetId": "fake-target", "enabled": True}


class _IsolatedLineStateTestCase(unittest.TestCase):
    """把 LINE_FAILURE_STATE_PATH 跟 LINE_QUOTA_PATH 都換成暫存路徑的共用
    基底。之前 SendLineMessageErrorWrappingTests 完全沒有 patch 狀態檔路徑，
    它的失敗路徑測試每跑一次就把假錯誤(例如假的getaddrinfo failed)寫進
    **正式的** line_notify_failure_state.json——2026-07-03 實際造成過誤導：
    正式狀態檔裡出現一筆 19:17:50 的「DNS解析失敗」，其實是當時跑全套
    測試時這裡的假錯誤污染進去的，被誤判成真實網路事故。跟同日
    test_auto_schedule.py 借用真實 job_id 是同一類「測試污染跨行程共用
    狀態」問題，所有會走到 send_line_message/_record_line_result 的測試
    一律要繼承這個基底隔離狀態檔。"""

    def setUp(self):
        self.tmp_dir = Path(tempfile.mkdtemp(prefix="line_state_test_"))
        self._patchers = [
            patch.object(line_notify, "LINE_FAILURE_STATE_PATH", self.tmp_dir / "failure_state.json"),
            patch.object(line_notify, "LINE_QUOTA_PATH", self.tmp_dir / "quota.json"),
            # 這些測試驗證的是底層送出/額度/錯誤包裝機制,與「只發賣出」這個上層呈現閘門
            # 無關;預設把 LINE_SELL_ONLY 關掉,讓 normal 級通知照原本邏輯走到 urlopen。
            # 只發賣出閘門本身由 LineSellOnlyTests 專門(重新 patch 成 True)驗證。
            patch.object(line_notify, "LINE_SELL_ONLY", False),
        ]
        for p in self._patchers:
            p.start()

    def tearDown(self):
        for p in self._patchers:
            p.stop()
        for leftover in self.tmp_dir.glob("*"):
            leftover.unlink()
        self.tmp_dir.rmdir()


class SendLineMessageErrorWrappingTests(_IsolatedLineStateTestCase):
    def test_http_error_wrapped_with_status_code(self):
        exc = HTTPError("https://api.line.me", 401, "Unauthorized", {}, None)
        exc.read = lambda: b'{"message":"invalid token"}'
        with patch.object(line_notify, "read_line_config", return_value=FAKE_CONFIG), \
             patch.object(line_notify, "urlopen", side_effect=exc):
            with self.assertRaises(RuntimeError) as ctx:
                line_notify.send_line_message("test")
        self.assertIn("LINE push failed: HTTP 401", str(ctx.exception))

    def test_url_error_is_also_wrapped_with_line_push_failed_prefix(self):
        # 對應這次修的 bug：DNS 失敗這類 URLError 之前完全不會被包裝，
        # 訊息裡沒有「LINE push failed」，用關鍵字搜尋 log 會漏掉。
        with patch.object(line_notify, "read_line_config", return_value=FAKE_CONFIG), \
             patch.object(line_notify, "urlopen", side_effect=URLError("[Errno 11001] getaddrinfo failed")):
            with self.assertRaises(RuntimeError) as ctx:
                line_notify.send_line_message("test")
        self.assertIn("LINE push failed", str(ctx.exception))

    def test_socket_timeout_is_also_wrapped(self):
        with patch.object(line_notify, "read_line_config", return_value=FAKE_CONFIG), \
             patch.object(line_notify, "urlopen", side_effect=socket.timeout("timed out")):
            with self.assertRaises(RuntimeError) as ctx:
                line_notify.send_line_message("test")
        self.assertIn("LINE push failed", str(ctx.exception))

    def test_success_path_still_returns_sent_true(self):
        class _FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return b""

        with patch.object(line_notify, "read_line_config", return_value=FAKE_CONFIG), \
             patch.object(line_notify, "urlopen", return_value=_FakeResponse()):
            result = line_notify.send_line_message("test")
        self.assertTrue(result["ok"])
        self.assertTrue(result["sent"])


class LineConsecutiveFailureStateTests(_IsolatedLineStateTestCase):
    """對應這次修的問題：send_line_message() 之前是完全無狀態的函式，連續
    失敗多輪(網路不穩/LINE API限流)完全沒有跨呼叫記憶，使用者不在電腦前時
    不會知道通知其實一直送不出去。改成把連續失敗次數/最後錯誤持久化到
    一個小 JSON 檔案，跨呼叫、跨行程都能累積，供設定頁狀態顯示使用。"""

    def test_no_state_file_returns_zero_failures(self):
        state = line_notify.read_line_failure_state()
        self.assertEqual(state["consecutiveFailures"], 0)
        self.assertEqual(state["lastError"], "")

    def test_failures_accumulate_across_calls(self):
        with patch.object(line_notify, "read_line_config", return_value=FAKE_CONFIG), \
             patch.object(line_notify, "urlopen", side_effect=URLError("network down")):
            for _ in range(3):
                with self.assertRaises(RuntimeError):
                    line_notify.send_line_message("test")
        state = line_notify.read_line_failure_state()
        self.assertEqual(state["consecutiveFailures"], 3)
        self.assertIn("LINE push failed", state["lastError"])
        self.assertTrue(state["lastFailureAt"])

    def test_success_resets_consecutive_failure_count(self):
        with patch.object(line_notify, "read_line_config", return_value=FAKE_CONFIG), \
             patch.object(line_notify, "urlopen", side_effect=URLError("network down")):
            with self.assertRaises(RuntimeError):
                line_notify.send_line_message("test")
        self.assertEqual(line_notify.read_line_failure_state()["consecutiveFailures"], 1)

        class _FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return b""

        with patch.object(line_notify, "read_line_config", return_value=FAKE_CONFIG), \
             patch.object(line_notify, "urlopen", return_value=_FakeResponse()):
            line_notify.send_line_message("test")
        state = line_notify.read_line_failure_state()
        self.assertEqual(state["consecutiveFailures"], 0)
        self.assertEqual(state["lastError"], "")

    def test_http_error_is_also_recorded(self):
        exc = HTTPError("https://api.line.me", 401, "Unauthorized", {}, None)
        exc.read = lambda: b'{"message":"invalid token"}'
        with patch.object(line_notify, "read_line_config", return_value=FAKE_CONFIG), \
             patch.object(line_notify, "urlopen", side_effect=exc):
            with self.assertRaises(RuntimeError):
                line_notify.send_line_message("test")
        state = line_notify.read_line_failure_state()
        self.assertEqual(state["consecutiveFailures"], 1)
        self.assertIn("HTTP 401", state["lastError"])


class LineFailureStateAtomicityAndConcurrencyTests(_IsolatedLineStateTestCase):
    """對應這次修的兩個問題：
      1. _write_line_failure_state 原本直接 write_text 覆寫，寫入中途被中斷
         會留下半截 JSON。改成 temp+os.replace 原子寫入後，這裡驗證正常
         寫入完成後不會留下 .tmp 暫存檔。
      2. _record_line_result 是「讀舊值->算新值->寫回」，daily_update.py
         排程執行緒跟 server.py 通知鏈執行緒可能同時呼叫，沒有鎖時其中
         一次的 +1 會被覆蓋掉。這裡直接呼叫 _record_line_result（略過
         send_line_message 的網路層）用多執行緒同時打，驗證加鎖後計數
         不會遺漏。"""

    def test_write_leaves_no_leftover_temp_file(self):
        line_notify._record_line_result(False, "boom")
        state_path = line_notify.LINE_FAILURE_STATE_PATH
        self.assertTrue(state_path.exists())
        temp_path = state_path.with_name(f"{state_path.name}.tmp")
        self.assertFalse(temp_path.exists(), "atomic write 後不該留下 .tmp 暫存檔")

    def test_concurrent_failures_do_not_lose_updates(self):
        call_count = 20
        threads = [
            threading.Thread(target=line_notify._record_line_result, args=(False, "boom"))
            for _ in range(call_count)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        state = line_notify.read_line_failure_state()
        self.assertEqual(state["consecutiveFailures"], call_count)


class LineMonthlyQuotaTests(_IsolatedLineStateTestCase):
    """LINE Messaging API 免費方案每月只有200則推播額度(2026-07-03發生過
    重複通知白白消耗額度的事故後加的追蹤機制)。這裡驗證：(1)成功送出才
    計數、失敗不計，(2)跨月自動歸零，(3)warn旗標在達到警示門檻時為真，
    (4)檔案損毀時視為0不炸掉。繼承共用基底隔離狀態檔，不觸碰真實計數檔。"""

    @property
    def quota_path(self):
        return line_notify.LINE_QUOTA_PATH

    def test_no_file_reads_as_zero(self):
        quota = line_notify.read_line_quota()
        self.assertEqual(quota["sent"], 0)
        self.assertEqual(quota["limit"], line_notify.LINE_MONTHLY_QUOTA)
        self.assertFalse(quota["warn"])

    def test_successful_send_increments_count(self):
        class _FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return b""

        with patch.object(line_notify, "read_line_config", return_value=FAKE_CONFIG), \
             patch.object(line_notify, "urlopen", return_value=_FakeResponse()):
            line_notify.send_line_message("test")
            line_notify.send_line_message("test")
        self.assertEqual(line_notify.read_line_quota()["sent"], 2)

    def test_failed_send_does_not_increment_count(self):
        with patch.object(line_notify, "read_line_config", return_value=FAKE_CONFIG), \
             patch.object(line_notify, "urlopen", side_effect=URLError("network down")):
            with self.assertRaises(RuntimeError):
                line_notify.send_line_message("test")
        self.assertEqual(line_notify.read_line_quota()["sent"], 0)

    def test_previous_month_count_resets_to_zero(self):
        import json as json_module
        self.quota_path.write_text(json_module.dumps({"month": "2020-01", "sent": 150}), encoding="utf-8")
        quota = line_notify.read_line_quota()
        self.assertEqual(quota["sent"], 0)

    def test_warn_flag_at_threshold(self):
        import datetime as dt_module
        import json as json_module
        current_month = dt_module.datetime.now().strftime("%Y-%m")
        self.quota_path.write_text(
            json_module.dumps({"month": current_month, "sent": line_notify.LINE_QUOTA_WARN_THRESHOLD}),
            encoding="utf-8",
        )
        quota = line_notify.read_line_quota()
        self.assertTrue(quota["warn"])
        self.assertEqual(quota["remaining"], line_notify.LINE_MONTHLY_QUOTA - line_notify.LINE_QUOTA_WARN_THRESHOLD)

    def test_corrupted_quota_file_reads_as_zero(self):
        self.quota_path.write_text("{ not valid json", encoding="utf-8")
        quota = line_notify.read_line_quota()
        self.assertEqual(quota["sent"], 0)


class _FakeResponse:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return b""


class LineQuotaGatekeeperTests(_IsolatedLineStateTestCase):
    """額度守門員：(1)剩餘額度進入保留池(<=20)時 normal 級通知回傳 suppressed
    不真的送、critical 級(賣出/停損/系統故障)照送；(2)跨過80%門檻(160則)當月
    發一次性警示，之後不重複；(3)警示本身送失敗不影響原訊息的成功回報、
    下一次送出會再重試警示。全部繼承隔離基底，不碰正式額度檔。"""

    def _write_quota(self, sent, warn_notified=False):
        import datetime as dt_module
        import json as json_module
        current_month = dt_module.datetime.now().strftime("%Y-%m")
        self.quota_path = line_notify.LINE_QUOTA_PATH
        self.quota_path.write_text(
            json_module.dumps({
                "month": current_month,
                "sent": sent,
                "warnNotifiedMonth": current_month if warn_notified else "",
            }),
            encoding="utf-8",
        )

    def test_normal_priority_suppressed_in_reserve_pool(self):
        self._write_quota(200 - line_notify.LINE_QUOTA_RESERVE_FOR_CRITICAL)  # remaining == 保留池
        with patch.object(line_notify, "read_line_config", return_value=FAKE_CONFIG), \
             patch.object(line_notify, "urlopen", return_value=_FakeResponse()) as mock_urlopen:
            result = line_notify.send_line_message("例行晨報")
        self.assertTrue(result["ok"], "suppressed 必須回 ok=True，否則排程會觸發3次重試+失敗警示連鎖")
        self.assertFalse(result["sent"])
        self.assertTrue(result["suppressed"])
        mock_urlopen.assert_not_called()

    def test_critical_priority_still_sends_in_reserve_pool(self):
        # warn_notified=True：199已遠超80%門檻，先標已警示，隔離掉警示邏輯
        # 只驗證保留池對 critical 放行這件事。
        self._write_quota(199, warn_notified=True)  # 只剩 1 則也要放行 critical
        with patch.object(line_notify, "read_line_config", return_value=FAKE_CONFIG), \
             patch.object(line_notify, "urlopen", return_value=_FakeResponse()) as mock_urlopen:
            result = line_notify.send_line_message("跌破停損", priority="critical")
        self.assertTrue(result["sent"])
        mock_urlopen.assert_called_once()

    def test_normal_priority_sends_when_quota_ample(self):
        self._write_quota(100)
        with patch.object(line_notify, "read_line_config", return_value=FAKE_CONFIG), \
             patch.object(line_notify, "urlopen", return_value=_FakeResponse()) as mock_urlopen:
            result = line_notify.send_line_message("例行晨報")
        self.assertTrue(result["sent"])
        mock_urlopen.assert_called_once()

    def test_concurrent_normal_sends_never_exceed_reserve_pool_boundary(self):
        # 2026-07-04 稽核修復的核心驗證：額度保留池檢查+佔用名額原本是「先讀
        # 再寫」兩步，中間沒鎖，多執行緒同時讀到同一個 remaining 都判定通過、
        # 都真的送出，讓保留池在臨界值上被多送。修成鎖內原子性的
        # check-and-reserve 之後，不管多少執行緒同時搶，實際送出的則數必須
        # 精確等於「這批呼叫開始前」還剩下的名額，不能多也不能少。
        # 剩5則名額(remaining=25，門檻20)，20個執行緒同時搶，必須精確只有
        # 5個成功、15個被保留池擋下。warn_notified=True 隔離掉80%警示邏輯
        # (200-20-5=175則已經超過160的警示門檻，不標記的話第一次成功送出
        # 會自動觸發一則額外的critical警示訊息，污染這裡要驗證的名額計數)。
        slots = 5
        self._write_quota(200 - line_notify.LINE_QUOTA_RESERVE_FOR_CRITICAL - slots, warn_notified=True)
        results = []
        results_lock = threading.Lock()

        def _send():
            result = line_notify.send_line_message("盤中進場訊號", priority="normal")
            with results_lock:
                results.append(result)

        with patch.object(line_notify, "read_line_config", return_value=FAKE_CONFIG), \
             patch.object(line_notify, "urlopen", return_value=_FakeResponse()):
            threads = [threading.Thread(target=_send) for _ in range(20)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        sent_count = sum(1 for r in results if r["sent"])
        suppressed_count = sum(1 for r in results if r.get("suppressed"))
        self.assertEqual(sent_count, slots, "保留池臨界值上不能被多執行緒同時擠過去")
        self.assertEqual(suppressed_count, 20 - slots)
        self.assertEqual(line_notify.read_line_quota()["remaining"], line_notify.LINE_QUOTA_RESERVE_FOR_CRITICAL,
                          "剛好用完這批名額後，remaining必須精確等於保留池門檻，不能透支")

    def test_warn_notification_fires_once_when_crossing_threshold(self):
        self._write_quota(line_notify.LINE_QUOTA_WARN_THRESHOLD - 1)  # 這一則送完剛好踩到門檻
        with patch.object(line_notify, "read_line_config", return_value=FAKE_CONFIG), \
             patch.object(line_notify, "urlopen", return_value=_FakeResponse()) as mock_urlopen:
            line_notify.send_line_message("第160則")
            # 原訊息 + 80%警示 = 2 次網路呼叫
            self.assertEqual(mock_urlopen.call_count, 2)
            self.assertTrue(line_notify.read_line_quota()["warnNotified"])
            line_notify.send_line_message("後續訊息")
            # 已警示過，本月不再重複：只多 1 次
            self.assertEqual(mock_urlopen.call_count, 3)

    def test_concurrent_threshold_crossings_only_warn_once(self):
        # 2026-07-04 稽核修復的第二項驗證：_maybe_send_quota_warning 原本
        # 「讀 warnNotified」到「標記+送出」中間沒鎖，多執行緒同時跨過80%
        # 門檻會各自讀到 warnNotified=False、各自送出警示。修成鎖內原子性的
        # check-and-mark 之後，不管幾個執行緒同時跨過門檻，警示只能送一次。
        self._write_quota(line_notify.LINE_QUOTA_WARN_THRESHOLD - 1)
        warn_send_count = {"n": 0}
        count_lock = threading.Lock()

        def _urlopen_side_effect(request, timeout=30):
            # 用 request body 分辨是警示訊息還是一般訊息(警示文字帶「額度警示」)
            body = request.data.decode("utf-8") if request.data else ""
            if "額度警示" in body:
                with count_lock:
                    warn_send_count["n"] += 1
            return _FakeResponse()

        def _send():
            line_notify.send_line_message("盤中進場訊號", priority="normal")

        with patch.object(line_notify, "read_line_config", return_value=FAKE_CONFIG), \
             patch.object(line_notify, "urlopen", side_effect=_urlopen_side_effect):
            threads = [threading.Thread(target=_send) for _ in range(10)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        self.assertEqual(warn_send_count["n"], 1, "10個執行緒同時跨過80%門檻，警示只能送一次")

    def test_warn_notification_failure_does_not_retry_but_original_message_still_succeeds(self):
        # 2026-07-04 稽核修復後的新行為：_maybe_send_quota_warning 把「讀
        # warnNotified 判斷」到「標記已警示」改成鎖內原子操作、且標記發生在
        # 真的送出警示之前(換掉舊版「送失敗才不標記、下次重試」的設計)，
        # 目的是堵住多執行緒同時跨過80%門檻各自發送警示的race condition。
        # 代價：警示送失敗不會重試(要等下個月門檻重新跨過)，但不影響原本
        # 那則訊息本身的成功回報。
        self._write_quota(line_notify.LINE_QUOTA_WARN_THRESHOLD - 1)
        calls = {"n": 0}

        def _urlopen_side_effect(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 2:  # 第2次呼叫=80%警示那則，讓它失敗
                raise URLError("network hiccup")
            return _FakeResponse()

        with patch.object(line_notify, "read_line_config", return_value=FAKE_CONFIG), \
             patch.object(line_notify, "urlopen", side_effect=_urlopen_side_effect):
            result = line_notify.send_line_message("第160則")
            self.assertTrue(result["sent"], "警示送失敗不能拖垮原訊息的成功回報")
            self.assertTrue(line_notify.read_line_quota()["warnNotified"], "標記已警示發生在送出之前，即使送出失敗也已標記")
            line_notify.send_line_message("後續訊息")  # 第3次=訊息；warnNotified已是True，不會再嘗試送警示
        self.assertEqual(calls["n"], 3, "已標記過的警示不會重試")

    def test_force_bypasses_reserve_pool_even_with_normal_priority(self):
        # 2026-07-04 低優先稽核修復：force=True 原本只繞過 enabled 檢查、卻不
        # 繞過額度保留池，語意矛盾(force 應該是「無論如何都要送」)。修復後
        # force 跟 critical 一樣不受保留池限制、但仍計入月額度。
        # remaining==RESERVE(保留池上限)時，純 normal 會被 suppressed，
        # 帶 force=True 必須照送。warn_notified=True 隔離掉80%警示邏輯。
        self._write_quota(200 - line_notify.LINE_QUOTA_RESERVE_FOR_CRITICAL, warn_notified=True)
        with patch.object(line_notify, "read_line_config", return_value=FAKE_CONFIG), \
             patch.object(line_notify, "urlopen", return_value=_FakeResponse()) as mock_urlopen:
            blocked = line_notify.send_line_message("例行", priority="normal")  # 對照組：被擋
            self.assertTrue(blocked["suppressed"], "同樣額度下純 normal 應被保留池擋下")
            self.assertFalse(blocked["sent"])
            result = line_notify.send_line_message("強制送出", force=True, priority="normal")
        self.assertTrue(result["sent"], "force=True 應繞過額度保留池照送")
        mock_urlopen.assert_called_once()  # 只有 force 那則真的送出，被擋的 normal 不打 urlopen
        # force 那則仍計入月額度(維持統計正確)
        self.assertEqual(line_notify.read_line_quota()["sent"],
                         200 - line_notify.LINE_QUOTA_RESERVE_FOR_CRITICAL + 1)

    def test_record_line_sent_resets_stale_warn_month_across_month_boundary(self):
        # 2026-07-04 低優先稽核修復：跨月時 _record_line_sent/_rollback_line_sent
        # 不能把上個月的 warnNotifiedMonth 原封寫進新月份紀錄(會出現
        # month=本月、warnNotifiedMonth=上個月 的自相矛盾狀態)。
        import datetime as dt_module
        import json as json_module
        current_month = dt_module.datetime.now().strftime("%Y-%m")
        self.quota_path = line_notify.LINE_QUOTA_PATH
        self.quota_path.write_text(json_module.dumps({
            "month": "2020-01", "sent": 199, "warnNotifiedMonth": "2020-01",
        }), encoding="utf-8")
        with line_notify._LINE_FAILURE_STATE_LOCK:
            line_notify._record_line_sent()
        raw = json_module.loads(self.quota_path.read_text(encoding="utf-8"))
        self.assertEqual(raw["month"], current_month, "跨月後月份應更新為本月")
        self.assertEqual(raw["sent"], 1, "上個月的199不計入本月，本月從1開始")
        self.assertEqual(raw["warnNotifiedMonth"], "", "上個月的警示標記不該帶進本月")
        self.assertFalse(line_notify.read_line_quota()["warnNotified"],
                         "跨月後本月尚未警示過，應可重新觸發80%警示")


class LineSellOnlyTests(_IsolatedLineStateTestCase):
    """只發賣出模式(LINE_SELL_ONLY=True,使用者 2026-07-06 偏好):賣出/停損以外的
    例行通知(晨報/盤後摘要/進場/漲停打開/突破,都是 priority=normal 或預設)一律不送、
    回 suppressed(ok=True 讓排程重試鏈不誤判失敗);critical(賣出/停損/系統故障)
    與 force(通知測試按鈕)照送。基底已把 LINE_SELL_ONLY 設 False,這裡各測試重新
    patch 成 True。額度充足以隔離掉保留池邏輯,只驗證 sell-only 閘門本身。"""

    def setUp(self):
        super().setUp()
        # 額度充足,排除保留池(<=20)干擾——確保 normal 若被擋是「只發賣出」而非額度不足
        import datetime as dt_module
        import json as json_module
        month = dt_module.datetime.now().strftime("%Y-%m")
        line_notify.LINE_QUOTA_PATH.write_text(
            json_module.dumps({"month": month, "sent": 100, "warnNotifiedMonth": month}),
            encoding="utf-8",
        )

    def test_normal_priority_suppressed_in_sell_only_mode(self):
        with patch.object(line_notify, "LINE_SELL_ONLY", True), \
             patch.object(line_notify, "read_line_config", return_value=FAKE_CONFIG), \
             patch.object(line_notify, "urlopen", return_value=_FakeResponse()) as mock_urlopen:
            result = line_notify.send_line_message("例行晨報")
        self.assertTrue(result["ok"], "suppressed 必須回 ok=True,否則排程重試鏈會誤判失敗")
        self.assertFalse(result["sent"])
        self.assertTrue(result["suppressed"])
        mock_urlopen.assert_not_called()

    def test_critical_priority_still_sends_in_sell_only_mode(self):
        with patch.object(line_notify, "LINE_SELL_ONLY", True), \
             patch.object(line_notify, "read_line_config", return_value=FAKE_CONFIG), \
             patch.object(line_notify, "urlopen", return_value=_FakeResponse()) as mock_urlopen:
            result = line_notify.send_line_message("跌破停損,建議賣出", priority="critical")
        self.assertTrue(result["sent"], "賣出/停損提醒在只發賣出模式下必須照送")
        mock_urlopen.assert_called_once()

    def test_force_still_sends_in_sell_only_mode(self):
        with patch.object(line_notify, "LINE_SELL_ONLY", True), \
             patch.object(line_notify, "read_line_config", return_value=FAKE_CONFIG), \
             patch.object(line_notify, "urlopen", return_value=_FakeResponse()) as mock_urlopen:
            result = line_notify.send_line_message("StockAI LINE 測試", force=True)
        self.assertTrue(result["sent"], "通知測試按鈕(force=True)在只發賣出模式下仍要能送")
        mock_urlopen.assert_called_once()


if __name__ == "__main__":
    unittest.main()
