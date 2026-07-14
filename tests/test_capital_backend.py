"""群益 COM 備援環境檢查的回歸測試。"""
import json
import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import capital_backend as capital_module
from capital_backend import CapitalBackend


class _Center:
    def __init__(self, login_code=0):
        self.login_code = login_code

    def SKCenterLib_GetReturnCodeMessage(self, code):
        return "not connected" if code else "success"

    def SKCenterLib_Login(self, _user_id, _password):
        return self.login_code


class _Quote:
    def __init__(self, connection_code=1000):
        self.connection_code = connection_code
        self.entered = False
        self.left = False

    def SKQuoteLib_IsConnected(self):
        return self.connection_code

    def SKQuoteLib_EnterMonitor(self):
        self.entered = True
        return 0

    def SKQuoteLib_LeaveMonitor(self):
        self.left = True
        return 0


class _Client:
    def __init__(self, login_code=0, connection_code=1000):
        self.center = _Center(login_code)
        self.quote = _Quote(connection_code)

    def Dispatch(self, clsid):
        if clsid == capital_module.CENTER_CLSID:
            return self.center
        if clsid == capital_module.QUOTE_CLSID:
            return self.quote
        raise AssertionError(f"unexpected CLSID {clsid}")


class CapitalBackendStatusTests(unittest.TestCase):
    def _backend(self, configured=False):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        config = root / "capital_api.json"
        component = root / "SKCOM.dll"
        component.write_bytes(b"test")
        if configured:
            config.write_text(
                json.dumps({"userId": "user", "password": "secret", "accountNo": "9876543"}),
                encoding="utf-8",
            )
        return CapitalBackend(
            config_path=config,
            component_path=component,
            health_path=root / "capital_quote_health.json",
        )

    def test_unconfigured_com_ready_state_never_enables_failover(self):
        backend = self._backend(configured=False)
        with patch.object(backend, "_load_com_client", return_value=_Client()):
            result = backend._status_direct()
        self.assertTrue(result["comReady"])
        self.assertFalse(result["configured"])
        self.assertFalse(result["readyForFailover"])
        self.assertEqual(result["dataScope"], "market_quotes_only")
        self.assertEqual(result["fallbackCapabilities"]["brokerHoldings"]["mode"], "disabled")
        self.assertEqual(result["fallbackCapabilities"]["brokerHoldings"]["reason"], "capital_quote_only")
        self.assertEqual(result["fallbackCapabilities"]["orderExecution"]["mode"], "never_auto_fallback")
        self.assertEqual(result["quoteConnectionCode"], 1000)
        self.assertNotIn("secret", json.dumps(result))

    def test_configured_but_disconnected_state_still_blocks_failover(self):
        backend = self._backend(configured=True)
        with patch.object(backend, "_load_com_client", return_value=_Client()):
            result = backend._status_direct()
        self.assertTrue(result["configured"])
        self.assertTrue(result["comReady"])
        self.assertFalse(result["readyForFailover"])
        self.assertIn("尚未建立", result["reason"])

    def test_missing_component_is_reported_without_loading_com(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        backend = CapitalBackend(
            config_path=Path(directory.name) / "capital_api.json",
            component_path=Path(directory.name) / "missing.dll",
        )
        with patch.object(backend, "_load_com_client") as load_com:
            result = backend.status()
        self.assertFalse(result["comReady"])
        self.assertFalse(result["readyForFailover"])
        load_com.assert_not_called()

    def test_successful_manual_test_requires_login_and_connected_quote(self):
        backend = self._backend(configured=True)
        quote_result = {
            "ok": True,
            "configured": True,
            "usable": True,
            "loginCode": 0,
            "monitorCode": 0,
            "quoteConnectionCode": 0,
            "symbol": "2330",
            "stockName": "台積電",
            "price": 2490.0,
            "referencePrice": 2460.0,
            "totalVolume": 12345,
            "tradingDay": 20260710,
            "dealTime": 133000,
            "quoteTimestamp": "20260710 133000",
            "source": "Capital Strategy King COM",
            "quoteEventConfirmed": True,
        }
        with patch.object(backend, "_run_quote_probe", return_value=quote_result):
            result = backend._test_connection_direct()
        self.assertTrue(result["ok"])
        self.assertTrue(result["usable"])
        status_health = backend._load_health()
        self.assertEqual(status_health["quote"]["symbol"], "2330")
        self.assertNotIn("secret", json.dumps(status_health, ensure_ascii=False))
        with patch.object(backend, "_load_com_client", return_value=_Client()):
            status = backend._status_direct()
        self.assertTrue(status["quoteVerified"])
        self.assertTrue(status["readyForFailover"])
        self.assertTrue(status["fallbackCapabilities"]["marketQuotes"]["enabled"])
        self.assertTrue(status["fallbackCapabilities"]["holdingMarketPrices"]["enabled"])
        self.assertNotIn("capitalAccountSnapshot", status["fallbackCapabilities"])
        self.assertFalse(hasattr(backend, "account_snapshot"))
        self.assertEqual(result["accountNoMasked"], "****543")

    def test_failed_login_never_starts_quote_monitor(self):
        backend = self._backend(configured=True)
        failed = {
            "ok": False,
            "configured": True,
            "usable": False,
            "loginCode": 1000,
            "error": "群益登入失敗",
        }
        with patch.object(backend, "_run_quote_probe", return_value=failed):
            result = backend._test_connection_direct()
        self.assertFalse(result["usable"])
        self.assertEqual(result["loginCode"], 1000)
        self.assertFalse(backend.health_path.exists())

    def test_connected_without_real_quote_is_not_usable_or_persisted(self):
        backend = self._backend(configured=True)
        no_quote = {
            "ok": False,
            "configured": True,
            "usable": False,
            "loginCode": 0,
            "quoteConnectionCode": 0,
            "error": "群益連線成功，但未收到有效股票報價",
        }
        with patch.object(backend, "_run_quote_probe", return_value=no_quote):
            result = backend._test_connection_direct()
        self.assertFalse(result["usable"])
        self.assertFalse(backend.health_path.exists())

    def test_previous_trading_day_is_rejected_during_market_session(self):
        quote = {"code": "2330", "currentPrice": 100, "tradingDay": 20260709, "dealTime": 133000}
        fresh, reason = CapitalBackend.validate_quote_freshness(
            "2330", quote, now=datetime.fromisoformat("2026-07-10T10:00:00+08:00")
        )
        self.assertFalse(fresh)
        self.assertEqual(reason, "not_today_during_market_session")

    def test_same_day_quote_with_deal_time_is_fresh_during_market_session(self):
        quote = {"code": "2330", "currentPrice": 100, "tradingDay": 20260710, "dealTime": 95959}
        fresh, reason = CapitalBackend.validate_quote_freshness(
            "2330", quote, now=datetime.fromisoformat("2026-07-10T10:00:00+08:00")
        )
        self.assertTrue(fresh)
        self.assertEqual(reason, "")

    def test_same_day_but_old_deal_time_is_rejected_during_market_session(self):
        quote = {"code": "2330", "currentPrice": 100, "tradingDay": 20260710, "dealTime": 93000}
        fresh, reason = CapitalBackend.validate_quote_freshness(
            "2330", quote, now=datetime.fromisoformat("2026-07-10T10:00:00+08:00")
        )
        self.assertFalse(fresh)
        self.assertEqual(reason, "intraday_quote_too_old")

    def test_live_quotes_filters_stale_symbols_from_batch(self):
        backend = self._backend(configured=True)
        backend._save_health(backend.load_config(), {
            "symbol": "2330", "stockName": "台積電", "price": 100,
            "tradingDay": 20260710, "dealTime": 93000,
        })
        probe_result = {
            "ok": True,
            "usable": True,
            "quotes": {
                "2330": {
                    "code": "2330", "currentPrice": 100, "open": 98, "high": 101, "low": 97,
                    "tradingDay": 20260710, "dealTime": 95959,
                    "quoteTimestamp": "2026-07-10 09:59:59",
                },
                "2303": {"code": "2303", "currentPrice": 50, "tradingDay": 20260709, "dealTime": 133000},
            },
        }
        probe_result["quotes"]["2330"]["eventConfirmed"] = True
        probe_result["quotes"]["2303"]["eventConfirmed"] = True
        with patch.object(backend, "_run_quote_probe", return_value=probe_result):
            result = backend.live_quotes(
                ["2330", "2303"], now=datetime.fromisoformat("2026-07-10T10:00:00+08:00")
        )
        self.assertEqual(set(result["quotes"]), {"2330"})
        self.assertEqual(result["rejectedSymbols"]["2303"], "not_today_during_market_session")
        normalized = result["quotes"]["2330"]
        self.assertEqual(normalized["openPrice"], 98)
        self.assertEqual(normalized["highPrice"], 101)
        self.assertEqual(normalized["lowPrice"], 97)
        self.assertEqual(normalized["snapshotAt"], "2026-07-10 09:59:59")
        self.assertTrue(normalized["fresh"])

    def test_live_quote_cache_avoids_repeated_capital_login(self):
        backend = self._backend(configured=True)
        backend._save_health(backend.load_config(), {
            "symbol": "2330", "stockName": "台積電", "price": 100,
            "tradingDay": 20260710, "dealTime": 93000,
        })
        probe_result = {
            "ok": True, "usable": True,
            "quotes": {"2330": {
                "code": "2330", "currentPrice": 100, "open": 99, "high": 101, "low": 98,
                "tradingDay": 20260710, "dealTime": 95959, "eventConfirmed": True,
            }},
        }
        now = datetime.fromisoformat("2026-07-10T10:00:00+08:00")
        with patch.object(backend, "_run_quote_probe", return_value=probe_result) as probe:
            first = backend.live_quotes(["2330"], now=now)
            second = backend.live_quotes(["2330"], now=now)
        self.assertTrue(first["ok"])
        self.assertTrue(second["cached"])
        self.assertEqual(probe.call_count, 1)

    def test_three_probe_failures_open_circuit_and_skip_fourth_login(self):
        backend = self._backend(configured=True)
        backend._save_health(backend.load_config(), {
            "symbol": "2330", "stockName": "台積電", "price": 100,
            "tradingDay": 20260710, "dealTime": 93000,
        })
        failed = {"ok": False, "usable": False, "quotes": {}, "error": "連線失敗"}
        now = datetime.fromisoformat("2026-07-10T10:00:00+08:00")
        with patch.object(backend, "_run_quote_probe", return_value=failed) as probe:
            for _ in range(3):
                backend.live_quotes(["2330"], now=now)
            fourth = backend.live_quotes(["2330"], now=now)
        self.assertEqual(probe.call_count, 3)
        self.assertTrue(fourth["circuitOpen"])
        self.assertIn("斷路器", fourth["error"])

    def test_manual_test_without_quote_event_never_enables_failover(self):
        backend = self._backend(configured=True)
        with patch.object(backend, "_run_quote_probe", return_value={
            "ok": True, "usable": True, "symbol": "2330", "price": 100,
            "quoteEventConfirmed": False,
        }):
            result = backend._test_connection_direct()
        self.assertFalse(result["ok"])
        self.assertFalse(result["usable"])
        self.assertFalse(backend.health_path.exists())

    def test_save_requires_securities_account_and_never_returns_full_number(self):
        backend = self._backend(configured=False)
        with self.assertRaisesRegex(ValueError, "證券帳號"):
            backend.save_config({"userId": "user", "password": "secret"})
        with patch.object(backend, "_load_com_client", return_value=_Client()):
            result = backend.save_config({
                "userId": "user", "password": "secret", "accountNo": "9876543",
            })
        self.assertTrue(result["configured"])
        self.assertEqual(result["accountNoMasked"], "****543")
        self.assertNotIn("9876543", json.dumps(result, ensure_ascii=False))

    def test_quote_probe_contains_no_account_or_order_report_calls(self):
        probe_text = capital_module.QUOTE_PROBE_PATH.read_text(encoding="utf-8")
        forbidden = (
            "SKOrderLib", "GetRealBalanceReport", "GetProfitLossGWReport",
            "GetFulfillReport", "account_snapshot",
        )
        for token in forbidden:
            with self.subTest(token=token):
                self.assertNotIn(token, probe_text)


if __name__ == "__main__":
    unittest.main()
