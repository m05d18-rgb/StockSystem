"""
settlements_payload(未到期交割款查詢)的回歸測試。

對應 2026-07-03 事故：shioaji 1.3.3 的 api.list_settlements 在編譯層對
pydantic 2.12 拋 "dictionary update sequence element #0 has length 6"，
之前程式只呼叫這支，介面永遠顯示「無資料(永豐未回傳)」——實際上使用者
當天有 -78,410 元的 T+1 應付交割款。修法：改以實測正常的
api.settlements(account) 為主(回傳 SettlementV1(date/amount/T))，
list_settlements 降為備援。

全部用假 API 物件，不登入真實永豐。

執行方式：
  python -m unittest tests.test_settlements_payload -v
"""
import datetime
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sinopac_backend import SinoPacBackend


class _FakeSettlement:
    """模擬 shioaji SettlementV1(pydantic model)：有 model_dump()。"""

    def __init__(self, date, amount, t):
        self._data = {"date": date, "amount": amount, "T": t}

    def model_dump(self):
        return dict(self._data)


def _three_days():
    return [
        _FakeSettlement(datetime.date(2026, 7, 3), 0.0, 0),
        _FakeSettlement(datetime.date(2026, 7, 6), -78410.0, 1),
        _FakeSettlement(datetime.date(2026, 7, 7), 0.0, 2),
    ]


class _ApiSettlementsOk:
    """新路徑正常、舊路徑炸(=實機 shioaji 1.3.3 + pydantic 2.12 的真實行為)"""

    def settlements(self, account):
        return _three_days()

    def list_settlements(self, account):
        raise ValueError("dictionary update sequence element #0 has length 6; 2 is required")


class _ApiOnlyLegacyWorks:
    def settlements(self, account):
        raise AttributeError("settlements removed")

    def list_settlements(self, account):
        return _three_days()


class _ApiBothBroken:
    def settlements(self, account):
        raise RuntimeError("primary down")

    def list_settlements(self, account):
        raise RuntimeError("fallback down")


class SettlementsPayloadTests(unittest.TestCase):
    def setUp(self):
        self.backend = SinoPacBackend()

    def test_primary_settlements_api_used(self):
        payload = self.backend.settlements_payload(_ApiSettlementsOk(), "fake-account", {})
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["count"], 3)
        self.assertEqual(payload["total"], -78410.0, "T+0/1/2 三天加總=應付交割款")
        dates = [item["date"] for item in payload["items"]]
        self.assertIn("2026-07-06", dates)

    def test_falls_back_to_list_settlements(self):
        payload = self.backend.settlements_payload(_ApiOnlyLegacyWorks(), "fake-account", {})
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["total"], -78410.0)

    def test_both_broken_reports_error_with_both_messages(self):
        payload = self.backend.settlements_payload(_ApiBothBroken(), "fake-account", {})
        self.assertFalse(payload["ok"])
        self.assertIsNone(payload["total"])
        self.assertIn("settlements:", payload["rawError"])
        self.assertIn("list_settlements fallback:", payload["rawError"])

    def test_pending_total_excludes_today_and_past_settlements(self):
        # 帳戶總值 bug(2026-07-06):今天(T=0)/過去的交割款早上就交割完、已反映在
        # acc_balance,若再從可用餘額扣一次會重複計→帳戶總值少一整筆。pendingTotal 只算
        # 「未來(交割日>今天)還沒交割」的;total 仍保留三天全部供對照。
        today = datetime.date.today()
        past = today - datetime.timedelta(days=3)
        tomorrow = today + datetime.timedelta(days=1)

        class _Api:
            def settlements(self, account):
                return [
                    _FakeSettlement(past, -50000.0, 0),      # 過去:已交割
                    _FakeSettlement(today, -78410.0, 0),     # 今天:早上已交割完(反映在 acc_balance)
                    _FakeSettlement(tomorrow, -12345.0, 1),  # 未來:真正待交割
                ]

        payload = self.backend.settlements_payload(_Api(), "fake-account", {})
        self.assertTrue(payload["ok"])
        self.assertAlmostEqual(payload["total"], -50000.0 - 78410.0 - 12345.0)  # total=三天全部
        self.assertAlmostEqual(payload["pendingTotal"], -12345.0)  # 只有未來那筆才是未交割待扣

    def test_zero_settlements_is_valid_zero_not_missing(self):
        class _ApiEmpty:
            def settlements(self, account):
                return []

        payload = self.backend.settlements_payload(_ApiEmpty(), "fake-account", {})
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["total"], 0.0, "沒有未到期交割款=0元，不是「無資料」")


if __name__ == "__main__":
    unittest.main()
