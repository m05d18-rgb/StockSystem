"""
build_features_for_rows() 的 volume_ratio 特徵回歸測試，對應這次修的 bug：

avg_volume 均量分母原本用 self.sma(volumes, 20, index)，這個窗口的右端點
包含當日本身，導致當日爆量會把自己那根K的20日均量墊高，系統性稀釋
volume_ratio(真實5倍量爆發只會算出約4.17倍)。跟同一份檔案裡
short_term_target() 的量縮判斷(明確排除當日：range(max(0, j-20), j))
定義不一致。修法：avg_volume 改用 self.sma(volumes, 20, index - 1)，
分母只看訊號日之前的20天。

全部用合成K線，不碰資料庫/網路(market_rows 傳空字典，MarketContext 對
空字典的所有查詢都會安全回傳 0.0/None，不會拋例外)。

執行方式：
  python -m unittest tests.test_volume_ratio_feature -v
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ml_backend import FEATURE_NAMES, backend

IDX_VR = FEATURE_NAMES.index("volume_ratio")


def make_flat_rows_with_final_spike(total=150, base_volume=100_000, spike_multiplier=5.0):
    rows = []
    for i in range(total):
        close = 100.0
        volume = base_volume * spike_multiplier if i == total - 1 else base_volume
        rows.append({
            "symbol": "TEST9902",
            "date": f"2026-{(i // 28) + 1:02d}-{(i % 28) + 1:02d}",
            "open": close, "high": close * 1.01, "low": close * 0.99,
            "close": close, "volume": volume,
        })
    return rows


class VolumeRatioExcludesTodayTests(unittest.TestCase):
    def test_true_spike_ratio_is_not_diluted_by_todays_own_volume(self):
        rows = make_flat_rows_with_final_spike(total=150, base_volume=100_000, spike_multiplier=5.0)
        features = backend.build_features_for_rows(rows, market_rows={}, sector_strength={})
        last_item = next(item for item in features if item["index"] == len(rows) - 1)
        volume_ratio = last_item["x"][IDX_VR]
        # 真實倍數是 5.0；修復前的舊算法會把當日巨量也算進分母，
        # 得出約 4.1667(被稀釋)。
        self.assertAlmostEqual(volume_ratio, 5.0, places=4)

    def test_old_including_today_formula_would_have_been_diluted(self):
        # 對照組：直接驗證「含當日」跟「不含當日」兩種均量算法的差異方向與
        # 大小，確認修復前的算法確實會系統性低估。
        rows = make_flat_rows_with_final_spike(total=150, base_volume=100_000, spike_multiplier=5.0)
        volumes = [row["volume"] for row in rows]
        index = len(rows) - 1
        avg_excluding_today = backend.sma(volumes, 20, index - 1)
        avg_including_today = backend.sma(volumes, 20, index)
        ratio_excluding_today = volumes[index] / avg_excluding_today
        ratio_including_today = volumes[index] / avg_including_today
        self.assertAlmostEqual(ratio_excluding_today, 5.0, places=4)
        self.assertLess(ratio_including_today, ratio_excluding_today)
        self.assertAlmostEqual(ratio_including_today, 500_000 / 120_000, places=4)

    def test_no_spike_ratio_stays_near_one(self):
        rows = make_flat_rows_with_final_spike(total=150, base_volume=100_000, spike_multiplier=1.0)
        features = backend.build_features_for_rows(rows, market_rows={}, sector_strength={})
        last_item = next(item for item in features if item["index"] == len(rows) - 1)
        volume_ratio = last_item["x"][IDX_VR]
        self.assertAlmostEqual(volume_ratio, 1.0, places=4)


if __name__ == "__main__":
    unittest.main()
