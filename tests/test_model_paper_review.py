import os
import datetime as dt
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ml_backend import StockMLBackend


class ModelPaperReviewTests(unittest.TestCase):
    def _backend(self, tmp_dir):
        backend = StockMLBackend()
        backend.db_path = Path(tmp_dir) / "stock_system.sqlite3"
        backend.init_db()
        return backend

    def test_official_inactive_period_blocks_only_post_effective_price_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            backend = self._backend(tmp)
            rows = [
                {
                    "symbol": "2888", "date": "2025-07-23", "open": 10,
                    "high": 10.2, "low": 9.9, "close": 10.1, "volume": 1000,
                    "price_source": "FinMind TaiwanStockPrice",
                },
                {
                    "symbol": "2888", "date": "2026-07-09", "open": 10,
                    "high": 10.2, "low": 9.9, "close": 10.1, "volume": 1000,
                    "price_source": "FinMind TaiwanStockPrice",
                },
            ]

            written = backend.upsert_price_rows(rows)
            with backend.connect() as conn:
                saved = conn.execute(
                    "SELECT date FROM prices WHERE symbol = '2888' ORDER BY date"
                ).fetchall()

            self.assertEqual(written, 1)
            self.assertEqual([row[0] for row in saved], ["2025-07-23"])

    def test_delisted_refresh_excludes_codes_present_in_current_official_universe(self):
        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            @staticmethod
            def read():
                return json.dumps({
                    "status": "ok",
                    "data": [
                        ["091/11/04", "舊公司", "2301"],
                        ["114/07/24", "新光金", "2888"],
                    ],
                }, ensure_ascii=False).encode("utf-8")

        with tempfile.TemporaryDirectory() as tmp:
            backend = self._backend(tmp)
            active = {
                "symbols": {"2301", *[f"{value:04d}" for value in range(3000, 4500)]},
                "count": 1501,
                "evidenceDate": "2026-07-13",
                "errors": [],
            }
            with patch.object(backend, "current_official_active_symbols", return_value=active), \
                 patch("ml_backend.urlopen", return_value=Response()):
                result = backend.refresh_twse_delisted_periods(force=True)
            with backend.connect() as conn:
                rows = dict(conn.execute("""
                    SELECT symbol, status FROM market_symbol_inactive_periods
                    WHERE symbol IN ('2301', '2888')
                """).fetchall())

        self.assertEqual(result["activeExcluded"], 1)
        self.assertNotIn("2301", rows)
        self.assertEqual(rows["2888"], "delisted")

    def test_cleanup_archives_invalid_market_rows_and_reserved_test_strategy(self):
        with tempfile.TemporaryDirectory() as tmp:
            backend = self._backend(tmp)
            with backend.connect() as conn:
                conn.execute("""
                    INSERT INTO prices (
                        symbol, date, open, high, low, close, volume,
                        price_source, updated_at
                    ) VALUES (
                        '2888', '2026-07-09', 10, 10.2, 9.9, 10.1, 1000,
                        'FinMind TaiwanStockPrice', '2026-07-10 00:00:00'
                    )
                """)
                conn.execute("""
                    INSERT INTO predictions (
                        created_at, symbol, price_date, model_version, probability,
                        threshold, action, target_horizon, target_return, close
                    ) VALUES (
                        '2026-07-10 00:00:00', '2888', '2026-07-09', 'test-model',
                        0.5, 0.6, 'WAIT', 10, 0.1, 10.1
                    )
                """)
                conn.execute("""
                    INSERT INTO strategy_signals (
                        signal_date, strategy, side, symbol, price, created_at, updated_at
                    ) VALUES (
                        '2026-06-30', 'Test', 'BUY', '3481', 68.7,
                        '2026-06-30 09:00:00', '2026-06-30 09:00:00'
                    )
                """)
                conn.execute("""
                    INSERT INTO strategy_calibration (
                        calibration_date, strategy, mode, sample_count, pending_5d,
                        suggested_action, weight_multiplier, threshold_delta, reason,
                        observation_days, apply_ready, applied, metrics_json,
                        created_at, updated_at
                    ) VALUES (
                        '2026-07-10', 'Test', 'observation', 0, 0, 'observe_more',
                        1, 0, 'test', 1, 0, 0, '{}',
                        '2026-07-10 17:05:00', '2026-07-10 17:05:00'
                    )
                """)

            result = backend.cleanup_invalid_production_data()
            with backend.connect() as conn:
                counts = {
                    table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                    for table in ("prices", "predictions", "strategy_signals", "strategy_calibration")
                }
                audits = conn.execute(
                    "SELECT COUNT(*) FROM data_cleanup_audit WHERE cleanup_key = ?",
                    (result["cleanupKey"],),
                ).fetchone()[0]

            self.assertEqual(counts, {
                "prices": 0, "predictions": 0,
                "strategy_signals": 0, "strategy_calibration": 0,
            })
            self.assertEqual(audits, 4)
            with backend.connect() as conn:
                with self.assertRaisesRegex(ValueError, "reserved test strategy"):
                    backend.save_strategy_signal(conn, {
                        "signalDate": "2026-07-10", "strategy": "TEST",
                        "side": "BUY", "symbol": "2330",
                    })

    def test_strategy_stats_exclude_reserved_test_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            backend = self._backend(tmp)
            with backend.connect() as conn:
                conn.execute("""
                    INSERT INTO strategy_signals (
                        signal_date, strategy, side, symbol, price,
                        return_5d, hit_5d, created_at, updated_at
                    ) VALUES
                    ('2026-07-01', 'real_rule', 'BUY', '2330', 100, 0.05, 1,
                     '2026-07-01 09:00:00', '2026-07-01 09:00:00'),
                    ('2026-07-01', 'Test', 'BUY', '3481', 50, -0.05, 0,
                     '2026-07-01 09:00:00', '2026-07-01 09:00:00')
                """)

            result = backend.strategy_signal_performance(refresh_outcomes=False)

            self.assertEqual(result["signalCount"], 1)
            self.assertEqual([item["strategy"] for item in result["strategies"]], ["real_rule"])

    def test_strategy_signal_performance_keeps_buy_sell_details(self):
        with tempfile.TemporaryDirectory() as tmp:
            backend = self._backend(tmp)
            with backend.connect() as conn:
                conn.execute("""
                    INSERT INTO strategy_signals (
                        signal_date, strategy, side, symbol, name, decision, score, price,
                        return_1d, return_3d, return_5d, return_10d, return_20d, return_60d,
                        hit_1d, hit_3d, hit_5d, hit_10d, hit_20d, hit_60d, created_at, updated_at
                    ) VALUES
                    ('2026-07-01', 'model', 'BUY_CANDIDATE', '2330', '台積電', '買進', 88, 100,
                     0.01, 0.03, 0.06, 0.08, 0.10, 0.18, 1, 1, 1, 1, 1, 1, '2026-07-01 09:00:00', '2026-07-01 09:00:00'),
                    ('2026-07-02', 'model', 'EXIT_CONFIRM', '2317', '鴻海', '賣出', 55, 200,
                     -0.01, -0.02, -0.04, -0.05, -0.08, -0.16, 1, 1, 1, 1, 1, 1, '2026-07-02 09:00:00', '2026-07-02 09:00:00'),
                    ('2026-07-04', 'model_exit', 'EXIT_CONFIRM', '2330', '台積電', '賣出', 70, 108,
                     0.00, 0.00, 0.00, 0.00, -0.01, -0.02, 1, 1, 1, 1, 1, 1, '2026-07-04 09:00:00', '2026-07-04 09:00:00'),
                    ('2026-07-03', 'model', 'WATCH', '2603', '長榮', '觀察', 50, 150,
                     0.01, 0.02, 0.03, 0.04, 0.05, 0.06, 1, 1, 1, 1, 1, 1, '2026-07-03 09:00:00', '2026-07-03 09:00:00')
                """)
                for date, open_price, high, low, close in [
                    ("2026-07-01", 99, 101, 98, 100),
                    ("2026-07-02", 100, 103, 99, 102),
                    ("2026-07-04", 106, 109, 105, 108),
                    ("2026-07-05", 108, 109, 107, 108),
                ]:
                    conn.execute("""
                        INSERT INTO prices (
                            symbol, date, open, high, low, close, volume, price_source, updated_at
                        ) VALUES ('2330', ?, ?, ?, ?, ?, 1000000, 'TWSE official test', '2026-07-06 00:00:00')
                    """, (date, open_price, high, low, close))

            result = backend.strategy_signal_performance(refresh_outcomes=False)

            self.assertTrue(result["ok"])
            self.assertEqual(result["overall"]["signals"], 4)
            self.assertEqual(result["overall"]["actionableSignals"], 3)
            self.assertEqual(result["overall"]["horizons"]["5d"]["actionableSamples"], 3)
            self.assertEqual(result["overall"]["horizons"]["5d"]["precision"], 1.0)
            self.assertEqual(result["overall"]["horizons"]["20d"]["actionableSamples"], 3)
            self.assertEqual(result["overall"]["horizons"]["60d"]["actionableSamples"], 3)
            self.assertEqual(result["overall"]["horizonGroups"]["short"]["primary"], "5d")
            self.assertEqual(result["overall"]["horizonGroups"]["mid"]["primary"], "20d")
            self.assertEqual(result["overall"]["horizonGroups"]["long"]["primary"], "60d")
            self.assertEqual(len(result["recentSignals"]), 3)
            sell = next(item for item in result["recentSignals"] if item["symbol"] == "2317")
            self.assertEqual(sell["horizons"]["5d"]["adjustedReturn"], 0.04)
            self.assertEqual(sell["horizons"]["60d"]["adjustedReturn"], 0.16)
            self.assertTrue(sell["horizons"]["5d"]["hit"])
            self.assertNotIn("WATCH", {item["side"] for item in result["recentSignals"]})
            paper = result["paperTrades"]
            self.assertEqual(paper["closedCount"], 1)
            self.assertEqual(paper["orphanSellSignals"], 1)
            self.assertEqual(paper["modelOrphanSellSignals"], 1)
            self.assertEqual(paper["closed"][0]["symbol"], "2330")
            self.assertEqual(paper["closed"][0]["entryStrategy"], "model")
            self.assertEqual(paper["closed"][0]["exitStrategy"], "model_exit")
            self.assertAlmostEqual(paper["closed"][0]["returnPct"], 0.08)
            self.assertEqual(paper["closed"][0]["pnlPerLot"], 8000)
            # 100 -> 108, one lot. Fees/tax are 621 and two-sided slippage is 208.
            self.assertEqual(paper["closed"][0]["shares"], 1000)
            self.assertEqual(paper["closed"][0]["totalSlippageCost"], 208)
            self.assertEqual(paper["closed"][0]["totalCosts"], 829)
            self.assertEqual(paper["closed"][0]["netPnlPerLot"], 7171)
            self.assertAlmostEqual(paper["closed"][0]["netReturnPct"], 7171 / 100243)
            self.assertEqual(paper["closedNetPnlPerLot"], 7171)
            self.assertEqual(paper["totalNetPnlPerLot"], 7171)
            self.assertEqual(paper["winRateConfidence95"]["samples"], 1)
            self.assertEqual(paper["confidenceLevel"], "insufficient")

    def test_strategy_calibration_saves_observation_only_recommendations(self):
        with tempfile.TemporaryDirectory() as tmp:
            backend = self._backend(tmp)
            with backend.connect() as conn:
                for i in range(20):
                    conn.execute("""
                        INSERT INTO strategy_signals (
                            signal_date, strategy, side, symbol, name, decision, score, price,
                            return_5d, hit_5d, created_at, updated_at
                        ) VALUES (?, 'weak_model', 'BUY_CANDIDATE', ?, '測試', '買進', 50, 100,
                                  -0.02, 0, ?, ?)
                    """, (
                        f"2026-06-{i + 1:02d}",
                        f"Z{i:03d}",
                        f"2026-06-{i + 1:02d} 09:00:00",
                        f"2026-06-{i + 1:02d} 09:00:00",
                    ))

            result = backend.save_strategy_calibration_suggestions(
                calibration_date="2026-07-09",
                min_samples=20,
            )
            rows = backend.list_strategy_calibration()
            weak = next(row for row in rows if row["strategy"] == "weak_model")

            self.assertTrue(result["ok"])
            self.assertEqual(weak["mode"], "observation")
            self.assertEqual(weak["sample_count"], 20)
            self.assertEqual(weak["suggested_action"], "lower_weight_and_raise_threshold")
            self.assertEqual(weak["applied"], 0)
            self.assertEqual(weak["apply_ready"], 0)
            self.assertLess(weak["average_return_5d"], 0)
            with backend.connect() as conn:
                meta = dict(conn.execute("""
                    SELECT key, value FROM model_meta
                    WHERE key IN (
                        'last_strategy_calibration_date',
                        'last_strategy_calibration_saved',
                        'last_strategy_calibration_adjustment_candidates'
                    )
                """).fetchall())
            self.assertEqual(meta["last_strategy_calibration_date"], "2026-07-09")
            self.assertEqual(meta["last_strategy_calibration_saved"], str(result["saved"]))
            self.assertEqual(meta["last_strategy_calibration_adjustment_candidates"], "1")

    def test_strategy_calibration_schedule_status_is_atomic_and_recoverable(self):
        with tempfile.TemporaryDirectory() as tmp:
            backend = self._backend(tmp)
            with backend.connect() as conn:
                conn.execute("""
                    INSERT INTO strategy_signals (
                        signal_date, strategy, side, symbol, name, decision, score, price,
                        return_5d, hit_5d, created_at, updated_at
                    ) VALUES (
                        '2026-07-01', 'schedule_probe', 'BUY_CANDIDATE', '2330', '台積電',
                        '買進', 60, 100, 0.01, 1, '2026-07-01 09:00:00', '2026-07-01 09:00:00'
                    )
                """)
            result = backend.save_strategy_calibration_suggestions(
                calibration_date="2026-07-10",
                min_samples=20,
                schedule_job_id="1705_strategy_calibration",
            )
            with backend.connect() as conn:
                status = dict(conn.execute("""
                    SELECT key, value FROM model_meta
                    WHERE key LIKE 'auto_schedule_1705_strategy_calibration_%'
                """).fetchall())
                conn.execute("DELETE FROM model_meta WHERE key = 'auto_schedule_1705_strategy_calibration_date'")
                conn.execute("UPDATE model_meta SET value = '2026-07-09' WHERE key = 'last_strategy_calibration_date'")

            self.assertEqual(status["auto_schedule_1705_strategy_calibration_date"], "2026-07-10")
            self.assertEqual(status["auto_schedule_1705_strategy_calibration_status"], "success")
            self.assertEqual(status["auto_schedule_1705_strategy_calibration_attempt_count"], "0")
            self.assertIn("策略校準觀察", result["scheduleMessage"])

            recovered = backend.reconcile_strategy_calibration_schedule_state(today="2026-07-10")
            with backend.connect() as conn:
                fixed = dict(conn.execute("""
                    SELECT key, value FROM model_meta
                    WHERE key IN (
                        'last_strategy_calibration_date',
                        'auto_schedule_1705_strategy_calibration_date',
                        'auto_schedule_1705_strategy_calibration_status'
                    )
                """).fetchall())
            self.assertTrue(recovered["recovered"])
            self.assertEqual(fixed["last_strategy_calibration_date"], "2026-07-10")
            self.assertEqual(fixed["auto_schedule_1705_strategy_calibration_date"], "2026-07-10")
            self.assertEqual(fixed["auto_schedule_1705_strategy_calibration_status"], "success_recovered")

    def test_updates_mid_and_long_horizon_outcomes(self):
        with tempfile.TemporaryDirectory() as tmp:
            backend = self._backend(tmp)
            with backend.connect() as conn:
                conn.execute("""
                    INSERT INTO strategy_signals (
                        signal_date, strategy, side, symbol, name, decision, score, price,
                        created_at, updated_at
                    ) VALUES (
                        '2026-01-01', 'model', 'BUY_CANDIDATE', '2330', '台積電', '買進',
                        80, 100, '2026-01-01 09:00:00', '2026-01-01 09:00:00'
                    )
                """)
                start = dt.date(2026, 1, 1)
                for i in range(61):
                    trade_date = start + dt.timedelta(days=i)
                    close = 100 + i
                    conn.execute("""
                        INSERT INTO prices (
                            symbol, date, open, high, low, close, volume, price_source, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        "2330",
                        trade_date.isoformat(),
                        close, close + 1, close - 1, close, 1000000,
                        "TWSE official test",
                        "2026-03-10 00:00:00",
                    ))

            updated = backend.update_strategy_signal_outcomes(limit=10)
            with backend.connect() as conn:
                conn.row_factory = __import__("sqlite3").Row
                row = conn.execute("""
                    SELECT return_20d, return_60d, hit_20d, hit_60d
                    FROM strategy_signals
                    WHERE strategy = 'model' AND symbol = '2330'
                """).fetchone()

            self.assertEqual(updated, 1)
            self.assertAlmostEqual(row["return_20d"], 0.20)
            self.assertAlmostEqual(row["return_60d"], 0.60)
            self.assertEqual(row["hit_20d"], 1)
            self.assertEqual(row["hit_60d"], 1)

    def test_strategy_signal_records_trade_horizon(self):
        with tempfile.TemporaryDirectory() as tmp:
            backend = self._backend(tmp)

            result = backend.record_strategy_signals({
                "signals": [{
                    "signalDate": "2026-07-09",
                    "strategy": "monster_brain_short_trade",
                    "side": "BUY_CANDIDATE",
                    "symbol": "2330",
                    "name": "台積電",
                    "decision": "可列入買進觀察",
                    "price": 100,
                    "tradeHorizon": "short_trade",
                    "tradeHorizonLabel": "短期短炒",
                    "tradeHorizonDays": "1-5日",
                    "tradeHorizonScore": 0.88,
                }]
            })
            stats = backend.strategy_signal_performance(refresh_outcomes=False)
            signal = stats["recentSignals"][0]

            self.assertTrue(result["ok"])
            self.assertEqual(signal["strategy"], "monster_brain_short_trade")
            self.assertEqual(signal["tradeHorizon"], "short_trade")
            self.assertEqual(signal["tradeHorizonLabel"], "短期短炒")
            self.assertEqual(signal["tradeHorizonDays"], "1-5日")
            self.assertAlmostEqual(signal["tradeHorizonScore"], 0.88)

    def test_strategy_signal_sessions_do_not_overwrite_same_day_signal(self):
        with tempfile.TemporaryDirectory() as tmp:
            backend = self._backend(tmp)

            result = backend.record_strategy_signals({
                "signals": [
                    {
                        "signalDate": "2026-07-09",
                        "signalSession": "open_0905",
                        "signalSessionLabel": "開盤初篩",
                        "signalTime": "09:05",
                        "strategy": "monster_brain_short_trade",
                        "side": "BUY_CANDIDATE",
                        "symbol": "2330",
                        "name": "台積電",
                        "decision": "開盤可買",
                        "price": 100,
                    },
                    {
                        "signalDate": "2026-07-09",
                        "signalSession": "close_1520",
                        "signalSessionLabel": "收盤後",
                        "signalTime": "15:20",
                        "strategy": "monster_brain_short_trade",
                        "side": "BUY_CANDIDATE",
                        "symbol": "2330",
                        "name": "台積電",
                        "decision": "收盤可買",
                        "price": 105,
                    },
                ]
            })
            stats = backend.strategy_signal_performance(refresh_outcomes=False)
            signals = sorted(stats["recentSignals"], key=lambda row: row["signalTime"])

            self.assertTrue(result["ok"])
            self.assertEqual(len(signals), 2)
            self.assertEqual([row["signalSession"] for row in signals], ["open_0905", "close_1520"])
            self.assertEqual([row["signalSessionLabel"] for row in signals], ["開盤初篩", "收盤後"])
            self.assertEqual([row["price"] for row in signals], [100.0, 105.0])
            self.assertEqual(
                [row["strategy"] for row in signals],
                ["monster_brain_short_trade_open_0905", "monster_brain_short_trade_close_1520"],
            )

    def test_strategy_signal_performance_groups_by_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            backend = self._backend(tmp)
            with backend.connect() as conn:
                conn.execute("""
                    INSERT INTO strategy_signals (
                        signal_date, signal_session, signal_session_label, signal_time,
                        strategy, side, symbol, name, decision, score, price,
                        return_5d, return_20d, hit_5d, hit_20d, created_at, updated_at
                    ) VALUES
                    ('2026-07-09', 'open_0905', '開盤初篩', '09:05',
                     'monster_open', 'BUY_CANDIDATE', '2330', '台積電', '開盤買', 80, 100,
                     0.04, 0.08, 1, 1, '2026-07-09 09:05:00', '2026-07-09 09:05:00'),
                    ('2026-07-09', 'close_1520', '收盤後', '15:20',
                     'monster_close', 'BUY_CANDIDATE', '2317', '鴻海', '收盤買', 70, 200,
                     -0.01, 0.09, 0, 1, '2026-07-09 15:20:00', '2026-07-09 15:20:00')
                """)

            stats = backend.strategy_signal_performance(refresh_outcomes=False)
            groups = {item["key"]: item for item in stats["sessionGroups"]}

            self.assertIn("open_0905", groups)
            self.assertIn("close_1520", groups)
            self.assertEqual(groups["open_0905"]["label"], "開盤初篩")
            self.assertEqual(groups["open_0905"]["primary"], "5d")
            self.assertEqual(groups["open_0905"]["primaryMetrics"]["precision"], 1.0)
            self.assertEqual(groups["close_1520"]["primary"], "20d")
            self.assertEqual(groups["close_1520"]["horizons"]["5d"]["precision"], 0.0)
            self.assertEqual(groups["close_1520"]["horizons"]["20d"]["precision"], 1.0)
            self.assertEqual(stats["overall"]["sessionGroups"][0]["key"], "open_0905")

    def test_paper_trades_use_realistic_next_open_and_stop_loss(self):
        with tempfile.TemporaryDirectory() as tmp:
            backend = self._backend(tmp)
            with backend.connect() as conn:
                conn.execute("""
                    INSERT INTO strategy_signals (
                        signal_date, signal_session, signal_session_label, signal_time,
                        strategy, side, symbol, name, decision, price, stop_price, target_price,
                        created_at, updated_at
                    ) VALUES (
                        '2026-07-01', 'close_1520', '收盤後', '15:20',
                        'model_close', 'BUY_CANDIDATE', '2330', '台積電', '收盤買', 100, 95, 112,
                        '2026-07-01 15:20:00', '2026-07-01 15:20:00'
                    )
                """)
                for date, open_price, high, low, close in [
                    ("2026-07-01", 100, 101, 99, 100),
                    ("2026-07-02", 102, 104, 101, 103),
                    ("2026-07-03", 103, 104, 94, 96),
                ]:
                    conn.execute("""
                        INSERT INTO prices (
                            symbol, date, open, high, low, close, volume, price_source, updated_at
                        ) VALUES ('2330', ?, ?, ?, ?, ?, 1000000, 'TWSE official test', '2026-07-04 00:00:00')
                    """, (date, open_price, high, low, close))

            stats = backend.strategy_signal_performance(refresh_outcomes=False)
            trade = stats["paperTrades"]["closed"][0]

            self.assertEqual(trade["entryFillMode"], "next_open")
            self.assertEqual(trade["entryFillDate"], "2026-07-02")
            self.assertEqual(trade["entryPrice"], 102.0)
            self.assertEqual(trade["exitReason"], "stop_loss")
            self.assertEqual(trade["exitDate"], "2026-07-03")
            self.assertEqual(trade["exitPrice"], 95.0)
            self.assertLess(trade["returnPct"], 0)
            self.assertLess(trade["netPnlPerLot"], trade["pnlPerLot"])

    def test_paper_trade_same_day_signal_can_remain_unfilled(self):
        with tempfile.TemporaryDirectory() as tmp:
            backend = self._backend(tmp)
            with backend.connect() as conn:
                conn.execute("""
                    INSERT INTO strategy_signals (
                        signal_date, signal_session, signal_session_label, signal_time,
                        strategy, side, symbol, name, decision, price, buy_point,
                        created_at, updated_at
                    ) VALUES (
                        '2026-07-01', 'open_0905', '開盤初篩', '09:05',
                        'model_open', 'BUY_CANDIDATE', '2330', '台積電', '開盤買', 120, 120,
                        '2026-07-01 09:05:00', '2026-07-01 09:05:00'
                    )
                """)
                conn.execute("""
                    INSERT INTO prices (
                        symbol, date, open, high, low, close, volume, price_source, updated_at
                    ) VALUES ('2330', '2026-07-01', 100, 110, 99, 105, 1000000, 'TWSE official test', '2026-07-01 15:00:00')
                """)

            stats = backend.strategy_signal_performance(refresh_outcomes=False)

            self.assertEqual(stats["paperTrades"]["openCount"], 0)
            self.assertEqual(stats["paperTrades"]["unfilledBuySignals"], 1)
            self.assertEqual(stats["paperTrades"]["unfilledBuys"][0]["entryFillMode"], "not_touched")

    def test_open_paper_trade_includes_estimated_net_pnl(self):
        with tempfile.TemporaryDirectory() as tmp:
            backend = self._backend(tmp)
            with backend.connect() as conn:
                conn.execute("""
                    INSERT INTO strategy_signals (
                        signal_date, signal_session, signal_session_label, signal_time,
                        strategy, side, symbol, name, decision, price, created_at, updated_at
                    ) VALUES (
                        '2026-07-01', 'close_1520', '收盤後', '15:20',
                        'model_close', 'BUY_CANDIDATE', '2330', '台積電', '收盤買', 100,
                        '2026-07-01 15:20:00', '2026-07-01 15:20:00'
                    )
                """)
                for date, open_price, high, low, close in [
                    ("2026-07-01", 100, 101, 99, 100),
                    ("2026-07-02", 102, 104, 101, 103),
                    ("2026-07-03", 109, 111, 108, 110),
                ]:
                    conn.execute("""
                        INSERT INTO prices (
                            symbol, date, open, high, low, close, volume, price_source, updated_at
                        ) VALUES ('2330', ?, ?, ?, ?, ?, 1000000, 'TWSE official test', '2026-07-04 00:00:00')
                    """, (date, open_price, high, low, close))

            paper = backend.strategy_signal_performance(refresh_outcomes=False)["paperTrades"]
            trade = paper["open"][0]

            self.assertEqual(paper["openCount"], 1)
            self.assertEqual(trade["entryPrice"], 102.0)
            self.assertEqual(trade["latestPrice"], 110.0)
            self.assertEqual(trade["unrealizedPnlPerLot"], 8000.0)
            # Fees/tax are 632 and estimated two-sided slippage is 212.
            self.assertEqual(trade["totalSlippageCost"], 212)
            self.assertEqual(trade["totalCosts"], 844)
            self.assertEqual(trade["unrealizedNetPnlPerLot"], 7156.0)
            self.assertAlmostEqual(trade["unrealizedNetReturnPct"], 7156 / 102247)
            self.assertEqual(paper["openNetPnlPerLot"], 7156.0)
            self.assertEqual(paper["totalNetPnlPerLot"], 7156.0)

    def test_paper_buy_rejects_locked_limit_up_and_insufficient_liquidity(self):
        with tempfile.TemporaryDirectory() as tmp:
            backend = self._backend(tmp)
            with backend.connect() as conn:
                conn.execute("""
                    INSERT INTO strategy_signals (
                        signal_date, signal_session, signal_session_label, signal_time,
                        strategy, side, symbol, name, decision, price, created_at, updated_at
                    ) VALUES
                    ('2026-07-01', 'close_1520', '收盤後', '15:20',
                     'model_close', 'BUY_CANDIDATE', '2330', '台積電', '收盤買', 100,
                     '2026-07-01 15:20:00', '2026-07-01 15:20:00'),
                    ('2026-07-01', 'close_1520', '收盤後', '15:20',
                     'model_close', 'BUY_CANDIDATE', '2317', '鴻海', '收盤買', 100,
                     '2026-07-01 15:20:00', '2026-07-01 15:20:00')
                """)
                for symbol, rows in {
                    "2330": [
                        ("2026-07-01", 100, 101, 99, 100, 1000000),
                        ("2026-07-02", 110, 110, 110, 110, 1000000),
                    ],
                    "2317": [
                        ("2026-07-01", 100, 101, 99, 100, 1000000),
                        ("2026-07-02", 100, 101, 99, 100, 10000),
                    ],
                }.items():
                    for date, open_price, high, low, close, volume in rows:
                        conn.execute("""
                            INSERT INTO prices (
                                symbol, date, open, high, low, close, volume, price_source, updated_at
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'TWSE official test', '2026-07-03 00:00:00')
                        """, (symbol, date, open_price, high, low, close, volume))

            paper = backend.strategy_signal_performance(refresh_outcomes=False)["paperTrades"]
            modes = {trade["symbol"]: trade["entryFillMode"] for trade in paper["unfilledBuys"]}

            self.assertEqual(paper["openCount"], 0)
            self.assertEqual(paper["unfilledBuySignals"], 2)
            self.assertEqual(modes["2330"], "locked_limit_up")
            self.assertEqual(modes["2317"], "insufficient_liquidity")
            self.assertEqual(paper["unfilledBuyReasonCounts"]["locked_limit_up"], 1)

    def test_model_sell_does_not_fill_on_locked_limit_down(self):
        with tempfile.TemporaryDirectory() as tmp:
            backend = self._backend(tmp)
            with backend.connect() as conn:
                conn.execute("""
                    INSERT INTO strategy_signals (
                        signal_date, signal_session, signal_session_label, signal_time,
                        strategy, side, symbol, name, decision, price, created_at, updated_at
                    ) VALUES
                    ('2026-07-01', 'close_1520', '收盤後', '15:20',
                     'model_close', 'BUY_CANDIDATE', '2330', '台積電', '收盤買', 100,
                     '2026-07-01 15:20:00', '2026-07-01 15:20:00'),
                    ('2026-07-02', 'close_1520', '收盤後', '15:20',
                     'model_exit', 'EXIT_CONFIRM', '2330', '台積電', '收盤賣', 100,
                     '2026-07-02 15:20:00', '2026-07-02 15:20:00')
                """)
                for date, open_price, high, low, close in [
                    ("2026-07-01", 100, 101, 99, 100),
                    ("2026-07-02", 100, 101, 99, 100),
                    ("2026-07-03", 90, 90, 90, 90),
                ]:
                    conn.execute("""
                        INSERT INTO prices (
                            symbol, date, open, high, low, close, volume, price_source, updated_at
                        ) VALUES ('2330', ?, ?, ?, ?, ?, 1000000, 'TWSE official test', '2026-07-04 00:00:00')
                    """, (date, open_price, high, low, close))

            paper = backend.strategy_signal_performance(refresh_outcomes=False)["paperTrades"]

            self.assertEqual(paper["closedCount"], 0)
            self.assertEqual(paper["openCount"], 1)
            self.assertEqual(paper["unfilledSellSignals"], 1)
            self.assertEqual(paper["unfilledSells"][0]["exitFillMode"], "locked_limit_down")

    def test_stop_loss_waits_through_locked_limit_down_then_uses_gap_open(self):
        with tempfile.TemporaryDirectory() as tmp:
            backend = self._backend(tmp)
            with backend.connect() as conn:
                conn.execute("""
                    INSERT INTO strategy_signals (
                        signal_date, signal_session, signal_session_label, signal_time,
                        strategy, side, symbol, name, decision, price, stop_price,
                        created_at, updated_at
                    ) VALUES (
                        '2026-07-01', 'close_1520', '收盤後', '15:20',
                        'model_close', 'BUY_CANDIDATE', '2330', '台積電', '收盤買', 100, 95,
                        '2026-07-01 15:20:00', '2026-07-01 15:20:00'
                    )
                """)
                for date, open_price, high, low, close in [
                    ("2026-07-01", 100, 101, 99, 100),
                    ("2026-07-02", 100, 101, 99, 100),
                    ("2026-07-03", 90, 90, 90, 90),
                    ("2026-07-04", 85, 90, 84, 88),
                ]:
                    conn.execute("""
                        INSERT INTO prices (
                            symbol, date, open, high, low, close, volume, price_source, updated_at
                        ) VALUES ('2330', ?, ?, ?, ?, ?, 1000000, 'TWSE official test', '2026-07-05 00:00:00')
                    """, (date, open_price, high, low, close))

            trade = backend.strategy_signal_performance(refresh_outcomes=False)["paperTrades"]["closed"][0]

            self.assertEqual(trade["exitReason"], "stop_loss")
            self.assertEqual(trade["exitDate"], "2026-07-04")
            self.assertEqual(trade["exitPrice"], 85.0)
            self.assertEqual(trade["exitFillMode"], "gap_open")
            self.assertEqual(trade["blockedExitDays"], 1)

    def test_paper_account_rejects_buy_when_cash_is_insufficient(self):
        with tempfile.TemporaryDirectory() as tmp:
            backend = self._backend(tmp)
            with backend.connect() as conn:
                conn.execute("""
                    INSERT INTO strategy_signals (
                        signal_date, signal_session, signal_session_label, signal_time,
                        strategy, side, symbol, name, decision, price, created_at, updated_at
                    ) VALUES
                    ('2026-07-01', 'close_1520', '收盤後', '15:20',
                     'model_close', 'BUY_CANDIDATE', '2317', '鴻海', '收盤買', 1600,
                     '2026-07-01 15:20:00', '2026-07-01 15:20:00'),
                    ('2026-07-01', 'close_1520', '收盤後', '15:20',
                     'model_close', 'BUY_CANDIDATE', '2330', '台積電', '收盤買', 1600,
                     '2026-07-01 15:20:01', '2026-07-01 15:20:01')
                """)
                for symbol in ("2317", "2330"):
                    for date in ("2026-07-01", "2026-07-02"):
                        conn.execute("""
                            INSERT INTO prices (
                                symbol, date, open, high, low, close, volume, price_source, updated_at
                            ) VALUES (?, ?, 1600, 1610, 1590, 1600, 1000000,
                                      'TWSE official test', '2026-07-03 00:00:00')
                        """, (symbol, date))

            paper = backend.strategy_signal_performance(refresh_outcomes=False)["paperTrades"]

            self.assertEqual(paper["simulation"]["initialCapital"], 2_500_000)
            self.assertEqual(paper["openCount"], 1)
            self.assertEqual(paper["capitalRejectedBuySignals"], 1)
            self.assertEqual(paper["unfilledBuyReasonCounts"]["insufficient_paper_cash"], 1)
            self.assertLess(paper["cashBalance"], 1_000_000)
            self.assertLessEqual(paper["peakOpenPositions"], paper["simulation"]["maxOpenPositions"])
            rejected = next(
                trade for trade in paper["unfilledBuys"]
                if trade["entryFillMode"] == "insufficient_paper_cash"
            )
            self.assertGreater(rejected["entryCapitalRequired"], rejected["paperCashAvailable"])

    def test_short_horizon_position_exits_on_next_open_after_five_sessions(self):
        with tempfile.TemporaryDirectory() as tmp:
            backend = self._backend(tmp)
            with backend.connect() as conn:
                conn.execute("""
                    INSERT INTO strategy_signals (
                        signal_date, signal_session, signal_session_label, signal_time,
                        strategy, side, symbol, name, decision, price,
                        trade_horizon, trade_horizon_label, trade_horizon_days,
                        created_at, updated_at
                    ) VALUES (
                        '2026-07-01', 'close_1520', '收盤後', '15:20',
                        'model_short', 'BUY_CANDIDATE', '2330', '台積電', '短線買', 100,
                        'short_trade', '短期短炒', '1-5日',
                        '2026-07-01 15:20:00', '2026-07-01 15:20:00'
                    )
                """)
                for day, open_price in enumerate(range(100, 108), start=1):
                    date = f"2026-07-{day:02d}"
                    conn.execute("""
                        INSERT INTO prices (
                            symbol, date, open, high, low, close, volume, price_source, updated_at
                        ) VALUES ('2330', ?, ?, ?, ?, ?, 1000000,
                                  'TWSE official test', '2026-07-09 00:00:00')
                    """, (date, open_price, open_price + 1, open_price - 1, open_price))

            trade = backend.strategy_signal_performance(refresh_outcomes=False)["paperTrades"]["closed"][0]

            self.assertEqual(trade["entryFillDate"], "2026-07-02")
            self.assertEqual(trade["exitReason"], "time_exit")
            self.assertEqual(trade["exitFillMode"], "horizon_next_open")
            self.assertEqual(trade["exitDate"], "2026-07-08")
            self.assertEqual(trade["maxHoldingSessions"], 5)
            self.assertEqual(trade["heldSessions"], 6)

    def test_orphan_sells_are_classified_by_cause(self):
        with tempfile.TemporaryDirectory() as tmp:
            backend = self._backend(tmp)
            with backend.connect() as conn:
                conn.execute("""
                    INSERT INTO strategy_signals (
                        signal_date, signal_session, signal_session_label, signal_time,
                        strategy, side, symbol, name, decision, price, buy_point,
                        created_at, updated_at
                    ) VALUES
                    ('2026-07-01', 'open_0905', '開盤初篩', '09:05',
                     'model_open', 'BUY_CANDIDATE', '2330', '台積電', '開盤買', 120, 120,
                     '2026-07-01 09:05:00', '2026-07-01 09:05:00'),
                    ('2026-07-02', 'close_1520', '收盤後', '15:20',
                     'model_exit', 'EXIT_CONFIRM', '2330', '台積電', '模型賣', 100, NULL,
                     '2026-07-02 15:20:00', '2026-07-02 15:20:00'),
                    ('2026-07-02', 'close_1520', '收盤後', '15:20',
                     'portfolio_exit_brain', 'EXIT_CONFIRM', '2317', '鴻海', '持股出場', 200, NULL,
                     '2026-07-02 15:20:00', '2026-07-02 15:20:00')
                """)
                conn.execute("""
                    INSERT INTO prices (
                        symbol, date, open, high, low, close, volume, price_source, updated_at
                    ) VALUES ('2330', '2026-07-01', 100, 110, 99, 105, 1000000, 'TWSE official test', '2026-07-01 15:00:00')
                """)

            stats = backend.strategy_signal_performance(refresh_outcomes=False)
            paper = stats["paperTrades"]
            breakdown = {row["reason"]: row for row in paper["orphanSellBreakdown"]}

            self.assertEqual(paper["orphanSellSignals"], 2)
            self.assertEqual(paper["modelOrphanSellSignals"], 1)
            self.assertEqual(paper["externalHoldingExitSignals"], 1)
            self.assertEqual(paper["externalHoldingExitSymbols"], 1)
            self.assertEqual(breakdown["prior_buy_not_filled"]["count"], 1)
            self.assertEqual(breakdown["prior_buy_not_filled"]["modelCount"], 1)
            self.assertEqual(breakdown["real_holding_exit_without_paper_entry"]["modelCount"], 0)
            by_symbol = {row["symbol"]: row for row in paper["orphanSells"]}
            self.assertTrue(by_symbol["2330"]["isModelOrphan"])
            self.assertFalse(by_symbol["2317"]["isModelOrphan"])
            external_state = paper["externalHoldingExitStates"][0]
            self.assertEqual(external_state["symbol"], "2317")
            self.assertEqual(external_state["count"], 1)
            self.assertEqual(external_state["repeatDays"], 1)

    def test_external_holding_exit_states_group_repeated_portfolio_exit_by_symbol(self):
        with tempfile.TemporaryDirectory() as tmp:
            backend = self._backend(tmp)
            with backend.connect() as conn:
                conn.execute("""
                    INSERT INTO strategy_signals (
                        signal_date, signal_session, signal_session_label, signal_time,
                        strategy, side, symbol, name, decision, price, created_at, updated_at
                    ) VALUES
                    ('2026-07-01', 'close_1520', '收盤後', '15:20',
                     'portfolio_exit_brain', 'EXIT_CONFIRM', '2330', '台積電', '等待轉弱確認', 100,
                     '2026-07-01 15:20:00', '2026-07-01 15:20:00'),
                    ('2026-07-02', 'open_0905', '開盤初篩', '09:05',
                     'portfolio_exit_brain_open_0905', 'EXIT_CONFIRM', '2330', '台積電', '等待轉弱確認', 101,
                     '2026-07-02 09:05:00', '2026-07-02 09:05:00'),
                    ('2026-07-02', 'close_1520', '收盤後', '15:20',
                     'portfolio_exit_brain_close_1520', 'EXIT_CONFIRM', '2330', '台積電', '等待轉弱確認', 102,
                     '2026-07-02 15:20:00', '2026-07-02 15:20:00')
                """)

            stats = backend.strategy_signal_performance(refresh_outcomes=False)
            paper = stats["paperTrades"]
            state = paper["externalHoldingExitStates"][0]

            self.assertEqual(paper["externalHoldingExitSignals"], 3)
            self.assertEqual(paper["externalHoldingExitSymbols"], 1)
            self.assertEqual(state["symbol"], "2330")
            self.assertEqual(state["count"], 3)
            self.assertEqual(state["repeatDays"], 2)
            self.assertEqual(state["latestDate"], "2026-07-02")
            self.assertEqual(state["latestPrice"], 102.0)

    def test_real_trade_alignment_compares_real_buys_and_sells_to_model_signals(self):
        with tempfile.TemporaryDirectory() as tmp:
            backend = self._backend(tmp)
            with backend.connect() as conn:
                conn.execute("""
                    INSERT INTO strategy_signals (
                        signal_date, strategy, side, symbol, name, decision, price, created_at, updated_at
                    ) VALUES
                    ('2026-07-01', 'model_buy', 'BUY_CANDIDATE', '2330', '台積電', '模型買', 100,
                     '2026-07-01 09:00:00', '2026-07-01 09:00:00'),
                    ('2026-07-04', 'model_exit', 'EXIT_CONFIRM', '2330', '台積電', '模型賣', 108,
                     '2026-07-04 09:00:00', '2026-07-04 09:00:00')
                """)
                conn.execute("""
                    INSERT INTO trades (
                        created_at, buy_at, symbol, side, price, shares, status, filled_shares, filled_at,
                        exit_price, exit_at, pnl
                    ) VALUES
                    ('2026-07-02 09:31:00', '2026-07-02 09:31:00', '2330', 'BUY', 101, 1000, 'closed', 1000, '2026-07-02 09:31:00',
                     109, '2026-07-05 13:10:00', 8000),
                    ('2026-07-03 09:31:00', '2026-07-03 09:31:00', '2317', 'BUY', 200, 1000, 'filled', 1000, '2026-07-03 09:31:00',
                     NULL, NULL, NULL)
                """)
                conn.execute("""
                    INSERT INTO trades (
                        created_at, symbol, side, price, shares, status, filled_shares, filled_at
                    ) VALUES (
                        '2026-07-05 13:10:00', '2330', 'SELL', 109, 1000, 'filled', 1000, '2026-07-05 13:10:00'
                    )
                """)

            stats = backend.strategy_signal_performance(refresh_outcomes=False)
            alignment = stats["realTradeAlignment"]

            self.assertEqual(alignment["buyTrades"], 2)
            self.assertEqual(alignment["buyAligned"], 1)
            self.assertEqual(alignment["buyMissed"], 1)
            self.assertEqual(alignment["sellTrades"], 1)
            self.assertEqual(alignment["sellAligned"], 1)
            self.assertEqual(alignment["roundTrips"], 1)
            self.assertEqual(alignment["roundTripWins"], 1)
            self.assertEqual(alignment["roundTripTotalPnl"], 8000.0)
            self.assertAlmostEqual(alignment["roundTripAvgReturn"], 109 / 101 - 1)
            self.assertEqual(alignment["bothAlignedRoundTrips"], 1)
            self.assertEqual(alignment["roundTripRows"][0]["pnl"], 8000.0)
            self.assertTrue(alignment["roundTripRows"][0]["buyModelMatched"])
            self.assertTrue(alignment["roundTripRows"][0]["sellModelMatched"])
            missed = {(row["symbol"], row["side"]) for row in alignment["missed"]}
            self.assertIn(("2317", "BUY"), missed)


if __name__ == "__main__":
    unittest.main()
