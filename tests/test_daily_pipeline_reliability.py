"""
每日更新管線可靠性修復的回歸測試，對應 07/02 實際發生的 database is locked
(重試24次×60秒busy_timeout=27分鐘後失敗)與其連鎖問題：

  1. update_outcomes 舊版在同一條連線上逐筆 UPDATE，第一筆就取得全域寫入鎖
     並持有到整個迴圈結束(實測 5130 筆 pending 涉及 865 檔股票，每筆還重載
     一次該股全部歷史)。改為「先讀→無鎖計算→一次 executemany 短交易」，
     計算邏輯抽成純函式 compute_prediction_outcomes 直接測。
  2. update_market_data 舊版邊打 Yahoo(每來源最多35秒逾時)邊寫入，網路等待
     期間扣住全域寫入鎖。改為先完成全部抓取再開連線寫入。
  3. update_prices 舊版單一股票抓取失敗直接往外拋，一檔壞掉毀掉整個每日
     更新。改為逐股隔離，只有全部失敗才視為整體失敗。
  4. ensure_model_ready_rows 舊版補抓失敗直接往外拋，訓練迴圈對數百檔股票
     各呼叫一次，任何一檔網路失敗就毀掉整個訓練。改為補不到就用既有資料。
  5. reserve_finmind_call 舊版「讀計數→+1→寫回」無鎖，多執行緒併發時計數
     低估，額度保護被打穿。加 FINMIND_USAGE_LOCK。
  6. build_training_samples 的 build_sector_strength 改 repair=False，
     品質差的股票不再每天被重複網路補抓兩次。

全部用 mock 隔絕網路與真實資料庫寫入，不打 FinMind/Yahoo、不動正式資料。

執行方式：
  python -m unittest tests.test_daily_pipeline_reliability -v
"""
import os
import sys
import threading
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ml_backend
from ml_backend import backend


def make_rows(symbol, dates_closes):
    return [{"symbol": symbol, "date": date, "close": close} for date, close in dates_closes]


def make_prediction(pid, symbol, price_date, close, horizon=2, target_return=0.02):
    return {
        "id": pid, "symbol": symbol, "price_date": price_date,
        "close": close, "target_horizon": horizon, "target_return": target_return,
    }


