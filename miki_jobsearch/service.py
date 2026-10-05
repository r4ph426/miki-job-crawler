"""Durable outbox, Berlin scheduling, report rendering and SMTP delivery."""

import copy
import fcntl
import hashlib
import html
import json
import os
import smtplib
import subprocess
import tempfile
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path
from zoneinfo import ZoneInfo

from .research import ResearchError, canonical_url, research, select_jobs, tls_context, validate_report, verify_job

BERLIN = ZoneInfo("Europe/Berlin")
TERMINAL = {"sent", "sending", "uncertain", "partial"}


class StateSyncError(RuntimeError):
    pass


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
    return local.weekday() < 5 and local.hour >= config["hour"]


def next_run(now, config):
    local = now.astimezone(ZoneInfo(config["timezone"]))
    candidate = local.replace(hour=config["hour"], minute=0, second=0, microsecond=0)
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
    today = next((r for r in runs if r["date"] == day), None)
    eligible = due(now, config) and (not today or today.get("status") not in TERMINAL)
    planned = now.astimezone(BERLIN).isoformat() if eligible else next_run(now, config)
    return {
        "timezone": config["timezone"], "scheduled_hour": config["hour"],
        "scheduled_days": "Monday–Friday", "next_eligible_run": planned,
        "historical_jobs": len(load_history(root, store)), "runs": runs,
        "reserved_jobs_awaiting_reconciliation": len(reserved_jobs(store)),
        "note": "Local records only. A deployment scheduler is required for unattended operation.",
    }


