"""
妖股候選複盤看板 compute_candidate_followthrough 的回歸測試。

功能H：用 prices 直算雷達候選掃描後 10 交易日實際走勢(曾摸+10% / 第10日收盤達標 /
分群),補雷達戰績看板(predictions.hit 未回填)的空窗。核心邏輯用 in-memory DB 完全隔離、
確定性驗證,不碰正式資料庫。誠實收斂:未滿10日不進命中分母、分群樣本不足只標明不下比率。

執行方式：
  python -m unittest tests.test_candidate_followthrough -v
"""
import os
import sqlite3
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ml_backend
from ml_backend import backend, MONSTER_TARGET_HORIZON_DAYS


def _mem(scores, prices):
    """scores: (scan_date,symbol,price_date,close,score,action,surge_setup,volume_ratio)
       prices: (symbol,date,close,high)"""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("""CREATE TABLE monster_scores (scan_date TEXT, symbol TEXT, price_date TEXT,
        close REAL, score REAL, action TEXT, surge_setup INTEGER, volume_ratio REAL,
        invalid_for_trading INTEGER DEFAULT 0)""")
    conn.execute("CREATE TABLE prices (symbol TEXT, date TEXT, close REAL, high REAL)")
    conn.execute("CREATE TABLE stock_info (symbol TEXT, name TEXT)")
    conn.executemany("""
        INSERT INTO monster_scores (
            scan_date, symbol, price_date, close, score, action, surge_setup, volume_ratio
        ) VALUES (?,?,?,?,?,?,?,?)
    """, scores)
    conn.executemany("INSERT INTO prices VALUES (?,?,?,?)", prices)
    conn.commit()
    return conn


def _price_series(symbol, start_date_ord, entry, values):
    """values: list of (close, high) for consecutive days starting at start_date_ord.
    以簡單遞增日期 2099-MM-DD 生成(足夠隔離、不碰真實日期)。"""
    rows = []
    for i, (cl, hi) in enumerate(values):
        day = start_date_ord + i
        date = f"2099-01-{day:02d}"
        rows.append((symbol, date, cl, hi))
    return rows


