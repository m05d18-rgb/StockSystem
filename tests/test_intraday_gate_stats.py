"""
② 盤中閘門量測(record_intraday_gate_stats)的回歸測試。

功能:每日累積「多少檔翻可買(當日各輪聯集)、狀態/型態分布、跨日壓進 history」,
用來誠實回答「能買的是不是太少/門檻太嚴」——純記錄,不影響任何 canBuy/門檻判斷。

隔離鐵律:狀態存在正式 model_meta 表,測試 patch INTRADAY_GATE_STATS_KEY 成
__test_*__ 假 key 並在 setUp/tearDown 清理,絕不觸碰正式量測資料。

執行方式:
  python -m unittest tests.test_intraday_gate_stats -v
"""
import json
import os
import sys
import unittest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server as server_module

TEST_KEY = "__test_intraday_gate_stats__"


def _bycode(canbuy_codes):
    """組一個 by_code:canbuy_codes 內的檔 canBuy=True(回測低接可觀察/pullback),
    其餘 canBuy=False(尚未通過開盤確認/無型態)。"""
    universe = ["1111", "2222", "3333"]
    out = {}
    for code in universe:
        hit = code in canbuy_codes
        out[code] = {
            "canBuy": hit,
            "shadowCanBuy": hit,
            "status": "回測低接可觀察" if hit else "尚未通過開盤確認",
            "setupType": "pullback" if hit else "",
        }
    return out


class IntradayGateStatsTests(unittest.TestCase):
    def setUp(self):
        self._today = MagicMock(return_value="2026-07-07")
        self._patchers = [
            patch.object(server_module, "INTRADAY_GATE_STATS_KEY", TEST_KEY),
            patch.object(server_module, "scheduler_today", self._today),
        ]
        for p in self._patchers:
            p.start()
        self._delete_test_key()

    def tearDown(self):
        self._delete_test_key()
        for p in self._patchers:
            p.stop()

    def _delete_test_key(self):
        with server_module.backend.connect() as conn:
            conn.execute("DELETE FROM model_meta WHERE key = ?", (TEST_KEY,))
            conn.commit()

    def _read(self):
        with server_module.backend.connect() as conn:
            row = conn.execute("SELECT value FROM model_meta WHERE key = ?", (TEST_KEY,)).fetchone()
        return json.loads(row[0]) if row and row[0] else None

    def test_buyable_union_accumulates_across_polls(self):
        # 型態一閃即逝:同一天不同輪各出現不同可買檔,當日聯集要累加而非覆蓋。
        server_module.record_intraday_gate_stats(_bycode(["1111"]), 3)
        data = server_module.record_intraday_gate_stats(_bycode(["2222"]), 3)
        self.assertEqual(data["buyableUnion"], 2)
        self.assertEqual(set(data["buyableCodes"]), {"1111", "2222"})
        self.assertEqual(data["polls"], 2)
        self.assertEqual(data["maxConcurrent"], 1)   # 每輪各 1 檔
        self.assertEqual(data["lastConcurrent"], 1)
        self.assertEqual(data["candidateCount"], 3)

    def test_status_and_setup_distribution_from_last_poll(self):
        data = server_module.record_intraday_gate_stats(_bycode(["1111"]), 3)
        self.assertEqual(data["statusDist"].get("尚未通過開盤確認"), 2)
        self.assertEqual(data["statusDist"].get("回測低接可觀察"), 1)
        self.assertEqual(data["setupDist"].get("pullback"), 1)
        self.assertEqual(data["setupDist"].get("無"), 2)

    def test_zero_buyable_records_cleanly(self):
        data = server_module.record_intraday_gate_stats(_bycode([]), 3)
        self.assertEqual(data["buyableUnion"], 0)
        self.assertEqual(data["maxConcurrent"], 0)
        self.assertEqual(data["statusDist"].get("尚未通過開盤確認"), 3)

    def test_date_rollover_archives_yesterday_to_history(self):
        self._today.return_value = "2026-07-06"
        server_module.record_intraday_gate_stats(_bycode(["1111", "2222"]), 5)
        self._today.return_value = "2026-07-07"
        data = server_module.record_intraday_gate_stats(_bycode(["3333"]), 4)
        # 今天重置:只算今天這輪的 3333
        self.assertEqual(data["date"], "2026-07-07")
        self.assertEqual(data["buyableUnion"], 1)
        self.assertEqual(set(data["buyableCodes"]), {"3333"})
        # 昨天(07-06)壓進 history,聯集 2 檔
        self.assertTrue(any(h.get("date") == "2026-07-06" and h.get("buyableUnion") == 2 for h in data["history"]))

    def test_history_capped(self):
        for i in range(server_module.INTRADAY_GATE_STATS_HISTORY_MAX + 5):
            self._today.return_value = f"2026-06-{(i % 28) + 1:02d}_{i}"
            server_module.record_intraday_gate_stats(_bycode(["1111"]), 3)
        data = self._read()
        self.assertLessEqual(len(data["history"]), server_module.INTRADAY_GATE_STATS_HISTORY_MAX)

    def test_entry_guardrail_veto_is_counted_and_any_release_is_a_risk_leak(self):
        states = {
            "9999": {
                "canBuy": False,
                "formalWatchAllowed": False,
                "scheduleAllowed": False,
                "candidateEntryGuardrailVetoed": True,
                "status": "不追價進場防線否決，只觀察",
                "setupType": "breakout",
            }
        }
        data = server_module.record_intraday_gate_stats(states, 1)
        self.assertEqual(data["entryGuardrailVetoCount"], 1)
        self.assertEqual(data["riskLeakCount"], 0)

        states["9999"]["scheduleAllowed"] = True
        data = server_module.record_intraday_gate_stats(states, 1)
        self.assertEqual(data["riskLeakCount"], 1)
        self.assertEqual(data["riskLeakCodes"], ["9999"])

    def test_shadow_confirmation_accumulates_without_formal_buy_release(self):
        states = _bycode([])
        states["1111"]["shadowCanBuy"] = True
        data = server_module.record_intraday_gate_stats(states, 3)
        self.assertEqual(data["buyableUnion"], 0)
        self.assertEqual(data["shadowBuyableUnion"], 1)
        self.assertEqual(data["shadowBuyableCodes"], ["1111"])

    def test_quote_provenance_tracks_provider_fallback_and_wrong_dates(self):
        states = {
            "1111": {
                "hasIntradayQuote": True,
                "quoteFresh": True,
                "snapshotAt": "2026-07-07 09:55:00",
                "source": "Sinopac Shioaji",
            },
            "2222": {
                "hasIntradayQuote": True,
                "quoteFresh": True,
                "snapshotAt": "2026-07-07 09:55:01",
                "source": "Capital 群益",
            },
            "3333": {
                "hasIntradayQuote": True,
                "quoteFresh": True,
                "snapshotAt": "2026-07-06 13:30:00",
                "source": "Sinopac stale cache",
            },
        }
        data = server_module.record_intraday_gate_stats(states, 3)
        self.assertEqual(data["freshQuoteTimestampCount"], 3)
        self.assertEqual(data["fallbackQuoteCount"], 1)
        self.assertEqual(data["fallbackQuoteCodes"], ["2222"])
        self.assertEqual(data["quoteDateMismatchCodes"], ["3333"])
        self.assertEqual(data["quoteSources"]["Capital 群益"], 1)


if __name__ == "__main__":
    unittest.main()
