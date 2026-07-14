"""
server.py 每日盤後 LINE 摘要(build_daily_digest_message/auto_daily_digest)
的回歸測試。這個功能每交易日只送一則彙整訊息(月額度 200 則的節約設計)，
要驗證：(1)正常路徑各段內容都在，(2)任何一段的資料來源掛掉只讓那段省略/
顯示失敗，整則摘要照樣組得出來，(3)auto_daily_digest 送出失敗會往外拋
(讓 run_auto_schedule_job 的重試機制接手)，(4)掃描日期不是今天時明確標示
不是把舊資料冒充今天的。

全部用 patch.object mock 資料來源，不觸碰真實資料庫/永豐/LINE API。

執行方式：
  python -m unittest tests.test_daily_digest -v
"""
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server as server_module


def _fake_scan_payload(scan_date):
    return {
        "scanDate": scan_date,
        "candidates": [
            {"symbol": "2330", "name": "台積電", "score": 82.0, "buyAllowed": True},
            {"symbol": "2454", "name": "聯發科", "score": 75.4, "buyAllowed": False},
            {"symbol": "2317", "name": "鴻海", "score": 68.0, "buyAllowed": True},
        ],
    }


class _FakeConn:
    """依查詢關鍵字回傳預設列的假DB連線(with語法相容)。"""

    def __init__(self, hit_row=(12, 5), training_progress=None):
        self.hit_row = hit_row
        self.training_progress = training_progress

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, sql, params=()):
        cursor = MagicMock()
        if "FROM predictions" in sql:
            cursor.fetchone.return_value = self.hit_row
        elif "training_progress" in sql:
            cursor.fetchone.return_value = (self.training_progress,) if self.training_progress else None
        else:
            cursor.fetchone.return_value = None
        return cursor


class BuildDailyDigestMessageTests(unittest.TestCase):
    TODAY = "2026-07-03"

    def _build(self, scan_date=None, holdings_error=False, scan_error=False,
               hit_row=(12, 5), training_progress=None):
        scan_date = scan_date or self.TODAY
        fake_holdings = {"holdings": [{"code": "2330"}, {"code": "2317"}]}
        with patch.object(server_module, "scheduler_today", return_value=self.TODAY), \
             patch.object(server_module.backend, "list_monster_scores",
                          side_effect=RuntimeError("scan boom") if scan_error else None,
                          return_value=None if scan_error else _fake_scan_payload(scan_date)), \
             patch.object(server_module.backend, "connect",
                          return_value=_FakeConn(hit_row=hit_row, training_progress=training_progress)), \
             patch.object(server_module.sinopac_backend, "holdings",
                          side_effect=RuntimeError("sinopac down") if holdings_error else None,
                          return_value=None if holdings_error else fake_holdings):
            return server_module.build_daily_digest_message()

    def test_normal_path_contains_all_sections(self):
        message = self._build(training_progress='{"trainedAt": "2026-07-03 15:18:30"}')
        self.assertIn("盤後摘要 07-03", message)
        self.assertIn("雷達候選 3 檔(可買 2 檔)", message)
        self.assertIn("2330 台積電 分數82", message)
        # 12 筆 < 20 樣本門檻:只報累積筆數、不報命中率(避免小樣本 % 誤導)
        self.assertIn("近30天買進訊號已結算 12 筆（樣本累積中，暫不評斷命中率）", message)
        self.assertIn("持股 2 檔", message)
        self.assertIn("模型今日已重訓(15:18)", message)

    def test_top_candidates_sorted_by_score_with_buyable_mark(self):
        message = self._build()
        lines = message.split("\n")
        candidate_lines = [line for line in lines if "分數" in line]
        self.assertEqual(len(candidate_lines), 3)
        self.assertIn("✅ 2330", candidate_lines[0])  # 分數最高排最前
        self.assertIn("👀 2454", candidate_lines[1])  # 不可買用不同標記

    def test_stale_scan_date_is_clearly_labeled_not_disguised_as_today(self):
        message = self._build(scan_date="2026-07-01")
        self.assertIn("今天沒有新的雷達掃描結果", message)
        self.assertIn("2026-07-01", message)
        self.assertNotIn("分數", message)  # 舊候選不該被列出來冒充今天的

    def test_holdings_failure_skips_section_but_message_still_builds(self):
        message = self._build(holdings_error=True)
        self.assertNotIn("持股", message)
        self.assertIn("雷達候選", message)  # 其他段照常

    def test_scan_failure_shows_error_but_message_still_builds(self):
        message = self._build(scan_error=True)
        self.assertIn("雷達結果讀取失敗", message)
        self.assertIn("近30天買進訊號", message)  # 其他段照常

    def test_no_settled_signals_omits_hit_rate_line(self):
        message = self._build(hit_row=(0, 0))
        self.assertNotIn("近30天買進訊號", message)

    def test_hit_rate_shown_only_when_sample_sufficient(self):
        # 樣本 >= 20 才報命中率百分比
        message = self._build(hit_row=(25, 10))
        self.assertIn("近30天買進訊號已結算 25 筆，+10%命中 10 筆(40%)", message)


