"""
realtime_tick_collector.py TickCollector.on_tick() 的回歸測試，對應這次修的 bug：

shioaji TickSTKv1.volume 平常是「張」(1000股)，但盤中零股(intraday_odd=True)
這個欄位改成「股」——舊版程式碼完全沒檢查 intraday_odd，volume=50 的零股單
(50股，連1張都不到)會被誤判成 volume=50張(50,000股)的大單，汙染
large_order_flow。修法：intraday_odd 為真時排除大單門檻判斷，money_flow
不受影響(零股單本身還是真實資金流)。

不觸碰真實 Shioaji 連線/資料庫，TickCollector.__init__ 本身不連網路，
用假的 tick 物件(SimpleNamespace)直接呼叫 on_tick()。

執行方式：
  python -m unittest tests.test_realtime_tick_collector -v
"""
import concurrent.futures
import datetime as dt
import os
import sys
import time
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from realtime_tick_collector import (
    LARGE_ORDER_LOT_THRESHOLD, TAIPEI_TZ, TickCollector,
    market_session_active, select_watch_symbols, taipei_now,
)


def _fake_tick(code="2330", tick_type=1, amount=1000.0, volume=50, intraday_odd=False):
    return SimpleNamespace(
        code=code,
        tick_type=tick_type,
        amount=amount,
        volume=volume,
        intraday_odd=intraday_odd,
        datetime=dt.datetime(2026, 7, 2, 10, 0, 0),
    )


def _fake_bidask(code="2330", intraday_odd=False, simtrade=False, suspend=False):
    return SimpleNamespace(
        code=code,
        datetime=dt.datetime(2026, 7, 14, 9, 31, 10),
        bid_price=[100.0, 99.9, 99.8, 99.7, 99.6],
        bid_volume=[100, 80, 60, 40, 20],
        diff_bid_vol=[2, 1, 0, -1, 0],
        ask_price=[100.1, 100.2, 100.3, 100.4, 100.5],
        ask_volume=[50, 40, 30, 20, 10],
        diff_ask_vol=[-1, 0, 1, 0, 0],
        intraday_odd=intraday_odd,
        simtrade=simtrade,
        suspend=suspend,
    )


class OnTickIntradayOddTests(unittest.TestCase):
    def setUp(self):
        self.collector = TickCollector(api_key="x", secret_key="y", simulation=True)

    def test_odd_lot_tick_at_lot_threshold_is_not_counted_as_large_order(self):
        # volume=50 的零股單是 50 股，連 1 張都不到，絕對不該被當成
        # >=50 張(50,000股)的大單。
        self.assertGreaterEqual(50, LARGE_ORDER_LOT_THRESHOLD)
        tick = _fake_tick(volume=50, intraday_odd=True)
        self.collector.on_tick(None, tick)
        bucket = self.collector.stats_for("2330", "2026-07-02")
        self.assertEqual(bucket.large_order_flow, 0.0)
        self.assertAlmostEqual(bucket.total_volume_lots, 0.05)

    def test_regular_lot_tick_at_same_volume_is_still_counted_as_large_order(self):
        # 同樣 volume=50，但不是零股(intraday_odd=False)：這是 50 張的真大單，
        # 修法不該連正常整股單的大單判斷都一起關掉。
        tick = _fake_tick(volume=50, intraday_odd=False)
        self.collector.on_tick(None, tick)
        bucket = self.collector.stats_for("2330", "2026-07-02")
        self.assertEqual(bucket.large_order_flow, 50 * 1000)
        self.assertEqual(bucket.total_volume_lots, 50)

    def test_odd_lot_tick_still_counts_toward_money_flow(self):
        # 零股單本身還是真實成交金額，不該因為排除大單判斷就連 money_flow
        # 也漏算。
        tick = _fake_tick(tick_type=1, amount=2500.0, volume=50, intraday_odd=True)
        self.collector.on_tick(None, tick)
        bucket = self.collector.stats_for("2330", "2026-07-02")
        self.assertEqual(bucket.money_flow, 2500.0)
        self.assertEqual(bucket.tick_count, 1)
        self.assertEqual(bucket.raw_tick_count, 1)
        self.assertEqual(bucket.unknown_tick_count, 0)
        self.assertTrue(bucket.last_tick_at)

    def test_sell_side_odd_lot_does_not_pollute_large_order_flow(self):
        tick = _fake_tick(tick_type=2, volume=999, intraday_odd=True)
        self.collector.on_tick(None, tick)
        bucket = self.collector.stats_for("2330", "2026-07-02")
        self.assertEqual(bucket.large_order_flow, 0.0)

    def test_unknown_direction_tick_is_kept_as_heartbeat_but_not_flow(self):
        # tick_type=0 仍代表 WebSocket 收到一筆成交，只是不能安全推論買賣方；
        # 必須記錄心跳供資料品質判讀，但不可灌進 money_flow / 大單流向。
        tick = _fake_tick(tick_type=0, amount=5000.0, volume=100)
        self.collector.on_tick(None, tick)
        bucket = self.collector.stats_for("2330", "2026-07-02")
        self.assertEqual(bucket.raw_tick_count, 1)
        self.assertEqual(bucket.unknown_tick_count, 1)
        self.assertEqual(bucket.tick_count, 0)
        self.assertEqual(bucket.money_flow, 0.0)
        self.assertEqual(bucket.total_volume_lots, 100)
        self.assertTrue(bucket.last_tick_at)


