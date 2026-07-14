"""
盤中 LINE 跨路徑去重＋總量閘門(整併 I)的回歸測試。

3 條盤中進場 LINE 路徑(#169 進場翻正/②漲停打開/⑤盤中點火)原本各自去重、各自上限，
同一檔可跨多路徑收到多則(合計上限可達 6~8 則)。整併成共用閘門：同股當天只要被任一
進場路徑 LINE 過，其餘路徑不再重複推 LINE；再加跨路徑每日總量上限。只收斂 LINE、不影響
桌面/網頁訊號；exit_guardian 出場訊號豁免(不納入此閘門)。

隔離鐵律：所有 meta state key 都 patch 成 __test_*__，setUp/tearDown 清乾淨，
**絕不動到正式去重 key**(誤刪會害真實盤中重複 LINE)。

執行方式：
  python -m unittest tests.test_intraday_line_budget -v
"""
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server
from ml_backend import backend

TEST_BUDGET_KEY = "__test_intraday_line_budget__"
TEST_LIMIT_KEY = "__test_limit_up_open_notify__"
TEST_SURGE_KEY = "__test_intraday_surge_notify__"
TODAY = "2099-01-01"


def _clear_keys():
    with backend.connect() as conn:
        for k in (TEST_BUDGET_KEY, TEST_LIMIT_KEY, TEST_SURGE_KEY):
            conn.execute("DELETE FROM model_meta WHERE key = ?", (k,))
        conn.commit()


class CrossPathGateHelperTests(unittest.TestCase):
    def setUp(self):
        self._p = patch.object(server, "INTRADAY_LINE_BUDGET_STATE_KEY", TEST_BUDGET_KEY)
        self._p.start()
        _clear_keys()

    def tearDown(self):
        self._p.stop()
        _clear_keys()

    def test_all_allowed_when_nothing_lined(self):
        self.assertEqual(server.cross_path_line_candidates(["1101", "2330"], TODAY), ["1101", "2330"])

    def test_marked_symbol_excluded_cross_path(self):
        server.mark_cross_path_lined(["1101"], TODAY)
        # 1101 已被某路徑 LINE 過 → 其餘路徑不再推它，但 2330 仍可
        self.assertEqual(server.cross_path_line_candidates(["1101", "2330"], TODAY), ["2330"])

    def test_mark_increments_total_and_dedups(self):
        server.mark_cross_path_lined(["1101"], TODAY)
        state = server._read_intraday_line_budget_state(TODAY)
        self.assertEqual(state["totalLineCount"], 1)
        self.assertIn("1101", state["notifiedSymbols"])

    def test_total_cap_blocks_all(self):
        with patch.object(server, "INTRADAY_TOTAL_LINE_CAP", 2):
            server.mark_cross_path_lined(["A"], TODAY)
            server.mark_cross_path_lined(["B"], TODAY)  # total=2=cap
            # 達上限後，即使是全新 symbol 也一律不再 LINE
            self.assertEqual(server.cross_path_line_candidates(["C"], TODAY), [])

    def test_cross_day_reset(self):
        server.mark_cross_path_lined(["1101"], TODAY)
        # 換一天 → 全新狀態，1101 又可推
        self.assertEqual(server.cross_path_line_candidates(["1101"], "2099-01-02"), ["1101"])

    def test_mark_empty_is_noop(self):
        server.mark_cross_path_lined([], TODAY)
        state = server._read_intraday_line_budget_state(TODAY)
        self.assertEqual(state["totalLineCount"], 0)
        self.assertEqual(state["notifiedSymbols"], [])

    def test_clear_reenables_symbol_without_decrementing_total(self):
        # #169 reflip：canBuy 掉回 false 清除跨路徑標記，讓全新翻正能再推；
        # 但已送出的訊息仍計入當日總量(totalLineCount 不回退)。
        server.mark_cross_path_lined(["1101"], TODAY)
        server.clear_cross_path_lined(["1101"], TODAY)
        self.assertEqual(server.cross_path_line_candidates(["1101"], TODAY), ["1101"], "清除後可再推")
        state = server._read_intraday_line_budget_state(TODAY)
        self.assertEqual(state["totalLineCount"], 1, "清除不回退總量(訊息已送出)")

    def test_clear_noncross_is_noop(self):
        server.mark_cross_path_lined(["1101"], TODAY)
        server.clear_cross_path_lined(["9999"], TODAY)  # 不在集合中
        self.assertIn("1101", server._read_intraday_line_budget_state(TODAY)["notifiedSymbols"])


