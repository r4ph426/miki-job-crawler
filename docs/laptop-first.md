# Laptop-first operation

All times use Europe/Berlin. The service still sends Monday–Friday.

- Previous calendar day, 16:00: the logged-in Mac prepares the next day's report.
  This includes Sunday for Monday. Friday/Saturday have no advance preparation.
- After upload: the Mac fetches remote state again and confirms readiness locally.
- From 17:00: one independent remote readback confirms the evening report.
- 08:30: the Mac and GitHub may send a prepared report. No cloud research occurs
  at this stage if preparation was missed.
- 09:30: a missing report is researched and sent on the Mac.
- From 09:45: GitHub researches/sends if the Mac has not claimed a live attempt.
  An active 40-minute lease defers cloud work; subsequent slots retry.

GitHub schedules can be late or absent. The laptop must be awake, online, and logged
in. The 60-second launchd tick catches eligible missed slots after wake; it cannot
wake a powered-off laptop. Local research has a 35-minute process timeout.
GitHub retains its 40-minute job timeout. No precise inbox-time guarantee is made.

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

## Mac setup

Use Python 3.11+ (the desktop app's bundled Python is currently usable). Run from
the repository with the selected interpreter:

```sh
python3 -m scripts.configure_laptop_keychain
python3 -m scripts.laptop_scheduler --check-credentials
python3 -m scripts.install_laptop_scheduler --install
python3 -m scripts.laptop_scheduler --status
```

The interactive `security` tool asks for each value without putting it in files,
command arguments, or chat. Dedicated Keychain services are
`miki-jobsearch/MIKI_OPENAI_API_KEY`, `miki-jobsearch/BREVO_API_KEY`,
`miki-jobsearch/MAIL_FROM`, `miki-jobsearch/MAIL_TO`, and optional
`miki-jobsearch/MAIL_CC`. Git pushes use the existing macOS Git credential helper.
Verify repository write access independently; a public read alone is insufficient.

The installer writes `~/Library/LaunchAgents/de.miki.jobsearch.plist` and loads the
per-user agent. Logs and credential-free local status live in `out/laptop-scheduler/`.
The plist contains no secrets. The laptop does not execute untrusted pull requests
or act as a GitHub self-hosted runner.

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
