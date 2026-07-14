"""
自動排程可靠性修復的回歸測試，對應這次修的 4 個問題：
  1. 排程失敗完全不重試也不通知 -> run_auto_schedule_job 現在會重試
     AUTO_SCHEDULE_MAX_RETRIES 次，超過才標記失敗並呼叫 LINE 通知。
  2. 排程判斷完全用系統本地時區 -> taipei_localtime() 明確用 Asia/Taipei，
     不管伺服器部署在哪個時區都不會讓開盤/收盤/排程視窗跑掉。
  3. 妖股掃描與模型循環已拆開；auto_monster_scan 會等純規則掃描真的結束，
     模型則由 auto_model_cycle 獨立重訓與批量預測。
  4. 窄時間窗被跳過就永久錯過，且完全沒有感知 -> auto_schedule_worker
     每輪都呼叫 check_missed_auto_schedule_windows 偵測「視窗已關但整天
     沒執行過」，第一次偵測到才發 LINE 通知（不重複洗版）。

全部使用假的 job_id / 假的 struct_time，不會動到正式的排程 meta 紀錄；
凡是寫進 model_meta 的測試資料，測試結束都會清乾淨。LINE 通知一律用
mock 頂掉，不會真的打 LINE API。

執行方式：
  python -m unittest tests.test_auto_schedule -v
"""
import os
import sys
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server
from ml_backend import StockMLBackend, backend


class RetiredTrainingSymbolTests(unittest.TestCase):
    def test_server_training_scope_excludes_delisted_symbols(self):
        self.assertEqual(server.normalize_training_symbols(["2888", "6806", "2330"]), ["2330"])


class RuntimeStatusPersistenceTests(unittest.TestCase):
    def _backend(self, tmp):
        test_backend = StockMLBackend()
        test_backend.db_path = Path(tmp) / "stock_system.sqlite3"
        test_backend.init_db()
        return test_backend

    def test_monster_scan_status_survives_restart_and_marks_interrupted_run(self):
        original = dict(server.monster_scan_status)
        with tempfile.TemporaryDirectory() as tmp:
            test_backend = self._backend(tmp)
            with patch.object(server, "backend", test_backend):
                server.persist_runtime_status(server.MONSTER_SCAN_STATUS_META_KEY, {
                    "running": True,
                    "phase": "純規則評分",
                    "processed": 20,
                    "saved": 5,
                    "startedAt": "2026-07-14 09:00:00",
                    "finishedAt": "",
                    "message": "掃描中",
                })
                server.monster_scan_status.clear()
                server.monster_scan_status.update({
                    "running": False, "phase": "尚未開始", "message": "",
                })
                restored = server.restore_monster_scan_status()
        server.monster_scan_status.clear()
        server.monster_scan_status.update(original)

        self.assertFalse(restored["running"])
        self.assertEqual(restored["phase"], "重啟中斷")
        self.assertIn("服務重啟中斷", restored["message"])

    def test_data_gap_status_restores_last_completed_result(self):
        original = dict(server.data_gap_repair_status)
        with tempfile.TemporaryDirectory() as tmp:
            test_backend = self._backend(tmp)
            with patch.object(server, "backend", test_backend):
                server.persist_runtime_status(server.DATA_GAP_REPAIR_STATUS_META_KEY, {
                    "ok": False,
                    "running": False,
                    "retry": True,
                    "finishedAt": "2026-07-13 17:00:00",
                    "checked": 79,
                    "attempted": 10,
                    "repaired": 4,
                    "stillMissing": 6,
                    "failed": 0,
                    "message": "仍缺 6 檔",
                })
                server.data_gap_repair_status.clear()
                server.data_gap_repair_status.update({
                    "ok": True, "running": False, "message": "尚未執行資料缺口修復",
                })
                restored = server.restore_data_gap_repair_status()
        server.data_gap_repair_status.clear()
        server.data_gap_repair_status.update(original)

        self.assertEqual(restored["checked"], 79)
        self.assertEqual(restored["stillMissing"], 6)
        self.assertEqual(restored["finishedAt"], "2026-07-13 17:00:00")


class _ImmediateThread:
    """讓 run_auto_schedule_job 內部的背景執行緒同步跑完，測試才能立刻斷言結果。"""

    def __init__(self, target=None, args=(), kwargs=None, daemon=None):
        self._target = target
        self._args = args
        self._kwargs = kwargs or {}

    def start(self):
        self._target(*self._args, **self._kwargs)


def _weekday_now(hour, minute):
    # 2026-07-02 是星期四(tm_wday=3)，欄位都寫死，測試不受實際執行日期影響。
    return time.struct_time((2026, 7, 2, hour, minute, 0, 3, 183, 0))


class TaipeiLocaltimeTests(unittest.TestCase):
    def test_returns_current_taipei_time_not_system_local(self):
        expected = datetime.now(ZoneInfo("Asia/Taipei"))
        result = server.taipei_localtime()
        self.assertEqual(result.tm_year, expected.year)
        self.assertEqual(result.tm_mon, expected.month)
        self.assertEqual(result.tm_mday, expected.day)
        self.assertEqual(result.tm_hour, expected.hour)
        self.assertEqual(result.tm_wday, expected.weekday())

    def test_market_schedule_only_allows_confirmed_trading_day(self):
        self.assertTrue(server.market_schedule_allowed({"known": True, "isTradingDay": True}))
        self.assertFalse(server.market_schedule_allowed({"known": True, "isTradingDay": False}))
        self.assertFalse(server.market_schedule_allowed({"known": False, "isTradingDay": None}))


