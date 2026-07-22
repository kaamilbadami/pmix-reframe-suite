#!/bin/bash
set -euo pipefail

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
checker="$script_dir/check_trusted_pmix_tests_pr.py"
test_dir=$(mktemp -d)
trap 'rm -rf -- "$test_dir"' EXIT

head_sha=0123456789abcdef0123456789abcdef01234567
changed_sha=89abcdef0123456789abcdef0123456789abcdef
pass_count=0

fail() {
    printf 'not ok - %s\n' "$1" >&2
    exit 1
}

pass() {
    printf 'ok - %s\n' "$1"
    pass_count=$((pass_count + 1))
}

assert_status() {
    local expected=$1
    local actual=$2
    local label=$3
    [[ $actual == "$expected" ]] ||
        fail "$label (expected status $expected, got $actual)"
}

write_fixture() {
    local path=$1
    local author=$2
    local state=$3
    local head_repository=$4
    local base_repository=$5
    local sha=${6:-$head_sha}
    local number=${7:-42}

    python3 - "$path" "$author" "$state" "$head_repository" \
        "$base_repository" "$sha" "$number" <<'PY'
import json
import pathlib
import sys

path, author, state, head_repository, base_repository, sha, number = sys.argv[1:]
document = {
    "id": 9001,
    "number": int(number),
    "state": state,
    "user": {"id": 101, "login": author},
    "head": {
        "sha": sha,
        "repo": {"id": 202, "full_name": head_repository},
    },
    "base": {
        "repo": {"id": 303, "full_name": base_repository},
    },
}
pathlib.Path(path).write_text(json.dumps(document) + "\n", encoding="utf-8")
PY
}

run_checker() {
    local fixture=$1
    local output=$2
    shift 2
    set +e
    python3 "$checker" --pr-json "$fixture" --pr-number 42 \
        --output "$output" "$@" > "$test_dir/stdout" 2> "$test_dir/stderr"
    checker_status=$?
    set -e
}

assert_no_output() {
    [[ ! -e $1 ]] || fail "$2 left an eligibility output"
}

fork_kaamil="$test_dir/fork-kaamil.json"
fork_kaamil_output="$test_dir/fork-kaamil.env"
write_fixture "$fork_kaamil" kaamilbadami open contributor/pmix-tests \
    kaamilbadami/pmix-tests
before_fixture="$test_dir/fork-kaamil.before"
cp -- "$fork_kaamil" "$before_fixture"
run_checker "$fork_kaamil" "$fork_kaamil_output"
assert_status 0 "$checker_status" 'kaamilbadami fork PR eligibility'
pass 'kaamilbadami fork PR is eligible'

expected_output="PR_ELIGIBLE=1
PR_NUMBER=42
PR_AUTHOR=kaamilbadami
PR_HEAD_SHA=$head_sha
PR_HEAD_REPOSITORY=contributor/pmix-tests
PR_BASE_REPOSITORY=kaamilbadami/pmix-tests
PR_FROM_FORK=1"
[[ $(< "$fork_kaamil_output") == "$expected_output" ]] ||
    fail 'eligible fork output fields, order, or values differ'
[[ $(tail -c 1 "$fork_kaamil_output" | od -An -tuC) =~ 10 ]] ||
    fail 'eligible output is not newline terminated'
[[ $(wc -l < "$fork_kaamil_output") == 7 ]] ||
    fail 'eligible output has the wrong field count'
pass 'eligible output has exact ordered fields and newline termination'

grep -Fxq 'PR_FROM_FORK=1' "$fork_kaamil_output" ||
    fail 'trusted fork was not marked as a fork'
pass 'trusted fork output sets PR_FROM_FORK=1'

fork_rhc="$test_dir/fork-rhc.json"
write_fixture "$fork_rhc" rhc54 open rhc54/pmix-tests kaamilbadami/pmix-tests
run_checker "$fork_rhc" "$test_dir/fork-rhc.env"
assert_status 0 "$checker_status" 'rhc54 fork PR eligibility'
pass 'rhc54 fork PR is eligible'

