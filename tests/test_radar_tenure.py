"""
妖股雷達進榜天數 compute_radar_tenure / measure_tenure_hit_rate 的回歸測試。

功能G：純讀 monster_scores.scan_date 歷史算每檔候選在雷達候選池的生命週期
(daysOnRadar 連續在榜掃描日數 / rounds 進出榜輪次 / firstSeen / peak / isPeakToday)，
唯讀不改評分。核心邏輯用 in-memory DB 完全隔離、確定性驗證，不碰正式資料庫。

執行方式：
  python -m unittest tests.test_radar_tenure -v
"""
import os
import sqlite3
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ml_backend
from ml_backend import backend


class _ConnCtx:
    def __init__(self, conn):
        self._conn = conn

    def __enter__(self):
        return self._conn

    def __exit__(self, *a):
        return False


def _mem_scores(rows):
    """rows: list of (scan_date, symbol, score)。回傳 in-memory conn(含 monster_scores)。"""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE monster_scores (scan_date TEXT, symbol TEXT, score REAL)")
    conn.executemany("INSERT INTO monster_scores (scan_date, symbol, score) VALUES (?,?,?)", rows)
    conn.commit()
    return conn


class RadarTenureLogicTests(unittest.TestCase):
    """直接測 _radar_tenure_from_conn 的純計算(in-memory DB，確定性)。"""

    def _run(self, rows, symbols=None, lookback=30):
        conn = _mem_scores(rows)
        try:
            return backend._radar_tenure_from_conn(conn, symbols, lookback)
        finally:
            conn.close()

    def test_consecutive_days(self):
        rows = [("2099-01-01", "A", 50), ("2099-01-02", "A", 60), ("2099-01-03", "A", 70)]
        r = self._run(rows)["A"]
        self.assertEqual(r["daysOnRadar"], 3)
        self.assertEqual(r["rounds"], 1)
        self.assertEqual(r["firstSeen"], "2099-01-01")

    def test_gap_resets_days_but_counts_rounds(self):
        # A 在 01、03 在榜，02 缺席(但 B 在 02，讓 02 成為有效掃描日)
        rows = [("2099-01-01", "A", 50), ("2099-01-02", "B", 40),
                ("2099-01-03", "A", 70)]
        r = self._run(rows)["A"]
        self.assertEqual(r["daysOnRadar"], 1, "最新掃描日往回只連續1天(02缺席即停)")
        self.assertEqual(r["rounds"], 2, "01一輪、03一輪=2輪")

    def test_single_day(self):
        rows = [("2099-01-03", "A", 70)]
        r = self._run(rows)["A"]
        self.assertEqual(r["daysOnRadar"], 1)
        self.assertEqual(r["rounds"], 1)

    def test_peak_today(self):
        rows = [("2099-01-01", "A", 50), ("2099-01-02", "A", 60), ("2099-01-03", "A", 88)]
        r = self._run(rows)["A"]
        self.assertTrue(r["isPeakToday"])
        self.assertEqual(r["peakScore"], 88)
        self.assertEqual(r["peakScoreDate"], "2099-01-03")

    def test_peak_not_today(self):
        rows = [("2099-01-01", "A", 90), ("2099-01-02", "A", 60), ("2099-01-03", "A", 55)]
        r = self._run(rows)["A"]
        self.assertFalse(r["isPeakToday"])
        self.assertEqual(r["peakScoreDate"], "2099-01-01")

    def test_symbol_filter(self):
        rows = [("2099-01-03", "A", 70), ("2099-01-03", "B", 60)]
        r = self._run(rows, symbols=["A"])
        self.assertIn("A", r)
        self.assertNotIn("B", r)

    def test_lookback_window_excludes_old(self):
        # A 只出現在很久以前，不在最新掃描日的 30 天窗內 -> 不回傳
        rows = [("2099-01-01", "A", 70), ("2099-06-01", "B", 60)]
        r = self._run(rows, lookback=30)
        self.assertNotIn("A", r, "A 在窗外(距最新掃描日>30天)應被排除")
        self.assertIn("B", r)

    def test_empty_returns_empty(self):
        self.assertEqual(self._run([]), {})

    def test_days_on_radar_counts_scan_days_not_calendar(self):
        # 掃描日 01、02、05(週末沒掃 03/04)，A 三天都在 -> daysOnRadar=3(以掃描日計，非日曆日)
        rows = [("2099-01-01", "A", 50), ("2099-01-02", "A", 55), ("2099-01-05", "A", 60)]
        r = self._run(rows)["A"]
        self.assertEqual(r["daysOnRadar"], 3)
        self.assertEqual(r["rounds"], 1)


