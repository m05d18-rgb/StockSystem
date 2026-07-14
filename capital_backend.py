"""群益策略王 COM 元件的唯讀健康檢查與實際報價驗證。"""
from datetime import datetime, time as dt_time, timedelta, timezone
from pathlib import Path
import hashlib
import json
import os
import subprocess
import sys
import threading
import time


ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "capital_api.json"
HEALTH_PATH = ROOT / "capital_quote_health.json"
QUOTE_PROBE_PATH = ROOT / "capital_quote_probe.ps1"
CAPITAL_HOME = Path(os.environ.get("CAPITAL_API_HOME", r"C:\CapitalAPI"))
SKCOM_PATH = CAPITAL_HOME / "SKCOM.dll"

# 策略王 API 2.13.58 x64 TypeLib 的已註冊 coclass CLSID。此版本沒有舊版
# ProgID，必須用 CLSID 建立；不要猜 SKCOMLib.SKQuoteLib 這類舊名稱。
CENTER_CLSID = "{AC30BAB5-194A-4515-A8D3-6260749F8577}"
QUOTE_CLSID = "{E7BCB8BB-E1F0-4F6F-A944-2679195E5807}"
CAPITAL_TEST_LOCK = threading.Lock()
TAIPEI_TZ = timezone(timedelta(hours=8))
CAPITAL_MAX_CLOSED_MARKET_AGE_DAYS = 7
CAPITAL_MAX_INTRADAY_QUOTE_AGE_SECONDS = 180
CAPITAL_QUOTE_CACHE_TTL_SECONDS = 90
CAPITAL_CIRCUIT_FAILURE_THRESHOLD = 3
CAPITAL_CIRCUIT_COOLDOWN_SECONDS = 120


def mask_user_id(value):
    value = str(value or "").strip()
    if len(value) < 4:
        return ""
    return f"{value[:2]}{'*' * (len(value) - 4)}{value[-2:]}"


def mask_account_no(value):
    value = str(value or "").strip()
    if len(value) < 4:
        return ""
    return f"{'*' * (len(value) - 3)}{value[-3:]}"


