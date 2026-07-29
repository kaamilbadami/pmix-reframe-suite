#!/bin/bash
# Credential-free exact-SHA OpenPMIx PR execution.
set +x
set -euo pipefail
umask 077

fail() {
    printf 'error: %s\n' "$1" >&2
    exit 2
}

(( $# == 0 )) || fail 'this helper accepts no arguments'

script_dir=$(cd -- "${BASH_SOURCE[0]%/*}" && pwd -P)
repo_root=$(cd -- "$script_dir/.." && pwd -P)
cd -- "$repo_root"

for forbidden_name in \
    GITHUB_PR_READ_TOKEN GITHUB_STATUS_TOKEN CI_JOB_TOKEN CI_REPOSITORY_URL \
    CI_JOB_JWT CI_JOB_JWT_V2 OPENPMIX_PR_PROTECTED_SENTINEL \
    OPENPMIX_PR_CHECKOUT_TEST_MODE
do
    [[ ! -v $forbidden_name ]] ||
        fail 'execution environment contains a forbidden credential variable'
done
[[ ${CI_PIPELINE_ID:-} =~ ^[1-9][0-9]*$ ]] ||
    fail 'CI_PIPELINE_ID must be a canonical positive integer'

output_dir=ci-openpmix-pr-execution
evidence_dir=$output_dir/evidence
checkout_dir=$output_dir/openpmix
reframe_prefix=$output_dir/reframe
report_file=$reframe_prefix/run-report.json
preparation_record=ci-openpmix-pr-preparation/preparation.env
checkout_record=$evidence_dir/checkout.env
result_record=$output_dir/result.env
records=ci/openpmix_pr_artifacts.py
checkout_helper=ci/fetch_openpmix_pr_checkout.sh
exact_runner=ci/run_exact_pmix_commit.sh
python_bin=/lustre/orion/gen243/proj-shared/pmix-reframe-ci-tools/pmix-py310/bin/python
expected_rfm=/lustre/orion/gen243/proj-shared/pmix-reframe-ci-tools/reframe-4.10/bin/reframe

for helper in "$records" "$checkout_helper" "$exact_runner"; do
    [[ -f $helper && ! -L $helper ]] ||
        fail 'a trusted OpenPMIx execution helper is unavailable'
done
[[ ${PMIX_PYTHON:-} == "$python_bin" && -x $python_bin ]] ||
    fail 'fixed Frontier Python is unavailable'
[[ ${RFM_BIN:-} == "$expected_rfm" && -x $RFM_BIN ]] ||
    fail 'fixed ReFrame executable is unavailable'
[[ -f $preparation_record && ! -L $preparation_record ]] ||
    fail 'trusted OpenPMIx preparation record is unavailable'

/usr/bin/rm -rf --one-file-system -- "$output_dir"
if [[ -L $output_dir || -e $output_dir ]]; then
    fail 'could not remove previous OpenPMIx execution output'
fi
/usr/bin/mkdir -m 700 -- "$output_dir" "$evidence_dir"

"$python_bin" "$records" read-preparation \
    --input "$preparation_record" \
    --require-ready \
    --expected-pipeline-id "$CI_PIPELINE_ID" \
    --field PR_HEAD_SHA >/dev/null

execution_id=$(/usr/bin/od -An -N16 -tx1 /dev/urandom |
    /usr/bin/tr -d ' \n')
[[ $execution_id =~ ^[0-9a-f]{32}$ ]] ||
    fail 'could not create execution identifier'

# Ensure every later failure has a current, pipeline-bound result.
"$python_bin" "$records" write-result \
    --preparation "$preparation_record" \
    --pipeline-id "$CI_PIPELINE_ID" \
    --execution-id "$execution_id" \
    --result error \
    --classification frontend-error \
    --runner-exit-status unavailable \
    --report-sha256 missing \
    --checkout-evidence-sha256 missing \
    --output "$result_record"

selected_number=$("$python_bin" "$records" read-preparation \
    --input "$preparation_record" --require-ready \
    --expected-pipeline-id "$CI_PIPELINE_ID" --field PR_NUMBER)
selected_sha=$("$python_bin" "$records" read-preparation \
    --input "$preparation_record" --require-ready \
    --expected-pipeline-id "$CI_PIPELINE_ID" --field PR_HEAD_SHA)
selected_author=$("$python_bin" "$records" read-preparation \
    --input "$preparation_record" --require-ready \
    --expected-pipeline-id "$CI_PIPELINE_ID" --field PR_AUTHOR)

printf 'Executing OpenPMIx PR number: %s\n' "$selected_number"
printf 'Executing OpenPMIx PR author: %s\n' "$selected_author"
printf 'Executing exact OpenPMIx SHA: %s\n' "$selected_sha"

checkout_status=0
/bin/bash "$checkout_helper" \
    "$selected_number" "$selected_sha" "$checkout_dir" ||
    checkout_status=$?
if (( checkout_status != 0 )); then
    case $checkout_status in
        5) classification=head-changed ;;
        6) classification=missing-pr-ref ;;
        7) classification=unauthorized-submodule ;;
        *) classification=checkout-error ;;
    esac
    "$python_bin" "$records" write-result \
        --preparation "$preparation_record" \
        --pipeline-id "$CI_PIPELINE_ID" \
        --execution-id "$execution_id" \
        --result error \
        --classification "$classification" \
        --runner-exit-status unavailable \
        --report-sha256 missing \
        --checkout-evidence-sha256 missing \
        --output "$result_record"
    exit 2
