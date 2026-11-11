from __future__ import annotations

import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

from services.account_service import AccountService
from services.config import config
from services.storage.json_storage import JSONStorageBackend
from test.test_account_export import MemoryStorage, make_jwt


class AccountTokenRefreshTests(unittest.TestCase):
    def setUp(self):
        self.addCleanup(mock.patch.stopall)
        self.log = mock.patch("services.account_service.log_service.add").start()
        mock.patch.object(AccountService, "_save_cumulative_total").start()
        self.storage = MemoryStorage([{
            "access_token": "old-at", "refresh_token": "rt_old", "id_token": "old-id",
            "email": "owner@example.com", "type": "Plus", "quota": 7, "success": 11,
            "proxy": "http://account-proxy:8080", "created_at": "2026-01-01 00:00:00",
        }])
        self.service = AccountService(self.storage)

    def mock_session(self, payload, status=200):
        session = mock.Mock()
        session.post.return_value.status_code = status
        session.post.return_value.json.return_value = payload
        mock.patch("curl_cffi.requests.Session", return_value=session).start()
        return session

    def test_request_matches_reference_json_and_uses_account_proxy(self):
        session = self.mock_session({"access_token": "new-at", "refresh_token": "rt_new", "id_token": "new-id"})
        with mock.patch("services.proxy_service.proxy_settings.build_session_kwargs", return_value={}) as options:
            result = self.service._request_access_token_refresh("rt_old", self.service.get_account("old-at"))
        options.assert_called_once_with(
            account=self.service.get_account("old-at"), upstream=True, impersonate="chrome110", verify=True,
        )
        self.assertEqual(result["refresh_token"], "rt_new")
        args, kwargs = session.post.call_args
        self.assertEqual(args, ("https://auth.openai.com/oauth/token",))
        self.assertEqual(kwargs["headers"]["Content-Type"], "application/json")
        self.assertNotIn("data", kwargs)
        self.assertEqual(kwargs["json"], {
            "client_id": "app_2SKx67EdpoN0G6j64rFvigXD", "grant_type": "refresh_token",
            "redirect_uri": "com.openai.chat://auth0.openai.com/ios/com.openai.chat/callback",
            "refresh_token": "rt_old",
        })
        session.close.assert_called_once()

    def test_missing_optional_tokens_preserves_existing_rt_and_id(self):
        self.mock_session({"access_token": "new-at"})
        result = self.service.refresh_access_token("old-at", force=True)
        account = self.service.get_account(result)
        self.assertEqual(account["refresh_token"], "rt_old")
        self.assertEqual(account["id_token"], "old-id")

    def test_blank_or_invalid_optional_tokens_preserve_credentials(self):
        for rt, identity in (("  ", "  "), (123, {})):
            with self.subTest(rt=rt):
                self.mock_session({"access_token": "old-at", "refresh_token": rt, "id_token": identity})
                self.service.refresh_access_token("old-at", force=True)
                account = self.service.get_account("old-at")
                self.assertEqual(account["refresh_token"], "rt_old")
                self.assertEqual(account["id_token"], "old-id")

    def test_force_refresh_rotates_keys_aliases_slots_and_preserves_metadata(self):
        before = self.service.get_account("old-at")
        self.service._image_inflight["old-at"] = 2
        with mock.patch.object(self.service, "_request_access_token_refresh", return_value={
            "access_token": "new-at", "refresh_token": "rt_new", "id_token": "new-id",
        }):
            result = self.service.refresh_account_tokens(["old-at"], "rotation-test")
        self.assertEqual(result["refreshed"], 1)
        self.assertEqual(self.service.list_tokens(), ["new-at"])
        self.assertEqual(self.service.resolve_access_token("old-at"), "new-at")
        account = self.service.get_account("old-at")
        for field in ("email", "type", "quota", "success", "proxy", "created_at"):
            self.assertEqual(account[field], before[field])
        self.assertEqual(account["refresh_token"], "rt_new")
        self.assertTrue(account["last_token_refresh_at"])
        self.assertEqual(self.service._image_inflight, {"new-at": 2})
        self.service.release_image_slot("old-at")
        self.assertEqual(self.service._image_inflight["new-at"], 1)
        self.assertTrue(self.service.get_refresh_progress("rotation-test")["done"])

    def test_manual_refresh_forces_even_unexpired_tokens_and_clears_backoff(self):
        unexpired = make_jwt({"exp": int(time.time()) + 7 * 86400})
        self.service = AccountService(MemoryStorage([{
            "access_token": unexpired, "refresh_token": "rt_old",
            "last_token_refresh_error": "previous failure",
            "last_token_refresh_error_at": self.service._now(),
        }]))
        with mock.patch.object(self.service, "_request_access_token_refresh", return_value={"access_token": unexpired}) as request:
            result = self.service.refresh_account_tokens([unexpired])
        request.assert_called_once()
        self.assertEqual(result["refreshed"], 1)
        self.assertIsNone(self.service.get_account(unexpired)["last_token_refresh_error"])

    def test_failed_refresh_keeps_credentials_and_redacts_errors_without_relogin(self):
        self.service.update_account("old-at", {"email": "owner@example.com", "password": "test-password"})
        with (
            mock.patch.object(self.service, "_request_access_token_refresh", side_effect=RuntimeError("app_session_terminated rt_old old-at old-id")),
            mock.patch("services.account_service.Thread") as thread,
        ):
            result = self.service.refresh_account_tokens(["old-at"])
        thread.assert_not_called()
        self.assertEqual(result["refreshed"], 0)
        self.assertEqual(len(result["errors"]), 1)
        account = self.service.get_account("old-at")
        self.assertEqual(account["refresh_token"], "rt_old")
        self.assertEqual(account["id_token"], "old-id")
        for secret in ("rt_old", "old-at", "old-id"):
            self.assertNotIn(secret, account["last_token_refresh_error"])
            self.assertNotIn(secret, result["errors"][0]["error"])
            self.assertNotIn(secret, str(self.log.call_args_list))

    def test_batch_deduplicates_skips_missing_rt_and_reports_missing_account(self):
        self.service.add_accounts(["without-rt"])
        with mock.patch.object(self.service, "_request_access_token_refresh", return_value={"access_token": "new-at"}) as request:
            result = self.service.refresh_account_tokens(["old-at", "old-at", "without-rt", "missing"], "batch-test")
        request.assert_called_once()
        self.assertEqual(result["refreshed"], 1)
        self.assertEqual(result["skipped"], 1)
        self.assertEqual(result["errors"][0]["error"], "account not found")
        self.assertEqual(self.service.get_refresh_progress("batch-test")["processed"], 3)

    def test_http_errors_and_invalid_success_payloads_close_session(self):
        for payload, status, expected in (
            ({"error": {"message": "expired rt_old"}}, 400, "oauth_refresh_http_400"),
            ({}, 200, "missing access_token"),
            ({"access_token": "   "}, 200, "missing access_token"),
            ({"access_token": 123}, 200, "missing access_token"),
            ([], 200, "non-JSON response"),
        ):
            with self.subTest(payload=payload):
                session = self.mock_session(payload, status)
                with self.assertRaisesRegex(RuntimeError, expected) as error:
                    self.service._request_access_token_refresh("rt_old")
                self.assertNotIn("rt_old", str(error.exception))
                session.close.assert_called_once()

    def test_non_json_error_does_not_expose_upstream_body(self):
        session = self.mock_session(None, 502)
        session.post.return_value.json.side_effect = ValueError("invalid JSON")
        session.post.return_value.text = "<html>rt_old private details</html>"
        with self.assertRaisesRegex(RuntimeError, "oauth_refresh_http_502: non-JSON response"):
            self.service._request_access_token_refresh("rt_old")
        session.close.assert_called_once()

    def test_concurrent_refresh_uses_latest_rotated_rt(self):
        used = []

        def refresh(rt, *_args, **_kwargs):
            used.append(rt)
            time.sleep(0.01)
            return {"access_token": f"at-{len(used)}", "refresh_token": f"rt_{len(used)}"}

        with mock.patch.object(self.service, "_request_access_token_refresh", side_effect=refresh):
            with ThreadPoolExecutor(max_workers=2) as executor:
                results = list(executor.map(lambda _: self.service.refresh_access_token("old-at", force=True), range(2)))
        self.assertEqual(used, ["rt_old", "rt_1"])
        self.assertEqual(set(results), {"at-1", "at-2"})
        self.assertEqual(self.service.resolve_access_token("old-at"), "at-2")

    def test_import_saves_rotated_credentials_before_info_lookup_and_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            storage = JSONStorageBackend(Path(directory) / "accounts.json")
            service = AccountService(storage)

            def info(token, **_kwargs):
                stored = AccountService(storage).get_account(token)
                self.assertEqual(stored["refresh_token"], "rt_new")
                raise ConnectionError("account info unavailable")

            with (
                mock.patch.object(service, "_request_access_token_refresh", return_value={"access_token": "new-at", "refresh_token": "rt_new"}),
                mock.patch.object(service, "fetch_remote_info", side_effect=info),
            ):
                result = service.import_refresh_tokens(["rt_input", "rt_input"], "import-test")
            self.assertEqual(result["added"], 1)
            self.assertEqual(result["refreshed"], 1)
            self.assertEqual(result["skipped"], 1)
            self.assertEqual(result["errors"], [])
            self.assertEqual(len(result["warnings"]), 1)
            self.assertEqual(AccountService(storage).get_account("new-at")["refresh_token"], "rt_new")

    def test_import_existing_rt_rotates_account_instead_of_adding_duplicate(self):
        with (
            mock.patch.object(self.service, "_request_access_token_refresh", return_value={"access_token": "new-at", "refresh_token": "rt_new"}),
            mock.patch.object(self.service, "fetch_remote_info"),
        ):
            result = self.service.import_refresh_tokens(["rt_old"])
        self.assertEqual(result["added"], 0)
        self.assertEqual(result["refreshed"], 1)
        self.assertEqual(len(result["items"]), 1)
        self.assertEqual(result["items"][0]["email"], "owner@example.com")

    def test_import_failure_is_indexed_and_never_returns_or_logs_rt(self):
        with mock.patch.object(self.service, "_request_access_token_refresh", side_effect=RuntimeError("expired rt_old")):
            result = self.service.import_refresh_tokens(["rt_old"])
        self.assertEqual(result["refreshed"], 0)
        self.assertEqual(result["errors"], [{"index": 1, "error": "expired [redacted]"}])
        self.assertEqual(self.service.get_account("old-at")["refresh_token"], "rt_old")
        self.assertNotIn("rt_old", str(self.log.call_args_list))

    def test_import_info_lookup_preserves_limited_account_when_auto_removal_enabled(self):
        with (
            mock.patch.object(self.service, "_request_access_token_refresh", return_value={"access_token": "new-at", "refresh_token": "rt_new"}),
            mock.patch("services.openai_backend_api.OpenAIBackendAPI") as backend,
            mock.patch.dict(config.data, {"auto_remove_rate_limited_accounts": True}),
        ):
            backend.return_value.get_user_info.return_value = {"status": "限流", "quota": 0}
            result = self.service.import_refresh_tokens(["rt_old"])
        self.assertEqual(result["refreshed"], 1)
        self.assertEqual(result["warnings"], [])
        self.assertEqual(self.service.get_account("new-at")["refresh_token"], "rt_new")
        self.assertEqual(self.service.get_account("new-at")["status"], "限流")

    def test_import_invalid_info_does_not_reuse_rt_or_delete_saved_tokens(self):
        from services.openai_backend_api import InvalidAccessTokenError

        with (
            mock.patch.object(self.service, "_request_access_token_refresh", return_value={"access_token": "new-at", "refresh_token": "rt_new"}) as request,
            mock.patch("services.openai_backend_api.OpenAIBackendAPI") as backend,
        ):
            backend.return_value.get_user_info.side_effect = InvalidAccessTokenError("info rejected")
            result = self.service.import_refresh_tokens(["rt_old"])
        request.assert_called_once()
        self.assertEqual(result["refreshed"], 1)
        self.assertEqual(len(result["warnings"]), 1)
        self.assertEqual(self.service.get_account("new-at")["refresh_token"], "rt_new")


if __name__ == "__main__":
    unittest.main()
