"""Brevo's durable future outbox. Queue acceptance is distinct from actual send."""
import hashlib
import hmac
import json
import re
import uuid
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone

from .research import tls_context
from .service import (BERLIN, TERMINAL, DeliveryFailure, NoMailRedirect, atomic_json,
                      mail_settings, make_message, read_json, record_delivered_jobs,
                      run_lock, send_brevo)


class ProviderStatusError(RuntimeError):
    pass


def send_time(day):
    target = date.fromisoformat(day)
    if target.weekday() >= 5:
        raise ValueError("Scheduled delivery requires a weekday")
    return datetime.combine(target, datetime.min.time(), BERLIN).replace(hour=8, minute=30)


def recipients(settings):
    return {s.strip().lower() for key in ("MAIL_TO", "MAIL_CC")
            for s in settings[key].split(",") if s.strip()}


def recipient_identity(settings):
    # Public run records must not reveal recipient addresses or guessable hashes.
    value = "\n".join(sorted(recipients(settings))).encode()
    return hmac.new(settings["BREVO_API_KEY"].encode(), value, hashlib.sha256).hexdigest()


def provider_get(path, settings):
    request = urllib.request.Request("https://api.brevo.com/v3/" + path,
        headers={"api-key": settings["BREVO_API_KEY"], "Accept": "application/json"})
    opener = urllib.request.build_opener(NoMailRedirect(),
                                       urllib.request.HTTPSHandler(context=tls_context()))
    try:
        with opener.open(request, timeout=30) as response:
            raw = response.read(1000001)
            if response.status != 200 or len(raw) > 1000000:
                raise ValueError("Invalid provider status")
            result = json.loads(raw)
            if not isinstance(result, dict):
                raise ValueError("Invalid provider status")
            return result
    except urllib.error.HTTPError as error:
        code = error.code
        error.close()
        raise ProviderStatusError(f"Brevo status unavailable (HTTP {code})") from None
    except (OSError, urllib.error.URLError, ValueError, UnicodeError):
        raise ProviderStatusError("Brevo status unavailable; inspect provider logs") from None


def instant(value):
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("Provider timestamp lacks timezone")
    return result.astimezone(timezone.utc)


def queue_status(record, settings, getter=provider_get):
    identifiers = message_identifiers(record)
    if not identifiers and record.get("provider_batch_id"):
        identifiers = [record["provider_batch_id"]]
    if not identifiers:
        raise ProviderStatusError("Queue identifier unavailable; do not resubmit")
    states = [single_queue_status(record, identifier, settings, getter) for identifier in identifiers]
    return "queued" if all(state == "queued" for state in states) else states[0] if len(set(states)) == 1 else "inProgress"


def single_queue_status(record, identifier, settings, getter):
    identifier = urllib.parse.quote("<" + identifier.strip("<>") + ">" if "@" in identifier else identifier, safe="")
    response = getter("smtp/emailStatus/" + identifier, settings)
    candidates = response.get("batches", [response])
    if not isinstance(candidates, list):
        raise ProviderStatusError("Brevo queue status has an unexpected shape")
    matched = []
    for item in candidates:
        if not isinstance(item, dict):
            continue
        try:
            same_time = instant(item.get("scheduledAt", "")) == instant(record["scheduled_at"])
        except (ValueError, TypeError, AttributeError):
            continue
        if same_time and item.get("status") in {"queued", "inProgress", "processed"}:
            matched.append(item["status"])
    if len(matched) != 1:
        raise ProviderStatusError("Brevo did not confirm the expected scheduled time")
    return matched[0]


