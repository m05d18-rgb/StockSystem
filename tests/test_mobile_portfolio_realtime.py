import os
import sys
import unittest
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server


class MobilePortfolioRealtimeTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 7, 13, 10, 0, 0, tzinfo=server.TAIPEI_TZ)
        self.summary = {
            "updatedAt": "2026-07-13 09:55:00",
            "availableAfterSettlement": 50000,
        }
        self.holdings = {
            "2330": {
                "code": "2330", "shares": 1000, "quantity": 1,
                "price": 100, "currentPrice": 105, "pnl": 5000,
            },
            "2317": {
                "code": "2317", "shares": 1000, "quantity": 1,
                "price": 50, "currentPrice": 48, "pnl": -2000,
            },
        }

    def test_fresh_quote_batch_updates_prices_pnl_and_audit_metadata(self):
        quotes = {
            "2330": {
                "currentPrice": 110, "referencePrice": 108,
                "changeRate": 1.85, "snapshotAt": "2026-07-13 09:59:45",
                "receivedAt": "2026-07-13T09:59:46+08:00", "source": "Shioaji quote",
            },
            "2317": {
                "currentPrice": 45, "referencePrice": 46,
                "changeRate": -2.17, "snapshotAt": "2026-07-13 09:59:50",
                "receivedAt": "2026-07-13T09:59:51+08:00", "source": "Capital Strategy King COM",
                "fresh": True, "stale": False,
            },
        }

        result = server.merge_portfolio_quote_snapshot(
            self.summary, self.holdings, quotes,
            {"ok": True, "source": "Shioaji + Capital quote"},
            now=self.now,
        )

        self.assertTrue(result["complete"])
        self.assertEqual(result["fresh"], 2)
        self.assertEqual(result["holdings"]["2330"]["pnl"], 10000)
        self.assertEqual(result["holdings"]["2317"]["pnl"], -5000)
        self.assertEqual(result["summary"]["totalCost"], 150000)
        self.assertEqual(result["summary"]["currentValue"], 155000)
        self.assertEqual(result["summary"]["totalPnl"], 5000)
        self.assertTrue(result["summary"]["quoteFresh"])
        self.assertEqual(result["summary"]["quoteUpdatedAt"], "2026-07-13 10:00:00")
        self.assertEqual(result["holdings"]["2330"]["quoteAt"], "2026-07-13 09:59:45")
        self.assertEqual(result["holdings"]["2317"]["quoteSource"], "Capital Strategy King COM")
        self.assertEqual(result["summary"]["updatedAt"], "2026-07-13 09:55:00")

    def test_missing_quote_is_explicit_and_does_not_replace_last_price(self):
        result = server.merge_portfolio_quote_snapshot(
            self.summary,
            self.holdings,
            {"2330": {
                "currentPrice": 110,
                "snapshotAt": "2026-07-13 09:59:45",
                "source": "Shioaji quote",
            }},
            {"ok": True, "source": "Shioaji quote"},
            now=self.now,
        )

        self.assertFalse(result["complete"])
        self.assertEqual(result["missing"], ["2317"])
        self.assertFalse(result["summary"]["quoteFresh"])
        self.assertEqual(result["summary"]["quoteCoverage"]["fresh"], 1)
        self.assertEqual(result["holdings"]["2317"]["currentPrice"], 48)
        self.assertEqual(result["holdings"]["2317"]["pnl"], -2000)
        self.assertFalse(result["holdings"]["2317"]["quoteFresh"])
        self.assertEqual(result["holdings"]["2317"]["quoteFreshnessReason"], "missing_quote")


if __name__ == "__main__":
    unittest.main()
