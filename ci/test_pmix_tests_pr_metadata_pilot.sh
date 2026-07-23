#!/bin/bash
set -euo pipefail

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
repo_root=$(cd -- "$script_dir/.." && pwd)
wrapper="$script_dir/run_pmix_tests_pr_metadata_pilot.sh"
parent_ci="$repo_root/.gitlab-ci.yml"

python3 - "$wrapper" "$parent_ci" <<'PY'
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
token = "stub-read-token-that-must-not-appear"
head_sha = "0123456789abcdef0123456789abcdef01234567"
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


fetcher_stub = r'''#!/usr/bin/env python3
import json
import os
from pathlib import Path
import sys

arguments = sys.argv[1:]
Path(os.environ["STUB_FETCH_ARGS"]).write_text(json.dumps(arguments))
status = int(os.environ.get("STUB_FETCH_STATUS", "0"))
if status:
    raise SystemExit(status)
output = Path(arguments[arguments.index("--output") + 1])
output.write_text(os.environ["STUB_PR_JSON"])
'''


checker_stub = r'''#!/usr/bin/env python3
import json
import os
from pathlib import Path
import sys

arguments = sys.argv[1:]
Path(os.environ["STUB_CHECK_ARGS"]).write_text(json.dumps(arguments))
Path(os.environ["STUB_CHECK_MARKER"]).write_text("called\n")
status = int(os.environ.get("STUB_CHECK_STATUS", "0"))
output = Path(arguments[arguments.index("--output") + 1])
if status == 0 or os.environ.get("STUB_CHECK_WRITE_ON_FAILURE") == "1":
    output.write_text(
        "PR_ELIGIBLE=1\n"
        "PR_NUMBER=42\n"
        "PR_AUTHOR=kaamilbadami\n"
        "PR_HEAD_SHA=0123456789abcdef0123456789abcdef01234567\n"
        "PR_HEAD_REPOSITORY=kaamilbadami/pmix-tests\n"
        "PR_BASE_REPOSITORY=kaamilbadami/pmix-tests\n"
        "PR_FROM_FORK=0\n"
    )
raise SystemExit(status)
'''


