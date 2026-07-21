#!/bin/bash
set -euo pipefail

usage() {
    cat <<'EOF'
Usage: ci/discover_untested_pmix_commits.sh STATE_FILE OUTPUT_FILE [UPSTREAM_URL]

Write every OpenPMIx master commit after the saved PMIX_COMMIT to OUTPUT_FILE,
oldest to newest. UPSTREAM_URL defaults to the official OpenPMIx repository.
EOF
}

if (( $# < 2 || $# > 3 )); then
    usage >&2
    exit 2
fi

state_file=$1
output_file=$2
upstream_url=${3:-https://github.com/openpmix/openpmix.git}

if [[ ! -f $state_file ]]; then
    printf 'error: state file is missing: %s\n' "$state_file" >&2
    exit 1
fi

pmix_field_count=0
last_known_good=
while IFS= read -r state_line || [[ -n $state_line ]]; do
    if [[ $state_line == PMIX_COMMIT=* ]]; then
        pmix_field_count=$((pmix_field_count + 1))
        last_known_good=${state_line#PMIX_COMMIT=}
    fi
done < "$state_file"

if (( pmix_field_count == 0 )); then
    printf 'error: state file does not contain PMIX_COMMIT\n' >&2
    exit 1
fi
if (( pmix_field_count > 1 )); then
    printf 'error: state file contains multiple PMIX_COMMIT fields\n' >&2
    exit 1
fi
if [[ ! $last_known_good =~ ^[0-9A-Fa-f]{40}$ ]]; then
    printf 'error: saved PMIX_COMMIT must be exactly 40 hexadecimal characters\n' >&2
    exit 1
fi

state_path=$(realpath -e -- "$state_file") || {
    printf 'error: could not resolve state file: %s\n' "$state_file" >&2
    exit 1
}
output_path=$(realpath -m -- "$output_file") || {
    printf 'error: could not resolve output file: %s\n' "$output_file" >&2
    exit 1
}
if [[ $state_path == "$output_path" ]]; then
    printf 'error: output file must not be the state file\n' >&2
    exit 1
fi
if [[ -d $output_file ]]; then
    printf 'error: output file resolves to an existing directory: %s\n' \
        "$output_file" >&2
    exit 1
fi

output_dir=$(dirname -- "$output_file")
output_name=$(basename -- "$output_file")
if [[ ! -d $output_dir ]]; then
    printf 'error: output directory does not exist: %s\n' "$output_dir" >&2
    exit 1
fi
if ! output_tmp=$(mktemp "$output_dir/.${output_name}.tmp.XXXXXX"); then
    printf 'error: output file cannot be written: %s\n' "$output_file" >&2
    exit 1
fi

work_dir=$(mktemp -d)
history_repo="$work_dir/openpmix-history.git"
cleanup() {
    rm -f -- "$output_tmp"
    rm -rf -- "$work_dir"
}
trap cleanup EXIT

git init --bare -q "$history_repo"
if ! git --git-dir="$history_repo" fetch --quiet --no-tags "$upstream_url" \
    '+refs/heads/*:refs/remotes/upstream/*'; then
    printf 'error: could not fetch OpenPMIx master history from %s\n' \
        "$upstream_url" >&2
    exit 1
fi

master_ref=refs/remotes/upstream/master
if ! master_sha=$(git --git-dir="$history_repo" rev-parse --verify \
    "$master_ref^{commit}"); then
    printf 'error: fetched OpenPMIx history does not contain master\n' >&2
    exit 1
fi

if ! git --git-dir="$history_repo" cat-file -e \
    "$last_known_good^{commit}" 2>/dev/null; then
    printf 'error: last-known-good commit is absent from fetched history: %s\n' \
        "$last_known_good" >&2
    exit 1
fi
if ! git --git-dir="$history_repo" merge-base --is-ancestor \
    "$last_known_good" "$master_sha"; then
    printf 'error: last-known-good commit is not an ancestor of master: %s\n' \
        "$last_known_good" >&2
    exit 1
fi

git --git-dir="$history_repo" rev-list --reverse --topo-order \
    "$last_known_good..$master_sha" > "$output_tmp"

mapfile -t untested_commits < "$output_tmp"
for commit_sha in "${untested_commits[@]}"; do
    if [[ ! $commit_sha =~ ^[0-9a-f]{40}$ ]]; then
        printf 'error: git returned a malformed commit SHA: %s\n' \
            "$commit_sha" >&2
        exit 1
    fi
done

if ! mv -f -- "$output_tmp" "$output_file"; then
    printf 'error: output file cannot be written: %s\n' "$output_file" >&2
    exit 1
fi

printf 'last-known-good: %s\n' "$last_known_good" >&2
printf 'current master: %s\n' "$master_sha" >&2
printf 'untested commits: %d\n' "${#untested_commits[@]}" >&2
