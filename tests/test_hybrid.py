import copy
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from miki_jobsearch.hybrid import claim, execute, plan, readiness, scheduled_target
from miki_jobsearch.service import StateSyncError, atomic_json, run
from miki_jobsearch.status_feed import write_feed

ROOT = Path(__file__).resolve().parent.parent
FIXTURE = json.loads((ROOT / 'tests/fixtures/report.json').read_text())


class HybridTests(unittest.TestCase):
    def test_prepared_only_delivery_and_no_automatic_laptop_research(self):
        for clock, actor, phase in [
            ('2026-10-07T08:29:59+02:00','github',None),
            ('2026-10-07T08:30:00+02:00','github','deliver'),
            ('2026-10-07T09:45:00+02:00','github','deliver'),
            ('2026-10-07T16:00:00+02:00','laptop',None),
            ('2026-10-07T09:30:00+02:00','laptop',None),
            ('2026-10-11T16:00:00+02:00','laptop',None),
            ('2026-10-26T07:29:59+00:00','github',None),
            ('2026-10-26T07:30:00+00:00','github','deliver'),
        ]:
            self.assertEqual(plan(datetime.fromisoformat(clock),actor)[0],phase)

    def test_morning_and_deadline_windows_across_dst_and_weekends(self):
        cases = [
            ('2026-10-07T08:59:59+02:00','prepare-next',None),
            ('2026-10-07T09:00:00+02:00','prepare-next','2026-10-08'),
            ('2026-10-07T15:59:59+02:00','prepare-next','2026-10-08'),
            ('2026-10-07T16:00:00+02:00','prepare-next',None),
            ('2026-10-07T15:59:59+02:00','readiness-check',None),
            ('2026-10-07T16:00:00+02:00','readiness-check','2026-10-08'),
            ('2026-10-09T09:00:00+02:00','prepare-next',None),
            ('2026-10-11T09:00:00+02:00','prepare-next','2026-10-12'),
            ('2026-10-25T07:00:00+00:00','prepare-next',None),
            ('2026-10-25T08:00:00+00:00','prepare-next','2026-10-26'),
        ]
        for clock,task,expected in cases:
            self.assertEqual(scheduled_target(datetime.fromisoformat(clock),task),expected)

    def test_morning_preparation_excludes_jobs_in_todays_queued_report(self):
        with tempfile.TemporaryDirectory() as folder:
            store=Path(folder)
            atomic_json(store/'runs/2026-10-07.json',{'date':'2026-10-07','status':'prepared','report':copy.deepcopy(FIXTURE)})
            result=run(ROOT,store,now=datetime.fromisoformat('2026-10-07T09:00:00+02:00'),
                       dry_run=False,prepare=True,delivery_date='2026-10-08',allow_early_prepare=True,
                       researcher=lambda *a:copy.deepcopy(FIXTURE),verifier=lambda *a:(True,'verified'))
            self.assertEqual(result['hits'],0)

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

    def test_manual_refresh_preserves_old_report_and_restores_it_after_failure(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);day='2026-10-08';now=datetime.now(timezone.utc)
            path=root/'state/runs'/f'{day}.json'
            previous={'date':day,'status':'prepared','report':{'jobs':[]},'subject':'Old report'}
            atomic_json(path,previous);path.with_suffix('.html').write_text('Old HTML')
            atomic_json(root/'state/coordinator.json',{'lease':{'token':'token','expires_at':(now+timedelta(minutes=40)).isoformat()}})
            def fails(*args,**kwargs):
                atomic_json(path,{'date':day,'status':'failed'})
                raise RuntimeError('research failed')
            with patch('miki_jobsearch.hybrid.claim',return_value='token'),patch('miki_jobsearch.hybrid.complete'),patch('miki_jobsearch.hybrid.git_persister',return_value=Mock()):
                with self.assertRaises(RuntimeError):
                    execute(root,'manual',phase='prepare',day=day,now=now,runner=fails,refresh=True)
            self.assertEqual(json.loads(path.read_text()),previous)
            self.assertEqual(path.with_suffix('.html').read_text(),'Old HTML')
            self.assertEqual(len(list((root/'state/preparation-history').glob('*.json'))),2)

    def test_ready_report_is_researched_again_only_on_explicit_manual_refresh(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);day='2026-10-08';now=datetime.now(timezone.utc)
            atomic_json(root/'state/runs'/f'{day}.json',{'date':day,'status':'prepared','report':{'jobs':[]}})
            atomic_json(root/'state/coordinator.json',{'lease':{'token':'token','expires_at':(now+timedelta(minutes=40)).isoformat()}})
            runner=Mock(return_value={'status':'prepared'})
            with patch('miki_jobsearch.hybrid.claim',return_value='token'),patch('miki_jobsearch.hybrid.complete'),patch('miki_jobsearch.hybrid.git_persister',return_value=Mock()):
                execute(root,'manual',phase='prepare',day=day,runner=runner)
                runner.assert_not_called()
                execute(root,'manual',phase='prepare',day=day,runner=runner,refresh=True)
                runner.assert_called_once()
                self.assertTrue(runner.call_args.kwargs['prepare'])
                with self.assertRaises(ValueError):
                    execute(root,'github',phase='prepare',day=day,refresh=True)

    def test_delivery_warning_targets_today_from_ten_on_weekdays_across_dst(self):
        for clock,expected in [
            ('2026-10-08T09:59:59+02:00',None),
            ('2026-10-08T10:00:00+02:00','2026-10-08'),
            ('2026-10-09T10:00:00+02:00','2026-10-09'),
            ('2026-10-10T10:00:00+02:00',None),
            ('2026-10-26T08:59:59+00:00',None),
            ('2026-10-26T09:00:00+00:00','2026-10-26')]:
            self.assertEqual(scheduled_target(datetime.fromisoformat(clock),'delivery-check'),expected)
