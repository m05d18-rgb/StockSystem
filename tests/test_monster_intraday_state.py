"""
compute_monster_intraday_state (server.py) 的整合測試。

這個函式是妖股「能不能買」判斷鏈第 3 層(盤中時間窗+技術面型態)的核心，
把候選股資料(來自 ml_backend 的評分結果)跟盤中即時報價，組合算出最終
canBuy/status。這裡刻意測「整條鏈串起來的最終輸出」而不是拆開測每個
中間布林值，因為過去發現的實際 bug(過熱繞過、stop_price=0 停損失效等)
都是「單一子條件看起來正確，但組合起來的最終行為不對」，只測子函式測不出來。

全部用凍結的合成 candidate/quote 資料，不呼叫真實資料庫/即時報價。

執行方式：
  python -m unittest tests.test_monster_intraday_state -v
"""
import os
import sys
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server


INITIAL_WINDOW = {"active": True, "phase": "initial", "label": "09:30-10:00 初次買進確認"}
DIP_WINDOW = {"active": True, "phase": "dip", "label": "10:00-13:15 只看低接/V轉"}
PREMARKET_WINDOW = {"active": False, "phase": "premarket", "label": "等待 09:05 初篩"}
CLOSED_WINDOW = {"active": False, "phase": "closed", "label": "13:15 後不再進場"}
VOLUME_RULE = {"min": 0.3, "max": None, "progress": 0.5, "label": "09:30 後量能門檻隨時間提高"}


def make_candidate(**overrides):
    base = {
        "symbol": "9999",
        "close": 100.0,
        "buyTrigger": 105.0,
        "pullbackPrice": 98.0,
        "stopPrice": 93.0,
        "avgVolume20Lots": 1000.0,
        "score": 70.0,
        "probability": 0.5,
        "threshold": 0.4,
        "buyAllowed": True,
        "overheated": False,
        "surgeSetup": False,
        "counterTrendStrength": False,
    }
    base.update(overrides)
    return base


def make_quote(**overrides):
    base = {
        "currentPrice": 106.0,
        "openPrice": 101.0,
        "highPrice": 107.0,
        "lowPrice": 100.5,
        "totalVolume": 2500.0,
        "snapshotAt": "2026-07-02 09:45:00",
    }
    base.update(overrides)
    return base


def compute(candidate, quote, entry_window, has_quote=True, volume_rule=None, quote_fresh=True, market_data_fresh=True):
    return server.compute_monster_intraday_state(
        candidate["symbol"], candidate, quote, has_quote,
        entry_window, (volume_rule or VOLUME_RULE)["min"], (volume_rule or VOLUME_RULE), "Shioaji quote",
        quote_fresh=quote_fresh, market_data_fresh=market_data_fresh,
    )


class HappyPathTests(unittest.TestCase):
    """技術面型態確實成立、時段允許時，canBuy 要正確算出 True。"""

    def test_breakout_in_initial_window_is_buyable(self):
        result = compute(make_candidate(), make_quote(), INITIAL_WINDOW)
        self.assertTrue(result["canBuy"])
        self.assertEqual(result["setupType"], "breakout")
        self.assertEqual(result["status"], "突破可觀察")

    def test_v_rebound_in_dip_window_is_buyable(self):
        candidate = make_candidate(buyTrigger=105.0, pullbackPrice=98.0, stopPrice=93.0)
        quote = make_quote(openPrice=97.0, highPrice=99.0, lowPrice=94.0, currentPrice=98.5, totalVolume=2000.0)
        result = compute(candidate, quote, DIP_WINDOW)
        self.assertTrue(result["canBuy"])
        self.assertEqual(result["setupType"], "v_rebound")
        self.assertEqual(result["status"], "V轉低接可觀察")

    def test_pullback_in_initial_window_is_buyable(self):
        # buyTrigger 故意設遠高於現價，讓 breakout 條件不成立，只剩回測型態。
        candidate = make_candidate(buyTrigger=120.0, pullbackPrice=98.0, stopPrice=90.0)
        quote = make_quote(openPrice=99.0, highPrice=99.5, lowPrice=97.5, currentPrice=98.2, totalVolume=2000.0)
        result = compute(candidate, quote, INITIAL_WINDOW)
        self.assertTrue(result["canBuy"])
        self.assertEqual(result["setupType"], "pullback")
        self.assertEqual(result["status"], "回測低接可觀察")


