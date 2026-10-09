# Advance research on GitHub

All times use Europe/Berlin, including daylight-saving changes.

- Previous calendar day, 09:00: GitHub researches the next weekday report.
  Runs are Sunday–Thursday, including Sunday for Monday. A retry runs from 13:00.
- As soon as research and source verification finish: save the approved Electric
  Blue HTML, push the durable queue marker, and send the complete email to Brevo
  with `scheduledAt` for the following weekday at 08:30. Verify the queue by GET.
- 16:00: the GitHub backup checks that the report is actually queued for the correct
  time. An HTML report alone is insufficient. Missing/unknown readiness warns only
  the operator configured in `MAIL_ALERT_TO`.
- Next weekday, 08:30: Brevo releases the saved email from its own queue. No morning
  GitHub or laptop execution is required. Allow up to five minutes provider delay;
  inbox arrival is a separate step.
- 08:40: the GitHub backup polls send/delivery events for the exact message and all
  configured recipients. Unconfirmed acceptance triggers one operator warning per
  day, stored separately from the readiness warning. It never sends the jobs again.
- A separate Codex heartbeat can notify when the local app is available. This is
  not an always-on external monitor. Verify cloud/external monitoring separately.
- Manual recovery: choose `prepare-next` in `daily.yml` or the mobile button. It
  prepares and queues the next weekday; it does not send the job email immediately.

The former laptop 16:00 research, 17:00 check and 09:30 recovery are disabled.
The macOS launch agent was unloaded; its plist, logs and history are preserved.
The local coordinator also has no automatic work slots. No laptop credentials
are required for the GitHub process. The Codex notification requires the local
computer and app to be running; the GitHub warning email is independent of them.
GitHub schedules can be late or absent, so these times are targets rather than
an exact start or inbox-time guarantee.

## Shared state and duplicate protection

Each local execution uses a fresh temporary Git clone, leaving the development
checkout untouched. Both actors claim `state/coordinator.json` with an ordinary
Git push before work. Competing pushes fail closed. Each outbox mutation is pushed
before provider submission. A worker whose lease expired stops before sending.
Never force-push/rebase a failed delivery-state write to resolve a conflict.

`sent`, `sending`, `uncertain`, `partial`, `scheduling`, `scheduled`, and
`schedule_uncertain` block new automatic submissions.
Unknown acceptance still requires provider-log reconciliation. A failed claim or
network outage cannot silently authorize a second send. Reports and run history
remain in `state/runs/` and `state/history.json`.

Advance preparation uses the actual research date for source research and the
future delivery date for deadlines and email heading. The email labels the earlier
research date. Sources may change between preparation and sending.

## Mobile status and manual start

Private Site: https://miki-crawler-status.raphael-regli.chatgpt.site

The owner signs in with ChatGPT. The dashboard retrieves the sanitized operational
feed `state/status-feed.json`. This underlying feed is public because the repository
is public; it contains timestamps/status/counts only, no report text or addresses.
A failed fetch displays unknown status, never cached readiness as confirmed current.
The saved timestamp is shown. Only committed remote readiness counts as ready.

The manual button dispatches `daily.yml` with `mode=prepare-next`. It explicitly
prepares the next weekday, including Friday for Monday, and queues it at Brevo.
An explicit manual start re-researches an already prepared report. Its prior JSON/HTML
version is preserved in `state/preparation-history/`; failed refreshes restore the
previous saved report. Once the provider may have accepted a queue request, the
queue guard takes precedence and a refresh never restores the old unscheduled state.
Already queued reports require confirmed cancellation before a refresh. Shared leases and terminal delivery states still apply. A dispatch
acknowledgement is not research completion. Active workflows disable repeated
requests; the durable claim prevents concurrent workers from sending twice.

Configure a fine-grained GitHub token restricted to this repository with
**Actions: read/write** as the private Site runtime secret `GITHUB_ACTIONS_TOKEN`.
The backend alone uses it; it never appears in browser code, files, or the feed.
For a secure handoff to Codex, run `python3 -m scripts.configure_manual_start`.
This stores the value under `miki-jobsearch/GITHUB_ACTIONS_TOKEN` in macOS Keychain
using a secure interactive prompt. Tell Codex only that the entry is ready; never
send the token in chat. Codex can retrieve that dedicated entry without printing
it and configure the native Sites runtime secret.
After configuring a runtime secret, deploy the saved Site version again to apply
it. Without it, the direct button is disabled and the GitHub workflow link remains
available; choose `prepare-next` there. Owner authentication and same-origin checks
protect the POST endpoint.
