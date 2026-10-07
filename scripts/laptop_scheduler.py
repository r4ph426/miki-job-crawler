"""macOS scheduler. Secrets stay in Keychain; production runs use fresh clones."""
import argparse
import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from miki_jobsearch.hybrid import git, plan, readiness
from miki_jobsearch.service import BERLIN, atomic_json, read_json, run_lock

ROOT = Path(__file__).resolve().parent.parent
REPOSITORY = "https://github.com/r4ph426/miki-job-crawler.git"
SERVICE_PREFIX = "miki-jobsearch/"
SECRET_NAMES = ("MIKI_OPENAI_API_KEY", "BREVO_API_KEY", "MAIL_FROM", "MAIL_TO", "MAIL_CC")


def credentials():
    values = {}
    for name in SECRET_NAMES:
        result = subprocess.run(["/usr/bin/security", "find-generic-password", "-s", SERVICE_PREFIX + name,
                                 "-w"], capture_output=True, text=True)
        if result.returncode == 0 and result.stdout.strip():
            values[name] = result.stdout.strip()
    return values


def notify(message):
    # Fixed script, argument data; no shell interpolation or credentials.
    subprocess.run(["/usr/bin/osascript", "-e",
        'on run argv\n display notification (item 1 of argv) with title "Miki Crawler"\nend run',
        message], capture_output=True)


def tick():
    now = datetime.now(timezone.utc)
    phase, day = plan(now, "laptop")
    if sys.version_info < (3, 11):
        raise RuntimeError("Laptop scheduler requires Python 3.11+")
    if not phase:
        return {"status": "idle"}
    cache = ROOT / "out/laptop-scheduler"
    with run_lock(cache):
        state = read_json(cache / "status.json", {})
        key = f"{phase}:{day}"
        previous = state.get("attempts", {}).get(key, {})
        if previous.get("success") and (phase != "prepare" or now.astimezone(BERLIN).hour < 17
                                         or state.get("confirmed_day") == day):
            return {"status": "already_checked", "date": day}
        if previous.get("at") and (now - datetime.fromisoformat(previous["at"])).total_seconds() < 900:
            return {"status": "backoff", "date": day}
        secrets = credentials()
        missing = [k for k in SECRET_NAMES[:-1] if not secrets.get(k)]
        if missing:
            result = {"status": "credentials_missing", "missing": missing, "date": day}
        else:
            with tempfile.TemporaryDirectory(prefix="miki-laptop-") as folder:
                checkout = Path(folder) / "checkout"
                git(Path(folder), "clone", "--depth", "1", REPOSITORY, str(checkout))
                git(checkout, "config", "user.name", "miki-laptop-bot")
                git(checkout, "config", "user.email", "miki-laptop-bot@users.noreply.github.com")
                if phase == "prepare" and readiness(checkout, day)["ready"]:
                    result = readiness(checkout, day)
                    # This independent readback verifies committed remote readiness.
                    if now.astimezone(BERLIN).hour >= 17 and state.get("confirmed_day") != day:
                        notify(f"Kontrollprüfung: Bericht für {day} ist bereit für 08:30 Uhr.")
                        state["confirmed_day"] = day
                else:
                    env = dict(os.environ, **secrets, STATE_BRANCH="main", EMAIL_PROVIDER="brevo",
                               GIT_TERMINAL_PROMPT="0")
                    try:
                        if phase == "prepare":
                            notify(f"Recherche für {day} gestartet.")
                        process = subprocess.run([sys.executable, "-m", "miki_jobsearch.hybrid", "--actor", "laptop"],
                                cwd=checkout, env=env, capture_output=True, text=True, timeout=35 * 60)
                        result = json.loads(process.stdout) if process.returncode == 0 else {"status": "failed", "date": day}
                    except subprocess.TimeoutExpired:
                        result = {"status": "timed_out", "date": day}
                    # Verify remote state from a fresh fetch, independently of the
                    # child process's claim that its push succeeded.
                    git(checkout, "fetch", "origin", "main")
                    git(checkout, "reset", "--hard", "origin/main")
                    result["remote_readiness"] = readiness(checkout, day)
                    if phase == "prepare" and result["remote_readiness"]["ready"]:
                        notify(f"Bericht für {day} hochgeladen und bestätigt: bereit für 08:30 Uhr.")
        previous_status = previous.get("status")
        successful = result["status"] in {"prepared", "ready", "sent", "skipped"}
        state.setdefault("attempts", {})[key] = {"at": now.isoformat(), "status": result["status"], "success": successful}
        state["last_result"] = result
        atomic_json(cache / "status.json", state)
        if result["status"] in {"credentials_missing", "failed", "timed_out"} and previous_status != result["status"]:
            notify("Lokale Recherche nicht bereit. Status prüfen; GitHub-Fallback bleibt vorgesehen.")
        return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check-credentials", action="store_true")
    parser.add_argument("--status", action="store_true")
    args = parser.parse_args()
    if args.check_credentials:
        values = credentials()
        result = {"configured": {k: bool(values.get(k)) for k in SECRET_NAMES}}
    elif args.status:
        result = read_json(ROOT / "out/laptop-scheduler/status.json", {"status": "not_started"})
    else:
        result = tick()
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"{type(error).__name__}: Laptop scheduler failed; no blind resend.", file=sys.stderr)
        sys.exit(1)
