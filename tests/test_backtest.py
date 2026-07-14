"""
backtest.py 的回歸測試，對應今天修的兩個防禦：
  1. 隔日開盤價 <=0 時不進場（避免除以接近 0 的進場價產生 inf）。
  2. 持倉期間某天收盤價 <=0 時跳過出場判斷，不拿壞資料算報酬率。
  3. report() 對非有限值(inf/nan)的最後一道防線。
"""
import math
import os
import sys
import unittest

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backtest import simulate_trades, report


def make_row(date, close, ma5=None, ma20=None, ma60=None, macd=1.0, rsi=55.0, vol_ratio=1.0,
             high=None, low=None, open_=None, volume=1_000_000,
             ret5=0.0, ret20=0.0, change1=0.0, high20=None, vol_ratio_exit=None):
    # 預設值不會觸發妖股進場（ret5=0 不滿足 5 日急漲、high20 遠高於收盤價）；
    # 要觸發訊號的列用 surge_row() 或明確傳入動能欄位。
    # vol_ratio_exit(前20日均量、不含當日) 預設等於 vol_ratio：合成測試資料
    # 沒有真實逐日量能序列可算差異，兩者用同一個值不影響既有測試案例的
    # 出場判斷結果，只有量縮出場相關測試會明確傳入不同值。
    return {
        "symbol": "TEST",
        "date": date,
        "open": open_ if open_ is not None else close,
        "high": high if high is not None else close * 1.02,
        "low": low if low is not None else close * 0.98,
        "close": close,
        "volume": volume,
        "ma5": ma5 if ma5 is not None else close * 0.98,
        "ma20": ma20 if ma20 is not None else close * 0.95,
        "ma60": ma60 if ma60 is not None else close * 0.90,
        "rsi": rsi,
        "macd": macd,
        "atr": close * 0.02,
        "vol20": volume,
        "vol_ratio": vol_ratio,
        "vol_ratio_exit": vol_ratio_exit if vol_ratio_exit is not None else vol_ratio,
        "ret5": ret5,
        "ret20": ret20,
        "change1": change1,
        "high20": high20 if high20 is not None else close * 1.10,
    }


def surge_row(date, close, **kwargs):
    """符合妖股飆股進場條件的一列：突破 20 日高、5/20 日急漲、放量、強於大盤。"""
    defaults = dict(
        ret5=8.0, ret20=12.0, change1=2.0, vol_ratio=1.6, rsi=60.0,
        high20=close * 0.99, ma5=close * 0.98, ma20=close * 0.95,
        volume=1_000_000,
    )
    defaults.update(kwargs)
    return make_row(date, close, **defaults)


def empty_market_series(dates):
    ser = pd.Series([100.0] * len(dates), index=dates)
    ma20 = pd.Series([95.0] * len(dates), index=dates)
    return ser, ma20


class ZeroPriceDefenseTests(unittest.TestCase):
    def _build_sdf(self, rows):
        return pd.DataFrame(rows)

    def test_does_not_enter_when_next_day_open_is_zero(self):
        rows = []
        # 前 80 天填充讓迴圈的 range(80, n-1) 有得跑(對齊 ml_backend.py
        # build_features_for_rows 至少80筆才產生特徵的門檻)，且不觸發買進訊號
        for i in range(80):
            rows.append(make_row(f"d{i}", 100.0))
        # 第 80 天觸發妖股買進訊號（突破 20 日高、5/20 日急漲、放量、強於大盤）
        rows.append(surge_row("signal_day", 110.0))
        # 隔日開盤價異常為 0 —— 不該進場
        rows.append(make_row("bad_open_day", 111.0, open_=0.0))
        rows.append(make_row("final_day", 112.0))
        sdf = self._build_sdf(rows)
        dates = sdf["date"].tolist()
        mkt_ser, mkt_ma20 = empty_market_series(dates)
        trades = simulate_trades(sdf, mkt_ser, mkt_ma20)
        # 不應該有任何一筆交易是用 0 元進場算出來的
        for trade in trades:
            self.assertGreater(trade["entry_price"], 0)

    def test_skips_exit_evaluation_when_close_is_zero_during_holding(self):
        rows = []
        for i in range(80):
            rows.append(make_row(f"d{i}", 100.0))
        rows.append(surge_row("signal_day", 110.0))
        rows.append(make_row("entry_next_day", 111.0))
        # 持倉期間某天收盤價異常為 0
        rows.append(make_row("broken_day", 0.0))
        rows.append(make_row("recovered_day", 115.0))
        sdf = self._build_sdf(rows)
        dates = sdf["date"].tolist()
        mkt_ser, mkt_ma20 = empty_market_series(dates)
        trades = simulate_trades(sdf, mkt_ser, mkt_ma20)
        for trade in trades:
            self.assertTrue(math.isfinite(trade["gain_pct_net"]))
            self.assertGreater(trade["exit_price"], 0)


