#!/bin/bash
set -euo pipefail

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
collector_script="$script_dir/collect_ci_artifacts.sh"
test_dir=$(mktemp -d)
trap 'rm -rf -- "$test_dir"' EXIT

fail() {
    printf 'not ok - %s\n' "$1" >&2
    exit 1
}

skip_dir="$test_dir/skip"
mkdir -p -- "$skip_dir/.ci-state"
printf 'PMIX_RUN_SUITE=0\n' > "$skip_dir/.ci-state/pmix-decision.env"
(cd -- "$skip_dir" && "$collector_script") || fail 'skip collection failed'
[[ -f $skip_dir/ci-artifacts/artifact-summary.txt ]] || fail 'skip summary missing'
grep -Fq 'Pipeline execution: intentional skip' \
    "$skip_dir/ci-artifacts/artifact-summary.txt" || fail 'skip summary is incorrect'

full_dir="$test_dir/full"
build_dir="$full_dir/stage/frontier/batch/pmix_test/build_pmix_test"
fetch_dir="$full_dir/stage/frontier/batch/pmix_test/fetch_pmix_test"
mkdir -p -- "$full_dir/.ci-state" "$full_dir/output" "$build_dir/pmix-git" "$fetch_dir"
printf 'PMIX_RUN_SUITE=1\n' > "$full_dir/.ci-state/pmix-decision.env"
printf 'output\n' > "$full_dir/output/result.txt"
printf 'build output\n' > "$build_dir/rfm_build.out"
printf 'config log\n' > "$build_dir/pmix-git/config.log"
ln -s -- missing-site-packages "$build_dir/python-site-packages"
printf 'PMIX_COMMIT=test\n' > "$fetch_dir/pmix-commit.env"

(cd -- "$full_dir" && "$collector_script") || fail 'full collection failed'
artifacts="$full_dir/ci-artifacts"
grep -Fq 'Pipeline execution: full run' "$artifacts/artifact-summary.txt" || \
    fail 'full-run summary is incorrect'
[[ -f $artifacts/output/result.txt ]] || fail 'output was not copied'
[[ -f $artifacts/stage/frontier/batch/pmix_test/build_pmix_test/rfm_build.out ]] || \
    fail 'build output was not copied'
[[ -f $artifacts/stage/frontier/batch/pmix_test/build_pmix_test/pmix-git/config.log ]] || \
    fail 'config.log was not copied'
[[ -L $artifacts/stage/frontier/batch/pmix_test/build_pmix_test/python-site-packages ]] || \
    fail 'python-site-packages symlink was not preserved'
[[ -f $artifacts/stage/frontier/batch/pmix_test/fetch_pmix_test/pmix-commit.env ]] || \
    fail 'pmix-commit.env was not copied'

printf 'ok - artifact collector skip and full-run smoke test\n'
