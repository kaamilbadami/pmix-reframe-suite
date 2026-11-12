"""Offline tests for fixed pins, one-check execution, reporting and isolation."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import yaml
import openpmix_fixed_diagnostic as diagnostic

ROOT = Path(__file__).resolve().parents[1]


class DiagnosticTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name)
        self.env = dict(CI_JOB_ID='123', CI_PIPELINE_ID='456', CI_COMMIT_SHA='a' * 40,
                        RFM_BIN='/fixed/reframe')
        self.root_patch = patch.object(diagnostic, 'ROOT', self.repo)
        self.root_patch.start()
        self.addCleanup(self.root_patch.stop)
        self.root = self.repo / '.ci-work/123'
        (self.root / 'reframe').mkdir(parents=True)

    def report(self, runtime='pass', fixture='pass'):
        cases = [dict(name=diagnostic.CHECK, fixture=False, result=runtime)]
        cases += [dict(name=n, fixture=True, result=fixture) for n in (
            'fetch_libevent', 'build_libevent', 'fetch_pmix', 'build_pmix', 'fetch_prrte', 'build_prrte')]
        (self.root / 'reframe/run-report.json').write_text(json.dumps(dict(runs=[dict(testcases=cases)])))

    def test_fetches_exact_object_without_live_pr_or_master_resolution(self):
        calls = []
        def git(path, *args):
            calls.append(args)
            return diagnostic.PMIX_SHA if args == ('rev-parse', 'HEAD') else ''
        with patch.object(diagnostic, 'git', side_effect=git), patch.object(diagnostic, 'validate', return_value='none'):
            diagnostic.checkout_exact(self.root / 'source')
        self.assertIn(('fetch', '--no-tags', '--no-recurse-submodules', '--depth=1', 'origin', diagnostic.PMIX_SHA), calls)
        self.assertIn(('checkout', '--detach', diagnostic.PMIX_SHA), calls)
        self.assertNotIn('refs/pull', str(calls))
        self.assertNotIn('master', str(calls))

    def test_mismatched_checkout_is_rejected(self):
        with patch.object(diagnostic, 'git', return_value='b' * 40), self.assertRaisesRegex(ValueError, 'differs'):
            diagnostic.checkout_exact(self.root / 'source')

    def test_reframe_runs_only_one_small_two_node_check(self):
        args = diagnostic.command(self.env, self.root)
        self.assertEqual(args[args.index('-n') + 1], diagnostic.CHECK)
        for selection in ('node_counts=2', 'ppr_values=1', 'trials=1', 'slots_per_node=1'):
            self.assertIn(selection, args)
        self.assertNotIn('openpmix_single_allocation_config.py', ' '.join(args))
        self.assertIn('openpmix_pr_sysconfig.yaml', ' '.join(args))

    def test_results_are_diagnostic_not_full_suite_or_pr_success(self):
        for runtime, fixture, expected in [('pass', 'pass', 'passed'), ('fail', 'pass', 'failed'),
                                            ('skipped', 'fail', 'incomplete')]:
            self.report(runtime, fixture)
            result = diagnostic.record(self.env)
            self.assertEqual(result['status'], expected)
            self.assertEqual(result['passed'] + result['failed'] + result['incomplete'], 1)
            self.assertEqual(result['mode'], 'fixed-multinode-diagnostic')
            self.assertEqual(result['pmix_commit'], diagnostic.PMIX_SHA)
            self.assertEqual(result['prrte_commit'], diagnostic.PRRTE_SHA)
        (self.root / 'reframe/run-report.json').unlink()
        self.assertEqual(diagnostic.record(self.env)['status'], 'incomplete')

    def test_runner_uses_fixed_dependencies_and_clean_source_even_if_caller_overrides(self):
        self.report()
        seen = []
        def run(args, **kwargs):
            seen.append(kwargs['env'])
            return subprocess.CompletedProcess(args, 0)
        with patch.object(diagnostic, 'checkout_exact', return_value='none'), patch.object(diagnostic.subprocess, 'run', side_effect=run):
            env = dict(self.env, PMIX_COMMIT='b' * 40, PRRTE_COMMIT='c' * 40,
                       PMIX_SINGLE_OFFLINE_SOURCES='/bad/source', PMIX_RFM_EXEC_POLICY='serial')
            self.assertEqual(diagnostic.run(env), 0)
        self.assertEqual(seen[0]['PMIX_COMMIT'], diagnostic.PMIX_SHA)
        self.assertEqual(seen[0]['PRRTE_COMMIT'], diagnostic.PRRTE_SHA)
        self.assertNotIn('PMIX_SINGLE_OFFLINE_SOURCES', seen[0])

    def test_credential_bearing_direct_execution_is_rejected(self):
        with self.assertRaisesRegex(ValueError, 'clean environment'):
            diagnostic.run(dict(self.env, CI_JOB_TOKEN='secret'))

    def test_real_launcher_removes_credentials_and_preserves_only_job_identity(self):
        (self.repo / 'ci').mkdir()
        site = self.repo / 'site.sh'
        site.write_text('module() { :; }\n')
        launcher = (ROOT / 'ci/run_openpmix_fixed_diagnostic.sh').read_text()
        launcher = launcher.replace('/lustre/orion/gen243/proj-shared/pmix-reframe-ci-tools/pmix-py310/bin/python', sys.executable)
        launcher = launcher.replace('/etc/profile.d/olcf-env.sh', str(site)).replace('/etc/bash.bashrc.local', str(site))
        path = self.repo / 'ci/run_openpmix_fixed_diagnostic.sh'
        path.write_text(launcher)
        (self.repo / 'ci/openpmix_fixed_diagnostic.py').write_text(
            'import os,json,pathlib\npathlib.Path("received.json").write_text(json.dumps(dict(os.environ)))\n')
        # Launcher requires a fresh job root.
        shutil.rmtree(self.root)
        env = dict(os.environ, **self.env, CI_JOB_TOKEN='secret', GITHUB_STATUS_TOKEN='secret',
                   CUSTOM_SECRET_SENTINEL='secret', PRRTE_COMMIT='f' * 40)
        result = subprocess.run(['bash', str(path)], cwd=self.repo, env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        received = json.loads((self.repo / 'received.json').read_text())
        for key in ('CI_JOB_TOKEN', 'GITHUB_STATUS_TOKEN', 'CUSTOM_SECRET_SENTINEL', 'PRRTE_COMMIT'):
            self.assertNotIn(key, received)
        self.assertEqual(received['CI_JOB_ID'], '123')
        self.assertEqual(received['CI_COMMIT_SHA'], 'a' * 40)

    def test_pipeline_is_explicit_web_only_and_excludes_normal_execution(self):
        parent = yaml.safe_load((ROOT / '.gitlab-ci.yml').read_text())
        jobs = yaml.safe_load((ROOT / 'ci/openpmix_fixed_diagnostic.yml').read_text())
        job = jobs['run-openpmix-fixed-diagnostic']
        self.assertEqual(job['extends'], ['.frontier-shell-runner'])
        self.assertEqual(job['rules'][0]['if'], '$CI_PIPELINE_SOURCE == "web" && $OPENPMIX_FIXED_DIAGNOSTIC == "1"')
        self.assertEqual(job['rules'][-1], {'when': 'never'})
        self.assertEqual(parent['pmix-python-suite']['rules'][0],
                         {'if': '$OPENPMIX_FIXED_DIAGNOSTIC == "1"', 'when': 'never'})
        pr = yaml.safe_load((ROOT / 'ci/openpmix_pr_jobs.yml').read_text())
        for name in ('.openpmix-pr-rules', '.openpmix-pr-always-rules', '.openpmix-baseline-rules', 'prepare-openpmix-baseline'):
            self.assertEqual(pr[name]['rules'][0], {'if': '$OPENPMIX_FIXED_DIAGNOSTIC == "1"', 'when': 'never'})
        self.assertNotIn('report_github', str(job))
        self.assertNotIn('apply_reconciled', str(job))


if __name__ == '__main__':
    unittest.main()
