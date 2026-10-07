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
            ("date", "status", "hits", "researched_at", "created_at", "accepted_at")}
        records[r.get("date", path.stem)]["has_report"] = bool(r.get("report")) and not r.get("fixture")
    shared = read_json(store / "coordinator.json", {})
    payload = {"updated_at": datetime.now(timezone.utc).isoformat(), "timezone": "Europe/Berlin",
               "schedule": {"prepare": "16:00", "send": "08:30", "local_retry": "09:30", "github_fallback": "09:45"},
               "records": records,
               "lease": ({k: shared["lease"].get(k) for k in ("actor", "phase", "date", "started_at", "expires_at")}
                         if shared.get("lease") else None),
               "confirmations": shared.get("days", {}),
               "laptop": read_json(store / "laptop-health.json", {"status": "credentials_required"})}
    atomic_json(store / "status-feed.json", payload)
    return payload
