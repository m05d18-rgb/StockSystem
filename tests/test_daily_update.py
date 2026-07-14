"""
daily_update.py 的回歸測試，對應今天加的機制：
  1. build_daily_training_symbols 不再讓動態候選覆蓋掉全產業基礎樣本(DEFAULT_SYMBOLS)。
  2. save_daily_brain_v2_snapshots 把每日 Brain v2 分量分數存下來，供未來驗證權重用。
  3. run_daily_data_integrity_check 把 data_integrity_check.py 接進每日排程，
     有問題才用 LINE 通知——測試用 mock 頂掉 data_integrity_check.run 跟
     send_line_message，確保跑測試不會真的打 LINE API 發訊息給使用者。

save_daily_brain_v2_snapshots 會真的呼叫 build_brain_decision + 寫入正式資料庫
(用真實股票代號 2330，因為需要真實價格資料才能算出分數)，測試結束後清理自己
寫入的那一筆，不留痕跡。
"""
import json
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from daily_update import (
    backfill_candidate_advanced_flow,
    backfill_pending_prediction_prices,
    backup_database, build_daily_training_symbols, normalize_code, run,
    data_integrity_status_payload, persist_data_integrity_repair_queue,
    run_daily_data_integrity_check,
    save_daily_brain_v2_snapshots, unique_codes,
)
from ml_backend import DEFAULT_SYMBOLS, backend


class NormalizeCodeTruncationTests(unittest.TestCase):
    """對應這次修的bug：normalize_code() 原本只過濾非數字字元，沒有像
    server.py的normalize_symbol()一樣截斷成4碼，兩邊對「合法股票代碼」的
    定義不一致。"""

    def test_truncates_to_four_digits(self):
        self.assertEqual(normalize_code("23300"), "2330")
        self.assertEqual(normalize_code("123456"), "1234")

    def test_short_code_unaffected(self):
        self.assertEqual(normalize_code("2330"), "2330")

    def test_non_digit_characters_stripped_before_truncation(self):
        self.assertEqual(normalize_code("TW-2330-X"), "2330")

    def test_delisted_symbols_are_excluded_from_training_inputs(self):
        self.assertEqual(unique_codes(["2888", "6806", "2330"]), ["2330"])


class TrainingSymbolUnionTests(unittest.TestCase):
    def test_default_symbols_always_included_even_with_dynamic_candidates(self):
        result = build_daily_training_symbols(["2302", "2329"])
        for symbol in DEFAULT_SYMBOLS:
            self.assertIn(symbol, result["symbols"])

    def test_holdings_are_included(self):
        result = build_daily_training_symbols(["9999"])
        self.assertIn("9999", result["symbols"])

    def test_never_returns_empty(self):
        # 即使持股與動態候選都抓不到，DEFAULT_SYMBOLS 這個基礎樣本永遠都在，
        # 不該再需要額外的 fallback 分支。
        result = build_daily_training_symbols([])
        self.assertGreaterEqual(len(result["symbols"]), len(DEFAULT_SYMBOLS))


class BrainV2SnapshotTests(unittest.TestCase):
    TEST_SYMBOL = "2330"

    def tearDown(self):
        with backend.connect() as conn:
            conn.execute(
                "DELETE FROM brain_v2_snapshots WHERE symbol = ? AND context = 'monster'",
                (self.TEST_SYMBOL,),
            )
            conn.commit()

    def test_saves_snapshot_with_all_components(self):
        with backend.connect() as conn:
            conn.execute(
                "DELETE FROM brain_v2_snapshots WHERE symbol = ? AND context = 'monster'",
                (self.TEST_SYMBOL,),
            )
            conn.commit()
        result = save_daily_brain_v2_snapshots(symbols=[self.TEST_SYMBOL])
        self.assertEqual(result["saved"], 1)
        self.assertEqual(result["errors"], [])
        with backend.connect() as conn:
            conn.row_factory = None
            row = conn.execute(
                "SELECT v2_score, formal_model_score, kline_score, required_component_failures "
                "FROM brain_v2_snapshots WHERE symbol = ? AND context = 'monster'",
                (self.TEST_SYMBOL,),
            ).fetchone()
        self.assertIsNotNone(row)
        v2_score, formal_model_score, kline_score, required_failures = row
        self.assertIsNotNone(v2_score)
        # 2026-07-09 Brain 拆模型:決策改由特徵函式直算、不跑模型推論,formalModel 那個
        # 「僅供參考、權重0」的模型機率分不再計算,所以快照的 formal_model_score 為 None。
        # 型態量能分(v2_score/kline_score)照常算——這才是實際進決策的分數。
        self.assertIsNone(formal_model_score)
        self.assertIsNotNone(kline_score)
        self.assertIn("[", required_failures)  # 是個 JSON 陣列字串

    def test_repeated_call_does_not_duplicate_row(self):
        save_daily_brain_v2_snapshots(symbols=[self.TEST_SYMBOL])
        save_daily_brain_v2_snapshots(symbols=[self.TEST_SYMBOL])
        with backend.connect() as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM brain_v2_snapshots WHERE symbol = ? AND context = 'monster'",
                (self.TEST_SYMBOL,),
            ).fetchone()[0]
        self.assertEqual(count, 1)


