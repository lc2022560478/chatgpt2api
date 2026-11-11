from __future__ import annotations

import os
import unittest
from unittest import mock

os.environ.setdefault("CHATGPT2API_AUTH_KEY", "test-auth")

from services.openai_backend_api import ImageContentPolicyError, ImageStreamHardTimeoutError
from services.protocol import conversation
from utils.helper import UpstreamHTTPError


class ImageStreamRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.closed = False
        self.backend = mock.Mock()
        self.backend.resolve_conversation_image_urls.return_value = ["https://files.test/result.png"]
        self.request = conversation.ConversationRequest(
            prompt="draw", model="gpt-image-2", image_request_deadline=130.0,
            image_attempt_deadline=120.0,
        )
        self.last = {
            "type": "conversation.event", "conversation_id": "conv-existing",
            "file_ids": ["generated-file"], "sediment_ids": [],
        }
        for patcher in (
            mock.patch.object(conversation.time, "monotonic", return_value=100.0),
            mock.patch.object(conversation, "_get_detailed_error_from_tasks", return_value=""),
            mock.patch.object(conversation, "_download_and_format_generated_images", return_value=[{"b64_json": "image"}]),
            mock.patch.object(conversation, "logger"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def _run(self, error: Exception | None):
        def events(*_args, **_kwargs):
            try:
                yield self.last
                if error is not None:
                    raise error
            finally:
                self.closed = True

        with mock.patch.object(conversation, "conversation_events", events):
            return list(conversation.stream_image_outputs(self.backend, self.request))

    def test_connection_loss_delivers_known_image_without_regeneration_or_poll(self) -> None:
        def resolve(*_args, **_kwargs):
            self.assertTrue(self.closed)
            return ["https://files.test/result.png"]

        self.backend.resolve_conversation_image_urls.side_effect = resolve
        result = self._run(RuntimeError("Failed to perform, curl: (56) Connection closed abruptly"))

        self.assertEqual(result[-1].kind, "result")
        self.assertEqual(result[-1].conversation_id, "conv-existing")
        self.backend.resolve_conversation_image_urls.assert_called_once_with(
            "conv-existing", ["generated-file"], [], poll=False, poll_timeout_secs=20.0,
        )

    def test_connection_loss_polls_existing_conversation_with_remaining_budget(self) -> None:
        self.last.update(file_ids=[], text="Working on it")
        result = self._run(ConnectionResetError("connection reset"))

        self.assertEqual(result[-1].kind, "result")
        self.backend.resolve_conversation_image_urls.assert_called_once_with(
            "conv-existing", [], [], poll_timeout_secs=20.0,
        )

    def test_temporarily_unavailable_known_url_falls_back_to_same_conversation(self) -> None:
        self.backend.resolve_conversation_image_urls.side_effect = [[], ["https://files.test/result.png"]]
        result = self._run(ConnectionError("connection reset"))

        self.assertEqual(result[-1].kind, "result")
        self.assertEqual(self.backend.resolve_conversation_image_urls.call_count, 2)
        self.assertEqual(self.backend.resolve_conversation_image_urls.call_args.kwargs["poll_timeout_secs"], 20.0)

    def test_stream_watchdog_can_recover_only_with_remaining_attempt_budget(self) -> None:
        self.last["file_ids"] = []
        result = self._run(ImageStreamHardTimeoutError("stream watchdog timeout"))
        self.assertEqual(result[-1].kind, "result")

    def test_no_conversation_id_does_not_guess_or_poll_another_conversation(self) -> None:
        self.last["conversation_id"] = ""
        error = ConnectionResetError("connection reset")
        with self.assertRaises(ConnectionResetError) as raised:
            self._run(error)
        self.assertIs(raised.exception, error)
        self.backend.resolve_conversation_image_urls.assert_not_called()

    def test_exhausted_attempt_does_not_start_recovery(self) -> None:
        self.request.image_attempt_deadline = 100.0
        error = ImageStreamHardTimeoutError("stream timed out")
        with self.assertRaises(ImageStreamHardTimeoutError) as raised:
            self._run(error)
        self.assertIs(raised.exception, error)
        self.assertEqual(error.conversation_id, "conv-existing")
        self.backend.resolve_conversation_image_urls.assert_not_called()

    def test_authentication_policy_and_unknown_errors_are_not_recovered(self) -> None:
        for error in (
            UpstreamHTTPError("/backend-api/conversation", 401, {"error": "unauthorized"}),
            ImageContentPolicyError("rejected by policy"),
            ValueError("invalid upstream payload"),
        ):
            with self.subTest(error=type(error).__name__):
                with self.assertRaises(type(error)) as raised:
                    self._run(error)
                self.assertIs(raised.exception, error)
                self.backend.resolve_conversation_image_urls.assert_not_called()

    def test_blocked_stream_does_not_recover_after_network_error(self) -> None:
        self.last["blocked"] = True
        with self.assertRaises(ConnectionResetError):
            self._run(ConnectionResetError("connection reset"))
        self.backend.resolve_conversation_image_urls.assert_not_called()

    def test_normal_stream_retains_existing_confirmation_behavior(self) -> None:
        result = self._run(None)
        self.assertEqual(result[-1].kind, "result")
        self.backend.resolve_conversation_image_urls.assert_called_once_with(
            "conv-existing", ["generated-file"], [], poll_timeout_secs=20.0,
        )


if __name__ == "__main__":
    unittest.main()
