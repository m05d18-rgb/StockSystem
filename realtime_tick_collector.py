"""
永豐 Shioaji 即時 tick 訂閱收集器(獨立常駐子程序)。

用途
----
由 server.py 在台股開盤前用 subprocess.Popen 啟動、收盤後 terminate。全程
維持一個 Shioaji 登入連線，訂閱觀察名單(持股優先，上限 MAX_SUBSCRIBE_SYMBOLS
檔)的即時 Tick 資料，用官方 streaming tick_type 語意累積當日訊號量，定期寫進
realtime_flow_staging 暫存表，讓 ml_backend.py 的 merge_intraday_confirmation
讀出來 merge 回 prices 主表的 realtime_money_flow / realtime_large_order_flow。

為什麼跟 prices 主表分開存
--------------------------
每日批次 update_prices() 對 prices 表是 INSERT OR REPLACE，如果即時資金流直接
寫進 prices，批次再跑一次就會把當天已經收集到的真實資料覆蓋成 NULL。所以這裡
只寫進獨立的 realtime_flow_staging，由 ml_backend.py 在組 rows 的時候才 merge
進去，不受批次重跑影響。

tick_type 買賣方向語意(務必核對官方文件，不要相信網路二手轉述，曾經查到
互相矛盾的說法)
-----------------------------------------------------------------------
官方文件 https://sinotrade.github.io/tutor/market_data/streaming/stocks/
（streaming TickSTKv1，本程式使用的即時推播 API，跟歷史 ticks() REST API的
tick_type 定義不同，不要混用）明確寫："tick_type (int) Tick type
{1: ask, 2: bid, 0: unknown}"。
即 tick_type==1 表示這筆成交發生在賣方掛出的「賣價(ask)」上，也就是買方
主動進場吃掉賣單成交、屬於買盤主動成交(外盤)；tick_type==2 表示成交發生在
買方掛出的「買價(bid)」上，即賣方主動殺出成交、屬於賣盤主動成交(內盤)；
tick_type==0 是無法判斷方向，直接排除，不計入訊號量(不能用 0 當作"中性"
偷偷頂著，那不是真的中性，是看不出方向)。
"""
from pathlib import Path
from zoneinfo import ZoneInfo
import copy
import concurrent.futures
import datetime as dt
import math
import sqlite3
import sys
import threading
import time
import traceback

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from ml_backend import DEFAULT_SYMBOLS, backend, now_text  # noqa: E402
from sinopac_backend import sinopac_backend  # noqa: E402

LOG_DIR = ROOT / "realtime_tick_logs"
# 開盤/收盤時段判斷跟tick落日期一律要用台北時間，不能用系統本地時區——
# server.py 的 taipei_localtime() 已經是這個原則，這支子程序被 subprocess.Popen
# 啟動時完全繼承OS系統時區、沒有任何env強制釘死時區，如果哪天這台機器的
# 系統時區被改掉(VM重建/雲端遷移/時區誤設)，這裡的開盤/收盤判斷會跟
# server.py用台北時間算出的spawn/keep-alive視窗不同步，而且不會有任何
# 錯誤訊息。
TAIPEI_TZ = ZoneInfo("Asia/Taipei")


def taipei_now():
    return dt.datetime.now(TAIPEI_TZ)
MAX_SUBSCRIBE_SYMBOLS = 200  # 永豐 Shioaji 每帳號可訂閱檔數上限(已知約 200)
# 妖股雷達前端一次最多顯示 100 檔。這些檔案必須優先留在 tick 訂閱池，否則
# 畫面上雖顯示「即時主力動向」，實際上後段候選永遠只會顯示無 tick 資料。
MAX_RADAR_TICK_SYMBOLS = 100
# The live account rejects 200 Tick + 100 BidAsk topics with
# "Max Num Subscriptions Exceeded".  Keep the full 200-symbol Tick coverage
# used by the radar and reserve a conservative 40 topics for real level-five
# data (holdings and radar candidates are already first in the symbol order).
MAX_ORDERBOOK_SYMBOLS = 40
SCANNER_INTERVAL_SECONDS = 60
HOT_POOL_REFRESH_SECONDS = 60
MAX_HOT_SYMBOLS = 60
ROTATION_INTERVAL_SECONDS = 20
ROTATION_BATCH_SIZE = 400
LARGE_ORDER_LOT_THRESHOLD = 50  # 單筆成交 >=50張(=50,000股)視為大單
# server.py 用 subprocess.Popen.terminate()/PowerShell Stop-Process -Force
# 結束這個子程序時，Windows 底層是 TerminateProcess——這是 OS 層級的強制
# 終止，不會觸發 Python 的 finally/例外/signal handler，main() 收盤後的
# 「最後 flush」完全不會執行。這在 Windows 上沒有真正安全的解法(subprocess
# 沒有 SIGTERM 語意可攔截，要靠 CTRL_BREAK_EVENT+process group 才能做到
# 優雅關閉，但那條路徑需要真實開盤時的 Shioaji 連線才能驗證，這個會話
# 無法安全測試)。折衷做法：把定期 flush 週期從 60 秒縮到 20 秒，把「被強制
# 終止時最多遺失多少尾盤 tick」的曝險window縮小到原本的 1/3，同時
# realtime_flow_staging 是獨立小表、跟已知的 database is locked 問題(發生
# 在 prices 主表)不同表，20 秒一次的寫入頻率不會顯著增加鎖競爭風險。
FLUSH_INTERVAL_SECONDS = 20
# realtime_flow_staging 用 PRIMARY KEY (symbol, date)，INSERT OR REPLACE 只會
# 覆蓋「今天」那一列，歷史日期的列完全沒有任何讀取路徑會用到(唯一消費端
# server.py 的合併查詢固定 WHERE date=今天)，卻永久留在表裡沒有清理機制。
# collector 本來就是每天開盤前才啟動一次的獨立程序，在這裡順手清理最省事，
# 不用改 server.py 的常駐邏輯。保留天數留寬鬆一點方便事後追查，不用對齊
# prices 主表的長保留。
REALTIME_FLOW_RETENTION_DAYS = 14
# The five-minute research bars need a materially longer history than the
# short-lived realtime merge table. The intraday model remains disabled until
# at least 60 real sessions have accumulated.
INTRADAY_BAR_RETENTION_DAYS = 400
SOURCE_TAG = "SinoPac Shioaji 即時tick訂閱(streaming TickSTKv1, on_tick_stk_v1)"
ORDER_BOOK_SOURCE_TAG = "SinoPac Shioaji 即時五檔(streaming BidAskSTKv1, on_bidask_stk_v1)"
MARKET_START_MINUTES = 9 * 60 - 5  # 08:55，提早一點開始訂閱熱機
MARKET_END_MINUTES = 13 * 60 + 35  # 13:35，收盤後留一點緩衝把尾盤成交收完


def log(message):
    line = f"[{now_text()}] {message}"
    print(line, flush=True)
    try:
        LOG_DIR.mkdir(exist_ok=True)
        with (LOG_DIR / "collector.log").open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except Exception:
        pass


def normalize_codes(values):
    output = []
    seen = set()
    for value in values or []:
        # 不要 [:4] 截斷:6 碼股票/ETF(如 00631L 取數字=00631)會被截成 0063 這種不同的
        # 合法代碼,把 A 的即時 tick 資金流寫到 B 的 key 下污染特徵。改成只收「恰好 4 位數字」,
        # 長代碼直接跳過(寧可漏收也不污染)。
        code = "".join(ch for ch in str(value or "") if ch.isdigit())
        if len(code) == 4 and code not in seen:
            seen.add(code)
            output.append(code)
    return output