class TaipeiTimezoneTests(unittest.TestCase):
    """對應這次修的bug：market_session_active()/wait_for_market_open()/
    main()裡的今日日期原本全部用dt.datetime.now()/dt.date.today()(系統本地
    時區)，跟server.py的taipei_localtime()/ml_backend.py的today_key()
    『業務日期一律用Asia/Taipei』原則不一致——子程序被subprocess.Popen
    啟動時完全繼承OS系統時區，沒有任何地方釘死成台北時間，一旦這台機器的
    系統時區被改掉，開盤/收盤判斷會跟server.py用台北時間算出的視窗不同步
    且沒有任何錯誤訊息。"""

    def test_taipei_now_returns_asia_taipei_tzaware_datetime(self):
        now = taipei_now()
        self.assertEqual(now.tzinfo, TAIPEI_TZ)

    def test_market_session_active_accepts_tzaware_taipei_datetime(self):
        # taipei_now()回傳tz-aware datetime，market_session_active()要能
        # 正常處理這種輸入(舊版預期的是dt.datetime.now()的naive datetime)。
        during_session = dt.datetime(2026, 7, 3, 10, 0, 0, tzinfo=TAIPEI_TZ)
        self.assertTrue(market_session_active(during_session))
        before_open = dt.datetime(2026, 7, 3, 8, 0, 0, tzinfo=TAIPEI_TZ)
        self.assertFalse(market_session_active(before_open))
        after_close = dt.datetime(2026, 7, 3, 14, 0, 0, tzinfo=TAIPEI_TZ)
        self.assertFalse(market_session_active(after_close))
        weekend = dt.datetime(2026, 7, 4, 10, 0, 0, tzinfo=TAIPEI_TZ)  # 週六
        self.assertFalse(market_session_active(weekend))


