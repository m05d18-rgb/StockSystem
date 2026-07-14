"""
account_balance_payload(永豐帳戶餘額查詢)的回歸測試。

對應 2026-07-04 稽核發現：跟 2026-07-03 修過的 settlements_payload 同一類 bug——
Shioaji 的 AccountBalance(pydantic model)model_dump() 常見欄位(date/update_time)
是 datetime.date/datetime.datetime，原本 `"raw": data` 直接把整包 model_dump()
結果塞進要 json.dumps() 的回應 dict，遇到日期型別欄位會讓 /api/sinopac/holdings
的序列化整個炸掉。修法：比照 settlements_payload 已經在用的 `json_safe()`，改成
`"raw": self.json_safe(data)`。

全部用假 API 物件，不登入真實永豐。

執行方式：
  python -m unittest tests.test_account_balance_payload -v
"""
import datetime
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sinopac_backend import SinoPacBackend


class _FakeAccountBalance:
    """模擬 shioaji AccountBalance(pydantic model)：有 model_dump()，
    date/update_time 是 datetime 物件，不是 json 可序列化的基本型別。"""

    def model_dump(self):
        return {
            "acc_balance": 87513.0,
            "date": datetime.date(2026, 7, 3),
            "update_time": datetime.datetime(2026, 7, 3, 13, 30, 0),
            "currency": "TWD",
        }


class _ApiAccountBalanceOk:
    def account_balance(self, account):
        return _FakeAccountBalance()


class _ApiAccountBalanceBroken:
    def account_balance(self, account):
        raise RuntimeError("connection reset")


class AccountBalancePayloadTests(unittest.TestCase):
    def setUp(self):
        self.backend = SinoPacBackend()

    def test_raw_field_is_actually_json_serializable(self):
        payload = self.backend.account_balance_payload(_ApiAccountBalanceOk(), "fake-account", {})
        self.assertTrue(payload["ok"])
        # 這是關鍵斷言：不是檢查型別，是真的跑一次 json.dumps()，
        # 跟正式 server.py 序列化回應給瀏覽器的路徑一樣。
        json.dumps(payload)

    def test_datetime_fields_converted_to_iso_strings(self):
        payload = self.backend.account_balance_payload(_ApiAccountBalanceOk(), "fake-account", {})
        self.assertEqual(payload["raw"]["date"], "2026-07-03")
        self.assertEqual(payload["raw"]["update_time"], "2026-07-03T13:30:00")

    def test_available_cash_still_extracted_correctly(self):
        payload = self.backend.account_balance_payload(_ApiAccountBalanceOk(), "fake-account", {})
        self.assertEqual(payload["availableCash"], 87513.0)
        self.assertEqual(payload["availableCashSource"], "acc_balance")

    def test_api_failure_returns_error_not_exception(self):
        payload = self.backend.account_balance_payload(_ApiAccountBalanceBroken(), "fake-account", {})
        self.assertFalse(payload["ok"])
        self.assertIn("error", payload)


if __name__ == "__main__":
    unittest.main()
