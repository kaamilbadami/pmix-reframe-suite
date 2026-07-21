#!/bin/bash
set -euo pipefail

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
repo_root=$(cd -- "$script_dir/.." && pwd)
child_ci="$script_dir/pmix_failed_result_child.yml"
parent_ci="$repo_root/.gitlab-ci.yml"
synthetic_sha=deadbeefdeadbeefdeadbeefdeadbeefdeadbeef
pass_count=0

fail() {
    printf 'not ok - %s\n' "$1" >&2
    exit 1
}

pass() {
    printf 'ok - %s\n' "$1"
    pass_count=$((pass_count + 1))
}

[[ -f $child_ci ]] || fail 'static child pipeline is missing'
[[ -f $parent_ci ]] || fail 'parent pipeline is missing'
pass 'parent and child pipeline files exist'

if python3 -c 'import yaml' >/dev/null 2>&1; then
    python3 - "$child_ci" "$parent_ci" "$synthetic_sha" <<'PY'
import json
import pathlib
import re
import sys

import yaml


child_path = pathlib.Path(sys.argv[1])
parent_path = pathlib.Path(sys.argv[2])
synthetic_sha = sys.argv[3]
child = yaml.safe_load(child_path.read_text(encoding="utf-8"))
parent = yaml.safe_load(parent_path.read_text(encoding="utf-8"))

assert synthetic_sha == "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef"
assert re.fullmatch(r"[0-9a-fA-F]{40}", synthetic_sha)

child_header_keys = {"stages", "include", "variables"}
diagnostic_jobs = set(child) - child_header_keys
assert diagnostic_jobs == {"pmix-failed-result-artifact"}
assert child["stages"] == ["test"]
assert child["include"] == [{
    "project": "ci/resources/templates",
    "ref": "main",
    "file": ["/runners.yml"],
}]
assert child["variables"] == {
    "OLCF_SERVICE_ACCOUNT": "gen243_auser",
    "FF_GIT_URLS_WITHOUT_TOKENS": "1",
}

job = child["pmix-failed-result-artifact"]
assert job["stage"] == "test"
assert job["extends"] == [".frontier-shell-runner"]
assert job["timeout"] == "10m"
assert job["variables"] == {"PMIX_COMMIT": synthetic_sha}
assert re.fullmatch(r"[0-9a-fA-F]{40}", job["variables"]["PMIX_COMMIT"])
expected_script = (
    "printf '%s\\n' "
    "'Intentional failure for PMIx result artifact validation.'\n"
    "exit 1\n"
)
assert job["script"] == [expected_script]
assert re.search(r"(?m)^exit [1-9][0-9]*$", job["script"][0])
writer_command = "bash ci/write_pmix_commit_result.sh ci-results"
assert job["after_script"] == [writer_command]
all_commands = "\n".join(job["script"] + job["after_script"])
assert all_commands.count(writer_command) == 1
assert job["artifacts"] == {
    "when": "always",
    "expire_in": "14 days",
    "paths": [f"ci-results/{synthetic_sha}.env"],
}
assert "reports" not in job["artifacts"]
assert not any("*" in path for path in job["artifacts"]["paths"])
assert not job.get("allow_failure", False)
assert "resource_group" not in job

for forbidden in (
    r"\b(?:cmake|make|ninja|reframe|srun|sbatch|salloc|scancel|squeue|sacct)\b",
    r"\b(?:git|curl|wget|ssh)\b",
    r"\b(?:build|fetch|clone)\b",
    r"openpmix",
    r"prrte",
    r"run_exact_pmix_commit",
    r"discover",
    r"reconcil",
    r"\.ci-state",
    r"report_github_status",
):
    assert re.search(forbidden, all_commands, re.IGNORECASE) is None

diagnostic_rule = (
    '$CI_PIPELINE_SOURCE == "web" && $PMIX_FAILED_RESULT_PILOT == "1"'
)
diagnostic_rules = [{"if": diagnostic_rule}, {"when": "never"}]
diagnostic_trigger = parent["trigger-pmix-failed-result-pilot"]
assert diagnostic_trigger == {
    "stage": "pilot-trigger",
    "rules": diagnostic_rules,
    "trigger": {
        "include": [{"local": "ci/pmix_failed_result_child.yml"}],
        "strategy": "mirror",
    },
}
assert "PMIX_FAILED_RESULT_PILOT" not in parent.get("variables", {})

