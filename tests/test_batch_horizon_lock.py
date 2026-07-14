import tempfile
from pathlib import Path

import pytest

from ml_backend import StockMLBackend


def build_backend(tmp_dir):
    backend = StockMLBackend()
    backend.db_path = Path(tmp_dir) / "stock_system.sqlite3"
    backend.init_db()
    for symbol, price, buy_date in (
        ("2330", 100, "2026-07-02 09:31:00"),
        ("2454", 200, "2026-07-03 09:32:00"),
    ):
        backend.record_trade({
            "symbol": symbol,
            "side": "BUY",
            "price": price,
            "shares": 1000,
            "buyDate": buy_date,
            "status": "filled",
        })
    return backend


def holdings():
    return {
        "2330": {"code": "2330", "shares": 1000, "quantity": 1, "price": 100},
        "2454": {"code": "2454", "shares": 1000, "quantity": 1, "price": 200},
    }


def assignments():
    return [
        {"symbol": "2330", "tradeId": 1, "strategyHorizon": "short_trade"},
        {"symbol": "2454", "tradeId": 2, "strategyHorizon": "long_trend"},
    ]


def test_preview_writes_nothing_then_apply_locks_all_in_one_audit_batch():
    with tempfile.TemporaryDirectory() as tmp:
        backend = build_backend(tmp)
        preview = backend.lock_existing_position_horizons(
            assignments(), holdings(), apply=False
        )
        with backend.connect() as conn:
            before = conn.execute(
                "SELECT symbol, strategy_horizon FROM trades ORDER BY symbol"
            ).fetchall()
            before_audits = conn.execute(
                "SELECT COUNT(*) FROM portfolio_horizon_lock_batches"
            ).fetchone()[0]

        applied = backend.lock_existing_position_horizons(
            assignments(), holdings(), apply=True
        )
        with backend.connect() as conn:
            after = conn.execute(
                "SELECT symbol, strategy_horizon, strategy_horizon_source "
                "FROM trades ORDER BY symbol"
            ).fetchall()
            audits = conn.execute(
                "SELECT COUNT(*) FROM portfolio_horizon_lock_batches"
            ).fetchone()[0]

    assert preview["preview"] is True
    assert preview["requiredSymbols"] == ["2330", "2454"]
    assert preview["requiredTradeIds"] == [1, 2]
    assert [lot["buyDate"] for lot in preview["requiredLots"]] == [
        "2026-07-02", "2026-07-03",
    ]
    assert [tuple(row) for row in before] == [("2330", "unknown"), ("2454", "unknown")]
    assert before_audits == 0
    assert applied["applied"] is True
    assert applied["auditId"] is not None
    assert [tuple(row) for row in after] == [
        ("2330", "short_trade", "manual_batch_lot_lock"),
        ("2454", "long_trend", "manual_batch_lot_lock"),
    ]
    assert audits == 1


def test_missing_one_assignment_rolls_back_the_entire_batch():
    with tempfile.TemporaryDirectory() as tmp:
        backend = build_backend(tmp)
        with pytest.raises(ValueError, match="尚有 1 個 lot 未選"):
            backend.lock_existing_position_horizons(
                assignments()[:1], holdings(), apply=True
            )
        with backend.connect() as conn:
            horizons = conn.execute(
                "SELECT strategy_horizon FROM trades ORDER BY symbol"
            ).fetchall()
            audits = conn.execute(
                "SELECT COUNT(*) FROM portfolio_horizon_lock_batches"
            ).fetchone()[0]
    assert [row[0] for row in horizons] == ["unknown", "unknown"]
    assert audits == 0


def test_locked_batch_is_immutable_on_second_submission():
    with tempfile.TemporaryDirectory() as tmp:
        backend = build_backend(tmp)
        backend.lock_existing_position_horizons(assignments(), holdings(), apply=True)
        with pytest.raises(ValueError, match="沒有未知週期"):
            backend.lock_existing_position_horizons(assignments(), holdings(), apply=True)
        with backend.connect() as conn:
            rows = conn.execute(
                "SELECT symbol, strategy_horizon FROM trades ORDER BY symbol"
            ).fetchall()
    assert [tuple(row) for row in rows] == [("2330", "short_trade"), ("2454", "long_trend")]


