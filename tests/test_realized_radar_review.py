"""
真實成交複盤(compute_realized_radar_review)的回歸測試。

把永豐真實已實現損益(每筆只有賣出日+損益、沒有買進日/買進價)對齊雷達推薦史
(monster_scores)，分成誠實三態：
  recommended    賣出日(含)之前雷達曾對這檔判可買(buy_allowed=1)
  candidate_only 有進候選池、但賣出日前那次判不可買
  not_scanned    雷達有在跑(交易在涵蓋窗內)，但持有期間這檔沒進候選
  no_history     交易早於 earliest_scan_date，雷達當時尚未記錄 → 無從評斷

刻意不算「跟單 vs 沒跟單」勝率——多數舊交易發生在雷達上線前，把那些當自選會製造
假訊號。純計算、無 DB。

執行方式：
  python -m unittest tests.test_realized_radar_review -v
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server as server_module

review = server_module.compute_realized_radar_review


def _rec(code, sell_date, pnl, pr_ratio=0.1, price=100.0, qty=1):
    return {"code": code, "realized_date": sell_date, "pnl": pnl,
            "pr_ratio": pr_ratio, "price": price, "quantity": qty}


def _local_rec(code, buy_date, sell_date, pnl, pr_ratio=0.1, price=100.0, qty=1):
    row = _rec(code, sell_date, pnl, pr_ratio=pr_ratio, price=price, qty=qty)
    row["radar_date"] = buy_date
    return row


class ComputeRealizedRadarReviewTests(unittest.TestCase):
    def test_empty_records(self):
        result = review([], {}, "2026-06-25")
        self.assertEqual(result["trades"], [])
        s = result["summary"]
        self.assertEqual(s["count"], 0)
        self.assertIsNone(s["winRate"])
        self.assertEqual(s["totalPnl"], 0)

    def test_recommended_uses_latest_scan_not_after_sell(self):
        # 賣出日之前有 buy_allowed=1 → recommended；賣出日之後的掃描必須被忽略
        radar = {"A": [
            {"scan_date": "2026-06-26", "buy_allowed": 1, "score": 88.0, "action": "NEXT_DAY_WATCH"},
            {"scan_date": "2026-07-01", "buy_allowed": 1, "score": 70.0, "action": "NEXT_DAY_WATCH"},
        ]}
        result = review([_rec("A", "2026-06-30", 14908, pr_ratio=0.2682, price=70.8)], radar, "2026-06-25")
        t = result["trades"][0]
        self.assertEqual(t["radarState"], "recommended")
        self.assertEqual(t["radarScanDate"], "2026-06-26", "要取賣出日之前的掃描，不是之後那筆")
        self.assertEqual(t["radarScore"], 88.0)
        self.assertEqual(t["pnlPct"], 26.82)
        self.assertAlmostEqual(t["buyPriceApprox"], 55.83, places=2)  # 70.8/(1+0.2682)

    def test_local_trade_uses_buy_date_not_sell_date_for_radar_alignment(self):
        # 本地 trades 有買進日後，雷達對齊要看買進日前是否已推薦；不能用賣出日前
        # 後來才出現的 buy_allowed=1 反推成「買進時雷達有推薦」。
        radar = {"A": [
            {"scan_date": "2026-07-03", "buy_allowed": 1, "score": 88.0, "action": "CAN_BUY"},
        ]}
        result = review([_local_rec("A", "2026-07-01", "2026-07-05", 1000)], radar, "2026-06-25")
        t = result["trades"][0]
        self.assertEqual(t["radarDate"], "2026-07-01")
        self.assertEqual(t["radarState"], "not_scanned")

    def test_candidate_only_when_never_buy_allowed(self):
        radar = {"B": [{"scan_date": "2026-06-30", "buy_allowed": 0, "score": 60.0, "action": "WAIT"}]}
        result = review([_rec("B", "2026-07-01", -500, pr_ratio=-0.05, price=95.0)], radar, "2026-06-25")
        t = result["trades"][0]
        self.assertEqual(t["radarState"], "candidate_only")
        self.assertEqual(t["radarScore"], 60.0)

    def test_not_scanned_in_window_without_radar_rows(self):
        # 賣出日在涵蓋窗內(>=earliest)、但這檔完全沒進過候選 → not_scanned
        result = review([_rec("C", "2026-07-02", 300)], {}, "2026-06-25")
        self.assertEqual(result["trades"][0]["radarState"], "not_scanned")

    def test_no_history_when_sold_before_coverage(self):
        # 賣出日早於雷達最早掃描日 → no_history(無從評斷)
        result = review([_rec("D", "2026-05-26", 3689, pr_ratio=0.0731, price=54.4)], {}, "2026-06-25")
        t = result["trades"][0]
        self.assertEqual(t["radarState"], "no_history")
        self.assertAlmostEqual(t["buyPriceApprox"], 50.69, places=2)  # 54.4/1.0731

    def test_pr_ratio_total_loss_no_divzero(self):
        # pr_ratio=-1(理論歸零)不可回推買價 → None，且不炸
        result = review([_rec("E", "2026-07-03", -1000, pr_ratio=-1.0, price=10.0)], {}, "2026-06-25")
        self.assertIsNone(result["trades"][0]["buyPriceApprox"])

    def test_missing_pr_ratio_gives_none_pct_and_buyprice(self):
        result = review([_rec("F", "2026-07-03", -1, pr_ratio=None, price=None)], {}, "2026-06-25")
        t = result["trades"][0]
        self.assertIsNone(t["pnlPct"])
        self.assertIsNone(t["buyPriceApprox"])
        self.assertEqual(t["pnl"], -1.0)

    def test_summary_buckets_and_winrate_and_sorting(self):
        radar = {
            "A": [{"scan_date": "2026-06-26", "buy_allowed": 1, "score": 88.0, "action": "NEXT_DAY_WATCH"}],
            "B": [{"scan_date": "2026-06-30", "buy_allowed": 0, "score": 60.0, "action": "WAIT"}],
        }
        records = [
            _rec("A", "2026-06-30", 14908, pr_ratio=0.2682, price=70.8),  # recommended, win
            _rec("B", "2026-07-01", -500, pr_ratio=-0.05, price=95.0),    # candidate_only, loss
            _rec("C", "2026-07-02", 300),                                 # not_scanned, win
            _rec("D", "2026-05-26", 3689, pr_ratio=0.0731, price=54.4),   # no_history, win
        ]
        result = review(records, radar, "2026-06-25")
        s = result["summary"]
        self.assertEqual(s["count"], 4)
        self.assertEqual(s["wins"], 3)
        self.assertEqual(s["losses"], 1)
        self.assertEqual(s["winRate"], round(3 / 4, 4))
        self.assertEqual(s["totalPnl"], round(14908 - 500 + 300 + 3689, 2))
        self.assertEqual(s["coverageStart"], "2026-06-25")
        self.assertEqual(s["inWindowCount"], 3, "D 賣在 05-26 早於涵蓋窗，不算 inWindow")
        bs = s["byState"]
        self.assertEqual(bs["recommended"]["count"], 1)
        self.assertEqual(bs["candidateOnly"]["count"], 1)
        self.assertEqual(bs["notScanned"]["count"], 1)
        self.assertEqual(bs["noHistory"]["count"], 1)
        self.assertEqual(bs["recommended"]["wins"], 1)
        # 依賣出日新到舊排序：C(07-02) → B(07-01) → A(06-30) → D(05-26)
        order = [t["code"] for t in result["trades"]]
        self.assertEqual(order, ["C", "B", "A", "D"])

    def test_names_are_attached(self):
        result = review([_rec("A", "2026-07-02", 100)], {}, "2026-06-25", names_by_code={"A": "台積電"})
        self.assertEqual(result["trades"][0]["name"], "台積電")

    def test_no_earliest_scan_date_falls_back_to_not_scanned(self):
        # earliest 為 None(monster_scores 全空) → 無 radar 列一律 not_scanned(不會誤判 no_history)
        result = review([_rec("Z", "2026-05-01", 100)], {}, None)
        self.assertEqual(result["trades"][0]["radarState"], "not_scanned")


if __name__ == "__main__":
    unittest.main()
