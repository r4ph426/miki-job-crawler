"""Durable outbox, Berlin scheduling, report rendering and email delivery."""

import copy
import fcntl
import hashlib
import json
import os
import re
import smtplib
import subprocess
import tempfile
import urllib.error
import urllib.request
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path
from zoneinfo import ZoneInfo

from .newsletter import DESIGN_VERSION, render_report
from .research import ResearchError, canonical_url, research, select_jobs, tls_context, validate_report, verify_job
from .salaries import with_salary_estimates

BERLIN = ZoneInfo("Europe/Berlin")
TERMINAL = {"sent", "sending", "uncertain", "partial"}


class StateSyncError(RuntimeError):
    pass


class MailConfigurationError(RuntimeError):
    def __init__(self, message, code):
        super().__init__(message)
        self.code = code


class DeliveryFailure(RuntimeError):
    def __init__(self, message, ambiguous=False):
        super().__init__(message)
        self.ambiguous = ambiguous


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as f:
        json.dump(value, f, ensure_ascii=False, indent=2)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
        temp = Path(f.name)
    os.replace(temp, path)
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def read_json(path, default):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else copy.deepcopy(default)


@contextmanager
def run_lock(store):
    store.mkdir(parents=True, exist_ok=True)
    with (store / ".run.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise RuntimeError("Another run owns the local service lock") from error
        yield


def load_history(root, store):
    return read_json(root / "data/seed-history.json", []) + read_json(store / "history.json", [])


def reserved_jobs(store):
    """Quarantine recommendations whose previous SMTP acceptance is still unknown."""
    reserved = []
    for path in (store / "runs").glob("*.json"):
        record = read_json(path, {})
        if record.get("status") in {"sending", "uncertain"}:
            reserved.extend(dict(job, date=record["date"], origin="Reserved pending delivery reconciliation")
                            for job in record.get("report", {}).get("jobs", []))
    return reserved


def due(now, config):
    if now.tzinfo is None:
        raise ValueError("Run time must include a timezone")
    local = now.astimezone(ZoneInfo(config["timezone"]))
    return local.weekday() < 5 and (local.hour, local.minute) >= (config["hour"], config.get("minute", 0))


def next_run(now, config):
    local = now.astimezone(ZoneInfo(config["timezone"]))
    candidate = local.replace(hour=config["hour"], minute=config.get("minute", 0), second=0, microsecond=0)
    if candidate <= local:
        candidate += timedelta(days=1)
    while candidate.weekday() >= 5:
        candidate += timedelta(days=1)
    return candidate.isoformat()


def status(root, store, now=None):
    now = now or datetime.now(timezone.utc)
    config = read_json(root / "config/search.json", {})
    runs = [read_json(p, {}) for p in sorted((store / "runs").glob("*.json"), reverse=True)]
    day = now.astimezone(BERLIN).date().isoformat()
    today = read_json(store / "runs" / f"{day}.json", {})
    eligible = due(now, config) and (not today or today.get("status") not in TERMINAL)
    planned = now.astimezone(BERLIN).isoformat() if eligible else next_run(now, config)
    return {
        "timezone": config["timezone"], "scheduled_hour": config["hour"],
        "scheduled_minute": config.get("minute", 0),
        "scheduled_days": "Monday–Friday", "next_eligible_run": planned,
        "historical_jobs": len(load_history(root, store)), "runs": runs,
        "reserved_jobs_awaiting_reconciliation": len(reserved_jobs(store)),
        "note": "Local records only. A deployment scheduler is required for unattended operation.",
    }



def plain_addresses(value, required=True, single=False):
    if not value.strip():
        return not required
    parts = value.split(",")
    if single and len(parts) != 1:
        return False
    return all(re.fullmatch(r"[^@\s<>\"'`]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,63}", part.strip())
               for part in parts)


def mail_settings(environ=None):
    environ = os.environ if environ is None else environ
    provider = environ.get("EMAIL_PROVIDER", "").strip().lower() or "brevo"
    if provider not in {"brevo", "gmail"}:
        raise MailConfigurationError("EMAIL_PROVIDER must be brevo or gmail", "unsupported_mail_provider")
    required = (["GMAIL_USER", "GMAIL_APP_PASSWORD"] if provider == "gmail" else ["BREVO_API_KEY"]) + ["MAIL_TO"]
    missing = [k for k in required if not environ.get(k, "").strip()]
    mail_from = environ.get("MAIL_FROM", "").strip() or environ.get("GMAIL_USER", "").strip()
    if not mail_from:
        missing.append("MAIL_FROM")
    if missing:
        raise MailConfigurationError("Missing secure mail configuration: " + ", ".join(missing), "missing_mail_configuration")
    settings = {k: environ.get(k, "").strip() for k in required + ["MAIL_CC"]}
    settings.update(EMAIL_PROVIDER=provider, MAIL_FROM=mail_from)
    if provider == "gmail":
        # Google's display groups app passwords using spaces, sometimes nonbreaking.
        settings["GMAIL_APP_PASSWORD"] = "".join(settings["GMAIL_APP_PASSWORD"].split())
    else:
        key = settings["BREVO_API_KEY"]
        if not key.isascii() or not key.isprintable() or any(c.isspace() for c in key) or any(c in key for c in "\"'`"):
            raise MailConfigurationError("BREVO_API_KEY must be the unquoted API key", "invalid_mail_api_key")
    for key in ["MAIL_FROM", "MAIL_TO", "MAIL_CC"] + (["GMAIL_USER"] if provider == "gmail" else []):
        if "\r" in settings[key] or "\n" in settings[key]:
            raise MailConfigurationError(f"{key} must not contain line breaks", "mail_address_line_breaks")
        if not plain_addresses(settings[key], required=key != "MAIL_CC", single=key in {"GMAIL_USER", "MAIL_FROM"}):
            raise MailConfigurationError(f"{key} must contain plain email addresses", "invalid_mail_addresses")
    return settings


def make_message(record, body, settings):
    message = EmailMessage()
    message["From"], message["To"] = settings["MAIL_FROM"], settings["MAIL_TO"]
    if settings["MAIL_CC"]:
        message["Cc"] = settings["MAIL_CC"]
    message["Subject"], message["Message-ID"] = record["subject"], record["message_id"]
    message.set_content("Der Stellenbericht liegt als HTML-Version vor. Bitte einen HTML-fähigen E-Mail-Client verwenden.")
    message.add_alternative(body, subtype="html")
    return message


class NoMailRedirect(urllib.request.HTTPRedirectHandler):
    """Never forward a mail API credential or delivery body to a redirect."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def send_brevo(message, settings, on_sending):
    """Save a durable marker before POST; never retry an uncertain acceptance."""
    payload = {
        "sender": {"email": settings["MAIL_FROM"], "name": "Stellen für Miki"},
        "to": [{"email": address.strip()} for address in settings["MAIL_TO"].split(",")],
        "subject": str(message["Subject"]),
        "htmlContent": message.get_body(preferencelist=("html",)).get_content(),
        "textContent": message.get_body(preferencelist=("plain",)).get_content(),
        "headers": {"X-Miki-Message-ID": str(message["Message-ID"])},
        "tags": ["miki-jobsearch"],
    }
    if settings["MAIL_CC"]:
        payload["cc"] = [{"email": address.strip()} for address in settings["MAIL_CC"].split(",")]
    # Additional provider deduplication; durable local/Git state remains the guard.
    payload["headers"]["Idempotency-Key"] = hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()
    request = urllib.request.Request("https://api.brevo.com/v3/smtp/email",
                                     data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                                     headers={"api-key": settings["BREVO_API_KEY"],
                                              "Content-Type": "application/json", "Accept": "application/json"},
                                     method="POST")
    opener = urllib.request.build_opener(NoMailRedirect(), urllib.request.HTTPSHandler(context=tls_context()))
    on_sending()  # A failed checkpoint must prevent even the first POST.
    try:
        with opener.open(request, timeout=45) as response:
            code = response.status
            raw = response.read(16001)
    except urllib.error.HTTPError as error:
        # Body may contain private data. Only fixed guidance and the status are saved.
        code = error.code
        error.close()
        if code in {400, 401, 402, 403, 404, 405, 413, 415, 422, 429}:
            guidance = ("check the API key" if code == 401 else
                        "check sender verification and account approval" if code in {400, 403, 422} else
                        "check account quota or billing" if code in {402, 429} else "check mail configuration")
            raise DeliveryFailure(f"Brevo rejected delivery (HTTP {code}); {guidance}") from error
        raise DeliveryFailure(f"Brevo acceptance is uncertain (HTTP {code}); inspect transactional logs before retrying",
                              ambiguous=True) from error
    except (OSError, urllib.error.URLError) as error:
        raise DeliveryFailure("Brevo acceptance is uncertain (connection interrupted); inspect transactional logs before retrying",
                              ambiguous=True) from error
    try:
        data = json.loads(raw) if len(raw) <= 16000 else None
        provider_id = data.get("messageId") if isinstance(data, dict) else None
        if code != 201 or not isinstance(provider_id, str) or not re.fullmatch(r"<?[A-Za-z0-9_.+\-]{1,160}@[A-Za-z0-9.-]{1,90}>?", provider_id):
            raise ValueError("Missing acknowledgement")
    except (ValueError, UnicodeError):
        raise DeliveryFailure("Brevo acceptance is uncertain (invalid acknowledgement); inspect transactional logs before retrying",
                              ambiguous=True) from None
    return {"status": "sent", "refused_count": 0, "provider_message_id": provider_id}


def send_mail(message, settings, on_sending):
    sender = send_brevo if settings["EMAIL_PROVIDER"] == "brevo" else send_smtp
    return sender(message, settings, on_sending)


def send_smtp(message, settings, on_sending):
    """Persist sending before DATA; distinguish rejection from ambiguous acceptance."""
    connection = None
    for port in [465, 587]:
        connection = None
        phase = "connect"
        try:
            if port == 465:
                connection = smtplib.SMTP_SSL("smtp.gmail.com", port, timeout=45, context=tls_context())
            else:
                connection = smtplib.SMTP("smtp.gmail.com", port, timeout=45)
                phase = "starttls"
                connection.ehlo()
                connection.starttls(context=tls_context())
                connection.ehlo()
            phase = "authenticate"
            features = connection.esmtp_features
            if port == 587 and isinstance(features, dict) and "LOGIN" in features.get("auth", "").upper().split():
                # smtplib.login prefers PLAIN. A server closing that exchange may
                # still accept its advertised LOGIN mechanism on a fresh TLS session.
                phase = "authenticate-login"
                connection.user = settings["GMAIL_USER"]
                connection.password = settings["GMAIL_APP_PASSWORD"]
                connection.auth("LOGIN", connection.auth_login, initial_response_ok=False)
            else:
                connection.login(settings["GMAIL_USER"], settings["GMAIL_APP_PASSWORD"], initial_response_ok=False)
            break
        except (OSError, smtplib.SMTPException, UnicodeError) as error:
            if connection is not None:
                connection.close()
            # Both routes require verified TLS before authentication. A definite
            # credential rejection needs account correction rather than retries.
            if port == 465 and not isinstance(error, (smtplib.SMTPAuthenticationError, UnicodeError)):
                continue
            smtp_code = getattr(error, "smtp_code", None)
            code_note = f"; SMTP {smtp_code}" if type(smtp_code) is int and 100 <= smtp_code <= 599 else ""
            raise DeliveryFailure(f"SMTP {phase} failed on port {port} ({type(error).__name__}{code_note})") from error
    try:
        on_sending()
        recipients = [s.strip() for s in (settings["MAIL_TO"] + "," + settings["MAIL_CC"]).split(",") if s.strip()]
        try:
            refused = connection.send_message(message, to_addrs=recipients)
        except (smtplib.SMTPRecipientsRefused, smtplib.SMTPSenderRefused, smtplib.SMTPDataError, smtplib.SMTPNotSupportedError) as error:
            raise DeliveryFailure(f"SMTP explicitly rejected delivery ({type(error).__name__})") from error
        except (OSError, smtplib.SMTPException) as error:
            raise DeliveryFailure(f"SMTP acceptance is uncertain ({type(error).__name__}); inspect Gmail using Message-ID", ambiguous=True) from error
        return {"status": "partial" if refused else "sent", "refused_count": len(refused)}
    finally:
        # A failed QUIT cannot turn an accepted DATA response into a failed send.
        connection.close()


def git_persister(root, store):
    if store.resolve() != (root / "state").resolve():
        raise StateSyncError("Git persistence requires the repository state/ directory")
    branch = os.environ.get("STATE_BRANCH")
    if not branch:
        raise StateSyncError("STATE_BRANCH is required for Git persistence")
    def command(*args, **kwargs):
        return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, **kwargs)
    if command("check-ref-format", "--branch", branch).returncode:
        raise StateSyncError("Invalid state branch")
    def persist(record):
        from .status_feed import write_feed
        write_feed(root, store)
        steps = [("add", "--", "state")]
        for args in steps:
            if command(*args).returncode:
                raise StateSyncError("Could not stage delivery state")
        diff = command("diff", "--cached", "--quiet", "--", "state")
        if diff.returncode not in [0, 1]:
            raise StateSyncError("Could not inspect staged delivery state")
        if diff.returncode == 1:
            if command("commit", "--only", "-m", f"Record Miki run {record['date']}: {record['status']}", "--", "state").returncode:
                raise StateSyncError("Could not commit delivery state")
        if command("push", "origin", f"HEAD:refs/heads/{branch}").returncode:
            raise StateSyncError("Could not push delivery state; delivery must not be retried blindly")
    return persist


def record_delivered_jobs(store, record):
    delivered = read_json(store / "history.json", [])
    keys = {(j["date"], canonical_url(j["url"])) for j in delivered}
    for job in record["report"]["jobs"]:
        key = (record["date"], canonical_url(job["url"]))
        if key not in keys:
            delivered.append(dict(job, date=record["date"], delivery_status=record["status"]))
            keys.add(key)
    atomic_json(store / "history.json", delivered)


def record_path(store, day, revision=None):
    if revision and not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,39}", revision):
        raise ValueError("Revision must use 1–40 lowercase letters, digits, hyphens or underscores")
    suffix = f"--{revision}" if revision else ""
    return store / "runs" / f"{day}{suffix}.json"


def reconcile(store, day, result, note, persist=None, revision=None):
    """Explicit operator decision after checking provider delivery logs."""
    day = date.fromisoformat(day).isoformat()
    if result not in ["sent", "not-sent"] or not note.strip():
        raise ValueError("Reconciliation requires sent/not-sent and an evidence note")
    persist = persist or (lambda record: None)
    with run_lock(store):
        path = record_path(store, day, revision)
        record = read_json(path, {})
        if record.get("status") not in ["sending", "uncertain", "partial"]:
            raise ValueError("Only sending, uncertain or partial deliveries require reconciliation")
        if record["status"] == "partial" and result == "not-sent":
            raise ValueError("Some recipients already received this message; do not resend to everyone")
        record.setdefault("reconciliations", []).append({
            "at": datetime.now(timezone.utc).isoformat(), "previous_status": record["status"],
            "result": result, "note": note,
        })
        record["status"] = "sent" if result == "sent" else "failed"
        record.pop("error", None)
        if result == "sent":
            record_delivered_jobs(store, record)
        atomic_json(path, record)
        persist(record)
        return {"date": day, "status": record["status"], "message_id": record["message_id"]}


def run(root, store, now=None, dry_run=True, fixture=None, output_dir=None,
        researcher=research, verifier=verify_job, sender=send_mail, persist=None,
        prepare=False, revision=None, revision_reason=None, delivery_date=None,
        require_prepared=False, allow_early_prepare=False):
    now = now or datetime.now(timezone.utc)
    config = read_json(root / "config/search.json", {})
    if (not dry_run or prepare) and fixture is not None:
        raise ValueError("Fixture data cannot be emailed")
    if prepare and dry_run:
        raise ValueError("Preparation writes durable live state; do not combine it with dry-run")
    if revision and not (revision_reason and revision_reason.strip()):
        raise ValueError("An explicit reason is required for a revised report")
    local = now.astimezone(ZoneInfo(config["timezone"]))
    research_day = local.date()
    day = date.fromisoformat(delivery_date) if delivery_date else research_day
    if delivery_date and (not prepare or day.weekday() >= 5
                          or not research_day < day <= research_day + timedelta(days=3)
                          or (not allow_early_prepare and (day != research_day + timedelta(days=1) or local.hour < 16))):
        raise ValueError("Advance preparation requires the next calendar weekday from 16:00 Berlin")
    if not delivery_date and not due(now, config) and fixture is None:
        return {"date": str(day), "status": "skipped", "reason": "Outside weekday Berlin delivery window"}
    if not dry_run and persist is None:
        # Local server mode must use a durable mounted directory, documented separately.
        persist = lambda record: None
    with run_lock(store):
        history = load_history(root, store)
        exclusion_history = history + reserved_jobs(store)
        if delivery_date:
            # Today's mail may still be queued when tomorrow's research starts.
            # Reserve previously prepared earlier reports to avoid repeating jobs.
            for previous in (store / "runs").glob("*.json"):
                pending = read_json(previous, {})
                if (pending.get("date", "9999") < str(day)
                        and pending.get("status") in {"prepared", "failed", "configuration_failed"}):
                    exclusion_history.extend(pending.get("report", {}).get("jobs", []))
        path = record_path(store, str(day), revision)
        record = read_json(path, {})
        if not dry_run and record.get("status") in TERMINAL:
            return {"date": str(day), "status": "skipped", "delivery_status": record["status"],
                    "reason": "Already sent or requires manual delivery reconciliation"}
        if require_prepared and (not record.get("report") or record.get("fixture")):
            return {"date": str(day), "status": "skipped", "reason": "No prepared live report"}
        if revision:
            original = read_json(record_path(store, str(day)), {})
            if original.get("status") != "sent":
                raise ValueError("A revised report requires a confirmed sent original")
            if any(r.get("status") in {"sending", "uncertain", "partial"}
                   for r in [read_json(p, {}) for p in (store / "runs").glob(f"{day}--*.json")]):
                raise ValueError("Reconcile existing revised deliveries before preparing another")
            config = dict(config, previous_candidate_urls=[j["url"] for j in original.get("report", {}).get("rejected", [])])
        if not dry_run:
            record.update(date=str(day), last_attempt_at=now.isoformat())
            if os.environ.get("GITHUB_RUN_ID"):
                record["last_attempt_run_id"] = os.environ["GITHUB_RUN_ID"]
        try:
            settings = mail_settings() if not dry_run and not prepare else None
        except MailConfigurationError as error:
            record.update(status="configuration_failed", error=str(error), configuration_diagnostic=error.code)
            atomic_json(path, record)
            persist(record)
            raise
        if dry_run or prepare or not record.get("report"):
            try:
                raw = validate_report(copy.deepcopy(fixture)) if fixture is not None else researcher(root, config, exclusion_history, research_day)
                verifier_fn = (lambda job, config: (True, "Fixture only; not live verified")) if fixture is not None else verifier
                jobs, rejected = select_jobs(raw, config, exclusion_history, day, verifier_fn)
                report = dict(raw, jobs=jobs, rejected=rejected)
                if delivery_date:
                    report["summary"] = (f"Recherche vom {research_day.strftime('%d.%m.%Y')} für den Versand "
                                         f"am {day.strftime('%d.%m.%Y')}. " + report["summary"])
                verification_failures = [j for j in rejected if j["reason"] not in {
                    "Already reported or repeated within this run", "Below score threshold", "Application deadline passed", "Expired"}]
                if not jobs and verification_failures and fixture is None:
                    raise ResearchError("All plausible candidates failed independent verification; retry research instead of reporting zero jobs",
                                        code="no_verified_candidates")
                report = with_salary_estimates(report, day, read_json(root / "config/salary-benchmarks.json", {}))
                subject, body = render_report(report, day, history, fixture is not None, revision)
            except Exception as error:
                if not dry_run:
                    failure = {"date": str(day), "status": "research_failed", "hits": 0,
                               "failed_at": now.isoformat(), "error": f"Research failed ({type(error).__name__}); see workflow log"}
                    if isinstance(error, ResearchError):
                        failure["research_diagnostic"] = error.diagnostic
                    if revision:
                        failure.update(revision=revision, revision_reason=revision_reason)
                    if "report" in locals():
                        failure["candidate_diagnostics"] = report.get("rejected", [])
                    atomic_json(path, failure)
                    persist(failure)
                raise
            record = {"date": str(day), "created_at": now.isoformat(), "status": "prepared", "subject": subject,
                      "report": report, "attempts": [], "hits": len(jobs), "researched_at": now.isoformat(),
                      "prepared_at": datetime.now(timezone.utc).isoformat(),
                      "message_id": f"<miki-{day}-{hashlib.sha256(body.encode()).hexdigest()[:16]}@miki-jobsearch>"}
            if revision:
                record.update(revision=revision, revision_reason=revision_reason,
                              supersedes_message_id=original["message_id"])
            if not dry_run:
                record["last_attempt_at"] = now.isoformat()
                if os.environ.get("GITHUB_RUN_ID"):
                    record["last_attempt_run_id"] = os.environ["GITHUB_RUN_ID"]
        else:
            # Reuse the live research but apply current rendering to an unsent outbox.
            # Terminal delivery states were already blocked above.
            record["report"] = with_salary_estimates(record["report"], day, read_json(root / "config/salary-benchmarks.json", {}))
            record["subject"], body = render_report(record["report"], day, history, revision=revision)
            record["message_id"] = f"<miki-{day}-{hashlib.sha256(body.encode()).hexdigest()[:16]}@miki-jobsearch>"
        if dry_run:
            output = output_dir or root / "out" / str(day)
            output.mkdir(parents=True, exist_ok=True)
            (output / "mail.html").write_text(body, encoding="utf-8")
            atomic_json(output / "report.json", dict(record, status="dry_run", fixture=fixture is not None))
            return {"date": str(day), "status": "dry_run", "hits": record["hits"], "output_dir": str(output)}
        path.parent.mkdir(parents=True, exist_ok=True)
        (path.with_suffix(".html")).write_text(body, encoding="utf-8")
        record["status"] = "prepared"
        record["design_version"] = DESIGN_VERSION
        if settings:
            record["email_provider"] = settings["EMAIL_PROVIDER"]
        record.pop("error", None)
        record.pop("configuration_diagnostic", None)
        atomic_json(path, record)
        persist(record)  # If this fails, delivery must not be called.
        if prepare:
            return {"date": str(day), "revision": revision, "status": "prepared", "hits": record["hits"], "record": str(path)}
        if revision and not record["report"]["jobs"]:
            raise ResearchError("Refusing to send an empty revised report", code="no_verified_candidates")
        def on_sending():
            record["status"] = "sending"
            record["attempts"].append({"started_at": datetime.now(timezone.utc).isoformat(), "status": "sending"})
            atomic_json(path, record)
            persist(record)  # Durable ambiguity marker exists before SMTP DATA or API POST.
        try:
            outcome = sender(make_message(record, body, settings), settings, on_sending)
        except DeliveryFailure as error:
            record["status"] = "uncertain" if error.ambiguous else "failed"
            record["error"] = str(error)
            if record["attempts"] and record["attempts"][-1]["status"] == "sending":
                record["attempts"][-1]["status"] = record["status"]
            atomic_json(path, record)
            persist(record)
            raise
        except Exception as error:
            # Before on_sending there can be no DATA acceptance. Save only the
            # exception type; leave a sending checkpoint intact if DATA is possible.
            if record["status"] == "prepared":
                record["status"] = "failed"
                record["error"] = f"Delivery preparation failed ({type(error).__name__})"
                atomic_json(path, record)
                persist(record)
            raise
        record["status"] = outcome["status"]
        record["refused_count"] = outcome["refused_count"]
        if outcome.get("provider_message_id"):
            record["provider_message_id"] = outcome["provider_message_id"]
        record["accepted_at"] = datetime.now(timezone.utc).isoformat()
        record["attempts"][-1]["status"] = record["status"]
        record_delivered_jobs(store, record)
        atomic_json(path, record)
        persist(record)
        return {"date": str(day), "status": record["status"], "hits": record["hits"], "message_id": record["message_id"]}