class MonsterScanResultErrorCountTests(unittest.TestCase):
    def test_uses_complete_count_when_error_details_are_truncated(self):
        result = {"errorCount": 96, "errors": [{"symbol": str(i)} for i in range(20)]}
        self.assertEqual(server.monster_scan_result_error_count(result), 96)

    def test_falls_back_to_error_detail_length_for_legacy_results(self):
        result = {"errors": [{"symbol": "1107"}, {"symbol": "1230"}]}
        self.assertEqual(server.monster_scan_result_error_count(result), 2)


class AutoScheduleRetryTests(unittest.TestCase):
    JOB_ID = "__test_retry_job__"

    def _clear_state(self):
        with backend.connect() as conn:
            for suffix in ("date", "status", "message", "at", "attempt_date", "attempt_count"):
                conn.execute("DELETE FROM model_meta WHERE key = ?", (f"auto_schedule_{self.JOB_ID}_{suffix}",))
            conn.commit()
        server.auto_schedule_running.pop(self.JOB_ID, None)

    def setUp(self):
        self._clear_state()

    def tearDown(self):
        self._clear_state()

    def _run_sync(self, worker):
        with patch("server.threading.Thread", _ImmediateThread), \
             patch("server.taipei_localtime", return_value=_weekday_now(14, 35)):
            server.run_auto_schedule_job(self.JOB_ID, worker)

    def test_failure_below_retry_cap_does_not_mark_done_and_does_not_notify(self):
        def failing_worker():
            raise RuntimeError("boom")

        with patch("server.notify_auto_schedule_failure") as mock_notify:
            self._run_sync(failing_worker)

        today = server.scheduler_today(_weekday_now(14, 35))
        self.assertFalse(server.auto_schedule_has_run(self.JOB_ID, today))
        mock_notify.assert_not_called()

    def test_failure_reaches_retry_cap_marks_failed_and_notifies_once(self):
        def failing_worker():
            raise RuntimeError("boom")

        with patch("server.notify_auto_schedule_failure") as mock_notify:
            for _ in range(server.AUTO_SCHEDULE_MAX_RETRIES):
                self._run_sync(failing_worker)

        today = server.scheduler_today(_weekday_now(14, 35))
        self.assertTrue(server.auto_schedule_has_run(self.JOB_ID, today))
        mock_notify.assert_called_once()

    def test_success_after_earlier_failure_marks_done_and_resets_attempts(self):
        call_count = {"n": 0}

        def flaky_worker():
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise RuntimeError("boom")
            return "ok"

        with patch("server.notify_auto_schedule_failure") as mock_notify:
            self._run_sync(flaky_worker)  # 第一次失敗，還在重試次數內
            self._run_sync(flaky_worker)  # 第二次成功

        today = server.scheduler_today(_weekday_now(14, 35))
        self.assertTrue(server.auto_schedule_has_run(self.JOB_ID, today))
        self.assertEqual(server.auto_schedule_attempt_count(self.JOB_ID, today), 0)
        mock_notify.assert_not_called()

    def test_retry_sentinel_does_not_mark_done_or_count_as_failure(self):
        with patch("server.notify_auto_schedule_failure") as mock_notify:
            self._run_sync(lambda: server.AUTO_SCHEDULE_RETRY)

        today = server.scheduler_today(_weekday_now(14, 35))
        self.assertFalse(server.auto_schedule_has_run(self.JOB_ID, today))
        self.assertEqual(server.auto_schedule_attempt_count(self.JOB_ID, today), 0)
        mock_notify.assert_not_called()

    def test_partial_outcome_is_recorded_as_partial_not_success(self):
        self._run_sync(lambda: {
            "scheduleStatus": "partial",
            "message": "已補抓但仍有 2 檔待補",
        })

        today = server.scheduler_today(_weekday_now(14, 35))
        self.assertTrue(server.auto_schedule_has_run(self.JOB_ID, today))
        with backend.connect() as conn:
            status = conn.execute(
                "SELECT value FROM model_meta WHERE key = ?",
                (f"auto_schedule_{self.JOB_ID}_status",),
            ).fetchone()[0]
        self.assertEqual(status, "partial")


