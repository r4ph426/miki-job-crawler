"""Minimal read-only public operational feed; never includes reports or addresses."""
from datetime import datetime, timezone


def write_feed(root, store):
    from .service import atomic_json, read_json
    records = {}
    for path in sorted((store / "runs").glob('*.json'))[-30:]:
        r = read_json(path, {})
        if "--" in path.stem:
            continue
        records[r.get("date", path.stem)] = {k: r.get(k) for k in
            ("date", "status", "hits", "researched_at", "prepared_at", "created_at", "accepted_at",
             "scheduled_at", "schedule_accepted_at", "schedule_verified_at", "provider_status",
             "provider_checked_at", "expected_recipients", "sent_recipient_count", "delivered_recipient_count", "delivered_at")}
        records[r.get("date", path.stem)]["has_report"] = bool(r.get("report")) and not r.get("fixture")
        records[r.get("date", path.stem)]["has_mail"] = path.with_suffix(".html").exists() and not r.get("fixture")
    shared = read_json(store / "coordinator.json", {})
    payload = {"updated_at": datetime.now(timezone.utc).isoformat(), "timezone": "Europe/Berlin",
               "schedule": {"mode": "brevo_scheduled", "prepare": "09:00", "prepare_retry": "13:00",
                            "check": "16:00", "send": "08:30", "provider_poll": "08:37",
                            "delivery_check": "08:40", "prepare_days": "Sunday–Thursday",
                            "send_days": "Monday–Friday"},
               "records": records,
               "lease": ({k: shared["lease"].get(k) for k in ("actor", "phase", "date", "started_at", "expires_at")}
                         if shared.get("lease") else None),
               "confirmations": shared.get("days", {}),
               "laptop": {"status": "disabled"},
               "delivery_alerts": {p.stem: {k: read_json(p, {}).get(k) for k in ("status", "checked_at", "accepted_at")}
                                   for p in sorted((store / "delivery-alerts").glob("*.json"))[-14:]},
               "alerts": {p.stem: {k: read_json(p, {}).get(k) for k in ("status", "checked_at", "accepted_at")}
                          for p in sorted((store / "alerts").glob("*.json"))[-14:]}}
    atomic_json(store / "status-feed.json", payload)
    return payload