class RadarTrackRecordStatsTests(unittest.TestCase):
    """雷達戰績端點(/api/radar/track-record)的統計函式：只算BUY_CANDIDATE
    (不混入WAIT)、附全體已結算對照組與12.6%基準率。用假DB連線，不觸碰
    真實predictions表。"""

    class _TrackRecordConn:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def execute(self, sql, params=()):
            cursor = MagicMock()
            if "action = ?" in sql:
                cursor.fetchone.return_value = (20, 8, 0.042)  # BUY_CANDIDATE
            else:
                cursor.fetchone.return_value = (500, 60, -0.001)  # 全體(含WAIT)
            return cursor

    def test_stats_shape_and_hit_rates(self):
        with patch.object(server_module.backend, "connect", return_value=self._TrackRecordConn()):
            result = server_module.radar_track_record_stats()
        self.assertTrue(result["ok"])
        self.assertEqual(result["buy30"]["settled"], 20)
        self.assertEqual(result["buy30"]["hits"], 8)
        self.assertAlmostEqual(result["buy30"]["hitRate"], 0.4)
        self.assertAlmostEqual(result["buy30"]["avgOutcomeReturn"], 0.042)
        self.assertEqual(result["allSettled90"]["settled"], 500)
        self.assertAlmostEqual(result["allSettled90"]["hitRate"], 0.12)
        self.assertAlmostEqual(result["baselineRate"], 0.126)

    def test_zero_settled_returns_none_rates_without_division_error(self):
        class _EmptyConn(self._TrackRecordConn):
            def execute(self, sql, params=()):
                cursor = MagicMock()
                cursor.fetchone.return_value = (0, 0, None)
                return cursor

        with patch.object(server_module.backend, "connect", return_value=_EmptyConn()):
            result = server_module.radar_track_record_stats()
        self.assertTrue(result["ok"])
        self.assertIsNone(result["buy30"]["hitRate"])
        self.assertIsNone(result["buy30"]["avgOutcomeReturn"])


class HoldingsDividendCalendarTests(unittest.TestCase):
    """持股除權息日曆：FinMind TaiwanStockDividend 的除權/除息日在 warn_days
    內的持股要被列出。全部 mock(假DB連線/假FinMind/假永豐)，不觸碰正式
    meta 快取 key，也不打真實 API。"""

    class _CacheConn:
        def __init__(self):
            self.saved = {}

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def execute(self, sql, params=()):
            cursor = MagicMock()
            cursor.fetchone.return_value = None  # 無快取
            return cursor

    def _run(self, dividend_rows, today="2026-07-03", warn_days=14):
        fake_holdings = {"holdings": [{"code": "2330"}]}
        conn = self._CacheConn()
        with patch.object(server_module, "scheduler_today", return_value=today), \
             patch.object(server_module.sinopac_backend, "holdings", return_value=fake_holdings), \
             patch.object(server_module, "read_finmind_token", return_value="fake-token"), \
             patch.object(server_module.backend, "fetch_finmind_dataset", return_value=dividend_rows), \
             patch.object(server_module.backend, "connect", return_value=conn), \
             patch.object(server_module.backend, "set_meta"):
            return server_module.holdings_dividend_calendar(warn_days=warn_days)

    def test_upcoming_ex_dividend_within_window_is_listed(self):
        result = self._run([{
            "CashExDividendTradingDate": "2026-07-10",
            "StockExDividendTradingDate": "0",
        }])
        self.assertTrue(result["ok"])
        self.assertEqual(len(result["items"]), 1)
        item = result["items"][0]
        self.assertEqual(item["symbol"], "2330")
        self.assertEqual(item["kind"], "除息")
        self.assertEqual(item["daysUntil"], 7)

    def test_past_and_far_future_dates_are_excluded(self):
        result = self._run([{
            "CashExDividendTradingDate": "2026-06-20",  # 已過
            "StockExDividendTradingDate": "2026-09-01",  # 超過warn_days
        }])
        self.assertEqual(result["items"], [])

    def test_zero_placeholder_dates_do_not_crash(self):
        result = self._run([{
            "CashExDividendTradingDate": "0",
            "StockExDividendTradingDate": "",
        }])
        self.assertTrue(result["ok"])
        self.assertEqual(result["items"], [])

    def test_duplicate_announcements_are_deduped(self):
        row = {"CashExDividendTradingDate": "2026-07-10", "StockExDividendTradingDate": "0"}
        result = self._run([row, dict(row)])  # 公告修訂造成的重複列
        self.assertEqual(len(result["items"]), 1)

    def test_holdings_failure_returns_error_without_raising(self):
        with patch.object(server_module.sinopac_backend, "holdings", side_effect=RuntimeError("sinopac down")), \
             patch.object(server_module, "scheduler_today", return_value="2026-07-03"), \
             patch.object(server_module.backend, "connect", return_value=self._CacheConn()):
            result = server_module.holdings_dividend_calendar()
        self.assertFalse(result["ok"])
        self.assertIn("sinopac down", result["error"])


