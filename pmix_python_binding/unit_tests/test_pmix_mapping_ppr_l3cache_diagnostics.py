import ast
import os
from pathlib import Path
import shlex
import subprocess
import tempfile
import threading
import unittest


WORKLOAD = (
    Path(__file__).resolve().parents[1]
    / "workloads"
    / "spawn_mapping_ppr_l3cache_test.py"
)
WRAPPER = (
    Path(__file__).resolve().parents[1]
    / "wrappers"
    / "run_pmix_python_mapping_ppr_l3cache_test.sh"
)
REFRAME_TEST = (
    Path(__file__).resolve().parents[1]
    / "reframe"
    / "pmix_python_mapping_ppr_l3cache_test.py"
)


def workload_tree():
    return ast.parse(WORKLOAD.read_text(encoding="utf-8"))


def workload_definition(name, initial_namespace=None):
    definition = next(
        node
        for node in workload_tree().body
        if isinstance(node, (ast.FunctionDef, ast.ClassDef))
        and node.name == name
    )
    module = ast.Module(body=[definition], type_ignores=[])
    namespace = dict(initial_namespace or {})
    exec(compile(module, str(WORKLOAD), "exec"), namespace)
    return namespace[name]


def topology_command(proof_directory):
    tree = workload_tree()
    assignment = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name)
            and target.id == "topology_command"
            for target in node.targets
        )
    )

    return eval(
        compile(ast.Expression(assignment.value), str(WORKLOAD), "eval"),
        {"proof_directory_shell": shlex.quote(str(proof_directory))}
    )


def assigned_value(name):
    tree = workload_tree()
    assignment = next(
        node
        for node in tree.body
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == name
            for target in node.targets
        )
    )

    return ast.literal_eval(assignment.value)


