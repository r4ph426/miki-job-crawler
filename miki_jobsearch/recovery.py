"""Reconcile desired delivery state on every wakeup; never blindly repeat a POST."""
import argparse
import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .hybrid import check_provider, execute
from .research import API_ACTION_REQUIRED_CODES, ResearchError
from .scheduled_mail import ProviderStatusError, send_time
from .service import BERLIN, DeliveryFailure, MailConfigurationError, StateSyncError, read_json

MAX_ATTEMPTS_PER_PHASE_PER_WINDOW = 3
RETRY_SECONDS = 60
PROVIDER_STATES = {"sent", "scheduled", "scheduling", "schedule_uncertain", "sending", "uncertain", "partial"}
RETRY_STATES = {"missing", "research_failed", "configuration_failed", "failed", "prepared", "schedule_failed"}
ATTENTION_STATES = {"research_failed", "configuration_failed", "failed", "schedule_failed",
                    "retry_budget_exhausted", "provider_unconfirmed", "delivery_problem",
                    "manual_review_required", "schedule_uncertain", "scheduling", "sending",
                    "uncertain", "partial", "research_access_blocked"}


def utcnow():
    return datetime.now(timezone.utc)


def targets(now):
    """Current day from 06:00; next weekday from 09:00, including Friday/weekends."""
    local = now.astimezone(BERLIN)
    result = []
    if local.weekday() < 5 and local.hour >= 6:
        result.append(local.date().isoformat())
    if local.hour >= 9:
        following = local.date() + timedelta(days=1)
        while following.weekday() >= 5:
            following += timedelta(days=1)
        if send_time(following.isoformat()) - now <= timedelta(hours=72):
            result.append(following.isoformat())
    return result


def retry_allowed(record, phase, now):
    attempts = [a for a in record.get("attempts", []) if a.get("phase") == phase]
    def window(when):
        local = when.astimezone(BERLIN)
        return local.date(), "morning" if local.hour < 9 else "day" if local.hour < 16 else "evening"
    current = [a for a in attempts if window(datetime.fromisoformat(a["started_at"])) == window(now)]
    if len(current) >= MAX_ATTEMPTS_PER_PHASE_PER_WINDOW:
        return False, "retry_budget_exhausted"
    if attempts and now - datetime.fromisoformat(attempts[-1]["started_at"]) < timedelta(seconds=RETRY_SECONDS):
        return False, "retry_cooldown"
    return True, "allowed"


