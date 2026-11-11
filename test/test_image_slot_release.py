from __future__ import annotations

import os
import unittest
from collections import Counter
from unittest import mock

os.environ.setdefault("CHATGPT2API_AUTH_KEY", "test-auth")

from services.account_service import ImagePreflightTransportError
from services.openai_backend_api import (
    ImagePollTimeoutError,
    ImageStreamHardTimeoutError,
    OpenAIBackendAPI,
)
from services.protocol import conversation
from utils.helper import UpstreamHTTPError


class FakeAccountService:
    def __init__(self, tokens: list[str]) -> None:
        self.tokens = iter(tokens)
        self.inflight: Counter[str] = Counter()
        self.marks: list[tuple[str, bool]] = []
        self.releases: list[str] = []
        self.selected_tokens: list[str] = []
        self.selection_exclusions: list[set[str]] = []
        self.selection_timeouts: list[float | None] = []
        self.refresh_calls: list[tuple[str, dict[str, object]]] = []
        self.invalid_removals: list[tuple[str, str]] = []

    def get_available_access_token(self, **kwargs) -> str:
        excluded = set(kwargs.get("excluded_tokens") or set())
        self.selection_exclusions.append(excluded)
        self.selection_timeouts.append(kwargs.get("timeout_secs"))
        try:
            token = next(self.tokens)
        except StopIteration as exc:
            raise RuntimeError("no alternate image account") from exc
        if token in excluded:
            raise AssertionError(f"retried excluded account: {token}")
        self.selected_tokens.append(token)
        self.inflight[token] += 1
        return token

    @staticmethod
    def get_account(token: str) -> dict[str, str]:
        return {"email": f"{token}@example.test"}

    def release_image_slot(self, token: str) -> None:
        self.releases.append(token)
        self.inflight[token] -= 1
        if not self.inflight[token]:
            del self.inflight[token]

    def mark_image_result(self, token: str, success: bool) -> None:
        self.marks.append((token, success))
        self.release_image_slot(token)

    def refresh_access_token(self, token: str, **kwargs) -> str:
        self.refresh_calls.append((token, dict(kwargs)))
        return token

    def remove_invalid_token(self, token: str, event: str) -> None:
        self.invalid_removals.append((token, event))


class ReleasingThenFailingMarkAccountService(FakeAccountService):
    def mark_image_result(self, token: str, success: bool) -> None:
        self.marks.append((token, success))
        self.release_image_slot(token)
        raise OSError("image counter persistence failed")


class AccountLookupFailureService(FakeAccountService):
    @staticmethod
    def get_account(_token: str) -> dict[str, str]:
        raise RuntimeError("account lookup failed")


class PreflightDelayAccountService(FakeAccountService):
    def __init__(self, tokens: list[str], clock: list[float]) -> None:
        super().__init__(tokens)
        self.clock = clock

    def get_available_access_token(self, **kwargs) -> str:
        token = super().get_available_access_token(**kwargs)
        self.clock[0] += float(kwargs.get("timeout_secs") or 0)
        return token


class FakeBackend:
    instances: list["FakeBackend"] = []

    def __init__(self, access_token: str) -> None:
        self.access_token = access_token
        self.closed = False
        self.progress_callback = None
        self.instances.append(self)

    def close(self) -> None:
        self.closed = True


def successful_output(*_args):
    yield conversation.ImageOutput(
        kind="result",
        model="gpt-image-2",
        index=1,
        total=1,
        data=[{"b64_json": "image"}],
    )