class AutoMonsterScanWatchdogTests(unittest.TestCase):
    def setUp(self):
        self._original_status = dict(server.monster_scan_status)

    def tearDown(self):
        server.monster_scan_status.clear()
        server.monster_scan_status.update(self._original_status)

    # scheduler_today 固定，讓「當天是否已完成」判斷在測試裡可預測。
    FAKE_TODAY = "2099-01-01"

    def _patches(self, scan_result):
        return [
            patch("server.scheduler_today", return_value=self.FAKE_TODAY),
            patch.object(backend, "train_model", side_effect=AssertionError("妖股排程不得重訓模型")),
            patch.object(backend, "refresh_radar_deployment_readiness", return_value={"formalReady": False}),
            patch("server.start_monster_scan_job", return_value=scan_result),
        ]

    def _run(self, scan_result={"ok": True, "started": True}):
        patches = self._patches(scan_result)
        for item in patches:
            item.start()
        try:
            return server.auto_monster_scan()
        finally:
            for item in patches:
                item.stop()

    def test_waits_for_scan_completion_and_reports_success(self):
        server.monster_scan_status.update({"running": False, "phase": "完成", "message": "測試完成訊息", "finishedAt": "", "trigger": "manual"})
        message = self._run()
        self.assertIn("掃描完成", message)
        self.assertIn("測試完成訊息", message)

    def test_scan_ends_in_failure_raises_so_run_auto_schedule_job_can_retry(self):
        server.monster_scan_status.update({"running": False, "phase": "失敗", "message": "模擬掃描失敗", "finishedAt": "", "trigger": "manual"})
        with self.assertRaises(RuntimeError) as ctx:
            self._run()
        self.assertIn("模擬掃描失敗", str(ctx.exception))

    def test_manual_scan_running_returns_retry_instead_of_marking_done(self):
        # 手動掃描進行中：舊版回「略過重複啟動」被標記成功，全市場自動掃描
        # 整天被跳過。新版回 RETRY sentinel，等手動掃描結束後再真正執行。
        server.monster_scan_status.update({"running": True, "phase": "掃描中", "trigger": "manual"})
        result = self._run()
        self.assertIs(result, server.AUTO_SCHEDULE_RETRY)

    def test_watchdog_timeout_returns_retry_for_later_settlement(self):
        # watchdog 逾時：舊版直接回成功訊息(之後背景失敗無人知曉)，
        # 新版回 RETRY，讓下一輪迴圈在掃描真正結束後依真實結果標記。
        server.monster_scan_status.update({"running": False, "phase": "掃描中", "message": "", "finishedAt": "", "trigger": "manual"})

        def start_and_mark_running(**kwargs):
            server.monster_scan_status["running"] = True
            return {"ok": True, "started": True}

        with patch("server.scheduler_today", return_value=self.FAKE_TODAY), \
             patch.object(backend, "train_model", side_effect=AssertionError("妖股排程不得重訓模型")), \
             patch("server.start_monster_scan_job", side_effect=start_and_mark_running), \
             patch.object(server, "MONSTER_SCAN_WATCHDOG_SECONDS", -1):
            result = server.auto_monster_scan()
        self.assertIs(result, server.AUTO_SCHEDULE_RETRY)

    def test_scan_not_started_race_returns_retry(self):
        server.monster_scan_status.update({"running": False, "phase": "", "finishedAt": "", "trigger": ""})
        result = self._run(scan_result={"ok": True, "started": False})
        self.assertIs(result, server.AUTO_SCHEDULE_RETRY)

    def test_finished_today_by_auto_reports_result_without_retraining(self):
        # 上一輪 watchdog 逾時後掃描由背景完成：重入時直接依真實結果回報，
        # 不重啟掃描；模型本來就由另一個 job 運行。
        server.monster_scan_status.update({
            "running": False, "phase": "完成", "message": "背景完成訊息",
            "finishedAt": f"{self.FAKE_TODAY} 15:15:00", "trigger": "auto-15:00",
        })
        with patch("server.scheduler_today", return_value=self.FAKE_TODAY), \
             patch.object(backend, "train_model") as mock_train, \
             patch.object(backend, "refresh_radar_deployment_readiness", return_value={"formalReady": False}), \
             patch("server.start_monster_scan_job") as mock_start:
            message = server.auto_monster_scan()
        self.assertIn("背景完成訊息", message)
        mock_train.assert_not_called()
        mock_start.assert_not_called()


class PremarketMonsterScanTests(unittest.TestCase):
    def setUp(self):
        self.original = dict(server.monster_scan_status)

    def tearDown(self):
        server.monster_scan_status.clear()
        server.monster_scan_status.update(self.original)

    def test_starts_rule_scan_and_returns_retry_until_background_finishes(self):
        server.monster_scan_status.update({
            "running": False, "phase": "", "finishedAt": "", "trigger": "",
        })
        with patch("server.scheduler_today", return_value="2026-07-13"), \
             patch("server.start_monster_scan_job", return_value={"started": True}) as start:
            result = server.auto_premarket_monster_scan()
        self.assertIs(result, server.AUTO_SCHEDULE_RETRY)
        self.assertEqual(start.call_args.kwargs["trigger"], "auto-08:50")
        self.assertEqual(start.call_args.kwargs["score_limit"], 100)

    def test_finished_scan_requires_same_day_valid_snapshot_before_success(self):
        server.monster_scan_status.update({
            "running": False,
            "phase": "完成",
            "message": "完成 100 檔",
            "finishedAt": "2026-07-13 09:01:00",
            "trigger": "auto-08:50",
        })
        with patch("server.scheduler_today", return_value="2026-07-13"), \
             patch.object(backend, "current_radar_decision_validity", return_value={
                 "validForTrading": True,
                 "selectedScanDate": "2026-07-13",
                 "summary": "雷達決策資料有效",
             }), \
             patch.object(backend, "refresh_radar_deployment_readiness", return_value={
                 "formalReady": False,
             }) as refresh:
            message = server.auto_premarket_monster_scan()
        self.assertIn("盤前妖股掃描完成", message)
        self.assertIn("觀察中", message)
        refresh.assert_called_once_with(as_of_date="2026-07-13")


