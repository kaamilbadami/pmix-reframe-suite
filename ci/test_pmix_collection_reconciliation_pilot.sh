#!/bin/bash
set -euo pipefail

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
repo_root=$(cd -- "$script_dir/.." && pwd)
wrapper="$script_dir/run_pmix_collection_reconciliation_pilot.sh"
parent_ci="$repo_root/.gitlab-ci.yml"
generator="$script_dir/generate_pmix_child_pipeline.py"

python3 - "$wrapper" "$parent_ci" "$generator" <<'PY'
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

import yaml


wrapper_source = Path(sys.argv[1]).resolve()
parent_ci = Path(sys.argv[2]).resolve()
generator = Path(sys.argv[3]).resolve()
baseline_sha = "a" * 40
commit_a = "b" * 40
commit_b = "c" * 40
suite_sha = "d" * 40
pipeline_id = "12345"
ordered_bytes = f"{commit_a}\n{commit_b}\n".encode()
pass_count = 0
case_count = 0
temporary = tempfile.TemporaryDirectory()
test_root = Path(temporary.name)


def check(condition, message):
    if not condition:
        raise AssertionError(message)


def passed(message):
    global pass_count
    pass_count += 1
    print(f"ok - {message}")


collector_mock = r'''#!/usr/bin/env python3
import json
import os
from pathlib import Path
import sys

arguments = sys.argv[1:]
Path(os.environ["MOCK_COLLECTOR_ARGUMENTS"]).write_text(json.dumps(arguments))
output = Path(arguments[arguments.index("--output") + 1])
output.mkdir(parents=True, exist_ok=True)
(output / "collection-report.json").write_bytes(b"collector-owned-report\n")
(output / ("b" * 40 + ".env")).write_bytes(b"collector-owned-result\n")
raise SystemExit(int(os.environ["MOCK_COLLECTOR_STATUS"]))
'''


reconciler_mock = r'''#!/usr/bin/env python3
import json
import os
from pathlib import Path
import sys

arguments = sys.argv[1:]
Path(os.environ["MOCK_RECONCILER_ARGUMENTS"]).write_text(json.dumps(arguments))
output = Path(arguments[arguments.index("--output") + 1])
output.mkdir(parents=True, exist_ok=True)
(output / "reconciliation.env").write_bytes(b"reconciler-owned-report\n")
(output / "proposed-pmix-master.env").write_bytes(b"reconciler-owned-proposal\n")
raise SystemExit(int(os.environ["MOCK_RECONCILER_STATUS"]))
'''