class DataIntegrityCheckNotifyTests(unittest.TestCase):
    """run_daily_data_integrity_check：全部正常就不通知；只有系統級故障(模型沒重訓/
    重訓閘門/結算異常)才發 LINE，個股資料品質問題不再發 LINE(2026-07-08 依需求關閉)；
    檢查本身或 LINE 推播出錯都不能讓整個每日更新掛掉。全部用 mock，不會真的打 LINE API。"""

    def setUp(self):
        self.queue_patcher = patch(
            "daily_update.persist_data_integrity_repair_queue",
            return_value={"ok": True, "symbols": [], "count": 0},
        )
        self.queue_patcher.start()

    def tearDown(self):
        self.queue_patcher.stop()

    def test_all_ok_does_not_send_notification(self):
        with patch("daily_update.data_integrity_check.run", return_value={
            "ok": True, "total": 5, "problemCount": 0, "problems": [],
            "modelFreshness": {"ok": True},
        }), patch("daily_update.send_line_message") as mock_send:
            result = run_daily_data_integrity_check(["2330"])
        mock_send.assert_not_called()
        self.assertFalse(result["notified"])

    def test_data_quality_problems_alone_do_not_send_line(self):
        # 2026-07-08：只有個股資料品質問題(無系統級故障)不再發 LINE,只記在結果供面板檢視。
        fake_result = {
            "ok": False, "total": 748, "problemCount": 27,
            "problems": [{"symbol": "9999", "latestDate": "2026-06-30", "issues": ["最新一筆 close=0 異常"]}],
            "modelFreshness": {"ok": True},
        }
        with patch("daily_update.data_integrity_check.run", return_value=fake_result), \
             patch("daily_update.send_line_message") as mock_send:
            result = run_daily_data_integrity_check(["9999"])
        mock_send.assert_not_called()
        self.assertFalse(result["notified"])
        self.assertEqual(result.get("notifySkipped"), "data_quality_only")

    def test_stale_model_is_mentioned_in_notification(self):
        fake_result = {
            "ok": False, "total": 3, "problemCount": 0, "problems": [],
            "modelFreshness": {"ok": False, "issue": "模型已經 5 天沒重訓（門檻 2 天）"},
        }
        with patch("daily_update.data_integrity_check.run", return_value=fake_result), \
             patch("daily_update.send_line_message", return_value={"ok": True, "sent": True}) as mock_send:
            run_daily_data_integrity_check(["2330"])
        sent_message = mock_send.call_args[0][0]
        self.assertIn("模型已經 5 天沒重訓", sent_message)

    def test_line_send_failure_does_not_propagate(self):
        # 需帶系統級故障(模型沒重訓)才會走到發 LINE;送出失敗不能拖垮每日更新。
        fake_result = {
            "ok": False, "total": 1, "problemCount": 1,
            "problems": [{"symbol": "9999", "latestDate": None, "issues": ["完全沒有價格資料"]}],
            "modelFreshness": {"ok": False, "issue": "模型已經 9 天沒重訓（門檻 2 天）"},
        }
        with patch("daily_update.data_integrity_check.run", return_value=fake_result), \
             patch("daily_update.send_line_message", side_effect=RuntimeError("LINE Messaging API is not configured")):
            result = run_daily_data_integrity_check(["9999"])  # 不該丟例外
        self.assertFalse(result["notified"])
        self.assertIn("error", result["notifyResult"])

    def test_integrity_check_exception_is_caught(self):
        with patch("daily_update.data_integrity_check.run", side_effect=RuntimeError("db locked")):
            result = run_daily_data_integrity_check(["2330"])  # 不該丟例外
        self.assertFalse(result["ok"])
        self.assertFalse(result["notified"])
        self.assertIn("db locked", result["error"])