class TestL3TopologyDiagnostics(unittest.TestCase):

    def completion_tracker(self):
        tracker_type = workload_definition(
            "TopologyCompletionTracker",
            {"threading": threading}
        )
        return tracker_type()

    def test_topology_timeouts_are_60_seconds(self):
        self.assertEqual(
            assigned_value("TOPOLOGY_PROOF_TIMEOUT_SECONDS"),
            60
        )
        self.assertEqual(
            assigned_value("TOPOLOGY_COMPLETION_TIMEOUT_SECONDS"),
            60
        )

        tree = workload_tree()
        topology_wait = next(
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "wait_for_files"
            and isinstance(node.args[0], ast.Name)
            and node.args[0].id == "topology_pattern"
        )
        timeout_argument = topology_wait.args[3]
        self.assertIsInstance(timeout_argument, ast.Name)
        self.assertEqual(
            timeout_argument.id,
            "TOPOLOGY_PROOF_TIMEOUT_SECONDS"
        )

    def run_topology_command(
        self,
        inject_read_failure=False
    ):
        with tempfile.TemporaryDirectory(
            prefix="pmix-l3-topology-",
            dir="/tmp"
        ) as temporary:
            proof_directory = Path(temporary)
            command = topology_command(proof_directory)

            if inject_read_failure:
                bad_level = (
                    proof_directory
                    / "fake-sysfs"
                    / "cpu0"
                    / "cache"
                    / "index0"
                    / "level"
                )
                bad_level.mkdir(parents=True)

                good_cache = (
                    proof_directory
                    / "fake-sysfs"
                    / "cpu1"
                    / "cache"
                    / "index0"
                )
                good_cache.mkdir(parents=True)
                (good_cache / "level").write_text(
                    "3\n",
                    encoding="utf-8"
                )
                (good_cache / "id").write_text(
                    "7\n",
                    encoding="utf-8"
                )
                (good_cache / "shared_cpu_list").write_text(
                    "0-7\n",
                    encoding="utf-8"
                )

                fake_glob = (
                    proof_directory
                    / "fake-sysfs"
                    / "cpu[0-9]*"
                    / "cache"
                    / "index*"
                    / "level"
                )
                command = command.replace(
                    "/sys/devices/system/cpu/"
                    "cpu[0-9]*/cache/index*/level",
                    str(fake_glob),
                    1
                )

            subprocess.run(
                ["/bin/bash", "-n", "-c", command],
                check=True
            )

            environment = os.environ.copy()
            environment["PMIX_RANK"] = "0"
            completed = subprocess.run(
                ["/bin/bash", "-c", command],
                env=environment,
                check=False
            )

            status_path = next(
                proof_directory.glob("l3_topology_*.status")
            )
            status = dict(
                line.split("=", 1)
                for line in status_path.read_text(
                    encoding="utf-8"
                ).splitlines()
            )

            final_proofs = list(
                proof_directory.glob("topology_*_l3")
            )
            temporary_proofs = list(
                proof_directory.glob("topology_*_l3.tmp")
            )
            temporary_contents = [
                proof.read_text(encoding="utf-8")
                for proof in temporary_proofs
            ]

            return (
                completed.returncode,
                status,
                len(final_proofs),
                len(temporary_proofs),
                temporary_contents
            )

    def test_early_pmix_job_end_is_retained(self):
        tracker = self.completion_tracker()
        source = {"nspace": "topology-7", "rank": 0}

        tracker.record(source)
        self.assertFalse(tracker.wait(0))

        tracker.set_namespace("topology-7")
        self.assertTrue(tracker.wait(0))
        self.assertEqual(tracker.source, source)

    def test_pmix_job_end_handler_records_completion_source(self):
        tree = workload_tree()
        handler = next(
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef)
            and node.name == "topology_completion_handler"
        )
        record_call = next(
            node
            for node in ast.walk(handler)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "topology_completion"
            and node.func.attr == "record"
        )
        self.assertIsInstance(record_call.args[0], ast.Name)
        self.assertEqual(record_call.args[0].id, "source")

        registration = next(
            node.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name)
                and target.id == "event_handler_result"
                for target in node.targets
            )
        )
        event_code = registration.args[0].elts[0]
        self.assertIsInstance(event_code, ast.Attribute)
        self.assertIsInstance(event_code.value, ast.Name)
        self.assertEqual(event_code.value.id, "pmix")
        self.assertEqual(event_code.attr, "PMIX_EVENT_JOB_END")

    def test_unrelated_namespace_does_not_satisfy_wait(self):
        tracker = self.completion_tracker()
        tracker.set_namespace("topology-7")
        tracker.record({"nspace": "unrelated-8", "rank": 0})

        self.assertFalse(tracker.wait(0))
        self.assertIsNone(tracker.source)

    def test_timeout_fails_clearly_and_prevents_mapped_spawn(self):
        tracker = self.completion_tracker()
        tracker.set_namespace("topology-7")
        spawn_after_completion = workload_definition(
            "spawn_after_topology_completion"
        )

        class Tool:
            spawn_calls = 0

            def spawn(self, job_info, apps):
                self.spawn_calls += 1
                return 0, "mapped-8"

        tool = Tool()

        with self.assertRaisesRegex(
            SystemExit,
            r"timed out waiting for topology namespace completion: "
            r"namespace=topology-7, timeout=0s"
        ):
            spawn_after_completion(tool, [], [], tracker, 0)

        self.assertEqual(tool.spawn_calls, 0)

    def test_exact_completion_allows_mapped_spawn(self):
        tracker = self.completion_tracker()
        tracker.set_namespace("topology-7")
        tracker.record({"nspace": "unrelated-8", "rank": 0})
        tracker.record({"nspace": "topology-7", "rank": 0})
        spawn_after_completion = workload_definition(
            "spawn_after_topology_completion"
        )

        class Tool:
            spawn_calls = 0

            def spawn(self, job_info, apps):
                self.spawn_calls += 1
                return 0, "mapped-8"

        tool = Tool()
        result = spawn_after_completion(
            tool,
            ["job-info"],
            ["app"],
            tracker,
            0
        )

        self.assertEqual(result, (0, "mapped-8"))
        self.assertEqual(tool.spawn_calls, 1)

    def test_exact_completion_wait_guards_mapped_spawn(self):
        tree = workload_tree()
        guarded_spawn = next(
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "spawn_after_topology_completion"
        )

        self.assertIsInstance(guarded_spawn.args[3], ast.Name)
        self.assertEqual(guarded_spawn.args[3].id, "topology_completion")
        self.assertIsInstance(guarded_spawn.args[4], ast.Name)
        self.assertEqual(
            guarded_spawn.args[4].id,
            "TOPOLOGY_COMPLETION_TIMEOUT_SECONDS"
        )

    def test_no_experiment_toggle_or_forced_delay_remains(self):
        for path in (WORKLOAD, WRAPPER, REFRAME_TEST):
            with self.subTest(path=path):
                source = path.read_text(encoding="utf-8")
                self.assertNotIn("L3_TOPOLOGY_SYNC", source)
                self.assertNotIn(
                    "L3_TOPOLOGY_EXIT_DELAY_SECONDS",
                    source
                )

        command_strings = [
            node.value
            for node in ast.walk(workload_tree())
            if isinstance(node, ast.Constant)
            and isinstance(node.value, str)
        ]
        self.assertFalse(any("sleep " in text for text in command_strings))

    def test_normal_scan_creates_final_proof(self):
        (
            returncode,
            status,
            final_count,
            temporary_count,
            _
        ) = (
            self.run_topology_command()
        )

        self.assertEqual(returncode, 0)
        self.assertEqual(status["exit_status"], "0")
        self.assertEqual(status["final_exists"], "yes")
        self.assertEqual(status["temporary_exists"], "no")
        self.assertEqual(final_count, 1)
        self.assertEqual(temporary_count, 0)

    def test_internal_read_failure_is_not_masked(self):
        (
            returncode,
            status,
            final_count,
            temporary_count,
            temporary_contents
        ) = (
            self.run_topology_command(inject_read_failure=True)
        )

        self.assertNotEqual(returncode, 0)
        self.assertNotEqual(status["exit_status"], "0")
        self.assertEqual(status["final_exists"], "no")
        self.assertEqual(status["temporary_exists"], "yes")
        self.assertEqual(final_count, 0)
        self.assertEqual(temporary_count, 1)
        self.assertIn("|7|0-7\n", temporary_contents[0])


if __name__ == "__main__":
    unittest.main()