def market_session_active(now=None):
    now = now or taipei_now()
    if now.weekday() > 4:
        return False
    minutes = now.hour * 60 + now.minute
    return MARKET_START_MINUTES <= minutes <= MARKET_END_MINUTES


def wait_for_market_open(poll_seconds=5, max_wait_seconds=3600):
    # server.py 可能提早幾分鐘就把這個子程序 spawn 起來(先登入、訂閱熱機)，
    # 這時 market_session_active() 還是 False。這裡耐心等到開盤，而不是
    # 直接退出，否則子程序會在還沒開始收集任何資料前就結束，server.py
    # 那邊的監督迴圈會誤判成「當機」而不斷重啟、重複登入。
    waited = 0
    while waited < max_wait_seconds:
        now = taipei_now()
        if now.weekday() > 4:
            log("今天不是交易日，不等待開盤")
            return False
        minutes = now.hour * 60 + now.minute
        if minutes > MARKET_END_MINUTES:
            log("已經超過收盤時間，不等待開盤")
            return False
        if minutes >= MARKET_START_MINUTES:
            return True
        time.sleep(poll_seconds)
        waited += poll_seconds
    log("等待開盤超過時限，放棄本次收集")
    return False


def select_watch_symbols(limit=MAX_SUBSCRIBE_SYMBOLS, holdings_override=None):
    # 優先序：使用者實際持股(全部保留) > 盤中發現熱門池 > 當前妖股雷達
    # 名單 > 高流動性觀察清單。熱門池每三分鐘重算，讓剛開始發動的股票能
    # 取得逐筆 tick，不必等到隔日雷達重跑。
    # 之前第二順位直接放 liquid_monster_universe，雷達名單不在前 200 名時會
    # 被擠出訂閱池，讓雷達的「即時主力」欄位名義上存在、實際永遠沒有資料。
    # list_monster_scores 是本地 DB 純讀取，collector 開機時讀一次不會額外打網路。
    limit = max(1, min(int(limit or MAX_SUBSCRIBE_SYMBOLS), MAX_SUBSCRIBE_SYMBOLS))
    holdings = []
    radar = []
    errors = []
    if holdings_override is not None:
        holdings = normalize_codes(holdings_override)
    else:
        try:
            payload = sinopac_backend.holdings()
            holding_rows = payload.get("holdings") or []
            holdings = normalize_codes([item.get("code") for item in holding_rows] or payload.get("codes") or [])
        except Exception as exc:
            errors.append(f"holdings: {exc}")

    hot = []
    try:
        hot = normalize_codes(backend.intraday_hot_symbols(limit=MAX_HOT_SYMBOLS))
    except Exception as exc:
        errors.append(f"intraday_hot_symbols: {exc}")

    try:
        radar_payload = backend.list_monster_scores(MAX_RADAR_TICK_SYMBOLS)
        radar = normalize_codes(item.get("symbol") for item in (radar_payload.get("candidates") or []))
    except Exception as exc:
        errors.append(f"monster_radar: {exc}")

    liquid = []
    try:
        liquid = normalize_codes(backend.liquid_monster_universe(limit))
    except Exception as exc:
        errors.append(f"liquid_monster_universe: {exc}")

    merged = []
    seen = set()
    for code in holdings + hot + radar + liquid:
        if code not in seen:
            seen.add(code)
            merged.append(code)
    if not merged:
        merged = normalize_codes(DEFAULT_SYMBOLS)
    selected = merged[:limit]
    selected_set = set(selected)
    radar_missing = [code for code in radar if code not in selected_set]
    return selected, {
        "holdings": len(holdings),
        "holdingSymbols": holdings,
        "hot": len(hot),
        "hotSymbols": hot,
        "radar": len(radar),
        "radarSubscribed": len(radar) - len(radar_missing),
        "radarMissing": radar_missing,
        "liquid": len(liquid),
        "errors": errors,
    }


class SymbolStats:
    __slots__ = (
        "money_flow", "large_order_flow", "tick_count",
        "raw_tick_count", "unknown_tick_count", "total_volume_lots", "last_tick_at",
        "minute_bars",
    )

    def __init__(self):
        self.money_flow = 0.0
        self.large_order_flow = 0.0
        self.tick_count = 0
        # raw_tick_count 是 WebSocket 實際送達的成交筆數；tick_count 只計入
        # 能判斷買/賣方向的 1/2。兩者拆開才看得出「沒有行情」與「有成交但
        # 方向未知」的差別，且未知方向永遠不會污染資金流判斷。
        self.raw_tick_count = 0
        self.unknown_tick_count = 0
        self.total_volume_lots = 0.0
        self.last_tick_at = ""
        self.minute_bars = {}


class MinuteStats:
    __slots__ = (
        "open", "high", "low", "close", "volume_lots",
        "active_buy_volume_lots", "active_sell_volume_lots", "unknown_volume_lots",
        "active_buy_amount", "active_sell_amount", "unknown_amount",
        "large_buy_volume_lots", "large_sell_volume_lots",
        "raw_tick_count", "directional_tick_count", "unknown_tick_count",
        "first_tick_at", "last_tick_at", "dirty",
    )

    def __init__(self):
        self.open = 0.0
        self.high = 0.0
        self.low = 0.0
        self.close = 0.0
        self.volume_lots = 0.0
        self.active_buy_volume_lots = 0.0
        self.active_sell_volume_lots = 0.0
        self.unknown_volume_lots = 0.0
        self.active_buy_amount = 0.0
        self.active_sell_amount = 0.0
        self.unknown_amount = 0.0
        self.large_buy_volume_lots = 0.0
        self.large_sell_volume_lots = 0.0
        self.raw_tick_count = 0
        self.directional_tick_count = 0
        self.unknown_tick_count = 0
        self.first_tick_at = ""
        self.last_tick_at = ""
        self.dirty = False

    def apply_tick(self, price, volume_lots, amount, tick_type, whole_lot, received_at):
        if price > 0:
            if self.open <= 0:
                self.open = price
                self.high = price
                self.low = price
            self.high = max(self.high, price)
            self.low = min(self.low, price)
            self.close = price
        self.volume_lots += max(0.0, volume_lots)
        self.raw_tick_count += 1
        self.first_tick_at = self.first_tick_at or received_at
        self.last_tick_at = received_at
        self.dirty = True
        if tick_type == 1:
            self.directional_tick_count += 1
            self.active_buy_volume_lots += max(0.0, volume_lots)
            self.active_buy_amount += max(0.0, amount)
            if whole_lot and volume_lots >= LARGE_ORDER_LOT_THRESHOLD:
                self.large_buy_volume_lots += volume_lots
        elif tick_type == 2:
            self.directional_tick_count += 1
            self.active_sell_volume_lots += max(0.0, volume_lots)
            self.active_sell_amount += max(0.0, amount)
            if whole_lot and volume_lots >= LARGE_ORDER_LOT_THRESHOLD:
                self.large_sell_volume_lots += volume_lots
        else:
            self.unknown_tick_count += 1
            self.unknown_volume_lots += max(0.0, volume_lots)
            self.unknown_amount += max(0.0, amount)