class OverheatedBypassRegressionTests(unittest.TestCase):
    """回歸測試：過熱的股票(overheated=True)即使 surgeSetup/分數都很漂亮，
    也不能被 formal_watch_allowed 的 OR 條件繞過而變成可買。"""

    def test_overheated_candidate_cannot_buy_even_with_surge_setup_and_high_score(self):
        candidate = make_candidate(
            buyAllowed=False, overheated=True, surgeSetup=True, score=95.0,
        )
        result = compute(candidate, make_quote(), INITIAL_WINDOW)
        self.assertFalse(result["canBuy"])
        self.assertFalse(result["formalWatchAllowed"])
        self.assertFalse(result["scheduleAllowed"])

    def test_same_setup_without_overheated_flag_is_buyable(self):
        # 對照組：其餘條件完全相同，只有 overheated 不同，證明擋下的原因
        # 確實是 overheated，不是其他巧合條件。
        candidate = make_candidate(
            buyAllowed=False, overheated=False, surgeSetup=True, score=95.0,
        )
        result = compute(candidate, make_quote(), INITIAL_WINDOW)
        self.assertTrue(result["canBuy"])


class PerformanceVetoRegressionTests(unittest.TestCase):
    def test_performance_veto_cannot_be_reopened_by_intraday_breakout(self):
        candidate = make_candidate(
            buyAllowed=False,
            performanceVetoed=True,
            surgeSetup=True,
            score=95.0,
        )
        result = compute(candidate, make_quote(), INITIAL_WINDOW)
        self.assertTrue(result["candidatePerformanceVetoed"])
        self.assertFalse(result["formalWatchAllowed"])
        self.assertFalse(result["scheduleAllowed"])
        self.assertFalse(result["canBuy"])
        self.assertTrue(result["shadowFormalWatchAllowed"])
        self.assertTrue(result["shadowScheduleAllowed"])
        self.assertTrue(result["shadowCanBuy"])
        self.assertIn("盤中報價戰績", result["status"])

    def test_danger_risk_remains_blocked_in_shadow_observation(self):
        candidate = make_candidate(
            buyAllowed=False,
            policyBuyAllowed=True,
            performanceVetoed=True,
            surgeSetup=True,
            score=95.0,
            riskFlags=[{
                "code": "long_upper_volume",
                "label": "長上影爆量·疑倒貨",
                "severity": "danger",
            }],
        )
        result = compute(candidate, make_quote(), INITIAL_WINDOW)
        self.assertFalse(result["shadowFormalWatchAllowed"])
        self.assertFalse(result["shadowScheduleAllowed"])
        self.assertFalse(result["shadowCanBuy"])


class EntryGuardrailVetoRegressionTests(unittest.TestCase):
    def test_entry_guardrail_cannot_be_reopened_by_intraday_breakout(self):
        candidate = make_candidate(
            buyAllowed=True,
            entryGuardrailVetoed=True,
            surgeSetup=True,
            score=95.0,
        )
        result = compute(candidate, make_quote(), INITIAL_WINDOW)
        self.assertTrue(result["candidateEntryGuardrailVetoed"])
        self.assertFalse(result["formalWatchAllowed"])
        self.assertFalse(result["scheduleAllowed"])
        self.assertFalse(result["canBuy"])
        self.assertFalse(result["shadowCanBuy"])
        self.assertIn("不追價進場防線", result["status"])

