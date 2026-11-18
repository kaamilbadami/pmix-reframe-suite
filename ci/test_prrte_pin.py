import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock

import generate_pmix_child_pipeline as generator
import prrte_pin as pin
import run_pmix_with_prrte_fallback as fallback
from pmix_result_summary import TESTS
from pmix_result_summary import record


OLD, NEW, PMIX, SUITE = (character * 40 for character in "abcd")


def report(result="fail", phase="compile_wait", runtime="fail_deps", other="pass"):
    return {"runs": [{"testcases": [
        dict(name="build_prrte ~frontier:batch+pmix_test", fixture=True,
             result=result, fail_phase=phase),
        dict(name="build_pmix ~frontier:batch+pmix_test", fixture=True, result=other),
    ] + [dict(name=name, fixture=False, result=runtime) for name in TESTS]}]}


class FallbackTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.prefix = self.root / "reframe"
        self.env = dict(PRRTE_COMMIT=OLD, PMIX_COMMIT=PMIX,
                        PMIX_JOB_ROOT=str(self.root), PMIX_RFM_PREFIX=str(self.prefix))

    def execute(self, reports, statuses):
        calls = []
        def run(argv, env):
            index = len(calls)
            calls.append(env)
            self.prefix.mkdir()
            (self.prefix / "run-report.json").write_text(json.dumps(reports[index]))
            stage = self.prefix / "stage/build_prrte"
            stage.mkdir(parents=True)
            (stage / "rfm_build.err").write_text("attempt {} compiler output".format(index))
            (stage / "large-source.c").write_text("not an artifact")
            self.assertEqual(env["PMIX_COMMIT"], PMIX)
            return statuses[index]
        return run, calls

    def policy(self):
        return json.loads((self.root / "prrte-policy.json").read_text())

    def test_build_failure_retries_same_commit_once_with_separate_evidence(self):
        execute, calls = self.execute([report(), report("pass", None, "pass")], [1, 0])
        resolve = Mock(return_value=NEW)
        self.assertEqual(fallback.run(self.env, execute, resolve), 0)
        resolve.assert_called_once_with()
        self.assertEqual([item["PRRTE_COMMIT"] for item in calls], [OLD, NEW])
        previous = self.root / "prrte-attempts/pinned/reframe/stage/build_prrte"
        self.assertIn("attempt 0", (previous / "rfm_build.err").read_text())
        self.assertFalse((previous / "large-source.c").exists())
        self.assertIn("attempt 1", (self.prefix / "stage/build_prrte/rfm_build.err").read_text())
        self.assertEqual(self.policy()["fallback_attempted"], True)
        self.assertEqual(len(self.policy()["attempts"]), 2)

    def test_success_runtime_failure_or_unconfirmed_build_never_resolves_master(self):
        cases = [(report("pass", None, "pass"), 0),
                 (report("pass", None, "fail"), 1),
                 (report(phase="sanity"), 1),
                 (report(other="fail"), 1), ({}, 1)]
        for index, (document, status) in enumerate(cases):
            with self.subTest(index=index), tempfile.TemporaryDirectory() as directory:
                self.root = Path(directory)
                self.prefix = self.root / "reframe"
                env = dict(self.env, PMIX_JOB_ROOT=str(self.root), PMIX_RFM_PREFIX=str(self.prefix))
                execute, calls = self.execute([document], [status])
                resolve = Mock()
                self.assertEqual(fallback.run(env, execute, resolve), status)
                resolve.assert_not_called()
                self.assertEqual(len(calls), 1)

    def test_unchanged_master_or_lookup_failure_does_not_rebuild(self):
        for response in [OLD, SystemExit(1)]:
            with self.subTest(response=response), tempfile.TemporaryDirectory() as directory:
                self.root = Path(directory)
                self.prefix = self.root / "reframe"
                env = dict(self.env, PMIX_JOB_ROOT=str(self.root), PMIX_RFM_PREFIX=str(self.prefix))
                execute, calls = self.execute([report()], [1])
                resolve = Mock(**({"side_effect": response} if isinstance(response, BaseException)
                                  else {"return_value": response}))
                self.assertEqual(fallback.run(env, execute, resolve), 1)
                self.assertEqual(len(calls), 1)
                self.assertFalse(self.policy()["fallback_attempted"])

    def test_failed_candidate_stops_after_two_attempts(self):
        execute, calls = self.execute([report(), report()], [1, 1])
        self.assertEqual(fallback.run(self.env, execute, Mock(return_value=NEW)), 1)
        self.assertEqual(len(calls), 2)

    def test_fallback_reporting_and_saved_boundary_promote_the_actual_checkout(self):
        project = self.root
        self.root = project / '.ci-work/123'
        self.root.mkdir(parents=True)
        self.prefix = self.root / 'reframe'
        self.env.update(PMIX_JOB_ROOT=str(self.root), PMIX_RFM_PREFIX=str(self.prefix),
                        CI_PROJECT_DIR=str(project), CI_JOB_ID='123', CI_PIPELINE_ID='456',
                        CI_COMMIT_SHA=SUITE, CI_JOB_STATUS='success')
        execute, calls = self.execute([report(), report('pass', None, 'pass')], [1, 0])
        def with_provenance(argv, env):
            result = execute(argv, env)
            (self.prefix / 'stage/build_prrte/prrte-source.env').write_text(
                'PRRTE_COMMIT={}\n'.format(env['PRRTE_COMMIT']))
            return result
        self.assertEqual(fallback.run(self.env, with_provenance, Mock(return_value=NEW)), 0)
        state, snapshot = project / 'pin.json', project / 'pin-snapshot.json'
        pin.prepare(state, snapshot, lambda: OLD)
        collection = project / 'collection'
        evidence = record(self.env, collection)
        self.assertEqual(evidence['prrte_revision'], NEW)
        self.assertEqual(evidence['prrte_policy']['pinned'], OLD)
        self.assertEqual(evidence['checks']['passed'], 11)
        (collection / (PMIX + '.env')).write_text(
            'PMIX_COMMIT={}\nSUITE_COMMIT={}\nCI_JOB_STATUS=success\nCI_JOB_ID=123\nCI_PIPELINE_ID=456\n'.format(PMIX, SUITE))
        (collection / 'attempts.json').write_text(json.dumps(dict(
            child_pipeline_id='456', jobs=[dict(commit=PMIX, id=123, status='success')])))
        # Exercise the real ordered Reconcile -> Apply path, with a later
        # failure. Only the passing prefix may authorize either saved state.
        ci = Path(__file__).resolve().parent
        saved = project / 'saved.env'
        initial = 'PMIX_COMMIT={}\nSUITE_COMMIT={}\nLAST_SUCCESS_EPOCH=1\n'.format('f' * 40, SUITE)
        saved.write_text(initial)
        baseline = project / 'baseline.env'
        baseline.write_text(initial)
        commits = project / 'commits'
        commits.write_text(PMIX + '\n' + 'e' * 40 + '\n')
        (collection / ('e' * 40 + '.env')).write_text(
            'PMIX_COMMIT={}\nSUITE_COMMIT={}\nCI_JOB_STATUS=failed\nCI_JOB_ID=124\nCI_PIPELINE_ID=456\n'.format('e' * 40, SUITE))
        reconciliation = project / 'reconciliation'
        result = subprocess.run([sys.executable, str(ci / 'reconcile_pmix_results.py'),
            '--baseline-state', str(baseline), '--current-state', str(saved),
            '--commits', str(commits), '--results', str(collection), '--suite-commit', SUITE,
            '--output', str(reconciliation)], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(result.returncode, 3, result.stderr)
        result = subprocess.run([sys.executable, str(ci / 'apply_reconciled_pmix_state.py'), 'apply',
            '--baseline-state', str(baseline), '--authoritative-state', str(saved),
            '--commits', str(commits), '--suite-commit', SUITE,
            '--report', str(reconciliation / 'reconciliation.env'),
            '--proposal', str(reconciliation / 'proposed-pmix-master.env')],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(result.returncode, 0, result.stderr)
        pin.promote(state, snapshot, saved, collection, SUITE, project / 'pin-update.json')
        self.assertEqual(pin.load(state)['prrte_commit'], NEW)


class PinTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.state = self.root / "pin.json"
        self.snapshot = self.root / "snapshot.json"
        self.saved = self.root / "saved.env"
        self.collection = self.root / "collection"
        self.collection.mkdir()
        self.output = self.root / "update.json"
        pin.prepare(self.state, self.snapshot, lambda: OLD)
        self.saved.write_text("PMIX_COMMIT={}\nSUITE_COMMIT={}\nLAST_SUCCESS_EPOCH=1\n".format(PMIX, SUITE))
        (self.collection / (PMIX + ".env")).write_text(
            "PMIX_COMMIT={}\nSUITE_COMMIT={}\nCI_JOB_STATUS=success\nCI_JOB_ID=123\nCI_PIPELINE_ID=456\n".format(PMIX, SUITE))
        (self.collection / "attempts.json").write_text(json.dumps(dict(
            child_pipeline_id="456", jobs=[dict(commit=PMIX, id=123, status="success")])))
        self.evidence = dict(schema_version=1, pmix_commit=PMIX, suite_commit=SUITE,
            ci_status="success", job_id=123, pipeline_id=456, total_checks=11,
            checks=dict(passed=11, failed=0, incomplete=0), prrte_revision=NEW,
            prrte_revision_source="checkout", recorded_at="2026-11-18T01:00:00+00:00",
            prrte_policy=dict(pinned=OLD, candidate=NEW, fallback_attempted=True))
        self.write_evidence()

    def write_evidence(self):
        (self.collection / (PMIX + ".summary.json")).write_text(json.dumps(self.evidence))

    def promote(self):
        return pin.promote(self.state, self.snapshot, self.saved, self.collection, SUITE, self.output)

    def test_existing_pin_is_retained_without_resolving_master(self):
        resolve = Mock()
        self.assertEqual(pin.prepare(self.state, self.snapshot, resolve), OLD)
        resolve.assert_not_called()

    def test_verified_seed_is_used_only_for_initialization(self):
        seed = self.root / "seed.json"
        seed.write_text(json.dumps(dict(schema_version=1, prrte_commit=NEW, history=[])))
        resolve = Mock()
        state = self.root / "seeded.json"
        self.assertEqual(pin.prepare(state, self.snapshot, resolve, seed), NEW)
        resolve.assert_not_called()
        self.assertEqual(pin.prepare(self.state, self.snapshot, resolve, seed), OLD)

    def test_full_passing_boundary_promotes_and_preserves_history_idempotently(self):
        self.assertEqual(self.promote()["status"], "promoted")
        value = pin.load(self.state)
        self.assertEqual(value["prrte_commit"], NEW)
        self.assertEqual(value["history"][0]["previous"], OLD)
        self.assertEqual(value["history"][0]["pmix_commit"], PMIX)
        self.promote()
        self.assertEqual(len(pin.load(self.state)["history"]), 1)

    def test_failed_incomplete_wrong_identity_or_requested_only_never_promotes(self):
        original = copy.deepcopy(self.evidence)
        variants = [dict(checks=dict(passed=10, failed=1, incomplete=0)),
                    dict(checks=dict(passed=10, failed=0, incomplete=1)),
                    dict(checks=dict(passed=True, failed=0, incomplete=0)),
                    dict(prrte_revision_source="requested"), dict(job_id=999),
                    dict(pipeline_id=999), dict(suite_commit="e" * 40),
                    dict(prrte_policy=dict(pinned=OLD, candidate=NEW, fallback_attempted=False))]
        for variant in variants:
            with self.subTest(variant=variant):
                self.evidence = dict(original, **variant)
                self.write_evidence()
                with self.assertRaises(ValueError):
                    self.promote()
                self.assertEqual(pin.load(self.state)["prrte_commit"], OLD)

    def test_stale_pin_cannot_be_overwritten(self):
        changed = pin.load(self.state)
        changed["prrte_commit"] = "e" * 40
        self.state.write_bytes(pin.content(changed))
        with self.assertRaises(ValueError):
            self.promote()
        self.assertEqual(pin.load(self.state), changed)

    def test_later_success_beyond_saved_boundary_does_not_promote(self):
        self.saved.write_text("PMIX_COMMIT={}\nSUITE_COMMIT={}\nLAST_SUCCESS_EPOCH=1\n".format("f" * 40, SUITE))
        self.assertEqual(self.promote()["status"], "unchanged")
        self.assertEqual(pin.load(self.state)["prrte_commit"], OLD)

    def test_generated_persistent_jobs_use_fallback_and_keep_both_attempts(self):
        commits, output = self.root / "commits", self.root / "child.yml"
        commits.write_text(PMIX + "\n")
        generator.main([str(commits), str(output), "--prrte-pin", str(self.state),
                        "--prrte-pin-snapshot", str(self.snapshot)])
        rendered = output.read_text()
        self.assertIn('PRRTE_COMMIT: "' + OLD + '"', rendered)
        self.assertIn("python3 ci/run_pmix_with_prrte_fallback.py", rendered)
        self.assertIn("prrte-attempts/", rendered)
        self.assertNotIn(str(self.state), rendered)


if __name__ == "__main__":
    unittest.main()
