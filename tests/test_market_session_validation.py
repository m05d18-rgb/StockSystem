import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server
from ml_backend import StockMLBackend


class MarketSessionValidationTests(unittest.TestCase):
    def backend(self, tmp):
        backend = StockMLBackend()
        backend.db_path = Path(tmp) / "stock_system.sqlite3"
        backend.init_db()
        with backend.connect() as conn:
            rows = [
                (f"{index:04d}", "2026-07-13", 10, 10, 10, 10, 1000, "2026-07-13 14:00:00")
                for index in range(1500)
            ]
            conn.executemany("""
                INSERT INTO prices (
                    symbol, date, open, high, low, close, volume, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, rows)
            backend.set_meta(conn, server.INTRADAY_GATE_STATS_KEY, json.dumps({
                "date": "2026-07-13",
                "polls": 5,
                "freshQuoteCount": 80,
                "freshQuoteTimestampCount": 80,
                "quoteSources": {"Sinopac Shioaji": 70, "Capital 群益": 10},
                "fallbackQuoteCount": 10,
                "fallbackQuoteCodes": ["1000"],
                "quoteDateMismatchCount": 0,
                "quoteDateMismatchCodes": [],
                "missingQuoteTimestampCount": 0,
                "missingQuoteTimestampCodes": [],
                "lastPollAt": "2026-07-13 09:59:55",
                "dailyDataFreshCount": 80,
                "marketDataFreshCount": 80,
                "buyableUnion": 2,
                "dangerRiskCount": 10,
                "invalidCandidateCount": 0,
                "riskLeakCount": 0,
                "riskLeakCodes": [],
            }))
            backend.set_meta(conn, server.INTRADAY_NOTIFICATION_PIPELINE_KEY, json.dumps({
                "date": "2026-07-13",
                "checkedAt": "2026-07-13 09:55:00",
                "entry": {"notified": 0, "skipped": "already_notified"},
                "errors": [],
            }))
            backend.set_meta(conn, server.RADAR_ENTRY_SNAPSHOT_PIPELINE_KEY, json.dumps({
                "date": "2026-07-13",
                "checkedAt": "2026-07-13 09:55:00",
                "ok": True,
                "expected": 2,
                "prepared": 2,
                "inserted": 0,
                "duplicates": 2,
                "persisted": 2,
                "missingSymbols": [],
            }))
        return backend

    @staticmethod
    def now(hour=10, minute=0):
        return time.strptime(f"2026-07-13 {hour:02d}:{minute:02d}:00", "%Y-%m-%d %H:%M:%S")

    @staticmethod
    def market_day(*_args, **_kwargs):
        return {
            "known": True,
            "isTradingDay": True,
            "date": "2026-07-13",
            "reason": "正常交易日",
            "source": "TWSE annual holiday schedule",
        }

    def test_intraday_report_requires_real_quotes_and_zero_risk_leaks(self):
        with tempfile.TemporaryDirectory() as tmp:
            backend = self.backend(tmp)
            with patch.object(server, "backend", backend), \
                 patch.object(server, "official_market_day_status", self.market_day), \
                 patch.object(server, "radar_market_data_health", return_value={"ok": True}), \
                 patch.object(backend, "current_radar_decision_validity", return_value={
                     "validForTrading": True, "summary": "雷達決策資料有效",
                 }):
                report = server.build_market_session_validation("intraday", now=self.now())
            self.assertTrue(report["ok"])
            self.assertEqual(report["failureCount"], 0)
            self.assertTrue(all(row["ok"] for row in report["checks"] if row["required"]))

            with backend.connect() as conn:
                row = conn.execute(
                    "SELECT value FROM model_meta WHERE key = ?", (server.INTRADAY_GATE_STATS_KEY,)
                ).fetchone()
                stats = json.loads(row[0])
                stats["riskLeakCount"] = 1
                stats["riskLeakCodes"] = ["9999"]
                backend.set_meta(conn, server.INTRADAY_GATE_STATS_KEY, json.dumps(stats))
            with patch.object(server, "backend", backend), \
                 patch.object(server, "official_market_day_status", self.market_day), \
                 patch.object(server, "radar_market_data_health", return_value={"ok": True}), \
                 patch.object(backend, "current_radar_decision_validity", return_value={
                     "validForTrading": True, "summary": "雷達決策資料有效",
                 }):
                leaked = server.build_market_session_validation("intraday", now=self.now())
            self.assertFalse(leaked["ok"])
            self.assertIn("風險否決不外洩", leaked["failures"])

    def test_intraday_report_fails_when_shadow_snapshot_is_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            backend = self.backend(tmp)
            with backend.connect() as conn:
                backend.set_meta(conn, server.RADAR_ENTRY_SNAPSHOT_PIPELINE_KEY, json.dumps({
                    "date": "2026-07-13",
                    "checkedAt": "2026-07-13 09:55:00",
                    "ok": False,
                    "expected": 1,
                    "prepared": 1,
                    "inserted": 0,
                    "duplicates": 0,
                    "persisted": 0,
                    "missingSymbols": ["9999"],
                    "error": "database is locked",
                }))
            with patch.object(server, "backend", backend), \
                 patch.object(server, "official_market_day_status", self.market_day), \
                 patch.object(server, "radar_market_data_health", return_value={"ok": True}), \
                 patch.object(backend, "current_radar_decision_validity", return_value={
                     "validForTrading": True, "summary": "雷達決策資料有效",
                 }):
                report = server.build_market_session_validation("intraday", now=self.now())
        self.assertFalse(report["ok"])
        self.assertIn("紙上快照管線", report["failures"])
        snapshot_check = next(
            item for item in report["checks"] if item["key"] == "radar_entry_snapshots"
        )
        self.assertFalse(snapshot_check["ok"])
        self.assertEqual(snapshot_check["evidence"]["missingSymbols"], ["9999"])

    def test_intraday_report_rejects_fresh_flag_with_wrong_quote_date(self):
        with tempfile.TemporaryDirectory() as tmp:
            backend = self.backend(tmp)
            with backend.connect() as conn:
                row = conn.execute(
                    "SELECT value FROM model_meta WHERE key = ?", (server.INTRADAY_GATE_STATS_KEY,)
                ).fetchone()
                stats = json.loads(row[0])
                stats["quoteDateMismatchCount"] = 1
                stats["quoteDateMismatchCodes"] = ["2330"]
                backend.set_meta(conn, server.INTRADAY_GATE_STATS_KEY, json.dumps(stats))
            with patch.object(server, "backend", backend), \
                 patch.object(server, "official_market_day_status", self.market_day), \
                 patch.object(server, "radar_market_data_health", return_value={"ok": True}), \
                 patch.object(backend, "current_radar_decision_validity", return_value={
                     "validForTrading": True, "summary": "雷達決策資料有效",
                 }):
                report = server.build_market_session_validation("intraday", now=self.now())
        self.assertFalse(report["ok"])
        self.assertIn("即時報價日期與來源", report["failures"])

    def test_close_report_checks_official_sync_radar_and_settlement(self):
        with tempfile.TemporaryDirectory() as tmp:
            backend = self.backend(tmp)
            with backend.connect() as conn:
                for key, value in {
                    "last_official_close_sync_status": "ready",
                    "last_official_close_sync_target_date": "2026-07-13",
                    "last_official_close_sync_latest_date": "2026-07-13",
                    "last_official_close_sync_latest_count": "6200",
                    "last_official_close_sync_error": "",
                    "last_portfolio_exit_settlement_at": "2026-07-13 14:50:00",
                    "last_portfolio_exit_settlement_count": "3",
                    "last_portfolio_exit_settlement_error": "",
                }.items():
                    backend.set_meta(conn, key, value)
                for session_key, job_id in server.PAPER_SIGNAL_SESSION_JOBS.items():
                    backend.set_meta(conn, f"auto_schedule_{job_id}_date", "2026-07-13")
                    backend.set_meta(conn, f"auto_schedule_{job_id}_status", "success")
                    backend.set_meta(conn, f"last_paper_signal_snapshot_{session_key}_at", "2026-07-13 15:20:00")
                    backend.set_meta(conn, f"last_paper_signal_snapshot_{session_key}_errors", "0")
            with patch.object(server, "backend", backend), \
                 patch.object(server, "official_market_day_status", self.market_day), \
                 patch.object(server, "radar_market_data_health", return_value={"ok": True}), \
                 patch.object(backend, "current_radar_decision_validity", return_value={
                     "validForTrading": True,
                     "summary": "雷達決策資料有效",
                 }):
                report = server.build_market_session_validation("close", now=self.now(18, 5))
            self.assertTrue(report["ok"])
            self.assertEqual(report["failureCount"], 0)
            self.assertTrue(next(
                item for item in report["checks"] if item["key"] == "paper_snapshot_sessions"
            )["ok"])

            saved = backend.record_market_session_validation(report)
            self.assertTrue(saved["ok"])
            records = backend.list_market_session_validations()
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["stage"], "close")
            self.assertTrue(records[0]["report"]["ok"])

    def test_schedule_has_intraday_and_close_validation_windows(self):
        self.assertEqual(server.AUTO_SCHEDULE_WINDOWS["0850_premarket_monster_scan"], (8, 50, 10, 0))
        self.assertEqual(server.AUTO_SCHEDULE_WINDOWS["0905_market_session_validation"], (9, 5, 9, 15))
        self.assertEqual(server.AUTO_SCHEDULE_WINDOWS["0950_market_session_validation"], (9, 50, 10, 10))
        self.assertEqual(server.AUTO_SCHEDULE_WINDOWS["1800_market_session_validation"], (18, 0, 18, 30))
        self.assertEqual(
            server.auto_schedule_retry_limit("1800_market_session_validation"),
            server.OFFICIAL_CLOSE_SYNC_MAX_RETRIES,
        )

    def test_open_report_checks_only_market_daily_and_radar_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            backend = self.backend(tmp)
            with patch.object(server, "backend", backend), \
                 patch.object(server, "official_market_day_status", self.market_day), \
                 patch.object(server, "radar_market_data_health", return_value={"ok": True}), \
                 patch.object(backend, "current_radar_decision_validity", return_value={
                     "validForTrading": True, "summary": "雷達決策資料有效",
                 }), \
                 patch.object(backend, "current_radar_deployment_readiness", return_value={
                     "readinessDate": "2026-07-13",
                     "enforced": False,
                     "formalReady": False,
                     "reasons": ["樣本不足，觀察中"],
                 }):
                report = server.build_market_session_validation("open", now=self.now(9, 6))
        self.assertTrue(report["ok"])
        self.assertEqual(
            [item["key"] for item in report["checks"]],
            ["official_market_day", "daily_bars", "radar_decision", "radar_performance"],
        )

    def test_acceptance_keeps_latest_result_for_each_stage(self):
        with tempfile.TemporaryDirectory() as tmp:
            backend = self.backend(tmp)
            for stage, ok in (("open", True), ("intraday", True), ("close", False)):
                backend.record_market_session_validation({
                    "sessionDate": "2026-07-13",
                    "stage": stage,
                    "checkedAt": f"2026-07-13 {stage}",
                    "ok": ok,
                    "failureCount": 0 if ok else 1,
                    "warningCount": 0,
                    "failures": [] if ok else ["收盤結算"],
                    "warnings": [],
                })
            acceptance = backend.market_session_acceptance("2026-07-13")
        self.assertTrue(acceptance["entryGuardReady"])
        self.assertTrue(acceptance["intradayReady"])
        self.assertFalse(acceptance["closeReady"])
        self.assertFalse(acceptance["fullDayReady"])
        self.assertEqual(acceptance["failedStages"], ["close"])

    def test_close_acceptance_is_persisted_only_with_all_three_stages(self):
        with tempfile.TemporaryDirectory() as tmp:
            backend = self.backend(tmp)
            close_id = None
            for stage in ("open", "intraday", "close"):
                saved = backend.record_market_session_validation({
                    "sessionDate": "2026-07-13",
                    "stage": stage,
                    "checkedAt": f"2026-07-13 {stage}",
                    "ok": True,
                    "failureCount": 0,
                    "warningCount": 0,
                    "failures": [],
                    "warnings": [],
                })
                if stage == "close":
                    close_id = saved["id"]
            acceptance = backend.finalize_market_session_acceptance(
                "2026-07-13", source_validation_id=close_id
            )
            history = backend.list_market_session_acceptance_history()
            with backend.connect() as conn:
                status = conn.execute(
                    "SELECT value FROM model_meta "
                    "WHERE key = 'last_market_session_acceptance_status'"
                ).fetchone()[0]

        self.assertTrue(acceptance["fullDayReady"])
        self.assertTrue(acceptance["saved"])
        self.assertEqual(acceptance["summary"], "開盤、盤中與收盤驗證均通過")
        self.assertEqual(len(history), 1)
        self.assertTrue(history[0]["full_day_ready"])
        self.assertEqual(status, "ready")

    def test_five_day_observation_requires_consecutive_full_day_acceptance(self):
        with tempfile.TemporaryDirectory() as tmp:
            backend = self.backend(tmp)
            started = backend.start_stability_observation(
                "six-fixes-test",
                start_session_date="2026-07-13",
                target_consecutive_sessions=5,
                scope=["six-root-fixes"],
            )
            failed = backend.record_stability_observation_day("2026-07-13", {
                "fullDayReady": False,
                "missingStages": ["intraday"],
                "failedStages": [],
                "summary": "缺少盤中驗證",
            })
            trading_days = [
                "2026-07-14", "2026-07-15", "2026-07-16",
                "2026-07-17", "2026-07-20",
            ]
            results = []
            for date in trading_days:
                results.append(backend.record_stability_observation_day(date, {
                    "fullDayReady": True,
                    "missingStages": [],
                    "failedStages": [],
                    "summary": "三階段均通過",
                }))

        self.assertTrue(started["active"])
        self.assertEqual(failed["consecutivePassDays"], 0)
        self.assertEqual(results[-2]["status"], "active")
        self.assertEqual(results[-1]["status"], "completed")
        self.assertEqual(results[-1]["consecutivePassDays"], 5)
        self.assertEqual(results[-1]["remainingPassDays"], 0)


if __name__ == "__main__":
    unittest.main()
