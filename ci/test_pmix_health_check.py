#!/usr/bin/env python3
"""Exercise daily selection, result reconciliation, and persistent state application."""

import argparse
from pathlib import Path
import tempfile
import unittest

from prepare_pmix_health_check import prepare
from reconcile_pmix_results import reconcile, parse_state
from apply_reconciled_pmix_state import apply_state, ValidationError


class HealthCheckTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.base = "a" * 40
        self.suite = "b" * 40
        self.state = self.root / "state"
        self.original = f"PMIX_COMMIT={self.base}\nSUITE_COMMIT={self.suite}\nLAST_SUCCESS_EPOCH=100000\n"
        self.state.write_text(self.original)
        self.baseline = self.root / "baseline"
        self.baseline.write_text(self.original)
        self.commits = self.root / "commits"
        self.commits.write_text("")
        self.results = self.root / "results"
        self.results.mkdir()
        self.output = self.root / "output"

    def test_due_boundary_and_backlog(self):
        for now in (99999, 186399):
            self.assertFalse(prepare(self.state, self.commits, now))
            self.assertEqual(self.commits.read_text(), "")
        self.assertTrue(prepare(self.state, self.commits, 186400))
        self.assertEqual(self.commits.read_text(), self.base + "\n")
        self.commits.write_text("c" * 40 + "\n")
        self.assertFalse(prepare(self.state, self.commits, 999999))
        self.assertEqual(self.commits.read_text(), "c" * 40 + "\n")

    def run_result(self, status, allow=True):
        prepare(self.state, self.commits, 186400)
        if status is not None:
            (self.results / (self.base + ".env")).write_text(
                f"PMIX_COMMIT={self.base}\nSUITE_COMMIT={self.suite}\n"
                f"CI_JOB_STATUS={status}\nCI_JOB_ID=1\nCI_PIPELINE_ID=2\n")
        args = ["--baseline-state", str(self.baseline), "--current-state", str(self.state),
                "--commits", str(self.commits), "--results", str(self.results),
                "--suite-commit", self.suite, "--output", str(self.output)]
        if allow:
            args.append("--allow-baseline-retest")
        return reconcile(args)

    def apply(self):
        apply_state(argparse.Namespace(
            baseline_state=self.baseline, authoritative_state=self.state,
            commits=self.commits, report=self.output / "reconciliation.env",
            proposal=self.output / "proposed-pmix-master.env", suite_commit=self.suite,
            allow_baseline_retest=True))

    def test_success_refreshes_timestamp_and_skips_next_check(self):
        self.assertEqual(self.run_result("success"), 0)
        self.apply()
        state, _ = parse_state(self.state)
        self.assertEqual(state["PMIX_COMMIT"], self.base)
        self.assertGreater(int(state["LAST_SUCCESS_EPOCH"]), 100000)
        self.commits.write_text("")
        self.assertFalse(prepare(self.state, self.commits, int(state["LAST_SUCCESS_EPOCH"]) + 1800))

    def test_failure_leaves_health_check_due(self):
        self.assertEqual(self.run_result("failed"), 3)
        self.apply()
        self.assertEqual(self.state.read_text(), self.original)
        self.commits.write_text("")
        self.assertTrue(prepare(self.state, self.commits, 186500))

    def test_missing_result_leaves_state_unchanged(self):
        self.assertEqual(self.run_result(None), 3)
        self.apply()
        self.assertEqual(self.state.read_text(), self.original)

    def test_baseline_retest_requires_explicit_permission(self):
        self.assertEqual(self.run_result("success", allow=False), 4)

    def test_stale_state_blocks_apply(self):
        self.assertEqual(self.run_result("success"), 0)
        self.state.write_text(self.original.replace("100000", "100001"))
        with self.assertRaises(ValidationError):
            self.apply()


if __name__ == "__main__":
    unittest.main()
