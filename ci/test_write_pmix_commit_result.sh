#!/bin/bash
set -euo pipefail

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
writer="$script_dir/write_pmix_commit_result.sh"
test_dir=$(mktemp -d)
trap 'rm -rf -- "$test_dir"' EXIT

lower_pmix_sha=0123456789abcdef0123456789abcdef01234567
upper_pmix_sha=89ABCDEF0123456789ABCDEF0123456789ABCDEF
lower_suite_sha=fedcba9876543210fedcba9876543210fedcba98
upper_suite_sha=FEDCBA9876543210FEDCBA9876543210FEDCBA98
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
    if "$@" > "$test_dir/failure.out" 2>&1; then
        fail "$label was accepted"
    fi
    pass "$label"
}

valid_environment=(
    env -i
    "PATH=$PATH"
    "PMIX_COMMIT=$lower_pmix_sha"
    "CI_COMMIT_SHA=$lower_suite_sha"
    CI_JOB_ID=123
    CI_PIPELINE_ID=456
)

expect_failure 'missing output-directory argument is rejected' \
    "${valid_environment[@]}" bash "$writer"
expect_failure 'too many arguments are rejected' \
    "${valid_environment[@]}" bash "$writer" one two

expect_failure 'missing PMIX_COMMIT is rejected' \
    env -i "PATH=$PATH" "CI_COMMIT_SHA=$lower_suite_sha" CI_JOB_ID=123 \
        CI_PIPELINE_ID=456 bash "$writer" "$test_dir/missing-pmix"
expect_failure 'missing CI_COMMIT_SHA is rejected' \
    env -i "PATH=$PATH" "PMIX_COMMIT=$lower_pmix_sha" CI_JOB_ID=123 \
        CI_PIPELINE_ID=456 bash "$writer" "$test_dir/missing-suite"
expect_failure 'missing CI_JOB_ID is rejected' \
    env -i "PATH=$PATH" "PMIX_COMMIT=$lower_pmix_sha" \
        "CI_COMMIT_SHA=$lower_suite_sha" CI_PIPELINE_ID=456 \
        bash "$writer" "$test_dir/missing-job"
expect_failure 'missing CI_PIPELINE_ID is rejected' \
    env -i "PATH=$PATH" "PMIX_COMMIT=$lower_pmix_sha" \
        "CI_COMMIT_SHA=$lower_suite_sha" CI_JOB_ID=123 \
        bash "$writer" "$test_dir/missing-pipeline"

for invalid_id in 0 01 -1 abc '1 2'; do
    expect_failure "malformed CI_JOB_ID is rejected: $invalid_id" \
        env -i "PATH=$PATH" "PMIX_COMMIT=$lower_pmix_sha" \
            "CI_COMMIT_SHA=$lower_suite_sha" "CI_JOB_ID=$invalid_id" \
            CI_PIPELINE_ID=456 bash "$writer" "$test_dir/invalid-job"
    expect_failure "malformed CI_PIPELINE_ID is rejected: $invalid_id" \
        env -i "PATH=$PATH" "PMIX_COMMIT=$lower_pmix_sha" \
            "CI_COMMIT_SHA=$lower_suite_sha" CI_JOB_ID=123 \
            "CI_PIPELINE_ID=$invalid_id" bash "$writer" \
            "$test_dir/invalid-pipeline"
done

for sha_variable in PMIX_COMMIT CI_COMMIT_SHA; do
    other_sha_assignment="CI_COMMIT_SHA=$lower_suite_sha"
    if [[ $sha_variable == CI_COMMIT_SHA ]]; then
        other_sha_assignment="PMIX_COMMIT=$lower_pmix_sha"
    fi
    for invalid_sha in \
        0123456789abcdef \
        " $lower_pmix_sha" \
        main \
        0123456789abcdef0123456789abcdef0123456g
    do
        expect_failure "$sha_variable rejects invalid SHA: $invalid_sha" \
            env -i "PATH=$PATH" "$other_sha_assignment" \
                "$sha_variable=$invalid_sha" CI_JOB_ID=123 CI_PIPELINE_ID=456 \
                bash "$writer" "$test_dir/invalid-sha"
    done
