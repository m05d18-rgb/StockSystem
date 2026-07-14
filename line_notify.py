"""共用的 LINE Messaging API 推播邏輯。

獨立成模組是因為 server.py 在模組載入時會 `from daily_update import run`，
如果 daily_update.py 反過來在模組層級匯入 server.py 就會造成循環匯入；
daily_update.py 也支援獨立執行(`python daily_update.py`，排程常這樣跑)，
不該依賴 server.py 完整初始化過一輪才能用。所以把 LINE 推播抽出來，
server.py 跟 daily_update.py 都直接匯入這個模組。
"""
import json
import os
import threading
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent
LINE_CONFIG_PATH = ROOT / "line_api.json"
LINE_FAILURE_STATE_PATH = ROOT / "line_notify_failure_state.json"
# LINE Messaging API 免費方案每月只有 200 則推播額度，2026-07-03 實際發生過
# 「重複通知白白消耗額度」的事故後，使用者明確提出這個限制。這裡持久化
# 追蹤每月已送出則數，供設定頁顯示與接近上限時的警示使用。注意：這是
# 「本系統自己送出」的計數，如果同一個 LINE channel 還有其他來源在送，
# 官方實際用量會比這個數字高，這個計數只能當下限參考。
LINE_QUOTA_PATH = ROOT / "line_notify_quota.json"
LINE_MONTHLY_QUOTA = 200
LINE_QUOTA_WARN_THRESHOLD = 160  # 用到八成先警示，留緩衝給賣出提醒這類關鍵通知
# 保留池：剩餘額度低於這個數字時，normal 級通知(晨報/盤後摘要/盤中進場推播
# 這類例行訊息)自動讓位不送出，僅放行 critical 級(賣出/停損提醒、排程失敗
# 警示)。月底額度耗盡時第一個死的不能是保命通知。
LINE_QUOTA_RESERVE_FOR_CRITICAL = 20
# 使用者偏好(2026-07-06):LINE「只通知要賣」——賣出/停損提醒(前端賣出鏈+後端
# exit guardian,都帶 priority="critical")以及罕見的系統故障警報照送;其餘例行通知
# (晨報、盤後摘要、盤中進場訊號、漲停打開、盤中突破 這些 priority="normal")一律不送。
# True=只發 critical;想恢復「全部通知」把這裡改回 False 即可(不需動其他程式)。
LINE_SELL_ONLY = True
# _record_line_result 是「讀舊值->算新值->寫回」，daily_update.py 的排程執行緒
# 跟 server.py 的通知鏈執行緒可能同時各自呼叫 send_line_message 送失敗訊息，
# 沒有鎖的話兩邊會讀到同一個舊 consecutiveFailures，各自 +1 後寫回，其中一次
# 的計數會被覆蓋掉、白白少算一次失敗。
_LINE_FAILURE_STATE_LOCK = threading.Lock()


def clean_unicode_text(value):
    text = str(value or "")
    if not any(0xD800 <= ord(ch) <= 0xDFFF for ch in text):
        return text
    return text.encode("utf-16", "surrogatepass").decode("utf-16", "replace")


def mask_secret(value):
    value = str(value or "").strip()
    if len(value) >= 10:
        return f"{value[:4]}...{value[-4:]}"
    return ""


def read_line_file_config():
    if LINE_CONFIG_PATH.exists():
        try:
            return json.loads(LINE_CONFIG_PATH.read_text(encoding="utf-8") or "{}")
        except json.JSONDecodeError:
            return {}
    return {}


def read_line_config():
    data = read_line_file_config()
    token = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN") or data.get("channelAccessToken") or ""
    target = os.environ.get("LINE_PUSH_TO") or data.get("targetId") or ""
    return {
        "channelAccessToken": str(token).strip(),
        "targetId": str(target).strip(),
        "enabled": bool(data.get("enabled", True)),
    }


