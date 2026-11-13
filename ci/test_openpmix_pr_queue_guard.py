"""Admission races, supersession, explicit retries, and no-compute outcomes."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from urllib.parse import parse_qs, urlsplit

import yaml
import openpmix_pr_queue_guard as guard
from discover_eligible_openpmix_prs import DiscoveryError

ROOT = Path(__file__).resolve().parents[1]
SHA = 'a' * 40


class GuardTests(unittest.TestCase):
    def setUp(self):
        self.env = dict(CI_API_V4_URL='https://gitlab.example/api/v4',
                        CI_PROJECT_ID='1', CI_JOB_TOKEN='test-only', CI_PIPELINE_ID='50',
                        OPENPMIX_PR_NUMBER='42', OPENPMIX_PR_EXPECTED_SHA=SHA)

    def evaluate(self, newer=(), first=50, status='running'):
        self.calls = []
        name = 'openpmix-pr:42:' + SHA

        def get(url, headers):
            self.calls.append(url)
            self.assertEqual(headers['JOB-TOKEN'], 'test-only')
            if url.endswith('/50'):
                return dict(id=50, name=name)
            query = parse_qs(urlsplit(url).query)
            if 'name' in query:
                self.assertEqual(query['name'], [name])
                self.assertEqual(query['sort'], ['asc'])
                return [dict(id=first, name=name, status=status)]
            return list(newer) + [dict(id=50, name=name)]

        return guard.decision(self.env, get)

    def test_original_selection_runs_and_does_not_mutate_gitlab(self):
        self.assertEqual(self.evaluate(), ('run', 'selected', None))
        self.assertEqual(len(self.calls), 3)

    def test_duplicate_race_allows_only_oldest_even_after_failure(self):
        for state in ('pending', 'running', 'success', 'failed', 'canceled', 'skipped'):
            self.assertEqual(self.evaluate(first=40, status=state), ('not-run', 'duplicate', 40))

    def test_new_head_supersedes_queued_old_head(self):
        rows = [dict(id=60, name='openpmix-pr:42:' + 'b' * 40)]
        self.assertEqual(self.evaluate(rows), ('not-run', 'superseded', 60))
        self.assertEqual(len(self.calls), 2)

    def test_other_prs_and_newer_duplicates_do_not_supersede(self):
        rows = [dict(id=61, name='openpmix-pr:43:' + 'b' * 40),
                dict(id=60, name='openpmix-pr:42:' + SHA)]
        self.assertEqual(self.evaluate(rows), ('run', 'selected', None))

    def test_api_failure_and_wrong_identity_fail_closed(self):
        def broken(url, headers):
            raise DiscoveryError('API unavailable')
        with self.assertRaises(DiscoveryError):
            guard.decision(self.env, broken)
        with self.assertRaises(DiscoveryError):
            guard.decision(self.env, lambda *args: dict(id=50, name='another pipeline'))

    def test_newer_scan_paginates(self):
        calls = []
        def get(url, headers):
            if url.endswith('/50'):
                return dict(id=50, name='openpmix-pr:42:' + SHA)
            query = parse_qs(urlsplit(url).query)
            page = int(query.get('page', ['1'])[0])
            calls.append(page)
            if page == 1:
                return [dict(id=i, name='unrelated') for i in range(200, 100, -1)]
            return [dict(id=60, name='openpmix-pr:42:' + 'b' * 40)]
        self.assertEqual(guard.decision(self.env, get), ('not-run', 'superseded', 60))
        self.assertEqual(calls, [1, 2])

    def test_excessive_history_fails_closed(self):
        def get(url, headers):
            if url.endswith('/50'):
                return dict(id=50, name='openpmix-pr:42:' + SHA)
            return [dict(id=i, name='unrelated') for i in range(200, 100, -1)]
        with self.assertRaisesRegex(DiscoveryError, 'exceeded its bound'):
            guard.decision(self.env, get)

    def test_skip_record_is_bound_to_pipeline_and_is_not_success_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'admission.json'
            data = dict(version=1, pipeline_id=50, pr_number='42', head_sha=SHA,
                        decision='not-run', reason='duplicate', related_pipeline_id=40)
            path.write_text(json.dumps(data))
            self.assertTrue(guard.skipped(self.env, path))
            data['pipeline_id'] = 49
            path.write_text(json.dumps(data))
            with self.assertRaises(DiscoveryError):
                guard.skipped(self.env, path)

    def test_explicit_manual_run_does_not_require_tokens_or_api(self):
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run(['python3', str(ROOT / 'ci/openpmix_pr_queue_guard.py'), 'check'],
                                    env={'PATH': os.environ['PATH']}, cwd=directory,
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_execution_lock_covers_admission_and_skips_never_launch_suite(self):
        jobs = yaml.safe_load((ROOT / 'ci/openpmix_pr_jobs.yml').read_text())
        job = jobs['run-openpmix-pr']
        self.assertEqual(job['resource_group'], 'pmix-python-suite-frontier')
        script = job['script'][0].replace('/bin/bash ci/run_openpmix_pr_isolated.sh', 'touch launched')
        with tempfile.TemporaryDirectory() as directory:
            binary = Path(directory) / 'python3'
            binary.write_text('#!/bin/bash\nexit "$GUARD_EXIT"\n')
            binary.chmod(0o755)
            for code, expected, launched in [('0', 0, True), ('3', 0, False), ('2', 2, False)]:
                marker = Path(directory) / 'launched'
                if marker.exists():
                    marker.unlink()
                env = dict(os.environ, GUARD_EXIT=code, PATH=directory + ':' + os.environ['PATH'])
                result = subprocess.run(['bash', '-c', script], env=env, cwd=directory,
                                        capture_output=True, text=True)
                self.assertEqual(result.returncode, expected, result.stderr)
                self.assertEqual(marker.exists(), launched)
        final = jobs['finalize-openpmix-pr']
        self.assertIn('was-skipped', final['script'][0])
        self.assertIn('ci-openpmix-pr-final/not-run.json', final['artifacts']['paths'])


if __name__ == '__main__':
    unittest.main()
