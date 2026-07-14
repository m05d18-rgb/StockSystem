"""獨立 AI 多週期短期淨獲利目標的回歸測試。

妖股雷達仍可追蹤 10 日內 +10%，但模型正例改為次日開盤進場後，3/5/10
日扣成本加權淨報酬在最大不利幅度懲罰後仍為正。
"""
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ml_backend
from ml_backend import backend


def make_rows(closes, volumes=None):
    rows = []
    for i, close in enumerate(closes):
        open_price = closes[i - 1] if i > 0 else close
        rows.append({
            "date": f"2026-{(i // 28) + 1:02d}-{(i % 28) + 1:02d}",
            "open": open_price,
            "high": max(open_price, close) * 1.01,
            "low": min(open_price, close) * 0.99,
            "close": close,
            "volume": (volumes[i] if volumes else 1_000_000),
        })
    return rows


class ShortProfitTargetLabelTests(unittest.TestCase):
    INDEX = 5

    def _flat_then(self, path):
        closes = [100.0] * 6 + path
        while len(closes) < self.INDEX + 16:
            closes.append(closes[-1])
        return closes

    def test_modest_profitable_path_is_positive_without_reaching_ten_percent(self):
        path = [101.0, 101.5, 102.0, 102.2, 102.5, 102.7, 103.0, 103.1, 103.2, 103.5]
        target = backend.short_term_target(make_rows(self._flat_then(path)), self.INDEX)

        self.assertEqual(target["y"], 1)
        self.assertEqual(target["exit_reason"], "short_profit_net_positive")
        self.assertGreater(target["net_return"], 0)
        self.assertLess(max(target["horizon_net_returns"].values()), 0.10)
        self.assertEqual(set(target["horizon_net_returns"]), {3, 5, 10})

    def test_flat_price_is_negative_after_realistic_costs(self):
        target = backend.short_term_target(
            make_rows(self._flat_then([100.0] * 10)), self.INDEX,
        )

        self.assertEqual(target["y"], 0)
        self.assertLess(target["net_return"], 0)
        self.assertEqual(target["target_strength"], 0.0)

    def test_large_intraperiod_drawdown_can_reject_profitable_endpoints(self):
        path = [75.0, 100.0, 105.0, 105.0, 105.0, 105.0, 105.0, 105.0, 105.0, 105.0]
        target = backend.short_term_target(make_rows(self._flat_then(path)), self.INDEX)

        self.assertGreater(target["future_return"], 0)
        self.assertLess(target["max_adverse_return"], -0.20)
        self.assertLess(target["risk_adjusted_return"], 0)
        self.assertEqual(target["y"], 0)

    def test_losing_path_is_negative_and_keeps_true_returns(self):
        path = [99.0, 98.0, 97.0, 96.0, 95.0, 94.0, 93.0, 92.0, 91.0, 90.0]
        target = backend.short_term_target(make_rows(self._flat_then(path)), self.INDEX)

        self.assertEqual(target["y"], 0)
        self.assertLess(target["net_return_3d"], 0)
        self.assertLess(target["net_return_5d"], 0)
        self.assertLess(target["net_return_10d"], 0)

    def test_target_requires_only_ten_future_sessions_not_twenty(self):
        closes = [100.0] * 6 + [102.0] * 10
        self.assertIsNotNone(backend.short_term_target(make_rows(closes), self.INDEX))


class TargetPolicyConsistencyTests(unittest.TestCase):
    def test_ai_target_is_separate_from_monster_radar_contract(self):
        self.assertEqual(ml_backend.MONSTER_TARGET_RETURN, 0.10)
        self.assertEqual(ml_backend.MONSTER_TARGET_HORIZON_DAYS, 10)
        self.assertEqual(ml_backend.SHORT_PROFIT_TARGET_TYPE, "short-profit-net-v1")
        self.assertEqual(ml_backend.SHORT_PROFIT_HORIZONS, (3, 5, 10))
        self.assertTrue(ml_backend.SHORT_PROFIT_POLICY_HASH)

    def test_thresholds_use_only_new_target_base_rate(self):
        with patch.object(backend, "_recent_target_hit_rate", return_value=0.44):
            self.assertEqual(backend.buy_signal_threshold(), 0.54)
        with patch.object(backend, "_recent_target_hit_rate", return_value=0.62):
            self.assertEqual(backend.buy_signal_threshold(), 0.46)
        with patch.object(backend, "_recent_target_hit_rate", return_value=0.30):
            self.assertEqual(backend.buy_signal_threshold(), 0.58)
        with patch.object(backend, "_recent_target_hit_rate", return_value=None):
            self.assertEqual(backend.buy_signal_threshold(), 0.50)
        with patch.object(backend, "_recent_target_hit_rate", return_value=0.52):
            self.assertEqual(backend.buy_signal_threshold(), 0.50)

    def test_policy_hash_changes_with_threshold_or_stock_universe(self):
        base = ml_backend.short_profit_policy_hash(["2330", "2454"], 0.50)
        self.assertNotEqual(
            base, ml_backend.short_profit_policy_hash(["2330", "2454"], 0.54),
        )
        self.assertNotEqual(
            base, ml_backend.short_profit_policy_hash(["2330", "2454", "2303"], 0.50),
        )


if __name__ == "__main__":
    unittest.main()
