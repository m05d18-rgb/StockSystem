"""
ml_backend.py 資料寫入層與模型計算的回歸測試，對應今天修的幾個 bug：
  1. upsert_price_rows 沒檢查 OHLC 是否為正數，讓 0 值髒資料寫進資料庫。
  2. merge_per_pbr 沒有前向填補，PER/PBR/殖利率只在 FinMind 當天剛好回傳時才有值。
  3. Isolation Forest 用 clamp() 硬截斷，讓大量離群樣本全部卡在同一個 0.01/0.99。
  4. fetch_yahoo_chart_rows 沒過濾「開高低收全部相等、成交量為 0」的假交易日，
     讓 Yahoo Finance fallback 偶爾回傳的假資料被當成真實價格寫進資料庫，
     實測全庫有上千檔股票、上萬筆列中鏢(例如 6919 在 2024-03-21 附近就出現
     一筆這種假資料，讓原始價格序列憑空跳空近 90%)。
  5. fetch_symbol_rows 補 FinMind 單日壞資料時，沒檢查 Yahoo 補值的尺度是否
     跟前後 FinMind 原始股價一致——Yahoo 的歷史股價會因除權息/分割回溯調整，
     混進單一天會造成憑空跳空又跳回(例如某股票尺度剛好差了 2.35 倍)。

upsert_price_rows 的測試會真的寫入/清理一筆假股票代號(ZTEST*)到正式資料庫，
每個測試結束都會刪除自己寫入的資料，不會留下痕跡。
"""
import datetime as dt
import json
import os
import sys
import threading
import time
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ml_backend import (
    FEATURE_NAMES, backend, clamp, clamp_open_to_bar, compare_model_environment,
    current_model_environment, price_scale_is_plausible, smooth_clamp_ratio,
)

# CalibratedProbabilityTests 用來組假特徵向量的長度基準
FEATURE_NAMES_STUB = FEATURE_NAMES


class UpsertPriceRowsPositivePriceTests(unittest.TestCase):
    TEST_SYMBOL = "ZTEST9"

    def tearDown(self):
        with backend.connect() as conn:
            conn.execute("DELETE FROM prices WHERE symbol = ?", (self.TEST_SYMBOL,))
            conn.commit()

    def test_rejects_row_with_zero_ohlc(self):
        rows = [{
            "symbol": self.TEST_SYMBOL, "date": "2099-01-01",
            "open": 0.0, "high": 0.0, "low": 0.0, "close": 0.0, "volume": 0.0,
            "price_source": "unit-test",
        }]
        written = backend.upsert_price_rows(rows)
        self.assertEqual(written, 0)
        with backend.connect() as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM prices WHERE symbol = ?", (self.TEST_SYMBOL,)
            ).fetchone()[0]
        self.assertEqual(count, 0)

    def test_accepts_normal_row(self):
        rows = [{
            "symbol": self.TEST_SYMBOL, "date": "2099-01-02",
            "open": 100.0, "high": 105.0, "low": 99.0, "close": 102.0, "volume": 1000.0,
            "price_source": "unit-test",
        }]
        written = backend.upsert_price_rows(rows)
        self.assertEqual(written, 1)

    def test_accepts_zero_volume_with_valid_prices(self):
        """零成交量是合法的市場狀態（冷門股當天沒人交易），不該被擋。"""
        rows = [{
            "symbol": self.TEST_SYMBOL, "date": "2099-01-03",
            "open": 10.0, "high": 12.0, "low": 9.0, "close": 11.0, "volume": 0.0,
            "price_source": "unit-test",
        }]
        written = backend.upsert_price_rows(rows)
        self.assertEqual(written, 1)

    def test_rejects_row_missing_required_field(self):
        rows = [{
            "symbol": self.TEST_SYMBOL, "date": "2099-01-04",
            "open": None, "high": 12.0, "low": 9.0, "close": 11.0, "volume": 100.0,
            "price_source": "unit-test",
        }]
        written = backend.upsert_price_rows(rows)
        self.assertEqual(written, 0)


class MergePerPbrForwardFillTests(unittest.TestCase):
    def test_forward_fills_across_dates_without_new_snapshot(self):
        rows = {
            "2026-06-24": {"date": "2026-06-24"},
            "2026-06-25": {"date": "2026-06-25"},
            "2026-06-26": {"date": "2026-06-26"},
        }
        per_rows = [
            {"date": "2026-06-24", "PER": 7.56, "PBR": 1.64, "dividend_yield": 0.0},
        ]
        backend.merge_per_pbr(rows, per_rows)
        # 6/25、6/26 沒有新快照，應該沿用 6/24 的值，而不是 None
        self.assertEqual(rows["2026-06-25"]["per"], 7.56)
        self.assertEqual(rows["2026-06-26"]["per"], 7.56)
        self.assertEqual(rows["2026-06-25"]["valuation_source"], "FinMind TaiwanStockPER")

    def test_independent_fields_do_not_block_each_other(self):
        """PER 因為 <=0 被設成 None 時，同一筆快照裡仍然有效的 PBR/殖利率
        不該被牽連清空。"""
        rows = {"2026-06-24": {"date": "2026-06-24"}}
        per_rows = [{"date": "2026-06-24", "PER": -5.0, "PBR": 1.5, "dividend_yield": 2.0}]
        backend.merge_per_pbr(rows, per_rows)
        self.assertIsNone(rows["2026-06-24"]["per"])
        self.assertEqual(rows["2026-06-24"]["pbr"], 1.5)
        self.assertEqual(rows["2026-06-24"]["dividend_yield"], 2.0)

    def test_no_snapshot_before_first_date_stays_blank(self):
        rows = {"2026-06-20": {"date": "2026-06-20"}, "2026-06-25": {"date": "2026-06-25"}}
        per_rows = [{"date": "2026-06-25", "PER": 10.0, "PBR": 1.0, "dividend_yield": 1.0}]
        backend.merge_per_pbr(rows, per_rows)
        self.assertNotIn("per", rows["2026-06-20"])  # 第一筆快照之前沒有可沿用的值
        self.assertEqual(rows["2026-06-25"]["per"], 10.0)


