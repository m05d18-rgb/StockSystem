import datetime as dt
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server
from ml_backend import StockMLBackend


TAIPEI = dt.timezone(dt.timedelta(hours=8))


def baseline(symbol, sector="其他"):
    return {
        "symbol": symbol,
        "name": f"測試{symbol}",
        "sector": sector,
        "priceDate": "2026-07-13",
        "previousClose": 100.0,
        "avgVolume20Lots": 1000.0,
        "avgTurnover20Million": 50.0,
        "historyDays": 20,
    }


def quote(current, high, snapshot_at="2026-07-14T10:30:00+08:00"):
    return {
        "currentPrice": current,
        "openPrice": 101.0,
        "highPrice": high,
        "lowPrice": 99.0,
        "totalVolume": 1000.0,
        "totalVolumeUnit": "lots",
        "snapshotAt": snapshot_at,
        "source": "Shioaji snapshot",
    }


class IntradayMarketDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.now = dt.datetime(2026, 7, 14, 10, 30, tzinfo=TAIPEI)

    def test_whole_market_rows_are_observation_only_and_keep_exchange_sector(self):
        baselines = {
            "8033": baseline("8033", "電子零組件"),
            "2231": baseline("2231", "汽車工業"),
            "9999": baseline("9999", "其他"),
        }
        payload = {
            "ok": True,
            "source": "Shioaji snapshot",
            "quotes": {
                "8033": quote(106.0, 106.5),
                "2231": quote(102.0, 108.0),
                "9999": quote(99.0, 108.0),
            },
        }

        result = server.build_intraday_market_discovery(
            baselines, payload, radar_codes={"2231"}, now=self.now,
        )

        by_symbol = {item["symbol"]: item for item in result["leaders"]}
        self.assertEqual(by_symbol["8033"]["state"], "active")
        self.assertEqual(by_symbol["8033"]["discoveryType"], "new_intraday")
        self.assertEqual(by_symbol["8033"]["sector"], "電子零組件")
        self.assertEqual(by_symbol["2231"]["state"], "faded")
        self.assertEqual(by_symbol["2231"]["discoveryType"], "existing_candidate")
        self.assertEqual(by_symbol["9999"]["state"], "reversed")
        self.assertTrue(result["coverageComplete"])
        for item in result["leaders"]:
            self.assertTrue(item["observationOnly"])
            self.assertFalse(item["canBuy"])
            self.assertNotIn("theme", item)

    def test_stale_quote_and_stock_that_never_reached_one_point_five_percent_are_excluded(self):
        baselines = {
            "1111": baseline("1111"),
            "2222": baseline("2222"),
        }
        payload = {
            "ok": True,
            "quotes": {
                "1111": quote(106.0, 107.0, "2026-07-14T10:20:00+08:00"),
                "2222": quote(101.0, 101.4),
            },
        }

        result = server.build_intraday_market_discovery(
            baselines, payload, now=self.now,
        )

        self.assertEqual(result["received"], 2)
        self.assertEqual(result["fresh"], 1)
        self.assertEqual(result["qualified"], 0)
        self.assertEqual(result["leaders"], [])

    def test_three_to_five_percent_move_is_early_observation_not_buy(self):
        result = server.build_intraday_market_discovery(
            {"2222": baseline("2222")},
            {"ok": True, "quotes": {"2222": quote(104.0, 104.2)}},
            now=self.now,
        )

        self.assertEqual(result["qualified"], 1)
        item = result["leaders"][0]
        self.assertEqual(item["stage"], "early")
        self.assertEqual(item["state"], "early")
        self.assertIn("尚未達 5%", item["status"])
        self.assertFalse(item["canBuy"])

    def test_low_liquidity_tier_accepts_five_million_but_not_less(self):
        low = baseline("3333")
        low.update({"avgVolume20Lots": 20.0, "avgTurnover20Million": 10.0})
        below = baseline("4444")
        below.update({"avgVolume20Lots": 20.0, "avgTurnover20Million": 10.0})
        payload = {
            "ok": True,
            "quotes": {
                "3333": {**quote(106.0, 107.0), "totalVolume": 50.0},
                "4444": {**quote(106.0, 107.0), "totalVolume": 40.0},
            },
        }

        result = server.build_intraday_market_discovery(
            {"3333": low, "4444": below}, payload, now=self.now,
        )

        self.assertEqual([item["symbol"] for item in result["leaders"]], ["3333"])
        self.assertEqual(result["leaders"][0]["liquidityTier"], "exception")
        self.assertTrue(result["leaders"][0]["baselineException"])
        self.assertGreaterEqual(result["leaders"][0]["turnoverMillion"], 5.0)

    def test_low_turnover_stock_is_excluded_even_when_price_spikes(self):
        payload = {
            "ok": True,
            "quotes": {
                "3333": {
                    **quote(106.0, 107.0),
                    "totalVolume": 10.0,
                },
            },
        }

        result = server.build_intraday_market_discovery(
            {"3333": baseline("3333")}, payload, now=self.now,
        )

        self.assertEqual(result["qualified"], 0)

    def test_two_distinct_snapshots_are_required_for_confirmation(self):
        first = server.build_intraday_market_discovery(
            {"1111": baseline("1111")},
            {"ok": True, "quotes": {"1111": quote(
                106.0, 106.5, "2026-07-14T10:30:00+08:00",
            )}},
            now=self.now,
        )
        server.apply_intraday_discovery_confirmations(
            first["leaders"], {}, now=self.now,
        )
        self.assertEqual(first["leaders"][0]["confirmationCount"], 1)
        self.assertFalse(first["leaders"][0]["consecutiveConfirmed"])

        second_now = self.now + dt.timedelta(seconds=20)
        second = server.build_intraday_market_discovery(
            {"1111": baseline("1111")},
            {"ok": True, "quotes": {"1111": quote(
                106.2, 106.6, "2026-07-14T10:30:20+08:00",
            )}},
            now=second_now,
        )
        server.apply_intraday_discovery_confirmations(
            second["leaders"], {"leaders": first["leaders"]}, now=second_now,
        )
        item = second["leaders"][0]
        self.assertEqual(item["confirmationCount"], 2)
        self.assertTrue(item["consecutiveConfirmed"])
        self.assertTrue(item["observationOnly"])
        self.assertFalse(item["canBuy"])

    def test_same_snapshot_does_not_fake_a_second_confirmation(self):
        row = server.build_intraday_market_discovery(
            {"1111": baseline("1111")},
            {"ok": True, "quotes": {"1111": quote(106.0, 106.5)}},
            now=self.now,
        )["leaders"][0]
        server.apply_intraday_discovery_confirmations([row], {}, now=self.now)
        repeated = dict(row)
        server.apply_intraday_discovery_confirmations(
            [repeated], {"leaders": [row]}, now=self.now + dt.timedelta(seconds=20),
        )
        self.assertEqual(repeated["confirmationCount"], 1)
        self.assertFalse(repeated["consecutiveConfirmed"])

    def test_every_requested_symbol_has_a_qualified_or_exclusion_audit(self):
        result = server.build_intraday_market_discovery(
            {"1111": baseline("1111"), "2222": baseline("2222")},
            {"ok": True, "quotes": {"1111": quote(106.0, 106.5)}},
            requested_symbols=["1111", "2222", "3333"],
            now=self.now,
        )

        audits = {item["symbol"]: item for item in result["_auditRows"]}
        self.assertEqual(set(audits), {"1111", "2222", "3333"})
        self.assertTrue(audits["1111"]["qualified"])
        self.assertEqual(
            audits["2222"]["exclusionReasons"][0]["code"],
            "missing_broker_quote",
        )
        self.assertEqual(
            audits["3333"]["exclusionReasons"][0]["code"],
            "missing_verified_daily_baseline",
        )

    def test_new_candidate_requires_second_quote_before_formal_shadow_signal(self):
        first = server.build_intraday_market_discovery(
            {"1111": baseline("1111")},
            {"ok": True, "quotes": {"1111": quote(106.0, 106.5)}},
            now=self.now,
        )["leaders"][0]
        server.apply_intraday_discovery_confirmations([first], {}, now=self.now)
        formal_result = {
            "canBuy": False,
            "shadowCanBuy": True,
            "status": "戰績觀察期",
            "candidateScore": 72.0,
            "executionEntryPrice": 106.1,
        }
        with patch.object(
            server, "compute_monster_intraday_state", return_value=formal_result,
        ):
            server.apply_intraday_candidate_rules(
                [first], {"1111": {"score": 72.0}}, now_tm=self.now.timetuple(),
            )
        self.assertFalse(first["candidateSignal"])
        self.assertIn(
            "candidate_waiting_second_fresh_quote",
            {reason["code"] for reason in first["candidateExclusionReasons"]},
        )

        second_now = self.now + dt.timedelta(seconds=20)
        second = server.build_intraday_market_discovery(
            {"1111": baseline("1111")},
            {"ok": True, "quotes": {"1111": quote(
                106.2, 106.6, "2026-07-14T10:30:20+08:00",
            )}},
            now=second_now,
        )["leaders"][0]
        server.apply_intraday_discovery_confirmations(
            [second], {"confirmationRows": [first]}, now=second_now,
        )
        with patch.object(
            server, "compute_monster_intraday_state", return_value=formal_result,
        ):
            server.apply_intraday_candidate_rules(
                [second], {"1111": {"score": 72.0}},
                now_tm=second_now.timetuple(),
            )
        self.assertTrue(second["consecutiveConfirmed"])
        self.assertTrue(second["candidateSignal"])
        self.assertTrue(second["formalShadowCanBuy"])
        self.assertFalse(second["formalCanBuy"])
        self.assertEqual(second["candidateExclusionReasons"], [])
        self.assertEqual(second["buyDecision"], "observe")
        self.assertEqual(second["buyDecisionLabel"], "不可買")
        self.assertIn("戰績門檻", second["buyDecisionReason"])

    def test_final_buy_decision_is_explicit_only_after_every_gate_passes(self):
        row = {
            "symbol": "1111",
            "consecutiveConfirmed": True,
            "highChangePct": 6.0,
            "state": "active",
            "highRetention": 0.98,
            "currentChangePct": 6.0,
            "turnoverMillion": 80.0,
            "volumeProgressRatio": 1.2,
            "minimumVolumeProgressRatio": 0.5,
            "previousClose": 100.0,
            "openPrice": 102.0,
            "currentPrice": 106.0,
            "highPrice": 106.5,
            "lowPrice": 101.0,
            "totalVolumeLots": 1000.0,
            "quoteFresh": True,
            "snapshotAt": "2026-07-14T10:30:20+08:00",
            "inRadar": False,
        }
        formal_result = {
            "canBuy": True,
            "shadowCanBuy": False,
            "status": "正式條件通過",
            "candidateScore": 82.0,
            "executionEntryPrice": 106.1,
        }
        with patch.object(
            server, "compute_monster_intraday_state", return_value=formal_result,
        ):
            server.apply_intraday_candidate_rules(
                [row], {"1111": {"score": 82.0}}, now_tm=self.now.timetuple(),
            )

        self.assertTrue(row["canBuy"])
        self.assertEqual(row["buyDecision"], "buy")
        self.assertEqual(row["buyDecisionLabel"], "可買")
        self.assertEqual(row["candidateExclusionReasons"], [])

    def test_summary_never_counts_intermediate_formal_flag_as_buyable(self):
        counts = server.summarize_intraday_candidate_decisions([
            {
                "consecutiveConfirmed": True,
                "candidateSignal": True,
                "formalCanBuy": True,
                "formalShadowCanBuy": False,
                "canBuy": False,
                "buyDecision": "blocked",
            },
            {
                "consecutiveConfirmed": True,
                "candidateSignal": True,
                "formalCanBuy": False,
                "formalShadowCanBuy": True,
                "canBuy": False,
                "buyDecision": "observe",
            },
            {
                "consecutiveConfirmed": True,
                "candidateSignal": True,
                "formalCanBuy": True,
                "canBuy": True,
                "buyDecision": "buy",
            },
        ])

        self.assertEqual(counts["confirmed"], 3)
        self.assertEqual(counts["candidateSignals"], 3)
        self.assertEqual(counts["formalBuyable"], 1)
        self.assertEqual(counts["rulePassedObservation"], 1)

    def test_formal_context_limit_rotates_uncached_candidates(self):
        original_cache = dict(server.intraday_discovery_formal_context_cache)
        rows = [
            {
                "symbol": f"{1000 + index}",
                "priceDate": "2026-07-13",
                "turnoverMillion": 100 - index,
                "highChangePct": 5.0,
                "consecutiveConfirmed": True,
                "actionableAtObservation": True,
                "inRadar": False,
            }
            for index in range(25)
        ]
        try:
            server.intraday_discovery_formal_context_cache = {}
            with patch.object(
                server.backend, "connect", side_effect=RuntimeError("no meta"),
            ), patch.object(
                server.backend,
                "monster_score_for_symbol",
                side_effect=lambda symbol, **kwargs: {
                    "symbol": symbol,
                    "score": 70.0,
                    "buyAllowed": False,
                    "riskFlags": [],
                },
            ) as scorer:
                first, _ = server.load_intraday_discovery_formal_contexts(
                    rows, [], daily_reference_date="2026-07-13",
                )
                second, _ = server.load_intraday_discovery_formal_contexts(
                    rows, [], daily_reference_date="2026-07-13",
                )
        finally:
            server.intraday_discovery_formal_context_cache = original_cache

        self.assertEqual(len(first), server.INTRADAY_DISCOVERY_FORMAL_CONTEXT_LIMIT)
        self.assertEqual(len(second), 25)
        self.assertEqual(scorer.call_count, 25)

    def test_realtime_tick_overlay_keeps_day_extremes_and_updates_price(self):
        merged = server.merge_intraday_tick_quotes(
            {"1111": {
                **quote(105.0, 108.0),
                "lowPrice": 99.0,
                "totalVolume": 1200.0,
            }},
            {"1111": {
                **quote(106.0, 106.5, "2026-07-14T10:30:20+08:00"),
                "lowPrice": 101.0,
                "totalVolume": 100.0,
            }},
        )
        self.assertEqual(merged["1111"]["currentPrice"], 106.0)
        self.assertEqual(merged["1111"]["highPrice"], 108.0)
        self.assertEqual(merged["1111"]["lowPrice"], 99.0)
        self.assertEqual(merged["1111"]["totalVolume"], 1200.0)
        self.assertIn("realtime tick", merged["1111"]["source"])

    def test_new_listing_live_turnover_exception_is_observation_only(self):
        new_listing = baseline("5555")
        new_listing.update({
            "avgVolume20Lots": 0.0,
            "avgTurnover20Million": 0.0,
            "historyDays": 0,
            "baselineMode": "live_reference_exception",
            "previousClose": 10.0,
        })
        live_quote = {
            **quote(10.2, 10.2),
            "openPrice": 10.0,
            "lowPrice": 9.9,
            "totalVolume": 500.0,
        }
        result = server.build_intraday_market_discovery(
            {"5555": new_listing},
            {"ok": True, "quotes": {"5555": live_quote}},
            now=self.now,
        )

        self.assertEqual(result["qualified"], 1)
        item = result["leaders"][0]
        self.assertEqual(item["stage"], "emerging")
        self.assertTrue(item["baselineException"])
        self.assertIn("low_history_live_exception", item["triggerReasons"])
        self.assertFalse(item["canBuy"])
        self.assertTrue(item["observationOnly"])

    def test_price_acceleration_uses_distinct_snapshots(self):
        history = {}
        first = {"1111": {
            **quote(101.0, 101.0, "2026-07-14T10:30:00+08:00"),
            "referencePrice": 100.0,
        }}
        second = {"1111": {
            **quote(102.0, 102.0, "2026-07-14T10:31:00+08:00"),
            "referencePrice": 100.0,
        }}
        server.apply_intraday_quote_acceleration(first, history)
        server.apply_intraday_quote_acceleration(second, history)

        self.assertIsNone(first["1111"]["priceAccelerationPctPerMinute"])
        self.assertAlmostEqual(
            second["1111"]["priceAccelerationPctPerMinute"], 1.0,
        )


