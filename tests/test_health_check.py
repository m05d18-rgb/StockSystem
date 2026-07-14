"""
health_check.py 的回歸測試，對應這次修的 bug：

run_system_health() 的 predictionTest 舊版只測單一寫死的股票(預設 2330)，
那檔股票自己的資料一次性缺口(不是模型/程式碼本身壞掉)就會讓整個健康檢查
判定失敗，連帶讓 ok=False/decisionsEnabled=False 波及「全部股票」的
data_health()/Brain Engine 判斷——不管其他股票的預測管線其實是通的。

修法：_run_prediction_health_check() 主要股票失敗時，換幾檔跨產業股票
(HEALTH_CHECK_FALLBACK_SYMBOLS)再試，只要有一檔測得出結果就代表管線本身
是通的；全部候選都失敗才是真的系統性故障。

全部用 patch.object(backend, "predict_symbol", ...) 餵假結果，不觸碰真實
Shioaji/FinMind/資料庫。

執行方式：
  python -m unittest tests.test_health_check -v
"""
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ml_backend import backend
from health_check import HEALTH_CHECK_FALLBACK_SYMBOLS, _run_prediction_health_check, run_system_health


class RunPredictionHealthCheckTests(unittest.TestCase):
    def test_primary_symbol_succeeds_uses_only_one_attempt(self):
        with patch.object(backend, "predict_symbol", return_value={"probability": 0.5}) as mock_predict:
            prediction, attempts, error = _run_prediction_health_check("2330")
        self.assertIsNotNone(prediction)
        self.assertIsNone(error)
        self.assertEqual(len(attempts), 1)
        self.assertEqual(mock_predict.call_count, 1)

    def test_primary_symbol_data_hiccup_falls_back_to_other_industries(self):
        # 2330 本身這次剛好資料有問題(丟例外)，但跨產業備援股票測得出結果
        # ——代表預測管線本身是通的，不該整個健康檢查判定失敗。
        def fake_predict(symbol, save=False, repair=False):
            if symbol == "2330":
                raise RuntimeError("2330 資料暫時缺口")
            return {"probability": 0.3}

        with patch.object(backend, "predict_symbol", side_effect=fake_predict):
            prediction, attempts, error = _run_prediction_health_check("2330")
        self.assertIsNotNone(prediction)
        self.assertIsNone(error)
        self.assertGreaterEqual(len(attempts), 2)
        self.assertFalse(attempts[0]["ok"])
        self.assertIn("2330 資料暫時缺口", attempts[0]["error"])
        self.assertTrue(any(a["ok"] for a in attempts[1:]))

    def test_all_candidates_failing_is_a_real_systemic_error(self):
        with patch.object(backend, "predict_symbol", side_effect=RuntimeError("model broken")):
            prediction, attempts, error = _run_prediction_health_check("2330")
        self.assertIsNone(prediction)
        self.assertIsNotNone(error)
        self.assertEqual(len(attempts), 1 + len(HEALTH_CHECK_FALLBACK_SYMBOLS))
        self.assertTrue(all(not a["ok"] for a in attempts))

    def test_no_result_but_no_exception_still_falls_back(self):
        # predict_symbol 回傳 None/空 dict(非例外)一樣要當作這個候選沒過。
        def fake_predict(symbol, save=False, repair=False):
            return None if symbol == "2330" else {"probability": 0.4}

        with patch.object(backend, "predict_symbol", side_effect=fake_predict):
            prediction, attempts, error = _run_prediction_health_check("2330")
        self.assertIsNotNone(prediction)
        self.assertIsNone(error)


class RunSystemHealthModelLoadErrorTests(unittest.TestCase):
    """對應這次修的 bug：run_system_health() 原本是「呼叫 backend.load_model()
    -> 另外讀 backend._model_load_error」，這在 ThreadingHTTPServer 下有
    TOCTOU 競態(另一個執行緒的 load_model() 呼叫可能在兩步之間把這個共享
    屬性洗成別的結果)。改用 load_model_with_error() 回傳的 tuple，這裡驗證
    錯誤訊息確實來自這次呼叫本身的回傳值，不是共享屬性。"""

    def test_load_failure_reports_the_error_from_this_call_not_shared_state(self):
        # 故意把共享屬性設成別的(模擬另一個執行緒殘留的舊值)，驗證
        # run_system_health 回報的錯誤是這次 load_model_with_error() 回傳的
        # 值，不是殘留的共享屬性。
        backend._model_load_error = "殘留自其他執行緒的舊錯誤，不該被顯示"
        with patch.object(backend, "load_model_with_error", return_value=(None, "這次真正的失敗原因")):
            health = run_system_health(symbol="2330", include_prediction=False)
        self.assertIn("這次真正的失敗原因", health["errors"])
        self.assertNotIn("殘留自其他執行緒的舊錯誤，不該被顯示", health["errors"])
        self.assertEqual(health["model"]["loadError"], "這次真正的失敗原因")


class RunSystemHealthLiveSpotCheckTests(unittest.TestCase):
    def test_against_real_pipeline_does_not_crash_and_reports_attempts(self):
        # 跟 market_data_quality/check_symbol 的驗證方式一致：直接對真實
        # 資料/模型跑一次，只確認結構正確、不拋例外，不斷言 ok 一定是
        # True(真實環境資料狀態會變動)。
        health = run_system_health(symbol="2330", include_prediction=True)
        self.assertIn("predictionTest", health)
        self.assertIn("attempts", health["predictionTest"])
        self.assertIsInstance(health["predictionTest"]["attempts"], list)


if __name__ == "__main__":
    unittest.main()
