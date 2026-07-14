"""
predict_symbol 結果快取的回歸測試。

背景：predict_symbol 完全沒有結果快取，三個入口(server.py 個股查詢、
Brain Engine build_brain_decision、妖股掃描/紙上交易快照)各自重跑完整
特徵計算+5模型推論；同一檔股票同時是持股又是妖股候選時，一輪快照掃描
會對它算兩次(先前正式模型審查已確認)。

快取設計的三道正確性保險，每道都要有測試：
  1. TTL(PREDICT_RESULT_CACHE_TTL_SECONDS)過期重算
  2. 模型 trained_at 變動(重訓)時快取視為無效
  3. upsert_price_rows 寫入該股新日K時逐股失效、update_market_data
     更新大盤資料時全部失效

另外：快取回傳 deepcopy(呼叫端就地修改不能污染快取)、save=True 命中
「save=False 存的快取」時仍要補寫 predictions 表。

用 2330 的真實資料庫日K做讀取(repair=False、save=False，不打網路不寫
predictions 表)；需要寫入的測試用 mock 隔絕。

執行方式：
  python -m unittest tests.test_predict_cache -v
"""
import os
import sys
import time
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ml_backend
from ml_backend import backend


class PredictCacheTests(unittest.TestCase):
    SYMBOL = "2330"

    def setUp(self):
        backend._predict_cache.clear()

    def tearDown(self):
        backend._predict_cache.clear()

    def test_second_call_within_ttl_skips_recompute_and_returns_equal_payload(self):
        with patch.object(backend, "build_features_for_rows", wraps=backend.build_features_for_rows) as spy:
            first = backend.predict_symbol(self.SYMBOL, save=False, repair=False)
            second = backend.predict_symbol(self.SYMBOL, save=False, repair=False)
        self.assertEqual(spy.call_count, 1, "第二次呼叫應該命中快取，不重跑特徵計算")
        self.assertEqual(first, second)

    def test_mutating_returned_payload_does_not_pollute_cache(self):
        first = backend.predict_symbol(self.SYMBOL, save=False, repair=False)
        first["probability"] = -999
        first["tradeGate"]["scoreOk"] = "污染"
        second = backend.predict_symbol(self.SYMBOL, save=False, repair=False)
        self.assertNotEqual(second["probability"], -999)
        self.assertNotEqual(second["tradeGate"]["scoreOk"], "污染")

    def test_expired_ttl_recomputes(self):
        backend.predict_symbol(self.SYMBOL, save=False, repair=False)
        backend._predict_cache[self.SYMBOL]["at"] = time.time() - ml_backend.PREDICT_RESULT_CACHE_TTL_SECONDS - 1
        with patch.object(backend, "build_features_for_rows", wraps=backend.build_features_for_rows) as spy:
            backend.predict_symbol(self.SYMBOL, save=False, repair=False)
        self.assertEqual(spy.call_count, 1, "TTL 過期必須重算")

    def test_model_retrain_invalidates_cache(self):
        backend.predict_symbol(self.SYMBOL, save=False, repair=False)
        backend._predict_cache[self.SYMBOL]["trainedAt"] = "1999-01-01 00:00:00"
        with patch.object(backend, "build_features_for_rows", wraps=backend.build_features_for_rows) as spy:
            backend.predict_symbol(self.SYMBOL, save=False, repair=False)
        self.assertEqual(spy.call_count, 1, "trained_at 不符(模型已重訓)必須重算")

    def test_upsert_price_rows_invalidates_that_symbol_only(self):
        backend._predict_cache["2330"] = {"at": time.time(), "trainedAt": "x", "payload": {}, "saved": True}
        backend._predict_cache["2317"] = {"at": time.time(), "trainedAt": "x", "payload": {}, "saved": True}
        rows = [{
            "symbol": "2330", "date": "2099-01-01",
            "open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 1.0,
        }]
        fake_conn = MagicMock()
        backend.upsert_price_rows(rows, conn=fake_conn)  # 用假連線，不寫真實DB
        self.assertNotIn("2330", backend._predict_cache)
        self.assertIn("2317", backend._predict_cache, "別檔股票的快取不該被波及")

    def test_update_market_data_clears_entire_cache(self):
        backend._predict_cache["2330"] = {"at": time.time(), "trainedAt": "x", "payload": {}, "saved": True}
        backend._predict_cache["2317"] = {"at": time.time(), "trainedAt": "x", "payload": {}, "saved": True}
        fake_conn = MagicMock(__enter__=MagicMock(return_value=MagicMock()), __exit__=MagicMock(return_value=False))
        with patch.object(backend, "fetch_yahoo_chart_rows", return_value=[{"date": "2026-01-01", "close": 1.0}]), \
             patch.object(backend, "connect", return_value=fake_conn), \
             patch.object(backend, "store_market_rows"), \
             patch.object(backend, "set_meta"):
            backend.update_market_data()
        self.assertEqual(backend._predict_cache, {}, "大盤資料變動影響所有股票，快取要全清")

    def test_cache_hit_with_save_true_after_save_false_still_persists_row(self):
        backend.predict_symbol(self.SYMBOL, save=False, repair=False)
        self.assertFalse(backend._predict_cache[self.SYMBOL]["saved"])
        with patch.object(backend, "_save_prediction_row") as mock_save:
            payload = backend.predict_symbol(self.SYMBOL, save=True, repair=False)
        mock_save.assert_called_once()
        self.assertTrue(backend._predict_cache[self.SYMBOL]["saved"])
        self.assertEqual(payload["symbol"], self.SYMBOL)
        # 再一次 save=True 不該重複寫入
        with patch.object(backend, "_save_prediction_row") as mock_save_again:
            backend.predict_symbol(self.SYMBOL, save=True, repair=False)
        mock_save_again.assert_not_called()

    def test_failed_prediction_is_not_cached(self):
        backend._predict_cache.clear()
        with patch.object(backend, "ensure_model_ready_rows", return_value=([], {
            "ok": False, "missing": ["rows"], "rows": 0,
            "chipCoverage": 0.0, "chipSourceCoverage": 0.0, "marginSourceCoverage": 0.0,
            "financeCoverage": 0.0, "financeSourceCoverage": 0.0,
        })):
            with self.assertRaises(RuntimeError):
                backend.predict_symbol("9999", save=False, repair=False)
        self.assertNotIn("9999", backend._predict_cache, "失敗的預測不能進快取")

    def test_invalidation_during_compute_is_not_lost(self):
        # 2026-07-04 稽核修復(Finding 2)回歸：read-compute-then-write 的 lost
        # invalidation 競態。執行緒A算到一半時，執行緒B寫入新日K失效該股(此時A
        # 還沒寫快取、pop 是 no-op)，A原本會用失效前資料覆寫快取，讓過時預測
        # 殘留最長 TTL 秒。修復後 A 寫回前比對 _predict_cache_gen，發現計算期間
        # 被失效過就放棄寫回。這裡在 build_features_for_rows(計算中途、且在
        # predict_symbol 讀鎖已釋放之後)觸發 _invalidate_predict_cache 模擬並發B。
        backend._predict_cache.clear()
        real_build = backend.build_features_for_rows

        def build_then_invalidate(*args, **kwargs):
            result = real_build(*args, **kwargs)
            backend._invalidate_predict_cache([{"symbol": self.SYMBOL}])  # 模擬 Thread B
            return result

        with patch.object(backend, "build_features_for_rows", side_effect=build_then_invalidate):
            backend.predict_symbol(self.SYMBOL, save=False, repair=False)
        self.assertNotIn(self.SYMBOL, backend._predict_cache,
                         "計算期間被失效，過時 payload 不該被寫回快取(gen 守衛)")

    def test_no_invalidation_during_compute_writes_cache_normally(self):
        # 對照組：沒有並發失效時，gen 不變，payload 正常寫入快取。
        backend._predict_cache.clear()
        backend.predict_symbol(self.SYMBOL, save=False, repair=False)
        self.assertIn(self.SYMBOL, backend._predict_cache, "無並發失效時應正常寫快取")

    def test_degraded_gates_flag_when_isolation_forest_missing(self):
        # 2026-07-04 稽核修復(Finding 1)回歸：extra_models available=True 但
        # isolation_forest 個別訓練失敗缺席時，anomaly 硬閘門靜默恆過——payload
        # 要用 degradedGates/anomalyGateActive 把這個退化顯性化(不改放行判定)。
        backend._predict_cache.clear()
        with patch.object(backend, "extra_model_probabilities", return_value={
            "xgboost": 0.6, "lightgbm": 0.6, "gradient_boosting": 0.6, "learning_to_rank": 0.6,
        }):
            payload = backend.predict_symbol(self.SYMBOL, save=False, repair=False)
        self.assertIn("anomaly", payload["degradedGates"])
        self.assertNotIn("rank", payload["degradedGates"], "learning_to_rank 有給，不算 rank 退化")
        self.assertFalse(payload["anomalyGateActive"])

    def test_no_degraded_gates_when_full_ensemble_present(self):
        backend._predict_cache.clear()
        with patch.object(backend, "extra_model_probabilities", return_value={
            "xgboost": 0.6, "lightgbm": 0.6, "gradient_boosting": 0.6,
            "learning_to_rank": 0.6, "isolation_forest": 0.6,
        }):
            payload = backend.predict_symbol(self.SYMBOL, save=False, repair=False)
        self.assertEqual(payload["degradedGates"], [])
        self.assertTrue(payload["anomalyGateActive"])


if __name__ == "__main__":
    unittest.main()
