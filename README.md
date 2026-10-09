# Miki job search

Weekday job research for Miki, with a German email report and a Friday overview.
The service searches public job sites through the OpenAI Responses web-search tool,
reassesses candidates from downloaded detail pages, checks exact source evidence,
filters previously reported jobs, and
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
`python3 -m miki_jobsearch run --dry-run` performs live research from 08:30 Berlin
time on weekdays. `RESEARCH_MODEL` can override the configured `gpt-5` model.
The GitHub runner has completed live research with `gpt-5`; a different key/model
or environment needs its own live check.

## Running continuously

Use the [deployment guide](docs/deployment.md) to configure GitHub Actions,
credentials and recipients, validate one real delivery, and hand over from the old
service. The [advance research operating guide](docs/laptop-first.md) describes GitHub
research at 09:00 on the previous calendar day (Sunday–Thursday), retries from 13:00,
and immediate scheduling of the completed email at Brevo. The 16:00 readiness check
requires a verified Brevo queue entry for the next weekday at 08:30 Europe/Berlin.
Brevo sends without a morning GitHub trigger; allow its documented five-minute
dispatch delay. The 08:40 GitHub watchdog polls actual recipient events and warns
the operator when send acceptance is unconfirmed. GitHub checks remain best-effort.

`state/runs/YYYY-MM-DD.json` records research and delivery outcomes; matching HTML
and `state/history.json` persist with Git commits before and after email delivery.
These commits are essential to the next runner's duplicate guard. A successful
scheduled provider response means queued, not sent. Actual send events produce
`sent` and `accepted_at`; inbox receipt remains separate. Brevo's
transactional logs show delivery and bounce events.

An explicitly requested correction can prepare a fresh crawl under a separate
revision, review its scored shortlist, then send it while preserving the original
email and its duplicate guard. See the correction workflow in the operations guide.

See [operations](docs/operations.md) for failures, reconciliation and the future
overview frontend. Run records already expose dates, hits, source checks, excluded
candidates, attempts, and delivery status; the frontend is the next development step.
