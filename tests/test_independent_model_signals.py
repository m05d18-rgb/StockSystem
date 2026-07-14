"""獨立模型紙上訊號不得取自妖股候選或混入其他策略。"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ml_backend import backend


class IndependentModelSignalTests(unittest.TestCase):
    PRICE_DATE = "2099-01-02"
    MODEL_VERSION = "ztest-independent-model"

    def setUp(self):
        backend.init_db()
        with backend.connect() as conn:
            conn.execute(
                "DELETE FROM strategy_signals WHERE signal_date = ? AND (strategy LIKE 'model_%' OR strategy = 'ztest_other')",
                (self.PRICE_DATE,),
            )
            conn.execute(
                "DELETE FROM predictions WHERE price_date = ? AND model_version = ?",
                (self.PRICE_DATE, self.MODEL_VERSION),
            )
            for index in range(25):
                symbol = f"Z{index:03d}"
                conn.execute("""
                    INSERT INTO predictions (
                        created_at, symbol, price_date, model_version, probability,
                        threshold, action, target_horizon, target_return, close
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    f"{self.PRICE_DATE} 15:10:{index:02d}", symbol, self.PRICE_DATE,
                    self.MODEL_VERSION, 0.90 - index * 0.01, 0.55,
                    "BUY_CANDIDATE", 10, 0.10, 100 + index,
                ))
            conn.execute("""
                INSERT INTO predictions (
                    created_at, symbol, price_date, model_version, probability,
                    threshold, action, target_horizon, target_return, close
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                f"{self.PRICE_DATE} 15:11:00", "ZWAIT", self.PRICE_DATE,
                self.MODEL_VERSION, 0.99, 0.55, "WAIT", 10, 0.10, 100,
            ))

    def tearDown(self):
        with backend.connect() as conn:
            conn.execute(
                "DELETE FROM strategy_signals WHERE signal_date = ? AND (strategy LIKE 'model_%' OR strategy = 'ztest_other')",
                (self.PRICE_DATE,),
            )
            conn.execute(
                "DELETE FROM predictions WHERE price_date = ? AND model_version = ?",
                (self.PRICE_DATE, self.MODEL_VERSION),
            )

    def test_sync_uses_only_model_buy_candidates_and_caps_daily_positions(self):
        result = backend.sync_model_prediction_signals(
            price_date=self.PRICE_DATE,
            max_per_day=20,
        )
        self.assertEqual(result["sourcePredictions"], 25)
        self.assertEqual(result["saved"], 20)
        with backend.connect() as conn:
            rows = conn.execute("""
                SELECT symbol, strategy, side, score, decision_source
                FROM strategy_signals
                WHERE signal_date = ? AND strategy LIKE 'model_%'
                ORDER BY score DESC
            """, (self.PRICE_DATE,)).fetchall()
        self.assertEqual(len(rows), 20)
        self.assertTrue(all(row[1] == "model_ensemble_10d" for row in rows))
        self.assertTrue(all(row[2] == "BUY_CANDIDATE" for row in rows))
        self.assertTrue(all("不使用妖股候選" in row[4] for row in rows))
        self.assertNotIn("ZWAIT", {row[0] for row in rows})
        self.assertIn("Z000", {row[0] for row in rows})
        self.assertNotIn("Z024", {row[0] for row in rows})

    def test_model_scope_excludes_unrelated_strategy_rows(self):
        before = backend.strategy_signal_performance(
            refresh_outcomes=False,
            strategy_prefix="model_",
        )["overall"]["signals"]
        backend.sync_model_prediction_signals(price_date=self.PRICE_DATE, max_per_day=3)
        with backend.connect() as conn:
            backend.save_strategy_signal(conn, {
                "signalDate": self.PRICE_DATE,
                "strategy": "ztest_other",
                "side": "BUY_CANDIDATE",
                "symbol": "ZOTHER",
                "price": 100,
            })
        result = backend.strategy_signal_performance(
            refresh_outcomes=False,
            strategy_prefix="model_",
        )
        self.assertEqual(result["overall"]["signals"], before + 3)
        self.assertTrue(all(
            str(item["strategy"]).startswith("model_")
            for item in result["strategies"]
        ))


if __name__ == "__main__":
    unittest.main()
