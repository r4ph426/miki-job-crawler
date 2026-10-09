# Operations

Run `python3 -m miki_jobsearch status` from the checkout to inspect JSON state.
GitHub workflow summaries show recent dates, statuses and hit counts. Do not treat a
green offline check, skipped job, or running process as evidence of live delivery.

| Stored status | Meaning and next action |
| --- | --- |
| `research_failed` | API/coverage/validation failed. No email or reported-job history update. Fix the cause; the next slot can retry. |
| `configuration_failed` | Required mail settings missing/invalid. Fix the named secure setting; any saved research is retained. |
| `prepared` | Saved report; not yet accepted by the provider. A retry reuses this report. |
| `scheduling` | Durable marker before the future Brevo POST. After interruption, inspect the queue before retrying. |
| `schedule_failed` | Explicitly rejected queue request. Fix the cause; automatic preparation retries reuse the report. |
| `schedule_uncertain` | Queue acceptance is unknown. No automatic send or refresh; inspect Brevo first. |
| `scheduled` | Brevo accepted a future email. `schedule_verified_at` plus `provider_status=queued` proves the expected queue entry. This is not a sent email. |
| `failed` | Provider explicitly rejected delivery, or preparation failed before submission. Fix credentials/service; retry reuses the report. |
| `sending` | Durable checkpoint before the API POST or SMTP DATA. After a crash, acceptance is unknown; reconcile. |
| `uncertain` | Submission outcome unknown; inspect provider logs. No automatic resend. |
| `partial` | SMTP accepted for some recipients but rejected others. No resend to all recipients. Investigate and complete delivery separately. |
| `sent` | Provider accepted the submission for all configured recipients; inbox receipt is not yet proven. Same-date production runs skip. |

Scheduled sends store a UUID `provider_batch_id` durably before the POST, then
retain either acknowledgement format (`messageId` or `messageIds`). They store
`scheduled_at` and `schedule_accepted_at` separately from `accepted_at`.
From 08:37, exact-message events for every configured To/CC recipient
are required to set `sent` and add jobs to delivered history. A processed queue alone
does not confirm send acceptance. `delivered_at` requires delivery events for everyone
and indicates receiving-server acceptance, not human reading or inbox placement.
Queued/unclear jobs remain reserved against repetition in later reports.

Warnings start at 08:40. Missing acknowledgement identifiers never authorize another
POST. A saved batch ID permits read-only queue lookup. After the scheduled time,
the service can recover missing message IDs only when it finds one unique sent
message for each recipient with the exact subject and saved HTML, followed by
matching provider events. Provider content rewriting can prevent this strict
match; that requires manual provider-log reconciliation, not an automatic retry.

Do not delete a scheduled run or refresh its body locally. First inspect Brevo's
queue using the saved message identifiers or `provider_batch_id`; cancellation requires a confirmed
204 response from `DELETE /v3/smtp/email/{identifier}`. Preserve that evidence in
the run record before an operator makes an unsent report retryable. The service
never automatically cancels or replaces a queued message.

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

## Fresh research and explicitly requested corrections

Discovery uses live web search. Candidate duties and qualifications are then
reassessed from independently downloaded full detail pages. Evidence quotes must
match those pages; inline HTML and typographic spacing are normalized. If every
plausible candidate fails independent verification, the run fails research instead
of sending a misleading zero-result email. Genuine empty searches and exhausted
previously reported results can still produce an honest zero-result report.

The assessment selects a passage ID from original text chunks supplied with each
full page. The application inserts that exact source text and rejects IDs belonging
to a different URL. This prevents the model from paraphrasing or joining excerpts.
The complete duties and qualifications remain available for the assessment, and a
second live-page verification still runs before a candidate can be sent.

The verifier accepts one matching pair of outer quotation marks added around a
verbatim excerpt. Its interior must still contain at least 40 characters and match
one contiguous source passage. Paraphrases and excerpts joined with ellipses fail.

When the user explicitly requests a corrected email after a confirmed delivery,
use a separate revision identifier and a reason. Never reset/delete the original
sent record. In **Miki weekday job search**, choose mode `prepare`, revision
`updated-list`, and the user's correction reason. This performs fresh research,
including a fresh check of earlier rejected URLs, and persists
`state/runs/YYYY-MM-DD--updated-list.json` plus HTML for review without sending.
Review titles, complete requirements, evidence, scores, commute and exclusions.
Then dispatch `send` with the same revision and reason to deliver the prepared
report without repeating research. Empty revised reports are refused.

The original daily record remains unchanged. Both the normal email and its
correction have durable checkpoints and independent duplicate guards. Repeated
dispatches with the same revision skip after acceptance. For an uncertain revised
delivery, reconcile with `--revision updated-list`; its jobs stay reserved until
the provider outcome is confirmed. A revision never authorizes an automatic resend
of the original email.

The list is checked as of its research date. Do not describe all included vacancies
as published that day when publication dates are unknown.

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

## GitHub runner acquisition failures

If a run has no steps and reports "The job was not acquired by Runner", research
and delivery never started. The manual workflow's `runner` input can select
`ubuntu-24.04-arm` to try the alternate Linux pool, or `macos-15` for another hosted
pool. Every production run validates the service before delivery; CI runs on ARM.
Scheduled runs use the pinned `ubuntu-24.04-arm` image, which passed the complete
service checks and a real corrected Brevo delivery. Retry only after the previous
run finishes, and retain the same
revision ID and delivery records.

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

## Salary estimates

Missing advertised pay can receive a separately labelled annual gross market
range, with a dated source and explicit hours basis. See [salary estimates](salary-estimates.md)
for matching, reference refreshes and limitations. This leaves prior delivery
records and all duplicate guards intact.

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
