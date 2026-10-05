"""Render a GitHub job summary without printing provider responses or credentials."""
import json
import sys

data = json.load(open(sys.argv[1], encoding="utf-8"))
print("### Miki delivery state\n")
print(f"Schedule: Monday–Friday, {data['scheduled_hour']}:00 {data['timezone']}.\n")
print(f"Stored recommendations: {data['historical_jobs']}.\n")
print("| Date | Report | Delivery status | Hits |")
print("| --- | --- | --- | --- |")
for record in data["runs"][:10]:
    print(f"| {record['date']} | {record.get('revision', 'daily')} | {record['status']} | {record.get('hits', 0)} |")
if not data["runs"]:
    print("| — | — | No production runs recorded | — |")
print("\n`sent` means the email provider accepted the message, not that it arrived in the inbox. Brevo delivery/bounce details are in its transactional logs.")
