import copy
import json
import unittest
from datetime import date
from html.parser import HTMLParser
from pathlib import Path

from miki_jobsearch.newsletter import render_report


class Tags(HTMLParser):
    def __init__(self):
        super().__init__()
        self.starts = []
        self.text = []

    def handle_starttag(self, tag, attrs):
        self.starts.append((tag, dict(attrs)))

    def handle_data(self, text):
        self.text.append(text)


class NewsletterTests(unittest.TestCase):
    def report(self):
        report = json.loads((Path(__file__).parent / "fixtures/report.json").read_text())
        report["rejected"] = []
        report["jobs"][0]["score"] = 83
        return report

    def test_source_fields_cannot_inject_markup_in_job_rows(self):
        report = self.report()
        for key in ["title", "employer", "district", "hours", "contract", "salary",
                    "commute", "pro", "con", "effort_details", "evidence"]:
            report["jobs"][0][key] = '<img src="bad" onerror="alert(1)">'
        _, body = render_report(report, date(2026, 10, 6), [])
        parsed = Tags()
        parsed.feed(body)
        self.assertFalse(any("onerror" in attrs for _, attrs in parsed.starts))
        self.assertIn('<img src="bad" onerror="alert(1)">', "".join(parsed.text))

    def test_each_job_keeps_content_and_the_original_application_link(self):
        report = self.report()
        second = copy.deepcopy(report["jobs"][0])
        second.update(title="Zweite Stelle", url="https://example.org/jobs/second")
        report["jobs"].append(second)
        _, body = render_report(report, date(2026, 10, 6), [])
        parsed = Tags()
        parsed.feed(body)
        text = "".join(parsed.text)
        links = [attrs["href"] for tag, attrs in parsed.starts if tag == "a"]
        self.assertEqual(links, [job["url"] for job in report["jobs"]])
        for number, job in enumerate(report["jobs"], 1):
            self.assertIn(f"{number}. {job['title']}", text)
            for key in ["employer", "pro", "con", "effort_details", "evidence"]:
                self.assertIn(job[key], text)
        self.assertNotIn("<script", body)
        self.assertNotIn("<svg", body)


if __name__ == "__main__":
    unittest.main()
