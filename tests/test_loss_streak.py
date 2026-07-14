"""
連續虧損熔斷 compute_loss_streak() / loss_streak_status() 的回歸測試。

功能C：短線散戶連續踩雷時最容易情緒化加碼凹單。從最近一筆已平倉交易往回數
連續虧損筆數，達門檻(預設3)就觸發熔斷警示。跟④交易複盤日誌共用同一份真實
已平倉交易資料源，trades 表空時 hasData=False/streak=0，有真實交易就自動生效。

執行方式：
  python -m unittest tests.test_loss_streak -v
"""
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server


def _rec(pnl=None, buy=100.0, sell=None, shares=1000):
    return {"symbol": "9999", "buyPrice": buy, "sellPrice": sell, "shares": shares, "pnl": pnl}


class ComputeLossStreakTests(unittest.TestCase):
    def test_no_trades_is_ok(self):
        r = server.compute_loss_streak([])
        self.assertEqual(r["streak"], 0)
        self.assertEqual(r["level"], "ok")
        self.assertFalse(r["tripped"])

    def test_most_recent_win_breaks_streak(self):
        # 由新到舊：最近一筆是賺的 → streak=0，即使之前有虧損
        records = [_rec(pnl=500), _rec(pnl=-300), _rec(pnl=-200)]
        r = server.compute_loss_streak(records)
        self.assertEqual(r["streak"], 0)
        self.assertEqual(r["level"], "ok")

    def test_two_consecutive_losses_is_caution(self):
        records = [_rec(pnl=-100), _rec(pnl=-200), _rec(pnl=300)]
        r = server.compute_loss_streak(records)
        self.assertEqual(r["streak"], 2)
        self.assertEqual(r["level"], "caution")
        self.assertFalse(r["tripped"])

    def test_three_consecutive_losses_trips_circuit(self):
        records = [_rec(pnl=-100), _rec(pnl=-200), _rec(pnl=-50), _rec(pnl=400)]
        r = server.compute_loss_streak(records)
        self.assertEqual(r["streak"], 3)
        self.assertTrue(r["tripped"])
        self.assertEqual(r["level"], "circuit")
        self.assertEqual(r["recentLossCount"], 3)

    def test_streak_stops_at_first_non_loss(self):
        # 連虧2 → 遇到打平(pnl=0，非虧損) → 停止；再往回的虧損不計入
        records = [_rec(pnl=-100), _rec(pnl=-200), _rec(pnl=0), _rec(pnl=-999)]
        r = server.compute_loss_streak(records)
        self.assertEqual(r["streak"], 2)

    def test_pnl_none_computed_from_prices(self):
        # pnl 缺 → 用 (sell-buy)*shares 補算；賣94買100=虧
        records = [_rec(pnl=None, buy=100, sell=94, shares=1000),
                   _rec(pnl=None, buy=100, sell=95, shares=1000),
                   _rec(pnl=None, buy=100, sell=110, shares=1000)]
        r = server.compute_loss_streak(records)
        self.assertEqual(r["streak"], 2, "前兩筆算出來是虧，第三筆賺→streak停在2")

    def test_custom_threshold(self):
        records = [_rec(pnl=-1), _rec(pnl=-1)]
        self.assertEqual(server.compute_loss_streak(records, threshold=2)["level"], "circuit")
        self.assertTrue(server.compute_loss_streak(records, threshold=2)["tripped"])


class _ConnCtx:
    def __init__(self, rows):
        self._rows = rows

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    class _Cur:
        def __init__(self, rows):
            self._rows = rows

        def fetchall(self):
            return self._rows

    def execute(self, *a, **k):
        return _ConnCtx._Cur(self._rows)


class LossStreakStatusTests(unittest.TestCase):
    def _rows(self, pnls):
        # 模擬 sqlite Row：用 dict（server 以 row["col"] 存取）
        out = []
        for i, pnl in enumerate(pnls):
            out.append({"symbol": "9999", "price": 100.0, "shares": 1000,
                        "exit_price": 100.0 + (pnl / 1000.0), "exit_at": f"2026-07-0{i+1} 10:00:00", "pnl": pnl})
        return out

    def test_empty_trades_has_no_data(self):
        with patch.object(server.backend, "connect", return_value=_ConnCtx([])):
            r = server.loss_streak_status()
        self.assertTrue(r["ok"])
        self.assertFalse(r["hasData"])
        self.assertEqual(r["streak"], 0)
        self.assertEqual(r["level"], "ok")

    def test_circuit_level_advice(self):
        with patch.object(server.backend, "connect", return_value=_ConnCtx(self._rows([-100, -200, -50]))):
            r = server.loss_streak_status()
        self.assertTrue(r["hasData"])
        self.assertEqual(r["level"], "circuit")
        self.assertIn("熔斷", r["advice"])

    def test_caution_level_advice(self):
        with patch.object(server.backend, "connect", return_value=_ConnCtx(self._rows([-100, -200]))):
            r = server.loss_streak_status()
        self.assertEqual(r["level"], "caution")
        self.assertIn("縮小部位", r["advice"])

    def test_db_error_returns_not_ok(self):
        with patch.object(server.backend, "connect", side_effect=RuntimeError("db locked")):
            r = server.loss_streak_status()
        self.assertFalse(r["ok"])
        self.assertIn("讀取交易記錄失敗", r["error"])


if __name__ == "__main__":
    unittest.main()
