import datetime as dt
import json
import tempfile
from pathlib import Path

from ml_backend import StockMLBackend


def backend_with_prices(tmp_dir, count=30):
    backend = StockMLBackend()
    backend.db_path = Path(tmp_dir) / "stock_system.sqlite3"
    backend.init_db()
    start = dt.date(2026, 1, 1)
    with backend.connect() as conn:
        for index in range(count):
            day = (start + dt.timedelta(days=index)).isoformat()
            conn.execute("""
                INSERT INTO prices (
                    symbol, date, open, high, low, close, volume, updated_at, price_source
                ) VALUES ('2330', ?, 100, 101, 99, 100, 1000000, ?, 'TWSE official')
            """, (day, f"{day} 14:00:00"))
    return backend, start


def create_verified_exit(backend, start):
    buy_date = (start + dt.timedelta(days=9)).isoformat()
    decision_date = (start + dt.timedelta(days=29)).isoformat()
    backend.record_trade({
        "symbol": "2330",
        "side": "BUY",
        "price": 100,
        "shares": 1000,
        "buyDate": buy_date,
        "status": "filled",
        "strategyHorizon": "short_trade",
    })
    holding = {
        "code": "2330",
        "name": "台積電",
        "shares": 1000,
        "price": 100,
        "currentPrice": 90,
        "openPrice": 90,
        "highPrice": 91,
        "lowPrice": 89,
        "totalVolume": 1_000_000,
        "snapshotAt": f"{decision_date} 13:30:00",
        "quoteFresh": True,
        "sessionFinal": True,
    }
    result = backend.portfolio_exit_analysis(
        {"2330": holding},
        summary={"updatedAt": f"{decision_date} 13:30:00"},
        evaluation_date=decision_date,
        persist=True,
    )
    assert result["items"][0]["decisionVerified"] is True
    return holding, decision_date


def append_future_prices(backend, start, count=60):
    with backend.connect() as conn:
        for offset in range(30, 30 + count):
            day = (start + dt.timedelta(days=offset)).isoformat()
            close = 89 - (offset - 30) * 0.2
            conn.execute("""
                INSERT INTO prices (
                    symbol, date, open, high, low, close, volume, updated_at, price_source
                ) VALUES ('2330', ?, ?, ?, ?, ?, 1000000, ?, 'TWSE official')
            """, (day, close, close + 1, close - 1, close, f"{day} 14:00:00"))


def test_history_is_append_only_and_same_decision_event_is_deduplicated():
    with tempfile.TemporaryDirectory() as tmp:
        backend, start = backend_with_prices(tmp)
        holding, decision_date = create_verified_exit(backend, start)
        backend.portfolio_exit_analysis(
            {"2330": holding},
            evaluation_date=decision_date,
            persist=True,
        )
        with backend.connect() as conn:
            latest_count = conn.execute("SELECT COUNT(1) FROM portfolio_exit_snapshots").fetchone()[0]
            history_count = conn.execute("SELECT COUNT(1) FROM portfolio_exit_history").fetchone()[0]
        assert latest_count == 1
        assert history_count == 1
        assert backend.list_portfolio_exit_history()["items"][0]["decision_type"] == "time_stop"


def test_verified_exit_settles_six_future_windows_and_builds_performance():
    with tempfile.TemporaryDirectory() as tmp:
        backend, start = backend_with_prices(tmp)
        create_verified_exit(backend, start)
        append_future_prices(backend, start, count=60)
        as_of = (start + dt.timedelta(days=89)).isoformat()

        settlement = backend.settle_portfolio_exit_history(as_of_date=as_of)
        performance = backend.portfolio_exit_performance()

        assert settlement["settled"] == 6
        assert settlement["pending"] == 0
        assert performance["outcomeCount"] == 6
        assert performance["historyCount"] == 1
        assert performance["verifiedEventCount"] == 1
        assert performance["pendingEvents"] == 0
        groups = performance["groups"]
        assert [group["horizonDays"] for group in groups] == [1, 3, 5, 10, 20, 60]
        assert all(group["policyVersion"] == "portfolio-exit-v2" for group in groups)
        assert all(group["strategyHorizon"] == "short_trade" for group in groups)
        assert all(group["precision"] == 1 for group in groups)
        assert all(group["netPnl"] > 0 for group in groups)
        assert all(group["prematureSellRate"] == 0 for group in groups)


