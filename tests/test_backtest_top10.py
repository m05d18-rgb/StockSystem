"""
backtest_top10.py 的回歸測試，對應這次修的三個問題(此腳本之前完全零測試覆蓋)：

1. radar_gate() 重建的入選門檻遺漏生產 quick_monster_filter() 的 liquidity_ok
   (均量>=1000張、成交額>=3000萬)硬性條件，讓低流動性股票混進回測命中率。
2. radar_gate()/quick_score() 的 counter_strength 遺漏 month_high_strength
   (接近月高)判斷腿，跟 ml_backend.py 生產邏輯不一致，影響的是選股層級
   而非分數層級。
3. deterministic_noise() 用內建 hash() 對字串雜湊，受 PYTHONHASHSEED 隨機化
   影響，每次執行結果不同，跟函式名稱/註解宣稱的「可重現」矛盾——改用
   hashlib.md5 固定雜湊。

執行方式：
  python -m unittest tests.test_backtest_top10 -v
"""
import os
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import backtest_top10 as bt10


def make_record(**overrides):
    base = {
        "symbol": "2330",
        "date": "2024-01-01",
        "ret5": 3.0,
        "ret20": 5.0,
        "vr": 1.5,
        # 2026-07-03 #163後：radar_gate/quick_score 預設用 vrIncl(舊語意，
        # 分母含當日)，"vr" 是模型特徵的新語意(排除當日)，兩者並存。
        "vrIncl": 1.5,
        "rel": 1.0,
        "change1": 1.0,
        "prob": 0.5,
        "y": 0,
        "net": 0.0,
        "avgVolume20Lots": bt10.MIN_MONSTER_AVG_VOLUME_LOTS,
        "turnoverMillion": bt10.MIN_MONSTER_TURNOVER_MILLION,
        "monthHighStrength": False,
    }
    base.update(overrides)
    return base


class RadarGateLiquidityTests(unittest.TestCase):
    def test_low_liquidity_stock_is_rejected_even_with_strong_momentum(self):
        rec = make_record(avgVolume20Lots=100, turnoverMillion=5, ret5=8, vr=2.0)
        self.assertFalse(bt10.radar_gate(rec))

    def test_meeting_liquidity_threshold_passes_when_other_conditions_met(self):
        rec = make_record(avgVolume20Lots=1000, turnoverMillion=30, ret5=3, vr=1.5)
        self.assertTrue(bt10.radar_gate(rec))

    def test_just_below_turnover_threshold_is_rejected(self):
        rec = make_record(avgVolume20Lots=2000, turnoverMillion=29.99, ret5=3, vr=1.5)
        self.assertFalse(bt10.radar_gate(rec))


class RadarGateMonthHighStrengthTests(unittest.TestCase):
    def test_pure_gate_passes_on_volume_strength_regardless_of_ret5_or_month_high(self):
        # 2026-07-07 妖股重定義:純型態量能閘門不再依賴 ret5/月高——量能放大+比大盤強
        # +流動性即入選(ret5低、無月高也通過)。回測 radar_pure OOS 0.742% 持平微升。
        rec = make_record(ret5=1.5, change1=1.2, monthHighStrength=False)
        self.assertTrue(bt10.radar_gate(rec))

    def test_pure_gate_rejects_when_not_stronger_than_market(self):
        # 比大盤弱(rel<=0)即使量能大也不入選——「強勢」是型態量能定義的一環。
        rec = make_record(rel=-0.5, vr=2.0, vrIncl=2.0)
        self.assertFalse(bt10.radar_gate(rec))

    def test_pure_gate_rejects_when_volume_not_expanded(self):
        # 量能未放大(vrIncl<1.2)不入選——量能是妖股型態核心。
        rec = make_record(vr=1.0, vrIncl=1.0)
        self.assertFalse(bt10.radar_gate(rec))

    def test_quick_score_gives_month_high_bonus(self):
        # 2026-07-07 pattern_strict：monthHighStrength=True 貢獻 quick_score 自己的
        # +34，也讓 counter_strength(其中一腿就是 monthHighStrength)翻成 True 再加
        # +6，兩者耦合是刻意對齊 ml_backend.py 生產邏輯的設計，差值應為 34+6=40。
        with_high = quick_score_of(monthHighStrength=True)
        without_high = quick_score_of(monthHighStrength=False)
        self.assertAlmostEqual(with_high - without_high, 40, places=6)


def quick_score_of(**overrides):
    return bt10.quick_score(make_record(**overrides))


class DeterministicNoiseTests(unittest.TestCase):
    def test_same_input_gives_same_value_within_process(self):
        first = bt10.deterministic_noise("2330", "2024-01-01")
        second = bt10.deterministic_noise("2330", "2024-01-01")
        self.assertEqual(first, second)

    def test_value_is_reproducible_across_fresh_processes(self):
        # 這是重現原本 bug 的關鍵測試：hash() 受 PYTHONHASHSEED 隨機化影響，
        # 同一支字串在不同行程裡值不同；改用 hashlib 後，兩個獨立子行程
        # (各自隨機的 PYTHONHASHSEED)必須算出完全一樣的值。
        script = (
            "import sys; sys.path.insert(0, r'" + os.path.dirname(os.path.dirname(os.path.abspath(__file__))) + "'); "
            "import backtest_top10 as bt10; print(bt10.deterministic_noise('2330', '2024-01-01'))"
        )
        results = set()
        for _ in range(2):
            proc = subprocess.run(
                [sys.executable, "-c", script],
                stdin=subprocess.DEVNULL,
                capture_output=True, text=True, check=True,
            )
            results.add(proc.stdout.strip())
        self.assertEqual(len(results), 1, f"跨行程結果不一致: {results}")

    def test_value_within_expected_range(self):
        value = bt10.deterministic_noise("2330", "2024-01-01")
        self.assertGreaterEqual(value, 0.0)
        self.assertLess(value, 1.0)


if __name__ == "__main__":
    unittest.main()