def read_line_failure_state():
    if LINE_FAILURE_STATE_PATH.exists():
        try:
            data = json.loads(LINE_FAILURE_STATE_PATH.read_text(encoding="utf-8") or "{}")
        except json.JSONDecodeError:
            data = {}
    else:
        data = {}
    return {
        "consecutiveFailures": int(data.get("consecutiveFailures") or 0),
        "lastError": str(data.get("lastError") or ""),
        "lastFailureAt": str(data.get("lastFailureAt") or ""),
    }


def _write_line_failure_state(state):
    # 原本直接 write_text 覆寫，寫入過程中被中斷(程式崩潰/磁碟滿)會留下
    # 半截 JSON，下次 read_line_failure_state 的 JSONDecodeError 防護雖然
    # 不會整個炸掉，但會讓連續失敗計數靜默歸零、示警閾值形同失效。
    # 比照 sinopac_backend.py save_config() 的 temp+os.replace 原子寫入。
    try:
        temp_path = LINE_FAILURE_STATE_PATH.with_name(f"{LINE_FAILURE_STATE_PATH.name}.tmp")
        temp_path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        os.replace(temp_path, LINE_FAILURE_STATE_PATH)
    except OSError:
        pass


def _record_line_result(ok, error=None):
    # send_line_message 本身是無狀態函式(呼叫端每次獨立呼叫)，連續失敗多次
    # (網路持續不穩/LINE API限流)完全沒有跨呼叫記憶，使用者不在電腦前時
    # 不會有任何「已經連續失敗N次」的示警。用檔案持久化計數，跨行程/跨呼叫
    # 都能累積，供設定頁狀態顯示或未來的升級通知使用。
    import datetime as _dt

    with _LINE_FAILURE_STATE_LOCK:
        if ok:
            if read_line_failure_state()["consecutiveFailures"]:
                _write_line_failure_state({"consecutiveFailures": 0, "lastError": "", "lastFailureAt": ""})
            return
        state = read_line_failure_state()
        _write_line_failure_state({
            "consecutiveFailures": state["consecutiveFailures"] + 1,
            "lastError": str(error or "")[:500],
            "lastFailureAt": _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        })


def _read_line_quota_raw():
    if LINE_QUOTA_PATH.exists():
        try:
            return json.loads(LINE_QUOTA_PATH.read_text(encoding="utf-8") or "{}")
        except json.JSONDecodeError:
            return {}
    return {}


def _write_line_quota_raw(data):
    try:
        temp_path = LINE_QUOTA_PATH.with_name(f"{LINE_QUOTA_PATH.name}.tmp")
        temp_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        os.replace(temp_path, LINE_QUOTA_PATH)
    except OSError:
        pass


def read_line_quota():
    import datetime as _dt

    current_month = _dt.datetime.now().strftime("%Y-%m")
    data = _read_line_quota_raw()
    # 跨月自動歸零：檔案裡記的是上個月的計數就視同 0，不用另外排程清理。
    sent = int(data.get("sent") or 0) if str(data.get("month") or "") == current_month else 0
    return {
        "month": current_month,
        "sent": sent,
        "limit": LINE_MONTHLY_QUOTA,
        "remaining": max(0, LINE_MONTHLY_QUOTA - sent),
        "warn": sent >= LINE_QUOTA_WARN_THRESHOLD,
        # 80% 警示這個月是否已經發過(跨月自動重置，跟 sent 同一套語意)。
        "warnNotified": str(data.get("warnNotifiedMonth") or "") == current_month,
    }


def _record_line_sent():
    # 只在真的送出成功時呼叫(失敗的送出不消耗LINE額度)。與失敗計數共用
    # 同一把鎖，read-modify-write 不會跨執行緒漏算。呼叫端必須已經持有
    # _LINE_FAILURE_STATE_LOCK，這裡本身不加鎖(避免非重入鎖死鎖)。
    import datetime as _dt

    quota = read_line_quota()
    raw = _read_line_quota_raw()
    _write_line_quota_raw({
        "month": quota["month"],
        "sent": quota["sent"] + 1,
        "updatedAt": _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        # 保留同月的警示標記，避免每送一則就把「已警示過」洗掉造成重複警示。
        "warnNotifiedMonth": _carry_warn_month(raw, quota["month"]),
    })


