"""妖股雷達即時資料覆蓋的回歸測試。

雷達畫面可讀 100 檔，但舊後端只對前 50 檔拿 Shioaji 報價；同時 tick 訂閱
池也沒有把雷達名單列為優先。這組測試固定兩個契約：
1. 雷達候選必須優先進入 200 檔 tick 訂閱池。
2. 盤中 API 要回報報價覆蓋率，不能靜默以昨日收盤價冒充即時價。
"""
import os
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import realtime_tick_collector as collector
import server
from ml_backend import StockMLBackend


class RadarTickSubscriptionTests(unittest.TestCase):
    def test_radar_candidates_are_prioritized_before_liquid_filler(self):
        radar_codes = [f"{i:04d}" for i in range(1000, 1100)]
        liquid_codes = [f"{i:04d}" for i in range(2000, 2200)]
        with patch.object(collector.sinopac_backend, "holdings", return_value={"holdings": [{"code": "2330"}]}), \
             patch.object(collector.backend, "list_monster_scores", return_value={"candidates": [{"symbol": code} for code in radar_codes]}), \
             patch.object(collector.backend, "liquid_monster_universe", return_value=liquid_codes):
            selected, info = collector.select_watch_symbols(limit=120)

        self.assertEqual(selected[0], "2330")
        self.assertTrue(set(radar_codes).issubset(selected))
        self.assertEqual(info["radarSubscribed"], len(radar_codes))
        self.assertEqual(info["radarMissing"], [])

    def test_radar_missing_is_reported_when_subscription_limit_is_exhausted(self):
        holdings = [{"code": f"{i:04d}"} for i in range(3000, 3200)]
        with patch.object(collector.sinopac_backend, "holdings", return_value={"holdings": holdings}), \
             patch.object(collector.backend, "list_monster_scores", return_value={"candidates": [{"symbol": "2330"}]}), \
             patch.object(collector.backend, "liquid_monster_universe", return_value=[]):
            selected, info = collector.select_watch_symbols()

        self.assertEqual(len(selected), collector.MAX_SUBSCRIBE_SYMBOLS)
        self.assertEqual(info["radarSubscribed"], 0)
        self.assertEqual(info["radarMissing"], ["2330"])


