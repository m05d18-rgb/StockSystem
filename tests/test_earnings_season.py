"""
財報季避雷(earnings_season_warning)的回歸測試。

台股法定財報申報截止固定(第一季報5/15、半年報8/14、第三季報11/14、年報3/31)，
月營收每月10日前公布。這幾個窗口是財報/營收跳空密集期，短線持股要提前知道。
純日曆判定、不打網路——屬全市場層級警示(台股無可靠的個股前瞻財報公告日資料)。

執行方式：
  python -m unittest tests.test_earnings_season -v
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server as server_module


class EarningsSeasonWarningTests(unittest.TestCase):
    def _types(self, today):
        result = server_module.earnings_season_warning(today)
        return {w["type"] for w in result["warnings"]}, result

    def test_days_before_quarterly_deadline_warns(self):
        # 5/10：距第一季報截止5/15 剩5天(<=10)→季報警示
        types, result = self._types("2026-05-10")
        self.assertTrue(result["active"])
        self.assertIn("quarterly", types)
        q = next(w for w in result["warnings"] if w["type"] == "quarterly")
        self.assertEqual(q["label"], "第一季財報")
        self.assertEqual(q["daysUntil"], 5)

    def test_far_from_any_deadline_and_mid_month_is_silent(self):
        # 2/15：最近截止3/31剩44天(>10)、非1~10日→無警示
        types, result = self._types("2026-02-15")
        self.assertFalse(result["active"])
        self.assertEqual(types, set())

    def test_first_ten_days_of_month_warns_monthly_revenue(self):
        # 2/05：距3/31很遠(無季報)，但是月初1~10日→月營收警示
        types, result = self._types("2026-02-05")
        self.assertIn("monthly_revenue", types)
        self.assertNotIn("quarterly", types)

    def test_both_warnings_when_deadline_near_and_early_month(self):
        # 8/10：距半年報8/14剩4天 + 月初1~10日→季報+月營收兩者都有
        types, result = self._types("2026-08-10")
        self.assertIn("quarterly", types)
        self.assertIn("monthly_revenue", types)
        q = next(w for w in result["warnings"] if w["type"] == "quarterly")
        self.assertEqual(q["label"], "半年報")

    def test_year_rollover_picks_next_year_deadline(self):
        # 12/20：今年11/14已過，最近截止是明年3/31(剩約101天>10)→無季報；
        # 非1~10日→無月營收。確認跨年找「今天之後最近」不會挑到過去的日期。
        types, result = self._types("2026-12-20")
        self.assertFalse(result["active"])

    def test_invalid_date_returns_inactive(self):
        result = server_module.earnings_season_warning("not-a-date")
        self.assertFalse(result["active"])
        self.assertEqual(result["warnings"], [])


if __name__ == "__main__":
    unittest.main()