same_repo="$test_dir/same-repo.json"
same_repo_output="$test_dir/same-repo.env"
write_fixture "$same_repo" kaamilbadami open kaamilbadami/pmix-tests \
    kaamilbadami/pmix-tests
run_checker "$same_repo" "$same_repo_output"
assert_status 0 "$checker_status" 'same-repository PR eligibility'
grep -Fxq 'PR_FROM_FORK=0' "$same_repo_output" ||
    fail 'same-repository PR was marked as a fork'
pass 'trusted same-repository PR is eligible with PR_FROM_FORK=0'

unknown_fork="$test_dir/unknown-fork.json"
write_fixture "$unknown_fork" unknown-user open unknown/pmix-tests \
    kaamilbadami/pmix-tests
run_checker "$unknown_fork" "$test_dir/unknown-fork.env"
assert_status 3 "$checker_status" 'unknown fork author policy rejection'
assert_no_output "$test_dir/unknown-fork.env" 'unknown fork author rejection'
pass 'unknown fork author is policy-rejected'

unknown_same="$test_dir/unknown-same.json"
write_fixture "$unknown_same" unknown-user open kaamilbadami/pmix-tests \
    kaamilbadami/pmix-tests
run_checker "$unknown_same" "$test_dir/unknown-same.env"
assert_status 3 "$checker_status" 'unknown same-repository author policy rejection'
pass 'unknown same-repository author is policy-rejected'

closed="$test_dir/closed.json"
write_fixture "$closed" kaamilbadami closed contributor/pmix-tests \
    kaamilbadami/pmix-tests
run_checker "$closed" "$test_dir/closed.env"
assert_status 3 "$checker_status" 'closed PR policy rejection'
pass 'closed PR is policy-rejected'

wrong_base="$test_dir/wrong-base.json"
write_fixture "$wrong_base" kaamilbadami open contributor/pmix-tests \
    openpmix/pmix-tests
run_checker "$wrong_base" "$test_dir/wrong-base.env"
assert_status 3 "$checker_status" 'wrong base policy rejection'
pass 'wrong base repository is policy-rejected'

mutate_fixture() {
    local source=$1
    local destination=$2
    local statement=$3
    python3 - "$source" "$destination" "$statement" <<'PY'
import json
import pathlib
import sys

document = json.loads(pathlib.Path(sys.argv[1]).read_text())
exec(sys.argv[3], {"document": document})
pathlib.Path(sys.argv[2]).write_text(json.dumps(document) + "\n", encoding="utf-8")
PY
}

for invalid_case in \
    'missing head repository|document["head"].pop("repo")' \
    'missing author|document.pop("user")' \
    'missing head SHA|document["head"].pop("sha")'
