"""Offline tests: submission count, failures, allocation guards and cleanup scope."""
import importlib.util
import os
from pathlib import Path
import runpy
import shutil
import signal
import sys
import time
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('cleanup', ROOT / 'ci/cleanup_pmix_slurm_jobs.py')
cleanup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cleanup)


class PilotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name)
        (self.repo / 'ci').mkdir()
        for name in ('run_pmix_single_allocation.sh', 'cleanup_pmix_slurm_jobs.py'):
            shutil.copy(ROOT / 'ci' / name, self.repo / 'ci' / name)
        (self.repo / 'ci/pmix_pilot_sources.py').write_text(
            'import os, pathlib, sys\n'
            'pathlib.Path(os.environ["CI_PROJECT_DIR"], "prepared").touch()\n'
            'sys.exit(int(os.environ.get("SOURCE_FAIL", "0")))\n')
        self.bin = self.repo / 'bin'
        self.bin.mkdir()
        self.env = dict(os.environ, CI_PROJECT_DIR=str(self.repo), CI_JOB_ID='123',
                        PMIX_COMMIT='a' * 40, PRRTE_COMMIT='b' * 40,
                        PMIX_PYTHON='/usr/bin/python3', RFM_BIN='/unused/reframe',
                        PATH=str(self.bin) + os.pathsep + os.environ['PATH'])
        self.fake('sbatch', 'printf "%s\\n" "$@" > "$CI_PROJECT_DIR/submitted"; exit "${FAKE_EXIT:-0}"')
        self.fake('squeue', 'exit 0')

    def fake(self, name, body):
        f = self.bin / name
        f.write_text('#!/bin/bash\n' + body + '\n')
        f.chmod(0o755)

    def launch(self):
        return subprocess.run(['bash', 'ci/run_pmix_single_allocation.sh'],
                              cwd=self.repo, env=self.env, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, universal_newlines=True, timeout=10)

    def test_one_four_node_allocation_and_serial_execution(self):
        result = self.launch()
        self.assertEqual(result.returncode, 0, result.stderr)
        args = (self.repo / 'submitted').read_text().splitlines()
        self.assertTrue((self.repo / 'prepared').exists())
        self.assertIn('--nodes=4', args)
        self.assertIn('--wait', args)
        self.assertIn('--time=00:45:00', args)
        self.assertEqual(sum(a.endswith('/rfm_job.sh') for a in args), 1)
        batch = self.repo / '.ci-work/123/reframe/stage/single-allocation/rfm_job.sh'
        self.assertIn('exec bash ci/run_exact_pmix_commit.sh', batch.read_text())
        self.assertNotIn('sbatch', batch.read_text())
        # Reusing an identity must not submit another allocation.
        (self.repo / 'submitted').unlink()
        self.assertNotEqual(self.launch().returncode, 0)
        self.assertFalse((self.repo / 'submitted').exists())

    def test_source_failure_never_requests_allocation(self):
        self.env['SOURCE_FAIL'] = '4'
        self.assertEqual(self.launch().returncode, 4)
        self.assertFalse((self.repo / 'submitted').exists())

    def test_failed_allocation_propagates_exit_status(self):
        self.env['FAKE_EXIT'] = '9'
        self.assertEqual(self.launch().returncode, 9)

    def test_missing_or_invalid_pins_never_submit(self):
        for key, value in [('PMIX_COMMIT', ''), ('PRRTE_COMMIT', 'main'),
                           ('CI_JOB_ID', '../123'), ('PRRTE_BRANCH', 'bad branch')]:
            with self.subTest(key=key):
                env = self.env.copy()
                self.env[key] = value
                self.assertNotEqual(self.launch().returncode, 0)
                self.assertFalse((self.repo / 'submitted').exists())
                self.env = env

    def test_cleanup_recognizes_outer_allocation_but_not_other_child(self):
        root = self.repo / '.ci-work/123'
        def record(base):
            cwd = str(base / 'reframe/stage/single-allocation')
            return ('JobId=42 UserId=gen243_auser(20841) WorkDir=' + cwd +
                    ' Command=' + cwd + '/rfm_job.sh')
        self.assertTrue(cleanup.owned_job(record(root), '42', root, 20841))
        self.assertFalse(cleanup.owned_job(record(self.repo / '.ci-work/124'), '42', root, 20841))
        commands = []
        def run(argv, **kwargs):
            commands.append(argv)
            output = '42' if argv[0] == 'squeue' else record(root)
            return subprocess.CompletedProcess(argv, 0, output, '')
        env = dict(self.env, CI_JOB_STATUS='running')
        report = cleanup.cleanup(env, run=run, uid=20841)
        self.assertEqual(report['canceled'], ['42'])
        self.assertIn(['scancel', '42'], commands)

    def config(self, hosts, hostname, allocated=True):
        env = {'SLURM_JOB_ID': '42', 'SLURM_JOB_NODELIST': 'frontier[1-4]'} if allocated else {}
        with patch.dict(os.environ, env, clear=True), \
             patch('subprocess.check_output', return_value=hosts), \
             patch('socket.gethostname', return_value=hostname):
            return runpy.run_path(str(ROOT / 'sysconfig-single-allocation.py'))

    def test_config_refuses_login_node_and_wrong_allocation(self):
        for hosts, hostname, allocated in [('', 'login06', False),
                ('frontier1 frontier2', 'frontier1', True),
                ('frontier1 frontier2 frontier3 frontier4', 'login06', True),
                ('frontier1 frontier2 frontier3 frontier4', 'frontier2', True)]:
            with self.assertRaises(RuntimeError):
                self.config(hosts, hostname, allocated)

    def test_config_uses_local_scheduler_and_one_test_at_a_time(self):
        config = self.config('frontier1 frontier2 frontier3 frontier4', 'frontier1')
        system = config['site_configuration']['systems'][0]
        self.assertEqual(system['max_local_jobs'], 1)
        partition = system['partitions'][0]
        self.assertEqual(partition['scheduler'], 'local')
        self.assertEqual(partition['max_jobs'], 1)

    def test_term_cancels_only_the_owned_outer_allocation(self):
        self.fake('sbatch', 'touch "$CI_PROJECT_DIR/started"; exec sleep 30')
        self.fake('squeue', 'echo 42')
        cwd = self.repo / '.ci-work/123/reframe/stage/single-allocation'
        record = ('JobId=42 UserId=test(' + str(os.getuid()) + ') WorkDir=' +
                  str(cwd) + ' Command=' + str(cwd / 'rfm_job.sh'))
        self.fake('scontrol', 'echo ' + repr(record))
        self.fake('scancel', 'echo "$1" > "$CI_PROJECT_DIR/canceled"')
        process = subprocess.Popen(['bash', 'ci/run_pmix_single_allocation.sh'],
                                   cwd=self.repo, env=self.env,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            deadline = time.monotonic() + 5
            while not (self.repo / 'started').exists() and time.monotonic() < deadline:
                time.sleep(.02)
            self.assertTrue((self.repo / 'started').exists())
            process.send_signal(signal.SIGTERM)
            self.assertEqual(process.wait(timeout=5), 143)
            self.assertEqual((self.repo / 'canceled').read_text().strip(), '42')
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()

    @unittest.skipUnless(importlib.util.find_spec('Cython'), 'requires Cython')
    def test_suite_serial_policy_and_failure_propagation(self):
        shutil.copy(ROOT / 'ci/run_pmix_python_suite.sh', self.repo / 'ci')
        (self.repo / 'pmix_python_binding/unit_tests').mkdir(parents=True)
        (self.repo / 'pmix_python_binding/unit_tests/test_stub.py').write_text(
            'import unittest\nclass Stub(unittest.TestCase):\n    def test_stub(self): pass\n')
        (self.repo / 'sysconfig.yaml').write_text('placeholder')
        self.fake('reframe', '''case " $* " in
          *" --version "*) echo 4.10.0 ;;
          *" -l "*) echo 'Found 11 check(s)' ;;
          *) printf '%s\\n' "$@" > "$CI_PROJECT_DIR/rfm-args"; exit 7 ;;
        esac''')
        self.env.update(PMIX_PYTHON=sys.executable, RFM_BIN=str(self.bin / 'reframe'),
                        PMIX_RFM_EXEC_POLICY='serial')
        result = subprocess.run(['bash', 'ci/run_pmix_python_suite.sh'],
                                cwd=self.repo, env=self.env, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, universal_newlines=True, timeout=15)
        self.assertEqual(result.returncode, 7, result.stdout + result.stderr)
        args = (self.repo / 'rfm-args').read_text().splitlines()
        self.assertIn('--exec-policy=serial', args)
        self.assertIn('--maxfail=1', args)
        del self.env['PMIX_RFM_EXEC_POLICY']
        result = subprocess.run(['bash', 'ci/run_pmix_python_suite.sh'],
                                cwd=self.repo, env=self.env, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, universal_newlines=True, timeout=15)
        self.assertEqual(result.returncode, 7, result.stdout + result.stderr)
        args = (self.repo / 'rfm-args').read_text().splitlines()
        self.assertNotIn('--exec-policy=serial', args)
        self.assertNotIn('--maxfail=1', args)


if __name__ == '__main__':
    unittest.main()
