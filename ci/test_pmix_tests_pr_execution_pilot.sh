#!/bin/bash
set -euo pipefail

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
repo_root=$(cd -- "$script_dir/.." && pwd -P)

python3.11 - "$repo_root/.gitlab-ci.yml" <<'PY'
import json
from pathlib import Path
import sys

import yaml


parent = yaml.safe_load(Path(sys.argv[1]).read_text())
pass_count = 0


def check(condition, message):
    if not condition:
        raise AssertionError(message)


def passed(message):
    global pass_count
    pass_count += 1
    print(f"ok - {message}")


execution_rule = (
    '$CI_PIPELINE_SOURCE == "web" && '
    '$PMIX_TESTS_PR_EXECUTION_PILOT == "1"'
)
execution_exclusion = {
    "if": '$PMIX_TESTS_PR_EXECUTION_PILOT == "1"',
    "when": "never",
}
prepare = parent["prepare-pmix-tests-pr-pilot"]
run = parent["run-pmix-tests-pr-pilot"]
finalize = parent["finalize-pmix-tests-pr-pilot"]

check(parent["stages"] == [
    "pilot-generate", "pilot-trigger", "pr-prepare", "test", "pr-finalize"
], "three-job trust-boundary stages changed")
check(prepare["stage"] == "pr-prepare" and run["stage"] == "test"
      and finalize["stage"] == "pr-finalize", "job ordering changed")
for name, job in (("prepare", prepare), ("run", run), ("finalize", finalize)):
    check(job["extends"] == [".frontier-shell-runner"],
          f"{name} runner inheritance changed")
    expected_rules = [{"if": execution_rule}]
    if name != "prepare":
        expected_rules[0]["when"] = "always"
    expected_rules.append({"when": "never"})
    check(job["rules"] == expected_rules,
          f"{name} is not guarded by the exact manual web flag")
passed("preparation, isolated execution, and fresh finalization are separate guarded jobs")

prepare_script = "\n".join(prepare["script"])
for required in (
    "${PMIX_TESTS_PR_NUMBER:-}", "${GITHUB_PR_READ_TOKEN:-}",
    "${GITHUB_STATUS_TOKEN:-}",
    '/bin/bash ci/prepare_trusted_pmix_tests_pr.sh "$PMIX_TESTS_PR_NUMBER"',
):
    check(required in prepare_script, f"preparation is missing {required}")
check("module load" not in prepare_script and "clone" not in prepare_script
      and "run_trusted" not in prepare_script,
      "preparation can clone or execute the PR workload")
check("/usr/bin/rm -rf --one-file-system -- ci-pr-preparation" in prepare_script
      and prepare_script.index("rm -rf") <
      prepare_script.index("${PMIX_TESTS_PR_NUMBER:-}"),
      "preparation does not invalidate stale artifacts before early failure")
check(prepare["artifacts"] == {
    "when": "always", "expire_in": "14 days",
    "paths": ["ci-pr-preparation/preparation.env"],
}, "preparation publishes data beyond the fixed identity record")
passed("preparation alone retrieves metadata, posts pending, and publishes strict identity")

check(run["resource_group"] == "pmix-python-suite-frontier",
      "Frontier serialization changed")
check(run["timeout"] == "1h", "execution timeout changed")
check(run["needs"] == [{
    "job": "prepare-pmix-tests-pr-pilot", "artifacts": True,
}], "execution does not depend only on the preparation artifact")
check(run["variables"] == {
    "GITHUB_PR_READ_TOKEN": "", "GITHUB_STATUS_TOKEN": "",
    "GIT_SUBMODULE_STRATEGY": "none", "GIT_LFS_SKIP_SMUDGE": "1",
}, "execution job credential/submodule controls changed")
run_script = "\n".join(run["script"])
check("module load" not in run_script
      and "/bin/bash ci/run_pmix_tests_pr_isolated.sh" in run_script,
      "execution does not cross the env-i wrapper")
check("/usr/bin/rm -rf --one-file-system -- ci-pr-execution" in run_script
      and run_script.index("rm -rf") <
      run_script.index("run_pmix_tests_pr_isolated.sh"),
      "execution does not invalidate stale artifacts before clean-boundary setup")
for forbidden in ("GITHUB_PR_READ_TOKEN", "GITHUB_STATUS_TOKEN",
                  "report_pmix_tests_pr_status", "after_script", ".ci-state"):
    check(forbidden not in run_script, f"execution script uses forbidden data: {forbidden}")
check("after_script" not in run, "execution job still performs trusted finalization")
passed("execution receives only preparation data and enters the clean environment wrapper")