do
    label=${invalid_case%%|*}
    statement=${invalid_case#*|}
    fixture="$test_dir/${label// /-}.json"
    output="$test_dir/${label// /-}.env"
    mutate_fixture "$fork_kaamil" "$fixture" "$statement"
    run_checker "$fixture" "$output"
    assert_status 4 "$checker_status" "$label invalidity"
    assert_no_output "$output" "$label invalidity"
done
pass 'missing head repository, author, and head SHA are invalid'

for invalid_sha in \
    ABCDEF0123456789ABCDEF0123456789ABCDEF01 \
    0123456789abcdef \
    master \
    " $head_sha" \
    "$head_sha " \
    0123456789abcdef0123456789abcdef0123456g
do
    fixture="$test_dir/sha-${pass_count}-${RANDOM}.json"
    output="$test_dir/sha-${pass_count}-${RANDOM}.env"
    write_fixture "$fixture" kaamilbadami open contributor/pmix-tests \
        kaamilbadami/pmix-tests "$invalid_sha"
    run_checker "$fixture" "$output"
    assert_status 4 "$checker_status" "invalid SHA $invalid_sha"
    assert_no_output "$output" "invalid SHA $invalid_sha"
done
pass 'uppercase, short, symbolic, padded, and nonhex head SHAs are invalid'

run_checker "$fork_kaamil" "$test_dir/number-mismatch.env" --pr-number 43
assert_status 4 "$checker_status" 'CLI and JSON PR number mismatch'
pass 'CLI and JSON PR number mismatch is invalid'

for invalid_number in 0 00 042 +42 -42 ' 42' '42 ' 4.2 true
do
    set +e
    python3 "$checker" --pr-json "$fork_kaamil" --pr-number "$invalid_number" \
        --output "$test_dir/bad-cli-number.env" >/dev/null 2>&1
    status=$?
    set -e
    assert_status 4 "$status" "noncanonical CLI PR number $invalid_number"
done
pass 'noncanonical CLI PR numbers are invalid'

duplicate="$test_dir/duplicate.json"
printf '%s\n' '{"id":1,"id":2,"number":42,"state":"open","user":{"id":3,"login":"kaamilbadami"},"head":{"sha":"0123456789abcdef0123456789abcdef01234567","repo":{"id":4,"full_name":"contributor/pmix-tests"}},"base":{"repo":{"id":5,"full_name":"kaamilbadami/pmix-tests"}}}' > "$duplicate"
run_checker "$duplicate" "$test_dir/duplicate.env"
assert_status 4 "$checker_status" 'duplicate JSON keys'
pass 'duplicate JSON object keys are invalid'

for id_case in \
    'boolean PR number|document["number"] = True' \
    'string PR number|document["number"] = "42"' \
    'boolean PR ID|document["id"] = False' \
    'string author ID|document["user"]["id"] = "101"' \
    'boolean head repository ID|document["head"]["repo"]["id"] = True' \
    'string base repository ID|document["base"]["repo"]["id"] = "303"'
do
    label=${id_case%%|*}
    statement=${id_case#*|}
    fixture="$test_dir/${label// /-}.json"
    mutate_fixture "$fork_kaamil" "$fixture" "$statement"
    run_checker "$fixture" "$test_dir/${label// /-}.env"
    assert_status 4 "$checker_status" "$label invalidity"
done
pass 'boolean and string values are invalid for every required numeric ID'

run_checker "$fork_kaamil" "$test_dir/expected-match.env" \
    --expected-head-sha "$head_sha"
assert_status 0 "$checker_status" 'matching expected head SHA'
pass 'matching expected head SHA succeeds'

run_checker "$fork_kaamil" "$test_dir/expected-changed.env" \
    --expected-head-sha "$changed_sha"
assert_status 5 "$checker_status" 'changed expected head SHA'
assert_no_output "$test_dir/expected-changed.env" 'changed expected head SHA'
pass 'changed expected head SHA receives dedicated status 5'

for invalid_expected in \
    ABCDEF0123456789ABCDEF0123456789ABCDEF01 master " $head_sha" "$head_sha "
do
    run_checker "$fork_kaamil" "$test_dir/invalid-expected.env" \
        --expected-head-sha "$invalid_expected"
    assert_status 4 "$checker_status" "invalid expected SHA $invalid_expected"
done
pass 'malformed expected head SHAs are invalid configuration'

stale_output="$test_dir/stale.env"
for stale_case in rejected invalid changed
do
    cp -- "$fork_kaamil_output" "$stale_output"
    case "$stale_case" in
        rejected)
            run_checker "$unknown_fork" "$stale_output"
            expected_status=3
            ;;
        invalid)
            run_checker "$duplicate" "$stale_output"
            expected_status=4
            ;;
        changed)
            run_checker "$fork_kaamil" "$stale_output" \
                --expected-head-sha "$changed_sha"
            expected_status=5
            ;;
    esac
    assert_status "$expected_status" "$checker_status" "$stale_case stale cleanup"
    assert_no_output "$stale_output" "$stale_case stale cleanup"
done
pass 'rejected, invalid, and changed-SHA runs remove stale eligible output'

input_target="$test_dir/input-target.json"
input_link="$test_dir/input-link.json"
output_for_input_link="$test_dir/input-link.env"
cp -- "$fork_kaamil" "$input_target"
ln -s -- "$input_target" "$input_link"
run_checker "$input_link" "$output_for_input_link"
assert_status 4 "$checker_status" 'input symlink rejection'
assert_no_output "$output_for_input_link" 'input symlink rejection'

