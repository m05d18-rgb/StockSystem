"""
2026-07-04 稽核修復(Finding 3)回歸：scan_monster_scores 的 MONSTER_WATCH_SYMBOLS
強制加入候選時，原本只判 `if watch_quick:` 而非 `watch_quick["ok"]`。
quick_monster_filter 對 stale_price_data / insufficient_history /
missing_verified_source 都回傳「truthy dict + ok=False」而非 None，所以斷更/
歷史不足/低流動的 watch 股會繞過快篩閘門進 predict_symbol，用凍結的舊日線
算出無法成交的買點寫進「今天」的掃描結果。修法：改成跟主候選迴圈一致，
只放行 ok=True。

本測試把 scan_monster_scores 的重量級相依(網路/模型/DB 寫入)全部 mock 掉，
只驗證閘門行為：ok=False 的 watch 股不得進入評分路徑(monster_score_for_symbol)。

2026-07-10 模型與妖股候選完全拆開：掃描改純型態量能，不可載入模型、重訓或
呼叫 predict_symbol；模型只由獨立批量預測/紙上交易流程運行。本測試同時鎖住
load_model 與 predict_symbol 都不得被妖股掃描碰到。

執行方式：
  python -m unittest tests.test_monster_watch_gate -v
"""
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ml_backend
from ml_backend import backend


class MonsterWatchGateTests(unittest.TestCase):
    WATCH = "ZTESTWATCH"

    def _run_scan_with_watch(self, watch_quick_result, score_should_run=False):
        # quick_monster_filter：主候選一律回 None(不干擾)，watch 股回傳指定結果。
        # 重量級相依全 mock；save_monster_score/set_meta/connect 一律隔離，
        # 絕不碰正式 DB(尤其 scan 尾端會 DELETE 當天 monster_scores，不隔離會刪
        # 到正式資料——見 feedback_test_id_isolation 鐵律)。
        def fake_quick(symbol):
            if symbol == self.WATCH:
                return watch_quick_result
            return None

        if score_should_run:
            score_spy = MagicMock(return_value={"symbol": self.WATCH, "score": 0})
        else:
            score_spy = MagicMock(side_effect=AssertionError("ok=False 的 watch 股不該進 monster_score_for_symbol"))
        model_spy = MagicMock(side_effect=AssertionError("妖股掃描不該載入或重訓模型"))
        predict_spy = MagicMock(side_effect=AssertionError("掃描去模型後不該呼叫 predict_symbol"))

        with patch.object(ml_backend, "MONSTER_WATCH_SYMBOLS", {self.WATCH}), \
             patch.object(backend, "update_market_data", return_value={}), \
             patch.object(backend, "load_model", model_spy), \
             patch.object(backend, "quick_monster_filter", side_effect=fake_quick), \
             patch.object(backend, "load_stock_info", return_value={}), \
             patch.object(backend, "compute_sector_momentum", return_value={}), \
             patch.object(backend, "update_prices", return_value={}), \
             patch.object(backend, "predict_symbol", predict_spy), \
             patch.object(backend, "monster_score_for_symbol", score_spy), \
             patch.object(backend, "save_monster_score", return_value=None), \
             patch.object(backend, "set_meta", return_value=None), \
             patch.object(backend, "connect", return_value=MagicMock()):
            backend.scan_monster_scores(symbols=[self.WATCH], limit=10, score_limit=10)
        # 不論 ok/not-ok，妖股掃描都不該碰模型載入或推論。
        model_spy.assert_not_called()
        predict_spy.assert_not_called()
        return score_spy

    def test_stale_watch_symbol_does_not_reach_scoring(self):
        # stale_price_data：truthy dict 但 ok=False——修復後不得進 monster_score_for_symbol
        stale = {"ok": False, "reason": "stale_price_data(2020-01-01)", "symbol": self.WATCH}
        score_spy = self._run_scan_with_watch(stale)
        score_spy.assert_not_called()

    def test_insufficient_history_watch_symbol_does_not_reach_scoring(self):
        rejected = {"ok": False, "reason": "insufficient_history", "symbol": self.WATCH}
        score_spy = self._run_scan_with_watch(rejected)
        score_spy.assert_not_called()

    def test_ok_watch_symbol_still_reaches_scoring(self):
        # 對照組：ok=True 的 watch 股仍要正常進入型態量能評分(修復不能誤殺正常 watch)。
        good = {"ok": True, "symbol": self.WATCH, "score": 5.0}
        score_spy = self._run_scan_with_watch(good, score_should_run=True)
        score_spy.assert_called_once()
        self.assertEqual(score_spy.call_args[0][0], self.WATCH)
        # 鎖住去模型:掃描評分一定帶 use_model=False。
        self.assertIs(score_spy.call_args.kwargs.get("use_model"), False)

    def test_closed_market_scan_is_invalidated_before_database_write(self):
        scored = {
            "symbol": self.WATCH,
            "score": 90,
            "buyAllowed": True,
            "action": "NEXT_DAY_WATCH",
        }
        save_spy = MagicMock()

        def fake_quick(symbol):
            return {"ok": True, "symbol": symbol, "score": 90}

        with patch.object(ml_backend, "MONSTER_WATCH_SYMBOLS", {self.WATCH}), \
             patch.object(backend, "update_market_data", return_value={}), \
             patch.object(backend, "quick_monster_filter", side_effect=fake_quick), \
             patch.object(backend, "load_stock_info", return_value={}), \
             patch.object(backend, "compute_sector_momentum", return_value={}), \
             patch.object(backend, "update_prices", return_value={}), \
             patch.object(backend, "monster_score_for_symbol", return_value=scored), \
             patch.object(backend, "save_monster_score", save_spy), \
             patch.object(backend, "set_meta", return_value=None), \
             patch.object(backend, "_cached_market_day_status", return_value={
                 "known": True,
                 "isTradingDay": False,
                 "reason": "週末",
             }), \
             patch.object(backend, "connect", return_value=MagicMock()):
            backend.scan_monster_scores(symbols=[self.WATCH], limit=10, score_limit=10)

        saved = save_spy.call_args.args[1]
        self.assertTrue(saved["recordedBuyAllowed"])
        self.assertFalse(saved["buyAllowed"])
        self.assertTrue(saved["invalidForTrading"])
        self.assertEqual(saved["action"], "WAIT")


if __name__ == "__main__":
    unittest.main()
