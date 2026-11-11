from __future__ import annotations

import time
import unittest
from unittest import mock

from fastapi import FastAPI
from fastapi.testclient import TestClient

import api.accounts as accounts_module
from services.account_service import AccountService
from services.config import config
from test.test_account_export import MemoryStorage


class AccountTokenRefreshApiTests(unittest.TestCase):
    def setUp(self):
        mock.patch("services.account_service.log_service.add").start()
        mock.patch.object(AccountService, "_save_cumulative_total").start()
        self.addCleanup(mock.patch.stopall)
        self.service = AccountService(MemoryStorage([
            {"access_token": "old-at", "refresh_token": "rt_old"},
            {"access_token": "no-rt"},
        ]))
        mock.patch.object(accounts_module, "account_service", self.service).start()
        self.request = mock.patch.object(self.service, "_request_access_token_refresh", return_value={
            "access_token": "new-at", "refresh_token": "rt_new", "id_token": "new-id",
        }).start()
        mock.patch.object(self.service, "fetch_remote_info").start()
        self.headers = {"Authorization": f"Bearer {config.auth_key}"}
        app = FastAPI()
        app.include_router(accounts_module.create_router())
        self.client = self.enterContext(TestClient(app))

    def wait_done(self, progress_id):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            response = self.client.get(f"/api/accounts/refresh/progress/{progress_id}", headers=self.headers)
            self.assertEqual(response.status_code, 200, response.text)
            if response.json()["done"]:
                return response.json()
            time.sleep(0.01)
        self.fail("background refresh did not complete")

    def test_both_token_routes_require_admin(self):
        for path, body in (
            ("/api/accounts/refresh-token", {"access_tokens": ["old-at"]}),
            ("/api/accounts/refresh-token/import", {"refresh_tokens": ["rt_old"]}),
        ):
            with self.subTest(path=path):
                response = self.client.post(path, json=body)
                self.assertEqual(response.status_code, 401, response.text)
        self.request.assert_not_called()

    def test_manual_refresh_is_async_and_progress_exists_immediately(self):
        response = self.client.post("/api/accounts/refresh-token", json={"access_tokens": ["old-at"]}, headers=self.headers)
        self.assertEqual(response.status_code, 200, response.text)
        progress = self.wait_done(response.json()["progress_id"])
        self.assertEqual(progress["total"], 1)
        self.assertEqual(progress["processed"], 1)
        self.assertEqual(progress["result"]["refreshed"], 1)
        self.assertEqual(self.service.get_account("old-at")["access_token"], "new-at")

    def test_empty_selection_refreshes_all_and_reports_accounts_without_rt(self):
        response = self.client.post("/api/accounts/refresh-token", json={}, headers=self.headers)
        progress = self.wait_done(response.json()["progress_id"])
        self.assertEqual(progress["total"], 2)
        self.assertEqual(progress["result"]["refreshed"], 1)
        self.assertEqual(progress["result"]["skipped"], 1)
        self.request.assert_called_once()

    def test_empty_pool_returns_400(self):
        self.service.delete_accounts(self.service.list_tokens())
        response = self.client.post("/api/accounts/refresh-token", json={}, headers=self.headers)
        self.assertEqual(response.status_code, 400, response.text)
        self.request.assert_not_called()

    def test_rt_import_deduplicates_and_rotates_matching_existing_account(self):
        response = self.client.post("/api/accounts/refresh-token/import", json={"refresh_tokens": [" rt_old ", "rt_old"]}, headers=self.headers)
        progress = self.wait_done(response.json()["progress_id"])
        self.assertEqual(progress["total"], 1)
        self.assertEqual(progress["result"]["added"], 0)
        self.assertEqual(progress["result"]["refreshed"], 1)
        self.assertEqual(self.service.get_account("old-at")["refresh_token"], "rt_new")
        self.request.assert_called_once()

    def test_empty_rt_import_and_oversized_batches_are_rejected(self):
        for tokens, status in (([], 400), (["  "], 400), (["x" * 16385], 400), (["rt_test"] * 101, 422)):
            with self.subTest(status=status, count=len(tokens)):
                response = self.client.post("/api/accounts/refresh-token/import", json={"refresh_tokens": tokens}, headers=self.headers)
                self.assertEqual(response.status_code, status)
        self.request.assert_not_called()

    def test_failed_exchange_is_reported_without_deleting_existing_account(self):
        self.request.side_effect = RuntimeError("invalid_grant rt_old")
        response = self.client.post("/api/accounts/refresh-token", json={"access_tokens": ["old-at"]}, headers=self.headers)
        progress = self.wait_done(response.json()["progress_id"])
        self.assertEqual(progress["result"]["refreshed"], 0)
        self.assertIn("invalid_grant", progress["result"]["errors"][0]["error"])
        self.assertNotIn("rt_old", progress["result"]["errors"][0]["error"])
        self.assertEqual(self.service.get_account("old-at")["refresh_token"], "rt_old")

    def test_update_binds_and_removes_rt_and_clears_refresh_error(self):
        self.service.update_account("no-rt", {"last_token_refresh_error": "old error", "last_token_refresh_error_at": "2026-01-01"})
        response = self.client.post("/api/accounts/update", json={"access_token": "no-rt", "refresh_token": " rt_bound "}, headers=self.headers)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["item"]["refresh_token"], "rt_bound")
        self.assertIsNone(response.json()["item"]["last_token_refresh_error"])
        response = self.client.post("/api/accounts/update", json={"access_token": "no-rt", "refresh_token": ""}, headers=self.headers)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["item"]["refresh_token"], "")

    def test_omitting_rt_from_update_preserves_saved_rt(self):
        response = self.client.post("/api/accounts/update", json={"access_token": "old-at", "proxy": ""}, headers=self.headers)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["item"]["refresh_token"], "rt_old")

    def test_unexpected_background_error_marks_task_done_without_exposing_secrets(self):
        with mock.patch.object(self.service, "refresh_account_tokens", side_effect=RuntimeError("private rt_old")):
            response = self.client.post("/api/accounts/refresh-token", json={"access_tokens": ["old-at"]}, headers=self.headers)
            progress = self.wait_done(response.json()["progress_id"])
        self.assertTrue(progress["error"])
        self.assertNotIn("rt_old", progress["error"])

    def test_refresh_progress_and_account_list_return_synchronized_expiry(self):
        self.request.return_value.update({
            "access_token_expires_at": "2030-01-01T00:00:00Z",
            "refresh_token_expires_at": "2031-01-01T00:00:00Z",
        })
        response = self.client.post("/api/accounts/refresh-token", json={"access_tokens": ["old-at"]}, headers=self.headers)
        progress = self.wait_done(response.json()["progress_id"])
        item = next(item for item in progress["result"]["items"] if item["access_token"] == "new-at")
        self.assertEqual(item["access_token_expires_at"], "2030-01-01T00:00:00+00:00")
        self.assertEqual(item["refresh_token_expires_at"], "2031-01-01T00:00:00+00:00")
        response = self.client.get("/api/accounts", headers=self.headers)
        listed = next(account for account in response.json()["items"] if account["access_token"] == "new-at")
        self.assertEqual(listed["access_token_expires_at"], item["access_token_expires_at"])
        self.assertEqual(listed["refresh_token_expires_at"], item["refresh_token_expires_at"])

    def test_rt_rebinding_and_removal_do_not_reuse_old_expiry(self):
        self.service.update_account("old-at", {"refresh_token_expires_at": "2030-01-01T00:00:00Z"})
        response = self.client.post("/api/accounts/update", json={"access_token": "old-at", "refresh_token": "rt_bound"}, headers=self.headers)
        self.assertIsNone(response.json()["item"]["refresh_token_expires_at"])
        self.service.update_account("old-at", {"refresh_token_expires_at": "2030-01-01T00:00:00Z"})
        response = self.client.post("/api/accounts/update", json={"access_token": "old-at", "refresh_token": ""}, headers=self.headers)
        self.assertIsNone(response.json()["item"]["refresh_token_expires_at"])

    def test_oauth_finish_preserves_received_expiry_metadata(self):
        with mock.patch.object(accounts_module.oauth_login_service, "finish", return_value={
            "access_token": "oauth-at", "refresh_token": "oauth-rt", "id_token": "oauth-id",
            "access_token_expires_at": "2030-01-01T00:00:00Z", "refresh_token_expires_at": "2031-01-01T00:00:00Z",
        }), mock.patch.object(self.service, "refresh_accounts", return_value={"refreshed": 1}):
            response = self.client.post("/api/accounts/oauth/finish", json={"session_id": "test-session", "callback": "test-code"}, headers=self.headers)
        self.assertEqual(response.status_code, 200, response.text)
        item = self.service.get_account("oauth-at")
        self.assertEqual(item["access_token_expires_at"], "2030-01-01T00:00:00+00:00")
        self.assertEqual(item["refresh_token_expires_at"], "2031-01-01T00:00:00+00:00")


if __name__ == "__main__":
    unittest.main()
