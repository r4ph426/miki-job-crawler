import copy
import io
import json
import os
import smtplib
import shutil
import subprocess
import tempfile
import unittest
import urllib.error
from datetime import date, datetime
from pathlib import Path
from unittest.mock import Mock, patch

from miki_jobsearch import research as provider
from miki_jobsearch.research import ResearchError, allowed_url, select_jobs, validate_report
from miki_jobsearch.service import (DeliveryFailure, MailConfigurationError, StateSyncError, atomic_json, due,
                                    git_persister, load_history, mail_settings, next_run, reconcile, render_report, run, send_smtp, status)
from scripts.import_archive import convert_history
from scripts.deployment_check import readiness

ROOT = Path(__file__).resolve().parent.parent
CONFIG = json.loads((ROOT / "config/search.json").read_text())
FIXTURE = json.loads((ROOT / "tests/fixtures/report.json").read_text())
MONDAY = datetime.fromisoformat("2026-10-05T10:05:00+02:00")
MAIL_ENV = {"GMAIL_USER": "sender@example.org", "GMAIL_APP_PASSWORD": "unit-test-only",
            "MAIL_TO": "recipient@example.org", "MAIL_CC": "copy@example.org"}


class ScheduleTests(unittest.TestCase):
    def test_dst_and_weekends(self):
        cases = [
            ("2026-10-23T07:59:00+00:00", False),
            ("2026-10-23T08:00:00+00:00", True),
            ("2026-10-26T08:00:00+00:00", False),
            ("2026-10-26T09:00:00+00:00", True),
            ("2027-03-29T08:00:00+00:00", True),
            ("2026-10-24T10:00:00+02:00", False),
            ("2026-10-25T10:00:00+01:00", False),
        ]
        for clock, expected in cases:
            with self.subTest(clock=clock):
                self.assertEqual(due(datetime.fromisoformat(clock), CONFIG), expected)

    def test_next_weekday_across_dst(self):
        self.assertEqual(next_run(datetime.fromisoformat("2026-10-23T12:00:00+02:00"), CONFIG),
                         "2026-10-26T10:00:00+01:00")


