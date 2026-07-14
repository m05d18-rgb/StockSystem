"""
server.py StockHandler 的 CORS/CSRF 回歸測試，對應這次修的問題：

之前 end_headers() 對「每一個」回應都送出 Access-Control-Allow-Origin: *，
且 do_POST/do_DELETE 完全沒有檢查請求來源。read_json_body() 又完全不看
Content-Type，攻擊者只要在受害者瀏覽器裡開一個惡意分頁，用 CORS 安全清單
內的 Content-Type(例如 text/plain)包住 JSON 字串，就能在不觸發 preflight
的情況下對本機伺服器送出任何 POST/DELETE——包含 /api/sinopac/order/place
這種會真的送出委託的端點。

修法：拿掉萬用 CORS header(同源不需要任何 CORS header)，並在 do_POST/
do_DELETE 最前面檢查 Origin header(存在時)是否跟 Host 一致，不一致直接
403，在碰到任何真正的業務邏輯之前就擋掉。

用真實 ThreadingHTTPServer 監聽 127.0.0.1 隨機埠(port 0)測試，故意打
一個不存在的路徑(/api/__test_nonexistent__)，確保驗證的是「請求有沒有
被放行到路由分派」，不會觸發任何真實業務邏輯(DB 寫入/外部 API 呼叫)。

執行方式：
  python -m unittest tests.test_server_security -v
"""
import http.client
import os
import sys
import unittest
from http.server import ThreadingHTTPServer
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server as server_module


class StockHandlerOriginTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server_module.StockHandler)
        cls.port = cls.httpd.server_address[1]
        import threading
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.thread.join(timeout=5)

    def _request(self, method, path, origin=None, body=b""):
        # 20秒(不是5秒)：這裡測的是「回應乾不乾淨」而非「回應多快」，
        # /api/monster-scores 等端點會查正式資料庫(百萬列等級)，在跟其他
        # 同時執行的行程(例如本機開著的預覽伺服器)競爭時，5秒常常不夠，
        # 造成跟程式碼正確性無關的逾時假警報。
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=20)
        headers = {}
        if origin is not None:
            headers["Origin"] = origin
        try:
            conn.request(method, path, body=body, headers=headers)
            response = conn.getresponse()
            status = response.status
            resp_headers = dict(response.getheaders())
            response.read()
            return status, resp_headers
        finally:
            conn.close()

    def _request_with_body(self, method, path, origin=None, extra_headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=20)
        headers = dict(extra_headers or {})
        if origin is not None:
            headers["Origin"] = origin
        try:
            conn.request(method, path, headers=headers)
            response = conn.getresponse()
            status = response.status
            resp_headers = dict(response.getheaders())
            body = response.read().decode("utf-8", errors="replace")
            return status, resp_headers, body
        finally:
            conn.close()

    def test_cross_origin_post_is_rejected_before_reaching_dispatch(self):
        status, _ = self._request(
            "POST", "/api/__test_nonexistent__",
            origin="http://evil.example.com",
        )
        self.assertEqual(status, 403)

    def test_cross_origin_post_matching_real_endpoint_is_still_rejected(self):
        # 就算路徑是真實存在的下單端點，跨源請求也要在碰到業務邏輯前被擋掉。
        status, _ = self._request(
            "POST", "/api/sinopac/order/place",
            origin="http://evil.example.com",
        )
        self.assertEqual(status, 403)

    def test_same_origin_post_reaches_dispatch(self):
        status, _ = self._request(
            "POST", "/api/__test_nonexistent__",
            origin=f"http://127.0.0.1:{self.port}",
        )
        self.assertEqual(status, 404)

    def test_post_with_no_origin_header_reaches_dispatch(self):
        # 沒有 Origin header 通常代表同源請求或非瀏覽器工具(curl等)，不能因此擋掉。
        status, _ = self._request("POST", "/api/__test_nonexistent__", origin=None)
        self.assertEqual(status, 404)

    def test_cross_origin_delete_is_rejected(self):
        status, _ = self._request(
            "DELETE", "/api/settings/finmind-token",
            origin="http://evil.example.com",
        )
        self.assertEqual(status, 403)

    def test_same_origin_delete_reaches_dispatch(self):
        status, _ = self._request(
            "DELETE", "/api/__test_nonexistent__",
            origin=f"http://127.0.0.1:{self.port}",
        )
        self.assertEqual(status, 404)

    def test_responses_no_longer_carry_wildcard_cors_header(self):
        status, headers = self._request("GET", "/api/settings/finmind-token")
        self.assertEqual(status, 200)
        self.assertNotIn("Access-Control-Allow-Origin", headers)

    def test_dead_ai_stock_report_endpoint_is_gone(self):
        status, _ = self._request(
            "POST", "/api/ai/stock-report",
            origin=f"http://127.0.0.1:{self.port}",
        )
        self.assertEqual(status, 404)

    def test_cloudflare_root_serves_dedicated_mobile_page(self):
        status, headers, body = self._request_with_body(
            "GET",
            "/",
            extra_headers={"CF-Ray": "test-ray", "CF-Connecting-IP": "203.0.113.10"},
        )
        self.assertEqual(status, 200)
        self.assertIn('id="mobileRemoteApp"', body)
        self.assertIn("<title>我的台股買賣助手</title>", body)
        self.assertIn('<strong class="brand">台股助手</strong>', body)
        self.assertNotIn("StockAI", body)
        self.assertIn('rel="manifest"', body)
        self.assertIn('rel="apple-touch-icon"', body)
        self.assertIn("Content-Security-Policy", headers)

    def test_cloudflare_mobile_manifest_and_icons_are_allowlisted(self):
        remote_headers = {"CF-Ray": "test-ray", "CF-Connecting-IP": "203.0.113.10"}
        status, _, manifest = self._request_with_body(
            "GET", "/site.webmanifest?v=9.9.302", extra_headers=remote_headers,
        )
        self.assertEqual(status, 200)
        self.assertIn('"name": "我的台股買賣助手"', manifest)
        self.assertIn('"short_name": "台股助手"', manifest)
        self.assertIn('"start_url": "/mobile-remote.html#holdings"', manifest)
        self.assertIn("stockai-icon-192.png", manifest)
        self.assertIn("stockai-icon-512.png", manifest)

        for path in (
            "/assets/icons/apple-touch-icon.png?v=9.9.302",
            "/assets/icons/favicon-32.png?v=9.9.302",
            "/assets/icons/stockai-icon-192.png?v=9.9.302",
            "/assets/icons/stockai-icon-512.png?v=9.9.302",
        ):
            icon_status, icon_headers, _ = self._request_with_body(
                "GET", path, extra_headers=remote_headers,
            )
            self.assertEqual(icon_status, 200, path)
            content_type = next(
                (value for key, value in icon_headers.items() if key.lower() == "content-type"),
                None,
            )
            self.assertEqual(content_type, "image/png", path)

    def test_cloudflare_mobile_script_displays_daily_change_rate(self):
        status, _, body = self._request_with_body(
            "GET",
            "/mobile-remote.js?v=9.9.302",
            extra_headers={"CF-Ray": "test-ray", "CF-Connecting-IP": "203.0.113.10"},
        )
        self.assertEqual(status, 200)
        self.assertIn("item.changeRate", body)
        self.assertIn("totalReturnRate", body)
        self.assertIn("總漲幅", body)
        self.assertIn("今日漲跌", body)

    def test_cloudflare_mobile_styles_respect_iphone_top_safe_area(self):
        status, _, body = self._request_with_body(
            "GET",
            "/mobile-remote.css?v=9.9.302",
            extra_headers={"CF-Ray": "test-ray", "CF-Connecting-IP": "203.0.113.10"},
        )
        self.assertEqual(status, 200)
        self.assertIn("calc(10px + env(safe-area-inset-top))", body)
        self.assertIn("calc(58px + env(safe-area-inset-top))", body)

    def test_local_root_still_serves_desktop_page(self):
        status, _, body = self._request_with_body("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn('href="#monsterRadar"', body)
        self.assertNotIn('id="mobileRemoteApp"', body)

    def test_cloudflare_cannot_open_desktop_settings(self):
        status, _, _ = self._request_with_body(
            "GET",
            "/settings.html",
            extra_headers={"CF-Ray": "test-ray", "CF-Connecting-IP": "203.0.113.10"},
        )
        self.assertEqual(status, 403)

    def test_cloudflare_post_is_read_only_even_with_matching_origin(self):
        status, _, _ = self._request_with_body(
            "POST",
            "/api/__test_nonexistent__",
            origin=f"https://stocks.example.com",
            extra_headers={
                "Host": "stocks.example.com",
                "CF-Ray": "test-ray",
                "CF-Connecting-IP": "203.0.113.10",
            },
        )
        self.assertEqual(status, 403)

    def test_cloudflare_intraday_requires_cached_only_and_never_refreshes_quotes(self):
        headers = {
            "Host": "stocks.example.com",
            "CF-Ray": "test-ray",
            "CF-Connecting-IP": "203.0.113.10",
        }
        with patch.object(server_module, "update_monster_intraday_quotes") as mock_update:
            blocked, _, _ = self._request_with_body(
                "GET",
                "/api/monster-intraday",
                origin="https://stocks.example.com",
                extra_headers=headers,
            )
            allowed, _, _ = self._request_with_body(
                "GET",
                "/api/monster-intraday?cachedOnly=1",
                origin="https://stocks.example.com",
                extra_headers=headers,
            )
        self.assertEqual(blocked, 403)
        self.assertEqual(allowed, 200)
        mock_update.assert_not_called()

    def test_malformed_limit_query_param_gets_clean_response_not_connection_reset(self):
        # 對應這次修的 bug：GET /api/monster-scores?limit=abc 之前會讓
        # int() 直接拋 ValueError 穿出 do_GET，BaseHTTPRequestHandler 對
        # 未攔截例外的預設行為是印 traceback 到 stderr、不送出任何 HTTP
        # 回應，用戶端只會看到連線被重置。safe_int() 修好後這裡應該收到
        # 正常的 HTTP 回應(用 fallback 值繼續處理，不是 500 也不是連線中斷)。
        status, _ = self._request(
            "GET", "/api/monster-scores?limit=abc",
            origin=f"http://127.0.0.1:{self.port}",
        )
        self.assertEqual(status, 200)

    def test_malformed_trades_limit_gets_clean_response(self):
        status, _ = self._request(
            "GET", "/api/trades?limit=abc",
            origin=f"http://127.0.0.1:{self.port}",
        )
        self.assertEqual(status, 200)

    def test_malformed_predictions_limit_gets_clean_response(self):
        status, _ = self._request(
            "GET", "/api/ml/predictions?limit=abc",
            origin=f"http://127.0.0.1:{self.port}",
        )
        self.assertEqual(status, 200)


class SideEffectingGetEndpointsCsrfTests(unittest.TestCase):
    """對應這次修的 bug：GET /api/monster-intraday 與 GET /api/ml/predict?repair=1
    都有真實副作用(觸發永豐/FinMind 真實呼叫、寫入資料庫)，但 do_GET 完全沒有
    呼叫 _is_trusted_request_origin()，CSRF 防護只加在 do_POST/do_DELETE 開頭，
    對這兩個有副作用的 GET 路由是空的。跨站頁面可用單純 GET(img/fetch no-cors)
    觸發，不需要繞過任何 SOP 限制。

    全部 mock 掉真正觸發外部呼叫的函式，確認：(1)跨源請求在碰到這些函式之前就被
    403 擋掉，(2)同源請求正常放行、確實會呼叫到這些函式(不會被誤擋)。
    """

    @classmethod
    def setUpClass(cls):
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server_module.StockHandler)
        cls.port = cls.httpd.server_address[1]
        import threading
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.thread.join(timeout=5)

    def _request(self, method, path, origin=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        headers = {}
        if origin is not None:
            headers["Origin"] = origin
        try:
            conn.request(method, path, headers=headers)
            response = conn.getresponse()
            status = response.status
            response.read()
            return status
        finally:
            conn.close()

    def test_cross_origin_monster_intraday_rejected_before_triggering_quotes(self):
        with patch.object(server_module, "update_monster_intraday_quotes") as mock_update:
            status = self._request(
                "GET", "/api/monster-intraday?force=1",
                origin="http://evil.example.com",
            )
        self.assertEqual(status, 403)
        mock_update.assert_not_called()

    def test_same_origin_monster_intraday_still_reaches_real_logic(self):
        with patch.object(server_module, "update_monster_intraday_quotes") as mock_update:
            status = self._request(
                "GET", "/api/monster-intraday?force=1",
                origin=f"http://127.0.0.1:{self.port}",
            )
        self.assertEqual(status, 200)
        mock_update.assert_called_once()

    def test_cross_origin_ml_predict_repair_rejected_before_calling_predict_symbol(self):
        with patch.object(server_module.backend, "predict_symbol") as mock_predict:
            status = self._request(
                "GET", "/api/ml/predict?symbol=2330&repair=1",
                origin="http://evil.example.com",
            )
        self.assertEqual(status, 403)
        mock_predict.assert_not_called()

    def test_same_origin_ml_predict_repair_still_reaches_real_logic(self):
        with patch.object(server_module.backend, "predict_symbol", return_value={"probability": 0.5}) as mock_predict:
            status = self._request(
                "GET", "/api/ml/predict?symbol=2330&repair=1",
                origin=f"http://127.0.0.1:{self.port}",
            )
        self.assertEqual(status, 200)
        mock_predict.assert_called_once()

    def test_cross_origin_ml_predict_without_repair_is_not_blocked(self):
        # repair=0(預設)沒有外部副作用，不需要擋——確認這個修法沒有誤傷一般查詢。
        with patch.object(server_module.backend, "predict_symbol", return_value={"probability": 0.5}) as mock_predict:
            status = self._request(
                "GET", "/api/ml/predict?symbol=2330",
                origin="http://evil.example.com",
            )
        self.assertEqual(status, 200)
        mock_predict.assert_called_once()


class SafeIntTests(unittest.TestCase):
    def test_valid_numeric_string_converts(self):
        self.assertEqual(server_module.safe_int("42", 0), 42)

    def test_invalid_string_returns_default(self):
        self.assertEqual(server_module.safe_int("abc", 80), 80)

    def test_empty_string_returns_default(self):
        self.assertEqual(server_module.safe_int("", 80), 80)

    def test_none_returns_default(self):
        self.assertEqual(server_module.safe_int(None, 80), 80)


if __name__ == "__main__":
    unittest.main()