def schedule_report(root, store, day, now=None, persist=None, sender=send_brevo,
                    getter=provider_get):
    now = now or datetime.now(timezone.utc)
    target = send_time(day)
    if not timedelta(0) < target - now <= timedelta(hours=72):
        raise ValueError("Brevo scheduling requires a future send within 72 hours")
    persist = persist or (lambda record: None)
    with run_lock(store):
        path = store / "runs" / f"{day}.json"
        record = read_json(path, {})
        if record.get("status") in TERMINAL:
            return {"date": day, "status": "skipped", "delivery_status": record["status"]}
        if (record.get("date") != day or record.get("fixture") or not record.get("report")
                or not path.with_suffix(".html").exists()):
            raise ValueError("Scheduling requires a saved live report and HTML")
        settings = mail_settings()
        if settings["EMAIL_PROVIDER"] != "brevo":
            raise ValueError("Future scheduling requires Brevo")
        record.update(scheduled_at=target.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
                      email_provider="brevo", recipient_set_id=recipient_identity(settings),
                      expected_recipients=len(recipients(settings)))
        record.setdefault("provider_batch_id", str(uuid.uuid4()))
        body = path.with_suffix(".html").read_text(encoding="utf-8")
        def save():
            atomic_json(path, record)
            persist(record)
        def on_scheduling():
            record["status"] = "scheduling"
            record.setdefault("attempts", []).append({"status": "scheduling", "started_at": now.isoformat()})
            save()  # Failed Git persistence must prevent the POST.
        try:
            outcome = sender(make_message(record, body, settings), settings, on_scheduling,
                             scheduled_at=record["scheduled_at"], batch_id=record["provider_batch_id"])
        except DeliveryFailure as error:
            record["status"] = "schedule_uncertain" if error.ambiguous else "schedule_failed"
            record["error"] = str(error)
            record["attempts"][-1]["status"] = record["status"]
            save()
            raise
        record.update(status="scheduled", provider_message_id=outcome["provider_message_id"],
                      provider_message_ids=outcome.get("provider_message_ids", [outcome["provider_message_id"]]),
                      schedule_accepted_at=datetime.now(timezone.utc).isoformat())
        record.pop("accepted_at", None)
        record.pop("error", None)
        record["attempts"][-1]["status"] = "scheduled"
        save()  # Persist acknowledgement before GET; failed GET can never retry POST.
        try:
            record["provider_status"] = queue_status(record, settings, getter)
            if record["provider_status"] != "queued":
                raise ProviderStatusError("Brevo has not confirmed a future queued message")
            record["schedule_verified_at"] = datetime.now(timezone.utc).isoformat()
        except ProviderStatusError:
            record["provider_check_error"] = "Queue acknowledgement saved; provider queue verification unavailable"
            save()
            raise
        record.pop("provider_check_error", None)
        save()
        return {"date": day, "status": "scheduled", "hits": record["hits"],
                "scheduled_at": record["scheduled_at"], "provider_status": record["provider_status"]}


def message_identifiers(record):
    return record.get("provider_message_ids") or ([record["provider_message_id"]]
                                                  if record.get("provider_message_id") else [])


def recover_sent_identifiers(record, body, settings, now, getter):
    """Recover lost acknowledgement only from unique, exact sent content for every recipient."""
    identifiers = set()
    target = instant(record["scheduled_at"])
    for address in recipients(settings):
        query = urllib.parse.urlencode({"email": address, "startDate": record["date"],
            "endDate": now.astimezone(BERLIN).date().isoformat(), "limit": 100, "sort": "desc"})
        emails = getter("smtp/emails?" + query, settings).get("transactionalEmails")
        if not isinstance(emails, list):
            raise ProviderStatusError("Sent-message inspection unavailable")
        candidates = set()
        for email in emails:
            if not isinstance(email, dict) or email.get("subject") != record["subject"]:
                continue
            if str(email.get("email", "")).lower() != address:
                continue
            try:
                when = instant(email.get("date", ""))
            except (ValueError, TypeError, AttributeError):
                continue
            identifier = email.get("messageId", "")
            if (not target <= when <= now + timedelta(minutes=1) or not isinstance(identifier, str)
                    or not re.fullmatch(r"<?[A-Za-z0-9_.+\-]{1,160}@[A-Za-z0-9.-]{1,90}>?", identifier)
                    or not isinstance(email.get("uuid"), str)):
                continue
            details = getter("smtp/emails/" + urllib.parse.quote(email["uuid"], safe=""), settings)
            if (details.get("subject") == record["subject"]
                    and str(details.get("email", "")).lower() == address
                    and isinstance(details.get("body"), str) and details["body"].strip() == body.strip()):
                candidates.add(identifier)
        if len(candidates) != 1:
            raise ProviderStatusError("No unique exact-content sent message for every recipient; inspect Brevo")
        identifiers.update(candidates)
    return sorted(identifiers)


