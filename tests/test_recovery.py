import copy
import json
import os
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

from miki_jobsearch.hybrid import execute
from miki_jobsearch.recovery import recover_day, retry_allowed, supervise, targets
from miki_jobsearch.research import ResearchError
from miki_jobsearch.service import StateSyncError, atomic_json, read_json, run

ROOT = Path(__file__).resolve().parent.parent
FIXTURE = json.loads((ROOT / "tests/fixtures/report.json").read_text())
DAY = "2026-10-19"


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / "state/runs" / f"{DAY}.json"
        self.now = datetime.fromisoformat("2026-10-19T07:00:00+02:00")

    def saved(self, status="prepared"):
        atomic_json(self.path, {"date": DAY, "status": status, "report": {"jobs": []}})
        self.path.with_suffix(".html").write_text("Saved live report")

    def test_actual_wall_clock_targets_delayed_events_and_weekends(self):
        cases = {
            "2026-10-19T05:59:00+02:00": [],
            "2026-10-19T06:00:00+02:00": ["2026-10-19"],
            "2026-10-19T23:00:00+02:00": ["2026-10-19", "2026-10-20"],
            "2026-10-16T09:00:00+02:00": ["2026-10-16", "2026-10-19"],
            "2026-10-17T10:00:00+02:00": ["2026-10-19"],
            "2026-10-18T22:00:00+02:00": ["2026-10-19"],
            "2026-10-26T05:00:00+00:00": ["2026-10-26"],
        }
        for clock, expected in cases.items():
            self.assertEqual(targets(datetime.fromisoformat(clock)), expected)

    def test_failed_preparation_is_researched_and_queued(self):
        atomic_json(self.path, {"date": DAY, "status": "research_failed"})
        phases = []
        def executor(root, actor, **kwargs):
            phases.append(kwargs["phase"])
            self.assertTrue(kwargs["recovery"])
            self.saved("prepared" if kwargs["phase"] == "research" else "scheduled")
            return {"status": "prepared" if kwargs["phase"] == "research" else "scheduled"}
        result = recover_day(self.root, DAY, clock=lambda: self.now, executor=executor)
        self.assertEqual(phases, ["research", "prepare"])
        self.assertEqual(result["status"], "scheduled")

    def test_research_crossing_deadline_sends_instead_of_scheduling_in_the_past(self):
        clocks = iter([self.now.replace(hour=8, minute=25), self.now.replace(hour=8, minute=32)])
        phases = []
        def executor(root, actor, **kwargs):
            phases.append(kwargs["phase"])
            self.saved()
            return {"status": "prepared" if kwargs["phase"] == "research" else "sent"}
        result = recover_day(self.root, DAY, clock=lambda: next(clocks), executor=executor)
        self.assertEqual(phases, ["research", "deliver"])
        self.assertEqual(result["status"], "sent")

    def test_saved_report_is_never_reresearched(self):
        self.saved("schedule_failed")
        runner = Mock(return_value={"status": "scheduled"})
        recover_day(self.root, DAY, clock=lambda: self.now, executor=runner)
        self.assertEqual(runner.call_args.kwargs["phase"], "prepare")
        runner.assert_called_once()

    def test_never_sends_before_0830_or_schedules_at_the_last_second(self):
        self.saved()
        runner = Mock()
        result = recover_day(self.root, DAY, clock=lambda: self.now.replace(hour=8, minute=29), executor=runner)
        self.assertEqual(result["status"], "awaiting_send_time")
        runner.assert_not_called()

    def test_ambiguous_or_accepted_messages_are_only_polled_never_reposted(self):
        for status in ("scheduled", "scheduling", "schedule_uncertain", "sending", "uncertain", "partial", "sent"):
            self.saved(status)
            runner = Mock()
            provider = Mock(side_effect=__import__("miki_jobsearch.scheduled_mail", fromlist=["ProviderStatusError"]).ProviderStatusError("down"))
            result = recover_day(self.root, DAY, clock=lambda: self.now, executor=runner, provider=provider)
            self.assertEqual(result["status"], "provider_unconfirmed")
            runner.assert_not_called()
            provider.assert_called_once()

    def test_successful_provider_reconciliation_is_quiet(self):
        self.saved("sent")
        r = read_json(self.path, {})
        r["delivered_at"] = self.now.isoformat()
        atomic_json(self.path, r)
        provider = Mock()
        self.assertEqual(recover_day(self.root, DAY, clock=lambda: self.now, provider=provider)["status"], "delivered")
        provider.assert_not_called()

    def test_configuration_and_research_failure_are_retryable_without_exposing_exception(self):
        secret = "private-credential-and-address"
        runner = Mock(side_effect=ResearchError(secret, code="api_connection_error"))
        result = recover_day(self.root, DAY, clock=lambda: self.now, executor=runner)
        self.assertTrue(result["retryable"])
        self.assertNotIn(secret, json.dumps(result))

    def test_shared_state_failure_stops_processing_immediately(self):
        recover = Mock(side_effect=StateSyncError("conflict"))
        with self.assertRaises(StateSyncError):
            supervise(self.root, clock=lambda: self.now.replace(hour=10), recover=recover)
        recover.assert_called_once()

    def test_attempt_budget_resets_for_morning_and_cooldown_survives_restart(self):
        attempts = [{"phase": "research", "started_at": (self.now - timedelta(minutes=i+1)).isoformat()} for i in range(4)]
        self.assertEqual(retry_allowed({"attempts": attempts}, "research", self.now)[1], "retry_budget_exhausted")
        previous = self.now - timedelta(days=1)
        for a in attempts:
            a["started_at"] = previous.isoformat()
        self.assertTrue(retry_allowed({"attempts": attempts}, "research", self.now)[0])
        attempts.append({"phase": "research", "started_at": (self.now - timedelta(seconds=30)).isoformat()})
        self.assertEqual(retry_allowed({"attempts": attempts}, "research", self.now)[1], "retry_cooldown")

    def test_budget_exhaustion_prevents_research(self):
        atomic_json(self.root / "state/recovery" / f"{DAY}.json", {"attempts": [
            {"phase": "research", "started_at": (self.now - timedelta(minutes=10)).isoformat()} for _ in range(4)]})
        runner = Mock()
        self.assertEqual(recover_day(self.root, DAY, clock=lambda: self.now, executor=runner)["status"], "retry_budget_exhausted")
        runner.assert_not_called()

    def test_exhausted_daytime_budget_cannot_consume_evening_or_morning_retries(self):
        day = self.now.replace(hour=10)
        ledger = {"attempts": [{"phase":"research", "started_at": (day - timedelta(minutes=i)).isoformat()}
                               for i in range(3)]}
        self.assertEqual(retry_allowed(ledger, "research", day + timedelta(hours=1))[1], "retry_budget_exhausted")
        self.assertTrue(retry_allowed(ledger, "research", day.replace(hour=16))[0])
        self.assertTrue(retry_allowed(ledger, "research", day.replace(hour=6) + timedelta(days=1))[0])

    def test_attempt_is_durable_before_research_and_failed_attempt_is_retained(self):
        atomic_json(self.root / "state/coordinator.json", {"lease": {"token": "token", "expires_at": "2099-01-01T00:00:00+00:00"}})
        saves = []
        def persist(record):
            saves.append(read_json(self.root / "state/recovery" / f"{DAY}.json", {}))
        def research(*args, **kwargs):
            self.assertEqual(saves[-1]["attempts"][-1]["status"], "running")
            raise ResearchError("unavailable", code="no_verified_candidates")
        with patch("miki_jobsearch.hybrid.claim", return_value="token"), patch("miki_jobsearch.hybrid.complete"), \
             patch("miki_jobsearch.hybrid.git_persister", return_value=persist):
            with self.assertRaises(ResearchError):
                execute(self.root, "github", now=self.now, phase="research", day=DAY, runner=research, recovery=True)
        self.assertEqual(read_json(self.root / "state/recovery" / f"{DAY}.json", {})["attempts"][-1]["status"], "failed")

    def test_same_day_early_research_is_allowed_only_as_preparation_and_never_sends(self):
        researcher = Mock(return_value=copy.deepcopy(FIXTURE))
        sender = Mock()
        with tempfile.TemporaryDirectory() as folder:
            result = run(ROOT, Path(folder), now=self.now, dry_run=False, prepare=True, delivery_date=DAY,
                         allow_early_prepare=True, researcher=researcher, verifier=lambda *a:(True,"verified"), sender=sender)
        self.assertEqual(result["status"], "prepared")
        sender.assert_not_called()

    def test_failed_shortlist_is_passed_to_new_research_without_weakening_verification(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Path(folder)
            atomic_json(store / "runs" / f"{DAY}.json", {"date": DAY, "status": "research_failed",
                        "candidate_diagnostics": [{"url": "https://failed.example/job"}]})
            researcher = Mock(return_value=copy.deepcopy(FIXTURE))
            run(ROOT, store, now=self.now.replace(hour=9), dry_run=False, prepare=True, researcher=researcher,
                verifier=lambda *a:(True,"verified"))
            config = researcher.call_args.args[1]
            self.assertEqual(config["previous_candidate_urls"], ["https://failed.example/job"])
            self.assertIn("Never weaken", config["recovery_instruction"])

    def test_real_git_transactions_release_lease_and_persist_finished_recovery(self):
        remote = self.root / "remote.git"
        checkout = self.root / "checkout"
        checkout.mkdir()
        def git(*args):
            return subprocess.run(["git", *args], cwd=checkout, capture_output=True, text=True, check=True).stdout.strip()
        subprocess.run(["git", "init", "--bare", str(remote)], capture_output=True, check=True)
        git("init", "-b", "main")
        git("config", "user.name", "test")
        git("config", "user.email", "test@example.org")
        (checkout / "initial").write_text("initial")
        git("add", ".")
        git("commit", "-m", "initial")
        git("remote", "add", "origin", str(remote))
        git("push", "-u", "origin", "main")
        def runner(root, store, **kwargs):
            record = {"date": DAY, "status": "prepared", "report": {"jobs": []}, "hits": 0}
            atomic_json(store / "runs" / f"{DAY}.json", record)
            kwargs["persist"](record)
            return {"status": "prepared"}
        with patch.dict(os.environ, {"STATE_BRANCH": "main"}):
            execute(checkout, "github", now=self.now, phase="research", day=DAY, runner=runner, recovery=True)
            from miki_jobsearch.hybrid import checkpoint
            checkpoint(checkout, "repeat checkpoint")  # Safe even when state is unchanged.
        self.assertNotIn("lease", read_json(checkout / "state/coordinator.json", {}))
        self.assertEqual(json.loads(git("show", "origin/main:state/recovery/" + DAY + ".json"))["status"], "prepared")
        self.assertEqual(json.loads(git("show", "origin/main:state/status-feed.json"))["lease"], None)


if __name__ == "__main__":
    unittest.main()