class IntradayDiscoveryBaselineTests(unittest.TestCase):
    def test_only_latest_complete_daily_rows_are_used_as_baselines(self):
        with tempfile.TemporaryDirectory() as tmp:
            backend = StockMLBackend()
            backend.db_path = Path(tmp) / "stock_system.sqlite3"
            backend.init_db()
            with backend.connect() as conn:
                conn.executemany(
                    "INSERT INTO stock_info (symbol, name, sector, market_type, updated_at) VALUES (?, ?, ?, ?, ?)",
                    [
                        ("1111", "完整股", "電子", "twse", "2026-07-13 14:00:00"),
                        ("2222", "過期股", "汽車", "twse", "2026-07-13 14:00:00"),
                    ],
                )
                rows = []
                for day in range(20):
                    date = (dt.date(2026, 6, 24) + dt.timedelta(days=day)).isoformat()
                    rows.append((
                        "1111", date, 100, 101, 99, 100, 1_000_000,
                        "TWSE OpenAPI STOCK_DAY_ALL", f"{date} 14:00:00",
                    ))
                    if date < "2026-07-13":
                        rows.append((
                            "2222", date, 50, 51, 49, 50, 2_000_000,
                            "TWSE OpenAPI STOCK_DAY_ALL", f"{date} 14:00:00",
                        ))
                conn.executemany(
                    """
                    INSERT INTO prices (
                        symbol, date, open, high, low, close, volume,
                        price_source, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    rows,
                )

            with patch.object(
                backend, "latest_complete_price_date", return_value="2026-07-13",
            ):
                result = backend.intraday_discovery_baselines(["1111", "2222"])

        self.assertEqual(set(result), {"1111"})
        self.assertEqual(result["1111"]["priceDate"], "2026-07-13")
        self.assertEqual(result["1111"]["previousClose"], 100.0)
        self.assertEqual(result["1111"]["avgVolume20Lots"], 1000.0)
        self.assertEqual(result["1111"]["avgTurnover20Million"], 100.0)
        self.assertEqual(result["1111"]["historyDays"], 20)
        self.assertEqual(result["1111"]["sector"], "電子")

    def test_latest_intraday_tick_quotes_returns_fresh_day_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            backend = StockMLBackend()
            backend.db_path = Path(tmp) / "stock_system.sqlite3"
            backend.init_db()
            now = dt.datetime.now().replace(microsecond=0)
            date_value = now.date().isoformat()
            timestamp = now.isoformat(sep=" ")
            with backend.connect() as conn:
                conn.executemany(
                    """
                    INSERT INTO intraday_minute_bars (
                        symbol, date, minute, open, high, low, close,
                        volume_lots, cumulative_volume_lots, last_tick_at,
                        source, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        ("1111", date_value, "09:01", 100, 102, 99, 101,
                         30, 30, timestamp, "test", timestamp),
                        ("1111", date_value, "09:02", 101, 104, 100, 103,
                         40, 70, timestamp, "test", timestamp),
                    ],
                )
            result = backend.latest_intraday_tick_quotes(
                ["1111"], trading_date=date_value, max_age_seconds=60,
            )

        self.assertEqual(result["1111"]["openPrice"], 100.0)
        self.assertEqual(result["1111"]["highPrice"], 104.0)
        self.assertEqual(result["1111"]["lowPrice"], 99.0)
        self.assertEqual(result["1111"]["currentPrice"], 103.0)
        self.assertEqual(result["1111"]["totalVolume"], 70.0)

    def test_rotation_staging_returns_fresh_all_market_quotes(self):
        with tempfile.TemporaryDirectory() as tmp:
            backend = StockMLBackend()
            backend.db_path = Path(tmp) / "stock_system.sqlite3"
            backend.init_db()
            now = dt.datetime.now().astimezone().replace(microsecond=0)
            scan_at = now.isoformat()
            saved = backend.upsert_intraday_rotation_quotes(
                {
                    "1111": {
                        "currentPrice": 10.2,
                        "referencePrice": 10.0,
                        "highPrice": 10.3,
                        "lowPrice": 9.9,
                        "totalVolume": 500,
                        "bidPrice": 10.1,
                        "askPrice": 10.2,
                        "snapshotAt": scan_at,
                    },
                    "2222": {
                        "currentPrice": 20.5,
                        "referencePrice": 20.0,
                        "highPrice": 20.6,
                        "lowPrice": 19.8,
                        "totalVolume": 800,
                        "snapshotAt": scan_at,
                    },
                },
                trading_date=scan_at[:10],
                scan_at=scan_at,
                round_id="round-1",
                batch_index=0,
                batch_count=6,
                requested_count=400,
                requested_symbols=["1111", "2222", "3333"],
                rotation_symbols=["1111", "2222"],
                universe_count=2374,
                fallback_codes=["2222"],
                missing_symbols=["3333"],
            )
            payload = backend.latest_intraday_rotation_payload(
                scan_at[:10], max_age_seconds=60,
            )

        self.assertEqual(saved["saved"], 2)
        self.assertEqual(payload["count"], 2)
        self.assertEqual(payload["quotes"]["1111"]["referencePrice"], 10.0)
        self.assertEqual(payload["quotes"]["1111"]["bidPrice"], 10.1)
        self.assertEqual(payload["latestCycle"]["fallbackCount"], 1)
        self.assertEqual(payload["latestCycle"]["missingSymbols"], ["3333"])


