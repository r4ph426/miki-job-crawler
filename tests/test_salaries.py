import copy
import json
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from miki_jobsearch.newsletter import render_report
from miki_jobsearch.salaries import estimate_salary, salary_missing, with_salary_estimates
from miki_jobsearch.service import DeliveryFailure, atomic_json, run

ROOT = Path(__file__).resolve().parent.parent
BENCHMARKS = json.loads((ROOT / "config/salary-benchmarks.json").read_text())
DAY = date(2026, 10, 6)
NOW = datetime.fromisoformat("2026-10-06T12:00:00+02:00")


class SalaryTests(unittest.TestCase):
    def job(self):
        return dict(title="Office Manager (all genders)", salary="Unbekannt", hours="Vollzeit")

    def test_estimates_for_todays_roles_use_the_matching_berlin_benchmark(self):
        cases = [
            ("(Jr.) Office Coordinator (m/f/d)", "Office Coordinator", 32000, 45000),
            ("Sachbearbeiter Einkauf in Voll-/Teilzeit (m/w/d)", "Sachbearbeiter/in Einkauf", 30000, 44000),
            ("Office Manager, 30h/week (all genders)", "Officemanager/in", 24000, 34000),
            ("Sales Coordinator / Vertriebsmitarbeiter (m/w/d)", "Sales Coordinator", 32000, 46000),
            ("Marketing Operations Coordinator (m/f/d)", "Marketing Coordinator", 36000, 52000),
            ("Marketing Coordinator (m/w/d) in Vollzeit", "Marketing Coordinator", 36000, 52000),
        ]
        for title, role, lower, upper in cases:
            with self.subTest(title=title):
                estimate = estimate_salary(dict(self.job(), title=title), DAY, BENCHMARKS)
                self.assertEqual(estimate["benchmark_role"], role)
                self.assertEqual((estimate["annual_min_eur"], estimate["annual_max_eur"]), (lower, upper))
                self.assertEqual(estimate["source_checked_on"], "2026-10-06")

    def test_missing_pay_is_recognized_without_overwriting_actual_pay_or_tariff(self):
        for salary in ["", "Unbekannt", "nicht genannt", "Gehalt nicht angegeben", "k.A.", "Not disclosed", "Nach Vereinbarung"]:
            with self.subTest(salary=salary):
                self.assertTrue(salary_missing(salary))
                self.assertIsNotNone(estimate_salary(dict(self.job(), salary=salary), DAY, BENCHMARKS))
        for salary in ["40.000–50.000 € brutto/Jahr", "3.500 EUR monatlich", "TVöD Bund E9b", "TV-L", "Nach Tarif", "ab 45k", "Gehalt unbekannt, TVöD E9b"]:
            with self.subTest(salary=salary):
                self.assertFalse(salary_missing(salary))
                self.assertIsNone(estimate_salary(dict(self.job(), salary=salary), DAY, BENCHMARKS))

    def test_explicit_weekly_hours_and_ranges_are_scaled_before_rounding(self):
        cases = [
            ("Teilzeit (30 Std/Woche, Mo–Fr 8–14 Uhr)", 24000, 34000, "30 Std./Woche"),
            ("30–40 Stunden", 24000, 46000, "30–40 Std./Woche"),
            ("Teilzeit, 32,5 Stunden pro Woche", 26000, 37000, "32,5 Std./Woche"),
            ("Full time, 40 hours/week", 33000, 46000, "40 Std./Woche"),
        ]
        for hours, lower, upper, label in cases:
            with self.subTest(hours=hours):
                estimate = estimate_salary(dict(self.job(), hours=hours), DAY, BENCHMARKS)
                self.assertEqual((estimate["annual_min_eur"], estimate["annual_max_eur"]), (lower, upper))
                self.assertIn(label, estimate["hours_basis"])

    def test_unspecified_or_ambiguous_hours_stay_a_labelled_full_time_equivalent(self):
        for hours in ["Teilzeit", "Voll- oder Teilzeit", "Unbekannt", "mindestens 30 Stunden", "20 oder 30 Stunden", "30 Stunden/Monat", "30 Stunden monatlich"]:
            with self.subTest(hours=hours):
                estimate = estimate_salary(dict(self.job(), hours=hours), DAY, BENCHMARKS)
                self.assertEqual((estimate["annual_min_eur"], estimate["annual_max_eur"]), (33000, 46000))
                self.assertIn("Vollzeitäquivalent", estimate["hours_basis"])
        job = dict(self.job(), title="Office Manager, 30h/week", hours="40 Stunden/Woche")
        self.assertIn("Vollzeitäquivalent", estimate_salary(job, DAY, BENCHMARKS)["hours_basis"])

    def test_unknown_specialist_leadership_and_ambiguous_roles_are_not_guessed(self):
        for title in ["Regulatory Affairs Specialist", "Senior Office Manager", "Head of Office Management",
                      "Front Office Manager", "Werkstudent Marketing Coordinator", "Office Manager / Project Coordinator"]:
            with self.subTest(title=title):
                self.assertIsNone(estimate_salary(dict(self.job(), title=title), DAY, BENCHMARKS))

    def test_stale_and_future_benchmarks_are_not_used(self):
        self.assertIsNone(estimate_salary(self.job(), DAY - timedelta(days=1), BENCHMARKS))
        self.assertIsNone(estimate_salary(self.job(), DAY + timedelta(days=181), BENCHMARKS))
        self.assertIsNone(estimate_salary(self.job(), DAY, {}))

    def report(self):
        report = json.loads((ROOT / "tests/fixtures/report.json").read_text())
        report["jobs"][0].update(title="Office Manager", hours="30 Std/Woche", score=83)
        report["rejected"] = []
        return report

    def test_email_marks_estimate_gross_yearly_hours_and_clickable_dated_source(self):
        report = self.report()
        enriched = with_salary_estimates(report, DAY, BENCHMARKS)
        self.assertNotIn("salary_estimate", report["jobs"][0])
        self.assertEqual(enriched["jobs"][0]["salary"], "nicht genannt")
        _, body = render_report(enriched, DAY, [])
        self.assertIn("Geschätzte Gehaltsspanne: ca. 24.000–34.000 € brutto/Jahr", body)
        self.assertIn("30 Std./Woche", body)
        self.assertIn("Stand 06.10.2026", body)
        self.assertIn('href="https://www.stepstone.de/gehalt/Officemanager-in/city/Berlin.html"', body)
        self.assertIn("Der Arbeitgeber nennt kein Gehalt", body)
        report["jobs"][0]["salary"] = "42.000–48.000 € brutto/Jahr"
        report["jobs"][0]["salary_estimate"] = enriched["jobs"][0]["salary_estimate"]
        _, body = render_report(report, DAY, [])
        self.assertIn("Gehalt: 42.000–48.000 € brutto/Jahr", body)
        self.assertNotIn("Geschätzte Gehaltsspanne", body)

    def test_derived_source_text_and_links_are_escaped(self):
        report = with_salary_estimates(self.report(), DAY, BENCHMARKS)
        for key in ["hours_basis", "benchmark_role", "source_name", "source_url"]:
            report["jobs"][0]["salary_estimate"][key] = '<img src="x" onerror="bad()">'
        _, body = render_report(report, DAY, [])
        self.assertNotIn('<img src="x"', body)
        self.assertNotIn('onerror="bad()"', body)

    def test_prepare_and_unsent_retry_persist_estimates_without_researching_again(self):
        raw = self.report()
        del raw["rejected"]
        del raw["jobs"][0]["score"]
        with TemporaryDirectory() as temp:
            store = Path(temp) / "state"
            researcher, sender = Mock(return_value=copy.deepcopy(raw)), Mock()
            options = dict(root=ROOT, store=store, now=NOW, dry_run=False, prepare=True,
                           researcher=researcher, verifier=lambda *a: (True, "test verified"), sender=sender)
            run(**options)
            path = store / "runs/2026-10-06.json"
            prepared = json.loads(path.read_text())
            self.assertIn("salary_estimate", prepared["report"]["jobs"][0])
            sender.assert_not_called()
            # Emulate an unsent report prepared by the previous service version.
            del prepared["report"]["jobs"][0]["salary_estimate"]
            atomic_json(path, prepared)
            env = {"EMAIL_PROVIDER": "gmail", "GMAIL_USER": "sender@example.org", "GMAIL_APP_PASSWORD": "test-only",
                   "MAIL_TO": "recipient@example.org", "MAIL_CC": ""}
            with patch.dict("os.environ", env):
                sender.side_effect = DeliveryFailure("test rejection before sending")
                with self.assertRaises(DeliveryFailure):
                    run(**dict(options, prepare=False))
            self.assertEqual(researcher.call_count, 1)
            self.assertIn("salary_estimate", json.loads(path.read_text())["report"]["jobs"][0])

    def test_sent_record_is_unchanged_and_never_resent_for_salary_enrichment(self):
        with TemporaryDirectory() as temp:
            store = Path(temp) / "state"
            path = store / "runs/2026-10-06.json"
            atomic_json(path, {"date": "2026-10-06", "status": "sent", "report": self.report()})
            original = path.read_bytes()
            sender, researcher = Mock(), Mock()
            result = run(ROOT, store, now=NOW, dry_run=False, researcher=researcher, sender=sender)
            self.assertEqual(result["delivery_status"], "sent")
            self.assertEqual(path.read_bytes(), original)
            sender.assert_not_called()
            researcher.assert_not_called()


if __name__ == "__main__":
    unittest.main()