class IndependentModelCycleTests(unittest.TestCase):
    """模型重訓/批量預測獨立運行，不再掛在妖股掃描前面。"""

    def test_model_cycle_trains_then_saves_predictions(self):
        conn = MagicMock()
        context = MagicMock()
        context.__enter__.return_value = conn
        with patch("server.scheduler_today", return_value="2099-01-01"), \
             patch.object(backend, "connect", return_value=context), \
             patch.object(backend, "load_model", return_value={
                 "trained_at": "2098-12-31 15:10:00"
             }), \
             patch("server.auto_model_training_symbols", return_value=(
                 ["2330"], {"holdings": [], "monster_liquid": ["2330"]}, []
             )), \
             patch.object(backend, "train_model", return_value={
                 "trained_at": "2099-01-01 15:10:00", "samples": 123
             }) as train, \
             patch.object(backend, "set_meta"), \
             patch.object(backend, "update_outcomes", return_value=7) as settle, \
             patch("server.auto_batch_save_predictions", return_value="批量 ML 預測：完成") as predict:
            message = server.auto_model_cycle()
        train.assert_called_once_with(["2330"])
        predict.assert_called_once_with()
        settle.assert_called_once_with()
        self.assertIn("獨立模型循環", message)

    def test_model_cycle_reentry_skips_same_day_training(self):
        conn = MagicMock()
        context = MagicMock()
        context.__enter__.return_value = conn
        with patch("server.scheduler_today", return_value="2099-01-01"), \
             patch.object(backend, "connect", return_value=context), \
             patch.object(backend, "load_model", return_value={
                 "trained_at": "2099-01-01 15:10:00",
                 "training_data_max_date": "2099-01-01",
             }), \
             patch.object(backend, "latest_complete_price_date", return_value="2099-01-01"), \
             patch.object(backend, "train_model") as train, \
             patch.object(backend, "update_outcomes", return_value=0) as settle, \
             patch("server.auto_batch_save_predictions", return_value="批量 ML 預測：完成") as predict:
            server.auto_model_cycle()
        train.assert_not_called()
        settle.assert_called_once_with()
        predict.assert_called_once_with()

    def test_model_cycle_retrains_when_same_day_model_missed_later_close(self):
        conn = MagicMock()
        context = MagicMock()
        context.__enter__.return_value = conn
        with patch("server.scheduler_today", return_value="2099-01-02"), \
             patch.object(backend, "connect", return_value=context), \
             patch.object(backend, "load_model", return_value={
                 "trained_at": "2099-01-02 15:10:00",
                 "training_data_max_date": "2099-01-01",
             }), \
             patch.object(backend, "latest_complete_price_date", return_value="2099-01-02"), \
             patch("server.auto_model_training_symbols", return_value=(
                 ["2330"], {"holdings": [], "monster_liquid": ["2330"]}, []
             )), \
             patch.object(backend, "train_model", return_value={
                 "trained_at": "2099-01-02 20:00:00",
                 "training_data_max_date": "2099-01-02",
                 "samples": 123,
             }) as train, \
             patch.object(backend, "set_meta"), \
             patch.object(backend, "update_outcomes", return_value=0), \
             patch("server.auto_batch_save_predictions", return_value="批量 ML 預測：完成"):
            server.auto_model_cycle()

        train.assert_called_once_with(["2330"])

    def test_rejected_training_is_recorded_as_attempt_but_not_as_active_model(self):
        conn = MagicMock()
        context = MagicMock()
        context.__enter__.return_value = conn
        with patch("server.scheduler_today", return_value="2099-01-01"), \
             patch.object(backend, "connect", return_value=context), \
             patch.object(backend, "load_model", return_value={
                 "trained_at": "2098-12-31 15:10:00"
             }), \
             patch("server.auto_model_training_symbols", return_value=(
                 ["2330"], {"holdings": [], "monster_liquid": ["2330"]}, []
             )), \
             patch.object(backend, "train_model", return_value={
                 "trained_at": "2099-01-01 15:10:00",
                 "samples": 50,
                 "gateRejected": True,
                 "gateReason": "訓練樣本縮水",
             }), \
             patch.object(backend, "set_meta") as set_meta, \
             patch.object(backend, "update_outcomes", return_value=0), \
             patch("server.auto_batch_save_predictions", return_value="批量 ML 預測：完成"):
            message = server.auto_model_cycle()

        written = {call.args[1]: call.args[2] for call in set_meta.call_args_list}
        self.assertEqual(written["last_auto_model_train_attempt_result"], "gate_rejected")
        self.assertNotIn("last_auto_model_train_independent", written)
        self.assertIn("被品質閘門拒絕", message)
        self.assertIn("沿用生效模型 2098-12-31 15:10:00", message)


