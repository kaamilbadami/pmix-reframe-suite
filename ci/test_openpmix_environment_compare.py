import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import yaml
import openpmix_environment_compare as diagnostic

ROOT = Path(__file__).resolve().parents[1]


class EnvironmentComparisonTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name)
        (self.repo / 'ci').mkdir()
        (self.repo / 'sysconfig.yaml').write_bytes((ROOT / 'sysconfig.yaml').read_bytes())
        self.root = self.repo / '.ci-work/123'
        self.root.mkdir(parents=True)
        self.source = dict(CI_JOB_ID='123', CI_PIPELINE_ID='456', CI_COMMIT_SHA='a' * 40,
                           PATH='/normal/bin:/usr/bin:/bin', HOME='/normal/home',
                           BASH_ENV='/site/lmod', CONDA_PREFIX='/miniforge',
                           LMOD_CMD='/site/lmod/libexec/lmod', LMOD_DIR='/site/lmod/libexec',
                           MODULEPATH='/site/modules', LD_LIBRARY_PATH='/site/libfabric/lib64',
                           CI_JOB_TOKEN='secret', CUSTOM_SECRET='secret')

    def test_environment_preserves_startup_settings_only_in_normal_case(self):
        normal = diagnostic.environment(self.source, self.root, 'normal')
        clean = diagnostic.environment(self.source, self.root, 'clean')
        self.assertEqual(normal['CONDA_PREFIX'], '/miniforge')
        self.assertNotIn('CONDA_PREFIX', clean)
        self.assertNotIn('BASH_ENV', clean)
        self.assertEqual(clean['LMOD_CMD'], self.source['LMOD_CMD'])
        self.assertEqual(clean['MODULEPATH'], self.source['MODULEPATH'])
        for env in (normal, clean):
            self.assertEqual(env['TMPDIR'], '/tmp')
            self.assertEqual(env['LD_LIBRARY_PATH'], '/site/libfabric/lib64')
            self.assertNotIn('CI_JOB_TOKEN', env)
            self.assertNotIn('CUSTOM_SECRET', env)
            self.assertEqual(env['PMIX_COMMIT'], diagnostic.PMIX_SHA)
            self.assertEqual(env['PRRTE_COMMIT'], diagnostic.PRRTE_SHA)
            self.assertEqual(env['PMIX_PYTHON'], str(self.root / 'venv/bin/python'))

    def test_clean_config_changes_export_and_both_modes_record_initialization(self):
        with patch.object(diagnostic, 'ROOT', self.repo):
            normal = yaml.safe_load(diagnostic.config(self.root, 'normal').read_text())
            clean = yaml.safe_load(diagnostic.config(self.root, 'clean').read_text())
        np = normal['systems'][0]['partitions'][0]
        cp = clean['systems'][0]['partitions'][0]
        self.assertNotIn('--export=NIL', np['access'])
        self.assertIn('--export=NIL', cp['access'])
        self.assertEqual(np['prepare_cmds'], cp['prepare_cmds'])
        self.assertIn('export TMPDIR=/tmp', np['prepare_cmds'])
        self.assertIn('site-init-start', np['prepare_cmds'][0])
        self.assertIn('site-init-finished', np['prepare_cmds'][-2])

    def test_only_one_unchanged_negative_runtime_check_is_selected(self):
        command = diagnostic.command(self.root, self.root / 'config.yaml')
        self.assertEqual(command[command.index('-n') + 1], 'PMIxPythonChildTimeoutTest')
        self.assertNotIn('run_pmix_python_suite.sh', str(command))
        self.assertIn('--exec-policy=serial', command)

    def test_both_modes_run_even_if_normal_fails_and_keep_evidence_separate(self):
        seen = []
        def run(command, **kwargs):
            seen.append(kwargs['env'])
            index = len(seen)
            cases = [dict(name=diagnostic.CHECK, fixture=False,
                          result='fail' if index == 1 else 'pass', jobid=str(index))]
            cases += [dict(name='fixture' + str(n), fixture=True, result='pass') for n in range(6)]
            (self.root / 'reframe/run-report.json').write_text(json.dumps(dict(runs=[dict(testcases=cases)])))
            return subprocess.CompletedProcess(command, 1 if index == 1 else 0)
        with patch.object(diagnostic, 'ROOT', self.repo), \
             patch.object(diagnostic, 'context', return_value=self.root), \
             patch.object(diagnostic, 'checkout_exact') as checkout, \
             patch.object(diagnostic.subprocess, 'run', side_effect=run):
            self.assertEqual(diagnostic.run(self.source), 1)
        checkout.assert_called_once_with(self.root / 'openpmix', diagnostic.PMIX_SHA)
        result = json.loads((self.root / 'environment-comparison.json').read_text())
        self.assertEqual(result['status'], 'complete')
        self.assertEqual([a['status'] for a in result['attempts']], ['failed', 'passed'])
        self.assertEqual(len(seen), 2)
        self.assertTrue((self.root / 'comparisons/normal/reframe/run-report.json').is_file())
        self.assertTrue((self.root / 'comparisons/clean/reframe/run-report.json').is_file())
        self.assertNotIn('secret', (self.root / 'comparisons/normal/startup-settings.json').read_text())

    def test_missing_reports_leave_overall_comparison_incomplete(self):
        with patch.object(diagnostic, 'ROOT', self.repo), \
             patch.object(diagnostic, 'context', return_value=self.root), \
             patch.object(diagnostic, 'checkout_exact'), \
             patch.object(diagnostic.subprocess, 'run',
                          return_value=subprocess.CompletedProcess([], 1)):
            self.assertEqual(diagnostic.run(self.source), 1)
        summary = json.loads((self.root / 'environment-comparison.json').read_text())
        self.assertEqual(summary['status'], 'incomplete')
        self.assertEqual(len(summary['attempts']), 2)

    def test_missing_report_never_becomes_pass(self):
        result = diagnostic.result(self.root, 'clean', 0, 'now', 1)
        self.assertEqual(result['status'], 'incomplete')

    def test_fixed_job_remains_opt_in_and_collects_both_modes(self):
        config = yaml.safe_load((ROOT / 'ci/openpmix_fixed_diagnostic.yml').read_text())
        job = config['run-openpmix-fixed-diagnostic']
        self.assertEqual(job['rules'][0]['if'], '$CI_PIPELINE_SOURCE == "web" && $OPENPMIX_FIXED_DIAGNOSTIC == "1"')
        self.assertIn('environment-comparison.json', str(job['artifacts']))
        self.assertIn('comparisons/*/reframe/run-report.json', str(job['artifacts']))
        self.assertNotIn('apply_reconciled', str(job))
        self.assertIn('OPENPMIX_ENV_COMPARE', (ROOT / 'ci/run_openpmix_fixed_diagnostic.sh').read_text())


if __name__ == '__main__':
    unittest.main()
