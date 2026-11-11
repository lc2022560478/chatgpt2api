from __future__ import annotations

import unittest
from unittest import mock

from curl_cffi import requests

from services.config import config
from services.openai_backend_api import ImagePollTimeoutError, OpenAIBackendAPI
from utils.helper import UpstreamHTTPError


class ImageResultRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = 1000.0
        self.backend = object.__new__(OpenAIBackendAPI)
        self.backend._query_backend_tasks = mock.Mock(return_value=[])
        self.backend._get_conversation = mock.Mock(return_value={})
        self.backend._extract_image_tool_records = mock.Mock(return_value=[{
            "file_ids": ["generated-file"],
            "sediment_ids": ["generated-attachment"],
        }])
        self.backend._find_content_policy_error_in_conversation = mock.Mock(return_value="")
        self.backend._get_file_download_url = mock.Mock(return_value="https://files.test/result.png")
        self.backend._get_attachment_download_url = mock.Mock(return_value="https://files.test/attachment.png")
        settings = dict(config.data)
        settings.update(
            image_poll_initial_wait_secs=0,
            image_poll_interval_secs=0.5,
            image_check_before_hit_enabled=True,
            image_settle_enabled=True,
            image_settle_secs=3,
        )
        for patcher in (
            mock.patch.object(config, "data", settings),
            mock.patch("services.openai_backend_api.time.time", side_effect=lambda: self.clock),
            mock.patch("services.openai_backend_api.time.sleep", side_effect=self._sleep),
            mock.patch("services.openai_backend_api.random.uniform", return_value=0),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def _sleep(self, seconds: float) -> None:
        self.clock += seconds

    def test_new_result_survives_settle_wait_exhausting_poll_budget(self) -> None:
        result = self.backend._poll_image_results("conv-result", timeout_secs=1)

        self.assertEqual(result, (["generated-file"], ["generated-attachment"]))
        self.assertEqual(self.clock, 1001.0)
        self.backend._get_conversation.assert_called_once_with("conv-result")

    def test_resolver_receives_ids_found_in_last_poll(self) -> None:
        urls = self.backend.resolve_conversation_image_urls(
            "conv-result", [], [], poll_timeout_secs=1,
        )

        self.assertEqual(urls, [
            "https://files.test/result.png",
            "https://files.test/attachment.png",
        ])
        self.assertEqual(self.clock, 1001.0)

    def test_initial_result_survives_settle_wait_without_another_http_request(self) -> None:
        result = self.backend._poll_image_results(
            "conv-result", timeout_secs=1, initial_file_ids=["stream-file"],
        )

        self.assertEqual(result, (["stream-file"], []))
        self.backend._get_conversation.assert_not_called()
        self.backend._query_backend_tasks.assert_not_called()

    def test_known_result_survives_transient_confirmation_error_at_deadline(self) -> None:
        config.data["image_settle_secs"] = 1
        self.backend._get_conversation.side_effect = [
            {}, requests.exceptions.RequestException("temporary connection reset"),
        ]

        result = self.backend._poll_image_results("conv-result", timeout_secs=2)

        self.assertEqual(result, (["generated-file"], ["generated-attachment"]))
        self.assertEqual(self.clock, 1002.0)
        self.assertEqual(self.backend._get_conversation.call_count, 2)

    def test_poll_without_any_result_still_times_out(self) -> None:
        self.backend._extract_image_tool_records.return_value = []

        with self.assertRaises(ImagePollTimeoutError) as raised:
            self.backend._poll_image_results("conv-pending", timeout_secs=1)

        self.assertEqual(raised.exception.conversation_id, "conv-pending")
        self.assertEqual(self.clock, 1001.0)

    def test_confirmation_authentication_error_is_not_swallowed(self) -> None:
        config.data["image_settle_secs"] = 1
        auth_error = UpstreamHTTPError("/backend-api/conversation", 401, {"error": "unauthorized"})
        self.backend._get_conversation.side_effect = [{}, auth_error]

        with self.assertRaises(UpstreamHTTPError) as raised:
            self.backend._poll_image_results("conv-result", timeout_secs=2)

        self.assertIs(raised.exception, auth_error)


if __name__ == "__main__":
    unittest.main()
