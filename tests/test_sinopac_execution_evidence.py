import os
import sys
import tempfile
import unittest
from pathlib import Path


sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ml_backend import StockMLBackend
from portfolio_exit import _position_economics


class SinoPacExecutionEvidenceTests(unittest.TestCase):
    def _backend(self, directory):
        backend = StockMLBackend()
        backend.db_path = Path(directory) / "stock_system.sqlite3"
        backend.init_db()
        return backend

    @staticmethod
    def _payload(records):
        return {
            "batchId": "unit-test-evidence",
            "sources": [{"filename": "proof.png", "sha256": "a" * 64}],
            "records": [dict(record, sourceFile="proof.png") for record in records],
        }

    def test_open_position_evidence_keeps_execution_and_broker_cost_separate(self):
        with tempfile.TemporaryDirectory() as directory:
            backend = self._backend(directory)
            backend.record_trade({
                "symbol": "2330",
                "side": "BUY",
                "price": 99.2,
                "shares": 1000,
                "buyDate": "2026-07-01",
                "status": "filled",
                "brokerSeqno": "X0001",
                "entryCostIncludesBuyFee": True,
                "brokerCostAmount": 99200,
            })
            payload = self._payload([{
                "symbol": "2330",
                "side": "BUY",
                "dealAt": "2026-07-01 09:01:02.003",
                "price": 100,
                "shares": 1000,
                "dseq": "X0001",
                "condition": "Cash",
                "openPosition": True,
            }])

            preview = backend.import_sinopac_execution_evidence(payload, apply=False)
            before = backend.list_trades(1)[0]
            applied = backend.import_sinopac_execution_evidence(payload, apply=True)
            trade = backend.list_trades(1)[0]
            lot = backend.fifo_open_trade_lots("2330", 1000)["lots"][0]
            repeated = backend.import_sinopac_execution_evidence(payload, apply=True)

            self.assertEqual(preview["openPositionGroupCount"], 1)
            self.assertEqual(before["price"], 99.2)
            self.assertEqual(before["filled_at"], None)
            self.assertEqual(applied["evidenceInserted"], 1)
            self.assertEqual(applied["openPositionLotsEnriched"], 1)
            self.assertEqual(trade["price"], 99.2)
            self.assertEqual(trade["executionPrice"], 100)
            self.assertEqual(trade["costBasisPrice"], 99.2)
            self.assertEqual(trade["filled_at"], "2026-07-01 09:01:02.003")
            self.assertEqual(trade["broker_dseq"], "X0001")
            self.assertEqual(trade["executionEvidenceCount"], 1)
            self.assertEqual(lot["price"], 100)
            self.assertEqual(lot["entryCostAmount"], 99200)
            self.assertEqual(repeated["evidenceInserted"], 0)
            self.assertEqual(repeated["evidenceDuplicates"], 1)

            economics = _position_economics(
                100, 110, 1000,
                entry_cost_includes_buy_fee=True,
                entry_cost_amount=99200,
            )
            self.assertEqual(economics["grossPnl"], 10000)
            self.assertEqual(economics["entryCostAmount"], 99200)
            self.assertEqual(economics["entryCostAdjustment"], -800)
            self.assertEqual(economics["entryCostSource"], "broker_reported")

    def test_history_evidence_builds_fifo_round_trip_with_broker_realized_pnl(self):
        with tempfile.TemporaryDirectory() as directory:
            backend = self._backend(directory)
            backend.save_realized_pnl([{
                "code": "2913",
                "quantity": 1,
                "price": 12.2,
                "pnl": 731,
                "pr_ratio": 0.064,
                "date": "2026-07-09",
                "cond": "Cash",
                "seqno": "11679",
            }])
            payload = self._payload([
                {
                    "symbol": "2913", "side": "BUY",
                    "dealAt": "2026-06-10 12:55:14.466", "price": 11.4,
                    "shares": 1000, "dseq": "X0Z3Z", "condition": "Cash",
                },
                {
                    "symbol": "2913", "side": "SELL",
                    "dealAt": "2026-07-09 09:14:59.110", "price": 12.2,
                    "shares": 1000, "dseq": "X0631", "condition": "Cash",
                },
            ])

            result = backend.import_sinopac_execution_evidence(payload, apply=True)
            trades = backend.list_trades(10)
            buy = next(row for row in trades if row["side"] == "BUY")
            sell = next(row for row in trades if row["side"] == "SELL")

            self.assertEqual(result["historyTradesCreated"], 2)
            self.assertEqual(result["closedRoundTrips"], 1)
            self.assertEqual(result["realizedPnlMatchCount"], 1)
            self.assertEqual(result["evidenceInserted"], 2)
            self.assertEqual(buy["status"], "closed")
            self.assertEqual(buy["exit_price"], 12.2)
            self.assertEqual(buy["pnl"], 731)
            self.assertEqual(buy["pnlPct"], 6.4)
            self.assertEqual(buy["pnl_basis"], "sinopac_realized")
            self.assertEqual(buy["realized_pnl_key"], "11679")
            self.assertEqual(sell["status"], "filled")

    def test_history_evidence_rejects_incomplete_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            backend = self._backend(directory)
            payload = self._payload([{
                "symbol": "2330", "side": "BUY",
                "dealAt": "2026-07-01 09:01:02.003", "price": 100,
                "shares": 1000, "dseq": "B1", "condition": "Cash",
            }])
            with self.assertRaisesRegex(ValueError, "不是完整 round-trip"):
                backend.import_sinopac_execution_evidence(payload, apply=False)
            self.assertEqual(backend.list_trades(10), [])


if __name__ == "__main__":
    unittest.main()