class OfficialCloseSyncTests(unittest.TestCase):
    def setUp(self):
        server.official_close_sync_last_attempt = 0.0

    def tearDown(self):
        server.official_close_sync_last_attempt = 0.0

    @staticmethod
    def _snapshot(date_value, count=1800):
        return {
            "ok": True,
            "available": count,
            "written": count,
            "latestDates": {date_value: count},
            "errors": [],
        }

    @staticmethod
    def _market_day(is_trading_day=True):
        return {
            "known": True,
            "isTradingDay": is_trading_day,
            "reason": "官方開休市表未列為休市日" if is_trading_day else "端午節",
            "source": "TWSE annual holiday schedule",
        }

    def test_complete_current_day_snapshot_refreshes_radar(self):
        now = _weekday_now(16, 0)
        result = self._snapshot("2026-07-02")
        with patch.object(backend, "sync_official_daily_snapshot", return_value=result), \
             patch("server.official_market_day_status", return_value=self._market_day()), \
             patch.object(backend, "settle_portfolio_exit_history", return_value={"settled": 0}), \
             patch("server.record_official_close_sync_status", return_value=("2026-07-02", 1800)) as record, \
             patch("server.start_monster_scan_job", return_value={"started": True}) as start_scan, \
             patch.object(backend, "set_meta"):
            message = server.auto_official_close_sync(now=now, force=True)

        self.assertIn("2026-07-02", message)
        self.assertIn("妖股雷達已啟動重掃", message)
        record.assert_called_once()
        self.assertEqual(record.call_args.args[0], "ready")
        start_scan.assert_called_once()

    def test_previous_date_before_final_window_keeps_waiting(self):
        now = _weekday_now(16, 0)
        result = self._snapshot("2026-07-01")
        with patch.object(backend, "sync_official_daily_snapshot", return_value=result), \
             patch("server.official_market_day_status", return_value=self._market_day()), \
             patch("server.record_official_close_sync_status", return_value=("2026-07-01", 1800)) as record, \
             patch("server.start_monster_scan_job") as start_scan:
            outcome = server.auto_official_close_sync(now=now, force=True)

        self.assertIs(outcome, server.AUTO_SCHEDULE_RETRY)
        self.assertEqual(record.call_args.args[0], "waiting")
        start_scan.assert_not_called()

    def test_final_window_planned_trading_day_missing_close_fails_even_without_ticks(self):
        now = _weekday_now(18, 20)
        result = self._snapshot("2026-07-01")
        with patch.object(backend, "sync_official_daily_snapshot", return_value=result), \
             patch("server.official_market_day_status", return_value=self._market_day()), \
             patch("server.record_official_close_sync_status", return_value=("2026-07-01", 1800)) as record, \
             patch("server.has_today_intraday_market_activity", return_value=False):
            with self.assertRaisesRegex(RuntimeError, "今日應開市"):
                server.auto_official_close_sync(now=now, force=True)

        self.assertEqual(record.call_args_list[-1].args[0], "failed")

    def test_final_window_official_holiday_keeps_previous_trading_day(self):
        now = _weekday_now(18, 20)
        result = self._snapshot("2026-07-01")
        with patch.object(backend, "sync_official_daily_snapshot", return_value=result), \
             patch("server.official_market_day_status", return_value=self._market_day(False)), \
             patch("server.record_official_close_sync_status", return_value=("2026-07-01", 1800)) as record, \
             patch("server.has_today_intraday_market_activity", return_value=False):
            message = server.auto_official_close_sync(now=now, force=True)

        self.assertIn("確認", message)
        self.assertEqual(record.call_args_list[-1].args[0], "scheduled_holiday")

    def test_official_holiday_is_finalized_before_close_retry_window_ends(self):
        now = _weekday_now(15, 0)
        result = self._snapshot("2026-07-01")
        with patch.object(backend, "sync_official_daily_snapshot", return_value=result), \
             patch("server.official_market_day_status", return_value=self._market_day(False)), \
             patch("server.record_official_close_sync_status", return_value=("2026-07-01", 1800)) as record:
            message = server.auto_official_close_sync(now=now, force=True)

        self.assertIn("休市日", message)
        self.assertEqual(record.call_args_list[-1].args[0], "scheduled_holiday")

    def test_final_window_unknown_calendar_never_assumes_holiday(self):
        now = _weekday_now(18, 20)
        result = self._snapshot("2026-07-01")
        unknown = {"known": False, "isTradingDay": None, "reason": "API unavailable"}
        with patch.object(backend, "sync_official_daily_snapshot", return_value=result), \
             patch("server.official_market_day_status", return_value=unknown), \
             patch("server.record_official_close_sync_status", return_value=("2026-07-01", 1800)) as record:
            with self.assertRaisesRegex(RuntimeError, "無法確認"):
                server.auto_official_close_sync(now=now, force=True)

        self.assertEqual(record.call_args_list[-1].args[0], "failed")

    def test_current_date_partial_snapshot_fails_even_without_tick_activity(self):
        now = _weekday_now(18, 20)
        result = self._snapshot("2026-07-02", count=284)
        with patch.object(backend, "sync_official_daily_snapshot", return_value=result), \
             patch("server.official_market_day_status", return_value=self._market_day()), \
             patch("server.record_official_close_sync_status", return_value=("2026-07-02", 284)) as record, \
             patch("server.has_today_intraday_market_activity", return_value=False):
            with self.assertRaisesRegex(RuntimeError, "低於完整門檻"):
                server.auto_official_close_sync(now=now, force=True)

        self.assertEqual(record.call_args_list[-1].args[0], "failed")


