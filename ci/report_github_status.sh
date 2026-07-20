#!/bin/bash
set +x
set -euo pipefail

if (( $# != 2 )); then
    printf 'usage: %s STATE DESCRIPTION\n' "${0##*/}" >&2
    exit 2
fi

state=$1
description=$2

case "$state" in
    pending|success|failure|error)
        ;;
    *)
        printf 'error: unsupported GitHub status state\n' >&2
        exit 2
        ;;
esac

if [[ -z ${GITHUB_STATUS_TOKEN:-} ]]; then
    printf 'error: GITHUB_STATUS_TOKEN is required\n' >&2
    exit 2
fi
if [[ ! ${CI_COMMIT_SHA:-} =~ ^[0-9A-Fa-f]{40}$ ]]; then
    printf 'error: CI_COMMIT_SHA must be exactly 40 hexadecimal characters\n' >&2
    exit 2
fi
if [[ ! ${CI_PIPELINE_URL:-} =~ ^https?://[^[:space:]]+$ ]]; then
    printf 'error: CI_PIPELINE_URL must be a valid HTTP(S) URL\n' >&2
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
    '{"state":"%s","target_url":"%s","description":"%s","context":"olcf/frontier-pmix-master"}' \
    "$(json_escape "$state")" \
    "$(json_escape "$CI_PIPELINE_URL")" \
    "$(json_escape "$description")"

api_url="https://api.github.com/repos/kaamilbadami/pmix-reframe-suite/statuses/${CI_COMMIT_SHA}"

printf '%s\n' \
    'Accept: application/vnd.github+json' \
    "Authorization: Bearer ${GITHUB_STATUS_TOKEN}" \
    'X-GitHub-Api-Version: 2026-03-10' \
    'Content-Type: application/json' |
    curl --silent --show-error --fail \
        --output /dev/null \
        --request POST \
        --header @- \
        --data "$payload" \
        "$api_url"

printf 'Reported GitHub status %s for commit %s\n' "$state" "$CI_COMMIT_SHA"
