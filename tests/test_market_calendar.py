import datetime as dt
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from market_calendar import (
    load_market_session_overrides,
    parse_dgpa_taipei_closure,
    parse_twse_calendar,
    planned_market_day,
)
from ml_backend import StockMLBackend
import server


def calendar_payload():
    return {
        "stat": "ok",
        "title": "115 年市場開休市日期",
        "data": [
            ["2026-01-01", "中華民國開國紀念日", "依規定放假1日。"],
            ["2026-01-02", "國曆新年開始交易日", "國曆新年開始交易。"],
            ["2026-02-11", "農曆春節前最後交易日", "農曆春節前最後交易。"],
            ["2026-02-12", "市場無交易，僅辦理結算交割作業", ""],
            ["2026-06-19", "端午節", "依規定放假1日。"],
        ],
    }


def test_unlisted_weekday_is_planned_trading_day():
    calendar = parse_twse_calendar(calendar_payload(), 2026)
    status = planned_market_day("2026-07-10", calendar)
    assert status["known"] is True
    assert status["isTradingDay"] is True
    assert status["source"] == "TWSE annual holiday schedule"


def test_holiday_and_market_no_trade_dates_are_closed():
    calendar = parse_twse_calendar(calendar_payload(), 2026)
    assert planned_market_day("2026-06-19", calendar)["isTradingDay"] is False
    assert planned_market_day("2026-02-12", calendar)["isTradingDay"] is False


def test_explicit_first_and_last_trading_day_markers_stay_open():
    calendar = parse_twse_calendar(calendar_payload(), 2026)
    assert planned_market_day("2026-01-02", calendar)["isTradingDay"] is True
    assert planned_market_day("2026-02-11", calendar)["isTradingDay"] is True


def test_weekend_is_closed_even_without_calendar():
    status = planned_market_day(dt.date(2026, 7, 11), None)
    assert status["known"] is True
    assert status["isTradingDay"] is False


def test_missing_weekday_calendar_is_unknown_not_assumed_holiday():
    status = planned_market_day("2026-07-10", None)
    assert status["known"] is False
    assert status["isTradingDay"] is None


def test_invalid_payload_is_rejected():
    with pytest.raises(ValueError):
        parse_twse_calendar({"stat": "error", "data": []}, 2026)


def dgpa_html(status, date_text="115年 7月 10日"):
    return f"""
    <html><body>
      <div>{date_text} 天然災害停止上班及上課情形</div>
      <table><tr><td>臺北市</td><td>{status}</td></tr></table>
    </body></html>
    """


def test_dgpa_full_day_taipei_closure_closes_market():
    closure = parse_dgpa_taipei_closure(dgpa_html("今天停止上班、停止上課。"))
    assert closure["date"] == "2026-07-10"
    assert closure["marketClosed"] is True
    assert closure["closureScope"] == "full_day_or_morning"


def test_dgpa_afternoon_only_closure_does_not_close_regular_market():
    closure = parse_dgpa_taipei_closure(dgpa_html("今天下午停止上班、停止上課。"))
    assert closure["marketClosed"] is False
    assert closure["closureScope"] == "afternoon_only"


def test_emergency_override_records_july_tenth_typhoon_closure():
    entries = load_market_session_overrides(Path(__file__).parents[1] / "market_session_overrides.json")
    status = entries["2026-07-10"]
    assert status["isTradingDay"] is False
    assert status["emergencyClosure"] is True
    assert "颱風" in status["reason"]


def test_server_caches_official_calendar_in_database():
    with tempfile.TemporaryDirectory() as tmp:
        backend = StockMLBackend()
        backend.db_path = Path(tmp) / "stock_system.sqlite3"
        backend.init_db()
        parsed = parse_twse_calendar(calendar_payload(), 2026)
        server.twse_market_calendar_cache.clear()
        with patch.object(server, "backend", backend), \
             patch.object(server, "fetch_twse_calendar", return_value=parsed) as fetch:
            status = server.official_market_day_status("2026-07-09", force_refresh=True)
        with backend.connect() as conn:
            stored = conn.execute(
                "SELECT value FROM model_meta WHERE key = 'twse_market_calendar_2026'"
            ).fetchone()
        server.twse_market_calendar_cache.clear()
        assert status["isTradingDay"] is True
        assert status["calendarStale"] is False
        assert stored and "twse-planned-calendar-v1" in stored[0]
        fetch.assert_called_once_with(2026)


def test_server_emergency_override_precedes_annual_calendar():
    with tempfile.TemporaryDirectory() as tmp:
        backend = StockMLBackend()
        backend.db_path = Path(tmp) / "stock_system.sqlite3"
        backend.init_db()
        with patch.object(server, "backend", backend), \
             patch.object(server, "fetch_twse_calendar") as fetch:
            status = server.official_market_day_status("2026-07-10", force_refresh=True)
        assert status["known"] is True
        assert status["isTradingDay"] is False
        assert status["emergencyClosure"] is True
        assert "Taipei City Government" in status["source"]
        fetch.assert_not_called()


