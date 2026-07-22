#!/bin/bash
set -euo pipefail

usage() {
    printf 'usage: %s PR_NUMBER PILOT_OUTPUT_DIRECTORY\n' "${0##*/}" >&2
}

fail() {
    printf 'error: %s\n' "$1" >&2
    exit 2
}

remove_regular_output() {
    local path=$1

    if [[ -L $path ]] || [[ -e $path && ! -f $path ]]; then
        fail 'pilot output files must be regular files'
    fi
    rm -f -- "$path" || fail 'could not remove a pilot output file'
}

if [[ $# -ne 2 ]]; then
    usage
    exit 2
fi

pr_number=$1
output_dir=$2
script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
fetcher="$script_dir/fetch_pmix_tests_pr.py"
checker="$script_dir/check_trusted_pmix_tests_pr.py"

[[ $pr_number =~ ^[1-9][0-9]*$ ]] ||
    fail 'PR number must be a canonical positive integer'
[[ -n ${GITHUB_PR_READ_TOKEN:-} ]] ||
    fail 'GITHUB_PR_READ_TOKEN is required'
[[ -f $fetcher && -x $fetcher && ! -L $fetcher ]] ||
    fail 'trusted PR metadata fetcher is unavailable'
[[ -f $checker && -x $checker && ! -L $checker ]] ||
    fail 'trusted PR eligibility checker is unavailable'

[[ -n $output_dir && $output_dir != /* ]] ||
    fail 'pilot output directory must be a relative path'
[[ $output_dir != *'.ci-state'* ]] ||
    fail 'pilot output directory may not contain .ci-state'

IFS=/ read -r -a output_components <<< "$output_dir"
current=.
for component in "${output_components[@]}"; do
    [[ -n $component && $component != . && $component != .. ]] ||
        fail 'pilot output directory contains an unsafe path component'
    [[ $component != .ci-state ]] ||
        fail 'pilot output directory may not use .ci-state'
    current="$current/$component"
    if [[ $current != "./$output_dir" ]]; then
        [[ -d $current && ! -L $current ]] ||
            fail 'pilot output parent must be a real directory'
    fi
done

if [[ -L $output_dir ]] || [[ -e $output_dir && ! -d $output_dir ]]; then
    fail 'pilot output path must be a real directory'
fi
if [[ ! -e $output_dir ]]; then
    mkdir -- "$output_dir" || fail 'could not create the pilot output directory'
fi
[[ -d $output_dir && ! -L $output_dir ]] ||
    fail 'pilot output path must be a real directory'

pr_json="$output_dir/pr.json"
trusted_env="$output_dir/trusted-pr.env"
remove_regular_output "$pr_json"
remove_regular_output "$trusted_env"

fetch_status=0
"$fetcher" \
    --pr-number "$pr_number" \
    --output "$pr_json" || fetch_status=$?
if (( fetch_status != 0 )); then
    remove_regular_output "$pr_json"
    remove_regular_output "$trusted_env"
    printf 'PR metadata fetch failed for PR %s\n' "$pr_number" >&2
    exit "$fetch_status"
fi

[[ -f $pr_json && ! -L $pr_json ]] ||
    fail 'fetcher did not publish a regular PR metadata file'
printf 'Fetched metadata for PR %s\n' "$pr_number"

check_status=0
"$checker" \
    --pr-json "$pr_json" \
    --pr-number "$pr_number" \
    --output "$trusted_env" || check_status=$?
if (( check_status != 0 )); then
    remove_regular_output "$trusted_env"
    printf 'PR metadata eligibility rejected for PR %s\n' "$pr_number" >&2
    exit "$check_status"
fi

[[ -f $trusted_env && ! -L $trusted_env ]] ||
    fail 'checker did not publish a regular eligibility record'
printf 'PR metadata eligibility accepted for PR %s\n' "$pr_number"
printf '%s\n' 'Eligibility record:'
cat -- "$trusted_env"