class MissedScheduleWindowTests(unittest.TestCase):
    # 之前這裡借用真實的 job_id "0905_initial_filter" 做測試，setUp/tearDown
    # 的 _clear_meta() 會直接 DELETE 正式資料庫裡這個真實排程的
    # missed_notified_date/date/status 等 key——這支測試連到的 backend 是
    # 跨行程共用的正式 stock_system.sqlite3，不是隔離的測試資料庫。實際
    # 後果：2026-07-03 這天每次跑這個測試檔，都會把正式的
    # auto_schedule_0905_initial_filter_missed_notified_date 清掉，讓還在
    # 跑的正式伺服器背景執行緒(auto_schedule_worker)在下一輪(20秒後)重新
    # 判定「今天還沒通知過」，對使用者真的重複發送一模一樣的LINE錯過排程
    # 警示——當天使用者收到兩次一模一樣的通知，且LINE Messaging API方案
    # 一個月只有200則額度，被非必要的重複通知白白浪費。改用完全不存在
    # 於 AUTO_SCHEDULE_WINDOWS 的假 job_id，不會撞到任何真實排程。
    JOB_ID = "__test_missed_schedule_job__"

    def setUp(self):
        self._clear_meta()

    def tearDown(self):
        self._clear_meta()

    def _clear_meta(self):
        with backend.connect() as conn:
            for suffix in ("date", "status", "message", "at", "missed_notified_date"):
                conn.execute("DELETE FROM model_meta WHERE key = ?", (f"auto_schedule_{self.JOB_ID}_{suffix}",))
            conn.commit()

    def _only_test_job_window(self):
        # 把 AUTO_SCHEDULE_WINDOWS 換成只有這個測試在意的 job，避免其他真實
        # 排程（例如 0845_data_gap_repair）因為測試用的假時間點剛好也過了
        # 視窗，混進來一起觸發通知，干擾這裡對呼叫次數的斷言。
        return patch.object(server, "AUTO_SCHEDULE_WINDOWS", {self.JOB_ID: (9, 5, 9, 15)})

    def test_window_not_closed_yet_does_not_notify(self):
        now = _weekday_now(9, 10)  # 假視窗 09:05-09:15 還沒結束(借用真實0905_initial_filter的時段，方便理解)
        with self._only_test_job_window(), patch("server.send_line_message_via_api") as mock_send:
            server.check_missed_auto_schedule_windows(now)
        mock_send.assert_not_called()

    def test_window_closed_and_never_ran_notifies_once_not_repeatedly(self):
        now = _weekday_now(9, 20)  # 視窗 09:05-09:15 已經過了
        with self._only_test_job_window(), patch("server.send_line_message_via_api") as mock_send:
            server.check_missed_auto_schedule_windows(now)
            server.check_missed_auto_schedule_windows(now)
            server.check_missed_auto_schedule_windows(now)
        mock_send.assert_called_once()

    def test_window_closed_but_job_already_ran_does_not_notify(self):
        now = _weekday_now(9, 20)
        with patch("server.taipei_localtime", return_value=now):
            server.mark_auto_schedule(self.JOB_ID, "success", "測試已完成")
        with self._only_test_job_window(), patch("server.send_line_message_via_api") as mock_send:
            server.check_missed_auto_schedule_windows(now)
        mock_send.assert_not_called()

    def test_running_job_does_not_emit_false_missed_notification(self):
        now = _weekday_now(9, 20)
        server.auto_schedule_running[self.JOB_ID] = True
        try:
            with self._only_test_job_window(), patch("server.send_line_message_via_api") as mock_send:
                server.check_missed_auto_schedule_windows(now)
            mock_send.assert_not_called()
        finally:
            server.auto_schedule_running.pop(self.JOB_ID, None)

    def test_strategy_calibration_existing_rows_count_as_effective_run(self):
        # 2026-07-09 實際案例：策略校準資料已補寫進 strategy_calibration，
        # 但 auto_schedule_1705_strategy_calibration_date 沒補標，漏跑偵測
        # 仍發出「完全沒執行過」。這裡用未來假日期隔離正式資料。
        job_id = "1705_strategy_calibration"
        fake_today = "2099-01-02"
        fake_now = time.struct_time((2099, 1, 2, 17, 31, 0, 4, 2, 0))
        with backend.connect() as conn:
            conn.execute("DELETE FROM strategy_calibration WHERE calibration_date = ?", (fake_today,))
            for suffix in ("date", "status", "message", "at", "missed_notified_date"):
                conn.execute("DELETE FROM model_meta WHERE key = ?", (f"auto_schedule_{job_id}_{suffix}",))
            conn.execute("""
                INSERT INTO strategy_calibration (
                    calibration_date, strategy, suggested_action, reason, created_at, updated_at
                ) VALUES (?, 'unit_test_strategy', 'observe_more', 'unit test', ?, ?)
            """, (fake_today, f"{fake_today} 17:10:00", f"{fake_today} 17:10:00"))
            conn.commit()
        try:
            with patch.object(server, "AUTO_SCHEDULE_WINDOWS", {job_id: (17, 5, 17, 30)}), \
                 patch("server.send_line_message_via_api") as mock_send:
                server.check_missed_auto_schedule_windows(fake_now)
            self.assertTrue(server.auto_schedule_has_run(job_id, fake_today))
            mock_send.assert_not_called()
        finally:
            with backend.connect() as conn:
                conn.execute("DELETE FROM strategy_calibration WHERE calibration_date = ?", (fake_today,))
                for suffix in ("date", "status", "message", "at", "missed_notified_date"):
                    conn.execute("DELETE FROM model_meta WHERE key = ?", (f"auto_schedule_{job_id}_{suffix}",))
                conn.commit()

    def test_weekend_never_notifies(self):
        # 2026-07-04 是星期六(tm_wday=5)
        weekend_now = time.struct_time((2026, 7, 4, 9, 20, 0, 5, 185, 0))
        with patch("server.send_line_message_via_api") as mock_send:
            server.check_missed_auto_schedule_windows(weekend_now)
        mock_send.assert_not_called()

    def test_emergency_market_closure_never_reports_jobs_as_missed(self):
        now = _weekday_now(18, 31)
        market_day = {
            "known": True,
            "isTradingDay": False,
            "reason": "臺北市全日停止上班",
        }
        with self._only_test_job_window(), patch("server.send_line_message_via_api") as mock_send:
            server.check_missed_auto_schedule_windows(now, market_day=market_day)
        mock_send.assert_not_called()

    def test_notify_failure_does_not_raise(self):
        now = _weekday_now(9, 20)
        with patch("server.send_line_message_via_api", side_effect=RuntimeError("LINE 沒設定")):
            server.check_missed_auto_schedule_windows(now)  # 不該往外拋例外

    def test_notify_failure_does_not_mark_as_notified_so_next_tick_retries(self):
        # 對應這次修的bug：原本不論send_line_message_via_api成功與否都會先
        # mark_missed_schedule_notified()，LINE推播失敗(例如DNS暫時解析
        # 失敗，2026-07-03實測發生過)就會讓這則錯過通知永久消失、使用者
        # 永遠不會知道。改成只有真的送出成功才標記，這裡驗證：第一次送
        # 失敗後dedup旗標不該被設，下一輪(20秒後)還要能再次嘗試送出，
        # 送成功後才不再重試。
        now = _weekday_now(9, 20)
        today = server.scheduler_today(now)
        with self._only_test_job_window(), \
             patch("server.send_line_message_via_api", side_effect=RuntimeError("DNS暫時解析失敗")) as mock_send:
            server.check_missed_auto_schedule_windows(now)
        mock_send.assert_called_once()
        self.assertFalse(server.missed_schedule_already_notified(self.JOB_ID, today))

        with self._only_test_job_window(), \
             patch("server.send_line_message_via_api", return_value={"ok": True, "sent": True}) as mock_send2:
            server.check_missed_auto_schedule_windows(now)
            server.check_missed_auto_schedule_windows(now)
        mock_send2.assert_called_once()  # 送成功後才標記，第二次呼叫不該再重複送
        self.assertTrue(server.missed_schedule_already_notified(self.JOB_ID, today))

    def test_dedup_survives_process_restart_because_it_is_db_backed(self):
        # 對應這次修的 bug：之前的去重集合只存在記憶體(_auto_schedule_
        # missed_notified)，伺服器重啟後這個 set 會回到空集合，同一個已經
        # 通知過的錯過視窗會在下一輪被重新判定成「還沒通知過」而重複發送
        # 一模一樣的 LINE 警示。改成寫進 model_meta 後，這裡直接呼叫
        # mark_missed_schedule_notified() 模擬「重啟前已經通知過」，確認
        # 不需要任何記憶體狀態、單靠 DB 就能正確判斷「今天已經通知過了」。
        now = _weekday_now(9, 20)
        today = server.scheduler_today(now)
        server.mark_missed_schedule_notified(self.JOB_ID, today)
        with self._only_test_job_window(), patch("server.send_line_message_via_api") as mock_send:
            server.check_missed_auto_schedule_windows(now)
        mock_send.assert_not_called()