class ResearchTests(unittest.TestCase):
    def test_all_sources_down_is_failure_not_empty_mail(self):
        report = copy.deepcopy(FIXTURE)
        report["jobs"] = []
        for source in report["source_checks"]:
            source["status"] = "unavailable"
        with self.assertRaises(ResearchError):
            validate_report(report)

    def test_missing_family_and_invalid_scores_rejected(self):
        report = copy.deepcopy(FIXTURE)
        report["searches"]["B"] = ["Einkauf"]
        with self.assertRaises(ResearchError):
            validate_report(report)
        report = copy.deepcopy(FIXTURE)
        report["jobs"][0]["scores"]["skill"] = 100
        with self.assertRaises(ResearchError):
            validate_report(report)

    def test_duplicates_expired_low_score_and_unverifiable(self):
        report = copy.deepcopy(FIXTURE)
        base = report["jobs"][0]
        report["jobs"] = []
        for name in ["known", "duplicate", "expired", "low", "unverifiable", "valid"]:
            j = copy.deepcopy(base)
            j["title"], j["url"] = name, f"https://www.stepstone.de/jobs/{name}"
            report["jobs"].append(j)
        report["jobs"][1]["title"] = "known"
        report["jobs"][2]["deadline"] = "2026-10-04"
        report["jobs"][3]["scores"] = dict(skill=10, entry=10, commute=0, conditions=0)
        history = [dict(base, title="known", url="https://www.stepstone.de/jobs/known?utm_source=test")]
        verifier = lambda j, config: (j["title"] != "unverifiable", "supporting quote missing")
        jobs, rejected = select_jobs(report, CONFIG, history, MONDAY.date(), verifier)
        self.assertEqual([j["title"] for j in jobs], ["valid"])
        self.assertEqual(len(rejected), 5)

    def test_url_restrictions(self):
        for url in ["http://www.stepstone.de/x", "https://localhost/x", "https://127.0.0.1/x",
                    "https://www.stepstone.de.evil.test/x", "https://user:pass@www.stepstone.de/x",
                    "https://www.stepstone.de:123/x"]:
            self.assertFalse(allowed_url(url, CONFIG["source_domains"]))
        self.assertTrue(allowed_url("https://aeyde.jobs.personio.de/job/1", CONFIG["source_domains"]))

    def test_detail_page_requires_real_quote_and_rejects_dead_link(self):
        job = copy.deepcopy(FIXTURE["jobs"][0])
        job["url"] = "https://aeyde.jobs.personio.de/job/123"
        response = Mock()
        response.headers.get_content_charset.return_value = "utf-8"
        response.read.return_value = ("<p>" + job["evidence"] + "</p>").encode()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        with patch.object(provider.urllib.request, "build_opener") as build:
            build.return_value.open.return_value = response
            self.assertTrue(provider.verify_job(job, CONFIG)[0])
            response.read.return_value = b"<p>Unrelated content</p>"
            self.assertFalse(provider.verify_job(job, CONFIG)[0])
            build.return_value.open.side_effect = urllib.error.HTTPError(job["url"], 410, "Gone", {}, None)
            self.assertEqual(provider.verify_job(job, CONFIG), (False, "Expired"))

    def test_api_response_without_search_cannot_be_accepted(self):
        response = Mock()
        response.read.return_value = json.dumps({"status": "completed", "output": []}).encode()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        with patch.dict(os.environ, {"MIKI_OPENAI_API_KEY": "unit-test-only"}), \
             patch.object(provider.urllib.request, "urlopen", return_value=response):
            with self.assertRaisesRegex(ResearchError, "did not execute web search"):
                provider.research(ROOT, CONFIG, [], MONDAY.date())

    def test_large_research_request_is_bounded_and_retains_source_verification(self):
        response = Mock()
        response.read.return_value = json.dumps({"status": "completed", "output": [
            {"type": "web_search_call", "status": "completed"},
            {"type": "message", "content": [{"type": "output_text", "text": json.dumps(FIXTURE)}]},
        ]}).encode()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        with patch.dict(os.environ, {"MIKI_OPENAI_API_KEY": "unit-test-only"}), \
             patch.object(provider.urllib.request, "urlopen", return_value=response) as opener:
            result = provider.research(ROOT, CONFIG, [], MONDAY.date())
        payload = json.loads(opener.call_args.args[0].data)
        self.assertEqual(payload["max_output_tokens"], 12000)
        self.assertEqual(payload["reasoning"], {"effort": "low"})
        self.assertEqual(len(result["source_checks"]), 5)

    def test_http_rate_limit_diagnostic_omits_private_provider_message(self):
        error = urllib.error.HTTPError("https://api.openai.com/v1/responses", 429, "Limited", {},
                                      io.BytesIO(json.dumps({"error": {"code": "rate_limit_exceeded",
                                                                      "message": "unit-test-secret"}}).encode()))
        with patch.dict(os.environ, {"MIKI_OPENAI_API_KEY": "unit-test-only"}), \
             patch.object(provider.urllib.request, "urlopen", side_effect=error):
            with self.assertRaises(ResearchError) as caught:
                provider.research(ROOT, CONFIG, [], MONDAY.date())
        self.assertEqual(caught.exception.diagnostic,
                         {"code": "api_http_error", "http_status": 429, "error_code": "rate_limit_exceeded"})
        self.assertNotIn("unit-test-secret", str(caught.exception))


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Path(self.temp.name) / "state"
        self.output = Path(self.temp.name) / "out"
        self.patch_env = patch.dict(os.environ, MAIL_ENV)
        self.patch_env.start()
        self.addCleanup(self.patch_env.stop)
        self.researcher = Mock(side_effect=lambda *args: copy.deepcopy(FIXTURE))
        self.verifier = lambda job, config: (True, "mock verified")
        self.transitions = []

    def send(self, message, settings, on_sending):
        on_sending()
        self.assertEqual(self.transitions[-1], "sending")
        self.assertEqual(message["To"], MAIL_ENV["MAIL_TO"])
        self.assertTrue(message.is_multipart())
        return {"status": "sent", "refused_count": 0}

    def invoke(self, **kwargs):
        options = dict(root=ROOT, store=self.store, now=MONDAY, dry_run=False,
                       researcher=self.researcher, verifier=self.verifier, sender=self.send,
                       persist=lambda record: self.transitions.append(record["status"]))
        options.update(kwargs)
        return run(**options)

    def record(self):
        return json.loads((self.store / "runs/2026-10-05.json").read_text())

    def test_sent_then_restart_does_not_research_or_send_again(self):
        result = self.invoke()
        self.assertEqual(result["status"], "sent")
        self.assertEqual(self.transitions, ["prepared", "sending", "sent"])
        self.assertEqual(len(load_history(ROOT, self.store)), 146)
        self.assertEqual(self.invoke()["delivery_status"], "sent")
        self.assertEqual(self.researcher.call_count, 1)

    def test_dry_run_never_changes_history_or_calls_sender(self):
        sender = Mock()
        result = self.invoke(dry_run=True, fixture=FIXTURE, output_dir=self.output, sender=sender)
        self.assertEqual(result["status"], "dry_run")
        self.assertFalse((self.store / "history.json").exists())
        sender.assert_not_called()
        self.assertIn("keine Live-Recherche", (self.output / "mail.html").read_text())
        with self.assertRaises(ValueError):
            self.invoke(fixture=FIXTURE)

    def test_known_failure_keeps_history_unchanged_and_reuses_outbox(self):
        def reject(message, settings, on_sending):
            on_sending()
            raise DeliveryFailure("explicit test rejection")
        with self.assertRaises(DeliveryFailure):
            self.invoke(sender=reject)
        self.assertEqual(self.record()["status"], "failed")
        self.assertEqual(len(load_history(ROOT, self.store)), 145)
        self.assertEqual(self.invoke()["status"], "sent")
        self.assertEqual(self.researcher.call_count, 1)

    def test_bad_mail_settings_preserve_research_and_report_current_configuration_failure(self):
        sender = Mock(side_effect=DeliveryFailure("test pre-DATA rejection"))
        with self.assertRaises(DeliveryFailure):
            self.invoke(sender=sender)
        sender.reset_mock()
        with patch.dict(os.environ, {"MAIL_TO": "private-invalid-address"}):
            with self.assertRaises(MailConfigurationError):
                self.invoke(sender=sender)
        record = self.record()
        self.assertEqual(record["status"], "configuration_failed")
        self.assertEqual(record["configuration_diagnostic"], "invalid_mail_addresses")
        self.assertIn("report", record)
        self.assertIn("last_attempt_at", record)
        self.assertNotIn("private-invalid-address", json.dumps(record))
        sender.assert_not_called()
        self.assertEqual(self.invoke()["status"], "sent")
        self.assertEqual(self.researcher.call_count, 1)

    def test_pre_data_unexpected_failure_records_type_without_private_message(self):
        sender = Mock(side_effect=ValueError("unit-test-secret"))
        with self.assertRaises(ValueError):
            self.invoke(sender=sender)
        self.assertEqual(self.record()["status"], "failed")
        self.assertEqual(self.record()["error"], "Delivery preparation failed (ValueError)")
        self.assertNotIn("unit-test-secret", json.dumps(self.record()))
        self.assertEqual(self.invoke()["status"], "sent")
        self.assertEqual(self.researcher.call_count, 1)

    def test_ambiguous_failure_requires_reconciliation(self):
        def unknown(message, settings, on_sending):
            on_sending()
            raise DeliveryFailure("unknown SMTP acceptance", ambiguous=True)
        with self.assertRaises(DeliveryFailure):
            self.invoke(sender=unknown)
        self.assertEqual(self.record()["status"], "uncertain")
        self.assertEqual(self.invoke()["delivery_status"], "uncertain")
        self.assertEqual(len(load_history(ROOT, self.store)), 145)

    def test_uncertain_jobs_are_reserved_on_later_days(self):
        def unknown(message, settings, on_sending):
            on_sending()
            raise DeliveryFailure("unknown SMTP acceptance", ambiguous=True)
        with self.assertRaises(DeliveryFailure):
            self.invoke(sender=unknown)
        self.assertEqual(status(ROOT, self.store, MONDAY)["reserved_jobs_awaiting_reconciliation"], 1)
        tomorrow = datetime.fromisoformat("2026-10-06T10:05:00+02:00")
        self.assertEqual(self.invoke(now=tomorrow)["hits"], 0)
        self.assertEqual(len(load_history(ROOT, self.store)), 145)

    def test_process_crash_after_checkpoint_does_not_retry_delivery(self):
        def crash(message, settings, on_sending):
            on_sending()
            raise RuntimeError("process interrupted")
        with self.assertRaises(RuntimeError):
            self.invoke(sender=crash)
        self.assertEqual(self.record()["status"], "sending")
        self.assertEqual(self.invoke()["delivery_status"], "sending")

    def test_partial_recipient_acceptance_is_not_resent(self):
        def partial(message, settings, on_sending):
            on_sending()
            return {"status": "partial", "refused_count": 1}
        self.assertEqual(self.invoke(sender=partial)["status"], "partial")
        self.assertEqual(self.invoke()["delivery_status"], "partial")
        self.assertEqual(len(load_history(ROOT, self.store)), 146)

    def test_failed_durable_checkpoint_prevents_smtp(self):
        sender = Mock()
        def fail(record):
            raise StateSyncError("push failed")
        with self.assertRaises(StateSyncError):
            self.invoke(sender=sender, persist=fail)
        sender.assert_not_called()

    def test_failed_sending_checkpoint_prevents_data(self):
        def fail(record):
            if record["status"] == "sending":
                raise StateSyncError("push failed")
        smtp = Mock()
        with patch("miki_jobsearch.service.smtplib.SMTP_SSL", return_value=smtp):
            with self.assertRaises(StateSyncError):
                self.invoke(sender=send_smtp, persist=fail)
        smtp.send_message.assert_not_called()

    def test_final_state_sync_failure_blocks_blind_retry(self):
        def fail(record):
            self.transitions.append(record["status"])
            if record["status"] == "sent":
                raise StateSyncError("final push failed")
        with self.assertRaises(StateSyncError):
            self.invoke(persist=fail)
        self.assertEqual(self.record()["status"], "sent")
        self.assertEqual(self.invoke()["delivery_status"], "sent")

    def test_weekend_cannot_send(self):
        sender = Mock()
        result = self.invoke(now=datetime.fromisoformat("2026-10-10T12:00:00+02:00"), sender=sender)
        self.assertEqual(result["status"], "skipped")
        sender.assert_not_called()
        self.researcher.assert_not_called()

    def test_research_failure_is_saved_and_never_sends(self):
        sender = Mock()
        self.researcher.side_effect = ResearchError("Private provider message: unit-test-secret",
                                                   code="api_http_error", http_status=429)
        with self.assertRaises(ResearchError):
            self.invoke(sender=sender)
        self.assertEqual(self.record()["status"], "research_failed")
        self.assertEqual(self.record()["research_diagnostic"], {"code": "api_http_error", "http_status": 429})
        self.assertNotIn("unit-test-secret", json.dumps(self.record()))
        sender.assert_not_called()
        self.assertEqual(len(load_history(ROOT, self.store)), 145)

    def test_reconcile_confirmed_delivery_adds_history_once(self):
        def crash(message, settings, on_sending):
            on_sending()
            raise RuntimeError("crash")
        with self.assertRaises(RuntimeError):
            self.invoke(sender=crash)
        reconcile(self.store, "2026-10-05", "sent", "Found Message-ID in Gmail Sent")
        self.assertEqual(len(load_history(ROOT, self.store)), 146)
        self.assertEqual(self.invoke()["delivery_status"], "sent")
        with self.assertRaises(ValueError):
            reconcile(self.store, "2026-10-05", "sent", "Already reconciled")

    def test_reconcile_not_sent_allows_safe_retry(self):
        def crash(message, settings, on_sending):
            on_sending()
            raise RuntimeError("crash")
        with self.assertRaises(RuntimeError):
            self.invoke(sender=crash)
        reconcile(self.store, "2026-10-05", "not-sent", "Confirmed Gmail did not accept the message")
        self.assertEqual(self.invoke()["status"], "sent")
        self.assertEqual(self.researcher.call_count, 1)

    def test_partial_delivery_cannot_be_marked_unsent(self):
        def partial(message, settings, on_sending):
            on_sending()
            return {"status": "partial", "refused_count": 1}
        self.invoke(sender=partial)
        with self.assertRaises(ValueError):
            reconcile(self.store, "2026-10-05", "not-sent", "Do not resend to accepted recipients")


