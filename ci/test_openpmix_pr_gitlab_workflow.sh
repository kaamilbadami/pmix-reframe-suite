#!/bin/bash
set -euo pipefail

repo_root=$(cd -- "${BASH_SOURCE[0]%/*}/.." && pwd -P)
cd "$repo_root"
python3 - <<'PY'
from pathlib import Path
import yaml


parent_text = Path(".gitlab-ci.yml").read_text()
jobs_text = Path("ci/openpmix_pr_jobs.yml").read_text()
child_text = Path("ci/openpmix_pr_child.yml").read_text()
parent = yaml.safe_load(parent_text)
jobs = yaml.safe_load(jobs_text)
child = yaml.safe_load(child_text)

assert isinstance(parent, dict) and isinstance(jobs, dict) and isinstance(child, dict)
print("ok - parent, reusable workflow, and child configurations parse as YAML")

for name in (
        "prepare-openpmix-pr", "run-openpmix-pr",
        "finalize-openpmix-pr"):
    assert name in jobs
assert jobs["prepare-openpmix-pr"]["stage"] == "pr-prepare"
assert jobs["run-openpmix-pr"]["stage"] == "test"
assert jobs["finalize-openpmix-pr"]["stage"] == "pr-finalize"
assert jobs["finalize-openpmix-pr"]["variables"]["GIT_STRATEGY"] == "clone"
print("ok - manual workflow preserves three jobs and fresh-checkout finalization")

rules = jobs[".openpmix-pr-rules"]["rules"]
serialized_rules = "\n".join(str(item) for item in rules)
assert 'CI_PIPELINE_SOURCE == "web"' in serialized_rules
assert 'CI_PIPELINE_SOURCE == "parent_pipeline"' in serialized_rules
assert 'OPENPMIX_PR_INTERNAL == "1"' in serialized_rules
assert jobs["run-openpmix-pr"]["variables"]["GITHUB_PR_READ_TOKEN"] == ""
assert jobs["run-openpmix-pr"]["variables"]["GITHUB_STATUS_TOKEN"] == ""
assert "run_openpmix_pr_isolated.sh" in jobs_text
print("ok - execution is opt-in and carries no GitHub metadata or status token")

discovery_rules = parent["generate-openpmix-pr-pipelines"]["rules"]
assert discovery_rules == [
    {"if": '$CI_PIPELINE_SOURCE == "schedule" && $OPENPMIX_PR_AUTODISCOVERY == "1"'},
    {"when": "never"},
]
assert "cache" not in parent["generate-openpmix-pr-pipelines"]
assert ".ci-state" not in str(parent["generate-openpmix-pr-pipelines"])
assert parent["trigger-openpmix-pr-pipelines"]["rules"] == discovery_rules
print("ok - automatic discovery is disabled by default and owns no scheduled PMIx state")

normal_rules = parent["pmix-python-suite"]["rules"]
assert {"if": '$OPENPMIX_PR_INTERNAL == "1"', "when": "never"} in normal_rules
assert {"if": '$CI_PIPELINE_SOURCE == "web"'} in normal_rules
assert {"if": '$CI_PIPELINE_SOURCE == "schedule"'} in normal_rules
assert parent["pmix-python-suite"]["cache"]["key"] == "pmix-master-state-v2"
assert ".ci-state/pmix-master.env" in parent["pmix-python-suite"]["cache"]["paths"]
print("ok - normal manual and scheduled PMIx suite rules and state remain intact")

all_openpmix = jobs_text + child_text + str(
    parent["generate-openpmix-pr-pipelines"]) + str(
    parent["trigger-openpmix-pr-pipelines"])
assert "report_github_status" not in all_openpmix
assert "GITHUB_STATUS_TOKEN" not in child_text
assert "write_pmix_commit_result" not in all_openpmix
assert "pmix-master.env" not in all_openpmix
print("ok - PR results cannot post upstream status or advance last-known-good state")

assert child["workflow"]["rules"][-1] == {"when": "never"}
assert "ci/openpmix_pr_jobs.yml" in child_text
assert parent["run-pmix-tests-pr-pilot"]["script"] is not None
assert "run_pmix_tests_pr_isolated.sh" in parent_text
print("ok - generated children reuse the workflow and the pmix-tests pilot remains separate")

print("1..6")
PY
