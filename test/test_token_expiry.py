from __future__ import annotations

import unittest
from datetime import datetime, timezone

from services.token_expiry import merge_account_credentials, parse_expiry_time, token_expiry_fields
from test.test_account_export import make_jwt


class TokenExpiryTests(unittest.TestCase):
    def test_absolute_formats_are_normalized_to_utc(self):
        expected = datetime(2030, 1, 1, tzinfo=timezone.utc)
        for value in (1893456000, "1893456000", 1893456000000, "2030-01-01T00:00:00Z",
                      "2030-01-01T08:00:00+08:00", "2030-01-01 00:00:00"):
            with self.subTest(value=value):
                self.assertEqual(parse_expiry_time(value), expected)

    def test_invalid_dates_do_not_raise(self):
        for value in (None, "", True, False, 0, -1, "NaN", "Infinity", "1e100", "not-a-date", {}, []):
            with self.subTest(value=value):
                self.assertIsNone(parse_expiry_time(value))

    def test_jwt_dates_take_precedence_and_tokens_have_independent_expiry(self):
        fields = token_expiry_fields({
            "access_token": make_jwt({"exp": 1893456000}),
            "refresh_token": make_jwt({"exp": 1924992000}),
            "access_token_expires_at": "2040-01-01T00:00:00Z",
            "refresh_token_expires_at": "2040-01-01T00:00:00Z",
        })
        self.assertEqual(fields["access_token_expires_at"], "2030-01-01T00:00:00+00:00")
        self.assertEqual(fields["refresh_token_expires_at"], "2031-01-01T00:00:00+00:00")

    def test_malformed_jwt_exp_uses_explicit_metadata(self):
        for claims in ({"exp": True}, {"exp": "not-a-date"}, {"exp": "Infinity"},
                       {"exp": 1893456000000}, {"exp": []}, {}, []):
            with self.subTest(claims=claims):
                fields = token_expiry_fields({
                    "access_token": make_jwt(claims), "expired": "2030-01-01T00:00:00Z",
                })
                self.assertEqual(fields["access_token_expires_at"], "2030-01-01T00:00:00+00:00")

    def test_jwt_epoch_zero_is_expired_not_unknown(self):
        self.assertEqual(token_expiry_fields({"access_token": make_jwt({"exp": 0})})[
            "access_token_expires_at"], "1970-01-01T00:00:00+00:00")

    def test_relative_lifetimes_require_an_explicit_receipt_anchor(self):
        payload = {"access_token": "opaque-at", "refresh_token": "opaque-rt",
                   "expires_in": 3600, "refresh_token_expires_in": 7200}
        self.assertEqual(token_expiry_fields(payload), {
            "access_token_expires_at": None, "refresh_token_expires_at": None,
        })
        self.assertEqual(token_expiry_fields(payload, received_at=datetime(2030, 1, 1, tzinfo=timezone.utc)), {
            "access_token_expires_at": "2030-01-01T01:00:00+00:00",
            "refresh_token_expires_at": "2030-01-01T02:00:00+00:00",
        })

    def test_invalid_lifetimes_are_unknown_and_zero_is_expired_at_receipt(self):
        anchor = datetime(2030, 1, 1, tzinfo=timezone.utc)
        for value in (True, -1, "bad", "Infinity", "NaN", 1e100, {}):
            with self.subTest(value=value):
                self.assertIsNone(token_expiry_fields({"access_token": "at", "expires_in": value},
                                                     received_at=anchor)["access_token_expires_at"])
        self.assertEqual(token_expiry_fields({"access_token": "at", "expires_in": 0}, received_at=anchor)[
            "access_token_expires_at"], anchor.isoformat())

    def test_no_token_means_no_expiry_even_when_metadata_is_present(self):
        self.assertEqual(token_expiry_fields({"access_token_expires_at": 1893456000,
                                              "refresh_token_expires_at": 1893456000}), {
            "access_token_expires_at": None, "refresh_token_expires_at": None,
        })

    def test_rotation_clears_old_aliases_without_affecting_unchanged_rt(self):
        current = {"access_token": "old-at", "refresh_token": "same-rt",
                   "access_token_expires_at": "2030-01-01T00:00:00Z", "expired": 1893456000,
                   "refresh_token_expires_at": "2031-01-01T00:00:00Z"}
        merged = merge_account_credentials(current, {"access_token": "new-at"})
        self.assertIsNone(merged["access_token_expires_at"])
        self.assertNotIn("expired", merged)
        self.assertEqual(merged["refresh_token_expires_at"], "2031-01-01T00:00:00+00:00")
        rotated = merge_account_credentials(merged, {"refresh_token": "new-rt"})
        self.assertIsNone(rotated["refresh_token_expires_at"])

    def test_new_lifetime_overrides_old_absolute_date_but_is_not_kept(self):
        current = {"access_token": "same-at", "access_token_expires_at": "2040-01-01T00:00:00Z"}
        anchor = datetime(2030, 1, 1, tzinfo=timezone.utc)
        merged = merge_account_credentials(current, {"expires_in": 3600}, received_at=anchor)
        self.assertEqual(merged["access_token_expires_at"], "2030-01-01T01:00:00+00:00")
        self.assertNotIn("expires_in", merged)
        reloaded = merge_account_credentials(merged, {"email": "owner@example.com"},
                                            received_at=datetime(2035, 1, 1, tzinfo=timezone.utc))
        self.assertEqual(reloaded["access_token_expires_at"], merged["access_token_expires_at"])

    def test_inherited_relative_lifetimes_are_never_rebased(self):
        fields = merge_account_credentials({"access_token": "at", "expires_in": 3600}, {"email": "user@example.com"},
                                          received_at=datetime(2030, 1, 1, tzinfo=timezone.utc))
        self.assertIsNone(fields["access_token_expires_at"])


if __name__ == "__main__":
    unittest.main()