done
pass 'short, whitespace, symbolic, and non-hex SHA values are rejected'

lower_output="$test_dir/lowercase/result"
"${valid_environment[@]}" CI_JOB_STATUS=success bash "$writer" "$lower_output"
[[ -d $lower_output ]] || fail 'missing output directory was not created'
pass 'missing output directory is created'
lower_result="$lower_output/$lower_pmix_sha.env"
[[ -f $lower_result ]] || fail 'lowercase SHA result filename is missing'
pass 'valid lowercase SHA and lowercase filename are accepted'

expected_lower="$test_dir/expected-lower.env"
printf 'PMIX_COMMIT=%s\nSUITE_COMMIT=%s\nCI_JOB_STATUS=success\nCI_JOB_ID=123\nCI_PIPELINE_ID=456\n' \
    "$lower_pmix_sha" "$lower_suite_sha" > "$expected_lower"
cmp -s -- "$expected_lower" "$lower_result" ||
    fail 'lowercase record does not match the exact schema'
pass 'success is preserved and the exact five-line field order is written'
[[ $(wc -l < "$lower_result") == 5 ]] || fail 'result does not have five lines'
pass 'result contains exactly five lines'

upper_output="$test_dir/uppercase"
env -i "PATH=$PATH" "PMIX_COMMIT=$upper_pmix_sha" \
    "CI_COMMIT_SHA=$upper_suite_sha" CI_JOB_STATUS=failed CI_JOB_ID=789 \
    CI_PIPELINE_ID=987 bash "$writer" "$upper_output"
upper_result="$upper_output/${upper_pmix_sha,,}.env"
[[ -f $upper_result ]] || fail 'uppercase SHA did not produce a lowercase filename'
grep -Fqx "PMIX_COMMIT=${upper_pmix_sha,,}" "$upper_result" ||
    fail 'uppercase PMIX_COMMIT was not normalized'
grep -Fqx "SUITE_COMMIT=${upper_suite_sha,,}" "$upper_result" ||
    fail 'uppercase CI_COMMIT_SHA was not normalized'
grep -Fqx 'CI_JOB_STATUS=failed' "$upper_result" ||
    fail 'failed status was not preserved'
pass 'valid uppercase SHAs produce lowercase filename and record values'
pass 'failed is preserved'

canceled_output="$test_dir/canceled"
"${valid_environment[@]}" CI_JOB_STATUS=canceled bash "$writer" "$canceled_output"
grep -Fqx 'CI_JOB_STATUS=canceled' "$canceled_output/$lower_pmix_sha.env" ||
    fail 'canceled status was not preserved'
pass 'canceled is preserved'

missing_status_output="$test_dir/missing-status"
"${valid_environment[@]}" bash "$writer" "$missing_status_output"
grep -Fqx 'CI_JOB_STATUS=unknown' \
    "$missing_status_output/$lower_pmix_sha.env" ||
    fail 'missing status was not normalized'
pass 'missing status is normalized to unknown'

empty_status_output="$test_dir/empty-status"
"${valid_environment[@]}" CI_JOB_STATUS= bash "$writer" "$empty_status_output"
grep -Fqx 'CI_JOB_STATUS=unknown' "$empty_status_output/$lower_pmix_sha.env" ||
    fail 'empty status was not normalized'
pass 'empty status is normalized to unknown'

unexpected_status_output="$test_dir/unexpected-status"
"${valid_environment[@]}" CI_JOB_STATUS=running \
    bash "$writer" "$unexpected_status_output"
grep -Fqx 'CI_JOB_STATUS=unknown' \
    "$unexpected_status_output/$lower_pmix_sha.env" ||
    fail 'unexpected status was not normalized'
pass 'unexpected status is normalized to unknown'