class OrderBookCollectionTests(unittest.TestCase):
    def setUp(self):
        self.collector = TickCollector(api_key="x", secret_key="y", simulation=True)

    def test_real_level5_quote_is_aggregated_into_five_minute_bucket(self):
        self.collector.on_bidask(None, _fake_bidask())
        self.collector.on_bidask(None, _fake_bidask())
        book = self.collector.order_book_for("2330", "2026-07-14", "09:30")
        self.assertEqual(book.observation_count, 2)
        self.assertEqual(book.spread_observation_count, 2)
        self.assertAlmostEqual(book.bid_depth_sum / 2, 300.0)
        self.assertAlmostEqual(book.ask_depth_sum / 2, 150.0)
        self.assertGreater(book.last_imbalance, 0)
        self.assertEqual(book.last_best_bid, 100.0)
        self.assertEqual(book.last_best_ask, 100.1)
        self.assertTrue(book.dirty)

    def test_odd_lot_trial_and_suspended_books_do_not_enter_training_data(self):
        self.collector.on_bidask(None, _fake_bidask(intraday_odd=True))
        self.collector.on_bidask(None, _fake_bidask(simtrade=True))
        self.collector.on_bidask(None, _fake_bidask(suspend=True))
        self.assertEqual(self.collector.order_books, {})


class SessionDownDetectionTests(unittest.TestCase):
    """對應這次修的bug：collector只註冊on_tick_stk_v1，完全沒訂閱Shioaji SDK
    的連線狀態回呼(on_session_down)。盤中WebSocket真的斷線時，SDK不會拋
    例外、行程也不會當機，quote_callback只是從此不再被呼叫，server.py的
    intraday_tick_worker()監督迴圈只看process.poll()判斷不出這種『假活著』。
    這裡驗證login()確實把on_session_down callback接上，觸發時會設定
    session_down旗標(main()主迴圈靠這個旗標主動結束行程，讓監督迴圈重啟)。"""

    def setUp(self):
        self.collector = TickCollector(api_key="x", secret_key="y", simulation=True)

    def test_login_registers_session_down_callback_that_sets_flag(self):
        captured = {}

        def fake_on_tick_stk_v1():
            def decorator(func):
                captured["tick_cb"] = func
                return func
            return decorator

        def fake_on_bidask_stk_v1():
            def decorator(func):
                captured["bidask_cb"] = func
                return func
            return decorator

        # 對齊真實 shioaji 1.3.3:on_session_down 簽名是 (func)->func，本身就是 decorator，
        # 不是工廠。舊 fake 寫成工廠(無參數 return decorator)剛好掩蓋了 collector 加括號的
        # 真 bug；改成直接吃 func 才能真的抓到「@self.api.on_session_down 不加括號」有沒有寫對。
        def fake_on_session_down(func):
            captured["session_down_cb"] = func
            return func

        fake_api = MagicMock()
        fake_api.login = MagicMock(return_value=[])
        fake_api.on_tick_stk_v1 = fake_on_tick_stk_v1
        fake_api.on_bidask_stk_v1 = fake_on_bidask_stk_v1
        fake_api.on_session_down = fake_on_session_down

        with patch("shioaji.Shioaji", return_value=fake_api):
            self.collector.login()

        self.assertIn("session_down_cb", captured)
        self.assertIn("bidask_cb", captured)
        self.assertFalse(self.collector.session_down.is_set())
        captured["session_down_cb"]()
        self.assertTrue(self.collector.session_down.is_set())


class LoginTimeoutTests(unittest.TestCase):
    """對應這次修的 bug：login() 沒有逾時保護，SDK 內部真的卡死(例如
    contracts_timeout 預設 0=無限等待)時子程序會永遠停在這一行，OS 層級
    process 卻依然「活著」，server.py 的監督迴圈判斷不出這種假活著、
    整天不會自動重啟。用假的 shioaji.Shioaji 模擬「login 永遠不返回」，
    確認 login() 真的會在逾時後往外拋例外，不會無限期卡住這個測試本身。"""

    def setUp(self):
        self.collector = TickCollector(api_key="x", secret_key="y", simulation=True)
        # 逾時測試不能真的等 90 秒，用很短的逾時值驗證機制本身有效。
        self.collector.LOGIN_TIMEOUT_SECONDS = 0.2

    def test_hung_login_raises_timeout_instead_of_blocking_forever(self):
        fake_api = MagicMock()
        fake_api.login = lambda **kwargs: time.sleep(10)  # 模擬永遠不返回的登入呼叫
        with patch("shioaji.Shioaji", return_value=fake_api):
            started = time.monotonic()
            with self.assertRaises(concurrent.futures.TimeoutError):
                self.collector.login()
            elapsed = time.monotonic() - started
        # 應該在逾時值附近就返回，不是卡到 fake_api.login 的 10 秒 sleep 結束。
        self.assertLess(elapsed, 5)

    def test_fast_login_succeeds_normally(self):
        fake_api = MagicMock()
        fake_api.login = MagicMock(return_value=[])
        with patch("shioaji.Shioaji", return_value=fake_api):
            self.collector.login()  # 不該拋例外
        fake_api.login.assert_called_once()


