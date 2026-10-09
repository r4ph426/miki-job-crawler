"""Read-only recovery inspection; never submit, cancel or reset a mail."""
import argparse
import json
import re
import urllib.parse
from datetime import date
from pathlib import Path

from miki_jobsearch.scheduled_mail import ProviderStatusError, provider_get, queue_status, recipients
from miki_jobsearch.service import atomic_json, mail_settings, read_json


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("day", type=lambda s: date.fromisoformat(s).isoformat())
    args = parser.parse_args()
    root = Path(__file__).resolve().parent.parent
    record = read_json(root / "state/runs" / f"{args.day}.json", {})
    settings = mail_settings()
    result = {"date": args.day, "operation": "GET-only scheduled queue inspection", "matches": [], "checks": []}
    ids = set(record.get("provider_message_ids", []))
    if record.get("provider_message_id"):
        ids.add(record["provider_message_id"])
    if record.get("message_id"):
        ids.add(record["message_id"])
    for address in recipients(settings):
        query = urllib.parse.urlencode({"email": address, "limit": 100, "sort": "desc"})
        try:
            response = provider_get("smtp/emails?" + query, settings)
            emails = response.get("transactionalEmails", [])
            result["checks"].append({"operation": "recipient email list", "count": len(emails)})
            for email in emails:
                identifier = email.get("messageId", "")
                if (email.get("subject") == record.get("subject") and isinstance(identifier, str)
                        and re.fullmatch(r"<?[A-Za-z0-9_.+\-]{1,160}@[A-Za-z0-9.-]{1,90}>?", identifier)):
                    ids.add(identifier)
        except ProviderStatusError as error:
            result["checks"].append({"operation": "recipient email list", "error": str(error)})
    for identifier in sorted(ids):
        try:
            candidate = dict(record, provider_message_id="<" + identifier.strip("<>") + ">")
            state = queue_status(candidate, settings)
            result["matches"].append({"provider_message_id": candidate["provider_message_id"], "provider_status": state,
                                      "scheduled_at": record["scheduled_at"]})
        except ProviderStatusError as error:
            result["checks"].append({"operation": "queue lookup", "error": str(error)})
    atomic_json(root / "state" / f"schedule-inspection-{args.day}.json", result)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
