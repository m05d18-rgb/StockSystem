from pathlib import Path
import copy
import datetime as dt
import json
import os
import subprocess
import sys
import threading
import time

from capital_backend import capital_backend


ROOT = Path(__file__).resolve().parent
TAIPEI_TZ = dt.timezone(dt.timedelta(hours=8))
CONFIG_PATH = ROOT / "sinopac_api.json"
SHARE_OVERRIDES_PATH = ROOT / "portfolio_share_overrides.json"
SHIOAJI_CALL_LOCK = threading.Lock()
SHIOAJI_MIN_CALL_GAP_SECONDS = 1.5
SHIOAJI_LAST_CALL_AT = 0.0
QUOTE_CACHE_TTL_SECONDS = 20
QUOTE_STALE_FALLBACK_SECONDS = 300
SHIOAJI_QUOTE_CIRCUIT_FAILURE_THRESHOLD = 3
SHIOAJI_QUOTE_CIRCUIT_COOLDOWN_SECONDS = 120
SHIOAJI_SNAPSHOT_BATCH_SIZE = 200
QUOTE_CACHE = {}
QUOTE_CODE_CACHE = {}
QUOTE_CACHE_LOCK = threading.Lock()
# place_order() 對賣出方向「核對真實庫存」到「送出委託」是兩次獨立的
# subprocess 呼叫，中間沒有鎖保護。永豐 list_positions 不會在委託送出的
# 瞬間就反映扣減(通常要等成交回報)，使用者手滑連點兩次賣出、或多個分頁
# 對同一帳號幾乎同時各送一筆賣單，兩邊都可能通過「賣出股數<=庫存」的
# 檢查，疊加送出超過實際庫存的委託。用一把全域鎖把整段「核對→送出」
# 序列化，這是單一使用者系統，犧牲的平行度可忽略。
SELL_ORDER_LOCK = threading.Lock()
ORDER_CONFIRM_TEXT = "我確認下單"
ORDER_ACTIONS = {
    "BUY": {"value": "Buy", "text": "買進"},
    "SELL": {"value": "Sell", "text": "賣出"},
}
ORDER_PRICE_TYPES = {"LMT", "MKT"}
ORDER_TYPES = {"ROD", "IOC", "FOK"}
ORDER_COND = "Cash"
ORDER_LOTS = {
    "COMMON": {
        "api": "Common",
        "text": "整張",
        "unit": "張",
        "shares_per_unit": 1000,
        "max_quantity": 100,
    },
    "INTRADAY_ODD": {
        "api": "IntradayOdd",
        "text": "零股",
        "unit": "股",
        "shares_per_unit": 1,
        "max_quantity": 999,
    },
}


def normalize_order_lot(value):
    key = str(value or "COMMON").strip().upper().replace("-", "_")
    aliases = {
        "COMMON": "COMMON",
        "LOT": "COMMON",
        "BOARD_LOT": "COMMON",
        "張": "COMMON",
        "整張": "COMMON",
        "INTRADAY_ODD": "INTRADAY_ODD",
        "INTRADAYODD": "INTRADAY_ODD",
        "ODD": "INTRADAY_ODD",
        "ODDLOT": "INTRADAY_ODD",
        "ODD_LOT": "INTRADAY_ODD",
        "SHARE": "INTRADAY_ODD",
        "SHARES": "INTRADAY_ODD",
        "股": "INTRADAY_ODD",
        "零股": "INTRADAY_ODD",
    }
    return aliases.get(key, key)


def mask_secret(value):
    value = str(value or "")
    if len(value) < 8:
        return ""
    return f"{value[:4]}...{value[-4:]}"


def mask_person_id(value):
    # mask_secret(頭4+尾4)是給高熵長金鑰用的；台灣身分證字號只有10碼，
    # 露出8碼等於幾乎沒遮。這裡只露出第1碼+最後2碼，夠使用者確認是自己的
    # 證號，又不會把整個證號攤在狀態API回應裡。
    value = str(value or "").strip()
    if len(value) < 8:
        return ""
    return f"{value[0]}{'*' * (len(value) - 3)}{value[-2:]}"


def is_etf_like_code(value):
    code = "".join(ch for ch in str(value or "") if ch.isdigit())
    return code.startswith("00")


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


def normalize_shioaji_snapshot_time(value):
    """Convert Shioaji's Python snapshot ``ts`` to an aware Taipei timestamp.

    Shioaji exposes snapshot time as a nanosecond integer whose displayed
    calendar fields are Taiwan local time. Treating it as a conventional UTC
    Unix epoch adds eight hours and makes every live quote look futuristic.
    Non-numeric SDK values are retained as ISO timestamps when possible.
    """
    if isinstance(value, dt.datetime):
        stamp = value
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=TAIPEI_TZ)
        else:
            stamp = stamp.astimezone(TAIPEI_TZ)
        return stamp.isoformat()

    text = str(value or "").strip()
    if not text:
        return ""
    try:
        number = float(text)
        if number > 100_000_000:
            if number > 100_000_000_000_000_000:
                number /= 1_000_000_000
            elif number > 100_000_000_000_000:
                number /= 1_000_000
            elif number > 100_000_000_000:
                number /= 1_000
            # The SDK's documented examples display the UTC calendar fields as
            # local Taiwan wall-clock time. Replace the zone instead of doing a
            # UTC -> Taipei conversion, which would incorrectly add eight hours.
            wall_clock = dt.datetime.fromtimestamp(number, dt.timezone.utc)
            return wall_clock.replace(tzinfo=TAIPEI_TZ).isoformat()
    except (TypeError, ValueError, OSError, OverflowError):
        pass

    try:
        parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=TAIPEI_TZ)
        else:
            parsed = parsed.astimezone(TAIPEI_TZ)
        return parsed.isoformat()
    except ValueError:
        return text