def recover_day(root, day, clock=utcnow, executor=execute, provider=check_provider):
    """Research separately so crossing 08:30 during research selects immediate send."""
    path = root / "state/runs" / f"{day}.json"
    for _ in range(3):  # research -> saved HTML -> queue/send, each under its own durable lease
        now = clock()
        record = read_json(path, {})
        status = record.get("status", "missing")
        if status in PROVIDER_STATES:
            if record.get("delivered_at"):
                return {"date": day, "status": "delivered"}
            # An unchanged ambiguous acknowledgement cannot be repaired by another
            # POST. Poll hourly ahead of delivery, then every 15 minutes; always
            # recheck when the actual scheduled send time arrives.
            if record.get("provider_check_error") and record.get("provider_checked_at"):
                checked = datetime.fromisoformat(record["provider_checked_at"])
                due = send_time(day)
                after = checked + timedelta(minutes=60 if now < due else 15)
                if now < due:
                    after = min(after, due)
                elif checked < due:
                    after = due
                if now < after:
                    return {"date": day, "status": "provider_unconfirmed", "retryable": False,
                            "next_check_at": after.isoformat()}
            try:
                result = provider(root, day, now=now)
            except (ProviderStatusError, MailConfigurationError):
                return {"date": day, "status": "provider_unconfirmed", "retryable": now >= send_time(day)}
            current = read_json(path, {})
            if current.get("delivered_at"):
                return {"date": day, "status": "delivered"}
            if current.get("delivery_problem"):
                return {"date": day, "status": "delivery_problem", "retryable": False}
            if result.get("status") == "deferred":
                return result
            if now >= send_time(day) and current.get("status", status) == "sent":
                return {"date": day, "status": "awaiting_delivery", "retryable": True}
            return {"date": day, "status": current.get("status", status), "retryable": False}
        if status not in RETRY_STATES or record.get("fixture"):
            return {"date": day, "status": "manual_review_required", "retryable": False}
        saved = bool(record.get("report")) and path.with_suffix(".html").exists()
        if not saved:
            # Account/credit failures need user action. Check at most hourly,
            # but allow an explicit successful key check to unblock immediately.
            diagnostic = record.get('research_diagnostic', {})
            if record.get('failed_at'):
                failed = datetime.fromisoformat(record['failed_at'])
                access = read_json(root / 'state/api-check.json', {})
                restored = (access.get('status') == 'ready' and access.get('checked_at')
                            and datetime.fromisoformat(access['checked_at']) > failed)
                seconds = diagnostic.get('retry_after_seconds', 0)
                if diagnostic.get('error_code') in API_ACTION_REQUIRED_CODES and not restored:
                    seconds = max(seconds, 3600)
                if now < failed + timedelta(seconds=seconds):
                    return {'date': day, 'status': 'research_access_blocked' if diagnostic.get('error_code') in API_ACTION_REQUIRED_CODES else 'retry_cooldown',
                            'error_code': diagnostic.get('error_code'), 'retryable': False,
                            'next_check_at': (failed + timedelta(seconds=seconds)).isoformat()}
            phase = "research"
        elif now >= send_time(day):
            # Never release a previous/future day's report or send before 08:30 Berlin.
            if day != now.astimezone(BERLIN).date().isoformat():
                return {"date": day, "status": "manual_review_required", "retryable": False}
            phase = "deliver"
        elif send_time(day) - now >= timedelta(minutes=5):
            phase = "prepare"
        else:
            return {"date": day, "status": "awaiting_send_time", "retryable": True}
        ledger = read_json(root / "state/recovery" / f"{day}.json", {})
        allowed, reason = retry_allowed(ledger, phase, now)
        if not allowed:
            return {"date": day, "status": reason, "retryable": reason == "retry_cooldown"}
        try:
            result = executor(root, "github", now=now, phase=phase, day=day, recovery=True)
        except StateSyncError:
            raise  # A failed shared checkpoint must halt all actions on this checkout.
        except (ResearchError, DeliveryFailure, MailConfigurationError) as error:
            current = read_json(path, {})
            diagnostic = current.get('research_diagnostic', {})
            action_required = diagnostic.get('error_code') in API_ACTION_REQUIRED_CODES
            return {"date": day, "status": 'research_access_blocked' if action_required else current.get("status", "failed"),
                    "error_type": type(error).__name__, 'error_code': diagnostic.get('error_code'),
                    "retryable": (not action_required and not diagnostic.get('retry_after_seconds')
                                  and current.get("status") not in PROVIDER_STATES)}
        if result.get("status") in {"deferred", "retry_budget_exhausted", "retry_cooldown"}:
            return dict(result, retryable=result["status"] != "retry_budget_exhausted")
        if phase == "deliver":
            return dict(result, retryable=True)  # Poll recipient events after a short propagation delay.
        if phase != "research":
            return dict(result, retryable=False)
        # Use a fresh wall clock after the potentially long research call.
    return {"date": day, "status": "retry_later", "retryable": True}


def supervise(root, clock=utcnow, recover=recover_day):
    now = clock()
    results = []
    for day in targets(now):
        results.append(recover(root, day, clock=clock))
    return results


def needs_attention(results, now):
    local = now.astimezone(BERLIN)
    return any(r.get("status") in ATTENTION_STATES or (
        r.get("status") == "awaiting_delivery" and (local.hour, local.minute) >= (8, 45)) for r in results)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--attempts", type=int, choices=range(1, 4), default=3)
    args = parser.parse_args()
    root = Path(__file__).resolve().parent.parent
    os.environ["STATE_BRANCH"] = "main"
    for attempt in range(args.attempts):
        results = supervise(root)
        print(json.dumps({"attempt": attempt + 1, "results": results}), flush=True)
        if not any(r.get("retryable") for r in results) or attempt == args.attempts - 1:
            break
        time.sleep(RETRY_SECONDS)
    # Warn only after attempting repair, once per report/day. No alerts for healthy state.
    from .alerts import check_delivery, check_readiness
    now = utcnow()
    local = now.astimezone(BERLIN)
    for result in results:
        if result["date"] == local.date().isoformat() and (local.hour, local.minute) >= (8, 45):
            check_delivery(root, result["date"], now=now)
        elif local.hour >= 22:
            check_readiness(root, result["date"], now=now)
    # Successfully assessing a known delivery incident is not a crashed workflow.
    # Keep the incident explicit in output/feed and use the deduplicated warning
    # above. Genuine execution/state-persistence exceptions still exit nonzero.
    print(json.dumps({"operation": "recovery_check", "needs_attention": needs_attention(results, now),
                      "results": results}), flush=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (StateSyncError, ResearchError, DeliveryFailure, MailConfigurationError, ProviderStatusError) as error:
        print(json.dumps({"status": "failed", "error_type": type(error).__name__}), flush=True)
        raise SystemExit(1)
