#!/bin/bash
set -euo pipefail

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
generator="$script_dir/generate_pmix_child_pipeline.py"
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
    '\.ci-state|should_run_pmix_suite|discover_untested|github|openpmix\.git|ls-remote|git fetch' \
    "$multiple_output"; then
    fail 'generated jobs contain forbidden discovery or state behavior'
fi
pass 'generated jobs contain no discovery, gate, remote, or state references'

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

for filename in sys.argv[1:]:
    yaml.safe_load(pathlib.Path(filename).read_text())
PY
else
    printf '# YAML parser unavailable; parse validation skipped\n'
fi
pass 'generated YAML parses when a parser is available'

printf '1..%d\n' "$pass_count"
