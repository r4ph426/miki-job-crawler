# Advance research on GitHub

All times use Europe/Berlin, including daylight-saving changes.

- Previous calendar day, 09:00: GitHub prepares the next weekday report.
  Scheduled research runs Sunday–Thursday, including Sunday for Monday.
- 16:00: GitHub checks for a completed, saved live report. If missing, a warning
  email goes only to the operator configured in `MAIL_ALERT_TO`. A Codex heartbeat
  checks the same remote state and notifies this chat when action is needed.
- Manual recovery: choose `prepare-next` in `daily.yml` or use the configured
  mobile button. Preparation does not send the job email immediately.
- Next day, 08:30: GitHub sends the prepared report Monday–Friday. Missing reports
  wait for manual preparation; delivery retries never start research.

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

`sent`, `sending`, `uncertain`, and `partial` are terminal for automatic attempts.
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
prepares the next weekday, including Friday for Monday, without sending an email.
Shared leases and existing prepared/terminal records still apply. A dispatch
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