class DataIntegrityRepairQueueTests(unittest.TestCase):
    def test_real_integrity_problems_are_persisted_for_the_repair_job(self):
        conn = MagicMock()
        context = MagicMock()
        context.__enter__.return_value = conn
        context.__exit__.return_value = False
        result = {
            "checkedAt": "2026-07-13 08:41:00",
            "expectedLatestDate": "2026-07-13",
            "total": 4,
            "problems": [{"symbol": "2258"}, {"symbol": "6831"}, {"symbol": "2258"}],
            "modelIneligible": [{"symbol": "7566", "eligibilityIssues": ["marginSourceCoverageOk"]}],
        }
        with patch("daily_update.backend.connect", return_value=context), \
             patch("daily_update.backend.set_meta") as set_meta, \
             patch("daily_update._atomic_write_text") as atomic_write:
            queued = persist_data_integrity_repair_queue(result)

        self.assertEqual(queued["symbols"], ["2258", "6831"])
        self.assertEqual(queued["count"], 2)
        written = {call.args[1]: call.args[2] for call in set_meta.call_args_list}
        self.assertEqual(written["last_data_integrity_problem_symbols_json"], '["2258","6831"]')
        self.assertEqual(written["last_data_integrity_model_ineligible_count"], "1")
        current = json.loads(written["last_data_integrity_result_json"])
        self.assertEqual(current["problemCount"], 3)
        self.assertEqual(current["healthyCount"], 1)
        self.assertEqual(current["modelIneligibleCount"], 1)
        atomic_write.assert_called_once()

    def test_status_payload_keeps_model_eligibility_separate_from_pipeline_health(self):
        status = data_integrity_status_payload({
            "ok": True,
            "checkedAt": "2026-07-13 22:00:00",
            "expectedLatestDate": "2026-07-13",
            "total": 2,
            "problems": [],
            "modelIneligible": [
                {"symbol": "7566", "eligibilityIssues": ["marginSourceCoverageOk"]},
            ],
        })

        self.assertTrue(status["dataPipelineOk"])
        self.assertEqual(status["problemCount"], 0)
        self.assertEqual(status["modelIneligibleCount"], 1)


