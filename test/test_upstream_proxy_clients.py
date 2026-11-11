"""Offline integration checks for the two upstream Session constructors.

Run in an isolated source tree with empty test storage, as service imports create
their usual module-level stores. No real HTTP Session or account lookup is used.
"""

from __future__ import annotations

import copy
import os
import unittest
from unittest import mock

os.environ.setdefault("CHATGPT2API_AUTH_KEY", "test-auth")

from services import openai_backend_api, proxy_service
from services.account_service import AccountService
from services.config import DEFAULT_PROXY_RUNTIME
from services.proxy_service import ProxySettingsStore


class _ConfigFixture:
    def __init__(self, *, enabled=True, mode="single_proxy", legacy=""):
        self.data = {
            "proxy": legacy,
            "image_request_timeout_secs": 200,
            "proxy_runtime": {
                **copy.deepcopy(DEFAULT_PROXY_RUNTIME),
                "enabled": enabled,
                "egress_mode": mode,
                "proxy_url": "http://runtime.example:8118",
                "resource_proxy_url": "http://resources.example:8118",
            },
        }

    def get_proxy_settings(self):
        return self.data["proxy"]

    def get_proxy_runtime_settings(self):
        return copy.deepcopy(self.data["proxy_runtime"])


class _ProxyRoutingCases:
    """Exercise each client against the real proxy-selection implementation."""

    def test_runtime_only_proxy_reaches_session(self):
        self._check_proxy(_ConfigFixture(), {}, "http://runtime.example:8118")

    def test_runtime_proxy_precedes_legacy_proxy(self):
        self._check_proxy(
            _ConfigFixture(legacy="http://legacy.example:8118"),
            {},
            "http://runtime.example:8118",
        )

    def test_account_proxy_precedes_runtime_and_legacy(self):
        self._check_proxy(
            _ConfigFixture(legacy="http://legacy.example:8118"),
            {"proxy": " socks5://account.example:1080 "},
            "socks5h://account.example:1080",
        )

    def test_disabled_runtime_has_no_proxy_without_legacy(self):
        self._check_proxy(_ConfigFixture(enabled=False), {}, None)

    def test_direct_mode_ignores_runtime_proxy(self):
        self._check_proxy(_ConfigFixture(mode="direct"), {}, None)

    def test_disabled_runtime_preserves_legacy_fallback(self):
        self._check_proxy(
            _ConfigFixture(enabled=False, legacy="http://legacy.example:8118"),
            {},
            "http://legacy.example:8118",
        )

    def test_direct_mode_preserves_legacy_fallback(self):
        self._check_proxy(
            _ConfigFixture(mode="direct", legacy="http://legacy.example:8118"),
            {},
            "http://legacy.example:8118",
        )

    def _assert_session_options(self, factory, expected_proxy):
        factory.assert_called_once()
        kwargs = factory.call_args.kwargs
        if expected_proxy is None:
            self.assertNotIn("proxy", kwargs)
        else:
            self.assertEqual(kwargs["proxy"], expected_proxy)
        self.assertTrue(kwargs["verify"])
        self.assertEqual(kwargs["impersonate"], "chrome110")
        # Routing must not inject a new session-wide timeout over the existing
        # per-request deadlines, nor leak factory-only options into curl_cffi.
        self.assertNotIn("timeout", kwargs)
        self.assertNotIn("upstream", kwargs)


class BackendProxyClientTests(_ProxyRoutingCases, unittest.TestCase):
    def _check_proxy(self, config, account, expected_proxy):
        before = copy.deepcopy(config.data)
        session = mock.Mock(headers={})
        with (
            mock.patch.object(openai_backend_api, "config", config),
            mock.patch.object(openai_backend_api, "proxy_settings", ProxySettingsStore(config)),
            mock.patch.object(openai_backend_api.account_service, "get_account", return_value=account) as lookup,
            mock.patch.object(openai_backend_api.requests, "Session", return_value=session) as factory,
        ):
            with openai_backend_api.OpenAIBackendAPI("test-access-token") as backend:
                self.assertIs(backend.session, session)
                lookup.assert_called_once_with("test-access-token")
                self._assert_session_options(factory, expected_proxy)
                self.assertEqual(session.headers["Authorization"], "Bearer test-access-token")
            backend.close()

        session.close.assert_called_once()
        session.get.assert_not_called()
        session.post.assert_not_called()
        self.assertEqual(config.data, before)
        self.assertEqual(config.data["image_request_timeout_secs"], 200)


