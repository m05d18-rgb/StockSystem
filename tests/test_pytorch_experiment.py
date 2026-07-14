import datetime as dt
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import numpy as np

import pytorch_experiment as pe
import realtime_tick_collector as rtc
from realtime_tick_collector import TickCollector


def daily_rows(count=130, start="2024-01-02", close=100.0):
    start_date = dt.date.fromisoformat(start)
    rows = []
    for index in range(count):
        date = start_date + dt.timedelta(days=index)
        rows.append({
            "date": date.isoformat(),
            "open": close,
            "high": close * 1.02,
            "low": close * 0.98,
            "close": close,
            "volume": 1_000_000,
            "foreign_buy_sell": 0,
            "trust_buy_sell": 0,
            "margin_balance": 10_000,
            "day_trade_ratio": 20,
            "revenue_growth": 10,
            "price_source": "TWSE official",
        })
    return rows


class MultiTargetContractTests(unittest.TestCase):
    def test_target_uses_same_conservative_radar_trade_contract(self):
        rows = daily_rows()
        signal_index = 59
        rows[signal_index + 1]["high"] = 112
        rows[signal_index + 1]["low"] = 92
        target = pe.build_multitarget(rows, signal_index)
        self.assertIsNotNone(target)
        hit, regressions = target
        self.assertEqual(hit, 0)
        self.assertLess(regressions[0], 0)
        self.assertEqual(regressions.shape, (len(pe.REGRESSION_TARGETS),))

    def test_sequence_features_do_not_read_future_rows(self):
        rows = daily_rows()
        before, _ = pe.sequence_feature_matrix(rows)
        rows[90]["close"] = 9999
        rows[90]["volume"] = 999999999
        after, _ = pe.sequence_feature_matrix(rows)
        np.testing.assert_allclose(before[:60], after[:60])

    def test_horizon_return_includes_both_sides_costs_and_slippage(self):
        result = pe.cost_aware_horizon_return(100.1, 110.0)
        self.assertGreater(result, 0.09)
        self.assertLess(result, 0.10)


class PurgedSplitTests(unittest.TestCase):
    def test_chronological_split_keeps_sixty_session_embargoes(self):
        start = dt.date(2023, 1, 2)
        dates = np.asarray([
            (start + dt.timedelta(days=index)).isoformat()
            for index in range(500)
        ])
        train, validation, test, metadata = pe.chronological_split(dates)
        train_indexes = np.where(train)[0]
        validation_indexes = np.where(validation)[0]
        test_indexes = np.where(test)[0]
        self.assertGreaterEqual(validation_indexes[0] - train_indexes[-1] - 1, 60)
        self.assertGreaterEqual(test_indexes[0] - validation_indexes[-1] - 1, 60)
        self.assertEqual(metadata["embargoSessions"], 60)

    def test_small_smoke_run_can_never_pass_quality_gate(self):
        strong = {
            "auc": 0.9,
            "dailyTop5Precision": 0.8,
            "dailyTop5AvgNetReturn": 0.05,
        }
        weak = {
            "auc": 0.6,
            "dailyTop5Precision": 0.5,
            "dailyTop5AvgNetReturn": 0.01,
        }
        gate = pe._daily_gate(
            {"tcn": strong, "xgboost": weak, "lightgbm": weak},
            sample_count=2500,
            test_count=350,
            universe_count=12,
        )
        self.assertFalse(gate["dailyTcnQualified"])
        self.assertFalse(gate["sampleGatePassed"])

    def test_large_train_test_base_rate_shift_blocks_gate(self):
        strong = {
            "auc": 0.9,
            "dailyTop5Precision": 0.8,
            "dailyTop5AvgNetReturn": 0.05,
        }
        weak = {
            "auc": 0.6,
            "dailyTop5Precision": 0.5,
            "dailyTop5AvgNetReturn": 0.01,
        }
        gate = pe._daily_gate(
            {"tcn": strong, "xgboost": weak, "lightgbm": weak},
            sample_count=30000,
            test_count=4800,
            universe_count=200,
            train_positive_rate=0.15,
            test_positive_rate=0.40,
        )
        self.assertFalse(gate["dailyTcnQualified"])
        self.assertFalse(gate["regimeGatePassed"])