class PathsRespectGateTests(unittest.TestCase):
    """實際的 ②⑤ 路徑函式要遵守跨路徑閘門：已被其他路徑 LINE 過的股票不重推。"""

    def setUp(self):
        self._patches = [
            patch.object(server, "INTRADAY_LINE_BUDGET_STATE_KEY", TEST_BUDGET_KEY),
            patch.object(server, "LIMIT_UP_OPEN_NOTIFY_STATE_KEY", TEST_LIMIT_KEY),
            patch.object(server, "INTRADAY_SURGE_NOTIFY_STATE_KEY", TEST_SURGE_KEY),
        ]
        for p in self._patches:
            p.start()
        _clear_keys()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        _clear_keys()

    @staticmethod
    def _surge_states(code):
        return {code: {
            "hasIntradayQuote": True,
            "quoteFresh": True,
            "candidateDailyDataFresh": True,
            "marketDataFresh": True,
            "scheduleAllowed": True,
            "volumeContinue": True,
            "dangerRisk": False,
            "candidateOverheated": False,
            "windowBlocked": False,
            "entryWindowPhase": "initial",
        }}

    def test_surge_skips_symbol_already_lined_elsewhere(self):
        # 先模擬 ZTEST9 已被某路徑(如②)LINE 過
        server.mark_cross_path_lined(["ZTEST9"], server.scheduler_today(server.taipei_localtime()))
        # ⑤點火偵測：ref=100, cur=107, high=107 → 漲幅7%、貼近高 → surge=True
        quotes = {"ZTEST9": {"referencePrice": 100.0, "currentPrice": 107.0, "highPrice": 107.0}}
        with patch.object(server, "send_line_message_via_api", return_value={"sent": True}) as mock_send:
            result = server.notify_intraday_surge(
                quotes, [{"symbol": "ZTEST9", "name": "測試"}], self._surge_states("ZTEST9"),
            )
        self.assertFalse(result["line"], "已被其他路徑 LINE 過 → ⑤ 不重複推 LINE")
        mock_send.assert_not_called()

    def test_surge_lines_fresh_symbol_and_marks_cross_path(self):
        quotes = {"ZTEST8": {"referencePrice": 100.0, "currentPrice": 107.0, "highPrice": 107.0}}
        with patch.object(server, "send_line_message_via_api", return_value={"sent": True}) as mock_send:
            result = server.notify_intraday_surge(
                quotes, [{"symbol": "ZTEST8", "name": "測試"}], self._surge_states("ZTEST8"),
            )
        self.assertTrue(result["line"])
        mock_send.assert_called_once()
        # 送出後應標記到跨路徑狀態，後續其他路徑不再推它
        self.assertEqual(server.cross_path_line_candidates(["ZTEST8"], server.scheduler_today(server.taipei_localtime())), [])

    def test_limit_up_open_skips_symbol_already_lined(self):
        server.mark_cross_path_lined(["ZTEST7"], server.scheduler_today(server.taipei_localtime()))
        # ②漲停打開：ref=100, high=110(>=109.8 摸漲停), cur=109(<=109.4 打開回落)
        quotes = {"ZTEST7": {"referencePrice": 100.0, "highPrice": 110.0, "currentPrice": 109.0}}
        with patch.object(server, "send_line_message_via_api", return_value={"sent": True}) as mock_send:
            result = server.notify_limit_up_open(quotes, [{"symbol": "ZTEST7", "name": "測試"}])
        self.assertFalse(result["line"])
        mock_send.assert_not_called()


if __name__ == "__main__":
    unittest.main()