class CandidateFollowthroughTests(unittest.TestCase):
    H = MONSTER_TARGET_HORIZON_DAYS  # 10

    def _run(self, scores, prices, lookback=60, min_group=8):
        conn = _mem(scores, prices)
        try:
            return backend._candidate_followthrough_from_conn(conn, lookback, min_group)
        finally:
            conn.close()

    def test_matured_hit_close(self):
        # 候選 price_date=01-01，之後 10 個交易日收盤漲到 >+10% → hitClose=1、settled=1
        entry = 100.0
        # day0=entry, day1..10 逐步漲到 115
        vals = [(entry, entry)] + [(100 + i * 1.6, 100 + i * 1.6) for i in range(1, 11)]  # day10 close=116
        prices = _price_series("A", 1, entry, vals)
        scores = [("2099-01-01", "A", "2099-01-01", entry, 80, "NEXT_DAY_WATCH", 0, 3.0)]
        r = self._run(scores, prices)
        self.assertEqual(r["overall"]["settled"], 1)
        self.assertEqual(r["overall"]["hitCloseRate"], 1.0)
        self.assertEqual(r["overall"]["touched10pctRate"], 1.0)

    def test_touched_but_close_fell_back(self):
        # 10日內 high 曾摸 +12%，但第10日收盤只 +3% → touched10pct=1 但 hitClose=0
        entry = 100.0
        vals = [(entry, entry)]
        for i in range(1, 6):
            vals.append((100 + i, 100 + i * 2.4))  # highs climb, day5 high=112(+12%)
        for i in range(6, 11):
            vals.append((103, 104))  # close settles at 103 (+3%)
        prices = _price_series("A", 1, entry, vals)
        scores = [("2099-01-01", "A", "2099-01-01", entry, 80, "WAIT", 1, 6.0)]
        r = self._run(scores, prices)
        obs_settled = r["overall"]["settled"]
        self.assertEqual(obs_settled, 1)
        self.assertEqual(r["overall"]["touched10pctRate"], 1.0, "曾摸+10%")
        self.assertEqual(r["overall"]["hitCloseRate"], 0.0, "第10日收盤未達標")

    def test_immature_not_in_denominator(self):
        # 只有 6 天 price 資料(不滿10日) → settled=0，但仍算 topFollowThrough 若曾摸+10%
        entry = 100.0
        vals = [(entry, entry)] + [(100 + i * 3, 100 + i * 3) for i in range(1, 6)]  # day5 close=115(+15%)
        prices = _price_series("A", 1, entry, vals)
        scores = [("2099-01-01", "A", "2099-01-01", entry, 80, "NEXT_DAY_WATCH", 0, 3.0)]
        r = self._run(scores, prices)
        self.assertEqual(r["overall"]["settled"], 0, "不滿10日不進命中分母")
        self.assertEqual(len(r["topFollowThrough"]), 1, "但曾摸+10%要進實例展示")
        self.assertFalse(r["topFollowThrough"][0]["matured"])
        self.assertGreaterEqual(r["topFollowThrough"][0]["maxFavorable"], 0.10)

    def test_dirty_close_skipped(self):
        # entry close=0 的髒列靜默跳過、不炸
        prices = _price_series("A", 1, 0, [(0, 0)] + [(10, 10)] * 10)
        scores = [("2099-01-01", "A", "2099-01-01", 0, 80, "WAIT", 0, 1.0)]
        r = self._run(scores, prices)
        self.assertEqual(r["overall"]["candidatesInWindow"], 0)

    def test_group_insufficient_sample(self):
        # 一檔成熟候選(surgeSetup) → surge 群 sample=1 < min_group(8) → insufficientSample=true
        entry = 100.0
        vals = [(entry, entry)] + [(100 + i * 1.6, 100 + i * 1.6) for i in range(1, 11)]
        prices = _price_series("A", 1, entry, vals)
        scores = [("2099-01-01", "A", "2099-01-01", entry, 80, "NEXT_DAY_WATCH", 1, 6.0)]
        r = self._run(scores, prices, min_group=8)
        surge = r["groups"]["bySurgeSetup"]["surge"]
        self.assertTrue(surge["insufficientSample"])
        self.assertIsNone(surge["touched10pctRate"])
        self.assertEqual(surge["sample"], 1)

    def test_empty_monster_scores(self):
        r = self._run([], [])
        self.assertTrue(r["ok"])
        self.assertEqual(r["overall"]["settled"], 0)
        self.assertEqual(r["topFollowThrough"], [])

    def test_hit_definition_matches_prediction_outcomes(self):
        # 2026-07-05 起兩者是「不同」定義:compute_prediction_outcomes 的 hit 改為「窗內
        # 任一天收盤達標」(對齊訓練標籤停利),followthrough 的 hitClose 仍是「第 N 天收盤
        # 達標」。這個單調上漲案例兩種定義都會達標,所以 hitCloseRate 仍等於 hit——當
        # 「明確會命中」的 sanity check 用(中途達標後回落的分歧情境另由 pipeline 測試覆蓋)。
        entry = 100.0
        vals = [(entry, entry)] + [(100 + i * 1.6, 100 + i * 1.6) for i in range(1, 11)]
        prices = _price_series("A", 1, entry, vals)
        scores = [("2099-01-01", "A", "2099-01-01", entry, 80, "WAIT", 0, 1.0)]
        r = self._run(scores, prices)
        # 用同資料跑 compute_prediction_outcomes
        rows_by_symbol = {"A": [{"date": d, "close": c} for (_s, d, c, _h) in prices]}
        preds = [{"symbol": "A", "price_date": "2099-01-01", "target_horizon": self.H,
                  "target_return": 0.10, "close": entry, "id": 1}]
        updates = backend.compute_prediction_outcomes(preds, rows_by_symbol)
        pred_hit = updates[0][3]
        cft_hit = r["topFollowThrough"][0]["hitClose"] if r["topFollowThrough"] else r["overall"]["hitCloseRate"]
        self.assertEqual(pred_hit, 1)
        self.assertEqual(r["overall"]["hitCloseRate"], float(pred_hit))


if __name__ == "__main__":
    unittest.main()
