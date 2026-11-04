"""Move one verified future Brevo queue to the direct outbox, only after HTTP 204."""
import argparse
import copy
import json
import os
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .hybrid import claim, complete
from .repair_schedule import cancel
from .scheduled_mail import ProviderStatusError, message_identifiers, queue_status, recipient_identity, send_time
from .service import StateSyncError, atomic_json, git_persister, mail_settings, read_json


def switch(root, day, expected_batch, now=None, persist=None, canceller=cancel, getter=None, cancel_message=False):
    now = now or datetime.now(timezone.utc)
    if uuid.UUID(expected_batch).version != 4 or send_time(day) - now <= timedelta(minutes=5):
        raise ValueError('Switch requires exact UUIDv4 and a future send more than five minutes away')
    path = root / 'state/runs' / f'{day}.json'
    record = read_json(path, {})
    settings = mail_settings()
    if (record.get('date') != day or record.get('status') != 'scheduled'
            or record.get('provider_batch_id') != expected_batch
            or record.get('accepted_at') or record.get('delivered_at') or record.get('fixture')
            or record.get('sent_recipient_count', 0) or record.get('delivered_recipient_count', 0)
            or not record.get('report') or not path.with_suffix('.html').exists()
            or record.get('recipient_set_id') != recipient_identity(settings)
            or settings['EMAIL_PROVIDER'] != 'brevo'):
        raise ValueError('Exact unsent queue, recipients and saved report must match; otherwise stop')
    previous = record.get('delivery_switch')
    identifiers = message_identifiers(record)
    if cancel_message:
        if (not previous or previous.get('status') != 'cancellation_unconfirmed'
                or previous.get('batch_id') != expected_batch or len(identifiers) != 1):
            raise ValueError('Message cancellation requires one exact message and an unresolved batch cancellation')
        identifier = identifiers[0]
    else:
        if previous:
            raise ValueError('Existing switch intent requires explicit inspection; cancellation blocked')
        identifier = expected_batch
    state = queue_status(record, settings, **({'getter': getter} if getter else {}))
    if state != 'queued':
        raise ProviderStatusError('Queue is already processing or unconfirmed; direct release blocked')
    save = persist or git_persister(root, root / 'state')
    original = copy.deepcopy(record)
    record['delivery_switch'] = {'status': 'cancellation_requested', 'batch_id': expected_batch,
                                'requested_at': now.isoformat(), 'identifier': identifier,
                                'previous_attempt': copy.deepcopy(previous)}
    atomic_json(path, record); save(record)
    try:
        if canceller(identifier, settings) != 204:
            raise ProviderStatusError('Cancellation unconfirmed; direct release blocked')
    except ProviderStatusError:
        record['delivery_switch']['status'] = 'cancellation_unconfirmed'
        atomic_json(path, record); save(record)
        raise
    record['delivery_switch'].update(status='cancelled', http_status=204,
                                    confirmed_at=datetime.now(timezone.utc).isoformat())
    atomic_json(path, record); save(record)
    original['delivery_switch'] = copy.deepcopy(record['delivery_switch'])
    archive = root / 'state/schedule-history' / f'{day}--{expected_batch}'
    atomic_json(archive.with_suffix('.json'), original)
    archive.with_suffix('.html').write_bytes(path.with_suffix('.html').read_bytes())
    for key in ('scheduled_at', 'provider_batch_id', 'provider_message_id', 'provider_message_ids',
                'provider_status', 'provider_checked_at', 'provider_check_error', 'schedule_accepted_at',
                'schedule_verified_at', 'schedule_recovered_at', 'provider_diagnostic', 'error'):
        record.pop(key, None)
    record.update(status='prepared', delivery_mode='prepared_immediate', planned_delivery_at=send_time(day).isoformat())
    atomic_json(path, record); save(record)
    return {'date': day, 'status': 'prepared', 'cancel_http_status': 204,
            'planned_delivery_at': record['planned_delivery_at']}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('day'); parser.add_argument('expected_batch')
    parser.add_argument('--cancel-message', action='store_true')
    args = parser.parse_args()
    root = Path(__file__).resolve().parent.parent
    os.environ['STATE_BRANCH'] = 'main'
    now = datetime.now(timezone.utc)
    token = claim(root, now, 'manual', 'switch-delivery', args.day)
    if token is None:
        raise StateSyncError('Another worker active; no cancellation attempted')
    save = git_persister(root, root / 'state')
    def persist(record):
        lease = read_json(root / 'state/coordinator.json', {}).get('lease', {})
        if lease.get('token') != token or datetime.now(timezone.utc) >= datetime.fromisoformat(lease['expires_at']):
            raise StateSyncError('Switch lease expired; stop')
        save(record)
    try:
        result = switch(root, args.day, args.expected_batch, now=now, persist=persist, cancel_message=args.cancel_message)
    except StateSyncError:
        raise
    except (ValueError, ProviderStatusError):
        complete(root, token, args.day, {'status': 'switch_blocked'})
        raise
    complete(root, token, args.day, result)
    print(json.dumps(result))


if __name__ == '__main__':
    main()
