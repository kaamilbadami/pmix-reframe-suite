#!/bin/bash
# Metadata-only trusted preparation for the manual OpenPMIx PR prototype.
# This helper does not post GitHub statuses and has no execution capability.
set +x
set -euo pipefail
export PATH=/usr/bin:/bin

fail() {
    printf 'error: %s\n' "$1" >&2
    exit 2
}

script_dir=$(cd -- "${BASH_SOURCE[0]%/*}" && pwd -P)
repo_root=$(cd -- "$script_dir/.." && pwd -P)
cd -- "$repo_root"

output_dir=ci-openpmix-pr-preparation
work_dir=$output_dir/private-work
preparation_record=$output_dir/preparation.env

# This is a fixed path. Remove it before any input or credential validation so
# a reused worktree cannot expose a stale ready record after an early failure.
/usr/bin/rm -rf --one-file-system -- "$output_dir"
if [[ -L $output_dir || -e $output_dir ]]; then
    fail 'could not remove the previous OpenPMIx preparation directory'
fi

if (( $# < 1 || $# > 2 )); then
    printf 'usage: %s PR_NUMBER [EXPECTED_HEAD_SHA]\n' "${0##*/}" >&2
    exit 2
fi

pr_number=$1
expected_head_sha=${2:-}
[[ $pr_number =~ ^[1-9][0-9]*$ ]] ||
    fail 'PR number must be a canonical positive integer'
if [[ -n $expected_head_sha &&
      ! $expected_head_sha =~ ^[0-9a-f]{40}$ ]]; then
    fail 'expected head SHA must be lowercase 40-character hexadecimal'
fi
[[ -n ${GITHUB_PR_READ_TOKEN:-} ]] ||
    fail 'GITHUB_PR_READ_TOKEN is required'
[[ ${CI_PIPELINE_ID:-} =~ ^[1-9][0-9]*$ ]] ||
    fail 'CI_PIPELINE_ID must be a canonical positive integer'

fetcher=ci/fetch_openpmix_pr.py
checker=ci/check_trusted_openpmix_pr.py
records=ci/openpmix_pr_artifacts.py
python_bin=/lustre/orion/gen243/proj-shared/pmix-reframe-ci-tools/pmix-py310/bin/python

for helper in "$fetcher" "$checker" "$records"; do
    [[ -f $helper && ! -L $helper ]] ||
        fail 'an OpenPMIx preparation helper is unavailable'
done
[[ -x $python_bin ]] || fail 'fixed Frontier Python is unavailable'

/usr/bin/mkdir -m 700 -- \
    "$output_dir" "$work_dir" "$work_dir/initial" "$work_dir/revalidated"
[[ -d $output_dir && ! -L $output_dir &&
   -d $work_dir && ! -L $work_dir ]] ||
    fail 'could not create the OpenPMIx preparation directory'

"$python_bin" "$fetcher" \
    --pr-number "$pr_number" \
    --output "$work_dir/initial/pr.json"
initial_checker_arguments=(
    --pr-json "$work_dir/initial/pr.json"
    --pr-number "$pr_number"
    --output "$work_dir/initial/eligibility.env"
)
if [[ -n $expected_head_sha ]]; then
    initial_checker_arguments+=(--expected-head-sha "$expected_head_sha")
fi
"$python_bin" "$checker" "${initial_checker_arguments[@]}"

# Publish a fail-closed record bound to the initially selected identity before
# performing the second network operation. It can never authorize execution.
"$python_bin" "$records" write-preparation \
    --eligibility "$work_dir/initial/eligibility.env" \
    --pr-number "$pr_number" \
    --pipeline-id "$CI_PIPELINE_ID" \
    --result error \
    --output "$preparation_record"

selected_sha=$("$python_bin" "$records" read-preparation \
    --input "$preparation_record" \
    --expected-pipeline-id "$CI_PIPELINE_ID" \
    --field PR_HEAD_SHA)
selected_author=$("$python_bin" "$records" read-preparation \
    --input "$preparation_record" \
    --expected-pipeline-id "$CI_PIPELINE_ID" \
    --field PR_AUTHOR)
selected_head_repository=$("$python_bin" "$records" read-preparation \
    --input "$preparation_record" \
    --expected-pipeline-id "$CI_PIPELINE_ID" \
    --field PR_HEAD_REPOSITORY)

printf 'Selected OpenPMIx PR number: %s\n' "$pr_number"
printf 'Selected OpenPMIx PR author: %s\n' "$selected_author"
printf 'Selected OpenPMIx PR base repository: %s\n' 'openpmix/openpmix'
printf 'Selected OpenPMIx PR head repository: %s\n' \
    "$selected_head_repository"
printf 'Selected OpenPMIx PR head SHA: %s\n' "$selected_sha"

# Re-fetch from the same fixed API endpoint and require the complete selected
# identity. Neither response URLs nor clone URLs are accepted as inputs.
"$python_bin" "$fetcher" \
    --pr-number "$pr_number" \
    --output "$work_dir/revalidated/pr.json"
"$python_bin" "$checker" \
    --pr-json "$work_dir/revalidated/pr.json" \
    --pr-number "$pr_number" \
    --expected-head-sha "$selected_sha" \
    --expected-author "$selected_author" \
    --expected-head-repository "$selected_head_repository" \
    --output "$work_dir/revalidated/eligibility.env"

# The ready record is published only after the authoritative second response
# passes the full policy and exact-identity checks.
"$python_bin" "$records" write-preparation \
    --eligibility "$work_dir/revalidated/eligibility.env" \
    --pr-number "$pr_number" \
    --pipeline-id "$CI_PIPELINE_ID" \
    --expected-sha "$selected_sha" \
    --expected-author "$selected_author" \
    --expected-head-repository "$selected_head_repository" \
    --result ready \
    --output "$preparation_record"

printf 'Published ready OpenPMIx preparation for PR %s at %s\n' \
    "$pr_number" "$selected_sha"