def _carry_warn_month(raw, current_month):
    # 2026-07-04 稽核修復：跨月時，raw 裡還留著上個月的 warnNotifiedMonth
    # 字串，若原封不動寫進新月份紀錄，會出現「month=本月、warnNotifiedMonth=
    # 上個月」的自相矛盾狀態。雖然下游 read_line_quota 的嚴格月份比對
    # (warnNotifiedMonth == current_month)會把這個舊值稀釋成 warnNotified=False
    # 不至於誤判，但持久化狀態自相矛盾遲早會踩雷——比照 sent 的跨月自動歸零，
    # 只有當儲存的警示月份就是本月時才保留，其餘一律歸零。
    raw_warn = str(raw.get("warnNotifiedMonth") or "")
    return raw_warn if raw_warn == current_month else ""


def _rollback_line_sent():
    # 2026-07-04 稽核修復：額度保留池的 check-and-reserve 是「先佔位、
    # HTTP 發送失敗再還回去」，這裡是還位的部分。呼叫端必須已經持有
    # _LINE_FAILURE_STATE_LOCK。
    import datetime as _dt

    quota = read_line_quota()
    raw = _read_line_quota_raw()
    _write_line_quota_raw({
        "month": quota["month"],
        "sent": max(0, quota["sent"] - 1),
        "updatedAt": _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "warnNotifiedMonth": _carry_warn_month(raw, quota["month"]),
    })


def _mark_quota_warn_notified():
    quota = read_line_quota()
    raw = _read_line_quota_raw()
    _write_line_quota_raw({**raw, "month": quota["month"], "sent": quota["sent"], "warnNotifiedMonth": quota["month"]})


def _maybe_send_quota_warning():
    # 跨過 80% 門檻的當下發一次性警示(每月最多一次)。呼叫端在送出成功後
    # 呼叫。2026-07-04 稽核修復：「讀 warnNotified 判斷」到「送出+標記」原本
    # 完全在鎖外，多執行緒同時跨過門檻會各自讀到 warnNotified=False、各自
    # 送出警示，違反「每月最多一次」的設計意圖。改成鎖內原子性地
    # 「讀取+如果未警示就立刻標記」，再到鎖外送出——代價是送出失敗不會重試
    # (下個月才會再有機會)，但這是一次性資訊性警示、不是賣出/停損這類保命
    # 通知，用這個代價換掉重複發送的問題是合理取捨。
    with _LINE_FAILURE_STATE_LOCK:
        quota = read_line_quota()
        if quota["sent"] < LINE_QUOTA_WARN_THRESHOLD or quota["warnNotified"]:
            return
        _mark_quota_warn_notified()
    try:
        send_line_message(
            f"⚠️ StockAI LINE 額度警示：本月已送出 {quota['sent']}/{quota['limit']} 則(80%)。"
            f"剩餘額度低於 {LINE_QUOTA_RESERVE_FOR_CRITICAL} 則時，晨報/盤後摘要等例行通知"
            f"會自動停送，只保留賣出/停損等關鍵提醒。",
            priority="critical",
            _quota_warning=True,
        )
    except Exception:
        # 警示送不出去不能影響原本那則訊息的成功回報。
        pass