class GitPersistenceTests(unittest.TestCase):
    def test_fresh_clone_preserves_delivery_guard_and_history(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            remote, checkout, clone = base / "remote.git", base / "checkout", base / "fresh"
            def git(cwd, *args):
                return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)
            git(base, "init", "--bare", str(remote))
            checkout.mkdir()
            git(checkout, "init", "-b", "main")
            git(checkout, "config", "user.name", "Offline Test")
            git(checkout, "config", "user.email", "offline@example.org")
            shutil.copytree(ROOT / "config", checkout / "config")
            shutil.copytree(ROOT / "data", checkout / "data")
            (checkout / ".gitignore").write_text("*.lock\n")
            git(checkout, "add", ".")
            git(checkout, "commit", "-m", "Offline test seed")
            git(checkout, "remote", "add", "origin", str(remote))
            git(checkout, "push", "origin", "main")
            git(remote, "symbolic-ref", "HEAD", "refs/heads/main")
            def send(message, settings, on_sending):
                on_sending()
                return {"status": "sent", "refused_count": 0}
            with patch.dict(os.environ, dict(MAIL_ENV, STATE_BRANCH="main")):
                persist = git_persister(checkout, checkout / "state")
                result = run(checkout, checkout / "state", now=MONDAY, dry_run=False,
                             researcher=lambda *args: copy.deepcopy(FIXTURE),
                             verifier=lambda *args: (True, "mock verified"), sender=send, persist=persist)
                self.assertEqual(result["status"], "sent")
                git(base, "clone", str(remote), str(clone))
                sender = Mock()
                result = run(clone, clone / "state", now=MONDAY, dry_run=False, sender=sender)
            self.assertEqual(result["delivery_status"], "sent")
            self.assertEqual(len(load_history(clone, clone / "state")), 146)
            sender.assert_not_called()
            self.assertEqual(git(clone, "status", "--porcelain").stdout, "")


