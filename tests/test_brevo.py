import copy
import io
import json
import os
import ssl
import tempfile
import unittest
import urllib.error
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch

from miki_jobsearch.service import (DeliveryFailure, MailConfigurationError, NoMailRedirect,
                                    StateSyncError, mail_settings, make_message, run, send_brevo)
from scripts.deployment_check import readiness

ROOT = Path(__file__).resolve().parent.parent
FIXTURE = json.loads((ROOT / "tests/fixtures/report.json").read_text())
MONDAY = datetime.fromisoformat("2026-10-05T10:05:00+02:00")
ENV = {"BREVO_API_KEY": "unit-test-api-key", "MAIL_FROM": "sender@example.org",
       "MAIL_TO": "recipient@example.org, second@example.org", "MAIL_CC": "copy@example.org"}
PROVIDER_ID = "<20261005.123456@smtp-relay.mailin.fr>"


def acknowledgement(body=None, status=201):
    response = Mock(status=status)
    response.read.return_value = json.dumps(body if body is not None else {"messageId": PROVIDER_ID}).encode()
    response.__enter__ = Mock(return_value=response)
    response.__exit__ = Mock(return_value=False)
    return response


class BrevoSettingsTests(unittest.TestCase):
    def test_default_uses_brevo_and_needs_no_google_password(self):
        settings = mail_settings(ENV)
        self.assertEqual(settings["EMAIL_PROVIDER"], "brevo")
        self.assertNotIn("GMAIL_APP_PASSWORD", settings)
        fallback = dict(ENV, GMAIL_USER=ENV["MAIL_FROM"])
        del fallback["MAIL_FROM"]
        self.assertEqual(mail_settings(fallback)["MAIL_FROM"], ENV["MAIL_FROM"])

    def test_missing_key_and_invalid_addresses_are_safe(self):
        missing = dict(ENV, BREVO_API_KEY="")
        with self.assertRaisesRegex(MailConfigurationError, "BREVO_API_KEY"):
            mail_settings(missing)
        for name, value in [("BREVO_API_KEY", "private-key\n" + "injection"),
                            ("MAIL_FROM", "private-invalid-address"),
                            ("MAIL_FROM", "sender@example.org,other@example.org"),
                            ("MAIL_TO", "recipient@example.org\nBcc: private-address"),
                            ("MAIL_TO", "recipient@example.org,"),
                            ("EMAIL_PROVIDER", "private-unsupported-provider")]:
            with self.subTest(name=name, value=value):
                with self.assertRaises(MailConfigurationError) as caught:
                    mail_settings(dict(ENV, **{name: value}))
                self.assertNotIn("private", str(caught.exception))

    def test_readiness_records_presence_and_format_without_values(self):
        result = readiness("send", dict(ENV, MIKI_OPENAI_API_KEY="private-openai-key"))
        self.assertEqual(result["email_provider"], "brevo")
        self.assertEqual(result["missing_requirements"], [])
        self.assertEqual(result["invalid_mail_formats"], [])
        self.assertNotIn("GMAIL_APP_PASSWORD", result["checked_requirements"])
        for value in ENV.values():
            self.assertNotIn(value, json.dumps(result))
        result = readiness("send", {"GMAIL_USER": "sender@example.org", "MAIL_TO": "recipient@example.org",
                                    "MIKI_OPENAI_API_KEY": "private-openai-key"})
        self.assertEqual(result["missing_requirements"], ["BREVO_API_KEY"])


class BrevoDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.settings = mail_settings(ENV)
        self.message = make_message({"subject": "Stellen für Miki", "message_id": "<miki-2026-10-05@test>"},
                                    "<html><body>Geprüfte Stellen für Miki</body></html>", self.settings)

    def test_payload_tls_and_durable_marker_precede_single_api_request(self):
        order = []
        opener = Mock()
        opener.open.side_effect = lambda *a, **k: (order.append("post") or acknowledgement())
        with patch("miki_jobsearch.service.urllib.request.build_opener", return_value=opener) as build:
            result = send_brevo(self.message, self.settings, lambda: order.append("persist"))
        self.assertEqual(order, ["persist", "post"])
        self.assertEqual(result["provider_message_id"], PROVIDER_ID)
        self.assertEqual(result["status"], "sent")
        request = opener.open.call_args.args[0]
        self.assertEqual(request.full_url, "https://api.brevo.com/v3/smtp/email")
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.get_header("Api-key"), ENV["BREVO_API_KEY"])
        payload = json.loads(request.data)
        self.assertEqual(payload["sender"]["email"], ENV["MAIL_FROM"])
        self.assertEqual(payload["to"], [{"email": "recipient@example.org"}, {"email": "second@example.org"}])
        self.assertEqual(payload["cc"], [{"email": ENV["MAIL_CC"]}])
        self.assertIn("Geprüfte Stellen", payload["htmlContent"])
        self.assertIn("Stellenbericht", payload["textContent"])
        self.assertNotIn(ENV["BREVO_API_KEY"], request.data.decode())
        self.assertIsInstance(build.call_args.args[0], NoMailRedirect)
        self.assertEqual(build.call_args.args[1]._context.verify_mode, ssl.CERT_REQUIRED)

    def test_no_cc_and_stable_provider_deduplication_reference(self):
        settings = dict(self.settings, MAIL_CC="")
        opener = Mock()
        opener.open.side_effect = lambda *a, **k: acknowledgement()
        with patch("miki_jobsearch.service.urllib.request.build_opener", return_value=opener):
            send_brevo(self.message, settings, Mock())
            send_brevo(self.message, settings, Mock())
            send_brevo(self.message, dict(settings, MAIL_TO="changed@example.org"), Mock())
        payloads = [json.loads(call.args[0].data) for call in opener.open.call_args_list]
        self.assertNotIn("cc", payloads[0])
        self.assertEqual(payloads[0]["headers"]["Idempotency-Key"], payloads[1]["headers"]["Idempotency-Key"])
        self.assertNotEqual(payloads[0]["headers"]["Idempotency-Key"], payloads[2]["headers"]["Idempotency-Key"])

    def test_failed_durable_marker_prevents_post(self):
        opener = Mock()
        with patch("miki_jobsearch.service.urllib.request.build_opener", return_value=opener):
            with self.assertRaises(StateSyncError):
                send_brevo(self.message, self.settings, Mock(side_effect=StateSyncError("checkpoint failed")))
        opener.open.assert_not_called()

    def test_explicit_rejections_are_retryable_and_never_expose_body(self):
        for code in [400, 401, 402, 403, 422, 429]:
            with self.subTest(code=code):
                error = urllib.error.HTTPError("https://api.brevo.com/v3/smtp/email", code, "private-message", {},
                                              io.BytesIO(b'{"message":"private-api-key-and-address"}'))
                opener = Mock()
                opener.open.side_effect = error
                with patch("miki_jobsearch.service.urllib.request.build_opener", return_value=opener):
                    with self.assertRaises(DeliveryFailure) as caught:
                        send_brevo(self.message, self.settings, Mock())
                self.assertFalse(caught.exception.ambiguous)
                self.assertIn(str(code), str(caught.exception))
                self.assertNotIn("private", str(caught.exception))
                self.assertEqual(opener.open.call_count, 1)

    def test_server_error_timeout_redirect_and_duplicate_are_uncertain(self):
        errors = [urllib.error.URLError("private-connection-info"), TimeoutError("private-info")]
        errors += [urllib.error.HTTPError("https://api.brevo.com/v3/smtp/email", code, "private-body", {},
                                         io.BytesIO(b"private-info")) for code in [302, 408, 409, 500, 503]]
        for error in errors:
            with self.subTest(error=type(error).__name__, code=getattr(error, "code", None)):
                opener = Mock()
                opener.open.side_effect = error
                with patch("miki_jobsearch.service.urllib.request.build_opener", return_value=opener):
                    with self.assertRaises(DeliveryFailure) as caught:
                        send_brevo(self.message, self.settings, Mock())
                self.assertTrue(caught.exception.ambiguous)
                self.assertNotIn("private", str(caught.exception))
                self.assertEqual(opener.open.call_count, 1)

    def test_invalid_success_never_marks_sent(self):
        responses = [acknowledgement({}, 201), acknowledgement({"messageId": "private-key"}),
                     acknowledgement(status=200), acknowledgement(["unexpected-list"])]
        broken = acknowledgement()
        broken.read.return_value = b"invalid-json"
        responses.append(broken)
        for response in responses:
            with self.subTest(response=response):
                opener = Mock()
                opener.open.return_value = response
                with patch("miki_jobsearch.service.urllib.request.build_opener", return_value=opener):
                    with self.assertRaises(DeliveryFailure) as caught:
                        send_brevo(self.message, self.settings, Mock())
                self.assertTrue(caught.exception.ambiguous)
                self.assertNotIn("private", str(caught.exception))

    def test_switch_reuses_unsent_report_then_restart_cannot_send_again(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, ENV, clear=True):
            store = Path(temp) / "state"
            researcher = Mock(side_effect=lambda *a: copy.deepcopy(FIXTURE))
            options = dict(root=ROOT, store=store, now=MONDAY, dry_run=False, researcher=researcher,
                           verifier=lambda *a: (True, "mock verified"))
            with patch.dict(os.environ, {"EMAIL_PROVIDER": "gmail", "GMAIL_USER": ENV["MAIL_FROM"],
                                         "GMAIL_APP_PASSWORD": "unit-test-password"}):
                with self.assertRaises(DeliveryFailure):
                    run(**options, sender=Mock(side_effect=DeliveryFailure("Prior SMTP authentication rejection")))
            prior = json.loads((store / "runs/2026-10-05.json").read_text())
            self.assertEqual(prior["email_provider"], "gmail")
            order = []
            opener = Mock()
            opener.open.side_effect = lambda *a, **k: (order.append("post") or acknowledgement())
            with patch("miki_jobsearch.service.urllib.request.build_opener", return_value=opener):
                result = run(**options, persist=lambda record: order.append(record["status"]))
                skipped = run(**options)
            self.assertEqual(result["status"], "sent")
            self.assertEqual(order, ["prepared", "sending", "post", "sent"])
            self.assertEqual(skipped["delivery_status"], "sent")
            self.assertEqual(researcher.call_count, 1)
            opener.open.assert_called_once()
            record = json.loads((store / "runs/2026-10-05.json").read_text())
            self.assertEqual(record["email_provider"], "brevo")
            self.assertEqual(record["provider_message_id"], PROVIDER_ID)
            self.assertNotIn(ENV["BREVO_API_KEY"], json.dumps(record))

    def test_uncertain_api_delivery_blocks_next_attempt(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, ENV, clear=True):
            store = Path(temp) / "state"
            options = dict(root=ROOT, store=store, now=MONDAY, dry_run=False,
                           researcher=lambda *a: copy.deepcopy(FIXTURE), verifier=lambda *a: (True, "mock verified"))
            opener = Mock()
            opener.open.side_effect = TimeoutError("private-info")
            with patch("miki_jobsearch.service.urllib.request.build_opener", return_value=opener):
                with self.assertRaises(DeliveryFailure):
                    run(**options)
                self.assertEqual(run(**options)["delivery_status"], "uncertain")
            opener.open.assert_called_once()


if __name__ == "__main__":
    unittest.main()
