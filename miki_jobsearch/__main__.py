import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

from .research import ResearchError
from .service import DeliveryFailure, MailConfigurationError, StateSyncError, git_persister, reconcile, run, status


def main():
    parser = argparse.ArgumentParser(description="Miki weekday job research and email service")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent.parent)
    parser.add_argument("--state-dir", type=Path)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("status")
    resolve = commands.add_parser("reconcile", help="Record a delivery decision after checking provider logs")
    resolve.add_argument("date", help="Berlin run date, YYYY-MM-DD")
    resolve.add_argument("--result", choices=["sent", "not-sent"], required=True)
    resolve.add_argument("--note", required=True, help="Evidence from provider logs; never include credentials")
    resolve.add_argument("--persist-git", action="store_true")
    resolve.add_argument("--revision", help="Revision identifier for a separate updated report")
    execute = commands.add_parser("run")
    delivery = execute.add_mutually_exclusive_group()
    delivery.add_argument("--send", action="store_true", help="Enable real delivery (default: dry run)")
    delivery.add_argument("--dry-run", action="store_true")
    delivery.add_argument("--prepare", action="store_true", help="Prepare and persist live research for review, without sending")
    execute.add_argument("--fixture", type=Path, help="Offline demo data; never compatible with --send")
    execute.add_argument("--output-dir", type=Path)
    execute.add_argument("--persist-git", action="store_true", help="Push outbox transitions; for the Actions runner")
    execute.add_argument("--now", help="ISO time for dry-run testing only")
    execute.add_argument("--revision", help="Explicitly authorized update of an already sent report; preserves the original")
    execute.add_argument("--revision-reason", help="Reason for the explicitly authorized updated report")
    args = parser.parse_args()
    root = args.root.resolve()
    store = (args.state_dir or root / "state").resolve()
    if args.command == "status":
        result = status(root, store)
    elif args.command == "reconcile":
        persist = git_persister(root, store) if args.persist_git else None
        result = reconcile(store, args.date, args.result, args.note, persist=persist, revision=args.revision)
    else:
        if (args.send or args.prepare) and (args.fixture or args.now):
            parser.error("--send/--prepare cannot use fixtures or override the clock")
        now = datetime.fromisoformat(args.now) if args.now else None
        fixture = json.loads(args.fixture.read_text(encoding="utf-8")) if args.fixture else None
        persist = git_persister(root, store) if args.persist_git else None
        result = run(root, store, now=now, dry_run=not (args.send or args.prepare), fixture=fixture,
                     output_dir=args.output_dir, persist=persist, prepare=args.prepare,
                     revision=args.revision, revision_reason=args.revision_reason)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if result.get("status") == "partial" or result.get("delivery_status") in {"sending", "uncertain", "partial"}:
        return 1
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as error:
        # Provider response bodies and credential values never belong in logs.
        if isinstance(error, ResearchError):
            detail = json.dumps(error.diagnostic)
        elif isinstance(error, (DeliveryFailure, MailConfigurationError, StateSyncError)):
            detail = str(error)
        else:
            detail = "Unexpected service failure; inspect the saved outbox and runner log."
        message = f"{type(error).__name__}: {detail}"
        print(message, file=sys.stderr)
        if os.environ.get("GITHUB_ACTIONS") == "true":
            escaped = message.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
            print(f"::error title=Miki service failed::{escaped}", file=sys.stderr)
        sys.exit(1)
