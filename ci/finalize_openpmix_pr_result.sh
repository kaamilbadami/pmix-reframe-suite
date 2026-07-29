#!/bin/bash
# Fresh-checkout, artifact-only OpenPMIx PR finalization.
set +x
set -euo pipefail

if (( $# != 2 )); then
    printf 'usage: %s PREPARATION_RECORD RESULT_RECORD\n' "${0##*/}" >&2
    exit 2
fi

preparation_record=$1
result_record=$2
script_dir=$(cd -- "${BASH_SOURCE[0]%/*}" && pwd -P)
repo_root=$(cd -- "$script_dir/.." && pwd -P)
cd -- "$repo_root"

[[ ${CI_PIPELINE_ID:-} =~ ^[1-9][0-9]*$ ]] || {
    printf '%s\n' 'error: CI_PIPELINE_ID must be canonical' >&2
    exit 2
}

records=ci/openpmix_pr_artifacts.py
python_bin=/lustre/orion/gen243/proj-shared/pmix-reframe-ci-tools/pmix-py310/bin/python
output_dir=ci-openpmix-pr-final
final_record=$output_dir/final-result.env

[[ -f $records && ! -L $records && -x $python_bin ]] || {
    printf '%s\n' 'error: trusted finalization tools are unavailable' >&2
    exit 2
}

/usr/bin/rm -rf --one-file-system -- "$output_dir"
if [[ -L $output_dir || -e $output_dir ]]; then
    printf '%s\n' 'error: could not clear finalization output' >&2
    exit 2
fi
/usr/bin/mkdir -m 700 -- "$output_dir"

"$python_bin" "$records" write-final \
    --preparation "$preparation_record" \
    --result "$result_record" \
    --pipeline-id "$CI_PIPELINE_ID" \
    --output "$final_record"

result=$("$python_bin" "$records" read-final \
    --input "$final_record" \
    --expected-pipeline-id "$CI_PIPELINE_ID" \
    --field RESULT)
classification=$("$python_bin" "$records" read-final \
    --input "$final_record" \
    --expected-pipeline-id "$CI_PIPELINE_ID" \
    --field RESULT_CLASSIFICATION)
sha=$("$python_bin" "$records" read-final \
    --input "$final_record" \
    --expected-pipeline-id "$CI_PIPELINE_ID" \
    --field PR_HEAD_SHA)

printf 'Final OpenPMIx PR result: SHA=%s result=%s classification=%s\n' \
    "$sha" "$result" "$classification"