fi

checked_out_sha=$(/usr/bin/git -C "$checkout_dir" rev-parse --verify HEAD)
"$python_bin" "$records" write-checkout \
    --preparation "$preparation_record" \
    --pipeline-id "$CI_PIPELINE_ID" \
    --fetched-sha "$selected_sha" \
    --checked-out-sha "$checked_out_sha" \
    --output "$checkout_record"
/usr/bin/git -C "$checkout_dir" show -s --format='%H' HEAD > \
    "$evidence_dir/checkout-commit.txt"
/usr/bin/git -C "$checkout_dir" status --porcelain=v1 \
    --untracked-files=all > "$evidence_dir/checkout-status-before.txt"

export PMIX_COMMIT=$selected_sha
export PMIX_TRUSTED_SOURCE_DIR=$repo_root/$checkout_dir
export PMIX_RFM_CONFIG_FILE=ci/openpmix_pr_sysconfig.yaml
export PMIX_RFM_PREFIX=$repo_root/$reframe_prefix
export PMIX_RFM_REPORT_FILE=$repo_root/$report_file

runner_status=0
/bin/bash "$exact_runner" || runner_status=$?
if (( runner_status < 0 || runner_status > 255 )); then
    runner_status=255
fi

/usr/bin/git -C "$checkout_dir" status --porcelain=v1 \
    --untracked-files=all > "$evidence_dir/checkout-status-after.txt"
if [[ -s $evidence_dir/checkout-status-after.txt ]] ||
   /usr/bin/git -C "$checkout_dir" symbolic-ref -q HEAD >/dev/null ||
   [[ $(/usr/bin/git -C "$checkout_dir" rev-parse --verify HEAD) != \
        "$selected_sha" ]]; then
    "$python_bin" "$records" write-result \
        --preparation "$preparation_record" \
        --pipeline-id "$CI_PIPELINE_ID" \
        --execution-id "$execution_id" \
        --result error \
        --classification checkout-error \
        --runner-exit-status "$runner_status" \
        --report-sha256 missing \
        --checkout-evidence-sha256 missing \
        --output "$result_record"
    exit 2
fi

classification_status=0
"$python_bin" "$records" classify-report \
    --preparation "$preparation_record" \
    --checkout "$checkout_record" \
    --report "$report_file" \
    --pipeline-id "$CI_PIPELINE_ID" \
    --execution-id "$execution_id" \
    --runner-exit-status "$runner_status" \
    --output "$result_record" || classification_status=$?

case $classification_status in
    0)
        printf 'OpenPMIx PR exact-SHA suite passed: %s\n' "$selected_sha"
        ;;
    1)
        printf 'OpenPMIx PR exact-SHA suite failed: %s\n' \
            "$selected_sha" >&2
        ;;
    *)
        classification_status=2
        printf 'OpenPMIx PR suite ended with an infrastructure error: %s\n' \
            "$selected_sha" >&2
        ;;
esac
exit "$classification_status"