class OAuthRefreshProxyClientTests(_ProxyRoutingCases, unittest.TestCase):
    def setUp(self):
        # Bypass AccountService.__init__: this method needs no stored accounts.
        self.service = AccountService.__new__(AccountService)
        self.session = mock.Mock()
        self.session.post.return_value = mock.Mock(
            status_code=200,
            text="test-response",
            json=mock.Mock(return_value={"access_token": "test-new-access-token"}),
        )

    def _patches(self, config):
        return (
            mock.patch.object(proxy_service, "proxy_settings", ProxySettingsStore(config)),
            mock.patch("curl_cffi.requests.Session", return_value=self.session),
        )

    def _check_proxy(self, config, account, expected_proxy):
        before = copy.deepcopy(config.data)
        proxy_patch, session_patch = self._patches(config)
        with proxy_patch, session_patch as factory:
            result = self.service._request_access_token_refresh(
                "test-refresh-token", account=account, timeout_secs=8.5,
            )

        self._assert_session_options(factory, expected_proxy)
        self.assertEqual(result["access_token"], "test-new-access-token")
        self.assertEqual(result["refresh_token"], "test-refresh-token")
        self.session.post.assert_called_once()
        self.assertEqual(self.session.post.call_args.args, (self.service._OAUTH_TOKEN_URL,))
        self.assertEqual(self.session.post.call_args.kwargs["timeout"], 8.5)
        self.session.close.assert_called_once()
        self.assertEqual(config.data, before)
        self.assertEqual(config.data["image_request_timeout_secs"], 200)

    def test_refresh_keeps_existing_default_and_deadline_bounds(self):
        for supplied, expected in ((None, 60.0), (200.0, 60.0), (7.25, 7.25), (0.01, 0.1)):
            with self.subTest(timeout_secs=supplied):
                self.session.reset_mock()
                proxy_patch, session_patch = self._patches(_ConfigFixture())
                with proxy_patch, session_patch:
                    self.service._request_access_token_refresh("test-refresh-token", timeout_secs=supplied)
                self.assertEqual(self.session.post.call_args.kwargs["timeout"], expected)
                self.session.close.assert_called_once()

    def test_refresh_expired_deadline_closes_without_request(self):
        for expired in (0.0, -1.0):
            with self.subTest(timeout_secs=expired):
                self.session.reset_mock()
                proxy_patch, session_patch = self._patches(_ConfigFixture())
                with proxy_patch, session_patch, self.assertRaises(TimeoutError):
                    self.service._request_access_token_refresh("test-refresh-token", timeout_secs=expired)
                self.session.post.assert_not_called()
                self.session.close.assert_called_once()

    def test_refresh_connection_failure_closes_session(self):
        self.session.post.side_effect = ConnectionError("test TLS connection failure")
        proxy_patch, session_patch = self._patches(_ConfigFixture())
        with proxy_patch, session_patch, self.assertRaises(ConnectionError):
            self.service._request_access_token_refresh("test-refresh-token", timeout_secs=8.5)
        self.session.close.assert_called_once()

    def test_refresh_http_failure_closes_session(self):
        self.session.post.return_value.status_code = 503
        self.session.post.return_value.json.return_value = {"error": "test unavailable"}
        proxy_patch, session_patch = self._patches(_ConfigFixture())
        with proxy_patch, session_patch, self.assertRaisesRegex(RuntimeError, "oauth_refresh_http_503"):
            self.service._request_access_token_refresh("test-refresh-token", timeout_secs=8.5)
        self.session.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