class ComputePredictionOutcomesTests(unittest.TestCase):
    """update_outcomes 的純計算部分：不碰 DB，直接餵合成資料驗證結算邏輯。"""

    def test_hit_when_return_reaches_target(self):
        rows = make_rows("2330", [("2026-01-01", 100.0), ("2026-01-02", 101.0), ("2026-01-03", 103.0)])
        prediction = make_prediction(1, "2330", "2026-01-01", 100.0, horizon=2, target_return=0.02)
        updates = backend.compute_prediction_outcomes([prediction], {"2330": rows})
        self.assertEqual(len(updates), 1)
        outcome_date, outcome_close, outcome_return, hit, pid = updates[0]
        self.assertEqual(outcome_date, "2026-01-03")
        self.assertEqual(outcome_close, 103.0)
        self.assertAlmostEqual(outcome_return, 0.03)
        self.assertEqual(hit, 1)
        self.assertEqual(pid, 1)

    def test_miss_when_return_below_target(self):
        rows = make_rows("2330", [("2026-01-01", 100.0), ("2026-01-02", 100.5), ("2026-01-03", 101.0)])
        prediction = make_prediction(2, "2330", "2026-01-01", 100.0, horizon=2, target_return=0.02)
        updates = backend.compute_prediction_outcomes([prediction], {"2330": rows})
        self.assertEqual(updates[0][3], 0)

    def test_hit_when_midwindow_reaches_even_if_dayN_pulls_back(self):
        # 2026-07-05 對齊訓練標籤:窗內「任一天」收盤達標就算命中,即使第 N 天收盤已回落到
        # 目標以下。舊的「只看第 N 天定點」定義會把這種「中途達標後回落」誤判成 miss。
        rows = make_rows("2330", [("2026-01-01", 100.0), ("2026-01-02", 112.0), ("2026-01-03", 104.0)])
        prediction = make_prediction(8, "2330", "2026-01-01", 100.0, horizon=2, target_return=0.10)
        updates = backend.compute_prediction_outcomes([prediction], {"2330": rows})
        outcome_date, outcome_close, outcome_return, hit, pid = updates[0]
        self.assertEqual(hit, 1)                      # 第2天曾達 +12% → 命中(舊定點式會誤判 0)
        self.assertEqual(outcome_date, "2026-01-03")  # outcome_* 仍記第 N 天(持有到期結果)
        self.assertAlmostEqual(outcome_return, 0.04)  # 第3天回落到 +4%

    def test_short_profit_prediction_settles_with_same_multihorizon_target(self):
        closes = [100.0, 101.0, 101.5, 102.0, 102.2, 102.5, 102.7, 103.0, 103.1, 103.2, 103.5]
        rows = []
        for index, close in enumerate(closes):
            open_price = closes[index - 1] if index else close
            rows.append({
                "symbol": "2330", "date": f"2026-01-{index + 1:02d}",
                "open": open_price, "high": max(open_price, close) * 1.01,
                "low": min(open_price, close) * 0.99, "close": close,
                "volume": 1_000_000,
            })
        prediction = {
            **make_prediction(9, "2330", "2026-01-01", 100.0, horizon=10, target_return=0),
            "target_type": ml_backend.SHORT_PROFIT_TARGET_TYPE,
        }

        updates = backend.compute_prediction_outcomes([prediction], {"2330": rows})

        self.assertEqual(len(updates), 1)
        outcome_date, outcome_close, outcome_return, hit, pid = updates[0]
        self.assertEqual(outcome_date, "2026-01-11")
        self.assertGreater(outcome_return, 0)
        self.assertEqual(hit, 1)
        self.assertEqual(pid, 9)

    def test_not_yet_matured_prediction_is_skipped(self):
        rows = make_rows("2330", [("2026-01-01", 100.0), ("2026-01-02", 101.0)])
        prediction = make_prediction(3, "2330", "2026-01-01", 100.0, horizon=5)
        updates = backend.compute_prediction_outcomes([prediction], {"2330": rows})
        self.assertEqual(updates, [])

    def test_zero_close_prediction_is_skipped_instead_of_dividing_by_zero(self):
        rows = make_rows("2330", [("2026-01-01", 100.0), ("2026-01-02", 101.0), ("2026-01-03", 103.0)])
        bad = make_prediction(4, "2330", "2026-01-01", 0.0, horizon=2)
        good = make_prediction(5, "2330", "2026-01-01", 100.0, horizon=2)
        updates = backend.compute_prediction_outcomes([bad, good], {"2330": rows})
        self.assertEqual(len(updates), 1)
        self.assertEqual(updates[0][4], 5)

    def test_price_date_missing_falls_back_to_next_trading_day(self):
        # 預測日剛好是資料裡沒有的日期(停牌/資料缺口)，要退回「之後最近一個
        # 交易日」當起點，跟舊版行為一致。
        rows = make_rows("2330", [("2026-01-01", 100.0), ("2026-01-03", 101.0), ("2026-01-04", 103.0), ("2026-01-05", 104.0)])
        prediction = make_prediction(6, "2330", "2026-01-02", 100.0, horizon=2, target_return=0.02)
        updates = backend.compute_prediction_outcomes([prediction], {"2330": rows})
        self.assertEqual(len(updates), 1)
        self.assertEqual(updates[0][0], "2026-01-05")  # 起點退到01-03(index 1)，+2=01-05

    def test_symbol_without_rows_is_skipped(self):
        prediction = make_prediction(7, "9999", "2026-01-01", 100.0)
        updates = backend.compute_prediction_outcomes([prediction], {})
        self.assertEqual(updates, [])


