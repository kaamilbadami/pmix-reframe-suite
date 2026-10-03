import ast
import contextlib
import glob
import io
import os
from pathlib import Path
import runpy
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest.mock import patch


WORKLOAD = Path(__file__).resolve().parents[1] / "workloads" / "spawn_mapping_ppr_l3cache_test.py"


class L3MappedCompletionTests(unittest.TestCase):
    def setUp(self):
        self.pmix = types.SimpleNamespace(
            PMIX_SUCCESS=0, PMIX_JOB_TERM_STATUS=b"pmix.jtermstat",
            PMIX_EVENT_JOB_END=100, PMIX_EVENT_ACTION_COMPLETE=101,
            PMIX_NOTIFY_COMPLETION=b"pmix.notify", PMIX_BOOL=1,
            PMIX_STRING=2, PMIX_MAPBY=b"pmix.map", PMIX_BINDTO=b"pmix.bind"
        )
        tree = ast.parse(WORKLOAD.read_text(encoding="utf-8"))
        definitions = []
        namespace = {
            "pmix": self.pmix, "threading": threading, "time": time,
            "os": os, "glob": glob, "topology_pattern": "unused",
            "process_pattern": "unused", "started_pattern": "unused"
        }
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
                definitions.append(node)
            elif isinstance(node, ast.Assign):
                try:
                    value = ast.literal_eval(node.value)
                except (ValueError, TypeError):
                    continue
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        namespace[target.id] = value
        exec(compile(ast.Module(body=definitions, type_ignores=[]), str(WORKLOAD), "exec"), namespace)
        self.helpers = namespace
        self.tracker = namespace["SpawnCompletionTracker"]()
        self.info = [{"key": "pmix.jtermstat", "value": 0}]

    def complete(self, info=None):
        self.tracker.set_namespace("mapped")
        self.tracker.record(100, {"nspace": "mapped"}, self.info if info is None else info)

    def wait(self, pattern, count=1, visibility=0):
        with contextlib.redirect_stdout(io.StringIO()):
            return self.helpers["wait_for_mapped_completion_and_proofs"](
                self.tracker, pattern, count,
                completion_timeout_seconds=0, visibility_timeout_seconds=visibility
            )

    def test_early_mapped_event_is_retained(self):
        self.tracker.record(100, {"nspace": "mapped"}, self.info)
        self.assertFalse(self.tracker.wait(0))
        self.tracker.set_namespace("mapped")
        self.assertTrue(self.tracker.wait(0))

    def test_topology_event_cannot_complete_mapped_job(self):
        self.tracker.set_namespace("mapped")
        self.tracker.record(100, {"nspace": "topology"}, self.info)
        self.assertFalse(self.tracker.wait(0))

    def test_proofs_cannot_replace_completion(self):
        self.tracker.set_namespace("mapped")
        with tempfile.NamedTemporaryFile() as proof:
            with self.assertRaisesRegex(SystemExit, "completion was not observed"):
                self.wait(proof.name)

    def test_complete_proofs_and_successful_job_pass(self):
        self.complete()
        with tempfile.NamedTemporaryFile() as proof:
            self.assertEqual(self.wait(proof.name), [proof.name])

    def test_failed_job_rejected_even_with_all_proofs(self):
        self.complete([{"key": "pmix.jtermstat", "value": 7}])
        with tempfile.NamedTemporaryFile() as proof:
            with self.assertRaisesRegex(SystemExit, "PMIX_JOB_TERM_STATUS=7"):
                self.wait(proof.name)

    def test_missing_termination_status_rejected(self):
        self.complete([])
        with self.assertRaisesRegex(SystemExit, "status was missing"):
            self.wait("unused")

    def test_delayed_proofs_after_completion_pass(self):
        self.complete()
        with tempfile.TemporaryDirectory() as directory:
            proof = Path(directory) / "process_node_1_l3"
            with patch.object(time, "sleep", side_effect=lambda _: proof.touch()):
                self.assertEqual(self.wait(str(proof), visibility=1), [str(proof)])

    def test_partial_proofs_after_completion_fail(self):
        self.complete()
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / "process_node_1_l3").touch()
            with self.assertRaisesRegex(SystemExit, "found 1 process proof files; expected 2"):
                self.wait(str(Path(directory) / "process_*_l3"), count=2)

    def run_workload(self, failure=None):
        owner = self

        class FakeTool:
            finalized = 0
            spawn_calls = 0

            def init(self, info):
                return (0, {})

            def register_event_handler(self, codes, info, callback):
                owner.assertEqual(codes, [owner.pmix.PMIX_EVENT_JOB_END])
                self.callback = callback
                return (1 if failure == "registration" else 0, 1)

            def spawn(self, info, apps):
                self.spawn_calls += 1
                owner.assertTrue(any(
                    item["key"] == owner.pmix.PMIX_NOTIFY_COMPLETION and item["value"]
                    for item in info
                ))
                if self.spawn_calls == 1:
                    Path("topology_node_1_l3").write_text("node|0|0-7\n", encoding="utf-8")
                    namespace = "topology"
                    event_info = owner.info
                else:
                    owner.assertEqual(apps[0]["maxprocs"], 1)
                    owner.assertEqual(info[0]["value"], "ppr:1:l3cache")
                    owner.assertEqual(info[1]["value"], "core")
                    if failure == "spawn":
                        return (1, None)
                    if failure == "namespace":
                        return (0, "")
                    Path("process_node_2_l3").write_text("node|2|0|0|0|0-7\n", encoding="utf-8")
                    namespace = "mapped"
                    event_info = (
                        [] if failure == "missing-status" else
                        [{"key": "pmix.jtermstat", "value": 7}] if failure == "termination" else
                        owner.info
                    )
                # Both callbacks occur before spawn returns.
                result = self.callback(1, 100, {"nspace": namespace}, event_info, [])
                owner.assertEqual(result, (owner.pmix.PMIX_EVENT_ACTION_COMPLETE, None))
                return (0, namespace)

            def finalize(self):
                self.finalized += 1
                return 1 if failure == "finalize" else 0

        tool = FakeTool()
        self.pmix.PMIxTool = lambda: tool
        output = io.StringIO()
        with tempfile.TemporaryDirectory() as directory:
            previous_directory = os.getcwd()
            try:
                os.chdir(directory)
                Path("dvm.uri").write_text("uri\n", encoding="utf-8")
                with patch.dict(sys.modules, {"pmix": self.pmix}):
                    with patch.object(sys, "argv", [str(WORKLOAD), "node", "1"]):
                        with contextlib.redirect_stdout(output):
                            if failure:
                                with self.assertRaises(SystemExit):
                                    runpy.run_path(str(WORKLOAD), run_name="__main__")
                            else:
                                runpy.run_path(str(WORKLOAD), run_name="__main__")
            finally:
                os.chdir(previous_directory)
        self.assertEqual(tool.finalized, 1)
        if not failure:
            self.assertEqual(tool.spawn_calls, 2)
            self.assertIn("PPR L3CACHE PLACEMENT VERIFIED", output.getvalue())
            self.assertIn("mapped namespace completion observed: mapped", output.getvalue())
        else:
            self.assertNotIn("PPR L3CACHE PLACEMENT VERIFIED", output.getvalue())

    def test_full_workload_correlates_two_early_events(self):
        self.run_workload()

    def test_registration_failure_finalizes(self):
        self.run_workload("registration")

    def test_mapped_spawn_failure_finalizes(self):
        self.run_workload("spawn")

    def test_mapped_job_failure_finalizes(self):
        self.run_workload("termination")

    def test_mapped_missing_status_finalizes(self):
        self.run_workload("missing-status")

    def test_missing_mapped_namespace_finalizes(self):
        self.run_workload("namespace")

    def test_finalize_failure_rejected(self):
        self.run_workload("finalize")


if __name__ == "__main__":
    unittest.main()