class SurgeEntryTests(unittest.TestCase):
    def test_surge_signal_enters_and_stops_out(self):
        # 妖股訊號日 → 隔日開盤進場 → 跌破 -7% 停損，完整走完一筆交易
        rows = [make_row(f"d{i}", 100.0) for i in range(80)]
        rows.append(surge_row("signal_day", 110.0))
        rows.append(make_row("entry_day", 111.0, open_=111.0))
        rows.append(make_row("crash_day", 100.0))   # 相對進場價 -10%，觸發停損
        rows.append(make_row("final_day", 99.0))
        sdf = pd.DataFrame(rows)
        mkt_ser, mkt_ma20 = empty_market_series(sdf["date"].tolist())
        trades = simulate_trades(sdf, mkt_ser, mkt_ma20)
        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0]["exit_reason"], "stop_loss")
        self.assertAlmostEqual(trades[0]["entry_price"], 111.0 * 1.001, places=2)

    def test_does_not_chase_large_gap_open(self):
        # 隔日開盤跳空 +6%（> 5% 上限）不追價
        rows = [make_row(f"d{i}", 100.0) for i in range(80)]
        rows.append(surge_row("signal_day", 110.0))
        rows.append(make_row("gap_day", 117.0, open_=116.6))  # 開盤 +6%
        rows.append(make_row("final_day", 118.0))
        sdf = pd.DataFrame(rows)
        mkt_ser, mkt_ma20 = empty_market_series(sdf["date"].tolist())
        trades = simulate_trades(sdf, mkt_ser, mkt_ma20)
        self.assertEqual(trades, [])

    def test_vol_weak_exit_uses_vol_ratio_exit_not_entry_side_vol_ratio(self):
        # vol_weak(量縮出場)要用 vol_ratio_exit(前20日均量、不含當日)，
        # 不能誤用進場端含當日的 vol_ratio——這裡刻意讓兩者給出相反的
        # 量縮判斷結果(vol_ratio=1.6 不量縮、vol_ratio_exit=0.5 量縮)，
        # 驗證出場邏輯真的讀對了欄位。
        rows = [make_row(f"d{i}", 100.0) for i in range(80)]
        rows.append(surge_row("signal_day", 110.0))
        rows.append(make_row("entry_day", 111.0, open_=111.0))
        # 持有第2天漲到+7%(在5~10%區間)，vol_ratio=1.6(不量縮) 但
        # vol_ratio_exit=0.5(量縮)：若出場邏輯誤用 vol_ratio 就不會出場，
        # 誤用 vol_ratio_exit 才會觸發 take_profit_5pct_vol_weak。
        rows.append(make_row("gain_day", 118.8888, vol_ratio=1.6, vol_ratio_exit=0.5))
        rows.append(make_row("final_day", 119.0))
        sdf = pd.DataFrame(rows)
        mkt_ser, mkt_ma20 = empty_market_series(sdf["date"].tolist())
        trades = simulate_trades(sdf, mkt_ser, mkt_ma20)
        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0]["exit_reason"], "take_profit_5pct_vol_weak")


class ReportNanDefenseTests(unittest.TestCase):
    def test_all_win_trades_do_not_print_nan_average_loss(self):
        df = pd.DataFrame([
            {"symbol": "A", "entry_date": "2026-05-01", "exit_date": "2026-05-04", "entry_price": 10, "exit_price": 11,
             "hold_days": 3, "gain_pct_raw": 10.0, "gain_pct_net": 9.5, "exit_reason": "take_profit_10pct", "win": True},
            {"symbol": "B", "entry_date": "2026-05-01", "exit_date": "2026-05-03", "entry_price": 10, "exit_price": 11,
             "hold_days": 2, "gain_pct_raw": 12.0, "gain_pct_net": 11.5, "exit_reason": "take_profit_10pct", "win": True},
        ])
        import io
        import contextlib
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            report(df)
        output = buffer.getvalue()
        self.assertNotIn("nan", output.lower())
        self.assertIn("平均虧損", output)

    def test_all_loss_trades_do_not_print_nan_average_win(self):
        df = pd.DataFrame([
            {"symbol": "A", "entry_date": "2026-05-01", "exit_date": "2026-05-04", "entry_price": 10, "exit_price": 9,
             "hold_days": 3, "gain_pct_raw": -10.0, "gain_pct_net": -10.5, "exit_reason": "stop_loss", "win": False},
        ])
        import io
        import contextlib
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            report(df)
        output = buffer.getvalue()
        self.assertNotIn("nan", output.lower())
        self.assertIn("平均獲利", output)


class ReportDefenseTests(unittest.TestCase):
    def test_drops_non_finite_trades_before_computing_stats(self):
        df = pd.DataFrame([
            {"symbol": "A", "entry_date": "2026-05-01", "exit_date": "2026-05-04", "entry_price": 10, "exit_price": 11,
             "hold_days": 3, "gain_pct_raw": 10.0, "gain_pct_net": 9.5, "exit_reason": "take_profit_10pct", "win": True},
            {"symbol": "B", "entry_date": "2026-05-01", "exit_date": "2026-05-03", "entry_price": 0.0001, "exit_price": 5,
             "hold_days": 2, "gain_pct_raw": float("inf"), "gain_pct_net": float("inf"), "exit_reason": "stop_loss", "win": True},
            {"symbol": "C", "entry_date": "2026-05-01", "exit_date": "2026-05-02", "entry_price": 10, "exit_price": 9,
             "hold_days": 1, "gain_pct_raw": -10.0, "gain_pct_net": -10.5, "exit_reason": "stop_loss", "win": False},
        ])
        # report() 只印報告不回傳值，這裡驗證呼叫本身不會因為 inf 而拋出例外
        # (numpy 對 inf 的比較/運算在統計函式裡不會拋錯，但會汙染結果，
        # 所以重點是它印出「已排除」的警告，而不是讓整份報告變成 inf/nan)
        import io
        import contextlib
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            report(df)
        output = buffer.getvalue()
        self.assertIn("已從統計中排除", output)  # 有交代排除了什麼、為什麼排除
        # 排除後的統計數字本身不該是畸形的 inf%/nan（警告訊息本身提到
        # "inf/nan" 是在說明原因，不算格式輸出，這裡只檢查數字欄位）
        for line in output.splitlines():
            if "已從統計中排除" in line:
                continue
            self.assertNotRegex(line, r"[+-]?inf%?")
            self.assertNotRegex(line, r"\bnan\b")


if __name__ == "__main__":
    unittest.main()
