import io
import json
import unittest
import urllib.error
from unittest.mock import Mock

from scripts.check_brevo_key import check


class BrevoKeyCheckTests(unittest.TestCase):
    def error_check(self, message, code=401):
        body = json.dumps({"code": "unauthorized", "message": message}).encode()
        error = urllib.error.HTTPError("https://api.brevo.com/v3/account", code, "private-provider-text", {}, io.BytesIO(body))
        opener = Mock(side_effect=error)
        result = check({"BREVO_API_KEY": "unit-test-key"}, opener)
        opener.assert_called_once()
        self.assertEqual(opener.call_args.args[0].get_method(), "GET")
        self.assertNotIn("private", json.dumps(result))
        return result

    def test_missing_smtp_other_provider_and_invalid_keys_never_make_request(self):
        for key, status in [("", "missing"), ("xsmtpsib-private-value", "wrong_key_type"),
                            ("sk-proj-private-value", "wrong_provider_key"),
                            ("private key", "invalid_key_format")]:
            with self.subTest(status=status):
                opener = Mock()
                result = check({"BREVO_API_KEY": key}, opener)
                self.assertEqual(result["status"], status)
                self.assertNotIn("private", json.dumps(result))
                opener.assert_not_called()

    def test_ip_denial_is_distinct_from_an_invalid_key_and_omits_address(self):
        result = self.error_check("We detected an unrecognised IP address 192.0.2.45: private-key-value")
        self.assertEqual(result["status"], "ip_access_denied")
        self.assertNotIn("192.0.2.45", json.dumps(result))
        self.assertEqual(self.error_check("Key not found: private-key-value")["status"], "invalid_api_key")
        self.assertEqual(self.error_check("private-key-value")["status"], "authentication_failed")
        self.assertEqual(self.error_check("private-key-value", 403)["status"], "permission_denied")

    def test_account_data_is_discarded_and_transactional_status_is_recorded(self):
        response = Mock(status=200)
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        for enabled, status in [(True, "ready"), (False, "transactional_email_disabled")]:
            with self.subTest(enabled=enabled):
                response.read.return_value = json.dumps({"email": "private@example.org", "firstName": "Private",
                                                        "relay": {"enabled": enabled, "data": "private-login"}}).encode()
                result = check({"BREVO_API_KEY": "unit-test-key"}, Mock(return_value=response))
                self.assertEqual(result["status"], status)
                self.assertEqual(result["transactional_email_enabled"], enabled)
                self.assertNotIn("private", json.dumps(result).casefold())

    def test_invalid_account_response_does_not_claim_authentication(self):
        response = Mock(status=200)
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        for body in [b"private-invalid-json", b"null", b"[]", b"x" * 16001]:
            response.read.return_value = body
            result = check({"BREVO_API_KEY": "unit-test-key"}, Mock(return_value=response))
            self.assertEqual(result["status"], "invalid_response")
            self.assertNotIn("private", json.dumps(result))


if __name__ == "__main__":
    unittest.main()
