"""
sinopac_backend.py 即時報價快取(QUOTE_CACHE/QUOTE_CODE_CACHE)的回歸測試，
對應這次修的問題：這條路徑之前完全零測試覆蓋，且有兩個競態缺陷：

1. store_quote_cache() 的「聚合快取+逐檔快取」雙寫入完全沒有鎖保護，
   quotes() 的 check-then-act(先查快取、miss 才打 Shioaji)之間也沒有鎖，
   多個 thread 併發查詢重疊代碼時，較舊(較早查詢、較晚寫回)的結果可能
   覆蓋較新的快取。
2. 覆寫判斷原本只看「寫入完成時間」，不看「查詢發起時間」，導致排隊較久
   的舊查詢在鎖釋放後才寫入時，會蓋掉已經寫入的新查詢結果。

修法：加 QUOTE_CACHE_LOCK 包住 cached_quote_payload/store_quote_cache 整個
讀寫過程(避免半新半舊的中間態被讀到)；store_quote_cache 額外接受
fetched_at(查詢發起時間)，只有新查詢的 fetched_at >= 已存快取的 fetchedAt
才允許覆寫，避免較舊查詢覆蓋較新快照。

執行方式：
  python -m unittest tests.test_sinopac_quote_cache -v
"""
import os
import sys
import threading
import time
import unittest
import json
import subprocess
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sinopac_backend as sinopac_module
from sinopac_backend import sinopac_backend


class QuoteCacheStaleOverwriteTests(unittest.TestCase):
    def setUp(self):
        sinopac_module.QUOTE_CACHE.clear()
        sinopac_module.QUOTE_CODE_CACHE.clear()

    def tearDown(self):
        sinopac_module.QUOTE_CACHE.clear()
        sinopac_module.QUOTE_CODE_CACHE.clear()

    def test_older_fetch_does_not_overwrite_newer_snapshot(self):
        cache_key = ("2330",)
        newer_payload = {"ok": True, "quotes": {"2330": {"price": 105.0}}, "count": 1}
        older_payload = {"ok": True, "quotes": {"2330": {"price": 100.0}}, "count": 1}

        # 較新的查詢(fetched_at 較大)先完成寫入。
        sinopac_backend.store_quote_cache(cache_key, newer_payload, fetched_at=200.0)
        # 較舊的查詢(fetched_at 較小)因排隊晚完成，此時才呼叫 store_quote_cache。
        sinopac_backend.store_quote_cache(cache_key, older_payload, fetched_at=100.0)

        self.assertEqual(sinopac_module.QUOTE_CODE_CACHE["2330"]["quote"]["price"], 105.0)
        self.assertEqual(sinopac_module.QUOTE_CACHE[cache_key]["payload"]["quotes"]["2330"]["price"], 105.0)

    def test_newer_fetch_does_overwrite_older_snapshot(self):
        cache_key = ("2330",)
        older_payload = {"ok": True, "quotes": {"2330": {"price": 100.0}}, "count": 1}
        newer_payload = {"ok": True, "quotes": {"2330": {"price": 105.0}}, "count": 1}

        sinopac_backend.store_quote_cache(cache_key, older_payload, fetched_at=100.0)
        sinopac_backend.store_quote_cache(cache_key, newer_payload, fetched_at=200.0)

        self.assertEqual(sinopac_module.QUOTE_CODE_CACHE["2330"]["quote"]["price"], 105.0)

    def test_per_code_overwrite_decision_is_independent_per_symbol(self):
        # 兩批查詢代碼有重疊：2330 較新查詢比較晚，2303 較新查詢比較早，
        # 確認逐檔覆寫判斷是各自獨立比對，不會因為同一次呼叫而互相影響。
        sinopac_backend.store_quote_cache(
            ("2330", "2303"),
            {"ok": True, "quotes": {"2330": {"price": 100.0}, "2303": {"price": 50.0}}, "count": 2},
            fetched_at=100.0,
        )
        sinopac_backend.store_quote_cache(
            ("2330",),
            {"ok": True, "quotes": {"2330": {"price": 105.0}}, "count": 1},
            fetched_at=200.0,
        )
        sinopac_backend.store_quote_cache(
            ("2303",),
            {"ok": True, "quotes": {"2303": {"price": 48.0}}, "count": 1},
            fetched_at=50.0,
        )

        self.assertEqual(sinopac_module.QUOTE_CODE_CACHE["2330"]["quote"]["price"], 105.0)
        self.assertEqual(sinopac_module.QUOTE_CODE_CACHE["2303"]["quote"]["price"], 50.0)


