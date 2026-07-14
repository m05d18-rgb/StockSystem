"""大盤紅綠燈『即時月線』回歸測試(2026-07-09 使用者「大盤要即時」)。

market_status(live_price=X) 應用即時加權價 vs 月線(20日均線)重算「站上/跌破月線」,
而非用昨天日線收盤。live_price=None 時完全退回日線行為(graceful)。
用 monkeypatch 合成 TAIEX 資料,完全不碰真實 DB(避免污染)。
"""
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ml_backend import backend, MarketContext


def make_taiex(closes, start_day=2):
    return {"TAIEX": [{"date": f"2026-06-{start_day + i:02d}", "close": c}
                      for i, c in enumerate(closes)]}


class TestMarketStatusLive(unittest.TestCase):
    def setUp(self):
        # 25 根日K:前 24 根收 100、最後一根收 98(略低於月線≈99.9)→ 日線紅燈
        self.rows = make_taiex([100.0] * 24 + [98.0])
        self.latest = "2026-06-26"

    def test_ma_value_correct(self):
        ctx = MarketContext(self.rows)
        # 月線 = 最近 20 根收盤均值 = (19*100 + 98)/20 = 99.9
        self.assertAlmostEqual(ctx.ma_value("TAIEX", self.latest, 20), 99.9, places=6)

    def test_ma_value_insufficient_history(self):
        ctx = MarketContext(make_taiex([100.0] * 10))
        self.assertEqual(ctx.ma_value("TAIEX", "2026-06-11", 20), 0.0)

    def test_no_live_price_falls_back_to_daily_red(self):
        with patch.object(backend, "load_market_rows", return_value=self.rows):
            s = backend.market_status()  # 不給即時價 = 日線
        self.assertTrue(s["ok"])
        self.assertFalse(s["liveGate"])
        self.assertFalse(s["allowBuy"])
        self.assertEqual(s["light"], "red")
        self.assertFalse(s["taiexAboveMonthLine"])

    def test_live_above_month_flips_red_to_yellow(self):
        with patch.object(backend, "load_market_rows", return_value=self.rows):
            s = backend.market_status(live_price=105.0)  # 即時站上月線(99.9)
        self.assertTrue(s["liveGate"])
        self.assertTrue(s["allowBuy"])
        self.assertTrue(s["taiexAboveMonthLine"])
        self.assertEqual(s["light"], "yellow")  # regime 震盪 → 黃(非綠)
        self.assertGreater(s["taiexMaGapPct"], 0)

    def test_live_below_month_stays_red(self):
        with patch.object(backend, "load_market_rows", return_value=self.rows):
            s = backend.market_status(live_price=95.0)  # 即時跌破月線
        self.assertTrue(s["liveGate"])
        self.assertFalse(s["allowBuy"])
        self.assertEqual(s["light"], "red")
        self.assertLess(s["taiexMaGapPct"], 0)

    def test_none_matches_market_gate_exactly(self):
        # 回歸:不給即時價時,allowBuy/hotMarket/站上月線 應與 market_gate(日線) 完全一致
        with patch.object(backend, "load_market_rows", return_value=self.rows):
            market = backend.market_features(self.latest, MarketContext(self.rows), 0.0)
            gate = backend.market_gate(market)
            s = backend.market_status()
        self.assertEqual(s["allowBuy"], gate["allowBuy"])
        self.assertEqual(s["hotMarket"], gate["hotMarket"])
        self.assertEqual(s["taiexAboveMonthLine"], gate["taiexAboveMonthLine"])

    def test_insufficient_history_ignores_live_price(self):
        rows = make_taiex([100.0] * 10)  # 不足 20 根 → ma_value=0 → 即時分支跳過
        with patch.object(backend, "load_market_rows", return_value=rows):
            s = backend.market_status(live_price=999.0)
        self.assertFalse(s["liveGate"])  # 沒重算,退回日線(不因拿不到月線就誤判)

    def test_invalid_live_price_ignored(self):
        with patch.object(backend, "load_market_rows", return_value=self.rows):
            s_none = backend.market_status()
            s_bad = backend.market_status(live_price="abc")  # 非數字
            s_zero = backend.market_status(live_price=0)      # 0 不合法
        self.assertFalse(s_bad["liveGate"])
        self.assertFalse(s_zero["liveGate"])
        self.assertEqual(s_bad["light"], s_none["light"])     # 退回日線,與不給時一致


if __name__ == "__main__":
    unittest.main()