output_target="$test_dir/output-target.env"
output_link="$test_dir/output-link.env"
printf 'target must remain unchanged\n' > "$output_target"
cp -- "$output_target" "$test_dir/output-target.before"
ln -s -- "$output_target" "$output_link"
run_checker "$fork_kaamil" "$output_link"
assert_status 4 "$checker_status" 'output symlink rejection'
cmp -s -- "$output_target" "$test_dir/output-target.before" ||
    fail 'output symlink target was changed'
[[ -L $output_link ]] || fail 'output symlink itself was followed or removed'
pass 'input and output symlinks are rejected without following targets'

collision="$test_dir/collision.json"
cp -- "$fork_kaamil" "$collision"
cp -- "$collision" "$test_dir/collision.before"
set +e
python3 "$checker" --pr-json "$collision" --pr-number 42 \
    --output "$collision" >/dev/null 2>&1
status=$?
set -e
assert_status 4 "$status" 'input/output path collision'
cmp -s -- "$collision" "$test_dir/collision.before" ||
    fail 'path collision changed the input'

hardlink="$test_dir/collision-hardlink.env"
ln -- "$collision" "$hardlink"
set +e
python3 "$checker" --pr-json "$collision" --pr-number 42 \
    --output "$hardlink" >/dev/null 2>&1
status=$?
set -e
assert_status 4 "$status" 'input/output hard-link collision'
cmp -s -- "$collision" "$test_dir/collision.before" ||
    fail 'hard-link collision changed the input'
pass 'input/output path and hard-link collisions are rejected safely'

python3 - "$checker" "$fork_kaamil" "$test_dir/replace-failure.env" <<'PY'
import importlib.util
import pathlib
import sys

checker_path = pathlib.Path(sys.argv[1])
fixture = pathlib.Path(sys.argv[2])
output = pathlib.Path(sys.argv[3])
output.write_text("PR_ELIGIBLE=1\nSTALE=1\n")

spec = importlib.util.spec_from_file_location("trusted_checker", checker_path)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
real_replace = module.os.replace

def fail_replace(source, destination):
    raise OSError("injected replacement failure")

module.os.replace = fail_replace
try:
    status = module.check([
        "--pr-json", str(fixture),
        "--pr-number", "42",
        "--output", str(output),
    ])
finally:
    module.os.replace = real_replace

assert status == module.EXIT_INVALID
assert not output.exists()
assert not list(output.parent.glob(f".{output.name}.tmp.*"))
PY
pass 'atomic replacement failure leaves no temporary or eligible output'

cmp -s -- "$fork_kaamil" "$before_fixture" ||
    fail 'input JSON changed during validation'
pass 'input JSON remains byte-for-byte unchanged'

state_case="$test_dir/state-case"
mkdir -p -- "$state_case/.ci-state"
printf 'preserve state\n' > "$state_case/.ci-state/sentinel"
cp -- "$fork_kaamil" "$state_case/.ci-state/pr.json"
cp -a -- "$state_case/.ci-state" "$test_dir/state-before"

set +e
python3 "$checker" --pr-json "$fork_kaamil" --pr-number 42 \
    --output "$state_case/.ci-state/result.env" >/dev/null 2>&1
status=$?
set -e
assert_status 4 "$status" 'state-component output path rejection'
[[ ! -e $state_case/.ci-state/result.env ]] ||
    fail 'checker created an eligibility output inside state'

set +e
python3 "$checker" --pr-json "$state_case/.ci-state/pr.json" --pr-number 42 \
    --output "$state_case/rejected-input.env" >/dev/null 2>&1
status=$?
set -e
assert_status 4 "$status" 'state-component input path rejection'
[[ ! -e $state_case/rejected-input.env ]] ||
    fail 'checker created output for a state-component input'

run_checker "$fork_kaamil" "$state_case/result.env"
assert_status 0 "$checker_status" 'normal eligibility outside state'
cmp -s -- "$test_dir/state-before/sentinel" \
    "$state_case/.ci-state/sentinel" || fail 'checker changed the state sentinel'
diff -r -- "$test_dir/state-before" "$state_case/.ci-state" >/dev/null ||
    fail 'checker modified existing state'
