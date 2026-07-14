import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ml_backend import StockMLBackend
from portfolio_exit import evaluate_exit_lot


def detail_payload():
    return {
        "ok": True,
        "simulation": False,
        "positionCount": 2,
        "reconciledPositionCount": 2,
        "lotCount": 3,
        "errors": [],
        "positions": [
            {
                "code": "6727",
                "expectedShares": 1000,
                "reconciled": True,
                "lots": [
                    {
                        "code": "6727",
                        "date": "2026-07-09",
                        "shares": 1000,
                        "costAmount": 543773,
                        "dseq": "A001",
                    }
                ],
            },
            {
                "code": "4114",
                "expectedShares": 2000,
                "reconciled": True,
                "lots": [
                    {
                        "code": "4114",
                        "date": "2026-07-08",
                        "shares": 1000,
                        "costAmount": 33798,
                        "dseq": "B001",
                    },
                    {
                        "code": "4114",
                        "date": "2026-07-08",
                        "shares": 1000,
                        "costAmount": 33147,
                        "dseq": "B002",
                    },
                ],
            },
        ],
    }


def holdings(shares_4114=2000):
    return {
        "ok": True,
        "holdings": [
            {"code": "6727", "shares": 1000, "price": 543.773},
            {"code": "4114", "shares": shares_4114, "price": 33.4725},
        ],
    }


class SinopacPositionDetailImportTests(unittest.TestCase):
    def backend(self, tmp):
        backend = StockMLBackend()
        backend.db_path = Path(tmp) / "stock_system.sqlite3"
        backend.init_db()
        return backend

    def test_preview_apply_and_rerun_are_atomic_and_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            backend = self.backend(tmp)
            existing = backend.record_trade({
                "symbol": "6727",
                "side": "BUY",
                "price": 543,
                "shares": 1000,
                "buyDate": "2026-07-09",
                "status": "filled",
            })

            preview = backend.import_sinopac_position_details(
                detail_payload(), holdings(), apply=False
            )
            self.assertFalse(preview["applied"])
            self.assertEqual(preview["positionCount"], 2)
            self.assertEqual(preview["lotCount"], 3)
            self.assertEqual(preview["matchedExistingLotCount"], 1)
            self.assertEqual(preview["newLotCount"], 2)
            self.assertEqual(len(backend.list_trades()), 1)

            applied = backend.import_sinopac_position_details(
                detail_payload(), holdings(), apply=True
            )
            self.assertTrue(applied["applied"])
            self.assertEqual(applied["newLotCount"], 2)
            self.assertEqual(len(applied["insertedTradeIds"]), 2)
            self.assertEqual(applied["auditPositionCount"], 1)

            with backend.connect() as conn:
                conn.row_factory = __import__("sqlite3").Row
                rows = conn.execute("SELECT * FROM trades ORDER BY id").fetchall()
                audits = conn.execute("SELECT * FROM legacy_lot_imports").fetchall()
            self.assertEqual(len(rows), 3)
            self.assertEqual(len(audits), 1)
            self.assertEqual(rows[0]["id"], existing["id"])
            self.assertEqual(rows[0]["entry_cost_includes_buy_fee"], 0)
            self.assertEqual(rows[0]["broker_cost_amount"], 543773)
            self.assertTrue(rows[0]["source_lot_key"].startswith("sinopac-position-detail:"))
            imported = [row for row in rows if row["symbol"] == "4114"]
            self.assertEqual([row["price"] for row in imported], [33.798, 33.147])
            self.assertTrue(all(row["entry_cost_includes_buy_fee"] == 1 for row in imported))
            self.assertTrue(all(row["strategy_horizon"] == "unknown" for row in rows))

            rerun = backend.import_sinopac_position_details(
                detail_payload(), holdings(), apply=True
            )
            self.assertEqual(rerun["newLotCount"], 0)
            self.assertEqual(rerun["insertedTradeIds"], [])
            self.assertEqual(rerun["auditPositionCount"], 0)
            self.assertEqual(len(backend.list_trades()), 3)

    def test_holding_quantity_mismatch_rejects_every_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            backend = self.backend(tmp)
            with self.assertRaisesRegex(ValueError, "4114.*不一致"):
                backend.import_sinopac_position_details(
                    detail_payload(), holdings(shares_4114=1000), apply=True
                )
            self.assertEqual(backend.list_trades(), [])

    def test_unmatched_local_open_buy_rejects_duplicate_import(self):
        with tempfile.TemporaryDirectory() as tmp:
            backend = self.backend(tmp)
            backend.record_trade({
                "symbol": "4114",
                "side": "BUY",
                "price": 99,
                "shares": 1000,
                "buyDate": "2026-07-01",
                "status": "filled",
            })
            with self.assertRaisesRegex(ValueError, "未對應的開放 BUY lot"):
                backend.import_sinopac_position_details(
                    detail_payload(), holdings(), apply=True
                )
            self.assertEqual(len(backend.list_trades()), 1)

    def test_actual_broker_cost_does_not_add_buy_fee_twice(self):
        result = evaluate_exit_lot(
            {
                "price": 33.798,
                "shares": 1000,
                "buyDate": "2026-07-08",
                "strategyHorizon": "short_trade",
                "entryCostIncludesBuyFee": True,
            },
            [{"date": "2026-07-08", "close": 33.75}],
            {
                "currentPrice": 35,
                "quoteDate": "2026-07-09",
                "snapshotAt": "2026-07-09 13:30:00",
                "quoteFresh": True,
            },
            evaluation_date="2026-07-09",
        )
        self.assertTrue(result["entryCostIncludesBuyFee"])
        self.assertEqual(result["buyAmount"], 33798)
        self.assertEqual(result["buyCommission"], 0)


if __name__ == "__main__":
    unittest.main()
