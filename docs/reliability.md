# Reliable delivery and recovery

The service's desired state is one verified live report per weekday, queued at
Brevo in advance for **08:30 Europe/Berlin**, then confirmed delivered to all
configured recipients. Provider acceptance, queue processing and recipient delivery
remain separate facts. Even Brevo documents up to five minutes of dispatch delay;
recipient inbox timing cannot be guaranteed.

## Reconcile state, not cron intent

`python3 -m miki_jobsearch.recovery` reconciles the actual current Berlin date and
the next weekday on every wakeup. A delayed GitHub event does not miss a narrow
preparation window or select a task from the original cron string.

- From 09:00: prepare the next weekday, including Friday/weekend preparation for
  Monday when it falls within Brevo's 72-hour scheduling limit.
- From 06:00 on weekdays: recover today's missing/failed research as well.
- Reuse a saved live report after a known scheduling/sending rejection. Do not
  spend another research call or change the content unnecessarily.
- Keep the saved report at 08:25–08:30 rather than scheduling in the last five
  minutes. From 08:30 send it immediately. Re-evaluate the clock after research so
  crossing the deadline does not attempt to schedule a message in the past.
- For `scheduled`, `scheduling`, `schedule_uncertain`, `sending`, `uncertain`,
  `partial` and `sent`, read provider evidence. Never reset a status to allow a
  second POST. A lost acknowledgement can be reconciled using the pre-persisted
  batch UUID or unique exact-content recipient records.
- Poll exact-message recipient events for immediate sends as well as scheduled
  sends. Record recipient counts, delivery and bounce/block problems, without
  publishing recipient addresses or provider response bodies.

## Bounded attempts survive restarts

Every automatic research/queue/send attempt is persisted in
`state/recovery/YYYY-MM-DD.json` **before** the action, under the existing shared
Git lease. Three attempts per phase in each Berlin time window (morning before 09:00,
daytime 09:00–16:00, evening from 16:00) and at least 60 seconds between starts
prevent runaway spend. Daytime failures cannot consume evening or morning retries. A single invocation makes up to three repair cycles,
with a 60-second pause. Failed candidate URLs are supplied to renewed research so
it explores a different shortlist without weakening source verification.

A failed Git claim/push stops execution before further actions. Unknown states and
fixture reports require manual review. Lease expiry or uncertain provider acceptance
never authorizes a duplicate email. Persistent provider ambiguity or a bounced
recipient can still require operator action; the service reports that truthfully.

## Wakeups

The GitHub preparation workflow wakes every hour. The morning recovery
workflow additionally wakes every 20 minutes over both Berlin DST offsets. Both
invoke the same recovery module and share the same non-cancelling concurrency group.
`SERVICE_ENABLED=false` pauses both scheduled and externally dispatched recovery.
Manual `daily.yml` operations remain explicit.

GitHub cron alone is not a reliable clock: it can delay/drop events. The independent
Cloudflare Worker in `scheduler/` invokes **only** `preparation.yml`, `task=recover`,
on `main`. It requires no research or mail credentials. It skips healthy reports
and already-running repair workflows. An unavailable/stale feed causes a wakeup;
an HTTP error is a failed invocation, not a successful health check.

The worker was activated in the existing Cloudflare account on 16 October 2026,
after explicit authorization to transfer the repository-scoped Actions credential.
A fresh installation still needs its own authentication and protected secret:

1. Authenticate Wrangler using the user's existing Cloudflare account.
2. For a fresh installation, first set `ENABLED=false` and deploy with
   `wrangler deploy --config scheduler/wrangler.toml`.
3. Put a fine-grained token for `r4ph426/miki-job-crawler`, Actions read/write only,
   into `GITHUB_ACTIONS_TOKEN` with `wrangler secret put`. Use a protected prompt or
   transfer directly from Keychain; never print it or save it in a source file.
4. Keep `ENABLED=true` and redeploy, preserving the secret. The cron is every
   fifteen minutes (UTC); target-date decisions use Europe/Berlin in code.
5. Verify a **scheduled** worker invocation, the resulting GitHub dispatch, the
   report's verified Brevo queue and subsequently each recipient's delivery event.
   A local unit test, accepted deployment or manual dispatch does not prove that
   the independent clock has run unattended.

No public HTTP endpoint can trigger a send; the worker's fetch handler always
returns 404. Disable the Worker with `ENABLED=false`, or pause recovery using the
GitHub variable above. GitHub remains the execution/state backend, so this removes
dependence on GitHub cron, not all GitHub outages. A full GitHub outage requires a
separate executor/state backend; it must not be improvised as a second sender.

## Notifications and validation

Weekday 08:30 delivery was re-authorized on 20 October 2026. The scheduled
workflows use ubuntu-24.04. The laptop agent remains disabled; cloud wakeups do
not depend on an awake Mac.

An assessed delivery incident is operational data, not a crashed workflow.
Recovery emits needs_attention=true for unresolved business states and retains
the incident in the run/feed; deduplicated readiness/delivery alerts remain
separate. Genuine execution and Git-persistence exceptions still fail the job.
Unchanged provider ambiguity is checked hourly before the send time, at the
send time, then every 15 minutes. It never permits another POST.
The independent scheduler enforces at least 15 minutes between dispatches and
backs off for an hour after infrastructure failures, even with an unavailable
status feed. Active repairs suppress new dispatches. Thus a persistent unresolved
provider acknowledgement cannot recreate the five-minute GitHub failure-mail loop.

Repair is attempted before notifying. Warn once after 22:00 if the next report is
still not ready, or from 08:45 if today's recipient delivery is still unconfirmed.
Warnings go only to `MAIL_ALERT_TO`; successful routine wakeups stay quiet. The
public feed includes recovery counts and delivery-problem flags, not secrets.

Run `python3 -m unittest discover -s tests -v` and
`node --test scheduler/worker.test.mjs`. Regression scenarios include failed
preparation followed by recovery, deadline crossing, DST/weekends, retry budgets,
crash checkpoints, ambiguous POSTs, bounce events and partial recipient delivery.

Sources: [GitHub schedule semantics](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule),
[Brevo scheduling](https://developers.brevo.com/docs/schedule-batch-sendings),
[Brevo email API](https://developers.brevo.com/reference/send-transac-email),
[Cloudflare Cron Triggers](https://developers.cloudflare.com/workers/configuration/cron-triggers/).
