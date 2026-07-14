"""
sector_rel_strength(FEATURE_NAMES[19]) 訓練/推論偏斜(#175)的調查記錄+回歸測試。

調查結果：build_training_samples() 用 build_sector_strength(symbols) 算出
逐歷史日期的族群相對強度餵給 build_features_for_rows()；但 predict_symbol()
(正式模型推論、也是妖股掃描每一檔的實際評分入口)從來沒有計算/傳入
sector_strength，導致這個特徵位置在 build_features_for_rows() 內 fallback 到
market["stock_vs_taiex_20"] —— 跟 FEATURE_NAMES[18](stock_vs_taiex_20 本身)
完全重複，是一個真實存在的 train/serve skew。

已實際做過修復實驗(用 liquid_monster_universe() 當母體、predict_symbol()
呼叫同一套 build_sector_strength() 算法)並用 backtest_top10.py 驗證：
2026-07-04 backtest 顯示「修好」反而讓 model_prob 排名法的 OOS 表現全面
變差(avgNetPct 1.644%→1.329%、hit10Rate 33.31%→32.15%、winRate
51.54%→50.23%、profitFactor 1.364→1.285，1300筆OOS交易，非雜訊等級的差距)。
研判可能原因：serving 端只能用「今天」的 liquid_monster_universe(681檔)
回算，跟訓練當下實際使用的訓練池(這次training run是714檔、且逐次訓練組成
會變動)在母體組成/日期覆蓋上有落差，即使統計定義相同，分布/尺度跟模型
means/stdevs[19] 的校準基準對不齊，比「維持跟 stock_vs_taiex_20 重複」的
現況更差。**已決定不修這個skew、維持現況**，跟 volume_ratio 修復
(project_next_tier_audit_2026_07_03.md)是同一種「邏輯正確不等於backtest
表現更好」案例。

若未來要重新嘗試，方向可以是：用跟該次訓練實際相同的symbols母體(而非
liquid_monster_universe())算sector_strength、或重新拿掉這個特徵改用其他
替代統計，而不是原地換一個母體來源——但都要重新走一次backtest_top10.py
驗證，不能只憑邏輯正確性套用。

這裡只留下 build_features_for_rows() 既有 fallback 行為(sector_strength
給空dict時退回stock_vs_taiex_20)的回歸測試——這個 fallback 現在是「已驗證
決定維持」的正式行為，不是待修的bug，未來不能被誤刪。

執行方式：
  python -m unittest tests.test_sector_strength_scoring -v
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ml_backend import FEATURE_NAMES, backend

IDX_SECTOR = FEATURE_NAMES.index("sector_rel_strength")
IDX_TAIEX20 = FEATURE_NAMES.index("stock_vs_taiex_20")


def make_flat_rows(symbol="TEST9903", total=150, base_close=100.0):
    rows = []
    for i in range(total):
        rows.append({
            "symbol": symbol,
            "date": f"2026-{(i // 28) + 1:02d}-{(i % 28) + 1:02d}",
            "open": base_close, "high": base_close * 1.01, "low": base_close * 0.99,
            "close": base_close, "volume": 100_000,
        })
    return rows


class SectorStrengthFeatureFallbackTests(unittest.TestCase):
    def test_empty_sector_strength_falls_back_to_stock_vs_taiex(self):
        rows = make_flat_rows()
        features = backend.build_features_for_rows(rows, market_rows={}, sector_strength={})
        last = next(item for item in features if item["index"] == len(rows) - 1)
        self.assertEqual(last["x"][IDX_SECTOR], last["x"][IDX_TAIEX20])

    def test_populated_sector_strength_overrides_fallback(self):
        rows = make_flat_rows()
        last_date = rows[-1]["date"]
        sector_strength = {(rows[-1]["symbol"], last_date): 0.42}
        features = backend.build_features_for_rows(rows, market_rows={}, sector_strength=sector_strength)
        last = next(item for item in features if item["index"] == len(rows) - 1)
        self.assertAlmostEqual(last["x"][IDX_SECTOR], 0.42, places=6)
        self.assertNotEqual(last["x"][IDX_SECTOR], last["x"][IDX_TAIEX20])


if __name__ == "__main__":
    unittest.main()
