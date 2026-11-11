from __future__ import annotations

import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from services.account_service import AccountService
from services.oauth_login_service import OAuthLoginService
from services.storage.json_storage import JSONStorageBackend
from test.test_account_export import MemoryStorage, make_jwt


class AccountTokenExpiryTests(unittest.TestCase):
    def setUp(self):
        self.addCleanup(mock.patch.stopall)
        mock.patch("services.account_service.log_service.add").start()
        mock.patch.object(AccountService, "_save_cumulative_total").start()
        self.service = AccountService(MemoryStorage([{
            "access_token": "old-at", "refresh_token": "rt_old", "id_token": "old-id",
            "access_token_expires_at": "2030-01-01T00:00:00Z",
            "refresh_token_expires_at": "2031-01-01T00:00:00Z",
        }]))

    def test_existing_accounts_gain_jwt_expiry_without_a_remote_call(self):
        token = make_jwt({"exp": 1893456000})
        service = AccountService(MemoryStorage([{"access_token": token, "refresh_token": "opaque-rt"}]))
        self.assertEqual(service.get_account(token)["access_token_expires_at"], "2030-01-01T00:00:00+00:00")
        self.assertIsNone(service.get_account(token)["refresh_token_expires_at"])

    def test_refresh_response_captures_both_relative_lifetimes(self):
        session = mock.Mock()
        session.post.return_value.status_code = 200
        session.post.return_value.json.return_value = {
            "access_token": "new-at", "refresh_token": "rt_new", "expires_in": 3600,
            "refresh_expires_in": 7200,
        }
        anchor = datetime(2030, 1, 1, tzinfo=timezone.utc)
        with (mock.patch("curl_cffi.requests.Session", return_value=session),
              mock.patch("services.account_service.datetime", wraps=datetime) as clock):
            clock.now.return_value = anchor
            result = self.service.refresh_account_tokens(["old-at"], "expiry-refresh")
        item = self.service.get_account("new-at")
        self.assertEqual(item["access_token_expires_at"], "2030-01-01T01:00:00+00:00")
        self.assertEqual(item["refresh_token_expires_at"], "2030-01-01T02:00:00+00:00")
        self.assertEqual(result["items"][0]["access_token_expires_at"], item["access_token_expires_at"])
        progress = self.service.get_refresh_progress("expiry-refresh")
        self.assertEqual(progress["result"]["items"][0]["refresh_token_expires_at"], item["refresh_token_expires_at"])

    def test_rotation_without_expiry_clears_stale_dates(self):
        self.service._apply_refreshed_tokens("old-at", {"access_token": "new-at", "refresh_token": "rt_new"}, "test")
        item = self.service.get_account("old-at")
        self.assertIsNone(item["access_token_expires_at"])
        self.assertIsNone(item["refresh_token_expires_at"])

    def test_missing_rt_expiry_preserves_date_only_when_rt_is_unchanged(self):
        session = mock.Mock()
        session.post.return_value.status_code = 200
        session.post.return_value.json.return_value = {"access_token": "new-at", "expires_in": 3600}
        with mock.patch("curl_cffi.requests.Session", return_value=session):
            self.service.refresh_access_token("old-at", force=True)
        self.assertEqual(self.service.get_account("old-at")["refresh_token_expires_at"], "2031-01-01T00:00:00+00:00")

    def test_failed_refresh_keeps_both_original_dates(self):
        before = self.service.get_account("old-at")
        with mock.patch.object(self.service, "_request_access_token_refresh", side_effect=RuntimeError("invalid_grant")):
            result = self.service.refresh_account_tokens(["old-at"])
        self.assertEqual(result["refreshed"], 0)
        after = self.service.get_account("old-at")
        for field in ("access_token_expires_at", "refresh_token_expires_at"):
            self.assertEqual(after[field], before[field])

    def test_rt_binding_and_removal_reset_only_rt_expiry(self):
        self.service.update_account("old-at", {"refresh_token": "rt_rebound"})
        item = self.service.get_account("old-at")
        self.assertIsNone(item["refresh_token_expires_at"])
        self.assertEqual(item["access_token_expires_at"], "2030-01-01T00:00:00+00:00")
        self.service.update_account("old-at", {"refresh_token": make_jwt({"exp": 1924992000})})
        self.assertEqual(self.service.get_account("old-at")["refresh_token_expires_at"], "2031-01-01T00:00:00+00:00")
        self.service.update_account("old-at", {"refresh_token": ""})
        self.assertIsNone(self.service.get_account("old-at")["refresh_token_expires_at"])

    def test_json_import_can_rebind_rt_without_reusing_old_expiry(self):
        self.service.add_account_items([{"access_token": "old-at", "refresh_token": "rt_rebound"}])
        self.assertIsNone(self.service.get_account("old-at")["refresh_token_expires_at"])

    def test_export_and_reimport_preserve_both_dates(self):
        [exported] = self.service.build_export_items()
        service = AccountService(MemoryStorage())
        service.add_account_items([exported])
        for field in ("access_token_expires_at", "refresh_token_expires_at"):
            self.assertEqual(service.get_account("old-at")[field], self.service.get_account("old-at")[field])

    def test_rt_import_persists_absolute_dates_before_lookup_and_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            storage = JSONStorageBackend(Path(directory) / "accounts.json")
            service = AccountService(storage)
            anchor = datetime(2030, 1, 1, tzinfo=timezone.utc)
            with (mock.patch.object(service, "_request_access_token_refresh", return_value={
                "access_token": "new-at", "refresh_token": "rt_new", "expires_in": 3600,
                "refresh_token_expires_in": 7200,
            }), mock.patch.object(service, "fetch_remote_info", side_effect=ConnectionError("offline")),
                  mock.patch("services.account_service.datetime", wraps=datetime) as clock):
                clock.now.return_value = anchor
                result = service.import_refresh_tokens(["rt_input"])
            self.assertEqual(result["errors"], [])
            item = AccountService(storage).get_account("new-at")
            self.assertEqual(item["access_token_expires_at"], "2030-01-01T01:00:00+00:00")
            self.assertEqual(item["refresh_token_expires_at"], "2030-01-01T02:00:00+00:00")
            self.assertNotIn("expires_in", item)

    def test_explicit_expiry_drives_automatic_refresh_for_opaque_at(self):
        self.service.update_account("old-at", {"access_token_expires_at": int(time.time()) + 60})
        self.assertEqual(self.service.list_expiring_access_tokens(), ["old-at"])
        with mock.patch.object(self.service, "_request_access_token_refresh", return_value={
            "access_token": "new-at", "expires_in": 3600,
        }) as request:
            self.service.refresh_access_token("old-at")
        request.assert_called_once()
        self.assertIsNotNone(self.service.get_account("new-at")["access_token_expires_at"])

    def test_password_relogin_synchronizes_both_expiry_fields(self):
        with mock.patch.object(self.service, "_login_with_password", return_value={
            "ok": True, "access_token": "new-at", "refresh_token": "rt_new", "id_token": "new-id",
            "access_token_expires_at": "2032-01-01T00:00:00Z", "refresh_token_expires_at": "2033-01-01T00:00:00Z",
        }):
            self.service._password_re_login_thread("old-at", "owner@example.com", "test-password", "test")
        item = self.service.get_account("old-at")
        self.assertEqual(item["access_token_expires_at"], "2032-01-01T00:00:00+00:00")
        self.assertEqual(item["refresh_token_expires_at"], "2033-01-01T00:00:00+00:00")

    def test_oauth_exchange_returns_absolute_expiry_metadata(self):
        session = mock.Mock()
        session.post.return_value.status_code = 200
        session.post.return_value.json.return_value = {
            "access_token": "oauth-at", "refresh_token": "oauth-rt", "id_token": "oauth-id",
            "expires_in": 3600, "refresh_token_expires_in": 7200,
        }
        with (mock.patch("curl_cffi.requests.Session", return_value=session),
              mock.patch("services.oauth_login_service.datetime", wraps=datetime) as clock):
            clock.now.return_value = datetime(2030, 1, 1, tzinfo=timezone.utc)
            result = OAuthLoginService._exchange_code("test-code", "test-verifier", "https://localhost/callback")
        self.assertEqual(result["access_token_expires_at"], "2030-01-01T01:00:00+00:00")
        self.assertEqual(result["refresh_token_expires_at"], "2030-01-01T02:00:00+00:00")


if __name__ == "__main__":
    unittest.main()
