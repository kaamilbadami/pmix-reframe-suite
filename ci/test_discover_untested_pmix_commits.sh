#!/bin/bash
set -euo pipefail

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
discovery_script="$script_dir/discover_untested_pmix_commits.sh"
test_dir=$(mktemp -d)
trap 'rm -rf -- "$test_dir"' EXIT

upstream_repo="$test_dir/upstream"
case_root="$test_dir/cases"
mkdir -p -- "$case_root"
git init -q --initial-branch=master "$upstream_repo"
git -C "$upstream_repo" config user.name 'PMIx discovery test'
git -C "$upstream_repo" config user.email 'pmix-discovery-test@example.invalid'

git -C "$upstream_repo" commit -q --allow-empty -m 'commit A'
commit_a=$(git -C "$upstream_repo" rev-parse HEAD)
git -C "$upstream_repo" commit -q --allow-empty -m 'commit B'
commit_b=$(git -C "$upstream_repo" rev-parse HEAD)
git -C "$upstream_repo" commit -q --allow-empty -m 'commit C'
commit_c=$(git -C "$upstream_repo" rev-parse HEAD)

git -C "$upstream_repo" switch -q -c side "$commit_a"
git -C "$upstream_repo" commit -q --allow-empty -m 'side commit'
side_commit=$(git -C "$upstream_repo" rev-parse HEAD)
git -C "$upstream_repo" switch -q master

suite_sha=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
absent_sha=ffffffffffffffffffffffffffffffffffffffff
pass_count=0

fail() {
    printf 'not ok - %s\n' "$1" >&2
    exit 1
}

pass() {
    printf 'ok - %s\n' "$1"
    pass_count=$((pass_count + 1))
}

write_state() {
    local path=$1
    local pmix_sha=$2

    printf 'PMIX_COMMIT=%s\nSUITE_COMMIT=%s\nLAST_SUCCESS_EPOCH=123456\n' \
        "$pmix_sha" "$suite_sha" > "$path"
}

expect_failure() {
    local label=$1
    local state_file=$2
    local output_file=$3
    local stderr_file="$case_root/failure-${pass_count}.err"

    if (cd "$case_root" &&
        bash "$discovery_script" "$state_file" "$output_file" \
            "$upstream_repo" > /dev/null 2> "$stderr_file"); then
        fail "$label was accepted"
    fi
    pass "$label"
}

missing_state="$case_root/missing-state.env"
expect_failure 'missing state file is rejected' \
    "$missing_state" "$case_root/missing-state.out"

missing_field_state="$case_root/missing-field.env"
printf 'SUITE_COMMIT=%s\nLAST_SUCCESS_EPOCH=123456\n' \
    "$suite_sha" > "$missing_field_state"
expect_failure 'missing PMIX_COMMIT field is rejected' \
    "$missing_field_state" "$case_root/missing-field.out"

empty_state="$case_root/empty.env"
write_state "$empty_state" ''
expect_failure 'empty PMIX_COMMIT is rejected' \
    "$empty_state" "$case_root/empty.out"

short_state="$case_root/short.env"
write_state "$short_state" 0123456789abcdef
expect_failure 'short PMIX_COMMIT is rejected' \
    "$short_state" "$case_root/short.out"

nonhex_state="$case_root/nonhex.env"
write_state "$nonhex_state" 0123456789abcdef0123456789abcdef0123456g
expect_failure 'non-hexadecimal PMIX_COMMIT is rejected' \
    "$nonhex_state" "$case_root/nonhex.out"

no_new_state="$case_root/no-new.env"
no_new_output="$case_root/no-new.out"
no_new_stderr="$case_root/no-new.err"
write_state "$no_new_state" "$commit_c"
bash "$discovery_script" "$no_new_state" "$no_new_output" \
    "$upstream_repo" > "$case_root/no-new.stdout" 2> "$no_new_stderr"
[[ ! -s $no_new_output ]] || fail 'no-new case produced commit output'
grep -Fqx 'untested commits: 0' "$no_new_stderr" ||
    fail 'no-new case reported the wrong count'
pass 'no new commits produces an empty output file'

one_new_state="$case_root/one-new.env"
one_new_output="$case_root/one-new.out"
write_state "$one_new_state" "$commit_b"
bash "$discovery_script" "$one_new_state" "$one_new_output" \
    "$upstream_repo" > "$case_root/one-new.stdout" \
    2> "$case_root/one-new.err"