expected_artifacts = [
    "ci-pr-execution/result.env",
    "ci-pr-execution/checkout.env",
    "ci-pr-execution/checkout-commit.txt",
    "ci-pr-execution/test-source.sha256",
    "ci-pr-execution/reframe/run-report.json",
    "ci-pr-execution/reframe/stage/frontier/batch/pmix_test/PMIxTestsPRPythonSmokeTest/pmix-tests-pr-run-started.env",
    "ci-pr-execution/reframe/stage/frontier/batch/pmix_test/PMIxTestsPRPythonSmokeTest/pmix-tests-pr-run-completed.env",
    "ci-pr-execution/reframe/stage/frontier/batch/pmix_test/PMIxTestsPRPythonSmokeTest/pmix-tests-pr-client-started.env",
    "ci-pr-execution/reframe/stage/frontier/batch/pmix_test/PMIxTestsPRPythonSmokeTest/pmix-tests-pr-client-completed.env",
    "ci-pr-execution/reframe/stage/frontier/batch/pmix_test/PMIxTestsPRPythonSmokeTest/pmix-tests-pr-client-duplicate",
    "ci-pr-execution/reframe/output/frontier/batch/pmix_test/PMIxTestsPRPythonSmokeTest/rfm_job.out",
    "ci-pr-execution/reframe/output/frontier/batch/pmix_test/PMIxTestsPRPythonSmokeTest/rfm_job.err",
]
check(run["artifacts"] == {
    "when": "always", "expire_in": "14 days", "paths": expected_artifacts,
}, "execution artifacts are not the fixed job-specific proof set")
serialized = json.dumps(run["artifacts"])
for forbidden in (".ci-state", "token"):
    check(forbidden not in serialized, f"unsafe/stale artifact path retained: {forbidden}")
for forbidden in ("reports/", "output/", "perflogs/"):
    check(forbidden not in run["artifacts"]["paths"],
          f"unsafe root artifact path retained: {forbidden}")
passed("execution retains only dedicated result, checkout, report, and run evidence")

check(finalize["variables"] == {
    "GIT_STRATEGY": "clone", "GIT_SUBMODULE_STRATEGY": "none",
    "GIT_LFS_SKIP_SMUDGE": "1", "GITHUB_PR_READ_TOKEN": "",
}, "finalization fresh-checkout controls changed")
check(finalize["needs"] == [
    {"job": "prepare-pmix-tests-pr-pilot", "artifacts": True},
    {"job": "run-pmix-tests-pr-pilot", "artifacts": True},
], "finalization does not retrieve the two artifacts separately")
finalize_script = "\n".join(finalize["script"])
for required in (
    "GIT_STRATEGY=clone",
    "/bin/bash ci/finalize_pmix_tests_pr_status.sh",
    "ci-pr-preparation/preparation.env", "ci-pr-execution/result.env",
):
    check(required in finalize_script, f"finalization is missing {required}")
for forbidden in ("ci-pr-execution/ci/", "run_trusted_pmix_tests_pr.sh",
                  "run-report.json", ".ci-state", "module load"):
    check(forbidden not in finalize_script,
          f"finalization executes or trusts execution workspace content: {forbidden}")
passed("finalization uses fresh suite code and separately downloaded data artifacts")

header_keys = {"stages", "include", "variables", "workflow"}
jobs = {name: value for name, value in parent.items()
        if name not in header_keys and isinstance(value, dict)}
trust_jobs = {
    "prepare-pmix-tests-pr-pilot", "run-pmix-tests-pr-pilot",
    "finalize-pmix-tests-pr-pilot",
}
unrelated = set(jobs) - trust_jobs
check(unrelated, "no unrelated jobs found for exclusion audit")
for name in sorted(unrelated):
    rules = jobs[name].get("rules")
    check(rules and rules[0] == execution_exclusion,
          f"unrelated job can run under execution pilot: {name}")
passed("normal suite, metadata pilot, and every unrelated pilot are excluded")

metadata = parent["validate-pmix-tests-pr-pilot"]
metadata_rule = '$CI_PIPELINE_SOURCE == "web" && $PMIX_TESTS_PR_PILOT == "1"'
check(metadata["rules"] == [
    execution_exclusion, {"if": metadata_rule}, {"when": "never"},
], "metadata-only pilot selection behavior changed")
check(metadata["script"] == [
    """set -euo pipefail

if [[ -z ${PMIX_TESTS_PR_NUMBER:-} ]]; then
    printf '%s\\n' 'error: PMIX_TESTS_PR_NUMBER is required' >&2
    exit 2
fi
if [[ -z ${GITHUB_PR_READ_TOKEN:-} ]]; then
    printf '%s\\n' 'error: GITHUB_PR_READ_TOKEN is required' >&2
    exit 2
fi

bash ci/run_pmix_tests_pr_metadata_pilot.sh \\
    "$PMIX_TESTS_PR_NUMBER" ci-pr-pilot
"""
], "metadata-only pilot script changed")
check(metadata["artifacts"] == {
    "when": "always", "expire_in": "14 days",
    "paths": ["ci-pr-pilot/pr.json", "ci-pr-pilot/trusted-pr.env"],
}, "metadata-only pilot artifacts changed")
passed("metadata-only pilot command and artifacts remain unchanged")

check(parent["workflow"]["rules"] == [
    {"if": '$CI_PIPELINE_SOURCE == "web"'},
    {"if": '$CI_PIPELINE_SOURCE == "schedule"'},
    {"when": "never"},
], "pipeline source policy changed")
check("PMIX_TESTS_PR_EXECUTION_PILOT" not in parent.get("variables", {}),
      "execution pilot is enabled by default")
passed("execution remains opt-in under the existing pipeline source policy")

print(f"1..{pass_count}")
PY
