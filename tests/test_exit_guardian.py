"""
伺服器端停損守門員(check_portfolio_exit_guardian)的回歸測試。

功能：前端每分鐘把防守價 POST 到 /api/portfolio/exit-watch(=心跳)；心跳
超過 3 分鐘(瀏覽器離線)且盤中時段，伺服器用 Shioaji 報價比對防守價，
跌破發 critical 級 LINE(每檔每日去重、每日上限3則、stale報價跳過、
除權息當日豁免、送達成功才標記)。

隔離鐵律：狀態存正式 model_meta，測試一律 patch 兩個 STATE_KEY 成
__test_*__ 假前綴並清理；除權息豁免測試 patch _today_ex_dividend_symbols
函式本身，絕不寫正式的 dividend_calendar_cache。報價/LINE/桌面通知全 mock。

執行方式：
  python -m unittest tests.test_exit_guardian -v
"""
import json
import os
import sys
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server as server_module

TEST_WATCH_KEY = "__test_exit_watch_state__"
TEST_NOTIFY_KEY = "__test_exit_guardian_notify_state__"


class ExitGuardianTests(unittest.TestCase):
    def setUp(self):
        self._patchers = [
            patch.object(server_module, "EXIT_WATCH_STATE_KEY", TEST_WATCH_KEY),
            patch.object(server_module, "EXIT_GUARDIAN_NOTIFY_STATE_KEY", TEST_NOTIFY_KEY),
            # 守門員 2026-07-08 起預設關閉(EXIT_GUARDIAN_ENABLED=False,使用者多半在
            # 電腦前、不需要離線代看)。這些測試驗證的是「開啟時的守門邏輯」本身,故
            # patch 成 True;另有 test_disabled_flag_skips_entire_check 專測關閉時跳過。
            patch.object(server_module, "EXIT_GUARDIAN_ENABLED", True),
        ]
        for p in self._patchers:
            p.start()
        self._delete_test_keys()

    def tearDown(self):
        self._delete_test_keys()
        for p in self._patchers:
            p.stop()

    def _delete_test_keys(self):
        with server_module.backend.connect() as conn:
            conn.execute(
                "DELETE FROM model_meta WHERE key IN (?, ?)", (TEST_WATCH_KEY, TEST_NOTIFY_KEY)
            )
            server_module.ensure_exit_decision_log_table(conn)
            conn.execute("DELETE FROM exit_decision_logs WHERE symbol LIKE 'ZTEST%'")
            conn.commit()

    def _write_watch(self, items, heartbeat_age_seconds=600, include_verified_decision=True):
        today = server_module.scheduler_today(server_module.taipei_localtime())
        normalized_items = []
        for raw in items:
            item = dict(raw)
            if include_verified_decision:
                item.setdefault("decisionVerified", True)
                item.setdefault("decisionType", "stop")
                item.setdefault("decisionReasons", ["今日轉跌", "停利/停損條件成立"])
                item.setdefault("decisionAt", f"{today}T10:00:00+08:00")
                item.setdefault("decisionDate", today)
                item.setdefault("decisionDataDate", today)
                item.setdefault("decisionDataReady", True)
                item.setdefault("quoteSource", "Shioaji quote")
                item.setdefault("policyVersion", "portfolio-exit-v2")
            normalized_items.append(item)
        with server_module.backend.connect() as conn:
            server_module.backend.set_meta(conn, TEST_WATCH_KEY, json.dumps({
                "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                "tsEpoch": time.time() - heartbeat_age_seconds,
                "items": normalized_items,
            }, ensure_ascii=False))

    def _read_notify_state(self):
        with server_module.backend.connect() as conn:
            row = conn.execute("SELECT value FROM model_meta WHERE key = ?", (TEST_NOTIFY_KEY,)).fetchone()
        return json.loads(row[0]) if row else None

    def _quotes(self, mapping, stale=False):
        return {"ok": True, "stale": stale, "quotes": {code: {"currentPrice": price} for code, price in mapping.items()}}

    def _held(self, codes):
        # 2026-07-04 新增的持股核對：check_portfolio_exit_guardian 在真的要
        # 發通知前會呼叫 sinopac_backend.holdings() 核對是否仍持有，這裡的
        # 假股票代號(ZTEST*)在真實永豐帳號裡當然不存在，不明確 mock 的話
        # 這台機器上如果剛好有可用的永豐設定，真的會呼叫 holdings() 把測試
        # 用的假部位濾掉(2026-07-04 實測踩過)。
        return {"ok": True, "holdings": [{"code": c} for c in codes]}

    def test_disabled_flag_skips_entire_check(self):
        # 預設 EXIT_GUARDIAN_ENABLED=False(使用者已關閉守門員)→ 整個守門檢查
        # 直接跳過、不比對報價、不發通知。硬停損通知另走前端 hardStopOverride。
        self._write_watch([{"code": "ZTEST1", "stopLoss": 50.0, "confirmSell": 49.0, "name": "測試"}])
        with patch.object(server_module, "EXIT_GUARDIAN_ENABLED", False):
            result = server_module.check_portfolio_exit_guardian(force=True)
        self.assertFalse(result["checked"])
        self.assertEqual(result.get("skipped"), "disabled")

    def test_fresh_heartbeat_means_browser_online_no_check(self):
        self._write_watch([{"code": "ZTEST1", "name": "測試股", "stopLoss": 95.0, "confirmSell": 94.5}], heartbeat_age_seconds=30)
        with patch.object(server_module.sinopac_backend, "quotes") as mock_quotes:
            result = server_module.check_portfolio_exit_guardian(force=True)
        self.assertEqual(result["skipped"], "browser_online")
        mock_quotes.assert_not_called()

    def test_breach_sends_critical_line_and_marks_dedup(self):
        self._write_watch([{"code": "ZTEST1", "name": "測試股", "stopLoss": 95.0, "confirmSell": 94.5}])
        with patch.object(server_module.sinopac_backend, "quotes",
                          return_value=self._quotes({"ZTEST1": 94.0})), \
             patch.object(server_module.sinopac_backend, "holdings", return_value=self._held(["ZTEST1"])), \
             patch.object(server_module, "_today_ex_dividend_symbols", return_value=set()), \
             patch.object(server_module, "send_line_message_via_api",
                          return_value={"ok": True, "sent": True}) as mock_line:
            result = server_module.check_portfolio_exit_guardian(force=True)
        self.assertEqual(result["channel"], "line")
        self.assertEqual(result["breaches"], 1)
        mock_line.assert_called_once()
        self.assertEqual(mock_line.call_args.kwargs.get("priority"), "critical",
                         "停損守門通知必須是critical級，額度保留池內仍要放行")
        message = mock_line.call_args[0][0]
        self.assertIn("ZTEST1", message)
        self.assertIn("94.00", message)          # 現價
        self.assertIn("94.50", message)          # 觸發用的是「確認賣出價」而非防守價 95.00
        self.assertIn("確認賣出價", message)
        self.assertIn("計算驗證", message)
        self.assertIn("今日轉跌", message)
        self.assertNotIn("95.00", message, "不該再用防守價當觸發/顯示,那會太早提醒")
        state = self._read_notify_state()
        self.assertIn("ZTEST1", state["symbols"])
        self.assertEqual(state["lineCount"], 1)
        logs = [
            row for row in server_module.list_exit_decision_logs()["logs"]
            if row["symbol"] == "ZTEST1"
        ]
        self.assertEqual(len(logs), 1)
        self.assertEqual(logs[0]["current_price"], 94.0)
        self.assertEqual(logs[0]["confirm_sell_price"], 94.5)
        self.assertEqual(logs[0]["channel"], "line")
        self.assertEqual(logs[0]["decision_verified"], 1)
        self.assertEqual(logs[0]["decision_type"], "stop")
        self.assertIn("今日轉跌", logs[0]["decision_reasons"])

    def test_price_breach_without_calculated_sell_verification_is_rejected(self):
        self._write_watch(
            [{"code": "ZTEST1", "name": "測試股", "stopLoss": 95.0, "confirmSell": 94.5}],
            include_verified_decision=False,
        )
        with patch.object(server_module.sinopac_backend, "quotes") as mock_quotes, \
             patch.object(server_module, "send_line_message_via_api") as mock_line:
            result = server_module.check_portfolio_exit_guardian(force=True)
        self.assertEqual(result["skipped"], "no_verified_exit_decision")
        self.assertEqual(result["decisionRejected"], {"decision_not_verified": 1})
        mock_quotes.assert_not_called()
        mock_line.assert_not_called()

    def test_yesterdays_sell_verification_cannot_trigger_today(self):
        self._write_watch([{
            "code": "ZTEST1", "name": "測試股", "stopLoss": 95.0,
            "confirmSell": 94.5, "decisionDate": "2000-01-01",
        }])
        with patch.object(server_module.sinopac_backend, "quotes") as mock_quotes:
            result = server_module.check_portfolio_exit_guardian(force=True)
        self.assertEqual(result["skipped"], "no_verified_exit_decision")
        self.assertEqual(result["decisionRejected"], {"decision_not_today": 1})
        mock_quotes.assert_not_called()

    def test_no_breach_when_price_above_stop(self):
        self._write_watch([{"code": "ZTEST1", "name": "測試股", "stopLoss": 95.0, "confirmSell": 94.5}])
        with patch.object(server_module.sinopac_backend, "quotes",
                          return_value=self._quotes({"ZTEST1": 96.5})), \
             patch.object(server_module, "_today_ex_dividend_symbols", return_value=set()), \
             patch.object(server_module, "send_line_message_via_api") as mock_line:
            result = server_module.check_portfolio_exit_guardian(force=True)
        self.assertEqual(result["breaches"], 0)
        mock_line.assert_not_called()

    def test_clamped_confirmsell_still_breaches_on_absolute_stop(self):
        # 2026-07-07 稽核修復:前端 confirmSell 被 current×0.99 夾擠而低於現價時(跳空/急殺
        # 貫穿停損),舊版只有 current<=confirmSell 這條 → 永遠不成立 → 漏發停損。新版用
        # 絕對停損線 stopLoss×0.99 兜底:現價 94 已跌破停損 95,即使 confirmSell 被夾到 92
        # (低於現價),仍必須觸發 critical 停損通知。
        self._write_watch([{"code": "ZTEST1", "name": "測試股", "stopLoss": 95.0, "confirmSell": 92.0}])
        with patch.object(server_module.sinopac_backend, "quotes",
                          return_value=self._quotes({"ZTEST1": 94.0})), \
             patch.object(server_module.sinopac_backend, "holdings", return_value=self._held(["ZTEST1"])), \
             patch.object(server_module, "_today_ex_dividend_symbols", return_value=set()), \
             patch.object(server_module, "send_line_message_via_api",
                          return_value={"ok": True, "sent": True}) as mock_line:
            server_module.check_portfolio_exit_guardian(force=True)
        mock_line.assert_called_once()
        state = self._read_notify_state()
        self.assertIn("ZTEST1", (state or {}).get("symbols", []))

    def test_stale_quotes_skip_round(self):
        self._write_watch([{"code": "ZTEST1", "name": "測試股", "stopLoss": 95.0, "confirmSell": 94.5}])
        with patch.object(server_module.sinopac_backend, "quotes",
                          return_value=self._quotes({"ZTEST1": 90.0}, stale=True)), \
             patch.object(server_module, "send_line_message_via_api") as mock_line:
            result = server_module.check_portfolio_exit_guardian(force=True)
        self.assertEqual(result["skipped"], "stale_quotes")
        mock_line.assert_not_called()

    def test_same_symbol_same_day_deduped(self):
        self._write_watch([{"code": "ZTEST1", "name": "測試股", "stopLoss": 95.0, "confirmSell": 94.5}])
        with patch.object(server_module.sinopac_backend, "quotes",
                          return_value=self._quotes({"ZTEST1": 94.0})), \
             patch.object(server_module.sinopac_backend, "holdings", return_value=self._held(["ZTEST1"])), \
             patch.object(server_module, "_today_ex_dividend_symbols", return_value=set()), \
             patch.object(server_module, "send_line_message_via_api",
                          return_value={"ok": True, "sent": True}) as mock_line:
            server_module.check_portfolio_exit_guardian(force=True)
            result = server_module.check_portfolio_exit_guardian(force=True)
        self.assertEqual(result["breaches"], 0, "同檔同日已通知過不重複")
        self.assertEqual(mock_line.call_count, 1)

    def test_line_failure_does_not_mark_dedup(self):
        self._write_watch([{"code": "ZTEST1", "name": "測試股", "stopLoss": 95.0, "confirmSell": 94.5}])
        with patch.object(server_module.sinopac_backend, "quotes",
                          return_value=self._quotes({"ZTEST1": 94.0})), \
             patch.object(server_module.sinopac_backend, "holdings", return_value=self._held(["ZTEST1"])), \
             patch.object(server_module, "_today_ex_dividend_symbols", return_value=set()), \
             patch.object(server_module, "send_line_message_via_api",
                          side_effect=RuntimeError("LINE push failed: DNS")):
            with self.assertRaises(RuntimeError):
                server_module.check_portfolio_exit_guardian(force=True)
        self.assertIsNone(self._read_notify_state(), "送出失敗不能標記，下一輪要重試")

    def test_daily_line_cap_falls_back_to_desktop(self):
        today = server_module.scheduler_today(server_module.taipei_localtime())
        with server_module.backend.connect() as conn:
            server_module.backend.set_meta(conn, TEST_NOTIFY_KEY, json.dumps({
                "date": today, "symbols": [], "lineCount": server_module.EXIT_GUARDIAN_DAILY_LINE_CAP,
            }))
        self._write_watch([{"code": "ZTEST1", "name": "測試股", "stopLoss": 95.0, "confirmSell": 94.5}])
        with patch.object(server_module.sinopac_backend, "quotes",
                          return_value=self._quotes({"ZTEST1": 94.0})), \
             patch.object(server_module.sinopac_backend, "holdings", return_value=self._held(["ZTEST1"])), \
             patch.object(server_module, "_today_ex_dividend_symbols", return_value=set()), \
             patch.object(server_module, "send_line_message_via_api") as mock_line, \
             patch.object(server_module, "send_windows_desktop_notification",
                          return_value={"ok": True, "sent": True}) as mock_desktop:
            result = server_module.check_portfolio_exit_guardian(force=True)
        self.assertEqual(result["channel"], "desktop")
        mock_line.assert_not_called()
        mock_desktop.assert_called_once()
        self.assertIn("ZTEST1", self._read_notify_state()["symbols"])

    def test_ex_dividend_today_symbol_exempted(self):
        # 除權息當天參考價下調，昨天同步的防守價會假觸發，該檔當日豁免
        self._write_watch([{"code": "ZTESTD", "name": "除息股", "stopLoss": 95.0}])
        with patch.object(server_module.sinopac_backend, "quotes",
                          return_value=self._quotes({"ZTESTD": 90.0})), \
             patch.object(server_module, "_today_ex_dividend_symbols", return_value={"ZTESTD"}), \
             patch.object(server_module, "send_line_message_via_api") as mock_line:
            result = server_module.check_portfolio_exit_guardian(force=True)
        self.assertEqual(result["breaches"], 0)
        mock_line.assert_not_called()

    def test_no_watch_data_skips(self):
        result = server_module.check_portfolio_exit_guardian(force=True)
        self.assertEqual(result["skipped"], "no_watch_data")

    def test_monitoring_disabled_skips_entire_check(self):
        # 2026-07-04 稽核修復：使用者主動取消勾選「背景監控」，跟心跳斷了
        # 是完全不同的語意，伺服器要整個跳過檢查，不能誤判成離線接管。
        self._write_watch([{"code": "ZTEST1", "name": "測試股", "stopLoss": 95.0, "confirmSell": 94.5}])
        with server_module.backend.connect() as conn:
            state = json.loads(conn.execute(
                "SELECT value FROM model_meta WHERE key = ?", (TEST_WATCH_KEY,)
            ).fetchone()[0])
            state["monitoring"] = False
            server_module.backend.set_meta(conn, TEST_WATCH_KEY, json.dumps(state))
        with patch.object(server_module.sinopac_backend, "quotes") as mock_quotes:
            result = server_module.check_portfolio_exit_guardian(force=True)
        self.assertEqual(result["skipped"], "monitoring_disabled")
        mock_quotes.assert_not_called()

    def test_sold_while_offline_symbol_not_notified(self):
        # 2026-07-04 稽核修復：使用者離線期間透過這套系統以外的管道賣掉
        # 股票，伺服器手上還是賣出前的舊快照——發通知前額外核對真實持股，
        # 已經不再持有的代碼不該再發停損通知(浪費每日額度+誤導使用者)。
        self._write_watch([{"code": "ZTEST1", "name": "測試股", "stopLoss": 95.0, "confirmSell": 94.5}])
        with patch.object(server_module.sinopac_backend, "quotes",
                          return_value=self._quotes({"ZTEST1": 94.0})), \
             patch.object(server_module.sinopac_backend, "holdings", return_value=self._held([])), \
             patch.object(server_module, "_today_ex_dividend_symbols", return_value=set()), \
             patch.object(server_module, "send_line_message_via_api") as mock_line:
            result = server_module.check_portfolio_exit_guardian(force=True)
        self.assertEqual(result["skipped"], "no_longer_held")
        mock_line.assert_not_called()

    def test_holdings_check_failure_fails_open_still_notifies(self):
        # 持股核對本身失敗(網路/API問題)不能讓真正該通知的停損被吞掉，
        # 寧可fail-open多發一則。
        self._write_watch([{"code": "ZTEST1", "name": "測試股", "stopLoss": 95.0, "confirmSell": 94.5}])
        with patch.object(server_module.sinopac_backend, "quotes",
                          return_value=self._quotes({"ZTEST1": 94.0})), \
             patch.object(server_module.sinopac_backend, "holdings",
                          side_effect=RuntimeError("Shioaji 連線逾時")), \
             patch.object(server_module, "_today_ex_dividend_symbols", return_value=set()), \
             patch.object(server_module, "send_line_message_via_api",
                          return_value={"ok": True, "sent": True}) as mock_line:
            result = server_module.check_portfolio_exit_guardian(force=True)
        self.assertEqual(result["channel"], "line")
        mock_line.assert_called_once()

    def test_holdings_ok_false_also_fails_open(self):
        # holdings()回傳ok=False(帶錯誤訊息)但沒有拋例外的情況，同樣不能
        # 濾掉真正的breach。
        self._write_watch([{"code": "ZTEST1", "name": "測試股", "stopLoss": 95.0, "confirmSell": 94.5}])
        with patch.object(server_module.sinopac_backend, "quotes",
                          return_value=self._quotes({"ZTEST1": 94.0})), \
             patch.object(server_module.sinopac_backend, "holdings",
                          return_value={"ok": False, "error": "尚未設定永豐 API"}), \
             patch.object(server_module, "_today_ex_dividend_symbols", return_value=set()), \
             patch.object(server_module, "send_line_message_via_api",
                          return_value={"ok": True, "sent": True}) as mock_line:
            result = server_module.check_portfolio_exit_guardian(force=True)
        self.assertEqual(result["channel"], "line")
        mock_line.assert_called_once()


    def test_graze_below_defense_above_confirm_does_not_fire(self):
        # 2026-07-06 使用者回饋「真的要賣才提醒」的核心情境:現價只低於防守價一點點
        # (在防守價 95 與確認賣出價 94.5 之間)不該發 LINE——這正是當初 2302 52.10 vs
        # 防守 52.14 只低 0.08% 就誤發的修正。
        self._write_watch([{"code": "ZTEST1", "name": "測試股", "stopLoss": 95.0, "confirmSell": 94.5}])
        with patch.object(server_module.sinopac_backend, "quotes",
                          return_value=self._quotes({"ZTEST1": 94.8})), \
             patch.object(server_module.sinopac_backend, "holdings", return_value=self._held(["ZTEST1"])), \
             patch.object(server_module, "_today_ex_dividend_symbols", return_value=set()), \
             patch.object(server_module, "send_line_message_via_api") as mock_line:
            result = server_module.check_portfolio_exit_guardian(force=True)
        self.assertEqual(result["breaches"], 0, "只摸到防守價、還沒到確認賣出價,不該發")
        mock_line.assert_not_called()

    def test_fallback_uses_confirm_factor_when_no_confirm_sell(self):
        # 舊快取沒帶 confirmSell 時,退回用 stopLoss × EXIT_GUARDIAN_CONFIRM_FACTOR(0.99)
        # 自行推算確認賣出價(95→94.05)。價 94.0 ≤ 94.05 才發,一樣保留 1% 緩衝。
        self._write_watch([{"code": "ZTEST1", "name": "測試股", "stopLoss": 95.0}])  # 無 confirmSell
        with patch.object(server_module.sinopac_backend, "quotes",
                          return_value=self._quotes({"ZTEST1": 94.0})), \
             patch.object(server_module.sinopac_backend, "holdings", return_value=self._held(["ZTEST1"])), \
             patch.object(server_module, "_today_ex_dividend_symbols", return_value=set()), \
             patch.object(server_module, "send_line_message_via_api",
                          return_value={"ok": True, "sent": True}) as mock_line:
            result = server_module.check_portfolio_exit_guardian(force=True)
        self.assertEqual(result["breaches"], 1)
        mock_line.assert_called_once()

    def test_fallback_graze_between_defense_and_confirm_factor_does_not_fire(self):
        # 無 confirmSell 時,價 94.5 在防守 95 與推算確認價 94.05 之間 → 一樣不發。
        self._write_watch([{"code": "ZTEST1", "name": "測試股", "stopLoss": 95.0}])
        with patch.object(server_module.sinopac_backend, "quotes",
                          return_value=self._quotes({"ZTEST1": 94.5})), \
             patch.object(server_module, "_today_ex_dividend_symbols", return_value=set()), \
             patch.object(server_module, "send_line_message_via_api") as mock_line:
            result = server_module.check_portfolio_exit_guardian(force=True)
        self.assertEqual(result["breaches"], 0)
        mock_line.assert_not_called()