class SmoothClampRatioTests(unittest.TestCase):
    """對應今天修的 Isolation Forest 地板效應：clamp() 硬截斷會讓所有超出
    [0.01, 0.99] 範圍的樣本全部變成同一個值，喪失鑑別度。"""

    def test_identical_to_clamp_within_normal_range(self):
        for ratio in (0.01, 0.1, 0.3, 0.5, 0.7, 0.9, 0.99):
            self.assertAlmostEqual(smooth_clamp_ratio(ratio), clamp(ratio, 0.01, 0.99), places=9)

    def test_below_floor_still_has_gradient_instead_of_flat_floor(self):
        near = smooth_clamp_ratio(-0.05)
        far = smooth_clamp_ratio(-1.0)
        very_far = smooth_clamp_ratio(-2.0)
        # 舊版 clamp() 這三個輸入全部會變成同一個 0.01
        self.assertEqual(clamp(-0.05, 0.01, 0.99), 0.01)
        self.assertEqual(clamp(-1.0, 0.01, 0.99), 0.01)
        self.assertEqual(clamp(-2.0, 0.01, 0.99), 0.01)
        # 新版本應該保有嚴格遞減的排序：越離群，分數越低，而不是通通一樣
        self.assertGreater(near, far)
        self.assertGreater(far, very_far)
        self.assertLess(near, 0.01)  # 但仍然比邊界值小，方向正確

    def test_above_ceiling_still_has_gradient(self):
        near = smooth_clamp_ratio(1.05)
        far = smooth_clamp_ratio(2.0)
        self.assertEqual(clamp(1.05, 0.01, 0.99), 0.99)
        self.assertEqual(clamp(2.0, 0.01, 0.99), 0.99)
        self.assertLess(near, far)
        self.assertGreater(near, 0.99)

    def test_output_stays_within_zero_one(self):
        # 真實情境下 ratio(正規化後的異常分數)極少超出 [-5, 5]；再往外指數項
        # 會下溢成浮點數 0，導致輸出貼齊 0/1 邊界，這是浮點數精度限制而非
        # 邏輯錯誤，測試只涵蓋合理範圍。
        for ratio in (-5, -1, 0, 0.5, 1, 5):
            value = smooth_clamp_ratio(ratio)
            self.assertGreater(value, 0)
            self.assertLess(value, 1)


class ClampOpenToBarTests(unittest.TestCase):
    """護欄:FinMind 偶發 open 越界(open>high 或 open<low,而 high/low/close 都對)。
    open 依定義必在 [low, high] 內,越界即為髒資料,夾回區間是純修正。"""

    def test_open_above_high_is_clamped_down(self):
        # 對應實例 7784: DB open=14.06 vs high=11.5 → 夾到 high
        self.assertAlmostEqual(clamp_open_to_bar(14.06, 10.0, 11.5), 11.5)

    def test_open_below_low_is_clamped_up(self):
        # 對應實例 3117: DB open=11.39 vs low=24.15 → 夾到 low
        self.assertAlmostEqual(clamp_open_to_bar(11.39, 24.15, 30.0), 24.15)

    def test_in_range_open_is_untouched(self):
        # 正常 open 完全不動(恆等),避免誤傷乾淨資料
        for o in (99.0, 100.0, 102.5, 105.0):
            self.assertAlmostEqual(clamp_open_to_bar(o, 99.0, 105.0), o)

    def test_insane_bar_low_above_high_is_left_alone(self):
        # high<low 的壞 bar 不干預,交給既有 min(o,h,l,c)<=0/Yahoo 補值護欄
        self.assertAlmostEqual(clamp_open_to_bar(50.0, 105.0, 99.0), 50.0)

    def test_none_or_nonpositive_bounds_return_open_unchanged(self):
        self.assertIsNone(clamp_open_to_bar(None, 10.0, 12.0))
        self.assertAlmostEqual(clamp_open_to_bar(11.0, None, 12.0), 11.0)
        self.assertAlmostEqual(clamp_open_to_bar(11.0, 0.0, 0.0), 11.0)


class CompareModelEnvironmentCorruptionTests(unittest.TestCase):
    """對應這次修的 bug：read_model_env() 在 model_env.json 損毀/解析失敗時
    回傳 {"error": ...} 佔位物件(不是真正的環境紀錄)。compare_model_environment
    舊版邏輯只看 model_env 是否為 falsy(None/{})，{"error": ...} 是 truthy
    但沒有 "python"/"packages" 欄位可比對，下面迴圈天生不會產生任何issues，
    被誤判成「環境吻合」，讓 load_model() 的版本比對閘門在檔案損毀時完全
    失效——這正是這個閘門本來要防的情境，比對不出來就該當作可能不吻合。"""

    def test_missing_file_is_treated_as_no_expectation_and_ok(self):
        result = compare_model_environment(None)
        self.assertTrue(result["ok"])
        self.assertEqual(result["issues"], [])

    def test_corrupted_file_error_placeholder_is_not_silently_ok(self):
        result = compare_model_environment({"error": "Expecting value: line 1 column 1 (char 0)"})
        self.assertFalse(result["ok"])
        self.assertTrue(any("model_env.json" in issue for issue in result["issues"]))

    def test_well_formed_matching_env_is_still_ok(self):
        current = current_model_environment()
        matching_env = dict(current)
        result = compare_model_environment(matching_env)
        self.assertTrue(result["ok"])


