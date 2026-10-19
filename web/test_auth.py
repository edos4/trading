"""Auth guards for the web UI."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from web.auth import (
    create_session_token,
    read_session_token,
    require_password_configured,
    verify_credentials,
)


class WebAuthTests(unittest.TestCase):
    def test_refuse_empty_password(self) -> None:
        with patch("web.auth.settings") as s:
            s.web_ui_password = ""
            with self.assertRaises(RuntimeError):
                require_password_configured()

    def test_verify_credentials_constant_time_ok(self) -> None:
        with patch("web.auth.settings") as s:
            s.web_ui_username = "admin"
            s.web_ui_password = "s3cret"
            s.web_ui_users = ""
            self.assertTrue(verify_credentials("admin", "s3cret"))
            self.assertFalse(verify_credentials("admin", "wrong"))
            self.assertFalse(verify_credentials("other", "s3cret"))

    def test_verify_credentials_accepts_configured_users(self) -> None:
        with patch("web.auth.settings") as s:
            s.web_ui_username = "admin"
            s.web_ui_password = "s3cret"
            s.web_ui_users = "ed:TBY, extra:pass"
            self.assertTrue(verify_credentials("admin", "s3cret"))
            self.assertTrue(verify_credentials("ed", "TBY"))
            self.assertTrue(verify_credentials("extra", "pass"))
            self.assertFalse(verify_credentials("ed", "wrong"))
            self.assertFalse(verify_credentials("nobody", "TBY"))

    def test_verify_credentials_ignores_malformed_entries(self) -> None:
        with patch("web.auth.settings") as s:
            s.web_ui_username = "admin"
            s.web_ui_password = "s3cret"
            s.web_ui_users = "ed, :pw, ,good:pw"
            self.assertTrue(verify_credentials("admin", "s3cret"))
            self.assertTrue(verify_credentials("good", "pw"))
            self.assertFalse(verify_credentials("ed", "pw"))

    def test_session_roundtrip(self) -> None:
        with patch("web.auth.settings") as s:
            s.web_ui_username = "admin"
            s.web_ui_password = "s3cret"
            s.web_ui_secret_key = "unit-test-secret-key"
            s.web_ui_session_hours = 12
            token = create_session_token("admin")
            self.assertEqual(read_session_token(token), "admin")
            self.assertIsNone(read_session_token("not-a-token"))


if __name__ == "__main__":
    unittest.main()
