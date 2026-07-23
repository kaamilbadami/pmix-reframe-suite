#!/bin/bash
# Trusted fresh-checkout finalizer.  It strictly parses the approved-author
# execution result, but does not claim resilience to malicious code running as
# the same service account; stronger isolation is outside this MVP.
set +x
set -euo pipefail

if (( $# != 2 )); then
    printf 'usage: %s PREPARATION_RECORD EXECUTION_RESULT_RECORD\n' "${0##*/}" >&2
    exit 2
fi

preparation_record=$1
result_record=$2
script_dir=$(cd -- "${BASH_SOURCE[0]%/*}" && pwd -P)
records=$script_dir/pmix_tests_pr_artifacts.py
reporter=$script_dir/report_pmix_tests_pr_status.sh
python_bin=/lustre/orion/gen243/proj-shared/pmix-reframe-ci-tools/pmix-py310/bin/python

[[ -f $records && ! -L $records ]] || {
    printf '%s\n' 'error: trusted artifact parser is unavailable' >&2
    exit 2
}
[[ -x $reporter && -f $reporter && ! -L $reporter ]] || {
    printf '%s\n' 'error: trusted status reporter is unavailable' >&2
    exit 2
}
[[ -x $python_bin ]] || {
    printf '%s\n' 'error: fixed Frontier Python is unavailable' >&2
    exit 2
}
[[ -n ${GITHUB_STATUS_TOKEN:-} ]] || {
    printf '%s\n' 'error: GITHUB_STATUS_TOKEN is required' >&2
    exit 2
}
[[ ${CI_PIPELINE_ID:-} =~ ^[1-9][0-9]*$ ]] || {
    printf '%s\n' 'error: CI_PIPELINE_ID must be a canonical positive integer' >&2
    exit 2
}

decision=$("$python_bin" "$records" final-decision \
    --preparation "$preparation_record" \
    --result "$result_record" \
    --pipeline-id "$CI_PIPELINE_ID") || exit $?
read -r original_sha execution_result extra <<< "$decision"
[[ -z ${extra:-} && $original_sha =~ ^[0-9a-f]{40}$ ]] || {
    printf '%s\n' 'error: trusted final decision output is invalid' >&2
    exit 2
}

case $execution_result in
    success)
        state=success
        description='Frontier PMIx tests PR check passed'
        ;;
    failure)
        state=failure
        description='Frontier PMIx tests PR check failed'
        ;;
    error)
        state=error
        description='Frontier PMIx tests PR infrastructure or validation error'
        ;;
    *)
        printf '%s\n' 'error: trusted final decision state is invalid' >&2
        exit 2
        ;;
esac

exec "$reporter" "$original_sha" "$state" "$description"
