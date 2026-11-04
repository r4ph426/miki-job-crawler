import copy
import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

from miki_jobsearch.hybrid import execute, readiness
from miki_jobsearch.recovery import prewarm_release, recover_day
from miki_jobsearch.scheduled_mail import ProviderStatusError, recipient_identity
from miki_jobsearch.service import StateSyncError, atomic_json, read_json
from miki_jobsearch.switch_delivery import switch
from tests.test_scheduled_mail import ENV

DAY='2026-11-05'
BATCH='da151f0c-a188-403f-b7ce-ff66db580e8c'


class DirectDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.path=self.root/'state/runs'/f'{DAY}.json'
        atomic_json(self.root/'config/search.json', {'delivery_mode':'prepared_immediate'})
        self.record={'date':DAY,'status':'prepared','report':{'jobs':[]},'hits':0,'attempts':[]}
        atomic_json(self.path,self.record);self.path.with_suffix('.html').write_text('Live saved report')
        self.now=datetime.fromisoformat('2026-11-05T08:00:00+01:00')

    def test_future_prepared_report_is_ready_without_scheduling_post(self):
        self.assertTrue(readiness(self.root,DAY)['ready'])
        runner=Mock();provider=Mock()
        result=recover_day(self.root,DAY,clock=lambda:self.now,executor=runner,provider=provider)
        self.assertEqual(result['status'],'prepared');runner.assert_not_called();provider.assert_not_called()

    def test_only_saved_report_is_released_at_0830_and_uses_durable_executor(self):
        runner=Mock(return_value={'status':'sent'})
        recover_day(self.root,DAY,clock=lambda:self.now.replace(minute=30),executor=runner)
        self.assertEqual(runner.call_args.kwargs['phase'],'deliver')
        self.assertTrue(runner.call_args.kwargs['recovery'])
        runner.assert_called_once()

    def test_prewarm_waits_until_0830_in_summer_and_winter_then_refreshes_state(self):
        for day,stamp in [('2026-11-05','2026-11-05T08:00:00+01:00'),('2026-10-08','2026-10-08T08:13:00+02:00')]:
            path=self.root/'state/runs'/f'{day}.json'
            atomic_json(path,dict(self.record,date=day));path.with_suffix('.html').write_text('report')
            current=[datetime.fromisoformat(stamp)]
            def sleep(seconds):
                self.assertLessEqual(seconds,30)
                current[0]+=timedelta(seconds=seconds)
            sync=Mock()
            self.assertTrue(prewarm_release(self.root,clock=lambda:current[0],sleeper=sleep,sync=sync))
            self.assertEqual((current[0].hour,current[0].minute),(8,30));sync.assert_called_once()

    def test_prewarm_never_waits_for_fixture_missing_report_or_existing_provider_queue(self):
        for change in ({'fixture':True},{'report':None},{'scheduled_at':DAY+'T07:30:00Z'}, {'provider_message_id':'old'}, {'status':'sent'}):
            atomic_json(self.path,dict(self.record,**change));sleep=Mock();sync=Mock()
            self.assertFalse(prewarm_release(self.root,clock=lambda:self.now,sleeper=sleep,sync=sync))
            sleep.assert_not_called();sync.assert_not_called()

    def test_direct_preparation_never_calls_provider_scheduler(self):
        atomic_json(self.root/'state/coordinator.json',{'lease':{'token':'lease','expires_at':'2099-01-01T00:00:00+00:00'}})
        scheduler=Mock()
        with patch('miki_jobsearch.hybrid.claim',return_value='lease'),patch('miki_jobsearch.hybrid.complete'),patch('miki_jobsearch.hybrid.git_persister',return_value=Mock()):
            result=execute(self.root,'manual',phase='prepare',day=DAY,now=self.now,scheduler=scheduler)
        self.assertEqual(result['status'],'prepared');scheduler.assert_not_called()

    def queued(self):
        self.record.update(status='scheduled',scheduled_at=DAY+'T07:30:00Z',
                           recipient_set_id=recipient_identity(ENV),provider_batch_id=BATCH,
                           provider_message_id='<old@smtp-relay.mailin.fr>')
        atomic_json(self.path,self.record)

    def test_confirmed_cancellation_is_saved_before_old_ids_are_removed(self):
        self.queued();seen=[]
        def save(record): seen.append(copy.deepcopy(record))
        with patch.dict('os.environ',ENV,clear=True):
            result=switch(self.root,DAY,BATCH,now=self.now-timedelta(days=1),persist=save,
                          canceller=Mock(return_value=204),getter=lambda *a:{'status':'queued','scheduledAt':DAY+'T07:30:00Z'})
        self.assertEqual(result['cancel_http_status'],204)
        self.assertEqual(seen[1]['delivery_switch']['http_status'],204)
        self.assertIn('provider_message_id',seen[1]);self.assertNotIn('provider_message_id',seen[2])
        self.assertEqual(seen[2]['status'],'prepared')
        self.assertEqual(read_json(self.root/'state/schedule-history'/f'{DAY}--{BATCH}.json',{})['status'],'scheduled')

    def test_unknown_cancel_or_processing_queue_never_enables_direct_send(self):
        for response in (404,200,None,ProviderStatusError('timeout')):
            self.queued();cancel=Mock(side_effect=response) if isinstance(response,Exception) else Mock(return_value=response)
            with patch.dict('os.environ',ENV,clear=True),self.assertRaises(ProviderStatusError):
                switch(self.root,DAY,BATCH,now=self.now-timedelta(days=1),persist=Mock(),canceller=cancel,
                       getter=lambda *a:{'status':'queued','scheduledAt':DAY+'T07:30:00Z'})
            self.assertEqual(read_json(self.path,{})['status'],'scheduled')
        self.queued();cancel=Mock()
        with patch.dict('os.environ',ENV,clear=True),self.assertRaises(ProviderStatusError):
            switch(self.root,DAY,BATCH,now=self.now-timedelta(days=1),persist=Mock(),canceller=cancel,
                   getter=lambda *a:{'status':'processed','scheduledAt':DAY+'T07:30:00Z'})
        cancel.assert_not_called()

    def test_persistence_failure_prevents_cancellation_or_outbox_release(self):
        for at in (1,2,3):
            self.queued();cancel=Mock(return_value=204)
            with patch.dict('os.environ',ENV,clear=True),self.assertRaises(StateSyncError):
                switch(self.root,DAY,BATCH,now=self.now-timedelta(days=1),persist=Mock(side_effect=[None]*(at-1)+[StateSyncError('no push')]),
                       canceller=cancel,getter=lambda *a:{'status':'queued','scheduledAt':DAY+'T07:30:00Z'})
            self.assertEqual(cancel.call_count,0 if at==1 else 1)
