# Miki job search

Weekday job research for Miki, with a German email report and a Friday overview.
The service searches public job sites through the OpenAI Responses web-search tool,
checks candidate detail pages independently, filters previously reported jobs, and
delivers through Brevo's transactional email API. Gmail SMTP remains an optional
backend. Research criteria are in `config/`.

The service has been rebuilt locally from `miki-jobsearch.zip`. The original archive
recorded 145 recommendations through 2 October 2026; these are imported into
`data/seed-history.json`. The archive's CV, home address, recipient addresses, and
old agent instructions are not copied into the repository.

**Deployment:** GitHub Actions runs the service on the weekday schedule or a manual
workflow dispatch. Real research and delivery require configured credentials.
Check `state/deployment.json` and dated run records for actual runner outcomes.
The Codex development environment does not provide an unattended scheduler.

## Development

Python 3.11+ on Linux/macOS; only the standard library is required. No dependency
installation, API key, or mail account is needed for the offline checks.

```sh
cd /workspace/miki-job-crawler
python3 -m unittest discover -s tests -v
python3 -m miki_jobsearch run --dry-run --fixture tests/fixtures/report.json
python3 -m miki_jobsearch status
```

The offline demo writes `out/YYYY-MM-DD/mail.html` and `report.json`. It uses clearly
marked test data, sends nothing, and does not update reported-job history.

With `MIKI_OPENAI_API_KEY` supplied securely and outbound access configured,
`python3 -m miki_jobsearch run --dry-run` performs live research after 10:00 Berlin
time on weekdays. `RESEARCH_MODEL` can override the configured `gpt-5` model.
The GitHub runner has completed live research with `gpt-5`; a different key/model
or environment needs its own live check.

## Running continuously

Use the [deployment guide](docs/deployment.md) to configure GitHub Actions,
credentials and recipients, validate one real delivery, and hand over from the old
service. Scheduled runs target 10:00 Europe/Berlin, Monday–Friday, including daylight
saving changes. GitHub may delay scheduled jobs; this is not a precise-time SLA.

`state/runs/YYYY-MM-DD.json` records research and delivery outcomes; matching HTML
and `state/history.json` persist with Git commits before and after email delivery.
These commits are essential to the next runner's duplicate guard. A successful
provider response means accepted for delivery, not proven inbox receipt. Brevo's
transactional logs show delivery and bounce events.

See [operations](docs/operations.md) for failures, reconciliation and the future
overview frontend. Run records already expose dates, hits, source checks, excluded
candidates, attempts, and delivery status; the frontend is the next development step.
