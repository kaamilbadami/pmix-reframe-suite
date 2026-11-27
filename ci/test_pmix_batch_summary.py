"""A passing latest attempt must never hide an earlier checkpoint blocker."""
import json
from pathlib import Path
import tempfile
import unittest

import yaml
import collect_pmix_child_results as collector
import reconcile_pmix_results as reconciler
import apply_reconciled_pmix_state as updater
from pmix_batch_summary import batch_summary
from pmix_result_summary import eastern_time, age_text
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[1]
BASE, A, B, C, SUITE, PRRTE = [letter * 40 for letter in 'dabcfe']


class BatchSummaryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.collection = self.root / 'collection'
        self.collection.mkdir()
        self.output = self.root / 'report'
        self.baseline = self.root / 'baseline.env'
        self.state(self.baseline, BASE)
        self.commits = self.root / 'commits.txt'
        self.commits.write_text(A + '\n' + B + '\n' + C + '\n')
        self.jobs = [dict(id=i, commit=sha, status='success', finished_at='2026-11-13T12:00:00Z',
                          web_url='https://gitlab.example/project/-/jobs/' + str(i))
                     for i, sha in enumerate((A, B, C), 1)]
        self.index()

    def state(self, path, sha):
        path.write_text('PMIX_COMMIT={}\nSUITE_COMMIT={}\nLAST_SUCCESS_EPOCH=1\n'.format(sha, SUITE))

    def index(self):
        index = dict(jobs=self.jobs, child_pipeline_id='20',
                     pipeline_url='https://gitlab.example/project/-/pipelines/20')
        (self.collection / 'attempts.json').write_text(json.dumps(index))
        (self.collection / 'summary-attempt.json').write_text(json.dumps(dict(index, job=self.jobs[-1])))

    def result(self, sha, status, checks=None):
        job = next(job for job in self.jobs if job['commit'] == sha)
        job['status'] = 'success' if status == 'success' else 'failed'
        (self.collection / (sha + '.env')).write_text(
            'PMIX_COMMIT={}\nSUITE_COMMIT={}\nCI_JOB_STATUS={}\nCI_JOB_ID={}\nCI_PIPELINE_ID=20\n'.format(
                sha, SUITE, status, job['id']))
        sidecar = dict(schema_version=1, pmix_commit=sha, suite_commit=SUITE,
                       prrte_revision=PRRTE, prrte_revision_source='checkout',
                       job_id=job['id'], pipeline_id=20, ci_status=status,
                       total_checks=11, checks=checks or dict(passed=11, failed=0, incomplete=0))
        (self.collection / (sha + '.summary.json')).write_text(json.dumps(sidecar))
        self.index()

    def render(self, saved=None, code=None):
        return batch_summary(self.collection, self.baseline, self.commits, SUITE,
                             self.output, saved, code)

    def final_with_record(self, record, code=0):
        return batch_summary(self.collection, self.baseline, self.commits, SUITE,
                             self.output, self.baseline, code, tested_batch_record=record)

    def empty_current_batch(self):
        self.commits.write_text('')
        (self.collection / 'attempts.json').write_text('{"jobs": [], "child_pipeline_id": "30", "pipeline_url": "https://gitlab.example/project/-/pipelines/30"}')
        (self.collection / 'summary-attempt.json').write_text('{"job": null}')

    def test_final_report_retains_tested_batch_across_no_work_runs(self):
        for sha in (A, B, C):
            self.result(sha, 'success')
        record = self.root / 'state.last-tested-batch.json'
        before = self.baseline.read_bytes()
        data = self.final_with_record(record)
        self.assertEqual(data['latest_tested_batch']['pipeline_id'], 20)
        self.assertEqual(data['latest_tested_batch']['commits_tested'], 3)
        original = record.read_bytes()
        self.empty_current_batch()
        data = self.final_with_record(record)
        self.assertEqual(data['pipeline_url'], 'https://gitlab.example/project/-/pipelines/30')
        self.assertEqual(data['latest_tested_batch']['pipeline_id'], 20)
        self.assertEqual(record.read_bytes(), original)
        self.assertEqual(self.baseline.read_bytes(), before)
        text = (self.output / 'latest-summary.txt').read_text()
        self.assertIn('Latest batch that tested commits: https://gitlab.example/project/-/pipelines/20', text)
        self.assertIn('Latest test finished in that batch: 11/13/26 7:00 AM EST', text)
        self.assertIn('No tests ran during this check.', text)

    def test_failed_runtime_tests_count_even_if_checkpoint_apply_fails(self):
        self.result(A, 'failed', dict(passed=0, failed=11, incomplete=0))
        record = self.root / 'latest-tested.json'
        data = self.final_with_record(record, code=1)
        self.assertEqual(data['latest_tested_batch']['commits_tested'], 1)
        self.assertEqual(data['latest_tested_batch']['commit_counts']['failed'], 1)
        self.assertEqual(data['saved_checkpoint'], BASE)
        self.assertTrue(record.is_file())

    def test_jobs_without_completed_runtime_evidence_do_not_replace_link(self):
        self.result(A, 'success')
        record = self.root / 'latest-tested.json'
        self.final_with_record(record)
        original = record.read_bytes()
        for sha in (A, B, C):
            self.result(sha, 'failed', dict(passed=0, failed=0, incomplete=11))
        self.assertEqual(self.final_with_record(record)['latest_tested_batch']['commits_tested'], 1)
        self.assertEqual(record.read_bytes(), original)
        # A missing sidecar also cannot establish that runtime checks ran.
        for sha in (A, B, C):
            (self.collection / (sha + '.summary.json')).unlink()
        self.final_with_record(record)
        self.assertEqual(record.read_bytes(), original)

    def test_newer_failed_batch_replaces_a_passing_batch_link(self):
        self.result(A, 'success')
        record = self.root / 'latest-tested.json'
        self.final_with_record(record)
        self.result(A, 'failed', dict(passed=4, failed=7, incomplete=0))
        for name in ('attempts.json', 'summary-attempt.json'):
            path = self.collection / name
            value = json.loads(path.read_text())
            value.update(child_pipeline_id='30', pipeline_url='https://gitlab.example/project/-/pipelines/30')
            path.write_text(json.dumps(value))
        for suffix in ('.summary.json', '.env'):
            path = self.collection / (A + suffix)
            if suffix == '.summary.json':
                value = json.loads(path.read_text())
                value['pipeline_id'] = 30
                path.write_text(json.dumps(value))
            else:
                path.write_text(path.read_text().replace('CI_PIPELINE_ID=20', 'CI_PIPELINE_ID=30'))
        data = self.final_with_record(record)
        self.assertEqual(data['latest_tested_batch']['pipeline_id'], 30)
        self.assertEqual(data['latest_tested_batch']['commit_counts']['failed'], 1)
        self.assertEqual(data['saved_checkpoint'], BASE)

    def test_no_record_yet_is_explicit_and_preliminary_reports_do_not_persist(self):
        record = self.root / 'latest-tested.json'
        for sha in (A, B, C):
            self.result(sha, 'success')
        data = batch_summary(self.collection, self.baseline, self.commits, SUITE,
                             self.output, tested_batch_record=record)
        self.assertIsNotNone(data['latest_tested_batch'])
        self.assertFalse(record.exists())
        self.empty_current_batch()
        self.assertIsNone(self.final_with_record(record)['latest_tested_batch'])
        self.assertIn('Not recorded yet.', (self.output / 'latest-summary.txt').read_text())

    def test_older_pipeline_and_invalid_links_cannot_replace_saved_record(self):
        self.result(A, 'success')
        record = self.root / 'latest-tested.json'
        self.final_with_record(record)
        value = json.loads(record.read_text())
        value.update(pipeline_id=25, pipeline_url='https://gitlab.example/project/-/pipelines/25')
        record.write_text(json.dumps(value))
        self.assertEqual(self.final_with_record(record)['latest_tested_batch']['pipeline_id'], 25)
        index = json.loads((self.collection / 'attempts.json').read_text())
        index.update(child_pipeline_id='30', pipeline_url='https://token@elsewhere.example/-/pipelines/30')
        (self.collection / 'attempts.json').write_text(json.dumps(index))
        self.assertEqual(self.final_with_record(record)['latest_tested_batch']['pipeline_id'], 25)

    def test_eastern_display_tracks_daylight_saving_and_date_rollover(self):
        self.assertEqual(eastern_time('2026-11-24T17:52:03.086000+00:00'),
                         '11/24/26 12:52 PM EST')
        self.assertEqual(eastern_time('2026-07-24T17:52:03Z'),
                         '07/24/26 1:52 PM EDT')
        self.assertEqual(eastern_time('2026-11-24T02:00:00Z'),
                         '11/23/26 9:00 PM EST')
        self.assertEqual(eastern_time(None), 'Unknown')

    def test_no_later_pass_warning_when_all_commits_fail(self):
        for sha in (A, B, C):
            self.result(sha, 'failed', dict(passed=4, failed=7, incomplete=0))
        self.render(self.baseline, 0)
        self.assertNotIn('Later passing commits', (self.output / 'latest-summary.md').read_text())

    def test_checkpoint_time_uses_saved_state_and_age_is_at_generation(self):
        for sha in (A, B, C):
            self.result(sha, 'success')
        saved = self.root / 'saved.env'
        epoch = int(datetime(2026, 11, 24, 17, 52, tzinfo=timezone.utc).timestamp())
        saved.write_text('PMIX_COMMIT={}\nSUITE_COMMIT={}\nLAST_SUCCESS_EPOCH={}\n'.format(C, SUITE, epoch))
        now = datetime(2026, 11, 26, 18, 0, tzinfo=timezone.utc)
        data = batch_summary(self.collection, self.baseline, self.commits, SUITE,
                             self.output, saved, 0, now=now)
        text = (self.output / 'latest-summary.txt').read_text()
        self.assertIn('Last known good recorded: 11/24/26 12:52 PM EST', text)
        self.assertIn('Time since last known good: 2 days, 8 minutes ago', text)
        self.assertEqual(data['last_known_good_age_seconds'], 173280)
        self.assertEqual(data['generated_at'], now.isoformat())
        self.assertEqual(text, (self.output / 'latest-summary.md').read_text())
        self.assertNotIn('**', text)
        self.assertNotIn('#', text)
        self.assertNotIn('(Eastern)', text)
        self.assertEqual(age_text(-1), 'Timestamp is in the future; check clock settings.')

    def test_failed_job_with_only_incomplete_checks_stays_blocked(self):
        self.result(A, 'failed', dict(passed=4, failed=0, incomplete=7))
        self.result(B, 'success')
        self.result(C, 'success')
        data = self.render(self.baseline, 0)
        self.assertEqual(data['commit_counts'], dict(passed=2, failed=0, incomplete=1))
        self.assertEqual(data['passing_boundary'], BASE)
        self.assertEqual(data['items'][0]['status'], 'failed')
        self.assertEqual(data['status'], 'blocked')

    def test_a_pass_b_fail_c_pass_keeps_blocker_as_headline(self):
        self.result(A, 'success')
        self.result(B, 'failed', dict(passed=10, failed=1, incomplete=0))
        self.result(C, 'success')
        saved = self.root / 'saved.env'
        self.state(saved, A)
        data = self.render(saved, 0)
        self.assertEqual(data['saved_checkpoint'], A)
        self.assertEqual(data['passing_boundary'], A)
        self.assertEqual(data['first_blocker']['commit'], B)
        self.assertEqual(data['first_blocker']['runtime_checks']['failed'], 1)
        self.assertEqual(data['commit_counts'], dict(passed=2, failed=1, incomplete=0))
        self.assertEqual(data['commits_beyond_passing_boundary'], 2)
        self.assertEqual(data['latest_attempt']['status'], 'passed')
        self.assertEqual(data['status'], 'blocked')
        self.assertEqual(data['checkpoint_update'], 'applied')
        text = (self.output / 'latest-summary.md').read_text()
        self.assertLess(text.index('First blocker:'), text.index('Failed or incomplete commits'))
        self.assertIn('1 failed / 0 incomplete', text)
        self.assertIn('Test job for this commit: ' + self.jobs[1]['web_url'], text)

    def test_collect_does_not_claim_candidate_was_saved(self):
        for sha in (A, B, C):
            self.result(sha, 'success')
        before = self.baseline.read_bytes()
        data = self.render()
        self.assertEqual(data['saved_checkpoint'], BASE)
        self.assertEqual(data['passing_boundary'], C)
        self.assertEqual(data['checkpoint_update'], 'pending')
        self.assertIn('not a confirmed saved update', (self.output / 'latest-summary.md').read_text())
        self.assertEqual(self.baseline.read_bytes(), before)

    def test_all_pass_after_apply_confirms_checkpoint(self):
        for sha in (A, B, C):
            self.result(sha, 'success')
        saved = self.root / 'saved.env'
        self.state(saved, C)
        data = self.render(saved, 0)
        self.assertEqual(data['status'], 'complete')
        self.assertEqual(data['saved_checkpoint'], C)
        self.assertIsNone(data['first_blocker'])
        self.assertEqual(data['commits_beyond_passing_boundary'], 0)
        text = (self.output / 'latest-summary.md').read_text()
        self.assertNotIn('Failed or incomplete commits', text)
        self.assertNotIn('PRRTE revision', text)
        self.assertNotIn('Runtime checks for this commit', text)
        self.assertNotIn('Test job for this commit', text)
        self.assertIn('Batch pipeline (all commit jobs)', text)
        self.assertIn('Latest test time: 11/13/26 7:00 AM EST', text)
        self.assertEqual([item['attempt']['checks']['passed'] for item in data['items']], [11, 11, 11])
        self.assertEqual(data['boundary_attempt']['tested_at'], '2026-11-13T12:00:00+00:00')

    def test_report_shows_every_failed_or_incomplete_commit(self):
        self.result(A, 'success')
        self.result(B, 'failed', dict(passed=10, failed=1, incomplete=0))
        self.result(C, 'timedout', dict(passed=8, failed=0, incomplete=3))
        self.render(self.baseline, 0)
        text = (self.output / 'latest-summary.md').read_text()
        self.assertNotIn('Commit: ' + A, text)
        self.assertIn('Commit: ' + B, text)
        self.assertIn('Commit: ' + C, text)
        self.assertIn('8 passed / 0 failed / 3 incomplete', text)
        self.assertIn('Test job for this commit: ' + self.jobs[1]['web_url'], text)
        self.assertIn('Test job for this commit: ' + self.jobs[2]['web_url'], text)
        self.assertIn('timed out', text)

    def test_collector_preserves_runtime_evidence_for_all_commits(self):
        for sha in (A, B, C):
            self.result(sha, 'success')
        calls = []
        outer = self
        class Client:
            token = 'not-a-real-token'
            def project_url(self, path):
                return path
            def get(self, path, limit):
                calls.append(path)
                sha = path.rsplit('/', 1)[-1][:-len('.summary.json')]
                return 'ok', 200, (outer.collection / (sha + '.summary.json')).read_bytes(), {}
        jobs = [dict(job, name='pmix-' + job['commit']) for job in self.jobs]
        collector.collect_summary(Client(), jobs, [A, B, C], '20', self.collection)
        self.assertEqual(len(calls), 3)
        data = self.render()
        self.assertTrue(all(item['attempt']['prrte_revision'] == PRRTE for item in data['items']))

    def test_timeout_is_incomplete_even_if_latest_passes(self):
        self.result(A, 'timedout', dict(passed=8, failed=0, incomplete=3))
        self.result(B, 'success')
        self.result(C, 'success')
        data = self.render(self.baseline, 0)
        self.assertEqual(data['commit_counts'], dict(passed=2, failed=0, incomplete=1))
        self.assertEqual(data['saved_checkpoint'], BASE)
        self.assertEqual(data['first_blocker']['runtime_checks'], dict(passed=8, failed=0, incomplete=3))
        self.assertIn('timed out', data['first_blocker']['reason'])

    def test_missing_and_wrong_suite_evidence_block_the_prefix(self):
        self.result(A, 'success')
        self.result(C, 'success')
        self.assertEqual(self.render()['first_blocker']['status'], 'missing')
        self.result(B, 'success')
        record = self.collection / (B + '.env')
        record.write_text(record.read_text().replace(SUITE, '0' * 40))
        self.assertEqual(self.render()['first_blocker']['status'], 'malformed')

    def test_wrong_sidecar_cannot_supply_runtime_counts(self):
        self.result(A, 'failed', dict(passed=0, failed=11, incomplete=0))
        record = self.collection / (A + '.summary.json')
        data = json.loads(record.read_text())
        data['job_id'] = 999
        record.write_text(json.dumps(data))
        data = self.render()
        self.assertEqual(data['first_blocker']['runtime_checks'], dict(passed=0, failed=0, incomplete=11))

    def test_failed_or_unverified_apply_never_claims_update(self):
        for sha in (A, B, C):
            self.result(sha, 'success')
        data = self.render(self.baseline, 1)
        self.assertEqual(data['status'], 'checkpoint_update_failed')
        self.assertEqual(data['saved_checkpoint'], BASE)
        data = self.render(self.baseline, 0)
        self.assertEqual(data['checkpoint_update'], 'unconfirmed')
        self.assertEqual(data['status'], 'checkpoint_update_unconfirmed')

    def test_empty_batch_and_baseline_health_retest(self):
        self.commits.write_text('')
        self.jobs = []
        (self.collection / 'summary-attempt.json').write_text('{"job": null}')
        self.index = lambda: None
        (self.collection / 'attempts.json').write_text('{"jobs": []}')
        data = self.render(self.baseline, 0)
        self.assertEqual(data['status'], 'no_new_commits')
        self.assertEqual(data['commits_in_batch'], 0)
        text = (self.output / 'latest-summary.md').read_text()
        self.assertIn('No tests ran during this check.', text)
        self.assertNotIn('Passing boundary', text)
        self.assertNotIn('Unavailable', text)
        self.assertNotIn('Suite revision', text)
        metadata = Path(str(self.commits) + '.head.json')
        metadata.write_text(json.dumps(dict(commit=BASE, committed_epoch=0)))
        self.render(self.baseline, 0)
        text = (self.output / 'latest-summary.md').read_text()
        self.assertIn('No new commits to test since 12/31/69 7:00 PM EST.', text)
        # Never attach another commit's date to this no-new-commits result.
        metadata.write_text(json.dumps(dict(commit=A, committed_epoch=0)))
        self.render(self.baseline, 0)
        self.assertNotIn('since 12/31/69', (self.output / 'latest-summary.md').read_text())
        self.commits.write_text(BASE + '\n')
        self.jobs = [dict(id=1, commit=BASE, status='success')]
        self.result(BASE, 'success')
        data = self.render(self.baseline, 0)
        self.assertEqual(data['new_commits'], 0)
        self.assertEqual(data['commits_in_batch'], 1)

    def test_apply_publishes_final_report_after_application(self):
        config = yaml.safe_load((ROOT / '.gitlab-ci.yml').read_text())
        job = config['apply-pmix-reconciled-state-scheduled']
        self.assertIn(dict(job='collect-pmix-child-results-scheduled', artifacts=True), job['needs'])
        script = job['script'][0]
        self.assertLess(script.index('apply_reconciled_pmix_state.py apply'),
                        script.index('pmix_batch_summary.py'))
        self.assertIn('--apply-exit-code "$apply_status"', script)
        self.assertIn('--tested-batch-record', script)
        self.assertNotIn('--tested-batch-record', config['collect-pmix-child-results-scheduled']['script'][0])
        self.assertIn('ci-scheduled/report/', job['artifacts']['paths'])
        self.assertIn('exit "$apply_status"', script)
        self.assertEqual(job['artifacts']['when'], 'always')

    def test_real_reconcile_apply_and_report_agree_on_a_failed_b_passed_c(self):
        self.result(A, 'success')
        self.result(B, 'failed', dict(passed=10, failed=1, incomplete=0))
        self.result(C, 'success')
        current = self.root / 'current.env'
        current.write_bytes(self.baseline.read_bytes())
        reconciliation = self.root / 'reconciliation'
        code = reconciler.reconcile([
            '--baseline-state', str(self.baseline), '--current-state', str(current),
            '--commits', str(self.commits), '--results', str(self.collection),
            '--suite-commit', SUITE, '--output', str(reconciliation)])
        self.assertEqual(code, 3)
        code = updater.main([
            'apply', '--baseline-state', str(self.baseline), '--authoritative-state', str(current),
            '--commits', str(self.commits), '--suite-commit', SUITE,
            '--report', str(reconciliation / 'reconciliation.env'),
            '--proposal', str(reconciliation / 'proposed-pmix-master.env')])
        self.assertEqual(code, 0)
        data = self.render(current, code)
        self.assertEqual(data['saved_checkpoint'], A)
        self.assertEqual(data['first_blocker']['commit'], B)
        self.assertEqual(data['status'], 'blocked')

    def test_collector_fetches_blocker_boundary_and_latest_evidence(self):
        self.result(A, 'success')
        self.result(B, 'failed', dict(passed=10, failed=1, incomplete=0))
        self.result(C, 'success')
        calls = []
        outer = self
        class Client:
            token = 'not-a-real-token'
            def project_url(self, path):
                return path
            def get(self, path, limit):
                calls.append(path)
                sha = path.rsplit('/', 1)[-1][:-len('.summary.json')]
                return 'ok', 200, (outer.collection / (sha + '.summary.json')).read_bytes(), {}
        jobs = [dict(job, name='pmix-' + job['commit']) for job in self.jobs]
        collector.collect_summary(Client(), jobs, [A, B, C], '20', self.collection)
        self.assertEqual(len(calls), 3)
        for job in self.jobs:
            self.assertTrue(any('jobs/{}/artifacts/'.format(job['id']) in path for path in calls))
        data = self.render()
        self.assertEqual(data['first_blocker']['runtime_checks']['failed'], 1)
        self.assertEqual(data['boundary_attempt']['prrte_revision'], PRRTE)


if __name__ == '__main__':
    unittest.main()
