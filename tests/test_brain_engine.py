"""
Brain v2 引擎與 ml_backend/backtest 資料管線的回歸測試。

這裡的測試全部用凍結的合成輸入，不呼叫真實資料庫/即時市場資料 —— Brain v2
的分數會隨市場即時波動（見 build_brain_decision 內部 predict_symbol 預設
repair=True 會重抓大盤行情），拿即時資料當測試基準會不穩定、無法重現。

執行方式：
  python -m unittest discover -s tests -v
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from modules.brain import engine as brain_engine


def make_component(key, score, weight, ok, auxiliary=False):
    return {"key": key, "label": key, "score": score, "weight": weight, "ok": ok, "value": "", "auxiliary": auxiliary}


class StrategyProfileTests(unittest.TestCase):
    """context 字串要正確路由到對應的策略腦袋，不能悄悄退回 monster。"""

    def test_monster_profile_thresholds(self):
        profile = brain_engine._brain_strategy_profile("monster")
        self.assertEqual(profile["key"], "monster")
        self.assertAlmostEqual(profile["entryThreshold"], 0.60)  # 2026-07-07 純規則化校準:0.57→0.60 補償移除formalModel後v2_score上移
        self.assertIn("kline", profile["requiredComponents"])
        self.assertIn("volume", profile["requiredComponents"])

    def test_portfolio_exit_profile_routes_correctly(self):
        profile = brain_engine._brain_strategy_profile("portfolio_exit")
        self.assertEqual(profile["key"], "portfolio_exit")
        self.assertAlmostEqual(profile["entryThreshold"], 0.45)
        self.assertNotIn("kline", profile["requiredComponents"])
        self.assertNotIn("volume", profile["requiredComponents"])
        self.assertNotIn("formalModel", profile["requiredComponents"])  # 2026-07-07 純規則化:模型不再是必要條件
        self.assertIn("market", profile["requiredComponents"])
        self.assertIn("risk", profile["requiredComponents"])
        self.assertIn("dataConfidence", profile["requiredComponents"])
        weights = profile["weights"]
        self.assertLess(weights["kline"], weights["market"])
        self.assertLess(weights["volume"], weights["strategyBacktest"])
        self.assertGreater(weights["risk"], 0.06)

    def test_portfolio_exit_aliases_route_the_same(self):
        base = brain_engine._brain_strategy_profile("portfolio_exit")
        for alias in ("portfolio-exit", "exit", "sell-alert", "sell"):
            profile = brain_engine._brain_strategy_profile(alias)
            self.assertEqual(profile["key"], "portfolio_exit")
            self.assertAlmostEqual(profile["entryThreshold"], base["entryThreshold"])

    def test_unknown_context_falls_back_to_monster(self):
        profile = brain_engine._brain_strategy_profile("some_unknown_context")
        self.assertEqual(profile["key"], "monster")


class DecisionFlowScoreTests(unittest.TestCase):
    def _rows(self, closes, volume=1_000_000):
        rows = []
        for idx, close in enumerate(closes, start=1):
            rows.append({
                "date": f"2026-05-{idx:02d}" if idx <= 31 else f"2026-06-{idx - 31:02d}",
                "open": close * 0.99,
                "high": close * 1.01,
                "low": close * 0.98,
                "close": close,
                "volume": volume * (1.8 if idx == len(closes) else 1.0),
                "foreign_buy_sell": 100,
                "trust_buy_sell": 50,
            })
        return rows

    def test_strong_breakout_scores_entry_and_hold(self):
        closes = [50 + idx * 0.5 for idx in range(70)]
        rows = self._rows(closes)

        result = brain_engine._brain_decision_flow_score(rows)

        self.assertTrue(result["ok"])
        self.assertGreaterEqual(result["entryScore"], 0.65)
        self.assertGreaterEqual(result["holdScore"], 0.60)
        self.assertLess(result["exitScore"], 0.34)
        self.assertTrue(result["signals"]["maBull"])
        self.assertTrue(result["signals"]["volumeExpanded"])
        self.assertTrue(result["signals"]["institutional3Buy"])

    def test_large_drop_raises_exit_and_force_risk(self):
        closes = [50 + idx * 0.5 for idx in range(69)]
        closes.append(closes[-1] * 0.88)
        rows = self._rows(closes)
        rows[-1]["foreign_buy_sell"] = -500
        rows[-1]["trust_buy_sell"] = -200

        result = brain_engine._brain_decision_flow_score(rows)

        self.assertTrue(result["ok"])
        self.assertTrue(result["signals"]["singleDrop8Pct"])
        self.assertTrue(result["signals"]["institutionalSell"])
        self.assertGreaterEqual(result["riskScore"], 0.25)
        self.assertGreaterEqual(result["exitScore"], 0.34)


class StrategyHorizonTests(unittest.TestCase):
    def _rows(self, closes, volume=1_000_000, last_volume_ratio=1.0):
        rows = []
        for idx, close in enumerate(closes, start=1):
            row_volume = volume * (last_volume_ratio if idx == len(closes) else 1.0)
            rows.append({
                "date": f"2026-01-{idx:02d}" if idx <= 31 else f"2026-03-{idx - 31:02d}",
                "open": close * 0.99,
                "high": close * 1.01,
                "low": close * 0.98,
                "close": close,
                "volume": row_volume,
                "foreign_buy_sell": 100,
                "trust_buy_sell": 50,
            })
        return rows

    def test_breakout_volume_classifies_as_short_trade(self):
        closes = [50.0] * 66 + [51.0, 52.0, 53.0, 58.0]
        rows = self._rows(closes, last_volume_ratio=2.2)
        decision_flow = brain_engine._brain_decision_flow_score(rows)

        result = brain_engine._brain_strategy_horizon(rows, decision_flow, {"volumeRatio": 2.2})

        self.assertTrue(result["ok"])
        self.assertEqual(result["key"], "short_trade")
        self.assertEqual(result["primaryHorizon"], "10d")
        self.assertIn("短期", result["label"])

    def test_ma20_continuation_classifies_as_mid_swing(self):
        closes = [100.0] * 45 + [100 + idx * 0.35 for idx in range(25)]
        rows = self._rows(closes, last_volume_ratio=1.1)
        decision_flow = brain_engine._brain_decision_flow_score(rows)

        result = brain_engine._brain_strategy_horizon(rows, decision_flow, {"volumeRatio": 1.1})

        self.assertTrue(result["ok"])
        self.assertEqual(result["key"], "mid_swing")
        self.assertEqual(result["primaryHorizon"], "20d")
        self.assertIn("中期", result["label"])

    def test_slow_ma60_trend_classifies_as_long_trend(self):
        closes = [80 + idx * 0.12 for idx in range(90)]
        rows = self._rows(closes, last_volume_ratio=1.0)
        decision_flow = brain_engine._brain_decision_flow_score(rows)

        result = brain_engine._brain_strategy_horizon(rows, decision_flow, {"volumeRatio": 1.0})

        self.assertTrue(result["ok"])
        self.assertEqual(result["key"], "long_trend")
        self.assertEqual(result["primaryHorizon"], "60d")
        self.assertIn("長期", result["label"])


class BrainV2AggregateTests(unittest.TestCase):
    """對應今天手算驗證過的 2330 案例：加權總分、必要條件、進場判斷。"""

    def _aggregate(self, **overrides):
        components = overrides.pop("v2_components")
        defaults = dict(
            v2_components=components,
            required_component_keys={"dataConfidence", "formalModel", "kline", "risk"},
            probability=0.36,
            threshold=0.45,
            formal_component=0.36,
            kline_component=0.59,
            volume_component=0.46,
            market_component=0.9,
            risk_component=1.0,
            data_complete=True,
            model_ready=True,
            data_confidence_score=0.94,
            data_confidence_threshold=0.6,
            entry_score_threshold=0.53,
            entry_context=True,
        )
        defaults.update(overrides)
        return brain_engine._brain_v2_aggregate(**defaults)

    def test_weighted_average_excludes_none_score_components(self):
        components = [
            make_component("a", 0.8, 0.5, True),
            make_component("b", 0.4, 0.5, False),
            make_component("auxSignal", None, 0.2, None, auxiliary=True),
        ]
        result = self._aggregate(v2_components=components, required_component_keys=set())
        # auxSignal 的 score 是 None，不該計入分子也不該計入分母
        self.assertAlmostEqual(result["v2_score"], 0.8 * 0.5 + 0.4 * 0.5, places=9)
        self.assertEqual(result["v2_pass_count"], 1)

    def test_total_score_above_threshold_but_required_component_fails_blocks_entry(self):
        """今天驗算過的 2330 邊界案例：總分 60.2% 超過 53% 門檻，
        但必要條件 formalModel(35.9%<45%) 沒過、也沒有 technical override，
        entry_allowed 必須是 False，decision_blocked 必須是 True。"""
        components = [
            make_component("formalModel", 0.3588389593643745, 0.14, False),
            make_component("kline", 0.5858258439046562, 0.2, True),
            make_component("volume", 0.4625, 0.14, False),
            make_component("market", 0.9, 0.12, True),
            make_component("chipMoney", 0.41769230769230764, 0.08, False),
            make_component("otherSignal", 0.6425, 0.07, True),
            make_component("strategyBacktest", 0.5950284368953473, 0.16, True),
            make_component("auxSignal", None, 0.0, None, auxiliary=True),
            make_component("risk", 1.0, 0.06, True),
            make_component("dataConfidence", 0.9416666666666665, 0.03, True),
        ]
        result = self._aggregate(
            v2_components=components,
            required_component_keys={"dataConfidence", "formalModel", "kline", "risk"},
            probability=0.3588389593643745,
            threshold=0.45,
            formal_component=0.3588389593643745,
            kline_component=0.5858258439046562,
            volume_component=0.4625,
            market_component=0.9,
            risk_component=1.0,
            data_confidence_score=0.9416666666666665,
            data_confidence_threshold=0.6,
            entry_score_threshold=0.53,
        )
        self.assertAlmostEqual(result["v2_score"], 0.6019975576105839, places=9)
        self.assertGreater(result["v2_score"], 0.53)  # 總分確實過門檻
        self.assertFalse(result["formal_pass"])  # 但正式模型沒過
        self.assertFalse(result["technical_override"])  # 也沒有觸發 override

    def _technical_override_components(self, kline_score, volume_score):
        return [
            make_component("formalModel", 0.5, 0.20, True),
            make_component("kline", kline_score, 0.06, kline_score >= 0.55),
            make_component("volume", volume_score, 0.06, volume_score >= 0.55),
            make_component("market", 0.9, 0.20, True),
            make_component("risk", 1.0, 0.10, True),
            make_component("dataConfidence", 0.9, 0.04, True),
        ]

    def test_technical_override_ignores_kline_volume_when_not_required(self):
        """portfolio_exit 這種把 kline/volume 排除在 requiredComponents 外的
        profile，technical_override 不該再用 monster 的固定門檻(kline>=0.64、
        volume>=0.55)卡它們——已經買進的股票不需要重新符合K線突破/量能剛
        啟動這種進場型態條件。"""
        components = self._technical_override_components(kline_score=0.20, volume_score=0.10)
        result = self._aggregate(
            v2_components=components,
            required_component_keys={"dataConfidence", "formalModel", "market", "risk"},
            probability=0.30,
            threshold=0.45,
            formal_component=0.5,
            kline_component=0.20,
            volume_component=0.10,
            market_component=0.9,
            risk_component=1.0,
            data_confidence_score=0.9,
            data_confidence_threshold=0.6,
            entry_score_threshold=0.45,
        )
        self.assertTrue(result["technical_override"])

    def test_technical_override_still_requires_kline_volume_when_required(self):
        """monster 這種把 kline/volume 列為必要條件的 profile，行為不能變——
        kline/volume 太弱時 technical_override 仍然要是 False。"""
        components = self._technical_override_components(kline_score=0.20, volume_score=0.10)
        result = self._aggregate(
            v2_components=components,
            required_component_keys={"dataConfidence", "formalModel", "market", "risk", "kline", "volume"},
            probability=0.30,
            threshold=0.45,
            formal_component=0.5,
            kline_component=0.20,
            volume_component=0.10,
            market_component=0.9,
            risk_component=1.0,
            data_confidence_score=0.9,
            data_confidence_threshold=0.6,
            entry_score_threshold=0.45,
        )
        self.assertFalse(result["technical_override"])
        self.assertEqual(
            sorted(row["key"] for row in result["required_component_failures"]),
            ["kline", "volume"],
        )

    def _monster_profile_components(self, formal_component, formal_pass, kline_score, volume_score, market_score, risk_score):
        # formalModel 的 ok 必須跟真實 build_brain_decision 的算法一致
        # (formal_component >= threshold，也就是 formal_pass 本身)，不能像
        # 舊測試那樣手動寫死 True——那樣會讓 formalModel 永遠不出現在
        # required_component_failures 裡，測不出「formalModel 也在
        # requiredComponents 清單內，把 technical_override 單方面否決掉」
        # 這個真實會發生在生產環境的 bug。
        return [
            make_component("formalModel", formal_component, 0.18, formal_pass),
            make_component("kline", kline_score, 0.17, kline_score >= 0.64),
            make_component("volume", volume_score, 0.18, volume_score >= 0.55),
            make_component("market", market_score, 0.10, market_score >= 0.52),
            make_component("chipMoney", 0.60, 0.12, True),
            make_component("strategyBacktest", 0.60, 0.12, True),
            make_component("risk", risk_score, 0.06, risk_score >= 0.55),
            make_component("dataConfidence", 0.90, 0.03, True),
        ]

    def test_technical_override_actually_bypasses_formal_gate_with_realistic_monster_profile(self):
        """複現稽核抓到的真實 bug：monster profile 的 requiredComponents 裡
        connait formalModel 自己，K線/量能/大盤/風控全部強勢、technical_
        override 算出 True，但 formalModel 的 ok(=formal_pass)是 False，
        修復前 v2_entry_allowed 仍然會被 required_component_failures 卡住
        變成 False——跟程式碼註解「讓已確認的盤中證據不被過時的日線分數
        蓋掉」的設計意圖完全相反。這裡用跟生產環境 monster profile 完全
        一致的 requiredComponents(含 formalModel)驗證修復後 entry 真的放行。"""
        components = self._monster_profile_components(
            formal_component=0.42, formal_pass=False,
            kline_score=0.70, volume_score=0.60, market_score=0.80, risk_score=0.80,
        )
        result = self._aggregate(
            v2_components=components,
            required_component_keys={"formalModel", "kline", "volume", "market", "risk", "dataConfidence"},
            probability=0.42,
            threshold=0.45,
            formal_component=0.42,
            kline_component=0.70,
            volume_component=0.60,
            market_component=0.80,
            risk_component=0.80,
            data_confidence_score=0.90,
            data_confidence_threshold=0.62,
            entry_score_threshold=0.57,
        )
        self.assertFalse(result["formal_pass"])
        self.assertTrue(result["technical_override"])
        self.assertNotIn("formalModel", [row.get("key") for row in result["required_component_failures"]])
        self.assertTrue(result["v2_entry_allowed"])

    def test_formal_model_still_blocks_when_neither_formal_pass_nor_override_hold(self):
        """formal_pass 沒過、technical_override 也沒過(例如風控太弱)時，
        formalModel 仍然要照常算進 required_component_failures——不能因為
        排除了 formalModel 的例外情況，連帶讓真正沒過關的案例也被誤放行，
        敘事文字(_brain_v2_narrative_blocker)才不會漏講「正式模型分數不夠」。"""
        components = self._monster_profile_components(
            formal_component=0.42, formal_pass=False,
            kline_score=0.70, volume_score=0.60, market_score=0.80, risk_score=0.30,
        )
        result = self._aggregate(
            v2_components=components,
            required_component_keys={"formalModel", "kline", "volume", "market", "risk", "dataConfidence"},
            probability=0.42,
            threshold=0.45,
            formal_component=0.42,
            kline_component=0.70,
            volume_component=0.60,
            market_component=0.80,
            risk_component=0.30,
            data_confidence_score=0.90,
            data_confidence_threshold=0.62,
            entry_score_threshold=0.57,
        )
        self.assertFalse(result["technical_override"])
        self.assertIn("formalModel", [row.get("key") for row in result["required_component_failures"]])
        self.assertFalse(result["v2_entry_allowed"])

    def test_technical_override_bypasses_formal_gate(self):
        """正式模型剛好在 override 門檻之上、且其他技術指標都夠強時，
        即使 formal_pass 沒過，也能靠 technical_override 通過。"""
        components = [
            make_component("formalModel", 0.42, 0.14, False),  # >=0.40 但 <threshold(0.45)
            make_component("kline", 0.70, 0.2, True),
            make_component("volume", 0.60, 0.14, True),
            make_component("market", 0.80, 0.12, True),
            make_component("chipMoney", 0.60, 0.08, True),
            make_component("otherSignal", 0.60, 0.07, True),
            make_component("strategyBacktest", 0.60, 0.16, True),
            make_component("risk", 0.80, 0.06, True),
            make_component("dataConfidence", 0.90, 0.03, True),
        ]
        result = self._aggregate(
            v2_components=components,
            required_component_keys=set(),
            probability=0.42,
            threshold=0.45,
            formal_component=0.42,
            kline_component=0.70,
            volume_component=0.60,
            market_component=0.80,
            risk_component=0.80,
            data_confidence_score=0.90,
            data_confidence_threshold=0.6,
            entry_score_threshold=0.53,
        )
        self.assertFalse(result["formal_pass"])
        self.assertTrue(result["technical_override"])
        self.assertTrue(result["v2_entry_allowed"])

    def test_all_conditions_pass_allows_entry(self):
        components = [
            make_component("formalModel", 0.60, 0.5, True),
            make_component("risk", 0.80, 0.5, True),
        ]
        result = self._aggregate(
            v2_components=components,
            required_component_keys=set(),
            probability=0.60,
            threshold=0.45,
            formal_component=0.60,
            kline_component=0.70,
            volume_component=0.70,
            market_component=0.70,
            risk_component=0.80,
            data_confidence_score=0.90,
            data_confidence_threshold=0.6,
            entry_score_threshold=0.53,
        )
        self.assertTrue(result["v2_entry_allowed"])
        self.assertFalse(result["entry_decision_blocked"])

    def test_data_incomplete_blocks_entry_and_marks_observe_only_upstream(self):
        components = [make_component("formalModel", 0.90, 1.0, True)]
        result = self._aggregate(
            v2_components=components,
            required_component_keys=set(),
            probability=0.90,
            threshold=0.45,
            formal_component=0.90,
            kline_component=0.90,
            volume_component=0.90,
            market_component=0.90,
            risk_component=0.90,
            data_complete=False,  # 資料不完整
            data_confidence_score=0.90,
            data_confidence_threshold=0.6,
            entry_score_threshold=0.53,
        )
        self.assertFalse(result["v2_entry_allowed"])
        # entry_context 情境下，data_complete=False 時 entry_decision_blocked
        # 應該是 False（因為這時該用 observe_only 顯示「資料不足」，不是「判斷擋下」）
        self.assertFalse(result["entry_decision_blocked"])


class NarrativeBlockerTests(unittest.TestCase):
    """把「Brain v2 進場條件未通過：正式模型分數」這種術語條列，改寫成
    講人話的敘事句子——用今天驗算過的 2330 案例確認文字內容跟軟性扣分的
    gap 分級掛得上，也確認沒有必要條件失敗時的 fallback 敘事版本。"""

    def test_required_component_failure_produces_narrative_sentence(self):
        components = [
            make_component("formalModel", 0.3588389593643745, 0.14, False),
            make_component("kline", 0.5858258439046562, 0.2, True),
            make_component("volume", 0.4625, 0.14, False),
            make_component("market", 0.9, 0.12, True),
            make_component("chipMoney", 0.41769230769230764, 0.08, False),
            make_component("otherSignal", 0.6425, 0.07, True),
            make_component("strategyBacktest", 0.5950284368953473, 0.16, True),
            make_component("auxSignal", None, 0.0, None, auxiliary=True),
            make_component("risk", 1.0, 0.06, True),
            make_component("dataConfidence", 0.9416666666666665, 0.03, True),
        ]
        required_component_failures = [components[0]]  # formalModel
        soft_gate = {"penaltyDetails": [{"key": "formalModel", "gap": 0.0912, "penalty": 0.0547}]}
        sentence = brain_engine._brain_v2_narrative_blocker(
            v2_components=components,
            required_component_failures=required_component_failures,
            soft_gate=soft_gate,
            technical_override=False,
            v2_score=0.6019975576105839,
            entry_score_threshold=0.53,
            data_confidence_score=0.9416666666666665,
            data_confidence_threshold=0.6,
            risk_component=1.0,
        )
        # gap 0.0912 落在「還差一小段」的分級(0.03~0.08 之外、算差距較大的邊界)
        # make_component 測試輔助函式把 label 設成 key 本身(非中文顯示字)，所以這裡比對 key。
        self.assertIn("formalModel", sentence)
        self.assertIn("差距較大", sentence)  # gap=0.0912 > 0.08
        self.assertIn("已經過關", sentence)  # 有列出已通過的其餘條件，不是只丟術語
        self.assertNotIn("Brain v2 進場條件未通過", sentence)  # 不再是舊的條列術語格式

    def test_close_gap_reads_as_only_a_little_short(self):
        components = [make_component("formalModel", 0.42, 0.14, False)]
        soft_gate = {"penaltyDetails": [{"key": "formalModel", "gap": 0.02, "penalty": 0.012}]}
        sentence = brain_engine._brain_v2_narrative_blocker(
            v2_components=components,
            required_component_failures=components,
            soft_gate=soft_gate,
            technical_override=False,
            v2_score=0.55,
            entry_score_threshold=0.53,
            data_confidence_score=0.9,
            data_confidence_threshold=0.6,
            risk_component=0.9,
        )
        self.assertIn("只差一點點", sentence)

    def test_technical_override_true_does_not_claim_it_fell_short(self):
        # technical_override=True 代表技術面替代條件「已經通過」，這裡走到
        # 這個分支代表 formalModel 以外還有別的必要條件沒過(例如市場分量)，
        # 不能再說「技術面替代條件也差一點沒能補上」——那句話跟 technical_
        # override 為真的事實矛盾。修復前這裡永遠只看 if technical_override
        # 就加這句話，不管它到底是真的通過還是沒通過。
        components = [make_component("market", 0.40, 0.10, False)]
        soft_gate = {"penaltyDetails": [{"key": "market", "gap": 0.12, "penalty": 0.05}]}
        sentence = brain_engine._brain_v2_narrative_blocker(
            v2_components=components,
            required_component_failures=components,
            soft_gate=soft_gate,
            technical_override=True,
            v2_score=0.55,
            entry_score_threshold=0.53,
            data_confidence_score=0.9,
            data_confidence_threshold=0.6,
            risk_component=0.9,
        )
        self.assertNotIn("技術面替代條件也差一點沒能補上", sentence)

    def test_technical_override_false_does_claim_it_fell_short(self):
        components = [make_component("formalModel", 0.42, 0.14, False)]
        soft_gate = {"penaltyDetails": [{"key": "formalModel", "gap": 0.12, "penalty": 0.05}]}
        sentence = brain_engine._brain_v2_narrative_blocker(
            v2_components=components,
            required_component_failures=components,
            soft_gate=soft_gate,
            technical_override=False,
            v2_score=0.55,
            entry_score_threshold=0.53,
            data_confidence_score=0.9,
            data_confidence_threshold=0.6,
            risk_component=0.9,
        )
        self.assertIn("技術面替代條件也差一點沒能補上", sentence)

    def test_no_required_failure_falls_back_to_overall_score_narrative(self):
        # 沒有任何必要條件被判定失敗，但整體分數本身還沒到門檻——
        # 這時不該回傳 None，要有 fallback 的敘事句子而不是舊的「本機核心分數未達標」。
        sentence = brain_engine._brain_v2_narrative_blocker(
            v2_components=[make_component("formalModel", 0.50, 1.0, True)],
            required_component_failures=[],
            soft_gate={"penaltyDetails": []},
            technical_override=False,
            v2_score=0.40,
            entry_score_threshold=0.53,
            data_confidence_score=0.9,
            data_confidence_threshold=0.6,
            risk_component=0.9,
        )
        self.assertIn("整體分數", sentence)
        self.assertIn("尚未達到進場標準", sentence)

    def test_no_reason_at_all_returns_none(self):
        # required_component_failures 空、整體分數/可信度/風控都過關的情況下
        # 理論上不該被判定為 entry_decision_blocked，但函式本身仍要對這種
        # 「找不到任何原因」的輸入回傳 None，而不是硬湊一句空話。
        sentence = brain_engine._brain_v2_narrative_blocker(
            v2_components=[make_component("formalModel", 0.90, 1.0, True)],
            required_component_failures=[],
            soft_gate={"penaltyDetails": []},
            technical_override=False,
            v2_score=0.90,
            entry_score_threshold=0.53,
            data_confidence_score=0.9,
            data_confidence_threshold=0.6,
            risk_component=0.9,
        )
        self.assertIsNone(sentence)


class ScoreTrendTests(unittest.TestCase):
    """「跟昨天比」的分數趨勢——用 brain_v2_snapshots 存下來的前一筆快照
    跟今天算出來的 v2_score 相減，判斷變好/變差/持平。"""

    def test_score_improved_marks_up_with_positive_delta(self):
        trend = brain_engine._brain_score_trend(
            v2_score=0.62,
            previous_snapshot={"price_date": "2026-06-30", "v2_score": 0.55},
        )
        self.assertEqual(trend["direction"], "up")
        self.assertAlmostEqual(trend["delta"], 0.07, places=9)
        self.assertEqual(trend["previousDate"], "2026-06-30")
        self.assertIn("+7.0%", trend["text"])

    def test_score_dropped_marks_down_with_negative_delta(self):
        trend = brain_engine._brain_score_trend(
            v2_score=0.50,
            previous_snapshot={"price_date": "2026-06-30", "v2_score": 0.60},
        )
        self.assertEqual(trend["direction"], "down")
        self.assertAlmostEqual(trend["delta"], -0.10, places=9)

    def test_tiny_change_marks_flat(self):
        trend = brain_engine._brain_score_trend(
            v2_score=0.601,
            previous_snapshot={"price_date": "2026-06-30", "v2_score": 0.600},
        )
        self.assertEqual(trend["direction"], "flat")

    def test_no_previous_snapshot_returns_none(self):
        self.assertIsNone(brain_engine._brain_score_trend(v2_score=0.60, previous_snapshot=None))

    def test_current_score_none_returns_none(self):
        self.assertIsNone(
            brain_engine._brain_score_trend(v2_score=None, previous_snapshot={"price_date": "2026-06-30", "v2_score": 0.6})
        )

    def test_previous_score_missing_returns_none(self):
        self.assertIsNone(
            brain_engine._brain_score_trend(v2_score=0.60, previous_snapshot={"price_date": "2026-06-30", "v2_score": None})
        )


class IsBorderlineTests(unittest.TestCase):
    """資料可信度門檻邊緣警示：分數離門檻在 margin 以內(不管過或沒過)
    都算邊緣案例，代表資料來源數量稍微變動就可能翻盤。"""

    def test_just_above_threshold_is_borderline(self):
        self.assertTrue(brain_engine._brain_is_borderline(0.62, 0.60, margin=0.05))

    def test_just_below_threshold_is_borderline(self):
        self.assertTrue(brain_engine._brain_is_borderline(0.58, 0.60, margin=0.05))

    def test_comfortably_above_threshold_is_not_borderline(self):
        self.assertFalse(brain_engine._brain_is_borderline(0.95, 0.60, margin=0.05))

    def test_comfortably_below_threshold_is_not_borderline(self):
        self.assertFalse(brain_engine._brain_is_borderline(0.20, 0.60, margin=0.05))

    def test_just_inside_margin_boundary_is_borderline(self):
        # 0.65 - 0.60 用浮點數算會是 0.050000000000000044，剛好卡在邊界外一點點，
        # 不是這裡要測的重點，所以刻意挑一個明顯落在 margin 內側的值。
        self.assertTrue(brain_engine._brain_is_borderline(0.649, 0.60, margin=0.05))

    def test_none_score_or_threshold_is_not_borderline(self):
        self.assertFalse(brain_engine._brain_is_borderline(None, 0.60))
        self.assertFalse(brain_engine._brain_is_borderline(0.60, None))


class KlineScoreTests(unittest.TestCase):
    """對應今天手算驗證過的 2330 K 線案例。"""

    def _rows(self):
        # 60 天以上、最後一天是黑K：open=2440 high=2475 low=2410 close=2410
        # 前一天 high=2395 close=2370；其餘天數用平緩走勢補滿門檻(len>=25)。
        rows = []
        for i in range(60):
            price = 2300 + i
            rows.append({"date": f"2026-01-{i%28+1:02d}", "open": price, "high": price + 5, "low": price - 5, "close": price, "volume": 1_000_000})
        rows[-2] = {"date": "2026-06-29", "open": 2370, "high": 2395, "low": 2360, "close": 2370, "volume": 40_000_000}
        rows[-1] = {"date": "2026-06-30", "open": 2440, "high": 2475, "low": 2410, "close": 2410, "volume": 49_540_227}
        return rows

    def test_bearish_candle_pattern_score(self):
        rows = self._rows()
        prediction = {"probability": 0.3588389593643745, "tradeGate": {}, "marketGate": {}}
        result = brain_engine._brain_kline_score(rows, prediction)
        self.assertTrue(result["ok"])
        self.assertIn("突破前高", result["patterns"])
        self.assertIn("實體黑K", result["patterns"])
        self.assertIn("上影線壓力", result["patterns"])
        self.assertAlmostEqual(result["components"]["pattern"], 0.29, places=6)

    def test_component_weighting_matches_final_score(self):
        rows = self._rows()
        prediction = {"probability": 0.3588389593643745, "tradeGate": {}, "marketGate": {}}
        result = brain_engine._brain_kline_score(rows, prediction)
        c = result["components"]
        # 2026-07-07 純規則化:拿掉模型 15%,依比例重分配給型態量能(見 engine.py score)。
        manual = c["pattern"] * 0.35 + c["volume"] * 0.24 + c["ma"] * 0.24 + c["market"] * 0.17
        self.assertAlmostEqual(manual, result["score"], places=9)

    def test_insufficient_rows_returns_no_data(self):
        result = brain_engine._brain_kline_score([{"open": 1, "high": 1, "low": 1, "close": 1, "volume": 1}] * 5, {})
        self.assertFalse(result["ok"])
        self.assertIsNone(result["score"])

    def test_genuine_uptrend_still_gets_full_ma_score(self):
        # _rows() 本身就是一路上漲、最後一天跳空突破的走勢，5MA>20MA>60MA
        # 且股價站上所有均線，改用嚴格 > 之後這種真多頭排列仍要拿滿分，
        # 不能連正常案例都被誤傷。
        result = brain_engine._brain_kline_score(self._rows(), {"probability": 0.5})
        self.assertAlmostEqual(result["components"]["ma"], 1.0, places=6)

    def _flat_rows(self, price=100.0, count=65):
        return [
            {
                "date": f"2026-01-{(i % 28) + 1:02d}",
                "open": price, "high": price, "low": price, "close": price, "volume": 1_000_000,
            }
            for i in range(count)
        ]

    def test_completely_flat_locked_price_does_not_get_perfect_ma_score(self):
        # close == ma5 == ma20 == ma60（例如長期一價鎖死、無量停牌復牌）不該被
        # 原本的 >= 判斷誤判成跟真正多頭排列一樣的滿分。
        result = brain_engine._brain_kline_score(self._flat_rows(), {"probability": 0.5})
        self.assertAlmostEqual(result["components"]["ma"], 0.25, places=6)

    def test_model_score_defaults_to_neutral_when_probability_missing(self):
        rows = self._rows()
        result = brain_engine._brain_kline_score(rows, {"tradeGate": {}, "marketGate": {}})
        self.assertAlmostEqual(result["components"]["model"], 0.50, places=6)

    def test_model_score_none_prediction_defaults_to_neutral(self):
        rows = self._rows()
        result = brain_engine._brain_kline_score(rows, None)
        self.assertAlmostEqual(result["components"]["model"], 0.50, places=6)

    def test_model_score_still_honors_a_genuine_zero_probability(self):
        # 缺值(None)要給中性分，但模型真的算出 0.0 這種低分是有意義的訊號，
        # 不能被「缺值預設」誤蓋掉——要能分辨「沒有結果」跟「結果就是很差」。
        rows = self._rows()
        result = brain_engine._brain_kline_score(rows, {"probability": 0.0})
        self.assertAlmostEqual(result["components"]["model"], 0.0, places=6)

    def _rows_for_volume_ratio(self, ratio, days=65, base_volume=1_000_000):
        rows = []
        for i in range(days):
            price = 100 + i * 0.01
            rows.append({
                "date": f"2026-01-{(i % 28) + 1:02d}",
                "open": price, "high": price + 0.5, "low": price - 0.5, "close": price, "volume": base_volume,
            })
        rows[-1] = dict(rows[-1])
        rows[-1]["volume"] = base_volume * ratio
        return rows

    def test_volume_score_is_monotonic_non_decreasing_across_ratio_range(self):
        ratios = [0.3, 0.7, 1.0, 1.2, 1.5, 2.0, 2.5, 3.0, 4.0, 6.0, 10.0, 20.0]
        scores = [
            brain_engine._brain_kline_score(self._rows_for_volume_ratio(ratio), {"probability": 0.5})["components"]["volume"]
            for ratio in ratios
        ]
        for earlier, later in zip(scores, scores[1:]):
            self.assertLessEqual(earlier, later + 1e-9, f"volume_score dropped somewhere in {ratios}: {scores}")

    def test_extreme_volume_no_longer_scores_below_moderate_volume(self):
        # 原本 2.50/4.00 兩個邊界會讓爆量股(例如20倍量)分數比溫和放量(2.5倍量)
        # 還低，直接違背「量能是動能訊號」的設計初衷。
        moderate = brain_engine._brain_kline_score(self._rows_for_volume_ratio(2.5), {"probability": 0.5})
        extreme = brain_engine._brain_kline_score(self._rows_for_volume_ratio(20.0), {"probability": 0.5})
        self.assertGreaterEqual(extreme["components"]["volume"], moderate["components"]["volume"])


class BacktestStrategyKdDirectionTests(unittest.TestCase):
    """naiveKd 跟 kd 兩個子指標對同一組 K/D 數值要給出同方向的判斷，
    不能一個說強、一個說弱，互相抵銷加權平均裡的訊號。"""

    def _rows(self, days=40):
        rows = []
        for i in range(days):
            price = 100 + i * 0.1
            rows.append({
                "date": f"2026-01-{(i % 28) + 1:02d}",
                "open": price, "high": price + 0.5, "low": price - 0.5, "close": price, "volume": 1_000_000,
            })
        return rows

    def _detail_score(self, result, key):
        for detail in result["details"]:
            if detail.get("key") == key:
                return detail.get("score")
        raise AssertionError(f"detail {key} not found in {result['details']}")

    def test_high_k_bullish_alignment_scores_higher_than_low_k_in_both_metrics(self):
        rows = self._rows()
        high_k_technical = {"kd": {"k": 80, "d": 70, "prevK": 78, "prevD": 72}}
        low_k_technical = {"kd": {"k": 15, "d": 30, "prevK": 18, "prevD": 28}}
        high_result = brain_engine._brain_backtest_strategy_score(rows, technical=high_k_technical)
        low_result = brain_engine._brain_backtest_strategy_score(rows, technical=low_k_technical)
        high_kd, low_kd = self._detail_score(high_result, "kd"), self._detail_score(low_result, "kd")
        high_naive, low_naive = self._detail_score(high_result, "naiveKd"), self._detail_score(low_result, "naiveKd")
        self.assertGreater(high_kd, low_kd)
        self.assertGreater(high_naive, low_naive)
        self.assertGreaterEqual(high_naive, 0.55)
        self.assertLess(low_naive, 0.55)

    def test_naive_kd_buckets_follow_momentum_direction_not_textbook_overbought(self):
        rows = self._rows()
        cases = [(10, 0.42), (50, 0.72), (80, 0.78), (90, 0.55)]
        for k_value, expected in cases:
            technical = {"kd": {"k": k_value, "d": k_value - 5, "prevK": k_value - 2, "prevD": k_value - 3}}
            result = brain_engine._brain_backtest_strategy_score(rows, technical=technical)
            naive_score = self._detail_score(result, "naiveKd")
            self.assertAlmostEqual(naive_score, expected, places=6, msg=f"k={k_value}")


if __name__ == "__main__":
    unittest.main()