def test_multi_lot_symbol_requires_trade_ids_and_allows_different_horizons():
    with tempfile.TemporaryDirectory() as tmp:
        backend = StockMLBackend()
        backend.db_path = Path(tmp) / "stock_system.sqlite3"
        backend.init_db()
        backend.record_trade({
            "symbol": "4114", "side": "BUY", "price": 33.8, "shares": 1000,
            "buyDate": "2026-07-08 10:30:00", "status": "filled",
        })
        backend.record_trade({
            "symbol": "4114", "side": "BUY", "price": 33.15, "shares": 1000,
            "buyDate": "2026-07-09 10:45:00", "status": "filled",
        })
        holding = {
            "4114": {"code": "4114", "shares": 2000, "quantity": 2, "price": 33.475}
        }
        with pytest.raises(ValueError, match="必須逐筆提供 tradeId"):
            backend.lock_existing_position_horizons(
                [{"symbol": "4114", "strategyHorizon": "short_trade"}],
                holding,
                apply=False,
            )
        with pytest.raises(ValueError, match="必須逐筆提供 tradeId"):
            backend.lock_existing_position_horizon(
                "4114", "short_trade", holding["4114"]
            )

        lot_assignments = [
            {"symbol": "4114", "tradeId": 1, "strategyHorizon": "short_trade"},
            {"symbol": "4114", "tradeId": 2, "strategyHorizon": "mid_swing"},
        ]
        preview = backend.lock_existing_position_horizons(
            lot_assignments, holding, apply=False
        )
        applied = backend.lock_existing_position_horizons(
            lot_assignments, holding, apply=True
        )
        with backend.connect() as conn:
            rows = conn.execute(
                "SELECT id, strategy_horizon, strategy_horizon_source "
                "FROM trades ORDER BY id"
            ).fetchall()

    assert preview["assignmentCount"] == 2
    assert [lot["tradeId"] for lot in preview["requiredLots"]] == [1, 2]
    assert [lot["shares"] for lot in preview["requiredLots"]] == [1000, 1000]
    assert applied["auditId"] is not None
    assert [tuple(row) for row in rows] == [
        (1, "short_trade", "manual_batch_lot_lock"),
        (2, "mid_swing", "manual_batch_lot_lock"),
    ]


def test_stale_preview_cannot_partially_overwrite_an_already_locked_lot():
    with tempfile.TemporaryDirectory() as tmp:
        backend = build_backend(tmp)
        preview = backend.lock_existing_position_horizons(
            assignments(), holdings(), apply=False
        )
        assert preview["assignmentCount"] == 2
        with backend.connect() as conn:
            conn.execute(
                "UPDATE trades SET strategy_horizon='mid_swing', "
                "strategy_horizon_source='concurrent_lock' WHERE id=2"
            )
        with pytest.raises(ValueError, match="禁止覆寫"):
            backend.lock_existing_position_horizons(
                assignments(), holdings(), apply=True
            )
        with backend.connect() as conn:
            rows = conn.execute(
                "SELECT id, strategy_horizon FROM trades ORDER BY id"
            ).fetchall()
            audits = conn.execute(
                "SELECT COUNT(*) FROM portfolio_horizon_lock_batches"
            ).fetchone()[0]

    assert [tuple(row) for row in rows] == [(1, "unknown"), (2, "mid_swing")]
    assert audits == 0


def test_removed_legacy_bulk_lock_is_reverted_once_with_append_only_audit():
    with tempfile.TemporaryDirectory() as tmp:
        backend = build_backend(tmp)
        with backend.connect() as conn:
            conn.execute(
                "UPDATE trades SET strategy_horizon='short_trade', "
                "strategy_horizon_source='manual_legacy_position_lock', "
                "strategy_horizon_locked_at='2026-07-11 19:42:00' WHERE id=1"
            )
            conn.execute(
                "UPDATE trades SET strategy_horizon='long_trend', "
                "strategy_horizon_source='manual_batch_lot_lock', "
                "strategy_horizon_locked_at='2026-07-11 19:43:00' WHERE id=2"
            )

        preview = backend.revert_unintended_legacy_horizon_locks(apply=False)
        applied = backend.revert_unintended_legacy_horizon_locks(apply=True)
        repeated = backend.revert_unintended_legacy_horizon_locks(apply=True)
        with backend.connect() as conn:
            rows = conn.execute(
                "SELECT id, strategy_horizon, strategy_horizon_source, "
                "strategy_horizon_locked_at FROM trades ORDER BY id"
            ).fetchall()
            audit = conn.execute(
                "SELECT assignment_count, payload_json "
                "FROM portfolio_horizon_lock_batches"
            ).fetchone()

    assert preview["candidateCount"] == 1
    assert applied["changed"] == 1
    assert applied["auditId"] is not None
    assert repeated["alreadyApplied"] is True
    assert [tuple(row) for row in rows] == [
        (1, "unknown", "legacy_bulk_lock_reverted", None),
        (2, "long_trend", "manual_batch_lot_lock", "2026-07-11 19:43:00"),
    ]
    assert audit[0] == 1
    assert '"source":"legacy_manual_horizon_revert_v1"' in audit[1]
