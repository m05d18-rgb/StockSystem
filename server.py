from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from html import escape
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urlparse
from urllib.request import Request, urlopen
import base64
import json
import re
import sqlite3
import statistics
import subprocess
import sys
import threading
import time
from collections import Counter
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from daily_update import load_latest_data_integrity_status
from daily_update import run as run_daily_job
from daily_update import set_daily_meta
from health_check import run_system_health
import line_notify
from line_notify import LINE_CONFIG_PATH, read_line_config, read_line_failure_state, read_line_file_config, read_line_quota
from line_notify import send_line_message as send_line_message_via_api
from ml_backend import (
    backend, DEFAULT_SYMBOLS, RETIRED_SYMBOLS, is_official_source, now_text,
    RADAR_MIN_FORMAL_SCORE, RADAR_MAX_ENTRY_DRIFT_PCT,
    RADAR_MIN_NET_REWARD_RISK_RATIO, radar_execution_analysis,
    radar_regime_threshold,
    classify_explicit_buy_strategy_horizon,
)
from modules.brain import build_brain_decision, configure_brain_engine
from capital_backend import capital_backend
from sinopac_backend import sinopac_backend
from pytorch_experiment import experiment_status as pytorch_experiment_status
from portfolio_exit import normalize_strategy_horizon
from market_calendar import (
    fetch_dgpa_taipei_closure,
    fetch_twse_calendar,
    load_market_session_overrides,
    planned_market_day,
)


ROOT = Path(__file__).resolve().parent
PORT = 8008
CACHE_SECONDS = 600


def _atomic_write_text(path, content, encoding="utf-8"):
    # 原子寫入:先寫同目錄 .tmp 再 replace(=os.replace,同磁碟原子換名),避免寫到一半
    # 崩潰/被中斷留下半截檔——設定檔半截 JSON 會讓下次讀取整個失敗(2026-07-07 稽核)。
    path = Path(path)
    tmp = path.parent / (path.name + ".tmp")
    tmp.write_text(content, encoding=encoding)
    tmp.replace(path)


def bind_server_with_retry(port, handler, attempts=5, delay_seconds=2):
    # 手機遠端已改由 cloudflared 在本機轉送，不再需要對區網或虛擬網卡監聽。
    # 僅綁 loopback 可避免同一區網上的其他裝置直接繞過 Cloudflare Access。
    for attempt in range(1, attempts + 1):
        try:
            return ThreadingHTTPServer(("127.0.0.1", port), handler)
        except OSError as exc:
            if attempt >= attempts:
                print(f"Failed to bind port {port} after {attempts} attempts: {exc}")
                raise
            print(f"Port {port} bind attempt {attempt} failed ({exc}), retrying...")
            time.sleep(delay_seconds)

AI_CONFIG_PATH = ROOT / "ai_api.json"
MARKET_SESSION_OVERRIDES_PATH = ROOT / "market_session_overrides.json"
cache = {}
finmind_user_info_cache = {"tokenHash": "", "fetchedAt": 0, "payload": None}
daily_update_started = False
daily_update_running = False
daily_update_lock = threading.Lock()   # W7: 防止 HTTP handler 與背景排程同時執行 full_daily_update
auto_schedule_started = False
auto_schedule_running = {}
official_close_sync_last_attempt = 0.0
twse_market_calendar_cache = {}
twse_market_calendar_lock = threading.Lock()
data_gap_repair_lock = threading.Lock()
DATA_GAP_REPAIR_COOLDOWN_SECONDS = 30 * 60
DATA_GAP_REPAIR_MAX_ATTEMPTS_PER_DAY = 3
data_gap_repair_status = {
    "ok": True,
    "running": False,
    "trigger": "",
    "startedAt": "",
    "finishedAt": "",
    "checked": 0,
    "attempted": 0,
    "repaired": 0,
    "stillMissing": 0,
    "deferred": 0,
    "notApplicable": 0,
    "failed": 0,
    "message": "尚未執行資料缺口修復",
    "details": [],
    "errors": [],
}
intraday_tick_started = False
intraday_tick_process = None
intraday_tick_process_lock = threading.Lock()
intraday_tick_status = {
    "ok": True,
    "running": False,
    "pid": None,
    "startedAt": "",
    "lastError": "",
}
SINOPAC_CONFIG_PATH = ROOT / "sinopac_api.json"
monster_intraday_running = False
monster_intraday_running_since = 0
monster_intraday_last_update = 0
portfolio_summary_cache_lock = threading.Lock()
# update_monster_intraday_quotes() 同時被 HTTP request thread(GET
# /api/monster-intraday 快取過期時同步呼叫)跟 auto_schedule_worker 背景
# thread(09:00-13:30 每 30 秒檢查一次)呼叫，兩者用的都是同一個 30 秒
# 節奏，check-then-act 的 monster_intraday_running 判斷沒上鎖的話很容易
# 兩邊同時通過，各自觸發一次 sinopac_backend.quotes()(各自 spawn 一個
# Shioaji 子程序)，且較慢完成的那個會整包覆寫較快完成那個剛寫入的新
# 報價。跟已修的 intraday_tick_process_lock 同樣道理，只在檢查/切換旗標
# 跟寫入最終結果的短暫瞬間持鎖，不包住中間會打網路的慢速工作本體。
monster_intraday_lock = threading.Lock()
INTRADAY_API_REFRESH_SECONDS = 30
# /api/monster-scores 前端要求 100 檔；盤中更新也必須以相同上限拉報價，不能
# 只更新前 50 檔而讓畫面後段回退成昨日收盤價。
MAX_MONSTER_INTRADAY_CANDIDATES = 100
INTRADAY_DISCOVERY_INTERVAL_SECONDS = 20
INTRADAY_DISCOVERY_RETRY_SECONDS = 60
INTRADAY_DISCOVERY_RESULT_LIMIT = 40
INTRADAY_DISCOVERY_MIN_HIGH_CHANGE_PCT = 1.5
INTRADAY_DISCOVERY_STANDARD_HIGH_CHANGE_PCT = 3.0
INTRADAY_DISCOVERY_STRONG_HIGH_CHANGE_PCT = 5.0
INTRADAY_DISCOVERY_MIN_TURNOVER_MILLION = 5.0
INTRADAY_DISCOVERY_HIGH_RETENTION_RATIO = 0.96
INTRADAY_DISCOVERY_MIN_ACCELERATION_PCT_PER_MINUTE = 0.35
INTRADAY_DISCOVERY_LOW_HISTORY_DAYS = 10
INTRADAY_DISCOVERY_ROTATION_MAX_AGE_SECONDS = 180
INTRADAY_DISCOVERY_CONFIRMATION_SCANS = 2
INTRADAY_DISCOVERY_CONFIRMATION_MAX_GAP_SECONDS = 150
INTRADAY_DISCOVERY_FORMAL_CONTEXT_LIMIT = 20
intraday_discovery_lock = threading.Lock()
intraday_discovery_running = False
intraday_discovery_last_attempt = 0.0
intraday_discovery_quote_history = {}
intraday_discovery_formal_context_cache = {}
intraday_discovery_baseline_cache = {
    "priceDate": "",
    "baselines": {},
    "metadata": {},
}
intraday_discovery_status = {
    "ok": True,
    "running": False,
    "checkedAt": "",
    "leaders": [],
    "message": "尚未執行盤中全市場探索",
}
monster_intraday_status = {
    "ok": True,
    "active": False,
    "buyableCount": 0,
    "shadowBuyableCount": 0,
    "snapshotPipeline": {"ok": False, "skipped": "not_run"},
    "updatedAt": "",
    "source": "",
    "marketDiscovery": intraday_discovery_status,
    "quotes": {},
    "error": "",
}
DATA_GAP_REQUIRED_FIELDS = (
    ("foreign_buy_sell", "外資買賣超", "chip_source"),
    ("trust_buy_sell", "投信買賣超", "chip_source"),
    ("margin_balance", "融資餘額", "margin_source"),
    ("short_balance", "融券餘額", "margin_source"),
    ("monthly_revenue", "月營收", "revenue_source"),
    ("revenue_growth", "營收年增率", "revenue_source"),
    ("per", "PER", "valuation_source"),
    ("pbr", "PBR", "valuation_source"),
    ("dividend_yield", "殖利率", "valuation_source"),
    ("gross_margin", "毛利率", "financial_statement_source"),
)


def intraday_status_is_fresh(status, last_update, max_age_seconds=INTRADAY_API_REFRESH_SECONDS):
    if not last_update:
        return False
    if not (status or {}).get("quotes") and not (status or {}).get("marketClosed"):
        return False
    return (time.time() - float(last_update)) < max_age_seconds


def intraday_status_with_cache_flag(status, cache_hit=False):
    payload = dict(status or {})
    payload["servedFromStatusCache"] = bool(cache_hit)
    if cache_hit and not payload.get("error"):
        if not payload.get("message"):
            payload["message"] = "沿用後端盤中報價快取，請以 updatedAt 與每檔 quoteFresh 判斷新鮮度。"
    return payload


def safe_int(value, default):
    # 好幾個 API handler 對查詢字串/JSON body 裡的數字參數(limit/maxSymbols
    # 等)直接呼叫 int()，完全沒有 try/except——遇到非數字輸入(例如
    # ?limit=abc)會讓 ValueError 直接往外穿出 do_GET/do_POST，而
    # BaseHTTPRequestHandler 的預設行為對這種未攔截例外不會送出任何 HTTP
    # 回應，只會把 traceback 印到 stderr 並直接斷開連線——用戶端拿到的是
    # 連線被重置，不是清楚的 400 錯誤，難以除錯也不一致(同一支程式裡
    # 有些 handler 已經有 try/except 回傳 500 JSON，這些卻沒有)。
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def clean_unicode_text(value):
    text = str(value or "")
    if not any(0xD800 <= ord(ch) <= 0xDFFF for ch in text):
        return text
    return text.encode("utf-16", "surrogatepass").decode("utf-16", "replace")


def clean_json_payload(value):
    if isinstance(value, str):
        return clean_unicode_text(value)
    if isinstance(value, list):
        return [clean_json_payload(item) for item in value]
    if isinstance(value, tuple):
        return [clean_json_payload(item) for item in value]
    if isinstance(value, dict):
        return {clean_unicode_text(key): clean_json_payload(item) for key, item in value.items()}
    return value


monster_scan_status = {
    "running": False,
    "phase": "尚未開始",
    "total": 0,
    "processed": 0,
    "saved": 0,
    "errors": 0,
    "current": "",
    "startedAt": "",
    "finishedAt": "",
    "message": "",
}
monster_scan_lock = threading.Lock()
system_health_snapshot = None

MONSTER_SCAN_STATUS_META_KEY = "runtime_monster_scan_status_json"
DATA_GAP_REPAIR_STATUS_META_KEY = "runtime_data_gap_repair_status_json"
INTRADAY_DISCOVERY_STATUS_META_KEY = "runtime_intraday_discovery_status_json"
STABILITY_OBSERVATION_KEY = "six-root-fixes-20260714"
STABILITY_OBSERVATION_SCOPE = [
    "invalid_market_data_guard",
    "strategy_stats_timeout",
    "runtime_status_restart_restore",
    "official_close_schedule_reconciliation",
    "reserved_test_data_isolation",
    "tcn_artifact_path_relocation",
]


def persist_runtime_status(meta_key, payload):
    snapshot = clean_json_payload(dict(payload or {}))
    try:
        with backend.connect() as conn:
            backend.set_meta(
                conn,
                meta_key,
                json.dumps(snapshot, ensure_ascii=False, separators=(",", ":"), default=str),
            )
    except Exception as exc:
        print(f"Persist runtime status {meta_key} failed: {exc}")
    return snapshot


def load_runtime_status(meta_key):
    try:
        with backend.connect() as conn:
            row = conn.execute(
                "SELECT value FROM model_meta WHERE key = ?", (meta_key,)
            ).fetchone()
        payload = json.loads(row[0]) if row and row[0] else None
        return payload if isinstance(payload, dict) else None
    except (json.JSONDecodeError, TypeError, sqlite3.Error):
        return None


def restore_monster_scan_status():
    if monster_scan_status.get("running"):
        return dict(monster_scan_status)
    if (
        str(monster_scan_status.get("phase") or "") not in {"", "尚未開始"}
        or monster_scan_status.get("finishedAt")
    ):
        return dict(monster_scan_status)
    saved = load_runtime_status(MONSTER_SCAN_STATUS_META_KEY)
    if not saved:
        keys = (
            "last_monster_scan_at", "last_monster_scan_count", "last_monster_scan_errors",
            "last_monster_scan_trigger", "last_monster_universe_total",
            "last_monster_liquid_universe",
        )
        try:
            with backend.connect() as conn:
                placeholders = ",".join("?" for _ in keys)
                meta = dict(conn.execute(
                    f"SELECT key, value FROM model_meta WHERE key IN ({placeholders})", keys
                ).fetchall())
        except sqlite3.Error:
            meta = {}
        finished_at = str(meta.get("last_monster_scan_at") or "")
        if finished_at:
            saved = {
                "running": False,
                "phase": "完成",
                "total": safe_int(meta.get("last_monster_universe_total"), 0),
                "processed": safe_int(meta.get("last_monster_universe_total"), 0),
                "saved": safe_int(meta.get("last_monster_scan_count"), 0),
                "errors": safe_int(meta.get("last_monster_scan_errors"), 0),
                "current": "",
                "startedAt": "",
                "finishedAt": finished_at,
                "trigger": str(meta.get("last_monster_scan_trigger") or "restored"),
                "liquidUniverse": safe_int(meta.get("last_monster_liquid_universe"), 0),
                "message": f"已由資料庫恢復最近一次妖股掃描：{finished_at}",
            }
    if not saved:
        return dict(monster_scan_status)
    if saved.get("running"):
        saved.update({
            "running": False,
            "phase": "重啟中斷",
            "current": "",
            "finishedAt": now_text(),
            "message": "上次妖股掃描因服務重啟中斷，排程可安全重新執行。",
        })
    with monster_scan_lock:
        monster_scan_status.update(saved)
        snapshot = dict(monster_scan_status)
    persist_runtime_status(MONSTER_SCAN_STATUS_META_KEY, snapshot)
    return snapshot


def restore_data_gap_repair_status():
    if data_gap_repair_status.get("running"):
        return dict(data_gap_repair_status)
    if (
        data_gap_repair_status.get("finishedAt")
        or str(data_gap_repair_status.get("message") or "")
        not in {"", "尚未執行資料缺口修復"}
    ):
        return dict(data_gap_repair_status)
    saved = load_runtime_status(DATA_GAP_REPAIR_STATUS_META_KEY)
    if not saved:
        keys = (
            "last_data_gap_repair_at", "last_data_gap_repair_trigger",
            "last_data_gap_repair_checked", "last_data_gap_repair_attempted",
            "last_data_gap_repair_repaired", "last_data_gap_repair_still_missing",
            "last_data_gap_repair_deferred", "last_data_gap_repair_not_applicable",
            "last_data_gap_repair_failed", "last_data_gap_repair_message",
        )
        try:
            with backend.connect() as conn:
                placeholders = ",".join("?" for _ in keys)
                meta = dict(conn.execute(
                    f"SELECT key, value FROM model_meta WHERE key IN ({placeholders})", keys
                ).fetchall())
        except sqlite3.Error:
            meta = {}
        finished_at = str(meta.get("last_data_gap_repair_at") or "")
        if finished_at:
            failed = safe_int(meta.get("last_data_gap_repair_failed"), 0)
            still_missing = safe_int(meta.get("last_data_gap_repair_still_missing"), 0)
            saved = {
                "ok": failed == 0 and still_missing == 0,
                "running": False,
                "retry": bool(failed or still_missing),
                "trigger": str(meta.get("last_data_gap_repair_trigger") or "restored"),
                "startedAt": "",
                "finishedAt": finished_at,
                "checked": safe_int(meta.get("last_data_gap_repair_checked"), 0),
                "attempted": safe_int(meta.get("last_data_gap_repair_attempted"), 0),
                "repaired": safe_int(meta.get("last_data_gap_repair_repaired"), 0),
                "stillMissing": still_missing,
                "deferred": safe_int(meta.get("last_data_gap_repair_deferred"), 0),
                "notApplicable": safe_int(meta.get("last_data_gap_repair_not_applicable"), 0),
                "failed": failed,
                "message": str(meta.get("last_data_gap_repair_message") or "已由資料庫恢復最近一次缺口修復"),
                "details": [],
                "errors": [],
            }
    if not saved:
        return dict(data_gap_repair_status)
    if saved.get("running"):
        saved.update({
            "ok": False,
            "running": False,
            "retry": True,
            "finishedAt": now_text(),
            "message": "上次資料缺口修復因服務重啟中斷，保留待辦供下一輪重試。",
        })
    data_gap_repair_status.update(saved)
    snapshot = dict(data_gap_repair_status)
    persist_runtime_status(DATA_GAP_REPAIR_STATUS_META_KEY, snapshot)
    return snapshot


def restore_intraday_discovery_status():
    """Restore the last display-only market discovery after a service restart."""
    global intraday_discovery_status
    if intraday_discovery_status.get("checkedAt"):
        return dict(intraday_discovery_status)
    saved = load_runtime_status(INTRADAY_DISCOVERY_STATUS_META_KEY)
    if not saved:
        return dict(intraday_discovery_status)
    saved = {
        **saved,
        "running": False,
        "restored": True,
    }
    if saved.get("trigger") and saved.get("ok") is not False:
        saved["message"] = (
            f"已恢復最近一次盤中全市場探索：{saved.get('checkedAt') or '時間未知'}"
        )
    with intraday_discovery_lock:
        intraday_discovery_status = saved
    with monster_intraday_lock:
        monster_intraday_status["marketDiscovery"] = dict(saved)
    leaders = saved.get("leaders") if isinstance(saved.get("leaders"), list) else []
    checked_at = str(saved.get("checkedAt") or "")
    if leaders and checked_at:
        try:
            backend.record_intraday_discovery_events(
                leaders,
                trading_date=checked_at[:10],
                observed_at=checked_at,
            )
        except Exception as exc:
            print(f"restore intraday discovery event backfill failed: {exc}")
    return dict(saved)

# 題材搜尋(handle_ai_theme_search)每次呼叫都是一次真實計費的 Perplexity API
# 呼叫，原本完全沒有節流——多分頁/多裝置各自按「手動掃描」會各自觸發，
# 沒有上限地疊加費用。跟 monster_scan_lock 同樣道理，用一個全域鎖+冷卻時間
# 戳記擋掉短時間內的重複觸發，不管是同一個分頁快速連點還是跨分頁/跨裝置。
THEME_SEARCH_COOLDOWN_SECONDS = 20
theme_search_lock = threading.Lock()
last_theme_search_at = 0.0

# /api/settings/ai/test 每次呼叫都是一次真實計費的 OpenAI+Perplexity API
# 呼叫，同樣道理需要節流；且它是GET端點，CSRF防護只加在do_POST/do_DELETE，
# 沒有Origin header的跨站img/no-cors fetch可以無限次觸發真實費用。
AI_SETTINGS_TEST_COOLDOWN_SECONDS = 20
ai_settings_test_lock = threading.Lock()
last_ai_settings_test_at = 0.0


def _server_brain_health_snapshot():
    return system_health_snapshot or {}


def _server_brain_quote_for_symbol(symbol):
    symbol = str(symbol or "")
    for status in (monster_intraday_status,):
        quotes = (status or {}).get("quotes") or {}
        quote = quotes.get(symbol)
        if quote:
            return quote, (status or {}).get("source") or quote.get("source") or "Shioaji quote"
    return None, ""


configure_brain_engine(
    health_provider=_server_brain_health_snapshot,
    quote_provider=_server_brain_quote_for_symbol,
)


def record_system_health(health):
    status = "success" if health.get("ok") else "failed"
    error = " | ".join(health.get("errors") or [])
    try:
        with backend.connect() as conn:
            backend.set_meta(conn, "last_system_health_status", status)
            backend.set_meta(conn, "last_system_health_at", health.get("checkedAt") or "")
            backend.set_meta(conn, "last_system_health_error", error)
            backend.set_meta(conn, "last_system_health_python", health.get("python", {}).get("executable") or "")
            backend.set_meta(conn, "last_system_health_model_version", health.get("model", {}).get("version") or "")
    except Exception as exc:
        health["recordWarning"] = f"system health meta write skipped: {exc}"
    return health


TAIPEI_TZ = ZoneInfo("Asia/Taipei")


def taipei_localtime():
    # 交易時段/排程視窗判斷全部要用台灣時間，不能用 time.localtime()（伺服器
    # 系統本地時區）——如果這套系統哪天被部署在非台北時區的機器上，開盤/收盤/
    # 各排程視窗會整批跑掉且沒有任何錯誤訊息。datetime.timetuple() 回傳的物件
    # 跟 time.struct_time 相容，可以直接餵給既有的 tm_wday/tm_hour/tm_min 判斷
    # 邏輯與 time.strftime()。
    return datetime.now(TAIPEI_TZ).timetuple()


def monster_entry_window(now=None):
    now = now or taipei_localtime()
    minutes = now.tm_hour * 60 + now.tm_min
    if not (0 <= now.tm_wday <= 4):
        return {"active": False, "phase": "closed", "label": "今日休市，不進場"}
    if minutes < (9 * 60 + 5):
        return {"active": False, "phase": "premarket", "label": "等待 09:05 初篩"}
    if minutes < (9 * 60 + 15):
        return {"active": False, "phase": "filter", "label": "09:05 初篩中，先不進場"}
    if minutes < (9 * 60 + 30):
        return {"active": False, "phase": "volume", "label": "09:15 看量能，等 09:30 確認"}
    if minutes < (10 * 60):
        return {"active": True, "phase": "initial", "label": "09:30-10:00 初次買進確認"}
    if minutes < (13 * 60 + 15):
        return {"active": True, "phase": "dip", "label": "10:00-13:15 只看低接/V轉"}
    return {"active": False, "phase": "closed", "label": "13:15 後不再進場"}


def monster_buy_confirm_window(now=None):
    return bool(monster_entry_window(now).get("active"))


def _clamp(value, low, high):
    return max(low, min(high, value))


def early_session_volume_rule(kind="monster", now=None):
    now = now or taipei_localtime()
    minutes = now.tm_hour * 60 + now.tm_min + (now.tm_sec / 60)
    initial_progress = _clamp((minutes - (9 * 60 + 30)) / 30, 0.0, 1.0)
    dip_progress = _clamp((minutes - (10 * 60)) / (3 * 60 + 15), 0.0, 1.0)
    if minutes <= 10 * 60:
        minimum = 0.25 + initial_progress * 0.25
    else:
        # 10:00 後不能把量能門檻永遠停在半日均量的 0.5 倍；越接近
        # 13:15，累積成交量理應越接近一個完整交易日。否則午後只成交
        # 平均量一半的股票，也可能靠小幅反彈被判成量能延續。
        minimum = 0.50 + dip_progress * 0.35
    session_progress = _clamp((minutes - (9 * 60 + 30)) / (3 * 60 + 45), 0.0, 1.0)
    return {
        "min": minimum,
        "max": None,
        "progress": session_progress,
        "label": "09:30-13:15 量能門檻隨時段提高",
        "source": "session_curve",
        "profileSamples": 0,
    }


def build_stock_intraday_volume_rules(rows, codes, target_minute, fallback_rule, min_samples=5):
    daily = {}
    for row in rows or []:
        item = dict(row)
        code = str(item.get("symbol") or "")
        date = str(item.get("date") or "")
        minute = str(item.get("minute") or "")
        volume = float(item.get("cumulative_volume_lots") or 0)
        if not code or not date or volume <= 0:
            continue
        bucket = daily.setdefault((code, date), {"at": 0.0, "total": 0.0})
        bucket["total"] = max(bucket["total"], volume)
        if minute <= target_minute:
            bucket["at"] = max(bucket["at"], volume)
    fractions = {str(code): [] for code in codes or []}
    for (code, _date), item in daily.items():
        if item["at"] > 0 and item["total"] > 0:
            fractions.setdefault(code, []).append(min(1.0, item["at"] / item["total"]))
    rules = {}
    for code, values in fractions.items():
        if len(values) < min_samples:
            continue
        median_fraction = statistics.median(values[-20:])
        threshold = _clamp(median_fraction * 0.90, 0.15, 0.95)
        rules[code] = {
            **fallback_rule,
            "min": threshold,
            "label": f"個股近 {min(len(values), 20)} 日同時段量能基準",
            "source": "stock_5m_profile",
            "profileSamples": min(len(values), 20),
            "expectedFraction": median_fraction,
        }
    return rules


def stock_intraday_volume_rules(codes, now, fallback_rule):
    if not codes:
        return {}
    today = f"{now.tm_year:04d}-{now.tm_mon:02d}-{now.tm_mday:02d}"
    cutoff = (datetime(now.tm_year, now.tm_mon, now.tm_mday) - timedelta(days=45)).strftime("%Y-%m-%d")
    target_minute = f"{now.tm_hour:02d}:{(now.tm_min // 5) * 5:02d}"
    try:
        placeholders = ",".join("?" for _ in codes)
        with backend.connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT symbol, date, minute, cumulative_volume_lots "
                f"FROM intraday_volume_profile WHERE symbol IN ({placeholders}) "
                "AND date >= ? AND date < ? ORDER BY symbol, date, minute",
                [*codes, cutoff, today],
            ).fetchall()
        return build_stock_intraday_volume_rules(rows, codes, target_minute, fallback_rule)
    except (sqlite3.OperationalError, TypeError, ValueError):
        return {}


def monster_intraday_setup_for_brain(symbol, context):
    # Brain Engine 的 kline/volume 分量原本看不到盤中已經被 update_monster_
    # intraday_quotes 完整驗證過的突破/回測/V轉型態，只憑昨天日K判斷。這裡把
    # 已快取的盤中 setupType/canBuy 帶給 build_brain_decision，讓已確認的盤中
    # 證據至少不會被過時的日線分數蓋掉。只在 monster context 才有意義，其他
    # context(如 portfolio_exit)沒有這份盤中候選資料，直接回傳 None。
    if context != "monster":
        return None
    quote = (monster_intraday_status.get("quotes") or {}).get(symbol)
    if not quote:
        return None
    return {"setupType": quote.get("setupType"), "canBuy": quote.get("canBuy")}


def _scan_trigger_zh(trigger):
    """把掃描觸發來源代碼轉成中文顯示(前端進度面板/報告用),未知的原樣保留。"""
    text = str(trigger or "").strip()
    mapping = {
        "manual": "手動",
        "auto": "自動",
        "auto-15:00": "15:00 自動排程",
        "startup": "啟動時自動",
        "scheduled": "排程",
    }
    if text in mapping:
        return mapping[text]
    if text.startswith("auto-"):
        return f"{text[len('auto-'):]} 自動排程"
    return text or "系統"


def monster_scan_result_error_count(result):
    """Return the complete scan error count even when error details are truncated."""
    raw_count = (result or {}).get("errorCount")
    if raw_count is not None:
        try:
            return max(0, int(raw_count))
        except (TypeError, ValueError):
            pass
    return len((result or {}).get("errors") or [])


def start_monster_scan_job(symbols=None, limit=300, score_limit=100, trigger="manual"):
    if symbols:
        symbols = [str(symbol).replace(".TWO", "").replace(".TW", "").strip() for symbol in symbols]
    limit = max(1, min(int(limit or 300), 2000))
    score_limit = max(1, min(int(score_limit or 100), 300))
    with monster_scan_lock:
        if monster_scan_status.get("running"):
            return {"ok": True, "started": False, "status": dict(monster_scan_status)}
        # 上一輪掃描殘留的附加狀態鍵(repair/officialSync/quickCandidates...)
        # 要清掉：前端的補齊階段進度條會拿舊 run 的 repairCandidates 當分母，
        # 新掃描開始的頭幾秒會顯示上一輪的錯誤進度。
        for stale_key in ("repair", "officialSync", "quickCandidates", "modelCandidates", "scoredCandidates", "universeTotal", "liquidUniverse"):
            monster_scan_status.pop(stale_key, None)
        monster_scan_status.update({
            "running": True,
            "phase": "準備掃描",
            "total": 0,
            "processed": 0,
            "saved": 0,
            "errors": 0,
            "current": "",
            "startedAt": time.strftime("%Y-%m-%d %H:%M:%S"),
            "finishedAt": "",
            "message": f"{_scan_trigger_zh(trigger)} 啟動妖股掃描",
            "trigger": trigger,
        })
        starting_snapshot = dict(monster_scan_status)
    persist_runtime_status(MONSTER_SCAN_STATUS_META_KEY, starting_snapshot)

    def update_progress(update):
        with monster_scan_lock:
            monster_scan_status.update(update)

    def worker():
        try:
            scan_symbols = symbols
            universe_total = len(scan_symbols) if scan_symbols else len(backend.listed_symbols())
            update_progress({
                "phase": "正式資料同步",
                "total": universe_total,
                "processed": 0,
                "saved": 0,
                "errors": 0,
                "current": "",
                "universeTotal": universe_total,
                "message": "掃描前同步 TWSE / TPEx 官方最新價量",
            })
            try:
                inactive_refresh = backend.refresh_twse_delisted_periods()
            except Exception as exc:
                inactive_refresh = {
                    "ok": False,
                    "error": str(exc),
                    "note": "沿用資料庫內已核對的停止交易期間",
                }
            invalid_cleanup = backend.cleanup_invalid_production_data()
            official_sync = backend.sync_official_daily_snapshot(symbols=scan_symbols)
            # 同步完全失敗(一筆都沒寫入且有錯誤)時不中止掃描——資料可能仍由
            # 每日更新保持新鮮，且個股層級已有 stale_price_data 護欄擋過期
            # 資料——但必須讓使用者看得見，不能靜默當作同步成功。
            sync_failed = bool(official_sync.get("errors")) and not int(official_sync.get("written") or 0)
            if sync_failed:
                with backend.connect() as conn:
                    backend.set_meta(conn, "last_monster_official_sync_failed_at", time.strftime("%Y-%m-%d %H:%M:%S"))
            update_progress({
                "phase": "官方同步失敗，改用既有資料繼續" if sync_failed else "正式資料同步完成",
                "total": universe_total,
                "processed": universe_total,
                "saved": int(official_sync.get("written") or 0),
                "errors": len(official_sync.get("errors") or []),
                "current": "",
                "universeTotal": universe_total,
                "officialSync": official_sync,
                "inactiveSymbolRefresh": inactive_refresh,
                "invalidDataCleanup": invalid_cleanup,
                "message": (
                    f"官方最新價量同步 {official_sync.get('written', 0)} 檔｜"
                    f"錯誤 {len(official_sync.get('errors') or [])} 筆"
                ),
            })
            if not scan_symbols:
                scan_symbols = backend.liquid_monster_universe()
                update_progress({
                    "phase": "流動性候選池",
                    "total": len(scan_symbols),
                    "processed": 0,
                    "saved": 0,
                    "errors": 0,
                    "current": "",
                    "universeTotal": universe_total,
                    "liquidUniverse": len(scan_symbols),
                    "message": f"全市場 {universe_total} 檔 → 流動性候選 {len(scan_symbols)} 檔 → 快速候選 0 檔 → 純規則評分 0 檔",
                })
            repair_result = backend.repair_before_scan(
                symbols=scan_symbols,
                max_repair=limit,
                progress_callback=update_progress
            )
            with monster_scan_lock:
                monster_scan_status["repair"] = repair_result
            result = backend.scan_monster_scores(symbols=scan_symbols, limit=limit, score_limit=score_limit, progress_callback=update_progress)
            result["repair"] = repair_result
            result["officialSync"] = official_sync
            result["universeTotal"] = universe_total
            result["liquidUniverse"] = len(scan_symbols)
            with backend.connect() as conn:
                backend.set_meta(conn, "last_monster_universe_total", str(universe_total))
                backend.set_meta(conn, "last_monster_liquid_universe", str(len(scan_symbols)))
                backend.set_meta(conn, "last_monster_scan_trigger", trigger)
            cache.clear()
            with monster_scan_lock:
                monster_scan_status.update({
                    "running": False,
                    "phase": "完成",
                    "processed": monster_scan_status.get("total", 0),
                    "saved": result.get("count", monster_scan_status.get("saved", 0)),
                    "errors": monster_scan_result_error_count(result),
                    "quickCandidates": result.get("quickCandidates", 0),
                    "modelCandidates": result.get("modelCandidates", 0),
                    "scoredCandidates": result.get("scoredCandidates", result.get("modelCandidates", 0)),
                    "universeTotal": universe_total,
                    "liquidUniverse": len(scan_symbols),
                    "officialSync": official_sync,
                    "current": "",
                    "finishedAt": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "message": (
                        f"全市場 {universe_total} 檔｜流動性候選 {len(scan_symbols)} 檔｜"
                        f"快速候選 {result.get('quickCandidates', 0)} 檔｜"
                        f"純規則評分 {result.get('scoredCandidates', result.get('modelCandidates', 0))} 檔｜"
                        f"更新 {time.strftime('%H:%M:%S')}"
                    ),
                })
                completed_snapshot = dict(monster_scan_status)
            persist_runtime_status(MONSTER_SCAN_STATUS_META_KEY, completed_snapshot)
        except Exception as exc:
            with monster_scan_lock:
                monster_scan_status.update({
                    "running": False,
                    "phase": "失敗",
                    "current": "",
                    "finishedAt": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "message": str(exc),
                })
                failed_snapshot = dict(monster_scan_status)
            persist_runtime_status(MONSTER_SCAN_STATUS_META_KEY, failed_snapshot)

    threading.Thread(target=worker, daemon=True).start()
    return {"ok": True, "started": True, "status": dict(monster_scan_status)}


def read_finmind_token():
    import os

    token = os.environ.get("FINMIND_API_TOKEN") or os.environ.get("FINMIND_TOKEN")
    if token:
        return token.strip()
    token_file = ROOT / "finmind_token.txt"
    if token_file.exists():
        return token_file.read_text(encoding="utf-8").strip()
    return ""


def fetch_finmind_user_info(token, max_age_seconds=300):
    if not token:
        return {"ok": False, "error": "FinMind Token is not configured"}
    token_hash = str(hash(token))
    now = time.time()
    cached = finmind_user_info_cache.get("payload")
    if (
        cached
        and finmind_user_info_cache.get("tokenHash") == token_hash
        and now - float(finmind_user_info_cache.get("fetchedAt") or 0) < max_age_seconds
    ):
        return dict(cached, cached=True)
    backend.reserve_finmind_call("FinMindUserInfo", "")
    request = Request(
        "https://api.web.finmindtrade.com/v2/user_info",
        headers={"Authorization": f"Bearer {token}"},
    )
    try:
        with urlopen(request, timeout=10) as response:
            raw = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        if exc.code in {402, 429}:
            backend.block_finmind_usage(f"FinMind user_info HTTP {exc.code}")
        raise
    payload = {
        "ok": True,
        "cached": False,
        "source": "FinMind user_info",
        "officialUserCount": raw.get("user_count"),
        "officialLimit": raw.get("api_request_limit"),
        "rawStatus": raw.get("status"),
    }
    finmind_user_info_cache.update({"tokenHash": token_hash, "fetchedAt": now, "payload": payload})
    return payload


def finmind_membership_status(token=None, include_official=False):
    usage = backend.read_finmind_usage()
    hard_limit = int(usage.get("hardLimit") or 6000)
    safe_limit = int(usage.get("safeLimit") or max(hard_limit - 1000, 0))
    reserved = int(usage.get("reserved") or max(hard_limit - safe_limit, 0))
    payload = {
        "plan": "Sponsor",
        "configured": bool(token),
        "hourlyLimit": hard_limit,
        "safeLimit": safe_limit,
        "reserved": reserved,
        "calls": int(usage.get("calls") or 0),
        "hour": usage.get("hour") or "",
        "blocked": bool(usage.get("blocked")),
        "lastError": usage.get("lastError") or "",
        "lastDataset": usage.get("lastDataset") or "",
        "lastSymbol": usage.get("lastSymbol") or "",
        "updatedAt": usage.get("updatedAt") or "",
        "usageSource": "local quota guard",
        "official": None,
        "note": "Sponsor 會員：官方上限 6000 次/小時；系統保留 1000 次安全額度，超過 5000 次會暫停 FinMind 呼叫。",
    }
    if include_official and token:
        try:
            official = fetch_finmind_user_info(token)
            payload["official"] = official
            official_limit = official.get("officialLimit")
            official_count = official.get("officialUserCount")
            if isinstance(official_limit, int) and official_limit > 0:
                payload["hourlyLimit"] = official_limit
            if isinstance(official_count, int) and official_count >= 0:
                payload["officialCalls"] = official_count
            usage = backend.read_finmind_usage()
            payload["calls"] = int(usage.get("calls") or payload["calls"])
            payload["blocked"] = bool(usage.get("blocked"))
            payload["lastError"] = usage.get("lastError") or ""
            payload["usageSource"] = "FinMind user_info + local quota guard"
        except Exception as exc:
            payload["official"] = {"ok": False, "error": str(exc), "source": "FinMind user_info"}
    return payload


def normalize_symbol(value):
    return "".join(ch for ch in str(value or "") if ch.isdigit())[:4]


def unique_symbols(values):
    output = []
    seen = set()
    for value in values or []:
        symbol = normalize_symbol(value)
        if len(symbol) == 4 and symbol not in seen:
            seen.add(symbol)
            output.append(symbol)
    return output


def advanced_flow_symbol_scope(scope="holdings", explicit_symbols=None, max_symbols=24):
    scope = str(scope or "holdings").strip().lower()
    max_symbols = max(1, min(int(max_symbols or 24), 120))
    sources = {"explicit": [], "holdings": [], "monster": [], "model": []}
    errors = []

    sources["explicit"] = unique_symbols(explicit_symbols or [])
    if sources["explicit"]:
        return {
            "scope": scope,
            "symbols": sources["explicit"][:max_symbols],
            "sources": sources,
            "errors": errors,
            "maxSymbols": max_symbols,
        }
    try:
        holdings = sinopac_backend.holdings()
        rows = holdings.get("holdings") or []
        sources["holdings"] = unique_symbols([item.get("code") for item in rows] or holdings.get("codes") or [])
    except Exception as exc:
        errors.append(f"holdings: {exc}")

    if scope in {"candidates", "all", "monster"}:
        try:
            payload = backend.list_monster_scores(80)
            sources["monster"] = unique_symbols(item.get("symbol") for item in payload.get("candidates", []))
        except Exception as exc:
            errors.append(f"monster: {exc}")
    if scope in {"model", "all"}:
        try:
            model = backend.load_model()
            sources["model"] = unique_symbols(model.get("symbols") or [])
        except Exception as exc:
            errors.append(f"model: {exc}")

    if sources["explicit"]:
        symbols = sources["explicit"]
    elif scope == "holdings":
        symbols = sources["holdings"]
    elif scope in {"candidates", "monster"}:
        symbols = unique_symbols(sources["holdings"] + sources["monster"])
    elif scope in {"model", "all"}:
        symbols = unique_symbols(sources["holdings"] + sources["monster"] + sources["model"])
    else:
        symbols = sources["holdings"]

    if not symbols:
        sources["fallback"] = unique_symbols(DEFAULT_SYMBOLS)
        symbols = sources["fallback"]
    return {
        "scope": scope,
        "symbols": symbols[:max_symbols],
        "sources": sources,
        "errors": errors,
        "maxSymbols": max_symbols,
    }


def advanced_flow_status(symbols=None):
    symbols = unique_symbols(symbols or [])
    # realtime_flow_staging 是盤中累積表；健康狀態只能統計台北交易日的列。
    # 舊版直接全表 SUM，昨天的數百萬筆 tick 會讓今天斷線/零資料仍看起來
    # 很健康，與 API 名稱的「即時」語意相反。
    staging_date = scheduler_today(taipei_localtime())
    placeholders = ",".join("?" for _ in symbols)
    where = f"WHERE symbol IN ({placeholders})" if symbols else ""
    params = symbols
    advanced_where_parts = ["date >= date('now', '-180 day')"]
    advanced_params = []
    if symbols:
        advanced_where_parts.append(f"symbol IN ({placeholders})")
        advanced_params.extend(symbols)
    advanced_where = "WHERE " + " AND ".join(advanced_where_parts)
    with backend.connect() as conn:
        conn.row_factory = None
        latest_rows = conn.execute(f"""
            WITH ranked AS (
                SELECT
                    symbol, date,
                    broker_branch_net_buy, main_force_buy_sell,
                    realtime_money_flow, realtime_large_order_flow,
                    branch_flow_source, realtime_flow_source,
                    ROW_NUMBER() OVER (PARTITION BY symbol ORDER BY date DESC) AS rn
                FROM prices
                {where}
            )
            SELECT
                symbol, date,
                broker_branch_net_buy, main_force_buy_sell,
                realtime_money_flow, realtime_large_order_flow,
                branch_flow_source, realtime_flow_source
            FROM ranked
            WHERE rn = 1
        """, params).fetchall()
        advanced_rows = conn.execute(f"""
            SELECT
                symbol, date,
                broker_branch_net_buy, main_force_buy_sell,
                realtime_money_flow, realtime_large_order_flow,
                branch_flow_source, realtime_flow_source
            FROM prices
            {advanced_where}
            ORDER BY symbol, date DESC
        """, advanced_params).fetchall()
        staging = conn.execute("""
            SELECT
                COUNT(*) AS rows,
                COUNT(DISTINCT symbol) AS symbols,
                COALESCE(SUM(tick_count), 0) AS ticks,
                COALESCE(SUM(raw_tick_count), 0) AS raw_ticks,
                COALESCE(SUM(unknown_tick_count), 0) AS unknown_ticks,
                MAX(last_tick_at) AS last_tick_at,
                MAX(updated_at) AS updated_at
            FROM realtime_flow_staging
            WHERE date = ?
        """, (staging_date,)).fetchone()

    total = len(latest_rows)
    def source_ok(source):
        return bool(source and is_official_source(source))
    def value_with_source(row, value_index, source_index, official_only=True):
        source = row[source_index]
        if row[value_index] is None or not source:
            return None
        if official_only and not source_ok(source):
            return None
        return row[value_index]
    latest_valid_by_symbol = {}
    for row in advanced_rows:
        if row[0] in latest_valid_by_symbol:
            continue
        if (
            value_with_source(row, 2, 6) is not None or
            value_with_source(row, 3, 6) is not None or
            value_with_source(row, 4, 7) is not None or
            value_with_source(row, 5, 7) is not None
        ):
            latest_valid_by_symbol[row[0]] = row
    counts = {
        "brokerBranch": len({row[0] for row in advanced_rows if (
            value_with_source(row, 2, 6) is not None or value_with_source(row, 3, 6) is not None
        )}),
        "realtimeFlow": len({row[0] for row in advanced_rows if (
            value_with_source(row, 4, 7) is not None or value_with_source(row, 5, 7) is not None
        )}),
    }
    samples = []
    sample_rows = list(latest_valid_by_symbol.values())[:20] or latest_rows[:20]
    for row in sample_rows:
        samples.append({
            "symbol": row[0],
            "date": row[1],
            "brokerBranch": value_with_source(row, 2, 6),
            "mainForce": value_with_source(row, 3, 6),
            "realtimeMoneyFlow": value_with_source(row, 4, 7),
            "realtimeLargeOrderFlow": value_with_source(row, 5, 7),
            "branchSource": row[6] or "",
            "realtimeSource": row[7] or "",
        })
    process_alive = intraday_tick_process is not None and intraday_tick_process.poll() is None
    return {
        "ok": True,
        "checkedAt": time.strftime("%Y-%m-%d %H:%M:%S"),
        "scopeSymbols": total,
        "coverage": counts,
        "samples": samples,
        "realtimeCollector": {
            **intraday_tick_status,
            "running": process_alive,
            "pid": intraday_tick_process.pid if process_alive else intraday_tick_status.get("pid"),
        },
        "staging": {
            "date": staging_date,
            "rows": int(staging[0] or 0),
            "symbols": int(staging[1] or 0),
            "ticks": int(staging[2] or 0),
            "rawTicks": int(staging[3] or 0),
            "unknownTicks": int(staging[4] or 0),
            "lastTickAt": staging[5] or "",
            "updatedAt": staging[6] or "",
        },
        "notes": [
            "主力分點：FinMind 券商分點日報，收盤後才有。",
            "即時資金流：Shioaji tick 推估，需盤中 collector 有啟動且有 tick。",
        ],
    }


def cleanup_stale_tick_collectors(keep_pid=None):
    script_path = str(ROOT / "realtime_tick_collector.py")
    keep_pid = int(keep_pid or 0)
    ps_script = (
        "$script = '" + script_path.replace("'", "''") + "'; "
        "$keep = " + str(keep_pid) + "; "
        "Get-CimInstance Win32_Process | "
        "Where-Object { $_.Name -like 'python*' -and $_.CommandLine -like ('*' + $script + '*') -and $_.ProcessId -ne $keep } | "
        "ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"
    )
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps_script],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            timeout=10,
        )
        return True
    except Exception as exc:
        intraday_tick_status["lastError"] = f"清理舊即時收集器失敗：{exc}"
        return False


def start_intraday_tick_collector(trigger="manual"):
    global intraday_tick_process, intraday_tick_status
    with intraday_tick_process_lock:
        running = intraday_tick_process is not None and intraday_tick_process.poll() is None
        if running:
            return {
                "ok": True,
                "started": False,
                "message": "即時資金流收集器已在執行",
                "pid": intraday_tick_process.pid,
            }
        if not SINOPAC_CONFIG_PATH.exists():
            intraday_tick_status.update({
                "ok": False,
                "running": False,
                "lastError": "尚未設定永豐 Shioaji API，無法啟動即時資金流。",
            })
            return {"ok": False, "started": False, "error": intraday_tick_status["lastError"]}
        script_path = ROOT / "realtime_tick_collector.py"
        flags = subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0
        try:
            cleanup_stale_tick_collectors()
            intraday_tick_process = subprocess.Popen(
                [sys.executable, str(script_path)],
                cwd=str(ROOT),
                creationflags=flags,
            )
            intraday_tick_status.update({
                "ok": True,
                "running": True,
                "pid": intraday_tick_process.pid,
                "startedAt": time.strftime("%Y-%m-%d %H:%M:%S"),
                "lastError": "",
                "trigger": trigger,
            })
            return {
                "ok": True,
                "started": True,
                "message": "即時資金流收集器已啟動；盤中有 tick 後才會寫入資料。",
                "pid": intraday_tick_process.pid,
            }
        except Exception as exc:
            intraday_tick_status.update({
                "ok": False,
                "running": False,
                "lastError": str(exc),
                "trigger": trigger,
            })
            return {"ok": False, "started": False, "error": str(exc)}


def mask_secret(value):
    value = str(value or "").strip()
    if len(value) >= 10:
        return f"{value[:4]}...{value[-4:]}"
    return ""


def api_key_format_error(value, label, prefixes=None):
    value = str(value or "").strip()
    if not value:
        return None
    if any(ord(char) > 127 for char in value):
        return f"{label} contains non-ASCII characters. Please paste the real API key, not placeholder text."
    if any(char.isspace() for char in value):
        return f"{label} must not contain spaces or line breaks."
    if len(value) < 20:
        return f"{label} looks too short."
    if prefixes and not value.startswith(tuple(prefixes)):
        return f"{label} format looks wrong. Expected prefix: {', '.join(prefixes)}"
    return None


def read_ai_file_config():
    if AI_CONFIG_PATH.exists():
        try:
            return json.loads(AI_CONFIG_PATH.read_text(encoding="utf-8") or "{}")
        except json.JSONDecodeError:
            return {}
    return {}


def read_ai_config():
    import os

    data = read_ai_file_config()
    openai_key = data.get("openaiApiKey") or os.environ.get("OPENAI_API_KEY") or ""
    perplexity_key = data.get("perplexityApiKey") or os.environ.get("PERPLEXITY_API_KEY") or ""
    return {
        "openaiApiKey": str(openai_key).strip(),
        "perplexityApiKey": str(perplexity_key).strip(),
        "enableAi": bool(data.get("enableAi", True)),
        "enableNews": bool(data.get("enableNews", True)),
        "openaiModel": str(data.get("openaiModel") or "gpt-5.4-mini").strip(),
        "perplexityModel": str(data.get("perplexityModel") or "sonar").strip(),
    }


def ai_config_status():
    config = read_ai_config()
    return {
        "ok": True,
        "openaiConfigured": bool(config["openaiApiKey"]),
        "perplexityConfigured": bool(config["perplexityApiKey"]),
        "openaiMasked": mask_secret(config["openaiApiKey"]),
        "perplexityMasked": mask_secret(config["perplexityApiKey"]),
        "enableAi": config["enableAi"],
        "enableNews": config["enableNews"],
        "openaiModel": config["openaiModel"],
        "perplexityModel": config["perplexityModel"],
    }


def line_config_status():
    config = read_line_config()
    failure_state = read_line_failure_state()
    quota = read_line_quota()
    return {
        "ok": True,
        "configured": bool(config["channelAccessToken"] and config["targetId"]),
        "enabled": config["enabled"],
        "tokenMasked": mask_secret(config["channelAccessToken"]),
        "targetMasked": mask_secret(config["targetId"]),
        "consecutiveFailures": failure_state["consecutiveFailures"],
        "lastFailureError": failure_state["lastError"],
        "lastFailureAt": failure_state["lastFailureAt"],
        "quota": quota,
    }


def send_windows_desktop_notification(title, message):
    title = clean_unicode_text(title)
    message = clean_unicode_text(message)
    title_b64 = base64.b64encode(title.encode("utf-8", errors="replace")).decode("ascii")
    message_b64 = base64.b64encode(message.encode("utf-8", errors="replace")).decode("ascii")
    script = f"""
$title = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{title_b64}'))
$message = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{message_b64}'))
try {{
  Add-Type -AssemblyName System.Windows.Forms
  Add-Type -AssemblyName System.Drawing
  $notify = New-Object System.Windows.Forms.NotifyIcon
  $notify.Icon = [System.Drawing.SystemIcons]::Warning
  $notify.BalloonTipIcon = [System.Windows.Forms.ToolTipIcon]::Warning
  $notify.BalloonTipTitle = $title
  $notify.BalloonTipText = $message
  $notify.Visible = $true
  $notify.ShowBalloonTip(12000)
  Start-Sleep -Seconds 13
  $notify.Dispose()
}} catch {{
  exit 1
}}
"""
    encoded = base64.b64encode(script.encode("utf-16le")).decode("ascii")
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    process = subprocess.Popen(
        ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-EncodedCommand", encoded],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=flags,
    )
    # 舊版 Popen 完直接回 sent:true：PowerShell 被執行原則/AppLocker 擋掉、
    # Add-Type 失敗(腳本 catch 內 exit 1)全都被當成功，前端因此永遠不會啟用
    # 瀏覽器 Notification 後備。腳本成功路徑會 Start-Sleep 13 秒(讓氣泡停留)，
    # 不能等它跑完；等 2.5 秒抓「快速失敗」：時限內就退出＝腳本炸了(exit 1)，
    # 還活著＝已過 Add-Type/NotifyIcon 建立這段失敗高風險區，視為送出。
    # 系統勿擾模式吞掉氣泡這種 OS 層失效，從退出碼看不出來，是已知極限。
    try:
        returncode = process.wait(timeout=2.5)
        if returncode != 0:
            return {"ok": False, "sent": False, "error": f"PowerShell 通知腳本失敗(exit {returncode})", "channel": "windows"}
    except subprocess.TimeoutExpired:
        pass
    return {"ok": True, "sent": True, "pid": process.pid, "channel": "windows"}


def range_to_start_date(range_value):
    days = {"1mo": 35, "3mo": 100, "6mo": 190, "1y": 370, "2y": 740, "3y": 1120, "5y": 1860}
    seconds = days.get(range_value, 1120) * 24 * 60 * 60
    return time.strftime("%Y-%m-%d", time.localtime(time.time() - seconds))


def offset_date(date_value, days):
    timestamp = time.mktime(time.strptime(date_value, "%Y-%m-%d")) + days * 24 * 60 * 60
    return time.strftime("%Y-%m-%d", time.localtime(timestamp))


def financial_statement_disclosure_date(period_end):
    # FinMind 回傳的財報 date 是「所屬期間」結束日（例如 Q1 是 3/31），不是「實際公開日」。
    # 台股法定最晚申報期限：Q1/Q3 季報是期末後 45 天、Q2 半年報是期末後 60 天、
    # 年報(Q4) 是期末後 90 天。用最晚法定期限當作這筆數字「最早可能被市場知道」的日期，
    # 訓練特徵才不會提前用到當時還沒公開的財報數字（前視偏誤 / lookahead bias）。
    try:
        month = int(str(period_end)[5:7])
    except (ValueError, TypeError, IndexError):
        return period_end
    lag_days = {3: 45, 9: 45, 6: 60, 12: 90}.get(month, 60)
    try:
        return offset_date(period_end, lag_days)
    except Exception:
        return period_end


def is_etf_like_stock(code="", name="", sector="", market_type=""):
    code = "".join(ch for ch in str(code or "") if ch.isdigit())
    text = " ".join(str(value or "") for value in (name, sector, market_type)).lower()
    return code.startswith("00") or "etf" in text


def stock_info_name_map(symbols):
    clean_symbols = []
    for raw in symbols or []:
        symbol = "".join(char for char in str(raw or "") if char.isdigit())[:4]
        if symbol and symbol not in clean_symbols:
            clean_symbols.append(symbol)
    if not clean_symbols:
        return {}
    placeholders = ",".join(["?"] * len(clean_symbols))
    try:
        with backend.connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(f"""
                SELECT symbol, name
                FROM stock_info
                WHERE symbol IN ({placeholders})
            """, clean_symbols).fetchall()
        return {row["symbol"]: row["name"] for row in rows}
    except Exception:
        return {}


def paper_signal_side(context, decision):
    if context == "portfolio_exit":
        return "EXIT_CONFIRM" if decision.get("canNotify") else "HOLD_WATCH"
    if decision.get("entryAllowed") or decision.get("action") == "BUY_CANDIDATE":
        return "BUY_CANDIDATE"
    return "WATCH"


PAPER_SIGNAL_SESSIONS = {
    "open_0905": {"label": "開盤初篩", "time": "09:05"},
    "volume_0915": {"label": "量能確認", "time": "09:15"},
    "intraday_0930": {"label": "盤中確認", "time": "09:30"},
    "preclose_1320": {"label": "收盤前", "time": "13:20"},
    "close_1520": {"label": "收盤後", "time": "15:20"},
    "manual": {"label": "手動快照", "time": ""},
}

PAPER_SIGNAL_SESSION_JOBS = {
    "open_0905": "0905_paper_signal_snapshot",
    "volume_0915": "0915_paper_signal_snapshot",
    "intraday_0930": "0930_paper_signal_snapshot",
    "preclose_1320": "1320_paper_signal_snapshot",
    "close_1520": "1520_paper_signal_snapshot",
}


def paper_signal_session_meta(session):
    key = str(session or "close_1520").strip() or "close_1520"
    meta = PAPER_SIGNAL_SESSIONS.get(key) or PAPER_SIGNAL_SESSIONS["close_1520"]
    return key, meta


def paper_signal_from_decision(symbol, context, name, decision, session="close_1520"):
    brain_v2 = decision.get("brainV2") or {}
    strategy_horizon = decision.get("strategyHorizon") or {}
    session_key, session_meta = paper_signal_session_meta(session)
    horizon_key = str(strategy_horizon.get("key") or "").strip()
    base_strategy = f"{context}_brain"
    strategy = f"{base_strategy}_{horizon_key}" if horizon_key and horizon_key != "unknown" else base_strategy
    latest_sources = decision.get("sources") or []
    data_source = " | ".join(
        str(row.get("source") or row.get("value") or "")
        for row in latest_sources[:4]
        if row
    )
    return {
        "signalDate": (decision.get("date") or time.strftime("%Y-%m-%d"))[:10],
        "signalSession": session_key,
        "signalSessionLabel": session_meta.get("label"),
        "signalTime": session_meta.get("time"),
        "strategy": strategy,
        "side": paper_signal_side(context, decision),
        "symbol": symbol,
        "name": name or symbol,
        "decision": decision.get("actionLabel") or decision.get("recommendation") or decision.get("action") or "WAIT",
        "score": (float(brain_v2.get("score")) * 100) if brain_v2.get("score") is not None else None,
        "modelVersion": "",
        "price": decision.get("currentPrice"),
        "buyPoint": None,
        "stopPrice": None,
        "targetPrice": None,
        "tradeHorizon": strategy_horizon.get("key"),
        "tradeHorizonLabel": strategy_horizon.get("label"),
        "tradeHorizonDays": strategy_horizon.get("holdingDays"),
        "tradeHorizonScore": strategy_horizon.get("score"),
        "dataDate": decision.get("date"),
        "dataSource": data_source or "Brain Engine verified rule sources",
        "decisionSource": "Brain Engine v2 rule-only paper signal snapshot",
        "evidence": {
            "context": context,
            "signalSession": session_key,
            "signalSessionLabel": session_meta.get("label"),
            "recommendation": decision.get("recommendation"),
            "action": decision.get("action"),
            "entryAllowed": decision.get("entryAllowed"),
            "canNotify": decision.get("canNotify"),
            "blockers": decision.get("blockers") or [],
            "brainV2": brain_v2,
            "strategyHorizon": strategy_horizon,
        },
    }


def record_paper_signal_snapshot(max_symbols=160, include_holdings=True, session="close_1520"):
    session_key, session_meta = paper_signal_session_meta(session)
    sources = {
        "portfolio_exit": [],
        "monster": [],
    }
    errors = []
    if include_holdings:
        try:
            payload = sinopac_backend.holdings()
            sources["portfolio_exit"] = [
                item.get("code") for item in payload.get("holdings") or []
                if item.get("code")
            ]
        except Exception as exc:
            errors.append(f"portfolio: {exc}")
    try:
        payload = backend.list_monster_scores(80)
        sources["monster"] = [
            item.get("symbol") for item in payload.get("candidates") or []
            if item.get("symbol")
        ]
    except Exception as exc:
        errors.append(f"monster: {exc}")

    symbols_by_context = {}
    all_symbols = []
    for context, raw_symbols in sources.items():
        clean = []
        for raw in raw_symbols:
            symbol = "".join(char for char in str(raw or "") if char.isdigit())[:4]
            if symbol and symbol not in clean:
                clean.append(symbol)
            if symbol and symbol not in all_symbols:
                all_symbols.append(symbol)
        symbols_by_context[context] = clean
    all_symbols = all_symbols[:max(1, int(max_symbols or 160))]
    name_map = stock_info_name_map(all_symbols)

    signals = []
    checked = 0
    for context, symbols in symbols_by_context.items():
        for symbol in symbols:
            if symbol not in all_symbols:
                continue
            try:
                decision = build_brain_decision(symbol, context=context)
                checked += 1
                if not decision.get("ok"):
                    errors.append(f"{symbol}/{context}: {decision.get('error') or 'Brain Engine failed'}")
                    continue
                signals.append(paper_signal_from_decision(symbol, context, name_map.get(symbol), decision, session=session_key))
            except Exception as exc:
                errors.append(f"{symbol}/{context}: {exc}")
    result = backend.record_strategy_signals({"signals": signals}) if signals else {"ok": True, "count": 0, "errors": []}
    stats = backend.strategy_signal_performance()
    return {
        "ok": True,
        "session": session_key,
        "sessionLabel": session_meta.get("label"),
        "checked": checked,
        "saved": result.get("count", 0),
        "sourceCounts": {key: len(value) for key, value in symbols_by_context.items()},
        "errors": (errors + (result.get("errors") or []))[:30],
        "stats": stats,
        "updatedAt": time.strftime("%Y-%m-%d %H:%M:%S"),
    }


def classify_sinopac_buy_strategy_horizon(payload, order):
    """Assign a horizon from verified source evidence, never from a client selector."""
    explicit = classify_explicit_buy_strategy_horizon(payload)
    if explicit.get("strategyHorizon") == "long_trend":
        return explicit
    context = str((payload or {}).get("orderContext") or "").strip().lower()
    if context != "monster_radar":
        return {
            "strategyHorizon": "unknown",
            "strategyHorizonSource": "sinopac_manual_order_unclassified",
            "evidence": [],
        }
    symbol = str((order or {}).get("code") or (payload or {}).get("symbol") or "").strip()
    scan_date = str((payload or {}).get("radarScanDate") or "")[:10]
    current_date = datetime.now(TAIPEI_TZ).date().isoformat()
    try:
        validation = backend.validate_radar_order_context(
            symbol,
            scan_date,
            current_date=current_date,
        )
    except Exception as exc:
        validation = {"ok": False, "reason": f"context_validation_failed: {exc}"}
    if validation.get("ok") is True:
        return {
            "strategyHorizon": "short_trade",
            "strategyHorizonSource": "verified_monster_radar_order_context",
            "evidence": validation,
        }
    return {
        "strategyHorizon": "unknown",
        "strategyHorizonSource": "monster_radar_order_context_rejected",
        "evidence": validation,
    }


def load_portfolio_summary_cache():
    with backend.connect() as conn:
        row = conn.execute(
            "SELECT value FROM model_meta WHERE key = 'portfolio_summary_cache'"
        ).fetchone()
    if not row or not row[0]:
        return None, None
    try:
        data = json.loads(row[0])
    except (json.JSONDecodeError, TypeError):
        return None, None
    if not isinstance(data, dict):
        return None, None
    if "summary" in data:
        summary = data.get("summary")
        holdings = data.get("holdings")
    else:
        summary, holdings = data, None
    return (
        summary if isinstance(summary, dict) else None,
        holdings if isinstance(holdings, dict) else None,
    )


def portfolio_cache_quote_symbols():
    with portfolio_summary_cache_lock:
        _, holdings = load_portfolio_summary_cache()
    symbols = []
    for code, item in (holdings or {}).items():
        symbol = str((item or {}).get("code") or code).replace(".TWO", "").replace(".TW", "").strip()
        if symbol and symbol not in symbols:
            symbols.append(symbol)
    return symbols


# StockHandler 繼承 SimpleHTTPRequestHandler 沒有做任何檔名限制，原本任何
# 落到 do_GET 結尾 super().do_GET() 的路徑都會被當成一般靜態檔案，原樣讀出
# 專案資料夾裡的檔案內容回傳——但這個資料夾裡同時放著 sinopac_api.json/
# ai_api.json/line_api.json/finmind_token.txt/Sinopac.pfx 這些含API金鑰、
# 交易密碼(=身分證字號)、CA憑證的機密檔案，以及完整原始碼、資料庫檔案。
# 用白名單而非黑名單：只有前端真的會載入的這幾個檔案能被靜態伺服器服務，
# 其餘一律 403，不管檔名有沒有想到過。
STATIC_FILE_ALLOWLIST = {
    "/",
    "/index.html",
    "/mobile-remote.html",
    "/settings.html",
    "/trades.html",
    "/app.js",
    "/mobile.js",
    "/mobile-remote.js",
    "/settings.js",
    "/trades.js",
    "/styles.css",
    "/mobile-remote.css",
    "/site.webmanifest",
    "/assets/icons/apple-touch-icon.png",
    "/assets/icons/favicon-32.png",
    "/assets/icons/stockai-icon-192.png",
    "/assets/icons/stockai-icon-512.png",
}

# 經 Cloudflare Tunnel 公開的手機入口採最小權限：只服務獨立手機頁與
# 這四個唯讀資料來源。桌面頁、設定頁、交易複盤及所有寫入端點均不可從
# Cloudflare 入口使用，避免 Access 帳號或手機遺失後擴大到真實下單權限。
CLOUDFLARE_MOBILE_STATIC_PATHS = {
    "/",
    "/index.html",
    "/mobile-remote.html",
    "/mobile-remote.js",
    "/mobile-remote.css",
    "/site.webmanifest",
    "/assets/icons/apple-touch-icon.png",
    "/assets/icons/favicon-32.png",
    "/assets/icons/stockai-icon-192.png",
    "/assets/icons/stockai-icon-512.png",
}
CLOUDFLARE_MOBILE_API_PATHS = {
    "/api/portfolio/summary-cache",
    "/api/stock-info",
    "/api/monster-scores",
    "/api/monster-intraday",
}


class StockHandler(SimpleHTTPRequestHandler):
    def translate_path(self, path):
        clean_path = urlparse(path).path.lstrip("/")
        return str((ROOT / clean_path).resolve())

    def end_headers(self):
        # 之前這裡對「每一個」回應都送出 Access-Control-Allow-Origin: *，這台
        # 伺服器只服務同源(同一個 http.server 同時發 index.html/settings.html
        # 跟 API，前端全部用相對路徑 fetch，從沒有跨源需求)，這個萬用 CORS
        # header 唯一的效果是讓瀏覽器允許「任何網站」的 JS 讀到這裡任何 API
        # 的回應內容——同源本來就不需要任何 CORS header，拿掉它不影響正常
        # 使用，但能擋掉跨源網頁讀取回應內容(例如 /api/settings/ai 洩漏金鑰
        # 是否已設定、model 名稱等中繼資料)。
        self.send_header("Cache-Control", "no-store")
        if self._is_cloudflare_mobile_request():
            self.send_header("Content-Security-Policy", (
                "default-src 'self'; connect-src 'self'; img-src 'self' data:; "
                "style-src 'self'; script-src 'self'; base-uri 'none'; "
                "frame-ancestors 'none'; form-action 'none'"
            ))
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
        super().end_headers()

    def _is_cloudflare_mobile_request(self):
        # cloudflared 會替所有經 Cloudflare Edge 進入 origin 的請求加上 CF-Ray。
        # 本機程式就算自行帶這個 header，也只會被降成唯讀手機權限，
        # 不會因此取得額外能力。
        return bool((self.headers.get("CF-Ray") or "").strip())

    def _prepare_cloudflare_mobile_get(self, parsed):
        if parsed.path == "/mobile":
            self.path = "/mobile-remote.html"
            parsed = urlparse(self.path)
        if not self._is_cloudflare_mobile_request():
            return parsed
        if parsed.path in {"/", "/index.html"}:
            self.path = "/mobile-remote.html"
            return urlparse(self.path)
        if parsed.path not in CLOUDFLARE_MOBILE_STATIC_PATHS \
                and parsed.path not in CLOUDFLARE_MOBILE_API_PATHS:
            self.send_error(403, "Forbidden (Cloudflare mobile access is read-only)")
            return None
        if parsed.path == "/api/monster-intraday":
            query = parse_qs(parsed.query)
            cached_only = str(query.get("cachedOnly", ["0"])[0]).lower() in {
                "1", "true", "yes"
            }
            if not cached_only:
                self.send_error(403, "Forbidden (cached intraday status only)")
                return None
        return parsed

    def _is_trusted_request_origin(self):
        # 防跨站偽造請求(CSRF)。伺服器目前只綁 loopback，cloudflared 也從
        # loopback 轉送；瀏覽器請求仍必須讓 Origin 與 Host 完全一致。沒有
        # Origin 的非瀏覽器工具只允許本機來源，保留終端機維護用途。
        origin = self.headers.get("Origin")
        if not origin:
            client_ip = self.client_address[0] if self.client_address else ""
            return client_ip in ("127.0.0.1", "::1", "::ffff:127.0.0.1")
        host = self.headers.get("Host") or ""
        try:
            origin_host = urlparse(origin).netloc
        except Exception:
            return False
        return bool(origin_host) and origin_host == host

    def send_head(self):
        # do_GET/do_HEAD 都會走到這裡（SimpleHTTPRequestHandler.do_HEAD 直接呼叫
        # send_head，不會經過 do_GET），是唯一能同時攔住 GET 與 HEAD 的地方。
        parsed = self._prepare_cloudflare_mobile_get(urlparse(self.path))
        if parsed is None:
            return None
        if parsed.path not in STATIC_FILE_ALLOWLIST:
            self.send_error(403, "Forbidden")
            return None
        return super().send_head()

    def do_GET(self):
        parsed = self._prepare_cloudflare_mobile_get(urlparse(self.path))
        if parsed is None:
            return
        if parsed.path == "/api/stock":
            self.handle_stock(parsed)
            return
        if parsed.path == "/api/settings/finmind-token":
            self.handle_token_status()
            return
        if parsed.path == "/api/settings/finmind-token/test":
            if not self._is_trusted_request_origin():
                self.send_error(403, "Forbidden (cross-origin request rejected)")
                return
            self.handle_token_test()
            return
        if parsed.path == "/api/settings/ai":
            self.write_json(ai_config_status())
            return
        if parsed.path == "/api/settings/ai/test":
            if not self._is_trusted_request_origin():
                self.send_error(403, "Forbidden (cross-origin request rejected)")
                return
            self.handle_ai_settings_test()
            return
        if parsed.path == "/api/settings/line":
            self.write_json(line_config_status())
            return
        if parsed.path == "/api/settings/line/test":
            if not self._is_trusted_request_origin():
                self.send_error(403, "Forbidden (cross-origin request rejected)")
                return
            self.handle_line_settings_test()
            return
        if parsed.path == "/api/stock-info":
            self.handle_stock_info(parsed)
            return
        if parsed.path == "/api/ml/status":
            self.write_json(backend.status())
            return
        if parsed.path == "/api/model-experiments/tcn/status":
            try:
                self.write_json(pytorch_experiment_status())
            except Exception as exc:
                self.write_json({
                    "ok": False,
                    "mode": "independent_observation_only",
                    "usedByRadar": False,
                    "error": str(exc),
                    "checkedAt": now_text(),
                }, status=500)
            return
        if parsed.path == "/api/system/stability-observation":
            self.write_json(backend.stability_observation_status())
            return
        if parsed.path == "/api/system/health":
            global system_health_snapshot
            try:
                query = parse_qs(parsed.query)
                include_prediction = str((query.get("predict") or ["1"])[0]).lower() in {"1", "true", "yes"}
                system_health_snapshot = record_system_health(run_system_health(include_prediction=include_prediction))
                self.write_json(system_health_snapshot)
            except Exception as exc:
                failed = {
                    "ok": False,
                    "mode": "independent_model_unavailable",
                    "reason": "獨立模型不可用；正式規則分析仍依資料健康運作",
                    "errors": [str(exc)],
                    "checkedAt": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "decisionsEnabled": False,
                }
                system_health_snapshot = record_system_health(failed)
                self.write_json(failed, status=500)
            return
        if parsed.path == "/api/ml/predict":
            self.handle_ml_predict(parsed)
            return
        if parsed.path == "/api/ml/predictions":
            self.handle_ml_predictions(parsed)
            return
        if parsed.path == "/api/radar/score-track-record":
            query = parse_qs(parsed.query)
            try:
                lookback_days = max(30, min(int(query.get("days", [365])[0]), 1095))
            except (TypeError, ValueError):
                lookback_days = 365
            stats = backend.compute_radar_score_track_record(lookback_days=lookback_days)
            stats["deploymentReadiness"] = backend.current_radar_deployment_readiness()
            self.write_json(stats)
            return
        if parsed.path == "/api/radar/discovery-recall":
            query = parse_qs(parsed.query)
            try:
                days = max(1, min(int(query.get("days", [30])[0]), 365))
            except (TypeError, ValueError):
                days = 30
            refresh = str(query.get("refresh", ["0"])[0]).lower() in {
                "1", "true", "yes",
            }
            if refresh and not self._is_trusted_request_origin():
                self.send_error(403, "Forbidden (cross-origin request rejected)")
                return
            self.write_json(backend.intraday_discovery_recall_history(
                days=days,
                refresh_latest=refresh,
            ))
            return
        if parsed.path == "/api/radar/strategy-experiments":
            query = parse_qs(parsed.query)
            self.write_json({
                "ok": True,
                "records": backend.list_radar_strategy_experiment_snapshots(
                    safe_int(query.get("limit", ["30"])[0], 30)
                ),
            })
            return
        if parsed.path == "/api/radar/deployment-readiness":
            query = parse_qs(parsed.query)
            refresh = str(query.get("refresh", ["0"])[0]).lower() in {"1", "true", "yes"}
            if refresh and not self._is_trusted_request_origin():
                self.send_error(403, "Forbidden (cross-origin request rejected)")
                return
            self.write_json(
                backend.refresh_radar_deployment_readiness()
                if refresh else backend.current_radar_deployment_readiness(refresh_if_stale=True)
            )
            return
        if parsed.path == "/api/radar/candidate-followthrough":
            self.write_json(backend.compute_candidate_followthrough())
            return
        if parsed.path == "/api/radar/track-record":
            self.write_json(radar_track_record_stats())
            return
        if parsed.path == "/api/portfolio/loss-streak":
            self.write_json(loss_streak_status())
            return
        if parsed.path == "/api/portfolio/trade-journal":
            self.write_json(trade_journal_stats())
            return
        if parsed.path == "/api/portfolio/realized-review":
            self.write_json(realized_radar_review_stats())
            return
        if parsed.path == "/api/portfolio/exit-decision-logs":
            query = parse_qs(parsed.query)
            self.write_json(list_exit_decision_logs(safe_int(query.get("limit", ["80"])[0], 80)))
            return
        if parsed.path == "/api/market/status":
            live = None
            # 2026-07-09 大盤即時(走A):優先用永豐 Shioaji 快照(併在庫存/報價同步那次登入、
            # 零額外登入、跟個股同鮮度);沒有或不夠新(>3分)才 fallback Yahoo(約延遲10-20分,
            # 維持現狀不退化)。
            try:
                shioaji_taiex = sinopac_backend.last_taiex_snapshot(max_age_seconds=180)
            except Exception:
                shioaji_taiex = None
            if shioaji_taiex and shioaji_taiex.get("price"):
                at = str(shioaji_taiex.get("at") or "")
                live = {
                    "taiexLivePrice": shioaji_taiex.get("price"),
                    "taiexLiveChangePct": shioaji_taiex.get("changeRate"),
                    "taiexLiveTime": at[11:16] if len(at) >= 16 else None,
                    "taiexLiveDate": at[:10] if len(at) >= 10 else None,
                    "taiexLiveSource": "永豐即時",
                }
            if live is None:
                try:
                    yahoo = backend.fetch_taiex_live()  # fallback:Yahoo,快取30s、失敗回None
                    if yahoo:
                        live = dict(yahoo)
                        live["taiexLiveSource"] = "Yahoo延遲"
                except Exception:
                    live = None
            # 2026-07-09 使用者「大盤要即時」:把即時加權價餵進 market_status,紅綠燈的
            # 「站上/跌破月線」用即時價 vs 月線重算(拿不到即時價則退回日線收盤,不退化)。
            live_price = live.get("taiexLivePrice") if live else None
            payload = backend.market_status(live_price=live_price)
            if live:
                payload.update(live)
            self.write_json(payload)
            return
        if parsed.path == "/api/backtest-summary":
            # 回測程度:讀 backtest_top10_result.json 的 model_prob OOS(已驗證的妖股策略成績)。唯讀無副作用。
            try:
                with open(ROOT / "backtest_top10_result.json", encoding="utf-8") as f:
                    bt = json.load(f)
                mp = (((bt.get("rankings") or {}).get("model_prob") or {}).get("oos")) or {}
                self.write_json({
                    "ok": bool(mp),
                    "avgNetPct": mp.get("avgNetPct"),
                    "hit10Rate": mp.get("hit10Rate"),
                    "winRate": mp.get("winRate"),
                    "profitFactor": mp.get("profitFactor"),
                    "trades": mp.get("trades"),
                    "avgHoldDays": mp.get("avgHoldDays"),
                    "oosBoundary": bt.get("oosBoundary"),
                    "dateRange": bt.get("dateRange"),
                })
            except Exception as exc:
                self.write_json({"ok": False, "error": str(exc)[:80]})
            return
        if parsed.path == "/api/data-freshness":
            self.write_json(backend.data_freshness())
            return
        if parsed.path == "/api/data-integrity":
            self.write_json(load_latest_data_integrity_status())
            return
        if parsed.path == "/api/market/session-validations":
            query = parse_qs(parsed.query)
            session_date = str(query.get("date", [scheduler_today(taipei_localtime())])[0])[:10]
            self.write_json({
                "ok": True,
                "records": backend.list_market_session_validations(
                    safe_int(query.get("limit", ["40"])[0], 40)
                ),
                "acceptance": backend.market_session_acceptance(session_date),
                "acceptanceHistory": backend.list_market_session_acceptance_history(
                    safe_int(query.get("limit", ["40"])[0], 40)
                ),
            })
            return
        if parsed.path == "/api/sector-leaders":
            self.write_json(backend.sector_leader_map())
            return
        if parsed.path == "/api/order-suggestions":
            self.write_json(backend.order_suggestion_list())
            return
        if parsed.path == "/api/user-prefs":
            self.handle_user_prefs_get()
            return
        if parsed.path == "/api/portfolio/summary-cache":
            self.handle_summary_cache_get()
            return
        if parsed.path == "/api/portfolio/exit-analysis":
            self.handle_portfolio_exit_analysis()
            return
        if parsed.path == "/api/portfolio/exit-analysis/snapshots":
            query = parse_qs(parsed.query)
            self.write_json(backend.list_portfolio_exit_snapshots(
                safe_int(query.get("limit", ["120"])[0], 120)
            ))
            return
        if parsed.path == "/api/portfolio/exit-analysis/history":
            query = parse_qs(parsed.query)
            self.write_json(backend.list_portfolio_exit_history(
                safe_int(query.get("limit", ["200"])[0], 200)
            ))
            return
        if parsed.path == "/api/portfolio/exit-analysis/performance":
            query = parse_qs(parsed.query)
            refresh = str(query.get("refresh", ["0"])[0]).lower() in {"1", "true", "yes"}
            if refresh and not self._is_trusted_request_origin():
                self.send_error(403, "Forbidden (cross-origin request rejected)")
                return
            self.write_json(backend.portfolio_exit_performance(refresh=refresh))
            return
        if parsed.path == "/api/portfolio/exit-watch/notified-today":
            self.handle_exit_watch_notified_get()
            return
        if parsed.path == "/api/holdings/dividend-calendar":
            self.write_json(holdings_dividend_calendar())
            return
        if parsed.path == "/api/model-data-check":
            self.handle_model_data_check(parsed)
            return
        if parsed.path == "/api/data/gap-repair/status":
            self.write_json(restore_data_gap_repair_status())
            return
        if parsed.path == "/api/brain/decision":
            self.handle_brain_decision(parsed)
            return
        if parsed.path == "/api/monster-scores":
            self.handle_monster_scores(parsed)
            return
        if parsed.path == "/api/new-stock-radar":
            query = parse_qs(parsed.query)
            limit = max(1, min(80, safe_int(query.get("limit", ["20"])[0], 20)))
            self.write_json(backend.new_stock_radar(limit=limit))
            return
        if parsed.path == "/api/monster-scan/status":
            self.handle_monster_scan_status()
            return
        if parsed.path == "/api/monster-intraday":
            if not self._is_trusted_request_origin():
                self.send_error(403, "Forbidden (cross-origin request rejected)")
                return
            query = parse_qs(parsed.query)
            force = str(query.get("force", ["0"])[0]).lower() in {"1", "true", "yes"}
            cached_only = str(query.get("cachedOnly", ["0"])[0]).lower() in {
                "1", "true", "yes"
            }
            cache_hit = False
            if cached_only:
                cache_hit = True
            elif not force and intraday_status_is_fresh(monster_intraday_status, monster_intraday_last_update):
                cache_hit = True
            else:
                update_monster_intraday_quotes(trigger="api-09:30")
            self.write_json(intraday_status_with_cache_flag(monster_intraday_status, cache_hit))
            return
        if parsed.path == "/api/quotes":
            self.handle_quotes(parsed)
            return
        if parsed.path == "/api/advanced-flow/status":
            query = parse_qs(parsed.query)
            scope = query.get("scope", ["holdings"])[0]
            max_symbols = safe_int(query.get("maxSymbols", ["24"])[0], 24)
            explicit = []
            if query.get("symbols"):
                explicit = ",".join(query.get("symbols", [])).split(",")
            symbol_scope = advanced_flow_symbol_scope(scope, explicit, max_symbols)
            payload = advanced_flow_status(symbol_scope["symbols"])
            payload.update({
                "scope": symbol_scope["scope"],
                "symbols": symbol_scope["symbols"],
                "sources": symbol_scope["sources"],
                "scopeErrors": symbol_scope["errors"],
            })
            self.write_json(payload)
            return
        if parsed.path == "/api/strategy-signals/stats":
            self.handle_strategy_signal_stats(parsed)
            return
        if parsed.path == "/api/paper-signals/stats":
            self.handle_strategy_signal_stats(parsed)
            return
        if parsed.path == "/api/strategy-calibration":
            self.handle_strategy_calibration(parsed)
            return
        if parsed.path == "/api/trades":
            self.handle_trades_list(parsed)
            return
        if parsed.path == "/api/trades/duplicates":
            self.handle_trades_duplicates(apply=False)
            return
        if parsed.path == "/api/sinopac/status":
            self.write_json(sinopac_backend.status())
            return
        if parsed.path == "/api/capital/status":
            self.write_json(capital_backend.status())
            return
        if parsed.path == "/api/sinopac/holdings":
            self.handle_sinopac_holdings()
            return
        if parsed.path == "/api/sinopac/realized-pnl":
            self.handle_sinopac_realized_pnl()
            return
        if parsed.path == "/api/sinopac/order-fills":
            if not self._is_trusted_request_origin():
                self.send_error(403, "Forbidden (cross-origin request rejected)")
                return
            self.handle_sinopac_order_fills(sync=False)
            return
        super().do_GET()

    def do_POST(self):
        if self._is_cloudflare_mobile_request():
            self.send_error(403, "Forbidden (Cloudflare mobile access is read-only)")
            return
        if not self._is_trusted_request_origin():
            self.send_error(403, "Forbidden (cross-origin request rejected)")
            return
        parsed = urlparse(self.path)
        if parsed.path == "/api/settings/finmind-token":
            self.handle_token_save()
            return
        if parsed.path == "/api/settings/ai":
            self.handle_ai_settings_save()
            return
        if parsed.path == "/api/settings/line":
            self.handle_line_settings_save()
            return
        if parsed.path == "/api/line/notify":
            self.handle_line_notify()
            return
        if parsed.path == "/api/desktop/notify":
            self.handle_desktop_notify()
            return
        if parsed.path == "/api/portfolio/exit-watch":
            self.handle_exit_watch_save()
            return
        if parsed.path == "/api/user-prefs":
            self.handle_user_prefs_save()
            return
        if parsed.path == "/api/portfolio/summary-cache":
            self.handle_summary_cache_save()
            return
        if parsed.path == "/api/portfolio/strategy-horizon":
            self.handle_portfolio_strategy_horizon_lock()
            return
        if parsed.path == "/api/portfolio/strategy-horizons/batch":
            self.handle_portfolio_strategy_horizon_batch_lock()
            return
        if parsed.path == "/api/portfolio/legacy-lots":
            self.handle_portfolio_legacy_lot_import()
            return
        if parsed.path == "/api/brain/decisions":
            self.handle_brain_decisions()
            return
        if parsed.path == "/api/ml/update":
            self.handle_ml_update()
            return
        if parsed.path == "/api/data/repair-before-scan":
            self.handle_repair_before_scan()
            return
        if parsed.path == "/api/data/repair-core-sources":
            self.handle_repair_core_sources()
            return
        if parsed.path == "/api/data/gap-repair":
            self.handle_data_gap_repair()
            return
        if parsed.path == "/api/advanced-flow/refresh":
            self.handle_advanced_flow_refresh()
            return
        if parsed.path == "/api/advanced-flow/start-tick":
            self.handle_advanced_flow_start_tick()
            return
        if parsed.path == "/api/monster-scan":
            self.handle_monster_scan()
            return
        if parsed.path == "/api/ai/theme-search":
            self.handle_ai_theme_search()
            return
        if parsed.path == "/api/trades":
            self.handle_trade_record()
            return
        if parsed.path == "/api/trades/deduplicate":
            self.handle_trades_duplicates(apply=True)
            return
        if parsed.path == "/api/strategy-signals":
            self.handle_strategy_signal_record()
            return
        if parsed.path == "/api/paper-signals/snapshot":
            self.handle_paper_signal_snapshot()
            return
        if parsed.path == "/api/sinopac/settings":
            self.handle_sinopac_save()
            return
        if parsed.path == "/api/capital/settings":
            self.handle_capital_save()
            return
        if parsed.path == "/api/capital/test":
            self.write_json(capital_backend.test_connection(), status=200)
            return
        if parsed.path == "/api/sinopac/order/preview":
            self.handle_sinopac_order_preview()
            return
        if parsed.path == "/api/sinopac/order/place":
            self.handle_sinopac_order_place()
            return
        if parsed.path == "/api/sinopac/order-fills/sync":
            self.handle_sinopac_order_fills(sync=True)
            return
        if parsed.path == "/api/sinopac/test-suite":
            self.handle_sinopac_test_suite()
            return
        self.send_response(404)
        self.end_headers()

    def do_DELETE(self):
        if self._is_cloudflare_mobile_request():
            self.send_error(403, "Forbidden (Cloudflare mobile access is read-only)")
            return
        if not self._is_trusted_request_origin():
            self.send_error(403, "Forbidden (cross-origin request rejected)")
            return
        parsed = urlparse(self.path)
        if parsed.path == "/api/settings/finmind-token":
            self.handle_token_delete()
            return
        if parsed.path == "/api/settings/ai":
            self.handle_ai_settings_delete()
            return
        if parsed.path == "/api/settings/line":
            self.handle_line_settings_delete()
            return
        if parsed.path == "/api/sinopac/settings":
            self.write_json(sinopac_backend.clear_config())
            return
        if parsed.path == "/api/capital/settings":
            self.write_json(capital_backend.clear_config())
            return
        self.send_response(404)
        self.end_headers()

    def handle_token_status(self):
        token = read_finmind_token()
        self.write_json({
            "ok": True,
            "configured": bool(token),
            "masked": f"{token[:4]}...{token[-4:]}" if len(token) >= 8 else "",
            "source": "finmind_token.txt or environment" if token else "not configured",
            "membership": finmind_membership_status(token, include_official=True),
        })

    def handle_token_save(self):
        length = int(self.headers.get("Content-Length", "0"))
        try:
            body = self.rfile.read(length).decode("utf-8")
            payload = json.loads(body or "{}")
            token = str(payload.get("token", "")).strip()
        except json.JSONDecodeError:
            self.write_json({"ok": False, "error": "Invalid JSON"}, status=400)
            return

        if len(token) < 12:
            self.write_json({"ok": False, "error": "Token looks too short"}, status=400)
            return

        _atomic_write_text(ROOT / "finmind_token.txt", token + "\n")
        cache.clear()
        self.write_json({
            "ok": True,
            "configured": True,
            "masked": f"{token[:4]}...{token[-4:]}",
            "membership": finmind_membership_status(token, include_official=True),
        })

    def handle_token_delete(self):
        token_file = ROOT / "finmind_token.txt"
        if token_file.exists():
            token_file.unlink()
        cache.clear()
        finmind_user_info_cache.update({"tokenHash": "", "fetchedAt": 0, "payload": None})
        self.write_json({"ok": True, "configured": False, "membership": finmind_membership_status(None)})

    def handle_token_test(self):
        token = read_finmind_token()
        if not token:
            self.write_json({
                "ok": False,
                "configured": False,
                "usable": False,
                "error": "FinMind Token is not configured",
            })
            return
        try:
            end_date = time.strftime("%Y-%m-%d")
            start_date = offset_date(end_date, -20)
            payload = self.fetch_finmind("2330", start_date, end_date, token)
            rows = payload.get("rows") or []
            latest = rows[-1] if rows else {}
            self.write_json({
                "ok": True,
                "configured": True,
                "usable": True,
                "source": payload.get("source"),
                "symbol": payload.get("symbol"),
                "rows": len(rows),
                "latestDate": latest.get("date"),
                "latestClose": latest.get("close"),
                "tokenMode": payload.get("tokenMode"),
                "checkedAt": time.strftime("%Y-%m-%d %H:%M:%S"),
                "membership": finmind_membership_status(token, include_official=True),
            })
        except Exception as exc:
            self.write_json({
                "ok": False,
                "configured": True,
                "usable": False,
                "error": str(exc),
                "membership": finmind_membership_status(token),
            })

    def handle_ai_settings_save(self):
        try:
            payload = self.read_json_body()
            openai_key = str(payload.get("openaiApiKey") or "").strip()
            perplexity_key = str(payload.get("perplexityApiKey") or "").strip()
            existing = read_ai_file_config()
            data = {
                "openaiApiKey": openai_key or existing.get("openaiApiKey", ""),
                "perplexityApiKey": perplexity_key or existing.get("perplexityApiKey", ""),
                "enableAi": bool(payload.get("enableAi", True)),
                "enableNews": bool(payload.get("enableNews", True)),
                "openaiModel": str(payload.get("openaiModel") or existing.get("openaiModel") or "gpt-5.4-mini").strip(),
                "perplexityModel": str(payload.get("perplexityModel") or existing.get("perplexityModel") or "sonar").strip(),
            }
            openai_error = api_key_format_error(openai_key, "OpenAI API Key", prefixes=("sk-",))
            perplexity_error = api_key_format_error(perplexity_key, "Perplexity API Key", prefixes=("pplx-",))
            if openai_error:
                raise ValueError(openai_error)
            if perplexity_error:
                raise ValueError(perplexity_error)
            _atomic_write_text(AI_CONFIG_PATH, json.dumps(data, ensure_ascii=False, indent=2))
            self.write_json(ai_config_status())
        except Exception as exc:
            self.write_json({"ok": False, "error": str(exc)}, status=400)

    def handle_ai_settings_delete(self):
        if AI_CONFIG_PATH.exists():
            AI_CONFIG_PATH.unlink()
        self.write_json(ai_config_status())

    def handle_ai_settings_test(self):
        global last_ai_settings_test_at
        with ai_settings_test_lock:
            now = time.time()
            if now - last_ai_settings_test_at < AI_SETTINGS_TEST_COOLDOWN_SECONDS:
                self.write_json({
                    "ok": False,
                    "error": "AI 連線測試剛執行過，請稍後再試",
                    "cooldown": True,
                }, status=200)
                return
            last_ai_settings_test_at = now
        config = read_ai_config()
        result = {
            "ok": True,
            "usable": False,
            "openai": self.test_openai_connection(config),
            "perplexity": self.test_perplexity_connection(config),
            "checkedAt": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        result["usable"] = bool(result["openai"].get("usable") or result["perplexity"].get("usable"))
        self.write_json(result)

    def test_openai_connection(self, config):
        if not config["enableAi"]:
            return {"configured": bool(config["openaiApiKey"]), "enabled": False, "usable": False, "message": "OpenAI analysis is disabled"}
        if not config["openaiApiKey"]:
            return {"configured": False, "enabled": True, "usable": False, "message": "OpenAI API Key is not configured"}
        key_error = api_key_format_error(config["openaiApiKey"], "OpenAI API Key", prefixes=("sk-",))
        if key_error:
            return {"configured": True, "enabled": True, "usable": False, "model": config["openaiModel"], "error": key_error}
        try:
            body = {
                "model": config["openaiModel"],
                "input": "Reply with exactly: OpenAI test ok",
                "max_output_tokens": 24,
            }
            request = Request(
                "https://api.openai.com/v1/responses",
                data=json.dumps(clean_json_payload(body), ensure_ascii=False).encode("utf-8", errors="replace"),
                headers={
                    "Authorization": f"Bearer {config['openaiApiKey']}",
                    "Content-Type": "application/json",
                },
                method="POST",
            )
            with urlopen(request, timeout=30) as response:
                payload = json.loads(response.read().decode("utf-8"))
            text = str(payload.get("output_text") or "").strip()
            if not text:
                for item in payload.get("output", []) or []:
                    for content in item.get("content", []) or []:
                        text = str(content.get("text") or "").strip()
                        if text:
                            break
                    if text:
                        break
            return {
                "configured": True,
                "enabled": True,
                "usable": bool(text),
                "model": config["openaiModel"],
                "message": text or "OpenAI returned no text",
            }
        except Exception as exc:
            return {
                "configured": True,
                "enabled": True,
                "usable": False,
                "model": config["openaiModel"],
                "error": str(exc),
            }

    def test_perplexity_connection(self, config):
        if not config["enableNews"]:
            return {"configured": bool(config["perplexityApiKey"]), "enabled": False, "usable": False, "message": "Perplexity news summary is disabled"}
        if not config["perplexityApiKey"]:
            return {"configured": False, "enabled": True, "usable": False, "message": "Perplexity API Key is not configured"}
        key_error = api_key_format_error(config["perplexityApiKey"], "Perplexity API Key", prefixes=("pplx-",))
        if key_error:
            return {"configured": True, "enabled": True, "usable": False, "model": config["perplexityModel"], "error": key_error}
        try:
            body = {
                "model": config["perplexityModel"],
                "messages": [
                    {"role": "system", "content": "Reply briefly."},
                    {"role": "user", "content": "Reply with exactly: Perplexity test ok"},
                ],
                "max_tokens": 24,
            }
            request = Request(
                "https://api.perplexity.ai/v1/sonar",
                data=json.dumps(clean_json_payload(body), ensure_ascii=False).encode("utf-8", errors="replace"),
                headers={
                    "Authorization": f"Bearer {config['perplexityApiKey']}",
                    "Content-Type": "application/json",
                },
                method="POST",
            )
            with urlopen(request, timeout=30) as response:
                payload = json.loads(response.read().decode("utf-8"))
            choices = payload.get("choices") or []
            message = choices[0].get("message") if choices else {}
            text = str((message or {}).get("content") or "").strip()
            return {
                "configured": True,
                "enabled": True,
                "usable": bool(text),
                "model": config["perplexityModel"],
                "message": text or "Perplexity returned no text",
            }
        except Exception as exc:
            return {
                "configured": True,
                "enabled": True,
                "usable": False,
                "model": config["perplexityModel"],
                "error": str(exc),
            }

    def handle_line_settings_save(self):
        try:
            payload = self.read_json_body()
            token = str(payload.get("channelAccessToken") or "").strip()
            target = str(payload.get("targetId") or "").strip()
            existing = read_line_file_config()
            data = {
                "channelAccessToken": token or existing.get("channelAccessToken", ""),
                "targetId": target or existing.get("targetId", ""),
                "enabled": bool(payload.get("enabled", True)),
            }
            if token and len(token) < 30:
                raise ValueError("LINE Channel access token looks too short")
            if target and len(target) < 8:
                raise ValueError("LINE User ID / Group ID looks too short")
            _atomic_write_text(LINE_CONFIG_PATH, json.dumps(data, ensure_ascii=False, indent=2))
            self.write_json(line_config_status())
        except Exception as exc:
            self.write_json({"ok": False, "error": str(exc)}, status=400)

    def handle_line_settings_delete(self):
        if LINE_CONFIG_PATH.exists():
            LINE_CONFIG_PATH.unlink()
        self.write_json(line_config_status())

    def handle_line_settings_test(self):
        try:
            # 測試按鈕是使用者親手按的，就算額度進入保留池也要真的送出去
            # 讓使用者確認通道可用，所以標 critical 繞過額度讓位。
            result = self.send_line_message(
                "StockAI LINE 測試成功：你的台股系統可以發送提醒。", force=True, priority="critical"
            )
            self.write_json({**result, "test": True})
        except Exception as exc:
            self.write_json({"ok": False, "sent": False, "error": str(exc)})

    def handle_line_notify(self):
        try:
            payload = self.read_json_body()
            message = str(payload.get("message") or "").strip()
            if not message:
                raise ValueError("LINE message is empty")
            # 前端只有賣出提醒鏈會打這個端點(標critical)，其他來源沒帶就當
            # normal 走額度讓位邏輯。白名單驗證避免任意字串繞過保留池。
            priority = str(payload.get("priority") or "normal")
            if priority not in ("normal", "critical"):
                priority = "normal"
            # 2026-07-09 賣出 LINE 全域靜音(跨裝置):存伺服器的旗標,任何裝置(含未重整的舊分頁)
            # 送「賣出類」LINE 都在此擋掉——這是 authoritative 關卡,不靠各瀏覽器 localStorage。
            # 只擋 category=portfolio_sell(賣出提醒鏈),測試通知/其他類型不帶此 category、不受影響。
            category = str(payload.get("category") or "")
            if category == "portfolio_sell" and _read_meta_flag(SELL_LINE_MUTED_META_KEY):
                result = {"ok": True, "sent": False, "muted": True}
                record_frontend_portfolio_sell_notification(payload, result, message, "line")
                self.write_json(result)
                return
            result = self.send_line_message(message, priority=priority)
            record_frontend_portfolio_sell_notification(payload, result, message, "line")
            self.write_json(result)
        except Exception as exc:
            self.write_json({"ok": False, "sent": False, "error": str(exc)}, status=400)

    def handle_desktop_notify(self):
        try:
            payload = self.read_json_body()
            title = str(payload.get("title") or "StockAI 提醒").strip()[:80]
            message = str(payload.get("message") or "").strip()[:900]
            if not message:
                raise ValueError("desktop notification message is empty")
            result = send_windows_desktop_notification(title, message)
            record_frontend_portfolio_sell_notification(payload, result, message, "desktop")
            self.write_json(result)
        except Exception as exc:
            self.write_json({"ok": False, "sent": False, "error": str(exc)}, status=400)

    def read_summary_cache(self):
        with portfolio_summary_cache_lock:
            return load_portfolio_summary_cache()

    def handle_summary_cache_get(self):
        # 手機純閱讀不會自己跑永豐同步，它的 localStorage 摘要/持股會停在舊值
        # (2026-07-03 實例：交割款修好後手機還顯示修復前的假可用資金)。
        # 桌面每次同步成功後把算好的摘要+持股明細 POST 上來，手機每45秒
        # 拉一次這份最新值——這就是「手機資料跟電腦即時同步」的機制。
        summary, holdings = self.read_summary_cache()
        self.write_json({
            "ok": True,
            "summary": summary,
            "holdings": holdings,
        })

    def handle_portfolio_exit_analysis(self):
        try:
            summary, holdings = self.read_summary_cache()
            if not holdings:
                snapshots = backend.list_portfolio_exit_snapshots(limit=120)
                self.write_json({
                    **snapshots,
                    "generatedAt": None,
                    "summaryUpdatedAt": None,
                    "alerts": [
                        alert for item in snapshots.get("items", [])
                        for alert in item.get("alerts", [])
                    ],
                    "source": "latest_persisted_snapshot",
                    "counts": {
                        "positions": len(snapshots.get("items", [])),
                        "actionable": sum(
                            1 for item in snapshots.get("items", [])
                            if item.get("decisionVerified") and int(item.get("sellShares") or 0) >= 1000
                        ),
                        "unknownHorizon": sum(
                            1 for item in snapshots.get("items", [])
                            if item.get("hasUnknownHorizon") is True
                            or item.get("strategyHorizon") == "unknown"
                            or any(
                                lot.get("strategyHorizon") == "unknown"
                                for lot in (item.get("lots") or [])
                                if isinstance(lot, dict)
                            )
                        ),
                        "unknownBuyDate": sum(1 for item in snapshots.get("items", []) if not item.get("positionBuyDateKnown")),
                    },
                })
                return
            result = backend.portfolio_exit_analysis(
                holdings,
                summary=summary,
                evaluation_date=scheduler_today(taipei_localtime()),
                persist=False,
            )
            result["source"] = "backend_canonical_live"
            self.write_json(result)
        except Exception as exc:
            self.write_json({"ok": False, "error": str(exc)}, status=500)

    def handle_portfolio_strategy_horizon_lock(self):
        try:
            payload = self.read_json_body()
            symbol = str(payload.get("symbol") or "").replace(".TWO", "").replace(".TW", "").strip()
            horizon = normalize_strategy_horizon(
                payload.get("strategyHorizon") or payload.get("strategy_horizon")
            )
            trade_id = safe_int(payload.get("tradeId") or payload.get("trade_id"), 0) or None
            if not symbol or horizon == "unknown":
                raise ValueError("必須提供股票代號與短期、中期或長期策略週期")

            summary, holdings = self.read_summary_cache()
            if not holdings:
                raise ValueError("目前沒有券商持股快照，請先回主頁同步永豐庫存")
            holding = None
            for raw_symbol, candidate in holdings.items():
                if not isinstance(candidate, dict):
                    continue
                candidate_symbol = str(candidate.get("code") or raw_symbol or "")
                candidate_symbol = candidate_symbol.replace(".TWO", "").replace(".TW", "").strip()
                if candidate_symbol == symbol:
                    holding = candidate
                    break
            if holding is None:
                raise ValueError("目前券商持股快照找不到這檔股票，禁止寫入策略週期")

            lock_result = backend.lock_existing_position_horizon(
                symbol, horizon, holding, trade_id=trade_id
            )
            analysis = backend.portfolio_exit_analysis(
                holdings,
                summary=summary,
                evaluation_date=scheduler_today(taipei_localtime()),
                persist=True,
            )
            item = next(
                (row for row in analysis.get("items", []) if str(row.get("symbol")) == symbol),
                None,
            )
            self.write_json({
                "ok": True,
                "lock": lock_result,
                "item": item,
                "counts": analysis.get("counts"),
                "summaryUpdatedAt": analysis.get("summaryUpdatedAt"),
            })
        except (ValueError, json.JSONDecodeError) as exc:
            self.write_json({"ok": False, "error": str(exc)}, status=400)

    def handle_portfolio_strategy_horizon_batch_lock(self):
        try:
            payload = self.read_json_body()
            mode = str(payload.get("mode") or "preview").strip().lower()
            if mode not in {"preview", "apply"}:
                raise ValueError("批次週期鎖定模式必須是 preview 或 apply")
            if mode == "apply" and payload.get("confirmAll") is not True:
                raise ValueError("整批寫入前必須明確確認全部持股週期")
            summary, holdings = self.read_summary_cache()
            if not holdings:
                raise ValueError("目前沒有券商持股快照，請先回主頁同步永豐庫存")
            result = backend.lock_existing_position_horizons(
                payload.get("assignments") or [],
                holdings,
                apply=mode == "apply",
                require_all=True,
            )
            response = {"ok": True, "batch": result}
            if mode == "apply":
                analysis = backend.portfolio_exit_analysis(
                    holdings,
                    summary=summary,
                    evaluation_date=scheduler_today(taipei_localtime()),
                    persist=True,
                )
                response.update({
                    "counts": analysis.get("counts"),
                    "items": analysis.get("items"),
                    "summaryUpdatedAt": analysis.get("summaryUpdatedAt"),
                })
            self.write_json(response)
        except (ValueError, json.JSONDecodeError) as exc:
            self.write_json({"ok": False, "error": str(exc)}, status=400)
        except Exception as exc:
            self.write_json({"ok": False, "error": str(exc)}, status=500)

    def handle_portfolio_legacy_lot_import(self):
        try:
            payload = self.read_json_body()
            symbol = str(payload.get("symbol") or "").replace(".TWO", "").replace(".TW", "").strip()
            if not symbol:
                raise ValueError("必須提供股票代號")
            summary, holdings = self.read_summary_cache()
            if not holdings:
                raise ValueError("目前沒有券商持股快照，請先回主頁同步永豐庫存")
            holding = None
            for raw_symbol, candidate in holdings.items():
                if not isinstance(candidate, dict):
                    continue
                candidate_symbol = str(candidate.get("code") or raw_symbol or "")
                candidate_symbol = candidate_symbol.replace(".TWO", "").replace(".TW", "").strip()
                if candidate_symbol == symbol:
                    holding = candidate
                    break
            if holding is None:
                raise ValueError("目前券商持股快照找不到這檔股票，禁止匯入分批 lot")

            import_result = backend.import_legacy_position_lots(
                symbol,
                payload.get("lots"),
                holding,
                note=payload.get("note"),
            )
            analysis = backend.portfolio_exit_analysis(
                holdings,
                summary=summary,
                evaluation_date=scheduler_today(taipei_localtime()),
                persist=True,
            )
            item = next(
                (row for row in analysis.get("items", []) if str(row.get("symbol")) == symbol),
                None,
            )
            self.write_json({
                "ok": True,
                "import": import_result,
                "item": item,
                "counts": analysis.get("counts"),
                "summaryUpdatedAt": analysis.get("summaryUpdatedAt"),
            })
        except (ValueError, json.JSONDecodeError) as exc:
            self.write_json({"ok": False, "error": str(exc)}, status=400)
        except Exception as exc:
            self.write_json({"ok": False, "error": str(exc)}, status=500)

    def handle_summary_cache_save(self):
        try:
            payload = self.read_json_body()
            summary = payload.get("summary")
            if not isinstance(summary, dict):
                raise ValueError("summary must be an object")
            holdings = payload.get("holdings")
            stored = {
                "summary": summary,
                "holdings": holdings if isinstance(holdings, dict) else None,
            }
            raw = json.dumps(stored, ensure_ascii=False)
            if len(raw) > 120000:
                raise ValueError("payload too large")
            with portfolio_summary_cache_lock:
                with backend.connect() as conn:
                    backend.set_meta(conn, "portfolio_summary_cache", raw)
            try:
                exit_analysis = backend.portfolio_exit_analysis(
                    stored["holdings"] or {},
                    summary=summary,
                    evaluation_date=scheduler_today(taipei_localtime()),
                    persist=True,
                )
                self.write_json({"ok": True, "exitAnalysis": exit_analysis.get("counts")})
            except Exception as exit_exc:
                self.write_json({
                    "ok": True,
                    "exitAnalysis": None,
                    "exitAnalysisError": str(exit_exc)[:200],
                })
        except Exception as exc:
            self.write_json({"ok": False, "error": str(exc)}, status=400)

    def handle_user_prefs_get(self):
        # 使用者偏好存伺服器(model_meta)而非各裝置 localStorage：使用者原則是
        # 「手機純閱讀、操作都在電腦」，電腦設定一次、跨裝置生效。
        # depositAmount=總入金、radarLotBudget=一張預算(雷達篩選+盤中推播過濾)。
        self.write_json({
            "ok": True,
            "depositAmount": _read_meta_positive_int("user_deposit_amount"),
            "radarLotBudget": _read_meta_positive_int(RADAR_LOT_BUDGET_META_KEY),
            "sellLineMuted": _read_meta_flag(SELL_LINE_MUTED_META_KEY),
        })

    def handle_user_prefs_save(self):
        # 部分更新：payload 有帶哪個 key 才更新哪個，避免只改預算時把入金歸零
        try:
            payload = self.read_json_body()
            updated = {}
            with backend.connect() as conn:
                for field, meta_key in (
                    ("depositAmount", "user_deposit_amount"),
                    ("radarLotBudget", RADAR_LOT_BUDGET_META_KEY),
                ):
                    if field not in payload:
                        continue
                    try:
                        value = max(0, int(float(payload.get(field))))
                    except (TypeError, ValueError):
                        value = 0
                    backend.set_meta(conn, meta_key, str(value))
                    updated[field] = value
                # 賣出 LINE 全域靜音是布林旗標(非正整數),另外處理:存 '1'/'0'
                if "sellLineMuted" in payload:
                    muted = bool(payload.get("sellLineMuted"))
                    backend.set_meta(conn, SELL_LINE_MUTED_META_KEY, "1" if muted else "0")
                    updated["sellLineMuted"] = muted
            self.write_json({"ok": True, **updated})
        except Exception as exc:
            self.write_json({"ok": False, "error": str(exc)}, status=400)

    def handle_exit_watch_save(self):
        # 停損守門員的資料來源+心跳：前端只轉送後端統一出場快照的防守價。
        # 只收白名單欄位並強制數值/長度界限，不讓任意 payload 塞進 model_meta。
        # monitoring 欄位(2026-07-04 新增)：使用者主動取消勾選「背景監控」時
        # 前端會顯式送一次 monitoring:false——跟「心跳斷了」是完全不同的語意
        # (使用者主動要求別再幫忙盯 vs 分頁真的斷線/關閉)，伺服器收到後直接
        # 記住這個意圖，check_portfolio_exit_guardian 看到就整個跳過，不會
        # 因為之後心跳過期而誤判成離線並接管。
        try:
            payload = self.read_json_body()
            items = []
            summary, holdings = self.read_summary_cache()
            if holdings:
                backend.portfolio_exit_analysis(
                    holdings,
                    summary=summary,
                    evaluation_date=scheduler_today(taipei_localtime()),
                    persist=True,
                )
            with backend.connect() as conn:
                snapshot_rows = conn.execute(
                    "SELECT symbol, payload_json FROM portfolio_exit_snapshots"
                ).fetchall()
            canonical_by_symbol = {}
            for symbol, raw_payload in snapshot_rows:
                try:
                    canonical = json.loads(raw_payload)
                except (TypeError, json.JSONDecodeError):
                    continue
                if isinstance(canonical, dict):
                    canonical_by_symbol[str(symbol)] = canonical
            for raw in (payload.get("items") or [])[:60]:
                code = str(raw.get("code") or "").strip()[:10]
                canonical = canonical_by_symbol.get(code)
                if not canonical or raw.get("policyVersion") != canonical.get("policyVersion"):
                    continue
                canonical_reasons = [
                    str(reason).strip()[:60]
                    for reason in (canonical.get("evidence") or [])[:8]
                    if str(reason).strip()
                ]
                if (
                    canonical.get("decisionVerified") is not True
                    or raw.get("decisionVerified") is not True
                    or canonical.get("dataReady") is not True
                    or raw.get("decisionDataReady") is not True
                    or str(raw.get("decisionType") or "") != str(canonical.get("type") or "")
                    or str(raw.get("decisionDate") or "")[:10] != str(canonical.get("decisionDate") or "")[:10]
                    or str(raw.get("decisionDataDate") or "")[:10] != str(canonical.get("dataDate") or "")[:10]
                    or [str(reason).strip()[:60] for reason in (raw.get("decisionReasons") or []) if str(reason).strip()]
                        != canonical_reasons
                ):
                    continue
                try:
                    stop = float(raw.get("stopLoss"))
                except (TypeError, ValueError):
                    continue
                if not code or not (stop > 0):
                    continue
                expected_stop = canonical.get("stopLoss") or canonical.get("trailingStop")
                try:
                    if abs(stop - float(expected_stop)) > max(0.02, abs(float(expected_stop)) * 0.0001):
                        continue
                except (TypeError, ValueError):
                    continue
                item = {"code": code, "name": str(raw.get("name") or "").strip()[:20], "stopLoss": stop}
                # confirmSell=確認賣出價(前端 = 防守價×0.99),守門員真正用它當賣出觸發線
                # (使用者要「真的要賣才提醒」)。前端沒帶或非正值就省略,守門員退回用係數推算。
                try:
                    confirm = float(raw.get("confirmSell"))
                    if confirm > 0:
                        expected_confirm = float(canonical.get("confirmSellPrice"))
                        if abs(confirm - expected_confirm) > max(0.02, abs(expected_confirm) * 0.0001):
                            continue
                        item["confirmSell"] = confirm
                except (TypeError, ValueError):
                    continue
                # absStop=絕對停損線(avgPrice 基準、不夾擠現價)。守門員用它兜底跳空/急殺;
                # 前端沒帶就省略,守門員退回用防守價推算(維持舊行為)。
                try:
                    abs_stop = float(raw.get("absStop"))
                    if abs_stop > 0:
                        expected_abs_stop = float(canonical.get("stopLoss"))
                        if abs(abs_stop - expected_abs_stop) > max(0.02, abs(expected_abs_stop) * 0.0001):
                            continue
                        item["absStop"] = abs_stop
                except (TypeError, ValueError):
                    pass
                # 離線守門員不可只看價格。同步前端已完成的型態量能賣出驗證，
                # 後端只接受布林真值、同日決策、可行動類型與至少兩項證據。
                raw_reasons = raw.get("decisionReasons")
                reasons = []
                if isinstance(raw_reasons, list):
                    for reason in raw_reasons[:8]:
                        text = str(reason or "").strip()[:60]
                        if text and text not in reasons:
                            reasons.append(text)
                item.update({
                    "decisionVerified": raw.get("decisionVerified") is True,
                    "decisionType": str(raw.get("decisionType") or "").strip()[:24],
                    "decisionReasons": reasons,
                    "decisionAt": str(raw.get("decisionAt") or "").strip()[:40],
                    "decisionDate": str(raw.get("decisionDate") or "").strip()[:10],
                    "decisionDataDate": str(raw.get("decisionDataDate") or "").strip()[:10],
                    "decisionDataReady": raw.get("decisionDataReady") is True,
                    "quoteSource": str(raw.get("quoteSource") or "").strip()[:80],
                    "policyVersion": str(raw.get("policyVersion") or "").strip()[:40],
                })
                items.append(item)
            state = {
                "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                "tsEpoch": time.time(),
                "items": items,
                "monitoring": payload.get("monitoring") is not False,
            }
            with backend.connect() as conn:
                backend.set_meta(conn, EXIT_WATCH_STATE_KEY, json.dumps(state, ensure_ascii=False))
            self.write_json({"ok": True, "count": len(items)})
        except Exception as exc:
            self.write_json({"ok": False, "error": str(exc)}, status=400)

    def handle_exit_watch_notified_get(self):
        # 2026-07-04 稽核修復：前端(localStorage exitAlertLog)跟伺服器
        # (model_meta EXIT_GUARDIAN_NOTIFY_STATE_KEY)各自維護獨立的去重狀態，
        # 使用者把背景分頁切回前景時 visibilitychange 補跑一次本機判斷，完全
        # 看不到伺服器背景這段時間可能已經接管並發出的 critical LINE，會對
        # 同一次跌破再通知一次。前端補跑前先查這支端點，把伺服器已通知的
        # 代碼直接寫進本機去重紀錄即可避免重複。
        today = scheduler_today(taipei_localtime())
        notify_state = _read_exit_guardian_notify_state(today)
        self.write_json({"ok": True, "date": today, "symbols": notify_state["symbols"]})

    def send_line_message(self, message, force=False, priority="normal"):
        return send_line_message_via_api(message, force=force, priority=priority)

    def read_json_body(self):
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0:
            return {}
        return json.loads(self.rfile.read(length).decode("utf-8") or "{}")

    def handle_ml_update(self):
        try:
            payload = self.read_json_body()
            requested_symbols = [
                str(symbol).replace(".TWO", "").replace(".TW", "").strip()
                for symbol in (payload.get("symbols") or [])
                if str(symbol).strip()
            ]
            requested_symbols = list(dict.fromkeys(requested_symbols))
            model_symbols = []
            try:
                current_model = backend.load_model()
                model_symbols = [
                    str(symbol).replace(".TWO", "").replace(".TW", "").strip()
                    for symbol in (current_model.get("symbols") or [])
                    if str(symbol).strip()
                ]
                model_symbols = list(dict.fromkeys(model_symbols))
            except Exception:
                model_symbols = []
            base_symbols = requested_symbols or model_symbols or DEFAULT_SYMBOLS
            symbols = normalize_training_symbols(base_symbols)
            if model_symbols and len(base_symbols) < max(20, int(len(model_symbols) * 0.5)):
                symbols = normalize_training_symbols(model_symbols)
            # forceRefresh=true：忽略當日快取，強制重抓 FinMind 後再重訓，
            # 給前端「立即重訓」之類的按鈕用，不用等隔天快取自然過期。
            force_refresh = bool(payload.get("forceRefresh"))
            # W7 修復：嘗試取得 daily_update_lock（非阻塞），
            # 若背景排程正在執行就拒絕，避免兩邊同時寫 DB。
            acquired = daily_update_lock.acquire(blocking=False)
            if not acquired:
                self.write_json({"ok": False, "error": "每日更新正在執行中，請稍後再試"}, status=409)
                return

            def _bg_update():
                global daily_update_running
                # 這條「立即重訓」手動路徑直接呼叫 backend.full_daily_update()，
                # 完全繞過 daily_update.py 的 run()，原本不會更新
                # last_daily_job_status/last_daily_job_at——如果使用者是在
                # 「今日每日更新失敗，只觀察」的提示下點這顆按鈕手動補救，
                # 就算這次手動重訓完全成功，畫面還是會繼續顯示失敗/過期狀態
                # (data_health()/latest_daily_update_health() 讀到的還是舊
                # 排程留下的 meta)，沒有任何提示這其實已經修好了。這裡補上
                # 跟排程路徑一致的 set_daily_meta() 呼叫，讓手動重訓也能
                # 正確解除 observe_only 鎖定。
                meta_payload = {"symbols": symbols, "source": "manual_ml_update", "trainingSymbols": symbols}
                try:
                    daily_update_running = True
                    backend.full_daily_update(symbols, force_refresh=force_refresh)
                    cache.clear()
                    try:
                        set_daily_meta("success", meta_payload, "")
                    except Exception as meta_exc:
                        print(f"[ml/update background] meta write failed: {meta_exc}")
                except Exception as exc:
                    print(f"[ml/update background] error: {exc}")
                    meta_payload["error"] = str(exc)
                    try:
                        set_daily_meta("failed", meta_payload, "")
                    except Exception as meta_exc:
                        print(f"[ml/update background] meta write failed: {meta_exc}")
                finally:
                    daily_update_running = False
                    daily_update_lock.release()

            try:
                t = threading.Thread(target=_bg_update, daemon=True)
                t.start()
            except Exception as thread_exc:
                # 執行緒建立/啟動失敗(資源耗盡等)時 _bg_update 的 finally 不會執行,
                # daily_update_lock 會永久洩漏、死鎖每日排程與後續所有更新。這裡補釋放並回報。
                daily_update_lock.release()
                self.write_json({"ok": False, "error": f"無法啟動重訓：{thread_exc}"}, status=500)
                return
            self.write_json({
                "ok": True,
                "message": "重訓已在背景啟動",
                "requestedSymbolCount": len(requested_symbols),
                "updatedSymbolCount": len(symbols),
            })
        except Exception as exc:
            self.write_json({"ok": False, "error": str(exc)}, status=500)

    def handle_ml_predict(self, parsed):
        query = parse_qs(parsed.query)
        symbol = (
            query.get("symbol", ["2330"])[0]
            .replace(".TW", "")
            .replace(".TWO", "")
            .strip()
        )
        repair = str((query.get("repair") or ["0"])[0]).lower() in {"1", "true", "yes"}
        if repair and not self._is_trusted_request_origin():
            self.send_error(403, "Forbidden (cross-origin request rejected)")
            return
        try:
            self.write_json({"ok": True, "prediction": backend.predict_symbol(symbol, save=True, repair=repair)})
        except Exception as exc:
            self.write_json({"ok": False, "error": str(exc)}, status=500)

    def handle_ml_predictions(self, parsed):
        query = parse_qs(parsed.query)
        limit = safe_int(query.get("limit", ["80"])[0], 80)
        self.write_json({"ok": True, "predictions": backend.list_predictions(limit)})

    def handle_model_data_check(self, parsed):
        query = parse_qs(parsed.query)
        raw_symbol = query.get("symbol", [""])[0]
        symbol = "".join(char for char in str(raw_symbol or "") if char.isdigit())[:4]
        if not symbol:
            self.write_json({"ok": False, "error": "缺少股票代號"})
            return
        try:
            rows = backend.rows_with_verified_sources(backend.load_price_rows(symbol))
            if not rows:
                self.write_json({
                    "ok": False,
                    "symbol": symbol,
                    "error": "沒有已驗證真實資料，模型不可使用。",
                })
                return
            latest = rows[-1]
            price_source = str(latest.get("price_source") or "").strip()
            source_text = "Yahoo fallback" if "yahoo" in price_source.lower() else price_source

            def has_value(key):
                value = latest.get(key)
                return value is not None and value != ""

            def item(label, key, source_key, required=False, value_key=None):
                target_key = value_key or key
                value = latest.get(target_key)
                source = str(latest.get(source_key) or "").strip()
                if not has_value(target_key):
                    status = "missing-required" if required else "missing"
                    status_text = "缺必要資料" if required else "無資料"
                elif not source:
                    status = "missing-source" if required else "missing"
                    status_text = "缺來源標記" if required else "無資料"
                elif "simulate" in source.lower() or "proxy" in source.lower():
                    status = "invalid"
                    status_text = "不可用"
                elif "yahoo" in source.lower():
                    status = "fallback"
                    status_text = "Yahoo fallback，不可用於正式模型"
                    source = "Yahoo fallback"
                elif not is_official_source(source):
                    status = "non-official"
                    status_text = "非正式來源，不可用於正式模型"
                else:
                    status = "ok"
                    status_text = "已驗證正式/授權來源"
                advanced_keys = {
                    "broker_branch_net_buy",
                    "main_force_buy_sell",
                    "realtime_money_flow",
                    "realtime_large_order_flow",
                }
                if key in advanced_keys and status in {"missing", "missing-source"}:
                    status_text = "未接正式來源，未納入模型"
                return {
                    "label": label,
                    "key": key,
                    "value": value,
                    "source": source or "無資料",
                    "required": required,
                    "status": status,
                    "statusText": status_text,
                }

            items = [
                item("收盤價", "close", "price_source", required=True),
                item("成交量", "volume", "price_source", required=True),
                item("外資買賣超", "foreign_buy_sell", "chip_source"),
                item("投信買賣超", "trust_buy_sell", "chip_source"),
                item("融資餘額", "margin_balance", "margin_source"),
                item("融券餘額", "short_balance", "margin_source"),
                item("主力分點", "broker_branch_net_buy", "branch_flow_source"),
                item("主力淨買賣", "main_force_buy_sell", "branch_flow_source"),
                item("即時資金流向", "realtime_money_flow", "realtime_flow_source"),
                item("即時大單流向", "realtime_large_order_flow", "realtime_flow_source"),
                item("月營收", "monthly_revenue", "revenue_source"),
                item("營收年增率", "revenue_growth", "revenue_source"),
                item("PER", "per", "valuation_source"),
                item("PBR", "pbr", "valuation_source"),
                item("毛利率", "gross_margin", "financial_statement_source"),
                item("財務來源", "finance_source", "finance_source", value_key="finance_source"),
            ]
            required_bad = [entry for entry in items if entry["required"] and entry["status"] != "ok"]
            optional_missing = [entry["label"] for entry in items if not entry["required"] and entry["status"] in ("missing", "missing-source")]
            advanced_keys = {
                "broker_branch_net_buy",
                "main_force_buy_sell",
                "realtime_money_flow",
                "realtime_large_order_flow",
            }
            advanced_missing = [
                entry["label"] for entry in items
                if entry["key"] in advanced_keys and entry["status"] in ("missing", "missing-source")
            ]
            core_optional_missing = [
                entry["label"] for entry in items
                if entry["key"] not in advanced_keys and not entry["required"] and entry["status"] in ("missing", "missing-source")
            ]
            can_use_model = not required_bad
            summary = "核心模型可用；進階資金流尚未接正式來源，未納入加權。" if can_use_model and advanced_missing else (
                "核心模型可用；進階資金流也有正式來源。" if can_use_model else "模型不可使用，缺少必要正式價量資料。"
            )
            self.write_json({
                "ok": True,
                "symbol": symbol,
                "date": latest.get("date"),
                "canUseModel": can_use_model,
                "priceSource": source_text or "無資料",
                "summary": summary,
                "missing": optional_missing,
                "coreMissing": core_optional_missing,
                "advancedMissing": advanced_missing,
                "items": items,
            })
        except Exception as exc:
            self.write_json({"ok": False, "symbol": symbol, "error": str(exc)})

    def handle_brain_decision(self, parsed):
        query = parse_qs(parsed.query)
        raw_symbol = query.get("symbol", [""])[0]
        symbol = "".join(char for char in str(raw_symbol or "") if char.isdigit())[:4]
        context = str(query.get("context", ["analysis"])[0] or "analysis").strip()[:40]
        if not symbol:
            self.write_json({"ok": False, "error": "缺少股票代號"}, status=400)
            return
        try:
            payload = build_brain_decision(
                symbol, context=context,
                intraday_setup=monster_intraday_setup_for_brain(symbol, context),
            )
            self.write_json(payload, status=200 if payload.get("ok") else 400)
        except Exception as exc:
            self.write_json({
                "ok": False,
                "symbol": symbol,
                "context": context,
                "error": str(exc),
                "recommendation": "只觀察",
                "observeOnly": True,
                "canNotify": False,
            }, status=500)

    def handle_brain_decisions(self):
        try:
            payload = self.read_json_body()
        except Exception as exc:
            self.write_json({"ok": False, "error": f"Invalid JSON: {exc}"}, status=400)
            return
        context = str(payload.get("context") or "analysis").strip()[:40]
        raw_symbols = payload.get("symbols") or []
        if isinstance(raw_symbols, str):
            raw_symbols = raw_symbols.replace("，", ",").split(",")
        max_symbols = min(max(safe_int(payload.get("maxSymbols") or 40, 40), 1), 120)
        symbols = []
        for raw in raw_symbols:
            symbol = "".join(char for char in str(raw or "") if char.isdigit())[:4]
            if symbol and symbol not in symbols:
                symbols.append(symbol)
        symbols = symbols[:max_symbols]
        if not symbols:
            self.write_json({"ok": False, "error": "缺少股票代號", "decisions": []}, status=400)
            return

        decisions = []
        failed = []
        for symbol in symbols:
            try:
                decision = build_brain_decision(
                    symbol, context=context,
                    intraday_setup=monster_intraday_setup_for_brain(symbol, context),
                )
            except Exception as exc:
                decision = {
                    "ok": False,
                    "symbol": symbol,
                    "context": context,
                    "error": str(exc),
                    "recommendation": "只觀察",
                    "observeOnly": True,
                    "canNotify": False,
                }
            if not decision.get("ok"):
                failed.append(f"{symbol}: {decision.get('error') or 'Brain Engine 讀取失敗'}")
            decisions.append(decision)

        self.write_json({
            "ok": True,
            "context": context,
            "count": len(decisions),
            "failed": failed,
            "decisions": decisions,
        })

    def handle_repair_core_sources(self):
        try:
            payload = self.read_json_body()
            explicit_symbols = payload.get("symbols") or []
            scope = payload.get("scope") or "holdings"
            max_symbols = int(payload.get("maxSymbols") or 24)
            symbol_scope = advanced_flow_symbol_scope(scope, explicit_symbols, max_symbols)
            symbols = symbol_scope["symbols"]
            counts = backend.update_prices(
                symbols,
                refresh_info=False,
                include_extended=False,
                force_refresh=bool(payload.get("forceRefresh", True)),
            )
            cache.clear()

            required = [
                ("foreign_buy_sell", "外資"),
                ("trust_buy_sell", "投信"),
                ("margin_balance", "融資"),
                ("short_balance", "融券"),
                ("monthly_revenue", "月營收"),
                ("revenue_growth", "營收年增率"),
                ("per", "PER"),
                ("pbr", "PBR"),
                ("gross_margin", "毛利率"),
            ]
            details = []
            for symbol in symbols:
                rows = backend.rows_with_verified_sources(backend.load_price_rows(symbol))
                latest = rows[-1] if rows else {}
                missing = [
                    label for key, label in required
                    if latest.get(key) is None or latest.get(key) == ""
                ]
                details.append({
                    "symbol": symbol,
                    "date": latest.get("date"),
                    "updatedRows": counts.get(symbol, 0),
                    "complete": not missing,
                    "missing": missing,
                    "priceSource": latest.get("price_source"),
                    "chipSource": latest.get("chip_source"),
                    "marginSource": latest.get("margin_source"),
                    "revenueSource": latest.get("revenue_source"),
                    "valuationSource": latest.get("valuation_source"),
                    "financialStatementSource": latest.get("financial_statement_source"),
                })
            self.write_json({
                "ok": True,
                "scope": symbol_scope["scope"],
                "symbols": symbols,
                "symbolCount": len(symbols),
                "sources": symbol_scope["sources"],
                "scopeErrors": symbol_scope["errors"],
                "updatedRows": counts,
                "details": details,
                "completeCount": sum(1 for item in details if item["complete"]),
                "message": "已補齊核心正式資料；缺資料者維持無資料，不用推估值替代。",
            })
        except Exception as exc:
            self.write_json({"ok": False, "error": str(exc)}, status=500)

    def handle_data_gap_repair(self):
        try:
            payload = self.read_json_body()
            max_symbols = int(payload.get("maxSymbols") or 120)
            trigger = str(payload.get("trigger") or "manual").strip()[:40] or "manual"
            result = run_data_gap_repair(
                max_symbols=max_symbols,
                trigger=trigger,
                force_refresh=bool(payload.get("forceRefresh", True)),
                include_extended=bool(payload.get("includeExtended", False)),
            )
            status = 409 if result.get("busy") or (result.get("running") and not result.get("checked")) else 200
            self.write_json(result, status=status)
        except Exception as exc:
            self.write_json({"ok": False, "error": str(exc)}, status=500)

    def handle_monster_scores(self, parsed):
        query = parse_qs(parsed.query)
        limit = safe_int(query.get("limit", ["80"])[0], 80)
        self.write_json(backend.list_monster_scores(limit))

    def perplexity_text(self, prompt):
        config = read_ai_config()
        if not config["enableNews"]:
            raise RuntimeError("新聞摘要尚未啟用")
        if not config["perplexityApiKey"]:
            raise RuntimeError("尚未設定 Perplexity API Key")
        body = {
            "model": config["perplexityModel"],
            "messages": [
                {
                    "role": "system",
                    "content": "你是台股新聞與產業題材摘要助理。請用繁體中文，重點列出近期消息、利多利空、是否有題材支撐。不要給保證獲利建議。"
                },
                {"role": "user", "content": prompt}
            ],
            # 這是每次呼叫都真實計費的路徑(手動掃描妖股時觸發)，只需要條列式
            # 短摘要，沒設上限的話回應長度(尤其換成sonar-pro等較貴模型時)
            # 不受控，費用可能不可預期地暴漲。
            "max_tokens": 1000,
        }
        request = Request(
            "https://api.perplexity.ai/v1/sonar",
            data=json.dumps(clean_json_payload(body), ensure_ascii=False).encode("utf-8", errors="replace"),
            headers={
                "Authorization": f"Bearer {config['perplexityApiKey']}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urlopen(request, timeout=60) as response:
            payload = json.loads(response.read().decode("utf-8"))
        choices = payload.get("choices") or []
        if choices:
            message = choices[0].get("message") or {}
            content = message.get("content")
            if content:
                return str(content).strip()
        raise RuntimeError("Perplexity 沒有回傳新聞摘要")

    def handle_ai_theme_search(self):
        # 主動題材股搜尋：只在使用者按「手動掃描短線妖股」時順便觸發一次，
        # 不進排程、不進候選池/評分——結果純粹是給人看的參考清單，避免
        # Perplexity 可能的幻覺/過時資訊直接影響買賣判斷（跟量化類股輪動
        # compute_sector_momentum 是兩條獨立路徑，互不覆寫）。
        global last_theme_search_at
        with theme_search_lock:
            now = time.time()
            if now - last_theme_search_at < THEME_SEARCH_COOLDOWN_SECONDS:
                self.write_json({
                    "ok": False,
                    "error": "題材搜尋剛執行過，請稍後再試",
                    "cooldown": True,
                }, status=200)
                return
            last_theme_search_at = now
        try:
            with backend.connect() as conn:
                meta = {row[0]: row[1] for row in conn.execute(
                    "SELECT key, value FROM model_meta WHERE key IN (?, ?)",
                    ("last_monster_hot_sectors", "last_monster_scan_at"),
                ).fetchall()}
            try:
                hot_sectors = json.loads(meta.get("last_monster_hot_sectors") or "[]")
            except (TypeError, ValueError):
                hot_sectors = []
            # hot_sectors 來自 FinMind 官方股票產業分類統計，目前是受控的
            # 固定枚舉值，沒有現成的使用者可操控注入路徑。但這是外部資料
            # 流進送給第三方(Perplexity) API 的 prompt 的邊界，加一層數量/
            # 長度上限做防禦性加固——就算未來上游資料異常混入超長或大量
            # 字串，也不會讓prompt長度/費用不受控。
            hot_sectors = [str(s)[:20] for s in hot_sectors[:6]]
            grounding = (
                f"系統今天用價量資料統計出目前相對大盤明顯轉強的產業：{'、'.join(hot_sectors)}。"
                if hot_sectors else "系統今天尚未偵測到明顯轉強的產業。"
            )
            prompt = (
                f"{grounding}\n"
                "請搜尋近期（近1-2週）台股市場上正在輪動的短線題材主線（例如AI伺服器鏈、記憶體、"
                "矽光子/光通訊、低軌衛星、機器人自動化、先進封裝、電子紙等，不限於上面統計到的產業），"
                "列出目前市場最關注、還在輪動中的具體個股。請用條列式回答，每個題材主線列 3-6 檔代表股"
                "（股票名稱+代號），每檔附一句話理由（例如法人買超、剛突破整理區、成交量放大等）。"
                "只列目前市場話題與代表股清單，不要給保證獲利的建議。"
            )
            text = self.perplexity_text(prompt)
            self.write_json({
                "ok": True,
                "text": text,
                "groundedHotSectors": hot_sectors,
                "generatedAt": time.strftime("%Y-%m-%d %H:%M:%S"),
            })
        except Exception as exc:
            self.write_json({"ok": False, "error": str(exc)}, status=200)

    def handle_monster_scan_status(self):
        payload = restore_monster_scan_status()
        payload["ok"] = True
        self.write_json(payload)

    def handle_repair_before_scan(self):
        # W6 修復：repair_before_scan 最多掃 300 檔，可能耗時數分鐘甚至更長，
        # 在 HTTP handler 同步執行會讓瀏覽器逾時並失去回傳值。
        # 改為立刻回傳 {"started": true}，實際工作在背景執行緒完成。
        try:
            payload = self.read_json_body()
            symbols = payload.get("symbols")
            if symbols:
                symbols = [str(symbol).replace(".TWO", "").replace(".TW", "").strip() for symbol in symbols]
            max_repair = int(payload.get("maxRepair") or 300)

            def _run():
                try:
                    backend.sync_official_daily_snapshot(symbols=symbols)
                    backend.repair_before_scan(symbols=symbols, max_repair=max_repair)
                    cache.clear()
                except Exception as exc:
                    print(f"repair_before_scan background error: {exc}")

            threading.Thread(target=_run, daemon=True).start()
            self.write_json({"ok": True, "started": True, "maxRepair": max_repair,
                             "message": "修復已在背景啟動，請稍後重新整理頁面查看結果"})
        except Exception as exc:
            self.write_json({"ok": False, "error": str(exc)}, status=500)

    def handle_monster_scan(self):
        try:
            payload = self.read_json_body()
            symbols = payload.get("symbols")
            self.write_json(start_monster_scan_job(
                symbols=symbols,
                limit=payload.get("limit") or 300,
                score_limit=payload.get("scoreLimit") or payload.get("modelLimit") or 100,
                trigger="manual"
            ))
        except Exception as exc:
            self.write_json({"ok": False, "error": str(exc)}, status=500)

    def handle_trade_record(self):
        try:
            payload = self.read_json_body()
            self.write_json(backend.record_trade(payload))
        except Exception as exc:
            self.write_json({"ok": False, "error": str(exc)}, status=400)

    def handle_trades_list(self, parsed):
        query = parse_qs(parsed.query)
        limit = safe_int(query.get("limit", ["80"])[0], 80)
        include_paper = str(query.get("includePaper", ["0"])[0]).lower() in {"1", "true", "yes"}
        self.write_json({"ok": True, "trades": backend.list_trades(limit, include_paper=include_paper)})

    def handle_trades_duplicates(self, apply=False):
        try:
            self.write_json(backend.trade_duplicate_groups(apply=apply))
        except Exception as exc:
            self.write_json({"ok": False, "error": str(exc)}, status=500)

    def handle_strategy_signal_record(self):
        try:
            payload = self.read_json_body()
            self.write_json(backend.record_strategy_signals(payload))
        except Exception as exc:
            self.write_json({"ok": False, "error": str(exc)}, status=400)

    def handle_paper_signal_snapshot(self):
        try:
            payload = self.read_json_body()
        except Exception:
            payload = {}
        try:
            max_symbols = int(payload.get("maxSymbols") or 160)
            include_holdings = str(payload.get("includeHoldings", "1")).lower() not in {"0", "false", "no"}
            session = str(payload.get("session") or "manual").strip() or "manual"
            session_key, _session_meta = paper_signal_session_meta(session)
            result = record_paper_signal_snapshot(max_symbols=max_symbols, include_holdings=include_holdings, session=session_key)
            record_paper_signal_snapshot_meta(result, session_key)
            if session_key in PAPER_SIGNAL_SESSION_JOBS:
                job_id = PAPER_SIGNAL_SESSION_JOBS[session_key]
                mark_auto_schedule(
                    job_id,
                    "success",
                    f"手動補跑紙上快照：已存 {result.get('saved', 0)} 筆，檢查 {result.get('checked', 0)} 檔",
                )
            self.write_json(result)
        except Exception as exc:
            self.write_json({"ok": False, "error": str(exc)}, status=500)

    def handle_strategy_signal_stats(self, parsed):
        query = parse_qs(parsed.query)
        strategy = (query.get("strategy", [""])[0] or "").strip() or None
        scope = (query.get("scope", [""])[0] or "").strip().lower()
        # 網頁讀取戰績時只讀已結算結果；收盤排程負責更新。需要人工補算時才
        # 明確帶 refresh=1，避免每次開頁都同步逐筆重算而讓 HTTP 請求逾時。
        refresh = str(query.get("refresh", ["0"])[0]).lower() in {"1", "true", "yes"}
        strategy_prefix = "model_" if scope == "model" and not strategy else None
        payload = backend.strategy_signal_performance(
            strategy,
            refresh_outcomes=refresh,
            strategy_prefix=strategy_prefix,
        )
        payload["scope"] = scope or "all"
        # 舊的多時段快照是 Brain/妖股規則訊號，不冒充獨立模型排程。
        payload["snapshotSchedule"] = [] if scope == "model" else paper_signal_snapshot_schedule_status()
        self.write_json(payload)

    def handle_strategy_calibration(self, parsed):
        query = parse_qs(parsed.query)
        refresh = str(query.get("refresh", ["0"])[0]).lower() in {"1", "true", "yes"}
        if refresh:
            today = scheduler_today(taipei_localtime())
            payload = backend.save_strategy_calibration_suggestions(
                calibration_date=today,
                min_samples=safe_int(query.get("minSamples", ["20"])[0], 20),
            )
            record_strategy_calibration_meta(payload, today)
        else:
            payload = {"ok": True, "mode": "observation"}
        payload["records"] = backend.list_strategy_calibration(
            safe_int(query.get("limit", ["80"])[0], 80)
        )
        self.write_json(payload)

    def handle_sinopac_save(self):
        try:
            payload = self.read_json_body()
            self.write_json(sinopac_backend.save_config(payload))
        except Exception as exc:
            self.write_json({"ok": False, "error": str(exc)}, status=400)

    def handle_capital_save(self):
        try:
            payload = self.read_json_body()
            self.write_json(capital_backend.save_config(payload))
        except Exception as exc:
            self.write_json({"ok": False, "error": str(exc)}, status=400)

    def handle_sinopac_holdings(self):
        # 交易複盤自動化(2026-07-09):每天第一次同步庫存時,搭「同一次永豐登入」的便車順便抓已實現
        # 損益存進 sinopac_realized_pnl(複盤卡資料源=你以後每筆成交都自動記錄比對雷達)。永豐短時間
        # 重複登入會被拒(400),所以絕不另開登入,而是像 quotes_direct 帶大盤 snapshot 那樣併進 holdings
        # 這次登入。once/day 靠 meta 記日期。已實現損益抓取全程隔離(子行程+這裡雙層 try),壞掉也絕不
        # 影響庫存回傳(庫存=停損監控命脈)。
        today = datetime.now(TAIPEI_TZ).strftime("%Y-%m-%d")
        want_realized = False
        try:
            with backend.connect() as conn:
                row = conn.execute(
                    "SELECT value FROM model_meta WHERE key = ?", (SINOPAC_REALIZED_LAST_IMPORT_KEY,)
                ).fetchone()
            want_realized = (not row) or (str(row[0]).strip() != today)
        except Exception:
            want_realized = False
        try:
            payload = sinopac_backend.holdings(include_realized_days=180 if want_realized else None)
        except Exception as exc:
            self.write_json({"ok": False, "error": str(exc)}, status=400)
            return
        # 若這次帶回已實現損益(每天第一次),存檔並標記今天已匯入;整段失敗絕不影響庫存回傳。
        if isinstance(payload, dict):
            records = payload.pop("realizedRecords", None)  # 不論如何都別把大包 records 塞回前端
            if want_realized and isinstance(records, list):
                try:
                    saved = backend.save_realized_pnl(records)
                    with backend.connect() as conn:
                        backend.set_meta(conn, SINOPAC_REALIZED_LAST_IMPORT_KEY, today)
                    payload["realizedSaved"] = saved
                except Exception:
                    pass
        self.write_json(payload)

    def handle_sinopac_realized_pnl(self):
        # 交易複盤【探測版】:抓永豐已實現損益(list_profit_loss),回傳原始樣本+count,先不寫 DB。
        # 唯讀、絕不下單。等看到真實欄位長相、確認 shape 後,下一步才把它映射寫進 trades 表做複盤比對。
        try:
            query = parse_qs(urlparse(self.path).query)
            days = int((query.get("days") or ["180"])[0])
        except Exception:
            days = 180
        days = max(1, min(days, 365))
        import datetime as _dt
        end = _dt.date.today()
        begin = end - _dt.timedelta(days=days)
        try:
            payload = sinopac_backend.realized_pnl(begin.isoformat(), end.isoformat())
        except Exception as exc:
            self.write_json({"ok": False, "error": str(exc)[:300], "count": 0})
            return
        # 把完整原始記錄寫進探測檔(留存除錯用),UI 只回前 20 筆樣本
        try:
            with open(ROOT / "realized_pnl_probe.json", "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
        except Exception:
            pass
        # 存進專用表 sinopac_realized_pnl(用 seqno 去重、不污染手動 trades 表),交易複盤用
        try:
            if isinstance(payload, dict) and payload.get("ok"):
                payload["saved"] = backend.save_realized_pnl(payload.get("records") or [])
        except Exception as exc:
            payload["saveError"] = str(exc)[:200]
        # 只回前 20 筆原始樣本 + count(整包可能很大)
        if isinstance(payload, dict) and isinstance(payload.get("records"), list):
            payload["sample"] = payload["records"][:20]
            payload["records"] = None
        self.write_json(payload)

    def handle_sinopac_order_preview(self):
        try:
            payload = self.read_json_body()
            self.write_json(sinopac_backend.order_preview(payload))
        except Exception as exc:
            self.write_json({"ok": False, "error": str(exc)}, status=400)

    def handle_sinopac_order_place(self):
        try:
            payload = self.read_json_body()
            result = sinopac_backend.place_order(payload)
            if isinstance(result, dict):
                order = result.get("order") if isinstance(result.get("order"), dict) else {}
                action = str(order.get("action") or payload.get("action") or "").upper()
                if action in {"BUY", "SELL"}:
                    price = float(order.get("price") or payload.get("price") or 0)
                    shares = int(order.get("shares") or 0)
                    if price > 0 and shares > 0:
                        refs = sinopac_backend.order_refs_from_trade_payload(result.get("trade") or {})
                        try:
                            record_payload = {
                                "symbol": order.get("code") or payload.get("symbol"),
                                "side": action,
                                "price": price,
                                "shares": shares,
                                "signal": "sinopac_order",
                                "status": "paper" if result.get("simulation") else "submitted",
                                "note": f"永豐 {action} 委託送出後自動記錄；實際成交資訊待成交回報校正",
                                **refs,
                            }
                            if action == "BUY":
                                classification = classify_sinopac_buy_strategy_horizon(payload, order)
                                strategy_horizon = classification["strategyHorizon"]
                                record_payload["strategyHorizon"] = strategy_horizon
                                record_payload["strategyHorizonSource"] = classification["strategyHorizonSource"]
                                if strategy_horizon == "short_trade":
                                    record_payload["note"] += "；來源已核對為當日妖股雷達，成交後鎖定短期策略"
                                elif strategy_horizon == "long_trend":
                                    record_payload["note"] += "；來源有明確存股／定期定額證據，成交後鎖定長期策略"
                                else:
                                    record_payload["note"] += "；策略週期無可驗證來源，不啟用時間出場"
                            local = backend.record_trade(record_payload)
                            result["localTradeRecorded"] = True
                            result["localTradeId"] = local.get("id")
                            result["localTradeBrokerRefs"] = refs
                            if action == "BUY":
                                result["localTradeStrategyHorizon"] = record_payload["strategyHorizon"]
                                result["localTradeStrategyHorizonSource"] = record_payload["strategyHorizonSource"]
                                result["localTradeStrategyEvidence"] = classification.get("evidence")
                        except Exception as trade_exc:
                            result["localTradeRecorded"] = False
                            result["localTradeError"] = str(trade_exc)[:200]
                    else:
                        result["localTradeRecorded"] = False
                        result["localTradeError"] = f"市價或未知價格 {action} 委託未寫入 trades；需等成交回報取得實際價格"
            self.write_json(result)
        except Exception as exc:
            self.write_json({"ok": False, "error": str(exc)}, status=400)

    def handle_sinopac_order_fills(self, sync=False):
        try:
            payload = sinopac_backend.order_fills()
            if sync:
                payload["sync"] = backend.sync_sinopac_order_fills(payload.get("fills") or [])
                payload["strategyHorizonEvidenceBackfill"] = (
                    backend.backfill_strategy_horizons_from_execution_evidence(apply=True)
                )
            self.write_json(payload)
        except Exception as exc:
            self.write_json({"ok": False, "error": str(exc)}, status=400)

    def handle_sinopac_test_suite(self):
        try:
            payload = self.read_json_body()
            self.write_json(sinopac_backend.test_suite(payload))
        except Exception as exc:
            self.write_json({"ok": False, "error": str(exc)}, status=400)

    def handle_advanced_flow_refresh(self):
        try:
            payload = self.read_json_body()
            scope = payload.get("scope") or "holdings"
            max_symbols = int(payload.get("maxSymbols") or 24)
            explicit_symbols = payload.get("symbols") or []
            symbol_scope = advanced_flow_symbol_scope(scope, explicit_symbols, max_symbols)
            symbols = symbol_scope["symbols"]
            force_refresh = bool(payload.get("forceRefresh", True))
            counts = backend.update_prices(
                symbols,
                refresh_info=False,
                include_extended=True,
                force_refresh=force_refresh,
            )
            cache.clear()
            status = advanced_flow_status(symbols)
            self.write_json({
                "ok": True,
                "scope": symbol_scope["scope"],
                "symbols": symbols,
                "symbolCount": len(symbols),
                "sources": symbol_scope["sources"],
                "scopeErrors": symbol_scope["errors"],
                "updatedRows": counts,
                "status": status,
                "message": (
                    "已補齊進階資金流資料；沒有正式來源的欄位仍會顯示無資料，不會用推估值替代。"
                ),
            })
        except Exception as exc:
            self.write_json({"ok": False, "error": str(exc)}, status=500)

    def handle_advanced_flow_start_tick(self):
        try:
            payload = self.read_json_body()
        except Exception as exc:
            self.write_json({"ok": False, "error": f"Invalid JSON: {exc}"}, status=400)
            return
        result = start_intraday_tick_collector(payload.get("trigger") or "manual-api")
        self.write_json(result, status=200 if result.get("ok") else 400)

    def handle_stock_info(self, parsed):
        query = parse_qs(parsed.query)
        codes = [
            "".join(ch for ch in code if ch.isdigit())
            for value in query.get("codes", [""])
            for code in value.split(",")
        ]
        codes = sorted({code for code in codes if code})
        local_info = self.fetch_local_stock_info(codes)
        if codes and len(local_info) == len(codes):
            self.write_json({
                "ok": True,
                "source": "local stock_info table",
                "stocks": local_info,
            })
            return
        token = read_finmind_token()
        token_mode = "token" if token else "no-token"
        cache_key = ("stock-info", token_mode)
        now = time.time()
        cached = cache.get(cache_key)
        try:
            if cached and now - cached["time"] < 24 * 60 * 60:
                info = cached["payload"]
            else:
                info = self.fetch_finmind_stock_info(token)
                cache[cache_key] = {"time": now, "payload": info}
            payload = {
                "ok": True,
                "source": "FinMind TaiwanStockInfo",
                "stocks": {
                    code: (info.get(code) or local_info.get(code))
                    for code in codes
                    if info.get(code) or local_info.get(code)
                },
            }
            self.write_json(payload)
        except Exception as exc:
            payload = {
                "ok": True,
                "source": "local stock_info table",
                "fallbackReason": f"FinMind TaiwanStockInfo failed: {exc}",
                "stocks": local_info,
            }
            self.write_json(payload)

    def handle_quotes(self, parsed):
        query = parse_qs(parsed.query)
        codes = [
            "".join(ch for ch in code if ch.isdigit())[:4]
            for value in query.get("symbols", query.get("codes", [""]))
            for code in str(value or "").split(",")
        ]
        codes = sorted({code for code in codes if len(code) == 4 and not is_etf_like_stock(code)})
        if not codes:
            self.write_json({
                "ok": True,
                "quotes": {},
                "count": 0,
                "source": "Shioaji quote",
                "updatedAt": time.strftime("%Y-%m-%d %H:%M:%S"),
            })
            return
        try:
            payload = sinopac_backend.quotes(codes)
            source = payload.get("source") or "Shioaji quote"
            if payload.get("stale"):
                source = "Shioaji quote stale cache"
            elif payload.get("cached") and source == "Shioaji quote":
                source = "Shioaji quote cache"
            self.write_json({
                **payload,
                "ok": bool(payload.get("ok", True)),
                "source": source,
                "updatedAt": time.strftime("%Y-%m-%d %H:%M:%S"),
            })
        except Exception as exc:
            self.write_json({
                "ok": False,
                "quotes": {},
                "count": 0,
                "source": "Shioaji quote",
                "error": str(exc),
                "updatedAt": time.strftime("%Y-%m-%d %H:%M:%S"),
            })

    def fetch_local_stock_info(self, codes):
        if not codes:
            return {}
        placeholders = ",".join("?" for _ in codes)
        with sqlite3.connect(ROOT / "stock_system.sqlite3", timeout=30) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                f"""
                SELECT symbol, name, sector, market_type, updated_at
                FROM stock_info
                WHERE symbol IN ({placeholders})
                """,
                codes,
            ).fetchall()
        return {
            str(row["symbol"]): {
                "symbol": str(row["symbol"]),
                "name": row["name"] or "",
                "sector": row["sector"] or "",
                "market": row["market_type"] or "",
                "updatedAt": row["updated_at"] or "",
                "source": "local stock_info table",
            }
            for row in rows
        }

    def fetch_verified_db_stock(self, stock_id, symbol, start_date, end_date):
        rows = backend.rows_with_verified_sources(backend.load_price_rows(stock_id))
        rows = [
            row for row in rows
            if str(row.get("date") or "") >= start_date and str(row.get("date") or "") <= end_date
        ]
        if not rows:
            return None
        latest = rows[-1]

        def value(row, key):
            item = row.get(key)
            return None if item is None or item == "" else item

        api_rows = []
        for row in rows:
            api_rows.append({
                "date": row.get("date"),
                "open": value(row, "open"),
                "high": value(row, "high"),
                "low": value(row, "low"),
                "close": value(row, "close"),
                "volume": value(row, "volume"),
                "foreign": value(row, "foreign_buy_sell"),
                "trust": value(row, "trust_buy_sell"),
                "margin": value(row, "margin_balance"),
                "short": value(row, "short_balance"),
                "revenueGrowth": value(row, "revenue_growth"),
                "grossMargin": value(row, "gross_margin"),
                "operatingMargin": value(row, "operating_margin"),
                "roe": value(row, "roe"),
                "debtRatio": value(row, "debt_ratio"),
                "operatingCashflowRatio": value(row, "operating_cashflow_ratio"),
                "per": value(row, "per"),
                "pbr": value(row, "pbr"),
                "dividendYield": value(row, "dividend_yield"),
                "dayTradeRatio": value(row, "day_trade_ratio"),
                "dayTradeImbalance": value(row, "day_trade_imbalance"),
                "securitiesLendingVolume": value(row, "securities_lending_volume"),
                "securitiesLendingFeeRate": value(row, "securities_lending_fee_rate"),
                "brokerBranchNetBuy": value(row, "broker_branch_net_buy"),
                "mainForceBuySell": value(row, "main_force_buy_sell"),
                "realtimeMoneyFlow": value(row, "realtime_money_flow"),
                "realtimeLargeOrderFlow": value(row, "realtime_large_order_flow"),
                "twIndex": value(row, "tw_index"),
                "usdTwd": value(row, "usd_twd"),
            })
        return {
            "ok": True,
            "symbol": symbol,
            "source": latest.get("price_source") or "verified local database",
            "tokenMode": "local-db",
            "fetchedAt": time.strftime("%Y-%m-%d %H:%M:%S"),
            "rows": api_rows,
            "warnings": [],
        }

    def handle_stock(self, parsed):
        query = parse_qs(parsed.query)
        symbol = query.get("symbol", ["2330.TW"])[0].upper()
        range_value = query.get("range", ["3y"])[0]
        interval = query.get("interval", ["1d"])[0]
        if not symbol.endswith((".TW", ".TWO")):
            symbol = f"{symbol}.TW"

        token = read_finmind_token()
        token_mode = "token" if token else "no-token"
        cache_key = (symbol, range_value, interval, token_mode)
        now = time.time()
        cached = cache.get(cache_key)
        if cached and now - cached["time"] < CACHE_SECONDS:
            if self.wants_html():
                self.write_stock_html(cached["payload"])
                return
            self.write_json(cached["payload"])
            return

        stock_id = symbol.split(".")[0]
        start_date = range_to_start_date(range_value)
        end_date = time.strftime("%Y-%m-%d")
        db_payload = self.fetch_verified_db_stock(stock_id, symbol, start_date, end_date)
        if db_payload:
            cache[cache_key] = {"time": now, "payload": db_payload}
            if self.wants_html():
                self.write_stock_html(db_payload)
                return
            self.write_json(db_payload)
            return
        try:
            payload = self.fetch_finmind(stock_id, start_date, end_date, token)
            self.attach_finmind_extras(payload, stock_id, start_date, end_date, token)
            self.attach_verified_db_flow_extras(payload, stock_id)
            cache[cache_key] = {"time": now, "payload": payload}
            if self.wants_html():
                self.write_stock_html(payload)
                return
            self.write_json(payload)
        except (HTTPError, URLError, KeyError, IndexError, TimeoutError, json.JSONDecodeError, RuntimeError) as exc:
            fallback_errors = []
            fallback_symbols = [symbol]
            if symbol.endswith(".TW"):
                fallback_symbols.append(symbol.replace(".TW", ".TWO"))
            elif symbol.endswith(".TWO"):
                fallback_symbols.append(symbol.replace(".TWO", ".TW"))
            for fallback_symbol in dict.fromkeys(fallback_symbols):
                try:
                    payload = self.fetch_yahoo(fallback_symbol, range_value, interval, f"FinMind failed: {exc}")
                    cache[(fallback_symbol, range_value, interval, token_mode)] = {"time": now, "payload": payload}
                    if self.wants_html():
                        self.write_stock_html(payload)
                        return
                    self.write_json(payload)
                    return
                except (HTTPError, URLError, KeyError, IndexError, TimeoutError, json.JSONDecodeError) as fallback_exc:
                    fallback_errors.append(f"{fallback_symbol}: {fallback_exc}")
            else:
                body = {"ok": False, "error": f"FinMind: {exc}; Yahoo fallback: {'; '.join(fallback_errors)}", "symbol": symbol}
                self.write_json(body, status=502)

    def fetch_finmind(self, stock_id, start_date, end_date, token):
        backend.reserve_finmind_call("TaiwanStockPrice", stock_id)
        params = {
            "dataset": "TaiwanStockPrice",
            "data_id": stock_id,
            "start_date": start_date,
            "end_date": end_date,
        }
        if token:
            params["token"] = token
        url = f"https://api.finmindtrade.com/api/v4/data?{urlencode(params)}"
        headers = {"User-Agent": "Mozilla/5.0"}
        request = Request(url, headers=headers)
        try:
            with urlopen(request, timeout=30) as response:
                raw = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            if exc.code in {402, 429}:
                backend.block_finmind_usage(f"FinMind TaiwanStockPrice {stock_id} HTTP {exc.code}")
            raise
        data = raw.get("data") or []
        if not data:
            raise KeyError(raw.get("msg") or "FinMind returned no rows")

        rows = []
        for item in data:
            values = {
                "date": item.get("date"),
                "open": item.get("open"),
                "high": item.get("max"),
                "low": item.get("min"),
                "close": item.get("close"),
                "volume": item.get("Trading_Volume"),
            }
            if all(values[key] is not None for key in ("date", "open", "high", "low", "close", "volume")):
                rows.append(values)
        if not rows:
            raise KeyError("FinMind rows did not contain OHLCV fields")
        return {
            "ok": True,
            "symbol": f"{stock_id}.TW",
            "source": "FinMind TaiwanStockPrice",
            "tokenMode": "sponsor/env-or-file" if token else "no-token",
            "fetchedAt": time.strftime("%Y-%m-%d %H:%M:%S"),
            "rows": rows,
        }

    def fetch_finmind_stock_info(self, token):
        backend.reserve_finmind_call("TaiwanStockInfo", "")
        params = {"dataset": "TaiwanStockInfo"}
        if token:
            params["token"] = token
        headers = {"User-Agent": "Mozilla/5.0"}
        request = Request(f"https://api.finmindtrade.com/api/v4/data?{urlencode(params)}", headers=headers)
        try:
            with urlopen(request, timeout=30) as response:
                raw = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            if exc.code in {402, 429}:
                backend.block_finmind_usage(f"FinMind TaiwanStockInfo HTTP {exc.code}")
            raise
        data = raw.get("data") or []
        if not data:
            raise KeyError(raw.get("msg") or "FinMind TaiwanStockInfo returned no rows")

        info = {}
        for item in data:
            code = str(item.get("stock_id", "")).strip()
            name = str(item.get("stock_name", "")).strip()
            sector = str(item.get("industry_category", "")).strip()
            if not code or not name:
                continue
            if is_etf_like_stock(code, name, sector, item.get("type", "")):
                continue
            existing = info.get(code)
            if not existing or existing.get("sector") in {"電子工業", "其他", ""}:
                info[code] = {
                    "code": code,
                    "name": name,
                    "sector": sector or "台股",
                    "type": item.get("type", ""),
                }
        return info

    def attach_finmind_extras(self, payload, stock_id, start_date, end_date, token):
        rows = payload.get("rows") or []
        by_date = {row["date"]: row for row in rows}
        datasets = [
            ("TaiwanStockInstitutionalInvestorsBuySell", self.merge_institutional),
            ("TaiwanStockMarginPurchaseShortSale", self.merge_margin),
            ("TaiwanStockMonthRevenue", self.merge_revenue),
            ("TaiwanStockPER", self.merge_per_pbr),
            ("TaiwanStockDayTrading", self.merge_day_trading),
            ("TaiwanStockSecuritiesLending", self.merge_securities_lending),
        ]
        for dataset, merge_fn in datasets:
            try:
                dataset_start = offset_date(start_date, -430) if dataset == "TaiwanStockMonthRevenue" else start_date
                data = self.fetch_finmind_dataset(dataset, stock_id, dataset_start, end_date, token)
                merge_fn(by_date, data)
            except Exception as exc:
                payload.setdefault("warnings", []).append(f"{dataset}: {exc}")
        try:
            statement_start = offset_date(start_date, -900)
            self.merge_financial_statements(
                by_date,
                self.fetch_finmind_dataset("TaiwanStockFinancialStatements", stock_id, statement_start, end_date, token),
                self.fetch_finmind_dataset("TaiwanStockBalanceSheet", stock_id, statement_start, end_date, token),
                self.fetch_finmind_dataset("TaiwanStockCashFlowsStatement", stock_id, statement_start, end_date, token),
            )
        except Exception as exc:
            payload.setdefault("warnings", []).append(f"financial_statements: {exc}")

    def attach_verified_db_flow_extras(self, payload, stock_id):
        rows = payload.get("rows") or []
        if not rows:
            return
        try:
            db_rows = backend.load_price_rows(stock_id)
        except Exception as exc:
            payload.setdefault("warnings", []).append(f"advanced_flow_db: {exc}")
            return
        by_date = {row.get("date"): row for row in db_rows}
        field_groups = [
            ("branch_flow_source", [
                ("broker_branch_net_buy", "brokerBranchNetBuy"),
                ("main_force_buy_sell", "mainForceBuySell"),
            ]),
            ("realtime_flow_source", [
                ("realtime_money_flow", "realtimeMoneyFlow"),
                ("realtime_large_order_flow", "realtimeLargeOrderFlow"),
            ]),
        ]
        for row in rows:
            db_row = by_date.get(row.get("date"))
            if not db_row:
                continue
            for source_key, mappings in field_groups:
                source = db_row.get(source_key)
                if not is_official_source(source):
                    continue
                for db_key, api_key in mappings:
                    value = db_row.get(db_key)
                    if value is not None:
                        row[api_key] = value
                        row[f"{api_key}Source"] = source

    def fetch_finmind_dataset(self, dataset, stock_id, start_date, end_date, token):
        backend.reserve_finmind_call(dataset, stock_id)
        params = {"dataset": dataset, "data_id": stock_id, "start_date": start_date, "end_date": end_date}
        if token:
            params["token"] = token
        headers = {"User-Agent": "Mozilla/5.0"}
        request = Request(f"https://api.finmindtrade.com/api/v4/data?{urlencode(params)}", headers=headers)
        try:
            with urlopen(request, timeout=30) as response:
                raw = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            if exc.code in {402, 429}:
                backend.block_finmind_usage(f"FinMind {dataset} {stock_id or ''} HTTP {exc.code}".strip())
            raise
        data = raw.get("data") or []
        if not data:
            raise KeyError(raw.get("msg") or "no rows")
        return data

    def merge_institutional(self, by_date, data):
        for item in data:
            row = by_date.get(item.get("date"))
            if not row:
                continue
            name = item.get("name", "")
            buy = self.safe_float(item.get("buy"))
            sell = self.safe_float(item.get("sell"))
            if buy is None or sell is None:
                continue
            value = (buy - sell) / 1000
            if "Foreign" in name or "外資" in name:
                row["foreign"] = value
            elif "Investment" in name or "投信" in name:
                row["trust"] = value

    def merge_margin(self, by_date, data):
        for item in data:
            row = by_date.get(item.get("date"))
            if not row:
                continue
            margin = self.safe_float(item.get("MarginPurchaseTodayBalance", item.get("MarginPurchaseBuy")))
            short = self.safe_float(item.get("ShortSaleTodayBalance", item.get("ShortSaleSell")))
            if margin is not None:
                row["margin"] = margin
            if short is not None:
                row["short"] = short

    def merge_revenue(self, by_date, data):
        revenue_by_period = {}
        for item in data:
            year = item.get("revenue_year")
            month = item.get("revenue_month")
            revenue = item.get("revenue")
            if year and month and revenue is not None:
                revenue_by_period[(int(year), int(month))] = revenue

        items = []
        for item in data:
            date = item.get("date")
            year = item.get("revenue_year")
            month = item.get("revenue_month")
            revenue = item.get("revenue")
            if not (date and year and month and revenue is not None):
                continue
            previous = revenue_by_period.get((int(year) - 1, int(month)))
            revenue_growth = ((revenue - previous) / previous) * 100 if previous else None
            items.append((date, revenue, revenue_growth))

        items.sort(key=lambda item: item[0])
        item_index = 0
        active = None
        for date in sorted(by_date):
            while item_index < len(items) and items[item_index][0] <= date:
                active = items[item_index]
                item_index += 1
            if active:
                by_date[date]["monthlyRevenue"] = active[1]
                if active[2] is not None:
                    by_date[date]["revenueGrowth"] = active[2]

    def safe_float(self, value, fallback=None):
        try:
            number = float(value)
        except (TypeError, ValueError):
            return fallback
        return number if number == number else fallback

    def merge_per_pbr(self, by_date, data):
        for item in data:
            row = by_date.get(item.get("date"))
            if not row:
                continue
            row["per"] = self.safe_float(item.get("PER"))
            if row["per"] is not None and row["per"] <= 0:
                row["per"] = None
            row["pbr"] = self.safe_float(item.get("PBR"))
            row["dividendYield"] = self.safe_float(item.get("dividend_yield"))

    def merge_day_trading(self, by_date, data):
        for item in data:
            row = by_date.get(item.get("date"))
            if not row:
                continue
            volume = self.safe_float(item.get("Volume"))
            daily_volume = self.safe_float(row.get("volume"))
            buy_amount = self.safe_float(item.get("BuyAmount"))
            sell_amount = self.safe_float(item.get("SellAmount"))
            if volume is None or daily_volume is None or daily_volume <= 0:
                continue
            row["dayTradeRatio"] = volume / max(daily_volume, 1)
            if buy_amount is not None and sell_amount is not None:
                row["dayTradeImbalance"] = (buy_amount - sell_amount) / max(buy_amount + sell_amount, 1)

    def merge_securities_lending(self, by_date, data):
        grouped = {}
        for item in data:
            date = item.get("date")
            if date not in by_date:
                continue
            bucket = grouped.setdefault(date, {"volume": 0.0, "volumeSeen": False, "feeTotal": 0.0, "feeCount": 0})
            volume = self.safe_float(item.get("volume"))
            if volume is not None:
                bucket["volume"] += volume
                bucket["volumeSeen"] = True
            fee_rate = self.safe_float(item.get("fee_rate"))
            if fee_rate is not None:
                bucket["feeTotal"] += fee_rate
                bucket["feeCount"] += 1
        for date, bucket in grouped.items():
            by_date[date]["securitiesLendingVolume"] = bucket["volume"] if bucket["volumeSeen"] else None
            by_date[date]["securitiesLendingFeeRate"] = bucket["feeTotal"] / bucket["feeCount"] if bucket["feeCount"] else None

    def merge_financial_statements(self, by_date, income_rows, balance_rows, cashflow_rows):
        quarterly = {}
        for data in (income_rows, balance_rows, cashflow_rows):
            for item in data:
                date = item.get("date")
                if not date:
                    continue
                quarterly.setdefault(date, {})[item.get("type")] = self.safe_float(item.get("value"))
        snapshots = []
        for date, values in sorted(quarterly.items()):
            revenue = values.get("Revenue")
            gross_profit = values.get("GrossProfit")
            operating_income = values.get("OperatingIncome")
            net_income = values.get("EquityAttributableToOwnersOfParent")
            equity = values.get("Equity") or values.get("EquityAttributableToOwnersOfParent")
            liabilities = values.get("Liabilities")
            assets = values.get("TotalAssets")
            operating_cashflow = (
                values.get("CashFlowsFromOperatingActivities") or
                values.get("NetCashInflowFromOperatingActivities") or
                values.get("CashReceivedThroughOperations")
            )
            snapshots.append((financial_statement_disclosure_date(date), {
                "grossMargin": (gross_profit / revenue) * 100 if revenue and gross_profit is not None else None,
                "operatingMargin": (operating_income / revenue) * 100 if revenue and operating_income is not None else None,
                "roe": (net_income / equity) * 100 if equity and net_income is not None else None,
                "debtRatio": (liabilities / assets) * 100 if assets and liabilities is not None else None,
                "operatingCashflowRatio": (operating_cashflow / revenue) * 100 if revenue and operating_cashflow is not None else None,
            }))
        snapshots.sort(key=lambda item: item[0])
        item_index = 0
        active = None
        for date in sorted(by_date):
            while item_index < len(snapshots) and snapshots[item_index][0] <= date:
                active = snapshots[item_index][1]
                item_index += 1
            if active:
                by_date[date].update({key: value for key, value in active.items() if value is not None})

    def fetch_yahoo(self, symbol, range_value, interval, reason):
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?range={range_value}&interval={interval}&events=history"
        request = Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urlopen(request, timeout=20) as response:
            raw = json.loads(response.read().decode("utf-8"))
        result = raw["chart"]["result"][0]
        timestamps = result.get("timestamp", [])
        quote = result["indicators"]["quote"][0]
        rows = []
        for index, timestamp in enumerate(timestamps):
            values = {
                "date": time.strftime("%Y-%m-%d", time.localtime(timestamp)),
                "open": quote.get("open", [])[index],
                "high": quote.get("high", [])[index],
                "low": quote.get("low", [])[index],
                "close": quote.get("close", [])[index],
                "volume": quote.get("volume", [])[index],
            }
            if all(values[key] is not None for key in ("open", "high", "low", "close", "volume")):
                rows.append(values)
        return {
            "ok": True,
            "symbol": symbol,
            "source": "Yahoo Finance chart API fallback",
            "fallbackReason": reason,
            "fetchedAt": time.strftime("%Y-%m-%d %H:%M:%S"),
            "rows": rows,
        }

    def write_json(self, payload, status=200):
        payload = clean_json_payload(payload)
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8", errors="replace")
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError, OSError):
            return

    def wants_html(self):
        accept = self.headers.get("Accept", "")
        return "text/html" in accept

    def write_stock_html(self, payload):
        rows = payload.get("rows") or []
        first = rows[0] if rows else {}
        last = rows[-1] if rows else {}
        change = ""
        if len(rows) >= 2:
            prev = rows[-2].get("close") or 0
            close = last.get("close") or 0
            if prev:
                change = f"{((close - prev) / prev) * 100:+.2f}%"
        html = f"""<!doctype html>
<html lang="zh-Hant">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{escape(payload.get("symbol", "Stock API"))} 資料摘要</title>
  <style>
    body {{ margin: 0; font-family: "Segoe UI", "Microsoft JhengHei", sans-serif; background: #f4f7fb; color: #0b1728; }}
    main {{ max-width: 920px; margin: 0 auto; padding: 32px 20px; }}
    a {{ color: #2563eb; font-weight: 800; text-decoration: none; }}
    .panel {{ background: #fff; border: 1px solid #dce4ee; border-radius: 8px; padding: 20px; box-shadow: 0 16px 40px rgba(15,23,42,.08); }}
    .grid {{ display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 12px; margin: 18px 0; }}
    .card {{ background: #eef4fb; border-radius: 8px; padding: 14px; }}
    .card span {{ display: block; color: #627085; font-size: 12px; font-weight: 800; }}
    .card strong {{ display: block; margin-top: 8px; font-size: 22px; }}
    pre {{ overflow: auto; background: #10151d; color: #eef4ff; padding: 14px; border-radius: 8px; }}
    @media (max-width: 720px) {{ .grid {{ grid-template-columns: 1fr; }} }}
  </style>
</head>
<body>
  <main>
    <p><a href="/">← 回到股票系統主頁</a></p>
    <section class="panel">
      <h1>{escape(payload.get("symbol", ""))} FinMind 資料摘要</h1>
      <p>這是 API 摘要頁。程式呼叫此端點時仍會收到 JSON；瀏覽器直接開啟時顯示此整理畫面。</p>
      <div class="grid">
        <div class="card"><span>資料來源</span><strong>{escape(payload.get("source", ""))}</strong></div>
        <div class="card"><span>資料筆數</span><strong>{len(rows)}</strong></div>
        <div class="card"><span>最新日期</span><strong>{escape(str(last.get("date", "")))}</strong></div>
        <div class="card"><span>最新收盤</span><strong>{escape(str(last.get("close", "")))}</strong></div>
      </div>
      <div class="grid">
        <div class="card"><span>日漲跌</span><strong>{escape(change)}</strong></div>
        <div class="card"><span>起始日期</span><strong>{escape(str(first.get("date", "")))}</strong></div>
        <div class="card"><span>Token 模式</span><strong>{escape(payload.get("tokenMode", ""))}</strong></div>
        <div class="card"><span>更新時間</span><strong>{escape(payload.get("fetchedAt", ""))}</strong></div>
      </div>
      <h2>最新一筆原始資料</h2>
      <pre>{escape(json.dumps(last, ensure_ascii=False, indent=2))}</pre>
    </section>
  </main>
</body>
</html>"""
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)



def last_daily_update_attempt_date():
    # 優先讀本機的 daily_update_logs/latest.json（純檔案寫入，不會因為
    # SQLite database is locked 而失敗），DB 讀取失敗時才退回資料庫，
    # 兩邊都失敗就回傳 None（視為今天還沒跑過，安全值）。
    try:
        latest_path = ROOT / "daily_update_logs" / "latest.json"
        if latest_path.exists():
            data = json.loads(latest_path.read_text(encoding="utf-8"))
            started = str(data.get("startedAt") or "")
            if started:
                return started[:10]
    except Exception:
        pass
    try:
        with backend.connect() as conn:
            updated_at = conn.execute("SELECT value FROM model_meta WHERE key = 'last_daily_job_at'").fetchone()
        if not updated_at:
            return None
        return str(updated_at[0])[:10]
    except Exception:
        return None


def latest_daily_update_health():
    today = time.strftime("%Y-%m-%d")
    try:
        with backend.connect() as conn:
            meta = {
                row[0]: row[1]
                for row in conn.execute(
                    "SELECT key, value FROM model_meta WHERE key IN (?, ?, ?, ?)",
                    (
                        "last_daily_job_status",
                        "last_daily_job_at",
                        "last_daily_job_error",
                        "last_data_update",
                    ),
                ).fetchall()
            }
    except Exception as exc:
        return {
            "ok": False,
            "mode": "observe_only",
            "reason": f"資料庫狀態讀取失敗：{exc}",
            "status": "",
            "updatedAt": "",
        }
    status = str(meta.get("last_daily_job_status") or "")
    updated_at = str(meta.get("last_daily_job_at") or "")
    error = str(meta.get("last_daily_job_error") or "")
    if updated_at.startswith(today) and status == "failed":
        return {
            "ok": False,
            "mode": "observe_only",
            "reason": f"今日每日更新失敗：{error or '未知錯誤'}",
            "status": status,
            "updatedAt": updated_at,
        }
    # 舊版只判斷 status != "success"，沒比對 updated_at 是不是今天——這代表
    # 「兩天前成功、之後每一次嘗試都失敗」的情況會一直被當成「今天一切
    # 正常」持續放行正式模型訊號。真實踩過這個 bug：updated_at 停在前天、
    # status 仍是舊的 success，這個函式當下回傳的就是 ok=True。跟
    # ml_backend.py data_health() 的 last_system_health_status 用同一套
    # 「今天更新過才算數」原則修。
    if status != "success" or not updated_at.startswith(today):
        return {
            "ok": False,
            "mode": "observe_only",
            "reason": "今日每日資料更新尚未成功，暫停盤中買賣放行",
            "status": status,
            "updatedAt": updated_at,
        }
    return {
        "ok": True,
        "mode": "normal",
        "reason": "",
        "status": status,
        "updatedAt": updated_at,
    }


def radar_market_data_health():
    """盤中妖股決策所需的全域日線資料品質。

    個別候選 priceDate 新鮮不代表整包個股日K已完整同步；例如只有少數
    股票寫入最新日期時，MAX(date) 仍會看似新鮮。盤中正式放行需同時確認
    個股日K覆蓋與大盤日線都通過資料新鮮度檢查。
    """
    try:
        payload = backend.data_freshness()
        sources = {
            str(row.get("name") or ""): row
            for row in (payload.get("sources") or [])
            if isinstance(row, dict)
        }
        required = ("個股日K", "大盤指數")
        failed = [name for name in required if not sources.get(name, {}).get("ok")]
        if failed:
            return {
                "ok": False,
                "mode": "observe_only",
                "reason": f"{('、'.join(failed))}未通過新鮮度檢查",
                "sources": {name: sources.get(name) for name in required},
                # 籌碼、財報等資料有各自的收盤/公告週期，不可強迫它們在盤中
                # 即時化；仍把來源日期回傳給 UI 稽核，避免使用者誤以為它們是
                # 即時資料。
                "allSources": list(payload.get("sources") or []),
                "checkedAt": payload.get("checkedAt") or "",
            }
        return {
            "ok": True,
            "mode": "normal",
            "reason": "",
            "sources": {name: sources.get(name) for name in required},
            "allSources": list(payload.get("sources") or []),
            "checkedAt": payload.get("checkedAt") or "",
        }
    except Exception as exc:
        return {
            "ok": False,
            "mode": "observe_only",
            "reason": f"市場資料新鮮度檢查失敗：{exc}",
            "sources": {},
        }


def intraday_flow_health(quotes, error="", quote_stale=False, quote_ok=True, radar_data_health=None):
    daily_health = latest_daily_update_health()
    radar_data_health = radar_data_health or {"ok": True, "mode": "normal", "reason": ""}
    quote_count = len(quotes or {})
    if error:
        return {
            "ok": False,
            "mode": "observe_only",
            "reason": f"盤中報價更新失敗：{error}",
            "daily": daily_health,
            "quoteCount": quote_count,
        }
    if not quote_ok:
        return {
            "ok": False,
            "mode": "observe_only",
            "reason": "盤中報價來源未確認成功，暫停買進/賣出決策",
            "daily": daily_health,
            "quoteCount": quote_count,
        }
    if quote_stale:
        return {
            "ok": False,
            "mode": "observe_only",
            "reason": "盤中報價已過期，僅觀察不產生買賣決策",
            "daily": daily_health,
            "quoteCount": quote_count,
        }
    if not radar_data_health.get("ok"):
        return {
            "ok": False,
            "mode": "observe_only",
            "reason": radar_data_health.get("reason") or "市場資料未通過新鮮度檢查",
            "daily": daily_health,
            "radarData": radar_data_health,
            "quoteCount": quote_count,
        }
    if quote_count <= 0:
        return {
            "ok": False,
            "mode": "observe_only",
            "reason": "盤中報價尚未取得，暫停買進/賣出決策",
            "daily": daily_health,
            "quoteCount": quote_count,
        }
    if not daily_health.get("ok"):
        return {
            "ok": False,
            "mode": "observe_only",
            "reason": daily_health.get("reason") or "正式資料狀態異常",
            "daily": daily_health,
            "quoteCount": quote_count,
        }
    return {
        "ok": True,
        "mode": "normal",
        "reason": "",
        "daily": daily_health,
        "quoteCount": quote_count,
    }


DAILY_UPDATE_JOB_ID = "portable_daily_update"


def should_run_portable_daily_update(now):
    today = time.strftime("%Y-%m-%d", now)
    after_morning_update_time = now.tm_hour > 8 or (now.tm_hour == 8 and now.tm_min >= 30)
    if not (0 <= now.tm_wday <= 4):
        return False
    if not after_morning_update_time or not read_finmind_token():
        return False
    # 排程的每日更新自 2026-07-08 起只抓資料、不重訓(run_daily_job(train=False);
    # 重訓移到收盤後 15:10 auto_model_cycle——train_model 是 CPU 密集的純 Python
    # 迴圈,2026-07-03 實測開盤期間補跑會把庫存/報價查詢從 1-2 秒拖到 12-19 秒近
    # 30 分鐘)。開盤時段(09:00-13:30)仍不觸發這個資料更新:開盤前 08:30-09:00 窗口
    # 已足夠跑完純資料抓取,盤中另有 30 秒即時報價與 data_gap_repair 持續補價,不需
    # 要再對全持股重抓一次。真的積欠(早上抓失敗)就遞延到收盤後補,當天 K 棒本來
    # 就收盤才完整。data_health 綁的是這個資料 job(last_daily_job),所以早上資料一
    # 更新完就會轉正常,不再因重訓沒跑完卡整個交易日 observe_only。
    if auto_schedule_window(now, 9, 0, 13, 30):
        return False
    # 舊版只判斷「今天有沒有嘗試過」，只要今天第一次嘗試失敗(實際發生過：
    # database is locked)，daily_update_worker 整天都不會再自動重試，
    # 要等隔天 08:30 才有下一次機會，中間任何當沖等級的短線妖股訊號時間
    # 窗口都會被錯過。改成「今天有沒有成功過」，還沒成功就持續依
    # AUTO_SCHEDULE_MAX_RETRIES 上限重試(跟已有的 auto_schedule_* 排程
    # 重試機制共用同一組 DB 持久化計數器，不是重新發明一套)。
    if latest_daily_update_health().get("ok"):
        return False
    if last_daily_update_attempt_date() != today:
        return True  # 今天第一次嘗試，永遠放行
    return auto_schedule_attempt_count(DAILY_UPDATE_JOB_ID, today) < AUTO_SCHEDULE_MAX_RETRIES


def daily_update_worker():
    global daily_update_running
    while True:
        try:
            now = taipei_localtime()
            today = scheduler_today(now)
            try:
                market_day = official_market_day_status(today) if 0 <= now.tm_wday <= 4 else {
                    "known": True,
                    "isTradingDay": False,
                    "reason": "週末",
                }
            except Exception as exc:
                market_day = {"known": False, "isTradingDay": None, "reason": str(exc)[:120]}
            # W7 修復：用 daily_update_lock 原子性地做「檢查→設旗」，
            # 防止 HTTP handler 同一時間也呼叫 full_daily_update 造成 TOCTOU 競態。
            should_run = False
            with daily_update_lock:
                if (
                    market_schedule_allowed(market_day)
                    and should_run_portable_daily_update(now)
                    and not daily_update_running
                ):
                    daily_update_running = True
                    should_run = True
            if should_run:
                try:
                    # 每日官方日K同步完成後，自動刷新妖股候選；否則候選分數會
                    # 停在前一日，即使全市場資料已更新也只能被新鮮度閘門擋下。
                    result = run_daily_job(train=False, scan_monster=True)
                    cache.clear()
                    if result.get("ok"):
                        record_auto_schedule_attempt(DAILY_UPDATE_JOB_ID, today, 0)
                        print(f"Portable daily update completed at {time.strftime('%Y-%m-%d %H:%M:%S')}")
                    else:
                        attempts = auto_schedule_attempt_count(DAILY_UPDATE_JOB_ID, today) + 1
                        record_auto_schedule_attempt(DAILY_UPDATE_JOB_ID, today, attempts)
                        print(f"Portable daily update failed (attempt {attempts}/{AUTO_SCHEDULE_MAX_RETRIES}): {result.get('error')}")
                        if attempts >= AUTO_SCHEDULE_MAX_RETRIES:
                            notify_auto_schedule_failure(DAILY_UPDATE_JOB_ID, attempts, result.get("error"))
                except Exception as exc:
                    attempts = auto_schedule_attempt_count(DAILY_UPDATE_JOB_ID, today) + 1
                    record_auto_schedule_attempt(DAILY_UPDATE_JOB_ID, today, attempts)
                    print(f"Portable daily update failed (attempt {attempts}/{AUTO_SCHEDULE_MAX_RETRIES}): {exc}")
                    if attempts >= AUTO_SCHEDULE_MAX_RETRIES:
                        notify_auto_schedule_failure(DAILY_UPDATE_JOB_ID, attempts, exc)
                finally:
                    with daily_update_lock:
                        daily_update_running = False
        except Exception as exc:
            # 這個背景執行緒是 daemon thread，一旦在迴圈內丟出未攔截的例外
            # （例如檢查「今天跑過沒」時資料庫剛好被鎖住），整個每日更新
            # 排程就會永久停止，必須重開 server.py 才會恢復。這裡攔截起來
            # 確保排程迴圈本身不會被任何單次例外打死。
            print(f"daily_update_worker loop error: {exc}")
        time.sleep(300)


def scheduler_today(now=None):
    return time.strftime("%Y-%m-%d", now or taipei_localtime())


def auto_schedule_window(now, start_hour, start_minute, end_hour, end_minute):
    if not (0 <= now.tm_wday <= 4):
        return False
    minutes = now.tm_hour * 60 + now.tm_min
    return (start_hour * 60 + start_minute) <= minutes < (end_hour * 60 + end_minute)


def market_schedule_allowed(market_day):
    return bool(
        isinstance(market_day, dict)
        and market_day.get("known") is True
        and market_day.get("isTradingDay") is True
    )


def auto_schedule_has_run(job_id, today):
    with backend.connect() as conn:
        row = conn.execute("SELECT value FROM model_meta WHERE key = ?", (f"auto_schedule_{job_id}_date",)).fetchone()
    if row and str(row[0]) == today:
        return True
    return auto_schedule_has_effective_run(job_id, today)


def auto_schedule_has_effective_run(job_id, today):
    # 有些排程也可能被手動補跑，或是在新功能部署時已經有實際產物，
    # 但尚未寫入 auto_schedule_*_date。漏跑偵測要以「實際產物」為準，
    # 避免明明已經補出資料卻仍發「完全沒執行過」的警示。
    if job_id != STRATEGY_CALIBRATION_JOB_ID:
        return False
    try:
        with backend.connect() as conn:
            row = conn.execute(
                "SELECT value FROM model_meta WHERE key = 'last_strategy_calibration_date'"
            ).fetchone()
            if row and str(row[0]) == today:
                return True
            row = conn.execute(
                "SELECT 1 FROM strategy_calibration WHERE calibration_date = ? LIMIT 1",
                (today,),
            ).fetchone()
            return bool(row)
    except Exception:
        return False


def mark_auto_schedule(job_id, status, message):
    now_text_value = time.strftime("%Y-%m-%d %H:%M:%S", taipei_localtime())
    today = now_text_value[:10]
    with backend.connect() as conn:
        backend.set_meta(conn, f"auto_schedule_{job_id}_date", today)
        backend.set_meta(conn, f"auto_schedule_{job_id}_status", status)
        backend.set_meta(conn, f"auto_schedule_{job_id}_message", message)
        backend.set_meta(conn, f"auto_schedule_{job_id}_at", now_text_value)
    auto_schedule_running[job_id] = False


# worker 回傳這個 sentinel 表示「這次先不做，也不要標記當天已執行」，
# 讓 auto_schedule_worker 的 20 秒迴圈在視窗內繼續重試。
AUTO_SCHEDULE_RETRY = object()

# 排程失敗最多重試幾次才放棄、標記當天失敗並發 LINE 通知。
# mark_auto_schedule 一旦寫入日期，auto_schedule_has_run 當天就不會再重跑，
# 所以失敗當下不能直接標記——要留在「還沒標記」狀態讓 20 秒迴圈自然重試。
AUTO_SCHEDULE_MAX_RETRIES = 3
OFFICIAL_CLOSE_SYNC_JOB_ID = "1435_official_close_sync"
STRATEGY_CALIBRATION_JOB_ID = "1705_strategy_calibration"
INTRADAY_DISCOVERY_RECALL_JOB_ID = "1810_intraday_discovery_recall"
STRATEGY_CALIBRATION_CATCHUP_MINUTE = 17 * 60 + 30
OFFICIAL_CLOSE_SYNC_MAX_RETRIES = 12
OFFICIAL_CLOSE_SYNC_MIN_ROWS = 1500
OFFICIAL_CLOSE_SYNC_RETRY_SECONDS = 300
OFFICIAL_CLOSE_SYNC_FINALIZE_MINUTE = 18 * 60 + 20


def auto_schedule_retry_limit(job_id):
    if job_id in {
        OFFICIAL_CLOSE_SYNC_JOB_ID,
        "1800_market_session_validation",
        INTRADAY_DISCOVERY_RECALL_JOB_ID,
    }:
        return OFFICIAL_CLOSE_SYNC_MAX_RETRIES
    return AUTO_SCHEDULE_MAX_RETRIES


def auto_schedule_attempt_count(job_id, today):
    with backend.connect() as conn:
        date_row = conn.execute(
            "SELECT value FROM model_meta WHERE key = ?", (f"auto_schedule_{job_id}_attempt_date",)
        ).fetchone()
        if not date_row or str(date_row[0]) != today:
            return 0
        count_row = conn.execute(
            "SELECT value FROM model_meta WHERE key = ?", (f"auto_schedule_{job_id}_attempt_count",)
        ).fetchone()
    return int(count_row[0]) if count_row and str(count_row[0]).isdigit() else 0


def record_auto_schedule_attempt(job_id, today, count):
    with backend.connect() as conn:
        backend.set_meta(conn, f"auto_schedule_{job_id}_attempt_date", today)
        backend.set_meta(conn, f"auto_schedule_{job_id}_attempt_count", str(count))


def safe_print(text):
    # Windows 主控台常用 cp950 之類非 UTF-8 編碼，emoji 這類字元會讓 print()
    # 直接丟 UnicodeEncodeError；這個例外若發生在通知函式內部，很容易被外層
    # try/except 誤判成「通知失敗」，導致一次原本會成功的 LINE 推播被跳過。
    # 主控台印不出來的字元換成安全字元，不讓純顯示用途的輸出去干擾程式邏輯。
    try:
        print(text)
    except UnicodeEncodeError:
        encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
        print(text.encode(encoding, errors="replace").decode(encoding, errors="replace"))


def notify_auto_schedule_failure(job_id, attempts, exc):
    try:
        send_line_message_via_api(
            f"⚠️ StockAI 排程「{job_id}」重試 {attempts} 次仍失敗，已放棄本次執行：{exc}",
            priority="critical",  # 系統故障警示：額度保留池內仍放行
        )
    except Exception as notify_exc:
        # 通知本身失敗（例如 LINE 沒設定）不能讓排程迴圈掛掉，只記錄。
        print(f"Auto schedule {job_id} failure notification could not be sent: {notify_exc}")


def run_auto_schedule_job(job_id, worker):
    now = taipei_localtime()
    today = scheduler_today(now)
    if auto_schedule_running.get(job_id) or auto_schedule_has_run(job_id, today):
        return
    auto_schedule_running[job_id] = True

    def wrapped():
        try:
            message = worker()
            if message is AUTO_SCHEDULE_RETRY:
                # 不標記日期，釋放 running 旗標，下一輪 20 秒迴圈重試
                auto_schedule_running[job_id] = False
                return
            schedule_status = "success"
            if isinstance(message, dict) and message.get("scheduleStatus"):
                # 有實際執行補抓、但外部來源尚未補齊時，不能把排程偽裝成
                # success，也不能在 20 秒內連打同一資料源。記成 partial，
                # 同日稍後的另一個缺口修復時段仍會繼續處理待補佇列。
                schedule_status = str(message.get("scheduleStatus") or "partial")
                message = str(message.get("message") or schedule_status)
            record_auto_schedule_attempt(job_id, today, 0)
            mark_auto_schedule(job_id, schedule_status, message)
            print(f"Auto schedule {job_id} completed: {message}")
        except Exception as exc:
            attempts = auto_schedule_attempt_count(job_id, today) + 1
            record_auto_schedule_attempt(job_id, today, attempts)
            retry_limit = auto_schedule_retry_limit(job_id)
            if attempts < retry_limit:
                # 還有重試機會：不標記今天已完成，釋放 running 旗標讓下一輪
                # 20 秒迴圈（在視窗內）重試，不需要通知使用者。
                auto_schedule_running[job_id] = False
                print(f"Auto schedule {job_id} failed (attempt {attempts}/{retry_limit}), will retry: {exc}")
            else:
                mark_auto_schedule(job_id, "failed", str(exc))
                print(f"Auto schedule {job_id} failed after {attempts} attempts: {exc}")
                notify_auto_schedule_failure(job_id, attempts, exc)

    threading.Thread(target=wrapped, daemon=True).start()


# 純粹給 check_missed_auto_schedule_windows 用來偵測「視窗關了但整天都沒
# 執行過」——跟下面迴圈裡實際判斷要不要執行的 auto_schedule_window(...) 呼叫
# 是分開的兩份資料，這裡只影響「要不要發通知」，不影響任何排程本身的行為，
# 就算未來新增排程時忘記同步更新這份表，頂多是少一則通知，不會誤觸發執行。
AUTO_SCHEDULE_WINDOWS = {
    # 08:15開盤前晨報：在0845缺口修復/0855盤前熱機之前，讓使用者開盤前
    # 就拿到今日觀察清單(含觸發/停損價)。
    "0815_morning_brief": (8, 15, 8, 50),
    "0845_data_gap_repair": (8, 45, 9, 5),
    "0850_premarket_monster_scan": (8, 50, 10, 0),
    "0905_market_session_validation": (9, 5, 9, 15),
    "0905_initial_filter": (9, 5, 9, 15),
    "0905_paper_signal_snapshot": (9, 5, 9, 15),
    "0915_volume_check": (9, 15, 9, 30),
    "0915_paper_signal_snapshot": (9, 15, 9, 30),
    "0930_paper_signal_snapshot": (9, 30, 10, 0),
    "0950_market_session_validation": (9, 50, 10, 10),
    "1320_paper_signal_snapshot": (13, 20, 13, 30),
    "1420_data_gap_repair": (14, 20, 14, 30),
    "1435_official_close_sync": (14, 35, 18, 30),
    "1500_monster_scan": (15, 0, 16, 0),
    "1510_batch_predictions": (15, 10, 17, 0),
    "1520_paper_signal_snapshot": (15, 20, 17, 30),
    "1540_data_gap_repair": (15, 40, 17, 30),
    STRATEGY_CALIBRATION_JOB_ID: (17, 5, 17, 30),
    # 17:35 之後：當天所有資料處理排程(掃描/批量預測/缺口修復)都已結束，
    # 摘要拿到的是最終狀態。每交易日 1 則 LINE(月額度 200 則，約用 22 則)。
    "1735_daily_digest": (17, 35, 18, 30),
    "1800_market_session_validation": (18, 0, 18, 30),
    INTRADAY_DISCOVERY_RECALL_JOB_ID: (18, 10, 18, 30),
    # Weekly only. check_missed_auto_schedule_windows consults
    # AUTO_SCHEDULE_WEEKDAYS so this never creates false missed-job alerts on
    # Monday through Thursday.
    "1840_weekly_tcn_experiment": (18, 40, 20, 30),
}

AUTO_SCHEDULE_WEEKDAYS = {
    "1840_weekly_tcn_experiment": {4},  # Friday, time.struct_time convention
}


def _schedule_window_text(window):
    start_hour, start_minute, end_hour, end_minute = window
    return {
        "start": f"{start_hour:02d}:{start_minute:02d}",
        "end": f"{end_hour:02d}:{end_minute:02d}",
        "label": f"{start_hour:02d}:{start_minute:02d}-{end_hour:02d}:{end_minute:02d}",
    }


def paper_signal_snapshot_schedule_status(now=None):
    now = now or taipei_localtime()
    today = scheduler_today(now)
    minutes = now.tm_hour * 60 + now.tm_min
    is_weekday = 0 <= now.tm_wday <= 4
    try:
        market_day = official_market_day_status(today) if is_weekday else {
            "known": True,
            "isTradingDay": False,
            "reason": "週末",
        }
    except Exception as exc:
        market_day = {"known": False, "isTradingDay": None, "reason": str(exc)[:120]}
    market_closed = market_day.get("known") is True and market_day.get("isTradingDay") is False
    status_labels = {
        "done": "已完成",
        "failed": "失敗",
        "upcoming": "未到",
        "running_window": "執行窗",
        "missed": "漏跑",
        "market_closed": "非交易日",
        "unknown": "未排程",
    }
    try:
        with backend.connect() as conn:
            meta = {str(row[0]): row[1] for row in conn.execute("SELECT key, value FROM model_meta").fetchall()}
    except Exception:
        meta = {}

    rows = []
    for session_key, job_id in PAPER_SIGNAL_SESSION_JOBS.items():
        session_meta = PAPER_SIGNAL_SESSIONS.get(session_key, {})
        window = AUTO_SCHEDULE_WINDOWS.get(job_id)
        window_text = _schedule_window_text(window) if window else {"start": "", "end": "", "label": ""}
        schedule_prefix = f"auto_schedule_{job_id}"
        snapshot_prefix = f"last_paper_signal_snapshot_{session_key}"
        ran_date = str(meta.get(f"{schedule_prefix}_date") or "")
        ran_at = str(meta.get(f"{schedule_prefix}_at") or meta.get(f"{snapshot_prefix}_at") or "")
        raw_status = str(meta.get(f"{schedule_prefix}_status") or "").lower()
        snapshot_date = str(meta.get(f"{snapshot_prefix}_at") or "")[:10]
        effective_done = ran_date == today or snapshot_date == today

        if effective_done:
            status = "failed" if raw_status == "failed" else "done"
        elif not window:
            status = "unknown"
        else:
            start_minutes = window[0] * 60 + window[1]
            end_minutes = window[2] * 60 + window[3]
            if not is_weekday or market_closed:
                status = "market_closed"
            elif minutes < start_minutes:
                status = "upcoming"
            elif minutes < end_minutes:
                status = "running_window"
            else:
                status = "missed"

        rows.append({
            "key": session_key,
            "label": session_meta.get("label") or session_key,
            "time": session_meta.get("time") or "",
            "jobId": job_id,
            "window": window_text,
            "status": status,
            "statusLabel": status_labels.get(status, status),
            "ranAt": ran_at,
            "saved": safe_int(meta.get(f"{snapshot_prefix}_saved"), 0),
            "checked": safe_int(meta.get(f"{snapshot_prefix}_checked"), 0),
            "errors": safe_int(meta.get(f"{snapshot_prefix}_errors"), 0),
            "message": meta.get(f"{schedule_prefix}_message") or "",
        })
    return rows


def build_market_session_validation(stage, now=None):
    """Build an evidence report for a real trading session without placing orders."""
    if stage not in {"open", "intraday", "close"}:
        raise ValueError("交易日驗證階段必須是 open、intraday 或 close")
    now = now or taipei_localtime()
    today = scheduler_today(now)
    checked_at = time.strftime("%Y-%m-%d %H:%M:%S", now)
    checks = []

    def add(key, label, ok, detail, required=True, evidence=None):
        checks.append({
            "key": key,
            "label": label,
            "ok": bool(ok),
            "required": bool(required),
            "detail": str(detail or ""),
            "evidence": evidence or {},
        })

    market_day = official_market_day_status(today)
    add(
        "official_market_day",
        "官方交易日",
        market_day.get("known") is True and market_day.get("isTradingDay") is True,
        market_day.get("reason") or market_day.get("source") or "無官方交易日證據",
        evidence=market_day,
    )

    with backend.connect() as conn:
        conn.row_factory = sqlite3.Row
        latest = conn.execute("""
            SELECT date, COUNT(DISTINCT symbol) AS row_count
            FROM prices
            GROUP BY date
            ORDER BY date DESC
            LIMIT 1
        """).fetchone()
        meta = {str(row[0]): row[1] for row in conn.execute("SELECT key, value FROM model_meta").fetchall()}
        exit_snapshot = conn.execute("""
            SELECT COUNT(*) AS total,
                   SUM(CASE WHEN decision_verified = 1 THEN 1 ELSE 0 END) AS verified
            FROM portfolio_exit_snapshots
            WHERE decision_date = ?
        """, (today,)).fetchone()
        unknown_horizons = conn.execute("""
            SELECT COUNT(DISTINCT symbol)
            FROM trades
            WHERE side = 'BUY' AND status != 'paper'
              AND exit_at IS NULL AND exit_price IS NULL
              AND COALESCE(strategy_horizon, 'unknown') = 'unknown'
        """).fetchone()[0]
        try:
            exit_log_count = conn.execute(
                "SELECT COUNT(*) FROM exit_decision_logs WHERE decision_date = ?", (today,)
            ).fetchone()[0]
        except sqlite3.OperationalError:
            exit_log_count = 0

    latest_date = str(latest["date"] or "") if latest else ""
    latest_count = int(latest["row_count"] or 0) if latest else 0
    daily_health = radar_market_data_health()
    add(
        "daily_bars",
        "日 K 完整性",
        latest_count >= 1500 and daily_health.get("ok") is True,
        f"最新 {latest_date or '-'}，{latest_count} 檔；{daily_health.get('reason') or '市場資料健康'}",
        evidence={"latestDate": latest_date, "rowCount": latest_count, "health": daily_health},
    )
    radar_validity = backend.current_radar_decision_validity(current_date=today)
    add(
        "radar_decision",
        "雷達決策資料",
        radar_validity.get("validForTrading") is True,
        radar_validity.get("summary") or "無雷達有效性結果",
        evidence=radar_validity,
    )
    if stage == "open":
        readiness = backend.current_radar_deployment_readiness()
        readiness_today = str(readiness.get("readinessDate") or "") == today
        performance_ok = bool(
            readiness_today
            and (
                readiness.get("enforced") is not True
                or readiness.get("formalReady") is True
            )
        )
        add(
            "radar_performance",
            "雷達真實戰績門檻",
            performance_ok,
            (
                "；".join(readiness.get("reasons") or [])
                if not performance_ok
                else "當日戰績驗證已完成並符合目前上線規則"
            ),
            evidence=readiness,
        )
    if stage in {"intraday", "close"}:
        try:
            gate_stats = json.loads(meta.get(INTRADAY_GATE_STATS_KEY) or "{}")
        except (json.JSONDecodeError, TypeError):
            gate_stats = {}
        gate_today = str(gate_stats.get("date") or "") == today
        add(
            "intraday_quotes",
            "即時報價",
            gate_today and int(gate_stats.get("polls") or 0) > 0 and int(gate_stats.get("freshQuoteCount") or 0) > 0,
            (
                f"輪詢 {int(gate_stats.get('polls') or 0)} 次，新鮮報價 "
                f"{int(gate_stats.get('freshQuoteCount') or 0)} 檔；"
                f"來源 {gate_stats.get('quoteSources') or {}}，群益備援 "
                f"{int(gate_stats.get('fallbackQuoteCount') or 0)} 檔"
            ),
            evidence=gate_stats,
        )
        fresh_quote_count = int(gate_stats.get("freshQuoteCount") or 0)
        timestamp_count = int(gate_stats.get("freshQuoteTimestampCount") or 0)
        quote_date_mismatch_count = int(gate_stats.get("quoteDateMismatchCount") or 0)
        missing_quote_timestamp_count = int(gate_stats.get("missingQuoteTimestampCount") or 0)
        add(
            "quote_provenance",
            "即時報價日期與來源",
            gate_today
            and fresh_quote_count > 0
            and timestamp_count == fresh_quote_count
            and quote_date_mismatch_count == 0
            and missing_quote_timestamp_count == 0,
            (
                f"新鮮報價 {fresh_quote_count} 檔，具時間戳 {timestamp_count} 檔；"
                f"非當日 {quote_date_mismatch_count} 檔，缺時間戳 {missing_quote_timestamp_count} 檔"
            ),
            evidence={
                "quoteSources": gate_stats.get("quoteSources") or {},
                "fallbackQuoteCount": int(gate_stats.get("fallbackQuoteCount") or 0),
                "fallbackQuoteCodes": gate_stats.get("fallbackQuoteCodes") or [],
                "quoteDateMismatchCount": quote_date_mismatch_count,
                "quoteDateMismatchCodes": gate_stats.get("quoteDateMismatchCodes") or [],
                "missingQuoteTimestampCount": missing_quote_timestamp_count,
                "missingQuoteTimestampCodes": gate_stats.get("missingQuoteTimestampCodes") or [],
                "lastPollAt": gate_stats.get("lastPollAt"),
            },
        )
        add(
            "risk_veto",
            "風險否決不外洩",
            gate_today and int(gate_stats.get("riskLeakCount") or 0) == 0,
            (
                "未發現 danger／無效／績效／進場防線否決候選被放行"
                if gate_today and int(gate_stats.get("riskLeakCount") or 0) == 0
                else f"外洩 {int(gate_stats.get('riskLeakCount') or 0)} 檔：{', '.join(gate_stats.get('riskLeakCodes') or [])}"
            ),
            evidence={
                "dangerRiskCount": gate_stats.get("dangerRiskCount"),
                "invalidCandidateCount": gate_stats.get("invalidCandidateCount"),
                "performanceVetoCount": gate_stats.get("performanceVetoCount"),
                "entryGuardrailVetoCount": gate_stats.get("entryGuardrailVetoCount"),
                "riskLeakCount": gate_stats.get("riskLeakCount"),
                "riskLeakCodes": gate_stats.get("riskLeakCodes") or [],
            },
        )
        add(
            "intraday_confirmation",
            "盤中確認",
            gate_today
            and int(gate_stats.get("dailyDataFreshCount") or 0) > 0
            and int(gate_stats.get("marketDataFreshCount") or 0) > 0,
            (
                f"候選日 K 新鮮 {int(gate_stats.get('dailyDataFreshCount') or 0)} 檔，"
                f"全域市場資料通過 {int(gate_stats.get('marketDataFreshCount') or 0)} 檔，"
                f"當日正式曾可買 {int(gate_stats.get('buyableUnion') or 0)} 檔，"
                f"觀察確認 {int(gate_stats.get('shadowBuyableUnion') or 0)} 檔"
            ),
            evidence=gate_stats,
        )

        try:
            snapshot_pipeline = json.loads(
                meta.get(RADAR_ENTRY_SNAPSHOT_PIPELINE_KEY) or "{}"
            )
        except (json.JSONDecodeError, TypeError):
            snapshot_pipeline = {}
        snapshot_today = str(snapshot_pipeline.get("date") or "") == today
        snapshot_ok = bool(
            snapshot_today
            and snapshot_pipeline.get("checkedAt")
            and snapshot_pipeline.get("ok") is True
            and not (snapshot_pipeline.get("missingSymbols") or [])
        )
        if snapshot_today:
            snapshot_detail = (
                f"應保存 {int(snapshot_pipeline.get('expected') or 0)} 檔，"
                f"已確認 {int(snapshot_pipeline.get('persisted') or 0)} 檔，"
                f"本輪新增 {int(snapshot_pipeline.get('inserted') or 0)} 檔，"
                f"重複 {int(snapshot_pipeline.get('duplicates') or 0)} 檔"
            )
            missing_symbols = snapshot_pipeline.get("missingSymbols") or []
            if missing_symbols:
                snapshot_detail += f"；缺漏 {', '.join(map(str, missing_symbols))}"
            if snapshot_pipeline.get("error"):
                snapshot_detail += f"；錯誤 {str(snapshot_pipeline.get('error'))[:240]}"
        else:
            snapshot_detail = "當日尚無紙上快照管線紀錄"
        add(
            "radar_entry_snapshots",
            "紙上快照管線",
            snapshot_ok,
            snapshot_detail,
            evidence=snapshot_pipeline,
        )

        try:
            notification_pipeline = json.loads(meta.get(INTRADAY_NOTIFICATION_PIPELINE_KEY) or "{}")
        except (json.JSONDecodeError, TypeError):
            notification_pipeline = {}
        notification_ok = (
            str(notification_pipeline.get("date") or "") == today
            and not (notification_pipeline.get("errors") or [])
            and isinstance(notification_pipeline.get("entry"), dict)
        )
        add(
            "buy_notifications",
            "買進通知管線",
            notification_ok,
            (
                f"已執行；結果 {notification_pipeline.get('entry')}"
                if notification_ok else "當日通知管線未執行或發生錯誤"
            ),
            evidence=notification_pipeline,
        )

    if stage == "close":
        paper_sessions = paper_signal_snapshot_schedule_status(now=now)
        failed_paper_sessions = [
            item for item in paper_sessions
            if item.get("status") != "done" or int(item.get("errors") or 0) > 0
        ]
        add(
            "paper_snapshot_sessions",
            "模型紙上快照時段",
            bool(paper_sessions) and not failed_paper_sessions,
            (
                f"{len(paper_sessions)} 個時段均完成且無錯誤"
                if paper_sessions and not failed_paper_sessions
                else "未完成：" + "、".join(
                    f"{item.get('label')}({item.get('statusLabel') or item.get('status')})"
                    for item in failed_paper_sessions
                )
            ),
            evidence={"sessions": paper_sessions},
        )
        close_status = str(meta.get("last_official_close_sync_status") or "")
        close_target = str(meta.get("last_official_close_sync_target_date") or "")[:10]
        close_latest = str(meta.get("last_official_close_sync_latest_date") or "")[:10]
        close_count = safe_int(meta.get("last_official_close_sync_latest_count"), 0)
        close_ok = (
            close_target == today
            and close_latest == today
            and close_count >= 1500
            and close_status in {"ready", "success", "completed"}
        )
        add(
            "official_close_sync",
            "官方收盤日 K",
            close_ok,
            f"狀態 {close_status or '-'}，目標 {close_target or '-'}，最新 {close_latest or '-'}（{close_count} 檔）",
            evidence={
                "status": close_status,
                "targetDate": close_target,
                "latestDate": close_latest,
                "latestCount": close_count,
                "error": meta.get("last_official_close_sync_error") or "",
            },
        )

        settlement_at = str(meta.get("last_portfolio_exit_settlement_at") or "")
        settlement_error = str(meta.get("last_portfolio_exit_settlement_error") or "")
        add(
            "close_settlement",
            "收盤結算",
            settlement_at[:10] == today and not settlement_error,
            (
                f"{settlement_at or '尚未執行'}；新增結算 "
                f"{safe_int(meta.get('last_portfolio_exit_settlement_count'), 0)} 筆"
                + (f"；錯誤 {settlement_error}" if settlement_error else "")
            ),
            evidence={
                "settledAt": settlement_at,
                "count": safe_int(meta.get("last_portfolio_exit_settlement_count"), 0),
                "error": settlement_error,
            },
        )

        verified_exit_count = int(exit_snapshot["verified"] or 0) if exit_snapshot else 0
        snapshot_count = int(exit_snapshot["total"] or 0) if exit_snapshot else 0
        sell_notification_ok = verified_exit_count == 0 or exit_log_count > 0
        add(
            "sell_notifications",
            "賣出通知管線",
            sell_notification_ok,
            (
                f"當日 {snapshot_count} 個出場快照，無已驗證賣出訊號"
                if verified_exit_count == 0
                else f"已驗證賣出訊號 {verified_exit_count} 個，通知紀錄 {exit_log_count} 筆"
            ),
            evidence={
                "snapshotCount": snapshot_count,
                "verifiedExitCount": verified_exit_count,
                "notificationLogCount": exit_log_count,
            },
        )
        add(
            "strategy_horizons",
            "持股策略週期",
            int(unknown_horizons or 0) == 0,
            f"仍有 {int(unknown_horizons or 0)} 檔週期未知，未知者禁止正式出場建議",
            required=False,
            evidence={"unknownHorizonPositions": int(unknown_horizons or 0)},
        )

    required_failures = [row for row in checks if row["required"] and not row["ok"]]
    warnings = [row for row in checks if not row["required"] and not row["ok"]]
    return {
        "ok": not required_failures,
        "sessionDate": today,
        "stage": stage,
        "checkedAt": checked_at,
        "marketDay": market_day,
        "checks": checks,
        "failureCount": len(required_failures),
        "warningCount": len(warnings),
        "failures": [row["label"] for row in required_failures],
        "warnings": [row["label"] for row in warnings],
    }


def auto_market_session_validation(stage):
    report = build_market_session_validation(stage)
    if stage == "open" and not report.get("ok"):
        now = taipei_localtime()
        if now.tm_hour * 60 + now.tm_min < 9 * 60 + 14:
            return AUTO_SCHEDULE_RETRY
    saved = backend.record_market_session_validation(report)
    acceptance = None
    if stage == "close":
        acceptance = backend.finalize_market_session_acceptance(
            session_date=report.get("sessionDate"),
            source_validation_id=saved.get("id"),
        )
        backend.record_stability_observation_day(
            report.get("sessionDate"), acceptance
        )
    if not report.get("ok"):
        raise RuntimeError(
            f"{stage} 交易日驗證失敗：{', '.join(report.get('failures') or [])}"
        )
    if stage == "close" and not (acceptance or {}).get("fullDayReady"):
        raise RuntimeError(
            "全日交易日驗收未完成："
            + str((acceptance or {}).get("summary") or "缺少開盤、盤中或收盤證據")
        )
    return (
        f"{stage} 交易日驗證通過，共 {len(report.get('checks') or [])} 項"
        + (f"，{report.get('warningCount')} 項待辦" if report.get("warningCount") else "")
        + ("；全日驗收已封存" if stage == "close" else "")
    )


def auto_intraday_discovery_recall():
    today = scheduler_today(taipei_localtime())
    latest_complete = str(backend.latest_complete_price_date() or "")[:10]
    if latest_complete != today:
        return AUTO_SCHEDULE_RETRY
    report = backend.settle_intraday_discovery_recall(today)
    if report.get("pending"):
        return AUTO_SCHEDULE_RETRY
    candidate_accuracy = backend.compute_intraday_candidate_accuracy(
        lookback_days=365,
    )
    if not candidate_accuracy.get("ok"):
        raise RuntimeError(
            candidate_accuracy.get("error") or "盤中新候選可買準確率結算失敗"
        )
    accuracy_summary = (
        f"；可買訊號已結算 {candidate_accuracy.get('settled', 0)} 筆，"
        f"待結算 {candidate_accuracy.get('pending', 0)} 筆"
    )
    if report.get("skipped"):
        return (
            f"妖股雷達找到率不納入 {today}："
            f"{report.get('reason') or '盤中觀測不完整'}{accuracy_summary}"
        )
    if not report.get("ok"):
        raise RuntimeError(report.get("reason") or "妖股雷達找到率結算失敗")
    return (
        f"妖股雷達找到率已結算：實際強勢 {report.get('actualMovers', 0)} 檔，"
        f"找到 {report.get('detectedMovers', 0)} 檔，"
        f"提早 {report.get('earlyDetected', 0)} 檔，"
        f"發現時仍可交易 {report.get('actionableDetected', 0)} 檔，"
        f"過晚 {report.get('lateDetected', 0)} 檔，"
        f"漏掉 {report.get('missedMovers', 0)} 檔，"
        f"找到率 {float(report.get('recall') or 0) * 100:.1f}%"
        f"{accuracy_summary}"
    )


def missed_schedule_already_notified(job_id, today):
    # 這個判斷之前只存在記憶體 set 裡：使用者當天中午因為部署新程式碼/
    # Windows 更新/手動重啟 server.py（盤中並非罕見），行程重開後這個 set
    # 會回到空集合，同一個已經通知過的「排程視窗被錯過」問題會在下一輪
    # 20 秒迴圈裡重新判定成「還沒通知過」，對使用者重複發送一模一樣的
    # LINE 警示（狼來了效應）。改成跟 auto_schedule_has_run/mark_auto_
    # schedule 同一套模式，寫進 model_meta，跨行程重啟也不會遺失。
    with backend.connect() as conn:
        row = conn.execute(
            "SELECT value FROM model_meta WHERE key = ?",
            (f"auto_schedule_{job_id}_missed_notified_date",),
        ).fetchone()
    return bool(row and str(row[0]) == today)


def mark_missed_schedule_notified(job_id, today):
    with backend.connect() as conn:
        backend.set_meta(conn, f"auto_schedule_{job_id}_missed_notified_date", today)


def check_missed_auto_schedule_windows(now, market_day=None):
    if not (0 <= now.tm_wday <= 4):
        return
    if isinstance(market_day, dict) and not market_schedule_allowed(market_day):
        return
    today = scheduler_today(now)
    minutes = now.tm_hour * 60 + now.tm_min
    for job_id, (start_hour, start_minute, end_hour, end_minute) in AUTO_SCHEDULE_WINDOWS.items():
        allowed_weekdays = AUTO_SCHEDULE_WEEKDAYS.get(job_id)
        if allowed_weekdays is not None and now.tm_wday not in allowed_weekdays:
            continue
        if minutes < (end_hour * 60 + end_minute):
            continue  # 視窗還沒結束，還有機會執行
        if (
            auto_schedule_running.get(job_id)
            or missed_schedule_already_notified(job_id, today)
            or auto_schedule_has_run(job_id, today)
        ):
            continue
        message = (
            f"⚠️ StockAI 排程「{job_id}」今天的時間窗"
            f"（{start_hour:02d}:{start_minute:02d}-{end_hour:02d}:{end_minute:02d}）已結束但完全沒執行過，"
            f"可能是伺服器當時沒開機或程式卡住"
        )
        safe_print(message)
        try:
            send_line_message_via_api(message, priority="critical")  # 排程漏跑=系統故障級警示
            # 只有真的送出成功才標記已通知——之前的寫法是不論送出成功與否
            # 都先標記，LINE推播失敗(例如DNS暫時解析不到、額度/設定問題)
            # 就會讓這則錯過通知永久消失，使用者永遠不會知道。這個迴圈
            # 每20秒跑一次(auto_schedule_worker)，不標記的話下一輪會
            # 自動重試，直到真的送出成功為止。
            mark_missed_schedule_notified(job_id, today)
        except Exception as notify_exc:
            print(f"Auto schedule missed-window notification could not be sent: {notify_exc}")


def catch_up_strategy_calibration_if_needed(now, market_day=None):
    """Run the close-only calibration after a late server start on a trading day."""
    if not (0 <= now.tm_wday <= 4) or not market_schedule_allowed(market_day):
        return False
    minutes = now.tm_hour * 60 + now.tm_min
    if minutes < STRATEGY_CALIBRATION_CATCHUP_MINUTE:
        return False
    today = scheduler_today(now)
    if (
        auto_schedule_running.get(STRATEGY_CALIBRATION_JOB_ID)
        or auto_schedule_has_run(STRATEGY_CALIBRATION_JOB_ID, today)
    ):
        return False

    def catchup_worker():
        message = auto_strategy_calibration()
        return f"{message}｜17:05 排程已於收盤後自動補跑"

    run_auto_schedule_job(STRATEGY_CALIBRATION_JOB_ID, catchup_worker)
    return True


def auto_schedule_worker():
    global monster_intraday_last_update
    while True:
        try:
            now = taipei_localtime()
            today = scheduler_today(now)
            try:
                market_day = official_market_day_status(today)
            except Exception as exc:
                market_day = {"known": False, "isTradingDay": None, "reason": str(exc)[:120]}
            catch_up_strategy_calibration_if_needed(now, market_day=market_day)
            check_missed_auto_schedule_windows(now, market_day=market_day)
            if not market_schedule_allowed(market_day):
                # 休市日不執行晨報、盤中掃描、買賣紙上訊號、停損守門員或模型校準。
                # 收盤同步仍保留，讓它把正式休市狀態寫入 data_freshness；交易日
                # 狀態未知時也只跑這個安全檢查，不放行任何買賣相關排程。
                if auto_schedule_window(now, 14, 35, 18, 30):
                    run_auto_schedule_job(OFFICIAL_CLOSE_SYNC_JOB_ID, auto_official_close_sync)
                time.sleep(20)
                continue
            if auto_schedule_window(now, 8, 15, 8, 50):
                run_auto_schedule_job("0815_morning_brief", auto_morning_brief)
            if auto_schedule_window(now, 8, 45, 9, 5):
                run_auto_schedule_job("0845_data_gap_repair", lambda: auto_data_gap_repair("auto-08:45"))
            if auto_schedule_window(now, 8, 50, 10, 0):
                run_auto_schedule_job("0850_premarket_monster_scan", auto_premarket_monster_scan)
            if auto_schedule_window(now, 9, 5, 9, 15):
                run_auto_schedule_job(
                    "0905_market_session_validation",
                    lambda: auto_market_session_validation("open"),
                )
                run_auto_schedule_job("0905_initial_filter", lambda: auto_monster_checkpoint("09:05 初篩", "開盤初篩候選名單，先排除跳空與風險"))
                run_auto_schedule_job("0905_paper_signal_snapshot", lambda: auto_paper_signal_snapshot("open_0905"))
            if auto_schedule_window(now, 9, 15, 9, 30):
                run_auto_schedule_job("0915_volume_check", lambda: auto_monster_checkpoint("09:15 看量能", "量能確認時間到，候選名單進入量能檢查"))
                run_auto_schedule_job("0915_paper_signal_snapshot", lambda: auto_paper_signal_snapshot("volume_0915"))
            # 整個盤中都更新妖股與持股共用報價；進場判斷仍由 entryWindow 限制在
            # 09:30-13:15。提早與延後抓價只供持股損益、風險監控與盤中資料驗證。
            if auto_schedule_window(now, 9, 0, 13, 30) and time.time() - monster_intraday_last_update >= 30:
                threading.Thread(target=update_monster_intraday_quotes, kwargs={"trigger": "auto-intraday"}, daemon=True).start()
            if auto_schedule_window(now, 9, 0, 13, 30):
                discovery_cooldown = (
                    INTRADAY_DISCOVERY_RETRY_SECONDS
                    if intraday_discovery_status.get("ok") is False
                    else INTRADAY_DISCOVERY_INTERVAL_SECONDS
                )
                if time.time() - intraday_discovery_last_attempt >= discovery_cooldown:
                    threading.Thread(
                        target=update_intraday_market_discovery,
                        kwargs={"trigger": "auto-intraday-discovery"},
                        daemon=True,
                    ).start()
            if auto_schedule_window(now, 9, 30, 10, 0):
                run_auto_schedule_job("0930_paper_signal_snapshot", lambda: auto_paper_signal_snapshot("intraday_0930"))
            if auto_schedule_window(now, 9, 50, 10, 10):
                run_auto_schedule_job(
                    "0950_market_session_validation",
                    lambda: auto_market_session_validation("intraday"),
                )
            # 停損守門員：整個盤中時段(9:00-13:30)都要看著，不限進場窗
            if auto_schedule_window(now, 9, 0, 13, 30):
                try:
                    check_portfolio_exit_guardian()
                except Exception as guardian_exc:
                    print(f"exit guardian check failed: {guardian_exc}")
            if auto_schedule_window(now, 13, 20, 13, 30):
                run_auto_schedule_job("1320_paper_signal_snapshot", lambda: auto_paper_signal_snapshot("preclose_1320"))
            if auto_schedule_window(now, 14, 20, 14, 30):
                run_auto_schedule_job("1420_data_gap_repair", lambda: auto_data_gap_repair("auto-14:20"))
            if auto_schedule_window(now, 14, 35, 18, 30):
                run_auto_schedule_job(OFFICIAL_CLOSE_SYNC_JOB_ID, auto_official_close_sync)
            if auto_schedule_window(now, 15, 0, 16, 0):
                run_auto_schedule_job("1500_monster_scan", auto_monster_scan)
            # 模型與妖股雷達完全拆開：15:00 雷達只跑真實價量規則；15:10
            # 才由獨立模型循環自行重訓並批量存預測，供模型紙上交易使用。
            if auto_schedule_window(now, 15, 10, 17, 0):
                run_auto_schedule_job("1510_batch_predictions", auto_model_cycle)
            if auto_schedule_window(now, 15, 20, 17, 30):
                run_auto_schedule_job("1520_paper_signal_snapshot", auto_paper_signal_snapshot)
            if auto_schedule_window(now, 15, 40, 17, 30):
                run_auto_schedule_job("1540_data_gap_repair", lambda: auto_data_gap_repair("auto-15:40"))
            if auto_schedule_window(now, 17, 5, 17, 30):
                run_auto_schedule_job(STRATEGY_CALIBRATION_JOB_ID, auto_strategy_calibration)
            if auto_schedule_window(now, 17, 35, 18, 30):
                run_auto_schedule_job("1735_daily_digest", auto_daily_digest)
            if auto_schedule_window(now, 18, 0, 18, 30):
                run_auto_schedule_job(
                    "1800_market_session_validation",
                    lambda: auto_market_session_validation("close"),
                )
            if auto_schedule_window(now, 18, 10, 18, 30):
                run_auto_schedule_job(
                    INTRADAY_DISCOVERY_RECALL_JOB_ID,
                    auto_intraday_discovery_recall,
                )
            if now.tm_wday == 4 and auto_schedule_window(now, 18, 40, 20, 30):
                run_auto_schedule_job("1840_weekly_tcn_experiment", auto_weekly_tcn_experiment)
        except Exception as exc:
            # 仿照 daily_update_worker 的防護模式：auto_schedule_has_run 內的資料庫
            # 連線、或任何子函式在某一次循環拋出未預期例外，都不能讓這個 daemon
            # thread 永久死亡（否則需重啟 server.py 才能恢復排程）。
            print(f"auto_schedule_worker loop error: {exc}")
        time.sleep(20)


def auto_monster_checkpoint(phase, message):
    with monster_scan_lock:
        if monster_scan_status.get("running"):
            # 有掃描正在跑（例如手動掃描）時不能把共用狀態蓋掉，
            # 否則前端進度和輪詢都會誤判掃描已結束。回傳 sentinel
            # 讓排程不標記當日已執行，掃描結束後下一輪重試補寫。
            return AUTO_SCHEDULE_RETRY
        monster_scan_status.update({
            "running": False,
            "phase": phase,
            "current": "",
            "finishedAt": time.strftime("%Y-%m-%d %H:%M:%S"),
            "message": message,
            "trigger": "auto",
        })
    cache.clear()
    return message


MONSTER_INTRADAY_NEAR_HIGH_RATIO = 0.98
MONSTER_INTRADAY_MAX_QUOTE_AGE_SECONDS = 180
MONSTER_MAX_BID_ASK_SPREAD_PCT = 0.8
MONSTER_MAX_ESTIMATED_SLIPPAGE_PCT = 0.5
MONSTER_MAX_ORDER_VOLUME_PARTICIPATION = 0.05


def parse_intraday_quote_time(value):
    text = str(value or "").strip()
    if not text:
        return None
    try:
        number = float(text)
        if number > 100_000_000:
            if number > 100_000_000_000_000_000:
                number /= 1_000_000_000
            elif number > 100_000_000_000_000:
                number /= 1_000_000
            elif number > 100_000_000_000:
                number /= 1_000
            return datetime.fromtimestamp(number, TAIPEI_TZ)
    except (TypeError, ValueError, OSError, OverflowError):
        pass
    iso_text = text.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(iso_text)
        return parsed.replace(tzinfo=TAIPEI_TZ) if parsed.tzinfo is None else parsed.astimezone(TAIPEI_TZ)
    except ValueError:
        pass
    for pattern in ("%Y%m%d %H%M%S", "%Y%m%d%H%M%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(text, pattern).replace(tzinfo=TAIPEI_TZ)
        except ValueError:
            continue
    return None


def intraday_quote_freshness(quote, batch_fresh=True, now=None):
    """混合永豐/群益報價逐檔判斷，並回傳 (fresh, age_seconds, reason)。"""
    if not batch_fresh or not isinstance(quote, dict) or not quote:
        return False, None, "batch_or_quote_missing"
    if quote.get("stale") is True:
        return False, None, "quote_marked_stale"
    if quote.get("fresh") is False:
        return False, None, "quote_marked_not_fresh"
    now = now or datetime.now(TAIPEI_TZ)
    if now.tzinfo is None:
        now = now.replace(tzinfo=TAIPEI_TZ)
    else:
        now = now.astimezone(TAIPEI_TZ)
    quote_at = parse_intraday_quote_time(
        quote.get("snapshotAt") or quote.get("quoteTimestamp") or quote.get("receivedAt")
    )
    age_seconds = (now - quote_at).total_seconds() if quote_at else None
    minutes = now.hour * 60 + now.minute
    decision_session = now.weekday() < 5 and (9 * 60) <= minutes <= (13 * 60 + 30)
    if decision_session:
        if quote_at is None:
            return False, None, "missing_quote_timestamp"
        if age_seconds < -30:
            return False, age_seconds, "future_quote_timestamp"
        if age_seconds > MONSTER_INTRADAY_MAX_QUOTE_AGE_SECONDS:
            return False, age_seconds, "quote_too_old"
    return True, age_seconds, ""


def intraday_quote_is_fresh(quote, batch_fresh=True, now=None):
    return intraday_quote_freshness(quote, batch_fresh, now=now)[0]


def _portfolio_number(value, default=None):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if number != number or number in (float("inf"), float("-inf")):
        return default
    return number


def build_intraday_market_discovery(
    baselines, quote_payload, radar_codes=None, now=None,
    result_limit=INTRADAY_DISCOVERY_RESULT_LIMIT,
    requested_symbols=None, scanner_symbols=None,
):
    """Build the whole-market recall layer from fresh broker quotes.

    This stage only recalls and explains candidates.  A new name can become a
    paper/formal signal only after the separate two-snapshot and production-gate
    pass; it never changes the stored radar score or its thresholds here.
    """
    baselines = baselines if isinstance(baselines, dict) else {}
    quote_payload = quote_payload if isinstance(quote_payload, dict) else {}
    quotes = quote_payload.get("quotes") if isinstance(quote_payload.get("quotes"), dict) else {}
    radar_codes = {str(code) for code in (radar_codes or [])}
    now = now or datetime.now(TAIPEI_TZ)
    if now.tzinfo is None:
        now = now.replace(tzinfo=TAIPEI_TZ)
    else:
        now = now.astimezone(TAIPEI_TZ)
    now_tm = now.timetuple()
    volume_rule = early_session_volume_rule("monster", now_tm)
    base_volume_progress = float(volume_rule.get("min") or 0)
    batch_fresh = bool(quote_payload.get("ok", True)) and not bool(quote_payload.get("stale"))
    expected_symbols = list(dict.fromkeys(
        str(symbol).strip() for symbol in (
            requested_symbols if requested_symbols is not None else baselines
        )
        if str(symbol).strip().isdigit() and len(str(symbol).strip()) == 4
    ))
    scanner_symbols = {str(symbol) for symbol in (scanner_symbols or [])}
    rows = []
    audit_rows = []
    received = 0
    fresh_count = 0
    for code in expected_symbols:
        baseline = baselines.get(code)
        audit = {
            "symbol": code,
            "name": str((baseline or {}).get("name") or code),
            "sector": str((baseline or {}).get("sector") or "上市櫃"),
            "inScanner": code in scanner_symbols,
            "inRadar": code in radar_codes,
            "exclusionReasons": [],
        }
        if not isinstance(baseline, dict) or not baseline:
            audit["exclusionReasons"].append({
                "code": "missing_verified_daily_baseline",
                "label": "缺少最新且完整的官方日 K 基準",
            })
            audit_rows.append(audit)
            continue
        quote = quotes.get(code)
        if not isinstance(quote, dict) or not quote:
            audit["exclusionReasons"].append({
                "code": "missing_broker_quote",
                "label": "永豐與群益皆無有效即時報價",
            })
            audit_rows.append(audit)
            continue
        received += 1
        quote_fresh, quote_age, freshness_reason = intraday_quote_freshness(
            quote, batch_fresh, now=now,
        )
        if not quote_fresh:
            audit.update({
                "quoteAgeSeconds": round(float(quote_age), 1)
                if quote_age is not None else None,
                "quoteFreshnessReason": str(freshness_reason or ""),
                "quoteSource": str(quote.get("source") or ""),
            })
            audit["exclusionReasons"].append({
                "code": "stale_or_invalid_quote_time",
                "label": "即時報價時間過期或無法驗證",
            })
            audit_rows.append(audit)
            continue
        fresh_count += 1
        previous_close = _portfolio_number((baseline or {}).get("previousClose"))
        current = _portfolio_number(quote.get("currentPrice"))
        high = _portfolio_number(quote.get("highPrice") or quote.get("high"))
        low = _portfolio_number(quote.get("lowPrice") or quote.get("low"))
        open_price = _portfolio_number(quote.get("openPrice") or quote.get("open"))
        total_volume_lots = _portfolio_number(quote.get("totalVolume") or quote.get("volume"), 0) or 0
        volume_unit = str(quote.get("totalVolumeUnit") or quote.get("volumeUnit") or "").lower()
        if volume_unit in {"share", "shares", "股"}:
            total_volume_lots /= 1000.0
        avg_volume20_lots = _portfolio_number((baseline or {}).get("avgVolume20Lots"), 0) or 0
        avg_turnover20_million = _portfolio_number(
            (baseline or {}).get("avgTurnover20Million"), 0
        ) or 0
        history_days = max(0, int((baseline or {}).get("historyDays") or 0))
        invalid_fields = []
        if not previous_close or previous_close <= 0:
            invalid_fields.append("前收盤價")
        if not current or current <= 0:
            invalid_fields.append("現價")
        if not high or high <= 0:
            invalid_fields.append("最高價")
        if invalid_fields:
            audit["exclusionReasons"].append({
                "code": "invalid_price_fields",
                "label": "缺少有效" + "、".join(invalid_fields),
            })
            audit_rows.append(audit)
            continue
        current_change = ((current / previous_close) - 1) * 100
        high_change = ((high / previous_close) - 1) * 100
        high_retention = current / high if high > 0 else 0
        volume_progress = (
            total_volume_lots / avg_volume20_lots
            if avg_volume20_lots > 0 else None
        )
        turnover_million = current * total_volume_lots * 1000 / 1_000_000
        acceleration = _portfolio_number(
            quote.get("priceAccelerationPctPerMinute")
        )
        scanner_ranks = list(quote.get("scannerRanks") or quote.get("rankTypes") or [])
        suspended = bool(quote.get("isSuspended") or quote.get("suspend"))
        limited_history = bool(
            history_days < INTRADAY_DISCOVERY_LOW_HISTORY_DAYS
            or avg_volume20_lots < 1000.0
            or avg_turnover20_million < 30.0
        )
        if limited_history:
            liquidity_tier = "exception"
            minimum_turnover = INTRADAY_DISCOVERY_MIN_TURNOVER_MILLION
            minimum_volume_progress = (
                max(0.20, base_volume_progress * 0.80)
                if avg_volume20_lots > 0 else 0.0
            )
        elif avg_turnover20_million >= 100.0:
            liquidity_tier = "high"
            minimum_turnover = 20.0
            minimum_volume_progress = max(0.12, base_volume_progress * 0.55)
        elif avg_turnover20_million >= 30.0:
            liquidity_tier = "medium"
            minimum_turnover = 10.0
            minimum_volume_progress = max(0.16, base_volume_progress * 0.70)
        else:
            liquidity_tier = "low"
            minimum_turnover = INTRADAY_DISCOVERY_MIN_TURNOVER_MILLION
            minimum_volume_progress = max(0.25, base_volume_progress * 0.90)
        audit.update({
            "priceDate": str((baseline or {}).get("priceDate") or "")[:10],
            "currentPrice": round(current, 4),
            "currentChangePct": round(current_change, 2),
            "highChangePct": round(high_change, 2),
            "highRetention": round(high_retention, 4),
            "totalVolumeLots": round(total_volume_lots, 2),
            "avgVolume20Lots": round(avg_volume20_lots, 2),
            "volumeProgressRatio": (
                round(volume_progress, 3) if volume_progress is not None else None
            ),
            "turnoverMillion": round(turnover_million, 1),
            "minimumTurnoverMillion": round(minimum_turnover, 1),
            "minimumVolumeProgressRatio": round(minimum_volume_progress, 3),
            "quoteSource": str(
                quote.get("source") or quote_payload.get("source") or "券商報價"
            ),
            "snapshotAt": str(
                quote.get("snapshotAt") or quote.get("quoteTimestamp") or ""
            ),
        })
        if high_change < INTRADAY_DISCOVERY_MIN_HIGH_CHANGE_PCT:
            audit["exclusionReasons"].append({
                "code": "high_change_below_recall_floor",
                "label": (
                    f"盤中最高漲幅 {high_change:.2f}% 未達 "
                    f"{INTRADAY_DISCOVERY_MIN_HIGH_CHANGE_PCT:.1f}% 召回門檻"
                ),
            })
        if suspended:
            audit["exclusionReasons"].append({
                "code": "suspended_quote",
                "label": "暫停交易報價不可列入候選",
            })
        if total_volume_lots <= 0:
            audit["exclusionReasons"].append({
                "code": "zero_intraday_volume",
                "label": "盤中尚無有效成交量",
            })
        if turnover_million < minimum_turnover:
            audit["exclusionReasons"].append({
                "code": "turnover_below_liquidity_floor",
                "label": (
                    f"成交額 {turnover_million:.1f} 百萬未達流動性門檻 "
                    f"{minimum_turnover:.1f} 百萬"
                ),
            })
        volume_signal = bool(
            volume_progress is not None
            and volume_progress >= minimum_volume_progress
        )
        acceleration_signal = bool(
            acceleration is not None
            and acceleration >= INTRADAY_DISCOVERY_MIN_ACCELERATION_PCT_PER_MINUTE
        )
        scanner_signal = bool(scanner_ranks)
        large_turnover_signal = turnover_million >= 100.0
        low_history_live_exception = bool(
            limited_history
            and turnover_million >= INTRADAY_DISCOVERY_MIN_TURNOVER_MILLION
        )
        if not any((
            volume_signal, acceleration_signal, scanner_signal,
            large_turnover_signal, low_history_live_exception,
        )):
            audit["exclusionReasons"].append({
                "code": "no_live_strength_confirmation",
                "label": "量能、價格加速、券商排行與大額成交皆未確認",
            })
        if high_change < INTRADAY_DISCOVERY_STANDARD_HIGH_CHANGE_PCT and not any((
            acceleration_signal, scanner_signal, volume_signal,
            turnover_million >= 30.0, low_history_live_exception,
        )):
            audit["exclusionReasons"].append({
                "code": "emerging_move_not_confirmed",
                "label": "未滿 3% 的起漲訊號缺少加速、排行或量能佐證",
            })
        if audit["exclusionReasons"]:
            audit_rows.append(audit)
            continue
        stage = "strong" if high_change >= INTRADAY_DISCOVERY_STRONG_HIGH_CHANGE_PCT else (
            "early" if high_change >= INTRADAY_DISCOVERY_STANDARD_HIGH_CHANGE_PCT
            else "emerging"
        )
        active = bool(
            stage == "strong"
            and
            current_change >= 3.0
            and high_retention >= INTRADAY_DISCOVERY_HIGH_RETENTION_RATIO
        )
        if current_change < 0:
            status = (
                "盤中曾轉強但目前已翻黑，不追"
                if stage != "strong" else "盤中曾漲逾 5%，目前已翻黑，不追"
            )
            state = "reversed"
        elif stage in {"emerging", "early"} and high_retention >= INTRADAY_DISCOVERY_HIGH_RETENTION_RATIO:
            status = (
                "1.5% 起漲或量價加速，提早觀察"
                if stage == "emerging"
                else "提早發現，尚未達 5% 強勢確認，只觀察"
            )
            state = "early"
        elif stage in {"emerging", "early"}:
            status = "提早出現漲勢但已回落，只保留紀錄"
            state = "faded"
        elif active and current_change >= 8.5:
            status = "接近漲停，盤中發現但不追"
            state = "near_limit"
        elif active:
            status = "盤中仍強，尚未經收盤規則驗證，只觀察"
            state = "active"
        elif high_retention < INTRADAY_DISCOVERY_HIGH_RETENTION_RATIO:
            status = "盤中曾強勢，現已明顯回落，不追"
            state = "faded"
        else:
            status = "盤中曾強勢，目前只觀察"
            state = "watch"
        late_discovery = bool(high_change >= 8.5)
        actionable_at_observation = bool(
            not suspended
            and total_volume_lots > 0
            and current_change < 8.5
            and state not in {"near_limit", "faded", "reversed"}
        )
        trigger_reasons = []
        if volume_signal:
            trigger_reasons.append("volume_progress")
        if acceleration_signal:
            trigger_reasons.append("price_acceleration")
        if scanner_signal:
            trigger_reasons.append("broker_ranking")
        if large_turnover_signal:
            trigger_reasons.append("large_turnover")
        if low_history_live_exception:
            trigger_reasons.append("low_history_live_exception")
        source = str(quote.get("source") or quote_payload.get("source") or "券商報價")
        rows.append({
            "symbol": code,
            "name": str((baseline or {}).get("name") or code),
            "sector": str((baseline or {}).get("sector") or "上市櫃"),
            "priceDate": str((baseline or {}).get("priceDate") or "")[:10],
            "previousClose": round(previous_close, 4),
            "currentPrice": round(current, 4),
            "openPrice": round(open_price, 4) if open_price else None,
            "highPrice": round(high, 4),
            "lowPrice": round(low, 4) if low else None,
            "bidPrice": _portfolio_number(quote.get("bidPrice")),
            "askPrice": _portfolio_number(quote.get("askPrice")),
            "currentChangePct": round(current_change, 2),
            "highChangePct": round(high_change, 2),
            "highRetention": round(high_retention, 4),
            "totalVolumeLots": round(total_volume_lots, 2),
            "avgVolume20Lots": round(avg_volume20_lots, 2),
            "avgTurnover20Million": round(avg_turnover20_million, 2),
            "historyDays": history_days,
            "baselineMode": str((baseline or {}).get("baselineMode") or "daily_history"),
            "baselineException": limited_history,
            "baselineExceptionReason": (
                "新上市或低基期，改採即時成交金額與量能"
                if limited_history else ""
            ),
            "volumeProgressRatio": (
                round(volume_progress, 3) if volume_progress is not None else None
            ),
            "turnoverMillion": round(turnover_million, 1),
            "minimumTurnoverMillion": round(minimum_turnover, 1),
            "minimumVolumeProgressRatio": round(minimum_volume_progress, 3),
            "liquidityTier": liquidity_tier,
            "priceAccelerationPctPerMinute": (
                round(acceleration, 3) if acceleration is not None else None
            ),
            "scannerRanks": scanner_ranks,
            "triggerReasons": trigger_reasons,
            "stage": stage,
            "active": active,
            "state": state,
            "status": status,
            "inRadar": code in radar_codes,
            "discoveryType": "existing_candidate" if code in radar_codes else "new_intraday",
            "observationOnly": True,
            "canBuy": False,
            "isSuspended": suspended,
            "actionableAtObservation": actionable_at_observation,
            "lateDiscovery": late_discovery,
            "quoteFresh": True,
            "quoteAgeSeconds": round(float(quote_age), 1) if quote_age is not None else None,
            "quoteFreshnessReason": str(freshness_reason or ""),
            "snapshotAt": str(quote.get("snapshotAt") or quote.get("quoteTimestamp") or ""),
            "quoteSource": source,
            "source": source,
        })
        audit_rows.append({**audit, "qualified": True})
    state_priority = {
        "near_limit": 0, "active": 1, "watch": 2, "early": 3,
        "faded": 4, "reversed": 5,
    }
    rows.sort(key=lambda item: (
        {"strong": 0, "early": 1, "emerging": 2}.get(item.get("stage"), 3),
        state_priority.get(item["state"], 9),
        -float(item["currentChangePct"]),
        -float(item["highChangePct"]),
        -float(item["turnoverMillion"]),
    ))
    limit = max(1, min(int(result_limit or INTRADAY_DISCOVERY_RESULT_LIMIT), 50))
    return {
        "leaders": rows[:limit],
        "_qualifiedRows": rows,
        "_auditRows": audit_rows,
        "qualified": len(rows),
        "requested": len(expected_symbols),
        "received": received,
        "fresh": fresh_count,
        "coverageComplete": bool(expected_symbols) and fresh_count == len(expected_symbols),
        "minimumHighChangePct": INTRADAY_DISCOVERY_MIN_HIGH_CHANGE_PCT,
        "standardHighChangePct": INTRADAY_DISCOVERY_STANDARD_HIGH_CHANGE_PCT,
        "strongHighChangePct": INTRADAY_DISCOVERY_STRONG_HIGH_CHANGE_PCT,
        "minimumAccelerationPctPerMinute": INTRADAY_DISCOVERY_MIN_ACCELERATION_PCT_PER_MINUTE,
        "minimumVolumeProgressRatio": "dynamic_by_liquidity",
        "minimumTurnoverMillion": INTRADAY_DISCOVERY_MIN_TURNOVER_MILLION,
        "observationOnly": True,
    }


def apply_intraday_discovery_confirmations(rows, previous_status=None, now=None):
    """Require two distinct, recent quote snapshots before calling a move confirmed."""
    rows = rows if isinstance(rows, list) else []
    previous_status = previous_status if isinstance(previous_status, dict) else {}
    previous_rows = {
        str(item.get("symbol") or ""): item
        for item in (
            previous_status.get("confirmationRows")
            or previous_status.get("leaders")
            or []
        )
        if isinstance(item, dict) and item.get("symbol")
    }
    now = now or datetime.now(TAIPEI_TZ)
    if now.tzinfo is None:
        now = now.replace(tzinfo=TAIPEI_TZ)
    else:
        now = now.astimezone(TAIPEI_TZ)
    eligible_states = {"early", "active", "watch", "near_limit"}

    for row in rows:
        state = str(row.get("state") or "")
        symbol = str(row.get("symbol") or "")
        previous = previous_rows.get(symbol) or {}
        current_snapshot = parse_intraday_quote_time(row.get("snapshotAt"))
        previous_snapshot = parse_intraday_quote_time(previous.get("snapshotAt"))
        previous_count = max(0, int(previous.get("confirmationCount") or 0))
        snapshot_advanced = bool(
            current_snapshot
            and previous_snapshot
            and current_snapshot > previous_snapshot
        )
        gap_ok = bool(
            snapshot_advanced
            and (current_snapshot - previous_snapshot).total_seconds()
            <= INTRADAY_DISCOVERY_CONFIRMATION_MAX_GAP_SECONDS
        )
        previous_eligible = str(previous.get("state") or "") in eligible_states
        if state not in eligible_states:
            count = 0
        elif gap_ok and previous_eligible:
            count = min(
                INTRADAY_DISCOVERY_CONFIRMATION_SCANS,
                max(1, previous_count) + 1,
            )
        elif current_snapshot and previous_snapshot and current_snapshot == previous_snapshot:
            count = previous_count
        else:
            count = 1
        confirmed = bool(count >= INTRADAY_DISCOVERY_CONFIRMATION_SCANS)
        row["confirmationCount"] = count
        row["requiredConfirmationCount"] = INTRADAY_DISCOVERY_CONFIRMATION_SCANS
        row["consecutiveConfirmed"] = confirmed
        if state not in eligible_states:
            row["confirmationLabel"] = "未通過連續確認"
        elif confirmed:
            row["confirmationLabel"] = f"連續 {count} 輪確認"
        else:
            row["confirmationLabel"] = (
                f"確認 {count}/{INTRADAY_DISCOVERY_CONFIRMATION_SCANS}，等待下一輪"
            )
        # Confirmation is research telemetry only. It must never create a buy gate.
        row["observationOnly"] = True
        row["canBuy"] = False
    return rows


def load_intraday_discovery_formal_contexts(
    rows, radar_candidates, daily_reference_date="", performance_veto=True,
    market_regime=None, decision_validity=None,
):
    """Load the unchanged daily rule context for confirmed intraday names."""
    global intraday_discovery_formal_context_cache
    contexts = {
        str(item.get("symbol") or ""): dict(item)
        for item in (radar_candidates or [])
        if isinstance(item, dict) and item.get("symbol")
    }
    errors = {}
    disposition = set()
    attention = set()
    try:
        with backend.connect() as conn:
            row = conn.execute(
                "SELECT value FROM model_meta WHERE key = ?",
                ("attention_disposition_cache",),
            ).fetchone()
        disposition, attention = backend._parse_attention_disposition(
            row[0] if row else ""
        )
    except Exception:
        pass
    regime_payload = market_regime if isinstance(market_regime, dict) else {}
    regime_key = str(regime_payload.get("key") or "theme_rotation")
    minimum_formal_score = max(
        RADAR_MIN_FORMAL_SCORE,
        float(radar_regime_threshold(regime_key)),
    )
    validity = decision_validity if isinstance(decision_validity, dict) else {}

    def finalized_context(raw_context, cache_key):
        context = dict(raw_context or {})
        context["performanceVetoed"] = bool(performance_veto)
        context["dailyDataReferenceDate"] = daily_reference_date
        context["marketRegime"] = regime_key
        context["regimeThreshold"] = minimum_formal_score
        context["minimumFormalScore"] = minimum_formal_score
        context["invalidForTrading"] = bool(
            context.get("invalidForTrading")
            or validity.get("invalidForTrading") is True
        )
        intraday_discovery_formal_context_cache[cache_key] = dict(context)
        return context

    eligible = sorted(
        (
            item for item in (rows or [])
            if item.get("consecutiveConfirmed")
            and item.get("actionableAtObservation")
            and not item.get("inRadar")
        ),
        key=lambda item: (
            float(item.get("turnoverMillion") or 0),
            float(item.get("highChangePct") or 0),
        ),
        reverse=True,
    )
    uncached = []
    for item in eligible:
        symbol = str(item.get("symbol") or "")
        price_date = str(item.get("priceDate") or daily_reference_date or "")[:10]
        cache_key = f"{price_date}:{symbol}"
        cached = intraday_discovery_formal_context_cache.get(cache_key)
        if cached is None:
            uncached.append(item)
        else:
            contexts[symbol] = finalized_context(cached, cache_key)

    for item in uncached[:INTRADAY_DISCOVERY_FORMAL_CONTEXT_LIMIT]:
        symbol = str(item.get("symbol") or "")
        price_date = str(item.get("priceDate") or daily_reference_date or "")[:10]
        cache_key = f"{price_date}:{symbol}"
        try:
            context = backend.monster_score_for_symbol(
                symbol,
                prediction=None,
                repair=False,
                use_model=False,
            )
            context = dict(context or {})
            flags = list(context.get("riskFlags") or [])
            if symbol in disposition:
                flags.append({
                    "code": "disposition",
                    "label": "已進處置",
                    "severity": "danger",
                })
            elif symbol in attention:
                flags.append({
                    "code": "attention",
                    "label": "注意股",
                    "severity": "warn",
                })
            context["riskFlags"] = flags
            context["riskVetoed"] = any(
                flag.get("severity") == "danger" for flag in flags
                if isinstance(flag, dict)
            )
            context["policyBuyAllowed"] = bool(context.get("buyAllowed"))
            contexts[symbol] = finalized_context(context, cache_key)
        except Exception as exc:
            errors[symbol] = str(exc)[:300]
    # Daily contexts are immutable until the official close date advances.
    if daily_reference_date:
        intraday_discovery_formal_context_cache = {
            key: value for key, value in intraday_discovery_formal_context_cache.items()
            if key.startswith(f"{daily_reference_date}:")
        }
    return contexts, errors


def apply_intraday_candidate_rules(
    rows, formal_contexts, now_tm=None, market_data_fresh=True,
):
    """Promote only twice-confirmed rows that clear the unchanged formal gates."""
    rows = rows if isinstance(rows, list) else []
    contexts = formal_contexts if isinstance(formal_contexts, dict) else {}
    now_tm = now_tm or taipei_localtime()
    entry_window = monster_entry_window(now_tm)
    for row in rows:
        reasons = []
        symbol = str(row.get("symbol") or "")
        if not row.get("consecutiveConfirmed"):
            reasons.append({
                "code": "candidate_waiting_second_fresh_quote",
                "label": "等待第 2 次不同時間的新鮮報價",
            })
        if float(row.get("highChangePct") or 0) < INTRADAY_DISCOVERY_STANDARD_HIGH_CHANGE_PCT:
            reasons.append({
                "code": "candidate_move_below_three_percent",
                "label": "盤中新候選尚未達 3% 強勢確認",
            })
        if str(row.get("state") or "") in {"near_limit", "faded", "reversed"}:
            reasons.append({
                "code": "candidate_chase_or_fade_blocked",
                "label": "接近漲停、沖高回落或翻黑，不追價",
            })
        if float(row.get("highRetention") or 0) < INTRADAY_DISCOVERY_HIGH_RETENTION_RATIO:
            reasons.append({
                "code": "candidate_high_retention_failed",
                "label": "現價未守住盤中高點附近",
            })
        if float(row.get("currentChangePct") or 0) >= 8.5:
            reasons.append({
                "code": "candidate_near_limit_chase_blocked",
                "label": "目前漲幅已達 8.5%，不追價",
            })
        if float(row.get("turnoverMillion") or 0) < 30.0:
            reasons.append({
                "code": "candidate_formal_turnover_failed",
                "label": "正式候選成交額未達 3,000 萬元",
            })
        volume_progress = _portfolio_number(row.get("volumeProgressRatio"))
        minimum_progress = float(row.get("minimumVolumeProgressRatio") or 0)
        if volume_progress is None or volume_progress < minimum_progress:
            reasons.append({
                "code": "candidate_formal_volume_failed",
                "label": "正式候選量能進度未達動態門檻",
            })
        previous_close = float(row.get("previousClose") or 0)
        open_price = float(row.get("openPrice") or 0)
        open_gap = (
            (open_price / previous_close - 1) * 100
            if previous_close > 0 and open_price > 0 else 0
        )
        if open_gap > 5.0:
            reasons.append({
                "code": "candidate_open_gap_chase_blocked",
                "label": "開高超過 5%，不追價",
            })

        context = contexts.get(symbol)
        formal_state = None
        if not context:
            reasons.append({
                "code": "candidate_formal_context_missing",
                "label": "缺少完整日線正式評分，維持盤中新候選",
            })
        else:
            quote = {
                "currentPrice": row.get("currentPrice"),
                "openPrice": row.get("openPrice"),
                "highPrice": row.get("highPrice"),
                "lowPrice": row.get("lowPrice"),
                "bidPrice": row.get("bidPrice"),
                "askPrice": row.get("askPrice"),
                "totalVolume": row.get("totalVolumeLots"),
                "snapshotAt": row.get("snapshotAt"),
            }
            volume_rule = {
                "min": minimum_progress,
                "label": "盤中召回層動態量能",
                "progress": volume_progress,
                "source": "intraday_discovery",
                "profileSamples": 0,
                "expectedFraction": minimum_progress,
            }
            formal_state = compute_monster_intraday_state(
                symbol,
                {
                    **context,
                    "dailyDataReferenceDate": (
                        context.get("dailyDataReferenceDate")
                        or row.get("priceDate")
                    ),
                },
                quote,
                True,
                entry_window,
                minimum_progress,
                volume_rule,
                row.get("quoteSource") or "券商即時報價",
                quote_fresh=bool(row.get("quoteFresh")),
                market_data_fresh=bool(market_data_fresh),
                quote_age_seconds=row.get("quoteAgeSeconds"),
                quote_freshness_reason=row.get("quoteFreshnessReason") or "",
            )
            if not (formal_state.get("canBuy") or formal_state.get("shadowCanBuy")):
                formal_blocks = [
                    ("invalid_decision", "candidateInvalidForTrading", "雷達決策資料無效"),
                    ("danger_risk", "dangerRisk", "高風險型態否決"),
                    ("entry_guardrail", "candidateEntryGuardrailVetoed", "不追價防線否決"),
                    ("overheated", "candidateOverheated", "短線過熱"),
                    ("score_floor", "scoreFloorBlocked", (
                        f"型態分數未達 {float(formal_state.get('minimumFormalScore') or RADAR_MIN_FORMAL_SCORE):.0f} 分"
                    )),
                    ("daily_data_stale", "candidateDataStaleBlocked", "候選日 K 過期"),
                    ("market_data_stale", "marketDataStaleBlocked", "全市場資料未通過新鮮度檢查"),
                    ("quote_stale", "quoteStaleBlocked", "即時報價過期"),
                    ("spread", "spreadBlocked", "買賣價差過大"),
                    ("slippage", "slippageBlocked", "預估滑價過高"),
                    ("capacity", "capacityBlocked", "成交量不足一張安全成交"),
                    ("entry_drift", "entryDriftBlocked", "實際成交價偏離買點過大"),
                    ("reward_risk", "rewardRiskBlocked", "扣成本後風報未達門檻"),
                    ("entry_window", "windowBlocked", "不在正式進場時段"),
                    ("false_breakout", "falseBreakout", "突破未守住買點"),
                    ("breakout_fade", "breakoutFadeBlocked", "突破後離高點過遠"),
                    ("late_breakout", "lateBreakoutBlocked", "10:00 後不追突破"),
                    ("extended_chase", "chaseBreakoutBlocked", "短線漲多不追突破"),
                ]
                for code_suffix, field, label in formal_blocks:
                    if formal_state.get(field):
                        reasons.append({
                            "code": f"candidate_formal_{code_suffix}",
                            "label": label,
                        })
                if not formal_state.get("setupType"):
                    reasons.append({
                        "code": "candidate_formal_setup_missing",
                        "label": "尚未形成突破、回測或 V 轉進場型態",
                    })
                if not any(
                    str(reason.get("code") or "").startswith("candidate_formal_")
                    for reason in reasons
                ):
                    reasons.append({
                        "code": "candidate_formal_gate_blocked",
                        "label": str(
                            formal_state.get("status") or "正式買進規則未通過"
                        ),
                    })

        ready = not reasons
        row["candidateStage"] = (
            "formal_buyable" if ready and formal_state and formal_state.get("canBuy")
            else "formal_shadow" if ready
            else "waiting_confirmation" if any(
                reason["code"] == "candidate_waiting_second_fresh_quote"
                for reason in reasons
            )
            else "blocked"
        )
        row["candidateExclusionReasons"] = reasons
        row["formalGateStatus"] = (
            formal_state.get("status") if formal_state else "等待完整日線正式評分"
        )
        row["formalScore"] = (
            formal_state.get("candidateScore") if formal_state
            else (context or {}).get("score")
        )
        row["executionEntryPrice"] = (
            formal_state.get("executionEntryPrice") if formal_state else None
        )
        row["formalCanBuy"] = bool(formal_state and formal_state.get("canBuy"))
        row["formalShadowCanBuy"] = bool(
            formal_state and formal_state.get("shadowCanBuy")
        )
        row["candidateSignal"] = bool(
            ready and not row.get("inRadar")
            and formal_state
            and (formal_state.get("canBuy") or formal_state.get("shadowCanBuy"))
        )
        row["canBuy"] = bool(ready and formal_state and formal_state.get("canBuy"))
        row["observationOnly"] = not row["canBuy"]
        if ready and row["candidateSignal"]:
            row["status"] = (
                "盤中新候選已通過正式規則，可買"
                if row["canBuy"]
                else "盤中新候選已通過正式規則，先做紙上驗證"
            )
    return rows


def merge_intraday_tick_quotes(base_quotes, tick_quotes):
    """Overlay subscribed 20-second tick snapshots on scanner/day snapshots."""
    merged = {
        str(code): dict(quote or {})
        for code, quote in (base_quotes or {}).items()
        if code and isinstance(quote, dict)
    }
    for code, tick in (tick_quotes or {}).items():
        if not isinstance(tick, dict):
            continue
        code = str(code)
        base = merged.get(code, {})
        base_high = _portfolio_number(base.get("highPrice") or base.get("high"), 0) or 0
        tick_high = _portfolio_number(tick.get("highPrice") or tick.get("high"), 0) or 0
        base_low = _portfolio_number(base.get("lowPrice") or base.get("low"), 0) or 0
        tick_low = _portfolio_number(tick.get("lowPrice") or tick.get("low"), 0) or 0
        base_volume = _portfolio_number(base.get("totalVolume") or base.get("volume"), 0) or 0
        tick_volume = _portfolio_number(tick.get("totalVolume") or tick.get("volume"), 0) or 0
        positive_lows = [value for value in (base_low, tick_low) if value > 0]
        merged[code] = {
            **base,
            **tick,
            "openPrice": base.get("openPrice") or base.get("open") or tick.get("openPrice"),
            "highPrice": max(base_high, tick_high) or None,
            "lowPrice": min(positive_lows) if positive_lows else None,
            "totalVolume": max(base_volume, tick_volume),
            "totalVolumeUnit": "lots",
            "source": "Shioaji scanner + realtime tick",
        }
    return merged


def apply_intraday_quote_acceleration(quotes, history=None):
    """Annotate distinct rotating snapshots with price-change acceleration."""
    history = intraday_discovery_quote_history if history is None else history
    for raw_code, raw_quote in (quotes or {}).items():
        if not isinstance(raw_quote, dict):
            continue
        code = str(raw_code)
        quote = raw_quote
        snapshot_at = parse_intraday_quote_time(
            quote.get("snapshotAt") or quote.get("receivedAt")
        )
        current = _portfolio_number(quote.get("currentPrice"))
        reference = _portfolio_number(quote.get("referencePrice"))
        previous = history.get(code) or {}
        acceleration = previous.get("acceleration")
        if snapshot_at and current and reference and reference > 0:
            change_pct = (current / reference - 1.0) * 100.0
            previous_at = previous.get("snapshotAt")
            previous_change = _portfolio_number(previous.get("changePct"))
            if previous_at and snapshot_at > previous_at and previous_change is not None:
                elapsed_minutes = (snapshot_at - previous_at).total_seconds() / 60.0
                if 0 < elapsed_minutes <= 5:
                    acceleration = (change_pct - previous_change) / elapsed_minutes
            if not previous_at or snapshot_at >= previous_at:
                history[code] = {
                    "snapshotAt": snapshot_at,
                    "changePct": change_pct,
                    "acceleration": acceleration,
                }
        quote["priceAccelerationPctPerMinute"] = (
            round(float(acceleration), 4) if acceleration is not None else None
        )
    return quotes


def intraday_discovery_baselines_for_quotes(symbols, quotes):
    """Use cached daily baselines and a live-reference exception for new listings."""
    global intraday_discovery_baseline_cache
    eligible = backend.listed_symbols()
    price_date = str(backend.latest_complete_price_date() or "")[:10]
    cache = intraday_discovery_baseline_cache
    if cache.get("priceDate") != price_date or not cache.get("baselines"):
        cache = {
            "priceDate": price_date,
            "baselines": backend.intraday_discovery_baselines(eligible),
            "metadata": backend.intraday_discovery_metadata(eligible),
        }
        intraday_discovery_baseline_cache = cache
    historical = cache.get("baselines") or {}
    metadata = cache.get("metadata") or {}
    output = {}
    for code in symbols or []:
        code = str(code)
        if code in historical:
            output[code] = dict(historical[code])
            continue
        quote = (quotes or {}).get(code) or {}
        reference = _portfolio_number(quote.get("referencePrice"))
        if not reference or reference <= 0:
            continue
        info = metadata.get(code) or {}
        output[code] = {
            "symbol": code,
            "name": str(info.get("name") or quote.get("name") or code),
            "sector": str(info.get("sector") or "上市櫃"),
            "marketType": str(info.get("marketType") or ""),
            "priceDate": price_date,
            "previousClose": reference,
            "avgVolume20Lots": 0.0,
            "avgTurnover20Million": 0.0,
            "historyDays": 0,
            "baselineMode": "live_reference_exception",
            "priceSource": str(quote.get("source") or "Shioaji live reference"),
        }
    return output


def _portfolio_holding_shares(holding):
    direct = _portfolio_number((holding or {}).get("shares"))
    if direct is not None and direct > 0:
        return direct
    lots = _portfolio_number((holding or {}).get("quantity"), 0) or 0
    return max(0, lots * 1000)


def merge_portfolio_quote_snapshot(summary, holdings, quotes, quote_payload=None, now=None):
    """Merge one broker quote batch into the cached positions without changing quantities.

    Inventory/account cash still comes from the last verified holdings sync. Only market
    fields and derived unrealized P/L are refreshed here, so the mobile page can stay live
    without opening the desktop page or performing a second Shioaji login.
    """
    summary = dict(summary or {})
    holdings = holdings if isinstance(holdings, dict) else {}
    quotes = quotes if isinstance(quotes, dict) else {}
    quote_payload = quote_payload if isinstance(quote_payload, dict) else {}
    now = now or datetime.now(TAIPEI_TZ)
    if now.tzinfo is None:
        now = now.replace(tzinfo=TAIPEI_TZ)
    else:
        now = now.astimezone(TAIPEI_TZ)
    checked_at = now.strftime("%Y-%m-%d %H:%M:%S")
    batch_fresh = bool(quote_payload.get("ok", True)) and not bool(quote_payload.get("stale"))
    merged_holdings = {}
    requested = []
    received = []
    fresh_codes = []
    missing = []
    sources = []

    market_fields = {
        "currentPrice": ("currentPrice", "price"),
        "referencePrice": ("referencePrice", "reference"),
        "changePrice": ("changePrice", "change_price"),
        "changeRate": ("changeRate", "changePct", "change_rate"),
        "openPrice": ("openPrice", "open"),
        "highPrice": ("highPrice", "high"),
        "lowPrice": ("lowPrice", "low"),
        "bidPrice": ("bidPrice", "bid_price"),
        "askPrice": ("askPrice", "ask_price"),
        "totalVolume": ("totalVolume", "total_volume", "volume"),
    }

    for key, raw_holding in holdings.items():
        holding = dict(raw_holding or {})
        code = str(holding.get("code") or key).replace(".TWO", "").replace(".TW", "").strip()
        if not code:
            continue
        requested.append(code)
        holding["code"] = code
        holding["quoteCheckedAt"] = checked_at
        quote = quotes.get(code)
        if isinstance(quote, dict) and quote:
            received.append(code)
            fresh, age_seconds, reason = intraday_quote_freshness(quote, batch_fresh, now=now)
            source = str(quote.get("source") or quote_payload.get("source") or "券商報價").strip()
            if source and source not in sources:
                sources.append(source)
            stamp = quote.get("snapshotAt") or quote.get("quoteTimestamp") or quote.get("receivedAt")
            parsed_stamp = parse_intraday_quote_time(stamp)
            holding.update({
                "quoteFresh": bool(fresh),
                "quoteFreshnessReason": str(reason or ""),
                "quoteAgeSeconds": round(float(age_seconds), 1) if age_seconds is not None else None,
                "quoteSource": source,
                "marketDataSource": source,
                "quoteAt": parsed_stamp.strftime("%Y-%m-%d %H:%M:%S") if parsed_stamp else str(stamp or ""),
                "quoteDate": parsed_stamp.date().isoformat() if parsed_stamp else "",
                "receivedAt": str(quote.get("receivedAt") or checked_at),
            })
            volume_unit = str(
                quote.get("totalVolumeUnit") or quote.get("volumeUnit") or ""
            ).strip().lower()
            if volume_unit:
                holding["totalVolumeUnit"] = volume_unit
            elif "shioaji" in source.lower():
                holding["totalVolumeUnit"] = "lots"
            if fresh:
                fresh_codes.append(code)
                holding["brokerPnl"] = holding.get("brokerPnl", holding.get("pnl"))
                for target, source_keys in market_fields.items():
                    value = next((quote.get(source_key) for source_key in source_keys if quote.get(source_key) is not None), None)
                    number = _portfolio_number(value)
                    if number is not None:
                        holding[target] = number
                holding["snapshotAt"] = str(stamp or checked_at)
                current_price = _portfolio_number(holding.get("currentPrice"))
                cost_price = _portfolio_number(holding.get("price"))
                shares = _portfolio_holding_shares(holding)
                if current_price is not None and cost_price is not None and shares > 0:
                    holding["pnl"] = (current_price - cost_price) * shares
                reference = _portfolio_number(holding.get("referencePrice"))
                if reference and current_price is not None and quote.get("changeRate") is None:
                    holding["changeRate"] = ((current_price - reference) / reference) * 100
        else:
            missing.append(code)
            holding.update({
                "quoteFresh": False,
                "quoteFreshnessReason": "missing_quote",
                "quoteAgeSeconds": None,
            })
        merged_holdings[code] = holding

    total_cost = 0.0
    current_value = 0.0
    total_pnl = 0.0
    pnl_known = False
    for holding in merged_holdings.values():
        shares = _portfolio_holding_shares(holding)
        cost = _portfolio_number(holding.get("price"), 0) or 0
        current = _portfolio_number(holding.get("currentPrice"), cost) or cost
        total_cost += shares * cost
        current_value += shares * current
        pnl = _portfolio_number(holding.get("pnl"))
        if pnl is not None:
            total_pnl += pnl
            pnl_known = True
    if not pnl_known:
        total_pnl = current_value - total_cost

    coverage_complete = bool(requested) and len(fresh_codes) == len(requested)
    summary.update({
        "count": len(merged_holdings),
        "totalCost": total_cost,
        "currentValue": current_value,
        "totalPnl": total_pnl,
        "returnRate": (total_pnl / total_cost * 100) if total_cost else None,
        "quoteCheckedAt": checked_at,
        "quoteUpdatedAt": checked_at if fresh_codes else summary.get("quoteUpdatedAt"),
        "quoteDate": max(
            (str(item.get("quoteDate") or "") for item in merged_holdings.values()),
            default="",
        ),
        "quoteSource": " + ".join(sources) or str(quote_payload.get("source") or ""),
        "quoteFresh": coverage_complete,
        "quoteStale": not coverage_complete,
        "quoteCoverage": {
            "requested": len(requested),
            "received": len(received),
            "fresh": len(fresh_codes),
            "missing": missing,
            "complete": coverage_complete,
        },
    })
    return {
        "ok": True,
        "summary": summary,
        "holdings": merged_holdings,
        "requested": len(requested),
        "received": len(received),
        "fresh": len(fresh_codes),
        "missing": missing,
        "complete": coverage_complete,
        "updatedAt": checked_at,
        "source": summary.get("quoteSource") or "",
    }


def update_portfolio_summary_quote_cache(quotes, quote_payload=None, now=None):
    with portfolio_summary_cache_lock:
        summary, holdings = load_portfolio_summary_cache()
        if not holdings:
            return {"ok": True, "skipped": "no_portfolio_cache", "requested": 0, "fresh": 0}
        result = merge_portfolio_quote_snapshot(summary, holdings, quotes, quote_payload, now=now)
        stored = {"summary": result["summary"], "holdings": result["holdings"]}
        raw = json.dumps(stored, ensure_ascii=False)
        if len(raw) > 120000:
            raise ValueError("portfolio quote cache payload too large")
        with backend.connect() as conn:
            backend.set_meta(conn, "portfolio_summary_cache", raw)
    return {key: value for key, value in result.items() if key not in {"summary", "holdings"}}


def compute_monster_intraday_state(
    code, item, quote, has_quote, entry_window, volume_threshold, volume_rule, quote_source,
    quote_fresh=True, market_data_fresh=True, quote_age_seconds=None, quote_freshness_reason="",
):
    """單一候選股的盤中「能不能買」判斷——純函式，不碰資料庫/網路/全域狀態，
    輸入輸出都是普通 dict，方便寫整合測試涵蓋完整判斷鏈(而不是只測單一子函式)。
    這裡的邏輯必須跟以前 inline 在 update_monster_intraday_quotes 迴圈裡時完全
    一致，不要讓兩邊各改各的、悄悄漂移。"""
    quote = quote or {}
    previous_close = float(item.get("close") or 0)
    current = float(quote.get("currentPrice") or previous_close or 0)
    open_price = float(quote.get("openPrice") or current or 0)
    high = float(quote.get("highPrice") or current or 0)
    low = float(quote.get("lowPrice") or current or 0)
    volume_lots = float(quote.get("totalVolume") or 0)
    bid_price = float(quote.get("bidPrice") or 0)
    ask_price = float(quote.get("askPrice") or 0)
    avg_volume = float(item.get("avg_volume20_lots") or item.get("avgVolume20Lots") or 0)
    buy_trigger = float(item.get("buy_trigger") or item.get("buyTrigger") or previous_close or 0)
    pullback_price = float(item.get("pullback_price") or item.get("pullbackPrice") or previous_close or 0)
    # stop_price/stopPrice 缺值(例如資料庫該筆為 NULL)時不能落到 0，
    # 否則 stop_broken 恆為 False、v_rebound 的停損檢查也會被跳過，
    # 停損保護形同靜默停用。比照 liquid_fallback 候選的慣例，用
    # 前收盤價的 93% 當保底停損。
    stop_price = float(item.get("stop_price") or item.get("stopPrice") or (previous_close * 0.93 if previous_close else 0))
    open_gap = ((open_price - previous_close) / previous_close) * 100 if previous_close else 0
    volume_ratio = volume_lots / avg_volume if avg_volume else None
    spread_known = bool(bid_price > 0 and ask_price >= bid_price)
    spread_mid = (bid_price + ask_price) / 2 if spread_known else 0
    bid_ask_spread_pct = ((ask_price - bid_price) / spread_mid * 100) if spread_mid > 0 else None
    estimated_slippage_pct = max(
        0.1,
        (bid_ask_spread_pct or 0) / 2,
    )
    spread_blocked = bool(
        spread_known and bid_ask_spread_pct is not None
        and bid_ask_spread_pct > MONSTER_MAX_BID_ASK_SPREAD_PCT
    )
    slippage_blocked = bool(estimated_slippage_pct > MONSTER_MAX_ESTIMATED_SLIPPAGE_PCT)
    order_volume_participation = (1.0 / volume_lots) if volume_lots > 0 else None
    capacity_blocked = bool(
        order_volume_participation is not None
        and order_volume_participation > MONSTER_MAX_ORDER_VOLUME_PARTICIPATION
    )
    dip_phase = entry_window.get("phase") == "dip"
    open_high_fade = open_price > previous_close and current < open_price * 0.985 if previous_close else False
    # 突破不是盤中「曾經碰到」就成立；最高價觸及後必須由現價守在買點上，
    # 否則是常見的假突破/沖高回落，不能被 pullback 或 V 轉分支洗回可買。
    breakout_touched = bool(buy_trigger and high >= buy_trigger)
    breakout_retained = bool(buy_trigger and current >= buy_trigger)
    breakout = breakout_touched and breakout_retained
    false_breakout = breakout_touched and not breakout_retained
    breakout_near_high = bool(high > 0 and current >= high * MONSTER_INTRADAY_NEAR_HIGH_RATIO)
    high_fade_rate = ((high - current) / high) * 100 if high > 0 else None
    # 守住觸發價不代表突破仍然健康。若曾突破且現價還在觸發價上方，
    # 但已從當日高點回落超過 2%，舊邏輯會改走 pullback 分支洗回可買。
    # 這類沖高回落先硬擋，等重新貼近高點或真正回測後下一輪再判斷。
    breakout_fade_blocked = bool(
        breakout_touched
        and breakout_retained
        and not breakout_near_high
    )
    stop_broken = current <= stop_price if stop_price else False
    too_high = open_gap > 5
    volume_continue = volume_ratio is not None and volume_ratio >= volume_threshold
    pullback_volume_ok = volume_ratio is not None and volume_ratio >= volume_threshold * 0.75
    pullback_hold = bool(
        pullback_price
        and current >= pullback_price * 0.995
        and (not buy_trigger or current <= buy_trigger * 1.01)
    )
    rebound_threshold = 1.002 if dip_phase else 1.006
    intraday_rebound = current >= low * rebound_threshold if low else False
    range_reclaim_price = (low + (high - low) * 0.55) if high and low and high > low else (low * 1.006 if low else 0)
    v_rebound = bool(
        intraday_rebound
        and range_reclaim_price
        and current >= range_reclaim_price
        and pullback_volume_ok
        and (not buy_trigger or current <= buy_trigger * 1.02)
        and (not stop_price or current > stop_price * 1.01)
    )
    candidate_overheated = bool(item.get("overheated"))
    candidate_invalid_for_trading = bool(item.get("invalidForTrading") or item.get("invalid_for_trading"))
    candidate_performance_vetoed = bool(item.get("performanceVetoed") or item.get("performance_vetoed"))
    candidate_entry_guardrail_vetoed = bool(
        item.get("entryGuardrailVetoed") or item.get("entry_guardrail_vetoed")
    )
    candidate_score = float(item.get("score") or 0)
    try:
        candidate_minimum_score = max(
            RADAR_MIN_FORMAL_SCORE,
            float(item.get("minimumFormalScore") or item.get("regimeThreshold") or RADAR_MIN_FORMAL_SCORE),
        )
    except (TypeError, ValueError):
        candidate_minimum_score = RADAR_MIN_FORMAL_SCORE
    score_floor_blocked = candidate_score < candidate_minimum_score
    candidate_data_date = str(item.get("price_date") or item.get("priceDate") or "")[:10]
    daily_data_reference_date = str(item.get("dailyDataReferenceDate") or "")[:10]
    # 參考日由 runtime 讀 prices 表的全市場最新日K決定，不自己猜週末/連假。
    # 沒有參考日的純函式/舊資料呼叫維持 fail-open；正式盤中更新一定會帶入。
    candidate_daily_data_fresh = bool(
        not daily_data_reference_date
        or (candidate_data_date and candidate_data_date >= daily_data_reference_date)
    )
    candidate_data_stale_blocked = not candidate_daily_data_fresh
    risk_flags = item.get("riskFlags") or item.get("risk_flags") or []
    if isinstance(risk_flags, str):
        try:
            risk_flags = json.loads(risk_flags)
        except (json.JSONDecodeError, TypeError):
            risk_flags = []
    danger_risk = bool(item.get("riskVetoed") or any(
        isinstance(flag, dict) and flag.get("severity") == "danger"
        for flag in (risk_flags if isinstance(risk_flags, list) else [])
    ))
    extended_runup = any(
        isinstance(flag, dict) and flag.get("code") == "extended_runup"
        for flag in (risk_flags if isinstance(risk_flags, list) else [])
    )
    # 盤中只可用「完整日線結構」補救，不可用高分數單獨補救。分數主要是
    # 排序用，單靠分數沒有確認突破/逆勢結構；否則舊掃描資料 buyAllowed=False
    # 仍會被盤中一根價格突破洗回可買。過熱與 danger 均為硬否決。
    candidate_policy_buy_allowed = bool(
        item.get("policyBuyAllowed")
        if item.get("policyBuyAllowed") is not None
        else (
            item.get("recordedBuyAllowed")
            or item.get("buyAllowed")
            or item.get("buy_allowed")
        )
    )
    shadow_formal_watch_allowed = bool(
        not candidate_invalid_for_trading
        and not candidate_entry_guardrail_vetoed
        and not danger_risk
        and not candidate_overheated
        and not score_floor_blocked
        and (
            item.get("surgeSetup")
            or item.get("counterTrendStrength")
        )
    )
    formal_watch_allowed = bool(
        not candidate_performance_vetoed and shadow_formal_watch_allowed
    )
    shadow_schedule_allowed = bool(
        not candidate_invalid_for_trading
        and not candidate_entry_guardrail_vetoed
        and not danger_risk
        and not candidate_overheated
        and not score_floor_blocked
        and (candidate_policy_buy_allowed or shadow_formal_watch_allowed)
    )
    schedule_allowed = bool(
        not candidate_performance_vetoed and shadow_schedule_allowed
    )
    breakout_setup = breakout and volume_continue and breakout_near_high
    pullback_setup = pullback_hold and pullback_volume_ok and intraday_rebound
    if dip_phase:
        setup_type = "v_rebound" if v_rebound else ("pullback" if pullback_setup else ("breakout" if breakout_setup else ""))
    else:
        setup_type = "breakout" if breakout_setup else ("pullback" if pullback_setup else ("v_rebound" if v_rebound else ""))
    # 真正成交口徑：有賣一價就以賣一價買進；缺賣一才用現價加估計滑價。
    # 這個價格同時交給進場快照與成本後風報，避免畫面判斷用 current、回測
    # 卻記 ask/current+slippage，導致同一筆交易有兩個不同進場價。
    execution_entry_price = (
        ask_price if ask_price > 0
        else current * (1 + estimated_slippage_pct / 100) if current > 0
        else 0.0
    )
    execution_analysis = radar_execution_analysis(
        execution_entry_price,
        planned_stop_price=stop_price,
        estimated_exit_slippage_pct=estimated_slippage_pct,
    )
    entry_drift_pct = (
        ((execution_entry_price - buy_trigger) / buy_trigger) * 100
        if execution_entry_price > 0 and buy_trigger > 0 else None
    )
    # 回測/V轉本來就限制在買點附近；突破分支補上相同的 2% 追價上限。
    entry_drift_blocked = bool(
        setup_type == "breakout"
        and entry_drift_pct is not None
        and entry_drift_pct > RADAR_MAX_ENTRY_DRIFT_PCT
    )
    reward_risk_blocked = not bool(execution_analysis.get("rewardRiskPassed"))
    # 沒有即時報價就不應以昨收/快取行情產生「可買」；只允許保留在觀察狀態。
    quote_missing_blocked = not bool(has_quote)
    # 永豐失敗時最多可回傳 5 分鐘的 stale fallback；可供 UI 顯示最後價格，
    # 但不能拿來產生新買賣決策。20 秒內的正常 cache 則 quote_fresh=True。
    quote_stale_blocked = not bool(quote_fresh)
    market_data_stale_blocked = not bool(market_data_fresh)
    setup_conditions_ok = bool(
        not quote_missing_blocked
        and not quote_stale_blocked
        and not candidate_data_stale_blocked
        and not market_data_stale_blocked
        and bool(setup_type)
        and not too_high
        and not open_high_fade
        and not stop_broken
    )
    shadow_setup_ok = bool(setup_conditions_ok and shadow_schedule_allowed)
    setup_ok = bool(setup_conditions_ok and schedule_allowed)
    late_breakout_blocked = entry_window.get("phase") == "dip" and setup_type == "breakout"
    # 5 日已漲多仍可列入雷達觀察，但禁止在盤中突破加速段追價；回測與
    # V 轉可用較佳風報比進場，所以保留。這只影響盤中實際放行，不改日線回測排名。
    chase_breakout_blocked = extended_runup and setup_type == "breakout"
    window_blocked = not entry_window.get("active")
    execution_gates_passed = bool(
        not false_breakout
        and not breakout_fade_blocked
        and not spread_blocked
        and not slippage_blocked
        and not capacity_blocked
        and not entry_drift_blocked
        and not reward_risk_blocked
        and not late_breakout_blocked
        and not chase_breakout_blocked
        and not window_blocked
    )
    shadow_can_buy = bool(shadow_setup_ok and execution_gates_passed)
    can_buy = bool(setup_ok and execution_gates_passed)
    if can_buy:
        if setup_type == "pullback":
            status = "回測低接可觀察"
        elif setup_type == "v_rebound":
            status = "V轉低接可觀察"
        else:
            status = "突破可觀察"
    elif candidate_invalid_for_trading:
        status = "雷達決策資料無效，僅保留稽核"
    elif danger_risk:
        status = "高風險型態，只觀察不追"
    elif candidate_entry_guardrail_vetoed:
        status = "不追價進場防線否決，只觀察"
    elif candidate_performance_vetoed:
        status = "盤中報價戰績與 walk-forward 尚未通過，只觀察"
    elif candidate_overheated:
        status = "短線過熱，只觀察不追"
    elif score_floor_blocked:
        status = f"型態分數未達 {candidate_minimum_score:.0f} 分（目前門檻），只觀察"
    elif window_blocked:
        status = str(entry_window.get("label") or "不在進場時段")
    elif candidate_data_stale_blocked:
        status = "候選日線資料非最新，僅觀察"
    elif market_data_stale_blocked:
        status = "市場日線資料未通過新鮮度檢查，僅觀察"
    elif quote_missing_blocked:
        status = "等待即時報價確認"
    elif quote_stale_blocked:
        status = "即時報價已過期，僅觀察"
    elif spread_blocked:
        status = "買賣價差過大，僅觀察"
    elif slippage_blocked:
        status = "預估滑價過高，僅觀察"
    elif capacity_blocked:
        status = "即時成交量不足一張安全成交，僅觀察"
    elif false_breakout and setup_ok:
        status = "突破未守住買點，等待重新站回"
    elif breakout_fade_blocked:
        status = "突破後離高點過遠，等待重新轉強"
    elif late_breakout_blocked and setup_ok:
        status = "10:00 後不追突破，等回測/V轉"
    elif chase_breakout_blocked and setup_ok:
        status = "5日漲多，不追盤中突破，等回測/V轉"
    elif entry_drift_blocked:
        status = f"實際成交價超過買點 {RADAR_MAX_ENTRY_DRIFT_PCT:.0f}%，不追價"
    elif reward_risk_blocked:
        status = f"成本後風報低於 {RADAR_MIN_NET_REWARD_RISK_RATIO:.2f}，僅觀察"
    else:
        status = "未通過開盤確認"
    return {
        "code": code,
        "hasIntradayQuote": bool(has_quote),
        "currentPrice": current,
        "openPrice": open_price,
        "highPrice": high,
        "lowPrice": low,
        "totalVolume": volume_lots,
        "bidPrice": bid_price or None,
        "askPrice": ask_price or None,
        "bidAskSpreadPct": bid_ask_spread_pct,
        "estimatedSlippagePct": estimated_slippage_pct,
        "spreadBlocked": spread_blocked,
        "slippageBlocked": slippage_blocked,
        "orderVolumeParticipation": order_volume_participation,
        "capacityBlocked": capacity_blocked,
        "executionEntryPrice": execution_entry_price or None,
        "executionStopPrice": execution_analysis.get("stopPrice"),
        "executionTargetPrice": execution_analysis.get("targetPrice"),
        "targetNetReturnPct": execution_analysis.get("targetNetReturnPct"),
        "stopNetReturnPct": execution_analysis.get("stopNetReturnPct"),
        "netRewardRiskRatio": execution_analysis.get("netRewardRiskRatio"),
        "minimumNetRewardRiskRatio": RADAR_MIN_NET_REWARD_RISK_RATIO,
        "rewardRiskPassed": bool(execution_analysis.get("rewardRiskPassed")),
        "rewardRiskBlocked": reward_risk_blocked,
        "entryDriftPct": entry_drift_pct,
        "maximumEntryDriftPct": RADAR_MAX_ENTRY_DRIFT_PCT,
        "entryDriftBlocked": entry_drift_blocked,
        "volumeRatio": volume_ratio,
        "volumeThreshold": volume_threshold,
        "volumeRule": volume_rule["label"],
        "volumeRuleProgress": volume_rule["progress"],
        "volumeRuleSource": volume_rule.get("source") or "session_curve",
        "volumeProfileSamples": int(volume_rule.get("profileSamples") or 0),
        "volumeExpectedFraction": volume_rule.get("expectedFraction"),
        "openGap": open_gap,
        "pullbackPrice": pullback_price,
        "pullbackHold": pullback_hold,
        "pullbackVolumeOk": pullback_volume_ok,
        "intradayRebound": intraday_rebound,
        "rangeReclaimPrice": range_reclaim_price,
        "vRebound": v_rebound,
        "breakoutTouched": breakout_touched,
        "breakoutRetained": breakout_retained,
        "breakoutNearHigh": breakout_near_high,
        "breakoutFadeBlocked": breakout_fade_blocked,
        "highFadeRate": high_fade_rate,
        "breakout": breakout,
        "falseBreakout": false_breakout,
        "tooHigh": too_high,
        "openHighFade": open_high_fade,
        "stopBroken": stop_broken,
        "volumeContinue": volume_continue,
        "setupType": setup_type,
        "dangerRisk": danger_risk,
        "candidateOverheated": candidate_overheated,
        "candidateInvalidForTrading": candidate_invalid_for_trading,
        "candidatePerformanceVetoed": candidate_performance_vetoed,
        "candidateEntryGuardrailVetoed": candidate_entry_guardrail_vetoed,
        "candidateScore": candidate_score,
        "minimumFormalScore": candidate_minimum_score,
        "baseMinimumFormalScore": RADAR_MIN_FORMAL_SCORE,
        "marketRegime": item.get("marketRegime") or item.get("market_regime"),
        "marketRegimeLabel": item.get("marketRegimeLabel"),
        "themeHeat": item.get("themeHeat") or item.get("theme_heat"),
        "scoreFloorBlocked": score_floor_blocked,
        "extendedRunup": extended_runup,
        "candidateDataDate": candidate_data_date,
        "dailyDataReferenceDate": daily_data_reference_date,
        "candidateDailyDataFresh": candidate_daily_data_fresh,
        "candidateDataStaleBlocked": candidate_data_stale_blocked,
        "marketDataFresh": bool(market_data_fresh),
        "marketDataStaleBlocked": market_data_stale_blocked,
        "scheduleAllowed": schedule_allowed,
        "formalWatchAllowed": formal_watch_allowed,
        "shadowScheduleAllowed": shadow_schedule_allowed,
        "shadowFormalWatchAllowed": shadow_formal_watch_allowed,
        "setupOk": setup_ok,
        "shadowSetupOk": shadow_setup_ok,
        "entryWindowPhase": entry_window.get("phase"),
        "entryWindowLabel": entry_window.get("label"),
        "windowBlocked": window_blocked,
        "quoteMissingBlocked": quote_missing_blocked,
        "quoteFresh": bool(quote_fresh),
        "quoteAgeSeconds": quote_age_seconds,
        "quoteFreshnessReason": quote_freshness_reason,
        "quoteStaleBlocked": quote_stale_blocked,
        "lateBreakoutBlocked": late_breakout_blocked,
        "chaseBreakoutBlocked": chase_breakout_blocked,
        "shadowCanBuy": shadow_can_buy,
        "canBuy": can_buy,
        "status": status,
        "source": quote_source,
        "snapshotAt": quote.get("snapshotAt") or "",
    }


# 盤中進場訊號推播：候選股 canBuy 當日首次 False→True 時推 LINE。晨報(08:15)
# 給的是靜態觸發價、盤後摘要(17:35)給的是結果，真正要出手的 09:30-13:15 盤中
# 這段原本完全沒有主動通知，人不在螢幕前就錯過整段進場窗。
INTRADAY_ENTRY_NOTIFY_DAILY_LINE_CAP = 2  # 每日 LINE 上限，超過改走桌面通知(0額度)
INTRADAY_ENTRY_NOTIFY_STATE_KEY = "intraday_entry_notify_state"
INTRADAY_NOTIFICATION_PIPELINE_KEY = "intraday_notification_pipeline"
RADAR_LOT_BUDGET_META_KEY = "user_radar_lot_budget"


def _read_meta_positive_int(key):
    with backend.connect() as conn:
        row = conn.execute("SELECT value FROM model_meta WHERE key = ?", (key,)).fetchone()
    try:
        value = int(float(row[0])) if row and row[0] else 0
    except (TypeError, ValueError):
        value = 0
    return max(0, value)


# 2026-07-09 賣出 LINE 全域靜音旗標(存伺服器 model_meta,跨裝置一致,不靠各瀏覽器 localStorage)
SELL_LINE_MUTED_META_KEY = "sell_line_muted"

# 2026-07-09 交易複盤自動化:記錄「已實現損益上次匯入日期」,讓每天第一次同步庫存時(搭同一次永豐
# 登入)順便抓 list_profit_loss 存進 sinopac_realized_pnl,之後同一天的庫存同步就不再重抓(避免每次
# 都拉、也不另開登入撞 400)。
SINOPAC_REALIZED_LAST_IMPORT_KEY = "sinopac_realized_last_import"


def _read_meta_flag(key):
    """讀 model_meta 的布林旗標(存 '1'/'0')。給跨裝置全域開關用(如賣出 LINE 靜音)。"""
    with backend.connect() as conn:
        row = conn.execute("SELECT value FROM model_meta WHERE key = ?", (key,)).fetchone()
    return bool(row and str(row[0]).strip() == "1")


def read_radar_lot_budget():
    value = _read_meta_positive_int(RADAR_LOT_BUDGET_META_KEY)
    return value if value > 0 else None


def _read_intraday_entry_notify_state(today):
    with backend.connect() as conn:
        row = conn.execute(
            "SELECT value FROM model_meta WHERE key = ?", (INTRADAY_ENTRY_NOTIFY_STATE_KEY,)
        ).fetchone()
    data = {}
    if row:
        try:
            data = json.loads(row[0] or "{}")
        except (json.JSONDecodeError, TypeError):
            data = {}
    if str(data.get("date") or "") != today:
        return {"date": today, "symbols": [], "lineCount": 0, "pendingOverBudget": []}
    return {
        "date": today,
        "symbols": [str(s) for s in (data.get("symbols") or [])],
        "lineCount": int(data.get("lineCount") or 0),
        "pendingOverBudget": [str(s) for s in (data.get("pendingOverBudget") or [])],
    }


def _write_intraday_entry_notify_state(state):
    with backend.connect() as conn:
        backend.set_meta(conn, INTRADAY_ENTRY_NOTIFY_STATE_KEY, json.dumps(state, ensure_ascii=False))


# ===== 盤中 LINE 跨路徑去重＋總量閘門(整併，非新功能) =====
# 盤中有 3 條各自獨立的進場 LINE 路徑：#169 進場翻正、②漲停打開、⑤盤中點火。
# 各自維護自己的去重集合與每日上限，彼此不知道對方推過什麼——同一檔在拉升途中
# 可能同時觸發⑤和#169，一天下來單股跨多路徑收到多則、合計上限可達 6~8 則，這是
# 14 個功能快速堆疊留下的通知洗版債。這裡加一層「跨路徑共用」的閘門：同一 symbol
# 當天只要被任一進場路徑 LINE 過，其餘路徑就不再對它重複推 LINE；再加一個跨路徑
# 每日 LINE 總上限。**只收斂 LINE，不影響桌面/網頁訊號**(那些由前端渲染盤中狀態，
# 與此閘門無關)，也**不動任何評分/門檻**。exit_guardian 出場訊號是資金保護、最不該
# 被額度擋，維持獨立路徑、不納入此閘門。
INTRADAY_LINE_BUDGET_STATE_KEY = "intraday_line_budget_state"
INTRADAY_TOTAL_LINE_CAP = 4  # 跨 3 條進場路徑的每日 LINE 訊息總上限(取代各自為政、合計最多 6~8 則)


def _read_intraday_line_budget_state(today):
    with backend.connect() as conn:
        row = conn.execute(
            "SELECT value FROM model_meta WHERE key = ?", (INTRADAY_LINE_BUDGET_STATE_KEY,)
        ).fetchone()
    data = {}
    if row:
        try:
            data = json.loads(row[0] or "{}")
        except (json.JSONDecodeError, TypeError):
            data = {}
    if str(data.get("date") or "") != today:
        return {"date": today, "notifiedSymbols": [], "totalLineCount": 0}
    return {
        "date": today,
        "notifiedSymbols": [str(s) for s in (data.get("notifiedSymbols") or [])],
        "totalLineCount": int(data.get("totalLineCount") or 0),
    }


def _write_intraday_line_budget_state(state):
    with backend.connect() as conn:
        backend.set_meta(conn, INTRADAY_LINE_BUDGET_STATE_KEY, json.dumps(state, ensure_ascii=False))


def cross_path_line_candidates(fresh, today):
    """回傳 fresh 中「今天還沒被任一盤中進場路徑 LINE 過」的 symbols；若跨路徑每日
    LINE 總量已達上限則回空清單。純讀、不標記——標記交給實際送出後的 mark_cross_path_lined，
    讓各路徑維持自己的「先標記再送」或「送達才標記」語意。"""
    state = _read_intraday_line_budget_state(today)
    if state["totalLineCount"] >= INTRADAY_TOTAL_LINE_CAP:
        return []
    lined = set(state["notifiedSymbols"])
    return [str(c) for c in fresh if str(c) not in lined]


def mark_cross_path_lined(symbols, today):
    """把實際送出 LINE 的 symbols 標記為今天已跨路徑推過，並讓跨路徑總量 +1
    (一則 LINE 訊息=+1，不論含幾檔)。空清單不動狀態。"""
    symbols = [str(s) for s in (symbols or [])]
    if not symbols:
        return
    state = _read_intraday_line_budget_state(today)
    lined = set(state["notifiedSymbols"])
    lined.update(symbols)
    state["notifiedSymbols"] = list(lined)
    state["totalLineCount"] += 1
    _write_intraday_line_budget_state(state)


def clear_cross_path_lined(symbols, today):
    """把 symbols 從今天的跨路徑已推集合移除(不動 totalLineCount——那則已送出的訊息
    仍計入當日總量)。用於 #169 進場訊號 canBuy 掉回 false＝這次訊號結束時：讓之後
    「經歷完整 false 週期的全新翻正」能再推一次(比照 #169 既有 reflip 語意)，跨路徑
    去重不會把獨立的第二次訊號永久鎖死。"""
    to_clear = {str(s) for s in (symbols or [])}
    if not to_clear:
        return
    state = _read_intraday_line_budget_state(today)
    lined = set(state["notifiedSymbols"])
    new_lined = lined - to_clear
    if new_lined == lined:
        return  # 沒有交集就不必寫
    state["notifiedSymbols"] = list(new_lined)
    _write_intraday_line_budget_state(state)


# ===== 漲停打開警示 =====
# 妖股鎖漲停後「打開」(盤中最高摸到漲停、但現價已回落離開漲停)常代表賣壓湧現、
# 動能失敗——是短線的出場/避開訊號。用盤中快照的 reference/high/current 三個價
# 判定，當天每檔第一次偵測到就推播一次。
LIMIT_UP_OPEN_NOTIFY_STATE_KEY = "limit_up_open_notify_state"
LIMIT_UP_OPEN_NOTIFY_DAILY_LINE_CAP = 2  # 每日 LINE 上限，超過只寫去重狀態(前端仍可讀)
# 台股單日漲跌幅 ±10%。tick 進位讓實際漲停價略低於 reference×1.10，用容差判定：
# high 摸到 reference×1.098 以上=盤中曾貼近/鎖住漲停；current 回落到 reference×1.094
# 以下=已明顯離開漲停(至少離約 0.6%)。兩者都成立才算「漲停打開」，避免正常小回檔誤報。
LIMIT_UP_TOUCH_RATIO = 1.098
LIMIT_UP_OPEN_RATIO = 1.094


def detect_limit_up_open(reference_price, high_price, current_price):
    """純判斷：盤中 high 曾摸到漲停、但 current 已回落離開漲停 = 漲停打開。
    回傳 bool。任一價格無效(<=0)或資料矛盾(high < current)一律回 False。"""
    try:
        ref = float(reference_price or 0)
        high = float(high_price or 0)
        cur = float(current_price or 0)
    except (TypeError, ValueError):
        return False
    if ref <= 0 or high <= 0 or cur <= 0:
        return False
    if high < cur:
        return False  # 當日最高不可能低於現價，視為髒資料
    touched_limit = high >= ref * LIMIT_UP_TOUCH_RATIO
    opened = cur <= ref * LIMIT_UP_OPEN_RATIO
    return bool(touched_limit and opened)


def _read_limit_up_open_notify_state(today):
    with backend.connect() as conn:
        row = conn.execute(
            "SELECT value FROM model_meta WHERE key = ?", (LIMIT_UP_OPEN_NOTIFY_STATE_KEY,)
        ).fetchone()
    data = {}
    if row:
        try:
            data = json.loads(row[0] or "{}")
        except (json.JSONDecodeError, TypeError):
            data = {}
    if str(data.get("date") or "") != today:
        return {"date": today, "symbols": [], "lineCount": 0}
    return {
        "date": today,
        "symbols": [str(s) for s in (data.get("symbols") or [])],
        "lineCount": int(data.get("lineCount") or 0),
    }


def _write_limit_up_open_notify_state(state):
    with backend.connect() as conn:
        backend.set_meta(conn, LIMIT_UP_OPEN_NOTIFY_STATE_KEY, json.dumps(state, ensure_ascii=False))


def build_limit_up_open_message(fresh_codes, quotes, name_by_code):
    lines = [f"⚠️ StockAI 漲停打開警示（{time.strftime('%H:%M')}）"]
    for code in fresh_codes[:8]:
        q = quotes.get(code) or {}
        name = str(name_by_code.get(code) or "").strip()
        cur = float(q.get("currentPrice") or 0)
        ref = float(q.get("referencePrice") or 0)
        rate = ((cur / ref - 1) * 100) if ref > 0 else 0
        lines.append(f"{code} {name}｜現價 {cur:.2f}（{rate:+.1f}%）鎖漲停後打開".rstrip())
    if len(fresh_codes) > 8:
        lines.append(f"…另有 {len(fresh_codes) - 8} 檔")
    lines.append("（賣壓湧現、動能可能轉弱，短線留意；非投資建議）")
    return "\n".join(lines)


def notify_limit_up_open(quotes, candidates):
    """盤中偵測妖股候選漲停打開，當天每檔第一次偵測到就推播一次。去重用當天已通知
    集合(只增不減——漲停打開是一次性事件，同一檔當天不重複警示)；先標記去重再送
    LINE，即使送失敗也不會每 20 秒重推。LINE 有每日上限，超過只記狀態不送。"""
    today = scheduler_today(taipei_localtime())
    state = _read_limit_up_open_notify_state(today)
    notified = set(state["symbols"])
    name_by_code = {
        str(c.get("symbol") or ""): str(c.get("name") or c.get("stockName") or "")
        for c in (candidates or [])
    }
    fresh = []
    for code, quote in (quotes or {}).items():
        code = str(code)
        if not code or code in notified:
            continue
        if detect_limit_up_open(
            (quote or {}).get("referencePrice"),
            (quote or {}).get("highPrice"),
            (quote or {}).get("currentPrice"),
        ):
            fresh.append(code)
    if not fresh:
        return {"notified": False, "fresh": []}
    notified.update(fresh)
    state["symbols"] = list(notified)
    # 跨路徑閘門：只 LINE 今天還沒被任一進場路徑推過的 symbols(且總量未達上限)。
    line_codes = cross_path_line_candidates(fresh, today) if state["lineCount"] < LIMIT_UP_OPEN_NOTIFY_DAILY_LINE_CAP else []
    if line_codes:
        state["lineCount"] += 1
        mark_cross_path_lined(line_codes, today)  # 先標記再送(比照本路徑既有語意)
    _write_limit_up_open_notify_state(state)  # 先寫去重(含lineCount)再送，避免重推
    if line_codes:
        message = build_limit_up_open_message(line_codes, quotes, name_by_code)
        try:
            send_line_message_via_api(message, priority="normal")
        except Exception as exc:
            print(f"Limit-up-open LINE notify failed: {exc}")
    return {"notified": True, "fresh": fresh, "line": bool(line_codes)}


# ===== 盤中即時突破/點火掃描 =====
# 妖股「點火」——盤中突然急拉、股價快速衝高——是短線最想在「當下」知道的事，
# 不是隔天收盤後掃描才發現(那時第一根漲勢已經走完)。用盤中 poll 已有的快照
# (reference/current/high)偵測「候選股盤中漲幅衝過門檻且貼近當日高(強勢拉升
# 途中，不是拉回)」，當天每檔第一次點火就推 LINE。範圍：目前只掃「今天已入選的
# 妖股候選(~50檔)」——它們本來就是妖股宇宙；全市場盤中掃描要另開額度預算+子程序，
# 是更重的未來版本。已鎖/貼近漲停交給漲停打開邏輯(②)，這裡專抓拉升途中的點火。
INTRADAY_SURGE_NOTIFY_STATE_KEY = "intraday_surge_notify_state"
INTRADAY_SURGE_NOTIFY_DAILY_LINE_CAP = 2
INTRADAY_SURGE_RATE_THRESHOLD = 6.0    # 盤中漲幅 >= 6% 視為點火
INTRADAY_SURGE_NEAR_LIMIT_RATE = 9.5   # 漲幅 >= 9.5% 視為已鎖/貼近漲停，交給②不重複
INTRADAY_SURGE_NEAR_HIGH_RATIO = MONSTER_INTRADAY_NEAR_HIGH_RATIO


def detect_intraday_surge(reference_price, current_price, high_price):
    """純判斷：盤中急拉點火 = 漲幅衝過門檻(>=6%)、還沒鎖漲停(<9.5%)、且現價貼近
    當日最高(強勢拉升途中，非回落)。回傳 bool。價格無效(<=0)回 False。"""
    try:
        ref = float(reference_price or 0)
        cur = float(current_price or 0)
        high = float(high_price or 0)
    except (TypeError, ValueError):
        return False
    if ref <= 0 or cur <= 0 or high <= 0:
        return False
    if cur > high * 1.001:
        return False  # 現價高於當日高代表快照欄位不一致，不拿壞資料發訊號
    change_rate = (cur / ref - 1) * 100
    if change_rate < INTRADAY_SURGE_RATE_THRESHOLD:
        return False
    if change_rate >= INTRADAY_SURGE_NEAR_LIMIT_RATE:
        return False  # 貼近漲停交給漲停打開邏輯，不在這裡重複推
    if cur < high * INTRADAY_SURGE_NEAR_HIGH_RATIO:
        return False  # 已從高點回落，不是強勢點火
    return True


def intraday_surge_state_allowed(state):
    """點火是提早警示，不等同 canBuy，但仍須通過正式候選、量能與資料健康守門。

    只在 09:30-10:00 初次確認窗發點火；10:00 後系統規則本來就禁止追突破，
    若還繼續發急拉通知，會和最終買賣判斷互相矛盾。缺任一狀態採 fail-closed。
    """
    if not isinstance(state, dict):
        return False
    return bool(
        state.get("hasIntradayQuote")
        and state.get("quoteFresh")
        and state.get("candidateDailyDataFresh")
        and state.get("marketDataFresh")
        and state.get("scheduleAllowed")
        and state.get("volumeContinue")
        and not state.get("dangerRisk")
        and not state.get("candidateOverheated")
        and not state.get("windowBlocked")
        and state.get("entryWindowPhase") == "initial"
    )


def _read_intraday_surge_notify_state(today):
    with backend.connect() as conn:
        row = conn.execute(
            "SELECT value FROM model_meta WHERE key = ?", (INTRADAY_SURGE_NOTIFY_STATE_KEY,)
        ).fetchone()
    data = {}
    if row:
        try:
            data = json.loads(row[0] or "{}")
        except (json.JSONDecodeError, TypeError):
            data = {}
    if str(data.get("date") or "") != today:
        return {"date": today, "symbols": [], "lineCount": 0}
    return {
        "date": today,
        "symbols": [str(s) for s in (data.get("symbols") or [])],
        "lineCount": int(data.get("lineCount") or 0),
    }


def _write_intraday_surge_notify_state(state):
    with backend.connect() as conn:
        backend.set_meta(conn, INTRADAY_SURGE_NOTIFY_STATE_KEY, json.dumps(state, ensure_ascii=False))


def build_intraday_surge_message(fresh_codes, quotes, name_by_code):
    lines = [f"🚀 StockAI 盤中點火掃描（{time.strftime('%H:%M')}）"]
    for code in fresh_codes[:8]:
        q = quotes.get(code) or {}
        name = str(name_by_code.get(code) or "").strip()
        cur = float(q.get("currentPrice") or 0)
        ref = float(q.get("referencePrice") or 0)
        rate = ((cur / ref - 1) * 100) if ref > 0 else 0
        lines.append(f"{code} {name}｜現價 {cur:.2f}（{rate:+.1f}%）盤中急拉".rstrip())
    if len(fresh_codes) > 8:
        lines.append(f"…另有 {len(fresh_codes) - 8} 檔")
    lines.append("（候選股盤中強勢拉升，短線留意是否追／是否已在名單；非投資建議）")
    return "\n".join(lines)


def notify_intraday_surge(quotes, candidates, intraday_states):
    """盤中偵測妖股候選點火(急拉衝過門檻且貼近當日高)，當天每檔第一次就推 LINE。
    去重當天已通知集合(只增不減)、每日 LINE 上限、先標記再送。跟②漲停打開、盤中
    進場推播互補：這裡抓的是「拉升途中的點火」，不是漲停打開、也不是進場閘門翻正。"""
    today = scheduler_today(taipei_localtime())
    state = _read_intraday_surge_notify_state(today)
    notified = set(state["symbols"])
    name_by_code = {
        str(c.get("symbol") or ""): str(c.get("name") or c.get("stockName") or "")
        for c in (candidates or [])
    }
    fresh = []
    blocked = []
    for code, quote in (quotes or {}).items():
        code = str(code)
        if not code or code in notified:
            continue
        is_surge = detect_intraday_surge(
            (quote or {}).get("referencePrice"),
            (quote or {}).get("currentPrice"),
            (quote or {}).get("highPrice"),
        )
        if not is_surge:
            continue
        if not intraday_surge_state_allowed((intraday_states or {}).get(code)):
            blocked.append(code)
            continue
        fresh.append(code)
    if not fresh:
        return {"notified": False, "fresh": [], "blocked": blocked}
    notified.update(fresh)
    state["symbols"] = list(notified)
    # 跨路徑閘門：只 LINE 今天還沒被任一進場路徑推過的 symbols(且總量未達上限)。
    line_codes = cross_path_line_candidates(fresh, today) if state["lineCount"] < INTRADAY_SURGE_NOTIFY_DAILY_LINE_CAP else []
    if line_codes:
        state["lineCount"] += 1
        mark_cross_path_lined(line_codes, today)  # 先標記再送(比照本路徑既有語意)
    _write_intraday_surge_notify_state(state)  # 先寫去重再送，避免重推
    if line_codes:
        message = build_intraday_surge_message(line_codes, quotes, name_by_code)
        try:
            send_line_message_via_api(message, priority="normal")
        except Exception as exc:
            print(f"Intraday surge LINE notify failed: {exc}")
    return {"notified": True, "fresh": fresh, "blocked": blocked, "line": bool(line_codes)}


def build_intraday_entry_message(fresh_codes, by_code, candidate_info):
    setup_labels = {"breakout": "突破", "pullback": "回測低接", "v_rebound": "V轉低接"}
    lines = [f"📈 StockAI 盤中進場訊號（{time.strftime('%H:%M')}）"]
    for code in fresh_codes[:5]:
        state = by_code.get(code) or {}
        item = candidate_info.get(code) or {}
        name = str(item.get("name") or "").strip()
        trigger_price = float(item.get("buy_trigger") or item.get("buyTrigger") or 0)
        stop_price = float(item.get("stop_price") or item.get("stopPrice") or 0)
        label = setup_labels.get(str(state.get("setupType") or ""), str(state.get("setupType") or ""))
        lines.append(f"{code} {name} {label}成立".rstrip())
        detail = f"　現價 {float(state.get('currentPrice') or 0):.2f}"
        if trigger_price:
            detail += f"｜觸發 {trigger_price:.2f}"
        if stop_price:
            detail += f"｜停損 {stop_price:.2f}"
        lines.append(detail)
    if len(fresh_codes) > 5:
        lines.append(f"…另有 {len(fresh_codes) - 5} 檔，詳見網頁")
    lines.append("（開盤確認通過的觀察訊號，非投資建議）")
    return "\n".join(lines)


def desktop_notify_buy_signal(title, message):
    """買進類桌面通知(盤中進場訊號)在「只發賣出」模式(LINE_SELL_ONLY)下一併靜音。
    使用者 2026-07-07 回饋:進場訊號的 LINE 已被 sell-only 擋掉,不該又退回改用 Windows
    桌面彈窗冒出來——那等於從另一個門違反「只在該賣時才打擾」的偏好。雷達畫面上該檔
    仍會顯示可買,只是不主動彈窗;賣出/停損(停損守門員)走各自路徑、不經這裡,不受影響。"""
    if line_notify.LINE_SELL_ONLY:
        return {"ok": True, "sent": False, "muted": "sell_only"}
    return send_windows_desktop_notification(title, message)


def notify_intraday_entry_triggers(previous_quotes, by_code, quote_payload, candidates):
    """canBuy 首次翻正的推播決策。防護設計(2026-07-04 稽核後修正版)：
    - stale 報價整輪跳過 diff(快取舊價造成的假觸發不值得一則額度)
    - 伺服器剛啟動沒有上一輪資料時不通知(避免重啟風暴)
    - 去重用「目前 canBuy=true 且已通知」的存活集合(symbols)，canBuy 掉回
      false 就從集合移除——同一檔股票當天 false→true→false→true 兩次獨立
      翻正都能各自推播，不會被「整天鎖死」的舊邏輯誤判成同一次訊號的抖動
      (2026-07-04 稽核發現：舊邏輯只認「今天出現過」，第二次起的合理進場
      訊號會被永久吞掉)
    - 一張預算過濾後仍買不起的候選不會憑空消失：進 pendingOverBudget 集合，
      之後每一輪都重新檢查一次是否已經跌回預算內，不需要等待新的翻正事件
      (2026-07-04 稽核發現：舊邏輯只在翻正瞬間過濾一次，之後canBuy若持續
      維持true、股價卻真的跌回預算內，永遠沒有機會補推播)
    - 每日 LINE 上限 2 則，超過或額度讓位(suppressed)改走桌面通知
    - 只有真的送達(LINE sent 或桌面 fallback)才寫去重狀態，LINE 網路失敗
      不標記，下一輪 30 秒迴圈自動重試——比照 check_missed_auto_schedule_windows
    """
    if quote_payload.get("stale"):
        return {"notified": 0, "skipped": "stale_quotes"}
    if not previous_quotes:
        return {"notified": 0, "skipped": "no_previous_round"}
    today = scheduler_today(taipei_localtime())
    notify_state = _read_intraday_entry_notify_state(today)
    active = set(notify_state["symbols"])
    pending_over_budget = set(notify_state["pendingOverBudget"])
    flipped = []
    dropped_false = []
    for code, state in by_code.items():
        can_buy_now = bool(state.get("canBuy"))
        if not can_buy_now:
            # 訊號本身已經失效：不管是已通知過還是還在等預算回落，都清掉，
            # 下次重新翻正才算全新的獨立訊號。
            active.discard(code)
            pending_over_budget.discard(code)
            dropped_false.append(code)
            continue
        prev = previous_quotes.get(code)
        if not prev or not prev.get("canBuy"):
            flipped.append(code)
    # canBuy 掉回 false ＝這次訊號結束：同步把它從跨路徑 LINE 集合移除，讓之後
    # 「經歷完整 false 週期的全新翻正」能再推一次 LINE(不被跨路徑去重永久鎖死)。
    if dropped_false:
        clear_cross_path_lined(dropped_false, today)
    lot_budget = read_radar_lot_budget()

    def _affordable(code):
        if not lot_budget:
            return True
        price = float((by_code.get(code) or {}).get("currentPrice") or 0)
        return price <= 0 or price * 1000 <= lot_budget

    fresh_from_flip = [c for c in flipped if c not in active]
    # 只在候選仍在本輪宇宙(by_code 有它、有現價)且回到預算內才算「解除超預算」。否則已離開
    # 宇宙的舊 pending 會因 _affordable 對缺價回 True 被誤判解除,推出一則現價 0.00 的假進場訊號。
    resolved_pending = [c for c in pending_over_budget if c in by_code and _affordable(c)]
    over_budget_count = 0
    fresh = []
    for code in fresh_from_flip:
        if _affordable(code):
            fresh.append(code)
        else:
            pending_over_budget.add(code)
            over_budget_count += 1
    fresh.extend(c for c in resolved_pending if c not in fresh)
    for code in resolved_pending:
        pending_over_budget.discard(code)
    if not fresh:
        # 這裡沒有任何要推播的內容，但 active/pending 的清理(canBuy 掉回
        # false 的訊號)本身跟通知有沒有送達無關，可以安全落地。
        notify_state["symbols"] = list(active)
        notify_state["pendingOverBudget"] = list(pending_over_budget)
        _write_intraday_entry_notify_state(notify_state)
        result = {"notified": 0, "skipped": "over_budget" if over_budget_count else "already_notified"}
        if over_budget_count:
            result["overBudget"] = over_budget_count
        return result
    candidate_info = {str(item.get("symbol") or ""): item for item in candidates}
    fresh.sort(key=lambda c: float((candidate_info.get(c) or {}).get("score") or 0), reverse=True)
    message = build_intraday_entry_message(fresh, by_code, candidate_info)
    safe_print(message)

    def _mark_notified():
        active.update(fresh)
        notify_state["symbols"] = list(active)
        notify_state["pendingOverBudget"] = list(pending_over_budget)
        _write_intraday_entry_notify_state(notify_state)

    if notify_state["lineCount"] >= INTRADAY_ENTRY_NOTIFY_DAILY_LINE_CAP:
        try:
            desktop_notify_buy_signal("盤中進場訊號", message)
        except Exception:
            pass
        _mark_notified()
        return {"notified": len(fresh), "channel": "desktop", "reason": "daily_line_cap"}
    # 跨路徑閘門：這批 fresh 若都已被其他盤中進場路徑(②漲停打開/⑤點火)LINE 過、
    # 或跨路徑每日總量已滿 → 不重複 LINE，只走桌面(桌面訊號不受閘門限制)。
    line_codes = cross_path_line_candidates(fresh, today)
    if not line_codes:
        try:
            desktop_notify_buy_signal("盤中進場訊號", message)
        except Exception:
            pass
        _mark_notified()
        return {"notified": len(fresh), "channel": "desktop", "reason": "cross_path_line_dedup"}
    # normal 級：月底額度進入保留池時會拿到 suppressed，自動降級桌面通知
    line_message = message if len(line_codes) == len(fresh) else build_intraday_entry_message(line_codes, by_code, candidate_info)
    result = send_line_message_via_api(line_message)
    if result.get("sent"):
        notify_state["lineCount"] += 1
        mark_cross_path_lined(line_codes, today)  # 送達才標記(比照本路徑既有語意)
        _mark_notified()
        return {"notified": len(fresh), "channel": "line"}
    if result.get("suppressed") or result.get("disabled"):
        try:
            desktop_notify_buy_signal("盤中進場訊號", message)
        except Exception:
            pass
        _mark_notified()
        return {"notified": len(fresh), "channel": "desktop", "reason": result.get("reason") or "line_disabled"}
    return {"notified": 0}


# ===== ①-a 盤中即時報價失明告警(2026-07-07) =====
# 盤中確認的即時價 100% 只靠永豐 Shioaji 快照,零公開備援。永豐 daily token /
# CA 憑證盤中過期時,update_monster_intraday_quotes 落 except、quotes 清空、
# health 轉 observe_only,前端會顯示「資料異常,暫停買進決策」——但只有開著網頁
# 才看得到。瀏覽器關著時整個雷達當天「失明」卻無聲無息:每檔都卡「仍需盤中確認」,
# 使用者分不出是「沒人確認」還是「永豐掛了」。這裡在盤中確認窗(09:30-13:15)偵測到
# 報價端失明就發一則 critical LINE(系統故障級,不受 LINE_SELL_ONLY 抑制),每日去重
# 一次避免每 30 秒洗版。**純告警,不碰任何買賣閘門/評分/門檻**;公開快照自動備援
# (①-b)碰 canBuy 判斷,故另案處理、需先證後改。
INTRADAY_BLACKOUT_NOTIFY_STATE_KEY = "intraday_blackout_notify_state"


def _read_intraday_blackout_notify_state(today):
    with backend.connect() as conn:
        row = conn.execute(
            "SELECT value FROM model_meta WHERE key = ?", (INTRADAY_BLACKOUT_NOTIFY_STATE_KEY,)
        ).fetchone()
    data = {}
    if row:
        try:
            data = json.loads(row[0] or "{}")
        except (json.JSONDecodeError, TypeError):
            data = {}
    if str(data.get("date") or "") != today:
        return {"date": today, "notified": False}
    return {"date": today, "notified": bool(data.get("notified"))}


def _write_intraday_blackout_notify_state(state):
    with backend.connect() as conn:
        backend.set_meta(conn, INTRADAY_BLACKOUT_NOTIFY_STATE_KEY, json.dumps(state, ensure_ascii=False))


def notify_intraday_quote_blackout(status):
    """盤中即時報價失明(永豐報價端中斷)時發一則 critical LINE。
    只在:①盤中確認窗內(09:30-13:15,quotes 本該在流)②失明成因是報價端
    (有 error 或 quoteCount<=0)而非每日資料未就緒(daily 分支是另一種已知的晨間
    observe-only,發它只會每天早上洗版)才觸發;每日去重一次。回傳決策結果供測試斷言。"""
    try:
        health = status.get("health") or {}
        if health.get("ok"):
            return {"skipped": "healthy"}
        quote_error = str(status.get("error") or status.get("quoteError") or "").strip()
        quote_count = int(health.get("quoteCount") or 0)
        daily_ok = bool((health.get("daily") or {}).get("ok"))
        if not (quote_error or quote_count <= 0):
            return {"skipped": "not_quote_blackout"}
        if not daily_ok:
            # daily 端未就緒(晨間 observe-only)是另一條已知路徑,不在這裡重複告警。
            return {"skipped": "daily_not_ready"}
        if not monster_buy_confirm_window():
            return {"skipped": "outside_confirm_window"}
        today = scheduler_today(taipei_localtime())
        state = _read_intraday_blackout_notify_state(today)
        if state.get("notified"):
            return {"skipped": "already_notified"}
        reason = health.get("reason") or "盤中即時報價中斷"
        message = (
            "🛑 妖股雷達盤中失明\n"
            f"{reason}\n"
            "→ 今日盤中「可買確認」與「賣出確認」全部暫停,雷達只能觀察。\n"
            "多半是永豐 daily token / CA 憑證盤中過期,重新登入永豐即可恢復。"
        )
        safe_print(message)
        result = send_line_message_via_api(message, priority="critical")
        if result.get("sent") or result.get("suppressed") or result.get("disabled"):
            # 送達、或被額度保留池/停用降級,都算「今日已告警」,不每 30 秒重試洗版;
            # 只有真的網路失敗(既非 sent 也非 suppressed/disabled)才不標記,下輪重試。
            state["notified"] = True
            _write_intraday_blackout_notify_state(state)
        return {"notified": 1 if result.get("sent") else 0, "result": result}
    except Exception as exc:
        print(f"Intraday blackout notification failed: {exc}")
        return {"error": str(exc)}


# ===== ② 盤中閘門量測(2026-07-07,回答「能買的是不是太少/門檻太嚴」)=====
# 使用者反映盤中能買的檔數太少、疑似門檻太嚴。但盲放寬買進閘=更多爛訊號、勝率
# 大概率變差(踩過多次的 logical-but-worse 陷阱)。誠實作法=先量測:每日累積
# 「多少檔翻成可買、卡在哪個狀態(未通過開盤確認/被安全閥擋)、型態分布」,累積
# 一兩週再看是不是某個特定閘(如量能門檻)真的過嚴,再用回測決定要不要動。
# **純記錄,完全不影響任何 canBuy/評分/門檻判斷。**
INTRADAY_GATE_STATS_KEY = "intraday_gate_stats"
INTRADAY_GATE_STATS_HISTORY_MAX = 20
RADAR_ENTRY_SNAPSHOT_PIPELINE_KEY = "radar_entry_snapshot_pipeline"
RADAR_ENTRY_SNAPSHOT_ALERT_STATE_KEY = "radar_entry_snapshot_alert_state"
radar_entry_snapshot_alert_last_date = ""


def notify_radar_entry_snapshot_failure(state):
    """Send at most one alert per day when a real shadow/formal signal is not persisted."""
    global radar_entry_snapshot_alert_last_date
    signal_date = str((state or {}).get("date") or "")[:10]
    expected = int((state or {}).get("expected") or 0)
    if not signal_date or expected <= 0 or (state or {}).get("ok") is True:
        return {"notified": 0, "skipped": "no_actionable_failure"}
    if radar_entry_snapshot_alert_last_date == signal_date:
        return {"notified": 0, "skipped": "already_notified"}
    try:
        with backend.connect() as conn:
            row = conn.execute(
                "SELECT value FROM model_meta WHERE key = ?",
                (RADAR_ENTRY_SNAPSHOT_ALERT_STATE_KEY,),
            ).fetchone()
        previous = json.loads(row[0] or "{}") if row and row[0] else {}
    except (json.JSONDecodeError, TypeError, sqlite3.Error):
        previous = {}
    if str(previous.get("date") or "") == signal_date and previous.get("notified"):
        radar_entry_snapshot_alert_last_date = signal_date
        return {"notified": 0, "skipped": "already_notified"}

    # Set the process-local guard before sending so a temporary DB failure cannot
    # produce the same alert on every intraday polling cycle.
    radar_entry_snapshot_alert_last_date = signal_date
    missing = ", ".join((state or {}).get("missingSymbols") or []) or "無法辨識"
    error = str((state or {}).get("error") or "")[:240]
    message = (
        f"⚠️ StockAI 雷達紙上快照寫入失敗\n"
        f"日期：{signal_date}\n"
        f"應保存：{expected} 檔；已確認：{int((state or {}).get('persisted') or 0)} 檔\n"
        f"缺漏：{missing}"
        + (f"\n錯誤：{error}" if error else "")
        + "\n正式買進持續停用，請檢查伺服器與 SQLite。"
    )
    try:
        desktop_result = send_windows_desktop_notification("雷達紙上快照失敗", message)
    except Exception as exc:
        desktop_result = {"sent": False, "error": str(exc)[:300]}
    try:
        line_result = send_line_message_via_api(message, priority="critical")
    except Exception as exc:
        line_result = {"sent": False, "error": str(exc)[:300]}
    alert_state = {
        "date": signal_date,
        "notified": True,
        "notifiedAt": now_text(),
        "missingSymbols": (state or {}).get("missingSymbols") or [],
        "error": error,
        "desktop": desktop_result,
        "line": line_result,
    }
    try:
        with backend.connect() as conn:
            backend.set_meta(
                conn,
                RADAR_ENTRY_SNAPSHOT_ALERT_STATE_KEY,
                json.dumps(alert_state, ensure_ascii=False, default=str),
            )
    except Exception as exc:
        print(f"Radar entry snapshot alert state write failed: {exc}")
    return {"notified": 1, "desktop": desktop_result, "line": line_result}


def record_radar_entry_snapshot_pipeline(candidates, states, signal_date, trigger="auto"):
    """Persist shadow/formal quotes and expose a durable, auditable pipeline result."""
    expected_codes = sorted({
        str(symbol) for symbol, state in (states or {}).items()
        if state and (state.get("canBuy") or state.get("shadowCanBuy"))
    })
    pipeline = {
        "date": str(signal_date or scheduler_today())[:10],
        "checkedAt": now_text(),
        "trigger": str(trigger or "auto"),
        "candidateCount": len(candidates or []),
        "expected": len(expected_codes),
        "formalCount": sum(
            1 for state in (states or {}).values() if state and state.get("canBuy")
        ),
        "shadowOnlyCount": sum(
            1 for state in (states or {}).values()
            if state and state.get("shadowCanBuy") and not state.get("canBuy")
        ),
        "expectedSymbols": expected_codes,
        "prepared": 0,
        "inserted": 0,
        "duplicates": 0,
        "skippedNoPrice": 0,
        "persisted": 0,
        "missingSymbols": [],
        "ok": False,
        "error": "",
    }
    try:
        details = backend.record_radar_entry_snapshots(
            candidates,
            states,
            signal_date=pipeline["date"],
            return_details=True,
        )
        if not isinstance(details, dict):
            raise RuntimeError("snapshot writer did not return audit details")
        for key in (
            "prepared", "inserted", "duplicates", "skippedNoPrice",
            "persisted", "missingSymbols",
        ):
            pipeline[key] = details.get(key, pipeline[key])
        pipeline["ok"] = bool(details.get("ok"))
        if not pipeline["ok"]:
            pipeline["error"] = "paper snapshot persistence incomplete"
    except Exception as exc:
        pipeline["error"] = str(exc)[:500]
        pipeline["missingSymbols"] = expected_codes

    try:
        with backend.connect() as conn:
            backend.set_meta(
                conn,
                RADAR_ENTRY_SNAPSHOT_PIPELINE_KEY,
                json.dumps(pipeline, ensure_ascii=False, default=str),
            )
    except Exception as exc:
        pipeline["ok"] = False
        pipeline["error"] = (
            f"{pipeline.get('error')}; " if pipeline.get("error") else ""
        ) + f"pipeline state write failed: {str(exc)[:300]}"
    try:
        pipeline["alert"] = notify_radar_entry_snapshot_failure(pipeline)
    except Exception as exc:
        pipeline["alert"] = {"notified": 0, "error": str(exc)[:300]}
    return pipeline


def record_intraday_gate_stats(by_code, candidate_count, now_tm=None):
    """每日累積盤中閘門通過情況。buyable 取當日各輪聯集(型態一閃即逝,單輪會低估
    當天真正出現過幾檔可買);狀態/型態分布記最後一輪快照;跨日把昨天摘要壓進
    history 供一兩週回看。回傳當日累積資料供測試斷言。"""
    try:
        now_tm = now_tm or taipei_localtime()
        today = scheduler_today(now_tm)
        buyable_now = sorted(str(c) for c, s in by_code.items() if isinstance(s, dict) and s.get("canBuy"))
        shadow_buyable_now = sorted(
            str(c) for c, s in by_code.items()
            if isinstance(s, dict) and s.get("shadowCanBuy")
        )
        status_dist = Counter(str((s or {}).get("status") or "") for s in by_code.values())
        setup_dist = Counter(str((s or {}).get("setupType") or "無") for s in by_code.values())
        states = [state for state in by_code.values() if isinstance(state, dict)]
        fresh_quote_items = [
            (str(code), state) for code, state in by_code.items()
            if isinstance(state, dict)
            and state.get("hasIntradayQuote")
            and state.get("quoteFresh")
        ]
        quote_sources = Counter(
            str(state.get("source") or "unknown") for _, state in fresh_quote_items
        )
        quote_date_mismatch_codes = sorted(
            code for code, state in fresh_quote_items
            if str(state.get("snapshotAt") or "")[:10] != today
        )
        missing_quote_timestamp_codes = sorted(
            code for code, state in fresh_quote_items
            if not str(state.get("snapshotAt") or "").strip()
        )
        fallback_quote_items = [
            (code, state) for code, state in fresh_quote_items
            if any(marker in str(state.get("source") or "").lower()
                   for marker in ("capital", "群益"))
        ]
        risk_leak_codes = sorted(
            str(code) for code, state in by_code.items()
            if isinstance(state, dict)
            and (
                state.get("dangerRisk")
                or state.get("candidateInvalidForTrading")
                or state.get("candidatePerformanceVetoed")
                or state.get("candidateEntryGuardrailVetoed")
            )
            and (state.get("formalWatchAllowed") or state.get("scheduleAllowed") or state.get("canBuy"))
        )
        with backend.connect() as conn:
            row = conn.execute("SELECT value FROM model_meta WHERE key = ?", (INTRADAY_GATE_STATS_KEY,)).fetchone()
            data = {}
            if row and row[0]:
                try:
                    data = json.loads(row[0])
                except (json.JSONDecodeError, TypeError):
                    data = {}
            history = data.get("history") or []
            if str(data.get("date") or "") != today:
                # 跨日:把昨天精簡摘要壓進 history,再重置今天。
                if data.get("date"):
                    history.append({
                        "date": data.get("date"),
                        "buyableUnion": len(data.get("buyableCodes") or []),
                        "shadowBuyableUnion": len(data.get("shadowBuyableCodes") or []),
                        "candidateCount": data.get("candidateCount"),
                        "maxConcurrent": data.get("maxConcurrent"),
                        "polls": data.get("polls"),
                    })
                    history = history[-INTRADAY_GATE_STATS_HISTORY_MAX:]
                data = {
                    "date": today,
                    "buyableCodes": [],
                    "shadowBuyableCodes": [],
                    "polls": 0,
                    "maxConcurrent": 0,
                    "maxShadowConcurrent": 0,
                }
            codes = set(data.get("buyableCodes") or [])
            codes.update(buyable_now)
            shadow_codes = set(data.get("shadowBuyableCodes") or [])
            shadow_codes.update(shadow_buyable_now)
            data["buyableCodes"] = sorted(codes)
            data["buyableUnion"] = len(codes)
            data["shadowBuyableCodes"] = sorted(shadow_codes)
            data["shadowBuyableUnion"] = len(shadow_codes)
            data["maxConcurrent"] = max(int(data.get("maxConcurrent") or 0), len(buyable_now))
            data["maxShadowConcurrent"] = max(
                int(data.get("maxShadowConcurrent") or 0), len(shadow_buyable_now)
            )
            data["lastConcurrent"] = len(buyable_now)
            data["lastShadowConcurrent"] = len(shadow_buyable_now)
            data["polls"] = int(data.get("polls") or 0) + 1
            data["candidateCount"] = candidate_count
            data["statusDist"] = dict(status_dist)
            data["setupDist"] = dict(setup_dist)
            data["quoteCount"] = sum(1 for state in states if state.get("hasIntradayQuote"))
            data["freshQuoteCount"] = sum(
                1 for state in states if state.get("hasIntradayQuote") and state.get("quoteFresh")
            )
            data["freshQuoteTimestampCount"] = sum(
                1 for _, state in fresh_quote_items if str(state.get("snapshotAt") or "").strip()
            )
            data["quoteSources"] = dict(quote_sources)
            data["quoteDateMismatchCount"] = len(quote_date_mismatch_codes)
            data["quoteDateMismatchCodes"] = quote_date_mismatch_codes
            data["missingQuoteTimestampCount"] = len(missing_quote_timestamp_codes)
            data["missingQuoteTimestampCodes"] = missing_quote_timestamp_codes
            data["fallbackQuoteCount"] = len(fallback_quote_items)
            data["fallbackQuoteCodes"] = sorted(code for code, _ in fallback_quote_items)
            data["lastPollAt"] = time.strftime("%Y-%m-%d %H:%M:%S", now_tm)
            data["dailyDataFreshCount"] = sum(
                1 for state in states if state.get("candidateDailyDataFresh")
            )
            data["marketDataFreshCount"] = sum(
                1 for state in states if state.get("marketDataFresh")
            )
            data["dangerRiskCount"] = sum(1 for state in states if state.get("dangerRisk"))
            data["invalidCandidateCount"] = sum(
                1 for state in states if state.get("candidateInvalidForTrading")
            )
            data["performanceVetoCount"] = sum(
                1 for state in states if state.get("candidatePerformanceVetoed")
            )
            data["entryGuardrailVetoCount"] = sum(
                1 for state in states if state.get("candidateEntryGuardrailVetoed")
            )
            data["riskLeakCount"] = len(risk_leak_codes)
            data["riskLeakCodes"] = risk_leak_codes
            data["history"] = history
            backend.set_meta(conn, INTRADAY_GATE_STATS_KEY, json.dumps(data, ensure_ascii=False))
        return data
    except Exception as exc:
        print(f"Intraday gate stats record failed: {exc}")
        return None


# ===== 伺服器端停損守門員 =====
# 整條賣出提醒鏈(防守價計算→輪詢→通知)都跑在瀏覽器裡，停損價只存在
# localStorage——手機鎖屏、分頁被回收、電腦沒開，跌破停損就完全沒有通知。
# 前端每輪監控把後端統一出場快照轉送到 /api/portfolio/exit-watch(這個 POST
# 同時就是心跳，伺服器會再核對 portfolio_exit_snapshots)；心跳逾時且在盤中時段，
# 伺服器接手比對報價，跌破才發 LINE。瀏覽器在線時伺服器完全靜默。
EXIT_WATCH_STATE_KEY = "portfolio_exit_watch_state"
EXIT_GUARDIAN_NOTIFY_STATE_KEY = "exit_guardian_notify_state"
# 2026-07-04 稽核發現：app.js 自承背景分頁的 setInterval 可能延遲「數分鐘到
# 數十分鐘」(見 runPortfolioAlertMonitorTick 呼叫端註解)，原本 180 秒的門檻
# 遠低於這個實際延遲，分頁根本沒關、網路也沒斷，只是切到背景就常態性被誤判
# 離線、觸發不必要的伺服器接管+重複通知。拉高到 10 分鐘，配合前端
# visibilitychange(hidden方向) 主動補送心跳，兩者一起大幅降低誤判率；
# 這支本來就只是「瀏覽器離線代看」的backstop，不是主要提醒路徑，反應慢一點
# 換取不必要的接管大幅減少是合理取捨。
EXIT_GUARDIAN_HEARTBEAT_MAX_AGE = 600
# 2026-07-07 稽核:守門員是「瀏覽器離線」才接手,額滿改發桌面通知=離線的人根本看不到,
# 大跌日多檔停損最需要通知時反而漏。停損是保命通知不是雜訊,上限拉高(月額度 200 充足、
# critical 走保留池不受一般限制);仍保留上限避免異常狂發。
EXIT_GUARDIAN_DAILY_LINE_CAP = 10
EXIT_GUARDIAN_CHECK_INTERVAL = 60  # worker 迴圈 20 秒一輪，守門檢查每 60 秒一次就夠
# 2026-07-06 使用者回饋「LINE 提醒是真的要賣才提醒」:守門員原本在剛摸到防守價(警戒)
# 就發 LINE，但前端自己的設計是「防守價只警戒(canNotify:false)、跌破防守後再確認賣出」，
# confirm 群組的價位 = 防守價 × 0.99(app.js confirmSellPrice)才是真正會通知賣出的線。
# 守門員改成用前端同步的 confirmSell 觸發;舊快取沒帶就用這個係數自行推算,一樣保留 1%
# 緩衝,不對「只低於防守價幾分錢」的盤中雜訊 graze 發通知(當初 2302 52.10 vs 防守 52.14
# 只低 0.08% 就發，就是這裡太敏感)。
EXIT_GUARDIAN_CONFIRM_FACTOR = 0.99
EXIT_WATCH_VERIFIED_DECISION_TYPES = {"stop", "phase1", "phase2", "phase3", "time_stop"}


def exit_watch_decision_is_verified(item, today):
    """Fail closed unless the browser synced the same calculated sell gate."""
    if item.get("decisionVerified") is not True:
        return False, "decision_not_verified"
    if item.get("policyVersion") != "portfolio-exit-v2":
        return False, "unknown_exit_policy"
    if item.get("decisionDataReady") is not True:
        return False, "decision_data_not_ready"
    if str(item.get("decisionDate") or "")[:10] != str(today or "")[:10]:
        return False, "decision_not_today"
    if str(item.get("decisionType") or "") not in EXIT_WATCH_VERIFIED_DECISION_TYPES:
        return False, "decision_type_not_actionable"
    reasons = item.get("decisionReasons")
    if not isinstance(reasons, list) or len([reason for reason in reasons if str(reason).strip()]) < 2:
        return False, "insufficient_decision_evidence"
    return True, ""
# 2026-07-08 使用者回饋「守門員不用，我多半在電腦前」：關閉伺服器端離線停損守門員。
# 保留整段程式碼與常數,想恢復把這個旗標改回 True 即可(不刪功能)。關閉後:瀏覽器
# 關著時沒有伺服器端停損 LINE 代看;瀏覽器開著時的硬停損通知完全不受影響——那條
# 走前端 shouldNotifyPortfolioAlert 的 hardStopOverride,與守門員是兩條獨立路徑。
EXIT_GUARDIAN_ENABLED = False
exit_guardian_last_check = 0.0


def read_exit_watch_state():
    with backend.connect() as conn:
        row = conn.execute("SELECT value FROM model_meta WHERE key = ?", (EXIT_WATCH_STATE_KEY,)).fetchone()
    if not row or not row[0]:
        return None
    try:
        data = json.loads(row[0])
    except (json.JSONDecodeError, TypeError):
        return None
    return data if isinstance(data, dict) else None


def _read_exit_guardian_notify_state(today):
    with backend.connect() as conn:
        row = conn.execute(
            "SELECT value FROM model_meta WHERE key = ?", (EXIT_GUARDIAN_NOTIFY_STATE_KEY,)
        ).fetchone()
    data = {}
    if row:
        try:
            data = json.loads(row[0] or "{}")
        except (json.JSONDecodeError, TypeError):
            data = {}
    if str(data.get("date") or "") != today:
        return {"date": today, "symbols": [], "lineCount": 0}
    return {
        "date": today,
        "symbols": [str(s) for s in (data.get("symbols") or [])],
        "lineCount": int(data.get("lineCount") or 0),
    }


def _write_exit_guardian_notify_state(state):
    with backend.connect() as conn:
        backend.set_meta(conn, EXIT_GUARDIAN_NOTIFY_STATE_KEY, json.dumps(state, ensure_ascii=False))


def _today_ex_dividend_symbols(today):
    # 除權息當天參考價直接下調，用昨天同步的防守價比對會假觸發。
    # 沿用 holdings_dividend_calendar 的日快取；快取沒有(還沒有人開過
    # 日曆)就回空集合，寧可漏掉這個豁免也不在守門路徑上打 FinMind。
    with backend.connect() as conn:
        row = conn.execute("SELECT value FROM model_meta WHERE key = ?", ("dividend_calendar_cache",)).fetchone()
    if not row or not row[0]:
        return set()
    try:
        cached = json.loads(row[0])
    except (json.JSONDecodeError, TypeError):
        return set()
    return {
        str(item.get("symbol") or "")
        for item in (cached.get("items") or [])
        if str(item.get("exDate") or "") == today
    }


def ensure_exit_decision_log_table(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS exit_decision_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            decision_date TEXT NOT NULL,
            symbol TEXT NOT NULL,
            name TEXT,
            current_price REAL,
            stop_price REAL,
            confirm_sell_price REAL,
            channel TEXT,
            reason TEXT,
            message TEXT
        )
    """)
    existing = {
        row[1] for row in conn.execute("PRAGMA table_info(exit_decision_logs)").fetchall()
    }
    additions = {
        "decision_type": "TEXT",
        "decision_verified": "INTEGER",
        "decision_reasons": "TEXT",
        "decision_at": "TEXT",
        "decision_data_date": "TEXT",
        "quote_source": "TEXT",
    }
    for name, definition in additions.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE exit_decision_logs ADD COLUMN {name} {definition}")


def record_exit_decision_logs(breaches, channel, reason, message):
    if not breaches:
        return 0
    today = scheduler_today(taipei_localtime())
    created_at = time.strftime("%Y-%m-%d %H:%M:%S", taipei_localtime())
    saved = 0
    try:
        with backend.connect() as conn:
            ensure_exit_decision_log_table(conn)
            for item in breaches:
                conn.execute("""
                    INSERT INTO exit_decision_logs (
                        created_at, decision_date, symbol, name, current_price,
                        stop_price, confirm_sell_price, channel, reason, message,
                        decision_type, decision_verified, decision_reasons,
                        decision_at, decision_data_date, quote_source
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    created_at,
                    today,
                    item.get("code"),
                    item.get("name") or "",
                    item.get("current"),
                    item.get("stop"),
                    item.get("sell"),
                    channel,
                    reason or "",
                    message,
                    item.get("decisionType") or "",
                    1 if item.get("decisionVerified") else 0,
                    json.dumps(item.get("decisionReasons") or [], ensure_ascii=False),
                    item.get("decisionAt") or "",
                    item.get("decisionDataDate") or "",
                    item.get("quoteSource") or "",
                ))
                saved += 1
    except Exception as exc:
        print(f"exit decision log write failed: {exc}")
    return saved


def record_frontend_portfolio_sell_notification(payload, result, message, requested_channel):
    if str((payload or {}).get("category") or "") != "portfolio_sell":
        return 0
    decision = (payload or {}).get("decision")
    if not isinstance(decision, dict):
        decision = {}
    symbol = "".join(ch for ch in str(decision.get("code") or "") if ch.isdigit())[:6]
    if not symbol:
        match = re.search(r"(?<!\d)(\d{4,6})(?!\d)", str(message or ""))
        symbol = match.group(1) if match else ""
    if not symbol:
        return 0
    sent = bool((result or {}).get("sent"))
    if sent:
        reason = "frontend_portfolio_sell"
    elif (result or {}).get("muted"):
        reason = "sell_line_muted"
    elif (result or {}).get("disabled"):
        reason = "line_disabled"
    elif (result or {}).get("suppressed"):
        reason = "quota_suppressed"
    else:
        reason = "not_sent"
    return record_exit_decision_logs([{
        "code": symbol,
        "name": str(decision.get("name") or "")[:80],
        "current": decision.get("currentPrice"),
        "stop": decision.get("stopLoss"),
        "sell": decision.get("confirmSellPrice"),
        "decisionType": str(decision.get("decisionType") or "")[:24],
        "decisionVerified": decision.get("decisionVerified") is True,
        "decisionReasons": decision.get("decisionReasons") or [],
        "decisionAt": str(decision.get("decisionAt") or "")[:40],
        "decisionDataDate": str(decision.get("decisionDataDate") or "")[:10],
        "quoteSource": str(decision.get("quoteSource") or "")[:80],
    }], requested_channel if sent else "none", reason, message)


def list_exit_decision_logs(limit=80):
    with backend.connect() as conn:
        ensure_exit_decision_log_table(conn)
        conn.row_factory = sqlite3.Row
        rows = conn.execute("""
            SELECT *
            FROM exit_decision_logs
            ORDER BY id DESC
            LIMIT ?
        """, (max(1, min(int(limit or 80), 300)),)).fetchall()
    return {"ok": True, "logs": [dict(row) for row in rows]}


def check_portfolio_exit_guardian(force=False):
    """守門檢查主體。回傳 dict 描述這一輪做了什麼(方便測試斷言與排查)。
    防護設計比照盤中進場推播：stale 報價跳過、每檔每日去重、每日 LINE
    上限 3 則、送達成功才寫去重狀態(失敗下一輪自動重試)、critical 級
    (額度保留池內仍放行——這正是保留池要保護的通知)。"""
    global exit_guardian_last_check
    if not EXIT_GUARDIAN_ENABLED:
        return {"checked": False, "skipped": "disabled"}
    if not force and time.time() - exit_guardian_last_check < EXIT_GUARDIAN_CHECK_INTERVAL:
        return {"checked": False, "skipped": "throttled"}
    exit_guardian_last_check = time.time()
    watch = read_exit_watch_state()
    if not watch or not watch.get("items"):
        return {"checked": False, "skipped": "no_watch_data"}
    if watch.get("monitoring") is False:
        # 使用者主動關閉「背景監控」核取方塊(跟心跳斷了是完全不同的語意)：
        # 尊重使用者的意圖，整個守門檢查都不做，不要因為之後心跳過期就
        # 誤判成離線並接管、對使用者不想要的監控發出通知。
        return {"checked": False, "skipped": "monitoring_disabled"}
    heartbeat_age = time.time() - float(watch.get("tsEpoch") or 0)
    if heartbeat_age < EXIT_GUARDIAN_HEARTBEAT_MAX_AGE:
        return {"checked": False, "skipped": "browser_online", "heartbeatAge": heartbeat_age}
    items = watch.get("items") or []
    today = scheduler_today(taipei_localtime())
    verified_items = []
    rejected_decisions = Counter()
    for item in items:
        verified, reject_reason = exit_watch_decision_is_verified(item, today)
        if verified:
            verified_items.append(item)
        else:
            rejected_decisions[reject_reason] += 1
    if not verified_items:
        return {
            "checked": False,
            "skipped": "no_verified_exit_decision",
            "decisionRejected": dict(rejected_decisions),
        }
    items = verified_items
    codes = [str(item.get("code") or "") for item in items if item.get("code")]
    if not codes:
        return {"checked": False, "skipped": "no_codes"}
    quote_payload = sinopac_backend.quotes(codes)
    if quote_payload.get("stale"):
        return {"checked": True, "skipped": "stale_quotes"}
    quotes = quote_payload.get("quotes") or {}
    ex_dividend_today = _today_ex_dividend_symbols(today)
    notify_state = _read_exit_guardian_notify_state(today)
    breaches = []
    for item in items:
        code = str(item.get("code") or "")
        stop = float(item.get("stopLoss") or 0)
        # 「真的要賣才提醒」:觸發線用「確認賣出價」(前端同步的 confirmSell = 防守價再跌1%),
        # 不是剛摸到防守價(警戒)就發。舊快取沒 confirmSell 就用 stopLoss×係數自行推算,一樣
        # 保留 1% 緩衝,不對只低於防守價幾分錢的盤中 graze 發通知。
        sell_level = float(item.get("confirmSell") or 0)
        if not (sell_level > 0):
            sell_level = stop * EXIT_GUARDIAN_CONFIRM_FACTOR if stop > 0 else 0.0
        if not code or not (sell_level > 0):
            continue
        if code in notify_state["symbols"] or code in ex_dividend_today:
            continue
        quote = quotes.get(code) or {}
        current = float(quote.get("currentPrice") or 0)
        # 2026-07-07 稽核:前端同步來的 confirmSell 可能被 current×0.99 夾擠而恆低於現價
        # (跳空/急殺貫穿停損時 current<=sell_level 永遠不成立)。額外用「絕對停損確認線
        # stopLoss×0.99(不隨現價下移,保留同樣 1% 緩衝)」兜底:跌破絕對停損也算跌破。
        # 絕對停損兜底改用前端同步的 absStop(avgPrice 基準、不隨現價下移);跳空/急殺時
        # 防守價與 confirmSell 都被夾在現價下方追不上,唯有絕對線能觸發。舊快取沒 absStop
        # 就退回用防守價 stop 推算(至少維持修前行為,不會更差)。
        abs_line = float(item.get("absStop") or 0)
        if not (abs_line > 0):
            abs_line = stop
        abs_stop_trigger = abs_line * EXIT_GUARDIAN_CONFIRM_FACTOR if abs_line > 0 else 0.0
        if current > 0 and (current <= sell_level or (abs_stop_trigger > 0 and current <= abs_stop_trigger)):
            breaches.append({
                "code": code,
                "name": str(item.get("name") or ""),
                "stop": stop,
                "sell": sell_level,
                "current": current,
                "decisionType": item.get("decisionType") or "",
                "decisionVerified": True,
                "decisionReasons": item.get("decisionReasons") or [],
                "decisionAt": item.get("decisionAt") or "",
                "decisionDataDate": item.get("decisionDataDate") or "",
                "quoteSource": item.get("quoteSource") or "",
            })
    if not breaches:
        return {"checked": True, "breaches": 0}
    # 2026-07-04 稽核發現：watch 快照是離線前最後一次同步，如果使用者在離線
    # 期間透過這套系統以外的管道(例如永豐官方APP)把股票賣掉，伺服器手上的
    # 舊快照仍會對已出清的股票發出停損通知、浪費每日額度。發通知前額外核對
    # 一次真實持股，濾掉已經不再持有的代碼。核對本身失敗(網路/API問題)
    # 就不濾——寧可多發一則可能失真的通知，也不能因為驗證步驟本身出錯而
    # 讓真正該通知的停損被吞掉。
    try:
        holdings_payload = sinopac_backend.holdings()
        if holdings_payload.get("ok"):
            held_codes = {str(item.get("code") or "") for item in (holdings_payload.get("holdings") or [])}
            breaches = [b for b in breaches if b["code"] in held_codes]
    except Exception:
        pass
    if not breaches:
        return {"checked": True, "breaches": 0, "skipped": "no_longer_held"}
    synced_at = str(watch.get("ts") or "")[11:16] or "?"
    lines = [f"🛡️ StockAI 停損守門員（{time.strftime('%H:%M')}，瀏覽器離線代看）"]
    for b in breaches[:5]:
        evidence = "、".join(b.get("decisionReasons") or [])
        lines.append(
            f"{b['code']} {b['name']} 現價 {b['current']:.2f} ≤ 確認賣出價 {b['sell']:.2f}"
            f"｜計算驗證：{evidence}".rstrip()
        )
    if len(breaches) > 5:
        lines.append(f"…另有 {len(breaches) - 5} 檔跌破")
    lines.append(f"（已跌破防守價再往下的確認賣出價、依 {synced_at} 同步；請開啟看盤頁確認是否出場）")
    message = "\n".join(lines)
    safe_print(message)
    fresh_codes = [b["code"] for b in breaches]

    def _mark_notified():
        notify_state["symbols"].extend(fresh_codes)
        _write_exit_guardian_notify_state(notify_state)

    # 2026-07-09 賣出 LINE 全域靜音(跨裝置)也要涵蓋守門員這條伺服器端路徑:旗標開啟時
    # 不送 LINE、改桌面代替(跟下面「每日上限」fallback 同語意),兌現 handle_line_notify
    # 註解宣稱的「任何裝置送賣出類 LINE 都在此擋掉」——否則守門員(可再打開的開關)會繞過靜音。
    if _read_meta_flag(SELL_LINE_MUTED_META_KEY):
        try:
            send_windows_desktop_notification("停損守門員", message)
        except Exception:
            pass
        record_exit_decision_logs(breaches, "desktop", "sell_line_muted", message)
        _mark_notified()
        return {"checked": True, "breaches": len(breaches), "channel": "desktop", "reason": "sell_line_muted"}

    if notify_state["lineCount"] >= EXIT_GUARDIAN_DAILY_LINE_CAP:
        try:
            send_windows_desktop_notification("停損守門員", message)
        except Exception:
            pass
        record_exit_decision_logs(breaches, "desktop", "daily_line_cap", message)
        _mark_notified()
        return {"checked": True, "breaches": len(breaches), "channel": "desktop", "reason": "daily_line_cap"}
    result = send_line_message_via_api(message, priority="critical")
    if result.get("sent"):
        notify_state["lineCount"] += 1
        record_exit_decision_logs(breaches, "line", "confirm_sell_breached", message)
        _mark_notified()
        return {"checked": True, "breaches": len(breaches), "channel": "line"}
    if result.get("disabled"):
        try:
            send_windows_desktop_notification("停損守門員", message)
        except Exception:
            pass
        record_exit_decision_logs(breaches, "desktop", "line_disabled", message)
        _mark_notified()
        return {"checked": True, "breaches": len(breaches), "channel": "desktop", "reason": "line_disabled"}
    record_exit_decision_logs(breaches, "none", "not_sent", message)
    return {"checked": True, "breaches": len(breaches), "channel": "none"}


def intraday_discovery_window(now=None):
    now = now or taipei_localtime()
    minutes = now.tm_hour * 60 + now.tm_min
    return 0 <= now.tm_wday <= 4 and (9 * 60) <= minutes <= (13 * 60 + 30)


def update_intraday_market_discovery(trigger="auto", force=False):
    """Refresh display-only whole-market intraday strength observations."""
    global intraday_discovery_running, intraday_discovery_last_attempt
    global intraday_discovery_status
    started = time.time()
    previous_confirmation_status = {}
    with intraday_discovery_lock:
        if intraday_discovery_running:
            return dict(intraday_discovery_status)
        previous_confirmation_status = dict(intraday_discovery_status)
        retrying = intraday_discovery_status.get("ok") is False
        cooldown = (
            INTRADAY_DISCOVERY_RETRY_SECONDS
            if retrying else INTRADAY_DISCOVERY_INTERVAL_SECONDS
        )
        if (
            not force
            and intraday_discovery_last_attempt
            and started - intraday_discovery_last_attempt < cooldown
        ):
            return dict(intraday_discovery_status)
        intraday_discovery_running = True
        intraday_discovery_last_attempt = started
        intraday_discovery_status = {
            **intraday_discovery_status,
            "running": True,
            "trigger": trigger,
            "error": "",
            "message": "正在讀取券商全市場輪巡、五類排行與即時 Tick",
        }
    try:
        now_tm = taipei_localtime()
        today = scheduler_today(now_tm)
        if not intraday_discovery_window(now_tm):
            result = {
                "ok": True,
                "running": False,
                "active": False,
                "checkedAt": time.strftime("%Y-%m-%d %H:%M:%S", now_tm),
                "trigger": trigger,
                "leaders": [],
                "observationOnly": True,
                "skipped": "outside_market_session",
                "message": "不在盤中時段，不執行全市場探索",
                "error": "",
            }
        else:
            market_day = official_market_day_status(today)
            if not market_schedule_allowed(market_day):
                result = {
                    "ok": True,
                    "running": False,
                    "active": False,
                    "checkedAt": time.strftime("%Y-%m-%d %H:%M:%S", now_tm),
                    "trigger": trigger,
                    "leaders": [],
                    "observationOnly": True,
                    "skipped": "market_closed",
                    "marketDay": market_day,
                    "message": "今日休市，不執行全市場探索",
                    "error": "",
                }
            else:
                scanner_status = backend.latest_intraday_scanner_payload(
                    trading_date=today,
                    max_age_seconds=150,
                )
                scanner_fallback_used = False
                scanner_error = ""
                if not scanner_status.get("fresh"):
                    try:
                        direct_scanner = sinopac_backend.market_scanners(count=200)
                        backend.upsert_intraday_scanner_rows(
                            direct_scanner.get("rows") or [],
                            trading_date=today,
                            scan_at=direct_scanner.get("scanAt") or now_text(),
                        )
                        scanner_status = backend.latest_intraday_scanner_payload(
                            trading_date=today,
                            max_age_seconds=150,
                        )
                        if not scanner_status.get("rows"):
                            scanner_status = {
                                **direct_scanner,
                                "fresh": bool(direct_scanner.get("quotes")),
                            }
                        scanner_fallback_used = True
                    except Exception as scanner_exc:
                        scanner_error = str(scanner_exc)[:500]

                scanner_rows = scanner_status.get("rows") or []
                scanner_quotes = scanner_status.get("quotes")
                if not isinstance(scanner_quotes, dict):
                    scanner_quotes = {
                        str(item.get("symbol") or ""): {
                            **item,
                            "source": item.get("source") or "sinopac_shioaji_scanner",
                        }
                        for item in scanner_rows
                        if item.get("symbol")
                    }

                rotation_payload = backend.latest_intraday_rotation_payload(
                    trading_date=today,
                    max_age_seconds=INTRADAY_DISCOVERY_ROTATION_MAX_AGE_SECONDS,
                )
                all_rotation_quotes = rotation_payload.get("quotes")
                if not isinstance(all_rotation_quotes, dict):
                    all_rotation_quotes = {}
                rotation_cycle = rotation_payload.get("latestCycle") or {}
                cycle_requested = [
                    str(symbol) for symbol in (
                        rotation_cycle.get("requestedSymbols") or []
                    ) if str(symbol)
                ]
                rotation_quotes = (
                    {
                        code: all_rotation_quotes[code]
                        for code in cycle_requested if code in all_rotation_quotes
                    }
                    if cycle_requested else all_rotation_quotes
                )
                combined_quotes = {**rotation_quotes, **scanner_quotes}
                baseline_symbols = list(dict.fromkeys([
                    *[str(item.get("symbol") or "") for item in scanner_rows],
                    *list(scanner_quotes),
                    *cycle_requested,
                    *list(rotation_quotes),
                ]))
                baseline_symbols = [symbol for symbol in baseline_symbols if symbol]
                try:
                    tick_quotes = backend.latest_intraday_tick_quotes(
                        baseline_symbols,
                        trading_date=today,
                        max_age_seconds=60,
                    )
                except Exception as tick_exc:
                    tick_quotes = {}
                    if not scanner_error:
                        scanner_error = f"realtime tick: {str(tick_exc)[:400]}"
                combined_quotes = merge_intraday_tick_quotes(
                    combined_quotes,
                    tick_quotes,
                )
                apply_intraday_quote_acceleration(combined_quotes)
                if not combined_quotes:
                    raise RuntimeError(
                        scanner_error
                        or "永豐全市場排行與輪巡快照都沒有可用報價"
                    )
                eligible_symbols = backend.listed_symbols()
                baselines = intraday_discovery_baselines_for_quotes(
                    baseline_symbols,
                    combined_quotes,
                )
                radar_payload = backend.list_monster_scores(
                    MAX_MONSTER_INTRADAY_CANDIDATES
                )
                radar_candidates = radar_payload.get("candidates") or []
                radar_codes = [
                    str(item.get("symbol") or "")
                    for item in radar_candidates
                    if item.get("symbol")
                ]
                quote_payload = {
                    "ok": True,
                    "stale": False,
                    "quotes": combined_quotes,
                    "source": "Shioaji scanner + all-market rotation + realtime tick",
                }
                quote_now = datetime(
                    now_tm.tm_year, now_tm.tm_mon, now_tm.tm_mday,
                    now_tm.tm_hour, now_tm.tm_min, now_tm.tm_sec,
                    tzinfo=TAIPEI_TZ,
                )
                built = build_intraday_market_discovery(
                    baselines,
                    quote_payload,
                    radar_codes=radar_codes,
                    now=quote_now,
                    requested_symbols=baseline_symbols,
                    scanner_symbols=scanner_quotes,
                )
                apply_intraday_discovery_confirmations(
                    built.get("_qualifiedRows") or [],
                    previous_confirmation_status,
                    now=quote_now,
                )
                qualified_rows = built.get("_qualifiedRows") or []
                deployment_readiness = radar_payload.get("deploymentReadiness") or {}
                decision_validity = radar_payload.get("decisionValidity") or {}
                market_regime = radar_payload.get("marketRegime") or {}
                performance_veto = deployment_readiness.get("formalReady") is not True
                daily_reference_date = str(
                    backend.latest_complete_price_date() or ""
                )[:10]
                radar_data_health = radar_market_data_health()
                try:
                    session_acceptance = backend.market_session_acceptance(today)
                except Exception as acceptance_exc:
                    session_acceptance = {
                        "ok": False,
                        "entryGuardReady": False,
                        "error": str(acceptance_exc)[:300],
                    }
                decision_market_data_fresh = bool(
                    radar_data_health.get("ok")
                    and session_acceptance.get("entryGuardReady") is True
                )
                formal_contexts, formal_context_errors = (
                    load_intraday_discovery_formal_contexts(
                        qualified_rows,
                        radar_candidates,
                        daily_reference_date=daily_reference_date,
                        performance_veto=performance_veto,
                        market_regime=market_regime,
                        decision_validity=decision_validity,
                    )
                )
                apply_intraday_candidate_rules(
                    qualified_rows,
                    formal_contexts,
                    now_tm=now_tm,
                    market_data_fresh=decision_market_data_fresh,
                )
                built["leaders"] = qualified_rows[:INTRADAY_DISCOVERY_RESULT_LIMIT]
                qualified_rows = built.pop("_qualifiedRows", [])
                audit_rows = built.pop("_auditRows", [])
                candidate_reasons = {
                    str(row.get("symbol") or ""): list(
                        row.get("candidateExclusionReasons") or []
                    )
                    for row in qualified_rows
                }
                for audit_row in audit_rows:
                    symbol = str(audit_row.get("symbol") or "")
                    if candidate_reasons.get(symbol):
                        audit_row["exclusionReasons"] = candidate_reasons[symbol]
                fresh_count = int(built.get("fresh") or 0)
                if fresh_count <= 0:
                    raise RuntimeError(
                        scanner_error
                        or "全市場券商報價沒有通過新鮮度檢查"
                    )
                checked_at = time.strftime("%Y-%m-%d %H:%M:%S", now_tm)
                try:
                    event_pipeline = backend.record_intraday_discovery_events(
                        qualified_rows,
                        trading_date=today,
                        observed_at=checked_at,
                    )
                except Exception as event_exc:
                    event_pipeline = {
                        "ok": False,
                        "inserted": 0,
                        "error": str(event_exc)[:500],
                    }
                try:
                    candidate_signal_pipeline = backend.record_intraday_candidate_signals(
                        qualified_rows,
                        signal_date=today,
                        signaled_at=checked_at,
                    )
                except Exception as signal_exc:
                    candidate_signal_pipeline = {
                        "ok": False,
                        "prepared": 0,
                        "inserted": 0,
                        "error": str(signal_exc)[:500],
                    }
                latest_cycle = rotation_payload.get("latestCycle") or {}
                audit_scope = set(latest_cycle.get("requestedSymbols") or [])
                audit_scope.update(scanner_quotes)
                scoped_audit_rows = [
                    row for row in audit_rows
                    if str(row.get("symbol") or "") in audit_scope
                ]
                try:
                    audit_pipeline = backend.record_intraday_discovery_audit(
                        scoped_audit_rows,
                        trading_date=today,
                        observed_at=checked_at,
                    )
                except Exception as audit_exc:
                    audit_pipeline = {
                        "ok": False,
                        "evaluated": 0,
                        "error": str(audit_exc)[:500],
                    }
                missing = [code for code in baseline_symbols if code not in combined_quotes]
                confirmed_count = sum(
                    1 for row in qualified_rows if row.get("consecutiveConfirmed")
                )
                signal_count = sum(
                    1 for row in qualified_rows if row.get("candidateSignal")
                )
                formal_buyable_count = sum(
                    1 for row in qualified_rows if row.get("formalCanBuy")
                )
                result = {
                    "ok": True,
                    "running": False,
                    "active": True,
                    "checkedAt": checked_at,
                    "trigger": trigger,
                    "source": quote_payload.get("source") or "Shioaji scanner",
                    "universe": len(eligible_symbols),
                    "scannedUniverse": len(baseline_symbols),
                    "baselineCount": len(baselines),
                    **built,
                    "scanner": {
                        "ok": bool(scanner_rows),
                        "fresh": bool(scanner_status.get("fresh")),
                        "count": len(scanner_rows),
                        "scanAt": scanner_status.get("scanAt"),
                        "ageSeconds": scanner_status.get("ageSeconds"),
                        "fallbackUsed": scanner_fallback_used,
                        "error": scanner_error,
                    },
                    "deepConfirmation": {
                        "intervalSeconds": INTRADAY_DISCOVERY_INTERVAL_SECONDS,
                        "requiredScans": INTRADAY_DISCOVERY_CONFIRMATION_SCANS,
                        "tickQuotes": len(tick_quotes),
                        "confirmed": confirmed_count,
                    },
                    "fullMarketRotation": {
                        "ok": bool(rotation_payload.get("ok")),
                        "scanAt": rotation_payload.get("scanAt"),
                        "fresh": len(rotation_quotes),
                        "stale": int(rotation_payload.get("staleCount") or 0),
                        "targetSeconds": 120,
                        "source": rotation_payload.get("source") or "",
                        "latestCycle": latest_cycle,
                    },
                    "eventPipeline": event_pipeline,
                    "auditPipeline": audit_pipeline,
                    "candidateSignalPipeline": candidate_signal_pipeline,
                    "formalContextErrors": formal_context_errors,
                    "radarDataHealth": radar_data_health,
                    "sessionAcceptance": session_acceptance,
                    "dailyDataReferenceDate": daily_reference_date,
                    "candidateSignalCount": signal_count,
                    "formalBuyableCount": formal_buyable_count,
                    "confirmationRows": qualified_rows,
                    "missingCount": len(missing),
                    "missingSymbols": missing[:30],
                    "partial": bool(missing) or not bool(built.get("coverageComplete")),
                    "durationSeconds": round(time.time() - started, 2),
                    "message": (
                        f"盤中全市場探索 {fresh_count}/{len(baselines)} 檔，"
                        f"輪巡新鮮 {len(rotation_quotes)}/{len(eligible_symbols)} 檔，"
                        f"永豐排行 {len(scanner_rows)} 檔，"
                        f"符合 {int(built.get('qualified') or 0)} 檔、"
                        f"兩輪確認 {confirmed_count} 檔、紙上訊號 {signal_count} 檔、"
                        f"正式可買 {formal_buyable_count} 檔；正式門檻未下修"
                    ),
                    "error": "",
                }
    except Exception as exc:
        previous = dict(intraday_discovery_status)
        result = {
            **previous,
            "ok": False,
            "running": False,
            "active": False,
            "trigger": trigger,
            "stale": bool(previous.get("checkedAt")),
            "durationSeconds": round(time.time() - started, 2),
            "message": "盤中全市場探索暫時失敗，保留上次觀察結果並稍後重試",
            "error": str(exc)[:500],
        }
    with intraday_discovery_lock:
        intraday_discovery_running = False
        intraday_discovery_status = result
    with monster_intraday_lock:
        monster_intraday_status["marketDiscovery"] = dict(result)
    persist_runtime_status(INTRADAY_DISCOVERY_STATUS_META_KEY, result)
    cache.clear()
    return result


# tick collector 每 20 秒 flush 一次(realtime_tick_collector.py FLUSH_INTERVAL_SECONDS)，
# 門檻抓 9 倍(180秒)容忍正常抖動，但抓得出斷線(on_session_down)且當天重啟次數
# 已達上限、整段時間沒再啟動的情況——不檢查新鮮度的話，realtime_flow_staging
# 裡當天早上的舊資料仍然符合 WHERE date=今天，會被原封不動當成「現在」的主力
# 動向顯示給交易者。
REALTIME_FLOW_STALE_SECONDS = 180


def realtime_flow_is_stale(updated_at_text, now=None):
    # updated_at 是 collector 用系統本地時間寫入(now_text())，這裡比較基準也
    # 用系統本地時間(而非 taipei_localtime())，兩邊一致；解析失敗一律當作
    # 不新鮮(fail-safe，寧可多顯示警告也不要讓壞掉的時間字串冒充新鮮資料)。
    if not updated_at_text:
        return False
    try:
        age_seconds = ((now or datetime.now()) - datetime.strptime(updated_at_text, "%Y-%m-%d %H:%M:%S")).total_seconds()
    except (TypeError, ValueError):
        return True
    return age_seconds > REALTIME_FLOW_STALE_SECONDS


def update_monster_intraday_quotes(trigger="auto"):
    global monster_intraday_running, monster_intraday_running_since, monster_intraday_last_update, monster_intraday_status
    with monster_intraday_lock:
        if monster_intraday_running:
            if monster_intraday_running_since and time.time() - monster_intraday_running_since < 20:
                return {"ok": True, "running": True, "message": "intraday quote update already running"}
            monster_intraday_running = False
        monster_intraday_running = True
        monster_intraday_running_since = time.time()
    try:
        now_tm = taipei_localtime()
        today = scheduler_today(now_tm)
        if not (0 <= now_tm.tm_wday <= 4):
            market_day = {
                "known": True,
                "isTradingDay": False,
                "date": today,
                "reason": "週末",
                "source": "weekday calendar",
            }
        else:
            try:
                market_day = official_market_day_status(today)
            except Exception as exc:
                market_day = {
                    "known": False,
                    "isTradingDay": None,
                    "date": today,
                    "reason": str(exc)[:160],
                }
        if not market_schedule_allowed(market_day):
            reason = market_day.get("reason") or "無法確認為交易日"
            with monster_intraday_lock:
                monster_intraday_last_update = time.time()
                monster_intraday_status = {
                    "ok": True,
                    "active": False,
                    "marketClosed": market_day.get("known") is True,
                    "marketDayKnown": market_day.get("known") is True,
                    "marketDay": market_day,
                    "entryWindow": {"active": False, "phase": "closed", "label": "今日休市，不進場"},
                    "updatedAt": time.strftime("%Y-%m-%d %H:%M:%S", now_tm),
                    "source": market_day.get("source") or "official market calendar",
                    "trigger": trigger,
                    "count": 0,
                    "candidateCount": 0,
                    "buyableCount": 0,
                    "shadowBuyableCount": 0,
                    "snapshotPipeline": {
                        "date": today,
                        "checkedAt": time.strftime("%Y-%m-%d %H:%M:%S", now_tm),
                        "ok": True,
                        "expected": 0,
                        "persisted": 0,
                        "inserted": 0,
                        "duplicates": 0,
                        "missingSymbols": [],
                        "skipped": "market_closed",
                    },
                    "quoteCount": 0,
                    "quoteOk": False,
                    "quoteStale": False,
                    "quoteCoverage": {"requested": 0, "received": 0, "missing": [], "complete": True},
                    "marketDiscovery": dict(intraday_discovery_status),
                    "quotes": {},
                    "error": "",
                    "message": f"{reason}；休市不登入券商、不更新盤中報價",
                    "health": {
                        "ok": True,
                        "mode": "market_closed",
                        "reason": reason,
                        "quoteCount": 0,
                    },
                }
            cache.clear()
            return monster_intraday_status
        payload = backend.list_monster_scores(MAX_MONSTER_INTRADAY_CANDIDATES)
        candidates = payload.get("candidates", [])
        candidate_source = "stored"
        radar_data_health = radar_market_data_health()
        try:
            session_acceptance = backend.market_session_acceptance(today)
        except Exception as exc:
            session_acceptance = {
                "ok": False,
                "sessionDate": today,
                "entryGuardReady": False,
                "error": str(exc)[:160],
            }
        entry_guard_ready = session_acceptance.get("entryGuardReady") is True
        decision_market_data_fresh = bool(radar_data_health.get("ok")) and entry_guard_ready
        daily_data_reference_date = ""
        try:
            with backend.connect() as conn:
                row = conn.execute("SELECT MAX(date) FROM prices").fetchone()
            daily_data_reference_date = str(row[0] or "")[:10] if row else ""
        except Exception:
            daily_data_reference_date = ""
        if not candidates:
            fallback_candidates = []
            for symbol in backend.liquid_monster_universe(100):
                try:
                    quick = backend.quick_monster_filter(symbol) or {}
                    rows = backend.load_price_rows(symbol)
                    latest = rows[-1] if rows else {}
                    close = float(latest.get("close") or 0)
                    if not close:
                        continue
                    fallback_candidates.append({
                        "symbol": symbol,
                        "close": close,
                        "score": quick.get("score", 0),
                        "avgVolume20Lots": quick.get("avgVolume20Lots") or quick.get("avg_volume20_lots") or 0,
                        "buyTrigger": close * 1.005,
                        "stopPrice": close * 0.93,
                        "buyAllowed": False,
                        "status": "盤中報價候選，等待正式妖股掃描",
                    })
                except Exception:
                    continue
            fallback_candidates.sort(key=lambda item: float(item.get("score") or 0), reverse=True)
            candidates = fallback_candidates[:MAX_MONSTER_INTRADAY_CANDIDATES]
            candidate_source = "liquid_fallback"
        else:
            candidates = candidates[:MAX_MONSTER_INTRADAY_CANDIDATES]
        codes = [item.get("symbol") for item in candidates if item.get("symbol")]
        try:
            portfolio_codes = portfolio_cache_quote_symbols()
        except Exception as exc:
            portfolio_codes = []
            print(f"Portfolio quote symbol load failed: {exc}")
        quote_codes = list(dict.fromkeys([*codes, *portfolio_codes]))
        quote_payload = sinopac_backend.quotes(quote_codes)
        all_quotes = quote_payload.get("quotes") or {}
        # 雷達狀態、覆蓋率與通知只能看候選報價；持股報價雖共用同一批登入，
        # 不得灌進雷達分母或讓零候選看似有即時資料。
        quotes = {code: all_quotes[code] for code in codes if code in all_quotes}
        quote_source = quote_payload.get("source") or "Shioaji quote"
        if quote_payload.get("stale"):
            quote_source = "Shioaji quote stale cache"
        elif quote_payload.get("cached") and quote_source == "Shioaji quote":
            quote_source = "Shioaji quote cache"
        quote_fresh = bool(quote_payload.get("ok", True)) and not bool(quote_payload.get("stale"))
        quote_now = datetime(
            now_tm.tm_year, now_tm.tm_mon, now_tm.tm_mday,
            now_tm.tm_hour, now_tm.tm_min, now_tm.tm_sec,
            tzinfo=TAIPEI_TZ,
        )
        try:
            portfolio_quote_refresh = update_portfolio_summary_quote_cache(
                all_quotes, quote_payload, now=quote_now,
            )
        except Exception as exc:
            portfolio_quote_refresh = {
                "ok": False,
                "error": str(exc)[:160],
                "requested": len(portfolio_codes),
                "fresh": 0,
            }
            print(f"Portfolio quote cache refresh failed: {exc}")
        candidate_fallback_codes = [
            code for code in (quote_payload.get("fallbackCodes") or []) if code in codes
        ]
        by_code = {}
        volume_rule = early_session_volume_rule("monster", now_tm)
        volume_rules = stock_intraday_volume_rules(codes, now_tm, volume_rule)
        # 同一批快照共用同一個時間基準：entry_window 若在迴圈內逐檔重算，
        # 跨越 10:00/13:15 邊界時前後檔會套用互斥的階段規則。
        entry_window = monster_entry_window(now_tm)
        for item in candidates:
            code = str(item.get("symbol") or "")
            quote = quotes.get(code) or {}
            # 不信任 scan_date 當日線日期：掃描可能在盤中重跑，但指標仍只到
            # 昨日收盤。以 prices 全市場最新日K比對每檔 price_date 才能抓出
            # 斷更個股，避免兩天前型態被即時價格洗成可買。
            item = {**item, "dailyDataReferenceDate": daily_data_reference_date}
            item_quote_source = quote.get("source") or quote_source
            item_volume_rule = volume_rules.get(code, volume_rule)
            item_quote_fresh, item_quote_age, item_quote_freshness_reason = intraday_quote_freshness(
                quote, quote_fresh, now=quote_now,
            )
            by_code[code] = compute_monster_intraday_state(
                code, item, quote, bool(quotes.get(code)), entry_window, item_volume_rule["min"], item_volume_rule,
                item_quote_source, quote_fresh=item_quote_fresh,
                market_data_fresh=decision_market_data_fresh,
                quote_age_seconds=item_quote_age,
                quote_freshness_reason=item_quote_freshness_reason,
            )
        # 主力動向(純顯示欄位，不進 canBuy 判斷式)：tick collector 每 20 秒累積
        # 的當日主動買賣盤淨額(元)與 50 張以上大單淨流向(股)，讓交易者判斷
        # 突破背後有沒有大單在買。沒訂閱到的檔保持 None(前端顯示「無tick」，
        # 不能顯示 0 冒充「大單中性」)。
        try:
            flow_today = scheduler_today(now_tm)
            with backend.connect() as conn:
                placeholders = ",".join("?" for _ in codes)
                flow_rows = conn.execute(
                    "SELECT symbol, realtime_money_flow, realtime_large_order_flow, tick_count, updated_at "
                    f"FROM realtime_flow_staging WHERE date = ? AND symbol IN ({placeholders})",
                    [flow_today, *codes],
                ).fetchall() if codes else []
            flow_by_code = {
                str(r[0]): {"moneyFlow": r[1], "largeOrderFlow": r[2], "tickCount": r[3], "updatedAt": r[4]}
                for r in flow_rows
            }
        except Exception:
            flow_by_code = {}
        for code, state in by_code.items():
            flow = flow_by_code.get(code)
            state["realtimeMoneyFlow"] = flow.get("moneyFlow") if flow else None
            state["realtimeLargeOrderFlow"] = flow.get("largeOrderFlow") if flow else None
            state["realtimeTickCount"] = flow.get("tickCount") if flow else None
            state["realtimeFlowStale"] = realtime_flow_is_stale(flow.get("updatedAt")) if flow else False
            state["realtimeFlowUpdatedAt"] = flow.get("updatedAt") if flow else None
        # 第一次通過完整盤中閘門時保存可成交 ask/報價；績效觀察期用
        # shadowCanBuy 寫紙上快照但不通知、不下單。每輪同時持久化寫入明細，
        # 讓收盤驗證能抓出「有 shadow、卻沒有快照」的靜默資料遺失。
        snapshot_pipeline = record_radar_entry_snapshot_pipeline(
            candidates,
            by_code,
            signal_date=scheduler_today(now_tm),
            trigger=trigger,
        )
        flow_health = intraday_flow_health(
            quotes,
            quote_stale=bool(quote_payload.get("stale")),
            quote_ok=bool(quote_payload.get("ok", True)),
            radar_data_health=radar_data_health,
        )
        if snapshot_pipeline.get("expected", 0) > 0 and not snapshot_pipeline.get("ok"):
            flow_health = {
                **flow_health,
                "ok": False,
                "mode": "snapshot_failed",
                "reason": "盤中紙上訊號未完整寫入快照，正式買進持續停用",
                "snapshotPipeline": snapshot_pipeline,
            }
        if not entry_guard_ready:
            flow_health = {
                **flow_health,
                "ok": False,
                "mode": "observe_only",
                "reason": "當日 09:05 開盤資料驗證尚未通過，暫停盤中買進判斷",
                "entryGuardReady": False,
            }
        with monster_intraday_lock:
            # 覆寫前留住上一輪的判斷結果，給盤中進場訊號推播做 canBuy 翻正 diff
            previous_intraday_quotes = monster_intraday_status.get("quotes") or {}
            monster_intraday_last_update = time.time()
            monster_intraday_status = {
                "ok": True,
                "active": bool(entry_window.get("active")),
                "entryWindow": entry_window,
                "updatedAt": time.strftime("%Y-%m-%d %H:%M:%S"),
                "source": quote_source,
                "trigger": trigger,
                "count": len(by_code),
                "candidateCount": len(candidates),
                "buyableCount": sum(1 for state in by_code.values() if state.get("canBuy")),
                "shadowBuyableCount": sum(
                    1 for state in by_code.values() if state.get("shadowCanBuy")
                ),
                "snapshotPipeline": snapshot_pipeline,
                "candidateSource": candidate_source,
                "dailyDataReferenceDate": daily_data_reference_date,
                "radarDataHealth": radar_data_health,
                "sessionAcceptance": session_acceptance,
                "entryGuardReady": entry_guard_ready,
                "quoteCount": len(quotes),
                "quoteOk": bool(quote_payload.get("ok", True)),
                "quoteError": quote_payload.get("error") or "",
                "quoteCached": bool(quote_payload.get("cached")),
                "quoteStale": bool(quote_payload.get("stale")),
                "quotePartialCache": bool(quote_payload.get("partialCache")),
                "missingCacheCodes": quote_payload.get("missingCacheCodes") or [],
                "quoteCacheAgeSeconds": quote_payload.get("cacheAgeSeconds"),
                "quoteFallbackUsed": bool(candidate_fallback_codes),
                "quoteFallbackProvider": quote_payload.get("fallbackProvider") or "",
                "quoteFallbackCodes": candidate_fallback_codes,
                "quoteFallbackReason": quote_payload.get("fallbackReason") or "",
                "quoteCoverage": {
                    "requested": len(codes),
                    "received": len(quotes),
                    "missing": [code for code in codes if code not in quotes],
                    "complete": len(quotes) == len(codes),
                },
                "stockVolumeProfileCount": len(volume_rules),
                "portfolioQuoteRefresh": portfolio_quote_refresh,
                "marketDiscovery": dict(intraday_discovery_status),
                "quotes": by_code,
                "error": "",
                # 健康檢查要傳「實際即時報價數」(quotes)不是候選狀態數(by_code,恆~50)。
                # 傳 by_code 會讓 quoteCount 恆 >0,「零即時報價→暫停決策」的失明閘門永遠不觸發。
                "health": flow_health,
            }
        cache.clear()
        notification_checks = {
            "date": scheduler_today(now_tm),
            "checkedAt": now_text(),
            "errors": [],
        }
        try:
            notification_checks["entry"] = notify_intraday_entry_triggers(
                previous_intraday_quotes, by_code, quote_payload, candidates
            )
        except Exception as notify_exc:
            # 通知失敗不能影響盤中狀態更新本身；沒標記去重的話下一輪自動重試
            print(f"Intraday entry trigger notification failed: {notify_exc}")
            notification_checks["errors"].append(f"entry: {notify_exc}")
        try:
            # 漲停打開警示用原始快照(quotes 帶 reference/high/current)，不是 by_code
            notification_checks["limitUpOpen"] = notify_limit_up_open(quotes, candidates)
        except Exception as notify_exc:
            print(f"Limit-up-open notification failed: {notify_exc}")
            notification_checks["errors"].append(f"limitUpOpen: {notify_exc}")
        try:
            notification_checks["surge"] = notify_intraday_surge(quotes, candidates, by_code)
        except Exception as notify_exc:
            print(f"Intraday surge notification failed: {notify_exc}")
            notification_checks["errors"].append(f"surge: {notify_exc}")
        # ①-a 失明告警:成功路徑幾乎都 health.ok(直接 skip),只有登入成功卻整批
        # 空報價(quoteCount<=0)這種罕見子情況才會發。放在鎖外,不佔盤中狀態鎖。
        try:
            notification_checks["blackout"] = notify_intraday_quote_blackout(monster_intraday_status)
        except Exception as notify_exc:
            print(f"Intraday blackout notification failed: {notify_exc}")
            notification_checks["errors"].append(f"blackout: {notify_exc}")
        # ② 閘門量測(純記錄「今天多少檔翻可買/卡在哪個閘」,累積後才誠實回答太嚴與否)
        try:
            record_intraday_gate_stats(by_code, len(candidates), now_tm)
        except Exception as stats_exc:
            print(f"Intraday gate stats record failed: {stats_exc}")
        try:
            with backend.connect() as conn:
                backend.set_meta(
                    conn,
                    INTRADAY_NOTIFICATION_PIPELINE_KEY,
                    json.dumps(notification_checks, ensure_ascii=False),
                )
            with monster_intraday_lock:
                monster_intraday_status["notificationChecks"] = notification_checks
        except Exception as notification_state_exc:
            print(f"Intraday notification pipeline status write failed: {notification_state_exc}")
        return monster_intraday_status
    except Exception as exc:
        with monster_intraday_lock:
            previous_quotes = monster_intraday_status.get("quotes") or {}
            monster_intraday_status = {
                "ok": bool(previous_quotes),
                "active": monster_buy_confirm_window(),
                "entryWindow": monster_entry_window(),
                "updatedAt": time.strftime("%Y-%m-%d %H:%M:%S"),
                "source": monster_intraday_status.get("source") or "Shioaji quote",
                "count": len(previous_quotes),
                "buyableCount": sum(
                    1 for state in previous_quotes.values() if state.get("canBuy")
                ),
                "shadowBuyableCount": sum(
                    1 for state in previous_quotes.values() if state.get("shadowCanBuy")
                ),
                "snapshotPipeline": monster_intraday_status.get("snapshotPipeline") or {
                    "ok": False,
                    "error": str(exc)[:500],
                },
                "marketDiscovery": dict(intraday_discovery_status),
                "quotes": previous_quotes,
                "error": str(exc),
                "trigger": trigger,
                "health": intraday_flow_health(previous_quotes, str(exc)),
            }
            blackout_status = monster_intraday_status
        # ①-a:永豐盤中報價 raise(daily token/CA 過期最常見)= 雷達失明主路徑。
        # 出鎖後再發 critical LINE,不在持鎖期間做 DB/網路 I/O。
        try:
            notify_intraday_quote_blackout(blackout_status)
        except Exception as notify_exc:
            print(f"Intraday blackout notification failed: {notify_exc}")
        return blackout_status
    finally:
        with monster_intraday_lock:
            monster_intraday_running = False
            monster_intraday_running_since = 0


def normalize_training_symbols(symbols):
    output = []
    seen = set()
    for symbol in symbols or []:
        clean = "".join(char for char in str(symbol or "") if char.isdigit())[:4]
        if len(clean) != 4 or clean in RETIRED_SYMBOLS or clean in seen:
            continue
        seen.add(clean)
        output.append(clean)
    try:
        return backend.filter_active_symbols(output)
    except Exception:
        return output


def auto_model_training_symbols(monster_limit=600):
    # sector_diversified(DEFAULT_SYMBOLS)一定要跟holdings/monster_liquid取
    # 聯集，不能只在兩者都空時才當fallback用——這是daily_update.py早上完整
    # 更新用的build_daily_training_symbols()同一套設計原則(見該檔案註解)：
    # DEFAULT_SYMBOLS是手動維護、涵蓋全部產業分類的固定基礎樣本，這個15:00
    # 排程觸發的重訓如果漏掉它，會用一份窄化的訓練池覆蓋掉早上剛訓好、含有
    # 全產業基礎樣本的模型，讓「避免模型只認得少數熱門股」的防線在當天稍晚
    # 被繞過。
    sources = {
        "sector_diversified": normalize_training_symbols(DEFAULT_SYMBOLS),
        "holdings": [],
        "monster_liquid": [],
        "fallback": [],
    }
    errors = []

    try:
        payload = sinopac_backend.holdings()
        holding_rows = payload.get("holdings") or []
        sources["holdings"] = normalize_training_symbols(
            [item.get("code") for item in holding_rows] or payload.get("codes") or []
        )
    except Exception as exc:
        errors.append(f"holdings: {exc}")

    try:
        sources["monster_liquid"] = normalize_training_symbols(
            backend.liquid_monster_universe(monster_limit)
        )
    except Exception as exc:
        errors.append(f"monster_liquid: {exc}")

    symbols = normalize_training_symbols(
        sources["sector_diversified"] + sources["holdings"] + sources["monster_liquid"]
    )
    if not symbols:
        sources["fallback"] = normalize_training_symbols(DEFAULT_SYMBOLS)
        symbols = sources["fallback"]
    return symbols, sources, errors


def data_gap_repair_symbol_universe(max_symbols=120):
    max_symbols = max(1, min(int(max_symbols or 120), 300))
    symbols, sources, errors = auto_model_training_symbols(monster_limit=max_symbols)
    queued = {
        "integrity_problems": [],
        "pending_repairs": [],
        "model_prediction_gaps": [],
    }
    meta_keys = {
        "integrity_problems": "last_data_integrity_problem_symbols_json",
        "pending_repairs": "data_gap_repair_pending_symbols_json",
        "model_prediction_gaps": "last_batch_predictions_repair_symbols_json",
    }
    try:
        with backend.connect() as conn:
            lookup_keys = tuple(meta_keys.values()) + ("data_gap_repair_backoff_json",)
            placeholders = ",".join("?" for _ in lookup_keys)
            rows = dict(conn.execute(
                f"SELECT key, value FROM model_meta WHERE key IN ({placeholders})",
                lookup_keys,
            ).fetchall())
        for source_key, meta_key in meta_keys.items():
            raw = json.loads(rows.get(meta_key) or "[]")
            queued[source_key] = normalize_training_symbols(raw if isinstance(raw, list) else [])
        legacy_backoff = json.loads(rows.get("data_gap_repair_backoff_json") or "{}")
        if isinstance(legacy_backoff, dict):
            queued["pending_repairs"] = normalize_training_symbols(
                queued["pending_repairs"] + list(legacy_backoff.keys())
            )
    except (json.JSONDecodeError, TypeError, sqlite3.Error):
        pass
    sources.update(queued)
    # 真正的完整性問題與持股優先，其次是上次尚未補完的股票；獨立模型
    # 缺口排在正式規則資料之後。舊版直接用 symbols[:max]，而 symbols 以
    # DEFAULT_SYMBOLS 開頭，max=120 時持股和待補股票可能永遠輪不到。
    prioritized = normalize_training_symbols(
        queued["integrity_problems"]
        + list(sources.get("holdings") or [])
        + queued["pending_repairs"]
        + queued["model_prediction_gaps"]
        + list(sources.get("monster_liquid") or [])
        + list(sources.get("sector_diversified") or [])
        + symbols
    )
    return {
        "symbols": prioritized[:max_symbols],
        "sources": sources,
        "errors": errors,
        "maxSymbols": max_symbols,
    }


def data_gap_not_applicable_fields(symbol, latest, stock_info=None):
    info = (stock_info or {}).get(symbol) or {}
    sector = str(info.get("sector") or "")
    market_type = str(info.get("market_type") or "").lower()
    fields = {}
    quality_checks = {}
    if market_type == "emerging":
        fields.update({
            "margin_balance": "興櫃股票沒有信用交易融資餘額",
            "short_balance": "興櫃股票沒有信用交易融券餘額",
            "per": "興櫃官方行情未提供交易所本益比",
            "pbr": "興櫃官方行情未提供交易所股價淨值比",
            "dividend_yield": "興櫃官方行情未提供交易所殖利率",
        })
        quality_checks["marginSourceCoverageOk"] = "興櫃股票不適用融資券覆蓋率"
    if "金融" in sector:
        fields["gross_margin"] = "金融業財報不使用一般製造業毛利率口徑"
    elif (
        latest.get("gross_margin") is None
        and is_official_source(latest.get("financial_statement_source"))
        and (latest.get("roe") is not None or latest.get("debt_ratio") is not None)
    ):
        fields["gross_margin"] = "官方財報已取得，但該公司報表未列 GrossProfit，無法計算毛利率"
    if market_type != "emerging" and (
        latest.get("per") is None
        and is_official_source(latest.get("valuation_source"))
        and (latest.get("pbr") is not None or latest.get("dividend_yield") is not None)
    ):
        fields["per"] = "官方估值資料已回覆，但虧損或不適用股票不提供本益比"
    if (
        latest.get("revenue_growth") is None
        and latest.get("monthly_revenue") == 0
        and is_official_source(latest.get("revenue_source"))
    ):
        fields["revenue_growth"] = "官方營收為零或缺少去年同期基期，無法計算年增率"
    return fields, quality_checks


def data_gap_symbol_detail(symbol, updated_rows=0, stock_info=None):
    rows = backend.rows_with_verified_sources(backend.load_price_rows(symbol))
    quality = backend.model_data_quality(symbol, rows)
    latest = rows[-1] if rows else {}
    if stock_info is None:
        try:
            stock_info = backend.load_stock_info()
        except Exception:
            stock_info = {}
    not_applicable_fields, structural_quality = data_gap_not_applicable_fields(
        symbol, latest, stock_info=stock_info
    )
    if (
        str(((stock_info or {}).get(symbol) or {}).get("market_type") or "").lower() == "emerging"
        and float(quality.get("chipSourceCoverage") or 0) >= 0.8
    ):
        # model_data_quality 的 chipCoverage 同時把外資/投信與融資/融券算進
        # 分母；興櫃沒有後兩者時，即使官方法人資料完整仍會得到假缺口。
        structural_quality["chipCoverageOk"] = "興櫃沒有融資券，法人來源已達可用覆蓋率"
    if (
        "chipCoverageOk" in (quality.get("missing") or [])
        and float(quality.get("chipSourceCoverage") or 0) >= 0.8
    ):
        structural_quality["chipCoverageOk"] = "官方法人資料已完整，合併覆蓋率不足由融資券欄位造成"
    if (
        "marginSourceCoverageOk" in (quality.get("missing") or [])
        and is_official_source(latest.get("margin_source"))
        and (latest.get("margin_balance") is not None or latest.get("short_balance") is not None)
    ):
        structural_quality["marginSourceCoverageOk"] = (
            "目前官方融資券資料已取得，但該股票可用歷史期數不足，不能補造不存在的歷史"
        )
    missing = []
    source_gaps = []
    sources = {}
    recent_official_values = {}
    for key, _label, source_key in DATA_GAP_REQUIRED_FIELDS:
        for row in reversed(rows[-20:]):
            value = row.get(key)
            source = row.get(source_key)
            if value is not None and value != "" and is_official_source(source):
                recent_official_values[key] = row
                break
    for key, label, source_key in DATA_GAP_REQUIRED_FIELDS:
        if key in not_applicable_fields:
            continue
        value = latest.get(key)
        source = latest.get(source_key)
        recent_row = recent_official_values.get(key) or {}
        sources[source_key] = source or recent_row.get(source_key) or sources.get(source_key) or ""
        if value is None or value == "":
            if key not in recent_official_values:
                missing.append(label)
        elif not is_official_source(source):
            if key not in recent_official_values:
                source_gaps.append(label)
    all_missing = sorted(set(missing + [f"{label}來源" for label in source_gaps]))
    quality_missing = list(quality.get("missing") or [])
    try:
        expected_latest_date = backend.latest_complete_price_date()
    except Exception:
        expected_latest_date = ""
    rule_quality = backend.rule_analysis_data_quality(
        symbol,
        rows,
        min_price_rows=120,
        expected_latest_date=expected_latest_date,
    )
    repairable_quality_missing = list(dict.fromkeys(
        list(rule_quality.get("missing") or [])
        + [key for key in quality_missing if key not in structural_quality]
    ))
    not_applicable = [
        {"field": key, "reason": reason}
        for key, reason in not_applicable_fields.items()
    ] + [
        {"qualityCheck": key, "reason": reason}
        for key, reason in structural_quality.items()
    ]
    complete = bool(rows) and not all_missing and not repairable_quality_missing
    return {
        "symbol": symbol,
        "date": latest.get("date"),
        "updatedRows": int(updated_rows or 0),
        "complete": complete,
        "needsRepair": not complete,
        "missing": all_missing,
        "qualityMissing": quality_missing,
        "repairableQualityMissing": repairable_quality_missing,
        "notApplicable": not_applicable,
        "modelEligible": bool(quality.get("ok")),
        "ruleDataQuality": rule_quality,
        "priceSource": latest.get("price_source"),
        "chipSource": sources.get("chip_source") or latest.get("chip_source"),
        "marginSource": sources.get("margin_source") or latest.get("margin_source"),
        "revenueSource": sources.get("revenue_source") or latest.get("revenue_source"),
        "valuationSource": sources.get("valuation_source") or latest.get("valuation_source"),
        "financialStatementSource": sources.get("financial_statement_source") or latest.get("financial_statement_source"),
        "quality": {
            "ok": bool(quality.get("ok")),
            "rows": quality.get("rows", 0),
            "recentRows": quality.get("recentRows", 0),
            "chipSourceCoverage": quality.get("chipSourceCoverage", 0),
            "marginSourceCoverage": quality.get("marginSourceCoverage", 0),
            "financeSourceCoverage": quality.get("financeSourceCoverage", 0),
        },
    }


def update_data_gap_repair_status(persist=False, **updates):
    data_gap_repair_status.update(updates)
    snapshot = dict(data_gap_repair_status)
    if persist:
        persist_runtime_status(DATA_GAP_REPAIR_STATUS_META_KEY, snapshot)
    return snapshot


def run_data_gap_repair(max_symbols=120, trigger="manual", force_refresh=True, include_extended=False):
    if not data_gap_repair_lock.acquire(blocking=False):
        current = dict(data_gap_repair_status)
        return {
            **current,
            "ok": False,
            "running": True,
            "busy": True,
            "retry": True,
            "trigger": trigger,
            "message": "資料缺口修復已在執行中",
        }

    daily_lock_acquired = False
    started_at = time.strftime("%Y-%m-%d %H:%M:%S")
    details = []
    errors = []
    checked = attempted = repaired = still_missing = deferred = failed = 0
    not_applicable = 0
    official_snapshot = {}
    try:
        daily_lock_acquired = daily_update_lock.acquire(blocking=False)
        if not daily_lock_acquired:
            return update_data_gap_repair_status(
                persist=True,
                ok=False,
                running=False,
                busy=True,
                retry=True,
                trigger=trigger,
                startedAt=started_at,
                finishedAt=time.strftime("%Y-%m-%d %H:%M:%S"),
                checked=0,
                attempted=0,
                repaired=0,
                stillMissing=0,
                deferred=0,
                notApplicable=0,
                failed=0,
                details=[],
                errors=[],
                message="每日更新正在寫入資料庫，缺口修復保留待辦並於下一輪重試",
            )

        # 先用 TWSE/TPEx 全市場快照一次補齊最新 OHLCV/估值，再做逐檔
        # FinMind 歷史籌碼、融資券與財務補抓。即使官方端暫時失敗，仍繼續
        # 逐檔來源，不能因第一來源失敗就整批跳過。
        try:
            official_snapshot = backend.sync_official_daily_snapshot()
        except Exception as exc:
            official_snapshot = {"ok": False, "error": str(exc)}
            errors.append(f"官方全市場快照：{exc}")
        universe = data_gap_repair_symbol_universe(max_symbols=max_symbols)
        errors.extend(list(universe.get("errors") or []))
        symbols = universe["symbols"]
        try:
            stock_info = backend.load_stock_info()
        except Exception as exc:
            stock_info = {}
            errors.append(f"股票基本資料：{exc}")
        repair_day = scheduler_today(taipei_localtime())
        use_same_day_backoff = str(trigger or "").startswith("auto")
        with backend.connect() as conn:
            backoff_row = conn.execute(
                "SELECT value FROM model_meta WHERE key = 'data_gap_repair_backoff_json'"
            ).fetchone()
        try:
            repair_backoff = json.loads(backoff_row[0]) if backoff_row and backoff_row[0] else {}
        except (json.JSONDecodeError, TypeError):
            repair_backoff = {}
        if not isinstance(repair_backoff, dict):
            repair_backoff = {}
        for retired_symbol in list(repair_backoff):
            if retired_symbol in RETIRED_SYMBOLS:
                repair_backoff.pop(retired_symbol, None)
        update_data_gap_repair_status(
            persist=True,
            ok=True,
            running=True,
            busy=False,
            retry=False,
            trigger=trigger,
            startedAt=started_at,
            finishedAt="",
            checked=0,
            attempted=0,
            repaired=0,
            stillMissing=0,
            deferred=0,
            notApplicable=0,
            failed=0,
            message=f"資料缺口修復中：0/{len(symbols)}",
            details=[],
            errors=errors[-30:],
            sources=universe.get("sources") or {},
            officialSnapshot=official_snapshot,
        )

        def repairable_quality(detail):
            return list(
                detail.get("repairableQualityMissing")
                if "repairableQualityMissing" in detail
                else (detail.get("qualityMissing") or [])
            )

        def gap_signature(detail):
            return "|".join(sorted(
                [*(detail.get("missing") or []), *repairable_quality(detail)]
            ))

        def cooldown_remaining(backoff):
            try:
                attempted_at = datetime.strptime(
                    str(backoff.get("attemptedAt") or ""), "%Y-%m-%d %H:%M:%S"
                )
                elapsed = (datetime.now() - attempted_at).total_seconds()
                return max(0, int(DATA_GAP_REPAIR_COOLDOWN_SECONDS - elapsed))
            except (TypeError, ValueError):
                return 0

        for index, symbol in enumerate(symbols, start=1):
            checked += 1
            before = data_gap_symbol_detail(symbol, stock_info=stock_info)
            not_applicable += len(before.get("notApplicable") or [])
            if not before["needsRepair"]:
                repair_backoff.pop(symbol, None)
                if len(details) < 80:
                    details.append({**before, "status": "already_complete"})
                update_data_gap_repair_status(
                    checked=checked,
                    attempted=attempted,
                    repaired=repaired,
                    stillMissing=still_missing,
                    deferred=deferred,
                    notApplicable=not_applicable,
                    failed=failed,
                    message=f"資料缺口修復中：{checked}/{len(symbols)}",
                    details=details[-80:],
                )
                continue

            signature = gap_signature(before)
            backoff = repair_backoff.get(symbol) or {}
            same_gap_today = bool(
                str(backoff.get("date") or "") == repair_day
                and str(backoff.get("gapSignature") or "") == signature
            )
            attempts_today = int(backoff.get("attemptCount") or 0) if same_gap_today else 0
            remaining = cooldown_remaining(backoff) if same_gap_today else 0
            if use_same_day_backoff and same_gap_today and (
                attempts_today >= DATA_GAP_REPAIR_MAX_ATTEMPTS_PER_DAY or remaining > 0
            ):
                still_missing += 1
                deferred += 1
                maxed = attempts_today >= DATA_GAP_REPAIR_MAX_ATTEMPTS_PER_DAY
                details.append({
                    **before,
                    "status": "verified_unavailable_today" if maxed else "retry_cooldown",
                    "lastAttemptAt": backoff.get("attemptedAt"),
                    "attemptCount": attempts_today,
                    "retryAfterSeconds": 0 if maxed else remaining,
                    "reason": (
                        f"今天已向可用來源補抓 {attempts_today} 次仍無資料，保留缺口供下個交易日再驗證"
                        if maxed else f"剛完成第 {attempts_today} 次補抓，冷卻後由下一時段自動重試"
                    ),
                })
                update_data_gap_repair_status(
                    checked=checked,
                    attempted=attempted,
                    repaired=repaired,
                    stillMissing=still_missing,
                    deferred=deferred,
                    notApplicable=not_applicable,
                    failed=failed,
                    message=f"資料缺口修復中：{checked}/{len(symbols)}",
                    details=details[-80:],
                )
                continue

            attempted += 1
            try:
                counts = backend.update_prices(
                    [symbol],
                    refresh_info=False,
                    include_extended=bool(include_extended),
                    force_refresh=bool(force_refresh),
                )
                after = data_gap_symbol_detail(
                    symbol,
                    updated_rows=counts.get(symbol, 0),
                    stock_info=stock_info,
                )
                before_gap_count = len(before.get("missing") or []) + len(repairable_quality(before))
                after_gap_count = len(after.get("missing") or []) + len(repairable_quality(after))
                improved = after["complete"] or after_gap_count < before_gap_count
                if improved:
                    repaired += 1
                if after["complete"]:
                    repair_backoff.pop(symbol, None)
                else:
                    still_missing += 1
                    after_signature = gap_signature(after)
                    prior_attempts = (
                        attempts_today
                        if same_gap_today and after_signature == signature
                        else 0
                    )
                    repair_backoff[symbol] = {
                        "date": repair_day,
                        "attemptedAt": now_text(),
                        "attemptCount": prior_attempts + 1,
                        "gapSignature": after_signature,
                    }
                details.append({
                    **after,
                    "status": "complete" if after["complete"] else ("partial" if improved else "still_missing"),
                    "attemptCount": (repair_backoff.get(symbol) or {}).get("attemptCount", 1),
                    "beforeMissing": before.get("missing") or [],
                    "beforeQualityMissing": repairable_quality(before),
                })
            except Exception as exc:
                failed += 1
                still_missing += 1
                errors.append(f"{symbol}: {exc}")
                repair_backoff[symbol] = {
                    "date": repair_day,
                    "attemptedAt": now_text(),
                    "attemptCount": attempts_today + 1,
                    "gapSignature": signature,
                }
                details.append({
                    "symbol": symbol,
                    "status": "failed",
                    "error": str(exc),
                    "attemptCount": attempts_today + 1,
                    "beforeMissing": before.get("missing") or [],
                    "beforeQualityMissing": repairable_quality(before),
                })
            update_data_gap_repair_status(
                checked=checked,
                attempted=attempted,
                repaired=repaired,
                stillMissing=still_missing,
                deferred=deferred,
                notApplicable=not_applicable,
                failed=failed,
                message=f"資料缺口修復中：{checked}/{len(symbols)}",
                details=details[-80:],
                errors=errors[-30:],
            )

        cache.clear()
        finished_at = time.strftime("%Y-%m-%d %H:%M:%S")
        message = (
            f"資料缺口修復完成：檢查 {checked} 檔，實際補抓 {attempted} 檔，"
            f"改善 {repaired} 檔，仍缺 {still_missing} 檔，待下輪 {deferred} 檔，"
            f"不適用欄位 {not_applicable} 個，錯誤 {failed} 檔"
        )
        pending_symbols = unique_symbols(repair_backoff.keys())
        with backend.connect() as conn:
            backend.set_meta(conn, "last_data_gap_repair_at", finished_at)
            backend.set_meta(conn, "last_data_gap_repair_trigger", trigger)
            backend.set_meta(conn, "last_data_gap_repair_checked", str(checked))
            backend.set_meta(conn, "last_data_gap_repair_attempted", str(attempted))
            backend.set_meta(conn, "last_data_gap_repair_repaired", str(repaired))
            backend.set_meta(conn, "last_data_gap_repair_still_missing", str(still_missing))
            backend.set_meta(conn, "last_data_gap_repair_deferred", str(deferred))
            backend.set_meta(conn, "last_data_gap_repair_not_applicable", str(not_applicable))
            backend.set_meta(conn, "last_data_gap_repair_failed", str(failed))
            backend.set_meta(conn, "last_data_gap_repair_message", message)
            backend.set_meta(
                conn,
                "data_gap_repair_pending_symbols_json",
                json.dumps(pending_symbols, ensure_ascii=False, separators=(",", ":")),
            )
            backend.set_meta(
                conn,
                "data_gap_repair_backoff_json",
                json.dumps(repair_backoff, ensure_ascii=False, separators=(",", ":")),
            )
        return update_data_gap_repair_status(
            persist=True,
            ok=failed == 0 and still_missing == 0,
            running=False,
            busy=False,
            retry=bool(failed or still_missing),
            partial=bool(still_missing and not failed),
            trigger=trigger,
            startedAt=started_at,
            finishedAt=finished_at,
            checked=checked,
            attempted=attempted,
            repaired=repaired,
            stillMissing=still_missing,
            deferred=deferred,
            notApplicable=not_applicable,
            failed=failed,
            message=message,
            details=details[-80:],
            errors=errors[-30:],
            sources=universe.get("sources") or {},
            officialSnapshot=official_snapshot,
            pendingSymbols=pending_symbols,
        )
    finally:
        if daily_lock_acquired:
            daily_update_lock.release()
        data_gap_repair_lock.release()


def auto_data_gap_repair(trigger="auto"):
    result = run_data_gap_repair(max_symbols=120, trigger=trigger, force_refresh=True, include_extended=False)
    if result.get("busy") or (result.get("running") and result.get("retry")):
        return AUTO_SCHEDULE_RETRY
    message = result.get("message") or "資料缺口修復完成"
    if result.get("failed"):
        raise RuntimeError(message)
    if result.get("stillMissing"):
        return {
            "scheduleStatus": "partial",
            "message": message + "；缺口已保留，下一個修復時段會再抓，不視為完整成功",
        }
    if result.get("ok") is False:
        raise RuntimeError(message)
    return message


def official_snapshot_latest_date(result):
    latest_dates = result.get("latestDates") if isinstance(result, dict) else {}
    normalized = {}
    for date_value, count in (latest_dates or {}).items():
        date_key = str(date_value or "")[:10]
        if not date_key:
            continue
        normalized[date_key] = normalized.get(date_key, 0) + safe_int(count, 0)
    latest_date = max(normalized) if normalized else ""
    return latest_date, normalized.get(latest_date, 0)


def official_annual_market_day_status(target, force_refresh=False):
    """Read and cache the TWSE annual schedule before emergency overrides."""
    cache_key = f"twse_market_calendar_{target.year}"
    calendar = None
    fetched_at = ""
    fetch_error = ""
    with twse_market_calendar_lock:
        cached = twse_market_calendar_cache.get(target.year)
        if isinstance(cached, dict):
            calendar = cached.get("calendar")
            fetched_at = str(cached.get("fetchedAt") or "")
        if calendar is None:
            try:
                with backend.connect() as conn:
                    row = conn.execute("SELECT value FROM model_meta WHERE key = ?", (cache_key,)).fetchone()
                stored = json.loads(row[0]) if row and row[0] else {}
                if isinstance(stored, dict) and isinstance(stored.get("calendar"), dict):
                    calendar = stored["calendar"]
                    fetched_at = str(stored.get("fetchedAt") or "")
            except (json.JSONDecodeError, TypeError, sqlite3.Error):
                calendar = None

        fetched_today = fetched_at[:10] == scheduler_today(taipei_localtime())
        if force_refresh or calendar is None or not fetched_today:
            try:
                calendar = fetch_twse_calendar(target.year)
                fetched_at = now_text()
                stored = {"fetchedAt": fetched_at, "calendar": calendar}
                with backend.connect() as conn:
                    backend.set_meta(conn, cache_key, json.dumps(stored, ensure_ascii=False, separators=(",", ":")))
            except Exception as exc:
                fetch_error = str(exc)[:240]
        if calendar is not None:
            twse_market_calendar_cache[target.year] = {
                "calendar": calendar,
                "fetchedAt": fetched_at,
            }

    fetched_today = fetched_at[:10] == scheduler_today(taipei_localtime())
    status = planned_market_day(target, calendar)
    status.update({
        "calendarFetchedAt": fetched_at or None,
        "calendarStale": bool(calendar is not None and not fetched_today),
        "calendarError": fetch_error,
    })
    return status


def official_market_day_status(target_date, force_refresh=False):
    """Return official planned status, then apply same-day emergency closure."""
    date_text = str(target_date or "")[:10]
    try:
        target = datetime.strptime(date_text, "%Y-%m-%d").date()
    except ValueError:
        return {"known": False, "isTradingDay": None, "date": date_text, "reason": "日期格式錯誤"}

    emergency_error = ""
    try:
        local_override = load_market_session_overrides(MARKET_SESSION_OVERRIDES_PATH).get(date_text)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        local_override = None
        emergency_error = str(exc)[:240]
    if local_override:
        return {
            **local_override,
            "calendarFetchedAt": None,
            "calendarStale": False,
            "calendarError": "",
            "emergencyCheckedAt": None,
            "emergencyError": emergency_error,
        }

    annual_status = official_annual_market_day_status(target, force_refresh=force_refresh)
    if annual_status.get("known") is True and annual_status.get("isTradingDay") is False:
        return {
            **annual_status,
            "emergencyClosure": False,
            "emergencyCheckedAt": None,
            "emergencyError": emergency_error,
        }

    dgpa_key = f"dgpa_taipei_closure_{date_text}"
    dgpa_closure = None
    dgpa_fetched_at = ""
    try:
        with backend.connect() as conn:
            row = conn.execute("SELECT value FROM model_meta WHERE key = ?", (dgpa_key,)).fetchone()
        stored = json.loads(row[0]) if row and row[0] else {}
        if isinstance(stored, dict) and isinstance(stored.get("closure"), dict):
            dgpa_closure = stored["closure"]
            dgpa_fetched_at = str(stored.get("fetchedAt") or "")
    except (json.JSONDecodeError, TypeError, sqlite3.Error) as exc:
        emergency_error = str(exc)[:240]

    today = scheduler_today(taipei_localtime())
    dgpa_fetched_today = dgpa_fetched_at[:10] == today
    if date_text == today and (force_refresh or not dgpa_fetched_today):
        try:
            fetched_closure = fetch_dgpa_taipei_closure()
            fetched_date = str(fetched_closure.get("date") or "")[:10]
            fetched_at = now_text()
            if fetched_date:
                with backend.connect() as conn:
                    backend.set_meta(
                        conn,
                        f"dgpa_taipei_closure_{fetched_date}",
                        json.dumps(
                            {"fetchedAt": fetched_at, "closure": fetched_closure},
                            ensure_ascii=False,
                            separators=(",", ":"),
                        ),
                    )
            if fetched_date == date_text:
                dgpa_closure = fetched_closure
                dgpa_fetched_at = fetched_at
        except Exception as exc:
            emergency_error = str(exc)[:240]

    if (
        isinstance(dgpa_closure, dict)
        and str(dgpa_closure.get("date") or "")[:10] == date_text
        and dgpa_closure.get("marketClosed") is True
    ):
        return {
            "known": True,
            "isTradingDay": False,
            "date": date_text,
            "reason": str(dgpa_closure.get("reason") or "臺北市天然災害停止上班"),
            "source": str(
                dgpa_closure.get("source")
                or "DGPA Taipei closure / TWSE natural-disaster rule"
            ),
            "evidenceUrl": str(dgpa_closure.get("evidenceUrl") or ""),
            "emergencyClosure": True,
            "calendarFetchedAt": annual_status.get("calendarFetchedAt"),
            "calendarStale": annual_status.get("calendarStale", False),
            "calendarError": annual_status.get("calendarError") or "",
            "emergencyCheckedAt": dgpa_fetched_at or None,
            "emergencyError": emergency_error,
        }

    return {
        **annual_status,
        "emergencyClosure": False,
        "emergencyCheckedAt": dgpa_fetched_at or None,
        "emergencyError": emergency_error,
    }


def record_official_close_sync_status(status, target_date, result=None, error="", market_day=None):
    result = result or {}
    market_day = market_day if isinstance(market_day, dict) else {}
    latest_date, latest_count = official_snapshot_latest_date(result)
    with backend.connect() as conn:
        backend.set_meta(conn, "last_official_close_sync_attempt_at", time.strftime("%Y-%m-%d %H:%M:%S", taipei_localtime()))
        backend.set_meta(conn, "last_official_close_sync_status", status)
        backend.set_meta(conn, "last_official_close_sync_target_date", target_date)
        backend.set_meta(conn, "last_official_close_sync_latest_date", latest_date)
        backend.set_meta(conn, "last_official_close_sync_latest_count", str(latest_count))
        backend.set_meta(conn, "last_official_close_sync_available", str(safe_int(result.get("available"), 0)))
        backend.set_meta(conn, "last_official_close_sync_written", str(safe_int(result.get("written"), 0)))
        backend.set_meta(conn, "last_official_close_sync_calendar_known", "1" if market_day.get("known") is True else "0")
        is_trading_day = market_day.get("isTradingDay")
        backend.set_meta(
            conn,
            "last_official_close_sync_calendar_is_trading_day",
            "1" if is_trading_day is True else "0" if is_trading_day is False else "",
        )
        backend.set_meta(conn, "last_official_close_sync_calendar_reason", str(market_day.get("reason") or ""))
        backend.set_meta(conn, "last_official_close_sync_calendar_source", str(market_day.get("source") or ""))
        backend.set_meta(
            conn,
            "last_official_close_sync_error",
            str(error or " | ".join(result.get("errors") or []))[:1000],
        )
    return latest_date, latest_count


def has_today_intraday_market_activity(target_date):
    try:
        with backend.connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM realtime_flow_staging WHERE date = ? LIMIT 1",
                (target_date,),
            ).fetchone()
        return bool(row)
    except Exception:
        return False


def auto_official_close_sync(now=None, force=False):
    """Converge the all-market official daily snapshot after the close.

    The morning daily job intentionally succeeds with the previous completed
    trading day, so it cannot be reused as proof that today's close is stored.
    This light-weight job polls only TWSE/TPEx snapshot endpoints and refreshes
    the radar after today's sufficiently complete snapshot becomes available.
    """
    global official_close_sync_last_attempt
    now = now or taipei_localtime()
    target_date = scheduler_today(now)
    minute_of_day = now.tm_hour * 60 + now.tm_min
    finalizing = minute_of_day >= OFFICIAL_CLOSE_SYNC_FINALIZE_MINUTE
    current_time = time.time()
    if (
        not force
        and not finalizing
        and current_time - official_close_sync_last_attempt < OFFICIAL_CLOSE_SYNC_RETRY_SECONDS
    ):
        return AUTO_SCHEDULE_RETRY
    if not daily_update_lock.acquire(blocking=False):
        return AUTO_SCHEDULE_RETRY
    market_day = {}
    try:
        official_close_sync_last_attempt = current_time
        result = backend.sync_official_daily_snapshot()
        latest_date, latest_count = official_snapshot_latest_date(result)
        market_day = official_market_day_status(target_date)
        ready = latest_date == target_date and latest_count >= OFFICIAL_CLOSE_SYNC_MIN_ROWS
        status = "ready" if ready else "waiting"
        record_official_close_sync_status(status, target_date, result=result, market_day=market_day)
    except Exception as exc:
        try:
            record_official_close_sync_status(
                "failed", target_date, error=str(exc), market_day=market_day
            )
        except Exception:
            pass
        raise
    finally:
        daily_update_lock.release()

    if ready:
        cache.clear()
        try:
            exit_settlement = backend.settle_portfolio_exit_history(as_of_date=target_date)
        except Exception as exc:
            exit_settlement = {"settled": 0, "error": str(exc)[:200]}
        scan_result = start_monster_scan_job(
            limit=300,
            score_limit=100,
            trigger="official-close-sync",
        )
        with backend.connect() as conn:
            backend.set_meta(conn, "last_official_close_sync_ready_at", time.strftime("%Y-%m-%d %H:%M:%S", now))
            backend.set_meta(conn, "last_official_close_sync_radar_refresh_started", "1" if scan_result.get("started") else "0")
            backend.set_meta(conn, "last_portfolio_exit_settlement_at", now_text())
            backend.set_meta(conn, "last_portfolio_exit_settlement_count", str(exit_settlement.get("settled", 0)))
            backend.set_meta(conn, "last_portfolio_exit_settlement_error", str(exit_settlement.get("error") or ""))
        return (
            f"官方收盤日K已更新 {latest_date} 共 {latest_count} 列；"
            + ("妖股雷達已啟動重掃" if scan_result.get("started") else "雷達掃描已在執行，沿用最新資料")
            + f"；出場驗證新結算 {exit_settlement.get('settled', 0)} 個視窗"
        )

    if market_day.get("known") is True and market_day.get("isTradingDay") is False:
        record_official_close_sync_status(
            "scheduled_holiday", target_date, result=result, market_day=market_day
        )
        return (
            f"官方交易日資料確認 {target_date} 為休市日（{market_day.get('reason') or '官方休市'}）；"
            f"保留前一交易日 {latest_date or '無日期'} 資料"
        )

    if finalizing:
        if latest_date == target_date:
            record_official_close_sync_status(
                "failed",
                target_date,
                result=result,
                error=f"官方今日收盤快照只有 {latest_count} 列，低於 {OFFICIAL_CLOSE_SYNC_MIN_ROWS} 列",
                market_day=market_day,
            )
            raise RuntimeError(
                f"官方今日收盤日K只有 {latest_count} 列，低於完整門檻 {OFFICIAL_CLOSE_SYNC_MIN_ROWS} 列"
            )
        if market_day.get("known") is not True:
            error = f"無法確認 {target_date} 證交所開休市狀態，禁止把缺少收盤資料視為休市"
        else:
            error = f"證交所行事曆顯示今日應開市，但官方收盤日K仍停在 {latest_date or '無日期'}"
        record_official_close_sync_status(
            "failed", target_date, result=result, error=error, market_day=market_day
        )
        raise RuntimeError(error)
    return AUTO_SCHEDULE_RETRY


def reconcile_official_close_sync_calendar_state(now=None):
    """Repair unresolved close-sync state using current official closure evidence."""
    now = now or taipei_localtime()
    today = scheduler_today(now)
    keys = (
        "last_official_close_sync_status",
        "last_official_close_sync_target_date",
        "last_official_close_sync_latest_date",
        "last_official_close_sync_latest_count",
        f"auto_schedule_{OFFICIAL_CLOSE_SYNC_JOB_ID}_date",
        f"auto_schedule_{OFFICIAL_CLOSE_SYNC_JOB_ID}_status",
    )
    with backend.connect() as conn:
        placeholders = ",".join("?" for _ in keys)
        meta = dict(conn.execute(
            f"SELECT key, value FROM model_meta WHERE key IN ({placeholders})",
            keys,
        ).fetchall())
    status = str(meta.get("last_official_close_sync_status") or "")
    target_date = str(meta.get("last_official_close_sync_target_date") or "")[:10]
    latest_date = str(meta.get("last_official_close_sync_latest_date") or "")[:10]
    latest_count = safe_int(meta.get("last_official_close_sync_latest_count"), 0)
    schedule_date = str(
        meta.get(f"auto_schedule_{OFFICIAL_CLOSE_SYNC_JOB_ID}_date") or ""
    )[:10]
    schedule_status = str(
        meta.get(f"auto_schedule_{OFFICIAL_CLOSE_SYNC_JOB_ID}_status") or ""
    )
    try:
        parsed_target = datetime.strptime(target_date, "%Y-%m-%d").date()
        parsed_today = datetime.strptime(today, "%Y-%m-%d").date()
    except ValueError:
        return {"ok": True, "recovered": False, "reason": "invalid_target_date"}
    actual_close_ready = (
        status in {"ready", "success", "completed"}
        and latest_date == target_date
        and latest_count >= OFFICIAL_CLOSE_SYNC_MIN_ROWS
    )
    if actual_close_ready:
        already_consistent = (
            schedule_date == target_date
            and schedule_status in {"success", "success_recovered"}
        )
        if already_consistent:
            return {"ok": True, "recovered": False, "reason": "actual_success_already_consistent"}
        message = (
            f"官方收盤同步狀態依實際資料修復：{target_date} 已有 "
            f"{latest_count} 檔正式日 K"
        )
        with backend.connect() as conn:
            backend.set_meta(conn, f"auto_schedule_{OFFICIAL_CLOSE_SYNC_JOB_ID}_date", target_date)
            backend.set_meta(conn, f"auto_schedule_{OFFICIAL_CLOSE_SYNC_JOB_ID}_status", "success_recovered")
            backend.set_meta(conn, f"auto_schedule_{OFFICIAL_CLOSE_SYNC_JOB_ID}_message", message)
            backend.set_meta(conn, f"auto_schedule_{OFFICIAL_CLOSE_SYNC_JOB_ID}_at", now_text())
            backend.set_meta(conn, f"auto_schedule_{OFFICIAL_CLOSE_SYNC_JOB_ID}_attempt_count", "0")
        return {
            "ok": True,
            "recovered": True,
            "status": "ready",
            "message": message,
        }
    if parsed_target > parsed_today or status not in {"previous_trading_day", "waiting", "failed"}:
        return {"ok": True, "recovered": False, "reason": "no_legacy_state"}
    if target_date == today and now.tm_hour * 60 + now.tm_min < OFFICIAL_CLOSE_SYNC_FINALIZE_MINUTE:
        return {"ok": True, "recovered": False, "reason": "before_finalize"}

    result = {
        "latestDates": {latest_date: latest_count} if latest_date else {},
        "available": latest_count,
        "written": 0,
        "errors": [],
    }
    market_day = official_market_day_status(target_date)
    if market_day.get("known") is True and market_day.get("isTradingDay") is False:
        repaired_status = "scheduled_holiday"
        message = f"官方交易日資料確認 {target_date} 為休市日（{market_day.get('reason') or '官方休市'}）"
        error = ""
    elif status == "failed":
        return {"ok": False, "recovered": False, "reason": "failed_state_still_open_or_unknown"}
    else:
        repaired_status = "failed"
        message = (
            f"證交所行事曆顯示 {target_date} 應開市，但官方收盤日K仍停在 {latest_date or '無日期'}"
            if market_day.get("known") is True
            else f"無法確認 {target_date} 證交所開休市狀態，禁止沿用可能休市成功狀態"
        )
        error = message
    record_official_close_sync_status(
        repaired_status,
        target_date,
        result=result,
        error=error,
        market_day=market_day,
    )
    with backend.connect() as conn:
        backend.set_meta(conn, f"auto_schedule_{OFFICIAL_CLOSE_SYNC_JOB_ID}_date", target_date)
        backend.set_meta(
            conn,
            f"auto_schedule_{OFFICIAL_CLOSE_SYNC_JOB_ID}_status",
            "success_recovered" if repaired_status == "scheduled_holiday" else "failed_recovered",
        )
        backend.set_meta(conn, f"auto_schedule_{OFFICIAL_CLOSE_SYNC_JOB_ID}_message", message)
        backend.set_meta(conn, f"auto_schedule_{OFFICIAL_CLOSE_SYNC_JOB_ID}_at", now_text())
    return {
        "ok": repaired_status != "failed",
        "recovered": True,
        "status": repaired_status,
        "message": message,
        "marketDay": market_day,
    }


# 15:00 排程啟動的掃描最多監控這麼久才放棄等待。掃描本身是 fire-and-forget
# 背景執行緒，run_auto_schedule_job 一收到 auto_monster_scan() 的回傳值就會
# 標記今天已完成——如果不在這裡等待，掃描真正卡死或中途失敗會完全沒人知道，
# 排程狀態卻已顯示成功。
MONSTER_SCAN_WATCHDOG_SECONDS = 600


def auto_monster_scan():
    # 這個 job 會被 20 秒排程迴圈在 15:00-16:00 視窗內反覆呼叫，直到回傳
    # 成功/失敗被標記為止。三個入口狀態：
    # (1) 有掃描在跑(手動或上一輪逾時的自動掃描)：回 RETRY，不標記
    #     ——舊版遇到手動掃描會回「略過重複啟動」被標記成功，全市場自動
    #     掃描整天被跳過。
    # (2) 今天的自動掃描已經跑完(上一輪 watchdog 逾時後由背景完成)：依真實
    #     結果回報成功/失敗，不重跑。
    # (3) 沒掃描在跑也沒完成過：直接啟動純規則掃描→watchdog 等待；
    #     逾時回 RETRY 讓下一輪迴圈用 (1)/(2) 接手。模型另由 15:10 job 運行。
    with monster_scan_lock:
        status_snapshot = dict(monster_scan_status)
    today = scheduler_today()
    if status_snapshot.get("running"):
        return AUTO_SCHEDULE_RETRY
    finished_today_by_auto = (
        str(status_snapshot.get("finishedAt") or "").startswith(today)
        and status_snapshot.get("trigger") == "auto-15:00"
    )
    if finished_today_by_auto:
        if status_snapshot.get("phase") == "失敗":
            raise RuntimeError(f"妖股掃描背景執行失敗：{status_snapshot.get('message', '')}")
        readiness = backend.refresh_radar_deployment_readiness(as_of_date=today)
        return (
            f"妖股掃描完成：{status_snapshot.get('message', '')}；"
            f"正式戰績門檻 {'通過' if readiness.get('formalReady') else '觀察中'}"
        )

    result = start_monster_scan_job(limit=300, score_limit=100, trigger="auto-15:00")
    if not result.get("started"):
        return AUTO_SCHEDULE_RETRY

    deadline = time.time() + MONSTER_SCAN_WATCHDOG_SECONDS
    final_status = None
    while time.time() < deadline:
        time.sleep(5)
        with monster_scan_lock:
            if not monster_scan_status.get("running"):
                final_status = dict(monster_scan_status)
                break
    if final_status is None:
        # 逾時仍在跑：回 RETRY 讓下一輪迴圈接手(入口狀態 (1)→掃描結束後
        # 走 (2) 依真實結果標記)。舊版在這裡直接回成功訊息，掃描之後卡死
        # 或失敗都無人知曉。
        return AUTO_SCHEDULE_RETRY
    if final_status.get("phase") == "失敗":
        raise RuntimeError(f"妖股掃描背景執行失敗：{final_status.get('message', '')}")
    readiness = backend.refresh_radar_deployment_readiness(as_of_date=today)
    return (
        f"15:00 純規則妖股掃描完成：{final_status.get('message', '')}；"
        f"正式戰績門檻 {'通過' if readiness.get('formalReady') else '觀察中'}"
    )


def auto_premarket_monster_scan():
    """Refresh the rule-only candidate snapshot before entry checkpoints.

    The job is non-blocking so the scheduler can keep running.  Closed-day
    audit scans remain stored, while this trading-day scan becomes the active
    decision snapshot once its full write completes.
    """
    today = scheduler_today()
    with monster_scan_lock:
        status_snapshot = dict(monster_scan_status)
    if status_snapshot.get("running"):
        return AUTO_SCHEDULE_RETRY
    finished_today = str(status_snapshot.get("finishedAt") or "").startswith(today)
    if finished_today and status_snapshot.get("trigger") == "auto-08:50":
        if status_snapshot.get("phase") == "失敗":
            raise RuntimeError(f"盤前妖股掃描失敗：{status_snapshot.get('message', '')}")
        validity = backend.current_radar_decision_validity(current_date=today)
        if (
            validity.get("validForTrading") is True
            and validity.get("selectedScanDate") == today
        ):
            readiness = backend.refresh_radar_deployment_readiness(as_of_date=today)
            return (
                f"盤前妖股掃描完成：{status_snapshot.get('message', '')}；"
                f"正式戰績門檻 {'通過' if readiness.get('formalReady') else '觀察中'}"
            )
        raise RuntimeError(
            f"盤前掃描完成但決策快照仍無效：{validity.get('summary') or '未知原因'}"
        )
    result = start_monster_scan_job(
        limit=300,
        score_limit=100,
        trigger="auto-08:50",
    )
    return AUTO_SCHEDULE_RETRY


def auto_batch_save_predictions():
    """
    每日 15:10 後自動為液態宇宙所有股票存一筆 ML 預測。
    predict_symbol 內部已有 (symbol, price_date, model_version) UNIQUE 去重，
    重複呼叫不會產生重複列。
    """
    result = backend.batch_save_predictions(limit=600)
    msg = (
        f"批量 ML 預測：檢查 {result['symbols_total']} 檔"
        f"，資格通過 {result.get('eligible_total', 0)} 檔"
        f"/{result.get('requested_total', 600)} 檔目標"
        f"，已存 {result['saved']} 筆"
        f"，既有 {result.get('skipped_existing_count', 0)} 筆"
        f"，資料不足跳過 {result.get('skipped_ineligible_count', 0)} 檔"
        f"，模型紙上買訊 {result.get('paper_signal_count', 0)} 筆"
        f"，錯誤 {result['error_count']} 筆"
    )
    with backend.connect() as conn:
        backend.set_meta(conn, "last_batch_predictions_saved", str(result["saved"]))
        backend.set_meta(conn, "last_batch_predictions_total", str(result["symbols_total"]))
        backend.set_meta(conn, "last_batch_predictions_requested", str(result.get("requested_total", 0)))
        backend.set_meta(conn, "last_batch_predictions_candidate_pool", str(result.get("candidate_pool_total", 0)))
        backend.set_meta(conn, "last_batch_predictions_shortfall", str(result.get("eligible_shortfall", 0)))
        backend.set_meta(conn, "last_batch_predictions_eligible", str(result.get("eligible_total", 0)))
        backend.set_meta(conn, "last_batch_predictions_ineligible", str(result.get("skipped_ineligible_count", 0)))
        backend.set_meta(conn, "last_batch_predictions_existing", str(result.get("skipped_existing_count", 0)))
        backend.set_meta(conn, "last_batch_predictions_errors", str(result["error_count"]))
        backend.set_meta(conn, "last_model_paper_signals_saved", str(result.get("paper_signal_count", 0)))
        backend.set_meta(conn, "last_batch_predictions_completed_at", now_text())
        backend.set_meta(
            conn,
            "last_batch_predictions_ineligible_json",
            json.dumps(result.get("ineligible") or [], ensure_ascii=False, default=str),
        )
        backend.set_meta(
            conn,
            "last_batch_predictions_repair_symbols_json",
            json.dumps(result.get("repair_symbols") or [], ensure_ascii=False),
        )
        backend.set_meta(
            conn,
            "last_batch_predictions_errors_json",
            json.dumps(result.get("errors") or [], ensure_ascii=False, default=str),
        )
        backend.set_meta(
            conn,
            "last_batch_predictions_paper_signal_errors_json",
            json.dumps(result.get("paper_signal_errors") or [], ensure_ascii=False, default=str),
        )
    return msg


def auto_model_cycle():
    """獨立模型日循環：重訓與批量預測不再是妖股掃描的前置條件。"""
    today = scheduler_today()
    try:
        active_model = backend.load_model() or {}
    except Exception:
        active_model = {}
    active_trained_at = str(active_model.get("trained_at") or "")
    active_data_max_date = str(active_model.get("training_data_max_date") or "")[:10]
    try:
        latest_complete_date = str(backend.latest_complete_price_date() or "")[:10]
    except Exception:
        latest_complete_date = ""
    training_data_lagged = bool(
        latest_complete_date
        and (not active_data_max_date or active_data_max_date < latest_complete_date)
    )
    cycle_note = f"沿用生效模型 {active_trained_at or '無'}"
    if not active_trained_at.startswith(today) or training_data_lagged:
        symbols, sources, source_errors = auto_model_training_symbols()
        model = backend.train_model(symbols)
        attempted_at = str(model.get("trained_at") or now_text())
        gate_rejected = model.get("gateRejected") is True
        gate_reason = str(model.get("gateReason") or "")
        with backend.connect() as conn:
            backend.set_meta(conn, "last_auto_model_train_attempt_at", attempted_at)
            backend.set_meta(
                conn,
                "last_auto_model_train_attempt_result",
                "gate_rejected" if gate_rejected else "accepted",
            )
            backend.set_meta(conn, "last_auto_model_train_attempt_reason", gate_reason)
            if not gate_rejected:
                backend.set_meta(conn, "last_auto_model_train_independent", attempted_at)
            backend.set_meta(conn, "last_auto_model_train_attempt_samples", str(model.get("samples", 0)))
            backend.set_meta(conn, "last_auto_model_train_symbol_count", str(len(symbols)))
            backend.set_meta(conn, "last_auto_model_train_holdings_count", str(len(sources["holdings"])))
            backend.set_meta(conn, "last_auto_model_train_monster_liquid_count", str(len(sources["monster_liquid"])))
            backend.set_meta(conn, "last_auto_model_train_symbols", ",".join(symbols))
            backend.set_meta(conn, "last_auto_model_train_source_errors", " | ".join(source_errors))
        if gate_rejected:
            cycle_note = (
                f"新模型 {attempted_at} 被品質閘門拒絕，"
                f"沿用生效模型 {active_trained_at or '無'}：{gate_reason or '品質未通過'}"
            )
        else:
            active_trained_at = attempted_at
            cycle_note = f"生效模型已更新為 {active_trained_at}"
    settled = backend.update_outcomes()
    prediction_message = auto_batch_save_predictions()
    return f"獨立模型循環：{cycle_note}；結算 {settled} 筆；{prediction_message}"


def auto_weekly_tcn_experiment():
    """Run the isolated daily TCN comparison once per Friday at most."""
    today = scheduler_today()
    with backend.connect() as conn:
        latest = conn.execute("""
            SELECT run_id, completed_at, data_max_date
            FROM model_experiment_runs
            WHERE status = 'completed'
            ORDER BY completed_at DESC
            LIMIT 1
        """).fetchone()
    if latest and str(latest[1] or "")[:10] == today:
        return f"日線 TCN 本週基線已完成：{latest[0]}，資料日 {latest[2] or '-'}"
    python_path = ROOT / ".venv-pytorch" / "Scripts" / "python.exe"
    experiment_path = ROOT / "pytorch_experiment.py"
    if not python_path.exists():
        raise RuntimeError(f"PyTorch 專用環境不存在：{python_path}")
    command = [
        str(python_path), str(experiment_path), "train",
        "--max-symbols", "400",
        "--max-samples", "30000",
        "--epochs", "5",
        "--stride", "2",
    ]
    result = subprocess.run(
        command,
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=3600,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "unknown error").strip()[-2000:]
        raise RuntimeError(f"日線 TCN 基線失敗：{detail}")
    try:
        payload = json.loads(result.stdout)
    except (TypeError, ValueError) as exc:
        raise RuntimeError("日線 TCN 完成但輸出不是有效 JSON") from exc
    with backend.connect() as conn:
        backend.set_meta(conn, "last_weekly_tcn_experiment_at", now_text())
        backend.set_meta(conn, "last_weekly_tcn_experiment_run_id", str(payload.get("runId") or ""))
        backend.set_meta(conn, "last_weekly_tcn_experiment_data_date", str(payload.get("dataMaxDate") or ""))
        backend.set_meta(conn, "last_weekly_tcn_experiment_qualified", "1" if payload.get("gate", {}).get("dailyTcnQualified") else "0")
    return (
        f"日線 TCN 同口徑基線完成：{payload.get('sampleCount', 0)} 筆"
        f"，外推閘門 {'通過' if payload.get('gate', {}).get('dailyTcnQualified') else '未通過'}"
        "；維持獨立觀察模式"
    )


def auto_paper_signal_snapshot(session="close_1520"):
    session_key, session_meta = paper_signal_session_meta(session)
    result = record_paper_signal_snapshot(max_symbols=180, include_holdings=True, session=session_key)
    record_paper_signal_snapshot_meta(result, session_key)
    return (
        f"紙上交易快照({session_meta.get('label') or session_key})：已存 {result.get('saved', 0)} 筆"
        f"，檢查 {result.get('checked', 0)} 檔"
        f"，錯誤 {len(result.get('errors') or [])} 筆"
    )


def record_paper_signal_snapshot_meta(result, session_key):
    with backend.connect() as conn:
        backend.set_meta(conn, "last_paper_signal_snapshot_at", result.get("updatedAt", ""))
        backend.set_meta(conn, "last_paper_signal_snapshot_session", session_key)
        backend.set_meta(conn, "last_paper_signal_snapshot_saved", str(result.get("saved", 0)))
        backend.set_meta(conn, "last_paper_signal_snapshot_checked", str(result.get("checked", 0)))
        backend.set_meta(conn, "last_paper_signal_snapshot_errors", str(len(result.get("errors") or [])))
        backend.set_meta(conn, f"last_paper_signal_snapshot_{session_key}_at", result.get("updatedAt", ""))
        backend.set_meta(conn, f"last_paper_signal_snapshot_{session_key}_saved", str(result.get("saved", 0)))
        backend.set_meta(conn, f"last_paper_signal_snapshot_{session_key}_checked", str(result.get("checked", 0)))
        backend.set_meta(conn, f"last_paper_signal_snapshot_{session_key}_errors", str(len(result.get("errors") or [])))


def auto_strategy_calibration():
    today = scheduler_today(taipei_localtime())
    settlement = backend.settle_portfolio_exit_history(as_of_date=today)
    result = backend.save_strategy_calibration_suggestions(
        calibration_date=today,
        min_samples=20,
        schedule_job_id=STRATEGY_CALIBRATION_JOB_ID,
    )
    radar_experiment = backend.save_radar_strategy_experiment_snapshot(
        analysis_date=today,
        lookback_days=1095,
    )
    live_settled = int(
        ((((radar_experiment.get("live") or {}).get("baseline") or {}).get("settled")) or 0)
    )
    qualified = sum(
        1 for item in (((radar_experiment.get("live") or {}).get("experiments")) or [])
        if item.get("adoptionCandidate") is True
    )
    return (
        f"{result.get('scheduleMessage')}｜出場驗證新結算 {settlement.get('settled', 0)} 個視窗"
        f"｜雷達規則觀察：盤中已結算 {live_settled} 筆，可採用實驗 {qualified} 個"
    )


def record_strategy_calibration_meta(result, today):
    suggestions = result.get("suggestions") or []
    risky = [
        item for item in suggestions
        if item.get("suggestedAction") in {"lower_weight_and_raise_threshold", "raise_threshold", "lower_weight"}
    ]
    # save_strategy_calibration_suggestions() 已在寫入建議的同一個 SQLite
    # transaction 內更新 meta；這裡只保留既有呼叫端需要的 risky 清單。
    return risky


def radar_track_record_stats():
    """雷達戰績：predictions 表裡已結算(hit 非 NULL)的 BUY_CANDIDATE 統計。
    只算買進訊號、不混入 WAIT——使用者要知道的是「系統叫我買的，實際多常
    在 10 個交易日內達 +10%」。附上全體已結算(含WAIT)的命中率當對照組，
    跟訓練時 ~12.6% 的自然基準率一起看，才知道模型有沒有真實鑑別力。"""

    def window_stats(conn, action_filter, days):
        cutoff = (datetime.now(TAIPEI_TZ) - timedelta(days=days)).strftime("%Y-%m-%d")
        if action_filter:
            row = conn.execute(
                "SELECT COUNT(*), COALESCE(SUM(hit), 0), AVG(outcome_return) FROM predictions "
                "WHERE hit IS NOT NULL AND action = ? AND price_date >= ?",
                (action_filter, cutoff),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT COUNT(*), COALESCE(SUM(hit), 0), AVG(outcome_return) FROM predictions "
                "WHERE hit IS NOT NULL AND price_date >= ?",
                (cutoff,),
            ).fetchone()
        total = int(row[0] or 0)
        hits = int(row[1] or 0)
        avg_return = float(row[2]) if row[2] is not None else None
        return {
            "settled": total,
            "hits": hits,
            "hitRate": round(hits / total, 4) if total else None,
            "avgOutcomeReturn": round(avg_return, 4) if avg_return is not None else None,
        }

    with backend.connect() as conn:
        buy30 = window_stats(conn, "BUY_CANDIDATE", 30)
        buy90 = window_stats(conn, "BUY_CANDIDATE", 90)
        all90 = window_stats(conn, None, 90)
        # 上線以來累計:雷達很挑、+10% 又要 10 交易日才結算,30/90 天窗的樣本先天就少,
        # 命中率永遠在雜訊區。拉到全期累積,樣本夠了才有機會有統計意義。
        buy_all = window_stats(conn, "BUY_CANDIDATE", 100000)
        # 結算進度:讓使用者看到管線正在填、不是卡住。培養中=該股仍更新到最新、只是還沒滿 10
        # 交易日(會隨交易日陸續結算);殭屍=該股 K 線停在比市場最新還舊(斷更/下市/停牌)→
        # 永遠等不到未來資料。純唯讀計數;一個 grouped MAX 查詢+dict 比對,不逐檔載歷史。
        settled_total = int(conn.execute("SELECT COUNT(*) FROM predictions WHERE hit IS NOT NULL").fetchone()[0] or 0)
        market_latest = conn.execute("SELECT MAX(date) FROM prices").fetchone()[0]
        sym_max = dict(conn.execute("SELECT symbol, MAX(date) FROM prices GROUP BY symbol").fetchall())
        pending_rows = conn.execute("SELECT symbol, price_date FROM predictions WHERE hit IS NULL").fetchall()
    maturing = zombie = 0
    for sym, _pd in pending_rows:
        mx = sym_max.get(sym)
        if mx and market_latest and mx >= market_latest:
            maturing += 1   # 該股仍更新到最新,只是還沒滿 10 交易日
        else:
            zombie += 1     # 該股斷更/無資料 → 永遠等不到未來 K
    settlement_progress = {"settled": settled_total, "maturing": maturing, "zombie": zombie}
    # 殭屍預測稽核：逾期未結算的筆數要一併回傳讓看板誠實——這些列不在
    # 命中率分母裡，逾期一多，顯示的命中率就不能盡信。
    # structurallyUnsettleable(下市/全額交割/長期停牌股的殭屍預測)一併帶出，
    # 讓前端能標註「另有 N 筆是停更股票、不計入」——否則修復上線那天
    # overduePending 會從舊值(含結構性殭屍列)瞬間掉到新值(已排除)，使用者
    # 看到數字驟降卻沒有解釋，容易誤以為結算管線一夜修好或系統壞了。
    try:
        settlement = backend.prediction_settlement_health()
        overdue_pending = settlement["overdue"]
        structurally_unsettleable = settlement.get("structurallyUnsettleable")
    except Exception:
        overdue_pending = None
        structurally_unsettleable = None
    return {
        "ok": True,
        "buy30": buy30,
        "buy90": buy90,
        "buyAllTime": buy_all,
        "allSettled90": all90,
        "baselineRate": 0.126,
        "overduePending": overdue_pending,
        "structurallyUnsettleable": structurally_unsettleable,
        "settlementProgress": settlement_progress,
        "note": "命中=10個交易日內達+10%；基準率~12.6%為訓練池全樣本的自然命中率",
    }


def holdings_dividend_calendar(warn_days=14):
    """持股的除權息日曆：抓 FinMind TaiwanStockDividend，列出 warn_days 天內
    即將除權/除息的持股。除權息當天參考價直接下調(價格跳空)，持有中的
    停損/停利價位語意會被打亂，對短線策略是實際風險，要提前知道。
    結果以日為單位快取在 model_meta——持股通常 10~20 檔，每天最多各打一次
    FinMind，額度負擔小；當天重複開頁面直接吃快取。"""
    today = scheduler_today()
    cache_key = "dividend_calendar_cache"
    with backend.connect() as conn:
        row = conn.execute("SELECT value FROM model_meta WHERE key = ?", (cache_key,)).fetchone()
    if row and row[0]:
        try:
            cached = json.loads(row[0])
            if cached.get("date") == today:
                return {"ok": True, "cached": True, "items": cached.get("items") or [],
                        "checkedCodes": cached.get("checkedCodes", 0)}
        except (TypeError, ValueError):
            pass

    try:
        payload = sinopac_backend.holdings()
        codes = [str(item.get("code") or "").strip() for item in (payload.get("holdings") or [])]
        codes = [code for code in codes if code]
    except Exception as exc:
        return {"ok": False, "error": f"讀取持股失敗：{exc}"}

    token = read_finmind_token()
    items = []
    year_start = f"{today[:4]}-01-01"
    for code in codes:
        try:
            rows = backend.fetch_finmind_dataset("TaiwanStockDividend", code, year_start, "", token)
        except Exception as exc:
            # 今年沒有配息公告的股票 FinMind 回 "no rows"，屬正常情況直接跳過；
            # 額度/網路類錯誤也不讓單檔失敗擋掉整張日曆。
            continue
        for row_data in rows:
            for field, kind in (("CashExDividendTradingDate", "除息"), ("StockExDividendTradingDate", "除權")):
                ex_date = str(row_data.get(field) or "").strip()
                if len(ex_date) != 10 or ex_date < today:
                    continue  # FinMind 對沒有的日期會給 "0"/空字串
                days_until = (datetime.strptime(ex_date, "%Y-%m-%d").date() - datetime.strptime(today, "%Y-%m-%d").date()).days
                if days_until <= warn_days:
                    items.append({"symbol": code, "exDate": ex_date, "kind": kind, "daysUntil": days_until})
    # 同一檔同日可能除權+除息各一列、公告修訂也可能重複，去重後按日期排序
    unique = {}
    for item in items:
        unique[(item["symbol"], item["exDate"], item["kind"])] = item
    items = sorted(unique.values(), key=lambda x: (x["exDate"], x["symbol"]))
    with backend.connect() as conn:
        backend.set_meta(conn, cache_key, json.dumps(
            {"date": today, "items": items, "checkedCodes": len(codes)}, ensure_ascii=False))
    return {"ok": True, "cached": False, "items": items, "checkedCodes": len(codes)}


# ===== 財報季避雷 =====
# 台股法定財報申報截止是固定日期(第一季報→5/15、半年報→8/14、第三季報→11/14、
# 年報→次年3/31)，月營收則每月10日前公布。這幾個窗口是財報/營收跳空的密集期，
# 短線持股抱過去有跳空風險。用純日曆判定「現在是不是接近某個公布窗」，不打 FinMind。
# 誠實說明：台股沒有可靠的「每檔股票下次財報公告日」前瞻資料集，只能用法定截止窗口，
# 屬「全市場層級」警示而非個股精準日期。未來 FinMind 若有前瞻公告日資料集可升級成個股。
EARNINGS_DEADLINES = [
    ("05-15", "第一季財報"),
    ("08-14", "半年報"),
    ("11-14", "第三季財報"),
    ("03-31", "年報"),
]
EARNINGS_WARN_DAYS = 10  # 法定截止日前幾天內算「密集公布期」


def earnings_season_warning(today=None):
    """回傳目前生效的財報/營收跳空警示(全市場層級)。純日曆、不打網路。
    quarterly：今天之後最近的法定財報截止日若在 EARNINGS_WARN_DAYS 內就警示；
    monthly_revenue：每月 1~10 日是月營收公布密集期。"""
    today = today or scheduler_today()
    try:
        today_date = datetime.strptime(today, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return {"active": False, "warnings": []}
    warnings = []
    upcoming = []
    for mmdd, label in EARNINGS_DEADLINES:
        # 找這個截止日「今天之後最近的一次」(今年或明年)
        for year in (today_date.year, today_date.year + 1):
            try:
                deadline = datetime.strptime(f"{year}-{mmdd}", "%Y-%m-%d").date()
            except ValueError:
                continue
            if deadline >= today_date:
                upcoming.append((deadline, label))
                break
    upcoming.sort()
    if upcoming:
        deadline, label = upcoming[0]
        days = (deadline - today_date).days
        if days <= EARNINGS_WARN_DAYS:
            warnings.append({
                "type": "quarterly",
                "label": label,
                "deadline": deadline.isoformat(),
                "daysUntil": days,
                "message": f"{label}申報截止 {deadline.strftime('%m/%d')}（{days} 天內），"
                           f"財報公布密集期，短線持股注意跳空",
            })
    if 1 <= today_date.day <= 10:
        warnings.append({
            "type": "monthly_revenue",
            "label": "月營收",
            "daysUntil": 0,
            "message": "本月營收公布密集期（每月10日前），短線持股注意營收跳空",
        })
    return {"active": bool(warnings), "warnings": warnings}


# ===== 交易複盤日誌 =====
# 短線散戶的第二個大洞是「不複盤」：不知道自己實際交易的勝率、平均抱幾天、
# 更關鍵的是「跟系統建議買的 vs 自己另外買的」哪個賺得多。這是讓使用者(和系統)
# 真正進步的學習迴圈，跟雷達戰績看板(算系統訊號命中率)不同——這裡算「使用者實際
# 交易」的成績。純計算層先做好且測試完整；資料來源(已平倉交易)目前接現有 trades
# 表的 round-trip，未來可升級接永豐 Shioaji list_profit_loss(已實現損益)。
def _journal_days_between(date_a, date_b):
    try:
        a = datetime.strptime(str(date_a)[:10], "%Y-%m-%d").date()
        b = datetime.strptime(str(date_b)[:10], "%Y-%m-%d").date()
        return (b - a).days
    except (TypeError, ValueError):
        return None


def compute_trade_journal(records, recommended_symbols=None):
    """純計算：把已平倉交易(round-trip)彙總成複盤統計。
    records: [{symbol, name, shares, buyPrice, sellPrice, buyDate, sellDate, pnl?}]
    recommended_symbols: 系統當時曾推薦 BUY_CANDIDATE 的 symbol 集合(算跟單率/跟單勝率)。
    回傳 {trades:[enriched], summary:{...}}。pnl 缺就用 (sell-buy)*shares 補算。"""
    recommended = {str(s) for s in (recommended_symbols or set())}
    enriched = []
    for record in records or []:
        buy = float(record.get("buyPrice") or 0)
        sell = float(record.get("sellPrice") or 0)
        shares = int(record.get("shares") or 0)
        pnl = record.get("pnl")
        if pnl is None and buy > 0 and sell > 0 and shares:
            pnl = (sell - buy) * shares
        pnl = float(pnl or 0)
        explicit_pnl_pct = record.get("pnlPct")
        try:
            explicit_pnl_pct = float(explicit_pnl_pct) if explicit_pnl_pct is not None else None
        except (TypeError, ValueError):
            explicit_pnl_pct = None
        pnl_pct = (
            round(explicit_pnl_pct, 2)
            if explicit_pnl_pct is not None
            else round((sell / buy - 1) * 100, 2)
            if buy > 0 and sell > 0
            else None
        )
        followed = str(record.get("symbol") or "") in recommended
        enriched.append({
            **record,
            "pnl": round(pnl, 2),
            "pnlPct": pnl_pct,
            "holdDays": _journal_days_between(record.get("buyDate"), record.get("sellDate")),
            "followedSystem": followed,
        })
    total = len(enriched)
    wins = [t for t in enriched if t["pnl"] > 0]
    hold_vals = [t["holdDays"] for t in enriched if t["holdDays"] is not None]
    followed_trades = [t for t in enriched if t["followedSystem"]]
    not_followed = [t for t in enriched if not t["followedSystem"]]

    def _win_rate(rows):
        return round(sum(1 for t in rows if t["pnl"] > 0) / len(rows), 4) if rows else None

    summary = {
        "count": total,
        "wins": len(wins),
        "losses": sum(1 for t in enriched if t["pnl"] < 0),
        "winRate": _win_rate(enriched),
        "totalPnl": round(sum(t["pnl"] for t in enriched), 2),
        "avgPnl": round(sum(t["pnl"] for t in enriched) / total, 2) if total else None,
        "avgHoldDays": round(sum(hold_vals) / len(hold_vals), 1) if hold_vals else None,
        "followedSystemCount": len(followed_trades),
        "followedSystemRate": round(len(followed_trades) / total, 4) if total else None,
        # 最有價值的複盤洞察：跟系統建議買的勝率 vs 自己另外買的勝率
        "followedWinRate": _win_rate(followed_trades),
        "notFollowedWinRate": _win_rate(not_followed),
        "pnlBasisCounts": dict(Counter(
            str(t.get("pnlBasis") or "gross_execution") for t in enriched
        )),
    }
    return {"trades": enriched, "summary": summary}


def trade_journal_stats(limit=200):
    """讀 trades 表已平倉的 round-trip(有 exit_price 與 exit_at)，交叉比對 predictions
    表當時是否曾對該股票發過 BUY_CANDIDATE，算出複盤統計。目前資料來源是本機 trades
    表；永豐已實現損益(Shioaji list_profit_loss)是未來升級點。"""
    try:
        with backend.connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT symbol, price, execution_price, shares, exit_price, exit_at, "
                "buy_at, filled_at, pnl, pnl_pct, pnl_basis, realized_pnl_key, note "
                "FROM trades WHERE status != 'paper' AND exit_price IS NOT NULL AND exit_at IS NOT NULL "
                "ORDER BY exit_at DESC LIMIT ?",
                (int(limit),),
            ).fetchall()
    except Exception as exc:
        return {"ok": False, "error": f"讀取交易記錄失敗：{exc}"}
    records = []
    symbols = set()
    for row in rows:
        symbol = str(row["symbol"] or "")
        symbols.add(symbol)
        records.append({
            "symbol": symbol,
            "shares": int(row["shares"] or 0),
            "buyPrice": float(row["execution_price"] or row["price"] or 0),
            "sellPrice": float(row["exit_price"] or 0),
            "buyDate": str(row["filled_at"] or row["buy_at"] or "")[:10],
            "sellDate": str(row["exit_at"] or "")[:10],
            "pnl": row["pnl"],
            "pnlPct": row["pnl_pct"],
            "pnlBasis": row["pnl_basis"] or "gross_execution",
            "realizedPnlKey": row["realized_pnl_key"],
        })
    recommended = set()
    if symbols:
        try:
            placeholders = ",".join("?" for _ in symbols)
            with backend.connect() as conn:
                rec_rows = conn.execute(
                    f"SELECT DISTINCT symbol FROM predictions WHERE action = 'BUY_CANDIDATE' "
                    f"AND symbol IN ({placeholders})",
                    list(symbols),
                ).fetchall()
            recommended = {str(r[0]) for r in rec_rows}
        except Exception:
            recommended = set()
    result = compute_trade_journal(records, recommended)
    result["ok"] = True
    result["source"] = "local_trades_table"
    return result


def compute_realized_radar_review(records, radar_by_code, earliest_scan_date, names_by_code=None):
    """純計算：把永豐真實已實現損益對齊雷達推薦史，分成『誠實三態』，不硬湊
    「跟單 vs 沒跟單」二元。

    為什麼不做二元：永豐 list_profit_loss 每筆只有賣出日、沒有買進日/買進價；
    而 monster_scores(雷達推薦快照)是每日表、最早只回溯到 earliest_scan_date。
    多數真實交易發生在雷達還沒開始記錄之前，若把那些「雷達當時根本沒資料」的
    交易一律算成『你自己選的』，勝率對比就會製造假訊號(讓自選看起來屌打雷達，
    其實只是雷達還沒上線)。所以每筆分成:
      recommended  = 賣出日(含)之前雷達曾對這檔判可買(buy_allowed=1)
      candidate_only = 有進過候選池、但賣出日前那次判不可買
      not_scanned  = 雷達有在跑(交易在涵蓋窗內)，但持有期間這檔沒進候選
      no_history   = 交易早於 earliest_scan_date，雷達當時尚未記錄 → 無從評斷

    records: [{code, realized_date, radar_date?, pnl, pr_ratio, quantity, price(賣價)}]
    radar_by_code: {code: [{scan_date, buy_allowed, score, action}, ...]}
    earliest_scan_date: monster_scores 最早掃描日(str)。names_by_code: {code: 中文名}
    回傳 {trades:[enriched(依賣出日新到舊)], summary:{...}}。"""
    names = names_by_code or {}
    earliest = str(earliest_scan_date)[:10] if earliest_scan_date else None
    enriched = []
    for record in records or []:
        code = str(record.get("code") or "").strip()
        sell_date = str(record.get("realized_date") or "")[:10]
        radar_date = str(record.get("radar_date") or sell_date)[:10]
        pnl = record.get("pnl")
        pnl = float(pnl) if pnl is not None else 0.0
        pr = record.get("pr_ratio")
        pr = float(pr) if pr is not None else None
        sell_price = record.get("price")
        sell_price = float(sell_price) if sell_price is not None else None
        pnl_pct = round(pr * 100, 2) if pr is not None else None
        # 買進成本回推:賣價 /(1+報酬率)。pr<=-1(理論全損)或缺值時不可推→None，
        # 不硬算(避免除以 0 或負成本)。
        buy_approx = None
        if sell_price is not None and pr is not None and pr > -0.999:
            buy_approx = round(sell_price / (1 + pr), 2)
        scans = radar_by_code.get(code) or []
        prior = [s for s in scans if s.get("scan_date") and str(s["scan_date"])[:10] <= radar_date]
        state = radar_scan_date = radar_score = radar_action = None
        if prior:
            buyable = [s for s in prior if int(s.get("buy_allowed") or 0) == 1]
            if buyable:
                state = "recommended"
                pick = max(buyable, key=lambda s: str(s["scan_date"]))
            else:
                state = "candidate_only"
                pick = max(prior, key=lambda s: str(s["scan_date"]))
            radar_scan_date = str(pick["scan_date"])[:10]
            radar_score = round(float(pick["score"]), 1) if pick.get("score") is not None else None
            radar_action = pick.get("action")
        elif earliest and radar_date and radar_date < earliest:
            state = "no_history"
        else:
            state = "not_scanned"
        enriched.append({
            "code": code,
            "name": names.get(code) or "",
            "sellDate": sell_date,
            "radarDate": radar_date,
            "pnl": round(pnl, 2),
            "pnlPct": pnl_pct,
            "shares": int(record.get("quantity") or 0),
            "sellPrice": sell_price,
            "buyPriceApprox": buy_approx,
            "radarState": state,
            "radarScanDate": radar_scan_date,
            "radarScore": radar_score,
            "radarAction": radar_action,
        })
    enriched.sort(key=lambda t: t["sellDate"], reverse=True)
    total = len(enriched)
    wins = sum(1 for t in enriched if t["pnl"] > 0)
    losses = sum(1 for t in enriched if t["pnl"] < 0)

    def bucket(state):
        rows = [t for t in enriched if t["radarState"] == state]
        return {
            "count": len(rows),
            "pnl": round(sum(t["pnl"] for t in rows), 2),
            "wins": sum(1 for t in rows if t["pnl"] > 0),
        }

    in_window = sum(1 for t in enriched
                    if not (earliest and t.get("radarDate") and t["radarDate"] < earliest))
    summary = {
        "count": total,
        "wins": wins,
        "losses": losses,
        "winRate": round(wins / total, 4) if total else None,
        "totalPnl": round(sum(t["pnl"] for t in enriched), 2),
        "coverageStart": earliest,
        "inWindowCount": in_window,
        "byState": {
            "recommended": bucket("recommended"),
            "candidateOnly": bucket("candidate_only"),
            "notScanned": bucket("not_scanned"),
            "noHistory": bucket("no_history"),
        },
    }
    return {"trades": enriched, "summary": summary}


def realized_radar_review_stats():
    """交易複盤資料源：優先用本地已平倉 trades(可用買進日精準對齊雷達)，
    沒有本地 round-trip 時才 fallback 到永豐已實現損益(只能用賣出日近似)。"""
    local_records = []
    try:
        with backend.connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute("""
                SELECT symbol, price, execution_price, shares, exit_price, exit_at,
                       buy_at, filled_at, pnl, pnl_pct, pnl_basis
                FROM trades
                WHERE status != 'paper'
                  AND side = 'BUY'
                  AND exit_price IS NOT NULL
                  AND exit_at IS NOT NULL
                ORDER BY exit_at DESC
                LIMIT 500
            """).fetchall()
        for row in rows:
            buy_price = float(row["execution_price"] or row["price"] or 0)
            sell_price = float(row["exit_price"] or 0)
            shares = int(row["shares"] or 0)
            pnl = row["pnl"]
            if pnl is None and buy_price > 0 and sell_price > 0:
                pnl = (sell_price - buy_price) * shares
            local_records.append({
                "code": str(row["symbol"] or ""),
                "realized_date": str(row["exit_at"] or "")[:10],
                "radar_date": str(row["filled_at"] or row["buy_at"] or row["exit_at"] or "")[:10],
                "pnl": pnl,
                "pr_ratio": (
                    float(row["pnl_pct"]) / 100
                    if row["pnl_pct"] is not None
                    else (sell_price / buy_price - 1)
                    if buy_price > 0 and sell_price > 0
                    else None
                ),
                "price": sell_price,
                "quantity": shares,
                "pnl_basis": row["pnl_basis"] or "gross_execution",
            })
    except Exception:
        local_records = []
    if local_records:
        result = _radar_review_from_records(local_records)
        if result.get("error"):
            result["ok"] = False
            return result
        result["ok"] = True
        result["source"] = "local_trades_table"
        summ = result["summary"]
        summ["pnlBasisCounts"] = dict(Counter(
            str(row.get("pnl_basis") or "gross_execution") for row in local_records
        ))
        rec = summ["byState"]["recommended"]["count"]
        result["note"] = (
            f"優先使用本地 trades 已平倉交易，共 {summ['count']} 筆；雷達對齊日期=買進日。"
            f"雷達推薦史自 {summ.get('coverageStart') or '—'} 起，{summ['inWindowCount']} 筆落在涵蓋窗內、"
            f"其中 {rec} 筆買進日前雷達曾判可買。"
        )
        return result

    try:
        records = backend.list_realized_pnl(limit=500)
    except Exception as exc:
        return {"ok": False, "error": f"讀取已實現損益失敗：{exc}"}
    if not records:
        return {
            "ok": True, "source": "sinopac_realized", "trades": [], "summary": {"count": 0},
            "note": "尚未匯入永豐已實現損益。到『買賣停損提醒』設定區按「抓永豐交易紀錄」匯入後即可複盤。",
        }
    result = _radar_review_from_records(records)
    if result.get("error"):
        result["ok"] = False
        return result
    result["ok"] = True
    result["source"] = "sinopac_realized"
    summ = result["summary"]
    rec = summ["byState"]["recommended"]["count"]
    result["note"] = (
        f"共 {summ['count']} 筆真實已實現損益；雷達推薦史自 {summ.get('coverageStart') or '—'} 起，"
        f"僅 {summ['inWindowCount']} 筆落在涵蓋窗內、其中 {rec} 筆雷達當時曾判可買。"
        "目前這是賣出日近似對齊；等本地 trades 有 round-trip 後會優先改用買進日。"
    )
    return result


def _radar_review_from_records(records):
    codes = sorted({str(r.get("code") or "").strip() for r in records if r.get("code")})
    radar_by_code, names_by_code, earliest_scan_date = {}, {}, None
    try:
        with backend.connect() as conn:
            conn.row_factory = sqlite3.Row
            earliest_scan_date = conn.execute("SELECT MIN(scan_date) FROM monster_scores").fetchone()[0]
            if codes:
                placeholders = ",".join("?" for _ in codes)
                for row in conn.execute(
                    f"SELECT symbol, scan_date, buy_allowed, score, action FROM monster_scores "
                    f"WHERE symbol IN ({placeholders})", codes,
                ).fetchall():
                    radar_by_code.setdefault(str(row["symbol"]), []).append({
                        "scan_date": row["scan_date"], "buy_allowed": row["buy_allowed"],
                        "score": row["score"], "action": row["action"],
                    })
                for row in conn.execute(
                    f"SELECT symbol, name FROM stock_info WHERE symbol IN ({placeholders})", codes,
                ).fetchall():
                    names_by_code[str(row["symbol"])] = row["name"]
    except Exception as exc:
        return {"trades": [], "summary": {"count": 0}, "error": f"對齊雷達推薦史失敗：{exc}"}
    return compute_realized_radar_review(records, radar_by_code, earliest_scan_date, names_by_code)


LOSS_STREAK_CIRCUIT_THRESHOLD = 3


def compute_loss_streak(records, threshold=LOSS_STREAK_CIRCUIT_THRESHOLD):
    """純計算連續虧損熔斷：records 須依平倉時間『由新到舊』排序。從最近一筆往回數，
    連續虧損幾筆(遇到第一筆非虧損就停)。streak>=threshold=觸發熔斷警示(建議暫停/縮小
    部位)，這是給短線散戶的心理紀律工具——連續踩雷時最容易情緒化加碼凹單。
    pnl 缺就用 (sell-buy)*shares 補算。"""
    streak = 0
    streak_trades = []
    for record in records or []:
        pnl = record.get("pnl")
        if pnl is None:
            buy = float(record.get("buyPrice") or 0)
            sell = float(record.get("sellPrice") or 0)
            shares = int(record.get("shares") or 0)
            pnl = (sell - buy) * shares if (buy > 0 and sell > 0 and shares) else 0
        if float(pnl or 0) < 0:
            streak += 1
            streak_trades.append(record)
        else:
            break
    tripped = streak >= threshold
    if streak == 0:
        level = "ok"
    elif tripped:
        level = "circuit"
    elif streak >= threshold - 1:
        level = "caution"
    else:
        level = "ok"
    return {"streak": streak, "threshold": threshold, "tripped": tripped,
            "level": level, "recentLossCount": len(streak_trades)}


def loss_streak_status(limit=20):
    """讀 trades 表最近已平倉的 round-trip(由新到舊)算連續虧損熔斷狀態。
    跟 ④ 交易複盤日誌共用同一份『已平倉真實交易』資料源，trades 表目前空時
    回 hasData:False/streak:0，一旦有真實平倉交易就自動生效。"""
    try:
        with backend.connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT symbol, price, shares, exit_price, exit_at, pnl "
                "FROM trades WHERE status != 'paper' AND exit_price IS NOT NULL AND exit_at IS NOT NULL "
                "ORDER BY exit_at DESC LIMIT ?",
                (int(limit),),
            ).fetchall()
    except Exception as exc:
        return {"ok": False, "error": f"讀取交易記錄失敗：{exc}"}
    records = [{
        "symbol": str(row["symbol"] or ""),
        "shares": int(row["shares"] or 0),
        "buyPrice": float(row["price"] or 0),
        "sellPrice": float(row["exit_price"] or 0),
        "sellDate": str(row["exit_at"] or "")[:10],
        "pnl": row["pnl"],
    } for row in rows]
    status = compute_loss_streak(records)
    streak = status["streak"]
    if status["level"] == "circuit":
        advice = f"⚠️ 已連續虧損 {streak} 筆，觸發熔斷：建議暫停進場、檢討策略，下一筆先大幅縮小部位再試"
    elif status["level"] == "caution":
        advice = f"已連續虧損 {streak} 筆，再 1 筆就觸發熔斷，建議下一筆縮小部位、更嚴守停損"
    else:
        advice = "近期無連續虧損，維持既有紀律即可"
    return {"ok": True, "hasData": bool(records), "advice": advice, **status}


def build_morning_brief_message():
    """開盤前晨報：使用者每天最需要資訊的時刻是開盤前，不是只有盤後。
    內容以「今天開盤要盯哪些價位」為主：最近一次雷達掃描的前5名(含觸發價/
    停損價，這兩個價位是盤中判斷「該不該進」的依據)、持股數、3天內除權息
    警示。跟盤後摘要同樣的韌性設計：每段各自try/except，單段失敗不擋整則。"""
    today = scheduler_today()
    lines = [f"🌅 StockAI 開盤前晨報 {today[5:]}"]

    try:
        payload = backend.list_monster_scores(80)
        candidates = payload.get("candidates") or []
        scan_date = str(payload.get("scanDate") or "")
        if candidates:
            lines.append(f"📋 今日觀察(依 {scan_date} 掃描)：")
            top = sorted(candidates, key=lambda c: float(c.get("score") or 0), reverse=True)[:5]
            for c in top:
                mark = "✅" if c.get("buyAllowed") else "👀"
                name = str(c.get("name") or "").strip()
                trigger = c.get("buy_trigger")
                stop = c.get("stop_price")
                trigger_text = f"觸發{float(trigger):.2f}" if trigger else ""
                stop_text = f"停損{float(stop):.2f}" if stop else ""
                price_text = "/".join(part for part in (trigger_text, stop_text) if part)
                lines.append(f"{mark} {c.get('symbol')} {name} {price_text}".rstrip())
        else:
            lines.append("📋 目前沒有雷達候選")
    except Exception as exc:
        lines.append(f"📋 觀察清單讀取失敗：{str(exc)[:60]}")

    try:
        holdings_payload = sinopac_backend.holdings()
        holding_rows = holdings_payload.get("holdings") or []
        if holding_rows:
            lines.append(f"💼 持股 {len(holding_rows)} 檔，開盤後留意賣出提醒")
    except Exception:
        pass

    try:
        calendar = holdings_dividend_calendar(warn_days=3)
        upcoming = calendar.get("items") or [] if calendar.get("ok") else []
        for item in upcoming[:3]:
            when = "今天" if item["daysUntil"] == 0 else f"剩{item['daysUntil']}天"
            lines.append(f"📅 {item['symbol']} {item['kind']}({when})，參考價會下調")
    except Exception:
        pass

    try:
        season = earnings_season_warning(today)
        for warn in (season.get("warnings") or [])[:2]:
            lines.append(f"⚠️ {warn['message']}")
    except Exception:
        pass

    return "\n".join(lines)


def auto_morning_brief():
    """開盤前晨報排程(08:15-08:50)：在0845資料缺口修復/08:55盤前熱機之前
    送出，內容用的是前一交易日的掃描結果(當天掃描15:00才會跑)。跟盤後
    摘要合計每交易日2則LINE(月額度200約用44)。"""
    message = build_morning_brief_message()
    send_line_message_via_api(message)
    return f"晨報已送出({message.count(chr(10)) + 1}行)"


def build_daily_digest_message():
    """組每日盤後摘要文字。跟送出拆開成獨立函式：可以單獨測試訊息組裝邏輯，
    也方便未來加「手動預覽今天的摘要」功能。每一段各自 try/except——任何
    一段的資料來源掛掉(掃描沒跑/永豐沒設定/DB查詢失敗)都只讓那一段顯示
    讀取失敗或直接省略，不能讓整則摘要發不出去。"""
    today = scheduler_today()
    lines = [f"📊 StockAI 盤後摘要 {today[5:]}"]

    try:
        payload = backend.list_monster_scores(80)
        candidates = payload.get("candidates") or []
        scan_date = str(payload.get("scanDate") or "")
        if scan_date != today:
            lines.append(f"🔍 今天沒有新的雷達掃描結果(最近一次：{scan_date or '無'})")
        else:
            buyable = [c for c in candidates if c.get("buyAllowed")]
            lines.append(f"🔍 雷達候選 {len(candidates)} 檔(可買 {len(buyable)} 檔)")
            top = sorted(candidates, key=lambda c: float(c.get("score") or 0), reverse=True)[:5]
            for c in top:
                mark = "✅" if c.get("buyAllowed") else "👀"
                name = str(c.get("name") or "").strip()
                # list_monster_scores 的 score 本來就是 0-100 尺度(妖股分數)，
                # 不是 0-1 機率，直接取整顯示。
                score = round(float(c.get("score") or 0))
                lines.append(f"{mark} {c.get('symbol')} {name} 分數{score}")
    except Exception as exc:
        lines.append(f"🔍 雷達結果讀取失敗：{str(exc)[:60]}")

    try:
        # 近30天已結算(hit非NULL)的BUY_CANDIDATE命中率——這是模型買進訊號
        # 的真實戰績，不是全體預測(全體含大量WAIT，混在一起沒有意義)。
        cutoff = (datetime.now(TAIPEI_TZ) - timedelta(days=30)).strftime("%Y-%m-%d")
        with backend.connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*), COALESCE(SUM(hit), 0) FROM predictions "
                "WHERE hit IS NOT NULL AND action = 'BUY_CANDIDATE' AND price_date >= ?",
                (cutoff,),
            ).fetchone()
        total, hits = int(row[0] or 0), int(row[1] or 0)
        # 樣本 < 20 筆的命中率是雜訊(雷達很挑,30天只結算得出個位數),不報百分比避免誤導,
        # 只報累積進度。夠了才報命中率。
        if total >= 20:
            lines.append(f"🎯 近30天買進訊號已結算 {total} 筆，+10%命中 {hits} 筆({hits * 100 // total}%)")
        elif total:
            lines.append(f"🎯 近30天買進訊號已結算 {total} 筆（樣本累積中，暫不評斷命中率）")
    except Exception:
        pass

    try:
        holdings_payload = sinopac_backend.holdings()
        holding_rows = holdings_payload.get("holdings") or []
        if holding_rows:
            lines.append(f"💼 持股 {len(holding_rows)} 檔，賣出提醒以系統頁面為準")
    except Exception:
        pass  # 永豐沒設定/連線失敗就省略這段，摘要照送

    try:
        # 7天內即將除權息的持股(除權息日參考價下調會打亂停損/停利價位語意)
        calendar = holdings_dividend_calendar(warn_days=7)
        upcoming = calendar.get("items") or [] if calendar.get("ok") else []
        for item in upcoming[:5]:
            lines.append(f"📅 {item['symbol']} {item['exDate']} {item['kind']}(剩{item['daysUntil']}天)，注意價位參考基準會下調")
    except Exception:
        pass

    try:
        with backend.connect() as conn:
            row = conn.execute(
                "SELECT value FROM model_meta WHERE key = 'training_progress'"
            ).fetchone()
        progress = json.loads(row[0]) if row and row[0] else {}
        trained_at = str(progress.get("trainedAt") or "")
        if trained_at.startswith(today):
            lines.append(f"🧠 模型今日已重訓({trained_at[11:16]})")
        elif trained_at:
            lines.append(f"⚠️ 模型今天沒有重訓(最近：{trained_at[:16]})")
    except Exception:
        pass

    return "\n".join(lines)


def auto_daily_digest():
    """每日盤後摘要：把雷達結果/推薦戰績/持股概況/模型狀態彙整成一則LINE
    訊息，每交易日只花 1 則額度(免費方案 200 則/月，22 個交易日約用 22 則)。
    排程在 17:35 之後：1430妖股掃描/1510批量預測/1540資料缺口修復都已跑完，
    摘要拿到的是當天最終狀態。send_line_message 失敗會往外拋，讓
    run_auto_schedule_job 的重試機制接手(最多3次)。"""
    message = build_daily_digest_message()
    send_line_message_via_api(message)
    return f"盤後摘要已送出({message.count(chr(10)) + 1}行)"


def intraday_tick_spawn_window(now):
    # 開盤(08:55)前 10 分鐘就 spawn 子程序，讓它有時間登入永豐+訂閱熱機；
    # 子程序自己的 wait_for_market_open() 會耐心等到 08:55 才開始收 tick，
    # 不會因為提早啟動就立刻退出。視窗上限抓 13:35(收盤緩衝)，避免太晚
    # 還 spawn 一個只能跑幾分鐘就被收盤條件結束的無意義子程序。
    if not (0 <= now.tm_wday <= 4):
        return False
    minutes = now.tm_hour * 60 + now.tm_min
    return (8 * 60 + 45) <= minutes <= (13 * 60 + 35)


def intraday_tick_should_keep_alive(now):
    # spawn 視窗之後再留一點緩衝(到 13:45)，讓子程序自己 flush+收尾退出；
    # 超過這個時間如果還活著，視為異常，由監督迴圈強制 terminate。
    if not (0 <= now.tm_wday <= 4):
        return False
    minutes = now.tm_hour * 60 + now.tm_min
    return (8 * 60 + 45) <= minutes <= (13 * 60 + 45)


def intraday_tick_worker():
    global intraday_tick_process, intraday_tick_status
    # 仿照 daily_update_worker/auto_schedule_worker 的「while True + 包一層
    # try/except，絕不讓單次例外打死整個 daemon thread」模式，多了一層
    # subprocess 生命週期管理：開盤前 spawn、收盤後等它自己退出(或逾時強制
    # terminate)、盤中如果非預期當機就重啟(有次數上限，避免無限重登入)。
    script_path = ROOT / "realtime_tick_collector.py"
    spawned_date = None
    restart_count = 0
    last_spawn_at = 0.0
    MAX_RESTARTS_PER_DAY = 3
    MIN_RESPAWN_GAP_SECONDS = 120

    while True:
        try:
            now = taipei_localtime()
            today = time.strftime("%Y-%m-%d", now)
            if today != spawned_date:
                # 換了一天，重置重啟計數與程序狀態追蹤。
                spawned_date = today
                restart_count = 0

            if not SINOPAC_CONFIG_PATH.exists():
                time.sleep(60)
                continue

            # start_intraday_tick_collector()(手動 API 觸發路徑)讀寫這兩個
            # 全域變數前都會拿 intraday_tick_process_lock，這個 30 秒背景
            # 監督迴圈原本完全沒拿鎖——使用者在自動監督迴圈判斷「該重啟」的
            # 同一瞬間手動按下啟動按鈕，兩邊會同時 spawn 出兩個沒人追蹤的
            # subprocess(其中一個的 pid 直接被另一邊的寫入蓋掉，變成孤兒
            # process)。整段讀取判斷到寫入都包進同一個鎖，跟手動路徑用同一把
            # 鎖保證互斥。
            with intraday_tick_process_lock:
                process_alive = intraday_tick_process is not None and intraday_tick_process.poll() is None
                try:
                    market_day = official_market_day_status(today)
                except Exception as exc:
                    market_day = {"known": False, "isTradingDay": None, "reason": str(exc)[:120]}
                trading_session = market_schedule_allowed(market_day)

                if not process_alive and trading_session and intraday_tick_spawn_window(now):
                    can_spawn = (
                        (intraday_tick_process is None and restart_count == 0)
                        or (intraday_tick_process is not None and restart_count < MAX_RESTARTS_PER_DAY
                            and time.time() - last_spawn_at >= MIN_RESPAWN_GAP_SECONDS)
                    )
                    if can_spawn:
                        if intraday_tick_process is not None:
                            restart_count += 1
                            exit_code = intraday_tick_process.poll()
                            print(f"intraday tick collector 非預期結束(exit={exit_code})，第 {restart_count} 次重啟")
                        flags = subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0
                        cleanup_stale_tick_collectors()
                        intraday_tick_process = subprocess.Popen(
                            [sys.executable, str(script_path)],
                            cwd=str(ROOT),
                            creationflags=flags,
                        )
                        intraday_tick_status.update({
                            "ok": True,
                            "running": True,
                            "pid": intraday_tick_process.pid,
                            "startedAt": time.strftime("%Y-%m-%d %H:%M:%S"),
                            "lastError": "",
                            "trigger": "auto",
                        })
                        last_spawn_at = time.time()
                        print(f"intraday tick collector 已啟動 pid={intraday_tick_process.pid}")
                elif process_alive and (not trading_session or not intraday_tick_should_keep_alive(now)):
                    print("intraday tick collector 超過收盤緩衝時間仍在執行，強制結束")
                    try:
                        intraday_tick_process.terminate()
                        intraday_tick_process.wait(timeout=30)
                    except Exception as exc:
                        print(f"intraday tick collector terminate 失敗：{exc}")
                        intraday_tick_status["lastError"] = str(exc)
                    intraday_tick_status["running"] = False
                    intraday_tick_process = None
        except Exception as exc:
            # 跟 daily_update_worker 一樣的理由：這是 daemon thread，迴圈內
            # 任何單次例外都不能讓整個監督迴圈永久停掉。
            print(f"intraday_tick_worker loop error: {exc}")
            intraday_tick_status.update({"ok": False, "lastError": str(exc)})
        time.sleep(30)


def start_intraday_tick_thread():
    global intraday_tick_started
    if intraday_tick_started:
        return
    intraday_tick_started = True
    cleanup_stale_tick_collectors()
    thread = threading.Thread(target=intraday_tick_worker, daemon=True)
    thread.start()


def start_auto_schedule_thread():
    global auto_schedule_started
    if auto_schedule_started:
        return
    restore_monster_scan_status()
    restore_data_gap_repair_status()
    restore_intraday_discovery_status()
    try:
        observation = backend.start_stability_observation(
            STABILITY_OBSERVATION_KEY,
            start_session_date=scheduler_today(taipei_localtime()),
            target_consecutive_sessions=5,
            scope=STABILITY_OBSERVATION_SCOPE,
        )
        print(
            "Stability observation: "
            f"{observation.get('consecutivePassDays', 0)}/"
            f"{observation.get('targetConsecutiveTradingDays', 5)} consecutive trading days"
        )
    except Exception as exc:
        print(f"Stability observation startup failed: {exc}")
    try:
        horizon_revert = backend.revert_unintended_legacy_horizon_locks(apply=True)
        if horizon_revert.get("changed"):
            print(
                f"Reverted unintended legacy short-horizon locks: "
                f"{horizon_revert.get('changed')} lots "
                f"(audit {horizon_revert.get('auditId')})"
            )
    except Exception as exc:
        print(f"Legacy horizon lock correction failed: {exc}")
    try:
        today = scheduler_today(taipei_localtime())
        with backend.connect() as conn:
            row = conn.execute(
                "SELECT value FROM model_meta WHERE key = 'last_strategy_horizon_evidence_audit_at'"
            ).fetchone()
        if not row or str(row[0] or "")[:10] != today:
            horizon_backfill = backend.backfill_strategy_horizons_from_execution_evidence(apply=True)
            print(
                "Strategy horizon evidence audit: "
                f"{horizon_backfill.get('updatedLots', 0)} updated / "
                f"{horizon_backfill.get('scannedLots', 0)} unknown lots"
            )
    except Exception as exc:
        print(f"Strategy horizon evidence audit failed: {exc}")
    try:
        history_seed = backend.backfill_portfolio_exit_history_from_snapshots()
        if history_seed.get("inserted"):
            print(
                f"Seeded portfolio exit history from latest snapshots: "
                f"{history_seed.get('inserted')} rows"
            )
    except Exception as exc:
        print(f"Portfolio exit history seed failed: {exc}")
    try:
        close_recovery = reconcile_official_close_sync_calendar_state()
        if close_recovery.get("recovered"):
            print(f"Recovered official close sync state: {close_recovery.get('message')}")
    except Exception as exc:
        print(f"Official close sync state recovery failed: {exc}")
    try:
        recovery = backend.reconcile_strategy_calibration_schedule_state(
            today=scheduler_today(taipei_localtime()),
            job_id=STRATEGY_CALIBRATION_JOB_ID,
        )
        if recovery.get("recovered"):
            print(
                f"Recovered strategy calibration schedule state: "
                f"{recovery.get('date')} ({recovery.get('rows')} rows)"
            )
    except Exception as exc:
        print(f"Strategy calibration state recovery failed: {exc}")
    auto_schedule_started = True
    thread = threading.Thread(target=auto_schedule_worker, daemon=True)
    thread.start()


def start_daily_update_thread():
    global daily_update_started
    if daily_update_started:
        return
    daily_update_started = True
    thread = threading.Thread(target=daily_update_worker, daemon=True)
    thread.start()


if __name__ == "__main__":
    try:
        system_health_snapshot = record_system_health(run_system_health(include_prediction=False))
        if system_health_snapshot.get("ok"):
            print(
                "System health OK: "
                f"model {system_health_snapshot.get('model', {}).get('version') or 'unknown'} "
                f"on {system_health_snapshot.get('python', {}).get('executable') or 'python'}"
            )
        else:
            print("System health FAILED: independent model disabled; rule analysis remains data-gated.")
            for item in system_health_snapshot.get("errors") or []:
                print(f" - {item}")
    except Exception as exc:
        system_health_snapshot = record_system_health({
            "ok": False,
            "mode": "independent_model_unavailable",
            "reason": "獨立模型不可用；正式規則分析仍依資料健康運作",
            "errors": [str(exc)],
            "checkedAt": time.strftime("%Y-%m-%d %H:%M:%S"),
            "decisionsEnabled": False,
        })
        print(f"System health FAILED: {exc}")
    start_daily_update_thread()
    start_auto_schedule_thread()
    start_intraday_tick_thread()
    server = bind_server_with_retry(PORT, StockHandler)
    print(f"Serving stock app with live data at http://127.0.0.1:{PORT}/")
    server.serve_forever()
