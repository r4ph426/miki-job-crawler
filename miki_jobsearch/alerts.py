"""One recipient-only readiness warning per delivery day; no job-history writes."""
import os
from datetime import datetime, timezone
from email.message import EmailMessage

from .hybrid import claim, complete, readiness
from .service import (DeliveryFailure, MailConfigurationError, StateSyncError, TERMINAL,
                      atomic_json, git_persister, mail_settings, read_json, send_mail)

STATUS_URL = 'https://miki-crawler-status.raphael-regli.chatgpt.site'
WORKFLOW_URL = 'https://github.com/r4ph426/miki-job-crawler/actions/workflows/daily.yml'


def check_readiness(root, day, now=None, sender=send_mail):
    return _check_warning(root, day, 'readiness', now, sender)


def check_delivery(root, day, now=None, sender=send_mail):
    return _check_warning(root, day, 'delivery', now, sender)


def _check_warning(root, day, kind, now=None, sender=send_mail):
    now = now or datetime.now(timezone.utc)
    store = root / 'state'
    if kind == 'delivery':
        delivery = read_json(store / 'runs' / f'{day}.json', {})
        delivered = (delivery.get('date') == day and delivery.get('status') == 'sent'
                     and bool(delivery.get('accepted_at')) and not delivery.get('fixture'))
        checked = {'date': day, 'ready': delivered, 'checked_at': now.isoformat()}
        quiet_status = 'delivered'
    else:
        checked = dict(readiness(root, day), checked_at=now.isoformat())
        quiet_status = 'ready'
    if checked['ready']:
        return dict(checked, status=quiet_status)
    path = store / ('delivery-alerts' if kind == 'delivery' else 'alerts') / f'{day}.json'
    existing = read_json(path, {})
    if existing.get('status') in TERMINAL:
        return {'date': day, 'status': 'already_alerted', 'warning_status': existing['status'], 'ready': False}
    token = claim(root, now, 'github', kind + '-check', day)
    if token is None:
        return {'date': day, 'status': 'deferred', 'ready': False,
                'reason': 'Another worker is active; retry the warning check'}
    save = git_persister(root, store)
    def persist(record):
        lease = read_json(store / 'coordinator.json', {}).get('lease', {})
        if lease.get('token') != token or datetime.now(timezone.utc) >= datetime.fromisoformat(lease['expires_at']):
            raise StateSyncError('Warning lease expired')
        atomic_json(path, record)
        save(record)
    record = {'date': day, 'kind': kind + '_warning', 'status': 'prepared',
              'checked_at': now.isoformat(), 'message_id': f'<miki-{kind}-{day}@miki-jobsearch>'}
    try:
        # Explicitly configured warning recipient only; never inherit job To/CC.
        env = dict(os.environ, MAIL_TO=os.environ.get('MAIL_ALERT_TO', ''), MAIL_CC='')
        settings = mail_settings(env)
        message = EmailMessage()
        message['From'], message['To'] = settings['MAIL_FROM'], settings['MAIL_TO']
        message['Message-ID'] = record['message_id']
        if kind == 'delivery':
            message['Subject'] = f'Miki: Versand für {day} ab 10 Uhr nicht bestätigt'
            text = (f'Für die Stellenmail vom {day} ist ab 10:00 Uhr Berliner Zeit kein vollständiger '
                    'Versand durch den Mailanbieter bestätigt. Der Versandstatus fehlt, ist fehlgeschlagen '
                    'oder noch unklar.\n\nBitte Status und GitHub-Läufe prüfen. Bei sending, uncertain oder '
                    'partial zuerst den Anbieterstatus abgleichen; nicht blind erneut senden.\n\n'
                    f'Status: {STATUS_URL}\nWorkflow: {WORKFLOW_URL}\n')
            html = (f'<p>Der Versand der Stellenmail vom <strong>{day}</strong> ist ab 10:00 Uhr '
                    'Berliner Zeit noch nicht vollständig bestätigt.</p><p>Bitte Status und GitHub-Läufe '
                    'prüfen. Bei unklarem oder teilweisem Versand zuerst den Anbieterstatus abgleichen.</p>')
        else:
            message['Subject'] = f'Miki: Bericht für {day} noch nicht bereit'
            text = (f'Für den Versand am {day} um 08:30 Uhr Berliner Zeit liegt noch kein fertiger Bericht vor.\n\n'
                    'Bitte die Recherche manuell starten: Im GitHub-Workflow den Modus prepare-next wählen. '
                    'Das bereitet den nächsten Versandtag vor und versendet jetzt keine Stellenmail.\n\n'
                    f'Status: {STATUS_URL}\nWorkflow: {WORKFLOW_URL}\n')
            html = (f'<p>Für den Versand am <strong>{day} um 08:30 Uhr</strong> liegt noch kein fertiger '
                    'Bericht vor.</p><p>Bitte im GitHub-Workflow <strong>prepare-next</strong> manuell starten. '
                    'Dabei wird jetzt keine Stellenmail versendet.</p>')
        message.set_content(text)
        message.add_alternative(html + f'<p><a href="{STATUS_URL}">Status prüfen</a> · '
                                f'<a href="{WORKFLOW_URL}">GitHub-Läufe öffnen</a></p>', subtype='html')
        persist(record)
        def on_sending():
            record.update(status='sending', started_at=datetime.now(timezone.utc).isoformat())
            persist(record)
        outcome = sender(message, settings, on_sending)
        record.update(status=outcome['status'], accepted_at=datetime.now(timezone.utc).isoformat())
        if outcome.get('provider_message_id'):
            record['provider_message_id'] = outcome['provider_message_id']
        persist(record)
        result = {'date': day, 'status': 'warning_sent', 'warning_status': record['status'], 'ready': False}
        complete(root, token, day, result)
        return result
    except StateSyncError:
        raise
    except Exception as error:
        if isinstance(error, DeliveryFailure):
            record['status'] = 'uncertain' if error.ambiguous else 'failed'
        elif record.get('status') != 'sending':
            record['status'] = 'configuration_failed' if isinstance(error, MailConfigurationError) else 'failed'
        # Never persist response bodies, recipient addresses, or secrets.
        record['error_type'] = type(error).__name__
        persist(record)
        complete(root, token, day, {'status': record['status']})
        raise