class IntradayDiscoveryEventHistoryTests(unittest.TestCase):
    def make_backend(self, tmp):
        test_backend = StockMLBackend()
        test_backend.db_path = Path(tmp) / "stock_system.sqlite3"
        test_backend.init_db()
        return test_backend

    @staticmethod
    def discovery_row(symbol="1111", stage="early", state="early"):
        return {
            "symbol": symbol,
            "name": f"測試{symbol}",
            "sector": "電子",
            "stage": stage,
            "state": state,
            "currentPrice": 104.0,
            "currentChangePct": 4.0,
            "highChangePct": 4.2,
            "volumeProgressRatio": 0.8,
            "turnoverMillion": 12.0,
            "liquidityTier": "medium",
            "quoteSource": "sinopac_shioaji_scanner",
            "canBuy": False,
            "actionableAtObservation": True,
            "lateDiscovery": False,
        }

    def test_events_are_append_only_and_heartbeat_is_five_minute_bucketed(self):
        with tempfile.TemporaryDirectory() as tmp:
            test_backend = self.make_backend(tmp)
            now = dt.datetime.now().replace(second=0, microsecond=0)
            first_at = now.strftime("%Y-%m-%d %H:%M:%S")
            next_bucket = (now + dt.timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S")
            date_value = first_at[:10]

            first = test_backend.record_intraday_discovery_events(
                [self.discovery_row()], date_value, first_at,
            )
            duplicate = test_backend.record_intraday_discovery_events(
                [self.discovery_row()], date_value, first_at,
            )
            later = test_backend.record_intraday_discovery_events(
                [self.discovery_row()], date_value, next_bucket,
            )
            with test_backend.connect() as conn:
                count = conn.execute(
                    "SELECT COUNT(*) FROM intraday_discovery_events"
                ).fetchone()[0]
            hot = test_backend.intraday_hot_symbols(
                trading_date=date_value, limit=10, max_age_minutes=30,
            )

        self.assertEqual(first["inserted"], 4)
        self.assertEqual(duplicate["inserted"], 0)
        self.assertEqual(later["inserted"], 1)
        self.assertEqual(count, 5)
        self.assertEqual(hot, ["1111"])

    def test_close_settlement_reports_detected_and_two_miss_reasons(self):
        with tempfile.TemporaryDirectory() as tmp:
            test_backend = self.make_backend(tmp)
            date_value = "2026-07-14"
            rows = []
            for symbol in ("1111", "2222", "3333"):
                rows.extend([
                    (
                        symbol, "2026-07-13", 100, 100, 99, 100, 100_000,
                        "TWSE OpenAPI STOCK_DAY_ALL", "2026-07-13 14:00:00",
                    ),
                    (
                        symbol, date_value, 101, 107, 100, 105, 100_000,
                        "TWSE OpenAPI STOCK_DAY_ALL", "2026-07-14 14:00:00",
                    ),
                ])
            with test_backend.connect() as conn:
                conn.executemany(
                    """
                    INSERT INTO prices (
                        symbol, date, open, high, low, close, volume,
                        price_source, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    rows,
                )
            test_backend.record_intraday_discovery_events(
                [self.discovery_row("1111")],
                date_value,
                "2026-07-14 09:20:00",
            )
            test_backend.upsert_intraday_scanner_rows(
                [{
                    "symbol": "3333",
                    "close": 105,
                    "high": 107,
                    "totalVolumeLots": 100,
                    "rankTypes": ["change_percent"],
                }],
                trading_date=date_value,
                scan_at="2026-07-14 10:00:00",
            )
            with patch.object(
                test_backend, "listed_symbols", return_value=["1111", "2222", "3333"],
            ), patch.object(
                test_backend,
                "intraday_scanner_coverage",
                return_value={"ok": True, "valid": True},
            ), patch.object(
                test_backend,
                "intraday_rotation_coverage",
                return_value={"ok": True, "valid": True},
            ):
                report = test_backend.settle_intraday_discovery_recall(date_value)
                history = test_backend.intraday_discovery_recall_history(days=10)

        self.assertTrue(report["ok"])
        self.assertEqual(report["actualMovers"], 3)
        self.assertEqual(report["detectedMovers"], 1)
        self.assertEqual(report["earlyDetected"], 1)
        self.assertEqual(report["actionableDetected"], 1)
        self.assertEqual(report["lateDetected"], 0)
        self.assertEqual(report["missedMovers"], 2)
        reasons = {item["symbol"]: item["reason"] for item in report["missed"]}
        self.assertEqual(reasons["2222"], "報價缺漏或未完成全市場輪巡")
        self.assertEqual(reasons["3333"], "不在原流動性母體且未通過例外門檻")
        self.assertEqual(history["latest"]["date"], date_value)
        self.assertAlmostEqual(history["aggregate"]["recall"], 1 / 3)
        self.assertIn("tradableAccuracy", history)

    def test_candidate_accuracy_excludes_same_day_high_before_signal(self):
        with tempfile.TemporaryDirectory() as tmp:
            test_backend = self.make_backend(tmp)
            signal_date = "2026-06-01"
            test_backend.record_intraday_candidate_signals([{
                "symbol": "1111",
                "candidateSignal": True,
                "executionEntryPrice": 100.0,
                "formalScore": 70.0,
                "priceDate": "2026-05-29",
                "quoteSource": "test",
            }], signal_date=signal_date, signaled_at=f"{signal_date} 10:00:00")
            prices = [(
                "1111", signal_date, 100, 120, 95, 110, 100_000,
                "test", f"{signal_date} 14:00:00",
            )]
            for index in range(1, 11):
                date_value = (dt.date(2026, 6, 1) + dt.timedelta(days=index)).isoformat()
                prices.append((
                    "1111", date_value, 100, 105, 95, 101, 100_000,
                    "test", f"{date_value} 14:00:00",
                ))
            with test_backend.connect() as conn:
                conn.executemany(
                    """
                    INSERT INTO prices (
                        symbol, date, open, high, low, close, volume,
                        price_source, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    prices,
                )
            accuracy = test_backend.compute_intraday_candidate_accuracy()

        self.assertTrue(accuracy["sameDayPathExcluded"])
        self.assertEqual(accuracy["settled"], 1)
        self.assertEqual(accuracy["hits"], 0)
        self.assertEqual(accuracy["targetHitRate"], 0.0)
        self.assertAlmostEqual(accuracy["avgMaxAdverse"], -0.05)

    def test_pre_feature_day_without_observations_is_not_scored_as_all_missed(self):
        with tempfile.TemporaryDirectory() as tmp:
            test_backend = self.make_backend(tmp)
            report = test_backend.settle_intraday_discovery_recall("2026-07-13")
            with test_backend.connect() as conn:
                saved = conn.execute(
                    "SELECT COUNT(*) FROM intraday_discovery_daily_stats"
                ).fetchone()[0]

        self.assertTrue(report["ok"])
        self.assertTrue(report["skipped"])
        self.assertFalse(report["valid"])
        self.assertIn("不納入找到率", report["reason"])
        self.assertEqual(saved, 0)

    def test_scanner_coverage_requires_open_close_span_and_enough_cycles(self):
        with tempfile.TemporaryDirectory() as tmp:
            test_backend = self.make_backend(tmp)
            date_value = "2026-07-15"
            start = dt.datetime(2026, 7, 15, 9, 0)
            cycles = []
            for index in range(130):
                scan_at = (start + dt.timedelta(minutes=index * 2)).strftime(
                    "%Y-%m-%d %H:%M:%S"
                )
                cycles.append((
                    date_value, scan_at, 500,
                    '{"change_percent":200,"day_range":200,"volume":200,"amount":200}',
                    "test", scan_at,
                ))
            with test_backend.connect() as conn:
                conn.executemany(
                    """
                    INSERT INTO intraday_scanner_cycles (
                        trading_date, scan_at, symbol_count, rank_counts_json,
                        source, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    cycles,
                )
            complete = test_backend.intraday_scanner_coverage(date_value)
            incomplete = test_backend.intraday_scanner_coverage("2026-07-14")

        self.assertTrue(complete["valid"])
        self.assertTrue(complete["fourRankTypesComplete"])
        self.assertFalse(complete["fiveRankTypesComplete"])
        self.assertEqual(complete["cycles"], 130)
        self.assertGreaterEqual(complete["spanMinutes"], 210)
        self.assertFalse(incomplete["valid"])
        self.assertIn("排行週期不足", incomplete["reason"])

    def test_rotation_coverage_requires_120_batches_one_round_and_95_percent_symbols(self):
        with tempfile.TemporaryDirectory() as tmp:
            test_backend = self.make_backend(tmp)
            date_value = "2026-07-15"
            start = dt.datetime(2026, 7, 15, 9, 0)
            cycles = []
            for round_index in range(130):
                for batch_index in range(6):
                    scan_at = (
                        start + dt.timedelta(seconds=(round_index * 120) + (batch_index * 20))
                    ).strftime("%Y-%m-%d %H:%M:%S")
                    cycles.append((
                        date_value, scan_at, f"round-{round_index}", batch_index,
                        6, 20, 20, 100,
                        json.dumps([f"{index:04d}" for index in range(1, 101)]),
                        "test", scan_at,
                    ))
            with test_backend.connect() as conn:
                conn.executemany(
                    """
                    INSERT INTO intraday_rotation_cycles (
                        trading_date, scan_at, round_id, batch_index, batch_count,
                        requested_count, received_count, universe_count,
                        rotation_symbols_json, source, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    cycles,
                )
                conn.executemany(
                    """
                    INSERT INTO intraday_rotation_staging (
                        trading_date, symbol, scan_at, round_id, batch_index,
                        batch_count, source, first_seen_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (date_value, f"{index:04d}", "2026-07-15 13:20:00",
                         "round-129", index % 6, 6, "test",
                         "2026-07-15 09:00:00", "2026-07-15 13:20:00")
                        for index in range(1, 101)
                    ],
                )
            with patch.object(
                test_backend, "listed_symbols",
                return_value=[f"{index:04d}" for index in range(1, 101)],
            ):
                coverage = test_backend.intraday_rotation_coverage(date_value)

        self.assertTrue(coverage["valid"])
        self.assertEqual(coverage["completeRounds"], 130)
        self.assertEqual(coverage["coverageRatio"], 1.0)

    def test_twenty_valid_days_and_recall_targets_only_unlock_paper_research(self):
        with tempfile.TemporaryDirectory() as tmp:
            test_backend = self.make_backend(tmp)
            with test_backend.connect() as conn:
                for index in range(20):
                    date_value = (dt.date(2026, 6, 1) + dt.timedelta(days=index)).isoformat()
                    conn.execute(
                        """
                        INSERT INTO intraday_discovery_daily_stats (
                            trading_date, actual_movers, detected_movers,
                            early_detected, actionable_detected, late_detected,
                            missed_movers, discovered_symbols, recall, early_recall,
                            actionable_recall, late_rate, precision, report_json,
                            created_at, updated_at
                        ) VALUES (?, 10, 10, 8, 7, 1, 0, 12, 1, .8, .7, .1,
                                  .833333, '{}', ?, ?)
                        """,
                        (date_value, f"{date_value} 18:10:00", f"{date_value} 18:10:00"),
                    )
            history = test_backend.intraday_discovery_recall_history(days=30)

        self.assertTrue(history["readiness"]["paperSimulationEligible"])
        self.assertFalse(history["readiness"]["automaticTradingEligible"])
        self.assertEqual(history["readiness"]["validDays"], 20)


class IntradayDiscoveryDesktopUiTests(unittest.TestCase):
    def test_discovery_section_is_desktop_only_and_has_no_order_fill_action(self):
        source = (Path(__file__).resolve().parents[1] / "app.js").read_text(
            encoding="utf-8",
        )
        helper = source.split(
            "function intradayMarketDiscoveryHtml", 1,
        )[1].split("function renderMonsterRadar", 1)[0]

        self.assertIn("盤中全市場新強勢", helper)
        self.assertIn("盤中新候選", helper)
        self.assertIn("<th>買進判定</th>", helper)
        self.assertIn("intraday-discovery-decision-cell", helper)
        decision_helper = source.split(
            "function intradayDiscoveryBuyDecision", 1,
        )[1].split("function intradayMarketDiscoveryHtml", 1)[0]
        self.assertIn('label: "可買"', decision_helper)
        self.assertIn('label: "不可買"', decision_helper)
        self.assertIn("row.canBuy === true", decision_helper)
        self.assertIn("candidateExclusionReasons", decision_helper)
        self.assertIn("目前非盤中", decision_helper)
        self.assertNotIn("data-order-fill", helper)
        self.assertIn(
            "const desktopDiscoveryHtml = !isMobileRadarView", source,
        )


class IntradayDiscoveryRestoreTests(unittest.TestCase):
    def test_last_discovery_is_restored_without_becoming_a_buy_signal(self):
        original_discovery = dict(server.intraday_discovery_status)
        original_market_discovery = dict(
            server.monster_intraday_status.get("marketDiscovery") or {},
        )
        saved = {
            "ok": True,
            "running": False,
            "checkedAt": "2026-07-14 13:19:27",
            "trigger": "test",
            "leaders": [{
                "symbol": "8033",
                "observationOnly": True,
                "canBuy": False,
            }],
        }
        try:
            server.intraday_discovery_status = {
                "ok": True,
                "running": False,
                "checkedAt": "",
                "leaders": [],
            }
            server.monster_intraday_status["marketDiscovery"] = {}
            with patch.object(
                server, "load_runtime_status", return_value=saved,
            ), patch.object(
                server.backend, "record_intraday_discovery_events",
            ) as record_events:
                result = server.restore_intraday_discovery_status()

            self.assertTrue(result["restored"])
            self.assertFalse(result["running"])
            self.assertEqual(result["leaders"][0]["symbol"], "8033")
            self.assertFalse(result["leaders"][0]["canBuy"])
            record_events.assert_called_once()
            self.assertEqual(
                server.monster_intraday_status["marketDiscovery"], result,
            )
        finally:
            server.intraday_discovery_status = original_discovery
            server.monster_intraday_status["marketDiscovery"] = (
                original_market_discovery
            )


if __name__ == "__main__":
    unittest.main()
