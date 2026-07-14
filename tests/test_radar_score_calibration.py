import datetime as dt
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import ml_backend
from ml_backend import (
    DEFAULT_RADAR_ENTRY_GUARDRAILS,
    DEFAULT_RADAR_RULE_WEIGHTS,
    RADAR_MIN_FORMAL_SCORE,
    StockMLBackend,
    backend,
    classify_radar_market_regime,
    compute_sector_theme_snapshot,
    load_radar_rule_config,
    is_intraday_confirmed_entry_mode,
    radar_daily_watch_allowed,
    radar_entry_guardrail_decision,
    radar_execution_analysis,
    radar_exit_levels,
    precision_recall_thresholds,
    simulate_radar_trade_path,
)
import radar_walk_forward as rwf


def price_rows(start="2026-01-02", count=10, *, high=106.0, low=98.0, close=102.0):
    start_date = dt.date.fromisoformat(start)
    return [
        {
            "date": (start_date + dt.timedelta(days=index)).isoformat(),
            "open": 100.0,
            "high": high,
            "low": low,
            "close": close,
        }
        for index in range(count)
    ]


class RadarTradePathTests(unittest.TestCase):
    def test_fixed_precision_recall_thresholds_do_not_change_scores(self):
        result = precision_recall_thresholds(
            [
                {"score": 60, "targetHit": True, "netReturn": 0.09},
                {"score": 65, "targetHit": False, "netReturn": -0.07},
                {"score": 70, "targetHit": True, "netReturn": 0.09},
                {"score": 80, "targetHit": False, "netReturn": -0.07},
            ],
            [60, 70, 80],
        )
        points = {item["threshold"]: item for item in result["points"]}
        self.assertFalse(result["scoreChanged"])
        self.assertEqual(points[60.0]["precision"], 0.5)
        self.assertEqual(points[60.0]["recall"], 1.0)
        self.assertEqual(points[70.0]["precision"], 0.5)
        self.assertEqual(points[70.0]["recall"], 0.5)
        self.assertEqual(points[80.0]["recall"], 0.0)

    def test_execution_analysis_uses_costs_and_passes_normal_spread(self):
        result = radar_execution_analysis(100.0, 90.0, estimated_exit_slippage_pct=0.1)
        self.assertTrue(result["rewardRiskPassed"])
        self.assertAlmostEqual(result["stopPrice"], 93.0)
        self.assertAlmostEqual(result["targetPrice"], 110.0)
        self.assertGreater(result["targetNetReturnPct"], 9.0)
        self.assertLess(result["stopNetReturnPct"], -7.0)
        self.assertGreater(result["netRewardRiskRatio"], 1.20)

    def test_execution_analysis_blocks_when_spread_implies_high_exit_slippage(self):
        result = radar_execution_analysis(100.0, 93.0, estimated_exit_slippage_pct=0.4)
        self.assertFalse(result["rewardRiskPassed"])
        self.assertLess(result["netRewardRiskRatio"], result["minimumNetRewardRiskRatio"])

    def test_target_hit_uses_cost_aware_exit(self):
        rows = price_rows()
        rows[1]["high"] = 111.0
        result = simulate_radar_trade_path(100.0, rows)
        self.assertTrue(result["settled"])
        self.assertTrue(result["targetHit"])
        self.assertEqual(result["exitReason"], "take_profit_10pct")
        self.assertEqual(result["holdDays"], 2)
        self.assertGreater(result["netReturn"], 0.09)
        self.assertLess(result["netReturn"], 0.10)

    def test_same_day_target_and_stop_is_conservative_stop(self):
        rows = price_rows()
        rows[0]["high"] = 112.0
        rows[0]["low"] = 92.0
        result = simulate_radar_trade_path(100.0, rows)
        self.assertFalse(result["targetHit"])
        self.assertTrue(result["stopHit"])
        self.assertEqual(result["exitReason"], "stop_loss_same_day_conflict")

    def test_ten_day_time_exit_is_settled(self):
        rows = price_rows(close=105.0)
        result = simulate_radar_trade_path(100.0, rows)
        self.assertTrue(result["matured"])
        self.assertTrue(result["settled"])
        self.assertFalse(result["targetHit"])
        self.assertFalse(result["stopHit"])
        self.assertEqual(result["exitReason"], "time_exit_10d")

    def test_daily_watch_requires_real_score_floor(self):
        args = {
            "setup_ok": True,
            "danger_risk": False,
            "radar_override": False,
            "surge_setup": False,
            "counter_override": False,
            "month_high_strength": True,
        }
        self.assertFalse(radar_daily_watch_allowed(RADAR_MIN_FORMAL_SCORE - 0.01, **args))
        self.assertTrue(radar_daily_watch_allowed(RADAR_MIN_FORMAL_SCORE, **args))
        self.assertFalse(radar_daily_watch_allowed(64.99, minimum_score=65, **args))
        self.assertTrue(radar_daily_watch_allowed(65, minimum_score=65, **args))

    def test_display_exit_levels_match_ten_percent_and_seven_percent_contract(self):
        levels = radar_exit_levels(100.0, 100.0, 10.0)
        self.assertAlmostEqual(levels["stopPrice"], 93.0)
        self.assertAlmostEqual(levels["takeProfit"], 110.0)
        self.assertAlmostEqual(levels["rewardRiskRatio"], 10 / 7)