class SmtpTests(unittest.TestCase):
    def test_advertised_login_fallback_requires_tls_and_does_not_repeat_plain_auth(self):
        smtp = Mock()
        smtp.esmtp_features = {"auth": "LOGIN PLAIN"}
        smtp.send_message.return_value = {}
        marker = Mock()
        with patch("miki_jobsearch.service.smtplib.SMTP_SSL", side_effect=smtplib.SMTPServerDisconnected("test")), \
             patch("miki_jobsearch.service.smtplib.SMTP", return_value=smtp):
            result = send_smtp(Mock(), MAIL_ENV, marker)
        calls = [call[0] for call in smtp.method_calls]
        self.assertLess(calls.index("starttls"), calls.index("auth"))
        self.assertLess(calls.index("auth"), calls.index("send_message"))
        self.assertEqual(smtp.auth.call_args.args[0], "LOGIN")
        self.assertFalse(smtp.auth.call_args.kwargs["initial_response_ok"])
        smtp.login.assert_not_called()
        marker.assert_called_once()
        self.assertEqual(result["status"], "sent")

    def test_closed_ssl_connection_uses_verified_starttls_before_login_and_data(self):
        smtp = Mock()
        smtp.send_message.return_value = {}
        marker = Mock()
        with patch("miki_jobsearch.service.smtplib.SMTP_SSL", side_effect=smtplib.SMTPServerDisconnected("test")), \
             patch("miki_jobsearch.service.smtplib.SMTP", return_value=smtp):
            result = send_smtp(Mock(), MAIL_ENV, marker)
        calls = [call[0] for call in smtp.method_calls]
        self.assertLess(calls.index("starttls"), calls.index("login"))
        self.assertLess(calls.index("login"), calls.index("send_message"))
        self.assertIsNotNone(smtp.starttls.call_args.kwargs["context"])
        self.assertFalse(smtp.login.call_args.kwargs["initial_response_ok"])
        marker.assert_called_once()
        self.assertEqual(result["status"], "sent")

    def test_starttls_failure_never_authenticates_or_submits_data(self):
        smtp = Mock()
        smtp.starttls.side_effect = smtplib.SMTPNotSupportedError("private server text")
        marker = Mock()
        with patch("miki_jobsearch.service.smtplib.SMTP_SSL", side_effect=smtplib.SMTPServerDisconnected("test")), \
             patch("miki_jobsearch.service.smtplib.SMTP", return_value=smtp):
            with self.assertRaises(DeliveryFailure) as caught:
                send_smtp(Mock(), MAIL_ENV, marker)
        self.assertIn("starttls failed on port 587", str(caught.exception))
        self.assertNotIn("private server text", str(caught.exception))
        smtp.login.assert_not_called()
        smtp.send_message.assert_not_called()
        marker.assert_not_called()

    def test_data_acceptance_survives_close(self):
        smtp = Mock()
        smtp.send_message.return_value = {}
        with patch("miki_jobsearch.service.smtplib.SMTP_SSL", return_value=smtp):
            marker = Mock()
            result = send_smtp(Mock(), MAIL_ENV, marker)
        marker.assert_called_once()
        smtp.close.assert_called_once()
        self.assertEqual(result["status"], "sent")

    def test_disconnect_during_data_is_uncertain(self):
        smtp = Mock()
        smtp.send_message.side_effect = smtplib.SMTPServerDisconnected("test")
        with patch("miki_jobsearch.service.smtplib.SMTP_SSL", return_value=smtp):
            with self.assertRaises(DeliveryFailure) as caught:
                send_smtp(Mock(), MAIL_ENV, Mock())
        self.assertTrue(caught.exception.ambiguous)

    def test_auth_failure_is_safe_to_retry(self):
        smtp = Mock()
        smtp.login.side_effect = smtplib.SMTPAuthenticationError(535, b"test")
        marker = Mock()
        with patch("miki_jobsearch.service.smtplib.SMTP_SSL", return_value=smtp):
            with self.assertRaises(DeliveryFailure) as caught:
                send_smtp(Mock(), MAIL_ENV, marker)
        self.assertFalse(caught.exception.ambiguous)
        marker.assert_not_called()


