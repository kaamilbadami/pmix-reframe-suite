import ast
import contextlib
import io
from pathlib import Path
import subprocess
import tempfile
import threading
import types
import unittest


WORKLOAD = (
    Path(__file__).resolve().parents[1]
    / "workloads"
    / "spawn_scaling_multinode_test.py"
)
WRAPPER = (
    Path(__file__).resolve().parents[1]
    / "wrappers"
    / "run_pmix_python_scaling_multinode_test.sh"
)


def workload_tree():
    return ast.parse(WORKLOAD.read_text(encoding="utf-8"))


def workload_helpers():
    """Load definitions before the workload's command-line entry point."""
    body = []

    for node in workload_tree().body:
        if (
            isinstance(node, ast.If)
            and isinstance(node.test, ast.Compare)
            and isinstance(node.test.left, ast.Call)
        ):
            break

        if (
            isinstance(node, ast.Import)
            and any(alias.name == "pmix" for alias in node.names)
        ):
            continue

        body.append(node)

    namespace = {
        "pmix": types.SimpleNamespace(
            PMIX_JOB_TERM_STATUS=b"pmix.jtermstat",
            PMIX_SUCCESS=0
        )
    }
    module = ast.Module(body=body, type_ignores=[])
    exec(compile(module, str(WORKLOAD), "exec"), namespace)
    return namespace


class ScalingMultinodeDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.helpers = workload_helpers()
        self.success_info = [
            {"key": "pmix.jtermstat", "value": 0}
        ]

    def test_july_spawn_layout_fix_is_preserved(self):
        tree = workload_tree()
        apps_assignment = next(
            node
            for node in tree.body
            if isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name) and target.id == "apps"
                for target in node.targets
            )
        )

        self.assertIsInstance(apps_assignment.value, ast.List)
        self.assertEqual(len(apps_assignment.value.elts), 1)

        app = apps_assignment.value.elts[0]
        app_values = {
            getattr(key, "value", getattr(key, "s", None)): value
            for key, value in zip(app.keys, app.values)
        }
        self.assertIsInstance(app_values["maxprocs"], ast.Name)
        self.assertEqual(app_values["maxprocs"].id, "num_processes")

        source = WORKLOAD.read_text(encoding="utf-8")
        self.assertIn("process_${{host}}_$$_host", source)

    def test_completion_event_is_requested_and_registered(self):
        source = WORKLOAD.read_text(encoding="utf-8")
        self.assertIn("pmix.PMIX_EVENT_JOB_END", source)
        self.assertIn("pmix.PMIX_NOTIFY_COMPLETION", source)

    def test_early_exact_namespace_completion_is_retained(self):
        tracker = self.helpers["SpawnCompletionTracker"]()
        source = {"nspace": "spawned-7", "rank": 0}

        tracker.record(0, source)
        self.assertFalse(tracker.wait(0))

        tracker.set_namespace("spawned-7")
        self.assertTrue(tracker.wait(0))
        self.assertEqual(tracker.event["source"], source)

    def test_unrelated_namespace_does_not_satisfy_wait(self):
        tracker = self.helpers["SpawnCompletionTracker"]()
        tracker.set_namespace("spawned-7")
        tracker.record(0, {"nspace": "unrelated-8", "rank": 0})

        self.assertFalse(tracker.wait(0))
        self.assertIsNone(tracker.event)

    def test_exact_completion_and_all_proofs_succeed(self):
        tracker = self.helpers["SpawnCompletionTracker"]()
        tracker.set_namespace("spawned-7")
        tracker.record(
            0,
            {"nspace": "spawned-7", "rank": 0},
            self.success_info
        )

        with tempfile.TemporaryDirectory(
            prefix="pmix-scaling-proofs-",
            dir="/tmp"
        ) as temporary:
            proof_directory = Path(temporary)
            first = proof_directory / "process_node1_101_host"
            second = proof_directory / "process_node2_202_host"
            first.write_text("node1\n", encoding="utf-8")
            second.write_text("node2\n", encoding="utf-8")

            with contextlib.redirect_stdout(io.StringIO()):
                proofs = self.helpers[
                    "wait_for_spawn_completion_and_proofs"
                ](
                    tracker,
                    str(proof_directory / "process_*_host"),
                    2,
                    ["node1", "node2"],
                    completion_timeout_seconds=0,
                    visibility_timeout_seconds=0
                )

        self.assertEqual(proofs, [str(first), str(second)])

    def test_normal_job_termination_status_succeeds(self):
        event = {"info": self.success_info}

        status = self.helpers["require_successful_job_termination"](event)

        self.assertEqual(status, 0)

    def test_nonzero_job_termination_status_fails(self):
        event = {
            "info": [{"key": "pmix.jtermstat", "value": 7}]
        }

        with contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(
                SystemExit,
                "PMIX_JOB_TERM_STATUS=7"
            ):
                self.helpers["require_successful_job_termination"](event)

    def test_missing_job_termination_status_fails(self):
        event = {"info": []}

        with contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(
                SystemExit,
                "completion status was missing"
            ):
                self.helpers["require_successful_job_termination"](event)

    def test_all_proofs_do_not_override_failed_termination(self):
        tracker = self.helpers["SpawnCompletionTracker"]()
        tracker.set_namespace("spawned-7")
        tracker.record(
            0,
            {"nspace": "spawned-7", "rank": 0},
            [{"key": "pmix.jtermstat", "value": 7}]
        )

        with tempfile.TemporaryDirectory(
            prefix="pmix-scaling-failed-job-",
            dir="/tmp"
        ) as temporary:
            proof_directory = Path(temporary)
            (proof_directory / "process_node1_101_host").write_text(
                "node1\n",
                encoding="utf-8"
            )
            (proof_directory / "process_node2_202_host").write_text(
                "node2\n",
                encoding="utf-8"
            )

            with contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaisesRegex(
                    SystemExit,
                    "PMIX_JOB_TERM_STATUS=7"
                ):
                    self.helpers[
                        "wait_for_spawn_completion_and_proofs"
                    ](
                        tracker,
                        str(proof_directory / "process_*_host"),
                        2,
                        ["node1", "node2"],
                        completion_timeout_seconds=0,
                        visibility_timeout_seconds=0
                    )

    def test_completion_timeout_reports_progress_and_namespace(self):
        tracker = self.helpers["SpawnCompletionTracker"]()
        tracker.set_namespace("spawned-7")

        with tempfile.TemporaryDirectory(
            prefix="pmix-scaling-timeout-",
            dir="/tmp"
        ) as temporary:
            proof_directory = Path(temporary)
            proof = proof_directory / "process_node1_101_host"
            proof.write_text("node1\n", encoding="utf-8")
            output = io.StringIO()

            with contextlib.redirect_stdout(output):
                with self.assertRaisesRegex(
                    SystemExit,
                    "spawned namespace completion was not observed"
                ):
                    self.helpers[
                        "wait_for_spawn_completion_and_proofs"
                    ](
                        tracker,
                        str(proof_directory / "process_*_host"),
                        2,
                        ["node1", "node2"],
                        completion_timeout_seconds=0,
                        visibility_timeout_seconds=0
                    )

        diagnostics = output.getvalue()
        self.assertIn("expected=2 observed=1", diagnostics)
        self.assertIn("counts=node1=1,node2=0", diagnostics)
        self.assertIn("selected_hosts=node1,node2", diagnostics)
        self.assertIn("spawned_namespace=spawned-7", diagnostics)
        self.assertIn("file=process_node1_101_host host=node1", diagnostics)

    def test_completed_job_gets_bounded_proof_visibility_wait(self):
        tracker = self.helpers["SpawnCompletionTracker"]()
        tracker.set_namespace("spawned-7")
        tracker.record(
            0,
            {"nspace": "spawned-7", "rank": 0},
            self.success_info
        )
        output = io.StringIO()

        with tempfile.TemporaryDirectory(
            prefix="pmix-scaling-visibility-",
            dir="/tmp"
        ) as temporary:
            with contextlib.redirect_stdout(output):
                with self.assertRaisesRegex(
                    SystemExit,
                    "hostname proofs remained incomplete after completion"
                ):
                    self.helpers[
                        "wait_for_spawn_completion_and_proofs"
                    ](
                        tracker,
                        str(Path(temporary) / "process_*_host"),
                        2,
                        ["node1", "node2"],
                        completion_timeout_seconds=0,
                        visibility_timeout_seconds=0
                    )

        diagnostics = output.getvalue()
        self.assertIn("completion_observed=True", diagnostics)
        self.assertIn("expected=2 observed=0", diagnostics)

    def test_proofs_appearing_after_completion_succeed(self):
        tracker = self.helpers["SpawnCompletionTracker"]()
        tracker.set_namespace("spawned-7")
        tracker.record(
            0,
            {"nspace": "spawned-7", "rank": 0},
            self.success_info
        )

        with tempfile.TemporaryDirectory(
            prefix="pmix-scaling-delayed-proofs-",
            dir="/tmp"
        ) as temporary:
            proof_directory = Path(temporary)
            expected_proofs = [
                proof_directory / "process_node1_101_host",
                proof_directory / "process_node2_202_host"
            ]

            def publish_proofs():
                threading.Event().wait(0.05)
                expected_proofs[0].write_text("node1\n", encoding="utf-8")
                expected_proofs[1].write_text("node2\n", encoding="utf-8")

            publisher = threading.Thread(target=publish_proofs)
            publisher.start()
            try:
                with contextlib.redirect_stdout(io.StringIO()):
                    proofs = self.helpers[
                        "wait_for_spawn_completion_and_proofs"
                    ](
                        tracker,
                        str(proof_directory / "process_*_host"),
                        2,
                        ["node1", "node2"],
                        completion_timeout_seconds=0,
                        visibility_timeout_seconds=1
                    )
            finally:
                publisher.join()

        self.assertEqual(proofs, [str(path) for path in expected_proofs])

    def test_partial_proof_set_after_completion_times_out(self):
        tracker = self.helpers["SpawnCompletionTracker"]()
        tracker.set_namespace("spawned-7")
        tracker.record(
            0,
            {"nspace": "spawned-7", "rank": 0},
            self.success_info
        )

        with tempfile.TemporaryDirectory(
            prefix="pmix-scaling-partial-proofs-",
            dir="/tmp"
        ) as temporary:
            proof_directory = Path(temporary)
            (proof_directory / "process_node1_101_host").write_text(
                "node1\n",
                encoding="utf-8"
            )
            output = io.StringIO()

            with contextlib.redirect_stdout(output):
                with self.assertRaisesRegex(
                    SystemExit,
                    "hostname proofs remained incomplete after completion"
                ):
                    self.helpers[
                        "wait_for_spawn_completion_and_proofs"
                    ](
                        tracker,
                        str(proof_directory / "process_*_host"),
                        2,
                        ["node1", "node2"],
                        completion_timeout_seconds=0,
                        visibility_timeout_seconds=0
                    )

        self.assertIn("expected=2 observed=1", output.getvalue())

    def test_wrapper_keeps_requested_placement_and_bounded_cleanup(self):
        source = WRAPPER.read_text(encoding="utf-8")
        self.assertIn("NODE_COUNTS=(1 2 4)", source)
        self.assertIn("SLOTS_PER_NODE=32", source)
        self.assertIn("TRIALS=5", source)
        self.assertIn('for node_count in "${NODE_COUNTS[@]}"', source)
        self.assertIn('timeout "${DVM_STOP_TIMEOUT_SECONDS}s"', source)
        self.assertIn("kill -KILL", source)

    def test_cleanup_fails_if_dvm_survives_final_kill(self):
        source = WRAPPER.read_text(encoding="utf-8")
        functions = source[
            source.index("dvm_is_running()"):
            source.index("# Run cleanup if the script exits early.")
        ]
        script = "\n".join([
            "set +e",
            functions,
            "DVM_STOP_TIMEOUT_SECONDS=0",
            "DVM_KILL_GRACE_SECONDS=0",
            "PRTE_PID=999999",
            "dvm_is_running() { return 0; }",
            "cleanup_dvm",
            "exit $?"
        ])

        with tempfile.TemporaryDirectory(
            prefix="pmix-scaling-cleanup-",
            dir="/tmp"
        ) as temporary:
            result = subprocess.run(
                ["bash", "-c", script],
                cwd=temporary,
                universal_newlines=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False
            )

        self.assertEqual(result.returncode, 1)
        self.assertIn("did not exit after KILL", result.stdout)


if __name__ == "__main__":
    unittest.main()
