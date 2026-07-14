"""
妖股短線規則引擎(modules/brain/monster_rule_engine.py)的回歸測試。

這是純函式，不碰資料庫/網路，全部用凍結的合成輸入測試。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from modules.brain.monster_rule_engine import monster_rule_engine


def base_inputs(**overrides):
    defaults = dict(
        volume_ratio=3.5,
        breakout=True,
        counter_trend_strength=False,
        day_trade_ratio=0.20,
        main_force_buy_sell_recent=[100, 200, 300],
        broker_branch_net_buy_recent=[50, 60, 70],
        rsi=60.0,
        ret5=8.0,
        change1=3.0,
        stock_stronger=True,
        data_confidence=0.8,
    )
    defaults.update(overrides)
    return defaults


class AllRulesPassTests(unittest.TestCase):
    def test_all_conditions_met_allows_can_buy_now(self):
        result = monster_rule_engine(base_inputs())
        self.assertEqual(result["action"], "CAN_BUY_NOW")
        self.assertFalse(result["vetoed"])
        self.assertIsNone(result["vetoReason"])
        self.assertFalse(result["overheated"])


class RequiredRuleFailureTests(unittest.TestCase):
    """必要規則沒過 -> 降級成 WATCH_ONLY，不是直接 REJECT。"""

    def test_low_volume_ratio_downgrades_to_watch_only(self):
        result = monster_rule_engine(base_inputs(volume_ratio=1.2))
        self.assertEqual(result["action"], "WATCH_ONLY")
        self.assertFalse(result["vetoed"])

    def test_no_breakout_and_no_counter_trend_downgrades(self):
        result = monster_rule_engine(base_inputs(breakout=False, counter_trend_strength=False))
        self.assertEqual(result["action"], "WATCH_ONLY")

    def test_high_day_trade_ratio_downgrades_not_rejects(self):
        result = monster_rule_engine(base_inputs(day_trade_ratio=0.60))
        self.assertEqual(result["action"], "WATCH_ONLY")
        self.assertFalse(result["vetoed"])

    def test_low_data_confidence_downgrades(self):
        result = monster_rule_engine(base_inputs(data_confidence=0.2))
        self.assertEqual(result["action"], "WATCH_ONLY")

    def test_chip_data_mismatch_downgrades(self):
        result = monster_rule_engine(base_inputs(
            main_force_buy_sell_recent=[100, -50], broker_branch_net_buy_recent=[50, 60],
        ))
        self.assertEqual(result["action"], "WATCH_ONLY")


class MissingDataIsNeutralTests(unittest.TestCase):
    """缺資料的規則要中性處理(ok=None)，不能被當成失敗擋下——
    避免覆蓋率不足的中小型妖股被誤殺。"""

    def test_missing_chip_data_does_not_block_can_buy_now(self):
        result = monster_rule_engine(base_inputs(
            main_force_buy_sell_recent=[], broker_branch_net_buy_recent=[],
        ))
        self.assertEqual(result["action"], "CAN_BUY_NOW")
        chip_rule = next(r for r in result["rules"] if r["key"] == "chipConcentration")
        self.assertIsNone(chip_rule["ok"])

    def test_missing_day_trade_ratio_does_not_block_can_buy_now(self):
        result = monster_rule_engine(base_inputs(day_trade_ratio=None))
        self.assertEqual(result["action"], "CAN_BUY_NOW")
        day_trade_rule = next(r for r in result["rules"] if r["key"] == "dayTradeRatio")
        self.assertIsNone(day_trade_rule["ok"])

    def test_missing_volume_ratio_is_neutral_but_still_downgrades(self):
        # volume_ratio 本身缺資料時該規則是中性(None)，但仍然不足以構成
        # CAN_BUY_NOW 的充分條件——這裡用其餘規則仍全過的情境驗證只有
        # volumeSurge 這條是 None，其餘沒被牽連。
        result = monster_rule_engine(base_inputs(volume_ratio=None))
        volume_rule = next(r for r in result["rules"] if r["key"] == "volumeSurge")
        self.assertIsNone(volume_rule["ok"])
        self.assertEqual(result["action"], "CAN_BUY_NOW")

    def test_missing_data_confidence_is_neutral(self):
        # 修過的bug：data_confidence=None 之前會被 `(None or 0) >= 0.55`
        # 算成False(必要條件失敗、降級WATCH_ONLY)，違反本類別的中性處理
        # 原則。現在要跟其他規則一樣，缺資料是None、不擋CAN_BUY_NOW。
        result = monster_rule_engine(base_inputs(data_confidence=None))
        confidence_rule = next(r for r in result["rules"] if r["key"] == "dataConfidence")
        self.assertIsNone(confidence_rule["ok"])
        self.assertEqual(result["action"], "CAN_BUY_NOW")


class OverheatGuardTests(unittest.TestCase):
    """過熱煞車：預設是唯一否決規則，但籌碼連三日買超+當沖比例低檔時降級
    而非全面封殺。"""

    def test_high_rsi_without_chip_support_rejects(self):
        result = monster_rule_engine(base_inputs(rsi=90.0, main_force_buy_sell_recent=[100]))
        self.assertEqual(result["action"], "REJECT")
        self.assertTrue(result["vetoed"])
        self.assertIn("過熱", result["vetoReason"])

    def test_high_rsi_with_strong_chip_streak_and_low_day_trade_downgrades_instead_of_rejecting(self):
        result = monster_rule_engine(base_inputs(
            rsi=90.0, main_force_buy_sell_recent=[100, 200, 300], day_trade_ratio=0.15,
        ))
        self.assertEqual(result["action"], "WATCH_ONLY")
        self.assertFalse(result["vetoed"])
        self.assertIn("降級", result["vetoReason"])

    def test_extreme_ret5_rejects(self):
        result = monster_rule_engine(base_inputs(ret5=25.0, main_force_buy_sell_recent=[100]))
        self.assertEqual(result["action"], "REJECT")

    def test_extreme_volume_ratio_rejects(self):
        result = monster_rule_engine(base_inputs(volume_ratio=6.0, main_force_buy_sell_recent=[100]))
        self.assertEqual(result["action"], "REJECT")

    def test_overheat_override_fails_if_day_trade_ratio_also_high(self):
        # 籌碼連三日買超，但當沖比例也偏高，override 條件不成立，還是要否決
        result = monster_rule_engine(base_inputs(
            rsi=90.0, main_force_buy_sell_recent=[100, 200, 300], day_trade_ratio=0.50,
        ))
        self.assertEqual(result["action"], "REJECT")
        self.assertTrue(result["vetoed"])

    def test_extreme_change1_rejects(self):
        # 修過的bug：舊版overheated判斷式漏了change1這條，跟ml_backend.py
        # 的正式Brain Engine定義對不起來，導致單日暴漲>9%但其餘三項未達標
        # 的股票，兩套系統對「過不過熱」給出不同答案。
        result = monster_rule_engine(base_inputs(change1=12.0, main_force_buy_sell_recent=[100]))
        self.assertEqual(result["action"], "REJECT")
        self.assertTrue(result["overheated"])

    def test_counter_trend_strength_suppresses_overheated(self):
        # 修過的bug：舊版overheated完全沒有counter_trend_strength豁免，
        # 跟ml_backend.py不一致——大盤弱但個股逆勢強時，即使RSI爆表也不該
        # 判定過熱。
        result = monster_rule_engine(base_inputs(
            rsi=95.0, counter_trend_strength=True, main_force_buy_sell_recent=[100],
        ))
        self.assertFalse(result["overheated"])
        self.assertFalse(result["vetoed"])


class BonusTagTests(unittest.TestCase):
    def test_limit_up_touch_tag_present(self):
        result = monster_rule_engine(base_inputs(change1=9.8))
        self.assertIn("limitUpTouch", result["bonusTags"])

    def test_no_limit_up_touch_tag_when_change_below_threshold(self):
        result = monster_rule_engine(base_inputs(change1=5.0))
        self.assertNotIn("limitUpTouch", result["bonusTags"])

    def test_market_relative_tag_from_stock_stronger(self):
        result = monster_rule_engine(base_inputs(stock_stronger=True, counter_trend_strength=False))
        self.assertIn("marketRelative", result["bonusTags"])

    def test_market_relative_tag_from_counter_trend_strength(self):
        result = monster_rule_engine(base_inputs(stock_stronger=False, counter_trend_strength=True))
        self.assertIn("marketRelative", result["bonusTags"])

    def test_no_market_relative_tag_when_neither_condition_met(self):
        result = monster_rule_engine(base_inputs(stock_stronger=False, counter_trend_strength=False, breakout=True))
        self.assertNotIn("marketRelative", result["bonusTags"])


if __name__ == "__main__":
    unittest.main()
