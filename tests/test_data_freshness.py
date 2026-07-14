"""
資料新鮮度儀表板 data_freshness() 的回歸測試。

功能E：現有健康檢查(run_system_health)偏重「模型能不能用」，data_freshness()
專門看「每個資料源多久沒更新了」——FinMind額度/模型/大盤/個股K/即時tick 任一
斷更就在儀表板亮出待更新，讓交易者一眼看出資料是不是新的。

測試以 in-memory DB + monkeypatch 控制各資料源時間，不碰正式資料庫。

執行方式：
  python -m unittest tests.test_data_freshness -v
"""
import os
import sqlite3
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ml_backend


class _ConnCtx:
    """模擬 backend.connect() 的 with-context 行為，包住共用 in-memory 連線。"""

    def __init__(self, conn):
        self._conn = conn

    def __enter__(self):
        return self._conn

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None:
            self._conn.commit()
        return False


def _make_conn(market_date=None, price_date=None, price_symbols=0, tick_updated_at=None, today="2026-07-04",
               otc_date=None, prev_price_date=None, prev_price_symbols=0):
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE market_prices (market_key TEXT, date TEXT, close REAL)")
    conn.execute("CREATE TABLE prices (symbol TEXT, date TEXT, close REAL)")
    conn.execute("CREATE TABLE model_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    conn.execute("""
        CREATE TABLE realtime_flow_staging (
            symbol TEXT, date TEXT, realtime_money_flow REAL,
            realtime_large_order_flow REAL, tick_count INTEGER,
            source TEXT, updated_at TEXT, PRIMARY KEY (symbol, date)
        )
    """)
    if market_date:
        conn.execute("INSERT INTO market_prices VALUES ('TAIEX', ?, 1.0)", (market_date,))
        # OTC 預設跟 TAIEX 同日期(新鮮)；otc_date 明給才測 OTC 落後
        conn.execute("INSERT INTO market_prices VALUES ('OTC', ?, 1.0)", (otc_date or market_date,))
    if prev_price_date:  # 前一交易日的覆蓋(給覆蓋驟降測試當基準)
        for i in range(max(prev_price_symbols, 1)):
            conn.execute("INSERT INTO prices VALUES (?, ?, 1.0)", (f"{1000 + i:04d}", prev_price_date))
    if price_date:
        for i in range(max(price_symbols, 1)):
            conn.execute("INSERT INTO prices VALUES (?, ?, 1.0)", (f"{1000 + i:04d}", price_date))
    if tick_updated_at:
        conn.execute(
            "INSERT INTO realtime_flow_staging VALUES ('ZTESTTICK', ?, 1.0, 1.0, 1, 'test', ?)",
            (today, tick_updated_at),
        )
    conn.commit()
    return conn


class DataFreshnessTests(unittest.TestCase):
    TODAY = "2026-07-04"

    def _run(self, conn, usage=None, model=None, radar_validity=None):
        usage = usage if usage is not None else {
            "updatedAt": "2026-07-04 12:00:00", "calls": 30, "safeLimit": 5000, "blocked": False,
        }
        model = model if model is not None else {"trained_at": "2026-07-04 08:00:00"}
        radar_validity = radar_validity if radar_validity is not None else {
            "validForTrading": True,
            "scanDate": "2026-07-03",
            "invalidReasons": [],
            "summary": "雷達決策資料有效",
        }
        with patch.object(ml_backend, "today_key", return_value=self.TODAY), \
             patch.object(ml_backend, "now_text", return_value="2026-07-04 13:00:00"), \
             patch.object(ml_backend.backend, "read_finmind_usage", return_value=usage), \
             patch.object(ml_backend.backend, "load_model", return_value=model), \
             patch.object(ml_backend.backend, "current_radar_decision_validity", return_value=radar_validity), \
             patch.object(ml_backend.backend, "connect", return_value=_ConnCtx(conn)):
            return ml_backend.backend.data_freshness()

    def _source(self, result, name_startswith):
        for s in result["sources"]:
            if s["name"].startswith(name_startswith):
                return s
        return None

    @staticmethod
    def _set_close_meta(conn, status):
        values = {
            "last_official_close_sync_attempt_at": "2026-07-04 18:20:00",
            "last_official_close_sync_status": status,
            "last_official_close_sync_target_date": "2026-07-04",
            "last_official_close_sync_latest_date": "2026-07-03",
            "last_official_close_sync_latest_count": "1800",
            "last_official_close_sync_error": "",
            "last_official_close_sync_calendar_reason": "官方開休市表未列為休市日",
            "last_official_close_sync_calendar_source": "TWSE annual holiday schedule",
        }
        conn.executemany("INSERT INTO model_meta(key, value) VALUES (?, ?)", values.items())
        conn.commit()

    def test_structure_has_required_keys(self):
        conn = _make_conn(market_date="2026-07-03", price_date="2026-07-03", price_symbols=5,
                          tick_updated_at="2026-07-04 09:30:00", today=self.TODAY)
        r = self._run(conn)
        self.assertTrue(r["ok"])
        self.assertIn("overallOk", r)
        self.assertIn("checkedAt", r)
        self.assertIsInstance(r["sources"], list)
        for s in r["sources"]:
            self.assertIn("name", s)
            self.assertIn("ok", s)
            self.assertIn("detail", s)

    def test_all_fresh_overall_ok(self):
        conn = _make_conn(market_date="2026-07-03", price_date="2026-07-03", price_symbols=5,
                          tick_updated_at="2026-07-04 09:30:00", today=self.TODAY)
        r = self._run(conn)
        self.assertTrue(r["overallOk"], "所有資料源都在門檻內，整體應為 OK")

    def test_otc_stale_flags_market_even_if_taiex_fresh(self):
        # TAIEX 新鮮(07-03)但 OTC 落後(06-20)→ 大盤整體要標 not ok(監控死角修復)
        conn = _make_conn(market_date="2026-07-03", otc_date="2026-06-20",
                          price_date="2026-07-03", price_symbols=5, today=self.TODAY)
        r = self._run(conn)
        mkt = self._source(r, "大盤")
        self.assertIsNotNone(mkt)
        self.assertFalse(mkt["ok"], "TAIEX 新鮮但 OTC 落後 → 大盤 not ok")
        self.assertIn("OTC", mkt["detail"])
        self.assertFalse(r["overallOk"])

    def test_price_coverage_drop_flags_not_ok(self):
        # 最新日只有 46 檔、前一交易日 6648 檔 → 全市場更新沒跑完 → 個股K not ok
        # (即使日期是新鮮的;這正是 2026-07-04 只看日期漏掉的 07-03 缺口)
        conn = _make_conn(market_date="2026-07-03",
                          prev_price_date="2026-07-02", prev_price_symbols=6648,
                          price_date="2026-07-03", price_symbols=46, today=self.TODAY)
        r = self._run(conn)
        px = self._source(r, "個股日K")
        self.assertIsNotNone(px)
        self.assertFalse(px["ok"], "最新日覆蓋<前日一半 → 標 not ok")
        self.assertIn("更新恐未完成", px["detail"])

    def test_full_price_coverage_stays_ok(self):
        # 覆蓋正常(前日 6900、今日 6648，>一半)→ 個股K ok
        conn = _make_conn(market_date="2026-07-03",
                          prev_price_date="2026-07-02", prev_price_symbols=6900,
                          price_date="2026-07-03", price_symbols=6648, today=self.TODAY)
        r = self._run(conn)
        px = self._source(r, "個股日K")
        self.assertTrue(px["ok"], "覆蓋正常應為 ok")

    def test_derivative_row_drop_does_not_fake_common_stock_coverage_gap(self):
        conn = _make_conn(
            market_date="2026-07-03",
            prev_price_date="2026-07-02",
            prev_price_symbols=1200,
            price_date="2026-07-03",
            price_symbols=1200,
            today=self.TODAY,
        )
        # 舊口徑會拿整包 5,700 對 1,208，誤判最新日不到前日一半；
        # 實際兩天一般股都是 1,200 檔，應維持健康。
        conn.executemany(
            "INSERT INTO prices VALUES (?, '2026-07-02', 1.0)",
            [(f"WPREV{i}",) for i in range(4500)],
        )
        conn.executemany(
            "INSERT INTO prices VALUES (?, '2026-07-03', 1.0)",
            [(f"WNEW{i}",) for i in range(8)],
        )
        conn.commit()

        r = self._run(conn)
        px = self._source(r, "個股日K")
        self.assertTrue(px["ok"])
        self.assertNotIn("更新恐未完成", px["detail"])

    def test_stale_market_data_flags_not_ok(self):
        # 大盤最新只到 6/20，距 7/4 已 14 天 > MARKET_DATA_MAX_STALE_DAYS(6)
        conn = _make_conn(market_date="2026-06-20", price_date="2026-07-03", price_symbols=5,
                          today=self.TODAY)
        r = self._run(conn)
        mkt = self._source(r, "大盤")
        self.assertIsNotNone(mkt)
        self.assertFalse(mkt["ok"], "大盤過期應標記 not ok")
        self.assertFalse(r["overallOk"], "任一源過期整體就不 OK")

    def test_blocked_finmind_flags_not_ok(self):
        conn = _make_conn(market_date="2026-07-03", price_date="2026-07-03", today=self.TODAY)
        r = self._run(conn, usage={"updatedAt": "2026-07-04 12:00:00", "calls": 4999,
                                   "safeLimit": 5000, "blocked": True})
        fm = self._source(r, "FinMind")
        self.assertIsNotNone(fm)
        self.assertFalse(fm["ok"], "FinMind 已阻擋(額度用盡)應標記 not ok")
        self.assertFalse(r["overallOk"])

    def test_old_model_flags_not_ok(self):
        # 模型日期早於最新完整日 K，至少落後一個實際交易日。
        conn = _make_conn(market_date="2026-07-03", price_date="2026-07-03", today=self.TODAY)
        r = self._run(conn, model={"trained_at": "2026-06-29 08:00:00"})
        m = self._source(r, "AI 模型")
        self.assertIsNotNone(m)
        self.assertFalse(m["ok"], "模型落後完整交易日應標記 not ok")
        self.assertGreaterEqual(m["tradingSessionLag"], 1)

    def test_one_trading_session_lag_is_not_hidden_by_calendar_tolerance(self):
        conn = _make_conn(
            market_date="2026-07-03",
            prev_price_date="2026-07-02",
            prev_price_symbols=5,
            price_date="2026-07-03",
            price_symbols=5,
            today=self.TODAY,
        )
        r = self._run(conn, model={"trained_at": "2026-07-02 08:00:00"})
        m = self._source(r, "AI 模型")
        self.assertFalse(m["ok"])
        self.assertEqual(m["tradingSessionLag"], 1)

    def test_weekend_calendar_days_do_not_age_a_current_model(self):
        conn = _make_conn(
            market_date="2026-07-09",
            price_date="2026-07-09",
            price_symbols=5,
            today="2026-07-12",
        )
        with patch.object(ml_backend, "today_key", return_value="2026-07-12"):
            r = self._run(conn, model={"trained_at": "2026-07-09 15:10:00"})
        m = self._source(r, "AI 模型")
        self.assertTrue(m["ok"])
        self.assertEqual(m["tradingSessionLag"], 0)
        self.assertIn("落後 0 個完整交易日", m["detail"])

    def test_no_tick_today_is_still_ok(self):
        # 盤外沒有 tick 是正常的，不能標成異常
        conn = _make_conn(market_date="2026-07-03", price_date="2026-07-03", price_symbols=5,
                          tick_updated_at=None, today=self.TODAY)
        r = self._run(conn)
        tick = self._source(r, "即時主力")
        self.assertIsNotNone(tick)
        self.assertTrue(tick["ok"], "盤外無 tick 屬正常，ok 應為 True")
        self.assertTrue(r["overallOk"], "只有 tick 缺(正常)不該讓整體變 not ok")

    def test_invalid_radar_decision_makes_overall_not_ok(self):
        conn = _make_conn(market_date="2026-07-03", price_date="2026-07-03", price_symbols=5,
                          today=self.TODAY)
        r = self._run(conn, radar_validity={
            "validForTrading": False,
            "scanDate": "2026-07-04",
            "invalidReasons": [{"code": "current_market_closed", "label": "今日非交易日"}],
            "summary": "今日非交易日",
        })
        radar = self._source(r, "雷達決策")
        self.assertIsNotNone(radar)
        self.assertFalse(radar["ok"])
        self.assertFalse(r["overallOk"])
        self.assertIn("所有買進建議已停用", radar["detail"])

    def test_legacy_previous_trading_day_status_is_not_healthy(self):
        conn = _make_conn(market_date="2026-07-03", price_date="2026-07-03", price_symbols=5,
                          today=self.TODAY)
        self._set_close_meta(conn, "previous_trading_day")
        r = self._run(conn)
        close = self._source(r, "收盤官方")
        self.assertIsNotNone(close)
        self.assertFalse(close["ok"])
        self.assertFalse(r["overallOk"])

    def test_official_scheduled_holiday_status_is_healthy(self):
        conn = _make_conn(market_date="2026-07-03", price_date="2026-07-03", price_symbols=5,
                          today=self.TODAY)
        self._set_close_meta(conn, "scheduled_holiday")
        r = self._run(conn)
        close = self._source(r, "收盤官方")
        self.assertIsNotNone(close)
        self.assertTrue(close["ok"])

    def test_price_symbol_count_surfaced_in_detail(self):
        conn = _make_conn(market_date="2026-07-03", price_date="2026-07-03", price_symbols=46,
                          today=self.TODAY)
        r = self._run(conn)
        px = self._source(r, "個股日K")
        self.assertIsNotNone(px)
        self.assertIn("46", px["detail"], "個股日K 明細應誠實揭露最新日期的檔數")

    def test_missing_model_trained_at_flags_not_ok(self):
        conn = _make_conn(market_date="2026-07-03", price_date="2026-07-03", today=self.TODAY)
        r = self._run(conn, model={})
        m = self._source(r, "AI 模型")
        self.assertFalse(m["ok"], "模型沒有 trained_at 應標記 not ok")


if __name__ == "__main__":
    unittest.main()
