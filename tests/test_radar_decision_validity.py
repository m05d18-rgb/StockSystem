import json
import tempfile
from pathlib import Path
from unittest.mock import patch

from ml_backend import StockMLBackend


def build_backend(tmp_dir):
    backend = StockMLBackend()
    backend.db_path = Path(tmp_dir) / "stock_system.sqlite3"
    backend.init_db()
    return backend


def test_typhoon_holiday_scan_is_kept_but_invalidated_for_trading():
    with tempfile.TemporaryDirectory() as tmp:
        backend = build_backend(tmp)
        with backend.connect() as conn:
            conn.execute(
                """
                INSERT INTO prices (
                    symbol, date, open, high, low, close, volume, updated_at, price_source
                ) VALUES ('2330', '2026-07-09', 100, 101, 99, 100, 1000000,
                          '2026-07-09 14:00:00', 'TWSE official')
                """
            )
            conn.execute(
                """
                INSERT INTO monster_scores (
                    scan_date, symbol, price_date, score, action, buy_allowed,
                    status, close, gap_limit, surge_setup, reasons, created_at
                ) VALUES (
                    '2026-07-10', '2330', '2026-07-09', 90, 'NEXT_DAY_WATCH', 1,
                    '原始可買', 100, 0.05, 1, '[]', '2026-07-10 15:00:00'
                )
                """
            )
        # 模擬升版／服務重啟：既有休市掃描要就地保留稽核欄位，但正式
        # buy_allowed 必須在資料庫層歸零。
        backend.init_db()

        payload = backend.list_monster_scores(limit=10)
        item = payload["candidates"][0]
        with backend.connect() as conn:
            stored = conn.execute(
                """
                SELECT buy_allowed, recorded_buy_allowed, invalid_for_trading
                FROM monster_scores
                WHERE scan_date = '2026-07-10' AND symbol = '2330'
                """
            ).fetchone()

    assert tuple(stored) == (0, 1, 1)
    assert item["storedBuyAllowed"] is False
    assert item["recordedBuyAllowed"] is True
    assert item["policyBuyAllowed"] is True
    assert item["buyAllowed"] is False
    assert item["invalidForTrading"] is True
    assert item["status"] == "休市日掃描，只供稽核"
    assert "scan_market_closed" in {row["code"] for row in item["invalidReasons"]}
    assert payload["decisionValidity"]["validForTrading"] is False


def test_stale_daily_bar_invalidates_decision():
    with tempfile.TemporaryDirectory() as tmp:
        backend = build_backend(tmp)
        status = backend.radar_decision_validity(
            "2026-07-09",
            price_dates=["2026-07-08"],
            latest_complete_price_date="2026-07-09",
            current_date="2026-07-09",
            meta={},
        )
    assert status["validForTrading"] is False
    assert "daily_bar_stale" in {row["code"] for row in status["invalidReasons"]}


def test_weekend_audit_scan_does_not_shadow_previous_valid_trading_scan():
    with tempfile.TemporaryDirectory() as tmp:
        backend = build_backend(tmp)
        calendar = {
            "version": "twse-planned-calendar-v1",
            "year": 2026,
            "entries": {},
        }
        with backend.connect() as conn:
            conn.execute("""
                INSERT INTO prices (
                    symbol, date, open, high, low, close, volume, updated_at, price_source
                ) VALUES ('2330', '2026-07-09', 100, 101, 99, 100, 1000000,
                          '2026-07-09 14:00:00', 'TWSE official')
            """)
            for scan_date in ("2026-07-09", "2026-07-11"):
                conn.execute("""
                    INSERT INTO monster_scores (
                        scan_date, symbol, price_date, score, action, buy_allowed,
                        status, close, gap_limit, surge_setup, reasons, created_at
                    ) VALUES (?, '2330', '2026-07-09', 80, 'NEXT_DAY_WATCH', 1,
                              '原始可買', 100, 0.05, 1, '[]', ?)
                """, (scan_date, f"{scan_date} 15:00:00"))
            backend.set_meta(conn, "twse_market_calendar_2026", json.dumps({
                "fetchedAt": "2026-07-11 08:00:00",
                "calendar": calendar,
            }))

        selection = backend.select_radar_decision_scan(current_date="2026-07-13")

    assert selection["selectedScanDate"] == "2026-07-09"
    assert selection["latestAuditScanDate"] == "2026-07-11"
    assert selection["usingFallbackValidScan"] is True
    assert selection["decisionValidity"]["validForTrading"] is True
    assert selection["latestAuditValidity"]["validForTrading"] is False
    assert "scan_market_closed" in {
        item["code"] for item in selection["latestAuditValidity"]["invalidReasons"]
    }


def test_cached_dgpa_notice_cannot_replace_weekend_reason_inside_radar_validation():
    with tempfile.TemporaryDirectory() as tmp:
        backend = build_backend(tmp)
        meta = {
            "dgpa_taipei_closure_2026-07-11": json.dumps({
                "fetchedAt": "2026-07-11 08:00:00",
                "closure": {
                    "date": "2026-07-11",
                    "marketClosed": True,
                    "reason": "臺北市停止上班",
                    "source": "DGPA Taipei closure",
                },
            }),
        }
        status = backend.radar_decision_validity(
            "2026-07-11",
            price_dates=["2026-07-09"],
            latest_complete_price_date="2026-07-09",
            current_date="2026-07-11",
            meta=meta,
        )
    assert status["scanMarket"]["reason"] == "週末"
    assert status["scanMarket"]["source"] == "weekday"
    assert status["currentMarket"]["reason"] == "週末"


def test_radar_order_context_requires_current_valid_scan_and_matching_symbol():
    with tempfile.TemporaryDirectory() as tmp:
        backend = build_backend(tmp)
        with backend.connect() as conn:
            conn.execute("""
                INSERT INTO monster_scores (
                    scan_date, symbol, price_date, score, action, buy_allowed,
                    status, close, gap_limit, surge_setup, reasons, created_at,
                    invalid_for_trading
                ) VALUES (
                    '2026-07-13', '2330', '2026-07-10', 80, 'NEXT_DAY_WATCH', 0,
                    '觀察', 100, 0.05, 1, '[]', '2026-07-13 08:50:00', 0
                )
            """)
        selection = {
            "selectedScanDate": "2026-07-13",
            "decisionValidity": {"validForTrading": True},
        }
        with patch.object(backend, "select_radar_decision_scan", return_value=selection):
            verified = backend.validate_radar_order_context(
                "2330", "2026-07-13", current_date="2026-07-13"
            )
            wrong_symbol = backend.validate_radar_order_context(
                "2317", "2026-07-13", current_date="2026-07-13"
            )
            stale_scan = backend.validate_radar_order_context(
                "2330", "2026-07-12", current_date="2026-07-13"
            )
    assert verified["ok"] is True
    assert wrong_symbol["reason"] == "symbol_not_in_radar_scan"
    assert stale_scan["reason"] == "radar_scan_mismatch"