class Case:
    def __init__(self, *, fetch_status=0, check_status=0, token_present=True):
        global case_count
        case_count += 1
        self.root = test_root / f"case-{case_count}"
        self.ci = self.root / "ci"
        self.ci.mkdir(parents=True)
        self.wrapper = self.ci / wrapper_source.name
        shutil.copy2(wrapper_source, self.wrapper)
        self.fetcher = self.ci / "fetch_pmix_tests_pr.py"
        self.checker = self.ci / "check_trusted_pmix_tests_pr.py"
        self.fetcher.write_text(fetcher_stub)
        self.checker.write_text(checker_stub)
        self.fetcher.chmod(0o755)
        self.checker.chmod(0o755)
        self.fetch_arguments = self.root / "fetch-arguments.json"
        self.check_arguments = self.root / "check-arguments.json"
        self.check_marker = self.root / "checker-called"
        self.output_name = "ci-pr-pilot"
        self.output = self.root / self.output_name
        self.environment = os.environ.copy()
        self.environment.pop("GITHUB_PR_READ_TOKEN", None)
        self.environment.update({
            "STUB_FETCH_ARGS": str(self.fetch_arguments),
            "STUB_CHECK_ARGS": str(self.check_arguments),
            "STUB_CHECK_MARKER": str(self.check_marker),
            "STUB_FETCH_STATUS": str(fetch_status),
            "STUB_CHECK_STATUS": str(check_status),
            "STUB_PR_JSON": json.dumps({
                "number": 42,
                "state": "open",
                "body": "metadata is inert",
            }) + "\n",
        })
        if token_present:
            self.environment["GITHUB_PR_READ_TOKEN"] = token

    def run(self, arguments=None, output_name=None):
        if arguments is None:
            chosen_output = self.output_name if output_name is None else output_name
            arguments = ["42", chosen_output]
        completed = subprocess.run(
            ["bash", str(self.wrapper), *arguments],
            cwd=self.root,
            env=self.environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        check(token.encode() not in completed.stdout, "token leaked to stdout")
        check(token.encode() not in completed.stderr, "token leaked to stderr")
        return completed

    def fetch_args(self):
        return json.loads(self.fetch_arguments.read_text())

    def check_args(self):
        return json.loads(self.check_arguments.read_text())


case = Case()
completed = case.run()
check(completed.returncode == 0, "eligible pilot did not succeed")
check((case.output / "pr.json").is_file(), "eligible pilot lost PR JSON")
check((case.output / "trusted-pr.env").is_file(), "eligible pilot lost eligibility")
passed("eligible trusted same-repository metadata completes successfully")

check(case.fetch_args() == [
    "--pr-number", "42", "--output", "ci-pr-pilot/pr.json",
], "fetcher arguments changed")
passed("fetcher receives the exact PR number and output path")

check(case.check_args() == [
    "--pr-json", "ci-pr-pilot/pr.json",
    "--pr-number", "42",
    "--output", "ci-pr-pilot/trusted-pr.env",
], "checker arguments changed")
passed("checker receives the exact JSON, PR number, and output paths")

expected_record = (
    "PR_ELIGIBLE=1\n"
    "PR_NUMBER=42\n"
    "PR_AUTHOR=kaamilbadami\n"
    f"PR_HEAD_SHA={head_sha}\n"
    "PR_HEAD_REPOSITORY=kaamilbadami/pmix-tests\n"
    "PR_BASE_REPOSITORY=kaamilbadami/pmix-tests\n"
    "PR_FROM_FORK=0\n"
)
check((case.output / "trusted-pr.env").read_text() == expected_record,
      "eligible safe fields changed")
passed("eligible output contains the expected safe fields")

case = Case(fetch_status=3)
completed = case.run()
check(completed.returncode == 3, "fetch status was not preserved")
check(not case.check_marker.exists(), "fetch failure invoked checker")
passed("fetch failure prevents checker execution")

case = Case(fetch_status=4)
case.output.mkdir()
(case.output / "pr.json").write_text("stale JSON\n")
(case.output / "trusted-pr.env").write_text("PR_ELIGIBLE=1\n")
completed = case.run()
check(completed.returncode == 4, "authentication failure status changed")
check(not (case.output / "pr.json").exists(), "fetch failure left stale JSON")
check(not (case.output / "trusted-pr.env").exists(),
      "fetch failure left stale eligibility")
passed("fetch failure removes both stale pilot outputs")

case = Case(check_status=3)
case.environment["STUB_CHECK_WRITE_ON_FAILURE"] = "1"
completed = case.run()
check(completed.returncode == 3, "policy rejection status changed")
check((case.output / "pr.json").is_file(), "policy rejection removed PR JSON")
check(not (case.output / "trusted-pr.env").exists(),
      "policy rejection left eligibility output")
passed("policy rejection preserves PR JSON and removes stale eligibility")

for status in (4, 5):
    case = Case(check_status=status)
    case.environment["STUB_CHECK_WRITE_ON_FAILURE"] = "1"
    completed = case.run()
    check(completed.returncode == status, f"checker status {status} changed")
    check((case.output / "pr.json").is_file(),
          f"checker status {status} removed PR JSON")
    check(not (case.output / "trusted-pr.env").exists(),
          f"checker status {status} left eligibility output")
passed("invalid and changed-head failures remove eligibility output")

case = Case(token_present=False)
completed = case.run()
check(completed.returncode == 2, "missing token did not fail locally")
check(not case.fetch_arguments.exists(), "missing token invoked fetcher")
parent = yaml.safe_load(parent_ci.read_text(encoding="utf-8"))
job_script = "\n".join(parent["validate-pmix-tests-pr-pilot"]["script"])
check("${GITHUB_PR_READ_TOKEN:-}" in job_script,
      "GitLab job lacks an explicit token requirement")
passed("missing token fails in the GitLab-facing path without disclosure")

case = Case()
completed = case.run(arguments=[])
check(completed.returncode == 2, "missing PR number did not fail")
check(not case.fetch_arguments.exists(), "missing PR number invoked fetcher")
passed("missing PR number is rejected")

for value in ("0", "00", "042", "+42", "-42", " 42", "42 ", "4.2"):
    case = Case()
    completed = case.run(arguments=[value, case.output_name])
    check(completed.returncode == 2, f"noncanonical PR number {value!r} passed")
    check(not case.fetch_arguments.exists(),
          f"noncanonical PR number {value!r} invoked fetcher")
passed("noncanonical PR numbers are rejected before fetching")

case = Case()
real_output = case.root / "real-output"
real_output.mkdir()
(case.root / "linked-output").symlink_to(real_output, target_is_directory=True)
for unsafe in (str(case.root / "absolute-output"), ".", "../escape", "linked-output"):
    completed = case.run(output_name=unsafe)
    check(completed.returncode == 2, f"unsafe output path {unsafe!r} passed")
check(not case.fetch_arguments.exists(), "unsafe output path invoked fetcher")
check(not list(real_output.iterdir()), "output symlink target changed")
passed("unsafe output paths and symbolic links are rejected")

case = Case()
state_dir = case.root / ".ci-state"
state_dir.mkdir()
sentinel = state_dir / "sentinel"
sentinel.write_text("preserve\n")
completed = case.run(output_name=".ci-state/pilot")
check(completed.returncode == 2, ".ci-state path passed")
check(sentinel.read_text() == "preserve\n", ".ci-state sentinel changed")
check(not (state_dir / "pilot").exists(), ".ci-state output was created")
passed(".ci-state paths fail without modification")

case = Case()
execution_sentinel = case.root / "raw-json-was-executed"
case.environment["STUB_PR_JSON"] = json.dumps({
    "body": f"$(touch {execution_sentinel}) ; touch {execution_sentinel}",
}) + "\n"
completed = case.run()
check(completed.returncode == 0, "inert raw JSON case failed")
check(not execution_sentinel.exists(), "raw JSON was executed or sourced")
passed("raw JSON is never executed or sourced")

source = wrapper_source.read_text(encoding="utf-8")
for forbidden in (
    "git checkout", "git fetch", "git clone", "statuses/", "report_github_status",
    "cmake", "make ", "reframe", "sbatch", "srun", "run_exact_pmix_commit",
    "update_state", "pmix-master.env", "prrte", "libevent",
):
    check(forbidden not in source.lower(),
          f"wrapper contains forbidden capability: {forbidden}")
passed("production wrapper has only metadata fetch and validation capability")

pilot_rule = '$CI_PIPELINE_SOURCE == "web" && $PMIX_TESTS_PR_PILOT == "1"'
execution_exclusion = {
    "if": '$PMIX_TESTS_PR_EXECUTION_PILOT == "1"',
    "when": "never",
}
job = parent["validate-pmix-tests-pr-pilot"]
check(job["rules"] == [execution_exclusion, {"if": pilot_rule}, {"when": "never"}],
      "manual pilot guard changed")
check(job["extends"] == [".frontier-shell-runner"],
      "runner inheritance convention changed")
check("${PMIX_TESTS_PR_NUMBER:-}" in job_script,
      "GitLab job lacks an explicit PR number requirement")
passed("GitLab job has the exact manual web-pilot guard and requirements")

check(job["artifacts"] == {
    "when": "always",
    "expire_in": "14 days",
    "paths": [
        "ci-pr-pilot/pr.json",
        "ci-pr-pilot/trusted-pr.env",
    ],
}, "pilot artifact behavior changed")
passed("job always publishes both possible artifacts for 14 days")

check(job["rules"][-1] == {"when": "never"},
      "absent pilot flag can fall through to execution")
passed("job does not run when the pilot flag is absent")

check("$CI_PIPELINE_SOURCE == \"web\"" in pilot_rule
      and "$CI_PIPELINE_SOURCE == \"schedule\"" not in pilot_rule,
      "scheduled pipeline can select the pilot job")
passed("job does not run for scheduled pipelines")

child_rule = '$CI_PIPELINE_SOURCE == "web" && $PMIX_CHILD_PIPELINE_PILOT == "1"'
check(parent["generate-pmix-child-pipeline-pilot"]["rules"] == [
    execution_exclusion, {"if": child_rule}, {"when": "never"},
], "child generation pilot rules changed")
check(parent["trigger-pmix-child-pipeline-pilot"]["rules"] == [
    execution_exclusion, {"if": child_rule}, {"when": "never"},
], "child trigger pilot rules changed")
check(parent["collect-reconcile-pmix-child-pipeline-pilot"]["rules"] == [
    execution_exclusion, {"if": child_rule, "when": "always"}, {"when": "never"},
], "child collection pilot rules changed")
passed("existing PMIx child-pipeline pilot rules remain intact")

suite_rules = parent["pmix-python-suite"]["rules"]
check(suite_rules[0] == execution_exclusion, "normal PMIx suite lacks execution-pilot exclusion")
check(suite_rules[1] == {
    "if": '$PMIX_TESTS_PR_PILOT == "1"', "when": "never",
}, "normal PMIx suite lacks the PR-pilot exclusion")
passed("normal and scheduled PMIx suite work is excluded by the pilot flag")

for forbidden_network in ("curl ", "wget ", "http://", "https://", "urllib", "socket"):
    check(forbidden_network not in source.lower(),
          f"wrapper gained external networking: {forbidden_network}")
check("--test-only-base-url" not in source,
      "wrapper contains a test-only network override")
passed("focused tests use only local stubs and cannot contact an external host")

temporary.cleanup()
print(f"1..{pass_count}")
PY
