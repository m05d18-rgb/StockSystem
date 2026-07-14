"""
trades.buy_at 的回歸測試。

買進日不能再只靠 created_at 推估；手動記錄或系統下單時要把實際/當下買進
時間寫進本地 trades 表，複盤統計也要優先讀 buy_at。
"""
import json
import os
import sys
import tempfile
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
import http.client

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server as server_module
from ml_backend import StockMLBackend
from sinopac_backend import SinoPacBackend


class TradeBuyDateStorageTests(unittest.TestCase):
    def _backend(self, tmp_dir):
        backend = StockMLBackend()
        backend.db_path = Path(tmp_dir) / "stock_system.sqlite3"
        backend.init_db()
        return backend

    def test_record_trade_persists_explicit_buy_date(self):
        with tempfile.TemporaryDirectory() as tmp:
            backend = self._backend(tmp)
            saved = backend.record_trade({
                "symbol": "2330",
                "side": "BUY",
                "price": 100,
                "shares": 1000,
                "buyDate": "2026-07-02 09:31:00",
                "status": "submitted",
            })
            self.assertTrue(saved["ok"])
            trade = backend.list_trades(1)[0]
            self.assertEqual(trade["buy_at"], "2026-07-02 09:31:00")
            self.assertEqual(trade["buyDate"], "2026-07-02")
            self.assertEqual(trade["sellDate"], "")
            self.assertIsNone(trade["pnlPct"])

    def test_filled_buy_locks_strategy_horizon_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            backend = self._backend(tmp)
            backend.record_trade({
                "symbol": "2330",
                "side": "BUY",
                "price": 100,
                "shares": 1000,
                "buyDate": "2026-07-02 09:31:00",
                "status": "filled",
                "strategyHorizon": "mid_swing",
            })
            trade = backend.list_trades(1)[0]
            self.assertEqual(trade["strategy_horizon"], "mid_swing")
            self.assertEqual(trade["strategyHorizon"], "mid_swing")
            self.assertEqual(trade["strategy_horizon_source"], "order_entry")
            self.assertEqual(trade["strategy_horizon_locked_at"], "2026-07-02 09:31:00")

    def test_submitted_buy_does_not_claim_a_buy_date_before_fill(self):
        with tempfile.TemporaryDirectory() as tmp:
            backend = self._backend(tmp)
            backend.record_trade({
                "symbol": "2330",
                "side": "BUY",
                "price": 100,
                "shares": 1000,
                "status": "submitted",
                "strategyHorizon": "short_trade",
            })
            trade = backend.list_trades(1)[0]
            self.assertIsNone(trade["buy_at"])
            self.assertEqual(trade["buyDate"], "")
            self.assertIsNone(trade["strategy_horizon_locked_at"])

    def test_legacy_position_lock_preserves_real_fifo_buy_date_and_is_immutable(self):
        with tempfile.TemporaryDirectory() as tmp:
            backend = self._backend(tmp)
            backend.record_trade({
                "symbol": "2330",
                "side": "BUY",
                "price": 100,
                "shares": 1000,
                "buyDate": "2026-06-25 09:31:00",
                "status": "filled",
            })
            holding = {"code": "2330", "shares": 1000, "price": 100}

            locked = backend.lock_existing_position_horizon("2330", "mid_swing", holding)
            lot = backend.fifo_open_trade_lots("2330", 1000)["lots"][0]

            self.assertTrue(locked["ok"])
            self.assertTrue(locked["buyDateKnown"])
            self.assertEqual(locked["syntheticShares"], 0)
            self.assertEqual(lot["buyDate"], "2026-06-25")
            self.assertEqual(lot["strategyHorizon"], "mid_swing")
            self.assertEqual(lot["strategyHorizonSource"], "manual_legacy_position_lock")
            with self.assertRaisesRegex(ValueError, "已鎖定"):
                backend.lock_existing_position_horizon("2330", "long_trend", holding)

    def test_legacy_position_without_fill_keeps_unknown_buy_date_and_disables_time_exit(self):
        with tempfile.TemporaryDirectory() as tmp:
            backend = self._backend(tmp)
            holding = {
                "code": "2330",
                "name": "台積電",
                "shares": 1000,
                "price": 100,
                "currentPrice": 103,
                "snapshotAt": "2026-07-10 13:30:00",
            }

            locked = backend.lock_existing_position_horizon("2330", "short_trade", holding)
            trade = backend.list_trades(1)[0]
            lot = backend.fifo_open_trade_lots("2330", 1000)["lots"][0]
            analysis = backend.portfolio_exit_analysis(
                {"2330": holding},
                summary={"updatedAt": "2026-07-10 13:30:00"},
                evaluation_date="2026-07-10",
                persist=False,
            )

            self.assertEqual(locked["syntheticShares"], 1000)
            self.assertFalse(locked["buyDateKnown"])
            self.assertIsNone(trade["buy_at"])
            self.assertIsNone(trade["filled_at"])
            self.assertEqual(trade["strategy_horizon"], "short_trade")
            self.assertFalse(lot["buyDateKnown"])
            self.assertEqual(analysis["items"][0]["strategyHorizon"], "short_trade")
            self.assertFalse(analysis["items"][0]["positionBuyDateKnown"])
            self.assertNotEqual(analysis["items"][0]["type"], "time_stop")

    def test_legacy_lot_import_reconciles_uncovered_shares_by_real_batches(self):
        with tempfile.TemporaryDirectory() as tmp:
            backend = self._backend(tmp)
            holding = {"code": "2330", "shares": 2000, "price": 95}
            result = backend.import_legacy_position_lots("2330", [
                {"buyDate": "2026-05-05", "price": 90, "shares": 1000, "strategyHorizon": "mid_swing"},
                {"buyDate": "2026-06-02", "price": 100, "shares": 1000, "strategyHorizon": "long_trend"},
            ], holding)
            reconciliation = backend.fifo_open_trade_lots("2330", 2000)

            self.assertTrue(result["ok"])
            self.assertEqual(result["migratedShares"], 2000)
            self.assertEqual(result["costVariance"], 0)
            self.assertEqual(reconciliation["unknownShares"], 0)
            self.assertEqual([lot["buyDate"] for lot in reconciliation["lots"]], ["2026-05-05", "2026-06-02"])
            self.assertEqual(
                [lot["strategyHorizon"] for lot in reconciliation["lots"]],
                ["mid_swing", "long_trend"],
            )
            with backend.connect() as conn:
                audit = conn.execute("SELECT * FROM legacy_lot_imports WHERE symbol = '2330'").fetchone()
            self.assertIsNotNone(audit)

    def test_legacy_lot_import_only_fills_broker_gap_and_preserves_real_fill(self):
        with tempfile.TemporaryDirectory() as tmp:
            backend = self._backend(tmp)
            backend.record_trade({
                "symbol": "4114", "side": "BUY", "price": 33.25, "shares": 1000,
                "buyDate": "2026-07-09 10:46:52", "status": "filled",
            })
            holding = {"code": "4114", "shares": 3000, "price": 34}
            result = backend.import_legacy_position_lots("4114", [
                {"buyDate": "2026-06-10", "price": 34.5, "shares": 1000, "strategyHorizon": "short_trade"},
                {"buyDate": "2026-06-20", "price": 34.75, "shares": 1000, "strategyHorizon": "mid_swing"},
            ], holding)
            trades = backend.list_trades(10)

            self.assertEqual(result["migratedShares"], 2000)
            self.assertEqual(len(trades), 3)
            real = next(row for row in trades if row["strategy_horizon_source"] == "not_provided")
            self.assertEqual(real["price"], 33.25)
            self.assertEqual(real["buy_at"], "2026-07-09 10:46:52")

    def test_legacy_lot_import_rejects_share_mismatch_without_writing(self):
        with tempfile.TemporaryDirectory() as tmp:
            backend = self._backend(tmp)
            holding = {"code": "2330", "shares": 2000, "price": 100}
            with self.assertRaisesRegex(ValueError, "必須等於待補 2000 股"):
                backend.import_legacy_position_lots("2330", [
                    {"buyDate": "2026-05-05", "price": 100, "shares": 1000, "strategyHorizon": "short_trade"},
                ], holding)
            self.assertEqual(backend.list_trades(10), [])

    def test_legacy_lot_import_can_replace_placeholder_but_not_locked_horizon(self):
        with tempfile.TemporaryDirectory() as tmp:
            backend = self._backend(tmp)
            holding = {"code": "2330", "shares": 2000, "price": 100}
            backend.lock_existing_position_horizon("2330", "long_trend", holding)
            with self.assertRaisesRegex(ValueError, "不得改變"):
                backend.import_legacy_position_lots("2330", [
                    {"buyDate": "2026-05-05", "price": 99, "shares": 2000, "strategyHorizon": "short_trade"},
                ], holding)
            result = backend.import_legacy_position_lots("2330", [
                {"buyDate": "2026-05-05", "price": 99, "shares": 1000, "strategyHorizon": "long_trend"},
                {"buyDate": "2026-06-05", "price": 101, "shares": 1000, "strategyHorizon": "long_trend"},
            ], holding)

            self.assertEqual(result["replacedTradeIds"], [1])
            self.assertEqual(len(result["importedTradeIds"]), 2)
            self.assertEqual({lot["strategyHorizon"] for lot in backend.fifo_open_trade_lots("2330", 2000)["lots"]}, {"long_trend"})

    def test_trade_journal_stats_prefers_buy_at_over_created_at(self):
        with tempfile.TemporaryDirectory() as tmp:
            backend = self._backend(tmp)
            with backend.connect() as conn:
                conn.execute("""
                    INSERT INTO trades (
                        created_at, buy_at, symbol, side, price, shares, status, exit_price, exit_at, pnl
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    "2026-07-09 21:00:00", "2026-07-02 09:31:00", "2330", "BUY",
                    100, 1000, "submitted", 110, "2026-07-05 13:30:00", 10000,
                ))
            with patch.object(server_module, "backend", backend):
                result = server_module.trade_journal_stats()
            self.assertTrue(result["ok"])
            self.assertEqual(result["trades"][0]["buyDate"], "2026-07-02")
            self.assertEqual(result["trades"][0]["holdDays"], 3)

    def test_sync_sinopac_order_fills_updates_matching_buy_trade(self):
        with tempfile.TemporaryDirectory() as tmp:
            backend = self._backend(tmp)
            backend.record_trade({
                "symbol": "2330",
                "side": "BUY",
                "price": 100,
                "shares": 1000,
                "status": "submitted",
                "brokerSeqno": "S123",
                "brokerOrdno": "O456",
                "brokerOrderId": "ID789",
                "strategyHorizon": "long_trend",
            })
            result = backend.sync_sinopac_order_fills([{
                "code": "2330",
                "action": "BUY",
                "price": 101.5,
                "shares": 1000,
                "dealAt": "2026-07-03 09:32:10",
                "brokerSeqno": "S123",
                "brokerOrdno": "O456",
                "brokerOrderId": "ID789",
                "raw": {"sample": True},
            }])
            self.assertEqual(result["updatedTrades"], 1)
            trade = backend.list_trades(1)[0]
            self.assertEqual(trade["buy_at"], "2026-07-03 09:32:10")
            self.assertEqual(trade["filled_at"], "2026-07-03 09:32:10")
            self.assertEqual(trade["filled_shares"], 1000)
            self.assertEqual(trade["status"], "filled")
            self.assertEqual(trade["price"], 101.5)
            self.assertEqual(trade["strategy_horizon"], "long_trend")
            self.assertEqual(trade["strategy_horizon_locked_at"], "2026-07-03 09:32:10")

    def test_sync_sinopac_order_fills_does_not_guess_without_broker_refs(self):
        with tempfile.TemporaryDirectory() as tmp:
            backend = self._backend(tmp)
            backend.record_trade({
                "symbol": "2330",
                "side": "BUY",
                "price": 100,
                "shares": 1000,
                "status": "submitted",
            })
            result = backend.sync_sinopac_order_fills([{
                "code": "2330",
                "action": "BUY",
                "price": 101.5,
                "shares": 1000,
                "dealAt": "2026-07-03 09:32:10",
            }])
            self.assertEqual(result["updatedTrades"], 0)
            self.assertEqual(result["unmatched"], 1)
            self.assertNotEqual(backend.list_trades(1)[0]["buy_at"], "2026-07-03 09:32:10")

    def test_sync_external_buy_fill_creates_local_filled_trade(self):
        with tempfile.TemporaryDirectory() as tmp:
            backend = self._backend(tmp)
            result = backend.sync_sinopac_order_fills([{
                "code": "2330",
                "action": "BUY",
                "price": 101.5,
                "shares": 1000,
                "dealAt": "2026-07-03 09:32:10",
                "brokerOrderId": "ID789",
                "brokerSeqno": "S123",
                "brokerOrdno": "O456",
            }])
            self.assertEqual(result["createdTrades"], 1)
            self.assertEqual(result["updatedTrades"], 1)
            self.assertTrue(result["details"][0]["createdLocalTrade"])
            trade = backend.list_trades(1)[0]
            self.assertEqual(trade["symbol"], "2330")
            self.assertEqual(trade["side"], "BUY")
            self.assertEqual(trade["status"], "filled")
            self.assertEqual(trade["buy_at"], "2026-07-03 09:32:10")
            self.assertEqual(trade["filled_shares"], 1000)
            self.assertEqual(trade["broker_order_id"], "ID789")
            self.assertEqual(trade["strategy_horizon"], "unknown")
            self.assertEqual(trade["strategy_horizon_source"], "external_fill_unknown")
            self.assertIsNone(trade["strategy_horizon_locked_at"])

    def test_sync_recurring_investment_fill_locks_long_term_horizon(self):
        with tempfile.TemporaryDirectory() as tmp:
            backend = self._backend(tmp)
            result = backend.sync_sinopac_order_fills([{
                "code": "2330",
                "action": "BUY",
                "price": 101.5,
                "shares": 1000,
                "dealAt": "2026-07-03 09:32:10",
                "brokerOrderId": "ID-SAVING",
                "brokerSeqno": "S-SAVING",
                "brokerOrdno": "O-SAVING",
                "raw": {"record": {"order_source": "stock_savings"}},
            }])
            self.assertEqual(result["createdTrades"], 1)
            trade = backend.list_trades(1)[0]
            self.assertEqual(trade["strategy_horizon"], "long_trend")
            self.assertEqual(
                trade["strategy_horizon_source"],
                "explicit_recurring_investment_evidence",
            )
            self.assertEqual(trade["strategy_horizon_locked_at"], "2026-07-03 09:32:10")

    def test_evidence_backfill_locks_only_explicit_recurring_investment_lot(self):
        with tempfile.TemporaryDirectory() as tmp:
            backend = self._backend(tmp)
            explicit = backend.record_trade({
                "symbol": "2330",
                "side": "BUY",
                "price": 101.5,
                "shares": 1000,
                "buyDate": "2026-07-03 09:32:10",
                "status": "filled",
                "strategyHorizon": "unknown",
                "strategyHorizonSource": "external_fill_unknown",
                "brokerOrderId": "ID-SAVING",
                "brokerSeqno": "SEQ-SAVING",
                "brokerOrdno": "ORD-SAVING",
            })
            ordinary = backend.record_trade({
                "symbol": "2317",
                "side": "BUY",
                "price": 88,
                "shares": 1000,
                "buyDate": "2026-07-03 09:35:00",
                "status": "filled",
                "strategyHorizon": "unknown",
                "strategyHorizonSource": "external_fill_unknown",
                "brokerOrderId": "ID-CASH",
                "brokerSeqno": "SEQ-CASH",
                "brokerOrdno": "ORD-CASH",
            })
            with backend.connect() as conn:
                conn.executemany("""
                    INSERT INTO sinopac_order_fills (
                        dedup_key, code, action, price, shares, deal_at,
                        broker_order_id, broker_seqno, broker_ordno,
                        source, raw_json, imported_at
                    ) VALUES (?, ?, 'BUY', ?, 1000, ?, ?, ?, ?, 'test', ?, ?)
                """, [
                    (
                        "saving", "2330", 101.5, "2026-07-03 09:32:10",
                        "ID-SAVING", "SEQ-SAVING", "ORD-SAVING",
                        json.dumps({"record": {"order_source": "stock_savings"}}),
                        "2026-07-12 10:00:00",
                    ),
                    (
                        "cash", "2317", 88, "2026-07-03 09:35:00",
                        "ID-CASH", "SEQ-CASH", "ORD-CASH",
                        json.dumps({"record": {"custom_field": ""}}),
                        "2026-07-12 10:00:00",
                    ),
                ])

            result = backend.backfill_strategy_horizons_from_execution_evidence(apply=True)
            with backend.connect() as conn:
                rows = {
                    int(row[0]): row
                    for row in conn.execute(
                        "SELECT id, strategy_horizon, strategy_horizon_source, "
                        "strategy_horizon_locked_at FROM trades WHERE id IN (?, ?)",
                        (explicit["id"], ordinary["id"]),
                    ).fetchall()
                }
                audit_count = conn.execute(
                    "SELECT COUNT(*) FROM strategy_horizon_evidence_audits"
                ).fetchone()[0]

        self.assertEqual(result["scannedLots"], 2)
        self.assertEqual(result["classifiedLots"], 1)
        self.assertEqual(result["updatedLots"], 1)
        self.assertEqual(rows[explicit["id"]][1], "long_trend")
        self.assertEqual(
            rows[explicit["id"]][2], "explicit_recurring_investment_evidence"
        )
        self.assertEqual(rows[explicit["id"]][3], "2026-07-03 09:32:10")
        self.assertEqual(rows[ordinary["id"]][1], "unknown")
        self.assertEqual(audit_count, 1)

    def test_sync_sell_fill_closes_single_matching_open_buy_trade(self):
        with tempfile.TemporaryDirectory() as tmp:
            backend = self._backend(tmp)
            backend.record_trade({
                "symbol": "2330",
                "side": "BUY",
                "price": 100,
                "shares": 1000,
                "buyDate": "2026-07-02 09:31:00",
                "status": "filled",
            })
            backend.record_trade({
                "symbol": "2330",
                "side": "SELL",
                "price": 110,
                "shares": 1000,
                "status": "submitted",
                "brokerSeqno": "SELL-S123",
                "brokerOrdno": "SELL-O456",
            })
            result = backend.sync_sinopac_order_fills([{
                "code": "2330",
                "action": "SELL",
                "price": 111.5,
                "shares": 1000,
                "dealAt": "2026-07-05 13:20:10",
                "brokerSeqno": "SELL-S123",
                "brokerOrdno": "SELL-O456",
            }])
            self.assertEqual(result["updatedTrades"], 1)
            self.assertEqual(result["closedTrades"], 1)
            rows = backend.list_trades(5)
            buy = next(row for row in rows if row["side"] == "BUY")
            sell = next(row for row in rows if row["side"] == "SELL")
            self.assertEqual(buy["exit_price"], 111.5)
            self.assertEqual(buy["exit_at"], "2026-07-05 13:20:10")
            self.assertEqual(buy["pnl"], 11500.0)
            self.assertEqual(buy["pnlPct"], 11.5)
            self.assertEqual(buy["sellDate"], "2026-07-05")
            self.assertEqual(buy["status"], "closed")
            self.assertEqual(sell["filled_shares"], 1000)
            self.assertEqual(sell["status"], "filled")

    def test_sync_sell_fill_uses_unique_sinopac_realized_pnl(self):
        with tempfile.TemporaryDirectory() as tmp:
            backend = self._backend(tmp)
            backend.save_realized_pnl([{
                "code": "2330",
                "quantity": 1,
                "price": 111.5,
                "pnl": 10880,
                "pr_ratio": 0.1088,
                "date": "2026-07-05",
                "seqno": "REALIZED-1",
            }])
            backend.record_trade({
                "symbol": "2330", "side": "BUY", "price": 100,
                "shares": 1000, "buyDate": "2026-07-02 09:31:00", "status": "filled",
            })
            backend.record_trade({
                "symbol": "2330", "side": "SELL", "price": 110,
                "shares": 1000, "status": "submitted", "brokerSeqno": "SELL-S123",
            })

            result = backend.sync_sinopac_order_fills([{
                "code": "2330", "action": "SELL", "price": 111.5, "shares": 1000,
                "dealAt": "2026-07-05 13:20:10", "brokerSeqno": "SELL-S123",
            }])

            self.assertEqual(result["realizedPnlReconciliation"]["matched"], 1)
            with backend.connect() as conn:
                row = conn.execute(
                    "SELECT pnl, pnl_pct, pnl_basis, realized_pnl_key FROM trades WHERE side='BUY'"
                ).fetchone()
            self.assertEqual(row[0], 10880.0)
            self.assertEqual(row[1], 10.88)
            self.assertEqual(row[2], "sinopac_realized")
            self.assertEqual(row[3], "REALIZED-1")

    def test_late_realized_pnl_import_replaces_existing_gross_execution(self):
        with tempfile.TemporaryDirectory() as tmp:
            backend = self._backend(tmp)
            backend.record_trade({
                "symbol": "2330", "side": "BUY", "price": 100,
                "shares": 1000, "buyDate": "2026-07-02 09:31:00", "status": "filled",
            })
            backend.record_trade({
                "symbol": "2330", "side": "SELL", "price": 110,
                "shares": 1000, "status": "submitted", "brokerSeqno": "SELL-S123",
            })
            backend.sync_sinopac_order_fills([{
                "code": "2330", "action": "SELL", "price": 111.5, "shares": 1000,
                "dealAt": "2026-07-05 13:20:10", "brokerSeqno": "SELL-S123",
            }])

            backend.save_realized_pnl([{
                "code": "2330", "quantity": 1, "price": 111.5,
                "pnl": 10880, "pr_ratio": 0.1088, "date": "2026-07-05",
                "seqno": "REALIZED-LATE",
            }])

            with backend.connect() as conn:
                row = conn.execute(
                    "SELECT pnl, pnl_pct, pnl_basis, realized_pnl_key FROM trades WHERE side='BUY'"
                ).fetchone()
            self.assertEqual(tuple(row), (10880.0, 10.88, "sinopac_realized", "REALIZED-LATE"))

    def test_realized_pnl_reconciliation_refuses_ambiguous_matches(self):
        with tempfile.TemporaryDirectory() as tmp:
            backend = self._backend(tmp)
            with backend.connect() as conn:
                for buy_price in (100, 101):
                    conn.execute("""
                        INSERT INTO trades (
                            created_at, buy_at, symbol, side, price, shares, status,
                            exit_price, exit_at, pnl, pnl_pct, pnl_basis
                        ) VALUES (?, ?, '2330', 'BUY', ?, 1000, 'closed',
                                  111.5, '2026-07-05 13:20:10', 0, 0, 'gross_execution')
                    """, ("2026-07-02 09:31:00", "2026-07-02 09:31:00", buy_price))
            backend.save_realized_pnl([
                {"code": "2330", "quantity": 1, "price": 111.5, "pnl": 10880,
                 "pr_ratio": 0.1088, "date": "2026-07-05", "seqno": "AMB-1"},
                {"code": "2330", "quantity": 1, "price": 111.5, "pnl": 9800,
                 "pr_ratio": 0.098, "date": "2026-07-05", "seqno": "AMB-2"},
            ])

            result = backend.reconcile_realized_pnl_to_trades("2026-07-05")

            self.assertEqual(result["matched"], 0)
            self.assertEqual(result["ambiguous"], 2)
            with backend.connect() as conn:
                rows = conn.execute(
                    "SELECT pnl_basis, realized_pnl_key FROM trades ORDER BY id"
                ).fetchall()
            self.assertEqual([tuple(row) for row in rows], [
                ("gross_execution", None), ("gross_execution", None),
            ])

    def test_sync_external_sell_fill_creates_sell_and_closes_open_buy(self):
        with tempfile.TemporaryDirectory() as tmp:
            backend = self._backend(tmp)
            backend.record_trade({
                "symbol": "2330",
                "side": "BUY",
                "price": 100,
                "shares": 1000,
                "buyDate": "2026-07-02 09:31:00",
                "status": "filled",
            })
            result = backend.sync_sinopac_order_fills([{
                "code": "2330",
                "action": "SELL",
                "price": 111.5,
                "shares": 1000,
                "dealAt": "2026-07-05 13:20:10",
                "brokerSeqno": "SELL-S123",
            }])
            self.assertEqual(result["createdTrades"], 1)
            self.assertEqual(result["closedTrades"], 1)
            rows = backend.list_trades(5)
            buy = next(row for row in rows if row["side"] == "BUY")
            sell = next(row for row in rows if row["side"] == "SELL")
            self.assertEqual(buy["status"], "closed")
            self.assertEqual(buy["exit_at"], "2026-07-05 13:20:10")
            self.assertEqual(sell["status"], "filled")
            self.assertEqual(sell["broker_seqno"], "SELL-S123")

    def test_sync_sell_fill_closes_multiple_open_buys_by_fifo(self):
        with tempfile.TemporaryDirectory() as tmp:
            backend = self._backend(tmp)
            for price in (100, 101):
                backend.record_trade({
                    "symbol": "2330",
                    "side": "BUY",
                    "price": price,
                    "shares": 1000,
                    "status": "filled",
                })
            backend.record_trade({
                "symbol": "2330",
                "side": "SELL",
                "price": 110,
                "shares": 1000,
                "status": "submitted",
                "brokerSeqno": "SELL-S123",
            })
            result = backend.sync_sinopac_order_fills([{
                "code": "2330",
                "action": "SELL",
                "price": 111.5,
                "shares": 1000,
                "dealAt": "2026-07-05 13:20:10",
                "brokerSeqno": "SELL-S123",
            }])
            self.assertEqual(result["updatedTrades"], 1)
            self.assertEqual(result["closedTrades"], 1)
            self.assertEqual(result["details"][0]["closedTradeIds"], [1])
            buys = [row for row in backend.list_trades(5) if row["side"] == "BUY"]
            first_buy = next(row for row in buys if row["id"] == 1)
            second_buy = next(row for row in buys if row["id"] == 2)
            self.assertEqual(first_buy["exit_at"], "2026-07-05 13:20:10")
            self.assertEqual(first_buy["pnl"], 11500.0)
            self.assertIsNone(second_buy["exit_at"])

    def test_reimporting_identical_sell_fill_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            backend = self._backend(tmp)
            for day in ("2026-07-01", "2026-07-02"):
                backend.record_trade({
                    "symbol": "2330",
                    "side": "BUY",
                    "price": 100,
                    "shares": 1000,
                    "buyDate": f"{day} 09:31:00",
                    "status": "filled",
                    "strategyHorizon": "short_trade",
                })
            backend.record_trade({
                "symbol": "2330",
                "side": "SELL",
                "price": 110,
                "shares": 1000,
                "status": "submitted",
                "brokerSeqno": "SELL-S123",
            })
            fill = {
                "code": "2330",
                "action": "SELL",
                "price": 111.5,
                "shares": 1000,
                "dealAt": "2026-07-05 13:20:10",
                "brokerSeqno": "SELL-S123",
                "raw": {"same": True},
            }
            first = backend.sync_sinopac_order_fills([fill])
            second = backend.sync_sinopac_order_fills([fill])
            self.assertEqual(first["closedTrades"], 1)
            self.assertEqual(second["imported"], 0)
            self.assertEqual(second["closedTrades"], 0)
            buys = [row for row in backend.list_trades(10) if row["side"] == "BUY"]
            self.assertEqual(sum(1 for row in buys if row["exit_at"]), 1)
            self.assertEqual(sum(1 for row in buys if not row["exit_at"]), 1)

    def test_sync_partial_sell_fill_splits_open_buy_for_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            backend = self._backend(tmp)
            backend.record_trade({
                "symbol": "2330",
                "side": "BUY",
                "price": 100,
                "shares": 1000,
                "buyDate": "2026-07-02 09:31:00",
                "status": "filled",
                "strategyHorizon": "mid_swing",
            })
            backend.record_trade({
                "symbol": "2330",
                "side": "SELL",
                "price": 110,
                "shares": 500,
                "status": "submitted",
                "brokerSeqno": "SELL-S123",
            })
            result = backend.sync_sinopac_order_fills([{
                "code": "2330",
                "action": "SELL",
                "price": 112,
                "shares": 500,
                "dealAt": "2026-07-05 13:20:10",
                "brokerSeqno": "SELL-S123",
            }])
            self.assertEqual(result["updatedTrades"], 1)
            self.assertEqual(result["closedTrades"], 1)
            self.assertEqual(result["splitTrades"], 1)
            rows = backend.list_trades(10)
            open_buy = next(row for row in rows if row["side"] == "BUY" and row["exit_at"] is None)
            closed_child = next(row for row in rows if row["side"] == "BUY" and row["parent_trade_id"] == 1)
            self.assertEqual(open_buy["shares"], 500)
            self.assertEqual(open_buy["status"], "filled")
            self.assertEqual(closed_child["shares"], 500)
            self.assertEqual(closed_child["exit_price"], 112.0)
            self.assertEqual(closed_child["pnl"], 6000.0)
            self.assertEqual(closed_child["status"], "closed")
            self.assertEqual(open_buy["strategy_horizon"], "mid_swing")
            self.assertEqual(closed_child["strategy_horizon"], "mid_swing")
            self.assertEqual(closed_child["strategy_horizon_locked_at"], open_buy["strategy_horizon_locked_at"])

    def test_fifo_lots_expose_uncovered_shares_without_guessing_date_or_horizon(self):
        with tempfile.TemporaryDirectory() as tmp:
            backend = self._backend(tmp)
            backend.record_trade({
                "symbol": "2330",
                "side": "BUY",
                "price": 100,
                "shares": 600,
                "buyDate": "2026-07-02 09:31:00",
                "status": "filled",
                "strategyHorizon": "short_trade",
            })
            result = backend.fifo_open_trade_lots("2330", 1000)
            self.assertEqual(result["coveredShares"], 600)
            self.assertEqual(result["unknownShares"], 400)
            self.assertFalse(result["fullyReconciled"])
            self.assertEqual(result["lots"][0]["buyDate"], "2026-07-02")
            self.assertEqual(result["lots"][0]["strategyHorizon"], "short_trade")

    def test_trade_duplicate_groups_preview_and_apply_exact_duplicates_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            backend = self._backend(tmp)
            for _ in range(3):
                backend.record_trade({
                    "symbol": "2330",
                    "side": "BUY",
                    "price": 100,
                    "shares": 1000,
                    "buyDate": "2026-07-02 09:31:00",
                    "status": "filled",
                    "brokerSeqno": "S123",
                })
            backend.record_trade({
                "symbol": "2330",
                "side": "BUY",
                "price": 101,
                "shares": 1000,
                "buyDate": "2026-07-02 09:31:00",
                "status": "filled",
                "brokerSeqno": "S123",
            })

            preview = backend.trade_duplicate_groups(apply=False)
            self.assertEqual(preview["groups"], 1)
            self.assertEqual(preview["duplicateRows"], 2)
            self.assertEqual(len(backend.list_trades(10)), 4)

            applied = backend.trade_duplicate_groups(apply=True)
            self.assertEqual(applied["deleted"], 2)
            rows = backend.list_trades(10)
            self.assertEqual(len(rows), 2)
            self.assertEqual(sorted(row["price"] for row in rows), [100.0, 101.0])


class SinopacFillNormalizationTests(unittest.TestCase):
    def test_normalizes_list_trades_status_deals_to_share_quantity(self):
        backend = SinoPacBackend()
        fill = backend.normalized_fill_from_trade_payload({
            "contract": {"code": "2330"},
            "order": {
                "action": "Buy",
                "order_lot": "Common",
                "id": "ID789",
                "seqno": "S123",
                "ordno": "O456",
            },
            "status": {
                "deals": [
                    {"price": 100.0, "quantity": 1, "ts": 1783051930.0},
                    {"price": 102.0, "quantity": 1, "ts": 1783051960.0},
                ],
            },
        })
        self.assertEqual(fill["code"], "2330")
        self.assertEqual(fill["action"], "BUY")
        self.assertEqual(fill["shares"], 2000)
        self.assertEqual(fill["price"], 101.0)
        self.assertEqual(fill["brokerSeqno"], "S123")
        self.assertRegex(fill["dealAt"], r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")

    def test_normalizes_order_deal_record_sdeal_to_share_quantity(self):
        backend = SinoPacBackend()
        fill = backend.normalized_fill_from_order_deal_record_payload({
            "OrderState": "SDEAL",
            "record": {
                "trade_id": "ID789",
                "seqno": "S123",
                "ordno": "O456",
                "broker_id": "BROKER",
                "account_id": "ACCOUNT",
                "action": "Buy",
                "code": "2330",
                "order_lot": "Common",
                "price": 101.5,
                "quantity": 2,
                "ts": 1783051930.0,
            },
        })
        self.assertEqual(fill["code"], "2330")
        self.assertEqual(fill["action"], "BUY")
        self.assertEqual(fill["price"], 101.5)
        self.assertEqual(fill["shares"], 2000)
        self.assertEqual(fill["brokerOrderId"], "ID789")
        self.assertEqual(fill["brokerSeqno"], "S123")
        self.assertEqual(fill["brokerOrdno"], "O456")
        self.assertEqual(fill["source"], "order_deal_records.SDEAL")
        self.assertNotIn("account_id", fill["raw"]["record"])
        self.assertNotIn("broker_id", fill["raw"]["record"])

    def test_order_deal_record_ignores_non_deal_order_state(self):
        backend = SinoPacBackend()
        fill = backend.normalized_fill_from_order_deal_record_payload({
            "OrderState": "SORDER",
            "record": {
                "operation": {"op_type": "New"},
                "order": {
                    "id": "ID789",
                    "seqno": "S123",
                    "ordno": "O456",
                    "action": "Buy",
                    "price": 101.5,
                    "quantity": 2,
                    "order_lot": "Common",
                },
                "contract": {"code": "2330"},
            },
        })
        self.assertIsNone(fill)

    def test_order_deal_record_accepts_shioaji_enum_string_state(self):
        backend = SinoPacBackend()
        fill = backend.normalized_fill_from_order_deal_record_payload({
            "OrderState": "OrderState.StockDeal",
            "record": {
                "trade_id": "ID789",
                "seqno": "S123",
                "ordno": "O456",
                "action": "Buy",
                "code": "2330",
                "order_lot": "Common",
                "price": 101.5,
                "quantity": 1,
                "ts": 1783051930.0,
            },
        })
        self.assertEqual(fill["code"], "2330")
        self.assertEqual(fill["shares"], 1000)

    def test_normalizes_order_deal_record_odd_lot_to_raw_share_quantity(self):
        backend = SinoPacBackend()
        fill = backend.normalized_fill_from_order_deal_record_payload({
            "OrderState": "SDEAL",
            "record": {
                "trade_id": "ID789",
                "seqno": "S123",
                "ordno": "O456",
                "action": "Sell",
                "code": "2330",
                "order_lot": "IntradayOdd",
                "price": 100.0,
                "quantity": 25,
                "ts": 1783051930.0,
            },
        })
        self.assertEqual(fill["action"], "SELL")
        self.assertEqual(fill["shares"], 25)


class SinopacOrderLocalTradeRecordTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server_module.StockHandler)
        cls.port = cls.httpd.server_address[1]
        import threading
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.thread.join(timeout=5)

    def _post_order(self, body):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=20)
        try:
            conn.request(
                "POST",
                "/api/sinopac/order/place",
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

    def test_buy_order_success_ignores_horizon_and_waits_for_fill_buy_date(self):
        order_result = {
            "ok": True,
            "simulation": False,
            "order": {
                "code": "2330",
                "action": "BUY",
                "price": 100.0,
                "shares": 1000,
            },
            "trade": {"status": {"status": "Submitted"}},
        }
        order_result["trade"]["order"] = {
            "id": "ID789",
            "seqno": "S123",
            "ordno": "O456",
        }
        with patch.object(server_module.sinopac_backend, "place_order", return_value=order_result), \
             patch.object(server_module.backend, "record_trade", return_value={"ok": True, "id": 77}) as record_trade:
            status, payload = self._post_order({
                "symbol": "2330",
                "action": "BUY",
                "priceType": "LMT",
                "price": 100,
                "quantity": 1,
                "strategyHorizon": "short_trade",
            })

        self.assertEqual(status, 200)
        self.assertTrue(payload["localTradeRecorded"])
        self.assertEqual(payload["localTradeId"], 77)
        args = record_trade.call_args.args[0]
        self.assertEqual(args["symbol"], "2330")
        self.assertEqual(args["side"], "BUY")
        self.assertEqual(args["price"], 100.0)
        self.assertEqual(args["shares"], 1000)
        self.assertEqual(args["status"], "submitted")
        self.assertEqual(args["brokerOrderId"], "ID789")
        self.assertEqual(args["brokerSeqno"], "S123")
        self.assertEqual(args["brokerOrdno"], "O456")
        self.assertEqual(args["strategyHorizon"], "unknown")
        self.assertEqual(args["strategyHorizonSource"], "sinopac_manual_order_unclassified")
        self.assertIn("不啟用時間出場", args["note"])
        self.assertNotIn("buyAt", args)

    def test_buy_order_without_strategy_horizon_is_accepted(self):
        order_result = {
            "ok": True,
            "simulation": False,
            "order": {"code": "2330", "action": "BUY", "price": 100.0, "shares": 1000},
            "trade": {"status": {"status": "Submitted"}},
        }
        with patch.object(server_module.sinopac_backend, "place_order", return_value=order_result) as place_order, \
             patch.object(server_module.backend, "record_trade", return_value={"ok": True, "id": 78}) as record_trade:
            status, payload = self._post_order({
                "symbol": "2330",
                "action": "BUY",
                "priceType": "LMT",
                "price": 100,
                "quantity": 1,
            })
        self.assertEqual(status, 200)
        self.assertTrue(payload["localTradeRecorded"])
        self.assertEqual(payload["localTradeStrategyHorizon"], "unknown")
        place_order.assert_called_once()
        args = record_trade.call_args.args[0]
        self.assertEqual(args["strategyHorizon"], "unknown")
        self.assertEqual(args["strategyHorizonSource"], "sinopac_manual_order_unclassified")

    def test_verified_radar_order_context_assigns_short_horizon(self):
        order_result = {
            "ok": True,
            "simulation": False,
            "order": {"code": "2330", "action": "BUY", "price": 100.0, "shares": 1000},
            "trade": {"status": {"status": "Submitted"}},
        }
        context_result = {
            "ok": True,
            "reason": "verified_current_radar_candidate",
            "symbol": "2330",
            "selectedScanDate": "2026-07-13",
        }
        with patch.object(server_module.sinopac_backend, "place_order", return_value=order_result), \
             patch.object(server_module.backend, "validate_radar_order_context", return_value=context_result) as validate, \
             patch.object(server_module.backend, "record_trade", return_value={"ok": True, "id": 79}) as record_trade:
            status, payload = self._post_order({
                "symbol": "2330",
                "action": "BUY",
                "priceType": "LMT",
                "price": 100,
                "quantity": 1,
                "orderContext": "monster_radar",
                "radarScanDate": "2026-07-13",
            })
        self.assertEqual(status, 200)
        validate.assert_called_once()
        args = record_trade.call_args.args[0]
        self.assertEqual(args["strategyHorizon"], "short_trade")
        self.assertEqual(
            args["strategyHorizonSource"],
            "verified_monster_radar_order_context",
        )
        self.assertEqual(payload["localTradeStrategyHorizon"], "short_trade")

    def test_rejected_radar_context_stays_unknown(self):
        order_result = {
            "ok": True,
            "simulation": False,
            "order": {"code": "2330", "action": "BUY", "price": 100.0, "shares": 1000},
            "trade": {"status": {"status": "Submitted"}},
        }
        with patch.object(server_module.sinopac_backend, "place_order", return_value=order_result), \
             patch.object(server_module.backend, "validate_radar_order_context", return_value={
                 "ok": False, "reason": "radar_scan_mismatch",
             }), \
             patch.object(server_module.backend, "record_trade", return_value={"ok": True, "id": 80}) as record_trade:
            status, payload = self._post_order({
                "symbol": "2330",
                "action": "BUY",
                "priceType": "LMT",
                "price": 100,
                "quantity": 1,
                "orderContext": "monster_radar",
                "radarScanDate": "2026-07-12",
            })
        self.assertEqual(status, 200)
        args = record_trade.call_args.args[0]
        self.assertEqual(args["strategyHorizon"], "unknown")
        self.assertEqual(args["strategyHorizonSource"], "monster_radar_order_context_rejected")
        self.assertEqual(payload["localTradeStrategyHorizon"], "unknown")

    def test_explicit_stock_savings_evidence_assigns_long_horizon(self):
        order_result = {
            "ok": True,
            "simulation": False,
            "order": {"code": "2330", "action": "BUY", "price": 100.0, "shares": 1000},
            "trade": {"status": {"status": "Submitted"}},
        }
        with patch.object(server_module.sinopac_backend, "place_order", return_value=order_result), \
             patch.object(server_module.backend, "record_trade", return_value={"ok": True, "id": 81}) as record_trade:
            status, payload = self._post_order({
                "symbol": "2330",
                "action": "BUY",
                "priceType": "LMT",
                "price": 100,
                "quantity": 1,
                "orderSource": "stock_savings",
            })
        self.assertEqual(status, 200)
        args = record_trade.call_args.args[0]
        self.assertEqual(args["strategyHorizon"], "long_trend")
        self.assertEqual(
            args["strategyHorizonSource"],
            "explicit_recurring_investment_evidence",
        )
        self.assertEqual(payload["localTradeStrategyHorizon"], "long_trend")

    def test_sell_order_success_records_local_sell_order_refs(self):
        order_result = {
            "ok": True,
            "simulation": False,
            "order": {
                "code": "2330",
                "action": "SELL",
                "price": 110.0,
                "shares": 1000,
            },
            "trade": {
                "order": {
                    "id": "SELL-ID789",
                    "seqno": "SELL-S123",
                    "ordno": "SELL-O456",
                },
                "status": {"status": "Submitted"},
            },
        }
        with patch.object(server_module.sinopac_backend, "place_order", return_value=order_result), \
             patch.object(server_module.backend, "record_trade", return_value={"ok": True, "id": 88}) as record_trade:
            status, payload = self._post_order({
                "symbol": "2330",
                "action": "SELL",
                "priceType": "LMT",
                "price": 110,
                "quantity": 1,
            })

        self.assertEqual(status, 200)
        self.assertTrue(payload["localTradeRecorded"])
        self.assertEqual(payload["localTradeId"], 88)
        args = record_trade.call_args.args[0]
        self.assertEqual(args["symbol"], "2330")
        self.assertEqual(args["side"], "SELL")
        self.assertEqual(args["price"], 110.0)
        self.assertEqual(args["shares"], 1000)
        self.assertEqual(args["status"], "submitted")
        self.assertNotIn("buyAt", args)
        self.assertEqual(args["brokerOrderId"], "SELL-ID789")
        self.assertEqual(args["brokerSeqno"], "SELL-S123")
        self.assertEqual(args["brokerOrdno"], "SELL-O456")

    def test_order_fill_sync_endpoint_imports_and_updates(self):
        order_fills = {
            "ok": True,
            "fills": [{
                "code": "2330",
                "action": "BUY",
                "price": 101.5,
                "shares": 1000,
                "dealAt": "2026-07-03 09:32:10",
                "brokerSeqno": "S123",
            }],
            "count": 1,
        }
        with patch.object(server_module.sinopac_backend, "order_fills", return_value=order_fills), \
             patch.object(server_module.backend, "sync_sinopac_order_fills", return_value={"ok": True, "imported": 1, "updatedTrades": 1, "unmatched": 0}) as sync:
            conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=20)
            try:
                conn.request(
                    "POST",
                    "/api/sinopac/order-fills/sync",
                    body="{}",
                    headers={
                        "Content-Type": "application/json",
                        "Origin": f"http://127.0.0.1:{self.port}",
                    },
                )
                response = conn.getresponse()
                payload = json.loads(response.read().decode("utf-8"))
            finally:
                conn.close()

        self.assertEqual(response.status, 200)
        self.assertEqual(payload["sync"]["updatedTrades"], 1)
        sync.assert_called_once_with(order_fills["fills"])

    def test_cross_origin_order_fills_get_is_rejected_before_fetch(self):
        with patch.object(server_module.sinopac_backend, "order_fills") as order_fills:
            conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=20)
            try:
                conn.request(
                    "GET",
                    "/api/sinopac/order-fills",
                    headers={"Origin": "http://evil.example.com"},
                )
                response = conn.getresponse()
                response.read()
            finally:
                conn.close()

        self.assertEqual(response.status, 403)
        order_fills.assert_not_called()


if __name__ == "__main__":
    unittest.main()