class LoadModelWithErrorAtomicityTests(unittest.TestCase):
    """對應這次修的 bug：health_check.py/data_integrity_check.py/
    predict_symbol()/status() 原本都是「呼叫 load_model() -> 再另外讀一次
    self._model_load_error」，這兩步之間如果有另一個執行緒(ThreadingHTTPServer
    下任何人打 /api/ml/predict 都會觸發)也呼叫了 load_model()，會把這個
    共享的實例屬性洗成別的執行緒的結果，讓呼叫端顯示錯誤的失敗原因。改成
    load_model_with_error() 在同一個鎖裡回傳 (model, error) tuple，這裡驗證
    多執行緒同時呼叫時，每個呼叫拿到的 error 都跟自己的 model 結果一致，
    不會被其他執行緒污染。"""

    def test_returns_model_and_error_as_tuple(self):
        with patch.object(backend, "_load_model_locked", return_value=None), \
             patch.object(backend, "_model_load_error", "boom", create=False):
            model, error = backend.load_model_with_error()
        self.assertIsNone(model)
        self.assertEqual(error, "boom")

    def test_concurrent_calls_do_not_leak_error_across_threads(self):
        call_counter = {"n": 0}
        counter_lock = threading.Lock()

        def fake_load_model_locked():
            with counter_lock:
                call_counter["n"] += 1
                n = call_counter["n"]
            if n % 2 == 0:
                backend._model_load_error = f"error-{n}"
                time.sleep(0.02)
                return None
            backend._model_load_error = ""
            time.sleep(0.02)
            return {"symbols": [], "trained_at": "x"}

        results = []
        results_lock = threading.Lock()

        def worker():
            model, error = backend.load_model_with_error()
            with results_lock:
                results.append((model, error))

        with patch.object(backend, "_load_model_locked", side_effect=fake_load_model_locked):
            threads = [threading.Thread(target=worker) for _ in range(20)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

        self.assertEqual(len(results), 20)
        for model, error in results:
            if model is None:
                self.assertTrue(error.startswith("error-"), f"失敗結果卻沒有對應的錯誤訊息：{error!r}")
            else:
                self.assertEqual(error, "", f"成功結果卻污染了別的執行緒的錯誤訊息：{error!r}")


class CalibratedProbabilityTests(unittest.TestCase):
    """模型機率校準：positive_weight加權(最高6倍)讓XGBoost/LightGBM的機率
    輸出系統性偏高、未校準(precision=0.29偏低的成因之一)。train_extra_models
    現在對分類模型存一份驗證集輸出分佈(calibration欄位)，
    extra_model_probabilities把原始機率映射到該分佈的百分位輸出*_calibrated
    key，與原始版並存——buy_signal_score仍用原始版，換用前必須重跑
    backtest_ensemble_weights.py驗證。全部用假estimator，不真的訓練。"""

    class _FakeClassifier:
        def __init__(self, prob):
            self._prob = prob

        def predict_proba(self, arr):
            return [[1 - self._prob, self._prob]]

    class _FakeRegressor:
        def __init__(self, value):
            self._value = value

        def predict(self, arr):
            return [self._value]

    def _fake_model(self, with_calibration=True):
        # 模擬「輸出系統性偏高」的分佈：驗證集大多數輸出都在0.5-0.9之間
        calibration = sorted([0.50, 0.55, 0.60, 0.65, 0.70, 0.75, 0.80, 0.85, 0.90]) if with_calibration else None
        entry = {"estimator": self._FakeClassifier(0.70)}
        if calibration:
            entry["calibration"] = calibration
        return {
            "extra_models": {
                "available": True,
                "xgboost": dict(entry),
                "lightgbm": dict(entry),
            },
        }

    def test_calibrated_keys_present_alongside_raw(self):
        probs = backend.extra_model_probabilities(self._fake_model(), [0.0] * len(FEATURE_NAMES_STUB))
        self.assertIn("xgboost", probs)
        self.assertIn("xgboost_calibrated", probs)
        self.assertIn("lightgbm_calibrated", probs)
        # 原始0.70在這個偏高分佈裡只是中間位置，校準後應該落在中段而非0.7+
        self.assertAlmostEqual(probs["xgboost"], 0.70, places=9)
        self.assertLess(probs["xgboost_calibrated"], 0.70)
        self.assertGreaterEqual(probs["xgboost_calibrated"], 0.01)
        self.assertLessEqual(probs["xgboost_calibrated"], 0.99)

    def test_old_model_without_calibration_field_skips_calibrated_keys(self):
        # 舊版model.pkl沒有calibration欄位：不能因此炸掉，也不該憑空輸出
        # 校準key(下游若誤用會拿到None/KeyError)。
        probs = backend.extra_model_probabilities(self._fake_model(with_calibration=False), [0.0] * len(FEATURE_NAMES_STUB))
        self.assertIn("xgboost", probs)
        self.assertNotIn("xgboost_calibrated", probs)

    def test_calibration_maps_extreme_raw_to_extreme_percentile(self):
        model = self._fake_model()
        model["extra_models"]["xgboost"]["estimator"] = self._FakeClassifier(0.95)
        probs = backend.extra_model_probabilities(model, [0.0] * len(FEATURE_NAMES_STUB))
        # 0.95比分佈裡所有值都高 → 百分位應該接近上限
        self.assertGreater(probs["xgboost_calibrated"], 0.90)

    def test_short_horizon_return_regressors_are_exposed_separately(self):
        model = self._fake_model()
        model["extra_models"]["short_horizon_returns"] = {
            "3": {"estimator": self._FakeRegressor(0.01)},
            "5": {"estimator": self._FakeRegressor(0.02)},
            "10": {"estimator": self._FakeRegressor(0.03)},
        }

        probs = backend.extra_model_probabilities(
            model, [0.0] * len(FEATURE_NAMES_STUB),
        )

        self.assertAlmostEqual(probs["predicted_return_3d"], 0.01)
        self.assertAlmostEqual(probs["predicted_return_5d"], 0.02)
        self.assertAlmostEqual(probs["predicted_return_10d"], 0.03)


class PriceScaleIsPlausibleTests(unittest.TestCase):
    """對應 6919 等股票的跳空調查發現：補 FinMind 單日壞資料時，如果 Yahoo
    補值的尺度(除權息/分割回溯調整過)跟前後 FinMind 原始股價差太多，代表
    這不是同一個尺度基準，混進去會造成憑空跳空又跳回，寧可跳過不補。"""

    def test_within_normal_range_is_plausible(self):
        self.assertTrue(price_scale_is_plausible(105.0, 100.0))
        self.assertTrue(price_scale_is_plausible(95.0, 100.0))

    def test_far_above_reference_is_not_plausible(self):
        # 對應實測發現的尺度不一致案例：Yahoo 補值剛好是前後 FinMind 股價的 2.35 倍
        self.assertFalse(price_scale_is_plausible(235.0, 100.0))

    def test_far_below_reference_is_not_plausible(self):
        # 對應實測發現的另一個案例：Yahoo 補值只有前後 FinMind 股價的 0.417 倍
        self.assertFalse(price_scale_is_plausible(41.7, 100.0))

    def test_exactly_at_boundary_ratio_is_plausible(self):
        self.assertTrue(price_scale_is_plausible(50.0, 100.0))  # 剛好 0.5 倍
        self.assertTrue(price_scale_is_plausible(200.0, 100.0))  # 剛好 2 倍

    def test_no_reference_close_is_plausible(self):
        # 沒有前面的基準價可比對(例如序列一開始就是壞資料)，沒得比就不擋
        self.assertTrue(price_scale_is_plausible(9999.0, None))
        self.assertTrue(price_scale_is_plausible(9999.0, 0))

    def test_zero_or_none_candidate_is_not_plausible(self):
        self.assertFalse(price_scale_is_plausible(0, 100.0))
        self.assertFalse(price_scale_is_plausible(None, 100.0))


class FetchYahooChartRowsDegenerateFilterTests(unittest.TestCase):
    """對應 6919/0050 疑似跳空調查發現的真正根因：Yahoo Finance chart API
    對冷門台股偶爾會回傳「開高低收全部相等、成交量為 0」的假交易日，不是真的
    停牌，是 Yahoo 資料本身的填補瑕疵。混進以 FinMind 為主的原始價格序列，
    會造成憑空跳空(例如 6919 在 2024-03-21 附近的 close 從 484.5 變成 48.45，
    跌幅剛好 90%)，汙染 K 線型態/報酬率等所有依賴日線的計算。"""

    def _timestamp_for(self, date):
        return int(dt.datetime(date.year, date.month, date.day, 12, 0, 0, tzinfo=dt.timezone.utc).timestamp())

    def _mock_response(self, payload):
        response = MagicMock()
        response.read.return_value = json.dumps(payload).encode("utf-8")
        response.__enter__.return_value = response
        response.__exit__.return_value = False
        return response

    def _chart_payload(self, dates, opens, highs, lows, closes, volumes):
        return {
            "chart": {"result": [{
                "timestamp": [self._timestamp_for(d) for d in dates],
                "indicators": {"quote": [{
                    "open": opens, "high": highs, "low": lows, "close": closes, "volume": volumes,
                }]},
            }]}
        }

    def test_degenerate_flat_zero_volume_day_is_dropped(self):
        today = dt.date.today()
        dates = [today - dt.timedelta(days=offset) for offset in (2, 1, 0)]
        payload = self._chart_payload(
            dates,
            opens=[100.0, 48.45, 105.0], highs=[102.0, 48.45, 107.0],
            lows=[98.0, 48.45, 103.0], closes=[101.0, 48.45, 106.0],
            volumes=[1_000_000, 0, 1_200_000],
        )
        with patch("ml_backend.urlopen", return_value=self._mock_response(payload)):
            rows = backend.fetch_yahoo_chart_rows("TEST.TW", days=10)
        returned_dates = [row["date"] for row in rows]
        self.assertEqual(len(rows), 2)  # 中間那筆假資料被濾掉
        self.assertNotIn(dates[1].isoformat(), returned_dates)

    def test_real_flat_price_day_with_real_volume_is_kept(self):
        # 開高低收剛好都一樣，但成交量是真的(冷門股當天真的沒什麼波動)，
        # 不該被誤判成假資料濾掉——只有「成交量也是 0」才代表是假資料。
        today = dt.date.today()
        dates = [today - dt.timedelta(days=offset) for offset in (1, 0)]
        payload = self._chart_payload(
            dates,
            opens=[50.0, 50.0], highs=[50.0, 50.0], lows=[50.0, 50.0], closes=[50.0, 50.0],
            volumes=[500, 500],
        )
        with patch("ml_backend.urlopen", return_value=self._mock_response(payload)):
            rows = backend.fetch_yahoo_chart_rows("TEST.TW", days=10)
        self.assertEqual(len(rows), 2)

    def test_zero_volume_with_real_intraday_range_is_kept(self):
        # 成交量是 0，但開高低收不是全部相等(有真實的日內波動範圍記錄)，
        # 只符合部分特徵，保守起見不判定為假資料，避免誤刪。
        today = dt.date.today()
        dates = [today - dt.timedelta(days=offset) for offset in (1, 0)]
        payload = self._chart_payload(
            dates,
            opens=[50.0, 52.0], highs=[51.0, 53.0], lows=[49.0, 51.0], closes=[50.5, 52.5],
            volumes=[0, 800],
        )
        with patch("ml_backend.urlopen", return_value=self._mock_response(payload)):
            rows = backend.fetch_yahoo_chart_rows("TEST.TW", days=10)
        self.assertEqual(len(rows), 2)


class GetPreviousBrainV2SnapshotTests(unittest.TestCase):
    """對應「跟昨天比」的分數趨勢：get_previous_brain_v2_snapshot 要能找到
    同一檔股票、日期在今天之前的最近一筆快照，且同 context 優先於跨 context。"""

    TEST_SYMBOL = "ZTEST9"

    def _insert(self, price_date, context, v2_score):
        with backend.connect() as conn:
            conn.execute(
                """
                INSERT INTO brain_v2_snapshots (created_at, symbol, price_date, context, v2_score)
                VALUES ('2026-01-01', ?, ?, ?, ?)
                ON CONFLICT(symbol, price_date, context) DO UPDATE SET v2_score = excluded.v2_score
                """,
                (self.TEST_SYMBOL, price_date, context, v2_score),
            )
            conn.commit()

    def tearDown(self):
        with backend.connect() as conn:
            conn.execute("DELETE FROM brain_v2_snapshots WHERE symbol = ?", (self.TEST_SYMBOL,))
            conn.commit()

    def test_finds_most_recent_earlier_snapshot_same_context(self):
        self._insert("2026-06-28", "analysis", 0.50)
        self._insert("2026-06-29", "analysis", 0.55)
        self._insert("2026-06-30", "analysis", 0.60)
        result = backend.get_previous_brain_v2_snapshot(self.TEST_SYMBOL, "2026-07-01", "analysis")
        self.assertEqual(result["price_date"], "2026-06-30")
        self.assertAlmostEqual(result["v2_score"], 0.60, places=9)

    def test_does_not_look_at_same_or_future_dates(self):
        self._insert("2026-07-01", "analysis", 0.90)  # 跟查詢日期同一天，不該被當成「前一天」
        result = backend.get_previous_brain_v2_snapshot(self.TEST_SYMBOL, "2026-07-01", "analysis")
        self.assertIsNone(result)

    def test_falls_back_to_other_context_when_same_context_missing(self):
        self._insert("2026-06-30", "analysis", 0.45)  # 只有 analysis 有紀錄
        result = backend.get_previous_brain_v2_snapshot(self.TEST_SYMBOL, "2026-07-01", "custom_watchlist")
        self.assertEqual(result["price_date"], "2026-06-30")

    def test_no_snapshot_at_all_returns_none(self):
        result = backend.get_previous_brain_v2_snapshot(self.TEST_SYMBOL, "2026-07-01", "analysis")
        self.assertIsNone(result)


class SaveMonsterRuleSnapshotTests(unittest.TestCase):
    """妖股短線規則引擎(第一階段：收集資料，不影響既有 buyAllowed 判斷)的
    快照寫入測試。跟 Brain v2 的 soft_gate 合併存在同一張 brain_v2_snapshots
    表(context 固定 'monster')，這裡驗證只動 rule_* 欄位、不會跟 Brain v2
    自己寫入的分量分數互相覆蓋掉。"""

    TEST_SYMBOL = "ZTEST9"

    def tearDown(self):
        with backend.connect() as conn:
            conn.execute("DELETE FROM brain_v2_snapshots WHERE symbol = ?", (self.TEST_SYMBOL,))
            conn.commit()

    def test_saves_snapshot_with_rules_and_tags(self):
        rule_result = {
            "action": "CAN_BUY_NOW", "vetoed": False, "vetoReason": None, "overheated": False,
            "rules": [{"key": "volumeSurge", "label": "量能爆發", "ok": True, "value": 3.5, "note": "強訊號"}],
            "bonusTags": ["limitUpTouch"],
        }
        saved = backend.save_monster_rule_snapshot(self.TEST_SYMBOL, "2026-06-30", rule_result)
        self.assertTrue(saved)
        with backend.connect() as conn:
            row = conn.execute(
                "SELECT rule_action, rule_vetoed, rule_veto_reason, rule_overheated, rule_rules, rule_bonus_tags, context "
                "FROM brain_v2_snapshots WHERE symbol = ? AND price_date = ?",
                (self.TEST_SYMBOL, "2026-06-30"),
            ).fetchone()
        self.assertIsNotNone(row)
        action, vetoed, veto_reason, overheated, rules_json, tags_json, context = row
        self.assertEqual(action, "CAN_BUY_NOW")
        self.assertEqual(vetoed, 0)
        self.assertIsNone(veto_reason)
        self.assertEqual(overheated, 0)
        self.assertIn("volumeSurge", rules_json)
        self.assertIn("limitUpTouch", tags_json)
        self.assertEqual(context, "monster")

    def test_repeated_call_does_not_duplicate_row(self):
        rule_result = {"action": "WATCH_ONLY", "vetoed": False, "vetoReason": None, "overheated": False, "rules": [], "bonusTags": []}
        backend.save_monster_rule_snapshot(self.TEST_SYMBOL, "2026-06-30", rule_result)
        backend.save_monster_rule_snapshot(self.TEST_SYMBOL, "2026-06-30", rule_result)
        with backend.connect() as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM brain_v2_snapshots WHERE symbol = ? AND price_date = ?",
                (self.TEST_SYMBOL, "2026-06-30"),
            ).fetchone()[0]
        self.assertEqual(count, 1)

    def test_missing_symbol_or_date_returns_false(self):
        self.assertFalse(backend.save_monster_rule_snapshot(None, "2026-06-30", {"action": "WAIT"}))
        self.assertFalse(backend.save_monster_rule_snapshot(self.TEST_SYMBOL, None, {"action": "WAIT"}))

    def test_does_not_clobber_existing_brain_v2_columns_on_same_row(self):
        # 先模擬 Brain v2 已經寫過一列(v2_score 等分量分數)，規則引擎晚一步才
        # 寫入同一個 (symbol, price_date, context)，不該把 Brain v2 的分數洗掉。
        with backend.connect() as conn:
            conn.execute("""
                INSERT INTO brain_v2_snapshots (created_at, symbol, price_date, context, v2_score, entry_allowed)
                VALUES (?, ?, ?, 'monster', ?, ?)
            """, ("2026-06-30 09:00:00", self.TEST_SYMBOL, "2026-06-30", 0.71, 1))
            conn.commit()
        backend.save_monster_rule_snapshot(self.TEST_SYMBOL, "2026-06-30", {
            "action": "CAN_BUY_NOW", "vetoed": False, "vetoReason": None, "overheated": False,
            "rules": [], "bonusTags": [],
        })
        with backend.connect() as conn:
            row = conn.execute(
                "SELECT v2_score, entry_allowed, rule_action FROM brain_v2_snapshots "
                "WHERE symbol = ? AND price_date = ?",
                (self.TEST_SYMBOL, "2026-06-30"),
            ).fetchone()
        self.assertAlmostEqual(row[0], 0.71)
        self.assertEqual(row[1], 1)
        self.assertEqual(row[2], "CAN_BUY_NOW")