class BuildMorningBriefMessageTests(unittest.TestCase):
    """開盤前晨報(08:15)：內容以「今天開盤要盯哪些價位」為主——最近一次
    掃描前5名含觸發價/停損價、持股數、3天內除權息。同盤後摘要的韌性設計。
    全部mock，不觸碰真實資料庫/永豐/FinMind/LINE。"""

    TODAY = "2026-07-03"

    def _build(self, candidates=None, dividend_items=None):
        scan_payload = {
            "scanDate": "2026-07-02",
            "candidates": candidates if candidates is not None else [
                {"symbol": "2330", "name": "台積電", "score": 82.0, "buyAllowed": True,
                 "buy_trigger": 870.0, "stop_price": 810.0},
                {"symbol": "2454", "name": "聯發科", "score": 75.0, "buyAllowed": False,
                 "buy_trigger": 1300.0, "stop_price": 1210.0},
            ],
        }
        calendar = {"ok": True, "items": dividend_items or []}
        with patch.object(server_module, "scheduler_today", return_value=self.TODAY), \
             patch.object(server_module.backend, "list_monster_scores", return_value=scan_payload), \
             patch.object(server_module.sinopac_backend, "holdings",
                          return_value={"holdings": [{"code": "2330"}]}), \
             patch.object(server_module, "holdings_dividend_calendar", return_value=calendar):
            return server_module.build_morning_brief_message()

    def test_contains_watchlist_with_trigger_and_stop_prices(self):
        message = self._build()
        self.assertIn("開盤前晨報 07-03", message)
        self.assertIn("依 2026-07-02 掃描", message)
        self.assertIn("✅ 2330 台積電 觸發870.00/停損810.00", message)
        self.assertIn("👀 2454 聯發科", message)
        self.assertIn("持股 1 檔", message)

    def test_dividend_today_is_flagged_as_today(self):
        message = self._build(dividend_items=[
            {"symbol": "2634", "exDate": "2026-07-03", "kind": "除息", "daysUntil": 0},
        ])
        self.assertIn("2634 除息(今天)", message)

    def test_no_candidates_shows_empty_note(self):
        message = self._build(candidates=[])
        self.assertIn("目前沒有雷達候選", message)

    def test_send_failure_propagates_for_retry(self):
        with patch.object(server_module, "build_morning_brief_message", return_value="晨報"), \
             patch.object(server_module, "send_line_message_via_api", side_effect=RuntimeError("LINE down")):
            with self.assertRaises(RuntimeError):
                server_module.auto_morning_brief()


class AutoDailyDigestTests(unittest.TestCase):
    def test_send_failure_propagates_for_retry_mechanism(self):
        # send失敗要往外拋，讓 run_auto_schedule_job 的重試(最多3次)接手，
        # 不能吞掉例外讓摘要靜默消失。
        with patch.object(server_module, "build_daily_digest_message", return_value="摘要內容"), \
             patch.object(server_module, "send_line_message_via_api", side_effect=RuntimeError("LINE down")):
            with self.assertRaises(RuntimeError):
                server_module.auto_daily_digest()

    def test_success_sends_exactly_one_message(self):
        with patch.object(server_module, "build_daily_digest_message", return_value="第一行\n第二行"), \
             patch.object(server_module, "send_line_message_via_api", return_value={"ok": True, "sent": True}) as mock_send:
            result = server_module.auto_daily_digest()
        mock_send.assert_called_once_with("第一行\n第二行")
        self.assertIn("2行", result)


if __name__ == "__main__":
    unittest.main()