class RadarTenurePublicWrapperTests(unittest.TestCase):
    """compute_radar_tenure(conn=None) 會自開連線；用 patch 導到 in-memory。"""

    def test_opens_own_connection(self):
        conn = _mem_scores([("2099-01-01", "A", 50), ("2099-01-02", "A", 60)])
        try:
            with patch.object(backend, "connect", return_value=_ConnCtx(conn)):
                r = backend.compute_radar_tenure()
            self.assertEqual(r["A"]["daysOnRadar"], 2)
        finally:
            conn.close()


class ListMonsterScoresTenureInjectionTests(unittest.TestCase):
    """list_monster_scores 每個候選都要帶 tenure 欄位(唯讀，值可為 dict 或 None)。
    純讀正式資料庫、不寫入不污染。"""

    def test_every_candidate_has_tenure_key(self):
        result = backend.list_monster_scores(limit=5)
        for c in result.get("candidates", []):
            self.assertIn("tenure", c, "每個候選都要有 tenure 欄位(前端 badge 依賴它)")


class MeasureTenureHitRateTests(unittest.TestCase):
    """measure_tenure_hit_rate 是量測用診斷(非上線)：按進榜第N天分桶算命中率。"""

    def _mem_both(self, score_rows, pred_rows):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("CREATE TABLE monster_scores (scan_date TEXT, symbol TEXT, score REAL)")
        conn.execute("CREATE TABLE predictions (symbol TEXT, price_date TEXT, action TEXT, hit INTEGER)")
        conn.executemany("INSERT INTO monster_scores (scan_date, symbol, score) VALUES (?,?,?)", score_rows)
        conn.executemany("INSERT INTO predictions (symbol, price_date, action, hit) VALUES (?,?,?,?)", pred_rows)
        conn.commit()
        return conn

    def test_no_data_returns_zero_sample(self):
        conn = self._mem_both([], [])
        try:
            with patch.object(backend, "connect", return_value=_ConnCtx(conn)):
                r = backend.measure_tenure_hit_rate()
            self.assertTrue(r["ok"])
            self.assertEqual(r["sampleSize"], 0)
        finally:
            conn.close()

    def test_buckets_by_tenure_day(self):
        # A 連續在榜 01、02；在 02 當天有一筆已結算 BUY_CANDIDATE(hit=1) -> 進榜第2天桶
        scores = [("2099-01-01", "A", 50), ("2099-01-02", "A", 60)]
        preds = [("A", "2099-01-02", "BUY_CANDIDATE", 1)]
        conn = self._mem_both(scores, preds)
        try:
            with patch.object(backend, "connect", return_value=_ConnCtx(conn)):
                r = backend.measure_tenure_hit_rate()
            self.assertEqual(r["sampleSize"], 1)
            bucket = next(b for b in r["byTenureBucket"] if b["tenureDay"] == 2)
            self.assertEqual(bucket["candidateCount"], 1)
            self.assertEqual(bucket["hit10Rate"], 1.0)
        finally:
            conn.close()

    def test_ignores_unsettled_and_non_buy(self):
        scores = [("2099-01-01", "A", 50)]
        # hit=None(未結算)與 action=WAIT 都不計入
        preds = [("A", "2099-01-01", "BUY_CANDIDATE", None), ("A", "2099-01-01", "WAIT", 1)]
        conn = self._mem_both(scores, preds)
        try:
            with patch.object(backend, "connect", return_value=_ConnCtx(conn)):
                r = backend.measure_tenure_hit_rate()
            self.assertEqual(r["sampleSize"], 0)
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
