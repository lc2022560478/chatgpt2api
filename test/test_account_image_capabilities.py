from __future__ import annotations

import os
import tempfile
import threading
import time
import unittest
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("CHATGPT2API_AUTH_KEY", "test-auth")

from services.account_service import (
    AccountService,
    ImagePreflightTransportError,
    is_transient_image_preflight_transport_error,
)
from services.auth_service import AuthService
from services.config import config
from services.openai_backend_api import InvalidAccessTokenError, OpenAIBackendAPI
from services.storage.json_storage import JSONStorageBackend
from utils.helper import (
    IMAGE_25_FLARE_MODEL,
    IMAGE_25_SUNBURST_MODEL,
    IMAGE_26_FLARE_MODEL,
    IMAGE_26_SUNBURST_MODEL,
    anonymize_token,
    is_supported_image_model,
    split_image_model,
)


class AccountCapabilityTests(unittest.TestCase):
    def test_image_accounts_require_positive_quota(self) -> None:
        self.assertFalse(
            AccountService._is_image_account_available(
                {"status": "限流", "quota": 1}
            )
        )
        self.assertFalse(
            AccountService._is_image_account_available(
                {"status": "正常", "quota": 0}
            )
        )
        self.assertTrue(AccountService._is_image_account_available({"status": "正常", "quota": 1}))

    def test_prolite_variants_are_normalized(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = AccountService(JSONStorageBackend(Path(tmp_dir) / "accounts.json"))
            self.assertEqual(service._normalize_account_type("prolite"), "ProLite")
            self.assertEqual(service._normalize_account_type("pro_lite"), "ProLite")

    def test_search_account_type_ignores_unrelated_scalar_values(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = AccountService(JSONStorageBackend(Path(tmp_dir) / "accounts.json"))
            self.assertIsNone(
                service._search_account_type(
                    {
                        "amr": ["pwd", "otp", "mfa"],
                        "chatgpt_compute_residency": "no_constraint",
                        "chatgpt_data_residency": "no_constraint",
                        "user_id": "user-I52GFfLGFM0dokFk2dBiKEBn",
                    }
                )
            )

    def test_mark_image_result_consumes_quota(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = AccountService(JSONStorageBackend(Path(tmp_dir) / "accounts.json"))
            service.add_accounts(["token-1"])
            service.update_account(
                "token-1",
                {
                    "status": "正常",
                    "quota": 1,
                },
            )

            updated = service.mark_image_result("token-1", success=True)

            self.assertIsNotNone(updated)
            self.assertEqual(updated["quota"], 0)
            self.assertEqual(updated["status"], "限流")

    def test_split_image_model_supports_plan_type_prefix(self) -> None:
        self.assertEqual(split_image_model("gpt-image-2"), (None, "gpt-image-2"))
        self.assertEqual(split_image_model(IMAGE_25_FLARE_MODEL), (None, IMAGE_25_FLARE_MODEL))
        self.assertEqual(split_image_model(IMAGE_25_SUNBURST_MODEL), (None, IMAGE_25_SUNBURST_MODEL))
        self.assertTrue(is_supported_image_model(IMAGE_25_FLARE_MODEL))
        self.assertTrue(is_supported_image_model(IMAGE_25_SUNBURST_MODEL))
        self.assertEqual(split_image_model(IMAGE_26_FLARE_MODEL), (None, IMAGE_26_FLARE_MODEL))
        self.assertEqual(split_image_model(IMAGE_26_SUNBURST_MODEL), (None, IMAGE_26_SUNBURST_MODEL))
        self.assertTrue(is_supported_image_model(IMAGE_26_FLARE_MODEL))
        self.assertTrue(is_supported_image_model(IMAGE_26_SUNBURST_MODEL))
        self.assertEqual(split_image_model("plus-codex-gpt-image-2"), ("plus", "codex-gpt-image-2"))
        self.assertEqual(split_image_model("team-codex-gpt-image-2"), ("team", "codex-gpt-image-2"))
        self.assertEqual(split_image_model("pro-codex-gpt-image-2"), ("pro", "codex-gpt-image-2"))
        self.assertEqual(split_image_model("plus-gpt-image-2"), (None, None))
        self.assertEqual(split_image_model("unknown-image-model"), (None, None))

    def test_versioned_image_models_map_to_their_real_upstream_ids(self) -> None:
        backend = object.__new__(OpenAIBackendAPI)
        self.assertEqual(backend._image_model_settings(IMAGE_25_FLARE_MODEL)[0], IMAGE_25_FLARE_MODEL)
        self.assertEqual(backend._image_model_settings(IMAGE_25_SUNBURST_MODEL)[0], IMAGE_25_SUNBURST_MODEL)
        self.assertEqual(backend._image_model_settings(IMAGE_26_FLARE_MODEL)[0], IMAGE_26_FLARE_MODEL)
        self.assertEqual(backend._image_model_settings(IMAGE_26_SUNBURST_MODEL)[0], IMAGE_26_SUNBURST_MODEL)

    def test_get_available_access_token_filters_by_plan_type(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = AccountService(JSONStorageBackend(Path(tmp_dir) / "accounts.json"))
            service.add_account_items(
                [
                    {"access_token": "token-plus", "type": "Plus", "status": "正常", "quota": 3},
                    {"access_token": "token-pro", "type": "Pro", "status": "正常", "quota": 3},
                ]
            )

            service.fetch_remote_info = lambda access_token, event="fetch_remote_info", **_kwargs: service.get_account(access_token)

            plus_token = service.get_available_access_token(plan_type="plus")
            pro_token = service.get_available_access_token(plan_type="pro")
            service.release_image_slot(plus_token)
            service.release_image_slot(pro_token)

            self.assertEqual(plus_token, "token-plus")
            self.assertEqual(pro_token, "token-pro")

    def test_image_preflight_reuses_a_recent_successful_validation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = AccountService(JSONStorageBackend(Path(tmp_dir) / "accounts.json"))
            service.add_account_items([{"access_token": "token-a", "status": "正常", "quota": 3}])
            calls: list[str] = []

            def fetch(access_token: str, *_args, **_kwargs):
                calls.append(access_token)
                return service.get_account(access_token)

            service.fetch_remote_info = fetch
            first = service.get_available_access_token()
            service.release_image_slot(first)
            second = service.get_available_access_token()
            service.release_image_slot(second)

            self.assertEqual((first, second), ("token-a", "token-a"))
            self.assertEqual(calls, ["token-a"])

    def test_image_preflight_coalesces_concurrent_validations(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = AccountService(JSONStorageBackend(Path(tmp_dir) / "accounts.json"))
            service.add_account_items([{"access_token": "token-a", "status": "正常", "quota": 10}])
            settings = dict(config.data)
            settings["image_account_concurrency"] = 8
            remote_started = threading.Event()
            allow_remote_result = threading.Event()
            calls: list[str] = []
            calls_lock = threading.Lock()

            def fetch(access_token: str, *_args, **_kwargs):
                with calls_lock:
                    calls.append(access_token)
                remote_started.set()
                self.assertTrue(allow_remote_result.wait(timeout=3))
                return service.get_account(access_token)

            def reserve_and_release() -> str:
                token = service.get_available_access_token()
                try:
                    return token
                finally:
                    service.release_image_slot(token)

            service.fetch_remote_info = fetch
            with patch.object(config, "data", settings):
                with ThreadPoolExecutor(max_workers=8) as executor:
                    futures = [executor.submit(reserve_and_release) for _ in range(8)]
                    try:
                        self.assertTrue(remote_started.wait(timeout=1))
                        deadline = time.monotonic() + 1
                        while time.monotonic() < deadline:
                            with service._lock:
                                if service._image_inflight.get("token-a", 0) == 8:
                                    break
                            time.sleep(0.01)
                    finally:
                        # Let the workers leave even if a diagnostic assertion
                        # above fails, so the test executor cannot deadlock.
                        allow_remote_result.set()
                    self.assertEqual([future.result(timeout=3) for future in futures], ["token-a"] * 8)

            self.assertEqual(calls, ["token-a"])

    def test_expired_image_preflight_cache_revalidates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = AccountService(JSONStorageBackend(Path(tmp_dir) / "accounts.json"))
            service.add_account_items([{"access_token": "token-a", "status": "正常", "quota": 3}])
            calls: list[str] = []

            def fetch(access_token: str, *_args, **_kwargs):
                calls.append(access_token)
                return service.get_account(access_token)

            service.fetch_remote_info = fetch
            token = service.get_available_access_token()
            service.release_image_slot(token)
            with service._lock:
                service._image_preflight_cache["token-a"] = time.monotonic() - 1
            token = service.get_available_access_token()
            service.release_image_slot(token)

            self.assertEqual(calls, ["token-a", "token-a"])

    def test_failed_image_preflight_is_not_cached(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = AccountService(JSONStorageBackend(Path(tmp_dir) / "accounts.json"))
            service.add_account_items([{"access_token": "token-a", "status": "正常", "quota": 3}])
            calls: list[str] = []

            def fail(access_token: str, *_args, **_kwargs):
                calls.append(access_token)
                raise RuntimeError("upstream unavailable")

            service.fetch_remote_info = fail
            for _ in range(2):
                with self.assertRaisesRegex(RuntimeError, "upstream unavailable"):
                    service.get_available_access_token()

            self.assertEqual(calls, ["token-a", "token-a"])
            self.assertEqual(service._image_preflight_cache, {})
            self.assertEqual(service._image_inflight, {})

    def test_non_auth_preflight_failures_preserve_error_without_scanning_accounts(self) -> None:
        for error in (
            RuntimeError("/backend-api/me failed: HTTP 403"),
            RuntimeError("/backend-api/me failed: HTTP 429"),
            RuntimeError("/backend-api/me failed: HTTP 500"),
            RuntimeError("/backend-api/me failed: HTTP 503"),
            ValueError("invalid JSON response"),
            TypeError("unexpected response type"),
        ):
            with self.subTest(error=repr(error)), tempfile.TemporaryDirectory() as tmp_dir:
                service = AccountService(JSONStorageBackend(Path(tmp_dir) / "accounts.json"))
                service.add_account_items([
                    {"access_token": f"token-{index}", "status": "正常", "quota": 3}
                    for index in range(21)
                ])
                with patch.object(service, "fetch_remote_info", side_effect=error) as fetch:
                    with self.assertRaises(type(error)) as raised:
                        service.get_available_access_token()
                self.assertIs(raised.exception, error)
                self.assertEqual(fetch.call_count, 1)
                self.assertEqual(service._image_inflight, {})
                self.assertEqual(service._image_preflight_flights, {})
                self.assertEqual(service._image_preflight_cache, {})
                self.assertTrue(all(account["quota"] == 3 for account in service.list_accounts()))

    def test_invalid_preflight_tries_the_next_account(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = AccountService(JSONStorageBackend(Path(tmp_dir) / "accounts.json"))
            service.add_account_items([
                {"access_token": "token-a", "status": "正常", "quota": 3},
                {"access_token": "token-b", "status": "正常", "quota": 3},
            ])
            calls: list[str] = []

            def fetch(access_token: str, *_args, **_kwargs):
                calls.append(access_token)
                if access_token == "token-a":
                    raise InvalidAccessTokenError("token invalidated (/backend-api/me)")
                return service.get_account(access_token)

            service.fetch_remote_info = fetch
            token = service.get_available_access_token()
            try:
                self.assertEqual(token, "token-b")
                self.assertEqual(calls, ["token-a", "token-b"])
                self.assertEqual(service._image_inflight, {"token-b": 1})
            finally:
                service.release_image_slot(token)
            self.assertEqual(service._image_inflight, {})

    def test_invalid_preflights_preserve_authentication_error_when_candidates_run_out(self) -> None:
        for account_count in (1, 3, 20, 21):
            with self.subTest(account_count=account_count), tempfile.TemporaryDirectory() as tmp_dir:
                service = AccountService(JSONStorageBackend(Path(tmp_dir) / "accounts.json"))
                service.add_account_items([
                    {"access_token": f"token-{index}", "status": "正常", "quota": 3}
                    for index in range(account_count)
                ])
                error = InvalidAccessTokenError("token invalidated (/backend-api/me)")
                with patch.object(service, "fetch_remote_info", side_effect=error) as fetch:
                    with self.assertRaises(InvalidAccessTokenError) as raised:
                        service.get_available_access_token()
                self.assertIs(raised.exception, error)
                self.assertEqual(fetch.call_count, min(account_count, 20))
                self.assertEqual(len({call.args[0] for call in fetch.call_args_list}), fetch.call_count)
                self.assertEqual(service._image_inflight, {})
                self.assertEqual(service._image_preflight_flights, {})
                self.assertEqual(service._image_preflight_cache, {})

    def test_zero_quota_preflight_tries_the_next_account(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = AccountService(JSONStorageBackend(Path(tmp_dir) / "accounts.json"))
            service.add_account_items([
                {"access_token": "token-a", "status": "正常", "quota": 3},
                {"access_token": "token-b", "status": "正常", "quota": 3},
            ])
            calls: list[str] = []

            def fetch(access_token: str, *_args, **_kwargs):
                calls.append(access_token)
                if access_token == "token-a":
                    return service.update_account(access_token, {"quota": 0})
                return service.get_account(access_token)

            service.fetch_remote_info = fetch
            token = service.get_available_access_token()
            try:
                self.assertEqual(token, "token-b")
                self.assertEqual(calls, ["token-a", "token-b"])
                self.assertEqual(service._image_inflight, {"token-b": 1})
            finally:
                service.release_image_slot(token)
            self.assertEqual(service._image_inflight, {})

    def test_confirmed_zero_quotas_still_report_quota_exhaustion(self) -> None:
        for account_count in (1, 21):
            with self.subTest(account_count=account_count), tempfile.TemporaryDirectory() as tmp_dir:
                service = AccountService(JSONStorageBackend(Path(tmp_dir) / "accounts.json"))
                service.add_account_items([
                    {"access_token": f"token-{index}", "status": "正常", "quota": 3}
                    for index in range(account_count)
                ])

                def fetch(access_token: str, *_args, **_kwargs):
                    return service.update_account(access_token, {"quota": 0})

                with patch.object(service, "fetch_remote_info", side_effect=fetch) as remote:
                    with self.assertRaisesRegex(RuntimeError, "^no available image quota"):
                        service.get_available_access_token()
                self.assertEqual(remote.call_count, min(account_count, 20))
                self.assertEqual(service._image_inflight, {})
                self.assertEqual(service._image_preflight_cache, {})

    def test_external_exclusions_keep_quota_error_when_no_preflight_was_attempted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = AccountService(JSONStorageBackend(Path(tmp_dir) / "accounts.json"))
            service.add_account_items([{"access_token": "token-a", "status": "正常", "quota": 3}])
            with patch.object(service, "fetch_remote_info") as fetch:
                with self.assertRaisesRegex(RuntimeError, "^no available image quota$"):
                    service.get_available_access_token(excluded_tokens={"token-a"})
            fetch.assert_not_called()
            self.assertEqual(service._image_inflight, {})

    def test_candidate_wait_error_is_not_replaced_by_an_earlier_authentication_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = AccountService(JSONStorageBackend(Path(tmp_dir) / "accounts.json"))
            service.add_account_items([{"access_token": "token-a", "status": "正常", "quota": 3}])
            acquire = service._acquire_next_candidate_token
            error = RuntimeError("timed out waiting for an available image account")

            def acquire_or_timeout(**kwargs):
                if "token-a" in kwargs.get("excluded_tokens", set()):
                    raise error
                return acquire(**kwargs)

            with (
                patch.object(service, "_acquire_next_candidate_token", side_effect=acquire_or_timeout),
                patch.object(service, "fetch_remote_info", side_effect=InvalidAccessTokenError("token invalidated")),
            ):
                with self.assertRaises(RuntimeError) as raised:
                    service.get_available_access_token()
            self.assertIs(raised.exception, error)
            self.assertEqual(service._image_inflight, {})

    def test_image_preflight_recognizes_only_transient_transport_failures(self) -> None:
        for raw_error in (
            "Failed to perform, curl: (5) Couldn't resolve proxy",
            "Failed to perform, curl: (6) Couldn't resolve host",
            "Failed to perform, curl: (7) Failed to connect",
            "Failed to perform, curl: (18) Partial transfer",
            "Failed to perform, curl: (28) Operation timed out",
            "Failed to perform, curl: (35) TLS connect error",
            "Failed to perform, curl: (52) Empty reply from server",
            "Failed to perform, curl: (55) Send failure",
            "Failed to perform, curl: (56) Connection closed abruptly",
            "Failed to perform, curl: (92) HTTP/2 stream was not closed cleanly",
            "Connection reset by peer",
            "Connection aborted",
            "Connection refused",
            "Temporary failure in name resolution",
            "Network is unreachable",
            "image account preflight timed out",
        ):
            with self.subTest(raw_error=raw_error):
                self.assertTrue(is_transient_image_preflight_transport_error(RuntimeError(raw_error)))

        self.assertTrue(is_transient_image_preflight_transport_error(ConnectionResetError()))
        self.assertTrue(is_transient_image_preflight_transport_error(ConnectionError()))
        self.assertTrue(is_transient_image_preflight_transport_error(TimeoutError()))
        self.assertFalse(is_transient_image_preflight_transport_error(InvalidAccessTokenError("token invalidated")))
        self.assertFalse(
            is_transient_image_preflight_transport_error(
                RuntimeError("/backend-api/me failed: HTTP 503")
            )
        )

    def test_image_preflight_transport_error_preserves_timeout_kind(self) -> None:
        for error in (TimeoutError(), RuntimeError("curl: (28)"), RuntimeError("image account preflight timed out")):
            with self.subTest(error=repr(error)):
                wrapped = ImagePreflightTransportError(error)
                self.assertTrue(wrapped.is_timeout)
                self.assertTrue(ImagePreflightTransportError(wrapped).is_timeout)
        self.assertFalse(ImagePreflightTransportError(ConnectionError()).is_timeout)
        self.assertFalse(ImagePreflightTransportError("curl: (35) TLS connect error").is_timeout)

    def test_native_preflight_timeouts_stop_scan_and_release_slot(self) -> None:
        for error in (TimeoutError(), ConnectionError(), RuntimeError("image account preflight timed out")):
            with self.subTest(error=repr(error)), tempfile.TemporaryDirectory() as tmp_dir:
                service = AccountService(JSONStorageBackend(Path(tmp_dir) / "accounts.json"))
                service.add_account_items([
                    {"access_token": "token-a", "status": "正常", "quota": 3},
                    {"access_token": "token-b", "status": "正常", "quota": 3},
                ])
                with patch.object(service, "fetch_remote_info", side_effect=error) as fetch:
                    with self.assertRaises(ImagePreflightTransportError) as raised:
                        service.get_available_access_token(timeout_secs=5)
                self.assertEqual(fetch.call_count, 1)
                self.assertEqual(raised.exception.is_timeout, not isinstance(error, ConnectionError))
                self.assertEqual(service._image_inflight, {})
                self.assertEqual(service._image_preflight_flights, {})

    def test_singleflight_follower_timeout_is_typed_and_releases_slot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = AccountService(JSONStorageBackend(Path(tmp_dir) / "accounts.json"))
            service.add_account_items([
                {"access_token": "token-a", "status": "正常", "quota": 3},
                {"access_token": "token-b", "status": "正常", "quota": 3},
            ])
            leader_flight = Future()
            service._image_preflight_flights["token-a"] = leader_flight
            with patch.object(service, "fetch_remote_info") as fetch:
                with self.assertRaises(ImagePreflightTransportError) as raised:
                    service.get_available_access_token(timeout_secs=0.01)
            self.assertTrue(raised.exception.is_timeout)
            self.assertEqual(service._image_inflight, {})
            self.assertIs(service._image_preflight_flights["token-a"], leader_flight)
            self.assertFalse(leader_flight.done())
            fetch.assert_not_called()

    def test_image_refresh_network_failure_does_not_probe_old_token_or_mark_invalid(self) -> None:
        for error in (TimeoutError(), ConnectionError(), RuntimeError("curl: (35) TLS connect error")):
            with self.subTest(error=repr(error)), tempfile.TemporaryDirectory() as tmp_dir:
                service = AccountService(JSONStorageBackend(Path(tmp_dir) / "accounts.json"))
                service.add_account_items([{
                    "access_token": "token-a", "refresh_token": "refresh-a", "status": "正常", "quota": 3,
                }])
                with (
                    patch.object(service, "_token_needs_refresh", return_value=True),
                    patch.object(service, "_request_access_token_refresh", side_effect=error) as refresh,
                    patch("services.openai_backend_api.OpenAIBackendAPI.get_user_info") as get_info,
                    patch.object(service, "_record_invalid_token_seen") as mark_invalid,
                ):
                    for _ in range(2):
                        with self.assertRaises(ImagePreflightTransportError):
                            service.get_available_access_token(timeout_secs=5)
                self.assertEqual(refresh.call_count, 1, "recent transport failure should keep refresh backoff")
                get_info.assert_not_called()
                mark_invalid.assert_not_called()
                account = service.get_account("token-a")
                self.assertEqual((account["status"], account["quota"]), ("正常", 3))
                self.assertEqual(service._image_inflight, {})

    def test_background_refresh_keeps_best_effort_transport_behavior(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = AccountService(JSONStorageBackend(Path(tmp_dir) / "accounts.json"))
            service.add_account_items([{
                "access_token": "token-a", "refresh_token": "refresh-a", "status": "正常", "quota": 3,
            }])
            with (
                patch.object(service, "_token_needs_refresh", return_value=True),
                patch.object(service, "_request_access_token_refresh", side_effect=TimeoutError()),
            ):
                self.assertEqual(service.refresh_access_token("token-a"), "token-a")
            self.assertEqual(service.get_account("token-a")["status"], "正常")

    def test_request_bound_refresh_preserves_confirmed_invalid_account_handling(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = AccountService(JSONStorageBackend(Path(tmp_dir) / "accounts.json"))
            service.add_account_items([{
                "access_token": "token-a", "refresh_token": "refresh-a", "status": "正常", "quota": 3,
            }])
            with (
                patch.object(service, "_token_needs_refresh", return_value=True),
                patch.object(service, "_request_access_token_refresh", side_effect=RuntimeError("oauth_refresh_http_401")),
                patch("services.openai_backend_api.OpenAIBackendAPI.get_user_info", side_effect=InvalidAccessTokenError("token invalidated (/backend-api/me)")),
                patch.object(service, "_record_invalid_token_seen", return_value=True) as invalid,
                patch.object(service, "remove_invalid_token") as remove,
            ):
                with self.assertRaises(InvalidAccessTokenError):
                    service.fetch_remote_info("token-a", timeout_secs=5)
            invalid.assert_called_once()
            remove.assert_called_once()

    def test_image_preflight_transport_failure_does_not_scan_other_accounts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = AccountService(JSONStorageBackend(Path(tmp_dir) / "accounts.json"))
            service.add_account_items(
                [
                    {"access_token": "token-a", "status": "正常", "quota": 3},
                    {"access_token": "token-b", "status": "正常", "quota": 3},
                ]
            )
            calls: list[str] = []
            raw_error = "Failed to perform, curl: (56) Connection closed abruptly."

            def fail(access_token: str, *_args, **_kwargs):
                calls.append(access_token)
                raise RuntimeError(raw_error)

            service.fetch_remote_info = fail
            with self.assertRaises(ImagePreflightTransportError) as raised:
                service.get_available_access_token()

            self.assertEqual(raised.exception.raw_error, raw_error)
            self.assertEqual(calls, ["token-a"])
            self.assertEqual(service._image_inflight, {})

    def test_failed_singleflight_releases_every_reserved_image_slot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = AccountService(JSONStorageBackend(Path(tmp_dir) / "accounts.json"))
            service.add_account_items([{"access_token": "token-a", "status": "正常", "quota": 3}])
            settings = dict(config.data)
            settings["image_account_concurrency"] = 3
            remote_started = threading.Event()
            fail_remote = threading.Event()
            calls: list[str] = []

            def fetch(access_token: str, *_args, **_kwargs):
                calls.append(access_token)
                remote_started.set()
                self.assertTrue(fail_remote.wait(timeout=3))
                raise RuntimeError("Failed to perform, curl: (35) TLS connect error")

            def reserve() -> None:
                with self.assertRaises(ImagePreflightTransportError):
                    service.get_available_access_token()

            service.fetch_remote_info = fetch
            with patch.object(config, "data", settings):
                with ThreadPoolExecutor(max_workers=3) as executor:
                    futures = [executor.submit(reserve) for _ in range(3)]
                    try:
                        self.assertTrue(remote_started.wait(timeout=1))
                    finally:
                        fail_remote.set()
                    for future in futures:
                        future.result(timeout=3)

            self.assertEqual(calls, ["token-a"])
            self.assertEqual(service._image_inflight, {})

    def test_token_rotation_invalidates_image_preflight_cache(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = AccountService(JSONStorageBackend(Path(tmp_dir) / "accounts.json"))
            service.add_account_items([{"access_token": "token-a", "status": "正常", "quota": 3}])
            calls: list[str] = []

            def fetch(access_token: str, *_args, **_kwargs):
                calls.append(access_token)
                return service.get_account(access_token)

            service.fetch_remote_info = fetch
            token = service.get_available_access_token()
            service.release_image_slot(token)
            service._apply_refreshed_tokens("token-a", {"access_token": "token-b"}, "test")

            with service._lock:
                self.assertNotIn("token-a", service._image_preflight_cache)
                self.assertNotIn("token-b", service._image_preflight_cache)

            token = service.get_available_access_token()
            service.release_image_slot(token)
            self.assertEqual(token, "token-b")
            self.assertEqual(calls, ["token-a", "token-b"])

    def test_stale_preflight_response_does_not_validate_a_rotated_token(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = AccountService(JSONStorageBackend(Path(tmp_dir) / "accounts.json"))
            service.add_account_items([{"access_token": "token-a", "status": "正常", "quota": 3}])
            calls: list[str] = []

            def fetch(access_token: str, *_args, **_kwargs):
                calls.append(access_token)
                if access_token == "token-a":
                    service._apply_refreshed_tokens("token-a", {"access_token": "token-b"}, "test")
                    # Simulate a /me response that started before the refresh.
                    return {"access_token": "token-a", "status": "正常", "quota": 3}
                return service.get_account(access_token)

            service.fetch_remote_info = fetch
            token = service.get_available_access_token()
            service.release_image_slot(token)

            self.assertEqual(token, "token-b")
            with service._lock:
                self.assertNotIn("token-b", service._image_preflight_cache)

            token = service.get_available_access_token()
            service.release_image_slot(token)
            self.assertEqual(calls, ["token-a", "token-b"])

    def test_get_available_access_token_excludes_a_rotated_token_alias(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = AccountService(JSONStorageBackend(Path(tmp_dir) / "accounts.json"))
            service.add_account_items(
                [
                    {"access_token": "token-a2", "status": "姝ｅ父", "quota": 3},
                    {"access_token": "token-b", "status": "姝ｅ父", "quota": 3},
                ]
            )
            service._token_aliases["token-a1"] = "token-a2"
            service.fetch_remote_info = lambda access_token, event="fetch_remote_info", **_kwargs: service.get_account(access_token)

            selected = service.get_available_access_token(excluded_tokens={"token-a1"})
            service.release_image_slot(selected)

            self.assertEqual(selected, "token-b")

    def test_get_available_access_token_respects_busy_slot_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = AccountService(JSONStorageBackend(Path(tmp_dir) / "accounts.json"))
            service.add_account_items([{"access_token": "token-a", "status": "ready", "quota": 3}])
            service.fetch_remote_info = lambda access_token, event="fetch_remote_info", **_kwargs: service.get_account(access_token)
            settings = dict(config.data)
            settings["image_account_concurrency"] = 1

            with patch.object(config, "data", settings):
                reserved = service.get_available_access_token()
                try:
                    with self.assertRaisesRegex(RuntimeError, "timed out waiting for an available image account"):
                        service.get_available_access_token(timeout_secs=0)
                finally:
                    service.release_image_slot(reserved)

            self.assertEqual(service._image_inflight, {})

    def test_preflight_cancellation_releases_the_reserved_image_slot(self) -> None:
        class Cancelled(BaseException):
            pass

        with tempfile.TemporaryDirectory() as tmp_dir:
            service = AccountService(JSONStorageBackend(Path(tmp_dir) / "accounts.json"))
            service.add_account_items([{"access_token": "token-a", "status": "姝ｅ父", "quota": 3}])

            def cancelled(*_args, **_kwargs):
                raise Cancelled("request cancelled")

            service.fetch_remote_info = cancelled
            with self.assertRaises(Cancelled):
                service.get_available_access_token()

            self.assertEqual(service._image_inflight, {})

    def test_fetch_remote_info_forwards_the_remaining_preflight_budget(self) -> None:
        class RecordingBackend:
            timeouts: list[float | None] = []

            def __init__(self, _access_token: str) -> None:
                pass

            def get_user_info(self, *, timeout_secs: float | None = None) -> dict:
                self.timeouts.append(timeout_secs)
                return {"status": "姝ｅ父", "quota": 3, "type": "Plus"}

            def close(self) -> None:
                pass

        with tempfile.TemporaryDirectory() as tmp_dir:
            service = AccountService(JSONStorageBackend(Path(tmp_dir) / "accounts.json"))
            service.add_account_items([{"access_token": "token-a", "status": "姝ｅ父", "quota": 3}])

            with patch("services.openai_backend_api.OpenAIBackendAPI", RecordingBackend):
                service.fetch_remote_info("token-a", timeout_secs=5)

            self.assertEqual(len(RecordingBackend.timeouts), 1)
            self.assertIsNotNone(RecordingBackend.timeouts[0])
            self.assertGreater(RecordingBackend.timeouts[0], 0)
            self.assertLessEqual(RecordingBackend.timeouts[0], 5)

    def test_refresh_access_token_lock_respects_timeout_budget(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = AccountService(JSONStorageBackend(Path(tmp_dir) / "accounts.json"))
            service.add_accounts(["token-a"])
            service._token_refresh_lock.acquire()
            try:
                with self.assertRaisesRegex(TimeoutError, "access token refresh timed out"):
                    service.refresh_access_token("token-a", timeout_secs=0)
            finally:
                service._token_refresh_lock.release()

    def test_refresh_accounts_can_remove_invalid_token_without_confirmation_delay(self) -> None:
        original_value = config.data.get("auto_remove_invalid_accounts")
        config.data["auto_remove_invalid_accounts"] = True
        try:
            with tempfile.TemporaryDirectory() as tmp_dir:
                service = AccountService(JSONStorageBackend(Path(tmp_dir) / "accounts.json"))
                service.add_account_items([{"access_token": "invalid-token", "status": "正常"}])

                with patch(
                    "services.openai_backend_api.OpenAIBackendAPI.get_user_info",
                    side_effect=InvalidAccessTokenError("token invalidated (/backend-api/me)"),
                ):
                    result = service.refresh_accounts(["invalid-token"], defer_invalid_removal=False)

                self.assertEqual(result["refreshed"], 0)
                self.assertEqual(len(result["errors"]), 1)
                self.assertEqual(result["items"], [])
                self.assertIsNone(service.get_account("invalid-token"))
        finally:
            if original_value is None:
                config.data.pop("auto_remove_invalid_accounts", None)
            else:
                config.data["auto_remove_invalid_accounts"] = original_value

    def test_refresh_accounts_defers_invalid_token_removal_by_default(self) -> None:
        original_value = config.data.get("auto_remove_invalid_accounts")
        config.data["auto_remove_invalid_accounts"] = True
        try:
            with tempfile.TemporaryDirectory() as tmp_dir:
                service = AccountService(JSONStorageBackend(Path(tmp_dir) / "accounts.json"))
                service.add_account_items([{"access_token": "invalid-token", "status": "正常"}])

                with patch(
                    "services.openai_backend_api.OpenAIBackendAPI.get_user_info",
                    side_effect=InvalidAccessTokenError("token invalidated (/backend-api/me)"),
                ):
                    result = service.refresh_accounts(["invalid-token"])

                account = service.get_account("invalid-token")
                self.assertEqual(result["refreshed"], 0)
                self.assertEqual(len(result["errors"]), 1)
                self.assertIsNotNone(account)
                self.assertEqual(account["invalid_count"], 1)
        finally:
            if original_value is None:
                config.data.pop("auto_remove_invalid_accounts", None)
            else:
                config.data["auto_remove_invalid_accounts"] = original_value


class TokenLogTests(unittest.TestCase):
    def test_anonymize_token_hides_raw_value(self) -> None:
        token = "super-secret-token"
        token_ref = anonymize_token(token)

        self.assertTrue(token_ref.startswith("token:"))
        self.assertNotIn(token, token_ref)


class AuthServiceTests(unittest.TestCase):
    def test_create_authenticate_disable_and_delete_user_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = AuthService(JSONStorageBackend(Path(tmp_dir) / "accounts.json", Path(tmp_dir) / "auth_keys.json"))

            item, raw_key = service.create_key(role="user", name="Alice")

            self.assertEqual(item["role"], "user")
            self.assertEqual(item["name"], "Alice")
            self.assertTrue(item["enabled"])
            self.assertTrue(raw_key.startswith("sk-"))

            authed = service.authenticate(raw_key)
            self.assertIsNotNone(authed)
            self.assertEqual(authed["id"], item["id"])
            self.assertEqual(authed["role"], "user")
            self.assertIsNotNone(authed["last_used_at"])

            updated = service.update_key(item["id"], {"enabled": False}, role="user")
            self.assertIsNotNone(updated)
            self.assertFalse(updated["enabled"])
            self.assertIsNone(service.authenticate(raw_key))

            self.assertTrue(service.delete_key(item["id"], role="user"))
            self.assertFalse(service.delete_key(item["id"], role="user"))
            self.assertEqual(service.list_keys(role="user"), [])

    def test_authenticate_ignores_last_used_save_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = AuthService(JSONStorageBackend(Path(tmp_dir) / "accounts.json", Path(tmp_dir) / "auth_keys.json"))
            item, raw_key = service.create_key(role="user", name="Alice")

            def fail_save() -> None:
                raise OSError("disk unavailable")

            service._save = fail_save

            authed = service.authenticate(raw_key)

            self.assertIsNotNone(authed)
            self.assertEqual(authed["id"], item["id"])
            self.assertIsNotNone(authed["last_used_at"])

    def test_update_user_key_replaces_raw_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = AuthService(JSONStorageBackend(Path(tmp_dir) / "accounts.json", Path(tmp_dir) / "auth_keys.json"))
            item, raw_key = service.create_key(role="user", name="Alice")

            updated = service.update_key(item["id"], {"key": "sk-user-custom-key"}, role="user")

            self.assertIsNotNone(updated)
            self.assertIsNone(service.authenticate(raw_key))

            authed = service.authenticate("sk-user-custom-key")
            self.assertIsNotNone(authed)
            self.assertEqual(authed["id"], item["id"])

    def test_user_key_name_must_be_unique(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = AuthService(JSONStorageBackend(Path(tmp_dir) / "accounts.json", Path(tmp_dir) / "auth_keys.json"))
            first, _ = service.create_key(role="user", name="Alice")
            second, _ = service.create_key(role="user", name="Bob")

            with self.assertRaisesRegex(ValueError, "这个名称已经在使用中了"):
                service.create_key(role="user", name="Alice")

            with self.assertRaisesRegex(ValueError, "这个名称已经在使用中了"):
                service.update_key(second["id"], {"name": "Alice"}, role="user")

            updated = service.update_key(first["id"], {"name": "Alice"}, role="user")
            self.assertIsNotNone(updated)
            self.assertEqual(updated["name"], "Alice")


if __name__ == "__main__":
    unittest.main()