def refresh_status(root, store, day, now=None, persist=None, getter=provider_get):
    """Read-only at Brevo. Never POST, cancel or replace a queued message."""
    now = now or datetime.now(timezone.utc)
    persist = persist or (lambda record: None)
    with run_lock(store):
        path = store / "runs" / f"{day}.json"
        record = read_json(path, {})
        if record.get("status") not in {"scheduled", "sent", "scheduling", "schedule_uncertain"} or not record.get("scheduled_at"):
            return {"date": day, "status": record.get("status", "missing")}
        settings = mail_settings()
        if (settings["EMAIL_PROVIDER"] != "brevo"
                or not hmac.compare_digest(record.get("recipient_set_id", ""), recipient_identity(settings))):
            raise ProviderStatusError("Recipient configuration or API key changed; reconcile provider logs")
        record["provider_checked_at"] = now.isoformat()
        try:
            if now < instant(record["scheduled_at"]):
                record["provider_status"] = queue_status(record, settings, getter)
                if record["provider_status"] == "queued":
                    record["schedule_verified_at"] = now.isoformat()
                    record["status"] = "scheduled"
                    record.setdefault("schedule_accepted_at", now.isoformat())
            else:
                # A processed queue entry alone does not prove that recipients were sent mail.
                identifiers = message_identifiers(record)
                if not identifiers:
                    identifiers = recover_sent_identifiers(record, path.with_suffix(".html").read_text(encoding="utf-8"),
                                                          settings, now, getter)
                    record.update(provider_message_ids=identifiers, provider_message_id=identifiers[0],
                                  schedule_recovered_at=now.isoformat())
                events = []
                for identifier in identifiers:
                    query = urllib.parse.urlencode({"messageId": "<" + identifier.strip("<>") + ">", "limit": 100, "sort": "desc"})
                    response = getter("smtp/statistics/events?" + query, settings)
                    if not isinstance(response.get("events"), list):
                        raise ProviderStatusError("Brevo delivery events unavailable")
                    events.extend(response["events"])
                accepted, delivered, times, delivered_times = set(), set(), [], []
                expected = recipients(settings)
                for event in events:
                    if not isinstance(event, dict):
                        continue
                    if str(event.get("messageId", "")).strip("<>") not in {identifier.strip("<>") for identifier in identifiers}:
                        continue
                    address = str(event.get("email", "")).lower().strip()
                    if address not in expected:
                        continue
                    try:
                        event_time = instant(event.get("date", ""))
                    except (ValueError, TypeError, AttributeError):
                        continue
                    if not instant(record["scheduled_at"]) <= event_time <= now + timedelta(minutes=1):
                        continue
                    if event.get("event") in {"request", "requests", "delivered"}:
                        accepted.add(address)
                        times.append(event_time)
                    if event.get("event") == "delivered":
                        delivered.add(address)
                        delivered_times.append(event_time)
                record.update(sent_recipient_count=len(accepted), delivered_recipient_count=len(delivered))
                if accepted == expected:
                    record.update(status="sent", provider_status="sent")
                    record.setdefault("accepted_at", max(times).isoformat())
                    record_delivered_jobs(store, record)
                if delivered == expected:
                    record["delivered_at"] = max(delivered_times).isoformat()
                    record["provider_status"] = "delivered"
            record.pop("provider_check_error", None)
        except ProviderStatusError as error:
            record["provider_check_error"] = str(error)
            atomic_json(path, record)
            persist(record)
            raise
        atomic_json(path, record)
        persist(record)
        return {"date": day, "status": record["status"], "provider_status": record.get("provider_status"),
                "scheduled_at": record["scheduled_at"], "accepted_at": record.get("accepted_at")}
