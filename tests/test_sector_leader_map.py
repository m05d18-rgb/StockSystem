"""
族群龍頭連動地圖 sector_leader_map() 的回歸測試。

功能D：熱門族群不只顯示名字，攤開族群內的候選股，龍頭(系統評分最高)領軍、
其餘為跟漲候補。純讀 list_monster_scores() 既有結果分組排序，不重掃、不打 FinMind。

以 monkeypatch 餵固定的 list_monster_scores 回傳值，驗證分組/排序/龍頭標記/
只保留熱門族群 的邏輯。

執行方式：
  python -m unittest tests.test_sector_leader_map -v
"""
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ml_backend


def _fake_scores(candidates, hot_sectors, sector_momentum=None):
    return {
        "ok": True,
        "scanDate": "2026-07-03",
        "candidates": candidates,
        "hotSectors": hot_sectors,
        "sectorMomentum": sector_momentum or {},
    }


def _cand(symbol, name, sector, score, change1=1.0, change5=5.0, buy=True):
    return {
        "symbol": symbol, "name": name, "sector": sector, "score": score,
        "change1": change1, "change5": change5, "buyAllowed": buy,
    }


class SectorLeaderMapTests(unittest.TestCase):
    def _run(self, candidates, hot_sectors, sector_momentum=None):
        with patch.object(ml_backend.backend, "list_monster_scores",
                          return_value=_fake_scores(candidates, hot_sectors, sector_momentum)):
            return ml_backend.backend.sector_leader_map()

    def test_only_hot_sectors_included(self):
        cands = [
            _cand("1111", "熱股A", "航運", 80),
            _cand("2222", "冷股B", "冷門業", 90),  # 分數更高但不在熱門族群
        ]
        r = self._run(cands, hot_sectors=["航運"])
        self.assertTrue(r["ok"])
        sectors = {s["sector"] for s in r["sectors"]}
        self.assertEqual(sectors, {"航運"}, "只有熱門族群該出現，冷門業要被過濾")

    def test_leader_is_highest_score(self):
        cands = [
            _cand("1111", "跟漲", "航運", 70),
            _cand("2222", "龍頭", "航運", 88),
            _cand("3333", "跟漲2", "航運", 75),
        ]
        r = self._run(cands, hot_sectors=["航運"])
        grp = r["sectors"][0]
        self.assertEqual(grp["leader"]["symbol"], "2222", "分數最高者是龍頭")
        self.assertTrue(grp["members"][0]["isLeader"])
        self.assertFalse(grp["members"][1]["isLeader"])
        # 成員依分數由高到低
        self.assertEqual([m["symbol"] for m in grp["members"]], ["2222", "3333", "1111"])

    def test_hot_sector_ordering_preserved(self):
        cands = [
            _cand("1111", "A", "航運", 80),
            _cand("2222", "B", "生技", 80),
        ]
        # hotSectors 已是 excessRet5 由高到低的既有排序，輸出要沿用
        r = self._run(cands, hot_sectors=["生技", "航運"])
        self.assertEqual([s["sector"] for s in r["sectors"]], ["生技", "航運"])

    def test_empty_hot_sector_group_skipped(self):
        # 族群在 hotSectors 名單上，但候選裡沒有該族群任何一檔 → 不輸出空群組
        cands = [_cand("1111", "A", "航運", 80)]
        r = self._run(cands, hot_sectors=["航運", "鋼鐵"])
        self.assertEqual([s["sector"] for s in r["sectors"]], ["航運"])

    def test_sector_momentum_stats_surfaced(self):
        cands = [_cand("1111", "A", "航運", 80)]
        momentum = {"航運": {"excessRet5": 10.3, "avgRet5": 12.1, "persistentHot": True}}
        r = self._run(cands, hot_sectors=["航運"], sector_momentum=momentum)
        grp = r["sectors"][0]
        self.assertEqual(grp["excessRet5"], 10.3)
        self.assertEqual(grp["avgRet5"], 12.1)
        self.assertTrue(grp["persistentHot"])

    def test_change_values_rounded(self):
        cands = [_cand("1111", "A", "航運", 80, change1=-2.6455026455, change5=14.880332986)]
        r = self._run(cands, hot_sectors=["航運"])
        m = r["sectors"][0]["members"][0]
        self.assertEqual(m["change1"], -2.6)
        self.assertEqual(m["change5"], 14.9)

    def test_none_score_sorted_last(self):
        cands = [
            _cand("1111", "有分", "航運", 60),
            _cand("2222", "無分", "航運", None),
        ]
        r = self._run(cands, hot_sectors=["航運"])
        members = r["sectors"][0]["members"]
        self.assertEqual(members[0]["symbol"], "1111", "有分數者排前面當龍頭")
        self.assertEqual(members[-1]["symbol"], "2222", "score=None 排最後")

    def test_no_hot_sectors_returns_empty(self):
        cands = [_cand("1111", "A", "航運", 80)]
        r = self._run(cands, hot_sectors=[])
        self.assertTrue(r["ok"])
        self.assertEqual(r["sectors"], [])

    def test_missing_sector_defaults_and_filtered(self):
        # 候選無 sector → 落到「台股」；若「台股」不在 hotSectors 就不輸出
        cands = [
            {"symbol": "1111", "name": "無族群", "score": 80, "change1": 1, "change5": 5, "buyAllowed": True},
        ]
        r = self._run(cands, hot_sectors=["航運"])
        self.assertEqual(r["sectors"], [], "無 sector 落到台股，不在熱門名單就過濾掉")

    def test_missing_sector_included_when_taiwan_is_hot(self):
        # 若「台股」本身在 hotSectors，無 sector 的候選就會被歸進來
        cands = [
            {"symbol": "1111", "name": "無族群", "score": 80, "change1": 1, "change5": 5, "buyAllowed": True},
        ]
        r = self._run(cands, hot_sectors=["台股"])
        self.assertEqual([s["sector"] for s in r["sectors"]], ["台股"])


if __name__ == "__main__":
    unittest.main()