class DangerRiskBypassRegressionTests(unittest.TestCase):
    """回歸測試：高風險型態已被日K掃描降級後，盤中突破/高分不能再洗回可買。"""

    def test_risk_vetoed_candidate_cannot_buy_even_with_breakout_and_high_score(self):
        candidate = make_candidate(
            buyAllowed=False, riskVetoed=True, overheated=False, surgeSetup=True, score=95.0,
            riskFlags=[{"code": "long_upper_volume", "label": "長上影爆量·疑倒貨", "severity": "danger"}],
        )
        result = compute(candidate, make_quote(), INITIAL_WINDOW)
        self.assertTrue(result["dangerRisk"])
        self.assertFalse(result["canBuy"])
        self.assertFalse(result["formalWatchAllowed"])
        self.assertFalse(result["scheduleAllowed"])
        self.assertEqual(result["status"], "高風險型態，只觀察不追")

    def test_danger_risk_flag_cannot_buy_even_without_risk_vetoed_field(self):
        candidate = make_candidate(
            buyAllowed=True, riskVetoed=False, overheated=False, surgeSetup=True, score=95.0,
            riskFlags=[{"code": "limitup_exhaust", "label": "連漲停後爆量打開·高風險", "severity": "danger"}],
        )
        result = compute(candidate, make_quote(), INITIAL_WINDOW)
        self.assertTrue(result["dangerRisk"])
        self.assertFalse(result["canBuy"])
        self.assertFalse(result["formalWatchAllowed"])
        self.assertFalse(result["scheduleAllowed"])
        self.assertEqual(result["status"], "高風險型態，只觀察不追")


class InvalidRadarDecisionBypassRegressionTests(unittest.TestCase):
    def test_invalid_holiday_scan_cannot_be_restored_by_intraday_breakout(self):
        candidate = make_candidate(
            buyAllowed=False,
            recordedBuyAllowed=True,
            policyBuyAllowed=True,
            invalidForTrading=True,
            surgeSetup=True,
            score=99.0,
        )
        result = compute(candidate, make_quote(), INITIAL_WINDOW)
        self.assertTrue(result["candidateInvalidForTrading"])
        self.assertFalse(result["formalWatchAllowed"])
        self.assertFalse(result["scheduleAllowed"])
        self.assertFalse(result["canBuy"])
        self.assertEqual(result["status"], "雷達決策資料無效，僅保留稽核")

class StrictStructureGateTests(unittest.TestCase):
    """盤中可買必須有日線結構，不讓純分數或過熱舊資料補救放行。"""

    def test_high_score_without_daily_structure_cannot_be_promoted_to_buy(self):
        candidate = make_candidate(
            buyAllowed=False, surgeSetup=False, counterTrendStrength=False, score=99.0,
        )
        result = compute(candidate, make_quote(), INITIAL_WINDOW)
        self.assertFalse(result["formalWatchAllowed"])
        self.assertFalse(result["scheduleAllowed"])
        self.assertFalse(result["canBuy"])

    def test_legacy_buy_allowed_row_with_overheated_flag_cannot_buy(self):
        candidate = make_candidate(buyAllowed=True, overheated=True, score=99.0)
        result = compute(candidate, make_quote(), INITIAL_WINDOW)
        self.assertFalse(result["formalWatchAllowed"])
        self.assertFalse(result["scheduleAllowed"])
        self.assertFalse(result["canBuy"])
        self.assertEqual(result["status"], "短線過熱，只觀察不追")

    def test_low_score_cannot_bypass_floor_with_legacy_buy_or_surge(self):
        candidate = make_candidate(
            buyAllowed=True, surgeSetup=True, counterTrendStrength=True, score=59.99,
        )
        result = compute(candidate, make_quote(), INITIAL_WINDOW)
        self.assertTrue(result["scoreFloorBlocked"])
        self.assertFalse(result["formalWatchAllowed"])
        self.assertFalse(result["scheduleAllowed"])
        self.assertFalse(result["canBuy"])
        self.assertIn("未達 60 分", result["status"])


