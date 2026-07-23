#!/bin/bash
# Credential-scrubbed approved-author execution.  This is not a sandbox for
# arbitrary PR code; this script never fetches PR metadata or receives tokens.
set +x
set -euo pipefail

fail() {
    printf 'error: %s\n' "$1" >&2
    exit 2
}

script_dir=$(cd -- "${BASH_SOURCE[0]%/*}" && pwd -P)
repo_root=$(cd -- "$script_dir/.." && pwd -P)
cd -- "$repo_root"

output_dir=ci-pr-execution
# The artifact policy is `when: always`; invalidate any previous directory
# entry before credentials, preparation data, or tools can cause an early exit.
/usr/bin/rm -rf --one-file-system -- "$output_dir"
if [[ -L $output_dir || -e $output_dir ]]; then
    fail 'could not remove the previous execution output directory'
fi

(( $# == 0 )) || fail 'this helper accepts no arguments'
for forbidden_name in \
    GITHUB_PR_READ_TOKEN GITHUB_STATUS_TOKEN CI_JOB_TOKEN CI_REPOSITORY_URL \
    CI_JOB_JWT CI_JOB_JWT_V2
do
    [[ ! -v $forbidden_name ]] ||
        fail 'execution environment contains a forbidden credential variable'
done
[[ ${CI_PIPELINE_ID:-} =~ ^[1-9][0-9]*$ ]] ||
    fail 'CI_PIPELINE_ID must be a canonical positive integer'

records=ci/pmix_tests_pr_artifacts.py
test_runner=ci/run_trusted_pmix_tests_pr_test.sh
python_bin=/lustre/orion/gen243/proj-shared/pmix-reframe-ci-tools/pmix-py310/bin/python
preparation_record=ci-pr-preparation/preparation.env
checkout_dir=$output_dir/pmix-tests
result_record=$output_dir/result.env
report_file=$output_dir/reframe/run-report.json
evidence_dir=$output_dir/reframe/stage/frontier/batch/pmix_test/PMIxTestsPRPythonSmokeTest
clone_url=https://github.com/kaamilbadami/pmix-tests.git

for helper in "$records" "$test_runner"; do
    [[ -f $helper && ! -L $helper ]] || fail 'a trusted execution helper is unavailable'
done
[[ ${PMIX_PYTHON:-} == "$python_bin" && -x $python_bin ]] ||
    fail 'fixed Frontier Python is unavailable'
[[ -f $preparation_record && ! -L $preparation_record ]] ||
    fail 'trusted preparation artifact is unavailable'
mkdir -m 700 -- "$output_dir"
[[ -d $output_dir && ! -L $output_dir ]] ||
    fail 'could not create the execution output directory'

# Reject another pipeline's preparation before checkout or workload execution.
"$python_bin" "$records" read-preparation \
    --input "$preparation_record" --require-ready \
    --expected-pipeline-id "$CI_PIPELINE_ID" --field PR_HEAD_SHA >/dev/null

execution_id=$(/usr/bin/od -An -N16 -tx1 /dev/urandom | /usr/bin/tr -d ' \n')
[[ $execution_id =~ ^[0-9a-f]{32}$ ]] || fail 'could not create execution identifier'

# Publish a fail-closed record before any checkout operation.  A later trusted
# report classification atomically replaces it.
"$python_bin" "$records" write-error-result \
    --preparation "$preparation_record" \
    --execution-id "$execution_id" \
    --pipeline-id "$CI_PIPELINE_ID" \
    --output "$result_record"

selected_sha=$("$python_bin" "$records" read-preparation \
    --input "$preparation_record" --require-ready \
    --expected-pipeline-id "$CI_PIPELINE_ID" --field PR_HEAD_SHA)
selected_number=$("$python_bin" "$records" read-preparation \
    --input "$preparation_record" --require-ready \
    --expected-pipeline-id "$CI_PIPELINE_ID" --field PR_NUMBER)
selected_author=$("$python_bin" "$records" read-preparation \
    --input "$preparation_record" --require-ready \
    --expected-pipeline-id "$CI_PIPELINE_ID" --field PR_AUTHOR)

printf 'Executing trusted PR number: %s\n' "$selected_number"
printf 'Executing trusted PR author: %s\n' "$selected_author"
printf '%s\n' 'Executing fixed repository: kaamilbadami/pmix-tests'
printf 'Executing exact PR head SHA: %s\n' "$selected_sha"

# No user/global Git configuration, credentials, hooks, submodules, branch
# names, or PR-provided URLs participate in this checkout.
export GIT_CONFIG_NOSYSTEM=1
export GIT_CONFIG_GLOBAL=/dev/null
export GIT_TERMINAL_PROMPT=0
export GIT_ASKPASS=/bin/false
/usr/bin/git -c credential.helper= -c core.hooksPath=/dev/null \
    -c protocol.file.allow=never -c http.followRedirects=false \
    clone --no-checkout --no-tags --no-recurse-submodules \
    "$clone_url" "$checkout_dir"
/usr/bin/git -C "$checkout_dir" -c credential.helper= -c core.hooksPath=/dev/null \
    -c protocol.file.allow=never -c http.followRedirects=false \
    fetch --no-tags origin "$selected_sha"
/usr/bin/git -C "$checkout_dir" -c core.hooksPath=/dev/null \
    checkout --detach "$selected_sha" --

checked_out_sha=$(/usr/bin/git -C "$checkout_dir" rev-parse --verify HEAD)
[[ $checked_out_sha == "$selected_sha" ]] || fail 'checkout resolved to another commit'
resolved_commit=$(/usr/bin/git -C "$checkout_dir" rev-parse --verify "$selected_sha^{commit}")
[[ $resolved_commit == "$selected_sha" ]] || fail 'selected SHA is not the exact commit'
if /usr/bin/git -C "$checkout_dir" symbolic-ref -q HEAD >/dev/null; then
    fail 'checkout is attached to a branch'
fi
origin_url=$(/usr/bin/git -C "$checkout_dir" remote get-url origin)
[[ $origin_url == "$clone_url" ]] || fail 'checkout origin is not the fixed repository'

checkout_absolute=$("$python_bin" "$records" validate-checkout --checkout "$checkout_dir")
printf 'Verified detached checkout at exact SHA: %s\n' "$checked_out_sha"

# These files are diagnostics for operators.  They are not trusted security
# evidence and are not consumed by finalization; only preparation.env and the
# strict result.env schema participate in the final decision.
printf '%s\n' \
    'PMIX_TESTS_PR_CHECKOUT_VERSION=1' \
    'PR_REPOSITORY=kaamilbadami/pmix-tests' \
    "PR_NUMBER=$selected_number" \
    "PR_HEAD_SHA=$selected_sha" \
    "CHECKED_OUT_SHA=$checked_out_sha" \
    "EXECUTION_ID=$execution_id" > "$output_dir/checkout.env"
/usr/bin/git -C "$checkout_dir" show -s --format='%H' HEAD > \
    "$output_dir/checkout-commit.txt"
/usr/bin/sha256sum -- \
    "$checkout_dir/python/server.py" \
    "$checkout_dir/python/client.py" > "$output_dir/test-source.sha256"

export PMIX_TESTS_SOURCE_DIR=$checkout_absolute
export PMIX_TESTS_PR_HEAD_SHA=$selected_sha
export PMIX_TESTS_PR_EXECUTION_ID=$execution_id

reframe_status=0
"$test_runner" || reframe_status=$?
if (( reframe_status < 0 || reframe_status > 255 )); then
    reframe_status=255
fi

classification_status=0
"$python_bin" "$records" classify-report \
    --preparation "$preparation_record" \
    --report "$report_file" \
    --evidence-directory "$evidence_dir" \
    --execution-id "$execution_id" \
    --pipeline-id "$CI_PIPELINE_ID" \
    --reframe-status "$reframe_status" \
    --output "$result_record" || classification_status=$?

case $classification_status in
    0)
        printf 'Trusted PMIx tests PR smoke check passed at %s\n' "$selected_sha"
        ;;
    1)
        printf 'Trusted PMIx tests PR smoke check failed at %s\n' "$selected_sha" >&2
        ;;
    *)
        classification_status=2
        printf 'Trusted PMIx tests PR execution ended with an infrastructure or validation error at %s\n' \
            "$selected_sha" >&2
        ;;
esac
exit "$classification_status"
