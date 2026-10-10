import ast
import contextlib
import io
import os
import runpy
import sys
from unittest import mock
from pathlib import Path
import subprocess
import tempfile
import threading
import types
import unittest


WORKLOAD = (
    Path(__file__).resolve().parents[1]
    / "workloads"
    / "spawn_mapping_ppr_node_test.py"
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


class PPRNodeCompletionTests(unittest.TestCase):
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
            key.value: value
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

    def run_workload(self, wrong_host=False):
        fake = types.ModuleType("pmix")
        for name in ["PMIX_JOB_TERM_STATUS", "PMIX_MAPBY", "PMIX_STRING",
                     "PMIX_EVENT_JOB_END", "PMIX_NOTIFY_COMPLETION", "PMIX_BOOL",
                     "PMIX_EVENT_ACTION_COMPLETE"]:
            setattr(fake, name, name)
        fake.PMIX_SUCCESS = 0
        class Tool:
            finalized = False
            def init(self, info):
                return (0, None)
            def register_event_handler(self, codes, info, handler):
                self.handler = handler
                return (0, 1)
            def spawn(self, info, apps):
                for key in [fake.PMIX_MAPBY, fake.PMIX_NOTIFY_COMPLETION]:
                    assert any(item["key"] == key for item in info)
                command = apps[0]["argv"][2]
                assert 'mv "$tmp" "$out"' in command
                for index, host in enumerate(["node1", "wrong" if wrong_host else "node2"]):
                    Path(f"process_{host}_{index}_host").write_text(host + "\n")
                self.handler(1, 0, {"nspace": "spawned-7"},
                             [{"key": fake.PMIX_JOB_TERM_STATUS, "value": 0}], [])
                return (0, "spawned-7")
            def finalize(self):
                self.finalized = True
                return 0
        tool = Tool()
        fake.PMIxTool = lambda: tool
        previous = os.getcwd()
        try:
            with tempfile.TemporaryDirectory() as temporary:
                os.chdir(temporary)
                Path("dvm.uri").write_text("fake-uri\n")
                with mock.patch.dict(sys.modules, {"pmix": fake}), \
                     mock.patch.object(sys, "argv", [str(WORKLOAD), "node1,node2", "1"]), \
                     contextlib.redirect_stdout(io.StringIO()):
                    if wrong_host:
                        with self.assertRaisesRegex(SystemExit, "expected hosts"):
                            runpy.run_path(str(WORKLOAD))
                    else:
                        runpy.run_path(str(WORKLOAD))
                self.assertTrue(tool.finalized)
        finally:
            os.chdir(previous)

    def test_full_workload_accepts_correct_placement(self):
        self.run_workload()

    def test_full_workload_rejects_wrong_host(self):
        self.run_workload(wrong_host=True)

    def test_empty_proof_after_completion_times_out(self):
        tracker = self.helpers["SpawnCompletionTracker"]()
        tracker.set_namespace("spawned-7")
        tracker.record(0, {"nspace": "spawned-7"}, self.success_info)
        with tempfile.TemporaryDirectory() as temporary:
            proof = Path(temporary) / "process_node1_101_host"
            proof.touch()
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                with self.assertRaisesRegex(SystemExit, "proofs remained incomplete"):
                    self.helpers["wait_for_spawn_completion_and_proofs"](
                        tracker, str(Path(temporary) / "process_*_host"),
                        1, ["node1"], 0, 0)
            self.assertIn("host=<empty>", output.getvalue())

    def test_empty_proof_can_become_ready(self):
        tracker = self.helpers["SpawnCompletionTracker"]()
        tracker.set_namespace("spawned-7")
        tracker.record(0, {"nspace": "spawned-7"}, self.success_info)
        with tempfile.TemporaryDirectory() as temporary:
            proof = Path(temporary) / "process_node1_101_host"
            proof.touch()
            def publish():
                threading.Event().wait(0.05)
                proof.write_text("node1\n")
            publisher = threading.Thread(target=publish)
            publisher.start()
            try:
                with contextlib.redirect_stdout(io.StringIO()):
                    result = self.helpers["wait_for_spawn_completion_and_proofs"](
                        tracker, str(Path(temporary) / "process_*_host"),
                        1, ["node1"], 0, 1)
                self.assertEqual(result, [str(proof)])
            finally:
                publisher.join()


if __name__ == "__main__":
    unittest.main()