class ExitWatchNotifiedTodayEndpointTests(unittest.TestCase):
    def setUp(self):
        self._patcher = patch.object(server_module, "EXIT_GUARDIAN_NOTIFY_STATE_KEY", TEST_NOTIFY_KEY)
        self._patcher.start()
        with server_module.backend.connect() as conn:
            conn.execute("DELETE FROM model_meta WHERE key = ?", (TEST_NOTIFY_KEY,))
            conn.commit()

    def tearDown(self):
        with server_module.backend.connect() as conn:
            conn.execute("DELETE FROM model_meta WHERE key = ?", (TEST_NOTIFY_KEY,))
            conn.commit()
        self._patcher.stop()

    def test_returns_todays_notified_symbols(self):
        # 2026-07-04 新增：前端切回分頁時要能查到伺服器背景這段時間已經
        # 通知過哪些代碼，避免對同一次跌破重複發送。
        today = server_module.scheduler_today(server_module.taipei_localtime())
        with server_module.backend.connect() as conn:
            server_module.backend.set_meta(conn, TEST_NOTIFY_KEY, json.dumps({
                "date": today, "symbols": ["ZTEST1", "ZTEST2"], "lineCount": 1,
            }))
        today_returned = server_module.scheduler_today(server_module.taipei_localtime())
        notify_state = server_module._read_exit_guardian_notify_state(today_returned)
        self.assertEqual(set(notify_state["symbols"]), {"ZTEST1", "ZTEST2"})


if __name__ == "__main__":
    unittest.main()