def render_report(report, day, history, fixture=False):
    esc = lambda value: html.escape(str(value), quote=True)
    jobs = report["jobs"]
    weekly = day.weekday() == 4
    subject = (f"Wochenüberblick Stellensuche · KW {day.isocalendar().week}" if weekly
               else f"Stellen für Miki · {day:%d.%m.%Y} · {len(jobs)} neue Treffer")
    body = ["<!doctype html><html lang='de'><head><meta charset='utf-8'></head>",
            "<body style='font-family:Arial,sans-serif;max-width:720px;margin:24px auto;color:#20252b;line-height:1.55'>",
            f"<h1>{esc(subject)}</h1>"]
    if fixture:
        body.append("<p><strong>Testdaten: keine Live-Recherche, keine E-Mail versendet.</strong></p>")
    summary = report["summary"]
    action = report["next_action"]
    if not jobs or report["rejected"]:
        summary = f"{len(jobs)} neue, unabhängig verifizierte Treffer ab 55 Punkten."
        action = (f"Heute die Anzeige {jobs[0]['title']} bei {jobs[0]['employer']} prüfen und die Bewerbung vorbereiten "
                  f"(geschätzter Aufwand: {jobs[0]['effort_minutes']} Minuten)." if jobs else
                  "Heute 30 Minuten für den Bewerbungsüberblick einplanen: Rückmeldungen und offene Bewerbungen im eigenen Postfach prüfen.")
    body.append(f"<p>{esc(summary)}</p>")
    outages = [s for s in report["source_checks"] if s["status"] != "ok"]
    if outages:
        body.append("<aside style='padding:16px;background:#fff1df'><strong>Quellenausfälle: eingeschränkte Abdeckung</strong><ul>")
        body.extend(f"<li>{esc(s['group'])}: {esc(s['details'])}</li>" for s in outages)
        body.append("</ul></aside>")
    verification_failures = [r for r in report["rejected"] if r["reason"] not in
                             ["Already reported or repeated within this run", "Below score threshold", "Application deadline passed", "Expired"]]
    if verification_failures:
        body.append(f"<p><strong>{len(verification_failures)} Kandidaten konnten nicht unabhängig geprüft werden und wurden nicht aufgenommen.</strong></p>")
    known = history + [dict(j, date=str(day)) for j in jobs]
    deadlines = [j for j in known if j.get("deadline") and str(day) <= j["deadline"] <= str(day + timedelta(days=5))]
    if deadlines:
        body.append("<aside style='padding:16px;background:#fee8e5'><strong>Fristen in den nächsten fünf Tagen</strong><ul>")
        body.extend(f"<li>{esc(j['deadline'])}: {esc(j['title'])}, {esc(j['employer'])}; Bewerbungsstatus unbekannt</li>" for j in deadlines)
        body.append("</ul></aside>")
    for j in jobs:
        body.extend([
            f"<section style='border-top:1px solid #ddd;margin-top:24px;padding-top:16px'><h2>{esc(j['title'])}</h2>",
            f"<p>{esc(j['employer'])} · {esc(j['district'])} · <strong>{j['score']}/100</strong></p>",
            f"<p>{esc(j['commute'])} · {esc(j['hours'])} · {esc(j['contract'])} · {esc(j['salary'])}</p>",
            f"<p><strong>Dafür:</strong> {esc(j['pro'])}<br><strong>Dagegen:</strong> {esc(j['con'])}</p>",
            f"<p><strong>Aufwand:</strong> {j['effort_minutes']} Min. ({esc(j['effort_details'])})</p>",
            f"<p><a href='{esc(j['url'])}'>Anzeige öffnen →</a></p></section>",
        ])
    if not jobs:
        body.append("<p>Keine neuen, unabhängig verifizierten Treffer ab 55 Punkten. Das ist keine Aussage über den gesamten Stellenmarkt.</p>")
    body.append(f"<p><strong>Vorschlag für heute:</strong> {esc(action)}</p>")
    if weekly:
        monday = day - timedelta(days=4)
        week_jobs = [j for j in known if str(monday) <= j["date"] <= str(day)]
        bins = {"55–69": 0, "70–84": 0, "85–100": 0, "unter 55 (Althistorie)": 0, "unbekannt (Althistorie)": 0}
        for j in week_jobs:
            score = j.get("score")
            key = ("unbekannt (Althistorie)" if score is None else "85–100" if score >= 85
                   else "70–84" if score >= 70 else "55–69" if score >= 55 else "unter 55 (Althistorie)")
            bins[key] += 1
        body.append(f"<h2>Wochenüberblick</h2><p>{len(week_jobs)} gemeldete Treffer einschließlich dieses Berichts.</p><ul>")
        body.extend(f"<li>{esc(k)} Punkte: {v}</li>" for k, v in bins.items())
        body.append("</ul><p>Bewerbungsstatus: nicht erfasst.</p><h3>Fristen der nächsten 14 Tage</h3><ul>")
        future = [j for j in known if j.get("deadline") and str(day) <= j["deadline"] <= str(day + timedelta(days=14))]
        body.extend(f"<li>{esc(j['deadline'])}: {esc(j['title'])}, {esc(j['employer'])}</li>" for j in future)
        if not future:
            body.append("<li>Keine bestätigten Fristen in der gespeicherten Historie.</li>")
        body.append("</ul><h3>Marktbeobachtung</h3><ul>")
        body.extend(f"<li>Familie {k}: {esc(v)}</li>" for k, v in report["family_observations"].items())
        body.append(f"</ul><p><strong>Wochenende:</strong> {esc(report['weekend_action'])}</p>")
    body.append("<h3>Geprüfte Suchbegriffe</h3>")
    body.extend(f"<p>{family}: {esc(', '.join(terms))}</p>" for family, terms in report["searches"].items())
    body.append("<p style='font-size:12px;color:#555'>Angaben vor einer Bewerbung in der Originalanzeige prüfen. Pendelzeiten sind Schätzungen.</p></body></html>")
    return subject, "\n".join(body)


def mail_settings():
    required = ["GMAIL_USER", "GMAIL_APP_PASSWORD", "MAIL_TO"]
    missing = [k for k in required if not os.environ.get(k, "").strip()]
    if missing:
        raise RuntimeError("Missing secure mail configuration: " + ", ".join(missing))
    settings = {k: os.environ.get(k, "").strip() for k in required + ["MAIL_CC"]}
    # Google's display groups app passwords using spaces, sometimes nonbreaking.
    settings["GMAIL_APP_PASSWORD"] = "".join(settings["GMAIL_APP_PASSWORD"].split())
    for key in ["GMAIL_USER", "MAIL_TO", "MAIL_CC"]:
        if "\r" in settings[key] or "\n" in settings[key]:
            raise RuntimeError("Mail addresses must not contain line breaks")
    for address in [settings["GMAIL_USER"]] + settings["MAIL_TO"].split(",") + settings["MAIL_CC"].split(","):
        if address.strip() and ("@" not in address or " " in address.strip()):
            raise RuntimeError("Use plain email addresses in mail configuration")
    return settings


