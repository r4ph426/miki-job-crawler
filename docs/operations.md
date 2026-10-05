# Operations

Run `python3 -m miki_jobsearch status` from the checkout to inspect JSON state.
GitHub workflow summaries show recent dates, statuses and hit counts. Do not treat a
green offline check, skipped job, or running process as evidence of live delivery.

| Stored status | Meaning and next action |
| --- | --- |
| `research_failed` | API/coverage/validation failed. No email or reported-job history update. Fix the cause; the next slot can retry. |
| `configuration_failed` | Required mail settings missing/invalid. Fix the named secure setting; any saved research is retained. |
| `prepared` | Saved report; not yet accepted by the provider. A retry reuses this report. |
| `failed` | Provider explicitly rejected delivery, or preparation failed before submission. Fix credentials/service; retry reuses the report. |
| `sending` | Durable checkpoint before the API POST or SMTP DATA. After a crash, acceptance is unknown; reconcile. |
| `uncertain` | Submission outcome unknown; inspect provider logs. No automatic resend. |
| `partial` | SMTP accepted for some recipients but rejected others. No resend to all recipients. Investigate and complete delivery separately. |
| `sent` | Provider accepted the submission for all configured recipients; inbox receipt is not yet proven. Same-date production runs skip. |

Research and detail-page fetches preserve TLS verification. In a managed cloud
environment the HTTP client uses `REQUESTS_CA_BUNDLE`/`SSL_CERT_FILE` when supplied.
Direct detail-page requests are limited to configured HTTPS source domains, with
bounded response sizes and timeouts. Only an accessible matching evidence quote
admits a candidate. JavaScript-only or blocked pages may therefore be omitted.
Configure an additional public source domain before accepting a newly found company
career URL; inspect the report's rejected candidates when coverage is unexpectedly low.

Source outages and failed candidate verification appear in the email. A complete
source outage is a failed search rather than a misleading zero-hit email. If a source
recovers, its seven-day window normally catches recent jobs; use a reviewed wider
sweep after a prolonged outage. Family counts and source checks are reported by the
research provider; live search-tool execution is checked independently, but these
counts are not a deterministic scraper audit.

## Reconcile uncertain delivery

First inspect Brevo's transactional logs for the date, subject, recipients and stored
`provider_message_id` (when an acknowledgement was received). The API also includes
the stable `message_id` in a custom `X-Miki-Message-ID` header. For Gmail, inspect its
Sent folder using `rfc822msgid:`. Absence from an inbox or a single search is insufficient
evidence that the provider did not accept the message. Confirm provider acceptance before
choosing a result. Keep the repository checked out at its latest state.

```sh
python3 -m miki_jobsearch reconcile YYYY-MM-DD --result sent --note "Confirmed acceptance in provider logs"
```

This records the operator decision, marks delivery accepted and adds its jobs to
reported history. When you have confirmed there was no acceptance, use
`--result not-sent`; this makes the saved report eligible for retry. A partially accepted
message cannot be marked entirely unsent. On the Actions state backend, set
`STATE_BRANCH` to the default branch and use `--persist-git` to push the decision;
otherwise review and push the changed `state/` files through the usual Git process.

The same stable Message-ID is retained when the report content is unchanged. Brevo
requests also carry a deterministic `Idempotency-Key`; the service's durable guard
does not rely on the provider retaining that key indefinitely. Neither backend can
provide an atomic delivery together with an external Git commit. If final state persistence
fails after acceptance, the remote `sending` checkpoint stops automatic redelivery,
but an operator must verify acceptance. Do not delete run records to make a job green.
Jobs from uncertain or interrupted deliveries are reserved on later days until
reconciliation, so a future run does not recommend the same jobs while acceptance
is unknown. They are counted separately from confirmed reported history.

## Brevo delivery failures

`401` usually means an invalid API key. For `400`, `403` or `422`, check sender
verification, account approval, and transactional email activation. `402`/`429`
require checking quota/billing/rate limits. Provider bodies are not copied into logs
or state because they may contain private data. Network interruptions, server errors,
redirects, duplicate-key conflicts and invalid success acknowledgements are treated
as uncertain acceptance and require reconciliation. An acknowledged send stores
`email_provider`, `provider_message_id` and `accepted_at`; check Brevo for subsequent
bounces or delivery events.

Run **Check Brevo API key** in GitHub Actions to diagnose authentication without
sending an email. The result in `state/brevo-key-check.json` distinguishes an SMTP
key, a key from another provider, an invalid API key, IP-access denial and disabled
transactional email. Account data, IP addresses and raw provider messages are discarded.
For IP-access denial, review Brevo's Authorized IP settings: GitHub-hosted runners
use changing outbound IP addresses. If a fixed IP is required, run the service on
a server with a stable address; changing one allowed IP will not make the hosted
weekday schedule reliable.

Gmail's Sent folder will not contain mail submitted through Brevo. A Brevo plugin
in a chat is not required: the unattended GitHub runner uses the API directly.

## Historical data and the frontend

The imported ledger is a record of recommendations, not a verified email log. One
historical recommendation has no score and is preserved as unknown. Detailed legacy
deadlines, rejections, research notes and application progress are not silently
converted into confirmed current state; the original ZIP remains the source for
those records. The original schedule and archived agent instructions are not active.

The next frontend can show the last run, next eligible weekday, accepted/failed/
uncertain delivery, source health, recommended jobs and run history. A manual-run or
pause control needs authentication and should use the same scheduler and delivery
guard. Do not expose credentials or the original CV through that frontend. Current
`status` output is local state, not proof that an external scheduler is activated.