non_directory="$test_dir/not-a-directory"
printf 'sentinel\n' > "$non_directory"
expect_failure 'existing non-directory output path is rejected' \
    "${valid_environment[@]}" bash "$writer" "$non_directory"
grep -Fqx sentinel "$non_directory" || fail 'non-directory output was changed'

replacement_dir="$test_dir/replacement"
mkdir -p -- "$replacement_dir"
replacement_result="$replacement_dir/$lower_pmix_sha.env"
printf 'old result\n' > "$replacement_result"
"${valid_environment[@]}" CI_JOB_STATUS=success \
    bash "$writer" "$replacement_dir"
cmp -s -- "$expected_lower" "$replacement_result" ||
    fail 'existing result was not replaced'
grep -Fq 'mv -f -- "$temporary_file" "$output_file"' "$writer" ||
    fail 'writer does not publish with the required atomic rename'
pass 'existing result is atomically replaced'

preserved_dir="$test_dir/preserved"
mkdir -p -- "$preserved_dir"
preserved_result="$preserved_dir/$lower_pmix_sha.env"
printf 'preserve existing result\n' > "$preserved_result"
cp -- "$preserved_result" "$test_dir/preserved-before"
expect_failure 'invalid input fails with an existing result' \
    env -i "PATH=$PATH" PMIX_COMMIT=main \
        "CI_COMMIT_SHA=$lower_suite_sha" CI_JOB_ID=123 CI_PIPELINE_ID=456 \
        bash "$writer" "$preserved_dir"
cmp -s -- "$test_dir/preserved-before" "$preserved_result" ||
    fail 'invalid input changed an existing result'
pass 'invalid input leaves an existing result unchanged'

if find "$replacement_dir" -maxdepth 1 -name '.*.env.tmp.*' -print -quit |
    grep -q .; then
    fail 'temporary file remains after success'
fi
pass 'no temporary file remains after success'

failing_bin="$test_dir/failing-bin"
mkdir -p -- "$failing_bin"
printf '#!/bin/bash\nexit 1\n' > "$failing_bin/mv"
chmod +x -- "$failing_bin/mv"
cp -- "$preserved_result" "$test_dir/write-failure-before"
expect_failure 'failed publish is reported' \
    env -i "PATH=$failing_bin:$PATH" "PMIX_COMMIT=$lower_pmix_sha" \
        "CI_COMMIT_SHA=$lower_suite_sha" CI_JOB_ID=123 CI_PIPELINE_ID=456 \
        bash "$writer" "$preserved_dir"
cmp -s -- "$test_dir/write-failure-before" "$preserved_result" ||
    fail 'failed publish changed an existing result'
if find "$preserved_dir" -maxdepth 1 -name '.*.env.tmp.*' -print -quit |
    grep -q .; then
    fail 'temporary file remains after failure'
fi
pass 'failed writing leaves the result unchanged and no temporary file'

state_repo="$test_dir/state-repo"
mkdir -p -- "$state_repo/.ci-state"
printf 'preserve state\n' > "$state_repo/.ci-state/sentinel"
cp -a -- "$state_repo/.ci-state" "$test_dir/state-before"
(
    cd -- "$state_repo"
    "${valid_environment[@]}" bash "$writer" results
)
diff -r -- "$test_dir/state-before" "$state_repo/.ci-state" >/dev/null ||
    fail 'writer modified existing .ci-state'
if grep -Fq '.ci-state' "$writer"; then
    fail 'writer contains a .ci-state reference'
fi
pass 'writer does not read, create, or modify .ci-state'

state_absent_repo="$test_dir/state-absent-repo"
mkdir -p -- "$state_absent_repo"
(
    cd -- "$state_absent_repo"
    "${valid_environment[@]}" bash "$writer" results
)
[[ ! -e $state_absent_repo/.ci-state ]] || fail 'writer created .ci-state'
pass 'writer leaves absent .ci-state absent'

printf '1..%d\n' "$pass_count"
