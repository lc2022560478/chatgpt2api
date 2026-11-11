from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
from threading import Event, Thread
import unittest
from unittest import mock

from api import support
from services.account_service import AccountService
from services.config import ConfigStore, config
from services.storage.json_storage import JSONStorageBackend


class ImageSuccessCooldownTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.storage = JSONStorageBackend(Path(self.tmp.name) / "accounts.json")
        self.service = AccountService(self.storage)
        self.settings = {
            **config.data,
            "image_success_cooldown_enabled": True,
            "image_success_cooldown_minutes": 7,
            "image_account_concurrency": 3,
            "auto_remove_rate_limited_accounts": True,
        }
        patcher = mock.patch.object(config, "data", self.settings)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.service.add_account_items([
            {"access_token": "token-a", "status": "正常", "quota": 5},
            {"access_token": "token-b", "status": "正常", "quota": 5},
        ])
        self.service.fetch_remote_info = lambda token, *_args, **_: self.service.get_account(token)

    def expire(self, token: str = "token-a") -> None:
        self.service.update_account(token, {
            "image_success_cooldown_until": (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),
        }, preserve_account=True)

    def test_success_limits_account_and_routes_next_request_to_another_account(self) -> None:
        first = self.service.get_available_access_token(timeout_secs=1)
        before = datetime.now(timezone.utc)
        account = self.service.mark_image_result(first, True)
        until = self.service._parse_time(account["image_success_cooldown_until"])
        self.assertEqual(account["status"], "限流")
        self.assertEqual((account["success"], account["quota"]), (1, 4))
        self.assertGreaterEqual(until, before + timedelta(minutes=7))
        self.assertLess(until, datetime.now(timezone.utc) + timedelta(minutes=7, seconds=1))
        self.assertEqual(self.service._image_inflight, {})
        second = self.service.get_available_access_token(timeout_secs=1)
        self.assertNotEqual(first, second)
        self.service.release_image_slot(second)
        self.assertIsNotNone(self.service.get_account(first))
        self.assertNotIn(first, self.service.list_limited_tokens())

    def test_failure_does_not_start_cooldown(self) -> None:
        account = self.service.mark_image_result("token-a", False)
        self.assertEqual(account["status"], "正常")
        self.assertEqual((account["fail"], account["quota"]), (1, 5))
        self.assertIsNone(account["image_success_cooldown_until"])

    def test_disabled_feature_keeps_existing_success_behavior(self) -> None:
        self.settings["image_success_cooldown_enabled"] = False
        account = self.service.mark_image_result("token-a", True)
        self.assertEqual(account["status"], "正常")
        self.assertEqual(account["quota"], 4)
        self.assertIsNone(account["image_success_cooldown_until"])

    def test_upstream_refresh_does_not_release_or_delete_cooling_account(self) -> None:
        first = self.service.mark_image_result("token-a", True)
        updated = self.service.update_account("token-a", {
            "status": "正常", "quota": 5, "restore_at": "2030-01-01T00:00:00+00:00",
        })
        self.assertEqual(updated["status"], "限流")
        self.assertEqual(updated["image_success_cooldown_until"], first["image_success_cooldown_until"])
        self.assertEqual(self.service.restore_expired_image_cooldowns(), 0)

    def test_upstream_quota_cooldown_preserves_quota_through_refresh_and_restores_at_deadline(self) -> None:
        restore_at = (datetime.now(timezone.utc) + timedelta(hours=4)).isoformat()
        account = self.service.mark_image_quota_exhausted(
            "token-a", restore_at=restore_at, error="upstream image request limit",
        )
        account = self.service.mark_image_result("token-a", False)

        self.assertEqual((account["status"], account["quota"], account["fail"]), ("限流", 5, 1))
        self.assertEqual(account["image_quota_cooldown_until"], restore_at)
        self.assertNotIn("token-a", self.service.list_limited_tokens())

        del self.service.fetch_remote_info

        class RemoteAccountInfo:
            def __init__(self, _token: str) -> None:
                pass

            def get_user_info(self, *, timeout_secs: float | None = None) -> dict:
                return {"status": "正常", "quota": 0}

            def close(self) -> None:
                pass

        with mock.patch("services.openai_backend_api.OpenAIBackendAPI", RemoteAccountInfo):
            refreshed = self.service.fetch_remote_info("token-a")
        self.assertEqual((refreshed["status"], refreshed["quota"]), ("限流", 5))
        self.assertIsNotNone(self.service.get_account("token-a"))

        self.service.update_account(
            "token-a",
            {"image_quota_cooldown_until": (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()},
            preserve_account=True,
        )
        self.assertEqual(self.service.restore_expired_image_cooldowns(), 1)
        restored = self.service.get_account("token-a")
        self.assertEqual((restored["status"], restored["quota"]), ("正常", 5))
        self.assertIsNone(restored["image_quota_cooldown_until"])

    def test_upstream_quota_cooldown_is_independent_of_success_cooldown_setting(self) -> None:
        self.settings["image_success_cooldown_enabled"] = False
        restore_at = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()

        account = self.service.mark_image_quota_exhausted("token-a", restore_at=restore_at)

        self.assertEqual((account["status"], account["quota"]), ("限流", 5))
        self.assertEqual(self.service.restore_expired_image_cooldowns(), 0)
        self.assertEqual(self.service.get_account("token-a")["status"], "限流")

    def test_upstream_quota_limit_without_deadline_keeps_account_and_quota_until_recovered(self) -> None:
        account = self.service.mark_image_quota_exhausted("token-a", error="image limit")
        account = self.service.mark_image_result("token-a", False)

        self.assertEqual((account["status"], account["quota"], account["fail"]), ("限流", 5, 1))
        self.assertTrue(account["image_quota_limited"])
        self.assertIsNone(account["image_quota_cooldown_until"])
        self.assertIn("token-a", self.service.list_limited_tokens())

        del self.service.fetch_remote_info

        class LimitedAccountInfo:
            def __init__(self, _token: str) -> None:
                pass

            def get_user_info(self, *, timeout_secs: float | None = None) -> dict:
                return {"status": "限流", "quota": 0}

            def close(self) -> None:
                pass

        with mock.patch("services.openai_backend_api.OpenAIBackendAPI", LimitedAccountInfo):
            refreshed = self.service.fetch_remote_info("token-a")
        self.assertEqual((refreshed["status"], refreshed["quota"]), ("限流", 5))
        self.assertTrue(refreshed["image_quota_limited"])

        class AvailableAccountInfo:
            def __init__(self, _token: str) -> None:
                pass

            def get_user_info(self, *, timeout_secs: float | None = None) -> dict:
                return {"status": "正常", "quota": 3}

            def close(self) -> None:
                pass

        with mock.patch("services.openai_backend_api.OpenAIBackendAPI", AvailableAccountInfo):
            refreshed = self.service.fetch_remote_info("token-a")
        self.assertEqual((refreshed["status"], refreshed["quota"]), ("正常", 3))
        self.assertFalse(refreshed["image_quota_limited"])

    def test_expired_cooldown_restores_quota_without_waiting_for_upstream(self) -> None:
        self.service.mark_image_result("token-a", True)
        self.expire()
        self.assertEqual(self.service.restore_expired_image_cooldowns(), 1)
        account = self.service.get_account("token-a")
        self.assertEqual((account["status"], account["quota"]), ("正常", 4))
        self.assertIsNone(account["image_success_cooldown_until"])
        self.assertEqual(self.service.restore_expired_image_cooldowns(), 0)

    def test_cooldown_survives_reload_and_expiration_restores_on_selection(self) -> None:
        self.service.mark_image_result("token-a", True)
        reloaded = AccountService(self.storage)
        self.assertEqual(reloaded.get_account("token-a")["status"], "限流")
        self.assertEqual(reloaded.restore_expired_image_cooldowns(), 0)
        reloaded.update_account("token-a", {
            "image_success_cooldown_until": (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),
        }, preserve_account=True)
        reloaded.delete_accounts(["token-b"])
        reloaded.fetch_remote_info = lambda token, *_args, **_: reloaded.get_account(token)
        token = reloaded.get_available_access_token(timeout_secs=1)
        self.assertEqual(token, "token-a")
        reloaded.release_image_slot(token)

    def test_disabling_switch_releases_existing_cooldowns(self) -> None:
        self.service.mark_image_result("token-a", True)
        self.settings["image_success_cooldown_enabled"] = False
        self.assertEqual(self.service.restore_expired_image_cooldowns(), 1)
        self.assertEqual(self.service.get_account("token-a")["status"], "正常")

    def test_success_cooldown_expiry_does_not_release_unknown_quota_deadline(self) -> None:
        self.service.mark_image_result("token-a", True)
        self.service.mark_image_quota_exhausted("token-a", error="image quota limit")
        self.expire()
        self.service.restore_expired_image_cooldowns()
        account = self.service.get_account("token-a")
        self.assertEqual(account["status"], "限流")
        self.assertTrue(account["image_quota_limited"])
        self.settings["image_success_cooldown_enabled"] = False
        self.service.mark_image_result("token-a", True)
        self.assertEqual(self.service.get_account("token-a")["status"], "限流")

    def test_no_fake_quota_is_created_when_cooldown_expires(self) -> None:
        upstream_reset = "2030-01-01T00:00:00+00:00"
        self.service.update_account("token-a", {"quota": 1, "restore_at": upstream_reset})
        account = self.service.mark_image_result("token-a", True)
        self.assertEqual(account["quota"], 0)
        self.expire()
        self.service.restore_expired_image_cooldowns()
        account = self.service.get_account("token-a")
        self.assertEqual((account["status"], account["quota"]), ("限流", 0))
        self.assertEqual(account["restore_at"], upstream_reset)
        self.assertIn("token-a", self.service.list_limited_tokens())

    def test_disabling_or_invalidating_account_during_cooldown_is_preserved(self) -> None:
        for status in ("禁用", "异常"):
            with self.subTest(status=status):
                self.service.mark_image_result("token-a", True)
                self.service.update_account("token-a", {"status": status})
                self.expire()
                self.service.restore_expired_image_cooldowns()
                self.assertEqual(self.service.get_account("token-a")["status"], status)

    def test_cooldown_uses_resolved_token_after_token_rotation(self) -> None:
        self.service._apply_refreshed_tokens("token-a", {"access_token": "token-new"}, "test")
        account = self.service.mark_image_result("token-a", True)
        self.assertEqual(account["access_token"], "token-new")
        self.assertEqual(account["status"], "限流")
        self.assertIsNotNone(self.service.get_account("token-new")["image_success_cooldown_until"])

    def test_queued_request_cannot_reuse_account_after_success(self) -> None:
        self.service.delete_accounts(["token-b"])
        token = self.service.get_available_access_token(timeout_secs=1)
        self.assertEqual(self.service._list_available_candidate_tokens(), [])
        waiting = Event()
        original_wait = self.service._image_slot_condition.wait
        results = []

        def wait(**kwargs):
            waiting.set()
            return original_wait(**kwargs)

        def reserve():
            try:
                results.append(self.service.get_available_access_token(timeout_secs=2))
            except RuntimeError as exc:
                results.append(exc)

        with mock.patch.object(self.service._image_slot_condition, "wait", side_effect=wait):
            thread = Thread(target=reserve, daemon=True)
            thread.start()
            try:
                self.assertTrue(waiting.wait(timeout=1))
                self.service.mark_image_result(token, True)
                thread.join(timeout=2)
                self.assertFalse(thread.is_alive())
                self.assertEqual(len(results), 1)
                self.assertIsInstance(results[0], RuntimeError)
                self.assertEqual(self.service._image_inflight, {})
            finally:
                thread.join(timeout=2)

    def test_background_watcher_restores_without_a_request_or_upstream_refresh(self) -> None:
        self.service.mark_image_result("token-a", True)
        self.expire()
        stop = Event()
        restored = Event()
        original_restore = self.service.restore_expired_image_cooldowns

        def restore():
            result = original_restore()
            restored.set()
            stop.set()
            return result

        with mock.patch.object(support, "account_service", self.service), mock.patch.object(
            self.service, "restore_expired_image_cooldowns", side_effect=restore,
        ):
            thread = support.start_image_cooldown_watcher(stop)
            try:
                self.assertTrue(restored.wait(timeout=2))
            finally:
                stop.set()
                thread.join(timeout=2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(self.service.get_account("token-a")["status"], "正常")

    def test_settings_defaults_bounds_and_persistence(self) -> None:
        path = Path(self.tmp.name) / "config.json"
        path.write_text('{"auth-key": "test-auth"}', encoding="utf-8")
        store = ConfigStore(path)
        self.assertFalse(store.image_success_cooldown_enabled)
        self.assertEqual(store.image_success_cooldown_minutes, 1)
        result = store.update({"image_success_cooldown_enabled": "on", "image_success_cooldown_minutes": "7"})
        self.assertTrue(result["image_success_cooldown_enabled"])
        self.assertEqual(result["image_success_cooldown_minutes"], 7)
        reloaded = ConfigStore(path)
        self.assertTrue(reloaded.image_success_cooldown_enabled)
        self.assertEqual(reloaded.image_success_cooldown_minutes, 7)
        for value, expected in ((0, 1), (-1, 1), (99999, 1440), ("invalid", 1), (None, 1)):
            with self.subTest(value=value):
                store.update({"image_success_cooldown_minutes": value})
                self.assertEqual(store.image_success_cooldown_minutes, expected)


if __name__ == "__main__":
    unittest.main()
