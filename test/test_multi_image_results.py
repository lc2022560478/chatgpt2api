from __future__ import annotations

import base64
import unittest
from unittest import mock

from services.config import config
from services.openai_backend_api import ImagePollTimeoutError, OpenAIBackendAPI
from services.protocol import conversation
from services.protocol.conversation import ImageOutput, extract_conversation_ids
from services.protocol.openai_v1_response import stream_image_response


def _conversation(file_ids: list[str], sediment_ids: list[str] | None = None) -> dict:
    parts: list[object] = [
        {"content_type": "image_asset_pointer", "asset_pointer": f"file-service://{file_id}"}
        for file_id in file_ids
    ]
    parts.extend(f"sediment://{sediment_id}" for sediment_id in (sediment_ids or []))
    return {
        "mapping": {
            "tool": {
                "message": {
                    "author": {"role": "tool"},
                    "create_time": 1,
                    "metadata": {"async_task_type": "image_gen"},
                    "content": {"content_type": "multimodal_text", "parts": parts},
                }
            }
        }
    }


class FakeBackend(OpenAIBackendAPI):
    def __init__(self, conversations: list[dict] | None = None) -> None:
        self.conversations = conversations or []
        self.calls = 0
        self.file_urls: dict[str, str] = {}
        self.sediment_urls: dict[str, str] = {}

    def _get_conversation(self, conversation_id: str) -> dict:
        self.calls += 1
        index = min(self.calls - 1, len(self.conversations) - 1)
        return self.conversations[index]

    def _get_file_download_url(self, file_id: str) -> str:
        return self.file_urls.get(file_id, "")

    def _get_attachment_download_url(self, conversation_id: str, attachment_id: str) -> str:
        return self.sediment_urls.get(attachment_id, "")