def test_exit_performance_never_mixes_different_policy_versions():
    with tempfile.TemporaryDirectory() as tmp:
        backend, start = backend_with_prices(tmp)
        create_verified_exit(backend, start)
        with backend.connect() as conn:
            conn.execute("""
                INSERT INTO portfolio_exit_history (
                    event_key, symbol, decision_date, generated_at,
                    policy_version, strategy_horizon, decision_type,
                    decision_verified, trade_id, buy_date, signal_price,
                    shares, sell_shares, payload_json, created_at
                )
                SELECT event_key || '-legacy', symbol, decision_date, generated_at,
                       'portfolio-exit-v1', strategy_horizon, decision_type,
                       decision_verified, trade_id, buy_date, signal_price,
                       shares, sell_shares, payload_json, created_at
                FROM portfolio_exit_history
            """)
        append_future_prices(backend, start, count=60)
        performance = backend.portfolio_exit_performance(
            refresh=True,
            as_of_date=(start + dt.timedelta(days=89)).isoformat(),
        )

        policies = {group["policyVersion"] for group in performance["groups"]}
        assert policies == {"portfolio-exit-v1", "portfolio-exit-v2"}
        assert len(performance["groups"]) == 12


def test_existing_latest_snapshot_seeds_history_once_after_deployment():
    with tempfile.TemporaryDirectory() as tmp:
        backend, start = backend_with_prices(tmp)
        create_verified_exit(backend, start)
        with backend.connect() as conn:
            conn.execute("DELETE FROM portfolio_exit_history")

        first = backend.backfill_portfolio_exit_history_from_snapshots()
        second = backend.backfill_portfolio_exit_history_from_snapshots()

        assert first["checked"] == 1
        assert first["inserted"] == 1
        assert second["inserted"] == 0
        assert backend.list_portfolio_exit_history()["count"] == 1


def test_unverified_observation_is_historized_but_not_scored_as_exit():
    with tempfile.TemporaryDirectory() as tmp:
        backend, start = backend_with_prices(tmp)
        decision_date = (start + dt.timedelta(days=29)).isoformat()
        holding = {
            "code": "2330", "shares": 1000, "price": 100, "currentPrice": 100,
            "snapshotAt": f"{decision_date} 13:30:00", "quoteFresh": True, "sessionFinal": True,
        }
        backend.portfolio_exit_analysis(
            {"2330": holding}, evaluation_date=decision_date, persist=True
        )
        append_future_prices(backend, start, count=60)
        settlement = backend.settle_portfolio_exit_history(
            as_of_date=(start + dt.timedelta(days=89)).isoformat()
        )
        assert backend.list_portfolio_exit_history()["count"] == 1
        assert settlement["considered"] == 0
        performance = backend.portfolio_exit_performance()
        assert performance["outcomeCount"] == 0
        assert performance["historyCount"] == 1
        assert performance["verifiedEventCount"] == 0


def test_invalid_history_is_preserved_but_excluded_from_settlement_and_performance():
    with tempfile.TemporaryDirectory() as tmp:
        backend, start = backend_with_prices(tmp)
        create_verified_exit(backend, start)
        with backend.connect() as conn:
            row = conn.execute(
                "SELECT id, payload_json FROM portfolio_exit_history"
            ).fetchone()
            payload = json.loads(row[1])
            payload.update({
                "volumeRatio": 0.001,
                "priceSource": "Shioaji / 永豐庫存",
            })
            conn.execute("""
                UPDATE portfolio_exit_history
                SET decision_date = '2026-07-13',
                    decision_type = 'phase2',
                    payload_json = ?
                WHERE id = ?
            """, (json.dumps(payload, ensure_ascii=False), row[0]))

        backend.init_db()
        append_future_prices(backend, start, count=60)
        settlement = backend.settle_portfolio_exit_history(
            as_of_date=(start + dt.timedelta(days=89)).isoformat()
        )
        performance = backend.portfolio_exit_performance()
        history = backend.list_portfolio_exit_history()["items"][0]

        assert history["invalid_for_trading"] == 1
        assert history["invalid_reason"] == "broker_volume_lots_compared_with_daily_shares"
        assert settlement["considered"] == 0
        assert performance["verifiedEventCount"] == 0
        assert performance["invalidEventCount"] == 1
        assert performance["outcomeCount"] == 0


def test_corrected_phase2_revision_gets_a_new_append_only_event_key():
    item = {
        "symbol": "2330",
        "decisionDate": "2026-07-13",
        "policyVersion": "portfolio-exit-v1",
        "strategyHorizon": "short_trade",
        "type": "phase2",
        "decisionVerified": True,
        "tradeId": 1,
        "buyDate": "2026-07-01",
        "sellShares": 1000,
        "status": "短期量縮轉弱",
    }
    old_key = StockMLBackend.portfolio_exit_event_key(item)
    item["calculationRevision"] = "volume-shares-v2"
    corrected_key = StockMLBackend.portfolio_exit_event_key(item)

    assert corrected_key != old_key
