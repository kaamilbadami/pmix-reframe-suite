#!/bin/bash
set -euo pipefail

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
repo_root=$(cd -- "$script_dir/.." && pwd)
generator="$script_dir/generate_pmix_child_pipeline.py"
parent_ci="$repo_root/.gitlab-ci.yml"
test_dir=$(mktemp -d)
trap 'rm -rf -- "$test_dir"' EXIT

lower_sha=0123456789abcdef0123456789abcdef01234567
upper_sha=89ABCDEF0123456789ABCDEF0123456789ABCDEF
third_sha=fedcba9876543210fedcba9876543210fedcba98
pass_count=0

fail() {
    printf 'not ok - %s\n' "$1" >&2
    exit 1
}

pass() {
    printf 'ok - %s\n' "$1"
    pass_count=$((pass_count + 1))
}

expect_failure() {
    local label=$1
    shift
    if python3 "$generator" "$@" > /dev/null 2> "$test_dir/error"; then
        fail "$label was accepted"
    fi
    pass "$label"
}

write_input() {
    local path=$1
    shift
    printf '%s\n' "$@" > "$path"
}

pilot_override_is_valid() {
    [[ $1 =~ ^[0-9A-Fa-f]{40}$ ]]
}

pilot_override_is_valid "$lower_sha" ||
    fail 'pilot override validation rejected a full lowercase SHA'
pass 'pilot override validation accepts a full lowercase SHA'
pilot_override_is_valid "$upper_sha" ||
    fail 'pilot override validation rejected a full uppercase SHA'
pass 'pilot override validation accepts a full uppercase SHA'

for invalid_override in \
    0123456789abcdef \
    master \
    refs/tags/v1.0 \
    " $lower_sha" \
    "$lower_sha " \
    0123456789abcdef0123456789abcdef0123456g
do
    if pilot_override_is_valid "$invalid_override"; then
        fail "pilot override validation accepted: $invalid_override"
    fi
done
pass 'pilot override validation rejects short, symbolic, whitespace, and non-hex values'

expect_failure 'missing arguments are rejected'
expect_failure 'too many arguments are rejected' one two three
expect_failure 'missing input is rejected' \
    "$test_dir/missing" "$test_dir/missing.yml"

valid_input="$test_dir/valid"
write_input "$valid_input" "$lower_sha"
expect_failure 'missing output directory is rejected' \
    "$valid_input" "$test_dir/missing-dir/output.yml"
output_directory="$test_dir/output-directory"
mkdir -p -- "$output_directory"
printf 'preserve\n' > "$output_directory/sentinel"
cp -a -- "$output_directory" "$test_dir/output-directory-before"
expect_failure 'directory output is rejected' "$valid_input" "$output_directory"
diff -r -- "$test_dir/output-directory-before" "$output_directory" \
    >/dev/null || fail 'rejected output directory changed'

same_file="$test_dir/same-file"
write_input "$same_file" "$lower_sha"
cp -- "$same_file" "$test_dir/same-file-before"
expect_failure 'input/output collision is rejected' "$same_file" "$same_file"
cmp -s -- "$test_dir/same-file-before" "$same_file" ||
    fail 'input/output collision changed the input'

blank_input="$test_dir/blank"
printf '%s\n\n' "$lower_sha" > "$blank_input"
expect_failure 'blank input line is rejected' "$blank_input" "$test_dir/blank.yml"
leading_input="$test_dir/leading"
write_input "$leading_input" " $lower_sha"
expect_failure 'leading whitespace is rejected' \
    "$leading_input" "$test_dir/leading.yml"
trailing_input="$test_dir/trailing"
write_input "$trailing_input" "$lower_sha "
expect_failure 'trailing whitespace is rejected' \
    "$trailing_input" "$test_dir/trailing.yml"

short_input="$test_dir/short"
write_input "$short_input" 0123456789abcdef
expect_failure 'short SHA is rejected' "$short_input" "$test_dir/short.yml"
nonhex_input="$test_dir/nonhex"
write_input "$nonhex_input" 0123456789abcdef0123456789abcdef0123456g
expect_failure 'non-hexadecimal SHA is rejected' \
    "$nonhex_input" "$test_dir/nonhex.yml"
symbolic_input="$test_dir/symbolic"
write_input "$symbolic_input" master
expect_failure 'symbolic ref is rejected' "$symbolic_input" "$test_dir/symbolic.yml"
duplicate_input="$test_dir/duplicate"
write_input "$duplicate_input" "$lower_sha" "$lower_sha"
expect_failure 'duplicate SHA is rejected' \
    "$duplicate_input" "$test_dir/duplicate.yml"