class MultiImageResultTests(unittest.TestCase):
    def test_stream_id_extractor_keeps_full_file_ids(self) -> None:
        payload = (
            '{"conversation_id":"conv-1"} '
            'file-service://file-first_123-extra sediment://sed-second_456-extra'
        )

        conversation_id, file_ids, sediment_ids = extract_conversation_ids(payload)

        self.assertEqual(conversation_id, "conv-1")
        self.assertEqual(file_ids, ["file-first_123-extra"])
        self.assertEqual(sediment_ids, ["sed-second_456-extra"])

    def test_conversation_record_extractor_finds_all_generated_assets(self) -> None:
        backend = FakeBackend()
        conversation = {
            "mapping": {
                "user": {
                    "message": {
                        "author": {"role": "user"},
                        "content": {"parts": ["file-service://file-user-input"]},
                    }
                },
                "tool": {
                    "message": {
                        "author": {"role": "tool"},
                        "create_time": 1,
                        "metadata": {
                            "async_task_type": "image_gen",
                            "nested": {"asset": "file-service://file-second"},
                        },
                        "content": {
                            "content_type": "text",
                            "parts": [
                                {"content_type": "image_asset_pointer", "asset_pointer": "file-service://file-first"},
                                "sediment://sed-first",
                            ],
                        },
                    }
                },
                "assistant": {
                    "message": {
                        "author": {"role": "assistant"},
                        "create_time": 2,
                        "metadata": {},
                        "content": {
                            "parts": [
                                {"content_type": "image_asset_pointer", "asset_pointer": "file-service://file-third"}
                            ]
                        },
                    }
                },
            }
        }

        records = backend._extract_image_tool_records(conversation)
        file_ids = [file_id for record in records for file_id in record["file_ids"]]
        sediment_ids = [sediment_id for record in records for sediment_id in record["sediment_ids"]]

        self.assertEqual(file_ids, ["file-first", "file-second", "file-third"])
        self.assertEqual(sediment_ids, ["sed-first"])

    def test_poll_waits_for_generated_asset_ids_to_settle(self) -> None:
        backend = FakeBackend([
            _conversation(["file-one"]),
            _conversation(["file-one", "file-two"], ["sed-one"]),
            _conversation(["file-one", "file-two"], ["sed-one"]),
        ])

        with (
            mock.patch.dict(config.data, {"image_poll_initial_wait_secs": 0, "image_poll_interval_secs": 0.5}),
            mock.patch("services.openai_backend_api.time.sleep", lambda _seconds: None),
        ):
            file_ids, sediment_ids = backend._poll_image_results("conv-1", timeout_secs=10)

        self.assertEqual(file_ids, ["file-one", "file-two"])
        self.assertEqual(sediment_ids, ["sed-one"])
        self.assertEqual(backend.calls, 3)

    def test_resolver_uses_file_and_sediment_urls(self) -> None:
        backend = FakeBackend()
        backend.file_urls = {"file-one": "https://files.test/one.png"}
        backend.sediment_urls = {
            "sed-one": "https://attachments.test/one.png",
            "sed-two": "https://attachments.test/two.png",
        }

        urls = backend._resolve_image_urls("conv-1", ["file-one"], ["sed-one", "sed-two"])

        self.assertEqual(urls, [
            "https://files.test/one.png",
            "https://attachments.test/one.png",
            "https://attachments.test/two.png",
        ])

    def test_resolver_keeps_stream_ids_when_poll_extension_fails(self) -> None:
        backend = FakeBackend()
        backend.file_urls = {"file-one": "https://files.test/one.png"}
        backend._get_conversation = mock.Mock(side_effect=RuntimeError("poll failed"))

        with mock.patch("services.openai_backend_api.time.sleep", lambda _seconds: None):
            urls = backend.resolve_conversation_image_urls("conv-1", ["file-one"], [], poll=True)

        self.assertEqual(urls, ["https://files.test/one.png"])

    def test_text_reply_poll_uses_remaining_attempt_deadline(self) -> None:
        class RecordingPollBackend:
            def __init__(self) -> None:
                self.resolver_timeouts: list[float] = []
                self.retry_timeouts: list[float] = []

            def resolve_conversation_image_urls(self, *_args, poll_timeout_secs: float, **_kwargs) -> list[str]:
                self.resolver_timeouts.append(poll_timeout_secs)
                return []

            def _poll_image_results(self, _conversation_id: str, timeout_secs: float, *_args) -> tuple[list[str], list[str]]:
                self.retry_timeouts.append(timeout_secs)
                raise ImagePollTimeoutError("still processing")

        backend = RecordingPollBackend()
        request = conversation.ConversationRequest(
            prompt="draw",
            model="gpt-image-2",
            image_request_deadline=200.0,
            image_attempt_deadline=105.0,
        )
        events = iter([{
            "type": "conversation.event",
            "conversation_id": "conv-1",
            "text": '{"referenced_image_ids": ["file-one"]}',
            "file_ids": [],
            "sediment_ids": [],
            "turn_use_case": "image gen",
        }])
        settings = dict(config.data)
        settings["image_poll_timeout_secs"] = 300

        with (
            mock.patch.object(conversation, "conversation_events", return_value=events),
            mock.patch.object(conversation, "_get_detailed_error_from_tasks", return_value=""),
            mock.patch.object(conversation.time, "monotonic", return_value=100.0),
            mock.patch.object(config, "data", settings),
        ):
            list(conversation.stream_image_outputs(backend, request))

        self.assertEqual(backend.resolver_timeouts, [5.0])
        self.assertEqual(backend.retry_timeouts, [5.0])

    def test_codex_stream_uses_stream_timeout_not_poll_timeout(self) -> None:
        class RecordingBackend:
            def __init__(self) -> None:
                self.timeouts: list[float] = []

            def iter_codex_image_response_events(self, **kwargs):
                self.timeouts.append(kwargs["timeout_secs"])
                return iter(())

        backend = RecordingBackend()
        request = conversation.ConversationRequest(
            prompt="draw",
            model="codex-gpt-image-2",
            image_request_deadline=1000.0,
            image_attempt_deadline=1000.0,
        )
        settings = dict(config.data)
        settings.update(image_stream_timeout_secs=80, image_poll_timeout_secs=60)

        with (
            mock.patch.object(conversation.time, "monotonic", return_value=900.0),
            mock.patch.object(conversation, "_codex_response_images", return_value=["aW1hZ2U="]),
            mock.patch.object(conversation, "format_image_result", return_value={"data": [{"b64_json": "image"}]}),
            mock.patch.object(config, "data", settings),
        ):
            outputs = list(conversation.stream_codex_image_outputs(backend, request))

        self.assertEqual(backend.timeouts, [80.0])
        self.assertEqual(outputs[0].kind, "result")

    def test_responses_stream_emits_all_image_output_items(self) -> None:
        first = base64.b64encode(b"first").decode("ascii")
        second = base64.b64encode(b"second").decode("ascii")
        events = list(stream_image_response(
            [ImageOutput(
                kind="result",
                model="gpt-image-2",
                index=1,
                total=1,
                data=[{"b64_json": first}, {"b64_json": second}],
            )],
            "draw two options",
            "gpt-image-2",
        ))

        done_events = [event for event in events if event.get("type") == "response.output_item.done"]
        completed = next(event["response"] for event in events if event.get("type") == "response.completed")

        self.assertEqual([event["output_index"] for event in done_events], [0, 1])
        self.assertEqual([item["result"] for item in completed["output"]], [first, second])


if __name__ == "__main__":
    unittest.main()