class CapitalBackend:
    def __init__(self, config_path=CONFIG_PATH, component_path=SKCOM_PATH, health_path=HEALTH_PATH):
        self.config_path = Path(config_path)
        self.component_path = Path(component_path)
        self.health_path = Path(health_path)
        self._quote_cache = {}
        self._quote_cache_lock = threading.Lock()
        self._live_quotes_lock = threading.Lock()
        self._probe_failures = 0
        self._circuit_open_until = 0.0

    def load_config(self):
        if not self.config_path.exists():
            return {}
        try:
            data = json.loads(self.config_path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def save_config(self, payload):
        user_id = str((payload or {}).get("userId") or "").strip()
        password = str((payload or {}).get("password") or "").strip()
        account_no = str((payload or {}).get("accountNo") or "").strip()
        if len(user_id) < 2 or len(password) < 2 or len(account_no) < 4:
            raise ValueError("請輸入群益登入帳號、登入密碼與證券帳號")
        temp_path = self.config_path.with_name(f"{self.config_path.name}.tmp")
        temp_path.write_text(
            json.dumps(
                {"userId": user_id, "password": password, "accountNo": account_no},
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        os.replace(temp_path, self.config_path)
        self._clear_health()
        return self.status()

    def clear_config(self):
        if self.config_path.exists():
            self.config_path.unlink()
        self._clear_health()
        return self.status()

    @staticmethod
    def _credential_key(user_id, account_no=""):
        identity = f"{str(user_id or '').strip()}|{str(account_no or '').strip()}"
        return hashlib.sha256(identity.encode("utf-8")).hexdigest()

    def _load_health(self):
        if not self.health_path.exists():
            return {}
        try:
            data = json.loads(self.health_path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def _clear_health(self):
        if self.health_path.exists():
            self.health_path.unlink()
        with self._quote_cache_lock:
            self._quote_cache.clear()
            self._probe_failures = 0
            self._circuit_open_until = 0.0

    def _save_health(self, config, result):
        quote = {
            "symbol": str(result.get("symbol") or ""),
            "stockName": str(result.get("stockName") or ""),
            "price": result.get("price"),
            "referencePrice": result.get("referencePrice"),
            "totalVolume": result.get("totalVolume"),
            "tradingDay": result.get("tradingDay"),
            "dealTime": result.get("dealTime"),
            "quoteTimestamp": str(result.get("quoteTimestamp") or ""),
            "source": str(result.get("source") or "Capital Strategy King COM"),
        }
        payload = {
            "verifiedAt": datetime.now(timezone.utc).isoformat(),
            "credentialKey": self._credential_key(config.get("userId"), config.get("accountNo")),
            "quote": quote,
        }
        temp_path = self.health_path.with_name(f"{self.health_path.name}.tmp")
        temp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temp_path, self.health_path)

    def _load_com_client(self):
        import win32com.client
        return win32com.client

    @staticmethod
    def _return_code_message(center, code):
        try:
            return str(center.SKCenterLib_GetReturnCodeMessage(int(code)) or "")
        except Exception:
            return ""

    def _run_child(self, mode):
        command = [sys.executable, str(Path(__file__).resolve()), mode]
        try:
            completed = subprocess.run(
                command, cwd=ROOT, capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=90,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return {"ok": False, "usable": False, "error": f"群益子程序失敗：{type(exc).__name__}"}
        lines = [line for line in completed.stdout.splitlines() if line.strip()]
        try:
            payload = json.loads(lines[-1]) if lines else None
        except json.JSONDecodeError:
            payload = None
        if not isinstance(payload, dict):
            return {"ok": False, "usable": False, "error": "群益子程序沒有回傳可讀結果"}
        return payload

    def _run_quote_probe(self, config, symbol="2330", symbols=None):
        if not QUOTE_PROBE_PATH.is_file():
            return {"ok": False, "usable": False, "error": "找不到群益實際報價探針"}
        powershell = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
        command = [
            str(powershell), "-NoProfile", "-STA", "-ExecutionPolicy", "Bypass",
            "-File", str(QUOTE_PROBE_PATH),
        ]
        requested_symbols = list(symbols or [])
        request_payload = {
            "userId": config.get("userId"),
            "password": config.get("password"),
        }
        if requested_symbols:
            request_payload["symbols"] = requested_symbols[:100]
        else:
            request_payload["symbol"] = symbol
        request = json.dumps(request_payload, ensure_ascii=False)
        environment = os.environ.copy()
        environment["CAPITAL_API_HOME"] = str(self.component_path.parent)
        try:
            completed = subprocess.run(
                command, cwd=ROOT, input=request, capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=70, env=environment,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return {"ok": False, "usable": False, "error": f"群益實際報價子程序失敗：{type(exc).__name__}"}
        lines = [line for line in completed.stdout.splitlines() if line.strip()]
        try:
            result = json.loads(lines[-1]) if lines else None
        except json.JSONDecodeError:
            result = None
        if not isinstance(result, dict):
            return {"ok": False, "usable": False, "error": "群益實際報價探針沒有回傳可讀結果"}
        if not result.get("usable") and str(result.get("error") or "").startswith("Capital"):
            result["error"] = "群益實際報價驗證失敗"
        return result

    def _is_verified_for_config(self, config):
        health = self._load_health()
        return bool(
            str(config.get("userId") or "").strip()
            and str(config.get("password") or "").strip()
            and str(config.get("accountNo") or "").strip()
            and health.get("credentialKey") == self._credential_key(config.get("userId"), config.get("accountNo"))
            and isinstance(health.get("quote"), dict)
            and health["quote"].get("symbol")
        )

    @staticmethod
    def _clean_codes(codes):
        clean = []
        for value in codes or []:
            code = "".join(ch for ch in str(value or "").strip() if ch.isalnum())
            if 2 <= len(code) <= 12 and code not in clean:
                clean.append(code)
        return clean[:100]

    @staticmethod
    def quote_datetime(quote):
        try:
            day = str(int((quote or {}).get("tradingDay") or 0)).zfill(8)
            deal = str(int((quote or {}).get("dealTime") or 0)).zfill(6)
            parsed = datetime.strptime(f"{day}{deal}", "%Y%m%d%H%M%S")
            return parsed.replace(tzinfo=TAIPEI_TZ)
        except (TypeError, ValueError):
            return None

    @classmethod
    def quote_age_seconds(cls, quote, now=None):
        now = now or datetime.now(TAIPEI_TZ)
        if now.tzinfo is None:
            now = now.replace(tzinfo=TAIPEI_TZ)
        else:
            now = now.astimezone(TAIPEI_TZ)
        quote_at = cls.quote_datetime(quote)
        return (now - quote_at).total_seconds() if quote_at else None

    @classmethod
    def validate_quote_freshness(cls, code, quote, now=None):
        now = now or datetime.now(TAIPEI_TZ)
        if now.tzinfo is None:
            now = now.replace(tzinfo=TAIPEI_TZ)
        else:
            now = now.astimezone(TAIPEI_TZ)
        if str((quote or {}).get("code") or code) != str(code):
            return False, "symbol_mismatch"
        try:
            price = float((quote or {}).get("currentPrice") or 0)
        except (TypeError, ValueError):
            price = 0
        if price <= 0:
            return False, "invalid_current_price"
        try:
            trading_day = datetime.strptime(str(int((quote or {}).get("tradingDay") or 0)), "%Y%m%d").date()
        except (TypeError, ValueError):
            return False, "invalid_trading_day"
        age_days = (now.date() - trading_day).days
        if age_days < 0:
            return False, "future_trading_day"
        local_clock = now.replace(tzinfo=None).time()
        during_market_session = now.weekday() < 5 and dt_time(8, 30) <= local_clock <= dt_time(13, 45)
        if during_market_session and trading_day != now.date():
            return False, "not_today_during_market_session"
        if not during_market_session and age_days > CAPITAL_MAX_CLOSED_MARKET_AGE_DAYS:
            return False, "closed_market_quote_too_old"
        if during_market_session:
            try:
                deal_time = int((quote or {}).get("dealTime") or 0)
            except (TypeError, ValueError):
                deal_time = 0
            if deal_time <= 0 or deal_time > 235959:
                return False, "invalid_intraday_deal_time"
            age_seconds = cls.quote_age_seconds(quote, now=now)
            if age_seconds is None:
                return False, "invalid_intraday_quote_timestamp"
            if age_seconds < -30:
                return False, "future_intraday_quote_timestamp"
            if age_seconds > CAPITAL_MAX_INTRADAY_QUOTE_AGE_SECONDS:
                return False, "intraday_quote_too_old"
        return True, ""

    def _cached_live_quotes(self, codes, now=None):
        monotonic_now = time.monotonic()
        output = {}
        ages = []
        with self._quote_cache_lock:
            for code in codes:
                entry = self._quote_cache.get(code)
                age = monotonic_now - float((entry or {}).get("storedAt") or 0)
                if not entry or age > CAPITAL_QUOTE_CACHE_TTL_SECONDS:
                    continue
                quote = dict(entry.get("quote") or {})
                fresh, _reason = self.validate_quote_freshness(code, quote, now=now)
                if not fresh:
                    continue
                quote["cacheAgeSeconds"] = round(max(0.0, age), 2)
                quote["quoteAgeSeconds"] = self.quote_age_seconds(quote, now=now)
                output[code] = quote
                ages.append(age)
        return output, (max(ages) if ages else None)

    def _store_live_quote_cache(self, quotes):
        stored_at = time.monotonic()
        with self._quote_cache_lock:
            for code, quote in (quotes or {}).items():
                self._quote_cache[code] = {"storedAt": stored_at, "quote": dict(quote or {})}

    def _circuit_state(self):
        remaining = max(0.0, self._circuit_open_until - time.monotonic())
        return remaining > 0, remaining

    def _record_probe_result(self, success):
        with self._quote_cache_lock:
            if success:
                self._probe_failures = 0
                self._circuit_open_until = 0.0
                return
            self._probe_failures += 1
            if self._probe_failures >= CAPITAL_CIRCUIT_FAILURE_THRESHOLD:
                self._circuit_open_until = time.monotonic() + CAPITAL_CIRCUIT_COOLDOWN_SECONDS

    def live_quotes(self, codes, now=None):
        """序列化完整的快取檢查→登入→正規化→寫回，避免併發重複登入。"""
        if not self._live_quotes_lock.acquire(timeout=60):
            return {
                "ok": False, "usable": False, "quotes": {}, "count": 0,
                "source": "Capital Strategy King COM", "error": "群益報價更新正在執行中",
            }
        try:
            return self._live_quotes_locked(codes, now=now)
        finally:
            self._live_quotes_lock.release()

    def _live_quotes_locked(self, codes, now=None):
        """取得最多 100 檔已驗證的群益即時報價；不符合新鮮度者不回傳。"""
        clean_codes = self._clean_codes(codes)
        if not clean_codes:
            return {"ok": True, "usable": True, "quotes": {}, "count": 0, "source": "Capital Strategy King COM"}
        config = self.load_config()
        if not self._is_verified_for_config(config):
            return {
                "ok": False, "usable": False, "quotes": {}, "count": 0,
                "source": "Capital Strategy King COM", "error": "群益報價尚未通過實際股票驗證",
            }
        if os.name != "nt" or not self.component_path.is_file():
            return {
                "ok": False, "usable": False, "quotes": {}, "count": 0,
                "source": "Capital Strategy King COM", "error": "群益 COM 元件不可用",
            }
        cached_quotes, cache_age = self._cached_live_quotes(clean_codes, now=now)
        missing_codes = [code for code in clean_codes if code not in cached_quotes]
        if not missing_codes:
            return {
                "ok": True, "usable": True, "quotes": cached_quotes, "count": len(cached_quotes),
                "requested": len(clean_codes), "missingSymbols": [], "rejectedSymbols": {},
                "source": "Capital Strategy King COM", "stale": False, "cached": True,
                "cacheAgeSeconds": round(cache_age or 0, 2), "circuitOpen": False, "error": "",
            }
        circuit_open, retry_seconds = self._circuit_state()
        if circuit_open:
            return {
                "ok": bool(cached_quotes), "usable": bool(cached_quotes), "quotes": cached_quotes,
                "count": len(cached_quotes), "requested": len(clean_codes),
                "missingSymbols": missing_codes, "rejectedSymbols": {},
                "source": "Capital Strategy King COM", "stale": False,
                "cached": bool(cached_quotes), "circuitOpen": True,
                "circuitRetrySeconds": round(retry_seconds, 1),
                "error": "群益報價暫停重連，等待斷路器冷卻",
            }
        if not CAPITAL_TEST_LOCK.acquire(timeout=45):
            return {
                "ok": False, "usable": False, "quotes": {}, "count": 0,
                "source": "Capital Strategy King COM", "error": "群益報價連線正在使用中",
            }
        try:
            # 等鎖期間另一個請求可能已完成同一批報價，拿鎖後先重查快取，
            # 避免兩個 HTTP thread 依序重複登入群益。
            refreshed_cache, refreshed_age = self._cached_live_quotes(missing_codes, now=now)
            cached_quotes.update(refreshed_cache)
            missing_codes = [code for code in clean_codes if code not in cached_quotes]
            if missing_codes:
                result = self._run_quote_probe(config, symbols=missing_codes)
            else:
                result = {"ok": True, "usable": True, "quotes": {}, "cached": True}
        finally:
            CAPITAL_TEST_LOCK.release()
        raw_quotes = result.get("quotes") if isinstance(result.get("quotes"), dict) else {}
        quotes = {}
        rejected = {}
        for code in missing_codes:
            quote = raw_quotes.get(code)
            if not isinstance(quote, dict):
                rejected[code] = "missing_quote"
                continue
            if quote.get("eventConfirmed") is not True:
                rejected[code] = "quote_event_not_confirmed"
                continue
            fresh, reason = self.validate_quote_freshness(code, quote, now=now)
            if not fresh:
                rejected[code] = reason
                continue
            # 群益原始 COM 欄位是 open/high/low，妖股盤中與永豐報價契約則
            # 使用 openPrice/highPrice/lowPrice。若不在資料源邊界正規化，
            # 切到群益時開高走低、貼近高點、V轉與點火判斷會把 OHLC 當缺值。
            quotes[code] = {
                **quote,
                "code": code,
                "currentPrice": quote.get("currentPrice"),
                "openPrice": quote.get("openPrice") if quote.get("openPrice") is not None else quote.get("open"),
                "highPrice": quote.get("highPrice") if quote.get("highPrice") is not None else quote.get("high"),
                "lowPrice": quote.get("lowPrice") if quote.get("lowPrice") is not None else quote.get("low"),
                "snapshotAt": quote.get("snapshotAt") or quote.get("quoteTimestamp") or quote.get("receivedAt") or "",
                "quoteAgeSeconds": self.quote_age_seconds(quote, now=now),
                "source": "Capital Strategy King COM",
                "fresh": True,
                "stale": False,
            }
        self._record_probe_result(bool(quotes) or not missing_codes)
        self._store_live_quote_cache(quotes)
        merged_quotes = {**cached_quotes, **quotes}
        missing = [code for code in clean_codes if code not in merged_quotes]
        usable = bool(merged_quotes)
        circuit_open, retry_seconds = self._circuit_state()
        return {
            **result,
            "ok": usable,
            "usable": usable,
            "quotes": merged_quotes,
            "count": len(merged_quotes),
            "requested": len(clean_codes),
            "missingSymbols": missing,
            "rejectedSymbols": rejected,
            "source": "Capital Strategy King COM",
            "stale": False,
            "cached": bool(cached_quotes),
            "cacheAgeSeconds": round(cache_age or refreshed_age or 0, 2) if cached_quotes else None,
            "circuitOpen": circuit_open,
            "circuitRetrySeconds": round(retry_seconds, 1) if circuit_open else 0,
            "error": "" if usable else (result.get("error") or "群益沒有通過新鮮度檢查的報價"),
        }

    def status(self):
        # 群益 DLL 在 HTTP worker thread 的 COM apartment 與 DLL 卸載階段會
        # 觸發 native access violation。所有 Web request 改在短生命週期 child
        # 的主執行緒執行；主服務只接收 JSON，避免單一設定頁請求拖垮 server。
        if threading.current_thread() is not threading.main_thread():
            return self._run_child("--status-direct")
        return self._status_direct()

    def _status_direct(self):
        config = self.load_config()
        configured = bool(
            str(config.get("userId") or "").strip()
            and str(config.get("password") or "").strip()
            and str(config.get("accountNo") or "").strip()
        )
        health = self._load_health()
        quote_verified = self._is_verified_for_config(config)
        result = {
            "ok": True,
            "provider": "Capital Strategy King COM",
            "dataScope": "market_quotes_only",
            "configured": configured,
            "userIdMasked": mask_user_id(config.get("userId")),
            "accountNoMasked": mask_account_no(config.get("accountNo")),
            "componentPathExists": self.component_path.is_file(),
            "comReady": False,
            "quoteConnectionCode": None,
            "quoteConnectionMessage": "",
            "quoteVerified": quote_verified,
            "quoteVerifiedAt": health.get("verifiedAt") if quote_verified else "",
            "lastVerifiedQuote": health.get("quote") if quote_verified else None,
            "readyForFailover": False,
            "fallbackCapabilities": {
                "marketQuotes": {"mode": "fallback", "enabled": quote_verified},
                "holdingMarketPrices": {"mode": "fallback", "enabled": quote_verified},
                "brokerHoldings": {"mode": "disabled", "enabled": False, "reason": "capital_quote_only"},
                "brokerFills": {"mode": "disabled", "enabled": False, "reason": "capital_quote_only"},
                "brokerBalance": {"mode": "disabled", "enabled": False, "reason": "capital_quote_only"},
                "brokerSettlements": {"mode": "disabled", "enabled": False, "reason": "capital_quote_only"},
                "orderExecution": {"mode": "never_auto_fallback", "enabled": False},
            },
            "reason": "群益台股報價尚未完成登入與實際快照驗證，未納入備援。",
        }
        if os.name != "nt":
            result["reason"] = "群益 COM 元件僅支援 Windows。"
            return result
        if not result["componentPathExists"]:
            result["reason"] = "找不到已註冊的群益 SKCOM.dll。"
            return result
        try:
            client = self._load_com_client()
            center = client.Dispatch(CENTER_CLSID)
            quote = client.Dispatch(QUOTE_CLSID)
            result["comReady"] = True
            result["readyForFailover"] = quote_verified
            # IsConnected 是目前未登入時唯一安全的連線探針；不可在 status API
            # 中呼叫 EnterMonitor/Login，避免單純開網頁就啟動外部行情連線。
            code = quote.SKQuoteLib_IsConnected()
            result["quoteConnectionCode"] = int(code) if code is not None else None
            result["quoteConnectionMessage"] = self._return_code_message(center, code)
            if not configured:
                result["reason"] = "群益 COM 元件可用，但尚未完整設定登入帳號、密碼與證券帳號。"
            elif quote_verified:
                result["reason"] = "群益實際股票報價已驗證，永豐報價失敗或缺漏時可自動備援。"
            elif int(code or 0) != 0:
                result["reason"] = "群益登入資料已設定，但尚未建立可驗證的台股報價連線。"
        except Exception as exc:
            result["reason"] = f"群益 COM 元件無法建立：{type(exc).__name__}"
        return result

    def test_connection(self):
        """使用者主動按下測試時才登入並確認報價連線，絕不送出委託。"""
        if threading.current_thread() is not threading.main_thread():
            if not CAPITAL_TEST_LOCK.acquire(blocking=False):
                return {"ok": False, "configured": True, "usable": False, "error": "群益連線測試正在執行"}
            try:
                return self._run_child("--test-direct")
            finally:
                CAPITAL_TEST_LOCK.release()
        return self._test_connection_direct()

    def _test_connection_direct(self):
        config = self.load_config()
        if not all(str(config.get(key) or "").strip() for key in ("userId", "password", "accountNo")):
            return {
                "ok": False, "configured": False, "usable": False,
                "error": "尚未完整設定群益登入帳號、登入密碼與證券帳號",
            }
        if os.name != "nt" or not self.component_path.is_file():
            return {"ok": False, "configured": True, "usable": False, "error": "群益 COM 元件不可用"}
        if not CAPITAL_TEST_LOCK.acquire(blocking=False):
            return {"ok": False, "configured": True, "usable": False, "error": "群益連線測試正在執行"}
        try:
            # 每次重測先撤銷舊驗證；只有本次真的收到股票報價事件後才重新啟用。
            self._clear_health()
            result = self._run_quote_probe(config, symbol="2330")
            result["accountNoMasked"] = mask_account_no(config.get("accountNo"))
            event_confirmed = result.get("quoteEventConfirmed") is True
            if result.get("ok") and result.get("usable") and event_confirmed:
                self._save_health(config, result)
            elif result.get("ok") and result.get("usable"):
                result.update({
                    "ok": False,
                    "usable": False,
                    "error": "群益尚未收到實際股票報價事件",
                })
            return result
        finally:
            CAPITAL_TEST_LOCK.release()


capital_backend = CapitalBackend()


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    if mode == "--status-direct":
        print(json.dumps(capital_backend._status_direct(), ensure_ascii=False))
    elif mode == "--test-direct":
        print(json.dumps(capital_backend._test_connection_direct(), ensure_ascii=False))
    else:
        raise SystemExit(2)
