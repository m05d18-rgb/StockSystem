"""
sinopac_backend.py 零股庫存單位混淆的回歸測試，對應這次修的 bug：

list_positions() 預設 unit=Unit.Common(整張口徑)，未滿一張的零股部位在
這個口徑下可能完全不會出現在回傳列表裡；position_to_holding() 原本無論
查詢用哪個 unit，一律把 quantity 當「張」乘以 1000 換算股數，這跟已經
修過的 on_tick() 零股單位混淆是同一類問題(shioaji 的 volume/quantity
語意會隨 intraday_odd/unit 切換，不能無腦套用同一個換算)。

修法：position_to_holding() 新增 unit_is_share 參數，Unit.Share 口徑
查回來的 quantity 直接當股數用，不再乘以 1000；holdings_direct() 額外
補查一次 Unit.Share，把「整張口徑完全沒看到」的零股專屬部位併入結果。

執行方式：
  python -m unittest tests.test_sinopac_holdings_unit -v
"""
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sinopac_backend import sinopac_backend


def _fake_position(code, quantity, price=100.0):
    return {"code": code, "quantity": quantity, "price": price, "pnl": 0.0, "direction": "Buy"}


class PositionToHoldingUnitTests(unittest.TestCase):
    def test_common_unit_multiplies_quantity_by_1000_for_shares(self):
        holding = sinopac_backend.position_to_holding(_fake_position("2330", 5))
        self.assertEqual(holding["shares"], 5000)
        self.assertEqual(holding["quantity"], 5)
        self.assertEqual(holding["quantitySource"], "shioaji_lot_quantity")

    def test_position_details_convert_lot_quantity_to_exact_shares(self):
        position = {"id": 7, "code": "2330", "quantity": 3, "price": 100, "direction": "Buy"}
        details = [
            {"date": "2026-06-01", "code": "2330", "quantity": 1, "price": 95135, "dseq": "A"},
            {"date": "2026-06-20", "code": "2330", "quantity": 2, "price": 204291, "dseq": "B"},
        ]
        result = sinopac_backend.normalized_position_detail_lots(position, details)
        self.assertTrue(result["reconciled"])
        self.assertEqual(result["expectedShares"], 3000)
        self.assertEqual([row["shares"] for row in result["lots"]], [1000, 2000])
        self.assertEqual([row["price"] for row in result["lots"]], [95.135, 102.1455])
        self.assertEqual([row["brokerCostPrice"] for row in result["lots"]], [95.135, 102.1455])
        self.assertEqual(
            [row["estimatedStandardFeeFillPrice"] for row in result["lots"]],
            [95.0, 102.0],
        )
        self.assertEqual([row["date"] for row in result["lots"]], ["2026-06-01", "2026-06-20"])

    def test_position_details_accept_share_quantity_for_odd_lot(self):
        position = {"id": 8, "code": "2330", "quantity": 300, "price": 100, "direction": "Buy"}
        details = [{"date": "2026-06-01", "code": "2330", "quantity": 300, "price": 29742, "dseq": "C"}]
        result = sinopac_backend.normalized_position_detail_lots(position, details, unit_is_share=True)
        self.assertTrue(result["reconciled"])
        self.assertEqual(result["lots"][0]["shares"], 300)
        self.assertEqual(result["lots"][0]["price"], 99.14)
        self.assertEqual(result["lots"][0]["estimatedStandardFeeFillPrice"], 99.0)

    def test_position_cost_reverses_broker_fee_to_legal_tick(self):
        self.assertEqual(
            sinopac_backend.estimated_fill_price_from_position_cost(543773, 1000),
            543.0,
        )
        self.assertEqual(
            sinopac_backend.estimated_fill_price_from_position_cost(33297, 1000),
            33.25,
        )

    def test_position_details_reject_unreconciled_quantity(self):
        position = {"id": 9, "code": "2330", "quantity": 3, "price": 100, "direction": "Buy"}
        details = [{"date": "2026-06-01", "code": "2330", "quantity": 2, "price": 99, "dseq": "D"}]
        result = sinopac_backend.normalized_position_detail_lots(position, details)
        self.assertFalse(result["reconciled"])
        self.assertEqual(result["lots"], [])


class HoldingsMarketDataFallbackTests(unittest.TestCase):
    def test_capital_only_fills_missing_market_fields_without_replacing_position(self):
        payload = {
            "ok": True,
            "accountMasked": "永豐帳戶",
            "holdings": [
                {"code": "2330", "shares": 1000, "quantity": 1, "price": 2000, "pnl": 10, "currentPrice": None},
                {"code": "2303", "shares": 2000, "quantity": 2, "price": 45, "pnl": 20, "currentPrice": 50},
            ],
        }
        capital = {
            "ok": True,
            "quotes": {
                "2330": {
                    "currentPrice": 2415, "referencePrice": 2465, "changeRate": -2.03,
                    "open": 2450, "high": 2460, "low": 2415, "totalVolume": 27057,
                    "quoteTimestamp": "2026-07-09 14:30:00",
                },
            },
        }
        with patch("sinopac_backend.capital_backend.live_quotes", return_value=capital) as live_quotes:
            result = sinopac_backend.enrich_holdings_market_data(payload)
        live_quotes.assert_called_once_with(["2330"])
        by_code = {item["code"]: item for item in result["holdings"]}
        self.assertEqual(by_code["2330"]["currentPrice"], 2415)
        self.assertEqual(by_code["2330"]["price"], 2000)
        self.assertEqual(by_code["2330"]["shares"], 1000)
        self.assertEqual(by_code["2330"]["marketDataSource"], "Capital Strategy King COM")
        self.assertEqual(by_code["2303"]["currentPrice"], 50)
        self.assertTrue(result["marketDataFallbackUsed"])
        self.assertEqual(result["marketDataFallbackCodes"], ["2330"])

    def test_no_capital_quote_keeps_missing_price_explicit(self):
        payload = {"ok": True, "holdings": [{"code": "2330", "shares": 1000, "price": 2000}]}
        with patch("sinopac_backend.capital_backend.live_quotes", return_value={
            "ok": False, "quotes": {}, "error": "stale quote",
        }):
            result = sinopac_backend.enrich_holdings_market_data(payload)
        self.assertNotIn("currentPrice", result["holdings"][0])
        self.assertFalse(result["marketDataFallbackUsed"])
        self.assertEqual(result["marketDataMissingCodes"], ["2330"])
        self.assertEqual(result["marketDataFallbackError"], "stale quote")

    def test_share_unit_does_not_multiply_quantity(self):
        # 零股部位：quantity=300 代表 300 股，不是 300 張(300,000股)。
        holding = sinopac_backend.position_to_holding(_fake_position("2330", 300), unit_is_share=True)
        self.assertEqual(holding["shares"], 300)
        self.assertEqual(holding["quantity"], 0.3)
        self.assertEqual(holding["quantitySource"], "shioaji_share_unit_quantity")

    def test_share_unit_whole_lot_quantity_still_correct(self):
        # Share 口徑下若剛好整張(quantity=5000)，換算股數應該還是 5000，
        # 不能因為切換了 unit 就多乘一次 1000。
        holding = sinopac_backend.position_to_holding(_fake_position("2330", 5000), unit_is_share=True)
        self.assertEqual(holding["shares"], 5000)
        self.assertEqual(holding["quantity"], 5)


if __name__ == "__main__":
    unittest.main()
