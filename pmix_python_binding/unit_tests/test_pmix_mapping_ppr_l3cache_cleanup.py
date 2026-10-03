from pathlib import Path
import subprocess
import tempfile
import unittest


WRAPPER = Path(__file__).resolve().parents[1] / "wrappers" / "run_pmix_python_mapping_ppr_l3cache_test.sh"


class L3CleanupTests(unittest.TestCase):
    def run_cleanup(self, setup, with_uri=False):
        source = WRAPPER.read_text(encoding="utf-8")
        functions = source[source.index("dvm_is_running()"):source.index("# Run cleanup if")]
        script = "\n".join([
            "set +e", functions, "DVM_STOP_TIMEOUT_SECONDS=0",
            "DVM_KILL_GRACE_SECONDS=0", 'PRTE_PID="test-pid"',
            setup, "cleanup_dvm", "cleanup_status=$?",
            "if [[ -f timeout-arguments ]]; then cat timeout-arguments; fi",
            'exit "$cleanup_status"'
        ])
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            if with_uri:
                (root / "dvm.uri").touch()
            proof = root / "process_node_1_l3"
            started = root / "started_node_1_l3"
            proof.touch()
            started.touch()
            result = subprocess.run(
                ["bash", "-c", script], cwd=directory,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                universal_newlines=True, timeout=5
            )
            self.assertTrue(proof.exists())
            self.assertTrue(started.exists())
            self.assertFalse((root / "dvm.uri").exists())
        return result

    def test_exited_dvm_is_reaped_without_kill(self):
        result = self.run_cleanup("\n".join([
            "dvm_is_running() { return 1; }",
            'wait() { echo "reaped"; return 0; }',
            'kill() { echo "unexpected kill"; return 1; }'
        ]))
        self.assertEqual(result.returncode, 0)
        self.assertIn("reaped", result.stdout)
        self.assertNotIn("unexpected kill", result.stdout)

    def test_term_stops_dvm(self):
        result = self.run_cleanup("\n".join([
            "running=1",
            "dvm_is_running() { (( running == 1 )); }",
            'kill() { echo "$1"; running=0; }',
            "wait() { return 0; }"
        ]))
        self.assertEqual(result.returncode, 0)
        self.assertIn("-TERM", result.stdout)
        self.assertNotIn("-KILL", result.stdout)

    def test_kill_stops_dvm_ignoring_term(self):
        result = self.run_cleanup("\n".join([
            "running=1",
            "dvm_is_running() { (( running == 1 )); }",
            'kill() { echo "$1"; if [[ "$1" == "-KILL" ]]; then running=0; fi; }',
            "wait() { return 0; }"
        ]))
        self.assertEqual(result.returncode, 0)
        self.assertIn("-TERM", result.stdout)
        self.assertIn("-KILL", result.stdout)

    def test_surviving_kill_fails_without_waiting(self):
        result = self.run_cleanup("\n".join([
            "dvm_is_running() { return 0; }",
            'kill() { echo "$1"; return 0; }',
            'wait() { echo "unexpected wait"; return 0; }'
        ]))
        self.assertEqual(result.returncode, 1)
        self.assertIn("did not exit after KILL", result.stdout)
        self.assertNotIn("unexpected wait", result.stdout)

    def test_pterm_timeout_is_bounded_and_diagnostics_preserved(self):
        result = self.run_cleanup("\n".join([
            "DVM_STOP_TIMEOUT_SECONDS=10",
            'PRRTE="/fixture"',
            'PRTE_PID=""',
            'timeout() { echo "timeout arguments: $*" > timeout-arguments; return 124; }'
        ]), with_uri=True)
        self.assertEqual(result.returncode, 0)
        self.assertIn("--kill-after=5s 10s /fixture/bin/pterm", result.stdout)

    def test_controller_failure_reports_status_and_preserves_failure(self):
        source = WRAPPER.read_text(encoding="utf-8")
        start = source.index("            controller_start_seconds=$SECONDS")
        end = source.index('            echo \\\n', start)
        controller = source[start:end]
        script = "\n".join([
            "set -euo pipefail", "node_count=4", "processes_per_l3cache=8",
            "trial=1", 'PYTHON="/mock-python"', 'EXPECTED_HOSTS="nodes"',
            "timeout() { return 124; }", controller,
            'echo "unexpected success"'
        ])
        result = subprocess.run(
            ["bash", "-c", script], stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, universal_newlines=True, timeout=5
        )
        self.assertEqual(result.returncode, 124)
        self.assertIn("nodes=4 ppr=8 trial=1 status=124", result.stdout)
        self.assertNotIn("unexpected success", result.stdout)


if __name__ == "__main__":
    unittest.main()
