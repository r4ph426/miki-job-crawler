import copy
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from miki_jobsearch.hybrid import claim, execute, plan, readiness
from miki_jobsearch.service import StateSyncError, atomic_json, run
from miki_jobsearch.status_feed import write_feed

ROOT = Path(__file__).resolve().parent.parent
FIXTURE = json.loads((ROOT / 'tests/fixtures/report.json').read_text())


class HybridTests(unittest.TestCase):
    def test_calendar_previous_day_including_sunday_and_dst(self):
        cases = [
            ('2026-10-07T15:59:00+02:00','laptop',None,'2026-10-07'),
            ('2026-10-07T16:00:00+02:00','laptop','prepare','2026-10-08'),
            ('2026-10-09T16:00:00+02:00','laptop',None,'2026-10-09'),
            ('2026-10-11T16:00:00+02:00','laptop','prepare','2026-10-12'),
            ('2026-10-26T07:29:59+00:00','github',None,'2026-10-26'),
            ('2026-10-26T07:30:00+00:00','github','deliver','2026-10-26'),
            ('2026-10-07T09:30:00+02:00','laptop','fallback','2026-10-07'),
            ('2026-10-07T09:44:59+02:00','github','deliver','2026-10-07'),
            ('2026-10-07T09:45:00+02:00','github','fallback','2026-10-07'),
            ('2026-10-07T10:10:00+02:00','laptop',None,'2026-10-07'),
        ]
        for clock,actor,phase,day in cases:
            self.assertEqual(plan(datetime.fromisoformat(clock),actor),(phase,day))

    def test_evening_preparation_records_delivery_day_but_researches_actual_day(self):
        with tempfile.TemporaryDirectory() as folder:
            researcher=Mock(return_value=copy.deepcopy(FIXTURE));sender=Mock()
            now=datetime.fromisoformat('2026-10-07T16:00:00+02:00')
            result=run(ROOT,Path(folder),now=now,dry_run=False,prepare=True,delivery_date='2026-10-08',
                       researcher=researcher,verifier=lambda *a:(True,'verified'),sender=sender)
            self.assertEqual(result['date'],'2026-10-08')
            self.assertEqual(str(researcher.call_args.args[-1]),'2026-10-07')
            sender.assert_not_called()
            r=json.loads((Path(folder)/'runs/2026-10-08.json').read_text())
            self.assertEqual(r['status'],'prepared')
            self.assertIn('Recherche vom 07.10.2026',r['report']['summary'])

    def test_expiry_is_filtered_for_the_delivery_day(self):
        fixture=copy.deepcopy(FIXTURE);fixture['jobs'][0]['deadline']='2026-10-07'
        with tempfile.TemporaryDirectory() as folder:
            result=run(ROOT,Path(folder),now=datetime.fromisoformat('2026-10-07T16:00:00+02:00'),
                       dry_run=False,prepare=True,delivery_date='2026-10-08',
                       researcher=lambda *a:fixture,verifier=lambda *a:(True,'verified'))
            self.assertEqual(result['hits'],0)

    def test_send_only_cannot_research_when_the_evening_report_is_missing(self):
        with tempfile.TemporaryDirectory() as folder:
            researcher=Mock();sender=Mock()
            result=run(ROOT,Path(folder),now=datetime.fromisoformat('2026-10-08T08:30:00+02:00'),
                       dry_run=False,require_prepared=True,researcher=researcher,sender=sender)
            self.assertEqual(result['status'],'skipped')
            researcher.assert_not_called();sender.assert_not_called()

    def test_advance_date_cannot_send_or_bypass_weekend_automatically(self):
        with tempfile.TemporaryDirectory() as folder:
            for prepare,day in [(False,'2026-10-08'),(True,'2026-10-10'),(True,'2026-10-12')]:
                with self.assertRaises(ValueError):
                    run(ROOT,Path(folder),now=datetime.fromisoformat('2026-10-07T16:00:00+02:00'),
                        dry_run=False,prepare=prepare,delivery_date=day)

    def test_active_laptop_lease_defers_github_research(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);now=datetime.now(timezone.utc)
            atomic_json(root/'state/coordinator.json',{'lease':{'expires_at':(now+timedelta(minutes=30)).isoformat()}})
            runner=Mock()
            result=execute(root,'github',now=now,phase='fallback',day='2026-10-08',runner=runner)
            self.assertEqual(result['status'],'deferred');runner.assert_not_called()

    def test_failed_remote_claim_never_researches_or_sends(self):
        with tempfile.TemporaryDirectory() as folder,patch('miki_jobsearch.hybrid.checkpoint',side_effect=StateSyncError('conflict')):
            runner=Mock()
            with self.assertRaises(StateSyncError):
                execute(Path(folder),'github',phase='fallback',day='2026-10-08',runner=runner)
            runner.assert_not_called()

    def test_prepared_delivery_reuses_report_and_terminal_state_blocks_both_actors(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);day='2026-10-08'
            atomic_json(root/'state/runs'/f'{day}.json',{'date':day,'status':'prepared','report':{'jobs':[]}})
            runner=Mock(return_value={'status':'sent'})
            with patch('miki_jobsearch.hybrid.claim',return_value='token'),patch('miki_jobsearch.hybrid.complete'),patch('miki_jobsearch.hybrid.git_persister',return_value=Mock()):
                execute(root,'github',phase='deliver',day=day,runner=runner)
            self.assertTrue(runner.call_args.kwargs['require_prepared'])
            for terminal in ('sent','sending','uncertain','partial'):
                atomic_json(root/'state/runs'/f'{day}.json',{'date':day,'status':terminal})
                runner.reset_mock()
                execute(root,'laptop',phase='fallback',day=day,runner=runner)
                runner.assert_not_called()

    def test_status_feed_drops_reports_addresses_and_keys(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);store=root/'state'
            atomic_json(store/'runs/2026-10-08.json',{'date':'2026-10-08','status':'prepared','hits':2,
                'report':{'summary':'private','jobs':[{'email':'private@example.org'}]},'MAIL_TO':'private@example.org'})
            payload=write_feed(root,store)
            self.assertTrue(payload['records']['2026-10-08']['has_report'])
            self.assertNotIn('private',json.dumps(payload))
            self.assertTrue(readiness(root,'2026-10-08')['ready'])