class IsolationStatusTests(unittest.TestCase):
    def test_status_never_enables_radar_or_formal_trading(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "experiment.sqlite3"
            with pe.connect(path) as conn:
                pe.ensure_experiment_schema(conn)
            status = pe.experiment_status(path)
        self.assertFalse(status["isolation"]["usedByRadar"])
        self.assertFalse(status["isolation"]["usedByFormalBuySell"])
        self.assertFalse(status["intradayBranch"]["enabledInProduction"])
        self.assertFalse(status["tftGate"]["enabledInProduction"])
        self.assertFalse(status["orderBookGate"]["enabledInProduction"])
        self.assertEqual(status["orderBookData"]["featureRows"], 0)
        self.assertTrue(status["schedule"]["orderBookCollection"]["automatic"])

    def test_status_persists_relocated_artifact_path_in_database(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = root / "experiment.sqlite3"
            artifact_root = root / "model_experiments"
            run_id = "20260710_191244_37c18b53"
            relocated = artifact_root / run_id
            relocated.mkdir(parents=True)
            with pe.connect(path) as conn:
                pe.ensure_experiment_schema(conn)
                conn.execute("""
                    INSERT INTO model_experiment_runs (
                        run_id, experiment_version, status, mode, started_at,
                        artifact_path
                    ) VALUES (?, 'tcn-v1', 'completed', 'observation', ?, ?)
                """, (
                    run_id,
                    "2026-07-10 19:12:44",
                    str(root / "missing-c-drive" / run_id),
                ))
            with patch.object(pe, "ARTIFACT_ROOT", artifact_root):
                status = pe.experiment_status(path)
            with pe.connect(path) as conn:
                stored = conn.execute(
                    "SELECT artifact_path FROM model_experiment_runs WHERE run_id = ?",
                    (run_id,),
                ).fetchone()[0]

        self.assertEqual(stored, str(relocated))
        self.assertEqual(status["latestRun"]["artifactPath"], str(relocated))
        self.assertTrue(status["latestRun"]["artifactPathRelocated"])


class IntradayAggregationTests(unittest.TestCase):
    class Tick:
        code = "2330"
        intraday_odd = False

        def __init__(self, when, price, volume, tick_type, amount):
            self.datetime = when
            self.close = price
            self.volume = volume
            self.tick_type = tick_type
            self.amount = amount

    class BidAsk:
        code = "2330"
        intraday_odd = False
        simtrade = False
        suspend = False

        def __init__(self, when):
            self.datetime = when
            self.bid_price = [100, 99.9, 99.8, 99.7, 99.6]
            self.bid_volume = [100, 80, 60, 40, 20]
            self.diff_bid_vol = [2, 1, 0, -1, 0]
            self.ask_price = [100.1, 100.2, 100.3, 100.4, 100.5]
            self.ask_volume = [50, 40, 30, 20, 10]
            self.diff_ask_vol = [-1, 0, 1, 0, 0]

    def test_real_ticks_form_five_minute_ohlc_and_directional_flow(self):
        collector = TickCollector("x", "y", True)
        collector.on_tick(None, self.Tick(dt.datetime(2026, 7, 10, 9, 1), 100, 10, 1, 1_000_000))
        collector.on_tick(None, self.Tick(dt.datetime(2026, 7, 10, 9, 4), 102, 5, 2, 510_000))
        collector.on_tick(None, self.Tick(dt.datetime(2026, 7, 10, 9, 6), 101, 3, 0, 303_000))
        bucket = collector.stats_for("2330", "2026-07-10")
        first = bucket.minute_bars["09:00"]
        second = bucket.minute_bars["09:05"]
        self.assertEqual((first.open, first.high, first.low, first.close), (100, 102, 100, 102))
        self.assertEqual(first.volume_lots, 15)
        self.assertEqual(first.active_buy_volume_lots, 10)
        self.assertEqual(first.active_sell_volume_lots, 5)
        self.assertEqual(second.unknown_volume_lots, 3)
        self.assertEqual(second.unknown_tick_count, 1)
        self.assertTrue(first.dirty)

    def test_flush_persists_real_minute_bar_without_recounting(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "ticks.sqlite3"
            with pe.connect(path) as conn:
                pe.ensure_experiment_schema(conn)
                conn.execute("""CREATE TABLE realtime_flow_staging (
                    symbol TEXT, date TEXT, realtime_money_flow REAL,
                    realtime_large_order_flow REAL, tick_count INTEGER,
                    raw_tick_count INTEGER, unknown_tick_count INTEGER,
                    total_volume_lots REAL, last_tick_at TEXT, source TEXT,
                    updated_at TEXT, PRIMARY KEY(symbol, date)
                )""")
                conn.execute("""CREATE TABLE intraday_volume_profile (
                    symbol TEXT, date TEXT, minute TEXT,
                    cumulative_volume_lots REAL, source TEXT, updated_at TEXT,
                    PRIMARY KEY(symbol, date, minute)
                )""")

            class TestBackend:
                @staticmethod
                def connect():
                    return pe.connect(path)

            collector = TickCollector("x", "y", True)
            collector.on_tick(
                None,
                self.Tick(dt.datetime(2026, 7, 10, 9, 1), 100, 10, 1, 1_000_000),
            )
            with patch.object(rtc, "backend", TestBackend()):
                self.assertEqual(collector.flush(), 1)
                self.assertEqual(collector.flush(), 1)
            with pe.connect(path) as conn:
                row = conn.execute("""
                    SELECT open, high, low, close, volume_lots,
                           active_buy_volume_lots, raw_tick_count
                    FROM intraday_minute_bars
                """).fetchone()
            self.assertEqual(tuple(row), (100, 100, 100, 100, 10, 10, 1))

    def test_flush_persists_real_order_book_features(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "books.sqlite3"
            with pe.connect(path) as conn:
                pe.ensure_experiment_schema(conn)

            class TestBackend:
                @staticmethod
                def connect():
                    return pe.connect(path)

            collector = TickCollector("x", "y", True)
            collector.on_bidask(None, self.BidAsk(dt.datetime(2026, 7, 14, 9, 31)))
            with patch.object(rtc, "backend", TestBackend()):
                self.assertEqual(collector.flush(), 0)
            self.assertEqual(collector.last_orderbook_written, 1)
            with pe.connect(path) as conn:
                row = conn.execute("""
                    SELECT observation_count, avg_bid_depth_lots,
                           avg_ask_depth_lots, last_best_bid, last_best_ask
                    FROM order_book_5m_features
                """).fetchone()
            self.assertEqual(tuple(row), (1, 300, 150, 100, 100.1))


if __name__ == "__main__":
    unittest.main()
