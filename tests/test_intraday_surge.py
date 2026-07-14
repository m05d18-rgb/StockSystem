"""
盤中即時突破/點火掃描(detect_intraday_surge / notify_intraday_surge)的回歸測試。

妖股候選盤中急拉(漲幅衝過6%、還沒鎖漲停<9.5%、且現價貼近當日高=強勢拉升途中)=
點火訊號，當天每檔第一次偵測到推 LINE 一次(每日上限、每檔去重、先標記再送)。

隔離鐵律：去重狀態存正式 model_meta，測試一律 patch INTRADAY_SURGE_NOTIFY_STATE_KEY
成 __test_*__ 假 key 並在 setUp/tearDown 清理，絕不觸碰正式的 intraday_surge_notify_state。
LINE 全部 mock，不打真實 API。

執行方式：
  python -m unittest tests.test_intraday_surge -v
"""
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server as server_module

TEST_STATE_KEY = "__test_intraday_surge_notify_state__"
TEST_LINE_BUDGET_KEY = "__test_intraday_line_budget_state__"


class DetectIntradaySurgeTests(unittest.TestCase):
    """純判斷。ref=100 → +6%=106(門檻)、+9.5%=109.5(近漲停排除)、貼近高門檻=high×0.98。"""

    def test_surge_above_threshold_near_high_is_true(self):
        # +7%(107)、當日高107、現價貼齊高：強勢點火
        self.assertTrue(server_module.detect_intraday_surge(100, 107, 107))

    def test_below_threshold_is_false(self):
        # +4%(104)未達6%門檻
        self.assertFalse(server_module.detect_intraday_surge(100, 104, 104))

    def test_near_limit_up_excluded(self):
        # +9.6%(109.6)已貼近漲停，交給漲停打開邏輯，不在這裡推
        self.assertFalse(server_module.detect_intraday_surge(100, 109.6, 109.6))

    def test_pulled_back_from_high_is_false(self):
        # 漲幅+7%達標，但現價107、當日高110→107 < 110×0.98=107.8，已回落非強勢
        self.assertFalse(server_module.detect_intraday_surge(100, 107, 110))

    def test_boundary_exactly_at_threshold_is_true(self):
        # 剛好+6%(106)、貼齊高：含入
        self.assertTrue(server_module.detect_intraday_surge(100, 106, 106))

    def test_invalid_prices_false(self):
        self.assertFalse(server_module.detect_intraday_surge(0, 107, 107))
        self.assertFalse(server_module.detect_intraday_surge(100, 0, 107))
        self.assertFalse(server_module.detect_intraday_surge(100, 107, 0))
        self.assertFalse(server_module.detect_intraday_surge(100, 107, 106))
        self.assertFalse(server_module.detect_intraday_surge(None, None, None))


class NotifyIntradaySurgeTests(unittest.TestCase):
    def setUp(self):
        # 整併 I 後，⑤盤中點火也會查跨路徑 LINE 閘門，這個共用 key 也要隔離。
        self._patchers = [
            patch.object(server_module, "INTRADAY_SURGE_NOTIFY_STATE_KEY", TEST_STATE_KEY),
            patch.object(server_module, "INTRADAY_LINE_BUDGET_STATE_KEY", TEST_LINE_BUDGET_KEY),
        ]
        for p in self._patchers:
            p.start()
        self._cleanup()

    def tearDown(self):
        self._cleanup()
        for p in self._patchers:
            p.stop()

    def _cleanup(self):
        with server_module.backend.connect() as conn:
            conn.execute("DELETE FROM model_meta WHERE key IN (?, ?)", (TEST_STATE_KEY, TEST_LINE_BUDGET_KEY))
            conn.commit()

    def _quotes(self, surging=True):
        # ZTEST8001 點火(+7%貼高)、ZTEST8002 平淡(+2%)
        return {
            "ZTEST8001": {"referencePrice": 100, "currentPrice": 107 if surging else 102, "highPrice": 107 if surging else 102},
            "ZTEST8002": {"referencePrice": 50, "currentPrice": 51, "highPrice": 51},
        }

    def _candidates(self):
        return [{"symbol": "ZTEST8001", "name": "點火妖股"}, {"symbol": "ZTEST8002", "name": "普通股"}]

    def _states(self, overrides=None):
        state = {
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
        }
        state.update(overrides or {})
        return {"ZTEST8001": state, "ZTEST8002": dict(state)}

    def test_fresh_surge_notifies_once_and_dedupes(self):
        with patch.object(server_module, "send_line_message_via_api") as mock_send:
            first = server_module.notify_intraday_surge(self._quotes(), self._candidates(), self._states())
            self.assertTrue(first["notified"])
            self.assertEqual(first["fresh"], ["ZTEST8001"])
            self.assertTrue(first["line"])
            mock_send.assert_called_once()
            second = server_module.notify_intraday_surge(self._quotes(), self._candidates(), self._states())
            self.assertFalse(second["notified"])
            mock_send.assert_called_once()

    def test_no_surge_does_not_notify(self):
        with patch.object(server_module, "send_line_message_via_api") as mock_send:
            result = server_module.notify_intraday_surge(self._quotes(surging=False), self._candidates(), self._states())
            self.assertFalse(result["notified"])
            mock_send.assert_not_called()

    def test_daily_line_cap_marks_but_skips_send(self):
        today = server_module.scheduler_today(server_module.taipei_localtime())
        server_module._write_intraday_surge_notify_state({
            "date": today, "symbols": [], "lineCount": server_module.INTRADAY_SURGE_NOTIFY_DAILY_LINE_CAP,
        })
        with patch.object(server_module, "send_line_message_via_api") as mock_send:
            result = server_module.notify_intraday_surge(self._quotes(), self._candidates(), self._states())
        self.assertTrue(result["notified"])
        self.assertFalse(result["line"])
        mock_send.assert_not_called()
        state = server_module._read_intraday_surge_notify_state(today)
        self.assertIn("ZTEST8001", state["symbols"])

    def test_danger_or_stale_or_weak_volume_surge_is_blocked(self):
        cases = (
            {"dangerRisk": True, "scheduleAllowed": False},
            {"quoteFresh": False},
            {"candidateDailyDataFresh": False},
            {"marketDataFresh": False},
            {"volumeContinue": False},
        )
        with patch.object(server_module, "send_line_message_via_api") as mock_send:
            for overrides in cases:
                with self.subTest(overrides=overrides):
                    result = server_module.notify_intraday_surge(
                        self._quotes(), self._candidates(), self._states(overrides),
                    )
                    self.assertFalse(result["notified"])
                    self.assertEqual(result["blocked"], ["ZTEST8001"])
        mock_send.assert_not_called()

    def test_surge_after_initial_entry_window_is_blocked(self):
        states = self._states({"entryWindowPhase": "dip"})
        result = server_module.notify_intraday_surge(self._quotes(), self._candidates(), states)
        self.assertFalse(result["notified"])
        self.assertEqual(result["blocked"], ["ZTEST8001"])


if __name__ == "__main__":
    unittest.main()