class ChaseProtectionTests(unittest.TestCase):
    """5 日漲多只禁止追突破，保留回測/V 轉這類風報比較佳的進場。"""

    EXTENDED_RUNUP = [{"code": "extended_runup", "label": "5日已漲18%·追高風險", "severity": "warn"}]

    def test_extended_runup_blocks_breakout_chasing(self):
        result = compute(make_candidate(riskFlags=self.EXTENDED_RUNUP), make_quote(), INITIAL_WINDOW)
        self.assertTrue(result["extendedRunup"])
        self.assertTrue(result["chaseBreakoutBlocked"])
        self.assertFalse(result["canBuy"])
        self.assertEqual(result["status"], "5日漲多，不追盤中突破，等回測/V轉")

    def test_extended_runup_still_allows_pullback_entry(self):
        candidate = make_candidate(
            buyTrigger=120.0, pullbackPrice=98.0, stopPrice=90.0, riskFlags=self.EXTENDED_RUNUP,
        )
        quote = make_quote(openPrice=99.0, highPrice=99.5, lowPrice=97.5, currentPrice=98.2, totalVolume=2000.0)
        result = compute(candidate, quote, INITIAL_WINDOW)
        self.assertEqual(result["setupType"], "pullback")
        self.assertFalse(result["chaseBreakoutBlocked"])
        self.assertTrue(result["canBuy"])


class BreakoutConfirmationTests(unittest.TestCase):
    """突破必須由現價守住，且未取得盤中報價時不可發出可買訊號。"""

    def test_high_touch_but_current_below_trigger_is_false_breakout(self):
        quote = make_quote(highPrice=107.0, currentPrice=104.5, lowPrice=100.0)
        result = compute(make_candidate(), quote, INITIAL_WINDOW)
        self.assertTrue(result["breakoutTouched"])
        self.assertFalse(result["breakoutRetained"])
        self.assertTrue(result["falseBreakout"])
        self.assertFalse(result["canBuy"])
        self.assertEqual(result["status"], "突破未守住買點，等待重新站回")

    def test_breakout_far_below_intraday_high_cannot_fall_through_to_pullback(self):
        # 最高衝到 110、現價仍守住 105 觸發價，但已離高點超過 4%。舊邏輯
        # 會因現價也符合 pullback_hold 而改走回測分支，重新洗成可買。
        quote = make_quote(openPrice=100.0, highPrice=110.0, lowPrice=100.0, currentPrice=105.5)
        result = compute(make_candidate(), quote, INITIAL_WINDOW)
        self.assertTrue(result["breakoutTouched"])
        self.assertTrue(result["breakoutRetained"])
        self.assertFalse(result["breakoutNearHigh"])
        self.assertTrue(result["breakoutFadeBlocked"])
        self.assertFalse(result["canBuy"])
        self.assertEqual(result["status"], "突破後離高點過遠，等待重新轉強")

    def test_quote_payload_without_live_confirmation_cannot_buy(self):
        result = compute(make_candidate(), make_quote(), INITIAL_WINDOW, has_quote=False)
        self.assertTrue(result["quoteMissingBlocked"])
        self.assertFalse(result["canBuy"])
        self.assertEqual(result["status"], "等待即時報價確認")

    def test_stale_quote_fallback_cannot_buy_even_when_quote_values_exist(self):
        result = compute(make_candidate(), make_quote(), INITIAL_WINDOW, quote_fresh=False)
        self.assertFalse(result["quoteFresh"])
        self.assertTrue(result["quoteStaleBlocked"])
        self.assertFalse(result["canBuy"])
        self.assertEqual(result["status"], "即時報價已過期，僅觀察")

    def test_global_market_data_freshness_failure_cannot_buy(self):
        result = compute(make_candidate(), make_quote(), INITIAL_WINDOW, market_data_fresh=False)
        self.assertFalse(result["marketDataFresh"])
        self.assertTrue(result["marketDataStaleBlocked"])
        self.assertFalse(result["canBuy"])
        self.assertFalse(result["shadowCanBuy"])
        self.assertEqual(result["status"], "市場日線資料未通過新鮮度檢查，僅觀察")


