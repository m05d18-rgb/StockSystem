"""
重訓品質閘門(evaluate_model_gate)與殭屍預測稽核(check_prediction_settlement)
的回歸測試。

閘門：train_model 覆蓋 model.pkl 前跟舊模型比對，AUC太低/跌太多/樣本縮水
就沿用舊版；特徵schema變更必須直接接受(舊模型在新程式碼下無法服役)；
連續拒絕只供稽核，不得繞過品質閘門。

殭屍稽核：price_date 早於25日曆天但 hit 仍 NULL 的預測不在戰績分母裡，
新增惡化超過基準20筆才警示(首次上線把存量記為基準避免假警報洪水)。

隔離鐵律：殭屍稽核測試 patch 基準 meta key 成 __test_*__、假預測列用
ZTEST 前綴 symbol + __test__ model_version 並在 teardown 清理；閘門狀態
測試 mock backend.read_model_gate_state，絕不寫正式的 model_gate_state
(寫進去會讓隔天正式完整性檢查發假 LINE 警示)。

執行方式：
  python -m unittest tests.test_model_gate -v
"""
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import data_integrity_check
import ml_backend
from ml_backend import (
    MODEL_GATE_MAX_AUC_RELATIVE_DROP,
    MODEL_GATE_MIN_AUC,
    backend,
    evaluate_model_gate,
)

FEATURES_A = ["f1", "f2", "f3"]
FEATURES_B = ["f1", "f2", "f3", "f4"]


def _old_model(auc=0.65, samples=100000, features=None):
    return {"metrics": {"auc": auc}, "samples": samples, "feature_names": features or FEATURES_A}


class EvaluateModelGateTests(unittest.TestCase):
    def test_no_old_model_accepts(self):
        accept, reason, forced = evaluate_model_gate(None, {"auc": 0.40}, 100, FEATURES_A)
        self.assertTrue(accept)
        self.assertIn("無舊模型", reason)
        self.assertFalse(forced, "沒有舊模型可比較是正常放行，不是強制放行")

    def test_feature_schema_change_always_accepts(self):
        # schema 變更時舊 model.pkl 在新程式碼下 load_model 會拒載，
        # 擋下新模型=系統無模型可用，比品質稍差嚴重得多
        accept, reason, forced = evaluate_model_gate(
            _old_model(auc=0.99, features=FEATURES_A), {"auc": 0.10}, 10, FEATURES_B
        )
        self.assertTrue(accept)
        self.assertIn("schema", reason)
        self.assertFalse(forced)

    def test_low_auc_rejected(self):
        accept, reason, forced = evaluate_model_gate(
            _old_model(auc=0.65), {"auc": MODEL_GATE_MIN_AUC - 0.01}, 100000, FEATURES_A
        )
        self.assertFalse(accept)
        self.assertIn("低於下限", reason)
        self.assertFalse(forced)

    def test_auc_drop_beyond_tolerance_rejected(self):
        accept, reason, forced = evaluate_model_gate(_old_model(auc=0.70), {"auc": 0.58}, 100000, FEATURES_A)
        self.assertFalse(accept)
        self.assertIn("跌幅", reason)
        self.assertFalse(forced)

    def test_auc_relative_drop_beyond_tolerance_rejected_even_when_absolute_drop_passes(self):
        # old=0.65, new=0.551：絕對差0.099(<0.10門檻，絕對值檢查會放行)，
        # 但相對降幅15.23%(>15%門檻)，且0.551仍>=MODEL_GATE_MIN_AUC(0.55)不會被
        # 下限擋下——只有新加的相對降幅檢查能抓到這種案例。
        accept, reason, forced = evaluate_model_gate(
            _old_model(auc=0.65), {"auc": 0.551}, 100000, FEATURES_A
        )
        self.assertFalse(accept, "絕對差0.099沒超過0.10門檻，但相對降幅15.23%超過15%門檻，該被擋")
        self.assertIn("相對降幅", reason)
        self.assertFalse(forced)

    def test_auc_relative_drop_within_tolerance_for_high_baseline_still_accepted(self):
        # 高基準模型：old=0.90 -> new=0.81，絕對差0.09(通過)，相對降幅10%(<15%通過)，
        # 兩個檢查都該放行，不能因為新加的相對值檢查誤傷原本會通過的案例。
        accept, reason, forced = evaluate_model_gate(
            _old_model(auc=0.90, samples=100000), {"auc": 0.81}, 98000, FEATURES_A
        )
        self.assertTrue(accept)
        self.assertFalse(forced)

    def test_sample_shrink_beyond_tolerance_rejected(self):
        accept, reason, forced = evaluate_model_gate(
            _old_model(auc=0.65, samples=100000), {"auc": 0.65}, 60000, FEATURES_A
        )
        self.assertFalse(accept)
        self.assertIn("縮水", reason)
        self.assertFalse(forced)

    def test_healthy_model_accepted(self):
        accept, reason, forced = evaluate_model_gate(
            _old_model(auc=0.65, samples=100000), {"auc": 0.64}, 98000, FEATURES_A
        )
        self.assertTrue(accept)
        self.assertFalse(forced)

    def test_consecutive_rejects_never_bypass_quality_gate(self):
        accept, reason, forced = evaluate_model_gate(
            _old_model(auc=0.70), {"auc": 0.30}, 100, FEATURES_A,
            consecutive_rejects=99,
        )
        self.assertFalse(accept)
        self.assertIn("低於下限", reason)
        self.assertFalse(forced)

    def test_read_model_gate_state_returns_ints_and_strings(self):
        state = backend.read_model_gate_state()
        self.assertIsInstance(state["consecutiveRejects"], int)
        self.assertIsInstance(state["lastRejectedAt"], str)
        self.assertIsInstance(state["lastAcceptedWasForced"], bool)


