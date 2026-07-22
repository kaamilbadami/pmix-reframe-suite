#!/bin/bash
set -euo pipefail

usage() {
    printf 'usage: %s ORDERED_COMMIT_LIST PILOT_OUTPUT_DIRECTORY\n' "$0" >&2
}

fail() {
    printf 'error: %s\n' "$1" >&2
    exit 2
}

if [[ $# -ne 2 ]]; then
    usage
    exit 2
fi

ordered_commits=$1
pilot_output=$2
script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
collector="$script_dir/collect_pmix_child_results.py"
reconciler="$script_dir/reconcile_pmix_results.py"

[[ ${PMIX_CHILD_PIPELINE_BASE_SHA:-} =~ ^[0-9a-f]{40}$ ]] ||
    fail 'PMIX_CHILD_PIPELINE_BASE_SHA must be a lowercase 40-character SHA'
[[ ${CI_COMMIT_SHA:-} =~ ^[0-9a-f]{40}$ ]] ||
    fail 'CI_COMMIT_SHA must be a lowercase 40-character SHA'
[[ ${CI_PIPELINE_ID:-} =~ ^[1-9][0-9]*$ ]] ||
    fail 'CI_PIPELINE_ID must be a positive decimal integer'

[[ -f $ordered_commits && ! -L $ordered_commits ]] ||
    fail 'ordered commit list must be a regular file'
[[ -f $collector && -x $collector && ! -L $collector ]] ||
    fail 'collector executable is missing'
[[ -f $reconciler && -x $reconciler && ! -L $reconciler ]] ||
    fail 'reconciler executable is missing'

if [[ -L $pilot_output ]] ||
   [[ -e $pilot_output && ! -d $pilot_output ]]; then
    fail 'pilot output path must be a real directory'
fi
mkdir -p -- "$pilot_output"
[[ ! -L $pilot_output && -d $pilot_output ]] ||
    fail 'pilot output path must be a real directory'

baseline_state="$pilot_output/baseline-pmix-master.env"
current_state="$pilot_output/current-pmix-master.env"
collection_output="$pilot_output/collection"
reconciliation_output="$pilot_output/reconciliation"

for known_state in "$baseline_state" "$current_state"; do
    if [[ -L $known_state ]] || [[ -e $known_state && ! -f $known_state ]]; then
        fail 'pilot state output must be a regular file'
    fi
done

last_success_epoch=$(date -u +%s) || fail 'could not obtain the current epoch'
[[ $last_success_epoch =~ ^[1-9][0-9]*$ ]] ||
    fail 'current epoch is not a canonical positive integer'

baseline_tmp=$(mktemp "$pilot_output/.baseline-pmix-master.env.tmp.XXXXXX") ||
    fail 'could not create the pilot baseline temporary file'
current_tmp=
cleanup() {
    if [[ -n $baseline_tmp ]]; then
        rm -f -- "$baseline_tmp"
    fi
    if [[ -n $current_tmp ]]; then
        rm -f -- "$current_tmp"
    fi
}
trap cleanup EXIT

printf 'PMIX_COMMIT=%s\nSUITE_COMMIT=%s\nLAST_SUCCESS_EPOCH=%s\n' \
    "$PMIX_CHILD_PIPELINE_BASE_SHA" \
    "$CI_COMMIT_SHA" \
    "$last_success_epoch" > "$baseline_tmp"
current_tmp=$(mktemp "$pilot_output/.current-pmix-master.env.tmp.XXXXXX") ||
    fail 'could not create the pilot current-state temporary file'
cp -- "$baseline_tmp" "$current_tmp"
mv -f -- "$baseline_tmp" "$baseline_state"
baseline_tmp=
mv -f -- "$current_tmp" "$current_state"
current_tmp=
trap - EXIT

collector_status=0
"$collector" \
    --commits "$ordered_commits" \
    --parent-pipeline-id "$CI_PIPELINE_ID" \
    --output "$collection_output" || collector_status=$?

case $collector_status in
    0|3)
        ;;
    4|5|6)
        exit "$collector_status"
        ;;
    *)
        printf 'error: collector returned unexpected status %s\n' \
            "$collector_status" >&2
        exit "$collector_status"
        ;;
esac

reconciler_status=0
"$reconciler" \
    --baseline-state "$baseline_state" \
    --current-state "$current_state" \
    --commits "$ordered_commits" \
    --results "$collection_output" \
    --suite-commit "$CI_COMMIT_SHA" \
    --output "$reconciliation_output" || reconciler_status=$?

exit "$reconciler_status"
