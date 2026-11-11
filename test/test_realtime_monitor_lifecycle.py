from __future__ import annotations

import unittest
from unittest import mock

from services.log_service import LoggedCall
from services.protocol import (
    conversation,
    openai_v1_chat_complete,
    openai_v1_image_generations,
    openai_v1_response,
)
from services.protocol.conversation import ConversationRequest
from services.realtime_monitor_service import RealtimeMonitorService


class RealtimeMonitorLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_logged_image_call_tracks_generation_and_completion(self):
        monitor = RealtimeMonitorService()
        call = LoggedCall(
            {"id": "admin", "name": "primary", "role": "admin"},
            "/v1/images/generations",
            "gpt-image-2",
            "image generation",
        )

        with (
            mock.patch("services.realtime_monitor_service.realtime_monitor_service", monitor),
            mock.patch("services.log_service.log_service.add"),
        ):
            result = await call.run(
                lambda payload: {"data": [], "_account_email": "image@example.test"},
                {"prompt": "test"},
            )

        self.assertEqual(result, {"data": []})
        snapshot = monitor.snapshot()
        self.assertEqual(snapshot["summary"]["active"], 0)
        self.assertEqual(snapshot["summary"]["success"], 1)
        self.assertEqual(snapshot["recent"][0]["account_email"], "image@example.test")
        self.assertIn("generating", [event["stage"] for event in snapshot["events"]])

    def test_image_handler_forwards_monitor_call_id(self):
        captured: list[ConversationRequest] = []

        def capture(request: ConversationRequest):
            captured.append(request)
            return iter(())

        with (
            mock.patch.object(openai_v1_image_generations, "stream_image_outputs_with_pool", side_effect=capture),
            mock.patch.object(openai_v1_image_generations, "collect_image_outputs", return_value={"data": []}),
            mock.patch.object(openai_v1_image_generations, "image_usage", return_value={}),
        ):
            openai_v1_image_generations.handle(
                {"prompt": "test", "model": "gpt-image-2", "_monitor_call_id": "call-123"},
            )

        self.assertEqual(captured[0].monitor_call_id, "call-123")

    def test_compatibility_image_handlers_forward_monitor_call_id(self):
        chat_requests: list[ConversationRequest] = []
        response_requests: list[ConversationRequest] = []

        def capture_chat(request: ConversationRequest):
            chat_requests.append(request)
            return iter(())

        def capture_response(request: ConversationRequest):
            response_requests.append(request)
            return iter(())

        chat_body = {
            "model": "gpt-image-2",
            "messages": [{"role": "user", "content": "test"}],
            "_monitor_call_id": "chat-call-123",
        }
        with (
            mock.patch.object(openai_v1_chat_complete, "stream_image_outputs_with_pool", side_effect=capture_chat),
            mock.patch.object(openai_v1_chat_complete, "collect_image_outputs", return_value={"data": []}),
            mock.patch.object(openai_v1_chat_complete, "image_usage", return_value={}),
        ):
            openai_v1_chat_complete.image_chat_response(chat_body)
            list(openai_v1_chat_complete.image_chat_events(chat_body))

        response_body = {
            "model": "gpt-image-2",
            "input": "test",
            "tools": [{"type": "image_generation"}],
            "_monitor_call_id": "response-call-123",
        }
        with mock.patch.object(openai_v1_response, "stream_image_outputs_with_pool", side_effect=capture_response):
            with self.assertRaisesRegex(RuntimeError, "image generation failed"):
                list(openai_v1_response.response_events(response_body))

        self.assertEqual([request.monitor_call_id for request in chat_requests], ["chat-call-123", "chat-call-123"])
        self.assertEqual(response_requests[0].monitor_call_id, "response-call-123")

    def test_image_stage_publishes_account_retry_details(self):
        monitor = RealtimeMonitorService()
        request = ConversationRequest(monitor_call_id="call-123")
        monitor.start("call-123", endpoint="/v1/images/generations", model="gpt-image-2", summary="image")

        with mock.patch("services.realtime_monitor_service.realtime_monitor_service", monitor):
            conversation._monitor_image_stage(
                request,
                "retrying_account",
                account_email="failed@example.test",
                previous_account_email="failed@example.test",
                retry_count=1,
                attempt=1,
                max_attempts=3,
            )

        record = monitor.snapshot()["active"][0]
        self.assertEqual(record["stage"], "retrying_account")
        self.assertEqual(record["retry_count"], 1)


if __name__ == "__main__":
    unittest.main()
