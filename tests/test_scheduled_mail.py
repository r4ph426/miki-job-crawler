import copy
import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from miki_jobsearch.hybrid import execute, readiness
from miki_jobsearch.scheduled_mail import (ProviderStatusError, queue_status, refresh_status,
                                         schedule_report, send_time)
from miki_jobsearch.service import (DeliveryFailure, StateSyncError, atomic_json,
                                   read_json, reserved_jobs, run, send_brevo)
from tests.test_brevo import acknowledgement

ROOT = Path(__file__).resolve().parent.parent
ENV = {"EMAIL_PROVIDER": "brevo", "BREVO_API_KEY": "test-only-key",
       "MAIL_FROM": "sender@example.org", "MAIL_TO": "miki@example.org", "MAIL_CC": "cc@example.org"}
PROVIDER_ID = "<scheduled-test@smtp-relay.mailin.fr>"
DAY = "2026-10-12"
NOW = datetime.fromisoformat("2026-10-11T09:05:00+02:00")


class ScheduledMailTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name); self.store = self.root / "state"
        self.path = self.store / "runs" / f"{DAY}.json"
        self.report = {"jobs": [{"url": "https://example.org/job", "title": "Job"}]}
        self.record = {"date": DAY, "status": "prepared", "report": self.report,
                       "subject": "Jobs", "message_id": "<local@miki-jobsearch>", "hits": 1, "attempts": []}
        atomic_json(self.path, self.record); self.path.with_suffix(".html").write_text("<p>Electric Blue</p>")
        env = patch.dict(os.environ, ENV, clear=True); env.start(); self.addCleanup(env.stop)
        self.sender = Mock(side_effect=self.send)
        self.getter = Mock(return_value={"status": "queued", "scheduledAt": "2026-10-12T06:30:00Z"})

    def send(self, message, settings, on_scheduling, scheduled_at):
        on_scheduling()
        self.assertEqual(read_json(self.path, {})["status"], "scheduling")
        self.assertEqual(scheduled_at, "2026-10-12T06:30:00Z")
        return {"status": "scheduled", "provider_message_id": PROVIDER_ID}

    def schedule(self, **kwargs):
        return schedule_report(self.root, self.store, DAY, now=NOW,
                               sender=self.sender, getter=self.getter, **kwargs)

    def test_summer_winter_times_and_weekend_rejection(self):
        self.assertEqual(send_time(DAY).astimezone(timezone.utc).hour, 6)
        self.assertEqual(send_time("2026-10-26").astimezone(timezone.utc).hour, 7)
        with self.assertRaises(ValueError): send_time("2026-10-10")

    def test_only_future_and_within_72_hours(self):
        for now in ("2026-10-09T08:29:00+02:00", "2026-10-12T08:30:00+02:00"):
            with self.assertRaises(ValueError):
                schedule_report(self.root, self.store, DAY, now=datetime.fromisoformat(now), sender=self.sender)
        self.sender.assert_not_called()

    def test_post_includes_scheduled_at_and_is_not_a_send_acknowledgement(self):
        opener = Mock(); opener.open.return_value = acknowledgement({"messageId": PROVIDER_ID})
        with patch("miki_jobsearch.service.urllib.request.build_opener", return_value=opener):
            result = schedule_report(self.root, self.store, DAY, now=NOW, getter=self.getter)
        self.assertEqual(result["status"], "scheduled")
        self.assertEqual(json.loads(opener.open.call_args.args[0].data)["scheduledAt"], "2026-10-12T06:30:00Z")
        record = read_json(self.path, {})
        self.assertNotIn("accepted_at", record)
        self.assertTrue(record["schedule_accepted_at"]); self.assertTrue(record["schedule_verified_at"])
        self.assertFalse((self.store / "history.json").exists())
        self.assertTrue(readiness(self.root, DAY)["ready"])
        self.assertEqual(len(reserved_jobs(self.store)), 1)
        for private in (ENV["BREVO_API_KEY"], ENV["MAIL_TO"], ENV["MAIL_CC"]):
            self.assertNotIn(private, self.path.read_text())

    def test_checkpoint_failure_prevents_post(self):
        opener = Mock()
        with patch("miki_jobsearch.service.urllib.request.build_opener", return_value=opener):
            with self.assertRaises(StateSyncError):
                schedule_report(self.root, self.store, DAY, now=NOW,
                                persist=Mock(side_effect=StateSyncError("remote down")))
        opener.open.assert_not_called()

    def test_scheduled_api_acknowledgement_can_return_message_ids_list(self):
        opener = Mock(); opener.open.return_value = acknowledgement({"messageIds": [PROVIDER_ID.strip("<>")]})
        with patch("miki_jobsearch.service.urllib.request.build_opener", return_value=opener):
            result = schedule_report(self.root, self.store, DAY, now=NOW, getter=self.getter)
        self.assertEqual(result["status"], "scheduled")
        self.assertEqual(read_json(self.path, {})["provider_message_ids"], [PROVIDER_ID.strip("<>")])
        self.assertIn("%3Cscheduled-test%40smtp-relay.mailin.fr%3E", self.getter.call_args.args[0])

    def test_restart_or_immediate_send_cannot_duplicate_queued_mail(self):
        self.schedule()
        self.assertEqual(self.schedule()["delivery_status"], "scheduled")
        result = run(ROOT, self.store, now=datetime.fromisoformat("2026-10-12T08:40:00+02:00"), dry_run=False)
        self.assertEqual(result["delivery_status"], "scheduled")
        self.sender.assert_called_once()

    def test_explicit_rejection_retries_saved_report_but_ambiguous_post_is_blocked(self):
        for ambiguous, expected in ((False, "schedule_failed"), (True, "schedule_uncertain")):
            atomic_json(self.path, self.record)
            def failed(message, settings, marker, **kwargs):
                marker(); raise DeliveryFailure("safe failure", ambiguous=ambiguous)
            with self.assertRaises(DeliveryFailure):
                schedule_report(self.root, self.store, DAY, now=NOW, sender=failed)
            self.assertEqual(read_json(self.path, {})["status"], expected)
            result = self.schedule()
            self.assertEqual(result["status"], "skipped" if ambiguous else "scheduled")

    def test_get_failure_after_acknowledgement_never_allows_second_post(self):
        self.getter.side_effect = ProviderStatusError("provider unavailable")
        with self.assertRaises(ProviderStatusError): self.schedule()
        self.assertEqual(read_json(self.path, {})["status"], "scheduled")
        self.assertFalse(readiness(self.root, DAY)["ready"])
        self.assertEqual(self.schedule()["delivery_status"], "scheduled")
        self.sender.assert_called_once()
        self.getter.side_effect = None
        refresh_status(self.root, self.store, DAY, now=NOW, getter=self.getter)
        self.assertTrue(readiness(self.root, DAY)["ready"])

    def test_queue_must_match_actual_send_time(self):
        for payload in ({"status": "queued", "scheduledAt": "2026-10-12T07:30:00Z"},
                        {"status": "queued", "scheduledAt": "2026-10-12T06:30:00"},
                        {"status": "unknown", "scheduledAt": "2026-10-12T06:30:00Z"}):
            with self.assertRaises(ProviderStatusError):
                queue_status(dict(self.record, provider_message_id=PROVIDER_ID,
                                  scheduled_at="2026-10-12T06:30:00Z"), ENV, Mock(return_value=payload))
        result = queue_status(dict(self.record, provider_message_id=PROVIDER_ID, scheduled_at="2026-10-12T06:30:00Z"),
                             ENV, Mock(return_value={"batches": [self.getter.return_value]}))
        self.assertEqual(result, "queued")

    def test_queue_is_not_delivery_and_all_recipients_need_exact_message_events(self):
        self.schedule()
        events = [{"email": "miki@example.org", "messageId": PROVIDER_ID, "event": "requests", "date": "2026-10-12T06:32:00Z"},
                  {"email": "cc@example.org", "messageId": "<wrong@example.org>", "event": "delivered", "date": "2026-10-12T06:33:00Z"}]
        getter = Mock(return_value={"events": events})
        now = datetime.fromisoformat("2026-10-12T08:40:00+02:00")
        result = refresh_status(self.root, self.store, DAY, now=now, getter=getter)
        self.assertEqual(result["status"], "scheduled")
        self.assertFalse((self.store / "history.json").exists())
        events[1]["messageId"] = PROVIDER_ID
        result = refresh_status(self.root, self.store, DAY, now=now, getter=getter)
        self.assertEqual(result["status"], "sent"); self.assertTrue(result["accepted_at"])
        self.assertNotIn("delivered_at", read_json(self.path, {}))
        events[0]["event"] = "delivered"
        refresh_status(self.root, self.store, DAY, now=now, getter=getter)
        self.assertTrue(read_json(self.path, {})["delivered_at"])
        self.assertEqual(len(read_json(self.store / "history.json", [])), 1)

    def test_changed_recipients_cannot_confirm_old_message(self):
        self.schedule(); getter = Mock()
        with patch.dict(os.environ, {"MAIL_TO": "changed@example.org"}):
            with self.assertRaises(ProviderStatusError):
                refresh_status(self.root, self.store, DAY, now=NOW, getter=getter)
        getter.assert_not_called()

    def test_refresh_never_restores_report_over_an_uncertain_queue_marker(self):
        atomic_json(self.root / "state/coordinator.json", {"lease": {"token": "token",
                    "expires_at": "2099-01-01T00:00:00+00:00"}})
        def uncertain(*args, **kwargs):
            atomic_json(self.path, dict(self.record, status="schedule_uncertain"))
            raise DeliveryFailure("uncertain", ambiguous=True)
        with patch("miki_jobsearch.hybrid.claim", return_value="token"), patch("miki_jobsearch.hybrid.complete"), \
             patch("miki_jobsearch.hybrid.git_persister", return_value=Mock()):
            with self.assertRaises(DeliveryFailure):
                execute(self.root, "manual", phase="prepare", day=DAY, now=NOW,
                        refresh=True, runner=Mock(), scheduler=uncertain)
        self.assertEqual(read_json(self.path, {})["status"], "schedule_uncertain")


if __name__ == "__main__":
    unittest.main()
