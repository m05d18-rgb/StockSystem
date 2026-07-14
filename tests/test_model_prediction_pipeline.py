import json
import os
import pickle
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server
from ml_backend import DATA_POLICY_VERSION, FEATURE_NAMES, StockMLBackend


class ModelPredictionPipelineTests(unittest.TestCase):
    def backend(self, tmp):
        backend = StockMLBackend()
        backend.db_path = Path(tmp) / "stock_system.sqlite3"
        backend.model_path = Path(tmp) / "model.pkl"
        backend.model_env_path = Path(tmp) / "model_env.json"
        backend.init_db()
        return backend

    def test_batch_prefilters_ineligible_symbols_and_keeps_full_reasons(self):
        with tempfile.TemporaryDirectory() as tmp:
            backend = self.backend(tmp)
            eligible = {
                "eligible": True, "symbol": "2330", "stage": "ready",
                "latestDate": "2026-07-10",
            }
            ineligible = {
                "eligible": False,
                "symbol": "9999",
                "stage": "stock_data_quality",
                "reason": "chipCoverageOk, marginSourceCoverageOk",
                "dataQuality": {"rows": 6, "missing": ["chipCoverageOk"]},
            }
            prediction = {"priceDate": "2026-07-10", "action": "WAIT"}
            with patch.object(backend, "load_model_with_error", return_value=({"version": "m1"}, "")), \
                 patch.object(backend, "model_prediction_eligibility", side_effect=[eligible, ineligible]), \
                 patch.object(backend, "predict_symbol", return_value=prediction) as predict, \
                 patch.object(backend, "sync_model_prediction_signals", return_value={"saved": 0}):
                result = backend.batch_save_predictions(symbols=["2330", "9999"])

        self.assertEqual(result["symbols_total"], 2)
        self.assertEqual(result["eligible_total"], 1)
        self.assertEqual(result["saved"], 1)
        self.assertEqual(result["skipped_ineligible_count"], 1)
        self.assertEqual(result["error_count"], 0)
        self.assertEqual(result["ineligible"][0]["symbol"], "9999")
        self.assertIn("marginSourceCoverageOk", result["ineligible"][0]["reason"])
        predict.assert_called_once_with("2330", save=True, repair=False)

    def test_prediction_eligibility_rejects_stock_behind_complete_market_date(self):
        with tempfile.TemporaryDirectory() as tmp:
            backend = self.backend(tmp)
            quality = {
                "ok": True,
                "missing": [],
                "rows": 200,
                "latestDate": "2026-06-17",
            }
            with patch.object(backend, "load_price_rows", return_value=[{"date": "2026-06-17"}]), \
                 patch.object(backend, "rows_with_verified_sources", return_value=[{"date": "2026-06-17"}]), \
                 patch.object(backend, "model_data_quality", return_value=quality), \
                 patch.object(backend, "latest_complete_price_date", return_value="2026-07-09"), \
                 patch.object(backend, "market_data_quality") as market_quality:
                result = backend.model_prediction_eligibility("6288", market_quality_cache={})

        self.assertFalse(result["eligible"])
        self.assertEqual(result["stage"], "stock_data_freshness")
        self.assertEqual(result["latestDate"], "2026-06-17")
        self.assertEqual(result["expectedLatestDate"], "2026-07-09")
        self.assertTrue(result["repairable"])
        market_quality.assert_not_called()

    def test_on_demand_prediction_rejects_stale_stock_before_inference(self):
        with tempfile.TemporaryDirectory() as tmp:
            backend = self.backend(tmp)
            quality = {
                "ok": True,
                "missing": [],
                "rows": 200,
                "latestDate": "2026-06-17",
            }
            with patch.object(
                backend, "load_model_with_error", return_value=({"trained_at": "test"}, "")
            ), patch.object(
                backend, "latest_complete_price_date", return_value="2026-07-09"
            ), patch.object(
                backend, "ensure_model_ready_rows",
                return_value=([{"date": "2026-06-17"}], quality),
            ), patch.object(backend, "build_features_for_rows") as features:
                with self.assertRaisesRegex(RuntimeError, "stock data stale"):
                    backend.predict_symbol("6288", save=False, repair=False)

        features.assert_not_called()

    def test_automatic_batch_overfetches_until_requested_eligible_count(self):
        with tempfile.TemporaryDirectory() as tmp:
            backend = self.backend(tmp)
            pool = [f"{index:04d}" for index in range(1, 9)]

            def eligibility(symbol, market_quality_cache=None):
                if symbol in {"0001", "0002"}:
                    return {
                        "eligible": False,
                        "symbol": symbol,
                        "stage": "stock_data_quality",
                        "reason": "marginSourceCoverageOk",
                        "repairable": True,
                    }
                return {
                    "eligible": True,
                    "symbol": symbol,
                    "stage": "ready",
                    "latestDate": "2026-07-10",
                }

            with patch.object(backend, "liquid_monster_universe", return_value=pool), \
                 patch.object(backend, "load_model_with_error", return_value=({"version": "m1"}, "")), \
                 patch.object(backend, "model_prediction_eligibility", side_effect=eligibility), \
                 patch.object(
                     backend, "predict_symbol",
                     return_value={"priceDate": "2026-07-10", "action": "WAIT"},
                 ) as predict, \
                 patch.object(backend, "sync_model_prediction_signals", return_value={"saved": 0}):
                result = backend.batch_save_predictions(limit=5)

        self.assertEqual(result["candidate_pool_total"], 8)
        self.assertEqual(result["symbols_total"], 7)
        self.assertEqual(result["eligible_total"], 5)
        self.assertTrue(result["filled_to_requested"])
        self.assertEqual(result["eligible_shortfall"], 0)
        self.assertEqual(result["repair_symbols"], ["0001", "0002"])
        self.assertEqual(predict.call_count, 5)

    def test_data_gap_repair_prioritizes_model_prediction_gaps(self):
        with tempfile.TemporaryDirectory() as tmp:
            backend = self.backend(tmp)
            with backend.connect() as conn:
                backend.set_meta(
                    conn,
                    "last_batch_predictions_repair_symbols_json",
                    json.dumps(["9999", "8888"]),
                )
            with patch.object(server, "backend", backend), \
                 patch.object(
                     server,
                     "auto_model_training_symbols",
                     return_value=(["1111", "2222"], {"holdings": []}, []),
                 ):
                result = server.data_gap_repair_symbol_universe(max_symbols=3)

        self.assertEqual(result["symbols"], ["9999", "8888", "1111"])
        self.assertEqual(result["sources"]["model_prediction_gaps"], ["9999", "8888"])

    def test_automatic_gap_repair_backs_off_same_unchanged_gap_for_the_day(self):
        with tempfile.TemporaryDirectory() as tmp:
            backend = self.backend(tmp)
            detail = {
                "symbol": "9999",
                "date": "2026-07-10",
                "updatedRows": 0,
                "complete": False,
                "needsRepair": True,
                "missing": [],
                "qualityMissing": ["marginSourceCoverageOk"],
            }
            with patch.object(server, "backend", backend), \
                 patch.object(
                     server,
                     "data_gap_repair_symbol_universe",
                     return_value={"symbols": ["9999"], "sources": {}, "errors": []},
                 ), \
                 patch.object(server, "data_gap_symbol_detail", return_value=detail), \
                 patch.object(server, "scheduler_today", return_value="2026-07-10"), \
                 patch.object(
                     backend,
                     "sync_official_daily_snapshot",
                     return_value={"ok": True, "available": 1, "written": 1},
                 ), \
                 patch.object(backend, "update_prices", return_value={"9999": 0}) as update:
                first = server.run_data_gap_repair(max_symbols=1, trigger="auto-test")
                second = server.run_data_gap_repair(max_symbols=1, trigger="auto-test")

        self.assertEqual(first["stillMissing"], 1)
        self.assertEqual(second["stillMissing"], 1)
        self.assertEqual(second["details"][0]["status"], "retry_cooldown")
        self.assertEqual(first["attempted"], 1)
        self.assertEqual(second["attempted"], 0)
        self.assertEqual(second["deferred"], 1)
        update.assert_called_once()

    def test_gap_repair_marks_known_unavailable_fields_not_applicable(self):
        fields, quality = server.data_gap_not_applicable_fields(
            "6831",
            {
                "per": None,
                "pbr": 2.5,
                "dividend_yield": 0.0,
                "valuation_source": "TWSE official MI_INDEX afterTrading",
            },
            stock_info={
                "6831": {"sector": "電腦及週邊設備業", "market_type": "emerging"},
            },
        )

        self.assertIn("margin_balance", fields)
        self.assertIn("short_balance", fields)
        self.assertIn("per", fields)
        self.assertIn("marginSourceCoverageOk", quality)

    def test_auto_gap_repair_busy_retries_instead_of_marking_success(self):
        with patch.object(
            server,
            "run_data_gap_repair",
            return_value={"ok": False, "busy": True, "retry": True, "message": "busy"},
        ):
            result = server.auto_data_gap_repair("auto-test")
        self.assertIs(result, server.AUTO_SCHEDULE_RETRY)

    def test_auto_gap_repair_unresolved_result_is_partial_not_success(self):
        with patch.object(
            server,
            "run_data_gap_repair",
            return_value={
                "ok": False,
                "busy": False,
                "failed": 0,
                "stillMissing": 2,
                "message": "仍缺 2 檔",
            },
        ):
            result = server.auto_data_gap_repair("auto-test")
        self.assertEqual(result["scheduleStatus"], "partial")
        self.assertIn("不視為完整成功", result["message"])

    def test_batch_does_not_recompute_existing_model_date_prediction(self):
        with tempfile.TemporaryDirectory() as tmp:
            backend = self.backend(tmp)
            with backend.connect() as conn:
                conn.execute("""
                    INSERT INTO predictions (
                        created_at, symbol, price_date, model_version, probability,
                        threshold, action, target_horizon, target_return, close
                    ) VALUES (
                        '2026-07-10 15:10:00', '2330', '2026-07-10', 'm1', 0.5,
                        0.45, 'WAIT', 10, 0.10, 100
                    )
                """)
            eligibility = {
                "eligible": True, "symbol": "2330", "stage": "ready",
                "latestDate": "2026-07-10",
            }
            with patch.object(backend, "load_model_with_error", return_value=({"version": "m1"}, "")), \
                 patch.object(backend, "model_prediction_eligibility", return_value=eligibility), \
                 patch.object(backend, "predict_symbol") as predict, \
                 patch.object(backend, "sync_model_prediction_signals", return_value={"saved": 0}):
                result = backend.batch_save_predictions(symbols=["2330"])

        self.assertEqual(result["saved"], 0)
        self.assertEqual(result["skipped_existing_count"], 1)
        self.assertEqual(result["error_count"], 0)
        predict.assert_not_called()

    def test_auto_batch_persists_detailed_ineligible_and_error_payloads(self):
        with tempfile.TemporaryDirectory() as tmp:
            backend = self.backend(tmp)
            batch = {
                "ok": True,
                "symbols_total": 3,
                "eligible_total": 1,
                "saved": 1,
                "skipped": 1,
                "skipped_ineligible_count": 1,
                "ineligible": [{"symbol": "9999", "reason": "priceRowsEnough"}],
                "error_count": 1,
                "errors": [{"symbol": "8888", "stage": "prediction", "error": "boom"}],
                "paper_signal_count": 0,
                "paper_signal_errors": [{"priceDate": "2026-07-10", "error": "paper boom"}],
            }
            with patch.object(server, "backend", backend), \
                 patch.object(backend, "batch_save_predictions", return_value=batch):
                message = server.auto_batch_save_predictions()
            with backend.connect() as conn:
                meta = {row[0]: row[1] for row in conn.execute(
                    "SELECT key, value FROM model_meta WHERE key LIKE 'last_batch_predictions_%'"
                ).fetchall()}

        self.assertIn("資料不足跳過 1 檔", message)
        self.assertEqual(json.loads(meta["last_batch_predictions_ineligible_json"])[0]["symbol"], "9999")
        self.assertEqual(json.loads(meta["last_batch_predictions_errors_json"])[0]["stage"], "prediction")
        self.assertEqual(
            json.loads(meta["last_batch_predictions_paper_signal_errors_json"])[0]["error"],
            "paper boom",
        )

    def test_model_freshness_uses_artifact_training_data_date(self):
        with tempfile.TemporaryDirectory() as tmp:
            backend = self.backend(tmp)
            rows = []
            for day in ("2026-07-09", "2026-07-10"):
                rows.extend([
                    (f"{index:04d}", day, 10, 10, 10, 10, 1000, f"{day} 14:00:00")
                    for index in range(1500)
                ])
            with backend.connect() as conn:
                conn.executemany("""
                    INSERT INTO prices (
                        symbol, date, open, high, low, close, volume, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, rows)
            stale = backend.model_training_freshness({
                "trained_at": "2026-07-12 09:00:00",
                "training_data_max_date": "2026-07-09",
                "training_sample_max_date": "2026-06-24",
            })
            fresh = backend.model_training_freshness({
                "trained_at": "2026-07-12 09:00:00",
                "training_data_max_date": "2026-07-10",
                "training_sample_max_date": "2026-06-25",
            })

        self.assertFalse(stale["ok"])
        self.assertEqual(stale["tradingSessionLag"], 1)
        self.assertEqual(stale["trainingDataMaxDate"], "2026-07-09")
        self.assertTrue(fresh["ok"])
        self.assertEqual(fresh["freshnessBasisDate"], "2026-07-10")

    def test_backfill_adds_verified_training_date_without_retraining(self):
        with tempfile.TemporaryDirectory() as tmp:
            backend = self.backend(tmp)
            model = {
                "version": "legacy-model",
                "trained_at": "2026-07-12 09:06:32",
                "data_policy": DATA_POLICY_VERSION,
                "feature_names": FEATURE_NAMES,
                "symbols": ["2330"],
            }
            backend.model_path.write_bytes(pickle.dumps(model))
            backend.write_model_env(model)
            with backend.connect() as conn:
                conn.execute("""
                    INSERT INTO prices (
                        symbol, date, open, high, low, close, volume, updated_at, price_source
                    ) VALUES (
                        '2330', '2026-07-10', 100, 101, 99, 100, 1000000,
                        '2026-07-12 08:45:00', 'TWSE official'
                    )
                """)
            result = backend.backfill_active_model_training_dates()
            stored = pickle.loads(backend.model_path.read_bytes())
            public = backend.public_model(stored)

        self.assertTrue(result["updated"])
        self.assertEqual(stored["training_data_max_date"], "2026-07-10")
        self.assertEqual(public["trainingDataMaxDate"], "2026-07-10")
        self.assertEqual(
            stored["training_date_metadata_source"],
            "verified_prices_updated_at_before_training",
        )


if __name__ == "__main__":
    unittest.main()
