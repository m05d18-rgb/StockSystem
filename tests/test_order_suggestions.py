"""
今日可買清單 order_suggestion_list() 的回歸測試。

功能F：把系統判定可買(buyAllowed)的候選集中成一張精簡表(進場/回檔/停損/停利)，
省得在整個雷達清單裡東翻西找。**純參考清單，不會自動下單**——manualOnly=True
讓前端顯示「需自行手動下單」。純讀 list_monster_scores() 既有結果，不重掃/不打 FinMind/
不觸發任何委託。

以 monkeypatch 餵固定 list_monster_scores 回傳值，驗證過濾/欄位/排序/manualOnly。

執行方式：
  python -m unittest tests.test_order_suggestions -v
"""
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ml_backend


def _cand(symbol, score, buy_allowed, buy_trigger=100.0, pullback=97.0, stop=93.0, take=115.0):
    return {
        "symbol": symbol, "name": f"股{symbol}", "sector": "航運", "score": score,
        "buyAllowed": buy_allowed, "buy_trigger": buy_trigger, "pullback_price": pullback,
        "stop_price": stop, "take_profit": take,
    }


class OrderSuggestionListTests(unittest.TestCase):
    def _run(self, candidates):
        fake = {
            "ok": True,
            "scanDate": "2026-07-03",
            "candidates": candidates,
            "decisionValidity": {
                "validForTrading": True,
                "invalidForTrading": False,
                "invalidReasons": [],
                "summary": "雷達決策資料有效",
            },
        }
        with patch.object(ml_backend.backend, "list_monster_scores", return_value=fake):
            return ml_backend.backend.order_suggestion_list()

    def test_only_buyable_included(self):
        cands = [_cand("1111", 80, True), _cand("2222", 90, False), _cand("3333", 70, True)]
        r = self._run(cands)
        symbols = {s["symbol"] for s in r["suggestions"]}
        self.assertEqual(symbols, {"1111", "3333"}, "只列 buyAllowed 的候選")
        self.assertEqual(r["count"], 2)

    def test_manual_only_flag_true(self):
        r = self._run([_cand("1111", 80, True)])
        self.assertTrue(r["manualOnly"], "必須標記 manualOnly，前端據此提示需手動下單")

    def test_sorted_by_score_desc(self):
        cands = [_cand("1111", 70, True), _cand("2222", 92, True), _cand("3333", 81, True)]
        r = self._run(cands)
        self.assertEqual([s["symbol"] for s in r["suggestions"]], ["2222", "3333", "1111"])

    def test_price_fields_rounded_and_mapped(self):
        cands = [_cand("1111", 80, True, buy_trigger=201.006, pullback=194.974,
                       stop=183.691071, take=228.694285)]
        r = self._run(cands)
        s = r["suggestions"][0]
        self.assertEqual(s["entryTrigger"], 201.01)
        self.assertEqual(s["pullbackPrice"], 194.97)
        self.assertEqual(s["stopPrice"], 183.69)
        self.assertEqual(s["takeProfit"], 228.69)

    def test_empty_when_no_buyable(self):
        cands = [_cand("1111", 80, False), _cand("2222", 90, False)]
        r = self._run(cands)
        self.assertTrue(r["ok"])
        self.assertEqual(r["count"], 0)
        self.assertEqual(r["suggestions"], [])

    def test_none_price_fields_tolerated(self):
        cands = [{"symbol": "1111", "name": "無價股", "sector": "航運", "score": 80,
                  "buyAllowed": True, "buy_trigger": None, "pullback_price": None,
                  "stop_price": None, "take_profit": None}]
        r = self._run(cands)
        s = r["suggestions"][0]
        self.assertIsNone(s["entryTrigger"])
        self.assertIsNone(s["stopPrice"])

    def test_scan_date_passed_through(self):
        r = self._run([_cand("1111", 80, True)])
        self.assertEqual(r["scanDate"], "2026-07-03")

    def test_invalid_radar_returns_empty_with_reason(self):
        fake = {
            "ok": True,
            "scanDate": "2026-07-10",
            "candidates": [{**_cand("1111", 80, False), "recordedBuyAllowed": True, "policyBuyAllowed": True}],
            "decisionValidity": {
                "validForTrading": False,
                "invalidForTrading": True,
                "invalidReasons": [{"code": "scan_market_closed", "label": "掃描日為休市日"}],
                "summary": "掃描日為休市日",
            },
        }
        with patch.object(ml_backend.backend, "list_monster_scores", return_value=fake):
            result = ml_backend.backend.order_suggestion_list()
        self.assertEqual(result["suggestions"], [])
        self.assertEqual(result["count"], 0)
        self.assertEqual(result["suppressedCount"], 1)
        self.assertIn("休市", result["reason"])

    def test_performance_gate_suppresses_policy_buyable_candidates(self):
        fake = {
            "ok": True,
            "scanDate": "2026-07-13",
            "candidates": [{**_cand("1111", 80, False), "policyBuyAllowed": True}],
            "decisionValidity": {
                "validForTrading": True,
                "invalidForTrading": False,
                "invalidReasons": [],
                "summary": "雷達決策資料有效",
            },
            "deploymentReadiness": {
                "enforced": True,
                "formalReady": False,
                "reasons": ["真實候選平均成本後報酬尚未轉正"],
            },
        }
        with patch.object(ml_backend.backend, "list_monster_scores", return_value=fake):
            result = ml_backend.backend.order_suggestion_list()
        self.assertEqual(result["suggestions"], [])
        self.assertEqual(result["suppressedCount"], 1)
        self.assertIn("尚未轉正", result["reason"])


if __name__ == "__main__":
    unittest.main()
