import copy
import io
import os
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import Mock, patch

from miki_jobsearch.repair_schedule import cancel, repair
from miki_jobsearch.scheduled_mail import ProviderStatusError, recipient_identity
from miki_jobsearch.service import StateSyncError, atomic_json, read_json
from tests.test_scheduled_mail import DAY, ENV, NOW

BATCH = '7f5ab5de-1363-459e-82ce-9e1fd965099e'


class RepairScheduleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / 'state/runs' / f'{DAY}.json'
        self.record = {'date': DAY, 'status': 'schedule_uncertain', 'report': {'jobs': []},
            'subject': 'Saved mail', 'provider_batch_id': BATCH,
            'recipient_set_id': recipient_identity(ENV), 'attempts': [{'status': 'schedule_uncertain'}]}
        atomic_json(self.path, self.record)
        self.path.with_suffix('.html').write_text('<p>Saved exact content</p>')
        env = patch.dict(os.environ, ENV, clear=True); env.start(); self.addCleanup(env.stop)
        self.canceller = Mock(return_value=204)
        self.scheduler = Mock(return_value={'date': DAY, 'status': 'scheduled'})
        self.persist = Mock()

    def repair(self, **kwargs):
        return repair(self.root, DAY, BATCH, now=NOW, persist=kwargs.pop('persist', self.persist),
                      canceller=self.canceller, scheduler=self.scheduler, **kwargs)

    def test_only_confirmed_cancellation_enables_replacement_and_preserves_original(self):
        def cancellation(identifier, settings):
            self.assertEqual(identifier, BATCH)
            self.assertEqual(read_json(self.path, {})['schedule_repair']['status'], 'cancellation_requested')
            self.assertGreater(self.persist.call_count, 0)
            return 204
        self.canceller.side_effect = cancellation
        self.repair()
        self.scheduler.assert_called_once()
        current = read_json(self.path, {})
        self.assertEqual(current['status'], 'prepared')
        self.assertNotEqual(current['provider_batch_id'], BATCH)
        self.assertEqual(current['schedule_repair']['http_status'], 204)
        archived = read_json(self.root / 'state/schedule-history' / f'{DAY}--{BATCH}.json', {})
        self.assertEqual(archived['attempts'], self.record['attempts'])
        self.assertEqual(archived['status'], 'schedule_uncertain')
        self.assertEqual(archived['schedule_repair']['status'], 'cancelled')
        self.assertEqual(self.persist.call_count, 3)

    def test_404_timeout_and_unexpected_response_never_reset_or_schedule(self):
        for response in (ProviderStatusError('HTTP 404'), ProviderStatusError('timeout'), 200, None):
            atomic_json(self.path, self.record)
            self.canceller.reset_mock(side_effect=True)
            if isinstance(response, Exception): self.canceller.side_effect = response
            else: self.canceller.return_value = response
            with self.assertRaises(ProviderStatusError): self.repair()
            self.scheduler.assert_not_called()
            current = read_json(self.path, {})
            self.assertEqual(current['status'], 'schedule_uncertain')
            self.assertEqual(current['provider_batch_id'], BATCH)
            self.assertEqual(current['schedule_repair']['status'], 'cancellation_unconfirmed')

    def test_failed_checkpoint_before_cancel_or_after_receipt_never_schedules(self):
        for failure_at in (1, 2, 3):
            atomic_json(self.path, self.record); self.canceller.reset_mock()
            outcomes = [None] * (failure_at - 1) + [StateSyncError('push failed')]
            with self.assertRaises(StateSyncError): self.repair(persist=Mock(side_effect=outcomes))
            self.scheduler.assert_not_called()
            self.assertEqual(self.canceller.call_count, 0 if failure_at == 1 else 1)

    def test_repair_cannot_be_repeated_after_uncertain_cancellation(self):
        self.canceller.side_effect = ProviderStatusError('HTTP 404')
        with self.assertRaises(ProviderStatusError): self.repair()
        self.canceller.reset_mock()
        with self.assertRaises(ValueError): self.repair()
        self.canceller.assert_not_called(); self.scheduler.assert_not_called()

    def test_wrong_identity_sent_fixture_missing_report_and_wrong_batch_are_rejected(self):
        variants = ({'provider_batch_id': 'other'}, {'recipient_set_id': 'other'},
            {'accepted_at': 'already'}, {'delivered_at': 'already'}, {'sent_recipient_count': 1},
            {'fixture': True}, {'status': 'sending'}, {'report': None})
        for changes in variants:
            record = copy.deepcopy(self.record); record.update(changes); atomic_json(self.path, record)
            with self.assertRaises(ValueError): self.repair()
        self.canceller.assert_not_called(); self.scheduler.assert_not_called()

    def test_cancel_uses_exact_uuid_delete_and_only_accepts_204(self):
        response = Mock(); response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False); response.status = 204
        opener = Mock(); opener.open.return_value = response
        with patch('miki_jobsearch.repair_schedule.urllib.request.build_opener', return_value=opener):
            self.assertEqual(cancel(BATCH, ENV), 204)
            request = opener.open.call_args.args[0]
            self.assertEqual(request.method, 'DELETE')
            self.assertEqual(request.full_url, 'https://api.brevo.com/v3/smtp/email/' + BATCH)
            response.status = 200
            with self.assertRaises(ProviderStatusError): cancel(BATCH, ENV)
            opener.open.side_effect = urllib.error.HTTPError(request.full_url, 404, 'private', {}, io.BytesIO(b'private'))
            with self.assertRaisesRegex(ProviderStatusError, 'HTTP 404') as caught: cancel(BATCH, ENV)
            self.assertNotIn('private', str(caught.exception))


if __name__ == '__main__':
    unittest.main()