class CandidateDailyFreshnessTests(unittest.TestCase):
    """即時報價不能把已落後全市場日K的候選洗回可買。"""

    def test_stale_candidate_daily_bar_is_observe_only_even_with_live_quote(self):
        candidate = make_candidate(
            priceDate="2026-07-08",
            dailyDataReferenceDate="2026-07-09",
        )
        result = compute(candidate, make_quote(), INITIAL_WINDOW)
        self.assertEqual(result["candidateDataDate"], "2026-07-08")
        self.assertEqual(result["dailyDataReferenceDate"], "2026-07-09")
        self.assertFalse(result["candidateDailyDataFresh"])
        self.assertTrue(result["candidateDataStaleBlocked"])
        self.assertFalse(result["canBuy"])
        self.assertEqual(result["status"], "候選日線資料非最新，僅觀察")

    def test_candidate_on_latest_daily_bar_remains_eligible_for_intraday_confirmation(self):
        candidate = make_candidate(
            priceDate="2026-07-09",
            dailyDataReferenceDate="2026-07-09",
        )
        result = compute(candidate, make_quote(), INITIAL_WINDOW)
        self.assertTrue(result["candidateDailyDataFresh"])
        self.assertFalse(result["candidateDataStaleBlocked"])
        self.assertTrue(result["canBuy"])


class StopPriceFallbackRegressionTests(unittest.TestCase):
    """回歸測試：stop_price/stopPrice 缺值時要有非零保底(前收盤93%)，
    不能讓停損保護對於缺資料的候選股形同虛設。"""

    def test_missing_stop_price_still_detects_broken_stop(self):
        candidate = make_candidate(stopPrice=None)
        quote = make_quote(currentPrice=90.0, highPrice=92.0, lowPrice=89.0, openPrice=91.0)
        result = compute(candidate, quote, INITIAL_WINDOW)
        self.assertTrue(result["stopBroken"])
        self.assertFalse(result["canBuy"])

    def test_missing_stop_price_does_not_false_trigger_when_price_is_healthy(self):
        candidate = make_candidate(stopPrice=None)
        result = compute(candidate, make_quote(), INITIAL_WINDOW)
        self.assertFalse(result["stopBroken"])
        self.assertTrue(result["canBuy"])


class HasIntradayQuoteRegressionTests(unittest.TestCase):
    """回歸測試：hasIntradayQuote 要真的反映有沒有抓到即時報價，
    不能是永遠 False 的死欄位。"""

    def test_no_quote_marks_has_intraday_quote_false(self):
        result = compute(make_candidate(), {}, INITIAL_WINDOW, has_quote=False)
        self.assertFalse(result["hasIntradayQuote"])

    def test_real_quote_marks_has_intraday_quote_true(self):
        result = compute(make_candidate(), make_quote(), INITIAL_WINDOW, has_quote=True)
        self.assertTrue(result["hasIntradayQuote"])


class PerSymbolQuoteFreshnessTests(unittest.TestCase):
    """混合報價來源時，每檔 stale/fresh 旗標不能被整批 ok 蓋過。"""

    def test_per_symbol_flags_fail_closed_without_breaking_shioaji_compatibility(self):
        now = server.datetime.fromisoformat("2026-07-10T10:00:00+08:00")
        cases = (
            ({"currentPrice": 100, "snapshotAt": "2026-07-10 09:59:30"}, True, True),
            ({"currentPrice": 100, "fresh": True, "snapshotAt": "2026-07-10 09:59:30"}, True, True),
            ({"currentPrice": 100, "fresh": False, "snapshotAt": "2026-07-10 09:59:30"}, True, False),
            ({"currentPrice": 100, "stale": True, "snapshotAt": "2026-07-10 09:59:30"}, True, False),
            ({"currentPrice": 100, "fresh": True, "snapshotAt": "2026-07-10 09:59:30"}, False, False),
            ({"currentPrice": 100}, True, False),
            ({}, True, False),
        )
        for quote, batch_fresh, expected in cases:
            with self.subTest(quote=quote, batch_fresh=batch_fresh):
                self.assertEqual(server.intraday_quote_is_fresh(quote, batch_fresh, now=now), expected)

    def test_same_day_quote_older_than_three_minutes_is_blocked(self):
        fresh, age, reason = server.intraday_quote_freshness(
            {"currentPrice": 100, "snapshotAt": "2026-07-10 09:55:00"},
            True,
            now=server.datetime.fromisoformat("2026-07-10T10:00:00+08:00"),
        )
        self.assertFalse(fresh)
        self.assertEqual(age, 300)
        self.assertEqual(reason, "quote_too_old")