empty_input="$test_dir/empty"
empty_output="$test_dir/empty.yml"
: > "$empty_input"
python3 "$generator" "$empty_input" "$empty_output"
grep -Fq 'No untested OpenPMIx commits were discovered.' "$empty_output" ||
    fail 'empty input omitted the no-op message'
if grep -Eq 'PMIX_COMMIT|run_exact_pmix_commit' "$empty_output"; then
    fail 'empty input generated an exact-commit job'
fi
if grep -Eq 'write_pmix_commit_result|ci-results|^[[:space:]]+artifacts:' \
    "$empty_output"; then
    fail 'empty input generated per-commit result behavior'
fi
pass 'empty input generates only the no-op job'

multiple_input="$test_dir/multiple"
multiple_output="$test_dir/multiple.yml"
write_input "$multiple_input" "$lower_sha" "$upper_sha" "$third_sha"
python3 "$generator" "$multiple_input" "$multiple_output"
[[ $(grep -Ec '^pmix-[0-9A-Fa-f]{40}:$' "$multiple_output") == 3 ]] ||
    fail 'generator did not create one job per SHA'
pass 'generator creates one job per SHA'

lower_line=$(grep -n "^pmix-$lower_sha:" "$multiple_output" | cut -d: -f1)
upper_line=$(grep -n "^pmix-$upper_sha:" "$multiple_output" | cut -d: -f1)
third_line=$(grep -n "^pmix-$third_sha:" "$multiple_output" | cut -d: -f1)
(( lower_line < upper_line && upper_line < third_line )) ||
    fail 'job order or SHA case was not preserved'
for sha in "$lower_sha" "$upper_sha" "$third_sha"; do
    grep -Fq "pmix-$sha:" "$multiple_output" || fail "missing job for $sha"
    grep -Fq "PMIX_COMMIT: \"$sha\"" "$multiple_output" ||
        fail "missing exact PMIX_COMMIT for $sha"
done
pass 'SHA case, order, and exact PMIX_COMMIT values are preserved'

[[ $(grep -Fc 'bash ci/run_exact_pmix_commit.sh' "$multiple_output") == 3 ]] ||
    fail 'not every job invokes the exact runner'
pass 'every job invokes the exact runner'
[[ $(grep -Fc 'bash ci/write_pmix_commit_result.sh ci-results' \
    "$multiple_output") == 3 ]] ||
    fail 'not every job invokes the result helper exactly once'
pass 'every job invokes the result helper exactly once'
for sha in "$lower_sha" "$upper_sha" "$third_sha"; do
    result_path="ci-results/${sha,,}.env"
    [[ $(grep -Fxc "      - $result_path" "$multiple_output") == 1 ]] ||
        fail "job does not have exactly its lowercase result path: $sha"
done
[[ $(grep -Fc 'when: always' "$multiple_output") == 3 ]] ||
    fail 'not every job preserves artifacts on all outcomes'
[[ $(grep -Fc 'expire_in: 14 days' "$multiple_output") == 3 ]] ||
    fail 'not every job has the required result retention'
if grep -Eqi 'reports:|dotenv|ci-results/.*\*' "$multiple_output"; then
    fail 'generated artifacts use a forbidden report or wildcard'
fi
pass 'every job has an always-retained lowercase SHA-specific result artifact'
grep -Fq 'project: ci/resources/templates' "$multiple_output" &&
    grep -Fq 'ref: main' "$multiple_output" &&
    grep -Fq -- '- /runners.yml' "$multiple_output" ||
    fail 'approved runner include is missing'
[[ $(grep -Fc -- '- .frontier-shell-runner' "$multiple_output") == 3 ]] ||
    fail 'not every job extends the Frontier runner'
[[ $(grep -Fc 'timeout: 1h' "$multiple_output") == 3 ]] ||
    fail 'not every job uses the current timeout'
[[ $(grep -Fc 'resource_group: pmix-python-suite-frontier' \
    "$multiple_output") == 3 ]] || fail 'resource group is missing'
pass 'runner include, base, timeout, and resource group are preserved'

if grep -Eqi \
    '\.ci-state|PMIX_CHILD_PIPELINE_BASE_SHA|should_run_pmix_suite|discover|reconcil|update.*state|state.*update|github|openpmix\.git|ls-remote|git fetch' \
    "$multiple_output"; then
    fail 'generated jobs contain forbidden state, reconciliation, discovery, or status behavior'
fi
pass 'generated jobs contain no state, updater, discovery, reconciliation, or GitHub behavior'

atomic_output="$test_dir/atomic.yml"
printf 'old output\n' > "$atomic_output"
python3 "$generator" "$valid_input" "$atomic_output"
grep -Fq "pmix-$lower_sha:" "$atomic_output" ||
    fail 'existing output was not replaced'