class RadarScoreTrackRecordTests(unittest.TestCase):
    def make_conn(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("""CREATE TABLE monster_scores (
            scan_date TEXT, symbol TEXT, price_date TEXT, score REAL,
            action TEXT, buy_allowed INTEGER, surge_setup INTEGER,
            invalid_for_trading INTEGER DEFAULT 0
        )""")
        conn.execute("CREATE TABLE stock_info (symbol TEXT, name TEXT, sector TEXT)")
        conn.execute("""CREATE TABLE prices (
            symbol TEXT, date TEXT, open REAL, high REAL, low REAL, close REAL
        )""")
        conn.execute("""CREATE TABLE radar_entry_snapshots (
            signal_date TEXT, symbol TEXT, scan_date TEXT, price_date TEXT,
            score REAL, setup_type TEXT, entry_at TEXT, entry_price REAL,
            entry_mode TEXT, quote_source TEXT, quote_age_seconds REAL,
            bid_price REAL, ask_price REAL, spread_pct REAL,
            estimated_slippage_pct REAL, created_at TEXT
        )""")
        return conn

    def insert_series(self, conn, symbol, event):
        rows = [(symbol, "2026-01-01", 100.0, 100.0, 100.0, 100.0)]
        for index in range(10):
            date = (dt.date(2026, 1, 2) + dt.timedelta(days=index)).isoformat()
            high, low, close = 106.0, 98.0, 102.0
            if event == "target" and index == 1:
                high = 112.0
            if event == "stop" and index == 0:
                low = 92.0
            if event == "snapshot_target" and index == 1:
                high = 117.0
            rows.append((symbol, date, 100.0, high, low, close))
        conn.executemany("INSERT INTO prices VALUES (?,?,?,?,?,?)", rows)

    def test_score_buckets_share_one_execution_policy_and_prefer_live_snapshot(self):
        conn = self.make_conn()
        candidates = [
            ("2026-01-01", "A", "2026-01-01", 45.0, "NEXT_DAY_WATCH", 1, 0),
            ("2026-01-01", "B", "2026-01-01", 55.0, "WAIT", 0, 0),
            ("2026-01-01", "C", "2026-01-01", 65.0, "WAIT", 0, 0),
            ("2026-01-01", "D", "2026-01-01", 95.0, "NEXT_DAY_WATCH", 1, 1),
        ]
        conn.executemany("""
            INSERT INTO monster_scores (
                scan_date, symbol, price_date, score, action, buy_allowed, surge_setup
            ) VALUES (?,?,?,?,?,?,?)
        """, candidates)
        conn.execute("""
            INSERT INTO monster_scores VALUES (
                '2026-01-02', 'E', '2026-01-01', 99, 'NEXT_DAY_WATCH', 1, 1, 1
            )
        """)
        conn.executemany("INSERT INTO stock_info VALUES (?,?,?)", [(code, code, "測試產業") for code in "ABCD"])
        self.insert_series(conn, "A", "target")
        self.insert_series(conn, "B", "stop")
        self.insert_series(conn, "C", "time")
        self.insert_series(conn, "D", "snapshot_target")
        self.insert_series(conn, "E", "target")
        conn.execute("""INSERT INTO radar_entry_snapshots VALUES (
            '2026-01-02','D','2026-01-01','2026-01-01',95,'breakout',
            '2026-01-02 09:31:00',105,'intraday_execution_analysis','test',0,
            104.5,105,0.47,0.24,'2026-01-02 09:31:00'
        )""")
        result = backend.compute_radar_score_track_record(lookback_days=365, conn=conn)
        self.assertEqual(result["overall"]["settled"], 4)
        self.assertEqual(result["overall"]["hits"], 2)
        self.assertEqual(result["entryModes"]["next_open_backtest"], 3)
        self.assertEqual(result["entryModes"]["intraday_execution_analysis"], 1)
        mode_performance = result["entryModePerformance"]
        self.assertEqual(mode_performance["intradayConfirmed"]["all"]["settled"], 1)
        self.assertEqual(mode_performance["intradayConfirmed"]["eligible"]["settled"], 1)
        self.assertEqual(mode_performance["nextOpenProxy"]["all"]["settled"], 3)
        self.assertEqual(mode_performance["nextOpenProxy"]["eligible"]["settled"], 1)
        by_label = {row["label"]: row for row in result["scoreBuckets"]}
        self.assertEqual(by_label["0-49"]["hits"], 1)
        self.assertEqual(by_label["50-59"]["stopRate"], 1.0)
        self.assertEqual(by_label["90-100"]["hits"], 1)
        self.assertEqual(result["policy"]["sameDayConflict"], "stop_first_conservative")
        self.assertEqual(result["policy"]["minimumFormalScore"], RADAR_MIN_FORMAL_SCORE)
        self.assertIn("diagnostics", result)
        regime_groups = {row["key"]: row for row in result["diagnostics"]["regimeGroups"]}
        self.assertEqual(regime_groups["theme_rotation"]["settled"], 2)
        conn.close()

    def test_rule_experiments_require_live_economics_and_never_apply_rules(self):
        observations = []

        def add(count, regime, win):
            for index in range(count):
                observations.append({
                    "symbol": f"{regime}-{win}-{index}",
                    "marketRegime": regime,
                    "themeHeat": 55,
                    "score": 75,
                    "change5": 8,
                    "surgeSetup": True,
                    "entryMode": "intraday_confirmed_ask",
                    "outcome": {
                        "settled": True,
                        "targetHit": win,
                        "stopHit": not win,
                        "netReturn": 0.10 if win else -0.05,
                        "holdDays": 3,
                        "maxFavorable": 0.12 if win else 0.02,
                        "maxAdverse": -0.02 if win else -0.07,
                    },
                })

        add(40, "risk_off", False)
        add(45, "theme_rotation", True)
        add(15, "theme_rotation", False)
        result = StockMLBackend._radar_observation_experiment_payload(observations)
        experiments = {
            item["key"]: item for item in result["live"]["experiments"]
        }

        self.assertFalse(result["productionRulesChanged"])
        self.assertEqual(result["live"]["baseline"]["settled"], 100)
        self.assertTrue(experiments["avoid_risk_off"]["researchQualified"])
        self.assertTrue(experiments["avoid_risk_off"]["adoptionCandidate"])
        self.assertFalse(experiments["avoid_risk_off"]["applied"])
        self.assertGreaterEqual(experiments["avoid_risk_off"]["targetHitLift"], 0.02)

    def test_strategy_experiment_daily_snapshot_is_immutable(self):
        with tempfile.TemporaryDirectory() as tmp:
            test_backend = StockMLBackend()
            test_backend.db_path = Path(tmp) / "stock_system.sqlite3"
            test_backend.init_db()
            experiments = StockMLBackend._radar_observation_experiment_payload([])
            track_record = {
                "latestScanDate": "2026-07-10",
                "observationExperiments": experiments,
                "diagnostics": {"underperformingGroups": []},
            }
            with patch.object(
                test_backend, "compute_radar_score_track_record", return_value=track_record
            ):
                first = test_backend.save_radar_strategy_experiment_snapshot("2026-07-10")
                second = test_backend.save_radar_strategy_experiment_snapshot("2026-07-10")
            with test_backend.connect() as conn:
                row_count = conn.execute(
                    "SELECT COUNT(*) FROM radar_strategy_experiment_runs"
                ).fetchone()[0]

        self.assertTrue(first["saved"])
        self.assertFalse(second["saved"])
        self.assertEqual(row_count, 1)


class RadarEntrySnapshotTests(unittest.TestCase):
    def test_shadow_confirmation_is_persisted_once_with_executable_price(self):
        with tempfile.TemporaryDirectory() as tmp:
            test_backend = StockMLBackend()
            test_backend.db_path = Path(tmp) / "stock_system.sqlite3"
            test_backend.init_db()
            candidates = [{
                "symbol": "9999",
                "scanDate": "2026-07-13",
                "priceDate": "2026-07-10",
                "score": 82,
            }]
            states = {"9999": {
                "canBuy": False,
                "shadowCanBuy": True,
                "executionEntryPrice": 101.25,
                "askPrice": 101.25,
                "currentPrice": 101.1,
                "setupType": "breakout",
                "source": "test quote",
                "snapshotAt": "2026-07-13 09:35:00",
                "quoteAgeSeconds": 1,
                "bidPrice": 101.2,
                "bidAskSpreadPct": 0.05,
                "estimatedSlippagePct": 0.1,
            }}
            first = test_backend.record_radar_entry_snapshots(
                candidates, states, signal_date="2026-07-13", return_details=True
            )
            second = test_backend.record_radar_entry_snapshots(
                candidates, states, signal_date="2026-07-13"
            )
            with test_backend.connect() as conn:
                row = conn.execute("""
                    SELECT entry_price, entry_mode
                    FROM radar_entry_snapshots
                    WHERE symbol = '9999'
                """).fetchone()
        self.assertTrue(first["ok"])
        self.assertEqual(first["eligibleStates"], 1)
        self.assertEqual(first["prepared"], 1)
        self.assertEqual(first["inserted"], 1)
        self.assertEqual(first["duplicates"], 0)
        self.assertEqual(first["persisted"], 1)
        self.assertEqual(first["missingSymbols"], [])
        self.assertEqual(second, 0)
        self.assertAlmostEqual(row[0], 101.25)
        self.assertEqual(row[1], "intraday_confirmed_shadow_execution")
        self.assertTrue(is_intraday_confirmed_entry_mode(row[1]))
        self.assertTrue(is_intraday_confirmed_entry_mode("intraday_execution_analysis"))

    def test_detailed_snapshot_result_reports_duplicate_as_persisted(self):
        with tempfile.TemporaryDirectory() as tmp:
            test_backend = StockMLBackend()
            test_backend.db_path = Path(tmp) / "stock_system.sqlite3"
            test_backend.init_db()
            candidates = [{"symbol": "9999", "scanDate": "2026-07-13"}]
            states = {"9999": {
                "shadowCanBuy": True,
                "executionEntryPrice": 100.0,
                "snapshotAt": "2026-07-13 09:35:00",
            }}
            test_backend.record_radar_entry_snapshots(
                candidates, states, signal_date="2026-07-13"
            )
            details = test_backend.record_radar_entry_snapshots(
                candidates, states, signal_date="2026-07-13", return_details=True
            )
        self.assertTrue(details["ok"])
        self.assertEqual(details["inserted"], 0)
        self.assertEqual(details["duplicates"], 1)
        self.assertEqual(details["persisted"], 1)

    def test_missing_executable_price_is_reported_as_pipeline_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            test_backend = StockMLBackend()
            test_backend.db_path = Path(tmp) / "stock_system.sqlite3"
            test_backend.init_db()
            details = test_backend.record_radar_entry_snapshots(
                [{"symbol": "9999", "scanDate": "2026-07-13"}],
                {"9999": {"shadowCanBuy": True}},
                signal_date="2026-07-13",
                return_details=True,
            )
        self.assertFalse(details["ok"])
        self.assertEqual(details["skippedNoPrice"], 1)
        self.assertEqual(details["missingSymbols"], ["9999"])


class RadarThemeRegimeTests(unittest.TestCase):
    def test_theme_heat_tracks_count_turnover_excess_and_ten_day_persistence(self):
        history = [{
            "date": f"2026-06-{day:02d}",
            "sectors": {"半導體": {"hot": True}},
        } for day in range(1, 10)]
        candidates = [
            {"symbol": f"S{i}", "sector": "半導體", "ret5": 12, "ret20": 25,
             "volumeRatio": 2.5, "turnoverMillion": 200}
            for i in range(5)
        ] + [
            {"symbol": f"O{i}", "sector": "其他", "ret5": 1, "ret20": 2,
             "volumeRatio": 1.2, "turnoverMillion": 20}
            for i in range(5)
        ]
        result = compute_sector_theme_snapshot(candidates, history=history)
        semi = result["sectors"]["半導體"]
        self.assertEqual(semi["count"], 5)
        self.assertGreater(semi["turnoverShare"], 0.8)
        self.assertGreater(semi["excessRet5"], 0)
        self.assertGreater(semi["excessRet20"], 0)
        self.assertEqual(semi["streakDays"], 10)
        self.assertTrue(semi["fermentation3"])
        self.assertTrue(semi["fermentation5"])
        self.assertTrue(semi["fermentation10"])
        self.assertGreaterEqual(semi["themeHeat"], 80)

    def test_market_regime_has_three_exhaustive_states(self):
        breadth = {"breadthSectorCount": 4, "hotSectorCount": 2, "themeHeatMax": 80}
        strong = classify_radar_market_regime({
            "taiex_ret_20": 0.08, "taiex_ma_gap": 0.04,
            "otc_ret_20": 0.10, "otc_ma_gap": 0.05,
        }, breadth)
        rotation = classify_radar_market_regime({
            "taiex_ret_20": 0.05, "taiex_ma_gap": 0.03,
            "otc_ret_20": -0.01, "otc_ma_gap": 0.01,
        }, {"breadthSectorCount": 2, "hotSectorCount": 1})
        risk_off = classify_radar_market_regime({
            "taiex_ret_20": -0.06, "taiex_ma_gap": -0.03,
            "otc_ret_20": -0.08, "otc_ma_gap": -0.04,
        }, breadth)
        self.assertEqual(strong["key"], "strong_breadth")
        self.assertEqual(rotation["key"], "theme_rotation")
        self.assertEqual(risk_off["key"], "risk_off")


class RadarWalkForwardTests(unittest.TestCase):
    def synthetic_records(self):
        records = []
        start = dt.date(2024, 1, 1)
        for day in range(430):
            date = (start + dt.timedelta(days=day)).isoformat()
            for index in range(10):
                records.append({
                    "symbol": f"M{index:02d}", "date": date, "volumeRatio": 4.0,
                    "monthHigh": False, "marketStrength": True, "surge": False,
                    "counter": False, "targetHit": False, "netReturn": -0.02,
                })
            for index in range(10):
                records.append({
                    "symbol": f"S{index:02d}", "date": date, "volumeRatio": 1.2,
                    "monthHigh": False, "marketStrength": True, "surge": True,
                    "counter": False, "targetHit": True, "netReturn": 0.08,
                })
        return records

    def no_chase_records(self):
        records = []
        start = dt.date(2023, 1, 1)
        for day in range(800):
            date = (start + dt.timedelta(days=day)).isoformat()
            for index in range(10):
                records.append({
                    "symbol": f"CHASE{index:02d}", "date": date,
                    "volumeRatio": 4.0, "monthHigh": True,
                    "marketStrength": True, "surge": True, "counter": False,
                    "ret5": 20.0, "targetHit": False, "netReturn": -0.07,
                })
            for index in range(10):
                records.append({
                    "symbol": f"CALM{index:02d}", "date": date,
                    "volumeRatio": 2.0, "monthHigh": True,
                    "marketStrength": True, "surge": False, "counter": False,
                    "ret5": 8.0, "targetHit": True, "netReturn": 0.10,
                })
        return records

    def test_walk_forward_uses_embargo_and_can_approve_rule_only_weights(self):
        surge_weights = {
            "volume": 20.0, "month_high": 0.0, "market_strength": 16.0,
            "surge": 64.0, "counter": 0.0,
        }
        result = rwf.walk_forward_calibrate(
            self.synthetic_records(),
            candidates=[dict(DEFAULT_RADAR_RULE_WEIGHTS), surge_weights],
        )
        self.assertTrue(result["approved"])
        self.assertEqual(result["effectiveWeights"], surge_weights)
        all_dates = sorted({row["date"] for row in self.synthetic_records()})
        positions = {date: index for index, date in enumerate(all_dates)}
        for fold in result["walkForward"]["folds"]:
            self.assertGreaterEqual(
                positions[fold["testStart"]] - positions[fold["trainEnd"]],
                rwf.EMBARGO_DAYS + 1,
            )
        threshold_calibration = result["regimeThresholdCalibration"]
        self.assertEqual(
            threshold_calibration["selectionBasis"],
            "precision_recall_walk_forward",
        )
        self.assertTrue(threshold_calibration["minimumFormalScorePreserved"])
        self.assertGreaterEqual(
            min(threshold_calibration["candidateThresholds"]),
            RADAR_MIN_FORMAL_SCORE,
        )

    def test_unapproved_config_keeps_default_weights_visible_as_observation(self):
        recommendation = {
            "volume": 30.0, "month_high": 20.0, "market_strength": 16.0,
            "surge": 28.0, "counter": 6.0,
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "weights.json"
            path.write_text(json.dumps({
                "method": "rule_only_walk_forward",
                "approved": False,
                "recommendedWeights": recommendation,
                "effectiveWeights": dict(DEFAULT_RADAR_RULE_WEIGHTS),
                "entryGuardrailCalibration": {
                    "approved": False,
                    "recommendedKey": "block_surge",
                    "recommendedRules": {"blockSurgeSetup": True, "maxRet5": None},
                },
            }), encoding="utf-8")
            config = load_radar_rule_config(path)
        self.assertEqual(config["source"], "walk_forward_observation")
        self.assertEqual(config["recommendedWeights"], recommendation)
        self.assertEqual(config["effectiveWeights"], DEFAULT_RADAR_RULE_WEIGHTS)
        guardrail = config["entryGuardrailCalibration"]
        self.assertFalse(guardrail["approved"])
        self.assertEqual(guardrail["effectiveRules"], DEFAULT_RADAR_ENTRY_GUARDRAILS)
        self.assertFalse(radar_entry_guardrail_decision(True, 20, config)["vetoed"])

    def test_approved_walk_forward_weights_stay_frozen_before_50_live_settlements(self):
        recommendation = {
            "volume": 30.0, "month_high": 20.0, "market_strength": 16.0,
            "surge": 28.0, "counter": 6.0,
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "weights.json"
            path.write_text(json.dumps({
                "method": "rule_only_walk_forward",
                "approved": True,
                "recommendedWeights": recommendation,
                "effectiveWeights": recommendation,
                "liveValidation": {
                    "approved": True,
                    "entryMode": "intraday_confirmed",
                    "settled": 49,
                    "avgNetReturn": 0.01,
                    "profitFactor": 1.2,
                },
            }), encoding="utf-8")
            config = load_radar_rule_config(path)
        self.assertFalse(config["approved"])
        self.assertEqual(config["source"], "live_sample_freeze")
        self.assertEqual(config["effectiveWeights"], DEFAULT_RADAR_RULE_WEIGHTS)
        self.assertTrue(config["liveValidation"]["frozen"])

    def test_weights_can_apply_after_live_sample_and_performance_gates(self):
        recommendation = {
            "volume": 30.0, "month_high": 20.0, "market_strength": 16.0,
            "surge": 28.0, "counter": 6.0,
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "weights.json"
            path.write_text(json.dumps({
                "method": "rule_only_walk_forward",
                "approved": True,
                "recommendedWeights": recommendation,
                "effectiveWeights": recommendation,
                "liveValidation": {
                    "approved": True,
                    "entryMode": "intraday_confirmed",
                    "settled": 50,
                    "avgNetReturn": 0.01,
                    "profitFactor": 1.2,
                },
            }), encoding="utf-8")
            config = load_radar_rule_config(path)
        self.assertTrue(config["approved"])
        self.assertEqual(config["source"], "walk_forward_approved")
        self.assertEqual(config["effectiveWeights"], recommendation)
        self.assertFalse(config["liveValidation"]["frozen"])

    def test_approved_entry_guardrail_can_apply_while_weights_stay_observation_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "weights.json"
            path.write_text(json.dumps({
                "method": "rule_only_walk_forward",
                "approved": False,
                "recommendedWeights": dict(DEFAULT_RADAR_RULE_WEIGHTS),
                "effectiveWeights": dict(DEFAULT_RADAR_RULE_WEIGHTS),
                "entryGuardrailCalibration": {
                    "approved": True,
                    "recommendedKey": "no_chase_combined",
                    "recommendedLabel": "排除完整追價且5日漲幅低於15%",
                    "recommendedRules": {"blockSurgeSetup": True, "maxRet5": 15},
                },
            }), encoding="utf-8")
            config = load_radar_rule_config(path)
        guardrail = config["entryGuardrailCalibration"]
        self.assertTrue(guardrail["approved"])
        self.assertEqual(
            guardrail["effectiveRules"],
            {"blockSurgeSetup": True, "maxRet5": 15.0},
        )
        self.assertTrue(radar_entry_guardrail_decision(True, 8, config)["vetoed"])
        self.assertTrue(radar_entry_guardrail_decision(False, 15, config)["vetoed"])
        self.assertFalse(radar_entry_guardrail_decision(False, 14.99, config)["vetoed"])

    def test_entry_guardrail_walk_forward_requires_and_can_pass_every_gate(self):
        result = rwf.walk_forward_calibrate(
            self.no_chase_records(),
            candidates=[dict(DEFAULT_RADAR_RULE_WEIGHTS)],
        )
        calibration = result["entryGuardrailCalibration"]
        self.assertTrue(calibration["approved"])
        self.assertEqual(calibration["recommendedKey"], "block_surge")
        self.assertEqual(
            calibration["effectiveRules"],
            {"blockSurgeSetup": True, "maxRet5": None},
        )
        self.assertTrue(all(calibration["adoptionChecks"].values()))
        self.assertGreaterEqual(calibration["recentOos"]["precisionLift"], 0.02)
        self.assertGreaterEqual(calibration["pressureTestOos"]["folds"], 12)

    def test_approved_regime_threshold_can_apply_while_weights_stay_observation_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "weights.json"
            path.write_text(json.dumps({
                "method": "rule_only_walk_forward",
                "approved": False,
                "recommendedWeights": dict(DEFAULT_RADAR_RULE_WEIGHTS),
                "effectiveWeights": dict(DEFAULT_RADAR_RULE_WEIGHTS),
                "regimeThresholdCalibration": {
                    "regimes": {
                        "strong_breadth": {
                            "approved": True,
                            "recommendedThreshold": 65,
                        }
                    }
                },
            }), encoding="utf-8")
            config = load_radar_rule_config(path)
        self.assertEqual(config["effectiveWeights"], DEFAULT_RADAR_RULE_WEIGHTS)
        calibration = config["regimeThresholdCalibration"]
        self.assertEqual(calibration["effectiveThresholds"]["strong_breadth"], 65)
        self.assertEqual(calibration["effectiveThresholds"]["theme_rotation"], 60)

    def test_regime_threshold_requires_all_adoption_gates_and_does_not_change_scores(self):
        records = []
        start = dt.date(2024, 1, 1)
        for day in range(800):
            date = (start + dt.timedelta(days=day)).isoformat()
            for index in range(3):
                records.append({
                    "symbol": f"W{index}", "date": date, "volumeRatio": 4.0,
                    "monthHigh": True, "marketStrength": True, "surge": False,
                    "counter": False, "marketRegime": "strong_breadth",
                    "targetHit": True, "netReturn": 0.08,
                })
            for index in range(17):
                records.append({
                    "symbol": f"L{index}", "date": date, "volumeRatio": 4.0,
                    "monthHigh": False, "marketStrength": True, "surge": False,
                    "counter": False, "marketRegime": "strong_breadth",
                    "targetHit": False, "netReturn": -0.02,
                })
        before = rwf.record_score(records[0], DEFAULT_RADAR_RULE_WEIGHTS)
        result = rwf.walk_forward_calibrate(
            records, candidates=[dict(DEFAULT_RADAR_RULE_WEIGHTS)]
        )
        calibration = result["regimeThresholdCalibration"]
        strong = calibration["regimes"]["strong_breadth"]
        self.assertTrue(strong["approved"])
        self.assertEqual(strong["effectiveThreshold"], 65)
        self.assertTrue(all(strong["adoptionChecks"].values()))
        self.assertEqual(
            before, rwf.record_score(records[0], DEFAULT_RADAR_RULE_WEIGHTS)
        )

    def test_calibrator_source_never_calls_independent_model(self):
        source = Path(rwf.__file__).read_text(encoding="utf-8")
        self.assertNotIn("load_model(", source)
        self.assertNotIn("predict_symbol(", source)
        self.assertIn('"independentModelUsed": False', source)

    def test_calibration_universe_does_not_rank_by_future_latest_turnover(self):
        source = Path(rwf.__file__).read_text(encoding="utf-8")
        self.assertEqual(
            rwf.UNIVERSE_POLICY,
            "deterministic_non_return_sample_with_point_in_time_liquidity",
        )
        self.assertNotIn("ORDER BY avg_turnover", source)
        self.assertIn("hashlib.sha256", source)


if __name__ == "__main__":
    unittest.main()
