import os
from pathlib import Path
import signal
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

from pmix_python_binding.common.prrte_startup import start_prrte


class PRRTEStartupTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.prefix = Path(self.directory.name) / "worker"

    def start(self, code, timeout=1):
        return start_prrte([sys.executable, "-u", "-c", code], self.prefix, timeout)

    def test_ready_preserves_both_streams(self):
        process = self.start("import sys,time; print('diagnostic',file=sys.stderr); print('DVM ready'); time.sleep(30)")
        try:
            self.assertIsNone(process.poll())
            self.assertIn("diagnostic", Path(str(self.prefix) + ".prte.stderr").read_text())
        finally:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)

    def test_silent_process_has_bounded_wait(self):
        started = time.monotonic()
        with self.assertRaisesRegex(RuntimeError, "PRRTE STARTUP TIMEOUT"):
            self.start("import time; time.sleep(30)", timeout=0.2)
        self.assertLess(time.monotonic() - started, 3)

    def test_partial_line_cannot_block_timeout(self):
        with self.assertRaisesRegex(RuntimeError, "partial diagnostic"):
            self.start("import sys,time; sys.stdout.write('partial diagnostic'); sys.stdout.flush(); time.sleep(30)", timeout=0.2)

    def test_stderr_flood_cannot_block_readiness(self):
        process = self.start("import sys,time; sys.stderr.write('x'*200000); sys.stderr.flush(); print('DVM ready'); time.sleep(30)")
        try:
            self.assertEqual(Path(str(self.prefix) + ".prte.stderr").stat().st_size, 200000)
        finally:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)

    def test_exit_before_readiness_reports_stderr_and_exit(self):
        with self.assertRaisesRegex(RuntimeError, "exit=7") as error:
            self.start("import sys; print('launch failure',file=sys.stderr); sys.exit(7)")
        self.assertIn("launch failure", str(error.exception))

    def test_real_targeted_and_mixed_controllers_retain_startup_failure_streams(self):
        suite = Path(__file__).resolve().parents[1]
        for kind in ('targeted', 'mixed_thread'):
            with self.subTest(controller=kind), tempfile.TemporaryDirectory() as directory:
                stage = Path(directory)
                controller = suite / 'controllers' / ('run_pmix_python_' + kind + '_compat.py')
                for helper in ('pmix_event_utils.py', 'prrte_startup.py'):
                    shutil.copy(suite / 'common' / helper, stage)
                (stage / 'pmix.py').write_text('# Startup fails before PMIx is used.\n')
                binary = stage / 'bin'
                binary.mkdir()
                prte = binary / 'prte'
                prte.write_text('#!' + sys.executable + '\n'
                    'import sys\n'
                    'print("startup banner")\n'
                    'sys.stderr.write("site banner\\n" + "x"*200000 + "\\nREMOTE_SESSION_PARENT_MISSING\\n")\n'
                    'sys.exit(7)\n')
                prte.chmod(0o755)
                sleeper = stage / 'sleeper'
                sleeper.touch()
                job_size = ['--job-size', '1'] if kind == 'targeted' else [
                    '--job-size-min', '1', '--job-size-max', '1']
                result = subprocess.run([sys.executable, str(controller), '--slots', '1',
                    *job_size, '--min-time', '1', '--max-time', '1', '--iters', '1',
                    '--out-file', 'controller.out', '--delay', '0', '--job', str(sleeper)],
                    cwd=stage, env=dict(os.environ, PRRTE=str(stage), PYTHONPATH=str(stage)),
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, timeout=10)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('exit=7', result.stderr)
                self.assertIn('REMOTE_SESSION_PARENT_MISSING', result.stderr)
                self.assertIn('startup banner', (stage / 'controller.out.prte.stdout').read_text())
                error_log = (stage / 'controller.out.prte.stderr').read_text()
                self.assertTrue(error_log.startswith('site banner'))
                self.assertGreater(len(error_log), 200000)
                self.assertIn('REMOTE_SESSION_PARENT_MISSING', error_log)

    def test_invalid_deadlines_do_not_launch(self):
        for timeout in (0, -1, float("inf"), float("nan")):
            with self.assertRaises(ValueError):
                self.start("raise Exception('must not launch')", timeout)


if __name__ == "__main__":
    unittest.main()
