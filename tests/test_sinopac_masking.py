"""
永豐設定頁資安細節修復的回歸測試：

  1. /api/sinopac/status 不再回傳 configPath(完整絕對路徑洩漏使用者名稱與
     磁碟結構，且前端從未使用)。
  2. personIdMasked 改用 mask_person_id：10碼身分證字號只露第1碼+最後2碼，
     不再用給高熵長金鑰設計的 mask_secret(頭4+尾4=露出8/10碼)。
  3. sanitize_error/sanitize_text 的遮罩清單補上 caPassword/personId(兩者
     都是身分證字號等級機密，會出現在錯誤訊息→前端顯示→log)。

全部用 mock 隔絕設定檔讀取，不觸碰真實 sinopac_api.json、不打任何 API。

執行方式：
  python -m unittest tests.test_sinopac_masking -v
"""
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sinopac_backend import mask_person_id, mask_secret, sinopac_backend


FAKE_CONFIG = {
    "apiKey": "test-api-key-abcdefgh1234",
    "secretKey": "test-secret-key-abcdefgh1234",
    "simulation": True,
    "caPath": "",
    "caPassword": "A123456789",
    "personId": "A123456789",
}


class MaskPersonIdTests(unittest.TestCase):
    def test_ten_char_national_id_only_exposes_three_chars(self):
        masked = mask_person_id("A123456789")
        self.assertEqual(masked, "A*******89")
        # 只露 3 碼：第1碼 + 最後2碼
        self.assertEqual(sum(1 for ch in masked if ch != "*"), 3)

    def test_short_value_returns_empty(self):
        self.assertEqual(mask_person_id("A12"), "")
        self.assertEqual(mask_person_id(""), "")
        self.assertEqual(mask_person_id(None), "")

    def test_mask_secret_still_used_for_long_api_keys(self):
        # 對照組：高熵長金鑰維持原本頭4尾4，這對長金鑰是安全的
        self.assertEqual(mask_secret("test-api-key-abcdefgh1234"), "test...1234")


class StatusPayloadTests(unittest.TestCase):
    def test_status_does_not_expose_config_path(self):
        with patch.object(sinopac_backend, "load_config", return_value=dict(FAKE_CONFIG)):
            payload = sinopac_backend.status()
        self.assertNotIn("configPath", payload)

    def test_status_person_id_masked_with_tight_mask(self):
        with patch.object(sinopac_backend, "load_config", return_value=dict(FAKE_CONFIG)):
            payload = sinopac_backend.status()
        self.assertEqual(payload["personIdMasked"], "A*******89")
        self.assertNotIn("A123456789", str(payload))

    def test_status_still_reports_configured_flags(self):
        with patch.object(sinopac_backend, "load_config", return_value=dict(FAKE_CONFIG)):
            payload = sinopac_backend.status()
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["configured"])
        self.assertEqual(payload["apiKeyMasked"], "test...1234")


class SanitizeTextTests(unittest.TestCase):
    def test_person_id_is_masked_out_of_error_text(self):
        message = "Shioaji login failed for person A123456789: CA error"
        cleaned = sinopac_backend.sanitize_text(message, FAKE_CONFIG)
        self.assertNotIn("A123456789", cleaned)
        self.assertIn("A*******89", cleaned)

    def test_ca_password_is_masked_out_of_error_text(self):
        config = dict(FAKE_CONFIG, caPassword="SecretPw99", personId="B987654321")
        message = "CA activation failed with password SecretPw99"
        cleaned = sinopac_backend.sanitize_text(message, config)
        self.assertNotIn("SecretPw99", cleaned)

    def test_api_key_masking_behaviour_is_preserved(self):
        message = f"HTTP 401 for key {FAKE_CONFIG['apiKey']}"
        cleaned = sinopac_backend.sanitize_text(message, FAKE_CONFIG)
        self.assertNotIn(FAKE_CONFIG["apiKey"], cleaned)
        self.assertIn("test...1234", cleaned)

    def test_short_config_values_do_not_mangle_unrelated_text(self):
        # caPassword 太短(<6)時不做 replace，避免訊息裡剛好相同的無關數字
        # 片段被誤遮(例如密碼 "1234" 會把 HTTP 狀態碼/股號裡的 1234 全遮掉)。
        config = dict(FAKE_CONFIG, caPassword="1234", personId="")
        message = "order for 1234 shares at price 1234"
        cleaned = sinopac_backend.sanitize_text(message, config)
        self.assertEqual(cleaned, message)

    def test_sanitize_error_keeps_not_exist_prefix_and_masks(self):
        exc = RuntimeError(f"key {FAKE_CONFIG['apiKey']} does not exist")
        cleaned = sinopac_backend.sanitize_error(exc, FAKE_CONFIG)
        self.assertIn("永豐 API Key 不存在或不屬於此帳號", cleaned)
        self.assertNotIn(FAKE_CONFIG["apiKey"], cleaned)


class AccountBalanceSettlementsMaskingTests(unittest.TestCase):
    """account_balance_payload/settlements_payload 之前完全沒過 sanitize_error，
    跟本檔案其他所有下單/查詢路徑的一貫原則不一致——SDK 內部例外訊息偶爾會
    夾帶帳號等診斷資訊，沒遮罩會直接透傳到 /api/sinopac/holdings 的 HTTP
    回應。這裡確認例外訊息裡的機密欄位真的被遮掉。"""

    class _FakeApi:
        def __init__(self, balance_exc=None, settlements_exc=None):
            self._balance_exc = balance_exc
            self._settlements_exc = settlements_exc

        def account_balance(self, account):
            if self._balance_exc:
                raise self._balance_exc
            return {}

        def list_settlements(self, account=None):
            if self._settlements_exc:
                raise self._settlements_exc
            return []

    def test_account_balance_error_is_masked(self):
        exc = RuntimeError(f"balance query failed for key {FAKE_CONFIG['apiKey']}")
        api = self._FakeApi(balance_exc=exc)
        result = sinopac_backend.account_balance_payload(api, "acc", FAKE_CONFIG)
        self.assertFalse(result["ok"])
        self.assertNotIn(FAKE_CONFIG["apiKey"], result["error"])

    def test_settlements_error_is_masked_including_fallback(self):
        exc = RuntimeError(f"settlements failed for {FAKE_CONFIG['personId']}")
        fallback_exc = RuntimeError(f"fallback also failed for key {FAKE_CONFIG['apiKey']}")
        api = self._FakeApi(settlements_exc=exc)
        # list_settlements() 的 fallback 呼叫(不帶 account)也要拋例外，才會走到
        # rawError 組裝那個分支。
        api.list_settlements = lambda account=None: (_ for _ in ()).throw(
            exc if account else fallback_exc
        )
        result = sinopac_backend.settlements_payload(api, "acc", FAKE_CONFIG)
        self.assertFalse(result["ok"])
        self.assertNotIn(FAKE_CONFIG["personId"], result["rawError"])
        self.assertNotIn(FAKE_CONFIG["apiKey"], result["rawError"])


if __name__ == "__main__":
    unittest.main()
