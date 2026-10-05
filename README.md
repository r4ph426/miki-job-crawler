# Miki job search

Weekday job research for Miki, with a German email report and a Friday overview.
The service searches public job sites through the OpenAI Responses web-search tool,
checks candidate detail pages independently, filters previously reported jobs, and
delivers through Gmail SMTP. Research criteria are in `config/`.

The service has been rebuilt locally from `miki-jobsearch.zip`. The original archive
recorded 145 recommendations through 2 October 2026; these are imported into
`data/seed-history.json`. The archive's CV, home address, recipient addresses, and
old agent instructions are not copied into the repository.

**Deployment status:** local implementation and offline validation only. No live
research or email has been verified, and the production schedule is not activated.
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
Provider/model compatibility still needs a live dry run during deployment.

## Running continuously

Use the [deployment guide](docs/deployment.md) to activate GitHub Actions, configure
credentials and recipients, validate one real delivery, and hand over from the old
service. Scheduled runs target 10:00 Europe/Berlin, Monday–Friday, including daylight
saving changes. GitHub may delay scheduled jobs; this is not a precise-time SLA.

`state/runs/YYYY-MM-DD.json` records research and delivery outcomes; matching HTML
and `state/history.json` persist with Git commits before and after SMTP delivery.
These commits are essential to the next runner's duplicate guard. A successful
SMTP response means accepted by Gmail, not proven inbox delivery.

See [operations](docs/operations.md) for failures, reconciliation and the future
overview frontend. Run records already expose dates, hits, source checks, excluded
candidates, attempts, and delivery status; the frontend is the next development step.