class Case:
    def __init__(self, collector_status=0, reconciler_status=0):
        global case_count
        case_count += 1
        self.root = test_root / f"case-{case_count}"
        self.ci = self.root / "ci"
        self.ci.mkdir(parents=True)
        self.wrapper = self.ci / wrapper_source.name
        shutil.copyfile(wrapper_source, self.wrapper)
        self.collector = self.ci / "collect_pmix_child_results.py"
        self.reconciler = self.ci / "reconcile_pmix_results.py"
        self.collector.write_text(collector_mock)
        self.reconciler.write_text(reconciler_mock)
        self.collector.chmod(0o755)
        self.reconciler.chmod(0o755)
        self.commits = self.root / "ordered-commits.txt"
        self.commits.write_bytes(ordered_bytes)
        self.output = self.root / "pilot-output"
        self.collector_arguments = self.root / "collector-arguments.json"
        self.reconciler_arguments = self.root / "reconciler-arguments.json"
        self.environment = os.environ.copy()
        self.environment.update({
            "PMIX_CHILD_PIPELINE_BASE_SHA": baseline_sha,
            "CI_COMMIT_SHA": suite_sha,
            "CI_PIPELINE_ID": pipeline_id,
            "MOCK_COLLECTOR_STATUS": str(collector_status),
            "MOCK_RECONCILER_STATUS": str(reconciler_status),
            "MOCK_COLLECTOR_ARGUMENTS": str(self.collector_arguments),
            "MOCK_RECONCILER_ARGUMENTS": str(self.reconciler_arguments),
        })

    def run(self):
        before = self.commits.read_bytes() if self.commits.is_file() else None
        completed = subprocess.run(
            ["bash", str(self.wrapper), str(self.commits), str(self.output)],
            cwd=self.root,
            env=self.environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if before is not None:
            check(self.commits.read_bytes() == before, "ordered commit list changed")
        return completed

    def collector_args(self):
        return json.loads(self.collector_arguments.read_text())

    def reconciler_args(self):
        return json.loads(self.reconciler_arguments.read_text())


case = Case(collector_status=0, reconciler_status=0)
completed = case.run()
check(completed.returncode == 0, "collector 0/reconciler 0 did not exit 0")
check(case.collector_args() == [
    "--commits", str(case.commits),
    "--parent-pipeline-id", pipeline_id,
    "--output", str(case.output / "collection"),
], "collector arguments changed")
check(case.reconciler_args() == [
    "--baseline-state", str(case.output / "baseline-pmix-master.env"),
    "--current-state", str(case.output / "current-pmix-master.env"),
    "--commits", str(case.commits),
    "--results", str(case.output / "collection"),
    "--suite-commit", suite_sha,
    "--output", str(case.output / "reconciliation"),
], "reconciler arguments changed")
passed("collector exit 0 invokes the reconciler with exact ordered arguments")

baseline = (case.output / "baseline-pmix-master.env").read_bytes()
current = (case.output / "current-pmix-master.env").read_bytes()
baseline_lines = baseline.decode().splitlines()
check(baseline_lines[:2] == [
    f"PMIX_COMMIT={baseline_sha}",
    f"SUITE_COMMIT={suite_sha}",
], "pilot baseline identity fields changed")
check(
    len(baseline_lines) == 3
    and re.fullmatch(r"LAST_SUCCESS_EPOCH=[1-9][0-9]*", baseline_lines[2]),
    "pilot baseline epoch is not canonical positive decimal",
)
check(current == baseline, "current snapshot is not byte-identical to baseline")
passed("pilot baseline schema is exact and the current snapshot is byte-identical")

check(
    (case.output / "collection/collection-report.json").read_bytes()
    == b"collector-owned-report\n",
    "wrapper changed the collector report",
)
check(
    (case.output / "reconciliation/reconciliation.env").read_bytes()
    == b"reconciler-owned-report\n",
    "wrapper changed the reconciliation report",
)
check(
    (case.output / "reconciliation/proposed-pmix-master.env").read_bytes()
    == b"reconciler-owned-proposal\n",
    "wrapper changed the reconciler proposal",
)
passed("collector and reconciler retain ownership of report, result, and proposal outputs")

case = Case(collector_status=3, reconciler_status=0)
completed = case.run()
check(completed.returncode == 0, "collector exit 3 did not preserve reconciler 0")
check(case.reconciler_arguments.is_file(), "collector exit 3 skipped reconciliation")
passed("collector exit 3 invokes reconciliation for a possible successful prefix")

case = Case(collector_status=0, reconciler_status=3)
completed = case.run()
check(completed.returncode == 3, "blocked reconciliation status was not preserved")
passed("the wrapper preserves a blocked reconciler status")

case = Case(collector_status=0, reconciler_status=5)
completed = case.run()
check(completed.returncode == 5, "stale reconciler status was not preserved")
passed("the wrapper preserves nonzero reconciler statuses")

for collector_status in (4, 5, 6):
    case = Case(collector_status=collector_status, reconciler_status=0)
    completed = case.run()
    check(completed.returncode == collector_status,
          f"collector status {collector_status} was not preserved")
    check(not case.reconciler_arguments.exists(),
          f"collector status {collector_status} invoked reconciliation")
passed("collector exits 4, 5, and 6 stop without reconciliation")

for malformed in (baseline_sha.upper(), "abc", " " + baseline_sha):
    case = Case()
    case.environment["PMIX_CHILD_PIPELINE_BASE_SHA"] = malformed
    completed = case.run()
    check(completed.returncode == 2, "malformed pilot baseline was accepted")
    check(not case.collector_arguments.exists(),
          "malformed pilot baseline invoked the collector")
passed("malformed pilot baseline SHAs are rejected before collection")

case = Case()
case.commits.unlink()
completed = case.run()
check(completed.returncode == 2 and not case.collector_arguments.exists(),
      "missing ordered list was not rejected before collection")
case = Case()
case.collector.unlink()
completed = case.run()
check(completed.returncode == 2 and not case.collector_arguments.exists(),
      "missing collector was not rejected")
case = Case()
case.reconciler.unlink()
completed = case.run()
check(completed.returncode == 2 and not case.collector_arguments.exists(),
      "missing reconciler was not rejected before collection")
passed("missing ordered list, collector, and reconciler files are rejected")

hidden_name = ".ci" + "-state"
case = Case()
hidden = case.root / hidden_name
hidden.mkdir()
sentinel = hidden / "sentinel"
sentinel.write_bytes(b"preserve\n")
completed = case.run()
check(completed.returncode == 0, "isolated state-safety run failed")
check(sentinel.read_bytes() == b"preserve\n", "shared-state sentinel changed")
source = wrapper_source.read_text(encoding="utf-8")
check(hidden_name not in source, "wrapper names the shared-state path")
absent = Case()
completed = absent.run()
check(completed.returncode == 0, "absent-state safety run failed")
check(not (absent.root / hidden_name).exists(), "wrapper created shared state")
passed("the wrapper neither references, creates, nor changes shared state")

for forbidden in (
    "reframe", "openpmix", "prrte", "slurm", "sbatch", "srun",
    "github", "git commit", "git push", "state_appl", "apply_state",
):
    check(forbidden not in source.lower(),
          f"wrapper contains forbidden capability: {forbidden}")
check("run_exact_pmix_commit" not in source,
      "wrapper can start a PMIx build/test run")
passed("the wrapper has no build, scheduler, ReFrame, GitHub, apply, commit, or push capability")

parent = yaml.safe_load(parent_ci.read_text(encoding="utf-8"))
pilot_rule = '$CI_PIPELINE_SOURCE == "web" && $PMIX_CHILD_PIPELINE_PILOT == "1"'
execution_exclusion = {
    "if": '$PMIX_TESTS_PR_EXECUTION_PILOT == "1"',
    "when": "never",
}
guarded_rules = [execution_exclusion, {"if": pilot_rule}, {"when": "never"}]
generation = parent["generate-pmix-child-pipeline-pilot"]
trigger = parent["trigger-pmix-child-pipeline-pilot"]
job = parent["collect-reconcile-pmix-child-pipeline-pilot"]
check(parent["workflow"]["rules"] == [
    {"if": '$CI_PIPELINE_SOURCE == "web"'},
    {"if": '$CI_PIPELINE_SOURCE == "schedule"'},
    {"when": "never"},
], "workflow guard changed")
check(generation["rules"] == guarded_rules, "generation pilot guard changed")
check(trigger["rules"] == guarded_rules, "trigger pilot guard changed")
check(job["rules"] == [
    execution_exclusion,
    {"if": pilot_rule, "when": "always"},
    {"when": "never"},
], "collection job guard or always behavior changed")
check(job["stage"] == "test", "collection job is not after the trigger stage")
check(parent["stages"].index(trigger["stage"]) < parent["stages"].index(job["stage"]),
      "collection stage is not ordered after the trigger stage")
check(job["needs"] == [
    {"job": "generate-pmix-child-pipeline-pilot", "artifacts": True},
    {"job": "trigger-pmix-child-pipeline-pilot", "artifacts": False},
], "collection job needs changed")
check(trigger["trigger"]["strategy"] == "mirror", "trigger no longer mirrors child status")
check(generation["artifacts"]["paths"] == [
    "ci-generated/pmix-untested-commits.txt",
    "ci-generated/pmix-child-pipeline.yml",
], "generation artifact names changed")
check(job["script"] == [
    "bash ci/run_pmix_collection_reconciliation_pilot.sh "
    "ci-generated/pmix-untested-commits.txt ci-pilot-results"
], "collection wrapper command changed")
check(job["artifacts"] == {
    "when": "always",
    "expire_in": "14 days",
    "paths": [
        "ci-generated/pmix-untested-commits.txt",
        "ci-generated/pmix-child-pipeline.yml",
        "ci-pilot-results/baseline-pmix-master.env",
        "ci-pilot-results/current-pmix-master.env",
        "ci-pilot-results/collection/collection-report.json",
        "ci-pilot-results/collection/*.env",
        "ci-pilot-results/reconciliation/reconciliation.env",
        "ci-pilot-results/reconciliation/proposed-pmix-master.env",
    ],
}, "collection job artifact publication changed")
check("cache" not in job and "resource_group" not in job,
      "collection job gained state cache or child serialization")
serialized_job = json.dumps(job).lower()
check(hidden_name not in serialized_job, "collection job references shared state")
for forbidden in ("pull_request", "merge_request", "whitelist", "allowlist", "github"):
    check(forbidden not in serialized_job,
          f"collection job gained forbidden {forbidden} behavior")
check(
    parent["pmix-python-suite"]["script"].count(
        "bash ci/test_pmix_collection_reconciliation_pilot.sh"
    ) == 1,
    "focused pilot test is absent or duplicated in local helper validation",
)
passed("parent YAML has the guarded always-run job, exact needs, and always-retained artifacts")

generated_input = test_root / "generated-input.txt"
generated_output = test_root / "generated-child.yml"
generated_input.write_text(f"{commit_a}\n{commit_b}\n")
subprocess.run(
    [sys.executable, str(generator), str(generated_input), str(generated_output)],
    check=True,
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
)
generated = yaml.safe_load(generated_output.read_text(encoding="utf-8"))
generated_jobs = [
    value for name, value in generated.items() if name.startswith("pmix-")
]
check(len(generated_jobs) == 2, "generator did not retain one job per commit")
for generated_job in generated_jobs:
    commands = "\n".join(generated_job["script"])
    check("ci/test_" not in commands,
          "generated per-commit job runs the helper-test suite")
    check(generated_job["resource_group"] == "pmix-python-suite-frontier",
          "generated per-commit serialization changed")
passed("generated child jobs retain serialization without running helper validation")

temporary.cleanup()
print(f"1..{pass_count}")
PY
