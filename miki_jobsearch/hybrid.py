"""Laptop-first research with a Git compare-and-swap lease and durable outbox."""
import argparse
import json
import os
import subprocess
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .status_feed import write_feed

from .service import (BERLIN, TERMINAL, StateSyncError, atomic_json, git_persister,
                      read_json, run)

LEASE_MINUTES = 40


def plan(now, actor):
    local = now.astimezone(BERLIN)
    minute = local.hour * 60 + local.minute
    tomorrow = local.date() + timedelta(days=1)
    if actor == "laptop" and minute >= 16 * 60 and tomorrow.weekday() < 5:
        return "prepare", tomorrow.isoformat()
    if local.weekday() >= 5:
        return None, local.date().isoformat()
    if actor == "laptop":
        if 9 * 60 + 30 <= minute < 10 * 60 + 10:
            return "fallback", local.date().isoformat()
        if 8 * 60 + 30 <= minute < 9 * 60 + 30:
            return "deliver", local.date().isoformat()
    elif minute >= 9 * 60 + 45:
        return "fallback", local.date().isoformat()
    elif minute >= 8 * 60 + 30:
        return "deliver", local.date().isoformat()
    return None, local.date().isoformat()


def readiness(root, day):
    record = read_json(root / "state/runs" / f"{day}.json", {})
    if record.get("status") in TERMINAL:
        return {"date": day, "status": record["status"], "ready": record["status"] == "sent"}
    ready = (record.get("status") == "prepared" and record.get("date") == day
             and bool(record.get("report")) and not record.get("fixture"))
    return {"date": day, "status": "ready" if ready else "not_ready", "ready": ready,
            "hits": record.get("hits", 0), "researched_at": record.get("researched_at"),
            "delivery_time": f"{day}T08:30:00 Europe/Berlin"}


def git(root, *args):
    result = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True,
                            timeout=90, env=dict(os.environ, GIT_TERMINAL_PROMPT="0"))
    if result.returncode:
        raise StateSyncError("Shared Git state unavailable; no delivery attempted")
    return result.stdout.strip()


def checkpoint(root, message):
    write_feed(root, root / "state")
    git(root, "add", "--", "state/coordinator.json", "state/status-feed.json", "state/laptop-health.json")
    git(root, "commit", "--only", "-m", message, "--", "state/coordinator.json", "state/status-feed.json", "state/laptop-health.json")
    # Non-fast-forward is a failed claim. Never rebase stale outbox state.
    git(root, "push", "origin", "HEAD:refs/heads/main")


def claim(root, now, actor, phase, day):
    path = root / "state/coordinator.json"
    health = root / "state/laptop-health.json"
    if not health.exists():
        atomic_json(health, {"status": "credentials_required"})
    if actor == "laptop":
        atomic_json(health, {"status": "available", "checked_at": now.isoformat()})
    shared = read_json(path, {})
    lease = shared.get("lease", {})
    if lease and datetime.fromisoformat(lease["expires_at"]) > now:
        return None
    token = uuid.uuid4().hex
    shared["lease"] = {"token": token, "actor": actor, "phase": phase, "date": day,
                       "started_at": now.isoformat(),
                       "expires_at": (now + timedelta(minutes=LEASE_MINUTES)).isoformat()}
    atomic_json(path, shared)
    checkpoint(root, f"Claim Miki {phase} {day} ({actor})")
    return token


def complete(root, token, day, result):
    path = root / "state/coordinator.json"
    shared = read_json(path, {})
    if shared.get("lease", {}).get("token") != token:
        raise StateSyncError("Shared lease ownership changed")
    shared.setdefault("days", {})[day] = dict(readiness(root, day),
        checked_at=datetime.now(timezone.utc).isoformat(), actor=shared["lease"]["actor"],
        last_result=result.get("status", "failed"))
    shared.pop("lease", None)
    atomic_json(path, shared)
    checkpoint(root, f"Confirm Miki readiness {day}")


def execute(root, actor, now=None, phase=None, day=None, runner=run):
    now = now or datetime.now(timezone.utc)
    planned, planned_day = plan(now, actor)
    phase, day = phase or planned, day or planned_day
    if not phase:
        return {"status": "skipped", "reason": "Outside hybrid schedule"}
    record = read_json(root / "state/runs" / f"{day}.json", {})
    if record.get("status") in TERMINAL:
        return {"status": "skipped", "delivery_status": record["status"], "date": day}
    if phase == "prepare" and readiness(root, day)["ready"]:
        return readiness(root, day)
    if phase == "deliver" and not record.get("report"):
        return {"status": "not_ready", "date": day, "reason": "Wait for local 09:30 retry"}
    token = claim(root, now, actor, phase, day)
    if token is None:
        return {"status": "deferred", "date": day, "reason": "Another worker holds the lease"}
    save = git_persister(root, root / "state")
    def persist(record):
        lease = read_json(root / "state/coordinator.json", {}).get("lease", {})
        if lease.get("token") != token or datetime.now(timezone.utc) >= datetime.fromisoformat(lease["expires_at"]):
            raise StateSyncError("Worker lease expired; no delivery attempted")
        save(record)
    try:
        result = runner(root, root / "state", now=now, dry_run=False, persist=persist,
                        prepare=phase == "prepare", delivery_date=day if phase == "prepare" else None,
                        require_prepared=phase == "deliver",
                        allow_early_prepare=actor == "manual")
    except StateSyncError:
        # The remote may have advanced or ownership expired. Leave the lease and
        # sending checkpoint for inspection; never merge and blindly retry.
        raise
    except Exception:
        complete(root, token, day, {"status": "failed"})
        raise
    complete(root, token, day, result)
    result["readiness"] = readiness(root, day)
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--actor", choices=["laptop", "github"], required=True)
    parser.add_argument("--manual-prepare", action="store_true", help="Explicitly prepare the next weekday without sending")
    args = parser.parse_args()
    root = Path(__file__).resolve().parent.parent
    os.environ["STATE_BRANCH"] = "main"
    if args.manual_prepare:
        now = datetime.now(timezone.utc)
        target = now.astimezone(BERLIN).date() + timedelta(days=1)
        while target.weekday() >= 5:
            target += timedelta(days=1)
        result = execute(root, "manual", now=now, phase="prepare", day=target.isoformat())
    else:
        result = execute(root, args.actor)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if result.get("delivery_status") in {"sending", "uncertain", "partial"}:
        return 1
    return 0


if __name__ == "__main__":
    import sys
    try:
        sys.exit(main())
    except Exception as error:
        print(f"{type(error).__name__}: Hybrid run failed; inspect shared state.", file=sys.stderr)
        sys.exit(1)
