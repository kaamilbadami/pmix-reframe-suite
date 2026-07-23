#!/bin/bash
# Trusted preparation boundary: metadata and status tokens never leave this job.
# Only the fixed same-repository author allowlist may reach execution; approved
# author code is trusted under the Frontier service account for this MVP.
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

output_dir=ci-pr-preparation
work_dir=$output_dir/private-work
preparation_record=$output_dir/preparation.env

# Artifacts are uploaded even when this job fails.  Remove the fixed output
# entry first so a reused runner worktree cannot republish another run's data.
/usr/bin/rm -rf --one-file-system -- "$output_dir"
if [[ -L $output_dir || -e $output_dir ]]; then
    fail 'could not remove the previous preparation output directory'
fi

if (( $# != 1 )); then
    printf 'usage: %s PR_NUMBER\n' "${0##*/}" >&2
    exit 2
fi

pr_number=$1
[[ $pr_number =~ ^[1-9][0-9]*$ ]] ||
    fail 'PR number must be a canonical positive integer'
[[ -n ${GITHUB_PR_READ_TOKEN:-} ]] || fail 'GITHUB_PR_READ_TOKEN is required'
[[ -n ${GITHUB_STATUS_TOKEN:-} ]] || fail 'GITHUB_STATUS_TOKEN is required'
[[ ${CI_PIPELINE_ID:-} =~ ^[1-9][0-9]*$ ]] ||
    fail 'CI_PIPELINE_ID must be a canonical positive integer'

metadata_wrapper=ci/run_pmix_tests_pr_metadata_pilot.sh
fetcher=ci/fetch_pmix_tests_pr.py
checker=ci/check_trusted_pmix_tests_pr.py
records=ci/pmix_tests_pr_artifacts.py
reporter=ci/report_pmix_tests_pr_status.sh
python_bin=/lustre/orion/scratch/kbadami/gen243/reframe_practice/pmix-py310/bin/python
for helper in "$metadata_wrapper" "$fetcher" "$checker" "$records" "$reporter"; do
    [[ -f $helper && ! -L $helper ]] || fail 'a trusted preparation helper is unavailable'
done
[[ -x $python_bin ]] || fail 'fixed Frontier Python is unavailable'

/usr/bin/mkdir -m 700 -- "$output_dir" "$work_dir"
/usr/bin/mkdir -m 700 -- "$work_dir/initial" "$work_dir/revalidated"
[[ -d $output_dir && ! -L $output_dir && -d $work_dir && ! -L $work_dir ]] ||
    fail 'could not create the preparation output directory'

# The existing metadata-only path remains authoritative.  The new record
# converter adds this MVP's same-repository-only policy.
"$metadata_wrapper" "$pr_number" "$work_dir/initial"
"$python_bin" "$records" write-preparation \
    --trusted-record "$work_dir/initial/trusted-pr.env" \
    --pr-number "$pr_number" \
    --pipeline-id "$CI_PIPELINE_ID" \
    --result error \
    --output "$preparation_record"

selected_sha=$("$python_bin" "$records" read-preparation \
    --input "$preparation_record" \
    --expected-pipeline-id "$CI_PIPELINE_ID" --field PR_HEAD_SHA)
selected_author=$("$python_bin" "$records" read-preparation \
    --input "$preparation_record" \
    --expected-pipeline-id "$CI_PIPELINE_ID" --field PR_AUTHOR)

printf 'Selected trusted PR number: %s\n' "$pr_number"
printf 'Selected trusted PR author: %s\n' "$selected_author"
printf '%s\n' 'Selected trusted PR repository: kaamilbadami/pmix-tests'
printf 'Selected trusted PR head SHA: %s\n' "$selected_sha"

# Re-fetch immediately before publication and require the original head.
"$fetcher" --pr-number "$pr_number" \
    --output "$work_dir/revalidated/pr.json"
"$checker" \
    --pr-json "$work_dir/revalidated/pr.json" \
    --pr-number "$pr_number" \
    --expected-head-sha "$selected_sha" \
    --output "$work_dir/revalidated/trusted-pr.env"

# Validate the full identity again, including the non-fork requirement.
"$python_bin" "$records" write-preparation \
    --trusted-record "$work_dir/revalidated/trusted-pr.env" \
    --pr-number "$pr_number" \
    --pipeline-id "$CI_PIPELINE_ID" \
    --expected-sha "$selected_sha" \
    --expected-author "$selected_author" \
    --result ready \
    --output "$work_dir/revalidated/preparation.env"

"$reporter" "$selected_sha" pending \
    'Frontier PMIx tests PR check is running'

# Publish only fixed validated non-secret fields.  Raw GitHub JSON and the
# eligibility helper's intermediate record remain outside the artifact path.
"$python_bin" "$records" write-preparation \
    --trusted-record "$work_dir/revalidated/trusted-pr.env" \
    --pr-number "$pr_number" \
    --pipeline-id "$CI_PIPELINE_ID" \
    --expected-sha "$selected_sha" \
    --expected-author "$selected_author" \
    --result ready \
    --output "$preparation_record"

printf 'Published trusted preparation for PR %s at %s\n' \
    "$pr_number" "$selected_sha"