class RunContinuesAfterFullDailyUpdateFailureTests(unittest.TestCase):
    """run() 的 backend.full_daily_update() 呼叫本身沒有 try/except 的話，
    一旦拋例外（實測發生過：database is locked）會直接跳到最外層 except，
    導致 save_daily_brain_v2_snapshots/run_daily_data_integrity_check(唯一
    會用 LINE 通知使用者「今天資料有問題」的地方)完全不會被呼叫到。全部
    mock 掉，不觸碰真實資料庫/LINE API。"""

    def test_full_daily_update_exception_still_runs_downstream_notification_steps(self):
        with patch("daily_update.load_sinopac_symbols", side_effect=RuntimeError("no sinopac")), \
             patch("daily_update.backend.full_daily_update", side_effect=RuntimeError("database is locked")), \
             patch("daily_update.backfill_candidate_advanced_flow", return_value={"ok": False, "error": "database is locked"}), \
             patch("daily_update.backfill_pending_prediction_prices", return_value={"ok": False, "error": "database is locked"}), \
             patch("daily_update.backup_database", return_value={"ok": True, "skipped": True}), \
             patch("daily_update.save_daily_brain_v2_snapshots", return_value={"saved": 0, "total": 0, "errors": []}) as mock_snapshots, \
             patch("daily_update.run_daily_data_integrity_check", return_value={"ok": True, "notified": False}) as mock_integrity, \
             patch("daily_update.save_run_log", return_value="fake_path"), \
             patch("daily_update.set_daily_meta"):
            payload = run()
        mock_snapshots.assert_called_once()
        mock_integrity.assert_called_once()
        self.assertFalse(payload["ok"])
        self.assertIn("database is locked", payload["error"])

    def test_full_daily_update_success_still_marks_ok_true(self):
        fake_result = {
            "ok": True, "updatedRows": {}, "priceFetchErrors": {}, "market": {},
            "model": {}, "trainingSymbols": [], "trainingSymbolCount": 0,
            "portfolioSymbols": [], "portfolioSymbolCount": 0,
            "predictions": [], "predictionErrors": [], "updatedOutcomes": {},
        }
        with patch("daily_update.load_sinopac_symbols", side_effect=RuntimeError("no sinopac")), \
             patch("daily_update.backend.full_daily_update", return_value=fake_result), \
             patch("daily_update.backfill_candidate_advanced_flow", return_value={"ok": True, "skipped": True}), \
             patch("daily_update.backfill_pending_prediction_prices", return_value={"ok": True, "skipped": True}), \
             patch("daily_update.backup_database", return_value={"ok": True, "skipped": True}), \
             patch("daily_update.save_daily_brain_v2_snapshots", return_value={"saved": 0, "total": 0, "errors": []}) as mock_snapshots, \
             patch("daily_update.run_daily_data_integrity_check", return_value={"ok": True, "notified": False}) as mock_integrity, \
             patch("daily_update.save_run_log", return_value="fake_path"), \
             patch("daily_update.set_daily_meta"):
            payload = run()
        mock_snapshots.assert_called_once()
        mock_integrity.assert_called_once()
        self.assertTrue(payload["ok"])
        self.assertIsNone(payload["error"])

    def test_partial_official_daily_snapshot_marks_daily_run_failed(self):
        fake_result = {
            "ok": False,
            "error": "官方全市場日K同步未通過覆蓋新鮮度檢查",
            "updatedRows": {}, "priceFetchErrors": {}, "market": {}, "model": {},
            "trainingSymbols": [], "trainingSymbolCount": 0,
            "portfolioSymbols": [], "portfolioSymbolCount": 0,
            "predictions": [], "predictionErrors": [], "updatedOutcomes": {},
        }
        with patch("daily_update.load_sinopac_symbols", side_effect=RuntimeError("no sinopac")), \
             patch("daily_update.backend.full_daily_update", return_value=fake_result), \
             patch("daily_update.backfill_candidate_advanced_flow", return_value={"ok": True, "skipped": True}), \
             patch("daily_update.backfill_pending_prediction_prices", return_value={"ok": True, "skipped": True}), \
             patch("daily_update.backup_database", return_value={"ok": True, "skipped": True}), \
             patch("daily_update.save_daily_brain_v2_snapshots", return_value={"saved": 0, "total": 0, "errors": []}), \
             patch("daily_update.run_daily_data_integrity_check", return_value={"ok": True, "notified": False}), \
             patch("daily_update.save_run_log", return_value="fake_path"), \
             patch("daily_update.set_daily_meta"):
            payload = run()
        self.assertFalse(payload["ok"])
        self.assertIn("官方全市場日K", payload["error"])


class FullDailyOfficialSnapshotTests(unittest.TestCase):
    """每日更新必須先同步官方全市場日K，且覆蓋不完整時不能偽裝成成功。"""

    def _run_full_update(self, daily_ok):
        freshness = {
            "ok": daily_ok,
            "sources": [
                {"name": "個股日K", "ok": daily_ok, "detail": "測試覆蓋"},
            ],
        }
        with patch.object(backend, "sync_official_daily_snapshot", return_value={
            "ok": True, "available": 6399, "written": 6394, "latestDates": {"2026-07-09": 6399}, "errors": [],
        }) as mock_sync, \
             patch.object(backend, "data_freshness", return_value=freshness), \
             patch.object(backend, "update_market_data", return_value={}), \
             patch.object(backend, "update_prices", return_value={"2330": 755}), \
             patch.object(backend, "load_model", side_effect=AssertionError("純資料更新不得載入模型")), \
             patch.object(backend, "predict_symbol", side_effect=AssertionError("純資料更新不得產生模型預測")), \
             patch.object(backend, "update_outcomes", side_effect=AssertionError("模型結算應由獨立模型循環執行")):
            result = backend.full_daily_update(
                symbols=["2330"], training_symbols=["2330"], train=False,
            )
        mock_sync.assert_called_once_with()
        return result

    def test_full_daily_update_requires_complete_official_daily_coverage(self):
        result = self._run_full_update(daily_ok=False)
        self.assertFalse(result["ok"])
        self.assertIn("官方全市場日K", result["error"])
        self.assertFalse(result["officialSnapshot"]["coverage"]["ok"])

    def test_full_daily_update_exposes_successful_official_snapshot(self):
        result = self._run_full_update(daily_ok=True)
        self.assertTrue(result["ok"])
        self.assertEqual(result["officialSnapshot"]["written"], 6394)
        self.assertTrue(result["officialSnapshot"]["coverage"]["ok"])