class RadarQuoteCoverageTests(unittest.TestCase):
    def test_radar_uses_the_same_100_row_limit_as_the_ui(self):
        self.assertEqual(server.MAX_MONSTER_INTRADAY_CANDIDATES, 100)

    def test_quote_coverage_marks_missing_quotes_explicitly(self):
        codes = ["2330", "2317", "2454"]
        quotes = {"2330": {"currentPrice": 100}, "2454": {"currentPrice": 200}}
        coverage = {
            "requested": len(codes),
            "received": len(quotes),
            "missing": [code for code in codes if code not in quotes],
            "complete": len(quotes) == len(codes),
        }
        self.assertEqual(coverage["missing"], ["2317"])
        self.assertFalse(coverage["complete"])

    def test_market_closed_skips_broker_quote_login(self):
        original_status = dict(server.monster_intraday_status)
        original_last_update = server.monster_intraday_last_update
        try:
            with patch.object(server, "taipei_localtime", return_value=server.time.struct_time((2026, 7, 11, 9, 30, 0, 5, 192, 0))), \
                 patch.object(server.backend, "list_monster_scores") as list_scores, \
                 patch.object(server.sinopac_backend, "quotes") as quotes:
                result = server.update_monster_intraday_quotes(trigger="test-closed")
            self.assertTrue(result["marketClosed"])
            self.assertEqual(result["quoteCount"], 0)
            self.assertEqual(result["buyableCount"], 0)
            self.assertEqual(result["shadowBuyableCount"], 0)
            self.assertEqual(result["quotes"], {})
            self.assertTrue(result["snapshotPipeline"]["ok"])
            self.assertEqual(result["snapshotPipeline"]["skipped"], "market_closed")
            self.assertIn("休市不登入券商", result["message"])
            self.assertTrue(server.intraday_status_is_fresh(result, server.monster_intraday_last_update))
            list_scores.assert_not_called()
            quotes.assert_not_called()
        finally:
            with server.monster_intraday_lock:
                server.monster_intraday_status = original_status
                server.monster_intraday_last_update = original_last_update
                server.monster_intraday_running = False
                server.monster_intraday_running_since = 0

    def test_market_closed_cache_preserves_the_closed_reason(self):
        payload = server.intraday_status_with_cache_flag({
            "ok": True,
            "marketClosed": True,
            "updatedAt": "2026-07-12 09:30:00",
            "message": "週末；休市不登入券商、不更新盤中報價",
        }, cache_hit=True)

        self.assertTrue(payload["servedFromStatusCache"])
        self.assertEqual(payload["message"], "週末；休市不登入券商、不更新盤中報價")


    def test_intraday_update_requests_all_100_visible_candidates(self):
        candidates = [
            {
                "symbol": f"{i:04d}", "close": 100.0, "buyTrigger": 105.0,
                "pullbackPrice": 98.0, "stopPrice": 93.0, "avgVolume20Lots": 1000.0,
                "buyAllowed": False, "priceDate": "2026-07-09",
            }
            for i in range(1000, 1100)
        ]
        quotes = {
            item["symbol"]: {
                "currentPrice": 100.0, "openPrice": 100.0, "highPrice": 100.0,
                "lowPrice": 100.0, "totalVolume": 1000.0,
            }
            for item in candidates
        }
        quotes["1000"].update({
            "source": "Capital Strategy King COM",
            "fresh": False,
            "stale": False,
            "openPrice": 99.0,
            "highPrice": 101.0,
            "lowPrice": 98.0,
        })

        class _Cursor:
            def fetchone(self):
                return ("2026-07-09",)

            def fetchall(self):
                return []

        class _Conn:
            def execute(self, *_args, **_kwargs):
                return _Cursor()

        @contextmanager
        def fake_connect():
            yield _Conn()

        quote_call = {}

        def fake_quotes(codes):
            quote_call["codes"] = list(codes)
            return {
                "ok": True,
                "quotes": quotes,
                "source": "Shioaji + Capital quote",
                "fallbackUsed": True,
                "fallbackProvider": "Capital Strategy King COM",
                "fallbackCodes": ["1000"],
                "fallbackReason": "Sinopac missing 1 quote(s)",
            }

        with patch.object(server.backend, "list_monster_scores", return_value={"candidates": candidates}), \
             patch.object(server, "taipei_localtime", return_value=server.time.struct_time((2026, 7, 9, 14, 0, 0, 3, 190, 0))), \
             patch.object(server, "official_market_day_status", return_value={"known": True, "isTradingDay": True}), \
             patch.object(server, "portfolio_cache_quote_symbols", return_value=["1000", "2330"]), \
             patch.object(server, "update_portfolio_summary_quote_cache", return_value={"ok": True, "requested": 2, "fresh": 1}), \
             patch.object(server.backend, "connect", side_effect=fake_connect), \
             patch.object(server.backend, "data_freshness", return_value={"checkedAt": "2026-07-10 09:30:00", "sources": [
                 {"name": "個股日K", "ok": True}, {"name": "大盤指數", "ok": True},
             ]}), \
             patch.object(server.sinopac_backend, "quotes", side_effect=fake_quotes), \
             patch.object(server, "notify_intraday_entry_triggers"), \
             patch.object(server, "notify_limit_up_open"), \
             patch.object(server, "notify_intraday_surge"), \
             patch.object(server, "notify_intraday_quote_blackout"), \
             patch.object(server, "record_intraday_gate_stats"):
            result = server.update_monster_intraday_quotes(trigger="test")

        self.assertEqual(len(quote_call["codes"]), 101)
        self.assertEqual(quote_call["codes"].count("1000"), 1)
        self.assertIn("2330", quote_call["codes"])
        self.assertEqual(result["quoteCoverage"]["requested"], 100)
        self.assertEqual(result["quoteCoverage"]["received"], 100)
        self.assertTrue(result["quoteCoverage"]["complete"])
        self.assertEqual(result["source"], "Shioaji + Capital quote")
        self.assertEqual(result["quotes"]["1000"]["source"], "Capital Strategy King COM")
        self.assertFalse(result["quotes"]["1000"]["quoteFresh"])
        self.assertTrue(result["quotes"]["1000"]["quoteStaleBlocked"])
        self.assertFalse(result["quotes"]["1000"]["canBuy"])
        self.assertTrue(result["quotes"]["1001"]["quoteFresh"])
        self.assertTrue(result["quoteFallbackUsed"])
        self.assertEqual(result["quoteFallbackCodes"], ["1000"])
        self.assertEqual(result["portfolioQuoteRefresh"]["requested"], 2)

    def test_observation_only_candidate_reaches_snapshot_pipeline_as_shadow(self):
        original_status = dict(server.monster_intraday_status)
        original_last_update = server.monster_intraday_last_update
        candidate = {
            "symbol": "9999",
            "close": 100.0,
            "buyTrigger": 105.0,
            "pullbackPrice": 98.0,
            "stopPrice": 93.0,
            "avgVolume20Lots": 1000.0,
            "score": 90.0,
            "minimumFormalScore": 60.0,
            "buyAllowed": False,
            "policyBuyAllowed": True,
            "performanceVetoed": True,
            "priceDate": "2026-07-10",
        }
        quote = {
            "currentPrice": 106.0,
            "openPrice": 101.0,
            "highPrice": 107.0,
            "lowPrice": 100.5,
            "totalVolume": 2500.0,
            "bidPrice": 105.9,
            "askPrice": 106.0,
            "snapshotAt": "2026-07-13 09:44:59",
        }

        class _Cursor:
            def fetchone(self):
                return ("2026-07-10",)

            def fetchall(self):
                return []

        class _Conn:
            def execute(self, *_args, **_kwargs):
                return _Cursor()

        @contextmanager
        def fake_connect():
            yield _Conn()

        try:
            with patch.object(
                server.backend, "list_monster_scores",
                return_value={"candidates": [candidate]},
            ), patch.object(
                server, "taipei_localtime",
                return_value=server.time.struct_time((2026, 7, 13, 9, 45, 0, 0, 194, 0)),
            ), patch.object(
                server, "official_market_day_status",
                return_value={"known": True, "isTradingDay": True},
            ), patch.object(
                server.backend, "market_session_acceptance",
                return_value={"ok": True, "entryGuardReady": True},
            ), patch.object(
                server.backend, "connect", side_effect=fake_connect,
            ), patch.object(
                server.backend, "data_freshness",
                return_value={"checkedAt": "2026-07-13 09:45:00", "sources": [
                    {"name": "個股日K", "ok": True},
                    {"name": "大盤指數", "ok": True},
                ]},
            ), patch.object(
                server.sinopac_backend, "quotes",
                return_value={"ok": True, "quotes": {"9999": quote}, "source": "test quote"},
            ), patch.object(
                server, "intraday_quote_freshness", return_value=(True, 1.0, "fresh"),
            ), patch.object(
                server.backend, "record_radar_entry_snapshots"
            ) as record_snapshots, patch.object(
                server, "notify_intraday_entry_triggers"
            ), patch.object(
                server, "notify_limit_up_open"
            ), patch.object(
                server, "notify_intraday_surge"
            ), patch.object(
                server, "notify_intraday_quote_blackout"
            ), patch.object(
                server, "record_intraday_gate_stats"
            ):
                record_snapshots.return_value = {
                    "ok": True,
                    "eligibleStates": 1,
                    "prepared": 1,
                    "inserted": 1,
                    "duplicates": 0,
                    "skippedNoPrice": 0,
                    "persisted": 1,
                    "missingSymbols": [],
                }
                result = server.update_monster_intraday_quotes(trigger="test-shadow")

            state = result["quotes"]["9999"]
            self.assertTrue(state["shadowCanBuy"])
            self.assertFalse(state["canBuy"])
            self.assertTrue(state["candidatePerformanceVetoed"])
            self.assertEqual(result["shadowBuyableCount"], 1)
            self.assertEqual(result["buyableCount"], 0)
            self.assertTrue(result["snapshotPipeline"]["ok"])
            self.assertEqual(result["snapshotPipeline"]["persisted"], 1)
            record_snapshots.assert_called_once()
            self.assertTrue(record_snapshots.call_args.kwargs["return_details"])
            recorded_states = record_snapshots.call_args.args[1]
            self.assertTrue(recorded_states["9999"]["shadowCanBuy"])
            self.assertFalse(recorded_states["9999"]["canBuy"])
        finally:
            with server.monster_intraday_lock:
                server.monster_intraday_status = original_status
                server.monster_intraday_last_update = original_last_update
                server.monster_intraday_running = False
                server.monster_intraday_running_since = 0


class RadarSnapshotAlertTests(unittest.TestCase):
    def test_snapshot_failure_alert_is_deduplicated_per_day(self):
        with tempfile.TemporaryDirectory() as tmp:
            test_backend = StockMLBackend()
            test_backend.db_path = Path(tmp) / "stock_system.sqlite3"
            test_backend.init_db()
            state = {
                "date": "2026-07-13",
                "ok": False,
                "expected": 1,
                "persisted": 0,
                "missingSymbols": ["9999"],
                "error": "database is locked",
            }
            with patch.object(server, "backend", test_backend), \
                 patch.object(server, "radar_entry_snapshot_alert_last_date", ""), \
                 patch.object(
                     server, "send_windows_desktop_notification",
                     return_value={"sent": True},
                 ) as desktop, \
                 patch.object(
                     server, "send_line_message_via_api",
                     return_value={"sent": True},
                 ) as line:
                first = server.notify_radar_entry_snapshot_failure(state)
                second = server.notify_radar_entry_snapshot_failure(state)

        self.assertEqual(first["notified"], 1)
        self.assertEqual(second["skipped"], "already_notified")
        desktop.assert_called_once()
        line.assert_called_once()


if __name__ == "__main__":
    unittest.main()