class TickQuoteTypeResolutionTests(unittest.TestCase):
    """對應盤中日誌抓到的第三個 bug:shioaji 1.3.3 把 QuoteType 從頂層 sj.QuoteType 搬到
    shioaji.constant.QuoteType,舊碼 sj.QuoteType.Tick 拋「module 'shioaji' has no attribute
    'QuoteType'」→200 檔全訂閱失敗、0 ticks。_tick_quote_type() 要能在真實 1.3.3 解出 Tick
    型別(用 getattr 相容新舊版)。這條測試直接對真 SDK 驗證,避免再退回錯的存取位置。"""

    def test_resolves_tick_quote_type_on_real_sdk(self):
        collector = TickCollector(api_key="x", secret_key="y", simulation=True)
        tick_type = collector._tick_quote_type()  # 解不出來(位置錯)會直接拋,測試就紅
        # QuoteType 是 str enum,值為 'tick'
        self.assertEqual(getattr(tick_type, "value", str(tick_type)), "tick")

    def test_resolves_bidask_quote_type_on_real_sdk(self):
        collector = TickCollector(api_key="x", secret_key="y", simulation=True)
        bidask_type = collector._bidask_quote_type()
        self.assertEqual(getattr(bidask_type, "value", str(bidask_type)), "bidask")


class AsyncSubscriptionEventTests(unittest.TestCase):
    def test_rejected_async_bidask_event_is_not_counted_as_success(self):
        collector = TickCollector(api_key="x", secret_key="y", simulation=True)
        collector.on_quote_event(
            400,
            16,
            "QUO/v1/STK/*/TSE/2330",
            "Max Num Subscriptions Exceeded",
        )
        self.assertEqual(collector.failed_subscription_symbols(0, "QUO"), {"2330"})
        self.assertEqual(collector.failed_subscription_symbols(0, "TIC"), set())

    def test_successful_subscription_event_is_not_reported_as_failure(self):
        collector = TickCollector(api_key="x", secret_key="y", simulation=True)
        collector.on_quote_event(
            200,
            16,
            "QUO/v1/STK/*/TSE/2330",
            "Subscribe or Unsubscribe ok",
        )
        self.assertEqual(collector.failed_subscription_symbols(0, "QUO"), set())