class WindowBlockedPriorityTests(unittest.TestCase):
    """不在進場時段時，就算技術面型態完美成立也不能顯示可買，
    狀態文字要用時間窗自己的 label，不能被型態文字蓋過去。"""

    def test_perfect_breakout_outside_active_window_is_not_buyable(self):
        result = compute(make_candidate(), make_quote(), PREMARKET_WINDOW)
        self.assertFalse(result["canBuy"])
        self.assertTrue(result["windowBlocked"])
        self.assertEqual(result["status"], PREMARKET_WINDOW["label"])

    def test_perfect_setup_after_market_close_is_not_buyable(self):
        result = compute(make_candidate(), make_quote(), CLOSED_WINDOW)
        self.assertFalse(result["canBuy"])
        self.assertEqual(result["status"], "13:15 後不再進場")


class LateBreakoutBlockedTests(unittest.TestCase):
    """10:00 後(dip 階段)不能靠突破型態進場，只能等回測或V轉；
    這條規則要在 setup_ok 已經成立的情況下仍然擋下 canBuy。"""

    def test_breakout_only_setup_in_dip_phase_is_blocked(self):
        candidate = make_candidate(buyTrigger=102.0, pullbackPrice=95.0, stopPrice=90.0)
        quote = make_quote(openPrice=101.0, highPrice=106.0, lowPrice=100.5, currentPrice=105.0, totalVolume=2500.0)
        result = compute(candidate, quote, DIP_WINDOW)
        self.assertEqual(result["setupType"], "breakout")
        self.assertTrue(result["setupOk"])
        self.assertTrue(result["lateBreakoutBlocked"])
        self.assertFalse(result["canBuy"])
        self.assertEqual(result["status"], "10:00 後不追突破，等回測/V轉")


class SafetyValveTests(unittest.TestCase):
    """跳空過高／開高走低這兩個安全閥，即使型態原本會成立也要擋下。"""

    def test_gap_up_over_five_percent_blocks_entry(self):
        candidate = make_candidate()
        quote = make_quote(openPrice=106.5, highPrice=110.0, currentPrice=108.0)  # gap = 6.5%
        result = compute(candidate, quote, INITIAL_WINDOW)
        self.assertTrue(result["tooHigh"])
        self.assertFalse(result["canBuy"])

    def test_open_high_then_fade_blocks_entry(self):
        candidate = make_candidate()
        quote = make_quote(openPrice=103.0, highPrice=104.0, currentPrice=100.0)  # 開高後回落超過1.5%
        result = compute(candidate, quote, INITIAL_WINDOW)
        self.assertTrue(result["openHighFade"])
        self.assertFalse(result["canBuy"])

    def test_wide_bid_ask_spread_blocks_otherwise_valid_breakout(self):
        quote = make_quote(bidPrice=104.0, askPrice=106.0)
        result = compute(make_candidate(), quote, INITIAL_WINDOW)
        self.assertGreater(result["bidAskSpreadPct"], server.MONSTER_MAX_BID_ASK_SPREAD_PCT)
        self.assertTrue(result["spreadBlocked"])
        self.assertFalse(result["canBuy"])
        self.assertEqual(result["status"], "買賣價差過大，僅觀察")

    def test_tight_bid_ask_spread_keeps_breakout_buyable(self):
        quote = make_quote(bidPrice=105.9, askPrice=106.0)
        result = compute(make_candidate(), quote, INITIAL_WINDOW)
        self.assertFalse(result["spreadBlocked"])
        self.assertFalse(result["slippageBlocked"])
        self.assertTrue(result["rewardRiskPassed"])
        self.assertGreater(result["netRewardRiskRatio"], result["minimumNetRewardRiskRatio"])
        self.assertTrue(result["canBuy"])

    def test_breakout_more_than_two_percent_above_trigger_is_not_chased(self):
        candidate = make_candidate(buyTrigger=100.0, stopPrice=93.0)
        quote = make_quote(openPrice=101.0, highPrice=104.2, lowPrice=100.5, currentPrice=104.0)
        result = compute(candidate, quote, INITIAL_WINDOW)
        self.assertEqual(result["setupType"], "breakout")
        self.assertTrue(result["entryDriftBlocked"])
        self.assertGreater(result["entryDriftPct"], result["maximumEntryDriftPct"])
        self.assertFalse(result["canBuy"])
        self.assertIn("不追價", result["status"])

    def test_cost_after_fees_blocks_near_limit_spread_even_before_spread_cap(self):
        quote = make_quote(bidPrice=105.3, askPrice=106.0)
        result = compute(make_candidate(), quote, INITIAL_WINDOW)
        self.assertFalse(result["spreadBlocked"])
        self.assertFalse(result["slippageBlocked"])
        self.assertTrue(result["rewardRiskBlocked"])
        self.assertFalse(result["canBuy"])
        self.assertIn("成本後風報", result["status"])


