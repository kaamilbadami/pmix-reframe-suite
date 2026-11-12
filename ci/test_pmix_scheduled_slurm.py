"""Regression checks for scheduled execution through the Jacamar Slurm runner."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import yaml

import generate_pmix_child_pipeline as generator
import pmix_scheduled_sources as staging


ROOT = Path(__file__).resolve().parents[1]
PMIX = 'a' * 40
PRRTE = 'b' * 40


class PipelineTests(unittest.TestCase):
    def test_two_commit_jobs_have_exact_inputs_and_separate_preparation(self):
        commits = [PMIX, 'c' * 40]
        selections = {sha: ('master', PRRTE) for sha in commits}
        pipeline = yaml.safe_load(generator.render_pipeline(
            commits, prrte_selections=selections, slurm_runner=True))
        self.assertEqual(pipeline['stages'], ['prepare', 'test'])
        for sha in commits:
            preparation = pipeline['prepare-pmix-' + sha]
            execution = pipeline['pmix-' + sha]
            self.assertEqual(preparation['extends'], ['.frontier-shell-runner'])
            self.assertEqual(execution['tags'], ['frontier', 'slurm'])
            self.assertEqual(execution['timeout'], '6h')
            self.assertEqual(execution['variables']['RUNNER_SCRIPT_TIMEOUT'], '5h45m')
            self.assertEqual(execution['variables']['PRRTE_COMMIT'], PRRTE)
            self.assertEqual(execution['needs'], [
                {'job': 'prepare-pmix-' + sha, 'artifacts': True}])
            self.assertIn('--nodes=4', execution['variables']['SCHEDULER_PARAMETERS'])
            self.assertIn('--time=00:30:00', execution['variables']['SCHEDULER_PARAMETERS'])
            self.assertEqual(execution['after_script'][-1],
                             'bash ci/write_pmix_commit_result.sh ci-results')
            self.assertIn('||', execution['after_script'][0])
            self.assertNotIn('resource_group', execution)

    def test_existing_shell_path_and_empty_batch(self):
        shell = yaml.safe_load(generator.render_pipeline([PMIX]))
        self.assertEqual(shell['pmix-' + PMIX]['timeout'], '2h')
        empty = yaml.safe_load(generator.render_pipeline([], slurm_runner=True))
        self.assertIn('no-untested-pmix-commits', empty)
        self.assertFalse(any(name.startswith('prepare-pmix-') for name in empty))

    def test_slurm_path_rejects_missing_pins_and_failure_pilot(self):
        with self.assertRaises(SystemExit):
            generator.render_pipeline([PMIX], slurm_runner=True)
        with self.assertRaises(SystemExit):
            generator.render_pipeline([PMIX], fail_commit=PMIX, slurm_runner=True)

    def test_scheduled_switch_is_forwarded_without_forwarding_pipeline_variables(self):
        parent = yaml.safe_load((ROOT / '.gitlab-ci.yml').read_text())
        batch = parent['run-pmix-scheduled-batch']
        self.assertEqual(parent['variables']['PMIX_SCHEDULED_SLURM'], '0')
        self.assertEqual(batch['variables']['PMIX_SCHEDULED_SLURM_INTERNAL'],
                         '$PMIX_SCHEDULED_SLURM')
        self.assertFalse(batch['trigger']['forward']['pipeline_variables'])
        script = '\n'.join(parent['generate-pmix-child-pipeline-scheduled']['script'])
        self.assertIn('PMIX_SCHEDULED_SLURM_INTERNAL', script)
        self.assertIn('executor_options=(--slurm-runner)', script)


class ExecutionTests(unittest.TestCase):
    def test_only_selected_environment_reaches_offline_serial_runner(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            ci = repo / 'ci'
            ci.mkdir()
            site = repo / 'site.sh'
            site.write_text('module() { :; }\n')
            runner = (ROOT / 'ci/run_pmix_scheduled_allocation.sh').read_text()
            runner = runner.replace(
                '/lustre/orion/gen243/proj-shared/pmix-reframe-ci-tools/pmix-py310/bin/python',
                sys.executable).replace(
                '/lustre/orion/gen243/proj-shared/pmix-reframe-ci-tools/reframe-4.10/bin/reframe',
                '/bin/true').replace('/etc/profile.d/olcf-env.sh', str(site)).replace(
                '/etc/bash.bashrc.local', str(site))
            (ci / 'run_pmix_scheduled_allocation.sh').write_text(runner)
            (ci / 'pmix_scheduled_sources.py').write_text(
                'import os, pathlib, sys\n'
                'if sys.argv[1] == "allocation":\n'
                ' pathlib.Path(".ci-work/123").mkdir(parents=True)\n'
                ' print("456 frontier[01-04]")\n'
                'else:\n'
                ' assert "CI_JOB_TOKEN" not in os.environ\n')
            (ci / 'run_exact_pmix_commit.sh').write_text(
                '#!/bin/bash\n/usr/bin/env -0 > received.env\nexit 7\n')
            env = dict(os.environ, PMIX_COMMIT=PMIX, PRRTE_COMMIT=PRRTE,
                       PRRTE_BRANCH='master', CI_JOB_ID='123', CI_PIPELINE_ID='789',
                       SLURM_JOB_ID='456', CI_JOB_TOKEN='secret', GITHUB_STATUS_TOKEN='secret',
                       SCHEDULED_SECRET_SENTINEL='secret', LD_PRELOAD='')
            result = subprocess.run(['bash', str(ci / 'run_pmix_scheduled_allocation.sh')],
                                    env=env, cwd=repo, capture_output=True)
            self.assertEqual(result.returncode, 7, result.stderr.decode())
            received = dict(row.split('=', 1) for row in
                            (repo / 'received.env').read_text().split('\0') if row)
            for key in ('CI_JOB_TOKEN', 'GITHUB_STATUS_TOKEN', 'SCHEDULED_SECRET_SENTINEL'):
                self.assertNotIn(key, received)
            self.assertEqual(received['PMIX_COMMIT'], PMIX)
            self.assertEqual(received['PRRTE_COMMIT'], PRRTE)
            self.assertEqual(received['SLURM_JOB_ID'], '456')
            self.assertEqual(received['PMIX_RFM_EXEC_POLICY'], 'serial')
            self.assertEqual(received['PMIX_RFM_CONFIG_FILE'],
                             str(repo / 'ci/openpmix_single_allocation_config.py'))
            self.assertEqual(received['PMIX_SINGLE_OFFLINE_SOURCES'],
                             str(repo / 'ci-scheduled-restored/sources/cache'))
            # A retry uses a fresh GitLab workspace. Refuse stale local state.
            retried = subprocess.run(['bash', str(ci / 'run_pmix_scheduled_allocation.sh')],
                                     env=env, cwd=repo, capture_output=True)
            self.assertNotEqual(retried.returncode, 0)

    def test_source_verification_rejects_changed_commit_and_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'openpmix').mkdir()
            (root / 'cache').mkdir()
            manifest = root / 'cache/manifest.json'
            expected = dict(pmix=PMIX, prrte=PRRTE, branch='master')
            manifest.write_text(json.dumps(expected))
            env = dict(PMIX_COMMIT=PMIX, PRRTE_COMMIT=PRRTE, PRRTE_BRANCH='master')
            values = [PMIX, staging.PMIX_URL, '']
            with patch.object(staging.sources, 'git', side_effect=values), \
                    patch.object(staging.sources, 'checked_tree'), \
                    patch.object(staging, 'validate'), \
                    patch.object(staging.subprocess, 'run', return_value=subprocess.CompletedProcess([], 1)):
                staging.verify(root, env)
            with patch.object(staging.sources, 'git', return_value='c' * 40):
                with self.assertRaises(ValueError):
                    staging.verify(root, env)
            manifest.write_text(json.dumps(dict(expected, prrte='d' * 40)))
            with patch.object(staging.sources, 'git', side_effect=values), \
                    patch.object(staging.sources, 'checked_tree'), \
                    patch.object(staging, 'validate'), \
                    patch.object(staging.subprocess, 'run', return_value=subprocess.CompletedProcess([], 1)):
                with self.assertRaises(ValueError):
                    staging.verify(root, env)


if __name__ == '__main__':
    unittest.main()