def test_weekend_status_never_uses_same_day_dgpa_closure_as_the_reason():
    with tempfile.TemporaryDirectory() as tmp:
        backend = StockMLBackend()
        backend.db_path = Path(tmp) / "stock_system.sqlite3"
        backend.init_db()
        parsed = parse_twse_calendar(calendar_payload(), 2026)
        server.twse_market_calendar_cache.clear()
        with patch.object(server, "backend", backend), \
             patch.object(server, "fetch_twse_calendar", return_value=parsed), \
             patch.object(server, "fetch_dgpa_taipei_closure") as dgpa:
            status = server.official_market_day_status("2026-07-11", force_refresh=True)
        server.twse_market_calendar_cache.clear()
    assert status["known"] is True
    assert status["isTradingDay"] is False
    assert status["reason"] == "週末"
    assert status["emergencyClosure"] is False
    dgpa.assert_not_called()


def test_startup_reclassifies_legacy_possible_holiday_as_failure_on_open_day():
    with tempfile.TemporaryDirectory() as tmp:
        backend = StockMLBackend()
        backend.db_path = Path(tmp) / "stock_system.sqlite3"
        backend.init_db()
        with backend.connect() as conn:
            values = {
                "last_official_close_sync_status": "previous_trading_day",
                "last_official_close_sync_target_date": "2026-07-10",
                "last_official_close_sync_latest_date": "2026-07-09",
                "last_official_close_sync_latest_count": "6399",
            }
            for key, value in values.items():
                backend.set_meta(conn, key, value)
        market_day = {
            "known": True,
            "isTradingDay": True,
            "reason": "官方開休市表未列為休市日",
            "source": "TWSE annual holiday schedule",
        }
        now = time.struct_time((2026, 7, 10, 22, 0, 0, 4, 191, 0))
        with patch.object(server, "backend", backend), \
             patch.object(server, "official_market_day_status", return_value=market_day):
            result = server.reconcile_official_close_sync_calendar_state(now=now)
        with backend.connect() as conn:
            meta = dict(conn.execute("""
                SELECT key, value FROM model_meta
                WHERE key IN (
                    'last_official_close_sync_status',
                    'auto_schedule_1435_official_close_sync_status'
                )
            """).fetchall())
        assert result["recovered"] is True
        assert result["status"] == "failed"
        assert meta["last_official_close_sync_status"] == "failed"
        assert meta["auto_schedule_1435_official_close_sync_status"] == "failed_recovered"


def test_next_day_startup_repairs_failed_sync_as_emergency_holiday():
    with tempfile.TemporaryDirectory() as tmp:
        backend = StockMLBackend()
        backend.db_path = Path(tmp) / "stock_system.sqlite3"
        backend.init_db()
        with backend.connect() as conn:
            values = {
                "last_official_close_sync_status": "failed",
                "last_official_close_sync_target_date": "2026-07-10",
                "last_official_close_sync_latest_date": "2026-07-09",
                "last_official_close_sync_latest_count": "6399",
            }
            for key, value in values.items():
                backend.set_meta(conn, key, value)
        market_day = {
            "known": True,
            "isTradingDay": False,
            "reason": "臺北市全日停止上班",
            "source": "DGPA Taipei closure / TWSE natural-disaster rule",
            "emergencyClosure": True,
        }
        now = time.struct_time((2026, 7, 11, 7, 35, 0, 5, 192, 0))
        with patch.object(server, "backend", backend), \
             patch.object(server, "official_market_day_status", return_value=market_day):
            result = server.reconcile_official_close_sync_calendar_state(now=now)
        with backend.connect() as conn:
            meta = dict(conn.execute("""
                SELECT key, value FROM model_meta
                WHERE key IN (
                    'last_official_close_sync_status',
                    'auto_schedule_1435_official_close_sync_status',
                    'auto_schedule_1435_official_close_sync_date'
                )
            """).fetchall())
        assert result["recovered"] is True
        assert result["status"] == "scheduled_holiday"
        assert meta["last_official_close_sync_status"] == "scheduled_holiday"
        assert meta["auto_schedule_1435_official_close_sync_status"] == "success_recovered"
        assert meta["auto_schedule_1435_official_close_sync_date"] == "2026-07-10"


def test_startup_prefers_later_actual_close_success_over_stale_failed_schedule_state():
    with tempfile.TemporaryDirectory() as tmp:
        backend = StockMLBackend()
        backend.db_path = Path(tmp) / "stock_system.sqlite3"
        backend.init_db()
        with backend.connect() as conn:
            values = {
                "last_official_close_sync_status": "ready",
                "last_official_close_sync_target_date": "2026-07-13",
                "last_official_close_sync_latest_date": "2026-07-13",
                "last_official_close_sync_latest_count": "6202",
                "auto_schedule_1435_official_close_sync_date": "2026-07-13",
                "auto_schedule_1435_official_close_sync_status": "failed_recovered",
                "auto_schedule_1435_official_close_sync_message": "舊失敗",
            }
            for key, value in values.items():
                backend.set_meta(conn, key, value)
        now = time.struct_time((2026, 7, 14, 7, 0, 0, 1, 195, 0))
        with patch.object(server, "backend", backend), \
             patch.object(server, "official_market_day_status") as market_day:
            result = server.reconcile_official_close_sync_calendar_state(now=now)
        with backend.connect() as conn:
            meta = dict(conn.execute("""
                SELECT key, value FROM model_meta
                WHERE key IN (
                    'auto_schedule_1435_official_close_sync_status',
                    'auto_schedule_1435_official_close_sync_message'
                )
            """).fetchall())

        assert result["recovered"] is True
        assert result["status"] == "ready"
        assert meta["auto_schedule_1435_official_close_sync_status"] == "success_recovered"
        assert "6202" in meta["auto_schedule_1435_official_close_sync_message"]
        market_day.assert_not_called()
