"""
read_meta_int(惰性 fallback)的回歸測試。

對應 2026-07-03「電腦版很LAG」事故：list_monster_scores 把
len(self.liquid_monster_universe())(~2.6秒的全市場流動性掃描)當一般參數
傳給 meta_int 當 fallback，Python eager 評估讓每個 /api/monster-scores
請求都白跑一次，端點 3 秒、整個桌面介面跟著卡。修法是 fallback 支援
callable 惰性求值——meta 有值時昂貴計算絕不能被執行。

執行方式：
  python -m unittest tests.test_read_meta_int -v
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ml_backend import read_meta_int


class ReadMetaIntTests(unittest.TestCase):
    def test_meta_value_present_returns_it(self):
        self.assertEqual(read_meta_int({"k": "681"}, "k", 0), 681)
        self.assertEqual(read_meta_int({"k": 42.7}, "k", 0), 42)
        self.assertEqual(read_meta_int({"k": "0"}, "k", 99), 0, "meta 存 0 是有效值，不能落到 fallback")

    def test_expensive_callable_fallback_not_called_when_meta_present(self):
        calls = {"n": 0}

        def expensive():
            calls["n"] += 1
            return 12345

        result = read_meta_int({"k": "681"}, "k", expensive)
        self.assertEqual(result, 681)
        self.assertEqual(calls["n"], 0, "meta 有值時昂貴 fallback 絕不能被執行(2.6秒全市場掃描)")

    def test_callable_fallback_called_when_meta_missing(self):
        calls = {"n": 0}

        def expensive():
            calls["n"] += 1
            return 12345

        self.assertEqual(read_meta_int({}, "k", expensive), 12345)
        self.assertEqual(calls["n"], 1)

    def test_plain_fallback_still_works(self):
        self.assertEqual(read_meta_int({}, "k", 7), 7)
        self.assertEqual(read_meta_int({"k": ""}, "k", 7), 7)
        self.assertEqual(read_meta_int({"k": None}, "k", 7), 7)

    def test_unparseable_meta_value_falls_back(self):
        self.assertEqual(read_meta_int({"k": "not-a-number"}, "k", lambda: 5), 5)

    def test_falsy_fallback_result_is_zero(self):
        self.assertEqual(read_meta_int({}, "k", lambda: None), 0)


if __name__ == "__main__":
    unittest.main()