class TrainingPoolErrorNotificationTests(unittest.TestCase):
    """對應這次修的bug：monster_liquid抓取失敗(FinMind額度用盡/DB鎖死)時，
    訓練池會靜默退化成只剩sector_diversified∪holdings，模型訓練仍會「成功」
    跑完，之前完全不會觸發任何告警。現在training_scope["errors"]非空時要
    送一次LINE通知。全部mock掉，不觸碰真實資料庫/LINE API。"""

    def _run_with_training_errors(self, training_errors):
        fake_training_scope = {
            "symbols": ["2330"], "sources": {}, "errors": training_errors,
        }
        fake_result = {
            "ok": True, "updatedRows": {}, "priceFetchErrors": {}, "market": {},
            "model": {}, "trainingSymbols": [], "trainingSymbolCount": 0,
            "portfolioSymbols": [], "portfolioSymbolCount": 0,
            "predictions": [], "predictionErrors": [], "updatedOutcomes": {},
        }
        with patch("daily_update.load_sinopac_symbols", side_effect=RuntimeError("no sinopac")), \
             patch("daily_update.build_daily_training_symbols", return_value=fake_training_scope), \
             patch("daily_update.backend.full_daily_update", return_value=fake_result), \
             patch("daily_update.backfill_candidate_advanced_flow", return_value={"ok": True, "skipped": True}), \
             patch("daily_update.backfill_pending_prediction_prices", return_value={"ok": True, "skipped": True}), \
             patch("daily_update.backup_database", return_value={"ok": True, "skipped": True}), \
             patch("daily_update.save_daily_brain_v2_snapshots", return_value={"saved": 0, "total": 0, "errors": []}), \
             patch("daily_update.run_daily_data_integrity_check", return_value={"ok": True, "notified": False}), \
             patch("daily_update.save_run_log", return_value="fake_path"), \
             patch("daily_update.set_daily_meta"), \
             patch("daily_update.send_line_message", return_value={"ok": True, "sent": True}) as mock_send:
            run()
        return mock_send

    def test_training_pool_errors_trigger_line_notification(self):
        mock_send = self._run_with_training_errors(["monster_liquid: FinMind quota blocked"])
        mock_send.assert_called_once()
        self.assertIn("monster_liquid", mock_send.call_args[0][0])

    def test_no_training_pool_errors_does_not_notify(self):
        mock_send = self._run_with_training_errors([])
        mock_send.assert_not_called()

    def test_notification_send_failure_does_not_propagate(self):
        fake_training_scope = {"symbols": ["2330"], "sources": {}, "errors": ["monster_liquid: boom"]}
        fake_result = {
            "ok": True, "updatedRows": {}, "priceFetchErrors": {}, "market": {},
            "model": {}, "trainingSymbols": [], "trainingSymbolCount": 0,
            "portfolioSymbols": [], "portfolioSymbolCount": 0,
            "predictions": [], "predictionErrors": [], "updatedOutcomes": {},
        }
        with patch("daily_update.load_sinopac_symbols", side_effect=RuntimeError("no sinopac")), \
             patch("daily_update.build_daily_training_symbols", return_value=fake_training_scope), \
             patch("daily_update.backend.full_daily_update", return_value=fake_result), \
             patch("daily_update.backfill_candidate_advanced_flow", return_value={"ok": True, "skipped": True}), \
             patch("daily_update.backfill_pending_prediction_prices", return_value={"ok": True, "skipped": True}), \
             patch("daily_update.backup_database", return_value={"ok": True, "skipped": True}), \
             patch("daily_update.save_daily_brain_v2_snapshots", return_value={"saved": 0, "total": 0, "errors": []}), \
             patch("daily_update.run_daily_data_integrity_check", return_value={"ok": True, "notified": False}), \
             patch("daily_update.save_run_log", return_value="fake_path"), \
             patch("daily_update.set_daily_meta"), \
             patch("daily_update.send_line_message", side_effect=RuntimeError("LINE not configured")):
            payload = run()  # 不該丟例外
        self.assertTrue(payload["ok"])