grep -Fq 'os.replace(temporary_name, output_path)' "$generator" ||
    fail 'generator does not use atomic replacement'
pass 'existing output is replaced atomically'

preserved_output="$test_dir/preserved.yml"
printf 'preserve existing output\n' > "$preserved_output"
cp -- "$preserved_output" "$test_dir/preserved-before"
expect_failure 'invalid generation fails' "$short_input" "$preserved_output"
cmp -s -- "$test_dir/preserved-before" "$preserved_output" ||
    fail 'failed generation changed existing output'
pass 'existing output is preserved after failure'

if python3 -c 'import yaml' >/dev/null 2>&1; then
    python3 - "$empty_output" "$multiple_output" <<'PY'
import pathlib
import sys
import yaml

lower_sha = "0123456789abcdef0123456789abcdef01234567"
upper_sha = "89ABCDEF0123456789ABCDEF0123456789ABCDEF"
third_sha = "fedcba9876543210fedcba9876543210fedcba98"

empty = yaml.safe_load(pathlib.Path(sys.argv[1]).read_text())
multiple = yaml.safe_load(pathlib.Path(sys.argv[2]).read_text())

header = {
    "stages": ["test"],
    "include": [{
        "project": "ci/resources/templates",
        "ref": "main",
        "file": ["/runners.yml"],
    }],
    "variables": {
        "OLCF_SERVICE_ACCOUNT": "gen243_auser",
        "FF_GIT_URLS_WITHOUT_TOKENS": "1",
    },
}
expected_noop = {
    **header,
    "no-untested-pmix-commits": {
        "stage": "test",
        "extends": [".frontier-shell-runner"],
        "script": [
            "printf '%s\\n' 'No untested OpenPMIx commits were discovered.'\n"
        ],
    },
}
assert empty == expected_noop

expected_script = """\
set -euo pipefail
module load miniforge3/23.11.0-0
python3 -m venv .ci-venv
source .ci-venv/bin/activate
python -m pip install --upgrade pip
python -m pip install "Cython==3.2.6" "reframe-hpc==4.10.0"
export PMIX_PYTHON="${CI_PROJECT_DIR}/.ci-venv/bin/python"
export RFM_BIN="${CI_PROJECT_DIR}/.ci-venv/bin/reframe"
bash ci/run_exact_pmix_commit.sh
"""
expected_multiple = dict(header)
for sha in (lower_sha, upper_sha, third_sha):
    expected_multiple[f"pmix-{sha}"] = {
        "stage": "test",
        "extends": [".frontier-shell-runner"],
        "timeout": "1h",
        "resource_group": "pmix-python-suite-frontier",
        "variables": {"PMIX_COMMIT": sha},
        "script": [expected_script],
        "after_script": ["bash ci/write_pmix_commit_result.sh ci-results"],
        "artifacts": {
            "when": "always",
            "expire_in": "14 days",
            "paths": [f"ci-results/{sha.lower()}.env"],
        },
    }
assert multiple == expected_multiple
PY
else
    printf '# YAML parser unavailable; parse validation skipped\n'
fi
pass 'parsed generated YAML matches the expected real and no-op structures'

if python3 -c 'import yaml' >/dev/null 2>&1; then
    python3 - "$parent_ci" <<'PY'
import pathlib
import re
import sys
import yaml

parent = yaml.safe_load(pathlib.Path(sys.argv[1]).read_text())
failed_result_rule = (
    '$CI_PIPELINE_SOURCE == "web" && $PMIX_FAILED_RESULT_PILOT == "1"'
)
pilot_rule = (
    '$CI_PIPELINE_SOURCE == "web" && $PMIX_CHILD_PIPELINE_PILOT == "1"'
)
pilot_rules = [{"if": pilot_rule}, {"when": "never"}]

generation = parent["generate-pmix-child-pipeline-pilot"]
trigger = parent["trigger-pmix-child-pipeline-pilot"]
suite = parent["pmix-python-suite"]