class DynamicWatchPoolTests(unittest.TestCase):
    def test_priority_is_holdings_then_hot_then_radar_then_liquid(self):
        with patch(
            "realtime_tick_collector.sinopac_backend.holdings",
            return_value={"holdings": [{"code": "9999"}]},
        ), patch(
            "realtime_tick_collector.backend.intraday_hot_symbols",
            return_value=["8888"],
        ), patch(
            "realtime_tick_collector.backend.list_monster_scores",
            return_value={"candidates": [{"symbol": "7777"}]},
        ), patch(
            "realtime_tick_collector.backend.liquid_monster_universe",
            return_value=["6666"],
        ):
            symbols, info = select_watch_symbols(limit=3)

        self.assertEqual(symbols, ["9999", "8888", "7777"])
        self.assertEqual(info["holdingSymbols"], ["9999"])
        self.assertEqual(info["hotSymbols"], ["8888"])
        self.assertEqual(info["radarSubscribed"], 1)

    def test_refresh_subscriptions_applies_tick_and_orderbook_delta(self):
        collector = TickCollector(api_key="x", secret_key="y", simulation=True)
        collector.symbols = ["1111", "2222"]
        collector.orderbook_symbols = ["1111"]
        quote_api = MagicMock()
        with patch.object(collector, "_quote_api", return_value=quote_api), \
                patch.object(collector, "_tick_quote_type", return_value="tick"), \
                patch.object(collector, "_bidask_quote_type", return_value="bidask"), \
                patch.object(collector, "find_contract", side_effect=lambda symbol: symbol), \
                patch.object(collector, "failed_subscription_symbols", return_value=set()), \
                patch("realtime_tick_collector.time.sleep"):
            result = collector.refresh_subscriptions(["1111", "3333"])

        quote_api.unsubscribe.assert_any_call("2222", quote_type="tick")
        quote_api.subscribe.assert_any_call("3333", quote_type="tick")
        quote_api.subscribe.assert_any_call("3333", quote_type="bidask")
        self.assertEqual(collector.symbols, ["1111", "3333"])
        self.assertEqual(collector.orderbook_symbols, ["1111", "3333"])
        self.assertEqual(result["tickAdded"], ["3333"])
        self.assertEqual(result["tickRemoved"], ["2222"])

    def test_scanner_cycle_writes_latest_staging_rows(self):
        collector = TickCollector(api_key="x", secret_key="y", simulation=True)
        collector.api = object()
        payload = {
            "ok": True,
            "count": 1,
            "rows": [{"symbol": "2330", "close": 100}],
            "rankCounts": {"change_percent": 1},
            "errors": [],
            "scanAt": "2026-07-14T10:00:00+08:00",
        }
        with patch(
            "realtime_tick_collector.sinopac_backend.stock_scanners",
            return_value=payload,
        ), patch(
            "realtime_tick_collector.backend.upsert_intraday_scanner_rows",
            return_value={"ok": True, "saved": 1},
        ) as upsert:
            result = collector.scan_market_rankings()

        self.assertEqual(result["saved"], 1)
        upsert.assert_called_once()

    def test_rotation_uses_all_market_batch_and_capital_only_for_missing_quote(self):
        collector = TickCollector(api_key="x", secret_key="y", simulation=True)
        collector.api = object()
        collector.rotation_symbols = ["1111", "2222"]
        sinopac_quotes = {
            "1111": {
                "currentPrice": 101.0,
                "snapshotAt": "2026-07-14T10:00:00+08:00",
            },
        }
        capital_quote = {
            "currentPrice": 52.0,
            "snapshotAt": "2026-07-14T10:00:01+08:00",
        }
        with patch(
            "realtime_tick_collector.sinopac_backend.stock_snapshots",
            return_value=(sinopac_quotes, None),
        ), patch(
            "sinopac_backend.capital_backend.live_quotes",
            return_value={"ok": True, "quotes": {"2222": capital_quote}},
        ) as capital_quotes, patch(
            "realtime_tick_collector.backend.intraday_hot_symbols",
            return_value=[],
        ), patch(
            "realtime_tick_collector.backend.upsert_intraday_rotation_quotes",
            return_value={"ok": True, "saved": 2},
        ) as upsert:
            result = collector.scan_rotation_batch()

        capital_quotes.assert_called_once_with(["2222"])
        kwargs = upsert.call_args.kwargs
        self.assertEqual(kwargs["rotation_symbols"], ["1111", "2222"])
        self.assertEqual(kwargs["requested_symbols"], ["1111", "2222"])
        self.assertEqual(kwargs["fallback_codes"], ["2222"])
        self.assertEqual(kwargs["missing_symbols"], [])
        self.assertEqual(result["fallbackCodes"], ["2222"])


if __name__ == "__main__":
    unittest.main()
