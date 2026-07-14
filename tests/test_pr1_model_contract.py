import os
import pickle
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ml_backend


class ModelPolicyContractTests(unittest.TestCase):
    def _model(self):
        symbols = ["2330", "2454"]
        threshold = 0.5
        return {
            "feature_names": list(ml_backend.FEATURE_NAMES),
            "data_policy": ml_backend.DATA_POLICY_VERSION,
            "target_type": ml_backend.SHORT_PROFIT_TARGET_TYPE,
            "target_policy_hash": ml_backend.SHORT_PROFIT_POLICY_HASH,
            "symbols": symbols,
            "threshold": threshold,
            "policy_spec": ml_backend.short_profit_policy_spec(symbols, threshold),
            "policy_hash": ml_backend.short_profit_policy_hash(symbols, threshold),
        }

    def test_loader_accepts_matching_policy_and_rejects_tampered_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            model_path = Path(tmp) / "model.pkl"
            test_backend = ml_backend.StockMLBackend()
            test_backend.model_path = model_path
            with patch.object(test_backend, "read_model_env", return_value=None):
                model_path.write_bytes(pickle.dumps(self._model()))
                loaded, error = test_backend.load_model_with_error()
                self.assertIsNotNone(loaded)
                self.assertEqual(error, "")

                tampered = self._model()
                tampered["threshold"] = 0.54
                model_path.write_bytes(pickle.dumps(tampered))
                test_backend._model_cache = None
                loaded, error = test_backend.load_model_with_error()
                self.assertIsNone(loaded)
                self.assertEqual(error, "model policy contract mismatch")


class ValidationIsolationTests(unittest.TestCase):
    class _ZeroRegressor:
        def __init__(self, **_kwargs):
            pass

        def fit(self, _x, _y, sample_weight=None):
            return self

        def predict(self, x):
            return np.zeros(len(x), dtype=float)

    @staticmethod
    def _row(value, y, date):
        return {
            "x": [value],
            "y": y,
            "date": date,
            "future_return": value,
            "expected_return": value,
            "target_strength": 1.0,
            "net_return_3d": value,
            "net_return_5d": value,
            "net_return_10d": value,
        }

    def test_regression_bias_uses_training_residuals_not_validation_answers(self):
        train = [self._row(0.10, 0, "2026-01-01"), self._row(0.20, 1, "2026-01-02")]
        validation = [self._row(0.90, 0, "2026-02-01"), self._row(1.00, 1, "2026-02-02")]
        with patch.object(ml_backend, "XGBClassifier", None), \
             patch.object(ml_backend, "LGBMClassifier", None), \
             patch.object(ml_backend, "HistGradientBoostingClassifier", None), \
             patch.object(ml_backend, "IsolationForest", None), \
             patch.object(ml_backend, "GradientBoostingRegressor", None), \
             patch.object(ml_backend, "HistGradientBoostingRegressor", self._ZeroRegressor):
            result = ml_backend.backend.train_extra_models(
                train, validation, lambda values: values, threshold=0.5,
            )

        self.assertAlmostEqual(result["learning_to_rank"]["bias_correction"], 0.15)
        for horizon in ("3", "5", "10"):
            model = result["short_horizon_returns"][horizon]
            self.assertAlmostEqual(model["bias_correction"], 0.15)
            self.assertAlmostEqual(model["metrics"]["mae"], 0.80)


class PredictionSignalIndexTests(unittest.TestCase):
    def test_signal_sync_query_has_covering_filter_index(self):
        with tempfile.TemporaryDirectory() as tmp:
            test_backend = ml_backend.StockMLBackend()
            test_backend.db_path = Path(tmp) / "stock_system.sqlite3"
            test_backend.init_db()
            with test_backend.connect() as conn:
                indexes = {row[1] for row in conn.execute("PRAGMA index_list(predictions)")}
                plan = conn.execute("""
                    EXPLAIN QUERY PLAN
                    SELECT created_at, symbol, price_date, model_version,
                           probability, threshold, action, target_type, close
                    FROM predictions
                    WHERE action = 'BUY_CANDIDATE' AND target_type = ? AND price_date = ?
                    ORDER BY price_date ASC, symbol ASC, created_at DESC
                """, (ml_backend.SHORT_PROFIT_TARGET_TYPE, "2026-07-14")).fetchall()

        self.assertIn("idx_predictions_signal_sync", indexes)
        self.assertTrue(any("idx_predictions_signal_sync" in str(row) for row in plan), plan)


if __name__ == "__main__":
    unittest.main()