class CheckModelGateTests(unittest.TestCase):
    """check_model_gate 只在「今天有拒絕」時列為問題。mock 狀態讀取，
    不寫正式 model_gate_state。"""

    def test_no_rejection_is_ok(self):
        with patch.object(data_integrity_check.backend, "read_model_gate_state", return_value={
            "consecutiveRejects": 0, "lastRejectedAt": "", "lastRejectReason": "", "lastAcceptedAt": "",
        }):
            result = data_integrity_check.check_model_gate()
        self.assertTrue(result["ok"])

    def test_rejection_today_is_problem(self):
        today = ml_backend.today_key()
        with patch.object(data_integrity_check.backend, "read_model_gate_state", return_value={
            "consecutiveRejects": 1, "lastRejectedAt": f"{today} 14:45:00",
            "lastRejectReason": "新模型AUC 0.500 低於下限 0.55", "lastAcceptedAt": "",
        }):
            result = data_integrity_check.check_model_gate()
        self.assertFalse(result["ok"])
        self.assertIn("品質閘門拒絕", result["issue"])

    def test_old_rejection_not_todays_problem(self):
        with patch.object(data_integrity_check.backend, "read_model_gate_state", return_value={
            "consecutiveRejects": 1, "lastRejectedAt": "2020-01-01 14:45:00",
            "lastRejectReason": "舊事", "lastAcceptedAt": "",
        }):
            result = data_integrity_check.check_model_gate()
        self.assertTrue(result["ok"])


class PublicModelGatePropagationTests(unittest.TestCase):
    """2026-07-04 低優先稽核修復：閘門擋下新模型時 train_model 回傳的 dict 帶
    gateRejected/gateReason(且 model.pkl 上仍是舊模型)，public_model 要把這兩個
    鍵透傳出去，否則 full_daily_update 寫進 latest.json 的 result.model 看不出
    這次重訓其實被擋下。public_model 只讀 meta/model_path，不寫任何狀態。"""

    def test_rejected_model_propagates_gate_flags(self):
        rejected = {
            "version": "v-new", "trained_at": "2026-07-04 12:00:00", "metrics": {"auc": 0.52},
            "gateRejected": True, "gateReason": "新模型AUC 0.520 相對降幅超過允許值",
        }
        public = backend.public_model(rejected)
        self.assertTrue(public["gateRejected"])
        self.assertIn("相對降幅", public["gateReason"])

    def test_accepted_model_defaults_gate_flags_to_false_none(self):
        accepted = {"version": "v-new", "trained_at": "2026-07-04 12:00:00", "metrics": {"auc": 0.66}}
        public = backend.public_model(accepted)
        self.assertFalse(public["gateRejected"])
        self.assertIsNone(public["gateReason"])