if find "$state_case/.ci-state" -maxdepth 1 -type f \
    -name '.*.tmp.*' -print -quit | grep -q .; then
    fail 'checker left a temporary eligibility file inside state'
fi
pass 'state-component paths are rejected and normal runs leave state unchanged'

python3 - "$checker" <<'PY'
import ast
import pathlib
import sys

source = pathlib.Path(sys.argv[1]).read_text(encoding="utf-8")
tree = ast.parse(source)
imports = set()
attributes = set()
names = set()
for node in ast.walk(tree):
    if isinstance(node, ast.Import):
        imports.update(alias.name.split(".")[0] for alias in node.names)
    elif isinstance(node, ast.ImportFrom):
        imports.add((node.module or "").split(".")[0])
    elif isinstance(node, ast.Attribute):
        attributes.add(node.attr)
    elif isinstance(node, ast.Name):
        names.add(node.id)

allowed_imports = {
    "argparse", "json", "os", "pathlib", "re", "stat", "tempfile", "typing"
}
assert imports <= allowed_imports, imports - allowed_imports
for forbidden in {
    "system", "popen", "spawn", "fork", "execv", "execve", "run", "Popen",
    "urlopen", "Request", "build_opener", "socket", "connect",
}:
    assert forbidden not in attributes
    assert forbidden not in names
for forbidden_text in (
    "GITHUB_TOKEN", "GITHUB_STATUS_TOKEN", "CI_JOB_TOKEN", "sbatch", "srun",
    "reframe", "git checkout", "git clone", "curl ",
):
    assert forbidden_text not in source
PY
pass 'production code has no network, command, build, scheduler, status, or checkout capability'

invalid_utf8="$test_dir/invalid-utf8.json"
printf '\377' > "$invalid_utf8"
run_checker "$invalid_utf8" "$test_dir/invalid-utf8.env"
assert_status 4 "$checker_status" 'invalid UTF-8'

invalid_json="$test_dir/invalid-json.json"
printf '{invalid}\n' > "$invalid_json"
run_checker "$invalid_json" "$test_dir/invalid-json.env"
assert_status 4 "$checker_status" 'invalid JSON'

array_json="$test_dir/array.json"
printf '[]\n' > "$array_json"
run_checker "$array_json" "$test_dir/array.env"
assert_status 4 "$checker_status" 'non-object top-level JSON'

nan_json="$test_dir/nan.json"
sed 's/"id": 9001/"id": NaN/' "$fork_kaamil" > "$nan_json"
run_checker "$nan_json" "$test_dir/nan.env"
assert_status 4 "$checker_status" 'nonstandard JSON number'
pass 'invalid UTF-8, JSON, top-level types, and nonstandard numbers are invalid'

for metadata_case in \
    'wrong state type|document["state"] = True' \
    'wrong user type|document["user"] = []' \
    'empty author|document["user"]["login"] = ""' \
    'malformed author|document["user"]["login"] = "bad user"' \
    'wrong head type|document["head"] = []' \
    'malformed head repository|document["head"]["repo"]["full_name"] = "bad repo"' \
    'missing base repository|document["base"].pop("repo")' \
    'malformed base repository|document["base"]["repo"]["full_name"] = "/pmix-tests"'
do
    label=${metadata_case%%|*}
    statement=${metadata_case#*|}
    fixture="$test_dir/type-${pass_count}-${RANDOM}.json"
    mutate_fixture "$fork_kaamil" "$fixture" "$statement"
    run_checker "$fixture" "$test_dir/type-${pass_count}-${RANDOM}.env"
    assert_status 4 "$checker_status" "$label invalidity"
done
pass 'missing, malformed, and unexpectedly typed required metadata fails closed'

unsafe_dir="$test_dir/unsafe-parent"
run_checker "$fork_kaamil" "$unsafe_dir/result.env"
assert_status 4 "$checker_status" 'missing output parent'
[[ ! -e $unsafe_dir ]] || fail 'checker created a missing output parent'
pass 'unsafe output paths are rejected without creating parents'

printf '1..%d\n' "$pass_count"
