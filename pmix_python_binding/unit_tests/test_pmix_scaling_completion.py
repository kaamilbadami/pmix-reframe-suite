import contextlib
import importlib.util
import io
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch


WORKLOAD = Path(__file__).resolve().parents[1] / "workloads" / "spawn_scaling_test.py"


class ScalingCompletionTests(unittest.TestCase):
    def setUp(self):
        self.pmix = types.SimpleNamespace(
            PMIX_SUCCESS=0, PMIX_JOB_TERM_STATUS=b"pmix.jtermstat",
            PMIX_EVENT_JOB_END=100, PMIX_EVENT_ACTION_COMPLETE=101,
            PMIX_NOTIFY_COMPLETION=b"pmix.notify", PMIX_BOOL=1, PMIX_STRING=2
        )
        spec = importlib.util.spec_from_file_location("scaling_workload", WORKLOAD)
        self.workload = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {"pmix": self.pmix}):
            spec.loader.exec_module(self.workload)
        self.tracker = self.workload.SpawnCompletionTracker()
        self.info = [{"key": "pmix.jtermstat", "value": 0}]

    def complete(self, info=None):
        self.tracker.set_namespace("child")
        self.tracker.record(100, {"nspace": "child"}, self.info if info is None else info)

    def wait(self, proofs, visibility=0):
        with contextlib.redirect_stdout(io.StringIO()):
            self.workload.wait_for_spawn_completion_and_proofs(
                self.tracker, proofs, completion_timeout_seconds=0,
                visibility_timeout_seconds=visibility
            )

    def test_early_completion_is_retained(self):
        self.tracker.record(100, {"nspace": "child"}, self.info)
        self.assertFalse(self.tracker.wait(0))
        self.tracker.set_namespace("child")
        self.assertTrue(self.tracker.wait(0))

    def test_unrelated_namespace_is_ignored(self):
        self.tracker.set_namespace("child")
        self.tracker.record(100, {"nspace": "other"}, self.info)
        self.assertFalse(self.tracker.wait(0))

    def test_proofs_without_completion_fail(self):
        self.tracker.set_namespace("child")
        with tempfile.NamedTemporaryFile() as proof:
            with self.assertRaisesRegex(SystemExit, "completion was not observed"):
                self.wait([proof.name])

    def test_successful_completion_and_proofs_pass(self):
        self.complete()
        with tempfile.NamedTemporaryFile() as proof:
            self.wait([proof.name])

    def test_failed_termination_overrides_complete_proofs(self):
        self.complete([{"key": "pmix.jtermstat", "value": 7}])
        with tempfile.NamedTemporaryFile() as proof:
            with self.assertRaisesRegex(SystemExit, "PMIX_JOB_TERM_STATUS=7"):
                self.wait([proof.name])

    def test_missing_termination_status_fails(self):
        self.complete([])
        with self.assertRaisesRegex(SystemExit, "status was missing"):
            self.wait([])

    def test_delayed_proof_visibility_passes(self):
        self.complete()
        with tempfile.TemporaryDirectory() as directory:
            proof = Path(directory) / "proof"
            with patch.object(self.workload.time, "sleep", side_effect=lambda _: proof.touch()):
                self.wait([str(proof)], visibility=1)

    def test_partial_proofs_timeout_lists_missing_file(self):
        self.complete()
        with tempfile.TemporaryDirectory() as directory:
            first = Path(directory) / "first"
            first.touch()
            with self.assertRaisesRegex(SystemExit, "second"):
                self.wait([str(first), str(Path(directory) / "second")])

    def run_main(self, failure=None):
        owner = self

        class FakeTool:
            finalized = 0
            registered = False

            def init(self, info):
                return (0, {})

            def register_event_handler(self, codes, info, callback):
                owner.assertEqual(codes, [owner.pmix.PMIX_EVENT_JOB_END])
                self.callback = callback
                self.registered = True
                return (1 if failure == "registration" else 0, 1)

            def spawn(self, info, apps):
                owner.assertTrue(self.registered)
                owner.assertEqual(info[0]["key"], owner.pmix.PMIX_NOTIFY_COMPLETION)
                owner.assertTrue(info[0]["value"])
                owner.assertEqual(len(apps), 2)
                if failure == "spawn":
                    return (1, None)
                # Deliver completion before spawn returns.
                event_info = [] if failure == "termination" else owner.info
                result = self.callback(1, 100, {"nspace": "child"}, event_info, [])
                owner.assertEqual(result, (owner.pmix.PMIX_EVENT_ACTION_COMPLETE, None))
                return (0, "child")

            def finalize(self):
                self.finalized += 1
                return 1 if failure == "finalize" else 0

        tool = FakeTool()
        self.pmix.PMIxTool = lambda: tool
        with patch.object(sys, "argv", ["spawn_scaling_test.py", "2"]):
            with patch("builtins.open", unittest.mock.mock_open(read_data="uri\n")):
                with patch.object(self.workload.os.path, "exists", return_value=True):
                    with contextlib.redirect_stdout(io.StringIO()):
                        if failure:
                            with self.assertRaises(SystemExit):
                                self.workload.main()
                        else:
                            self.workload.main()
        self.assertEqual(tool.finalized, 1)

    def test_main_handles_early_completion_and_finalizes(self):
        self.run_main()

    def test_main_finalizes_on_registration_failure(self):
        self.run_main("registration")

    def test_main_finalizes_on_spawn_failure(self):
        self.run_main("spawn")

    def test_main_finalizes_on_termination_failure(self):
        self.run_main("termination")

    def test_main_rejects_finalize_failure(self):
        self.run_main("finalize")


if __name__ == "__main__":
    unittest.main()