class CachedQuotePayloadTests(unittest.TestCase):
    def setUp(self):
        sinopac_module.QUOTE_CACHE.clear()
        sinopac_module.QUOTE_CODE_CACHE.clear()

    def tearDown(self):
        sinopac_module.QUOTE_CACHE.clear()
        sinopac_module.QUOTE_CODE_CACHE.clear()

    def test_fresh_aggregate_cache_hit_returns_payload(self):
        cache_key = ("2330",)
        sinopac_backend.store_quote_cache(
            cache_key, {"ok": True, "quotes": {"2330": {"price": 100.0}}, "count": 1}
        )
        result = sinopac_backend.cached_quote_payload(cache_key, max_age_seconds=20)
        self.assertIsNotNone(result)
        self.assertEqual(result["quotes"]["2330"]["price"], 100.0)
        self.assertTrue(result["cached"])

    def test_expired_cache_returns_none(self):
        cache_key = ("2330",)
        sinopac_module.QUOTE_CACHE[cache_key] = {
            "storedAt": time.time() - 999,
            "fetchedAt": time.time() - 999,
            "payload": {"ok": True, "quotes": {"2330": {"price": 100.0}}, "count": 1},
        }
        sinopac_module.QUOTE_CODE_CACHE["2330"] = {
            "storedAt": time.time() - 999,
            "fetchedAt": time.time() - 999,
            "quote": {"price": 100.0},
        }
        result = sinopac_backend.cached_quote_payload(cache_key, max_age_seconds=20)
        self.assertIsNone(result)


class QuoteCacheConcurrencyTests(unittest.TestCase):
    def setUp(self):
        sinopac_module.QUOTE_CACHE.clear()
        sinopac_module.QUOTE_CODE_CACHE.clear()

    def tearDown(self):
        sinopac_module.QUOTE_CACHE.clear()
        sinopac_module.QUOTE_CODE_CACHE.clear()

    def test_concurrent_stores_settle_on_the_freshest_fetch(self):
        # 模擬 quotes() 的真實情境：thread A 較早發起查詢(fetched_at 較小)
        # 但因排隊較晚才呼叫 store_quote_cache；thread B 較晚發起查詢
        # (fetched_at 較大)卻先完成。不論實際呼叫順序為何，最終快取都應該
        # 是 fetched_at 較大的那份(thread B)。
        cache_key = ("2330",)
        barrier = threading.Barrier(2)

        def store_older_but_slower():
            barrier.wait()
            time.sleep(0.05)
            sinopac_backend.store_quote_cache(
                cache_key, {"ok": True, "quotes": {"2330": {"price": 100.0}}, "count": 1},
                fetched_at=100.0,
            )

        def store_newer_but_faster():
            barrier.wait()
            sinopac_backend.store_quote_cache(
                cache_key, {"ok": True, "quotes": {"2330": {"price": 105.0}}, "count": 1},
                fetched_at=200.0,
            )

        t1 = threading.Thread(target=store_older_but_slower)
        t2 = threading.Thread(target=store_newer_but_faster)
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        self.assertEqual(sinopac_module.QUOTE_CODE_CACHE["2330"]["quote"]["price"], 105.0)