class StrategyCalibrationCatchupTests(unittest.TestCase):
    TRADING_DAY = {"known": True, "isTradingDay": True}

    def tearDown(self):
        server.auto_schedule_running.pop(server.STRATEGY_CALIBRATION_JOB_ID, None)

    def test_late_trading_day_start_runs_calibration_catchup(self):
        now = _weekday_now(21, 25)
        with patch("server.auto_schedule_has_run", return_value=False), \
             patch("server.run_auto_schedule_job") as run_job:
            started = server.catch_up_strategy_calibration_if_needed(now, self.TRADING_DAY)
        self.assertTrue(started)
        run_job.assert_called_once()
        self.assertEqual(run_job.call_args.args[0], server.STRATEGY_CALIBRATION_JOB_ID)
        self.assertTrue(callable(run_job.call_args.args[1]))

    def test_does_not_catch_up_before_window_has_ended(self):
        now = _weekday_now(17, 29)
        with patch("server.run_auto_schedule_job") as run_job:
            started = server.catch_up_strategy_calibration_if_needed(now, self.TRADING_DAY)
        self.assertFalse(started)
        run_job.assert_not_called()

    def test_does_not_catch_up_on_confirmed_market_closure(self):
        now = _weekday_now(21, 25)
        market_closed = {"known": True, "isTradingDay": False}
        with patch("server.run_auto_schedule_job") as run_job:
            started = server.catch_up_strategy_calibration_if_needed(now, market_closed)
        self.assertFalse(started)
        run_job.assert_not_called()

    def test_does_not_catch_up_when_today_already_has_output(self):
        now = _weekday_now(21, 25)
        with patch("server.auto_schedule_has_run", return_value=True), \
             patch("server.run_auto_schedule_job") as run_job:
            started = server.catch_up_strategy_calibration_if_needed(now, self.TRADING_DAY)
        self.assertFalse(started)
        run_job.assert_not_called()