class SinoPacBackend:
    def __init__(self):
        self._quote_health_lock = threading.Lock()
        self._quote_failure_count = 0
        self._quote_circuit_open_until = 0.0
        self._last_taiex_snapshot = None

    def quote_circuit_state(self):
        now = time.monotonic()
        with self._quote_health_lock:
            retry_after = max(0.0, self._quote_circuit_open_until - now)
            return {
                "open": retry_after > 0,
                "failureCount": self._quote_failure_count,
                "retryAfterSeconds": round(retry_after, 2),
            }

    def record_quote_probe_result(self, ok):
        with self._quote_health_lock:
            if ok:
                self._quote_failure_count = 0
                self._quote_circuit_open_until = 0.0
            else:
                self._quote_failure_count += 1
                if self._quote_failure_count >= SHIOAJI_QUOTE_CIRCUIT_FAILURE_THRESHOLD:
                    self._quote_circuit_open_until = max(
                        self._quote_circuit_open_until,
                        time.monotonic() + SHIOAJI_QUOTE_CIRCUIT_COOLDOWN_SECONDS,
                    )
        return self.quote_circuit_state()

    def reset_quote_circuit(self):
        self.record_quote_probe_result(True)

    @staticmethod
    def annotate_quote_circuit(payload, state):
        result = payload
        result.update({
            "sinopacCircuitOpen": bool(state.get("open")),
            "sinopacCircuitFailureCount": int(state.get("failureCount") or 0),
            "sinopacCircuitRetryAfterSeconds": state.get("retryAfterSeconds") or 0,
        })
        return result

    def cached_quote_payload(self, cache_key, max_age_seconds):
        with QUOTE_CACHE_LOCK:
            cached = QUOTE_CACHE.get(cache_key)
            if not cached:
                cached = None
            if cached:
                age = time.time() - cached.get("storedAt", 0)
                if age <= max_age_seconds:
                    payload = copy.deepcopy(cached.get("payload") or {})
                    payload["cached"] = True
                    payload["cacheAgeSeconds"] = round(age, 2)
                    return payload

            quotes = {}
            ages = []
            missing = []
            for code in cache_key:
                item = QUOTE_CODE_CACHE.get(code)
                age = time.time() - item.get("storedAt", 0) if item else None
                if not item or age is None or age > max_age_seconds:
                    missing.append(code)
                    continue
                quote = copy.deepcopy(item.get("quote") or {})
                if quote:
                    quotes[code] = quote
                    ages.append(age)
            if not quotes:
                return None
            if missing and max_age_seconds <= QUOTE_CACHE_TTL_SECONDS:
                return None
            return {
                "ok": True,
                "quotes": quotes,
                "count": len(quotes),
                "cached": True,
                "partialCache": bool(missing),
                "missingCacheCodes": missing,
                "cacheAgeSeconds": round(max(ages), 2) if ages else None,
            }

    def store_quote_cache(self, cache_key, payload, fetched_at=None):
        # fetched_at 記錄的是「發起這次查詢」的時間，不是「寫入快取」的時間；
        # 用它(而非寫入完成順序)決定要不要覆寫，避免併發時較早查詢、但因鎖
        # 排隊較晚完成的結果，蓋掉另一個較新查詢已經寫入的快照。
        fetched_at = time.time() if fetched_at is None else fetched_at
        with QUOTE_CACHE_LOCK:
            existing_aggregate = QUOTE_CACHE.get(cache_key)
            if not existing_aggregate or fetched_at >= existing_aggregate.get("fetchedAt", 0):
                QUOTE_CACHE[cache_key] = {
                    "storedAt": time.time(),
                    "fetchedAt": fetched_at,
                    "payload": copy.deepcopy(payload or {}),
                }
            stored_at = time.time()
            for code, quote in ((payload or {}).get("quotes") or {}).items():
                clean = self.clean_code(code)
                if not clean or not quote:
                    continue
                existing_code_entry = QUOTE_CODE_CACHE.get(clean)
                if existing_code_entry and existing_code_entry.get("fetchedAt", 0) > fetched_at:
                    continue
                QUOTE_CODE_CACHE[clean] = {
                    "storedAt": stored_at,
                    "fetchedAt": fetched_at,
                    "quote": copy.deepcopy(quote),
                }

    def run_shioaji_child(self, command, timeout, input_text=None):
        global SHIOAJI_LAST_CALL_AT
        with SHIOAJI_CALL_LOCK:
            elapsed = time.time() - SHIOAJI_LAST_CALL_AT
            if elapsed < SHIOAJI_MIN_CALL_GAP_SECONDS:
                time.sleep(SHIOAJI_MIN_CALL_GAP_SECONDS - elapsed)
            completed = subprocess.run(
                command,
                cwd=ROOT,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                input=input_text,
            )
            SHIOAJI_LAST_CALL_AT = time.time()
            return completed

    def load_config(self):
        if not CONFIG_PATH.exists():
            return {}
        try:
            return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except Exception as exc:
            # 之前這裡沒有try/except：設定檔一旦損毀(寫到一半被中斷/手動編輯
            # 壞掉)，json.loads會直接讓JSONDecodeError一路傳到holdings()/
            # quotes()/下單流程等11處呼叫點，造成永豐功能整個連續失效，
            # 且使用者只會看到原始JSON parse錯誤字串看不懂發生什麼事。
            # 比照load_share_overrides()的防禦模式：視為未設定，讓上層
            # 走「尚未設定永豐API」的正常提示路徑，而不是讓例外到處亂飛。
            print(f"sinopac_api.json 讀取失敗，視為未設定：{exc}")
            return {}

    def save_config(self, payload):
        api_key = str(payload.get("apiKey", "")).strip()
        secret_key = str(payload.get("secretKey", "")).strip()
        if len(api_key) < 8 or len(secret_key) < 8:
            raise ValueError("請輸入有效的永豐 API Key 與 Secret Key")
        old_config = self.load_config()
        ca_path = self.normalize_ca_path(payload.get("caPath", "")) or self.normalize_ca_path(old_config.get("caPath", ""))
        ca_password = str(payload.get("caPassword", "")).strip() or str(old_config.get("caPassword", "")).strip()
        person_id = str(payload.get("personId", "")).strip() or str(old_config.get("personId", "")).strip()
        config = {
            "apiKey": api_key,
            "secretKey": secret_key,
            "simulation": bool(payload.get("simulation", False)),
            "caPath": ca_path,
            "caPassword": ca_password,
            "personId": person_id,
        }
        # 原子寫入(temp+os.replace)：這個檔案存的是下單必要憑證，寫到一半
        # 被中斷(OneDrive同步鎖檔/程式崩潰/磁碟滿)會讓檔案停在半截JSON，
        # 之後每次讀取都會壞掉。比照daily_update.py的save_run_log模式。
        temp_path = CONFIG_PATH.with_name(f"{CONFIG_PATH.name}.tmp")
        temp_path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temp_path, CONFIG_PATH)
        return self.status()

    def clear_config(self):
        if CONFIG_PATH.exists():
            CONFIG_PATH.unlink()
        return self.status()

    def load_share_overrides(self):
        if not SHARE_OVERRIDES_PATH.exists():
            return {}
        try:
            data = json.loads(SHARE_OVERRIDES_PATH.read_text(encoding="utf-8"))
        except Exception:
            return {}
        output = {}
        for code, shares in (data or {}).items():
            clean = self.clean_code(code)
            try:
                number = int(float(shares))
            except (TypeError, ValueError):
                continue
            if clean and number > 0:
                output[clean] = number
        return output

    def status(self):
        config = self.load_config()
        ca_path = self.resolve_ca_path(config)
        ca_exists = bool(ca_path and ca_path.exists())
        return {
            "ok": True,
            "configured": bool(config.get("apiKey") and config.get("secretKey")),
            "apiKeyMasked": mask_secret(config.get("apiKey")),
            "secretKeyMasked": mask_secret(config.get("secretKey")),
            "caConfigured": self.ca_configured(config),
            "caPathLabel": ca_path.name if ca_path else "",
            "caPathExists": ca_exists,
            "personIdMasked": mask_person_id(config.get("personId")),
            "simulation": bool(config.get("simulation", False)),
            # 不回傳 configPath：完整絕對路徑會洩漏使用者名稱與磁碟結構，
            # 前端也從來沒有用到這個欄位。
        }

    def normalize_ca_path(self, value):
        return str(value or "").strip().strip("\"'").strip()

    def resolve_ca_path(self, config):
        raw = self.normalize_ca_path(config.get("caPath", ""))
        if not raw:
            return None
        path = Path(raw).expanduser()
        if not path.is_absolute():
            path = ROOT / path
        return path

    def ca_configured(self, config):
        ca_path = self.resolve_ca_path(config)
        return bool(ca_path and ca_path.exists() and config.get("caPassword") and config.get("personId"))

    def holdings(self, include_realized_days=None):
        config = self.load_config()
        if not config.get("apiKey") or not config.get("secretKey"):
            raise RuntimeError("尚未設定永豐 API Key / Secret Key")

        command = [sys.executable, str(Path(__file__).resolve()), "--holdings-direct"]
        # 交易複盤自動化(2026-07-09):傳天數時,子行程會在「同一次永豐登入」順便抓 list_profit_loss
        # (已實現損益),零額外登入(永豐短時間重複登入會被拒 400)。抓取在子行程端 try/except 隔離,
        # 任何失敗都只是不帶 realizedRecords,絕不影響庫存(停損監控命脈)。
        if include_realized_days:
            command.append(str(int(include_realized_days)))
        try:
            completed = self.run_shioaji_child(command, timeout=90 if include_realized_days else 60)
        except subprocess.TimeoutExpired as exc:
            output = (exc.stdout or "") + "\n" + (exc.stderr or "")
            message = self.sanitize_text(output.strip() or "永豐 API 讀取逾時", config)
            raise RuntimeError(message) from exc

        payload = self.parse_child_json(completed.stdout)
        if payload is None:
            output = "\n".join(part for part in [completed.stdout, completed.stderr] if part)
            message = self.sanitize_text(output.strip() or "永豐 API 子程序沒有回傳資料", config)
            raise RuntimeError(message)
        if not payload.get("ok"):
            raise RuntimeError(self.sanitize_text(payload.get("error") or "永豐 API 讀取失敗", config))
        return self.enrich_holdings_market_data(payload)

    def enrich_holdings_market_data(self, payload):
        """永豐庫存保留原券商語意，只用群益補缺失的共通市場報價。"""
        result = copy.deepcopy(payload or {})
        holdings = result.get("holdings") if isinstance(result.get("holdings"), list) else []
        missing_codes = []
        for item in holdings:
            code = self.clean_code((item or {}).get("code"))
            current_price = self.safe_float((item or {}).get("currentPrice"))
            if code and (current_price is None or current_price <= 0) and code not in missing_codes:
                missing_codes.append(code)
        if not missing_codes:
            result["marketDataFallbackUsed"] = False
            return result
        try:
            fallback = capital_backend.live_quotes(missing_codes)
        except Exception as exc:
            fallback = {"ok": False, "quotes": {}, "error": f"群益報價備援例外：{type(exc).__name__}"}
        quotes = fallback.get("quotes") if isinstance(fallback.get("quotes"), dict) else {}
        fallback_codes = []
        for item in holdings:
            code = self.clean_code((item or {}).get("code"))
            quote = quotes.get(code)
            if not isinstance(quote, dict):
                continue
            item.update({
                "currentPrice": quote.get("currentPrice"),
                "referencePrice": quote.get("referencePrice"),
                "changeRate": quote.get("changeRate"),
                "openPrice": quote.get("open"),
                "highPrice": quote.get("high"),
                "lowPrice": quote.get("low"),
                "totalVolume": quote.get("totalVolume"),
                "snapshotAt": quote.get("quoteTimestamp") or quote.get("receivedAt"),
                "marketDataSource": "Capital Strategy King COM",
            })
            fallback_codes.append(code)
        result.update({
            "marketDataFallbackUsed": bool(fallback_codes),
            "marketDataFallbackProvider": "Capital Strategy King COM" if fallback_codes else "",
            "marketDataFallbackCodes": sorted(set(fallback_codes)),
            "marketDataMissingCodes": [code for code in missing_codes if code not in fallback_codes],
            "marketDataFallbackError": "" if fallback_codes else (fallback.get("error") or "群益沒有有效報價"),
        })
        return result

    def realized_pnl(self, begin, end):
        """抓永豐『已實現損益』(list_profit_loss)——交易複盤用:把真實已賣出的買賣+損益抓回來。
        【探測版】回傳原始記錄不寫 DB,讓上機一按就看到真實欄位長相,確認 shape 後再寫進 trades。
        跟 holdings() 同一套子行程登入模式,唯讀、絕不下單。"""
        config = self.load_config()
        if not config.get("apiKey") or not config.get("secretKey"):
            raise RuntimeError("尚未設定永豐 API Key / Secret Key")
        command = [sys.executable, str(Path(__file__).resolve()), "--profit-loss-direct", str(begin), str(end)]
        try:
            completed = self.run_shioaji_child(command, timeout=90)
        except subprocess.TimeoutExpired as exc:
            output = (exc.stdout or "") + "\n" + (exc.stderr or "")
            message = self.sanitize_text(output.strip() or "永豐已實現損益讀取逾時", config)
            raise RuntimeError(message) from exc
        payload = self.parse_child_json(completed.stdout)
        if payload is None:
            output = "\n".join(part for part in [completed.stdout, completed.stderr] if part)
            message = self.sanitize_text(output.strip() or "永豐 API 子程序沒有回傳資料", config)
            raise RuntimeError(message)
        if not payload.get("ok"):
            raise RuntimeError(self.sanitize_text(payload.get("error") or "永豐已實現損益讀取失敗", config))
        return payload

    def capital_quote_fallback(self, codes, reason="", primary_payload=None):
        """用群益補齊永豐缺漏；沒有通過群益實際驗證時不會產生任何報價。"""
        primary = copy.deepcopy(primary_payload or {})
        primary_quotes = primary.get("quotes") if isinstance(primary.get("quotes"), dict) else {}
        primary_quotes = dict(primary_quotes)
        missing = [code for code in codes if code not in primary_quotes]
        if not missing:
            primary["source"] = primary.get("source") or "Shioaji quote"
            return primary
        try:
            capital_payload = capital_backend.live_quotes(missing)
        except Exception as exc:
            capital_payload = {"ok": False, "quotes": {}, "error": f"群益備援例外：{type(exc).__name__}"}
        capital_quotes = capital_payload.get("quotes") if isinstance(capital_payload.get("quotes"), dict) else {}
        if not capital_quotes:
            if primary_payload is None:
                return None
            primary.update({
                "ok": True,
                "quotes": primary_quotes,
                "count": len(primary_quotes),
                "source": primary.get("source") or "Shioaji quote",
                "partial": True,
                "missingSymbols": missing,
                "capitalFallbackError": capital_payload.get("error") or "群益備援沒有有效報價",
            })
            return primary

        merged = {}
        for code, quote in primary_quotes.items():
            merged[code] = {**quote, "source": quote.get("source") or "Shioaji quote"}
        for code, quote in capital_quotes.items():
            merged[code] = {**quote, "source": "Capital Strategy King COM"}
        remaining = [code for code in codes if code not in merged]
        source = "Shioaji + Capital quote" if primary_quotes else "Capital Strategy King COM"
        return {
            **primary,
            "ok": True,
            "quotes": merged,
            "count": len(merged),
            "source": source,
            "stale": False,
            "fallbackUsed": True,
            "fallbackProvider": "Capital Strategy King COM",
            "fallbackCodes": sorted(capital_quotes),
            "fallbackReason": str(reason or "Sinopac quote missing"),
            "partial": bool(remaining),
            "missingSymbols": remaining,
            "capitalRejectedSymbols": capital_payload.get("rejectedSymbols") or {},
            "error": "",
        }

    def quotes(self, codes):
        clean_codes = [self.clean_code(code) for code in (codes or [])]
        clean_codes = [code for code in dict.fromkeys(clean_codes) if code and not is_etf_like_code(code)]
        if not clean_codes:
            return {"ok": True, "quotes": {}, "count": 0}
        cache_key = tuple(clean_codes)
        cached = self.cached_quote_payload(cache_key, QUOTE_CACHE_TTL_SECONDS)
        if cached:
            cached["stale"] = False
            return cached
        fetch_started_at = time.time()

        def fallback_or_stale(message, circuit_state):
            fallback = self.capital_quote_fallback(clean_codes, reason=message)
            if fallback:
                self.annotate_quote_circuit(fallback, circuit_state)
                self.store_quote_cache(cache_key, fallback, fetched_at=fetch_started_at)
                return fallback
            stale = self.cached_quote_payload(cache_key, QUOTE_STALE_FALLBACK_SECONDS)
            if stale:
                stale.update({"ok": True, "stale": True, "error": message})
                self.annotate_quote_circuit(stale, circuit_state)
                return stale
            return None

        circuit_state = self.quote_circuit_state()
        if circuit_state["open"]:
            message = (
                "Sinopac quote circuit open; retry after "
                f"{circuit_state['retryAfterSeconds']:.0f} seconds"
            )
            recovered = fallback_or_stale(message, circuit_state)
            if recovered:
                return recovered
            raise RuntimeError(message)

        config = self.load_config()
        if not config.get("apiKey") or not config.get("secretKey"):
            fallback = self.capital_quote_fallback(
                clean_codes, reason="Sinopac API Key / Secret Key is not configured"
            )
            if fallback:
                self.annotate_quote_circuit(fallback, circuit_state)
                self.store_quote_cache(cache_key, fallback, fetched_at=fetch_started_at)
                return fallback
            raise RuntimeError("Sinopac API Key / Secret Key is not configured")
        command = [sys.executable, str(Path(__file__).resolve()), "--quotes-direct", ",".join(clean_codes)]
        try:
            completed = self.run_shioaji_child(command, timeout=60)
        except subprocess.TimeoutExpired as exc:
            output = (exc.stdout or "") + "\n" + (exc.stderr or "")
            message = self.sanitize_text(output.strip() or "Sinopac quote timeout", config)
            recovered = fallback_or_stale(message, self.record_quote_probe_result(False))
            if recovered:
                return recovered
            raise RuntimeError(message) from exc
        except Exception as exc:
            message = self.sanitize_text(str(exc) or "Sinopac quote failed", config)
            recovered = fallback_or_stale(message, self.record_quote_probe_result(False))
            if recovered:
                return recovered
            raise RuntimeError(message) from exc
        payload = self.parse_child_json(completed.stdout)
        if payload is None:
            output = "\n".join(part for part in [completed.stdout, completed.stderr] if part)
            message = self.sanitize_text(output.strip() or "Sinopac quote failed", config)
            recovered = fallback_or_stale(message, self.record_quote_probe_result(False))
            if recovered:
                return recovered
            raise RuntimeError(message)
        if not payload.get("ok"):
            message = self.sanitize_text(payload.get("error") or "Sinopac quote failed", config)
            recovered = fallback_or_stale(message, self.record_quote_probe_result(False))
            if recovered:
                return recovered
            raise RuntimeError(message)
        circuit_state = self.record_quote_probe_result(True)
        payload["source"] = payload.get("source") or "Shioaji quote"
        missing = [code for code in clean_codes if code not in (payload.get("quotes") or {})]
        if missing:
            payload = self.capital_quote_fallback(
                clean_codes,
                reason=f"Sinopac missing {len(missing)} quote(s)",
                primary_payload=payload,
            )
        self.annotate_quote_circuit(payload, circuit_state)
        self.store_quote_cache(cache_key, payload, fetched_at=fetch_started_at)
        # 2026-07-09 大盤即時(走A):存這次併抓到的加權指數在單例,server.py /api/market/status
        # 優先用(新鮮才用),否則 fallback Yahoo。零額外登入(併在 quotes 那次)。
        _taiex = payload.get("taiex")
        if isinstance(_taiex, dict) and _taiex.get("price"):
            self._last_taiex_snapshot = (time.time(), _taiex)
        return payload

    def scanner_volume_lots(self, data):
        """Normalize scanner volume to board lots across Shioaji SDK versions."""
        raw = self.safe_float((data or {}).get("total_volume"))
        close = self.safe_float((data or {}).get("close"))
        amount = self.safe_float((data or {}).get("total_amount"))
        if raw is None or raw < 0:
            raw = 0.0
        if raw <= 0 or not close or close <= 0 or not amount or amount <= 0:
            return raw
        inferred_lots = amount / (close * 1000.0)
        if inferred_lots <= 0:
            return raw
        # Some SDK builds expose shares while others expose board lots. Pick
        # the interpretation closest to the exchange-reported trade amount.
        lot_error = abs(raw - inferred_lots) / max(1.0, inferred_lots)
        share_error = abs((raw / 1000.0) - inferred_lots) / max(1.0, inferred_lots)
        return raw / 1000.0 if share_error + 0.05 < lot_error else raw

    def stock_scanners(self, api, count=200):
        """Fetch and merge Shioaji's market-wide live rankings on one session."""
        count = max(1, min(int(count or 200), 200))
        try:
            from shioaji.constant import ScannerType
        except Exception:
            ScannerType = None
        rank_specs = [
            ("change_percent", "ChangePercentRank"),
            ("day_range", "DayRangeRank"),
            ("volume", "VolumeRank"),
            ("amount", "AmountRank"),
            ("tick_count", "TickCountRank"),
        ]
        merged = {}
        rank_counts = {}
        errors = []
        scan_at = dt.datetime.now(TAIPEI_TZ).isoformat()
        for rank_key, enum_name in rank_specs:
            scanner_type = getattr(ScannerType, enum_name, enum_name) if ScannerType else enum_name
            try:
                items = api.scanners(
                    scanner_type=scanner_type,
                    # Shioaji Scanner 的 ascending=True 代表由大到小。
                    # False 會取到排行尾端，讓真正強勢股在召回層直接消失。
                    ascending=True,
                    count=count,
                ) or []
            except Exception as exc:
                rank_counts[rank_key] = 0
                errors.append(f"{rank_key}:{type(exc).__name__}:{exc}")
                continue
            rank_counts[rank_key] = len(items)
            for item in items:
                data = self.model_to_dict(item)
                code = self.clean_code(data.get("code"))
                if not (code.isdigit() and len(code) == 4) or is_etf_like_code(code):
                    continue
                close = self.safe_float(data.get("close"))
                change_price = self.safe_float(data.get("change_price"))
                reference = None
                if close is not None and change_price is not None:
                    reference = close - change_price
                snapshot_at = normalize_shioaji_snapshot_time(data.get("ts")) or scan_at
                total_volume_lots = self.scanner_volume_lots(data)
                row = merged.setdefault(code, {
                    "symbol": code,
                    "code": code,
                    "name": clean_unicode_text(data.get("name") or ""),
                    "date": str(data.get("date") or scan_at[:10])[:10],
                    "open": self.safe_float(data.get("open")),
                    "high": self.safe_float(data.get("high")),
                    "low": self.safe_float(data.get("low")),
                    "close": close,
                    "current": close,
                    "changePrice": change_price,
                    "referencePrice": reference,
                    "totalVolume": total_volume_lots,
                    "totalVolumeLots": total_volume_lots,
                    "totalAmount": self.safe_float(data.get("total_amount")),
                    "volumeRatio": self.safe_float(data.get("volume_ratio")),
                    "rankTypes": [],
                    "rankValues": {},
                    "source": "sinopac_shioaji_scanner",
                    "snapshotAt": snapshot_at,
                    "receivedAt": scan_at,
                })
                row["rankTypes"].append(rank_key)
                row["rankValues"][rank_key] = self.safe_float(data.get("rank_value"))
                # Keep the newest non-empty market fields when a symbol appears
                # in more than one ranking during the same scanner cycle.
                for output_key, input_key in (
                    ("name", "name"),
                    ("open", "open"),
                    ("high", "high"),
                    ("low", "low"),
                    ("close", "close"),
                    ("current", "close"),
                    ("changePrice", "change_price"),
                    ("totalAmount", "total_amount"),
                    ("volumeRatio", "volume_ratio"),
                ):
                    value = data.get(input_key)
                    if output_key == "name":
                        value = clean_unicode_text(value or "")
                    else:
                        value = self.safe_float(value)
                    if value not in (None, ""):
                        row[output_key] = value
                row["totalVolume"] = total_volume_lots
                row["totalVolumeLots"] = total_volume_lots
                row["snapshotAt"] = snapshot_at
        rows = sorted(merged.values(), key=lambda item: (
            -len(item.get("rankTypes") or []),
            -(self.safe_float(item.get("totalAmount")) or 0),
            item.get("symbol") or "",
        ))
        quotes = {}
        for row in rows:
            close = self.safe_float(row.get("close"))
            reference = self.safe_float(row.get("referencePrice"))
            change_rate = None
            if close is not None and reference and reference > 0:
                change_rate = (close / reference - 1.0) * 100.0
            quotes[row["symbol"]] = {
                "currentPrice": close,
                "referencePrice": reference,
                "changePrice": self.safe_float(row.get("changePrice")),
                "changeRate": change_rate,
                "openPrice": self.safe_float(row.get("open")),
                "highPrice": self.safe_float(row.get("high")),
                "lowPrice": self.safe_float(row.get("low")),
                "totalVolume": self.safe_float(row.get("totalVolumeLots")),
                "totalVolumeUnit": "lots",
                "totalAmount": self.safe_float(row.get("totalAmount")),
                "volumeRatio": self.safe_float(row.get("volumeRatio")),
                "scannerRanks": list(row.get("rankTypes") or []),
                "source": "sinopac_shioaji_scanner",
                "snapshotAt": str(row.get("snapshotAt") or scan_at),
                "receivedAt": scan_at,
            }
        return {
            "ok": bool(rows),
            "count": len(rows),
            "rows": rows,
            "quotes": quotes,
            "rankCounts": rank_counts,
            "errors": errors,
            "partial": bool(errors),
            "source": "sinopac_shioaji_scanner",
            "scanAt": scan_at,
        }

    def market_scanners(self, count=200):
        """Child-process fallback used only when collector staging is unavailable."""
        config = self.load_config()
        if not config.get("apiKey") or not config.get("secretKey"):
            raise RuntimeError("Sinopac API Key / Secret Key is not configured")
        command = [
            sys.executable,
            str(Path(__file__).resolve()),
            "--scanners-direct",
            str(max(1, min(int(count or 200), 200))),
        ]
        completed = self.run_shioaji_child(command, timeout=60)
        payload = self.parse_child_json(completed.stdout)
        if payload is None:
            output = "\n".join(part for part in [completed.stdout, completed.stderr] if part)
            raise RuntimeError(self.sanitize_text(output.strip() or "Sinopac scanner failed", config))
        if not payload.get("ok"):
            raise RuntimeError(self.sanitize_text(payload.get("error") or "Sinopac scanner failed", config))
        return payload

    def last_taiex_snapshot(self, max_age_seconds=180):
        """最近一次 quotes() 併抓到的加權指數(夠新才回),否則 None。給 /api/market/status
        優先用永豐即時、否則 fallback Yahoo。"""
        cache = getattr(self, "_last_taiex_snapshot", None)
        if not cache:
            return None
        ts, taiex = cache
        if (time.time() - ts) > max_age_seconds:
            return None
        return taiex

    def order_preview(self, payload):
        config = self.load_config()
        order = self.normalize_order_payload(payload, config, require_execution_ready=False)
        order["ok"] = True
        order["previewOnly"] = True
        order["confirmTextRequired"] = ORDER_CONFIRM_TEXT
        order["canPlace"] = bool(order.get("simulation") or order.get("caConfigured"))
        return order

    def test_suite(self, payload=None):
        payload = payload or {}
        config = self.load_config()
        status = self.status()
        results = []

        def add(key, label, state, message="", detail=None):
            results.append({
                "key": key,
                "label": label,
                "state": state,
                "ok": state == "pass",
                "message": str(message or ""),
                "detail": detail or {},
            })

        configured = bool(config.get("apiKey") and config.get("secretKey"))
        add(
            "api_config",
            "API Key / Secret",
            "pass" if configured else "fail",
            f"API {status.get('apiKeyMasked') or '未設定'} / Secret {status.get('secretKeyMasked') or '未設定'}",
            {"configured": configured, "simulation": bool(status.get("simulation"))},
        )
        ca_ready = self.ca_configured(config)
        ca_path = self.resolve_ca_path(config)
        add(
            "ca_config",
            "CA 憑證",
            "pass" if ca_ready else "warn",
            "已設定且檔案存在，可進行正式下單二次確認" if ca_ready else "未設定或檔案不存在，只能預覽，正式下單會被阻擋",
            {
                "caConfigured": ca_ready,
                "caPathLabel": status.get("caPathLabel") or "",
                "caPathExists": bool(ca_path and ca_path.exists()),
            },
        )

        holdings_payload = None
        if configured:
            try:
                holdings_payload = self.holdings()
                add(
                    "holdings",
                    "讀取庫存 / 帳號",
                    "pass",
                    f"讀到 {holdings_payload.get('count', 0)} 檔，帳號 {holdings_payload.get('accountMasked') or '-'}",
                    {
                        "count": holdings_payload.get("count", 0),
                        "codes": (holdings_payload.get("codes") or [])[:12],
                        "snapshotError": holdings_payload.get("snapshotError") or "",
                    },
                )
            except Exception as exc:
                add("holdings", "讀取庫存 / 帳號", "fail", self.sanitize_text(str(exc), config))
        else:
            add("holdings", "讀取庫存 / 帳號", "skip", "尚未設定 API Key / Secret")

        sample_codes = [self.clean_code(code) for code in (payload.get("symbols") or [])]
        sample_codes = [code for code in dict.fromkeys(sample_codes) if code and len(code) == 4 and not is_etf_like_code(code)]
        if not sample_codes and holdings_payload:
            sample_codes = [
                self.clean_code(code)
                for code in (holdings_payload.get("codes") or [])
                if self.clean_code(code) and not is_etf_like_code(code)
            ][:3]
        if not sample_codes:
            sample_codes = ["2330"]

        quote_payload = None
        if configured:
            try:
                quote_payload = self.quotes(sample_codes[:5])
                quotes = quote_payload.get("quotes") or {}
                add(
                    "quotes",
                    "即時報價",
                    "pass" if quotes else "warn",
                    f"測試 {', '.join(sample_codes[:5])}，回傳 {len(quotes)} 檔"
                    + ("，使用快取" if quote_payload.get("cached") else ""),
                    {
                        "requested": sample_codes[:5],
                        "count": len(quotes),
                        "snapshotError": quote_payload.get("snapshotError") or quote_payload.get("error") or "",
                        "cached": bool(quote_payload.get("cached")),
                        "stale": bool(quote_payload.get("stale")),
                    },
                )
            except Exception as exc:
                add("quotes", "即時報價", "fail", self.sanitize_text(str(exc), config), {"requested": sample_codes[:5]})
        else:
            add("quotes", "即時報價", "skip", "尚未設定 API Key / Secret")

        preview_symbol = sample_codes[0] if sample_codes else "2330"
        preview_order = {
            "symbol": preview_symbol,
            "action": "BUY",
            "priceType": "LMT",
            "price": 100,
            "quantity": 1,
            "orderType": "ROD",
        }
        if configured:
            try:
                preview = self.order_preview(preview_order)
                add(
                    "order_preview",
                    "下單預覽",
                    "pass",
                    f"{preview.get('code')} {preview.get('actionText')} {preview.get('quantity')} 張，預估 {preview.get('estimatedAmount', 0):,.0f} 元",
                    {
                        "canPlace": bool(preview.get("canPlace")),
                        "simulation": bool(preview.get("simulation")),
                        "caConfigured": bool(preview.get("caConfigured")),
                    },
                )
            except Exception as exc:
                add("order_preview", "下單預覽", "fail", self.sanitize_text(str(exc), config))

            try:
                self.place_order({
                    **preview_order,
                    "manualConfirm": False,
                    "confirmText": "",
                    "allowLiveOrder": False,
                })
                add("order_guard", "正式送單防呆", "fail", "未輸入確認文字卻沒有被阻擋，需立即檢查")
            except Exception as exc:
                message = self.sanitize_text(str(exc), config)
                safe_block = "我確認下單" in message or "人工確認" in message or "allowLiveOrder" in message
                add(
                    "order_guard",
                    "正式送單防呆",
                    "pass" if safe_block else "warn",
                    f"已阻擋送出：{message}",
                    {"liveOrderSent": False},
                )
        else:
            add("order_preview", "下單預覽", "skip", "尚未設定 API Key / Secret")
            add("order_guard", "正式送單防呆", "skip", "尚未設定 API Key / Secret")

        failed = [item for item in results if item["state"] == "fail"]
        warnings = [item for item in results if item["state"] == "warn"]
        return {
            "ok": not failed,
            "ready": configured and not failed,
            "checkedAt": time.strftime("%Y-%m-%d %H:%M:%S"),
            "summary": {
                "total": len(results),
                "pass": len([item for item in results if item["state"] == "pass"]),
                "warn": len(warnings),
                "fail": len(failed),
                "skip": len([item for item in results if item["state"] == "skip"]),
            },
            "status": status,
            "sampleSymbols": sample_codes[:5],
            "results": results,
            "safetyNote": "測試套件不會送出正式委託；正式下單仍需人工勾選、輸入確認文字，並通過瀏覽器二次確認。",
        }

    def place_order(self, payload):
        config = self.load_config()
        order = self.normalize_order_payload(payload, config, require_execution_ready=False)
        if str(payload.get("confirmText", "")).strip() != ORDER_CONFIRM_TEXT:
            raise ValueError(f"送出前必須輸入「{ORDER_CONFIRM_TEXT}」")
        if not bool(payload.get("manualConfirm")):
            raise ValueError("送出前必須勾選手動下單確認")
        if not bool(payload.get("allowLiveOrder")):
            raise ValueError("前端未送出 live order 允許旗標，已阻擋下單")
        if not order.get("simulation") and not order.get("caConfigured"):
            raise ValueError("正式下單需要 CA 憑證路徑、CA 密碼與身分證字號")
        # 賣出方向一定要在送出前即時核對永豐真實庫存——不能只信任前端傳來的
        # 資料。前端「賣出數量不可超過庫存」的檢查讀的是本機 localStorage
        # 快取，只在第一步「確認內容」跑過一次，第二步送出不會重跑；且快取
        # 可能因為背景同步延遲而跟目前真實庫存不同步。這裡是最後一道防線，
        # 直接呼叫永豐 API 核對，超過實際庫存就整筆擋下，不送出委託。
        #
        # 「核對庫存」到「送出委託」整段包在 SELL_ORDER_LOCK 內：永豐
        # list_positions 不會在委託送出瞬間就反映扣減(通常要等成交回報)，
        # 沒有鎖的話使用者手滑連點兩次賣出、或多分頁同時各送一筆賣單，
        # 兩邊都可能通過庫存核對、疊加送出超過實際庫存的委託。
        with SELL_ORDER_LOCK:
            if order["action"] == "SELL":
                try:
                    holdings_payload = self.holdings()
                except Exception as exc:
                    raise ValueError(f"送出前無法核對永豐真實庫存，為安全起見已阻擋下單：{exc}") from exc
                held = next(
                    (item for item in (holdings_payload.get("holdings") or []) if item.get("code") == order["code"]),
                    None,
                )
                held_shares = int((held or {}).get("shares") or 0)
                if order["shares"] > held_shares:
                    raise ValueError(
                        f"賣出數量 {order['shares']} 股超過永豐目前實際庫存 {held_shares} 股，已阻擋下單"
                    )
            command = [sys.executable, str(Path(__file__).resolve()), "--order-direct"]
            try:
                completed = self.run_shioaji_child(
                    command,
                    timeout=90,
                    input_text=json.dumps(clean_json_payload(order), ensure_ascii=False),
                )
            except subprocess.TimeoutExpired as exc:
                output = (exc.stdout or "") + "\n" + (exc.stderr or "")
                message = self.sanitize_text(output.strip() or "Sinopac order timeout", config)
                raise RuntimeError(message) from exc
            child_payload = self.parse_child_json(completed.stdout)
            if child_payload is None:
                output = "\n".join(part for part in [completed.stdout, completed.stderr] if part)
                raise RuntimeError(self.sanitize_text(output.strip() or "Sinopac order failed", config))
            if not child_payload.get("ok"):
                raise RuntimeError(self.sanitize_text(child_payload.get("error") or "Sinopac order failed", config))
            return child_payload

    def normalize_order_payload(self, payload, config=None, require_execution_ready=True):
        config = config or self.load_config()
        if not config.get("apiKey") or not config.get("secretKey"):
            raise ValueError("尚未設定永豐 API Key / Secret Key")
        code = self.clean_code(payload.get("symbol") or payload.get("code"))
        if len(code) != 4:
            raise ValueError("目前只允許 4 碼台股一般股票下單")
        if is_etf_like_code(code):
            raise ValueError("目前不開放 ETF / 權證 / 非一般股票下單")
        action = str(payload.get("action") or "").strip().upper()
        if action not in ORDER_ACTIONS:
            raise ValueError("買賣別只允許 BUY 或 SELL")
        price_type = str(payload.get("priceType") or "LMT").strip().upper()
        if price_type not in ORDER_PRICE_TYPES:
            raise ValueError("價格類型只允許 LMT 或 MKT")
        order_type = str(payload.get("orderType") or "ROD").strip().upper()
        if order_type not in ORDER_TYPES:
            raise ValueError("委託類型只允許 ROD / IOC / FOK")
        order_lot_key = normalize_order_lot(payload.get("orderLot") or payload.get("lotType"))
        if order_lot_key not in ORDER_LOTS:
            raise ValueError("Order lot must be COMMON or INTRADAY_ODD")
        if action == "SELL" and order_lot_key != "COMMON":
            raise ValueError("賣出依偏好只允許整張，禁止零股委託")
        order_lot = ORDER_LOTS[order_lot_key]
        quantity = int(float(payload.get("quantity") or 0))
        if quantity <= 0:
            raise ValueError("下單張數必須大於 0")
        if quantity > int(order_lot["max_quantity"]):
            raise ValueError(f"{order_lot['unit']}數必須是 1 到 {order_lot['max_quantity']}")
        if order_lot_key == "INTRADAY_ODD" and price_type != "LMT":
            raise ValueError("零股下單目前只允許限價")
        price = self.safe_float(payload.get("price"))
        if price_type == "LMT":
            if price is None or price <= 0:
                raise ValueError("限價單必須輸入大於 0 的價格")
        else:
            price = 0
        simulation = bool(config.get("simulation", False))
        ca_ready = self.ca_configured(config)
        if require_execution_ready and not simulation and not ca_ready:
            raise ValueError("正式下單需要 CA 憑證路徑、CA 密碼與身分證字號")
        shares = quantity * int(order_lot["shares_per_unit"])
        estimated_amount = (price or 0) * shares
        return {
            "code": code,
            "action": action,
            "actionText": ORDER_ACTIONS[action]["text"],
            "price": price,
            "priceType": price_type,
            "quantity": quantity,
            "shares": shares,
            "unit": order_lot["unit"],
            "orderLotKey": order_lot_key,
            "orderLotText": order_lot["text"],
            "orderType": order_type,
            "orderLot": order_lot["api"],
            "orderCond": ORDER_COND,
            "estimatedAmount": estimated_amount,
            "simulation": simulation,
            "caConfigured": ca_ready,
            "manualOnly": True,
        }

    def activate_ca_for_order(self, api, config):
        if bool(config.get("simulation", False)) and not self.ca_configured(config):
            return False
        if not self.ca_configured(config):
            raise RuntimeError("缺少 CA 憑證資料，無法送出正式下單")
        api.activate_ca(
            ca_path=str(self.resolve_ca_path(config)),
            ca_passwd=str(config.get("caPassword", "")),
            person_id=str(config.get("personId", "")),
        )
        return True

    def order_direct(self, order):
        config = self.load_config()
        if not config.get("apiKey") or not config.get("secretKey"):
            raise RuntimeError("Sinopac API Key / Secret Key is not configured")
        try:
            import shioaji as sj
        except Exception as exc:
            raise RuntimeError(f"Shioaji is not available: {exc}") from exc
        api = sj.Shioaji(simulation=bool(config.get("simulation", False)))
        try:
            # api.login(...) 之前完全沒有 try/except，Shioaji SDK 對登入失敗
            # (金鑰錯誤/風控鎖定)的例外訊息可能夾帶敏感資訊，一路往外拋只會
            # 被main()最外層未遮罩的except接到。這裡就近包一層，跟
            # holdings_direct()一樣用sanitize_error先過濾再往外拋。
            try:
                accounts = api.login(
                    api_key=config["apiKey"],
                    secret_key=config["secretKey"],
                    fetch_contract=True,
                    subscribe_trade=False,
                )
                account = getattr(api, "stock_account", None) or self.pick_stock_account(accounts)
                if account is None:
                    raise RuntimeError("找不到可用的永豐股票帳戶")
                self.activate_ca_for_order(api, config)
                contract = self.stock_contract(api, order["code"])
                if contract is None:
                    raise RuntimeError(f"找不到股票合約：{order['code']}")
                constants = getattr(sj, "constant", sj)
                action_enum = getattr(sj, "Action", None) or constants.Action
                price_type_enum = getattr(sj, "StockPriceType", None) or constants.StockPriceType
                order_type_enum = getattr(sj, "OrderType", None) or constants.OrderType
                order_lot_enum = getattr(sj, "StockOrderLot", None) or constants.StockOrderLot
                order_cond_enum = getattr(sj, "StockOrderCond", None) or constants.StockOrderCond
                order_factory = getattr(sj, "StockOrder", None) or api.Order
                action = action_enum.Buy if order["action"] == "BUY" else action_enum.Sell
                price_type = price_type_enum.LMT if order["priceType"] == "LMT" else price_type_enum.MKT
                order_type = getattr(order_type_enum, order["orderType"])
                order_lot = getattr(order_lot_enum, order.get("orderLot") or "Common")
                stock_order = order_factory(
                    action=action,
                    price=order["price"],
                    quantity=int(order["quantity"]),
                    price_type=price_type,
                    order_type=order_type,
                    order_lot=order_lot,
                    order_cond=order_cond_enum.Cash,
                    account=account,
                )
                trade = api.place_order(contract, stock_order)
            except Exception as exc:
                raise RuntimeError(self.sanitize_error(exc, config)) from exc
            return {
                "ok": True,
                "simulation": bool(config.get("simulation", False)),
                "order": order,
                "accountMasked": self.mask_account(account),
                "trade": self.trade_payload(trade),
            }
        finally:
            try:
                api.logout()
            except Exception:
                pass

    def stock_contract(self, api, code):
        for getter in (
            lambda: api.Contracts.Stocks.get(code),
            lambda: api.Contracts.Stocks[code],
            lambda: api.Contracts.Stocks.TSE[code],
            lambda: api.Contracts.Stocks.OTC[code],
        ):
            try:
                contract = getter()
            except Exception:
                contract = None
            if contract is not None:
                return contract
        return None

    def trade_payload(self, trade):
        data = self.model_to_dict(trade)
        return self.json_safe(data)

    def order_refs_from_trade_payload(self, trade_payload):
        data = trade_payload if isinstance(trade_payload, dict) else {}
        order = data.get("order") if isinstance(data.get("order"), dict) else {}
        status = data.get("status") if isinstance(data.get("status"), dict) else {}
        return {
            "brokerOrderId": str(order.get("id") or status.get("id") or data.get("id") or "").strip(),
            "brokerSeqno": str(order.get("seqno") or data.get("seqno") or "").strip(),
            "brokerOrdno": str(order.get("ordno") or data.get("ordno") or "").strip(),
        }

    def deal_time_text(self, value):
        if value in (None, ""):
            return ""
        try:
            if isinstance(value, (int, float)):
                return dt.datetime.fromtimestamp(float(value)).strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            pass
        text = str(value).strip().replace("T", " ")
        return text[:19]

    def order_lot_shares_per_unit(self, value):
        key = normalize_order_lot(str(value or "COMMON"))
        return int(ORDER_LOTS.get(key, ORDER_LOTS["COMMON"])["shares_per_unit"])

    def normalized_order_action(self, value):
        action = str(value or "").strip()
        if action.lower() == "buy" or action == "買進":
            return "BUY"
        if action.lower() == "sell" or action == "賣出":
            return "SELL"
        return action.upper()

    def sanitized_fill_raw(self, value):
        data = copy.deepcopy(value)

        def scrub(item):
            if isinstance(item, dict):
                cleaned = {}
                for key, child in item.items():
                    normalized_key = str(key).lower()
                    if normalized_key in {"account", "account_id", "broker_id"}:
                        continue
                    cleaned[key] = scrub(child)
                return cleaned
            if isinstance(item, list):
                return [scrub(child) for child in item]
            return item

        return scrub(data)

    def normalized_fill_from_trade_payload(self, trade_payload):
        data = trade_payload if isinstance(trade_payload, dict) else {}
        contract = data.get("contract") if isinstance(data.get("contract"), dict) else {}
        order = data.get("order") if isinstance(data.get("order"), dict) else {}
        status = data.get("status") if isinstance(data.get("status"), dict) else {}
        deals = status.get("deals") or []
        if isinstance(deals, dict):
            deals = list(deals.values())
        if not isinstance(deals, list) or not deals:
            return None
        code = self.clean_code(
            contract.get("code")
            or contract.get("stock_id")
            or order.get("code")
            or data.get("code")
        )
        action = self.normalized_order_action(order.get("action") or data.get("action"))
        shares_per_unit = self.order_lot_shares_per_unit(order.get("order_lot") or order.get("orderLot"))
        total_amount = 0.0
        total_shares = 0
        deal_times = []
        for deal in deals:
            item = deal if isinstance(deal, dict) else self.model_to_dict(deal)
            price = self.safe_float(item.get("price"))
            quantity = int(self.safe_float(item.get("quantity")) or 0)
            shares = quantity * shares_per_unit
            if price and shares > 0:
                total_amount += price * shares
                total_shares += shares
            deal_at = self.deal_time_text(item.get("ts") or item.get("time") or item.get("deal_time"))
            if deal_at:
                deal_times.append(deal_at)
        if not code or not action or total_shares <= 0:
            return None
        refs = self.order_refs_from_trade_payload(data)
        return {
            "code": code,
            "action": action,
            "price": round(total_amount / total_shares, 4) if total_shares else None,
            "shares": total_shares,
            "dealAt": min(deal_times) if deal_times else self.deal_time_text(status.get("modified_time") or status.get("order_datetime")),
            "brokerOrderId": refs["brokerOrderId"],
            "brokerSeqno": refs["brokerSeqno"],
            "brokerOrdno": refs["brokerOrdno"],
            "source": "list_trades.status.deals",
            "raw": self.sanitized_fill_raw(data),
        }

    def normalized_fill_from_order_deal_record_payload(self, record_payload):
        data = record_payload if isinstance(record_payload, dict) else {}
        state = str(data.get("OrderState") or data.get("order_state") or "").strip().upper()
        if state not in {"SDEAL", "STOCKDEAL", "ORDERSTATE.STOCKDEAL"}:
            return None
        record = data.get("record") if isinstance(data.get("record"), dict) else data
        code = self.clean_code(record.get("code") or record.get("stock_id"))
        action = self.normalized_order_action(record.get("action"))
        price = self.safe_float(record.get("price"))
        quantity = int(self.safe_float(record.get("quantity")) or 0)
        shares_per_unit = self.order_lot_shares_per_unit(record.get("order_lot") or record.get("orderLot"))
        shares = quantity * shares_per_unit
        if not code or action not in {"BUY", "SELL"} or not price or shares <= 0:
            return None
        return {
            "code": code,
            "action": action,
            "price": price,
            "shares": shares,
            "dealAt": self.deal_time_text(record.get("ts") or record.get("time") or record.get("deal_time")),
            "brokerOrderId": str(record.get("trade_id") or record.get("id") or "").strip(),
            "brokerSeqno": str(record.get("seqno") or "").strip(),
            "brokerOrdno": str(record.get("ordno") or "").strip(),
            "source": "order_deal_records.SDEAL",
            "raw": self.sanitized_fill_raw(data),
        }

    def order_fills(self):
        command = [sys.executable, str(Path(__file__).resolve()), "--order-fills-direct"]
        completed = self.run_shioaji_child(command, timeout=90)
        payload = self.parse_child_json(completed.stdout)
        if payload is None:
            output = "\n".join(part for part in [completed.stdout, completed.stderr] if part)
            raise RuntimeError(output.strip() or "Sinopac order fills failed")
        if not payload.get("ok"):
            raise RuntimeError(payload.get("error") or "Sinopac order fills failed")
        return payload

    def position_details(self):
        """Read broker open-position lots with real purchase date and cost."""
        config = self.load_config()
        if not config.get("apiKey") or not config.get("secretKey"):
            raise RuntimeError("尚未設定永豐 API Key / Secret Key")
        command = [sys.executable, str(Path(__file__).resolve()), "--position-details-direct"]
        completed = self.run_shioaji_child(command, timeout=120)
        payload = self.parse_child_json(completed.stdout)
        if payload is None:
            output = "\n".join(part for part in [completed.stdout, completed.stderr] if part)
            raise RuntimeError(self.sanitize_text(output.strip() or "永豐持股明細沒有回傳資料", config))
        if not payload.get("ok"):
            raise RuntimeError(self.sanitize_text(payload.get("error") or "永豐持股明細讀取失敗", config))
        return payload

    @staticmethod
    def stock_price_tick(price):
        price = float(price or 0)
        if price < 10:
            return 0.01
        if price < 50:
            return 0.05
        if price < 100:
            return 0.1
        if price < 500:
            return 0.5
        if price < 1000:
            return 1.0
        return 5.0

    def estimated_fill_price_from_position_cost(self, cost_amount, shares):
        if not cost_amount or shares <= 0:
            return None
        cost_price = float(cost_amount) / int(shares)
        pre_fee = cost_price / (1 + 0.001425)
        tick = self.stock_price_tick(pre_fee)
        return round(round(pre_fee / tick) * tick, 4)

    def normalized_position_detail_lots(self, position, details, unit_is_share=False):
        holding = self.position_to_holding(position, unit_is_share=unit_is_share)
        position_data = self.model_to_dict(position)
        expected_shares = int(holding.get("shares") or 0)
        normalized = []
        quantity_total = 0
        for detail in details or []:
            row = self.model_to_dict(detail)
            code = self.clean_code(row.get("code") or row.get("stock_id") or holding.get("code"))
            quantity = int(self.safe_float(row.get("quantity")) or 0)
            price = self.safe_float(row.get("price"))
            date_text = str(row.get("date") or "")[:10]
            if code != holding.get("code") or quantity <= 0 or not price or not date_text:
                continue
            quantity_total += quantity
            normalized.append({
                "code": code,
                "date": date_text,
                "costAmount": round(float(price), 4),
                "quantity": quantity,
                "shares": None,
                "dseq": str(row.get("dseq") or "").strip(),
                "direction": str(row.get("direction") or ""),
                "condition": str(row.get("cond") or ""),
            })

        multiplier = 0
        if expected_shares > 0 and quantity_total == expected_shares:
            multiplier = 1
        elif expected_shares > 0 and quantity_total * 1000 == expected_shares:
            multiplier = 1000
        reconciled = bool(normalized and multiplier > 0)
        if reconciled:
            for row in normalized:
                shares = row.pop("quantity") * multiplier
                row["shares"] = shares
                row["brokerCostPrice"] = round(float(row["costAmount"]) / shares, 4)
                row["estimatedStandardFeeFillPrice"] = self.estimated_fill_price_from_position_cost(
                    row["costAmount"], shares
                )
                row["price"] = row["brokerCostPrice"]
                row["priceSource"] = "Shioaji actual position cost per share including buy fee"
        return {
            "code": holding.get("code"),
            "positionId": int(self.safe_float(position_data.get("id")) or 0),
            "expectedShares": expected_shares,
            "detailQuantityTotal": quantity_total,
            "quantityMultiplier": multiplier or None,
            "reconciled": reconciled,
            "lots": normalized if reconciled else [],
            "reason": "" if reconciled else "持股明細股數無法與券商庫存核對",
        }

    def position_details_direct(self):
        config = self.load_config()
        if not config.get("apiKey") or not config.get("secretKey"):
            raise RuntimeError("尚未設定永豐 API Key / Secret Key")
        try:
            import shioaji as sj
        except Exception as exc:
            raise RuntimeError(f"本機尚未安裝 Shioaji：{exc}") from exc

        api = sj.Shioaji(simulation=bool(config.get("simulation", False)))
        try:
            try:
                accounts = api.login(
                    api_key=config["apiKey"],
                    secret_key=config["secretKey"],
                    fetch_contract=False,
                    subscribe_trade=False,
                )
                account = getattr(api, "stock_account", None) or self.pick_stock_account(accounts)
                if account is None:
                    raise RuntimeError("永豐帳號沒有可用的證券帳戶")
                positions = list(api.list_positions(account) or [])
                try:
                    share_positions = list(api.list_positions(account, unit=sj.constant.Unit.Share) or [])
                except Exception:
                    share_positions = []
            except Exception as exc:
                raise RuntimeError(self.sanitize_error(exc, config)) from exc

            position_rows = []
            lots = []
            errors = []
            common_codes = set()
            candidates = []
            for position in positions:
                holding = self.position_to_holding(position)
                if holding.get("code") and holding.get("shares") and not is_etf_like_code(holding["code"]):
                    candidates.append((position, False))
                    common_codes.add(holding["code"])
            for position in share_positions:
                holding = self.position_to_holding(position, unit_is_share=True)
                if (
                    holding.get("code")
                    and holding.get("shares")
                    and holding["code"] not in common_codes
                    and not is_etf_like_code(holding["code"])
                ):
                    candidates.append((position, True))

            for position, unit_is_share in candidates:
                raw = self.model_to_dict(position)
                raw_detail_id = raw.get("id")
                code = self.position_to_holding(position, unit_is_share=unit_is_share).get("code")
                if raw_detail_id is None:
                    errors.append({"code": code, "reason": "position detail_id 缺失"})
                    continue
                detail_id = int(self.safe_float(raw_detail_id) or 0)
                try:
                    details = api.list_position_detail(account, detail_id=detail_id) or []
                    normalized = self.normalized_position_detail_lots(
                        position, details, unit_is_share=unit_is_share
                    )
                    position_rows.append(normalized)
                    if normalized.get("reconciled"):
                        lots.extend(normalized.get("lots") or [])
                    else:
                        errors.append({"code": code, "reason": normalized.get("reason")})
                except Exception as exc:
                    errors.append({"code": code, "reason": self.sanitize_error(exc, config)})
            return {
                "ok": True,
                "simulation": bool(config.get("simulation", False)),
                "accountMasked": self.mask_account(account),
                "positionCount": len(position_rows),
                "reconciledPositionCount": sum(1 for row in position_rows if row.get("reconciled")),
                "lotCount": len(lots),
                "positions": position_rows,
                "lots": lots,
                "errors": errors,
            }
        finally:
            try:
                api.logout()
            except Exception:
                pass

    def order_fills_direct(self):
        config = self.load_config()
        if not config.get("apiKey") or not config.get("secretKey"):
            raise RuntimeError("尚未設定永豐 API Key / Secret Key")
        try:
            import shioaji as sj
        except Exception as exc:
            raise RuntimeError(f"本機尚未安裝 Shioaji：{exc}") from exc

        api = sj.Shioaji(simulation=bool(config.get("simulation", False)))
        try:
            try:
                accounts = api.login(
                    api_key=config["apiKey"],
                    secret_key=config["secretKey"],
                    fetch_contract=True,
                    subscribe_trade=False,
                )
                account = getattr(api, "stock_account", None) or self.pick_stock_account(accounts)
                if account is None:
                    raise RuntimeError("永豐帳號沒有可用的證券帳戶")
            except Exception as exc:
                raise RuntimeError(self.sanitize_error(exc, config)) from exc
            raw_trades = []
            fills = []
            fill_keys = set()

            def add_fill(fill):
                if not fill:
                    return
                key = "|".join([
                    str(fill.get("brokerOrderId") or ""),
                    str(fill.get("brokerSeqno") or ""),
                    str(fill.get("brokerOrdno") or ""),
                    str(fill.get("code") or ""),
                    str(fill.get("action") or ""),
                    str(fill.get("dealAt") or ""),
                    str(fill.get("price") or ""),
                    str(fill.get("shares") or ""),
                ])
                if key in fill_keys:
                    return
                fill_keys.add(key)
                fills.append(fill)

            try:
                raw_trades = [self.json_safe(self.model_to_dict(t)) for t in (api.list_trades() or [])]
                for trade in raw_trades:
                    add_fill(self.normalized_fill_from_trade_payload(trade))
            except Exception as exc:
                raw_trades = [{"error": self.sanitize_error(exc, config)}]
            raw_deal_records = []
            try:
                raw_deal_records = [
                    self.json_safe(self.model_to_dict(r))
                    for r in (api.order_deal_records(account) or [])
                ]
                for record in raw_deal_records:
                    add_fill(self.normalized_fill_from_order_deal_record_payload(record))
            except Exception as exc:
                raw_deal_records = [{"error": self.sanitize_error(exc, config)}]
            return {
                "ok": True,
                "simulation": bool(config.get("simulation", False)),
                "accountMasked": self.mask_account(account),
                "fills": fills,
                "count": len(fills),
                "rawTradeCount": len(raw_trades),
                "rawDealRecordCount": len(raw_deal_records),
                "rawTradesSample": raw_trades[:20],
                "rawDealRecordsSample": raw_deal_records[:20],
            }
        finally:
            try:
                api.logout()
            except Exception:
                pass

    def json_safe(self, value):
        if isinstance(value, dict):
            return {str(key): self.json_safe(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [self.json_safe(item) for item in value]
        if isinstance(value, (str, int, float, bool)) or value is None:
            return value
        enum_value = getattr(value, "value", None)
        if enum_value is not None:
            return self.json_safe(enum_value)
        iso = getattr(value, "isoformat", None)
        if callable(iso):
            try:
                return iso()
            except Exception:
                pass
        return str(value)

    def holdings_direct(self, realized_days=None):
        config = self.load_config()
        if not config.get("apiKey") or not config.get("secretKey"):
            raise RuntimeError("尚未設定永豐 API Key / Secret Key")
        try:
            import shioaji as sj
        except Exception as exc:
            raise RuntimeError(f"本機尚未安裝 Shioaji：{exc}") from exc

        api = sj.Shioaji(simulation=bool(config.get("simulation", False)))
        try:
            try:
                accounts = api.login(
                    api_key=config["apiKey"],
                    secret_key=config["secretKey"],
                    fetch_contract=True,
                    subscribe_trade=False,
                )
                account = getattr(api, "stock_account", None) or self.pick_stock_account(accounts)
                if account is None:
                    raise RuntimeError("永豐帳號沒有可用的證券帳戶")
                # list_positions 預設 unit=Unit.Common(整張口徑)，未滿一張的
                # 零股部位(用本檔案自己的 ORDER_LOTS.INTRADAY_ODD 買進的那種)
                # 在這個口徑下可能完全不會出現在回傳列表裡，導致使用者明明
                # 持有零股卻在庫存清單/賣出核對(place_order 的持股比對)裡
                # 完全看不到。額外用 unit=Unit.Share(股數口徑)補查一次，
                # 兩者合併——Share 口徑失敗(例如較舊版 SDK 不支援這個參數)
                # 不能讓整支庫存查詢掛掉，只影響零股補齊，優雅降級為只有
                # 整張部位。
                positions = api.list_positions(account)
                try:
                    share_positions = api.list_positions(account, unit=sj.constant.Unit.Share)
                except Exception:
                    share_positions = []
            except Exception as exc:
                raise RuntimeError(self.sanitize_error(exc, config)) from exc
            holdings = [self.position_to_holding(position) for position in positions]
            holdings = [item for item in holdings if item["code"] and item["quantity"] != 0 and not is_etf_like_code(item["code"])]
            known_codes = {item["code"] for item in holdings}
            odd_lot_holdings = [
                self.position_to_holding(position, unit_is_share=True) for position in share_positions
            ]
            for item in odd_lot_holdings:
                # Share 口徑回傳的部位裡，已經有整張口徑抓到的股票代號跳過，
                # 避免同一檔股票的整張與零股被重複計入；只補「整張口徑完全
                # 沒看到」的零股專屬部位。
                if item["code"] and item["shares"] != 0 and item["code"] not in known_codes and not is_etf_like_code(item["code"]):
                    holdings.append(item)
                    known_codes.add(item["code"])
            share_overrides = self.load_share_overrides()
            for item in holdings:
                override_shares = share_overrides.get(item["code"])
                if override_shares:
                    item["shares"] = override_shares
                    item["quantity"] = override_shares / 1000
                    item["quantitySource"] = "portfolio_share_overrides.json"
            snapshots, snapshot_error = self.stock_snapshots(api, [item["code"] for item in holdings])
            for item in holdings:
                item.update(snapshots.get(item["code"], {}))
            account_balance = self.account_balance_payload(api, account, config)
            settlements = self.settlements_payload(api, account, config)
            result = {
                "ok": True,
                "simulation": bool(config.get("simulation", False)),
                "accountMasked": self.mask_account(account),
                "count": len(holdings),
                "codes": [item["code"] for item in holdings],
                "holdings": holdings,
                "accountBalance": account_balance,
                "settlements": settlements,
                "snapshotError": snapshot_error or "",
            }
            # 交易複盤自動化(2026-07-09):同一次登入順便抓已實現損益(list_profit_loss,唯讀、絕不下單)。
            # 完全隔離——任何失敗都只是不帶 realizedRecords,絕不讓庫存查詢掛掉。呼叫端(server)每天只
            # 請求一次(once/day),不是每次同步都拉。
            if realized_days:
                try:
                    import datetime as _dt
                    _end = _dt.date.today()
                    _begin = _end - _dt.timedelta(days=int(realized_days))
                    if hasattr(api, "list_profit_loss"):
                        try:
                            _pl = api.list_profit_loss(account, _begin.isoformat(), _end.isoformat())
                        except TypeError:
                            _pl = api.list_profit_loss(account, begin_date=_begin.isoformat(), end_date=_end.isoformat())
                        _realized = []
                        for _r in (_pl or []):
                            try:
                                _realized.append(self.model_to_dict(_r))
                            except Exception:
                                pass
                        result["realizedRecords"] = _realized
                except Exception:
                    pass  # 已實現損益是加值,壞掉不影響庫存
            return result
        finally:
            try:
                api.logout()
            except Exception:
                pass

    def profit_loss_direct(self, begin, end):
        """子行程:登入永豐,呼叫 list_profit_loss(已實現損益),回傳原始記錄(探測版,不寫DB)。
        唯讀、絕不下單。欄位長相各 SDK 版本可能不同,故用 model_to_dict 原封不動回傳讓呼叫端看真相。"""
        config = self.load_config()
        if not config.get("apiKey") or not config.get("secretKey"):
            raise RuntimeError("尚未設定永豐 API Key / Secret Key")
        try:
            import shioaji as sj
        except Exception as exc:
            raise RuntimeError(f"本機尚未安裝 Shioaji：{exc}") from exc

        api = sj.Shioaji(simulation=bool(config.get("simulation", False)))
        try:
            try:
                accounts = api.login(
                    api_key=config["apiKey"],
                    secret_key=config["secretKey"],
                    fetch_contract=False,
                    subscribe_trade=False,
                )
                account = getattr(api, "stock_account", None) or self.pick_stock_account(accounts)
                if account is None:
                    raise RuntimeError("永豐帳號沒有可用的證券帳戶")
            except Exception as exc:
                raise RuntimeError(self.sanitize_error(exc, config)) from exc
            if not hasattr(api, "list_profit_loss"):
                return {"ok": False, "error": "此版本 Shioaji 沒有 list_profit_loss API", "records": [], "count": 0}
            try:
                records = api.list_profit_loss(account, begin, end)
            except TypeError:
                # 某些 SDK 版本用關鍵字參數
                records = api.list_profit_loss(account, begin_date=begin, end_date=end)
            raw = []
            for r in (records or []):
                try:
                    raw.append(self.model_to_dict(r))
                except Exception:
                    raw.append({"raw": str(r)})
            return {
                "ok": True,
                "accountMasked": self.mask_account(account),
                "begin": str(begin),
                "end": str(end),
                "count": len(raw),
                "records": raw,
            }
        finally:
            try:
                api.logout()
            except Exception:
                pass

    def quotes_direct(self, codes):
        config = self.load_config()
        if not config.get("apiKey") or not config.get("secretKey"):
            raise RuntimeError("Sinopac API Key / Secret Key is not configured")
        try:
            import shioaji as sj
        except Exception as exc:
            raise RuntimeError(f"Shioaji is not available: {exc}") from exc
        api = sj.Shioaji(simulation=bool(config.get("simulation", False)))
        try:
            api.login(
                api_key=config["apiKey"],
                secret_key=config["secretKey"],
                fetch_contract=True,
                subscribe_trade=False,
            )
            quotes, snapshot_error = self.stock_snapshots(api, codes)
            # 2026-07-09 大盤即時(走A):併進這次已登入的 api 抓加權指數快照,零額外登入。
            # 抓不到回 None,呼叫端 fallback Yahoo,不退化。
            taiex = self.index_snapshot(api)
            return {
                "ok": True,
                "simulation": bool(config.get("simulation", False)),
                "count": len(quotes),
                "quotes": quotes,
                "taiex": taiex,
                "snapshotError": snapshot_error or "",
            }
        finally:
            try:
                api.logout()
            except Exception:
                pass

    def scanners_direct(self, count=200):
        config = self.load_config()
        if not config.get("apiKey") or not config.get("secretKey"):
            raise RuntimeError("Sinopac API Key / Secret Key is not configured")
        try:
            import shioaji as sj
        except Exception as exc:
            raise RuntimeError(f"Shioaji is not available: {exc}") from exc
        api = sj.Shioaji(simulation=bool(config.get("simulation", False)))
        try:
            api.login(
                api_key=config["apiKey"],
                secret_key=config["secretKey"],
                fetch_contract=False,
                subscribe_trade=False,
            )
            return self.stock_scanners(api, count=count)
        finally:
            try:
                api.logout()
            except Exception:
                pass

    def index_snapshot(self, api):
        """用同一個已登入 api 抓加權指數(TSE 001)快照,併進 quotes_direct 那次登入、零
        額外登入。回傳 {price, referencePrice, changeRate, at} 或 None。指數合約路徑
        Contracts.Indexs.TSE['001'] 無法在無 Shioaji 登入的環境驗證,任何例外都回 None,
        讓 fetch_taiex_live fallback 回 Yahoo(最壞=維持現狀不退化)。"""
        try:
            contract = None
            for getter in (
                lambda: api.Contracts.Indexs.TSE["001"],
                lambda: api.Contracts.Indexs.TSE.get("001"),
                lambda: api.Contracts.Indexs["TSE"]["001"],
            ):
                try:
                    contract = getter()
                except Exception:
                    contract = None
                if contract is not None:
                    break
            if contract is None:
                return None
            snaps = api.snapshots([contract])
            if not snaps:
                return None
            data = self.model_to_dict(snaps[0])
            price = self.safe_float(data.get("close"))
            if price is None or price <= 0:
                return None
            reference = self.safe_float(data.get("reference"))
            change_rate = self.safe_float(data.get("change_rate"))
            if change_rate is None and reference and reference > 0:
                change_rate = (price / reference - 1) * 100
            return {
                "price": round(price, 2),
                "referencePrice": reference,
                "changeRate": None if change_rate is None else round(change_rate, 2),
                "at": time.strftime("%Y-%m-%d %H:%M:%S"),
            }
        except Exception:
            return None

    def sanitize_error(self, exc, config):
        message = self.sanitize_text(str(exc), config)
        if "not exist" in message:
            return f"永豐 API Key 不存在或不屬於此帳號：{message}"
        return message

    def sanitize_text(self, message, config):
        # caPassword/personId 都是身分證字號等級的機密，跟 apiKey/secretKey
        # 一樣不能出現在任何錯誤訊息/測試結果裡(這些文字會直接顯示在前端、
        # 寫進 log)。len>=6 防呆：太短的值直接 replace 容易誤傷訊息裡剛好
        # 相同的無關數字片段。
        message = str(message or "")
        for key in ("apiKey", "secretKey", "caPassword", "personId"):
            value = str(config.get(key, ""))
            if value and len(value) >= 6:
                masked = mask_person_id(value) if key in ("caPassword", "personId") else mask_secret(value)
                message = message.replace(value, masked or "***")
        return message

    def parse_child_json(self, output):
        for line in reversed((output or "").splitlines()):
            line = line.strip()
            if not line.startswith("{") or not line.endswith("}"):
                continue
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                continue
        return None

    def pick_stock_account(self, accounts):
        for account in accounts or []:
            text = str(account)
            if "Stock" in text or "證券" in text:
                return account
        return (accounts or [None])[0]

    def mask_account(self, account):
        account_id = str(getattr(account, "account_id", "") or getattr(account, "person_id", "") or account or "")
        return mask_secret(account_id) or "已登入"

    def position_to_holding(self, position, unit_is_share=False):
        raw = self.model_to_dict(position)
        contract = raw.get("contract") if isinstance(raw.get("contract"), dict) else {}
        code = (
            raw.get("code")
            or raw.get("stock_id")
            or raw.get("id")
            or contract.get("code")
        )
        # 用 is not None 而非 or：quantity=0 表示「今日已賣出，持倉清空」，
        # 不能落到 yd_quantity（昨日持有量）；否則已平倉的部位會被誤報為持有。
        def first_not_none(*candidates):
            for v in candidates:
                if v is not None:
                    return v
            return None
        quantity = first_not_none(
            raw.get("quantity"),
            raw.get("yd_quantity"),
            raw.get("today_quantity"),
            raw.get("qty"),
            0,
        )
        # price=0 對上市股票不可能是真實成交價，用 or 回落合理
        price = raw.get("price") or raw.get("avg_price") or raw.get("last_price") or 0
        # pnl=0.0 是合法的損益平衡值，不能用 or 讓它落到下一個欄位
        pnl = first_not_none(raw.get("pnl"), raw.get("profit_loss"), raw.get("unrealized_pnl"))
        direction = raw.get("direction") or raw.get("order_cond") or raw.get("cond")
        # unit=Unit.Share 查詢回來的 quantity 語意是「股」，不是「張」——
        # 跟 realtime_tick_collector.py 的 intraday_odd 是同一種單位混淆
        # 陷阱，不能無論查詢用哪個 unit 都套用同一個 ×1000 換算。
        shares = int(quantity or 0) if unit_is_share else int(quantity or 0) * 1000
        return {
            "code": self.clean_code(code),
            "quantity": shares / 1000,
            "shares": shares,
            "quantitySource": "shioaji_share_unit_quantity" if unit_is_share else "shioaji_lot_quantity",
            "price": float(price or 0),
            "pnl": None if pnl is None else float(pnl),
            "direction": str(direction or ""),
        }

    def stock_snapshots(self, api, codes):
        """回傳 (snapshots_dict, error_str_or_None)。
        呼叫方應把 error 帶進 JSON payload 回傳給父程序，讓 server.py 可以記錄
        或顯示，而不是讓 quotes={} 靜默貌似正常無報價。
        """
        contracts = []
        for code in codes:
            contract = None
            try:
                contract = api.Contracts.Stocks.get(code)
            except Exception:
                contract = None
            if contract is None:
                try:
                    contract = api.Contracts.Stocks[code]
                except Exception:
                    contract = None
            if contract is not None:
                contracts.append(contract)
        if not contracts:
            return {}, None
        snapshots = []
        errors = []
        for offset in range(0, len(contracts), SHIOAJI_SNAPSHOT_BATCH_SIZE):
            chunk = contracts[offset:offset + SHIOAJI_SNAPSHOT_BATCH_SIZE]
            try:
                snapshots.extend(api.snapshots(chunk) or [])
            except Exception as exc:
                errors.append(
                    f"第 {offset + 1}-{offset + len(chunk)} 檔：{type(exc).__name__}"
                )
        if errors:
            import sys as _sys
            error_msg = "stock_snapshots 部分失敗：" + "；".join(errors)
            print(f"[sinopac_backend] {error_msg}", file=_sys.stderr, flush=True)
            if not snapshots:
                return {}, error_msg
        else:
            error_msg = None
        output = {}
        for snapshot in snapshots or []:
            data = self.model_to_dict(snapshot)
            code = self.clean_code(data.get("code"))
            if not code:
                continue
            snapshot_at = normalize_shioaji_snapshot_time(data.get("ts"))
            output[code] = {
                "currentPrice": self.safe_float(data.get("close")),
                "referencePrice": self.safe_float(data.get("reference")),
                "changePrice": self.safe_float(data.get("change_price")),
                "changeRate": self.safe_float(data.get("change_rate")),
                "openPrice": self.safe_float(data.get("open")),
                "highPrice": self.safe_float(data.get("high")),
                "lowPrice": self.safe_float(data.get("low")),
                "bidPrice": self.safe_float(data.get("buy_price") or data.get("bid_price")),
                "askPrice": self.safe_float(data.get("sell_price") or data.get("ask_price")),
                "bidVolume": self.safe_float(data.get("buy_volume") or data.get("bid_volume")),
                "askVolume": self.safe_float(data.get("sell_volume") or data.get("ask_volume")),
                "totalVolume": self.safe_float(data.get("total_volume")),
                "totalVolumeUnit": "lots",
                "isSuspended": bool(data.get("suspend")),
                "simtrade": bool(data.get("simtrade")),
                "source": "sinopac_shioaji_rotation",
                "snapshotAt": snapshot_at,
                "receivedAt": dt.datetime.now(TAIPEI_TZ).isoformat(),
            }
        return output, error_msg

    def account_balance_payload(self, api, account, config=None):
        try:
            balance = api.account_balance(account)
        except Exception as exc:
            # 這裡跟本檔案其他所有下單/查詢路徑一樣要過 sanitize_error——
            # SDK 內部例外訊息偶爾會夾帶帳號等診斷資訊，沒遮罩會直接透傳
            # 到 /api/sinopac/holdings 的 HTTP 回應與瀏覽器 DevTools 網路紀錄。
            return {"ok": False, "error": self.sanitize_error(exc, config or {})}
        data = self.model_to_dict(balance)
        available_cash, available_cash_source = self.first_number_with_key(data, [
            "acc_balance",
            "available_balance",
            "available_cash",
            "availableCash",
            "bank_balance",
            "bankBalance",
            "cash_balance",
            "cashBalance",
            "balance",
            "available",
            "cash",
            "money",
            "withdrawable",
            "withdrawable_cash",
            "withdrawableCash",
            "settlement_available",
            "settlementAvailable",
            "after_settlement_available",
            "afterSettlementAvailable",
        ])
        return {
            "ok": True,
            "raw": self.json_safe(data),
            "availableCash": available_cash,
            "availableCashSource": available_cash_source or "",
            "rawKeys": sorted(str(key) for key in data.keys()),
            "updatedAt": str(data.get("date") or data.get("update_time") or data.get("updated_at") or ""),
        }

    def settlements_payload(self, api, account, config=None):
        # 2026-07-03 實測：shioaji 1.3.3 的 list_settlements 在編譯層
        # (solace api.pyx list_settlements) 對 pydantic 2.12 直接拋
        # "dictionary update sequence element #0 has length 6; 2 is required"
        # ——SDK 內部 bug，跟永豐有沒有回資料無關，之前介面因此永遠顯示
        # 「無資料(永豐未回傳)」。同樣官方的 api.settlements(account) 實測
        # 正常(回傳 SettlementV1(date/amount/T) 清單，T=0/1/2 三天)，改以
        # 它為主，list_settlements 降為備援(未來 SDK 修好或棄用舊 API 時
        # 仍有路可走)。
        try:
            settlements = api.settlements(account)
        except Exception as exc:
            first_error = self.sanitize_error(exc, config or {})
            try:
                settlements = api.list_settlements(account)
            except Exception as fallback_exc:
                fallback_error = self.sanitize_error(fallback_exc, config or {})
                return {
                    "ok": False,
                    "error": "永豐交割資料讀取失敗",
                    "rawError": f"settlements: {first_error}; list_settlements fallback: {fallback_error}",
                    "items": [],
                    "total": None,
                }
        today = time.strftime("%Y-%m-%d")  # 本地(台北)交易日
        items = []
        total = 0.0
        pending_total = 0.0
        for settlement in settlements or []:
            data = self.model_to_dict(settlement)
            amount, amount_source = self.first_number_with_key(data, [
                "amount",
                "amount_sum",
                "settlementAmount",
                "settlement_amount",
                "settlement_money",
                "money",
                "balance",
                "net_amount",
                "buy_sell_amount",
                "amt",
                "T",
                "t",
                "T1",
                "T2",
                "TDateMoney",
            ])
            date_str = str(data.get("date") or data.get("TDate") or data.get("settlement_date") or "")[:10]
            if amount is not None:
                total += amount
                # 只有「未來還沒交割」(交割日 > 今天)才算未交割待扣。今天(T=0)/過去的那筆
                # 早上就交割完、已反映在 acc_balance;若再從可用餘額扣一次會重複計——這正是
                # 「帳戶總值少了一整筆交割款」的成因(2026-07-06 修)。total 保留全部供對照。
                if date_str and date_str > today:
                    pending_total += amount
            items.append({
                "date": date_str,
                "amount": amount,
                "amountSource": amount_source or "",
                # SettlementV1.model_dump() 的 date 是 datetime.date 物件，
                # 直接塞 raw 會讓子行程的 json.dumps 炸 "not JSON serializable"
                # ——非基本型別一律轉字串。
                "raw": {
                    key: (value if isinstance(value, (int, float, str, bool, type(None))) else str(value))
                    for key, value in data.items()
                },
            })
        return {"ok": True, "items": items, "total": total, "pendingTotal": pending_total, "count": len(items)}

    def safe_float(self, value):
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        return number if number == number else None


    def first_number_with_key(self, data, keys):
        for key in keys:
            value = self.safe_float(data.get(key))
            if value is not None:
                return value, key
        return None, None

    def model_to_dict(self, value):
        if isinstance(value, dict):
            return value
        if hasattr(value, "_asdict"):
            try:
                return dict(value._asdict())
            except Exception:
                pass
        if hasattr(value, "model_dump"):
            try:
                data = value.model_dump()
                if isinstance(data, dict):
                    return data
            except Exception:
                pass
        if hasattr(value, "dict"):
            try:
                data = value.dict()
                if isinstance(data, dict):
                    return data
            except Exception:
                pass
        raw = getattr(value, "__dict__", None)
        if isinstance(raw, dict):
            return dict(raw)
        if isinstance(value, (list, tuple)):
            return {f"item_{index}": item for index, item in enumerate(value)}
        return {"raw": str(value)}

    def clean_code(self, value):
        return "".join(ch for ch in str(value or "") if ch.isdigit())


sinopac_backend = SinoPacBackend()


def main():
    try:
        if "--holdings-direct" in sys.argv:
            # 選填第2個參數=已實現損益回溯天數(交易複盤自動化,同一次登入順便抓)
            _idx = sys.argv.index("--holdings-direct")
            _realized_days = None
            if len(sys.argv) > _idx + 1:
                try:
                    _realized_days = int(sys.argv[_idx + 1])
                except (ValueError, TypeError):
                    _realized_days = None
            payload = SinoPacBackend().holdings_direct(realized_days=_realized_days)
        elif "--quotes-direct" in sys.argv:
            index = sys.argv.index("--quotes-direct")
            codes = sys.argv[index + 1].split(",") if len(sys.argv) > index + 1 else []
            payload = SinoPacBackend().quotes_direct(codes)
        elif "--scanners-direct" in sys.argv:
            index = sys.argv.index("--scanners-direct")
            count = int(sys.argv[index + 1]) if len(sys.argv) > index + 1 else 200
            payload = SinoPacBackend().scanners_direct(count=count)
        elif "--order-direct" in sys.argv:
            raw = sys.stdin.read()
            order = json.loads(raw or "{}")
            payload = SinoPacBackend().order_direct(order)
        elif "--order-fills-direct" in sys.argv:
            payload = SinoPacBackend().order_fills_direct()
        elif "--position-details-direct" in sys.argv:
            payload = SinoPacBackend().position_details_direct()
        elif "--profit-loss-direct" in sys.argv:
            index = sys.argv.index("--profit-loss-direct")
            begin = sys.argv[index + 1] if len(sys.argv) > index + 1 else ""
            end = sys.argv[index + 2] if len(sys.argv) > index + 2 else ""
            payload = SinoPacBackend().profit_loss_direct(begin, end)
        else:
            return 0
        print(json.dumps(payload, ensure_ascii=True))
        return 0
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=True))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
