"""Offline coverage for truthful counts, latest-attempt selection and CI wiring."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import yaml
import pmix_result_summary as summary
import collect_pmix_child_results as collector

PMIX, SUITE, PRRTE = 'a' * 40, 'b' * 40, 'c' * 40
ROOT = Path(__file__).resolve().parents[1]


class SummaryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.work = self.root / '.ci-work/123'
        (self.work / 'reframe').mkdir(parents=True)
        self.collection = self.root / 'collection'
        self.collection.mkdir()
        self.output = self.root / 'report'
        self.env = dict(CI_PROJECT_DIR=str(self.root), CI_JOB_ID='123', CI_PIPELINE_ID='456',
                        PMIX_COMMIT=PMIX, CI_COMMIT_SHA=SUITE, PRRTE_COMMIT=PRRTE, CI_JOB_STATUS='success')
        self.baseline = self.root / 'state.env'
        self.baseline.write_text('PMIX_COMMIT={}\nSUITE_COMMIT={}\nLAST_SUCCESS_EPOCH=1\n'.format('d' * 40, SUITE))
        self.job = dict(id=123, commit=PMIX, status='success', finished_at='2026-11-11T12:00:00Z',
                        web_url='https://gitlab.example/project/-/jobs/123')
        self.index()

    def index(self):
        (self.collection / 'summary-attempt.json').write_text(json.dumps(dict(
            job=self.job, child_pipeline_id='456', pipeline_url='https://gitlab.example/project/-/pipelines/456')))

    def report(self, outcomes=None):
        cases = [dict(name=name, result=(outcomes or {}).get(name, 'pass'), fixture=False)
                 for name in summary.TESTS]
        cases += [dict(name='build_pmix_1234', result='pass', fixture=True)]
        (self.work / 'reframe/run-report.json').write_text(json.dumps(dict(runs=[dict(testcases=cases)])))

    def publish(self):
        return summary.publish(self.collection, self.baseline, SUITE, self.output)

    def evidence(self):
        record = summary.record(self.env, self.collection)
        (self.collection / 'latest-attempt.json').write_text(json.dumps(record))
        return record

    def test_slurm_timeout_is_incomplete_but_assertion_failure_is_failed(self):
        timed, failed = summary.TESTS[:2]
        self.report({timed: 'fail', failed: 'fail'})
        path = self.work / 'reframe/run-report.json'
        doc = json.loads(path.read_text())
        doc['runs'][0]['testcases'][0]['jobid'] = 1234
        path.write_text(json.dumps(doc))
        stage = self.work / 'reframe/stage/frontier/batch/pmix_test' / timed
        stage.mkdir(parents=True)
        (stage / 'rfm_job.err').write_text('error: JOB 1234 ON frontier00001 CANCELLED DUE TO TIME LIMIT')
        record = self.evidence()
        self.assertEqual(record['checks'], dict(passed=9, failed=1, incomplete=1))
        self.assertIn(timed, record['check_reasons'])
        self.assertNotIn(failed, record['check_reasons'])

    def test_console_timeout_requires_matching_test_and_job_identity(self):
        timed, failed = summary.TESTS[:2]
        self.report({timed: 'fail', failed: 'fail'})
        console = self.work / 'reframe/run-console.log'
        console.write_text('FAILURE INFO for ' + timed + ' (run: 1/1)\n'
                           'Job type: batch job (id=1234)\n'
                           'error: JOB 1234 ON frontier00001 CANCELLED DUE TO TIME LIMIT\n'
                           'FAILURE INFO for ' + failed + ' (run: 1/1)\n'
                           'Job type: batch job (id=5678)\n'
                           'error: JOB 9999 ON frontier00001 CANCELLED DUE TO TIME LIMIT\n')
        result = self.evidence()
        self.assertEqual(result['checks'], dict(passed=9, failed=1, incomplete=1))
        self.assertEqual(result['check_outcomes'][failed], 'failed')

    def test_successful_expected_timeout_test_is_not_reclassified(self):
        self.report()
        (self.work / 'reframe/run-console.log').write_text(
            'FAILURE INFO for ' + summary.TESTS[0] + ' (run: 1/1)\n'
            'Job type: batch job (id=1234)\nerror: JOB 1234 CANCELLED DUE TO TIME LIMIT\n')
        self.assertEqual(self.evidence()['checks'], dict(passed=11, failed=0, incomplete=0))

    def test_success_counts_only_runtime_checks(self):
        self.report()
        record = self.evidence()
        self.assertEqual(record['checks'], dict(passed=11, failed=0, incomplete=0))
        data = self.publish()
        self.assertEqual(data['status'], 'passed')
        self.assertEqual(data['last_known_good_commit'], 'd' * 40)
        self.assertIn('batch-start checkpoint', (self.output / 'latest-summary.md').read_text())
        self.assertEqual(json.loads((self.output / 'latest-summary.json').read_text()), data)

    def test_timeout_preserves_partial_console_results(self):
        self.env['CI_JOB_STATUS'] = 'timedout'
        (self.work / 'reframe/run-console.log').write_text(
            '[       OK ] ( 6/17) build_prrte /123 @frontier:batch+pmix_test\n'
            '[ RUN      ] PMIxPythonScalingTest /123 @frontier:batch+pmix_test\n'
            '\x1b[32m[       OK ] ( 7/17) PMIxPythonChildTimeoutTest /123 @frontier:batch+pmix_test\x1b[0m\n')
        (self.work / 'reframe/run-report.json').write_text('{truncated')
        self.evidence()
        self.job.update(status='failed', failure_reason='job_execution_timeout')
        self.index()
        data = self.publish()
        self.assertEqual(data['checks'], dict(passed=1, failed=0, incomplete=10))
        self.assertEqual(data['status'], 'incomplete')
        self.assertIn('timed out', data['reason'])
        self.assertNotIn('waiting for an allocation', data['reason'])

    def test_failed_runtime_and_aborted_are_distinct(self):
        self.report({summary.TESTS[0]: 'fail', summary.TESTS[1]: 'aborted'})
        self.evidence()
        self.job['status'] = 'failed'
        self.index()
        data = self.publish()
        self.assertEqual(data['status'], 'failed')
        self.assertEqual(data['checks'], dict(passed=9, failed=1, incomplete=1))

    def test_missing_or_stale_evidence_never_becomes_a_pass(self):
        self.assertEqual(self.publish()['status'], 'incomplete')
        self.report()
        data = self.evidence()
        for key, value in [('job_id', 124), ('suite_commit', 'f' * 40), ('pipeline_id', 789),
                           ('pmix_commit', 'e' * 40), ('checks', dict(passed=True, failed=0, incomplete=10))]:
            with self.subTest(key=key):
                altered = dict(data, **{key: value})
                (self.collection / 'latest-attempt.json').write_text(json.dumps(altered))
                actual = self.publish()
                self.assertEqual(actual['status'], 'incomplete')
                self.assertEqual(actual['checks']['incomplete'], 11)

    def test_checkout_provenance_overrides_requested_revision(self):
        stage = self.work / 'reframe/stage/fetch_prrte_123'
        stage.mkdir(parents=True)
        (stage / 'prrte-source.env').write_text('PRRTE_COMMIT={}\n'.format('e' * 40))
        data = self.evidence()
        self.assertEqual(data['prrte_revision'], 'e' * 40)
        self.assertEqual(data['prrte_revision_source'], 'checkout')

    def test_latest_uses_completion_and_excludes_superseded_attempts(self):
        jobs = [dict(id=1, finished_at='2026-11-11T12:00:00Z'),
                dict(id=2, finished_at='2026-11-11T11:00:00Z')]
        self.assertEqual(summary.latest_job(jobs)['id'], 1)
        self.assertIsNone(summary.latest_job([]))
        jobs = [dict(id=123, name='pmix-' + PMIX, retried=True),
                dict(id=124, name='pmix-' + PMIX, status='failed',
                     finished_at='2026-11-11T12:00:00Z')]
        class Client:
            token = 'not-a-real-token'
            def project_url(self, path): return path
            def get(self, url, limit): return 'http_error', 404, None, {}
        collector.collect_summary(Client(), jobs, [PMIX], '456', self.collection)
        index = json.loads((self.collection / 'summary-attempt.json').read_text())
        self.assertEqual(index['job']['id'], 124)
        self.assertFalse((self.collection / 'latest-attempt.json').exists())

    def test_empty_batch_is_explicit(self):
        (self.collection / 'summary-attempt.json').write_text('{"job": null}')
        self.assertIsNone(self.publish()['pmix_commit'])

    def test_collector_downloads_summary_and_retains_metadata(self):
        self.report()
        record = self.evidence()
        self.job['name'] = 'pmix-' + PMIX
        self.job['commit'] = PMIX
        class Client:
            token = 'not-a-real-token'
            def project_url(self, path): return path
            def get(inner, url, limit):
                self.assertIn('123/artifacts/ci-results/' + PMIX + '.summary.json', url)
                return 'ok', 200, json.dumps(record).encode(), {}
        collector.collect_summary(Client(), [self.job], [PMIX], '456', self.collection)
        self.assertEqual(self.publish()['status'], 'passed')

    def test_summary_collection_is_opt_in_and_preserves_checkpoint_record(self):
        self.report()
        evidence = self.evidence()
        jobs = [dict(self.job, name='pmix-' + PMIX)]
        commits = self.root / 'commits.txt'
        commits.write_text(PMIX + '\n')
        env_record = ('PMIX_COMMIT=' + PMIX + '\nopaque checkpoint bytes\n').encode()
        class Client:
            token = 'not-a-real-token'
            def __init__(self, *args): pass
            def project_url(self, path): return path
            def get(self, url, limit): return 'ok', 200, json.dumps(evidence).encode(), {}
        environment = dict(CI_API_V4_URL='https://gitlab.example/api/v4',
                           CI_PROJECT_ID='1', CI_JOB_TOKEN=Client.token)
        with patch.dict(os.environ, environment), \
                patch.object(collector, 'GitLabGetClient', Client), \
                patch.object(collector, 'discover_child', return_value=('ok', 'bridges', '456')), \
                patch.object(collector, 'list_jobs', return_value=('ok', jobs)), \
                patch.object(collector, 'download_artifact', return_value=('collected', 200, env_record)):
            args = ['--summaries', '--commits', str(commits), '--parent-pipeline-id', '99',
                    '--output', str(self.collection)]
            self.assertEqual(collector.collect(args), 0)
            self.assertEqual((self.collection / (PMIX + '.env')).read_bytes(), env_record)
            self.assertEqual(self.publish()['status'], 'passed')
            # A later invocation must not reuse a stale sidecar when it is missing.
            with patch.object(Client, 'get', return_value=('http_error', 404, None, {})):
                self.assertEqual(collector.collect(args), 0)
            self.assertFalse((self.collection / 'latest-attempt.json').exists())
            self.assertEqual(self.publish()['status'], 'incomplete')

    def test_cli_and_timestamps_support_python36_api(self):
        import argparse
        real_add = argparse.ArgumentParser.add_subparsers
        def old_add(parser, **kwargs):
            if 'required' in kwargs:
                raise TypeError('unsupported on Python 3.6')
            return real_add(parser, **kwargs)
        # Python 3.6 has strptime, but no datetime.fromisoformat.
        class OldDatetime:
            strptime = summary.datetime.strptime
        self.report()
        self.evidence()
        self.job['finished_at'] = '2026-11-12T10:18:44.590-05:00'
        self.index()
        args = ['summary', 'publish', '--collection', str(self.collection),
                '--baseline', str(self.baseline), '--suite-commit', SUITE,
                '--output', str(self.output)]
        with patch.object(argparse.ArgumentParser, 'add_subparsers', old_add), \
                patch.object(summary, 'datetime', OldDatetime), patch('sys.argv', args):
            summary.main()
            jobs = [dict(id=1, finished_at='2026-11-12T11:00:00.123-05:00'),
                    dict(id=2, finished_at='2026-11-12T15:30:00Z')]
            self.assertEqual(summary.latest_job(jobs)['id'], 1)
        data = json.loads((self.output / 'latest-summary.json').read_text())
        self.assertEqual(data['tested_at'], '2026-11-12T15:18:44.590000+00:00')
        self.assertTrue((self.output / 'latest-summary.md').is_file())
        with patch('sys.argv', ['summary']), self.assertRaises(SystemExit) as error:
            summary.main()
        self.assertEqual(error.exception.code, 2)

    def test_generated_shell_and_slurm_jobs_record_evidence(self):
        import generate_pmix_child_pipeline as generator
        for slurm in (False, True):
            pipeline = yaml.safe_load(generator.render_pipeline([PMIX], prrte_selections={PMIX: ('master', PRRTE)}, slurm_runner=slurm))
            job = pipeline['pmix-' + PMIX]
            self.assertIn('pmix_result_summary.py record', '\n'.join(job['after_script']))
            self.assertIn('ci-results/' + PMIX + '.summary.json', job['artifacts']['paths'])
        ci = yaml.safe_load((ROOT / '.gitlab-ci.yml').read_text())
        job = ci['collect-pmix-child-results-scheduled']
        self.assertIn('--summaries', '\n'.join(job['script']))
        self.assertIn('ci-scheduled/report/', job['artifacts']['paths'])


if __name__ == '__main__':
    unittest.main()