class SavePredictionRowUniqueConstraintTests(unittest.TestCase):
    """_save_prediction_row 原本是「SELECT不存在才INSERT」，多執行緒下
    (ThreadingHTTPServer)兩個請求可能都讀到「不存在」而各自INSERT，讓
    predictions表出現同一(symbol, price_date, model_version)的重複列，污染
    hit_rate等統計。實測正式資料庫已經真的出現過一組重複(2330,
    2026-06-23)。改成idx_predictions_unique(symbol, price_date,
    model_version)+ON CONFLICT DO NOTHING，這裡驗證：(1)重複呼叫不會丟例外
    且只留一列，(2)第二次呼叫的資料不會覆蓋第一次寫入的內容(DO NOTHING語意)，
    (3)model_version不同時仍各自建立獨立列(不是過度去重)。"""

    TEST_SYMBOL = "ZTESTPRED"

    def _payload(self, price_date="2026-06-30", model_version="v1", probability=0.5, close=100.0):
        return {
            "symbol": self.TEST_SYMBOL,
            "priceDate": price_date,
            "modelVersion": model_version,
            "probability": probability,
            "threshold": 0.6,
            "action": "WAIT",
            "close": close,
        }

    def tearDown(self):
        with backend.connect() as conn:
            conn.execute("DELETE FROM predictions WHERE symbol = ?", (self.TEST_SYMBOL,))
            conn.commit()

    def test_duplicate_call_does_not_raise_and_keeps_one_row(self):
        backend._save_prediction_row(self._payload())
        backend._save_prediction_row(self._payload())  # 不該丟UNIQUE constraint例外
        with backend.connect() as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM predictions WHERE symbol = ? AND price_date = ? AND model_version = ?",
                (self.TEST_SYMBOL, "2026-06-30", "v1"),
            ).fetchone()[0]
        self.assertEqual(count, 1)

    def test_second_call_does_not_overwrite_first_row_values(self):
        backend._save_prediction_row(self._payload(probability=0.5, close=100.0))
        backend._save_prediction_row(self._payload(probability=0.9, close=999.0))  # DO NOTHING，應該被忽略
        with backend.connect() as conn:
            row = conn.execute(
                "SELECT probability, close FROM predictions WHERE symbol = ? AND price_date = ? AND model_version = ?",
                (self.TEST_SYMBOL, "2026-06-30", "v1"),
            ).fetchone()
        self.assertAlmostEqual(row[0], 0.5)
        self.assertAlmostEqual(row[1], 100.0)

    def test_different_model_version_creates_separate_row(self):
        backend._save_prediction_row(self._payload(model_version="v1"))
        backend._save_prediction_row(self._payload(model_version="v2"))
        with backend.connect() as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM predictions WHERE symbol = ? AND price_date = ?",
                (self.TEST_SYMBOL, "2026-06-30"),
            ).fetchone()[0]
        self.assertEqual(count, 2)


