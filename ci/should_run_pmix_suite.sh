#!/bin/bash
set -euo pipefail

upstream_url=https://github.com/openpmix/openpmix.git
upstream_ref=refs/heads/master
state_file=${PMIX_STATE_FILE:-.ci-state/pmix-master.env}
decision_file=${1:-${PMIX_DECISION_FILE:-.ci-state/pmix-decision.env}}
suite_sha=${CI_COMMIT_SHA:-}

if [[ ! $suite_sha =~ ^[0-9A-Fa-f]{40}$ ]]; then
    printf 'error: CI_COMMIT_SHA must be exactly 40 hexadecimal characters\n' >&2
    exit 1
fi

if ! remote_output=$(git ls-remote "$upstream_url" "$upstream_ref"); then
    printf 'error: could not query OpenPMIx master\n' >&2
    exit 1
fi

mapfile -t remote_lines <<< "$remote_output"
if (( ${#remote_lines[@]} != 1 )) ||
   [[ ! ${remote_lines[0]} =~ ^([[:xdigit:]]{40})[[:space:]]+refs/heads/master$ ]]; then
    printf 'error: expected exactly one valid SHA for OpenPMIx master\n' >&2
    exit 1
fi
current_sha=${BASH_REMATCH[1]}

pipeline_source=${CI_PIPELINE_SOURCE:-}
run_suite=1
run_reason=

case "$pipeline_source" in
    web)
        run_reason='manual web pipeline always runs the complete suite'
        ;;
    schedule)
        if [[ ! -f "$state_file" ]]; then
            run_reason='no previous successful suite state is available'
        else
            state_valid=1
            saved_pmix_sha=
            saved_suite_sha=
            saved_epoch=
            if ! mapfile -t state_lines < "$state_file"; then
                state_valid=0
            elif (( ${#state_lines[@]} != 3 )); then
                state_valid=0
            elif [[ ${state_lines[0]} =~ ^PMIX_COMMIT=([[:xdigit:]]{40})$ ]]; then
                saved_pmix_sha=${BASH_REMATCH[1]}
            else
                state_valid=0
            fi

            if (( state_valid )); then
                if [[ ${state_lines[1]} =~ ^SUITE_COMMIT=([0-9A-Fa-f]{40})$ ]]; then
                    saved_suite_sha=${BASH_REMATCH[1]}
                else
                    state_valid=0
                fi
            fi

            if (( state_valid )); then
                if [[ ${state_lines[2]} =~ ^LAST_SUCCESS_EPOCH=(0|[1-9][0-9]{0,18})$ ]]; then
                    saved_epoch=${BASH_REMATCH[1]}
                    if (( ${#saved_epoch} == 19 )) &&
                       [[ $saved_epoch > 9223372036854775807 ]]; then
                        state_valid=0
                    fi
                else
                    state_valid=0
                fi
            fi

            if (( ! state_valid )); then
                run_reason='previous successful suite state is invalid'
            elif [[ "$saved_pmix_sha" != "$current_sha" &&
                    "$saved_suite_sha" != "$suite_sha" ]]; then
                run_reason='OpenPMIx master and suite commit differ from the last successful complete run'
            elif [[ "$saved_pmix_sha" != "$current_sha" ]]; then
                run_reason='OpenPMIx master differs from the last successfully tested SHA'
            elif [[ "$saved_suite_sha" != "$suite_sha" ]]; then
                run_reason='suite commit differs from the last successfully tested SHA'
            else
                now_epoch=$(date -u +%s)
                saved_epoch_decimal=$((10#$saved_epoch))
                if (( saved_epoch_decimal > now_epoch )); then
                    run_reason='saved successful-run time is in the future'
                else
                    state_age=$((now_epoch - saved_epoch_decimal))
                    if (( state_age >= 86400 )); then
                        run_reason='at least 86400 seconds have passed since the last successful complete run'
                    else
                        run_suite=0
                        run_reason="OpenPMIx master and suite commit are unchanged and the last successful complete run is ${state_age} seconds old"
                    fi
                fi
            fi
        fi
        ;;
    *)
        printf 'error: unsupported CI_PIPELINE_SOURCE: %s\n' "$pipeline_source" >&2
        exit 1
        ;;
esac

decision_dir=$(dirname -- "$decision_file")
mkdir -p "$decision_dir"
decision_tmp=$(mktemp "${decision_file}.tmp.XXXXXX")
trap 'rm -f -- "$decision_tmp"' EXIT
{
    printf 'PMIX_COMMIT=%s\n' "$current_sha"
    printf 'PMIX_RUN_SUITE=%s\n' "$run_suite"
    printf 'PMIX_RUN_REASON=%q\n' "$run_reason"
} > "$decision_tmp"
mv -f -- "$decision_tmp" "$decision_file"
trap - EXIT

printf 'Current OpenPMIx master SHA: %s\n' "$current_sha"
printf 'Current suite commit SHA: %s\n' "$suite_sha"
if (( run_suite )); then
    printf 'Complete PMIx Python suite: run\n'
else
    printf 'Complete PMIx Python suite: skip\n'
fi
printf 'Reason: %s\n' "$run_reason"
