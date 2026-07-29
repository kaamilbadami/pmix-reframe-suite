#!/bin/bash
# Fetch one OpenPMIx pull-request head only through the fixed base repository.
set +x
set -euo pipefail

usage() {
    printf 'usage: %s PR_NUMBER PREPARED_SHA CHECKOUT_DIRECTORY\n' \
        "${0##*/}" >&2
}

fail() {
    local status=$1
    shift
    printf 'error: %s\n' "$*" >&2
    exit "$status"
}

if (( $# != 3 && $# != 5 )); then
    usage
    exit 2
fi

pr_number=$1
prepared_sha=$2
checkout_dir=$3
shift 3

[[ $pr_number =~ ^[1-9][0-9]*$ ]] ||
    fail 2 'PR number must be a canonical positive integer'
[[ $prepared_sha =~ ^[0-9a-f]{40}$ ]] ||
    fail 2 'prepared SHA must be lowercase 40-character hexadecimal'
[[ -n $checkout_dir && $checkout_dir != / && $checkout_dir != /* &&
   $checkout_dir != . && $checkout_dir != .. &&
   $checkout_dir != ../* && $checkout_dir != */../* &&
   $checkout_dir != */.. && $checkout_dir != .ci-state/* &&
   $checkout_dir != */.ci-state/* ]] ||
    fail 2 'checkout directory must be a safe repository-relative path'

upstream_url=https://github.com/openpmix/openpmix.git
git_protocol_option=never
if (( $# == 2 )); then
    [[ $1 == --test-upstream ]] ||
        fail 2 'unsupported checkout option'
    [[ ${OPENPMIX_PR_CHECKOUT_TEST_MODE:-} == 1 ]] ||
        fail 2 'test upstream requires explicit test mode'
    test_upstream=$2
    [[ $test_upstream == /* && -e $test_upstream &&
       ! -L $test_upstream ]] ||
        fail 2 'test upstream must be an existing absolute local path'
    upstream_url=$test_upstream
    git_protocol_option=always
elif (( $# != 0 )); then
    usage
    exit 2
fi

if [[ -L $checkout_dir || -e $checkout_dir ]]; then
    fail 2 'checkout directory already exists'
fi
checkout_parent=${checkout_dir%/*}
if [[ $checkout_parent == "$checkout_dir" ]]; then
    checkout_parent=.
fi
[[ -d $checkout_parent && ! -L $checkout_parent ]] ||
    fail 2 'checkout parent is unavailable or unsafe'

export GIT_CONFIG_NOSYSTEM=1
export GIT_CONFIG_GLOBAL=/dev/null
export GIT_TERMINAL_PROMPT=0
export GIT_ASKPASS=/bin/false

pull_ref="refs/pull/${pr_number}/head"
local_ref="refs/remotes/origin/openpmix-pr-${pr_number}"

/usr/bin/git init -q -- "$checkout_dir"
/usr/bin/git -C "$checkout_dir" \
    -c credential.helper= \
    -c core.hooksPath=/dev/null \
    -c protocol.file.allow="$git_protocol_option" \
    remote add origin "$upstream_url"

fetch_status=0
/usr/bin/git -C "$checkout_dir" \
    -c credential.helper= \
    -c core.hooksPath=/dev/null \
    -c protocol.file.allow="$git_protocol_option" \
    -c http.followRedirects=false \
    fetch --no-tags --no-recurse-submodules origin \
    "+${pull_ref}:${local_ref}" || fetch_status=$?
if (( fetch_status != 0 )); then
    fail 6 "fixed OpenPMIx PR ref is unavailable: ${pull_ref}"
fi

fetched_sha=$(/usr/bin/git -C "$checkout_dir" rev-parse --verify \
    "${local_ref}^{commit}") ||
    fail 6 'fetched PR ref did not resolve to a commit'
[[ $fetched_sha == "$prepared_sha" ]] ||
    fail 5 'fetched PR ref does not match the prepared SHA'

/usr/bin/git -C "$checkout_dir" -c core.hooksPath=/dev/null \
    checkout --detach "$prepared_sha" -- ||
    fail 2 'could not create detached exact-SHA checkout'
checked_out_sha=$(/usr/bin/git -C "$checkout_dir" rev-parse --verify HEAD)
[[ $checked_out_sha == "$prepared_sha" ]] ||
    fail 2 'checked-out HEAD does not match the prepared SHA'
resolved_sha=$(/usr/bin/git -C "$checkout_dir" rev-parse --verify \
    "${prepared_sha}^{commit}")
[[ $resolved_sha == "$prepared_sha" ]] ||
    fail 2 'prepared SHA is not the exact checked-out commit'
if /usr/bin/git -C "$checkout_dir" symbolic-ref -q HEAD >/dev/null; then
    fail 2 'OpenPMIx checkout is attached to a branch'
fi

origin=$(/usr/bin/git -C "$checkout_dir" remote get-url origin)
[[ $origin == "$upstream_url" ]] ||
    fail 2 'checkout origin changed unexpectedly'

gitlink_count=$(
    /usr/bin/git -C "$checkout_dir" ls-files --stage |
        /usr/bin/awk '$1 == "160000" { count++ } END { print count + 0 }'
)
if (( gitlink_count != 0 )) ||
   /usr/bin/git -C "$checkout_dir" ls-files --error-unmatch \
       .gitmodules >/dev/null 2>&1; then
    fail 7 'OpenPMIx PR contains unauthorized submodule metadata or gitlinks'
fi

if [[ -n $(/usr/bin/git -C "$checkout_dir" status \
        --porcelain=v1 --untracked-files=all) ]]; then
    fail 2 'exact OpenPMIx checkout is not clean'
fi

printf 'OPENPMIX_PR_PULL_REF=%s\n' "$pull_ref"
printf 'OPENPMIX_PR_FETCHED_SHA=%s\n' "$fetched_sha"
printf 'OPENPMIX_PR_CHECKED_OUT_SHA=%s\n' "$checked_out_sha"
printf '%s\n' 'OPENPMIX_PR_DETACHED_HEAD=1'
printf '%s\n' 'OPENPMIX_PR_CLEAN_CHECKOUT=1'
printf '%s\n' 'OPENPMIX_PR_GITLINK_COUNT=0'