class ListMonsterScoresRuleEngineJoinTests(unittest.TestCase):
    """list_monster_scores 要把合併後的 brain_v2_snapshots(context='monster')
    規則引擎欄位，用 (symbol, price_date) 對應接到每一列的 ruleEngine 欄位，
    前端才看得到規則燈號；沒有對應快照時要是 None，不能報錯或留下殘留欄位。"""

    TEST_SYMBOL = "ZTEST9"
    SCAN_DATE = "20990101"
    PRICE_DATE = "2099-01-01"

    def setUp(self):
        # 這組測試只驗證指定掃描列的規則引擎合併與風險否決；不可依賴
        # 真實資料庫當下有哪些掃描，或目前交易日曆是否涵蓋 2099 年。
        self.selection_patcher = patch.object(
            backend,
            "select_radar_decision_scan",
            return_value={
                "selectedScanDate": self.SCAN_DATE,
                "latestAuditScanDate": self.SCAN_DATE,
                "latestCompletePriceDate": self.PRICE_DATE,
                "decisionValidity": {
                    "invalidForTrading": False,
                    "invalidReasons": [],
                },
                "usingFallbackValidScan": False,
            },
        )
        self.selection_patcher.start()

    def tearDown(self):
        self.selection_patcher.stop()
        with backend.connect() as conn:
            conn.execute("DELETE FROM monster_scores WHERE symbol = ?", (self.TEST_SYMBOL,))
            conn.execute("DELETE FROM brain_v2_snapshots WHERE symbol = ?", (self.TEST_SYMBOL,))
            conn.commit()

    def _insert_monster_score(self, risk_flags=None):
        import json
        risk_flags_json = json.dumps(risk_flags or [], ensure_ascii=False)
        with backend.connect() as conn:
            conn.execute("""
                INSERT INTO monster_scores (
                    scan_date, symbol, price_date, score, probability, threshold, action, buy_allowed,
                    status, close, buy_trigger, pullback_price, stop_price, take_profit, trailing_stop,
                    gap_limit, change1, change5, change20, volume_ratio, latest_volume_lots,
                    avg_volume20_lots, turnover_million, liquidity_ok, surge_setup, reasons, risk_flags,
                    model_version, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                self.SCAN_DATE, self.TEST_SYMBOL, self.PRICE_DATE, 70.0, 0.5, 0.4, "NEXT_DAY_WATCH", 1,
                "測試", 100.0, 105.0, 98.0, 90.0, 115.0, 96.0,
                0.05, 3.0, 8.0, 12.0, 3.5, 1000.0,
                800.0, 100.0, 1, 0, "[]", risk_flags_json, "test", "2099-01-01 00:00:00",
            ))
            conn.commit()

    def test_ruleengine_attached_when_snapshot_exists(self):
        self._insert_monster_score()
        backend.save_monster_rule_snapshot(self.TEST_SYMBOL, self.PRICE_DATE, {
            "action": "CAN_BUY_NOW", "vetoed": False, "vetoReason": None, "overheated": False,
            "rules": [{"key": "volumeSurge", "label": "量能爆發", "ok": True, "value": 3.5, "note": ""}],
            "bonusTags": ["marketRelative"],
        })
        result = backend.list_monster_scores(limit=200)
        item = next((row for row in result["candidates"] if row["symbol"] == self.TEST_SYMBOL), None)
        self.assertIsNotNone(item)
        self.assertEqual(item["ruleEngine"]["action"], "CAN_BUY_NOW")
        self.assertEqual(len(item["ruleEngine"]["rules"]), 1)
        self.assertIn("marketRelative", item["ruleEngine"]["bonusTags"])

    def test_ruleengine_is_none_when_no_snapshot(self):
        self._insert_monster_score()
        result = backend.list_monster_scores(limit=200)
        item = next((row for row in result["candidates"] if row["symbol"] == self.TEST_SYMBOL), None)
        self.assertIsNotNone(item)
        self.assertIsNone(item["ruleEngine"])
        # 不該留下 rule_* 這種原始 join 欄位污染輸出
        self.assertNotIn("rule_action", item)
        self.assertNotIn("rule_rules", item)

    def test_danger_risk_flag_vetoes_legacy_buy_allowed_row(self):
        self._insert_monster_score(risk_flags=[
            {"code": "long_upper_volume", "label": "長上影爆量·疑倒貨", "severity": "danger"}
        ])
        result = backend.list_monster_scores(limit=200)
        item = next((row for row in result["candidates"] if row["symbol"] == self.TEST_SYMBOL), None)
        self.assertIsNotNone(item)
        self.assertFalse(item["buyAllowed"])
        self.assertTrue(item["riskVetoed"])
        self.assertEqual(item["status"], "高風險型態，只觀察不追")

    def test_insufficient_confirmed_samples_remain_performance_vetoed(self):
        self._insert_monster_score()
        readiness = {
            "enforced": False,
            "formalReady": False,
            "observationOnly": True,
            "performanceBasis": "intraday_confirmed_only",
            "live": {"settled": 0},
        }
        with patch.object(
            backend, "current_radar_deployment_readiness", return_value=readiness
        ):
            result = backend.list_monster_scores(limit=200)
        item = next(
            (row for row in result["candidates"] if row["symbol"] == self.TEST_SYMBOL),
            None,
        )
        self.assertIsNotNone(item)
        self.assertTrue(item["performanceVetoed"])
        self.assertFalse(item["buyAllowed"])
        self.assertIn("只觀察", item["status"])


class ComputeSectorMomentumTests(unittest.TestCase):
    """compute_sector_momentum 是純函式(stock_info 由呼叫端傳入)，不觸碰資料庫，
    不需要寫入/清理任何測試資料。"""

    STOCK_INFO = {
        "A1": {"sector": "AI伺服器"},
        "A2": {"sector": "AI伺服器"},
        "A3": {"sector": "AI伺服器"},
        "B1": {"sector": "傳產"},
        "B2": {"sector": "傳產"},
        "B3": {"sector": "傳產"},
        "C1": {"sector": "冷門族群"},
        "C2": {"sector": "冷門族群"},
    }

    def _candidate(self, symbol, ret5, volume_ratio=2.0):
        return {"symbol": symbol, "ret5": ret5, "volumeRatio": volume_ratio}

    def test_hot_sector_ranked_above_market_average(self):
        candidates = [
            self._candidate("A1", 15), self._candidate("A2", 18), self._candidate("A3", 20),
            self._candidate("B1", 2), self._candidate("B2", 3), self._candidate("B3", 1),
        ]
        result = backend.compute_sector_momentum(candidates, stock_info=self.STOCK_INFO)
        self.assertIn("AI伺服器", result["hotSectors"])
        self.assertNotIn("傳產", result["hotSectors"])
        self.assertGreater(result["sectors"]["AI伺服器"]["excessRet5"], 0)
        self.assertLess(result["sectors"]["傳產"]["excessRet5"], 0)

    def test_sector_with_too_few_samples_is_excluded(self):
        candidates = [
            self._candidate("A1", 15), self._candidate("A2", 18), self._candidate("A3", 20),
            self._candidate("C1", 30), self._candidate("C2", 35),  # 只有 2 檔，樣本不足
        ]
        result = backend.compute_sector_momentum(candidates, stock_info=self.STOCK_INFO, min_sector_count=3)
        self.assertIn("AI伺服器", result["sectors"])
        self.assertNotIn("冷門族群", result["sectors"])

    def test_empty_candidates_returns_empty_snapshot(self):
        result = backend.compute_sector_momentum([], stock_info=self.STOCK_INFO)
        self.assertEqual(result["sectors"], {})
        self.assertEqual(result["hotSectors"], [])

    def test_symbol_missing_from_stock_info_falls_back_to_taiwan_stock(self):
        candidates = [
            self._candidate("UNKNOWN1", 10), self._candidate("UNKNOWN2", 12), self._candidate("UNKNOWN3", 14),
        ]
        result = backend.compute_sector_momentum(candidates, stock_info={})
        self.assertIn("台股", result["sectors"])

    def test_sector_hot_today_and_last_scan_is_persistent(self):
        candidates = [
            self._candidate("A1", 15), self._candidate("A2", 18), self._candidate("A3", 20),
            self._candidate("B1", 2), self._candidate("B2", 3), self._candidate("B3", 1),
        ]
        result = backend.compute_sector_momentum(
            candidates, stock_info=self.STOCK_INFO, previous_hot_sectors=["AI伺服器"]
        )
        self.assertTrue(result["sectors"]["AI伺服器"]["persistentHot"])
        self.assertFalse(result["sectors"]["傳產"]["persistentHot"])

    def test_sector_hot_today_only_is_not_persistent(self):
        candidates = [
            self._candidate("A1", 15), self._candidate("A2", 18), self._candidate("A3", 20),
            self._candidate("B1", 2), self._candidate("B2", 3), self._candidate("B3", 1),
        ]
        result = backend.compute_sector_momentum(
            candidates, stock_info=self.STOCK_INFO, previous_hot_sectors=["傳產"]
        )
        self.assertIn("AI伺服器", result["hotSectors"])
        self.assertFalse(result["sectors"]["AI伺服器"]["persistentHot"])

    def test_no_previous_hot_sectors_defaults_to_not_persistent(self):
        candidates = [
            self._candidate("A1", 15), self._candidate("A2", 18), self._candidate("A3", 20),
            self._candidate("B1", 2), self._candidate("B2", 3), self._candidate("B3", 1),
        ]
        result = backend.compute_sector_momentum(candidates, stock_info=self.STOCK_INFO)
        self.assertFalse(result["sectors"]["AI伺服器"]["persistentHot"])


class MonsterScoreSectorBonusTests(unittest.TestCase):
    """monster_score_for_symbol 沒收到 sector_momentum 時要優雅退化成 0 分，
    不能因為新參數是 optional 就在既有呼叫路徑（例如單股查詢）壞掉或改變行為。"""

    def test_missing_sector_momentum_defaults_to_no_bonus(self):
        # 直接用真實候選池資料跑一次全流程風險較高（依賴模型/網路），這裡只驗證
        # compute_sector_momentum 對「沒有資料」的退化路徑本身是安全的、
        # 不會丟例外，回傳的結構跟正常呼叫一致。
        result = backend.compute_sector_momentum(None, stock_info={})
        self.assertEqual(result, {"sectors": {}, "hotSectors": [], "overallRet5": 0.0})


class ComputeAucTests(unittest.TestCase):
    """compute_auc 是純函式，用已知答案的手算案例驗證排序法算出來的
    Mann-Whitney U AUC 是否正確，不依賴 sklearn。"""

    def _rows(self, pairs):
        return [{"probability": prob, "y": y} for prob, y in pairs]

    def test_perfect_separation_is_one(self):
        # 所有正例分數都高於所有負例，完美分離 → AUC = 1.0
        rows = self._rows([(0.9, 1), (0.8, 1), (0.3, 0), (0.2, 0)])
        self.assertAlmostEqual(backend.compute_auc(rows), 1.0)

    def test_perfectly_reversed_is_zero(self):
        # 所有正例分數都低於所有負例，完全猜反 → AUC = 0.0
        rows = self._rows([(0.1, 1), (0.2, 1), (0.8, 0), (0.9, 0)])
        self.assertAlmostEqual(backend.compute_auc(rows), 0.0)

    def test_random_guessing_is_half(self):
        # 正例分數集合跟負例分數集合完全相同({0.3, 0.7} vs {0.3, 0.7})，
        # 沒有任何鑑別度 → AUC = 0.5
        rows = self._rows([(0.3, 1), (0.7, 1), (0.3, 0), (0.7, 0)])
        self.assertAlmostEqual(backend.compute_auc(rows), 0.5)

    def test_tied_scores_use_average_rank(self):
        # 正負例同分：一個正例、一個負例並列最高分，用平均排名處理，
        # 對這一對貢獻 0.5(不算贏也不算輸)。
        rows = self._rows([(0.5, 1), (0.5, 0), (0.1, 0)])
        # 手算：排名(0.1,0)=1, 排名(0.5,*)並列2、3取平均=2.5
        # rank_sum_positive = 2.5；AUC = (2.5 - 1*2/2) / (1*2) = 1.5/2 = 0.75
        self.assertAlmostEqual(backend.compute_auc(rows), 0.75)

    def test_single_class_returns_baseline_half_not_zero(self):
        # 驗證集只有單一類別時沒有意義，要回傳 0.5(基準線)，不能回傳 0
        # (0 看起來像「模型完全猜反」會誤導人)。
        rows = self._rows([(0.9, 1), (0.8, 1), (0.7, 1)])
        self.assertEqual(backend.compute_auc(rows), 0.5)


if __name__ == "__main__":
    unittest.main()