class RunContinuesAfterBrainV2SnapshotFailureTests(unittest.TestCase):
    """對應這次修的bug：save_daily_brain_v2_snapshots()呼叫本身沒有獨立
    try/except，一旦拋例外會直接跳到最外層except，導致run_daily_data_
    integrity_check(唯一會用LINE通知使用者「今天資料有問題」的地方)完全
    不會被呼叫到——跟RunContinuesAfterFullDailyUpdateFailureTests是同一種
    風險模式，這裡驗證save_daily_brain_v2_snapshots的姊妹呼叫也有同樣保護。"""

    def test_brain_v2_snapshot_exception_still_runs_integrity_check(self):
        fake_result = {
            "ok": True, "updatedRows": {}, "priceFetchErrors": {}, "market": {},
            "model": {}, "trainingSymbols": [], "trainingSymbolCount": 0,
            "portfolioSymbols": [], "portfolioSymbolCount": 0,
            "predictions": [], "predictionErrors": [], "updatedOutcomes": {},
        }
        with patch("daily_update.load_sinopac_symbols", side_effect=RuntimeError("no sinopac")), \
             patch("daily_update.backend.full_daily_update", return_value=fake_result), \
             patch("daily_update.backfill_candidate_advanced_flow", return_value={"ok": True, "skipped": True}), \
             patch("daily_update.backfill_pending_prediction_prices", return_value={"ok": True, "skipped": True}), \
             patch("daily_update.backup_database", return_value={"ok": True, "skipped": True}), \
             patch("daily_update.save_daily_brain_v2_snapshots", side_effect=RuntimeError("brain engine broken")), \
             patch("daily_update.run_daily_data_integrity_check", return_value={"ok": True, "notified": False}) as mock_integrity, \
             patch("daily_update.save_run_log", return_value="fake_path"), \
             patch("daily_update.set_daily_meta"):
            payload = run()  # 不該丟例外
        mock_integrity.assert_called_once()
        self.assertTrue(payload["ok"])
        self.assertIn("brain engine broken", payload["result"]["brainV2Snapshots"]["errors"][0])


class BackupDatabaseTests(unittest.TestCase):
    """資料庫每日備份(backup_database)：這個專案沒有git，600MB+的正式資料庫
    又放在OneDrive同步資料夾裡，OneDrive同步衝突弄壞SQLite檔案就全沒了。
    全部用暫存目錄裡的小型假資料庫測試，不觸碰真實stock_system.sqlite3。"""

    def setUp(self):
        import sqlite3
        import tempfile
        from pathlib import Path
        self.tmp_dir = Path(tempfile.mkdtemp(prefix="db_backup_test_"))
        self.source_path = self.tmp_dir / "source.sqlite3"
        self.backup_dir = self.tmp_dir / "backups"
        conn = sqlite3.connect(self.source_path)
        conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, val TEXT)")
        conn.execute("INSERT INTO t (val) VALUES ('hello')")
        conn.commit()
        conn.close()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_creates_consistent_backup(self):
        import sqlite3
        from pathlib import Path
        result = backup_database(source_path=self.source_path, backup_dir=self.backup_dir)
        self.assertTrue(result["ok"])
        self.assertFalse(result["skipped"])
        backup_path = Path(result["path"])
        self.assertTrue(backup_path.exists())
        conn = sqlite3.connect(backup_path)
        row = conn.execute("SELECT val FROM t").fetchone()
        conn.close()
        self.assertEqual(row[0], "hello")

    def test_same_day_second_call_skips(self):
        backup_database(source_path=self.source_path, backup_dir=self.backup_dir)
        result = backup_database(source_path=self.source_path, backup_dir=self.backup_dir)
        self.assertTrue(result["ok"])
        self.assertTrue(result["skipped"])

    def test_rotation_removes_oldest_beyond_keep(self):
        # 先偽造3個舊備份檔(檔名日期比今天舊)，再備份今天的，keep=3之下
        # 最舊的那個應該被清掉，總數維持3。
        self.backup_dir.mkdir(parents=True)
        for tag in ("20200101", "20200102", "20200103"):
            (self.backup_dir / f"stock_system_{tag}.sqlite3").write_bytes(b"old")
        result = backup_database(source_path=self.source_path, backup_dir=self.backup_dir, keep=3)
        self.assertTrue(result["ok"])
        remaining = sorted(p.name for p in self.backup_dir.glob("stock_system_*.sqlite3"))
        self.assertEqual(len(remaining), 3)
        self.assertNotIn("stock_system_20200101.sqlite3", remaining)

    def test_failure_returns_error_without_raising(self):
        result = backup_database(
            source_path=self.tmp_dir / "does_not_exist.sqlite3",
            backup_dir=self.backup_dir,
        )
        self.assertFalse(result["ok"])
        self.assertIn("error", result)

    def test_no_leftover_temp_file_after_success(self):
        backup_database(source_path=self.source_path, backup_dir=self.backup_dir)
        leftovers = list(self.backup_dir.glob("*.tmp"))
        self.assertEqual(leftovers, [])


