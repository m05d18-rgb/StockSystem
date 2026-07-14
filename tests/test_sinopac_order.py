"""
永豐手動下單 place_order() 的伺服器端庫存核對測試。

這是會送出真實委託單的功能，這裡完全用 mock 隔絕 load_config/holdings/
run_shioaji_child，全程不觸碰真實設定檔、不呼叫 Shioaji、不連線任何網路，
只測試「賣出數量超過永豐真實庫存時，place_order 是否正確擋下」這個
新加的安全檢查。

執行方式：
  python -m unittest tests.test_sinopac_order -v
"""
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sinopac_backend import sinopac_backend, ORDER_CONFIRM_TEXT


BASE_CONFIG = {
    "apiKey": "test-api-key-1234",
    "secretKey": "test-secret-key-1234",
    "simulation": True,
    "caPath": "",
    "caPassword": "",
    "personId": "",
}


def make_payload(**overrides):
    base = {
        "symbol": "2330",
        "action": "SELL",
        "priceType": "LMT",
        "price": 100.0,
        "orderLot": "COMMON",
        "quantity": 1,
        "orderType": "ROD",
        "manualConfirm": True,
        "confirmText": ORDER_CONFIRM_TEXT,
        "allowLiveOrder": True,
    }
    base.update(overrides)
    return base


def make_holdings_payload(code, shares):
    return {
        "ok": True,
        "holdings": [{"code": code, "quantity": shares // 1000, "shares": shares}],
    }


class PlaceOrderSellHoldingsCheckTests(unittest.TestCase):
    """回歸測試：place_order 對 SELL 方向要在真的送出前核對永豐真實庫存，
    不能只信任前端傳來的資料。"""

    def test_sell_exceeding_real_holdings_is_blocked(self):
        with patch.object(sinopac_backend, "load_config", return_value=dict(BASE_CONFIG)), \
             patch.object(sinopac_backend, "holdings", return_value=make_holdings_payload("2330", 1000)), \
             patch.object(sinopac_backend, "run_shioaji_child") as mock_child:
            payload = make_payload(action="SELL", orderLot="COMMON", quantity=2)  # 要賣2張=2000股，只有1000股
            with self.assertRaises(ValueError) as ctx:
                sinopac_backend.place_order(payload)
            self.assertIn("超過永豐目前實際庫存", str(ctx.exception))
            mock_child.assert_not_called()  # 根本不該送到下單子行程

    def test_sell_within_real_holdings_passes_the_check(self):
        with patch.object(sinopac_backend, "load_config", return_value=dict(BASE_CONFIG)), \
             patch.object(sinopac_backend, "holdings", return_value=make_holdings_payload("2330", 2000)), \
             patch.object(sinopac_backend, "run_shioaji_child") as mock_child:
            mock_child.return_value = type("Completed", (), {"stdout": '{"ok": true, "trade": {}}', "stderr": ""})()
            payload = make_payload(action="SELL", orderLot="COMMON", quantity=1)  # 賣1張=1000股，庫存有2000股
            sinopac_backend.place_order(payload)
            mock_child.assert_called_once()  # 通過庫存檢查，才會真的呼叫下單子行程

    def test_sell_odd_lot_is_blocked_before_holdings_or_order_submission(self):
        with patch.object(sinopac_backend, "load_config", return_value=dict(BASE_CONFIG)), \
             patch.object(sinopac_backend, "holdings") as mock_holdings, \
             patch.object(sinopac_backend, "run_shioaji_child") as mock_child:
            payload = make_payload(action="SELL", orderLot="INTRADAY_ODD", quantity=500)
            with self.assertRaises(ValueError) as ctx:
                sinopac_backend.place_order(payload)
            self.assertIn("只允許整張", str(ctx.exception))
            mock_holdings.assert_not_called()
            mock_child.assert_not_called()

    def test_sell_stock_not_held_at_all_is_blocked(self):
        with patch.object(sinopac_backend, "load_config", return_value=dict(BASE_CONFIG)), \
             patch.object(sinopac_backend, "holdings", return_value=make_holdings_payload("2317", 5000)), \
             patch.object(sinopac_backend, "run_shioaji_child") as mock_child:
            payload = make_payload(symbol="2330", action="SELL", orderLot="COMMON", quantity=1)  # 持股清單裡沒有2330
            with self.assertRaises(ValueError) as ctx:
                sinopac_backend.place_order(payload)
            self.assertIn("超過永豐目前實際庫存", str(ctx.exception))
            mock_child.assert_not_called()

    def test_buy_orders_are_not_subject_to_holdings_check(self):
        with patch.object(sinopac_backend, "load_config", return_value=dict(BASE_CONFIG)), \
             patch.object(sinopac_backend, "holdings") as mock_holdings, \
             patch.object(sinopac_backend, "run_shioaji_child") as mock_child:
            mock_child.return_value = type("Completed", (), {"stdout": '{"ok": true, "trade": {}}', "stderr": ""})()
            payload = make_payload(action="BUY", orderLot="COMMON", quantity=1)
            sinopac_backend.place_order(payload)
            mock_holdings.assert_not_called()  # 買進不需要核對庫存
            mock_child.assert_called_once()

    def test_holdings_api_failure_blocks_sell_instead_of_silently_allowing(self):
        with patch.object(sinopac_backend, "load_config", return_value=dict(BASE_CONFIG)), \
             patch.object(sinopac_backend, "holdings", side_effect=RuntimeError("永豐連線逾時")), \
             patch.object(sinopac_backend, "run_shioaji_child") as mock_child:
            payload = make_payload(action="SELL", orderLot="COMMON", quantity=1)
            with self.assertRaises(ValueError) as ctx:
                sinopac_backend.place_order(payload)
            self.assertIn("無法核對永豐真實庫存", str(ctx.exception))
            mock_child.assert_not_called()  # 核對不了庫存，安全起見不能送出


if __name__ == "__main__":
    unittest.main()