def send_line_message(message, force=False, priority="normal", _quota_warning=False):
    """priority: "normal"(例行通知，額度緊張時自動讓位) 或 "critical"(賣出/停損/
    系統故障這類保命通知，額度用到最後一則前都放行)。suppressed 回傳比照
    disabled 模式 ok=True——呼叫端(排程重試鏈)必須把「因額度保留而未送出」
    視為成功，否則會觸發 3 次重試+失敗警示的連鎖。"""
    config = read_line_config()
    if not force and not config["enabled"]:
        return {"ok": True, "sent": False, "disabled": True}
    # 只發賣出模式(LINE_SELL_ONLY):非 critical(=賣出/停損以外的例行通知)直接靜默。
    # 回傳比照 suppressed/disabled 讓 ok=True——呼叫端(排程重試鏈)必須把「刻意不送」
    # 視為成功,否則會誤觸 3 次重試+失敗警示連鎖。force=True 仍照送(通知測試按鈕會用到)。
    if LINE_SELL_ONLY and priority != "critical" and not force:
        return {"ok": True, "sent": False, "suppressed": True,
                "reason": "LINE 只發賣出/停損提醒模式,例行通知(晨報/盤後/進場/突破)已略過"}
    if not config["channelAccessToken"] or not config["targetId"]:
        raise RuntimeError("LINE Messaging API is not configured")
    # 2026-07-04 稽核修復：額度保留池檢查(read_line_quota)原本完全在鎖外，
    # server.py 是 ThreadingHTTPServer+多個背景執行緒，兩個執行緒可能同時
    # 讀到 remaining 剛好在門檻之上都判定通過、都真的送出，讓保留池在臨界值
    # 上被多個執行緒同時擠過去(check-then-act race)。改成鎖內原子性地
    # 「檢查+立刻預先佔用這則名額」(reserved=True)，HTTP 真的發送失敗再
    # 回滾(_rollback_line_sent)——鎖只包住毫秒級的檔案讀寫，不含網路 I/O。
    # 2026-07-04 稽核修復：force=True 原本只繞過 enabled 檢查、卻不繞過額度
    # 保留池，語意矛盾(force 應該是「無論如何都要送」)。加上 `not force`，讓
    # force 跟 critical 一樣不受保留池限制、但仍計入月額度(見下方成功分支的
    # `if not reserved` 補記)——目前唯一呼叫點雖然剛好也帶 priority="critical"
    # 沒踩到，但把語意補正避免未來新呼叫點只帶 force 就被保留池靜默擋下。
    reserved = False
    if priority != "critical" and not force:
        with _LINE_FAILURE_STATE_LOCK:
            quota = read_line_quota()
            if quota["remaining"] <= LINE_QUOTA_RESERVE_FOR_CRITICAL:
                return {
                    "ok": True,
                    "sent": False,
                    "suppressed": True,
                    "reason": f"本月LINE額度僅剩 {quota['remaining']} 則，保留給賣出/停損等關鍵通知",
                }
            _record_line_sent()
            reserved = True
    body = {
        "to": config["targetId"],
        "messages": [{"type": "text", "text": clean_unicode_text(message)[:5000]}],
    }
    request = Request(
        "https://api.line.me/v2/bot/message/push",
        data=json.dumps(body, ensure_ascii=False).encode("utf-8", errors="replace"),
        headers={
            "Authorization": f"Bearer {config['channelAccessToken']}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=30) as response:
            response.read()
        _record_line_result(True)
        if not reserved:
            # critical 級沒有預先佔用名額(不受保留池限制)，成功送出後才
            # 補記一次，維持月額度統計仍然計入這則。
            with _LINE_FAILURE_STATE_LOCK:
                _record_line_sent()
        if not _quota_warning:
            _maybe_send_quota_warning()
        return {"ok": True, "sent": True, "targetMasked": mask_secret(config["targetId"])}
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        error_message = f"LINE push failed: HTTP {exc.code} {detail}"
        _record_line_result(False, error_message)
        if reserved:
            with _LINE_FAILURE_STATE_LOCK:
                _rollback_line_sent()
        raise RuntimeError(error_message) from exc
    except Exception as exc:
        # HTTPError 是 URLError 的子類別，只接住它會漏掉 DNS 失敗/連線逾時/
        # connection reset 這類網路層例外(URLError/socket.timeout)——這些
        # 會用 urllib 原始的 `<urlopen error ...>` 字串往外拋，跟 HTTPError
        # 那種一看就知道是 LINE 推播失敗的訊息格式不一致。呼叫端(daily_update.py
        # 等)把這個例外字串記進 log/notifyResult 時，之後用「LINE push
        # failed」關鍵字搜尋 log 找問題會漏掉這類其實是 LINE 推播失敗、
        # 只是被網路層例外掩蓋的案例。
        error_message = f"LINE push failed: {exc}"
        _record_line_result(False, error_message)
        if reserved:
            with _LINE_FAILURE_STATE_LOCK:
                _rollback_line_sent()
        raise RuntimeError(error_message) from exc
