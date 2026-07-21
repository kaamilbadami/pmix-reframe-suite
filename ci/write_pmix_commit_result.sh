#!/bin/bash
set -euo pipefail

if [[ $# -ne 1 ]]; then
    printf 'usage: write_pmix_commit_result.sh OUTPUT_DIRECTORY\n' >&2
    exit 2
fi

output_dir=$1

if [[ ! ${PMIX_COMMIT+x} ]] ||
   [[ ! $PMIX_COMMIT =~ ^[0-9A-Fa-f]{40}$ ]]; then
    printf 'error: PMIX_COMMIT must be exactly 40 hexadecimal characters\n' >&2
    exit 2
fi

if [[ ! ${CI_COMMIT_SHA+x} ]] ||
   [[ ! $CI_COMMIT_SHA =~ ^[0-9A-Fa-f]{40}$ ]]; then
    printf 'error: CI_COMMIT_SHA must be exactly 40 hexadecimal characters\n' >&2
    exit 2
fi

if [[ ! ${CI_JOB_ID+x} ]] || [[ ! $CI_JOB_ID =~ ^[1-9][0-9]*$ ]]; then
    printf 'error: CI_JOB_ID must be a positive decimal integer\n' >&2
    exit 2
fi

if [[ ! ${CI_PIPELINE_ID+x} ]] ||
   [[ ! $CI_PIPELINE_ID =~ ^[1-9][0-9]*$ ]]; then
    printf 'error: CI_PIPELINE_ID must be a positive decimal integer\n' >&2
    exit 2
fi

pmix_commit=${PMIX_COMMIT,,}
suite_commit=${CI_COMMIT_SHA,,}

case ${CI_JOB_STATUS:-} in
    success|failed|canceled)
        job_status=$CI_JOB_STATUS
        ;;
    *)
        job_status=unknown
        ;;
esac

if [[ (-e $output_dir || -L $output_dir) && ! -d $output_dir ]]; then
    printf 'error: output path is not a directory: %s\n' "$output_dir" >&2
    exit 1
fi

mkdir -p -- "$output_dir"
output_file="$output_dir/$pmix_commit.env"
temporary_file=$(mktemp "$output_dir/.${pmix_commit}.env.tmp.XXXXXX")
trap 'rm -f -- "$temporary_file"' EXIT

printf 'PMIX_COMMIT=%s\nSUITE_COMMIT=%s\nCI_JOB_STATUS=%s\nCI_JOB_ID=%s\nCI_PIPELINE_ID=%s\n' \
    "$pmix_commit" \
    "$suite_commit" \
    "$job_status" \
    "$CI_JOB_ID" \
    "$CI_PIPELINE_ID" > "$temporary_file"

mv -f -- "$temporary_file" "$output_file"
trap - EXIT