class PaperSnapshotScheduleStatusTests(unittest.TestCase):
    SESSION_KEY = "__test_paper_session__"
    JOB_ID = "__test_paper_snapshot_job__"
    FAKE_TODAY = "2099-01-03"

    def setUp(self):
        self._clear_meta()

    def tearDown(self):
        self._clear_meta()

    def _clear_meta(self):
        with backend.connect() as conn:
            for suffix in ("date", "status", "message", "at"):
                conn.execute("DELETE FROM model_meta WHERE key = ?", (f"auto_schedule_{self.JOB_ID}_{suffix}",))
            for suffix in ("at", "saved", "checked", "errors"):
                conn.execute("DELETE FROM model_meta WHERE key = ?", (f"last_paper_signal_snapshot_{self.SESSION_KEY}_{suffix}",))
            conn.commit()

    def _patch_schedule(self):
        return patch.multiple(
            server,
            PAPER_SIGNAL_SESSIONS={self.SESSION_KEY: {"label": "測試時段", "time": "09:05"}},
            PAPER_SIGNAL_SESSION_JOBS={self.SESSION_KEY: self.JOB_ID},
            AUTO_SCHEDULE_WINDOWS={self.JOB_ID: (9, 5, 9, 15)},
        )

    def _status_at(self, hour, minute, market_day=None):
        now = time.struct_time((2099, 1, 3, hour, minute, 0, 4, 3, 0))
        market_day = market_day or {"known": True, "isTradingDay": True}
        with self._patch_schedule(), patch("server.official_market_day_status", return_value=market_day):
            return server.paper_signal_snapshot_schedule_status(now)[0]

    def test_before_window_is_upcoming(self):
        self.assertEqual(self._status_at(8, 55)["status"], "upcoming")

    def test_after_window_without_meta_is_missed(self):
        self.assertEqual(self._status_at(9, 20)["status"], "missed")

    def test_emergency_closure_is_shown_as_non_trading_day(self):
        market_day = {"known": True, "isTradingDay": False, "reason": "颱風休市"}
        self.assertEqual(self._status_at(9, 20, market_day=market_day)["status"], "market_closed")

    def test_snapshot_product_counts_as_done_even_when_auto_marker_is_missing(self):
        with backend.connect() as conn:
            backend.set_meta(conn, f"last_paper_signal_snapshot_{self.SESSION_KEY}_at", f"{self.FAKE_TODAY} 09:06:00")
            backend.set_meta(conn, f"last_paper_signal_snapshot_{self.SESSION_KEY}_saved", "12")
            backend.set_meta(conn, f"last_paper_signal_snapshot_{self.SESSION_KEY}_checked", "15")
            backend.set_meta(conn, f"last_paper_signal_snapshot_{self.SESSION_KEY}_errors", "1")
            conn.commit()
        row = self._status_at(9, 20)
        self.assertEqual(row["status"], "done")
        self.assertEqual(row["saved"], 12)
        self.assertEqual(row["checked"], 15)
        self.assertEqual(row["errors"], 1)


class AutoModelTrainingSymbolsIncludesSectorDiversifiedTests(unittest.TestCase):
    """對應這次修的 bug：14:30 排程觸發的重訓(auto_model_training_symbols)
    完全沒有把 DEFAULT_SYMBOLS(全產業多樣化基礎樣本)並進訓練池，只有在
    holdings/monster_liquid 都是空的極端情況才會當 fallback 用——導致這個
    排程重訓出來的模型，會用一份缺少全產業基礎樣本的窄化訓練池，覆蓋掉
    早上 daily_update.py 完整更新剛訓好的模型。"""

    def test_sector_diversified_symbols_included_even_when_other_sources_nonempty(self):
        from ml_backend import DEFAULT_SYMBOLS
        with patch.object(server.sinopac_backend, "holdings", return_value={"holdings": [{"code": "2317"}]}), \
             patch.object(server.backend, "liquid_monster_universe", return_value=["6488"]) as liquid:
            symbols, sources, errors = server.auto_model_training_symbols()
        liquid.assert_called_once_with(600)
        self.assertIn(DEFAULT_SYMBOLS[0], symbols)
        self.assertIn("2317", symbols)
        self.assertIn("6488", symbols)
        self.assertIn(DEFAULT_SYMBOLS[0], sources["sector_diversified"])
        self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main()