class ReportTests(unittest.TestCase):
    def test_google_app_password_display_spaces_are_removed(self):
        with patch.dict(os.environ, dict(MAIL_ENV, GMAIL_APP_PASSWORD="abcd efgh\u00a0ijkl mnop")):
            self.assertEqual(mail_settings()["GMAIL_APP_PASSWORD"], "abcdefghijklmnop")

    def test_rejected_jobs_cannot_appear_in_summary_or_application_action(self):
        report = copy.deepcopy(FIXTURE)
        report["summary"] = "4 passende Anzeigen gefunden"
        report["next_action"] = "Heute Rejected Company priorisieren"
        report["jobs"] = []
        report["rejected"] = [{"title": "Rejected Role", "url": "https://example.org/job",
                               "reason": "Supporting quotation not present in accessible detail-page text"}]
        _, body = render_report(report, MONDAY.date(), [])
        self.assertNotIn("4 passende Anzeigen", body)
        self.assertNotIn("Rejected Company", body)
        self.assertIn("0 neue, unabhängig verifizierte Treffer", body)
        self.assertIn("1 Kandidaten konnten nicht unabhängig", body)

    def test_deployment_check_records_names_without_credential_values(self):
        result = readiness("send", {"MIKI_OPENAI_API_KEY": "private-value", "GMAIL_USER": "private-address"})
        self.assertEqual(result["missing_requirements"], ["GMAIL_APP_PASSWORD", "MAIL_TO"])
        self.assertNotIn("private", json.dumps(result))
        self.assertEqual(readiness("live-dry-run", {"MIKI_OPENAI_API_KEY": "test"})["status"], "credentials_present")

    def test_friday_includes_daily_hits_weekly_totals_and_deadline(self):
        report = copy.deepcopy(FIXTURE)
        report["jobs"][0]["score"] = 83
        report["jobs"][0]["deadline"] = "2026-10-12"
        report["rejected"] = []
        previous = dict(report["jobs"][0], date="2026-10-05", score=None)
        subject, body = render_report(report, date(2026, 10, 9), [previous])
        self.assertIn("Wochenüberblick", subject)
        self.assertIn("2 gemeldete Treffer", body)
        self.assertIn("2026-10-12", body)
        self.assertIn("unbekannt (Althistorie)", body)
        self.assertIn("Bewerbungsstatus: nicht erfasst", body)

    def test_render_escapes_untrusted_html(self):
        report = copy.deepcopy(FIXTURE)
        report["summary"] = "<script>alert('x')</script>"
        report["jobs"][0]["score"] = 83
        report["rejected"] = []
        _, body = render_report(report, MONDAY.date(), [])
        self.assertNotIn("<script>", body)

    def test_import_preserves_unknown_scores(self):
        converted = convert_history("2026-09-22 | -- | Title | Employer | https://example.org/job")
        self.assertIsNone(converted[0]["score"])


if __name__ == "__main__":
    unittest.main()
