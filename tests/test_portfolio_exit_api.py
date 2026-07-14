import datetime as dt
import http.client
import json
import os
import sys
import tempfile
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server as server_module
from ml_backend import StockMLBackend


def build_backend(tmp_dir, evaluation_date=None):
    evaluation_date = evaluation_date or dt.date(2026, 7, 10)
    backend = StockMLBackend()
    backend.db_path = Path(tmp_dir) / "stock_system.sqlite3"
    backend.init_db()
    start = evaluation_date - dt.timedelta(days=29)
    buy_date = evaluation_date - dt.timedelta(days=10)
    with backend.connect() as conn:
        for index in range(30):
            date = (start + dt.timedelta(days=index)).isoformat()
            conn.execute("""
                INSERT INTO prices (
                    symbol, date, open, high, low, close, volume, updated_at, price_source
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                "2330", date, 103, 104, 102, 103, 1_000_000,
                f"{date} 14:00:00", "TWSE official",
            ))
    backend.record_trade({
        "symbol": "2330",
        "side": "BUY",
        "price": 100,
        "shares": 1000,
        "buyDate": f"{buy_date.isoformat()} 09:31:00",
        "status": "filled",
        "strategyHorizon": "short_trade",
    })
    cache = {
        "summary": {"updatedAt": f"{evaluation_date.isoformat()} 13:30:00"},
        "holdings": {
            "2330": {
                "code": "2330",
                "name": "台積電",
                "shares": 1000,
                "quantity": 1,
                "price": 100,
                "currentPrice": 103,
                "openPrice": 103,
                "highPrice": 104,
                "lowPrice": 102,
                "totalVolume": 1_000_000,
                "snapshotAt": f"{evaluation_date.isoformat()} 13:30:00",
                "quoteFresh": True,
                "sessionFinal": True,
            }
        },
    }
    with backend.connect() as conn:
        backend.set_meta(conn, "portfolio_summary_cache", json.dumps(cache, ensure_ascii=False))
    return backend


class TestPortfolioExitApi:
    @classmethod
    def setup_class(cls):
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server_module.StockHandler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def teardown_class(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.thread.join(timeout=5)

    def get_json(self, path):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=20)
        try:
            conn.request("GET", path)
            response = conn.getresponse()
            payload = json.loads(response.read().decode("utf-8"))
            return response.status, payload
        finally:
            conn.close()

    def post_json(self, path, body):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=20)
        try:
            conn.request(
                "POST",
                path,
                body=json.dumps(body),
                headers={
                    "Content-Type": "application/json",
                    "Origin": f"http://127.0.0.1:{self.port}",
                },
            )
            response = conn.getresponse()
            payload = json.loads(response.read().decode("utf-8"))
            return response.status, payload
        finally:
            conn.close()

    def test_api_returns_same_backend_policy_and_locked_fifo_result(self):
        evaluation_date = dt.date.today()
        with tempfile.TemporaryDirectory() as tmp:
            backend = build_backend(tmp, evaluation_date=evaluation_date)
            with patch.object(server_module, "backend", backend):
                status, payload = self.get_json("/api/portfolio/exit-analysis")
        assert status == 200
        assert payload["ok"] is True
        assert payload["policy"]["version"] == "portfolio-exit-v2"
        assert payload["policy"]["horizons"]["short_trade"]["fixedStopIsPrimary"] is False
        assert payload["policy"]["horizons"]["short_trade"]["reboundRisk"]["calibratedProbability"] is False
        assert payload["source"] == "backend_canonical_live"
        assert payload["counts"]["positions"] == 1
        item = payload["items"][0]
        assert item["strategyHorizon"] == "short_trade"
        assert item["buyDate"] == (evaluation_date - dt.timedelta(days=10)).isoformat()
        assert item["buyDateKnown"] is True
        assert item["type"] == "hold"
        assert item["decisionVerified"] is False
        assert len(payload["alerts"]) == 3
        assert item["brokerAveragePrice"] == 100
        assert all(alert["holdingPrice"] == 100 for alert in payload["alerts"])
        assert all(alert["holdingPriceSource"] == "broker_average" for alert in payload["alerts"])

    def test_snapshot_persistence_contains_the_identical_policy_payload(self):
        with tempfile.TemporaryDirectory() as tmp:
            backend = build_backend(tmp)
            with backend.connect() as conn:
                row = conn.execute(
                    "SELECT value FROM model_meta WHERE key = 'portfolio_summary_cache'"
                ).fetchone()
            cached = json.loads(row[0])
            live = backend.portfolio_exit_analysis(
                cached["holdings"], cached["summary"], evaluation_date="2026-07-10", persist=True
            )
            snapshots = backend.list_portfolio_exit_snapshots()
        assert snapshots["policy"]["version"] == live["policy"]["version"]
        assert snapshots["items"][0]["type"] == live["items"][0]["type"]
        assert snapshots["items"][0]["strategyHorizon"] == "short_trade"

    def test_typhoon_holiday_summary_timestamp_does_not_become_a_daily_bar(self):
        with tempfile.TemporaryDirectory() as tmp:
            backend = StockMLBackend()
            backend.db_path = Path(tmp) / "stock_system.sqlite3"
            backend.init_db()
            with backend.connect() as conn:
                for day in ("2026-07-07", "2026-07-08", "2026-07-09"):
                    conn.execute("""
                        INSERT INTO prices (
                            symbol, date, open, high, low, close, volume,
                            updated_at, price_source
                        ) VALUES ('2330', ?, 100, 101, 99, 100, 1000000, ?, 'TWSE official')
                    """, (day, f"{day} 14:00:00"))
            backend.record_trade({
                "symbol": "2330",
                "side": "BUY",
                "price": 100,
                "shares": 1000,
                "buyDate": "2026-07-08 09:31:00",
                "status": "filled",
                "strategyHorizon": "short_trade",
            })
            result = backend.portfolio_exit_analysis(
                {
                    "2330": {
                        "code": "2330",
                        "shares": 1000,
                        "price": 100,
                        "currentPrice": 108,
                        "quoteFresh": False,
                    }
                },
                summary={"updatedAt": "2026-07-10 13:30:00"},
                evaluation_date="2026-07-10",
                persist=False,
            )

        item = result["items"][0]
        assert item["quoteAt"] == "2026-07-10 13:30:00"
        assert item["dataDate"] == "2026-07-09"
        assert item["historyRows"] == 3
        assert item["quoteFresh"] is False
        assert item["decisionVerified"] is False

    def test_legacy_horizon_api_locks_unknown_fill_once_and_rebuilds_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            backend = build_backend(tmp)
            with backend.connect() as conn:
                conn.execute("""
                    UPDATE trades
                    SET strategy_horizon = 'unknown',
                        strategy_horizon_source = 'external_fill_unknown',
                        strategy_horizon_locked_at = NULL
                    WHERE symbol = '2330' AND side = 'BUY'
                """)
            with patch.object(server_module, "backend", backend):
                status, payload = self.post_json(
                    "/api/portfolio/strategy-horizon",
                    {"symbol": "2330", "tradeId": 1, "strategyHorizon": "long_trend"},
                )
                second_status, second_payload = self.post_json(
                    "/api/portfolio/strategy-horizon",
                    {"symbol": "2330", "tradeId": 1, "strategyHorizon": "short_trade"},
                )

            assert status == 200
            assert payload["ok"] is True
            assert payload["lock"]["strategyHorizon"] == "long_trend"
            assert payload["lock"]["buyDateKnown"] is True
            assert payload["item"]["strategyHorizon"] == "long_trend"
            assert payload["item"]["buyDate"] == "2026-06-30"
            assert payload["item"]["hasUnknownHorizon"] is False
            assert second_status == 400
            assert second_payload["ok"] is False
            assert "已鎖定" in second_payload["error"]

    def test_batch_horizon_api_previews_then_requires_explicit_apply_confirmation(self):
        with tempfile.TemporaryDirectory() as tmp:
            backend = build_backend(tmp)
            with backend.connect() as conn:
                conn.execute("""
                    UPDATE trades
                    SET strategy_horizon = 'unknown',
                        strategy_horizon_source = 'external_fill_unknown',
                        strategy_horizon_locked_at = NULL
                    WHERE symbol = '2330' AND side = 'BUY'
                """)
            assignment = [{
                "symbol": "2330", "tradeId": 1, "strategyHorizon": "mid_swing",
            }]
            with patch.object(server_module, "backend", backend):
                preview_status, preview = self.post_json(
                    "/api/portfolio/strategy-horizons/batch",
                    {"mode": "preview", "assignments": assignment},
                )
                rejected_status, rejected = self.post_json(
                    "/api/portfolio/strategy-horizons/batch",
                    {"mode": "apply", "assignments": assignment},
                )
                apply_status, applied = self.post_json(
                    "/api/portfolio/strategy-horizons/batch",
                    {"mode": "apply", "confirmAll": True, "assignments": assignment},
                )

            assert preview_status == 200
            assert preview["batch"]["preview"] is True
            assert preview["batch"]["requiredTradeIds"] == [1]
            assert preview["batch"]["requiredLots"][0]["buyDate"] == "2026-06-30"
            assert rejected_status == 400
            assert "明確確認" in rejected["error"]
            assert apply_status == 200
            assert applied["batch"]["applied"] is True
            assert applied["counts"]["unknownHorizon"] == 0

    def test_legacy_lot_api_imports_only_uncovered_broker_shares(self):
        with tempfile.TemporaryDirectory() as tmp:
            backend = build_backend(tmp)
            with backend.connect() as conn:
                cache = json.loads(conn.execute(
                    "SELECT value FROM model_meta WHERE key = 'portfolio_summary_cache'"
                ).fetchone()[0])
                cache["holdings"]["2330"]["shares"] = 2000
                cache["holdings"]["2330"]["quantity"] = 2
                backend.set_meta(conn, "portfolio_summary_cache", json.dumps(cache, ensure_ascii=False))
            with patch.object(server_module, "backend", backend):
                status, payload = self.post_json(
                    "/api/portfolio/legacy-lots",
                    {
                        "symbol": "2330",
                        "lots": [{
                            "buyDate": "2026-05-20",
                            "price": 100,
                            "shares": 1000,
                            "strategyHorizon": "mid_swing",
                        }],
                    },
                )

            assert status == 200
            assert payload["ok"] is True
            assert payload["import"]["migratedShares"] == 1000
            assert payload["item"]["brokerUncoveredShares"] == 0
            assert payload["item"]["migratableShares"] == 0
            assert payload["item"]["positionBuyDateKnown"] is True
            assert len(backend.fifo_open_trade_lots("2330", 2000)["lots"]) == 2