class CapitalQuoteFallbackTests(unittest.TestCase):
    def setUp(self):
        sinopac_module.QUOTE_CACHE.clear()
        sinopac_module.QUOTE_CODE_CACHE.clear()
        sinopac_backend.reset_quote_circuit()

    def tearDown(self):
        sinopac_module.QUOTE_CACHE.clear()
        sinopac_module.QUOTE_CODE_CACHE.clear()
        sinopac_backend.reset_quote_circuit()

    @staticmethod
    def _completed(payload):
        return type("Completed", (), {"stdout": json.dumps(payload), "stderr": ""})()

    def test_partial_sinopac_quotes_are_completed_by_capital_per_symbol(self):
        shioaji = self._completed({
            "ok": True,
            "quotes": {"2330": {"currentPrice": 100}},
            "count": 1,
        })
        capital = {
            "ok": True,
            "quotes": {"2303": {"currentPrice": 50, "source": "Capital Strategy King COM"}},
            "count": 1,
        }
        with patch.object(sinopac_backend, "load_config", return_value={"apiKey": "key", "secretKey": "secret"}), \
             patch.object(sinopac_backend, "run_shioaji_child", return_value=shioaji), \
             patch.object(sinopac_module.capital_backend, "live_quotes", return_value=capital):
            result = sinopac_backend.quotes(["2330", "2303"])
        self.assertEqual(set(result["quotes"]), {"2330", "2303"})
        self.assertEqual(result["source"], "Shioaji + Capital quote")
        self.assertTrue(result["fallbackUsed"])
        self.assertEqual(result["fallbackCodes"], ["2303"])
        self.assertEqual(result["quotes"]["2330"]["source"], "Shioaji quote")
        self.assertEqual(result["quotes"]["2303"]["source"], "Capital Strategy King COM")

    def test_sinopac_timeout_prefers_fresh_capital_over_stale_cache(self):
        cache_key = ("2330",)
        sinopac_backend.store_quote_cache(
            cache_key,
            {"ok": True, "quotes": {"2330": {"currentPrice": 90}}, "count": 1},
            fetched_at=time.time() - 30,
        )
        sinopac_module.QUOTE_CACHE[cache_key]["storedAt"] = time.time() - 30
        sinopac_module.QUOTE_CODE_CACHE["2330"]["storedAt"] = time.time() - 30
        timeout = subprocess.TimeoutExpired(cmd="quotes", timeout=60, output="timeout", stderr="")
        capital = {
            "ok": True,
            "quotes": {"2330": {"currentPrice": 100, "source": "Capital Strategy King COM"}},
            "count": 1,
        }
        with patch.object(sinopac_backend, "load_config", return_value={"apiKey": "key", "secretKey": "secret"}), \
             patch.object(sinopac_backend, "run_shioaji_child", side_effect=timeout), \
             patch.object(sinopac_module.capital_backend, "live_quotes", return_value=capital):
            result = sinopac_backend.quotes(["2330"])
        self.assertEqual(result["quotes"]["2330"]["currentPrice"], 100)
        self.assertEqual(result["source"], "Capital Strategy King COM")
        self.assertFalse(result["stale"])

    def test_unverified_capital_does_not_mask_missing_sinopac_configuration(self):
        with patch.object(sinopac_backend, "load_config", return_value={}), \
             patch.object(sinopac_module.capital_backend, "live_quotes", return_value={
                 "ok": False, "quotes": {}, "error": "群益報價尚未通過實際股票驗證"
             }):
            with self.assertRaisesRegex(RuntimeError, "not configured"):
                sinopac_backend.quotes(["2330"])

    def test_three_sinopac_failures_open_circuit_and_skip_fourth_probe(self):
        failed = self._completed({"ok": False, "error": "login failed"})

        def capital_quotes(codes):
            return {
                "ok": True,
                "quotes": {
                    code: {"currentPrice": 100, "source": "Capital Strategy King COM"}
                    for code in codes
                },
                "count": len(codes),
            }

        with patch.object(sinopac_backend, "load_config", return_value={"apiKey": "key", "secretKey": "secret"}), \
             patch.object(sinopac_backend, "run_shioaji_child", return_value=failed) as shioaji_probe, \
             patch.object(sinopac_module.capital_backend, "live_quotes", side_effect=capital_quotes):
            for _ in range(3):
                result = sinopac_backend.quotes(["2330"])
                self.assertEqual(result["source"], "Capital Strategy King COM")
                sinopac_module.QUOTE_CACHE.clear()
                sinopac_module.QUOTE_CODE_CACHE.clear()
            result = sinopac_backend.quotes(["2330"])

        self.assertEqual(shioaji_probe.call_count, 3)
        self.assertTrue(result["sinopacCircuitOpen"])
        self.assertEqual(result["sinopacCircuitFailureCount"], 3)
        self.assertGreater(result["sinopacCircuitRetryAfterSeconds"], 0)


if __name__ == "__main__":
    unittest.main()