mapfile -t one_new_commits < "$one_new_output"
(( ${#one_new_commits[@]} == 1 )) || fail 'one-new case returned the wrong count'
[[ ${one_new_commits[0]} == "$commit_c" ]] ||
    fail 'one-new case returned the wrong commit'
[[ ${#one_new_commits[0]} == 40 ]] ||
    fail 'one-new case did not return a full SHA'
pass 'one new commit produces exactly one full SHA'

multiple_state="$case_root/multiple.env"
multiple_output="$case_root/multiple.out"
multiple_stderr="$case_root/multiple.err"
write_state "$multiple_state" "$commit_a"
cp -- "$multiple_state" "$case_root/multiple-state-before"
bash "$discovery_script" "$multiple_state" "$multiple_output" \
    "$upstream_repo" > "$case_root/multiple.stdout" 2> "$multiple_stderr"
mapfile -t multiple_commits < "$multiple_output"
(( ${#multiple_commits[@]} == 2 )) ||
    fail 'multiple-new case returned the wrong count'
pass 'multiple new commits produces every SHA'
[[ ${multiple_commits[0]} == "$commit_b" &&
   ${multiple_commits[1]} == "$commit_c" ]] ||
    fail 'multiple-new commits are not oldest to newest'
pass 'multiple commits are ordered oldest to newest'
[[ ${multiple_commits[1]} == "$commit_c" ]] ||
    fail 'current master tip is missing from output'
pass 'current master tip is included'
if printf '%s\n' "${multiple_commits[@]}" | grep -Fxq "$commit_a"; then
    fail 'last-known-good commit was included in output'
fi
pass 'last-known-good commit is excluded'
grep -Fqx "last-known-good: $commit_a" "$multiple_stderr" ||
    fail 'diagnostics omit the last-known-good SHA'
grep -Fqx "current master: $commit_c" "$multiple_stderr" ||
    fail 'diagnostics omit the current master SHA'
grep -Fqx 'untested commits: 2' "$multiple_stderr" ||
    fail 'diagnostics report the wrong commit count'
[[ ! -s $case_root/multiple.stdout ]] ||
    fail 'diagnostics or commits leaked to stdout'
pass 'diagnostics remain separate from machine-readable output'

side_state="$case_root/side.env"
write_state "$side_state" "$side_commit"
expect_failure 'existing commit outside master ancestry is rejected' \
    "$side_state" "$case_root/side.out"
grep -Fq 'is not an ancestor of master' \
    "$case_root/failure-$((pass_count - 1)).err" ||
    fail 'non-ancestor failure did not explain the history mismatch'

absent_state="$case_root/absent.env"
write_state "$absent_state" "$absent_sha"
expect_failure 'commit absent from fetched history is rejected' \
    "$absent_state" "$case_root/absent.out"
grep -Fq 'is absent from fetched history' \
    "$case_root/failure-$((pass_count - 1)).err" ||
    fail 'absent-commit failure did not explain the missing history'

cmp -s -- "$case_root/multiple-state-before" "$multiple_state" ||
    fail 'original state file changed during discovery'
pass 'original state file remains byte-for-byte unchanged'

same_file_state="$case_root/same-file.env"
write_state "$same_file_state" "$commit_a"
cp -- "$same_file_state" "$case_root/same-file-before"
expect_failure 'state file cannot also be the output file' \
    "$same_file_state" "$same_file_state"
cmp -s -- "$case_root/same-file-before" "$same_file_state" ||
    fail 'state/output collision modified the state file'

directory_output_state="$case_root/directory-output.env"
directory_output="$case_root/existing-output-directory"
write_state "$directory_output_state" "$commit_a"
mkdir -p -- "$directory_output"
printf 'preserve directory contents\n' > "$directory_output/sentinel"
cp -a -- "$directory_output" "$case_root/directory-output-before"
expect_failure 'existing directory passed as output file is rejected' \
    "$directory_output_state" "$directory_output"
diff -r -- "$case_root/directory-output-before" "$directory_output" \
    >/dev/null || fail 'rejected output directory was modified'

unwritable_state="$case_root/unwritable-output.env"
write_state "$unwritable_state" "$commit_a"
expect_failure 'missing output directory is rejected' \
    "$unwritable_state" "$case_root/not-a-directory/commits.out"

[[ ! -e $case_root/.ci-state ]] ||
    fail 'test created .ci-state in its isolated working directory'
if grep -Fq '.ci-state' "$discovery_script"; then
    fail 'discovery helper contains a hidden .ci-state path'
fi
pass 'real .ci-state is neither referenced nor created'

printf '1..%d\n' "$pass_count"
