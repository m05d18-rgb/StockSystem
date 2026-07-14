"""
交易複盤日誌(compute_trade_journal)的回歸測試。

把已平倉交易(round-trip)彙總成複盤統計：勝率、平均抱幾天、總/平均損益，以及
最有價值的洞察——「跟系統建議買的勝率 vs 自己另外買的勝率」。純計算、無 DB。

執行方式：
  python -m unittest tests.test_trade_journal -v
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server as server_module


def _trade(symbol, buy, sell, shares=1000, buy_date="2026-06-01", sell_date="2026-06-05", pnl=None):
    return {"symbol": symbol, "buyPrice": buy, "sellPrice": sell, "shares": shares,
            "buyDate": buy_date, "sellDate": sell_date, "pnl": pnl}


class ComputeTradeJournalTests(unittest.TestCase):
    def test_empty_records(self):
        result = server_module.compute_trade_journal([])
        self.assertEqual(result["trades"], [])
        s = result["summary"]
        self.assertEqual(s["count"], 0)
        self.assertIsNone(s["winRate"])
        self.assertEqual(s["totalPnl"], 0)

    def test_pnl_autocomputed_when_missing(self):
        # 現價110 買100 1張 → (110-100)*1000 = +10000
        result = server_module.compute_trade_journal([_trade("2330", 100, 110)])
        t = result["trades"][0]
        self.assertEqual(t["pnl"], 10000.0)
        self.assertEqual(t["pnlPct"], 10.0)
        self.assertEqual(t["holdDays"], 4)  # 06-01 → 06-05

    def test_explicit_pnl_used_over_computed(self):
        # 明確給 pnl(含手續費)就用它，不用毛額 (sell-buy)*shares
        result = server_module.compute_trade_journal([_trade("2330", 100, 110, pnl=9500)])
        self.assertEqual(result["trades"][0]["pnl"], 9500.0)

    def test_win_rate_and_totals(self):
        records = [
            _trade("A", 100, 110),   # +10000 win
            _trade("B", 100, 90),    # -10000 loss
            _trade("C", 50, 55),     # +5000 win
        ]
        s = server_module.compute_trade_journal(records)["summary"]
        self.assertEqual(s["count"], 3)
        self.assertEqual(s["wins"], 2)
        self.assertEqual(s["losses"], 1)
        self.assertEqual(s["winRate"], round(2 / 3, 4))
        self.assertEqual(s["totalPnl"], 5000.0)  # 10000-10000+5000
        self.assertEqual(s["avgHoldDays"], 4.0)

    def test_followed_vs_not_followed_win_rate(self):
        # 核心洞察：跟系統建議(A,B)買的勝率 vs 自己另外買(C,D)的勝率
        records = [
            _trade("A", 100, 110),   # 跟單 win
            _trade("B", 100, 105),   # 跟單 win
            _trade("C", 100, 90),    # 自己 loss
            _trade("D", 100, 95),    # 自己 loss
        ]
        result = server_module.compute_trade_journal(records, recommended_symbols={"A", "B"})
        s = result["summary"]
        self.assertEqual(s["followedSystemCount"], 2)
        self.assertEqual(s["followedSystemRate"], 0.5)
        self.assertEqual(s["followedWinRate"], 1.0, "跟系統建議買的2筆全賺")
        self.assertEqual(s["notFollowedWinRate"], 0.0, "自己另外買的2筆全賠")
        # 逐筆的 followedSystem 標記正確
        by_symbol = {t["symbol"]: t["followedSystem"] for t in result["trades"]}
        self.assertTrue(by_symbol["A"])
        self.assertFalse(by_symbol["C"])

    def test_bad_dates_give_none_hold_days_not_crash(self):
        result = server_module.compute_trade_journal([
            _trade("X", 100, 110, buy_date="", sell_date="bad"),
        ])
        self.assertIsNone(result["trades"][0]["holdDays"])
        self.assertEqual(result["summary"]["avgHoldDays"], None)

    def test_zero_buy_price_gives_none_pct_no_divzero(self):
        result = server_module.compute_trade_journal([_trade("X", 0, 110, pnl=500)])
        self.assertIsNone(result["trades"][0]["pnlPct"])
        self.assertEqual(result["trades"][0]["pnl"], 500.0)


if __name__ == "__main__":
    unittest.main()