class ImageSlotReleaseTests(unittest.TestCase):
    def setUp(self) -> None:
        FakeBackend.instances = []

    def _patches(self, service: FakeAccountService, stream_fn):
        return mock.patch.multiple(
            conversation,
            account_service=service,
            OpenAIBackendAPI=FakeBackend,
            stream_image_outputs=stream_fn,
            is_supported_image_model=lambda _model: True,
            is_codex_image_model=lambda _model: False,
        )

    @staticmethod
    def _retry_config(
        enabled: bool = True,
        max_attempts: int = 3,
        request_timeout_secs: int = 300,
    ):
        data = dict(conversation.config.data)
        data.update(
            image_account_retry_enabled=enabled,
            image_max_account_attempts=max_attempts,
            image_request_timeout_secs=request_timeout_secs,
        )
        return mock.patch.object(conversation.config, "data", data)

    @staticmethod
    def _request(message_as_error: bool = False) -> conversation.ConversationRequest:
        return conversation.ConversationRequest(
            prompt="draw",
            model="gpt-image-2",
            message_as_error=message_as_error,
        )

    def assert_slots_finalized_once(
        self,
        service: FakeAccountService,
        expected_marks: list[tuple[str, bool]],
    ) -> None:
        self.assertEqual(service.marks, expected_marks)
        self.assertEqual(service.releases, [token for token, _ in expected_marks])
        self.assertEqual(service.inflight, {})

    def test_releases_slot_after_success(self) -> None:
        service = FakeAccountService(["token-1"])
        with self._patches(service, successful_output):
            outputs = list(conversation.stream_image_outputs_with_pool(self._request()))

        self.assertEqual(len(outputs), 1)
        self.assert_slots_finalized_once(service, [("token-1", True)])
        self.assertTrue(FakeBackend.instances[0].closed)

    def test_poll_timeout_retries_a_different_account(self) -> None:
        service = FakeAccountService(["account-a", "account-b"])

        def timeout_then_success(backend, *_args):
            if backend.access_token == "account-a":
                raise ImagePollTimeoutError("account-a timed out")
            yield from successful_output()

        with self._patches(service, timeout_then_success), self._retry_config():
            outputs = list(conversation.stream_image_outputs_with_pool(self._request()))

        self.assertEqual(len(outputs), 1)
        self.assertEqual(service.selection_exclusions, [set(), {"account-a"}])
        self.assert_slots_finalized_once(service, [("account-a", False), ("account-b", True)])

    def test_23_second_regression_caps_preflight_and_does_not_penalize_sse_watchdog(self) -> None:
        clock = [1000.0]
        service = PreflightDelayAccountService(["account-a", "account-b"], clock)
        attempt_windows: list[float] = []

        def watchdog_then_success(backend, request, *_args):
            attempt_windows.append((request.image_attempt_deadline or 0) - clock[0])
            if backend.access_token == "account-a":
                raise ImageStreamHardTimeoutError("image generation stream exceeded 80 seconds")
            yield from successful_output()

        settings = dict(conversation.config.data)
        settings.update(
            image_account_retry_enabled=True,
            image_max_account_attempts=2,
            image_request_timeout_secs=300,
            image_stream_timeout_secs=80,
            image_poll_timeout_secs=120,
        )
        with (
            self._patches(service, watchdog_then_success),
            mock.patch.object(conversation.config, "data", settings),
            mock.patch.object(conversation.time, "monotonic", side_effect=lambda: clock[0]),
        ):
            outputs = list(conversation.stream_image_outputs_with_pool(self._request()))

        self.assertEqual([output.kind for output in outputs], ["result"])
        self.assertEqual(service.selection_timeouts, [30.0, 30.0])
        self.assertGreaterEqual(attempt_windows[0], 80.0)
        self.assertNotIn(("account-a", False), service.marks)
        self.assertEqual(service.marks, [("account-b", True)])
        self.assertEqual(service.releases, ["account-a", "account-b"])
        self.assertEqual(service.inflight, {})

    def test_sse_watchdog_uses_stream_timeout_not_poll_timeout(self) -> None:
        backend = object.__new__(OpenAIBackendAPI)
        backend.access_token = "token"
        backend._report_progress = lambda _step: None
        backend._upload_image = lambda *_args, **_kwargs: {"file_id": "file"}
        backend._bootstrap = lambda **_kwargs: None
        backend._get_chat_requirements = lambda **_kwargs: object()
        backend._prepare_image_conversation = lambda *_args, **_kwargs: "conduit"
        backend._start_image_generation = lambda *_args, **_kwargs: object()
        captured: list[tuple[float, float | None]] = []

        def capped(_response, hard_cap_secs, *, deadline=None):
            captured.append((hard_cap_secs, deadline))
            yield "done"

        backend._iter_sse_payloads_capped = capped
        settings = dict(conversation.config.data)
        settings.update(image_stream_timeout_secs=80, image_poll_timeout_secs=120)
        deadline = 1300.0
        with mock.patch.object(conversation.config, "data", settings):
            self.assertEqual(
                list(backend._stream_picture_conversation("draw", "gpt-image-2", [], deadline=deadline)),
                ["done"],
            )

        self.assertEqual(captured, [(80.0, deadline)])

    def test_account_retries_share_one_request_deadline(self) -> None:
        service = FakeAccountService(["account-a", "account-b"])
        observed_deadlines: list[float | None] = []

        def timeout_then_success(backend, request, *_args):
            observed_deadlines.append(request.image_request_deadline)
            if backend.access_token == "account-a":
                raise ImagePollTimeoutError("account-a timed out")
            yield from successful_output()

        with (
            self._patches(service, timeout_then_success),
            self._retry_config(request_timeout_secs=30),
            mock.patch.object(conversation.time, "monotonic", return_value=1000.0),
        ):
            outputs = list(conversation.stream_image_outputs_with_pool(self._request()))

        self.assertEqual(len(outputs), 1)
        self.assertEqual(observed_deadlines, [1030.0, 1030.0])
        self.assertTrue(all(timeout is not None and timeout <= 30 for timeout in service.selection_timeouts))
        self.assert_slots_finalized_once(service, [("account-a", False), ("account-b", True)])

    def test_expired_request_deadline_does_not_switch_accounts(self) -> None:
        service = FakeAccountService(["account-a", "account-b"])
        clock = [1000.0]
        observed_deadlines: list[float | None] = []

        def timeout_after_request_deadline(backend, request, *_args):
            observed_deadlines.append(request.image_request_deadline)
            clock[0] = (request.image_request_deadline or clock[0]) + 0.1
            raise ImagePollTimeoutError(f"{backend.access_token} timed out")
            yield None

        with (
            self._patches(service, timeout_after_request_deadline),
            self._retry_config(request_timeout_secs=30),
            mock.patch.object(conversation.time, "monotonic", side_effect=lambda: clock[0]),
        ):
            with self.assertRaises(conversation.ImageGenerationError) as raised:
                list(conversation.stream_image_outputs_with_pool(self._request()))

        self.assertEqual(raised.exception.code, "upstream_timeout")
        self.assertIn("图片生成请求总超时", str(raised.exception))
        self.assertEqual(observed_deadlines, [1030.0])
        self.assertEqual(service.selected_tokens, ["account-a"])
        self.assert_slots_finalized_once(service, [("account-a", False)])

    def test_normal_message_returns_without_switching_accounts(self) -> None:
        service = FakeAccountService(["account-a", "account-b"])

        def plain_message(*_args):
            yield conversation.ImageOutput(
                kind="message",
                model="gpt-image-2",
                index=1,
                total=1,
                text="The model returned a text response.",
            )

        with self._patches(service, plain_message), self._retry_config():
            outputs = list(conversation.stream_image_outputs_with_pool(self._request()))

        self.assertEqual([output.kind for output in outputs], ["message"])
        self.assertEqual(service.selected_tokens, ["account-a"])
        self.assert_slots_finalized_once(service, [("account-a", False)])

    def test_error_message_retries_another_account(self) -> None:
        service = FakeAccountService(["account-a", "account-b"])

        def message_then_success(backend, *_args):
            if backend.access_token == "account-a":
                yield conversation.ImageOutput(
                    kind="message",
                    model="gpt-image-2",
                    index=1,
                    total=1,
                    text="The model returned a text response.",
                )
                return
            yield from successful_output()

        with self._patches(service, message_then_success), self._retry_config():
            outputs = list(conversation.stream_image_outputs_with_pool(self._request(message_as_error=True)))

        self.assertEqual(outputs[0].kind, "result")
        self.assert_slots_finalized_once(service, [("account-a", False), ("account-b", True)])

    def test_content_policy_error_does_not_switch_accounts(self) -> None:
        service = FakeAccountService(["account-a", "account-b"])

        def blocked(*_args):
            yield conversation.ImageOutput(
                kind="message",
                model="gpt-image-2",
                index=1,
                total=1,
                text="blocked",
                upstream_event_type="content_policy",
            )

        with self._patches(service, blocked), self._retry_config():
            with self.assertRaises(conversation.ImageGenerationError) as raised:
                list(conversation.stream_image_outputs_with_pool(self._request(message_as_error=True)))

        self.assertEqual(raised.exception.code, "content_policy_violation")
        self.assertEqual(service.selected_tokens, ["account-a"])
        self.assert_slots_finalized_once(service, [("account-a", False)])

    def test_unauthorized_error_refreshes_and_switches_accounts(self) -> None:
        service = FakeAccountService(["account-a", "account-b"])

        def unauthorized_then_success(backend, *_args):
            if backend.access_token == "account-a":
                raise UpstreamHTTPError("/backend-api/conversation", 401, {"error": "token_invalidated"})
            yield from successful_output()

        with self._patches(service, unauthorized_then_success), self._retry_config():
            outputs = list(conversation.stream_image_outputs_with_pool(self._request()))

        self.assertEqual(len(outputs), 1)
        self.assertEqual(len(service.refresh_calls), 1)
        refreshed_token, refresh_kwargs = service.refresh_calls[0]
        self.assertEqual(refreshed_token, "account-a")
        self.assertTrue(refresh_kwargs.pop("timeout_secs") > 0)
        self.assertEqual(refresh_kwargs, {"force": True, "event": "image_stream"})
        self.assertEqual(service.invalid_removals, [("account-a", "image_stream")])
        self.assert_slots_finalized_once(service, [("account-a", False), ("account-b", True)])

    def test_parallel_timeout_preserves_timeout_error_code(self) -> None:
        request = self._request()
        request.n = 2
        settings = dict(conversation.config.data)
        settings["image_parallel_generation"] = True

        def timed_out(*_args, **_kwargs):
            raise conversation.ImageGenerationError("timed out", code="upstream_timeout")

        with (
            mock.patch.object(conversation, "_generate_single_image", side_effect=timed_out),
            mock.patch.object(conversation.config, "data", settings),
        ):
            with self.assertRaises(conversation.ImageGenerationError) as raised:
                list(conversation.stream_image_outputs_with_pool(request))

        self.assertEqual(raised.exception.code, "upstream_timeout")

    def test_rate_limit_switches_accounts_without_refresh(self) -> None:
        service = FakeAccountService(["account-a", "account-b"])

        def rate_limited_then_success(backend, *_args):
            if backend.access_token == "account-a":
                raise UpstreamHTTPError("/backend-api/conversation", 429, {"error": "rate_limit_exceeded"})
            yield from successful_output()

        with self._patches(service, rate_limited_then_success), self._retry_config():
            outputs = list(conversation.stream_image_outputs_with_pool(self._request()))

        self.assertEqual(len(outputs), 1)
        self.assertEqual(service.refresh_calls, [])
        self.assert_slots_finalized_once(service, [("account-a", False), ("account-b", True)])

    def test_curl_56_transport_failure_uses_jitter_before_account_failover(self) -> None:
        service = FakeAccountService(["account-a", "account-b"])
        raw_error = (
            "Failed to perform, curl: (56) Connection closed abruptly. "
            "See https://curl.se/libcurl/c/libcurl-errors.html first for more details."
        )

        def disconnect_then_success(backend, *_args):
            if backend.access_token == "account-a":
                raise RuntimeError(raw_error)
            yield from successful_output()

        with (
            self._patches(service, disconnect_then_success),
            self._retry_config(max_attempts=2),
            mock.patch.object(conversation.random, "uniform", return_value=0.25) as jitter,
            mock.patch.object(conversation.time, "sleep") as sleep,
            mock.patch.object(conversation.logger, "warning") as warning,
        ):
            outputs = list(conversation.stream_image_outputs_with_pool(self._request()))

        self.assertEqual([output.kind for output in outputs], ["result"])
        self.assertTrue(conversation.is_tls_connection_error(raw_error))
        jitter.assert_called_once_with(0.0, conversation.IMAGE_TRANSPORT_RETRY_BASE_SECS)
        sleep.assert_called_once_with(conversation.IMAGE_TRANSPORT_RETRY_BASE_SECS + 0.25)
        self.assertEqual(service.selection_exclusions, [set(), {"account-a"}])
        self.assert_slots_finalized_once(service, [("account-a", False), ("account-b", True)])

        log_events = [
            call.args[0]
            for call in warning.call_args_list
            if call.args and isinstance(call.args[0], dict)
        ]
        transport_failure = next(event for event in log_events if event.get("event") == "image_upstream_transport_failure")
        transport_backoff = next(event for event in log_events if event.get("event") == "image_upstream_transport_retry_backoff")
        self.assertEqual(transport_failure["stage"], "image_stream")
        self.assertEqual(transport_failure["raw_error"], raw_error)
        self.assertEqual(transport_backoff["backoff_secs"], conversation.IMAGE_TRANSPORT_RETRY_BASE_SECS + 0.25)

    def test_preflight_transport_failure_uses_jitter_without_scanning_accounts(self) -> None:
        raw_error = "Failed to perform, curl: (35) TLS connect error: OPENSSL_internal"

        class PreflightFailureThenSuccessService(FakeAccountService):
            def __init__(self) -> None:
                super().__init__(["account-b"])
                self.preflight_calls = 0

            def get_available_access_token(self, **kwargs) -> str:
                self.preflight_calls += 1
                if self.preflight_calls == 1:
                    raise ImagePreflightTransportError(raw_error)
                return super().get_available_access_token(**kwargs)

        service = PreflightFailureThenSuccessService()
        with (
            self._patches(service, successful_output),
            self._retry_config(max_attempts=2),
            mock.patch.object(conversation.random, "uniform", return_value=0.25) as jitter,
            mock.patch.object(conversation.time, "sleep") as sleep,
            mock.patch.object(conversation.logger, "warning") as warning,
        ):
            outputs = list(conversation.stream_image_outputs_with_pool(self._request()))

        self.assertEqual([output.kind for output in outputs], ["result"])
        self.assertEqual(service.preflight_calls, 2)
        self.assertEqual(service.selected_tokens, ["account-b"])
        self.assert_slots_finalized_once(service, [("account-b", True)])
        jitter.assert_called_once_with(0.0, conversation.IMAGE_TRANSPORT_RETRY_BASE_SECS)
        sleep.assert_called_once_with(conversation.IMAGE_TRANSPORT_RETRY_BASE_SECS + 0.25)

        log_events = [
            call.args[0]
            for call in warning.call_args_list
            if call.args and isinstance(call.args[0], dict)
        ]
        transport_failure = next(event for event in log_events if event.get("event") == "image_upstream_transport_failure")
        transport_backoff = next(event for event in log_events if event.get("event") == "image_upstream_transport_retry_backoff")
        self.assertEqual(transport_failure["stage"], "getting_account")
        self.assertEqual(transport_failure["raw_error"], raw_error)
        self.assertEqual(transport_backoff["stage"], "getting_account")

    def test_typed_preflight_timeout_preserves_timeout_code_without_message_matching(self) -> None:
        service = FakeAccountService([])
        with (
            self._patches(service, successful_output),
            self._retry_config(max_attempts=1),
            mock.patch.object(
                service, "get_available_access_token",
                side_effect=ImagePreflightTransportError("deadline reached", is_timeout=True),
            ),
            mock.patch.object(conversation.logger, "warning"),
        ):
            with self.assertRaises(conversation.ImageGenerationError) as raised:
                list(conversation.stream_image_outputs_with_pool(self._request()))

        self.assertEqual(raised.exception.code, "upstream_timeout")
        self.assertEqual(raised.exception.stage, "getting_account")
        self.assertEqual(raised.exception.raw_error, "deadline reached")
        self.assertEqual(service.selected_tokens, [])
        self.assertEqual(FakeBackend.instances, [])

    def test_transport_failure_preserves_account_conversation_and_raw_error(self) -> None:
        service = FakeAccountService(["account-a"])
        raw_error = "Failed to perform, curl: (56) Connection closed abruptly."

        def progress_then_disconnect(*_args):
            yield conversation.ImageOutput(
                kind="progress",
                model="gpt-image-2",
                index=1,
                total=1,
                conversation_id="conversation-123",
            )
            raise RuntimeError(raw_error)

        with (
            self._patches(service, progress_then_disconnect),
            self._retry_config(max_attempts=1),
            mock.patch.object(conversation, "_remove_image_conversation_later"),
        ):
            with self.assertRaises(conversation.ImageGenerationError) as raised:
                list(conversation.stream_image_outputs_with_pool(self._request()))

        self.assertEqual(raised.exception.account_email, "account-a@example.test")
        self.assertEqual(raised.exception.conversation_id, "conversation-123")
        self.assertEqual(raised.exception.raw_error, raw_error)
        self.assertEqual(raised.exception.stage, "image_stream")
        self.assertEqual(raised.exception.attempt, 1)
        self.assert_slots_finalized_once(service, [("account-a", False)])

    def test_transport_backoff_stays_within_request_deadline(self) -> None:
        with (
            mock.patch.object(conversation.time, "monotonic", return_value=100.0),
            mock.patch.object(conversation.random, "uniform", return_value=0.75),
        ):
            delay = conversation._image_transport_retry_backoff_secs(1, 101.0)

        self.assertEqual(delay, 1.0 - conversation.IMAGE_TRANSPORT_RETRY_MIN_REMAINING_SECS)

    def test_deterministic_request_error_does_not_switch_accounts(self) -> None:
        service = FakeAccountService(["account-a", "account-b"])

        def invalid_request(*_args):
            raise UpstreamHTTPError("/backend-api/conversation", 422, {"error": "invalid prompt"})
            yield None

        with self._patches(service, invalid_request), self._retry_config():
            with self.assertRaises(conversation.ImageGenerationError) as raised:
                list(conversation.stream_image_outputs_with_pool(self._request()))

        self.assertEqual(raised.exception.status_code, 422)
        self.assertEqual(service.selected_tokens, ["account-a"])
        self.assert_slots_finalized_once(service, [("account-a", False)])

    def test_result_is_returned_when_account_persistence_fails(self) -> None:
        service = ReleasingThenFailingMarkAccountService(["account-a", "account-b"])

        with self._patches(service, successful_output), self._retry_config():
            outputs = list(conversation.stream_image_outputs_with_pool(self._request()))

        self.assertEqual(len(outputs), 1)
        self.assertEqual(outputs[0].kind, "result")
        self.assertEqual(service.selected_tokens, ["account-a"])
        self.assert_slots_finalized_once(service, [("account-a", True)])

    def test_postprocess_error_does_not_switch_accounts(self) -> None:
        service = FakeAccountService(["account-a", "account-b"])

        def postprocess_failure(*_args):
            raise conversation.ImageGenerationError(
                "image was generated but its result could not be retrieved",
                code=conversation.IMAGE_RESULT_POSTPROCESS_ERROR_CODE,
            )
            yield None

        with self._patches(service, postprocess_failure), self._retry_config():
            with self.assertRaises(conversation.ImageGenerationError) as raised:
                list(conversation.stream_image_outputs_with_pool(self._request()))

        self.assertEqual(raised.exception.code, conversation.IMAGE_RESULT_POSTPROCESS_ERROR_CODE)
        self.assertEqual(service.selected_tokens, ["account-a"])
        self.assert_slots_finalized_once(service, [("account-a", False)])

    def test_preflight_failure_finalizes_the_reserved_slot(self) -> None:
        service = AccountLookupFailureService(["account-a"])

        with self._patches(service, successful_output), self._retry_config():
            with self.assertRaises(conversation.ImageGenerationError):
                list(conversation.stream_image_outputs_with_pool(self._request()))

        self.assert_slots_finalized_once(service, [("account-a", False)])


if __name__ == "__main__":
    unittest.main()