def make_message(record, body, settings):
    message = EmailMessage()
    message["From"], message["To"] = settings["GMAIL_USER"], settings["MAIL_TO"]
    if settings["MAIL_CC"]:
        message["Cc"] = settings["MAIL_CC"]
    message["Subject"], message["Message-ID"] = record["subject"], record["message_id"]
    message.set_content("Der Stellenbericht liegt als HTML-Version vor. Bitte einen HTML-fähigen E-Mail-Client verwenden.")
    message.add_alternative(body, subtype="html")
    return message


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


def reconcile(store, day, result, note, persist=None):
    """Explicit operator decision after checking Gmail's stable Message-ID."""
    day = date.fromisoformat(day).isoformat()
    if result not in ["sent", "not-sent"] or not note.strip():
        raise ValueError("Reconciliation requires sent/not-sent and an evidence note")
    persist = persist or (lambda record: None)
    with run_lock(store):
        path = store / "runs" / f"{day}.json"
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
        researcher=research, verifier=verify_job, sender=send_smtp, persist=None):
    now = now or datetime.now(timezone.utc)
    config = read_json(root / "config/search.json", {})
    if not dry_run and fixture is not None:
        raise ValueError("Fixture data cannot be emailed")
    day = now.astimezone(ZoneInfo(config["timezone"])).date()
    if not due(now, config) and fixture is None:
        return {"date": str(day), "status": "skipped", "reason": "Outside weekday Berlin delivery window"}
    if not dry_run and persist is None:
        # Local server mode must use a durable mounted directory, documented separately.
        persist = lambda record: None
    with run_lock(store):
        history = load_history(root, store)
        exclusion_history = history + reserved_jobs(store)
        path = store / "runs" / f"{day}.json"
        record = read_json(path, {})
        if not dry_run and record.get("status") in TERMINAL:
            return {"date": str(day), "status": "skipped", "delivery_status": record["status"],
                    "reason": "Already sent or requires manual delivery reconciliation"}
        settings = mail_settings() if not dry_run else None
        if dry_run or not record.get("report"):
            try:
                raw = validate_report(copy.deepcopy(fixture)) if fixture is not None else researcher(root, config, exclusion_history, day)
                verifier_fn = (lambda job, config: (True, "Fixture only; not live verified")) if fixture is not None else verifier
                jobs, rejected = select_jobs(raw, config, exclusion_history, day, verifier_fn)
                report = dict(raw, jobs=jobs, rejected=rejected)
                subject, body = render_report(report, day, history, fixture is not None)
            except Exception as error:
                if not dry_run:
                    failure = {"date": str(day), "status": "research_failed", "hits": 0,
                               "failed_at": now.isoformat(), "error": f"Research failed ({type(error).__name__}); see workflow log"}
                    if isinstance(error, ResearchError):
                        failure["research_diagnostic"] = error.diagnostic
                    atomic_json(path, failure)
                    persist(failure)
                raise
            record = {"date": str(day), "created_at": now.isoformat(), "status": "prepared", "subject": subject,
                      "report": report, "attempts": [], "hits": len(jobs),
                      "message_id": f"<miki-{day}-{hashlib.sha256(body.encode()).hexdigest()[:16]}@miki-jobsearch>"}
        else:
            # Reuse the live research but apply current rendering to an unsent outbox.
            # Terminal delivery states were already blocked above.
            record["subject"], body = render_report(record["report"], day, history)
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
        record.pop("error", None)
        atomic_json(path, record)
        persist(record)  # If this fails, SMTP must not be called.
        def on_sending():
            record["status"] = "sending"
            record["attempts"].append({"started_at": datetime.now(timezone.utc).isoformat(), "status": "sending"})
            atomic_json(path, record)
            persist(record)  # Durable ambiguity marker exists before any DATA command.
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
        record["accepted_at"] = datetime.now(timezone.utc).isoformat()
        record["attempts"][-1]["status"] = record["status"]
        record_delivered_jobs(store, record)
        atomic_json(path, record)
        persist(record)
        return {"date": str(day), "status": record["status"], "hits": record["hits"], "message_id": record["message_id"]}