class UpdateOutcomesLoadOncePerSymbolTests(unittest.TestCase):
    """整條 update_outcomes 流程：每檔股票只載入一次歷史、寫入用一次 executemany。"""

    def test_loads_each_symbol_once_and_writes_in_single_batch(self):
        rows = make_rows("2330", [("2026-01-01", 100.0), ("2026-01-02", 101.0), ("2026-01-03", 103.0)])
        predictions = [
            make_prediction(1, "2330", "2026-01-01", 100.0, horizon=2),
            make_prediction(2, "2330", "2026-01-01", 100.0, horizon=1),
        ]
        read_conn = MagicMock()
        read_conn.execute.return_value.fetchall.return_value = predictions
        write_conn = MagicMock()
        connect_mock = MagicMock(side_effect=[
            MagicMock(__enter__=MagicMock(return_value=read_conn), __exit__=MagicMock(return_value=False)),
            MagicMock(__enter__=MagicMock(return_value=write_conn), __exit__=MagicMock(return_value=False)),
        ])
        with patch.object(backend, "connect", connect_mock), \
             patch.object(backend, "load_price_rows", return_value=rows) as mock_load:
            updated = backend.update_outcomes()
        self.assertEqual(updated, 2)
        mock_load.assert_called_once_with("2330")  # 兩筆 prediction、同一檔 → 只載一次
        write_conn.executemany.assert_called_once()  # 一次批量寫入，不是逐筆 execute

    def test_no_pending_predictions_never_opens_write_connection(self):
        read_conn = MagicMock()
        read_conn.execute.return_value.fetchall.return_value = []
        connect_mock = MagicMock(return_value=MagicMock(
            __enter__=MagicMock(return_value=read_conn), __exit__=MagicMock(return_value=False)
        ))
        with patch.object(backend, "connect", connect_mock):
            updated = backend.update_outcomes()
        self.assertEqual(updated, 0)
        self.assertEqual(connect_mock.call_count, 1)  # 只有讀取那一次


class UpdateMarketDataFetchBeforeWriteTests(unittest.TestCase):
    """update_market_data：全部網路抓取要在開啟資料庫連線之前完成。"""

    def _run_with_event_log(self, fetch_side_effect):
        events = []

        def fake_fetch(yahoo_symbol):
            events.append(("fetch", yahoo_symbol))
            return fetch_side_effect(yahoo_symbol)

        def fake_tpex_index():
            # OTC 走 TPEx 官方 tpex_index(非 Yahoo)，也要算一次「抓取」事件、
            # 且要在開連線前發生。TPEx 官方端定期正常，這裡固定回一列成功資料。
            events.append(("fetch", "TPEX_INDEX"))
            return [{"date": "2026-01-01", "close": 100.0}]

        conn = MagicMock()

        def fake_connect():
            events.append(("connect", None))
            return MagicMock(__enter__=MagicMock(return_value=conn), __exit__=MagicMock(return_value=False))

        with patch.object(backend, "fetch_yahoo_chart_rows", side_effect=fake_fetch), \
             patch.object(backend, "fetch_tpex_index_rows", side_effect=fake_tpex_index), \
             patch.object(backend, "connect", side_effect=fake_connect), \
             patch.object(backend, "store_market_rows") as mock_store, \
             patch.object(backend, "set_meta"):
            result = backend.update_market_data()
        return events, result, mock_store

    def test_all_fetches_happen_before_db_connection_opens(self):
        events, result, _ = self._run_with_event_log(lambda symbol: [{"date": "2026-01-01", "close": 100.0}])
        connect_positions = [i for i, (kind, _) in enumerate(events) if kind == "connect"]
        fetch_positions = [i for i, (kind, _) in enumerate(events) if kind == "fetch"]
        self.assertTrue(fetch_positions, "應該至少抓了一個來源")
        self.assertTrue(connect_positions, "最後應該開一次連線寫入")
        self.assertLess(max(fetch_positions), min(connect_positions),
                        f"抓取必須全部發生在開連線之前，實際順序：{events}")

    def test_one_source_failing_does_not_block_other_sources(self):
        def flaky(symbol):
            if "TWII" in symbol or symbol == ml_backend.MARKET_SOURCES[0][1]:
                raise RuntimeError("Yahoo timeout")
            return [{"date": "2026-01-01", "close": 100.0}]

        events, result, mock_store = self._run_with_event_log(flaky)
        self.assertTrue(any("Yahoo timeout" in w for w in result["warnings"]))
        self.assertEqual(mock_store.call_count, len(ml_backend.MARKET_SOURCES) - 1)