class CandidateAdvancedFlowBackfillTests(unittest.TestCase):
    """候選股主力分點自動補齊(backfill_candidate_advanced_flow):每日更新尾端把
    妖股候選的進階資金流補進 DB,讓面板不用手動按「補齊候選股資金流」。全部 mock
    backend,不觸碰真實 FinMind/資料庫。"""

    def test_still_attempts_backfill_when_main_update_failed(self):
        # 主更新失敗不能把另一條可用的補抓路徑直接標成 skipped。
        with patch(
            "daily_update.backend.list_monster_scores",
            return_value={"candidates": [{"symbol": "1519"}]},
        ) as mock_scores, patch(
            "daily_update.backend.update_prices", return_value={"1519": 1}
        ) as mock_update:
            result = backfill_candidate_advanced_flow({"error": "db locked", "symbols": []})
        self.assertTrue(result["ok"])
        self.assertNotIn("skipped", result)
        mock_scores.assert_called_once()
        mock_update.assert_called_once()

    def test_backfills_candidates_excluding_holdings_with_extended(self):
        with patch("daily_update.backend.list_monster_scores",
                   return_value={"candidates": [{"symbol": "2330"}, {"symbol": "1519"}, {"symbol": "6188"}]}), \
             patch("daily_update.backend.update_prices", return_value={"1519": 1, "6188": 1}) as mock_update:
            result = backfill_candidate_advanced_flow({"error": None, "symbols": ["2330"]}, force_refresh=True)
        self.assertTrue(result["ok"])
        self.assertEqual(result["symbols"], 2, "持股 2330 要被排除，只補 1519/6188")
        args, kwargs = mock_update.call_args
        self.assertEqual(sorted(args[0]), ["1519", "6188"])
        self.assertTrue(kwargs["include_extended"], "必須帶 include_extended 才會抓主力分點")
        self.assertTrue(kwargs["force_refresh"])

    def test_no_candidates_skips_update(self):
        with patch("daily_update.backend.list_monster_scores", return_value={"candidates": []}), \
             patch("daily_update.backend.update_prices") as mock_update:
            result = backfill_candidate_advanced_flow({"error": None, "symbols": ["2330"]})
        self.assertTrue(result["ok"])
        self.assertEqual(result["symbols"], 0)
        mock_update.assert_not_called()

    def test_backfill_exception_is_caught_not_raised(self):
        with patch("daily_update.backend.list_monster_scores",
                   return_value={"candidates": [{"symbol": "1519"}]}), \
             patch("daily_update.backend.update_prices", side_effect=RuntimeError("FinMind quota blocked")):
            result = backfill_candidate_advanced_flow({"error": None, "symbols": []})
        self.assertFalse(result["ok"])
        self.assertIn("FinMind quota blocked", result["error"])


