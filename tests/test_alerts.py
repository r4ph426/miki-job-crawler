import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from miki_jobsearch.alerts import check_delivery, check_readiness
from miki_jobsearch.service import DeliveryFailure, MailConfigurationError, atomic_json


class AlertTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.day='2026-10-08'
        self.now=datetime.now(timezone.utc)
        atomic_json(self.root/'state/coordinator.json',{'lease':{'token':'token','expires_at':(self.now+timedelta(hours=1)).isoformat()}})
        self.claim=patch('miki_jobsearch.alerts.claim',return_value='token');self.claim.start();self.addCleanup(self.claim.stop)
        self.complete=patch('miki_jobsearch.alerts.complete');self.complete.start();self.addCleanup(self.complete.stop)
        self.save=Mock();self.persister=patch('miki_jobsearch.alerts.git_persister',return_value=self.save)
        self.persister.start();self.addCleanup(self.persister.stop)
        self.env=patch.dict(os.environ,{'EMAIL_PROVIDER':'brevo','BREVO_API_KEY':'test-only-key','MAIL_FROM':'sender@example.org',
            'MAIL_TO':'job-recipient@example.org','MAIL_CC':'job-cc@example.org','MAIL_ALERT_TO':'operator@example.org'})
        self.env.start();self.addCleanup(self.env.stop)

    def test_ready_report_is_silent_even_without_warning_credentials(self):
        atomic_json(self.root/'state/runs'/f'{self.day}.json',{'date':self.day,'status':'sent','accepted_at':self.now.isoformat(),'report':{'jobs':[]}})
        sender=Mock()
        with patch.dict(os.environ,{},clear=True):
            self.assertEqual(check_readiness(self.root,self.day,sender=sender)['status'],'ready')
        sender.assert_not_called();self.save.assert_not_called()

    def test_warning_goes_only_to_operator_and_persists_before_sending_once(self):
        def sender(message,settings,on_sending):
            self.assertEqual(message['To'],'operator@example.org');self.assertIsNone(message['Cc'])
            self.assertEqual(settings['MAIL_CC'],'');self.assertEqual(self.save.call_args.args[0]['status'],'prepared')
            on_sending();self.assertEqual(self.save.call_args.args[0]['status'],'sending')
            return {'status':'sent','refused_count':0,'provider_message_id':'test-acceptance'}
        self.assertEqual(check_readiness(self.root,self.day,sender=sender)['status'],'warning_sent')
        other=Mock();self.assertEqual(check_readiness(self.root,self.day,sender=other)['warning_status'],'sent');other.assert_not_called()
        text=(self.root/'state/alerts'/f'{self.day}.json').read_text()
        self.assertNotIn('operator@example.org',text);self.assertNotIn('test-only-key',text)
        self.assertFalse((self.root/'state/history.json').exists())

    def test_uncertain_warning_is_not_blindly_resent(self):
        def sender(message,settings,on_sending):
            on_sending();raise DeliveryFailure('Ambiguous test outcome',ambiguous=True)
        with self.assertRaises(DeliveryFailure):check_readiness(self.root,self.day,sender=sender)
        other=Mock();result=check_readiness(self.root,self.day,sender=other)
        self.assertEqual(result['warning_status'],'uncertain');other.assert_not_called()

    def test_missing_operator_setting_never_falls_back_to_job_recipient(self):
        sender=Mock()
        with patch.dict(os.environ,{'MAIL_ALERT_TO':''}):
            with self.assertRaises(MailConfigurationError):check_readiness(self.root,self.day,sender=sender)
        sender.assert_not_called()
        record=json.loads((self.root/'state/alerts'/f'{self.day}.json').read_text())
        self.assertEqual(record['status'],'configuration_failed')

    def test_confirmed_delivery_is_quiet_without_warning_credentials(self):
        atomic_json(self.root/'state/runs'/f'{self.day}.json',{'date':self.day,'status':'sent','accepted_at':self.now.isoformat()})
        sender=Mock()
        with patch.dict(os.environ,{},clear=True):
            self.assertEqual(check_delivery(self.root,self.day,sender=sender)['status'],'delivered')
        sender.assert_not_called()

    def test_prepared_report_does_not_count_as_delivery_and_warning_is_once(self):
        atomic_json(self.root/'state/runs'/f'{self.day}.json',{'date':self.day,'status':'prepared','report':{'jobs':[]}})
        def sender(message,settings,on_sending):
            self.assertEqual(message['To'],'operator@example.org')
            self.assertIsNone(message['Cc'])
            self.assertIn('08:40 Uhr',message['Subject'])
            self.assertNotIn('prepare-next',message.get_body(preferencelist=('plain',)).get_content())
            on_sending();return {'status':'sent'}
        self.assertEqual(check_delivery(self.root,self.day,sender=sender)['status'],'warning_sent')
        self.assertTrue((self.root/'state/delivery-alerts'/f'{self.day}.json').exists())
        other=Mock();check_delivery(self.root,self.day,sender=other);other.assert_not_called()

    def test_partial_uncertain_and_unconfirmed_sent_are_not_success(self):
        for status in ('partial','uncertain','sent'):
            atomic_json(self.root/'state/runs'/f'{self.day}.json',{'date':self.day,'status':status})
            def sender(message,settings,on_sending):
                on_sending();return {'status':'sent'}
            result=check_delivery(self.root,self.day,sender=sender)
            self.assertEqual(result['status'],'warning_sent' if status=='partial' else 'already_alerted')

    def test_delivery_warning_does_not_suppress_evening_warning(self):
        atomic_json(self.root/'state/alerts'/f'{self.day}.json',{'status':'sent'})
        sender=Mock(return_value={'status':'sent'})
        self.assertEqual(check_delivery(self.root,self.day,sender=sender)['status'],'warning_sent')
        sender.assert_called_once()
