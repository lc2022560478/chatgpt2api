from datetime import datetime, timedelta, timezone
import tempfile
from pathlib import Path
import unittest
from unittest import mock

from services.account_service import AccountService
from services.config import config
from services.image_failure import extract_image_quota_restore_at, is_image_quota_error_text
from services.protocol import conversation
from services.storage.json_storage import JSONStorageBackend
from test.test_image_slot_release import FakeBackend, successful_output
from utils.helper import UpstreamHTTPError


QUOTA_MESSAGE = "你已达到 Free 套餐的图像生成请求上限。上限将在 10小时 后重置，届时可创建更多图像。"


class QuotaCountdownTests(unittest.TestCase):
    def test_countdowns(self):
        now = datetime(2026, 10, 8, tzinfo=timezone.utc)
        cases = [
            (QUOTA_MESSAGE, timedelta(hours=10)),
            ("上限将在1天2小时30分钟后重置", timedelta(days=1, hours=2, minutes=30)),
            ("上限将在 2 小时和 15 分钟后重置", timedelta(hours=2, minutes=15)),
            ("Your image generation limit resets in 1 hour 30 minutes.", timedelta(minutes=90)),
            ("Your image generation limit resets in 1.5 hours.", timedelta(minutes=90)),
            ("上限將在 20 分鐘後重設", timedelta(minutes=20)),
        ]
        for message, duration in cases:
            with self.subTest(message=message):
                actual = extract_image_quota_restore_at({"error": {"message": message}}, now=now)
                self.assertEqual(actual, (now + duration).isoformat())

    def test_missing_or_invalid_countdown_is_not_invented(self):
        for text in ("image generation request limit", "重置时间未知", "resets in -1 hours", "resets in 999999999999 days"):
            self.assertIsNone(extract_image_quota_restore_at(text))
        self.assertFalse(is_image_quota_error_text("Please draw a clock showing 10 hours"))
        self.assertTrue(is_image_quota_error_text(QUOTA_MESSAGE))


class QuotaPoolIntegrationTests(unittest.TestCase):
    def setUp(self):
        tmp = self.enterContext(tempfile.TemporaryDirectory())
        self.service = AccountService(JSONStorageBackend(Path(tmp) / "accounts.json"))
        self.enterContext(mock.patch.object(config, "data", {
            **config.data,
            "image_success_cooldown_enabled": False,
            "image_account_retry_enabled": True,
            "image_max_account_attempts": 2,
            "auto_remove_rate_limited_accounts": True,
            "image_remove_conversation_after_result": False,
            "image_remove_conversation_always": False,
        }))
        self.enterContext(mock.patch.object(self.service, "_save_cumulative_total"))
        self.enterContext(mock.patch("services.account_service.log_service.add"))
        self.service.add_account_items([
            {"access_token": "account-a", "status": "正常", "quota": 5},
            {"access_token": "account-b", "status": "正常", "quota": 5},
        ])
        self.service.fetch_remote_info = lambda token, *_a, **_kw: self.service.get_account(token)

    def run_rejection(self, mode):
        def stream(backend, *_args):
            if backend.access_token == "account-a":
                if mode == "http":
                    raise UpstreamHTTPError("/backend-api/conversation", 429, {"error": {"message": QUOTA_MESSAGE}})
                yield conversation.ImageOutput(kind="message", model="gpt-image-2", index=1, total=1,
                                               text=QUOTA_MESSAGE, conversation_id="quota-turn")
            else:
                yield from successful_output()

        before = datetime.now(timezone.utc)
        with mock.patch.multiple(conversation, account_service=self.service, OpenAIBackendAPI=FakeBackend,
                                 stream_image_outputs=stream):
            # message_as_error=False covers the chat route as well as /images routes.
            outputs = list(conversation.stream_image_outputs_with_pool(
                conversation.ConversationRequest(prompt="draw", model="gpt-image-2", message_as_error=False)))
        self.assertEqual(outputs[0].kind, "result")
        account = self.service.get_account("account-a")
        self.assertEqual((account["status"], account["quota"], account["fail"]), ("限流", 5, 1))
        deadline = datetime.fromisoformat(account["image_quota_cooldown_until"])
        self.assertGreaterEqual(deadline, before + timedelta(hours=10))
        self.assertLess(deadline, datetime.now(timezone.utc) + timedelta(hours=10, seconds=1))
        self.assertEqual(self.service._image_inflight, {})
        reloaded = AccountService(self.service.storage)
        self.assertEqual(reloaded.get_account("account-a")["status"], "限流")

    def test_sse_quota_message_limits_account_and_retries(self):
        self.run_rejection("sse")

    def test_http_quota_message_limits_account_and_retries(self):
        self.run_rejection("http")


if __name__ == "__main__":
    unittest.main()
