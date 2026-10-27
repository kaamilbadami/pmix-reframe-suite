import os
from pathlib import Path
import signal
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

    def test_invalid_deadlines_do_not_launch(self):
        for timeout in (0, -1, float("inf"), float("nan")):
            with self.assertRaises(ValueError):
                self.start("raise Exception('must not launch')", timeout)


if __name__ == "__main__":
    unittest.main()
