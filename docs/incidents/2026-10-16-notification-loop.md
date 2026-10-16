# Miki failure notification loop — 16 October 2026

Status: service paused at the user request. No automatic restart is authorized.

## Confirmed cause

The independent Cloudflare scheduler dispatched preparation.yml every five minutes.
The future report for 19 October remains provider_unconfirmed. The controller treats
that unresolved business state as exit code 1 on every check. GitHub then sends its
workflow-failure email according to the existing account notification preferences.
The scheduler sees an unhealthy report again and starts another run. Its only
suppression covers currently running workflows; it has no incident cooldown.
The workflow's retryable=false result does not prevent the next external dispatch.
Application alert deduplication does not cover GitHub's own failure notifications.

Evidence: runs 38884852288 (10:05 UTC) and 38885414612 (10:10 UTC) repeat the same
workflow_dispatch path. Run 38885414612 passed all 155 Python tests, then reported
2026-10-16 delivered and 2026-10-19 provider_unconfirmed, retryable=false, exiting 1.
The screenshot SHA 06c47e2 matches that run. This is repeated status checking, not
repeated newsletter sending. The assistant introduced this notification loop.

## Stop performed

- All eight repository workflows were disabled through GitHub Actions and read back
  as disabled_manually. There were no active workflows to cancel in the inventory.
- Cloudflare worker miki-delivery-scheduler was deployed with ENABLED=false and no
  cron triggers. Disabled version: 3629f3f5-2a74-484e-a2d5-b921d160caa0.
- The macOS launch agent de.miki.jobsearch was disabled and unloaded.
- The Codex automation miki-versand-um-10-uhr-pr-fen was already PAUSED.
- Scheduler configuration in Git now also records the paused state.

## Boundary

No further Brevo POST or DELETE was issued during this stop. The earlier Monday
request was accepted but its provider ID was lost. Both lookup and the controlled
cancellation attempt returned 404. Pausing our execution cannot prove that a
previously accepted provider-side email will not run. Its record and duplicate-send
guards remain unchanged. Do not resend or label this provider request cancelled.
No customer-support message was sent, as requested by the user.

## Required before any restart

Distinguish an execution failure from a known unresolved delivery incident. Persist
incident identity, notify once on a meaningful change, and back off further checks.
The external wakeup must respect retryability, incident cooldowns and the service
pause, rather than using unhealthy as unconditional permission to dispatch again.
Keep genuine execution errors visible. Validate the complete dispatch → workflow
result → account notification path, including repeated unchanged ambiguity, before
explicit user authorization to resume.
