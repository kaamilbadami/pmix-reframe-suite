#!/bin/bash
set -euo pipefail

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
test_dir=$(mktemp -d)
trap 'rm -rf -- "$test_dir"' EXIT

mock_repo="$test_dir/repo"
mock_ci="$mock_repo/ci"
runner="$mock_ci/run_exact_pmix_commit.sh"
capture_file="$test_dir/received-pmix-commit"
mkdir -p -- "$mock_ci"
cp -- "$script_dir/run_exact_pmix_commit.sh" "$runner"

cat > "$mock_ci/run_pmix_python_suite.sh" <<'MOCK_SUITE'
#!/bin/bash
set -euo pipefail

printf '%s' "${PMIX_COMMIT:?}" > "${MOCK_CAPTURE_FILE:?}"
MOCK_SUITE
chmod +x -- "$mock_ci/run_pmix_python_suite.sh"

pass_count=0

fail() {
    printf 'not ok - %s\n' "$1" >&2
    exit 1
}

pass() {
    printf 'ok - %s\n' "$1"
    pass_count=$((pass_count + 1))
}

reject_unset() {
    local label=$1
    local output_file="$test_dir/rejected-${pass_count}.out"

    rm -f -- "$capture_file"
    if (cd "$mock_repo" &&
        env -u PMIX_COMMIT MOCK_CAPTURE_FILE="$capture_file" \
            bash "$runner" > "$output_file" 2>&1); then
        fail "$label was accepted"
    fi
    [[ ! -e $capture_file ]] || fail "$label invoked the suite entry point"
    pass "$label"
}

reject_value() {
    local label=$1
    local value=$2
    local output_file="$test_dir/rejected-${pass_count}.out"

    rm -f -- "$capture_file"
    if (cd "$mock_repo" &&
        PMIX_COMMIT="$value" MOCK_CAPTURE_FILE="$capture_file" \
            bash "$runner" > "$output_file" 2>&1); then
        fail "$label was accepted"
    fi
    [[ ! -e $capture_file ]] || fail "$label invoked the suite entry point"
    pass "$label"
}

accept_value() {
    local label=$1
    local value=$2
    local output_file="$test_dir/accepted-${pass_count}.out"

    rm -f -- "$capture_file"
    (cd "$mock_repo" &&
        PMIX_COMMIT="$value" MOCK_CAPTURE_FILE="$capture_file" \
            bash "$runner" > "$output_file" 2>&1) || fail "$label was rejected"
    [[ -f $capture_file ]] || fail "$label did not invoke the suite entry point"
    [[ $(< "$capture_file") == "$value" ]] ||
        fail "$label changed PMIX_COMMIT before invoking the suite entry point"
    grep -Fqx "Selected exact OpenPMIx commit: $value" "$output_file" ||
        fail "$label did not print the selected exact SHA"
    pass "$label"
}

lower_sha=0123456789abcdef0123456789abcdef01234567
upper_sha=0123456789ABCDEF0123456789ABCDEF01234567
nonhex_sha=0123456789abcdef0123456789abcdef0123456g

[[ ${#nonhex_sha} == 40 ]] || fail 'test setup produced the wrong non-hex SHA length'

reject_unset 'missing PMIX_COMMIT is rejected'
reject_value 'empty PMIX_COMMIT is rejected' ''
reject_value 'short SHA is rejected' 0123456789abcdef
reject_value '40-character non-hex value is rejected' "$nonhex_sha"
reject_value 'PMIX_COMMIT containing whitespace is rejected' " $lower_sha"
reject_value 'branch name is rejected' master
reject_value 'tag name is rejected' v5.0.0

rm -rf -- "$mock_repo/.ci-state"
accept_value 'valid lowercase SHA is passed unchanged' "$lower_sha"
[[ ! -e $mock_repo/.ci-state ]] ||
    fail 'runner created .ci-state when it was absent'
pass 'runner does not create .ci-state'

mkdir -p -- "$mock_repo/.ci-state"
printf 'preserve this state\n' > "$mock_repo/.ci-state/sentinel"
cp -a -- "$mock_repo/.ci-state" "$test_dir/state-before"
accept_value 'valid uppercase SHA is passed unchanged' "$upper_sha"
diff -r -- "$test_dir/state-before" "$mock_repo/.ci-state" >/dev/null ||
    fail 'runner modified existing .ci-state'
pass 'runner does not modify .ci-state'

printf '1..%d\n' "$pass_count"