normal_pilot_rule = (
    '$CI_PIPELINE_SOURCE == "web" && $PMIX_CHILD_PIPELINE_PILOT == "1"'
)
normal_pilot_rules = [{"if": normal_pilot_rule}, {"when": "never"}]
generation = parent["generate-pmix-child-pipeline-pilot"]
normal_trigger = parent["trigger-pmix-child-pipeline-pilot"]
assert generation["stage"] == "pilot-generate"
assert generation["extends"] == [".frontier-shell-runner"]
assert generation["rules"] == normal_pilot_rules
assert generation["cache"] == {
    "key": "pmix-master-state-v2",
    "paths": [".ci-state/pmix-master.env"],
    "policy": "pull",
}
assert generation["artifacts"] == {
    "expire_in": "1 day",
    "paths": [
        "ci-generated/pmix-untested-commits.txt",
        "ci-generated/pmix-child-pipeline.yml",
    ],
}
assert normal_trigger == {
    "stage": "pilot-trigger",
    "rules": normal_pilot_rules,
    "trigger": {
        "include": [{
            "artifact": "ci-generated/pmix-child-pipeline.yml",
            "job": "generate-pmix-child-pipeline-pilot",
        }],
        "strategy": "mirror",
    },
}
assert "PMIX_FAILED_RESULT_PILOT" not in json.dumps(generation)
assert "PMIX_FAILED_RESULT_PILOT" not in json.dumps(normal_trigger)

suite = parent["pmix-python-suite"]
artifact_probe_rule = (
    '$CI_PIPELINE_SOURCE == "web" && $PMIX_ARTIFACT_RETRIEVAL_PILOT == "1"'
)
legacy_suite_rules = [
    {"if": normal_pilot_rule, "when": "never"},
    {"if": '$CI_PIPELINE_SOURCE == "web"'},
    {"if": '$CI_PIPELINE_SOURCE == "schedule"'},
    {"when": "never"},
]
assert suite["rules"] == [
    {"if": artifact_probe_rule, "when": "never"},
    {"if": diagnostic_rule, "when": "never"},
    *legacy_suite_rules,
]
assert parent["workflow"]["rules"] == [
    {"if": '$CI_PIPELINE_SOURCE == "web"'},
    {"if": '$CI_PIPELINE_SOURCE == "schedule"'},
    {"when": "never"},
]
assert parent["stages"] == ["pilot-generate", "pilot-trigger", "test"]
suite_script = "\n".join(suite["script"])
assert suite_script.count("bash ci/test_pmix_failed_result_child.sh") == 1

for forbidden_key in (
    "script",
    "after_script",
    "artifacts",
    "cache",
    "resource_group",
    "needs",
):
    assert forbidden_key not in diagnostic_trigger
PY
    pass 'parsed parent and child YAML match the isolated pilot contract'
else
    for required_text in \
        'pmix-failed-result-artifact:' \
        'PMIX_COMMIT: "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef"' \
        'exit 1' \
        'bash ci/write_pmix_commit_result.sh ci-results' \
        'when: always' \
        'expire_in: 14 days' \
        'ci-results/deadbeefdeadbeefdeadbeefdeadbeefdeadbeef.env'
    do
        grep -Fq "$required_text" "$child_ci" ||
            fail "child CI is missing: $required_text"
    done
    for required_text in \
        'trigger-pmix-failed-result-pilot:' \
        '$CI_PIPELINE_SOURCE == "web" && $PMIX_FAILED_RESULT_PILOT == "1"' \
        'local: ci/pmix_failed_result_child.yml' \
        'strategy: mirror'
    do
        grep -Fq "$required_text" "$parent_ci" ||
            fail "parent CI is missing: $required_text"
    done
    pass 'required parent and child text is present; YAML parser unavailable'
fi

printf '1..%d\n' "$pass_count"
