import io
import json
import unittest
import urllib.error
from unittest.mock import MagicMock

from scripts.check_api_key import check


class KeyCheckTests(unittest.TestCase):
    def test_missing_key_does_not_make_request(self):
        opener = MagicMock()
        self.assertEqual(check({}, "gpt-5", opener)["status"], "missing")
        opener.assert_not_called()

    def test_success_only_records_safe_status(self):
        response = MagicMock()
        response.__enter__.return_value.read.return_value = b'{"status":"completed"}'
        result = check({"MIKI_OPENAI_API_KEY": "private-key-value"}, "gpt-5", MagicMock(return_value=response))
        self.assertEqual(result["status"], "ready")
        self.assertNotIn("private-key-value", json.dumps(result))

    def test_auth_failure_does_not_record_key_fragments(self):
        error = urllib.error.HTTPError("https://api.openai.com/v1/responses", 401, "Unauthorized", {},
                                       io.BytesIO(b'{"error":{"code":"invalid_api_key","message":"private-key-value"}}'))
        result = check({"MIKI_OPENAI_API_KEY": "private-key-value"}, "gpt-5", MagicMock(side_effect=error))
        self.assertEqual(result["status"], "authentication_failed")
        self.assertNotIn("private-key-value", json.dumps(result))

    def test_billing_failure_is_distinct_from_wrong_key(self):
        error = urllib.error.HTTPError("https://api.openai.com/v1/responses", 429, "Quota", {},
                                       io.BytesIO(b'{"error":{"code":"insufficient_quota"}}'))
        result = check({"MIKI_OPENAI_API_KEY": "test"}, "gpt-5", MagicMock(side_effect=error))
        self.assertEqual(result["status"], "billing_required")

    def test_current_quota_codes_require_action_and_never_expose_response_message(self):
        for code in ('credit_balance_exhausted', 'organization_spend_limit_exceeded',
                     'project_spend_limit_exceeded', 'organization_usage_limit_exceeded'):
            with self.subTest(code=code):
                error = urllib.error.HTTPError('https://api.openai.com/v1/responses', 429, 'Quota', {},
                    io.BytesIO(json.dumps({'error': {'code': code, 'type': 'insufficient_quota',
                                                    'message': 'private-key-value'}}).encode()))
                result = check({'MIKI_OPENAI_API_KEY': 'private-key-value'}, 'gpt-5', MagicMock(side_effect=error))
                self.assertEqual(result['status'], 'billing_required')
                self.assertEqual(result['error_code'], code)
                self.assertNotIn('private-key-value', json.dumps(result))


if __name__ == "__main__":
    unittest.main()