assert generation["rules"] == pilot_rules
assert trigger["rules"] == pilot_rules
assert suite["rules"] == [
    {"if": failed_result_rule, "when": "never"},
    {"if": pilot_rule, "when": "never"},
    {"if": '$CI_PIPELINE_SOURCE == "web"'},
    {"if": '$CI_PIPELINE_SOURCE == "schedule"'},
    {"when": "never"},
]
assert parent["workflow"]["rules"] == [
    {"if": '$CI_PIPELINE_SOURCE == "web"'},
    {"if": '$CI_PIPELINE_SOURCE == "schedule"'},
    {"when": "never"},
]
assert parent["stages"].index(generation["stage"]) < parent["stages"].index(
    trigger["stage"]
)
assert generation["cache"] == {
    "key": "pmix-master-state-v2",
    "paths": [".ci-state/pmix-master.env"],
    "policy": "pull",
}
assert generation["artifacts"]["paths"] == [
    "ci-generated/pmix-untested-commits.txt",
    "ci-generated/pmix-child-pipeline.yml",
]
assert trigger["trigger"] == {
    "include": [{
        "artifact": "ci-generated/pmix-child-pipeline.yml",
        "job": "generate-pmix-child-pipeline-pilot",
    }],
    "strategy": "mirror",
}
assert "resource_group" not in generation
assert "resource_group" not in trigger
assert "script" not in trigger
generation_script = "\n".join(generation["script"])
assert "cached PMIx state was not restored" in generation_script
for forbidden in (
    "SUITE_COMMIT",
    "LAST_SUCCESS_EPOCH",
    "state_tmp",
    "Saved successful PMIx",
):
    assert forbidden not in generation_script
assert "PMIX_CHILD_PIPELINE_BASE_SHA" not in parent.get("variables", {})
assert "${PMIX_CHILD_PIPELINE_BASE_SHA:-}" in generation_script
assert (
    "[[ ! $PMIX_CHILD_PIPELINE_BASE_SHA =~ ^[0-9A-Fa-f]{40}$ ]]"
    in generation_script
)
assert "discovery_state=.ci-state/pmix-master.env" in generation_script
assert "discovery_state=ci-generated/pmix-pilot-state.env" in generation_script
assert re.search(
    r"printf 'PMIX_COMMIT=%s\\n' \"\$PMIX_CHILD_PIPELINE_BASE_SHA\" > "
    r"\\\n\s+ci-generated/pmix-pilot-state\.env",
    generation_script,
)
assert re.search(
    r"discover_untested_pmix_commits\.sh\s+\\\n\s+"
    r'"\$discovery_state"',
    generation_script,
)
validation_position = generation_script.index(
    "[[ ! $PMIX_CHILD_PIPELINE_BASE_SHA =~ ^[0-9A-Fa-f]{40}$ ]]"
)
required_state_position = generation_script.index(
    "if [[ ! -f .ci-state/pmix-master.env ]]"
)
discovery_position = generation_script.index(
    "bash ci/discover_untested_pmix_commits.sh"
)
assert required_state_position < validation_position
assert validation_position < discovery_position
checksum_before_position = generation_script.index(
    "official_state_checksum_before=$(sha256sum -- .ci-state/pmix-master.env)"
)
temporary_state_position = generation_script.index(
    "ci-generated/pmix-pilot-state.env"
)
generation_position = generation_script.index(
    "python3 ci/generate_pmix_child_pipeline.py"
)
checksum_after_position = generation_script.index(
    "official_state_checksum_after=$(sha256sum -- .ci-state/pmix-master.env)"
)
assert checksum_before_position < temporary_state_position
assert checksum_after_position > generation_position
assert (
    '[[ $official_state_checksum_before != "$official_state_checksum_after" ]]'
    in generation_script
)
assert "error: the manual pilot modified the official cached PMIx state" in generation_script
checksum_guard = generation_script[checksum_after_position:]
assert checksum_guard.index("error: the manual pilot modified") < checksum_guard.index(
    "exit 1"
)
assert "Pilot discovery baseline: official cached state" in generation_script
assert "Pilot discovery baseline override: %s" in generation_script
for line in generation_script.splitlines():
    assert not re.search(
        r"\b(?:cp|mv|sed)\b.*\.ci-state/pmix-master\.env", line
    )
    assert not (
        ".ci-state/pmix-master.env" in line
        and re.search(r"(?:^|\s)(?:>|>>)(?:\s|$)", line)
    )
for job in parent.values():
    if not isinstance(job, dict):
        continue
    rules = job.get("rules", [])
    has_schedule_rule = any(
        isinstance(rule, dict) and "schedule" in rule.get("if", "")
        for rule in rules
    )
    if has_schedule_rule:
        assert "PMIX_CHILD_PIPELINE_BASE_SHA" not in "\n".join(
            job.get("script", [])
        )
PY
else
    for required_text in \
        'generate-pmix-child-pipeline-pilot:' \
        'trigger-pmix-child-pipeline-pilot:' \
        '$CI_PIPELINE_SOURCE == "web" && $PMIX_CHILD_PIPELINE_PILOT == "1"' \
        'policy: pull' \
        'artifact: ci-generated/pmix-child-pipeline.yml' \
        'job: generate-pmix-child-pipeline-pilot' \
        'strategy: mirror'
    do
        grep -Fq "$required_text" "$parent_ci" ||
            fail "parent CI is missing: $required_text"
    done
fi
pass 'parent CI contains the guarded manual child-pipeline pilot'

printf '1..%d\n' "$pass_count"
