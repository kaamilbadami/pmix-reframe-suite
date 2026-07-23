#!/bin/bash
set +x
set -euo pipefail

if (( $# != 3 )); then
    printf 'usage: %s PR_HEAD_SHA STATE DESCRIPTION\n' "${0##*/}" >&2
    exit 2
fi

pr_head_sha=$1
state=$2
description=$3

if [[ ! $pr_head_sha =~ ^[0-9a-f]{40}$ ]]; then
    printf '%s\n' 'error: PR_HEAD_SHA must be a canonical lowercase 40-character SHA' >&2
    exit 2
fi

case "$state" in
    pending|success|failure|error)
        ;;
    *)
        printf '%s\n' 'error: unsupported GitHub status state' >&2
        exit 2
        ;;
esac

if [[ -z ${GITHUB_STATUS_TOKEN:-} ]]; then
    printf '%s\n' 'error: GITHUB_STATUS_TOKEN is required' >&2
    exit 2
fi
if [[ ! ${CI_PIPELINE_URL:-} =~ ^https://[^/[:space:]]+/.+/-/pipelines/[1-9][0-9]*$ ]]; then
    printf '%s\n' 'error: CI_PIPELINE_URL must be a valid HTTPS GitLab pipeline URL' >&2
    exit 2
fi

json_escape() {
    local value=$1
    local escaped=
    local character
    local character_code
    local encoded
    local index

    LC_ALL=C
    for (( index = 0; index < ${#value}; index++ )); do
        character=${value:index:1}
        case "$character" in
            '"'|\\)
                escaped+="\\$character"
                ;;
            *)
                printf -v character_code '%d' "'$character"
                if (( character_code < 32 )); then
                    printf -v encoded '\\u%04x' "$character_code"
                    escaped+=$encoded
                else
                    escaped+=$character
                fi
                ;;
        esac
    done
    printf '%s' "$escaped"
}

printf -v payload \
    '{"state":"%s","target_url":"%s","description":"%s","context":"olcf/frontier-pmix-tests-pr"}' \
    "$(json_escape "$state")" \
    "$(json_escape "$CI_PIPELINE_URL")" \
    "$(json_escape "$description")"

api_url="https://api.github.com/repos/kaamilbadami/pmix-tests/statuses/${pr_head_sha}"

printf '%s\n' \
    'Accept: application/vnd.github+json' \
    "Authorization: Bearer ${GITHUB_STATUS_TOKEN}" \
    'X-GitHub-Api-Version: 2026-03-10' \
    'Content-Type: application/json' |
    /usr/bin/curl --silent --show-error --fail \
        --max-redirs 0 \
        --output /dev/null \
        --request POST \
        --header @- \
        --data "$payload" \
        "$api_url"

printf 'Reported PMIx tests PR status %s for commit %s\n' \
    "$state" "$pr_head_sha"
