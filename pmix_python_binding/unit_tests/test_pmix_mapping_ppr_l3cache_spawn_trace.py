import ast
import contextlib
import faulthandler
import glob
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
WORKLOAD = ROOT / "workloads" / "spawn_mapping_ppr_l3cache_test.py"
WRAPPER = ROOT / "wrappers" / "run_pmix_python_mapping_ppr_l3cache_test.sh"


def diagnostic_function():
    tree = ast.parse(WORKLOAD.read_text(encoding="utf-8"))
    definition = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "spawn_with_diagnostics")
    namespace = {}
    exec(compile(ast.Module(body=[definition], type_ignores=[]), str(WORKLOAD), "exec"), namespace)
    return namespace["spawn_with_diagnostics"]


class L3SpawnTraceTests(unittest.TestCase):
    def run_diagnostic(self, tool):
        function = diagnostic_function()
        with tempfile.TemporaryDirectory() as directory:
            previous = os.getcwd()
            try:
                os.chdir(directory)
                with contextlib.redirect_stdout(io.StringIO()):
                    result = function(tool, ["job-info"], [{"maxprocs": 256}])
                content = next(Path(directory).glob("l3_mapped_spawn_*.trace")).read_text()
                return result, content
            finally:
                os.chdir(previous)

    def test_timer_is_armed_before_spawn_and_canceled_before_file_close(self):
        owner = self
        with patch.object(faulthandler, "dump_traceback_later") as arm:
            with patch.object(faulthandler, "cancel_dump_traceback_later") as cancel:
                class Tool:
                    def spawn(self, info, apps):
                        owner.assertEqual(info, ["job-info"])
                        owner.assertEqual(apps, [{"maxprocs": 256}])
                        arm.assert_called_once()
                        owner.assertEqual(arm.call_args[0], (30,))
                        owner.assertTrue(arm.call_args[1]["repeat"])
                        owner.assertFalse(arm.call_args[1]["exit"])
                        owner.assertFalse(arm.call_args[1]["file"].closed)
                        return 0, "mapped"

                def verify_open_file():
                    owner.assertFalse(arm.call_args[1]["file"].closed)

                cancel.side_effect = verify_open_file
                result, content = self.run_diagnostic(Tool())
                cancel.assert_called_once()
                self.assertTrue(arm.call_args[1]["file"].closed)
        self.assertEqual(result, (0, "mapped"))
        self.assertIn("requested_processes=256", content)
        self.assertIn("spawn_result=(0, 'mapped')", content)

    def test_spawn_exception_is_preserved_and_timer_canceled(self):
        class Tool:
            def spawn(self, info, apps):
                raise RuntimeError("injected spawn failure")

        with patch.object(faulthandler, "dump_traceback_later"):
            with patch.object(faulthandler, "cancel_dump_traceback_later") as cancel:
                with self.assertRaisesRegex(RuntimeError, "injected spawn failure"):
                    self.run_diagnostic(Tool())
                cancel.assert_called_once()

    def test_real_watchdog_records_blocked_spawn_without_killing_controller(self):
        # Separate process keeps this test's watchdog independent of the runner.
        source = """
import ast, pathlib, time
tree = ast.parse(pathlib.Path(WORKLOAD).read_text())
definition = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'spawn_with_diagnostics')
namespace = {}
exec(compile(ast.Module(body=[definition], type_ignores=[]), WORKLOAD, 'exec'), namespace)
class Tool:
    def spawn(self, info, apps):
        time.sleep(0.15)
        return 0, 'mapped'
assert namespace['spawn_with_diagnostics'](Tool(), [], [{'maxprocs': 256}], 0.02) == (0, 'mapped')
time.sleep(0.05)
"""
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run(
                [sys.executable, "-c", "WORKLOAD=" + repr(str(WORKLOAD)) + "\n" + source],
                cwd=directory, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                universal_newlines=True, timeout=5
            )
            content = next(Path(directory).glob("l3_mapped_spawn_*.trace")).read_text()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Timeout", content)
        self.assertIn("spawn_with_diagnostics", content)
        self.assertIn("spawn_result=(0, 'mapped')", content)

    def test_failure_report_counts_proofs_and_includes_trace(self):
        source = WRAPPER.read_text(encoding="utf-8")
        helper = source[source.index("report_controller_evidence()"):source.index("# Test each node-count")]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "started_node_1_l3").touch()
            (root / "started_node_2_l3").touch()
            (root / "process_node_1_l3").touch()
            trace = root / "l3_mapped_spawn_1.trace"
            trace.write_text("blocked spawn traceback\n")
            result = subprocess.run(
                ["bash", "-c", helper + "\nreport_controller_evidence"], cwd=directory,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                universal_newlines=True, timeout=5
            )
            self.assertTrue(trace.exists())
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("started=2 proofs=1 traces=1", result.stdout)
        self.assertIn("blocked spawn traceback", result.stdout)

    def test_failure_report_handles_no_visible_artifacts(self):
        source = WRAPPER.read_text(encoding="utf-8")
        helper = source[source.index("report_controller_evidence()"):source.index("# Test each node-count")]
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run(
                ["bash", "-c", helper + "\nreport_controller_evidence"], cwd=directory,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                universal_newlines=True, timeout=5
            )
        self.assertEqual(result.returncode, 0)
        self.assertIn("started=0 proofs=0 traces=0", result.stdout)


if __name__ == "__main__":
    unittest.main()