class UpdatePricesIsolationTests(unittest.TestCase):
    """update_prices：單檔失敗要隔離，全部失敗才整體失敗。"""

    def _fake_connect(self):
        return MagicMock(return_value=MagicMock(
            __enter__=MagicMock(return_value=MagicMock()), __exit__=MagicMock(return_value=False)
        ))

    def test_single_symbol_failure_does_not_kill_the_batch(self):
        def flaky_fetch(symbol, **kwargs):
            if symbol == "1111":
                raise RuntimeError("FinMind HTTP 502")
            return [{"symbol": symbol, "date": "2026-01-01", "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1}]

        with patch.object(backend, "fetch_symbol_rows", side_effect=flaky_fetch), \
             patch.object(backend, "upsert_price_rows", return_value=1), \
             patch.object(backend, "set_meta"), \
             patch.object(backend, "connect", self._fake_connect()):
            counts = backend.update_prices(["1111", "2222"], refresh_info=False)
        self.assertEqual(counts["1111"], 0)
        self.assertEqual(counts["2222"], 1)
        self.assertIn("1111", backend._last_price_fetch_errors)
        self.assertNotIn("2222", backend._last_price_fetch_errors)

    def test_all_symbols_failing_raises_instead_of_pretending_success(self):
        with patch.object(backend, "fetch_symbol_rows", side_effect=RuntimeError("quota blocked")), \
             patch.object(backend, "set_meta"), \
             patch.object(backend, "connect", self._fake_connect()):
            with self.assertRaises(RuntimeError) as ctx:
                backend.update_prices(["1111", "2222"], refresh_info=False)
        self.assertIn("全部", str(ctx.exception))
        # 全失敗 raise 之前也要先更新錯誤紀錄，不能讓屬性殘留上一輪的過期內容
        self.assertIn("1111", backend._last_price_fetch_errors)
        self.assertIn("2222", backend._last_price_fetch_errors)

    def test_duplicate_symbols_all_failing_still_counts_as_total_failure(self):
        # len(fetch_errors)以symbol去重、symbols可能含重複代碼——
        # 全失敗判斷要用去重後的數量比對，不然重複清單會漏判。
        with patch.object(backend, "fetch_symbol_rows", side_effect=RuntimeError("quota blocked")), \
             patch.object(backend, "set_meta"), \
             patch.object(backend, "connect", self._fake_connect()):
            with self.assertRaises(RuntimeError):
                backend.update_prices(["2330", "2330"], refresh_info=False)

    def test_upsert_failure_is_also_isolated_per_symbol(self):
        # 07/02 實際炸掉的位置是 upsert(database is locked)而不是 fetch，
        # 隔離範圍必須涵蓋寫入。
        good_rows = [{"symbol": "s", "date": "2026-01-01", "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1}]

        def flaky_upsert(rows, conn=None):
            if rows and rows[0]["symbol"] == "1111":
                raise RuntimeError("database is locked")
            return len(rows)

        def fetch(symbol, **kwargs):
            return [dict(good_rows[0], symbol=symbol)]

        with patch.object(backend, "fetch_symbol_rows", side_effect=fetch), \
             patch.object(backend, "upsert_price_rows", side_effect=flaky_upsert), \
             patch.object(backend, "set_meta"), \
             patch.object(backend, "connect", self._fake_connect()):
            counts = backend.update_prices(["1111", "2222"], refresh_info=False)
        self.assertIn("1111", backend._last_price_fetch_errors)
        self.assertIn("locked", backend._last_price_fetch_errors["1111"])
        self.assertEqual(counts["2222"], 1)


class EnsureModelReadyRowsRepairFailureTests(unittest.TestCase):
    """補抓失敗要降級使用既有資料，不能往外拋毀掉整批訓練/預測。"""

    def test_repair_fetch_failure_returns_existing_rows_instead_of_raising(self):
        existing_rows = [{"symbol": "2330", "date": "2026-01-01", "close": 100.0, "price_source": "FinMind TaiwanStockPrice"}]
        bad_quality = {"ok": False, "missing": ["rows"], "rows": 1}
        with patch.object(backend, "load_price_rows", return_value=existing_rows), \
             patch.object(backend, "rows_with_verified_sources", side_effect=lambda rows: rows), \
             patch.object(backend, "model_data_quality", return_value=bad_quality), \
             patch.object(backend, "update_prices", side_effect=RuntimeError("FinMind quota blocked")):
            rows, quality = backend.ensure_model_ready_rows("2330", repair=True)
        self.assertEqual(rows, existing_rows)
        self.assertFalse(quality["ok"])


class FinmindReservationRaceTests(unittest.TestCase):
    """reserve_finmind_call 的計數在多執行緒併發下不能低估。"""

    def test_concurrent_reservations_count_exactly(self):
        store = {"hour": backend.finmind_hour_key(), "calls": 0, "blocked": False, "lastError": ""}

        def fake_read():
            # 模擬「讀檔」：故意讓出 GIL，加大無鎖情況下的交錯機率
            snapshot = dict(store)
            threading.Event().wait(0.001)
            return snapshot

        def fake_write(usage):
            store.update(usage)

        threads_count, calls_per_thread = 8, 5
        errors = []

        def worker():
            for _ in range(calls_per_thread):
                try:
                    backend.reserve_finmind_call("TestDataset", "0000")
                except Exception as exc:
                    errors.append(exc)

        with patch.object(backend, "read_finmind_usage", side_effect=fake_read), \
             patch.object(backend, "write_finmind_usage", side_effect=fake_write):
            threads = [threading.Thread(target=worker) for _ in range(threads_count)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

        self.assertEqual(errors, [])
        self.assertEqual(store["calls"], threads_count * calls_per_thread,
                         "有鎖的情況下計數必須精確等於實際呼叫次數，不能因交錯而低估")


class BuildTrainingSamplesIsolationTests(unittest.TestCase):
    """訓練樣本建構：單檔炸例外要跳過該檔繼續，sector_strength 不再重複補抓。"""

    def _fake_meta_connect(self):
        return MagicMock(return_value=MagicMock(
            __enter__=MagicMock(return_value=MagicMock()), __exit__=MagicMock(return_value=False)
        ))

    def test_one_symbol_exploding_does_not_kill_the_whole_batch(self):
        good_rows = [{"date": f"2026-01-{i:02d}", "close": 100.0} for i in range(1, 21)]
        # 大盤覆蓋要早於樣本日期，樣本才不會被(正確的)大盤覆蓋護欄攔掉，
        # 這個測試才能專注驗證「逐股例外隔離」這一件事。
        market_rows = {"TAIEX": [{"date": f"2023-06-{i:02d}", "close": 100.0} for i in range(1, 26)]}

        def flaky_ready(symbol, repair=True):
            if symbol == "1111":
                raise ZeroDivisionError("pathological rows")
            return good_rows, {"ok": True}

        with patch.object(backend, "ensure_model_ready_rows", side_effect=flaky_ready), \
             patch.object(backend, "load_market_rows", return_value=market_rows), \
             patch.object(backend, "build_sector_strength", return_value={}), \
             patch.object(backend, "build_features_for_rows", return_value=[{"index": 10, "date": "2026-01-11", "x": [0.0]}]), \
             patch.object(backend, "short_term_target", return_value={"y": 1}), \
             patch.object(backend, "set_meta"), \
             patch.object(backend, "connect", self._fake_meta_connect()):
            samples, latest = backend.build_training_samples(["1111", "2222"])
        self.assertIsNone(latest["1111"])
        self.assertEqual(len(samples), 1)
        self.assertEqual(samples[0]["symbol"], "2222")

    def test_sector_strength_is_built_without_repair(self):
        with patch.object(backend, "ensure_model_ready_rows", return_value=([], {"ok": False})), \
             patch.object(backend, "load_market_rows", return_value={}), \
             patch.object(backend, "build_sector_strength", return_value={}) as mock_sector, \
             patch.object(backend, "set_meta"), \
             patch.object(backend, "connect", self._fake_meta_connect()):
            backend.build_training_samples(["2330"])
        _, kwargs = mock_sector.call_args
        args, _ = mock_sector.call_args
        repair_value = kwargs.get("repair", args[1] if len(args) > 1 else None)
        self.assertFalse(repair_value, "sector_strength 應該用 repair=False，主迴圈才是唯一補抓點")


class TrainingMarketCoverageGuardTests(unittest.TestCase):
    """訓練樣本的大盤資料覆蓋護欄：對齊預測端的品質閘門哲學，
    大盤 20 天 lookback 不可用的日期，其樣本不能靜默帶著補 0 的大盤特徵進訓練。"""

    def _fake_meta_connect(self):
        return MagicMock(return_value=MagicMock(
            __enter__=MagicMock(return_value=MagicMock()), __exit__=MagicMock(return_value=False)
        ))

    def _run(self, taiex_dates, feature_dates):
        market_rows = {"TAIEX": [{"date": date, "close": 100.0} for date in taiex_dates]}
        good_rows = [{"date": f"2026-01-{i:02d}", "close": 100.0} for i in range(1, 21)]
        features = [{"index": 10 + i, "date": date, "x": [0.0]} for i, date in enumerate(feature_dates)]
        with patch.object(backend, "ensure_model_ready_rows", return_value=(good_rows, {"ok": True})), \
             patch.object(backend, "load_market_rows", return_value=market_rows), \
             patch.object(backend, "build_sector_strength", return_value={}), \
             patch.object(backend, "build_features_for_rows", return_value=features), \
             patch.object(backend, "short_term_target", return_value={"y": 1}), \
             patch.object(backend, "set_meta"), \
             patch.object(backend, "connect", self._fake_meta_connect()):
            samples, _ = backend.build_training_samples(["2330"])
        return samples

    def test_samples_before_market_lookback_boundary_are_skipped(self):
        # TAIEX 有 25 天資料(2023-06-01 ~ 2023-06-25)，第21筆=2023-06-21 是界線；
        # 界線前的樣本(大盤ret20只能拿到補0假值)要被跳過，界線後的保留。
        taiex_dates = [f"2023-06-{i:02d}" for i in range(1, 26)]
        samples = self._run(taiex_dates, ["2023-06-10", "2023-06-21", "2023-06-25"])
        self.assertEqual([s["date"] for s in samples], ["2023-06-21", "2023-06-25"])

    def test_no_market_data_at_all_yields_zero_samples_not_polluted_ones(self):
        # 大盤資料整張表是空的：與其默默用全 0 大盤特徵訓練出污染模型，
        # 不如一筆都不收，讓 train_model 的「樣本不足」防線大聲失敗——
        # 跟預測端 market_data_quality 直接 raise 的行為對稱。
        samples = self._run([], ["2023-06-10", "2023-06-21"])
        self.assertEqual(samples, [])

    def test_current_production_alignment_keeps_all_samples(self):
        # 模擬目前正式資料庫的狀況：大盤覆蓋早於全部特徵日期，一筆都不該攔。
        taiex_dates = [f"2023-06-{i:02d}" for i in range(1, 26)]
        samples = self._run(taiex_dates, ["2024-01-05", "2024-01-06"])
        self.assertEqual(len(samples), 2)


if __name__ == "__main__":
    unittest.main()
