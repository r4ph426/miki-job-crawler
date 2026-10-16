"""Explicit operator repair: a confirmed provider cancellation must precede replacement."""
import argparse
import copy
import json
import os
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from .hybrid import claim, complete
from .research import tls_context
from .scheduled_mail import (ProviderStatusError, recipient_identity, schedule_report,
                             send_time)
from .service import (DeliveryFailure, MailConfigurationError, NoMailRedirect, StateSyncError, atomic_json, git_persister,
                      mail_settings, read_json)


def cancel(identifier, settings):
    request = urllib.request.Request(
        "https://api.brevo.com/v3/smtp/email/" + urllib.parse.quote(identifier, safe=""),
        headers={"api-key": settings["BREVO_API_KEY"], "Accept": "application/json"},
        method="DELETE")
    opener = urllib.request.build_opener(NoMailRedirect(),
        urllib.request.HTTPSHandler(context=tls_context()))
    try:
        with opener.open(request, timeout=30) as response:
            if response.status != 204:
                raise ProviderStatusError("Cancellation unconfirmed; replacement blocked")
    except urllib.error.HTTPError as error:
        code = error.code
        error.close()
        raise ProviderStatusError(f"Cancellation unconfirmed (HTTP {code}); replacement blocked") from None
    except (OSError, urllib.error.URLError):
        raise ProviderStatusError("Cancellation response unavailable; replacement blocked") from None
    return 204


def repair(root, day, expected_batch, now=None, persist=None, canceller=cancel,
           scheduler=schedule_report):
    now = now or datetime.now(timezone.utc)
    day = date.fromisoformat(day).isoformat()
    if uuid.UUID(expected_batch).version != 4:
        raise ValueError("Expected batch must be UUIDv4")
    if not timedelta(minutes=5) < send_time(day) - now <= timedelta(hours=72):
        raise ValueError("Repair requires more than five minutes before a send within 72 hours")
    path = root / "state/runs" / f"{day}.json"
    record = read_json(path, {})
    settings = mail_settings()
    if (record.get("date") != day or record.get("provider_batch_id") != expected_batch
            or record.get("status") not in {"scheduled", "schedule_uncertain"}
            or record.get("accepted_at") or record.get("delivered_at")
            or record.get("fixture") or not record.get("report")
            or record.get("sent_recipient_count", 0) or record.get("delivered_recipient_count", 0)
            or not path.with_suffix(".html").exists()
            or record.get("recipient_set_id") != recipient_identity(settings)
            or settings["EMAIL_PROVIDER"] != "brevo"):
        raise ValueError("Existing unsent schedule and exact recipient identity must match")
    if record.get("schedule_repair"):
        raise ValueError("A repair was already attempted; inspect its receipt before any further action")
    persist = persist or git_persister(root, root / "state")
    original = copy.deepcopy(record)
    record["schedule_repair"] = {"status": "cancellation_requested", "batch_id": expected_batch,
                                 "requested_at": now.isoformat()}
    atomic_json(path, record)
    persist(record)  # No DELETE if the durable intent cannot be saved.
    try:
        code = canceller(expected_batch, settings)
        if code != 204:
            raise ProviderStatusError("Cancellation unconfirmed; replacement blocked")
    except ProviderStatusError as error:
        record["schedule_repair"].update(status="cancellation_unconfirmed", error=str(error))
        atomic_json(path, record)
        persist(record)
        raise
    receipt = dict(record["schedule_repair"], status="cancelled", http_status=204,
                   confirmed_at=datetime.now(timezone.utc).isoformat())
    record["schedule_repair"] = receipt
    atomic_json(path, record)
    persist(record)  # A lost receipt must never enable a replacement POST.
    archive = root / "state/schedule-history" / f"{day}--{expected_batch}"
    original["schedule_repair"] = receipt
    atomic_json(archive.with_suffix(".json"), original)
    archive.with_suffix(".html").write_bytes(path.with_suffix(".html").read_bytes())
    record.update(status="prepared", provider_batch_id=str(uuid.uuid4()))
    for key in ("provider_message_id", "provider_message_ids", "provider_status", "error",
                "provider_check_error", "provider_diagnostic", "schedule_accepted_at",
                "schedule_verified_at", "provider_checked_at"):
        record.pop(key, None)
    atomic_json(path, record)
    persist(record)  # Archive, cancellation and new identity precede scheduling.
    return scheduler(root, root / "state", day, now=now, persist=persist)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("day")
    parser.add_argument("expected_batch")
    args = parser.parse_args()
    root = Path(__file__).resolve().parent.parent
    os.environ["STATE_BRANCH"] = "main"
    now = datetime.now(timezone.utc)
    token = claim(root, now, "manual", "repair-schedule", args.day)
    if token is None:
        raise StateSyncError("Another worker is active; no cancellation attempted")
    save = git_persister(root, root / "state")
    def persist(record):
        lease = read_json(root / "state/coordinator.json", {}).get("lease", {})
        if lease.get("token") != token or datetime.now(timezone.utc) >= datetime.fromisoformat(lease["expires_at"]):
            raise StateSyncError("Repair lease expired; no further provider mutation permitted")
        save(record)
    try:
        result = repair(root, args.day, args.expected_batch, now=now, persist=persist)
    except StateSyncError:
        raise
    except (ProviderStatusError, DeliveryFailure, MailConfigurationError, ValueError) as error:
        complete(root, token, args.day, {"status": "repair_blocked"})
        print(json.dumps({"date": args.day, "status": "repair_blocked", "reason": str(error)}))
        return 1
    complete(root, token, args.day, result)
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
