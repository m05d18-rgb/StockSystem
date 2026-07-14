import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server


class IntradayDiscoveryRecallScheduleTests(unittest.TestCase):
    def test_schedule_window_runs_after_official_close_sync_has_time_to_finish(self):
        self.assertEqual(
            server.AUTO_SCHEDULE_WINDOWS[server.INTRADAY_DISCOVERY_RECALL_JOB_ID],
            (18, 10, 18, 30),
        )

    def test_worker_retries_until_today_official_close_is_complete(self):
        with patch.object(server, "scheduler_today", return_value="2026-07-14"), \
                patch.object(
                    server.backend, "latest_complete_price_date", return_value="2026-07-13",
                ):
            result = server.auto_intraday_discovery_recall()

        self.assertIs(result, server.AUTO_SCHEDULE_RETRY)

    def test_worker_reports_real_recall_after_settlement(self):
        report = {
            "ok": True,
            "actualMovers": 10,
            "detectedMovers": 8,
            "missedMovers": 2,
            "recall": 0.8,
        }
        with patch.object(server, "scheduler_today", return_value="2026-07-14"), \
                patch.object(
                    server.backend, "latest_complete_price_date", return_value="2026-07-14",
                ), patch.object(
                    server.backend, "settle_intraday_discovery_recall", return_value=report,
                ) as settle, patch.object(
                    server.backend, "compute_intraday_candidate_accuracy",
                    return_value={"ok": True, "settled": 3, "pending": 2},
                ) as accuracy:
            result = server.auto_intraday_discovery_recall()

        settle.assert_called_once_with("2026-07-14")
        accuracy.assert_called_once_with(lookback_days=365)
        self.assertIn("找到率 80.0%", result)
        self.assertIn("可買訊號已結算 3 筆", result)

    def test_worker_marks_incomplete_observation_day_as_not_scored(self):
        report = {
            "ok": True,
            "skipped": True,
            "reason": "排行週期不足，不納入找到率",
        }
        with patch.object(server, "scheduler_today", return_value="2026-07-14"), \
                patch.object(
                    server.backend, "latest_complete_price_date", return_value="2026-07-14",
                ), patch.object(
                    server.backend, "settle_intraday_discovery_recall", return_value=report,
                ), patch.object(
                    server.backend, "compute_intraday_candidate_accuracy",
                    return_value={"ok": True, "settled": 0, "pending": 0},
                ):
            result = server.auto_intraday_discovery_recall()

        self.assertIn("不納入 2026-07-14", result)


if __name__ == "__main__":
    unittest.main()