class OrderBook5mStats:
    __slots__ = (
        "observation_count", "spread_observation_count",
        "bid_depth_sum", "ask_depth_sum", "imbalance_sum",
        "min_imbalance", "max_imbalance", "last_imbalance",
        "spread_sum", "max_spread_pct", "last_spread_pct",
        "microprice_gap_sum", "last_microprice_gap_pct",
        "last_best_bid", "last_best_ask",
        "net_bid_volume_change_lots", "net_ask_volume_change_lots",
        "first_snapshot_at", "last_snapshot_at", "dirty",
    )

    def __init__(self):
        self.observation_count = 0
        self.spread_observation_count = 0
        self.bid_depth_sum = 0.0
        self.ask_depth_sum = 0.0
        self.imbalance_sum = 0.0
        self.min_imbalance = 0.0
        self.max_imbalance = 0.0
        self.last_imbalance = 0.0
        self.spread_sum = 0.0
        self.max_spread_pct = 0.0
        self.last_spread_pct = None
        self.microprice_gap_sum = 0.0
        self.last_microprice_gap_pct = None
        self.last_best_bid = None
        self.last_best_ask = None
        self.net_bid_volume_change_lots = 0.0
        self.net_ask_volume_change_lots = 0.0
        self.first_snapshot_at = ""
        self.last_snapshot_at = ""
        self.dirty = False

    def apply(self, bid_prices, bid_volumes, diff_bid_volumes,
              ask_prices, ask_volumes, diff_ask_volumes, received_at):
        bid_prices = [float(value or 0) for value in (bid_prices or [])]
        ask_prices = [float(value or 0) for value in (ask_prices or [])]
        bid_volumes = [max(0.0, float(value or 0)) for value in (bid_volumes or [])]
        ask_volumes = [max(0.0, float(value or 0)) for value in (ask_volumes or [])]
        bid_depth = sum(bid_volumes[:5])
        ask_depth = sum(ask_volumes[:5])
        total_depth = bid_depth + ask_depth
        if total_depth <= 0:
            return False

        imbalance = (bid_depth - ask_depth) / total_depth
        best_bid = next((value for value in bid_prices[:5] if value > 0), None)
        best_ask = next((value for value in ask_prices[:5] if value > 0), None)
        self.observation_count += 1
        self.bid_depth_sum += bid_depth
        self.ask_depth_sum += ask_depth
        self.imbalance_sum += imbalance
        if self.observation_count == 1:
            self.min_imbalance = imbalance
            self.max_imbalance = imbalance
        else:
            self.min_imbalance = min(self.min_imbalance, imbalance)
            self.max_imbalance = max(self.max_imbalance, imbalance)
        self.last_imbalance = imbalance
        self.last_best_bid = best_bid
        self.last_best_ask = best_ask
        self.net_bid_volume_change_lots += sum(float(value or 0) for value in (diff_bid_volumes or [])[:5])
        self.net_ask_volume_change_lots += sum(float(value or 0) for value in (diff_ask_volumes or [])[:5])

        if best_bid and best_ask and best_ask >= best_bid:
            mid = (best_bid + best_ask) / 2
            spread_pct = ((best_ask - best_bid) / mid * 100) if mid > 0 else 0.0
            microprice = ((best_ask * bid_depth) + (best_bid * ask_depth)) / total_depth
            microprice_gap_pct = ((microprice - mid) / mid * 100) if mid > 0 else 0.0
            self.spread_observation_count += 1
            self.spread_sum += spread_pct
            self.max_spread_pct = max(self.max_spread_pct, spread_pct)
            self.last_spread_pct = spread_pct
            self.microprice_gap_sum += microprice_gap_pct
            self.last_microprice_gap_pct = microprice_gap_pct

        self.first_snapshot_at = self.first_snapshot_at or received_at
        self.last_snapshot_at = received_at
        self.dirty = True
        return True


def tick_session_parts(raw_datetime):
    """Return (date, five-minute bucket) without using the host timezone."""
    value = raw_datetime
    if isinstance(value, dt.datetime):
        if value.tzinfo is not None:
            value = value.astimezone(TAIPEI_TZ)
        date_text = value.date().isoformat()
        return date_text, f"{value.hour:02d}:{(value.minute // 5) * 5:02d}"
    text = str(value or "")
    date_text = text[:10] if len(text) >= 10 else taipei_now().date().isoformat()
    try:
        time_text = text.split("T", 1)[1] if "T" in text else text.split(" ", 1)[1]
        hour, minute = (int(part) for part in time_text[:5].split(":"))
        return date_text, f"{hour:02d}:{(minute // 5) * 5:02d}"
    except (IndexError, TypeError, ValueError):
        current = taipei_now()
        return date_text, f"{current.hour:02d}:{(current.minute // 5) * 5:02d}"