TEST_BASELINE_KEY = "__test_prediction_overdue_baseline__"
TEST_MODEL_VERSION = "__test_overdue_audit__"


class PredictionSettlementTests(unittest.TestCase):
    def setUp(self):
        self._patcher = patch.object(
            data_integrity_check, "PREDICTION_OVERDUE_BASELINE_KEY", TEST_BASELINE_KEY
        )
        self._patcher.start()
        self._cleanup()

    def tearDown(self):
        self._cleanup()
        self._patcher.stop()

    def _cleanup(self):
        with backend.connect() as conn:
            conn.execute("DELETE FROM model_meta WHERE key = ?", (TEST_BASELINE_KEY,))
            conn.execute("DELETE FROM predictions WHERE model_version = ?", (TEST_MODEL_VERSION,))
            conn.execute("DELETE FROM prices WHERE symbol LIKE 'ZTEST%'")
            conn.commit()

    def _insert_overdue(self, count, symbol_prefix="ZTEST", fresh_price=True):
        # 2026-07-04 稽核修復後：prediction_settlement_health 會查每個逾期
        # 股票在 prices 表的最新日期，本身也停更超過cutoff就歸類到
        # structurallyUnsettleable、不算真正的overdue——「真的逾期」的測試案例
        # 必須配一筆「今天」的新鮮 prices 列，模擬「股票資料還在正常更新，
        # 只是這筆結算沒跟上」的真實管線故障情境；stale symbol 才不給新鮮價。
        with backend.connect() as conn:
            for i in range(count):
                symbol = f"{symbol_prefix}{i:04d}"
                conn.execute(
                    "INSERT INTO predictions (created_at, symbol, price_date, model_version, "
                    "probability, threshold, action, target_horizon, target_return, close) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    ("2020-01-01 00:00:00", symbol, "2020-01-01", TEST_MODEL_VERSION,
                     0.5, 0.45, "BUY_CANDIDATE", 10, 0.10, 100.0),
                )
            conn.commit()
        if fresh_price:
            for i in range(count):
                backend.upsert_price_rows([{
                    "symbol": f"{symbol_prefix}{i:04d}", "date": ml_backend.today_key(),
                    "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0, "volume": 1000,
                }])

    def test_baseline_absorbs_existing_overdue_then_alerts_on_new_growth(self):
        # 第一次跑：把現有存量(含正式資料的歷史遺留)記為基準，不警示
        first = data_integrity_check.check_prediction_settlement()
        self.assertTrue(first["ok"], "首次上線存量要被基準吸收，不能報假警")
        # 新增 25 筆逾期(超過門檻 20)：模擬結算管線故障開始惡化
        self._insert_overdue(25)
        second = data_integrity_check.check_prediction_settlement()
        self.assertFalse(second["ok"])
        self.assertEqual(second["newOverdue"], 25)
        self.assertIn("逾期未結算", second["issue"])
        self.assertTrue(any(t["symbol"].startswith("ZTEST") for t in second["topSymbols"]),
                        "問題最多的股票清單要能點名兇手")

    def test_small_growth_below_threshold_is_ok(self):
        data_integrity_check.check_prediction_settlement()  # 建基準
        self._insert_overdue(5)
        result = data_integrity_check.check_prediction_settlement()
        self.assertTrue(result["ok"], "低於門檻的小幅新增不警示(避免雜訊)")

    def test_baseline_ratchets_down_when_overdue_clears(self):
        self._insert_overdue(25)
        data_integrity_check.check_prediction_settlement()  # 基準含這25筆
        self._cleanup()  # 逾期被結算(模擬修復)
        self._patcher.start  # keep patch active; cleanup 只清資料
        result = data_integrity_check.check_prediction_settlement()
        self.assertTrue(result["ok"])
        # 基準下修後再惡化 25 筆要能再次警示
        self._insert_overdue(25)
        again = data_integrity_check.check_prediction_settlement()
        self.assertFalse(again["ok"], "基準棘輪下修後，對重新惡化要保持敏感")

    def _insert_symbol(self, symbol, pred_count, price_mode):
        # 顆粒化版本：單一 symbol 插 pred_count 筆逾期預測(price_date 各異以避開
        # idx_predictions_unique(symbol, price_date, model_version) 約束)，
        # 再依 price_mode 決定該股票在 prices 表的最新價：
        #   'fresh' = 今天(股票還在更新，逾期是結算管線的錯 → real overdue)
        #   'stale' = 遠早於 25 天 cutoff(曾有資料但長期停更 → structurally unsettleable
        #             的主流真實情境：下市/全額交割/長期停牌，走 latest < cutoff 分支)
        #   'none'  = prices 表完全沒這檔(從未有資料 → 走 latest is None 分支)
        with backend.connect() as conn:
            for i in range(pred_count):
                conn.execute(
                    "INSERT INTO predictions (created_at, symbol, price_date, model_version, "
                    "probability, threshold, action, target_horizon, target_return, close) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    ("2020-01-01 00:00:00", symbol, f"2020-01-{(i % 28) + 1:02d}",
                     TEST_MODEL_VERSION, 0.5, 0.45, "BUY_CANDIDATE", 10, 0.10, 100.0),
                )
            conn.commit()
        if price_mode == "none":
            return
        price_date = ml_backend.today_key() if price_mode == "fresh" else "2020-06-01"
        backend.upsert_price_rows([{
            "symbol": symbol, "date": price_date,
            "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0, "volume": 1000,
        }])

    def test_stale_price_symbol_treated_as_structurally_unsettleable(self):
        # 2026-07-04 對抗式稽核發現：原本兩個新測試只覆蓋 latest is None 分支，
        # 從沒測到 latest < cutoff——但「下市/停牌股曾有資料只是很久沒更新」才是
        # 結構性無法結算的主流真實情境，走的正是 latest < cutoff 這條。
        before = data_integrity_check.check_prediction_settlement()
        before_dead = before["structurallyUnsettleable"]
        self._insert_symbol("ZTESTSTALE01", 4, price_mode="stale")
        result = data_integrity_check.check_prediction_settlement()
        self.assertTrue(result["ok"], "有舊價格但早已停更的股票，殭屍預測不該被當成管線故障")
        self.assertEqual(result["newOverdue"], 0)
        self.assertEqual(result["structurallyUnsettleable"] - before_dead, 4,
                         "latest < cutoff 分支要正確歸類為 structurally unsettleable")
        self.assertFalse(
            any(t["symbol"] == "ZTESTSTALE01" for t in result["topSymbols"]),
            "停更股票不該出現在 topSymbols 榜單",
        )

    def test_top_symbols_ordered_by_count_desc_with_varying_counts(self):
        # 對抗式稽核發現：原測試每個 symbol 都只有 1 筆(count 全 tie)，
        # ORDER BY COUNT(*) DESC 排序沒有區分度、等於沒被驗證。這裡讓不同
        # 股票有不同筆數(fresh 讓它們算真正 overdue)，鎖住排序契約。
        self._insert_symbol("ZTESTORDA", 3, price_mode="fresh")
        self._insert_symbol("ZTESTORDB", 8, price_mode="fresh")
        self._insert_symbol("ZTESTORDC", 5, price_mode="fresh")
        result = data_integrity_check.check_prediction_settlement()
        ours = [t for t in result["topSymbols"] if t["symbol"].startswith("ZTESTORD")]
        # 三檔都應該進 top5(除非正式資料有更多筆數的股票擠掉)，至少驗證彼此順序
        counts_in_order = [t["count"] for t in ours]
        self.assertEqual(counts_in_order, sorted(counts_in_order, reverse=True),
                         "topSymbols 必須依 count 由大到小排序")
        by_symbol = {t["symbol"]: t["count"] for t in ours}
        self.assertEqual(by_symbol.get("ZTESTORDB"), 8)
        self.assertEqual(by_symbol.get("ZTESTORDC"), 5)
        self.assertEqual(by_symbol.get("ZTESTORDA"), 3)

    def test_eligible_and_overdue_rate_contract(self):
        # 對抗式稽核發現(high)：eligible/overdueRate 兩個對外欄位完全沒有斷言
        # 鎖住，SQL 被誤改也不會被測試抓到。這裡鎖住三條契約：
        #   (1) eligible 分母 = 所有 price_date<=cutoff 的預測(不分是否結算)，
        #       插 N 筆逾期(不論 fresh/stale)就該讓 eligible 精確 +N。
        #   (2) overdueRate 恆等於 round(overdue/eligible, 4)。
        #   (3) 分子(overdue)只窄化 real overdue，分母(eligible)不受停更股票排除影響。
        before = data_integrity_check.check_prediction_settlement()
        before_eligible = before["eligible"]
        self._insert_symbol("ZTESTELIGA", 6, price_mode="fresh")   # 算 overdue
        self._insert_symbol("ZTESTELIGB", 4, price_mode="stale")   # 算 structurally
        result = data_integrity_check.check_prediction_settlement()
        self.assertEqual(result["eligible"] - before_eligible, 10,
                         "eligible 分母要含所有 price_date<=cutoff 的預測，含停更股票的")
        expected_rate = round(result["overdue"] / result["eligible"], 4) if result["eligible"] else 0.0
        self.assertEqual(result["overdueRate"], expected_rate,
                         "overdueRate 必須恆等於 overdue/eligible")

    def test_structurally_unsettleable_symbol_excluded_from_overdue_and_top_symbols(self):
        # 2026-07-04 稽核修復：下市/全額交割/長期停牌股，prices 資料本身已經
        # 停更，不給新鮮價(fresh_price=False)模擬——這類殭屍預測不該被當成
        # 真正的結算管線故障，也不該佔滿topSymbols稀釋真正故障的可見度。
        before = data_integrity_check.check_prediction_settlement()
        before_dead = before["structurallyUnsettleable"]
        self._insert_overdue(3, symbol_prefix="ZTESTDEAD", fresh_price=False)
        result = data_integrity_check.check_prediction_settlement()
        self.assertTrue(result["ok"], "結構性無法結算不該被當成真正的管線故障警示")
        self.assertEqual(result["newOverdue"], 0, "停更股票的殭屍預測不該被算進真正逾期")
        self.assertEqual(result["structurallyUnsettleable"] - before_dead, 3)
        self.assertFalse(
            any(t["symbol"].startswith("ZTESTDEAD") for t in result["topSymbols"]),
            "停更股票不該佔滿topSymbols榜單、稀釋真正故障的可見度",
        )

    def test_mixed_real_and_structurally_unsettleable_only_real_counts_as_overdue(self):
        before = data_integrity_check.check_prediction_settlement()
        before_dead = before["structurallyUnsettleable"]
        self._insert_overdue(25, symbol_prefix="ZTEST", fresh_price=True)
        self._insert_overdue(10, symbol_prefix="ZTESTDEAD", fresh_price=False)
        result = data_integrity_check.check_prediction_settlement()
        self.assertEqual(result["newOverdue"], 25, "只有股票本身還在更新的才算進真正逾期")
        self.assertEqual(result["structurallyUnsettleable"] - before_dead, 10)
        self.assertFalse(result["ok"], "真正逾期25筆仍要超過門檻警示，不能被停更股票的計入蓋過")


if __name__ == "__main__":
    unittest.main()