class _FakeConn:
    """模擬 backend.connect() 的 context manager,execute().fetchall() 回固定 rows。"""
    def __init__(self, rows):
        self._rows = rows
    def __enter__(self):
        return self
    def __exit__(self, *a):
        return False
    def execute(self, *a, **k):
        rows = self._rows
        class _Cur:
            def fetchall(self):
                return rows
        return _Cur()


class BackfillPendingPredictionPricesTests(unittest.TestCase):
    """未結算預測補價(backfill_pending_prediction_prices):把掉出名單、價格斷更的已預測股
    補回近期日K再結算,消除「殭屍預測永遠 hit=NULL」偏斜、讓命中率戰績又快又真累積。
    全部 mock backend,不觸碰真實 FinMind/DB。"""

    def test_still_attempts_pending_prices_when_main_update_failed(self):
        with patch("daily_update.backend.connect", return_value=_FakeConn([("1591",)])) as mock_conn, \
             patch("daily_update.backend.update_prices", return_value={"1591": 5}) as mock_up, \
             patch("daily_update.backend.update_outcomes", return_value=1) as mock_uo:
            result = backfill_pending_prediction_prices({"error": "db locked", "symbols": []})
        self.assertTrue(result["ok"])
        self.assertNotIn("skipped", result)
        mock_conn.assert_called_once()
        mock_up.assert_called_once()
        mock_uo.assert_called_once()

    def test_backfills_pending_excluding_holdings_daily_close_only(self):
        rows = [("1591",), ("1293",), ("2330",)]  # 2330 是持股,要被排除
        with patch("daily_update.backend.connect", return_value=_FakeConn(rows)), \
             patch("daily_update.backend.update_prices", return_value={"1591": 5, "1293": 5}) as mock_up, \
             patch("daily_update.backend.update_outcomes", return_value=2) as mock_uo:
            result = backfill_pending_prediction_prices({"error": None, "symbols": ["2330"]}, force_refresh=True)
        self.assertTrue(result["ok"])
        self.assertEqual(result["symbols"], 2, "持股 2330 要被排除,只補 1591/1293")
        args, kwargs = mock_up.call_args
        self.assertEqual(sorted(args[0]), ["1293", "1591"])
        self.assertFalse(kwargs["include_extended"], "結算只需日K收盤,不抓擴充資料集(壓額度)")
        self.assertFalse(kwargs["refresh_info"])
        self.assertTrue(kwargs["force_refresh"])
        mock_uo.assert_called_once()  # 補完價要當場結算
        self.assertEqual(result["settlement"], 2)

    def test_no_pending_skips_update(self):
        with patch("daily_update.backend.connect", return_value=_FakeConn([])), \
             patch("daily_update.backend.update_prices") as mock_up, \
             patch("daily_update.backend.update_outcomes") as mock_uo:
            result = backfill_pending_prediction_prices({"error": None, "symbols": []})
        self.assertTrue(result["ok"])
        self.assertEqual(result["symbols"], 0)
        mock_up.assert_not_called()
        mock_uo.assert_not_called()

    def test_exception_is_caught_not_raised(self):
        with patch("daily_update.backend.connect", return_value=_FakeConn([("1591",)])), \
             patch("daily_update.backend.update_prices", side_effect=RuntimeError("FinMind quota blocked")), \
             patch("daily_update.backend.update_outcomes") as mock_uo:
            result = backfill_pending_prediction_prices({"error": None, "symbols": []})
        self.assertFalse(result["ok"])
        self.assertIn("FinMind quota blocked", result["error"])
        mock_uo.assert_not_called()

    def test_cap_truncates_pending_by_env(self):
        rows = [(f"{9000 + i}",) for i in range(50)]  # 50 檔假 pending
        with patch("daily_update.backend.connect", return_value=_FakeConn(rows)), \
             patch.dict("os.environ", {"SETTLEMENT_BACKFILL_MAX_SYMBOLS": "10"}), \
             patch("daily_update.backend.update_prices", return_value={}) as mock_up, \
             patch("daily_update.backend.update_outcomes", return_value=0):
            result = backfill_pending_prediction_prices({"error": None, "symbols": []})
        self.assertEqual(result["symbols"], 10, "上限 10 應把 50 檔截斷到 10")
        self.assertEqual(len(mock_up.call_args[0][0]), 10)


if __name__ == "__main__":
    unittest.main()