class TickCollector:
    def __init__(self, api_key, secret_key, simulation):
        self.api_key = api_key
        self.secret_key = secret_key
        self.simulation = simulation
        self.api = None
        self.lock = threading.Lock()
        self.stats = {}  # {(symbol, date_text): SymbolStats}
        self.order_books = {}  # {(symbol, date_text, five_minute): OrderBook5mStats}
        self.symbols = []
        self.orderbook_symbols = []
        self.last_orderbook_written = 0
        self.subscription_events = []
        self.subscription_events_lock = threading.Lock()
        self.rotation_symbols = []
        self.rotation_cursor = 0
        self.rotation_round_sequence = 0
        self.rotation_round_id = ""
        self.rotation_retry_symbols = []
        # login()/subscribe() 成功後只註冊了 on_tick_stk_v1，完全沒訂閱
        # Shioaji SDK 的連線狀態回呼——盤中 WebSocket 若因網路波動/券商
        # 伺服器重啟斷線，SDK 不會拋例外、行程也不會當機，quote_callback
        # 只是從此不再被呼叫，tick_count 從斷線那刻起凍結，server.py 的
        # intraday_tick_worker() 監督迴圈只看 process.poll() 判斷不出這種
        # 「假活著」，整個盤中都不會觸發重啟。用 on_session_down 偵測到
        # 斷線就設這個旗標，main() 主迴圈看到後主動結束行程，讓 process.poll()
        # 變成非 None，借用既有的監督迴圈重啟機制(不需要另外設計IPC通知)。
        self.session_down = threading.Event()

    def stats_for(self, symbol, date_text):
        key = (symbol, date_text)
        bucket = self.stats.get(key)
        if bucket is None:
            bucket = SymbolStats()
            self.stats[key] = bucket
        return bucket

    def order_book_for(self, symbol, date_text, minute_key):
        key = (symbol, date_text, minute_key)
        bucket = self.order_books.get(key)
        if bucket is None:
            bucket = OrderBook5mStats()
            self.order_books[key] = bucket
        return bucket

    def bootstrap_today(self, symbols, date_text):
        # 如果子程序是重啟(例如盤中斷線重連)，先把 realtime_flow_staging 裡
        # 今天已經有的累積值讀回來當起點，不要從 0 重新累積，否則下一次
        # flush 的 INSERT OR REPLACE 會把重啟前已經收集到的真實資料蓋掉成
        # 比較小的數字，等於默默丟掉一部分真實資料。
        if not symbols:
            return
        try:
            with backend.connect() as conn:
                conn.row_factory = sqlite3.Row
                placeholders = ",".join("?" for _ in symbols)
                rows = conn.execute(
                    f"SELECT * FROM realtime_flow_staging WHERE date = ? AND symbol IN ({placeholders})",
                    [date_text, *symbols],
                ).fetchall()
                try:
                    minute_rows = conn.execute(
                        f"SELECT * FROM intraday_minute_bars WHERE date = ? AND symbol IN ({placeholders})",
                        [date_text, *symbols],
                    ).fetchall()
                except sqlite3.OperationalError:
                    minute_rows = []
                try:
                    order_book_rows = conn.execute(
                        f"SELECT * FROM order_book_5m_features WHERE date = ? AND symbol IN ({placeholders})",
                        [date_text, *symbols],
                    ).fetchall()
                except sqlite3.OperationalError:
                    order_book_rows = []
            with self.lock:
                for row in rows:
                    bucket = self.stats_for(row["symbol"], row["date"])
                    bucket.money_flow = row["realtime_money_flow"] or 0.0
                    bucket.large_order_flow = row["realtime_large_order_flow"] or 0.0
                    bucket.tick_count = row["tick_count"] or 0
                    row_keys = set(row.keys())
                    bucket.raw_tick_count = row["raw_tick_count"] if "raw_tick_count" in row_keys else bucket.tick_count
                    bucket.unknown_tick_count = row["unknown_tick_count"] if "unknown_tick_count" in row_keys else 0
                    bucket.total_volume_lots = row["total_volume_lots"] if "total_volume_lots" in row_keys else 0.0
                    bucket.last_tick_at = row["last_tick_at"] if "last_tick_at" in row_keys else ""
                for row in minute_rows:
                    bucket = self.stats_for(row["symbol"], row["date"])
                    minute = MinuteStats()
                    for field in (
                        "open", "high", "low", "close", "volume_lots",
                        "active_buy_volume_lots", "active_sell_volume_lots", "unknown_volume_lots",
                        "active_buy_amount", "active_sell_amount", "unknown_amount",
                        "large_buy_volume_lots", "large_sell_volume_lots",
                        "raw_tick_count", "directional_tick_count", "unknown_tick_count",
                        "first_tick_at", "last_tick_at",
                    ):
                        setattr(minute, field, row[field] or ("" if field.endswith("_at") else 0))
                    bucket.minute_bars[row["minute"]] = minute
                for row in order_book_rows:
                    book = self.order_book_for(row["symbol"], row["date"], row["minute"])
                    count = int(row["observation_count"] or 0)
                    spread_count = int(row["spread_observation_count"] or 0)
                    book.observation_count = count
                    book.spread_observation_count = spread_count
                    book.bid_depth_sum = float(row["avg_bid_depth_lots"] or 0) * count
                    book.ask_depth_sum = float(row["avg_ask_depth_lots"] or 0) * count
                    book.imbalance_sum = float(row["avg_imbalance"] or 0) * count
                    book.min_imbalance = float(row["min_imbalance"] or 0)
                    book.max_imbalance = float(row["max_imbalance"] or 0)
                    book.last_imbalance = float(row["last_imbalance"] or 0)
                    book.spread_sum = float(row["avg_spread_pct"] or 0) * spread_count
                    book.max_spread_pct = float(row["max_spread_pct"] or 0)
                    book.last_spread_pct = row["last_spread_pct"]
                    book.microprice_gap_sum = float(row["avg_microprice_gap_pct"] or 0) * spread_count
                    book.last_microprice_gap_pct = row["last_microprice_gap_pct"]
                    book.last_best_bid = row["last_best_bid"]
                    book.last_best_ask = row["last_best_ask"]
                    book.net_bid_volume_change_lots = float(row["net_bid_volume_change_lots"] or 0)
                    book.net_ask_volume_change_lots = float(row["net_ask_volume_change_lots"] or 0)
                    book.first_snapshot_at = row["first_snapshot_at"] or ""
                    book.last_snapshot_at = row["last_snapshot_at"] or ""
                    book.dirty = False
            if rows or minute_rows or order_book_rows:
                log(
                    f"還原今日累積資料：日彙總 {len(rows)} 檔，5分K {len(minute_rows)} 列，"
                    f"五檔特徵 {len(order_book_rows)} 列"
                )
        except Exception:
            log(f"bootstrap_today 失敗：{traceback.format_exc()}")

    def on_tick(self, exchange, tick):
        try:
            symbol = "".join(ch for ch in str(getattr(tick, "code", "") or "") if ch.isdigit())
            if len(symbol) != 4:
                return
            raw_datetime = getattr(tick, "datetime", None)
            date_text, minute_key = tick_session_parts(raw_datetime)
            intraday_odd = bool(getattr(tick, "intraday_odd", False))
            raw_volume = float(getattr(tick, "volume", 0) or 0)
            normalized_volume_lots = raw_volume / 1000 if intraday_odd else raw_volume
            tick_type = int(getattr(tick, "tick_type", 0) or 0)
            amount = float(getattr(tick, "amount", 0) or 0)
            price = float(
                getattr(tick, "close", 0)
                or getattr(tick, "price", 0)
                or 0
            )
            received_at = now_text()
            # `last_tick_at` 用收件端時間，這是偵測 WebSocket 是否仍有資料流入的
            # 心跳，不能用券商原始時間戳(可能延遲或格式不一)來冒充新鮮度。
            with self.lock:
                bucket = self.stats_for(symbol, date_text)
                bucket.raw_tick_count += 1
                bucket.total_volume_lots += max(0.0, normalized_volume_lots)
                bucket.last_tick_at = received_at
                minute = bucket.minute_bars.get(minute_key)
                if minute is None:
                    minute = MinuteStats()
                    bucket.minute_bars[minute_key] = minute
                minute.apply_tick(
                    price,
                    normalized_volume_lots,
                    amount,
                    tick_type,
                    not intraday_odd,
                    received_at,
                )
            if tick_type not in (1, 2):
                with self.lock:
                    bucket.unknown_tick_count += 1
                return  # 0=無法判斷方向，排除，不計入訊號量
            # shioaji TickSTKv1.volume 平常是「張」，但盤中零股(intraday_odd=True)
            # 這個欄位改成「股」——同樣是 volume=50，整股單是 50 張(50,000股)大單，
            # 零股單只是 50 股(連 1 張都不到)。不排除的話零股單會被誤判成大單，
            # 汙染 large_order_flow。零股單本身還是真實資金流，money_flow 照計。
            volume_lots = normalized_volume_lots
            sign = 1 if tick_type == 1 else -1  # 1=ask(買盤主動) / 2=bid(賣盤主動)
            with self.lock:
                bucket.tick_count += 1
                bucket.money_flow += sign * amount
                if not intraday_odd and volume_lots >= LARGE_ORDER_LOT_THRESHOLD:
                    bucket.large_order_flow += sign * volume_lots * 1000
        except Exception:
            log(f"on_tick 例外：{traceback.format_exc()}")

    def on_bidask(self, exchange, bidask):
        try:
            if bool(getattr(bidask, "intraday_odd", False)):
                return
            if bool(getattr(bidask, "simtrade", False)) or bool(getattr(bidask, "suspend", False)):
                return
            symbol = "".join(ch for ch in str(getattr(bidask, "code", "") or "") if ch.isdigit())
            if len(symbol) != 4:
                return
            date_text, minute_key = tick_session_parts(getattr(bidask, "datetime", None))
            received_at = now_text()
            with self.lock:
                bucket = self.order_book_for(symbol, date_text, minute_key)
                bucket.apply(
                    getattr(bidask, "bid_price", None),
                    getattr(bidask, "bid_volume", None),
                    getattr(bidask, "diff_bid_vol", None),
                    getattr(bidask, "ask_price", None),
                    getattr(bidask, "ask_volume", None),
                    getattr(bidask, "diff_ask_vol", None),
                    received_at,
                )
        except Exception:
            log(f"on_bidask 例外：{traceback.format_exc()}")

    def on_quote_event(self, resp_code, event_code, info, event):
        record = {
            "respCode": int(resp_code or 0),
            "eventCode": int(event_code or 0),
            "info": str(info or ""),
            "event": str(event or ""),
            "at": now_text(),
        }
        with self.subscription_events_lock:
            self.subscription_events.append(record)
            self.subscription_events = self.subscription_events[-1000:]
        if record["respCode"] not in (0, 200):
            log(
                f"行情訂閱事件失敗 resp={record['respCode']} event={record['eventCode']} "
                f"info={record['info']} detail={record['event']}"
            )

    def failed_subscription_symbols(self, start_index, topic_prefix):
        with self.subscription_events_lock:
            events = list(self.subscription_events[start_index:])
        failed = set()
        prefix = str(topic_prefix or "").upper() + "/"
        for item in events:
            if int(item.get("respCode") or 0) in (0, 200):
                continue
            info = str(item.get("info") or "")
            if not info.upper().startswith(prefix):
                continue
            for part in reversed(info.split("/")):
                symbol = "".join(ch for ch in part if ch.isdigit())
                if len(symbol) == 4:
                    failed.add(symbol)
                    break
        return failed

    # contracts_timeout 預設 0(無限等待)，若網路卡住但連線沒有明確斷開/拋
    # 例外，login() 可能永遠不返回——這個子程序就會停在這一行，永遠不會
    # 走到 subscribe()，OS 層級 process 卻依然「活著」，server.py 的監督
    # 迴圈只檢查 process.poll() 判斷不出這種假活著，整天不會自動重啟。
    LOGIN_TIMEOUT_SECONDS = 90
    LOGIN_CONTRACTS_TIMEOUT_MS = 60000

    def login(self):
        import shioaji as sj

        self.api = sj.Shioaji(simulation=self.simulation)
        # 用 concurrent.futures 包一層應用層逾時，就算 SDK 內部真的卡死也
        # 能讓這個子程序自己判定登入失敗並結束(結束後 process.poll() 才會
        # 回傳非 None，server.py 才有機會偵測到並重啟)。
        # 注意：executor.shutdown 故意用 wait=False——如果 login() 真的卡死，
        # ThreadPoolExecutor 預設的 wait=True 會讓這裡繼續卡住等那條背景
        # thread 結束，等於整個逾時機制形同虛設。wait=False 讓逾時後這個
        # 函式能立刻往外拋例外，被卡住的背景 thread 隨行程結束(main()
        # 逾時後會直接 return 1)一併被系統回收，不需要手動清理。
        executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        try:
            future = executor.submit(
                self.api.login,
                api_key=self.api_key,
                secret_key=self.secret_key,
                fetch_contract=True,
                contracts_timeout=self.LOGIN_CONTRACTS_TIMEOUT_MS,
                subscribe_trade=False,
            )
            future.result(timeout=self.LOGIN_TIMEOUT_SECONDS)
        finally:
            executor.shutdown(wait=False)

        @self.api.on_tick_stk_v1()
        def quote_callback(exchange, tick):
            self.on_tick(exchange, tick)

        @self.api.on_bidask_stk_v1()
        def bidask_callback(exchange, bidask):
            self.on_bidask(exchange, bidask)

        # shioaji 1.3.3:on_session_down 簽名是 (func)->func，本身就是 decorator(不是工廠)。
        # 原本寫成 @self.api.on_session_down() 加括號=少傳 func→每次登入拋 TypeError→
        # 子程序一起來就崩、從沒訂到任何一檔 tick(面板 0 ticks 的致命主因)。去掉括號才對。
        @self.api.on_session_down
        def session_down_callback():
            log("Shioaji連線斷線(on_session_down callback觸發)，設定旗標讓主迴圈結束行程")
            self.session_down.set()

        quote_api = self._quote_api()
        set_event_callback = getattr(quote_api, "set_event_callback", None)
        if callable(set_event_callback):
            set_event_callback(self.on_quote_event)

    def find_contract(self, symbol):
        try:
            contract = self.api.Contracts.Stocks.get(symbol)
        except Exception:
            contract = None
        if contract is None:
            try:
                contract = self.api.Contracts.Stocks[symbol]
            except Exception:
                contract = None
        return contract

    def _quote_api(self):
        # shioaji 1.3.3 把 subscribe/unsubscribe 從 api 搬到 api.quote(SolaceAPI);
        # 原本呼叫 self.api.subscribe 在 1.3.3 是 AttributeError。用 getattr 相容新舊版。
        return getattr(self.api, "quote", self.api)

    def _quote_type(self, name):
        # shioaji 1.3.3 把 QuoteType 從頂層 shioaji.QuoteType 搬到 shioaji.constant.QuoteType。
        # 舊碼 sj.QuoteType.Tick 在 1.3.3 拋「module 'shioaji' has no attribute 'QuoteType'」
        # →200 檔全訂閱失敗、0 ticks 的真因。用 getattr 相容:優先頂層,取不到抓 constant。
        import shioaji as sj
        quote_type = getattr(sj, "QuoteType", None)
        if quote_type is None:
            from shioaji.constant import QuoteType as quote_type
        return getattr(quote_type, name)

    def _tick_quote_type(self):
        return self._quote_type("Tick")

    def _bidask_quote_type(self):
        return self._quote_type("BidAsk")

    def subscribe(self, symbols):
        quote_api = self._quote_api()
        tick_type = self._tick_quote_type()
        bidask_type = self._bidask_quote_type()
        with self.subscription_events_lock:
            tick_event_start = len(self.subscription_events)
        subscribed = []
        for symbol in symbols:
            contract = self.find_contract(symbol)
            if contract is None:
                log(f"找不到 {symbol} 的合約資料，略過訂閱")
                continue
            try:
                quote_api.subscribe(contract, quote_type=tick_type)
                subscribed.append(symbol)
            except Exception as exc:
                log(f"訂閱 {symbol} 失敗：{exc}")
        time.sleep(0.5)
        failed_ticks = self.failed_subscription_symbols(tick_event_start, "TIC")
        if failed_ticks:
            subscribed = [symbol for symbol in subscribed if symbol not in failed_ticks]
            log(f"永豐拒絕 {len(failed_ticks)} 檔 Tick 訂閱：{', '.join(sorted(failed_ticks)[:30])}")
        self.symbols = subscribed
        with self.subscription_events_lock:
            bidask_event_start = len(self.subscription_events)
        orderbook_subscribed = []
        for symbol in subscribed[:MAX_ORDERBOOK_SYMBOLS]:
            contract = self.find_contract(symbol)
            if contract is None:
                continue
            try:
                quote_api.subscribe(contract, quote_type=bidask_type)
                orderbook_subscribed.append(symbol)
            except Exception as exc:
                log(f"訂閱 {symbol} 五檔失敗：{exc}")
        time.sleep(0.5)
        failed_bidask = self.failed_subscription_symbols(bidask_event_start, "QUO")
        if failed_bidask:
            orderbook_subscribed = [
                symbol for symbol in orderbook_subscribed if symbol not in failed_bidask
            ]
            log(f"永豐拒絕 {len(failed_bidask)} 檔五檔訂閱：{', '.join(sorted(failed_bidask)[:30])}")
        self.orderbook_symbols = orderbook_subscribed
        return subscribed

    def scan_market_rankings(self):
        """Use the existing logged-in session to refresh five market rankings."""
        payload = sinopac_backend.stock_scanners(self.api, count=200)
        rows = payload.get("rows") or []
        saved = backend.upsert_intraday_scanner_rows(
            rows,
            trading_date=(payload.get("scanAt") or now_text())[:10],
            scan_at=payload.get("scanAt") or now_text(),
        )
        if payload.get("errors"):
            log("永豐全市場排行部分失敗：" + "；".join(payload["errors"][:8]))
        log(
            f"永豐全市場排行 {payload.get('count', 0)} 檔，"
            f"寫入 {saved.get('saved', 0)} 檔，排行={payload.get('rankCounts') or {}}"
        )
        return {**payload, "saved": int(saved.get("saved") or 0)}

    def scan_rotation_batch(self):
        """Snapshot one all-market batch on the existing logged-in session."""
        if not self.rotation_symbols:
            self.rotation_symbols = normalize_codes(backend.listed_symbols())
            self.rotation_cursor = 0
        symbols = self.rotation_symbols
        if not symbols:
            raise RuntimeError("全市場輪巡沒有可用股票母體")
        batch_count = max(1, math.ceil(len(symbols) / ROTATION_BATCH_SIZE))
        if self.rotation_cursor <= 0:
            self.rotation_round_sequence += 1
            self.rotation_round_id = (
                f"{taipei_now():%Y%m%d-%H%M%S}-{self.rotation_round_sequence}"
            )
        start = self.rotation_cursor
        batch_index = start // ROTATION_BATCH_SIZE
        batch = symbols[start:start + ROTATION_BATCH_SIZE]
        try:
            hot_symbols = normalize_codes(
                backend.intraday_hot_symbols(limit=MAX_HOT_SYMBOLS)
            )
        except Exception:
            hot_symbols = []
        # Newly discovered names and the prior batch's missing quotes are
        # refreshed again before their normal rotation slot, allowing the
        # required two distinct fresh snapshots without opening a second
        # Shioaji login session.
        requested = normalize_codes([
            *batch,
            *hot_symbols,
            *self.rotation_retry_symbols,
        ])
        primary_quotes, snapshot_error = sinopac_backend.stock_snapshots(
            self.api, requested,
        )
        primary_payload = {
            "ok": True,
            "quotes": {
                code: {**quote, "source": "sinopac_shioaji_rotation"}
                for code, quote in primary_quotes.items()
            },
            "source": "sinopac_shioaji_rotation",
            "error": snapshot_error or "",
        }
        quote_payload = sinopac_backend.capital_quote_fallback(
            requested,
            reason=(
                snapshot_error
                or f"Sinopac rotation missing {len(requested) - len(primary_quotes)} quote(s)"
            ),
            primary_payload=primary_payload,
        )
        quotes = (quote_payload or primary_payload).get("quotes") or {}
        fallback_codes = (quote_payload or {}).get("fallbackCodes") or []
        missing = [symbol for symbol in requested if symbol not in quotes]
        self.rotation_retry_symbols = missing[:ROTATION_BATCH_SIZE]
        scan_at = taipei_now().isoformat()
        saved = backend.upsert_intraday_rotation_quotes(
            quotes,
            trading_date=scan_at[:10],
            scan_at=scan_at,
            round_id=self.rotation_round_id,
            batch_index=batch_index,
            batch_count=batch_count,
            requested_count=len(requested),
            requested_symbols=requested,
            rotation_symbols=batch,
            universe_count=len(symbols),
            fallback_codes=fallback_codes,
            missing_symbols=missing,
            source=(quote_payload or primary_payload).get("source"),
        )
        self.rotation_cursor = start + len(batch)
        if self.rotation_cursor >= len(symbols):
            self.rotation_cursor = 0
        log(
            f"全市場輪巡 {batch_index + 1}/{batch_count}："
            f"{len(quotes)}/{len(requested)} 檔，輪轉母批 {len(batch)} 檔，"
            f"群益補 {len(fallback_codes)} 檔，round={self.rotation_round_id}"
            + (f"，error={snapshot_error}" if snapshot_error else "")
        )
        return {
            **saved,
            "universe": len(symbols),
            "rotationRequested": len(batch),
            "followupRequested": len(requested) - len(batch),
            "fallbackCodes": fallback_codes,
            "missingSymbols": missing,
            "error": snapshot_error or "",
        }

    def refresh_subscriptions(self, desired_symbols):
        """Apply Tick/BidAsk subscription deltas without reconnecting Shioaji."""
        desired = normalize_codes(desired_symbols)[:MAX_SUBSCRIBE_SYMBOLS]
        quote_api = self._quote_api()
        tick_type = self._tick_quote_type()
        bidask_type = self._bidask_quote_type()
        current = list(self.symbols)
        current_set = set(current)
        desired_set = set(desired)
        target_books = desired[:MAX_ORDERBOOK_SYMBOLS]
        target_book_set = set(target_books)

        removed_books = []
        for symbol in list(self.orderbook_symbols):
            if symbol in target_book_set and symbol in desired_set:
                continue
            try:
                contract = self.find_contract(symbol)
                if contract is not None:
                    quote_api.unsubscribe(contract, quote_type=bidask_type)
                removed_books.append(symbol)
            except Exception as exc:
                log(f"取消 {symbol} 五檔訂閱失敗：{exc}")

        removed_ticks = []
        for symbol in current:
            if symbol in desired_set:
                continue
            try:
                contract = self.find_contract(symbol)
                if contract is not None:
                    quote_api.unsubscribe(contract, quote_type=tick_type)
                removed_ticks.append(symbol)
            except Exception as exc:
                log(f"取消 {symbol} Tick 訂閱失敗：{exc}")

        with self.subscription_events_lock:
            tick_event_start = len(self.subscription_events)
        added_ticks = []
        for symbol in desired:
            if symbol in current_set:
                continue
            contract = self.find_contract(symbol)
            if contract is None:
                log(f"找不到 {symbol} 的合約資料，略過動態訂閱")
                continue
            try:
                quote_api.subscribe(contract, quote_type=tick_type)
                added_ticks.append(symbol)
            except Exception as exc:
                log(f"動態訂閱 {symbol} Tick 失敗：{exc}")
        if added_ticks:
            time.sleep(0.5)
        failed_ticks = self.failed_subscription_symbols(tick_event_start, "TIC")
        if failed_ticks:
            added_ticks = [symbol for symbol in added_ticks if symbol not in failed_ticks]
            log(f"永豐拒絕 {len(failed_ticks)} 檔動態 Tick 訂閱")
        subscribed_set = (current_set - set(removed_ticks)) | set(added_ticks)
        self.symbols = [symbol for symbol in desired if symbol in subscribed_set]

        with self.subscription_events_lock:
            bidask_event_start = len(self.subscription_events)
        existing_books = set(self.orderbook_symbols) - set(removed_books)
        added_books = []
        for symbol in self.symbols[:MAX_ORDERBOOK_SYMBOLS]:
            if symbol in existing_books:
                continue
            contract = self.find_contract(symbol)
            if contract is None:
                continue
            try:
                quote_api.subscribe(contract, quote_type=bidask_type)
                added_books.append(symbol)
            except Exception as exc:
                log(f"動態訂閱 {symbol} 五檔失敗：{exc}")
        if added_books:
            time.sleep(0.5)
        failed_books = self.failed_subscription_symbols(bidask_event_start, "QUO")
        if failed_books:
            added_books = [symbol for symbol in added_books if symbol not in failed_books]
            log(f"永豐拒絕 {len(failed_books)} 檔動態五檔訂閱")
        book_set = existing_books | set(added_books)
        self.orderbook_symbols = [
            symbol for symbol in self.symbols[:MAX_ORDERBOOK_SYMBOLS]
            if symbol in book_set
        ]
        return {
            "desired": len(desired),
            "subscribed": len(self.symbols),
            "tickAdded": added_ticks,
            "tickRemoved": removed_ticks,
            "bookAdded": added_books,
            "bookRemoved": removed_books,
            "failedTicks": sorted(failed_ticks),
            "failedBooks": sorted(failed_books),
        }

    def unsubscribe_all(self):
        quote_api = self._quote_api()
        tick_type = self._tick_quote_type()
        bidask_type = self._bidask_quote_type()
        for symbol in self.symbols:
            try:
                contract = self.find_contract(symbol)
                if contract is not None:
                    quote_api.unsubscribe(contract, quote_type=tick_type)
            except Exception:
                pass
        for symbol in self.orderbook_symbols:
            try:
                contract = self.find_contract(symbol)
                if contract is not None:
                    quote_api.unsubscribe(contract, quote_type=bidask_type)
            except Exception:
                pass

    def logout(self):
        try:
            if self.api is not None:
                self.api.logout()
        except Exception:
            pass

    def flush(self):
        with self.lock:
            snapshot = copy.deepcopy(self.stats)
            order_book_snapshot = copy.deepcopy(self.order_books)
        if not snapshot and not order_book_snapshot:
            self.last_orderbook_written = 0
            return 0
        written = 0
        orderbook_written = 0
        clean_after_commit = []
        orderbook_clean_after_commit = []
        try:
            with backend.connect() as conn:
                for (symbol, date_text), bucket in snapshot.items():
                    conn.execute(
                        """
                        INSERT OR REPLACE INTO realtime_flow_staging (
                            symbol, date, realtime_money_flow, realtime_large_order_flow,
                            tick_count, raw_tick_count, unknown_tick_count, total_volume_lots,
                            last_tick_at, source, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            symbol, date_text, bucket.money_flow, bucket.large_order_flow,
                            bucket.tick_count, bucket.raw_tick_count, bucket.unknown_tick_count,
                            bucket.total_volume_lots, bucket.last_tick_at, SOURCE_TAG, now_text(),
                        ),
                    )
                    now = taipei_now()
                    if date_text == now.date().isoformat() and bucket.total_volume_lots > 0:
                        minute = f"{now.hour:02d}:{(now.minute // 5) * 5:02d}"
                        conn.execute(
                            """
                            INSERT OR REPLACE INTO intraday_volume_profile (
                                symbol, date, minute, cumulative_volume_lots, source, updated_at
                            ) VALUES (?, ?, ?, ?, ?, ?)
                            """,
                            (symbol, date_text, minute, bucket.total_volume_lots, SOURCE_TAG, now_text()),
                        )
                    cumulative_volume = 0.0
                    for minute_key in sorted(bucket.minute_bars):
                        minute_bar = bucket.minute_bars[minute_key]
                        cumulative_volume += max(0.0, minute_bar.volume_lots)
                        if not minute_bar.dirty or minute_bar.close <= 0:
                            continue
                        conn.execute(
                            """
                            INSERT OR REPLACE INTO intraday_minute_bars (
                                symbol, date, minute, open, high, low, close,
                                volume_lots, cumulative_volume_lots,
                                active_buy_volume_lots, active_sell_volume_lots,
                                unknown_volume_lots, active_buy_amount, active_sell_amount,
                                unknown_amount, large_buy_volume_lots, large_sell_volume_lots,
                                raw_tick_count, directional_tick_count, unknown_tick_count,
                                first_tick_at, last_tick_at, source, updated_at
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                symbol, date_text, minute_key,
                                minute_bar.open, minute_bar.high, minute_bar.low, minute_bar.close,
                                minute_bar.volume_lots, cumulative_volume,
                                minute_bar.active_buy_volume_lots, minute_bar.active_sell_volume_lots,
                                minute_bar.unknown_volume_lots, minute_bar.active_buy_amount,
                                minute_bar.active_sell_amount, minute_bar.unknown_amount,
                                minute_bar.large_buy_volume_lots, minute_bar.large_sell_volume_lots,
                                minute_bar.raw_tick_count, minute_bar.directional_tick_count,
                                minute_bar.unknown_tick_count, minute_bar.first_tick_at,
                                minute_bar.last_tick_at, SOURCE_TAG, now_text(),
                            ),
                        )
                        clean_after_commit.append(
                            (symbol, date_text, minute_key, minute_bar.raw_tick_count)
                        )
                    written += 1
                for (symbol, date_text, minute_key), book in order_book_snapshot.items():
                    if not book.dirty or book.observation_count <= 0:
                        continue
                    count = book.observation_count
                    spread_count = book.spread_observation_count
                    conn.execute(
                        """
                        INSERT OR REPLACE INTO order_book_5m_features (
                            symbol, date, minute, observation_count,
                            spread_observation_count, avg_bid_depth_lots,
                            avg_ask_depth_lots, avg_imbalance, min_imbalance,
                            max_imbalance, last_imbalance, avg_spread_pct,
                            max_spread_pct, last_spread_pct,
                            avg_microprice_gap_pct, last_microprice_gap_pct,
                            last_best_bid, last_best_ask,
                            net_bid_volume_change_lots, net_ask_volume_change_lots,
                            first_snapshot_at, last_snapshot_at, source, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            symbol, date_text, minute_key, count, spread_count,
                            book.bid_depth_sum / count,
                            book.ask_depth_sum / count,
                            book.imbalance_sum / count,
                            book.min_imbalance, book.max_imbalance,
                            book.last_imbalance,
                            (book.spread_sum / spread_count) if spread_count else None,
                            book.max_spread_pct if spread_count else None,
                            book.last_spread_pct,
                            (book.microprice_gap_sum / spread_count) if spread_count else None,
                            book.last_microprice_gap_pct,
                            book.last_best_bid, book.last_best_ask,
                            book.net_bid_volume_change_lots,
                            book.net_ask_volume_change_lots,
                            book.first_snapshot_at, book.last_snapshot_at,
                            ORDER_BOOK_SOURCE_TAG, now_text(),
                        ),
                    )
                    orderbook_written += 1
                    orderbook_clean_after_commit.append(
                        (symbol, date_text, minute_key, book.observation_count)
                    )
            with self.lock:
                for symbol, date_text, minute_key, raw_tick_count in clean_after_commit:
                    current_bucket = self.stats.get((symbol, date_text))
                    current = current_bucket.minute_bars.get(minute_key) if current_bucket else None
                    if current is not None and current.raw_tick_count == raw_tick_count:
                        current.dirty = False
                for symbol, date_text, minute_key, observation_count in orderbook_clean_after_commit:
                    current = self.order_books.get((symbol, date_text, minute_key))
                    if current is not None and current.observation_count == observation_count:
                        current.dirty = False
        except Exception:
            log(f"flush 寫入 realtime_flow_staging 失敗：{traceback.format_exc()}")
        self.last_orderbook_written = orderbook_written
        return written


def cleanup_old_flow_rows(retention_days=REALTIME_FLOW_RETENTION_DAYS):
    """清掉 realtime_flow_staging 裡超過保留天數的舊列。純字串比較日期
    (YYYY-MM-DD 格式可字典序比較)，跟 collector 自己寫入的格式一致，
    不需要額外解析。清理失敗只記log，不能讓開盤前的啟動流程因為這個
    非關鍵步驟而中止。"""
    cutoff = (taipei_now() - dt.timedelta(days=retention_days)).date().isoformat()
    intraday_cutoff = (
        taipei_now() - dt.timedelta(days=INTRADAY_BAR_RETENTION_DAYS)
    ).date().isoformat()
    try:
        with backend.connect() as conn:
            deleted = conn.execute(
                "DELETE FROM realtime_flow_staging WHERE date < ?", (cutoff,)
            ).rowcount
            try:
                conn.execute("DELETE FROM intraday_volume_profile WHERE date < ?", (cutoff,))
            except sqlite3.OperationalError:
                pass  # 舊資料庫第一次啟動、init_db 尚未建新表時不阻斷清理
            try:
                conn.execute("DELETE FROM intraday_minute_bars WHERE date < ?", (intraday_cutoff,))
            except sqlite3.OperationalError:
                pass
            try:
                conn.execute("DELETE FROM order_book_5m_features WHERE date < ?", (intraday_cutoff,))
            except sqlite3.OperationalError:
                pass
        if deleted:
            log(f"清掉 realtime_flow_staging 裡 {cutoff} 之前的 {deleted} 筆舊資料")
        return deleted
    except Exception:
        log(f"清理 realtime_flow_staging 失敗(不影響本次收集)：{traceback.format_exc()}")
        return 0


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

    cleanup_old_flow_rows()

    config = sinopac_backend.load_config()
    api_key = config.get("apiKey")
    secret_key = config.get("secretKey")
    if not api_key or not secret_key:
        log("尚未設定永豐 API Key/Secret Key，即時tick收集器不啟動")
        return 1
    simulation = bool(config.get("simulation", False))

    symbols, info = select_watch_symbols()
    if not symbols:
        log("找不到任何可訂閱的股票代碼，即時tick收集器不啟動")
        return 1
    log(
        f"觀察名單共 {len(symbols)} 檔(持股 {info['holdings']} / 盤中熱門 {info['hot']} "
        f"/ 雷達 {info['radarSubscribed']}/{info['radar']} "
        f"/ 流動性觀察 {info['liquid']})，errors={info['errors']}"
    )
    if info["radarMissing"]:
        log(f"警告：訂閱上限不足，未訂閱雷達候選 {len(info['radarMissing'])} 檔 → {', '.join(info['radarMissing'][:30])}")

    collector = TickCollector(api_key, secret_key, simulation)
    today = taipei_now().date().isoformat()
    collector.bootstrap_today(symbols, today)

    try:
        collector.login()
    except Exception:
        log(f"Shioaji 登入失敗：{traceback.format_exc()}")
        return 1

    subscribed = collector.subscribe(symbols)
    log(f"成功訂閱 {len(subscribed)}/{len(symbols)} 檔即時tick")
    log(
        f"成功訂閱 {len(collector.orderbook_symbols)}/{min(len(subscribed), MAX_ORDERBOOK_SYMBOLS)} "
        "檔真實五檔 BidAsk"
    )
    missed = [s for s in symbols if s not in set(subscribed)]
    if missed:
        # 訂閱數明顯低於預期時，明確列出未訂到的代碼，方便排查是合約找不到
        # 還是 Shioaji 帳號訂閱上限（已知社群資訊約 200 檔，未經官方正式確認）
        log(f"警告：以下 {len(missed)} 檔未能訂閱 → {', '.join(missed[:30])}"
            + (f"… 等共 {len(missed)} 檔" if len(missed) > 30 else ""))

    try:
        collector.scan_market_rankings()
    except Exception:
        log(f"首次永豐全市場排行失敗，主連線繼續收 Tick：{traceback.format_exc()}")

    last_flush = time.time()
    last_scanner = time.time()
    last_hot_pool_refresh = time.time()
    holding_symbols = list(info.get("holdingSymbols") or [])
    session_lost = False
    try:
        if wait_for_market_open():
            try:
                collector.scan_rotation_batch()
            except Exception:
                log(f"首次全市場輪巡失敗，稍後重試：{traceback.format_exc()}")
            last_rotation = time.time()
            while market_session_active():
                # session_down 由 on_session_down callback 在斷線當下設定：
                # WebSocket斷線後 quote_callback 不會再被呼叫，只靠時鐘判斷
                # market_session_active() 完全看不出來，行程會維持存活但
                # 靜默停止收tick。這裡主動結束行程，讓 process.poll() 變成
                # 非 None，借用 server.py 既有的監督迴圈重啟機制。
                if collector.session_down.is_set():
                    session_lost = True
                    log("偵測到連線已斷線，結束本次收集行程等待監督迴圈重啟")
                    break
                time.sleep(2)
                if time.time() - last_scanner >= SCANNER_INTERVAL_SECONDS:
                    try:
                        collector.scan_market_rankings()
                    except Exception:
                        log(f"永豐全市場排行更新失敗，稍後重試：{traceback.format_exc()}")
                    last_scanner = time.time()
                if time.time() - last_rotation >= ROTATION_INTERVAL_SECONDS:
                    last_rotation = time.time()
                    try:
                        collector.scan_rotation_batch()
                    except Exception:
                        log(f"全市場輪巡批次失敗，稍後重試：{traceback.format_exc()}")
                if time.time() - last_hot_pool_refresh >= HOT_POOL_REFRESH_SECONDS:
                    try:
                        desired, refresh_info = select_watch_symbols(
                            holdings_override=holding_symbols,
                        )
                        added = [symbol for symbol in desired if symbol not in set(collector.symbols)]
                        if added:
                            collector.flush()
                            collector.bootstrap_today(added, today)
                        change = collector.refresh_subscriptions(desired)
                        log(
                            f"動態 Tick 池：熱門 {refresh_info.get('hot', 0)} 檔，"
                            f"訂閱 {change['subscribed']}/{change['desired']}，"
                            f"新增 {len(change['tickAdded'])}、移除 {len(change['tickRemoved'])}"
                        )
                    except Exception:
                        log(f"動態 Tick 熱門池更新失敗，保留原訂閱：{traceback.format_exc()}")
                    last_hot_pool_refresh = time.time()
                if time.time() - last_flush >= FLUSH_INTERVAL_SECONDS:
                    written = collector.flush()
                    last_flush = time.time()
                    log(
                        f"flush {written} 檔到 realtime_flow_staging，"
                        f"五檔特徵 {collector.last_orderbook_written} 列"
                    )
    except KeyboardInterrupt:
        pass
    finally:
        written = collector.flush()
        log(f"結束前最後 flush {written} 檔")
        collector.unsubscribe_all()
        collector.logout()
        log("已取消訂閱並登出")
    return 2 if session_lost else 0


if __name__ == "__main__":
    raise SystemExit(main())
