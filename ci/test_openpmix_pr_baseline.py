import copy
import os
from pathlib import Path
import re
import tempfile
import unittest

import yaml

import openpmix_pr_baseline as baseline
from openpmix_pr_artifacts import RecordError


ROOT = Path(__file__).resolve().parent.parent


class BaselineTests(unittest.TestCase):
    def setUp(self):
        self.previous = Path.cwd()
        self.temp = tempfile.TemporaryDirectory()
        os.chdir(self.temp.name)
        baseline.PREPARATION.parent.mkdir()

    def tearDown(self):
        os.chdir(self.previous)
        self.temp.cleanup()

    def test_normal_pr_has_no_baseline_override(self):
        self.assertEqual(baseline.select("123"), "live-pr")

    def test_historical_preparation_and_both_revisions_are_fixed(self):
        baseline.prepare("123")
        self.assertEqual(baseline.select("123"), "master " + baseline.PRRTE_SHA)
        self.assertEqual(baseline.preparation("123")["PR_HEAD_SHA"], baseline.PMIX_SHA)
        self.assertNotIn("PR_STATE", baseline.PREPARATION.read_text())
        with self.assertRaises(RecordError):
            baseline.prepare("123")

    def test_pipeline_identity_cannot_be_reused(self):
        baseline.prepare("123")
        with self.assertRaises(RecordError):
            baseline.select("124")

    def test_tampered_pins_are_rejected(self):
        baseline.prepare("123")
        original = baseline.MANIFEST.read_text()
        for sha in (baseline.PMIX_SHA, baseline.PRRTE_SHA):
            with self.subTest(sha=sha):
                baseline.MANIFEST.write_text(original.replace(sha, "a" * 40))
                with self.assertRaises(RecordError):
                    baseline.select("123")

    def test_manifest_cannot_override_a_different_preparation(self):
        baseline.prepare("123")
        baseline.PREPARATION.write_text(baseline.PREPARATION.read_text().replace(
            baseline.PMIX_SHA, "a" * 40))
        with self.assertRaises(RecordError):
            baseline.select("123")

    def test_symlink_manifest_is_rejected(self):
        baseline.MANIFEST.symlink_to("missing.env")
        with self.assertRaises((RecordError, OSError)):
            baseline.select("123")


class WorkflowTests(unittest.TestCase):
    def test_manual_baseline_reuses_execution_and_is_separate_from_schedules(self):
        config = yaml.safe_load((ROOT / ".gitlab-ci.yml").read_text())
        config.update(yaml.safe_load((ROOT / "ci/openpmix_pr_jobs.yml").read_text()))
        # External template contributes only runner configuration to these jobs.
        config[".frontier-shell-runner"] = {"tags": ["frontier", "shell"]}

        def merge(left, right):
            result = copy.deepcopy(left)
            for key, value in right.items():
                result[key] = merge(result[key], value) if isinstance(value, dict) and isinstance(result.get(key), dict) else copy.deepcopy(value)
            return result

        def resolve(name):
            value = config[name]
            result = {}
            parents = value.get("extends", [])
            if isinstance(parents, str):
                parents = [parents]
            for parent in parents:
                result = merge(result, resolve(parent))
            return merge(result, {k: v for k, v in value.items() if k != "extends"})

        def enabled(job, env):
            for rule in job.get("rules", []):
                expression = rule.get("if")
                if expression:
                    expression = re.sub(r"\$([A-Z0-9_]+)", lambda m: repr(env.get(m[1], "")), expression)
                    if not eval(expression.replace("&&", " and ").replace("||", " or "), {"__builtins__": {}}, {}):
                        continue
                return rule.get("when") != "never"
            return False

        env = {"CI_PIPELINE_SOURCE": "web", "OPENPMIX_PR_BASELINE": "1"}
        selected = [name for name, value in config.items()
                    if not name.startswith(".") and name != "workflow" and isinstance(value, dict)
                    and enabled(resolve(name), env)]
        self.assertCountEqual(selected, ["prepare-openpmix-baseline", "validate-openpmix-baseline", "finalize-openpmix-baseline"])
        execution = resolve("validate-openpmix-baseline")
        normal = resolve("run-openpmix-pr")
        for key in ("resource_group",):
            self.assertEqual(execution[key], normal[key], key)
        self.assertIn('/bin/bash ci/run_openpmix_pr_isolated.sh', str(execution['script']))
        self.assertIn('Slurm runner allocation is required', str(execution['script']))
        self.assertTrue(set(normal['artifacts']['paths']).issubset(execution['artifacts']['paths']))
        self.assertIn('ci/openpmix_baseline_staging.py accounting', str(execution['after_script']))
        self.assertEqual(execution['tags'], ['frontier', 'slurm'])
        self.assertEqual(execution['timeout'], '4h')
        self.assertEqual(execution['variables']['RUNNER_SCRIPT_TIMEOUT'], '3h45m')
        self.assertEqual(execution['variables']['JACAMAR_SCHEDULER_ACTION'], 'default')
        self.assertIn('--nodes=4', execution['variables']['SCHEDULER_PARAMETERS'])
        self.assertIn('--time=00:45:00', execution['variables']['SCHEDULER_PARAMETERS'])
        self.assertEqual(execution["needs"], [{"job": "prepare-openpmix-baseline", "artifacts": True}])
        self.assertEqual(resolve("finalize-openpmix-baseline")["script"], resolve("finalize-openpmix-pr")["script"])
        for source in ("schedule", "parent_pipeline"):
            for name in selected:
                self.assertFalse(enabled(resolve(name), {**env, "CI_PIPELINE_SOURCE": source}))


if __name__ == "__main__":
    unittest.main()
