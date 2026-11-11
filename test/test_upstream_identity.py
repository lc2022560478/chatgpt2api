from __future__ import annotations

import os
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlparse

os.environ.setdefault("CHATGPT2API_AUTH_KEY", "test-auth")

from services.openai_backend_api import ChatRequirements, OpenAIBackendAPI
from utils.helper import UpstreamHTTPError, ensure_ok
from services.protocol.conversation import public_image_error_message
from services.oauth_login_service import OAuthLoginService


class UpstreamIdentityTests(unittest.TestCase):
    def backend(self, account=None, claims=None):
        session = Mock(headers={})
        with patch("services.openai_backend_api.account_service.get_account", return_value=account or {}), patch(
            "services.openai_backend_api.account_service._decode_jwt_payload", return_value=claims or {}
        ), patch("services.openai_backend_api.requests.Session", return_value=session):
            return OpenAIBackendAPI("test-token")

    def test_identity_headers_use_jwt_account_and_explicit_device(self):
        backend = self.backend(
            {"account_id": "cached-account", "fp": {"oai-did": "explicit-device"}, "oai-device-id": "legacy-device"},
            {"sub": "user-one", "https://api.openai.com/auth": {"chatgpt_account_id": "jwt-account"}},
        )
        self.assertEqual(backend.session.headers["Chatgpt-Account-Id"], "jwt-account")
        self.assertEqual(backend.session.headers["Oai-Did"], "explicit-device")
        self.assertEqual(backend.session.headers["OAI-Device-Id"], "explicit-device")
        backend.session.cookies.set.assert_called_once_with("oai-did", "explicit-device", domain="chatgpt.com", path="/")

    def test_device_survives_rotated_tokens_and_differs_between_accounts(self):
        first = self.backend(claims={"sub": "user-one", "jti": "old-token"})
        refreshed = self.backend(claims={"sub": "user-one", "jti": "new-token"})
        other = self.backend(claims={"sub": "user-two"})
        self.assertEqual(first.device_id, refreshed.device_id)
        self.assertNotEqual(first.device_id, other.device_id)

    def test_cached_account_id_is_used_when_token_has_no_account_claim(self):
        backend = self.backend({"account_id": "cached-account"}, {"sub": "user-one"})
        self.assertEqual(backend.session.headers["Chatgpt-Account-Id"], "cached-account")

    def test_image_headers_forward_all_verification_fields(self):
        backend = self.backend(claims={"sub": "user-one"})
        requirements = ChatRequirements(token="requirements", proof_token="proof", turnstile_token="turnstile", so_token="observer")
        headers = backend._image_headers("/backend-api/f/conversation", requirements, "conduit", "text/event-stream")
        self.assertEqual(headers["OpenAI-Sentinel-Chat-Requirements-Token"], "requirements")
        self.assertEqual(headers["OpenAI-Sentinel-Proof-Token"], "proof")
        self.assertEqual(headers["OpenAI-Sentinel-Turnstile-Token"], "turnstile")
        self.assertEqual(headers["OpenAI-Sentinel-SO-Token"], "observer")
        self.assertEqual(headers["X-Conduit-Token"], "conduit")
        self.assertEqual(headers["Accept"], "text/event-stream")
        self.assertTrue(headers["X-Oai-Turn-Trace-Id"])

    def test_account_lookup_supplies_missing_account_header(self):
        backend = self.backend(claims={"sub": "user-one"})
        backend.session.get.return_value = SimpleNamespace(
            status_code=200, json=lambda: {"accounts": {"default": {"account": {"account_id": "remote-account"}}}}
        )
        backend._get_default_account()
        self.assertEqual(backend.session.headers["Chatgpt-Account-Id"], "remote-account")

    def test_streaming_error_retains_upstream_body_and_closes_response(self):
        response = SimpleNamespace(
            status_code=404, text="", queue=object(), headers={},
            iter_content=lambda: iter([b'{"detail":', b'"Model not found"}']), close=Mock(),
        )
        with self.assertRaises(UpstreamHTTPError) as raised:
            ensure_ok(response, "/backend-api/f/conversation")
        self.assertEqual(raised.exception.body, {"detail": "Model not found"})
        response.close.assert_called_once()

    def test_success_stream_is_not_consumed(self):
        response = SimpleNamespace(status_code=200, iter_content=Mock(), close=Mock())
        ensure_ok(response, "conversation")
        response.iter_content.assert_not_called()
        response.close.assert_not_called()

    def test_public_model_error_keeps_cause_without_internal_details(self):
        result = public_image_error_message('/backend-api/f/conversation failed: status=404, body={"detail":"Model not found"}')
        self.assertIn("Model not found", result)
        self.assertNotIn("backend-api", result)
        self.assertNotIn("body=", result)

    def test_oauth_import_preserves_the_device_used_for_authorization(self):
        service = OAuthLoginService()
        started = service.start()
        query = parse_qs(urlparse(started["authorize_url"]).query)
        callback = "https://platform.openai.com/auth/callback?code=test-code&state=" + query["state"][0]
        with patch.object(service, "_exchange_code", return_value={"access_token": "test-token"}):
            result = service.finish(started["session_id"], callback)
        self.assertEqual(result["fp"]["oai-did"], query["device_id"][0])
        self.assertEqual(result["fp"]["oai-device-id"], query["device_id"][0])


if __name__ == "__main__":
    unittest.main()