class IntradayVolumeScheduleTests(unittest.TestCase):
    """累積成交量門檻要一路提高到 13:15，不能 10:00 後永遠停在 0.5 倍。"""

    @staticmethod
    def rule(hour, minute):
        return server.early_session_volume_rule(
            "monster",
            SimpleNamespace(tm_hour=hour, tm_min=minute, tm_sec=0),
        )

    def test_threshold_increases_through_initial_and_dip_sessions(self):
        self.assertAlmostEqual(self.rule(9, 30)["min"], 0.25, places=4)
        self.assertAlmostEqual(self.rule(10, 0)["min"], 0.50, places=4)
        self.assertGreater(self.rule(11, 30)["min"], 0.50)
        self.assertAlmostEqual(self.rule(13, 15)["min"], 0.85, places=4)

    def test_threshold_stays_capped_after_entry_window(self):
        self.assertAlmostEqual(self.rule(13, 30)["min"], 0.85, places=4)


class StockSpecificVolumeProfileTests(unittest.TestCase):
    def test_five_days_of_profiles_replace_generic_session_curve(self):
        rows = []
        for day in range(1, 6):
            date = f"2026-07-{day:02d}"
            rows.extend([
                {"symbol": "2330", "date": date, "minute": "10:00", "cumulative_volume_lots": 400},
                {"symbol": "2330", "date": date, "minute": "13:25", "cumulative_volume_lots": 1000},
            ])
        fallback = {"min": 0.5, "max": None, "progress": 0.4, "label": "共用", "source": "session_curve"}
        rules = server.build_stock_intraday_volume_rules(rows, ["2330"], "10:00", fallback)
        self.assertEqual(rules["2330"]["source"], "stock_5m_profile")
        self.assertEqual(rules["2330"]["profileSamples"], 5)
        self.assertAlmostEqual(rules["2330"]["expectedFraction"], 0.4, places=4)
        self.assertAlmostEqual(rules["2330"]["min"], 0.36, places=4)

    def test_less_than_five_days_keeps_generic_rule(self):
        rows = [
            {"symbol": "2330", "date": f"2026-07-0{day}", "minute": "10:00", "cumulative_volume_lots": 400}
            for day in range(1, 5)
        ]
        fallback = {"min": 0.5, "progress": 0.4, "label": "共用", "source": "session_curve"}
        self.assertEqual(
            server.build_stock_intraday_volume_rules(rows, ["2330"], "10:00", fallback),
            {},
        )


if __name__ == "__main__":
    unittest.main()
