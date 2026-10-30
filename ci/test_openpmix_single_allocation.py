"""Offline allocation lifecycle, credential boundary, and report validation."""
import copy
import json
import os
from pathlib import Path
import runpy
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import cleanup_openpmix_allocation as cleanup
import openpmix_pr_artifacts as records
import openpmix_pr_baseline as baseline
import pmix_single_sources as sources

ROOT = Path(__file__).resolve().parents[1]


class AllocationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name)
        ci = self.repo / 'ci'
        ci.mkdir()
        for name in ('run_openpmix_single_allocation.sh', 'openpmix_pr_baseline.py',
                     'openpmix_pr_artifacts.py', 'openpmix_pr_sysconfig.yaml',
                     'cleanup_openpmix_allocation.py', 'cleanup_pmix_slurm_jobs.py',
                     'monitor_pmix_allocation.py'):
            shutil.copy(ROOT / 'ci' / name, ci / name)
        (ci / 'pmix_single_sources.py').write_text(
            'import pathlib, sys\npathlib.Path(sys.argv[2]).mkdir(parents=True)\n')
        (ci / 'run_exact_pmix_commit.sh').write_text(
            '#!/bin/bash\n/usr/bin/env -0 > received.env\nexit "${MOCK_RESULT:-0}"\n')
        prep = self.repo / baseline.PREPARATION.parent
        prep.mkdir()
        (prep / 'preparation.env').write_text(''.join(
            k + '=' + v + '\n' for k, v in baseline.preparation('123').items()))
        (prep / 'baseline.env').write_text(''.join(
            k + '=' + v + '\n' for k, v in baseline.manifest('123').items()))
        self.bin = self.repo / 'bin'
        self.bin.mkdir()
        self.env = dict(PATH=str(self.bin) + ':/usr/bin:/bin', HOME=str(self.repo),
                        TMPDIR=str(self.repo), PYTHONPATH='', CI_PIPELINE_ID='123',
                        PMIX_PYTHON=sys.executable, RFM_BIN='/fixed/reframe',
                        PMIX_COMMIT=baseline.PMIX_SHA, PRRTE_COMMIT=baseline.PRRTE_SHA,
                        PRRTE_BRANCH='master', PMIX_TRUSTED_SOURCE_DIR=str(self.repo / 'checkout'),
                        PMIX_RFM_PREFIX=str(self.repo / 'ci-openpmix-pr-execution/reframe'),
                        PMIX_RFM_REPORT_FILE=str(self.repo / 'ci-openpmix-pr-execution/reframe/report.json'),
                        GITHUB_PR_READ_TOKEN='SECRET_SENTINEL', CI_JOB_TOKEN='SECRET_SENTINEL')
        self.fake('squeue', 'exit 0')
        self.fake('scontrol', 'exit 1')
        self.fake('sacct', 'echo "42|submit|eligible|start|end|0:10|COMPLETED|0:0"')
        self.fake('sbatch', 'printf "%s\\n" "$@" > "$HOME/submitted"; echo 42; sleep 0.3; exit 0')

    def fake(self, name, body):
        path = self.bin / name
        path.write_text('#!/bin/bash\n' + body + '\n')
        path.chmod(0o755)

    def launch(self):
        return subprocess.run(['/bin/bash', 'ci/run_openpmix_single_allocation.sh'],
                              cwd=self.repo, env=self.env, text=True,
                              capture_output=True, timeout=15)

    @property
    def allocation(self):
        return Path(self.env['PMIX_RFM_PREFIX']) / 'stage/single-allocation-123'

    def test_one_allocation_serial_full_suite_and_no_caller_credentials(self):
        result = self.launch()
        self.assertEqual(result.returncode, 0, result.stderr)
        args = (self.repo / 'submitted').read_text().splitlines()
        for arg in ('--nodes=4', '--ntasks=256', '--ntasks-per-node=64',
                    '--core-spec=0', '--threads-per-core=1', '--wait',
                    '--time=00:45:00', '--export=NIL'):
            self.assertIn(arg, args)
        self.assertEqual(sum(x.endswith('/rfm_job.sh') for x in args), 1)
        script = (self.allocation / 'rfm_job.sh').read_text()
        self.assertIn('PMIX_RFM_EXEC_POLICY=serial', script)
        self.assertIn('exec /bin/bash ci/run_exact_pmix_commit.sh', script)
        self.assertNotIn('SECRET_SENTINEL', script)
        self.assertNotIn('sbatch', script)
        # Execute the generated environment/runner portion in an empty env.
        # Site initialization is separately covered by its executable tests.
        body = script[script.index('cd -- '):]
        ran = subprocess.run(['/bin/bash', '-euc', body], env={'PATH': '/usr/bin:/bin'},
                             capture_output=True, text=True, timeout=5)
        self.assertEqual(ran.returncode, 0, ran.stderr)
        entries = (self.repo / 'received.env').read_bytes().split(b'\0')
        self.assertFalse(any(x.startswith((b'CI_JOB_TOKEN=', b'GITHUB_PR_READ_TOKEN=')) for x in entries))
        self.assertIn(b'SLURM_EXPORT_ENV=ALL', entries)
        diagnostics = (self.allocation / 'allocation-diagnostics.jsonl').read_text()
        self.assertIn('COMPLETED', diagnostics)

    def test_slurm_failure_status_propagates(self):
        self.fake('sbatch', 'echo 42; exit 9')
        self.assertEqual(self.launch().returncode, 9)

    def test_runner_owned_allocation_does_not_submit_or_download(self):
        self.env.update(OPENPMIX_RUNNER_ALLOCATION='1', SLURM_JOB_ID='42',
                        SLURM_JOB_NODELIST='frontier[1-4]')
        (self.repo / 'ci/openpmix_baseline_staging.py').write_text(
            'import pathlib\npathlib.Path("allocation-recorded").touch()\n')
        result = self.launch()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.repo / 'allocation-recorded').exists())
        self.assertFalse((self.repo / 'submitted').exists())
        self.assertFalse((self.repo / 'ci-openpmix-pr-execution/sources').exists())
        received = (self.repo / 'received.env').read_bytes()
        self.assertIn(b'PMIX_RFM_EXEC_POLICY=serial\0', received)
        self.assertIn(b'SLURM_EXPORT_ENV=ALL\0', received)

    def test_suite_uses_serial_policy_and_preserves_test_failure(self):
        shutil.copy(ROOT / 'ci/run_pmix_python_suite.sh', self.repo / 'ci')
        (self.repo / 'Cython.py').write_text('__version__ = "test-only"\n')
        tests = self.repo / 'pmix_python_binding/unit_tests'
        tests.mkdir(parents=True)
        (tests / 'test_stub.py').write_text('import unittest\nclass T(unittest.TestCase):\n    def test_ok(self): pass\n')
        (self.repo / 'sysconfig.yaml').write_text('test-only')
        self.fake('cython', 'exit 0')
        self.fake('reframe', '''case " $* " in
          *" --version "*) echo 4.10.0 ;;
          *" -l "*) echo 'Found 11 check(s)' ;;
          *) printf '%s\\n' "$@" > "$HOME/rfm-args"; exit 7 ;;
        esac''')
        env = {**self.env, 'RFM_BIN': str(self.bin / 'reframe'), 'PYTHONPATH': str(self.repo)}
        for policy in ('serial', ''):
            result = subprocess.run(['/bin/bash', 'ci/run_pmix_python_suite.sh'], cwd=self.repo,
                                    env={**env, 'PMIX_RFM_EXEC_POLICY': policy},
                                    text=True, capture_output=True, timeout=5)
            self.assertEqual(result.returncode, 7, result.stdout + result.stderr)
            args = (self.repo / 'rfm-args').read_text().splitlines()
            self.assertEqual('--exec-policy=serial' in args, policy == 'serial')
            self.assertNotIn('--maxfail=1', args)

    def test_invalid_baseline_never_submits(self):
        (self.repo / baseline.MANIFEST).unlink()
        self.assertNotEqual(self.launch().returncode, 0)
        self.assertFalse((self.repo / 'submitted').exists())

    def test_failed_source_preparation_never_submits(self):
        (self.repo / 'ci/pmix_single_sources.py').write_text('raise SystemExit(7)\n')
        self.assertEqual(self.launch().returncode, 7)
        self.assertFalse((self.repo / 'submitted').exists())

    def test_local_report_cli_requires_baseline_marker(self):
        tool = self.repo / 'ci/openpmix_pr_artifacts.py'
        def call(*args):
            return subprocess.run([sys.executable, str(tool), *args], cwd=self.repo,
                                  capture_output=True, text=True, timeout=5)
        prepared = str(baseline.PREPARATION)
        result = call('write-checkout', '--preparation', prepared, '--pipeline-id', '123',
                      '--fetched-sha', baseline.PMIX_SHA, '--checked-out-sha', baseline.PMIX_SHA,
                      '--output', 'checkout.env')
        self.assertEqual(result.returncode, 0, result.stderr)
        args = ('classify-report', '--preparation', prepared, '--pipeline-id', '123',
                '--checkout', 'checkout.env', '--report', 'missing.json', '--execution-id', 'c' * 32,
                '--runner-exit-status', '0', '--output', 'result.env', '--baseline-single-allocation')
        cases = [dict(name=name, system='frontier', partition='batch', environ='pmix_test',
                      scheduler='local', fixture=fixture, result='pass')
                 for fixture, names in [(True, records.EXPECTED_FIXTURES), (False, records.EXPECTED_TESTS)]
                 for name in names]
        document = dict(session_info=dict(version='4.10.0', data_version='4.2'), restored_cases=[],
                        runs=[dict(run_index=0, num_cases=17, num_failures=0,
                                   num_aborted=0, num_skipped=0, testcases=cases)])
        (self.repo / 'missing.json').write_text(json.dumps(document))
        result = call(*args)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('RESULT=success', (self.repo / 'result.env').read_text())
        (self.repo / 'result.env').unlink()
        (self.repo / baseline.MANIFEST).unlink()
        result = call(*args)
        self.assertEqual(result.returncode, 2)
        self.assertIn('restricted to the fixed baseline', result.stderr)
        self.assertFalse((self.repo / 'result.env').exists())

    def test_term_cancels_only_owned_allocation(self):
        self.fake('sbatch', 'echo 42; touch "$HOME/started"; exec sleep 30')
        record = ('JobId=42 JobState=PENDING UserId=test(' + str(os.getuid()) + ') WorkDir=' +
                  str(self.allocation) + ' Command=' + str(self.allocation / 'rfm_job.sh'))
        self.fake('squeue', 'echo 42')
        self.fake('scontrol', 'echo ' + shlex.quote(record))
        self.fake('scancel', 'echo "$1" > "$HOME/canceled"')
        process = subprocess.Popen(['/bin/bash', 'ci/run_openpmix_single_allocation.sh'],
                                   cwd=self.repo, env=self.env,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            deadline = time.monotonic() + 5
            while not (self.repo / 'started').exists() and time.monotonic() < deadline:
                time.sleep(.02)
            self.assertTrue((self.repo / 'started').exists())
            process.send_signal(signal.SIGTERM)
            self.assertEqual(process.wait(timeout=8), 143)
            self.assertEqual((self.repo / 'canceled').read_text().strip(), '42')
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()


class GuardTests(unittest.TestCase):
    def config(self, hosts, hostname, env=None):
        with patch.dict(os.environ, env or {'SLURM_JOB_ID': '42', 'SLURM_JOB_NODELIST': 'frontier[1-4]'}, clear=True), \
             patch('subprocess.check_output', return_value=hosts), \
             patch('socket.gethostname', return_value=hostname):
            return runpy.run_path(str(ROOT / 'ci/openpmix_single_allocation_config.py'))['site_configuration']

    def test_config_requires_four_nodes_and_first_compute_host(self):
        for hosts, host in [('frontier1 frontier2', 'frontier1'),
                            ('frontier1 frontier2 frontier3 frontier4', 'login03'),
                            ('frontier1 frontier2 frontier3 frontier4', 'frontier2')]:
            with self.assertRaises(RuntimeError):
                self.config(hosts, host)
        with self.assertRaises(RuntimeError):
            self.config('', 'login03', {'OTHER': 'value'})
        config = self.config('frontier1 frontier2 frontier3 frontier4', 'frontier1')
        self.assertEqual(config['systems'][0]['partitions'][0]['scheduler'], 'local')
        self.assertEqual(config['systems'][0]['partitions'][0]['max_jobs'], 1)
        self.assertNotIn('access', config['systems'][0]['partitions'][0])

    def test_cleanup_rejects_other_uid_pipeline_and_stage(self):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            stage = repo / 'ci-openpmix-pr-execution/reframe/stage'
            for uid, directory, expected in [(22, stage / 'single-allocation-123', ['42']),
                    (23, stage / 'single-allocation-123', []),
                    (22, stage / 'single-allocation-124', []),
                    (22, repo / 'other', [])]:
                commands = []
                record = f'JobId=42 JobState=PENDING UserId=test({uid}) WorkDir={directory} Command={directory}/rfm_job.sh'
                def run(argv, **kwargs):
                    commands.append(argv)
                    return subprocess.CompletedProcess(argv, 0, '42' if argv[0] == 'squeue' else record, '')
                report = cleanup.cleanup(repo, '123', run=run, uid=22)
                self.assertEqual(report['canceled'], expected)
                self.assertEqual(['scancel', '42'] in commands, bool(expected))

    def test_local_report_requires_explicit_scheduler_and_exact_full_suite(self):
        cases = [dict(name=name, system='frontier', partition='batch', environ='pmix_test',
                      scheduler='local', fixture=fixture, result='pass')
                 for fixture, names in [(True, records.EXPECTED_FIXTURES), (False, records.EXPECTED_TESTS)]
                 for name in names]
        document = dict(session_info=dict(version='4.10.0', data_version='4.2'), restored_cases=[],
                        runs=[dict(run_index=0, num_cases=17, num_failures=0,
                                   num_aborted=0, num_skipped=0, testcases=cases)])
        self.assertEqual(records.classify_reframe_report(document, 0, 'local'), ('success', 'tests-passed'))
        with self.assertRaises(records.RecordError):
            records.classify_reframe_report(document, 0)
        document['runs'][0]['testcases'].pop()
        with self.assertRaises(records.RecordError):
            records.classify_reframe_report(document, 0, 'local')

    def test_source_cache_checks_pins_and_archive_digest(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            destination = root / 'dest'
            destination.mkdir()
            archive = root / sources.TARBALL
            archive.write_bytes(b'fixture')
            env = dict(PMIX_COMMIT=baseline.PMIX_SHA, PRRTE_COMMIT=baseline.PRRTE_SHA, PRRTE_BRANCH='master')
            manifest = sources.pins(env)
            import hashlib
            manifest['libevent_sha256'] = hashlib.sha256(archive.read_bytes()).hexdigest()
            (root / 'manifest.json').write_text(json.dumps(manifest))
            sources.consume('libevent', root, destination, env)
            archive.write_bytes(b'changed')
            with self.assertRaises(ValueError):
                sources.consume('libevent', root, destination, env)
            with self.assertRaises(ValueError):
                sources.consume('libevent', root, destination, {**env, 'PMIX_COMMIT': 'a' * 40})


if __name__ == '__main__':
    unittest.main()
