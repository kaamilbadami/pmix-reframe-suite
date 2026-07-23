#!/bin/bash
# Scrub the GitLab job environment before trusted-author PR execution.
#
# This is credential hygiene, not an OS sandbox.  Only approved same-repository
# authors are eligible, and their code is trusted under the Frontier service
# account for this MVP.  Supporting arbitrary PR code would require a separate
# account, container boundary, or equivalent stronger isolation.
set +x
set -euo pipefail

if (( $# != 0 )); then
    printf 'usage: %s\n' "${0##*/}" >&2
    exit 2
fi

script_dir=$(cd -- "${BASH_SOURCE[0]%/*}" && pwd -P)
repo_root=$(cd -- "$script_dir/.." && pwd -P)
home_dir=$repo_root/.ci-pr-execution-home
tmp_dir=$repo_root/.ci-pr-execution-tmp
output_dir=$repo_root/ci-pr-execution

if [[ ${PMIX_TESTS_PR_SANITIZED_STAGE:-} != 1 ]]; then
    # Clear the always-uploaded result location before any clean-boundary setup
    # can fail.  rm removes a symlink entry itself and does not follow it.
    /usr/bin/rm -rf --one-file-system -- "$output_dir"
    if [[ -L $output_dir || -e $output_dir ]]; then
        printf '%s\n' 'error: could not remove previous execution output' >&2
        exit 2
    fi
    [[ ${CI_PIPELINE_ID:-} =~ ^[1-9][0-9]*$ ]] || {
        printf '%s\n' 'error: CI_PIPELINE_ID must be a canonical positive integer' >&2
        exit 2
    }
    for path in "$home_dir" "$tmp_dir"; do
        if [[ -L $path || -e $path ]]; then
            printf 'error: isolated environment path already exists: %s\n' "$path" >&2
            exit 2
        fi
        /usr/bin/mkdir -m 700 -- "$path"
    done

    # No caller-provided PATH, loader path, module namespace, CI variable,
    # proxy, credential helper, JWT/OIDC variable, or project/group variable
    # crosses this boundary.  Module state is regenerated only after env -i
    # from trusted, fixed Frontier configuration.
    exec /usr/bin/env -i \
        "HOME=$home_dir" \
        "TMPDIR=$tmp_dir" \
        'PATH=/usr/bin:/bin' \
        'LANG=C.UTF-8' \
        'LC_ALL=C.UTF-8' \
        'SHELL=/bin/bash' \
        'USER=gitlab-ci' \
        'LOGNAME=gitlab-ci' \
        'MODULEPATH=/opt/cray/pe/lmod/modulefiles/core:/opt/cray/pe/lmod/modulefiles/craype-targets/default:/opt/cray/pe/modulefiles/Core:/sw/frontier/modulefiles:/opt/cray/modulefiles' \
        "CI_PIPELINE_ID=$CI_PIPELINE_ID" \
        'PMIX_TESTS_PR_SANITIZED_STAGE=1' \
        /bin/bash --noprofile --norc "$script_dir/run_pmix_tests_pr_isolated.sh"
fi

# This code runs only after env -i.  Fixed Lmod initialization and exact
# Frontier programming-environment modules are loaded without sourcing user or
# site login profiles and without exposing the original GitLab job environment.
# The workload and ReFrame executables themselves are fixed, existing installs;
# this workflow performs no package download or installation.
# shellcheck disable=SC1091
source /opt/cray/pe/lmod/lmod/init/bash
module load \
    craype-x86-trento \
    PrgEnv-cray/8.6.0 \
    miniforge3/23.11.0-0

export HOME=$home_dir
export TMPDIR=$tmp_dir
export LANG=C.UTF-8
export LC_ALL=C.UTF-8
export SHELL=/bin/bash
export USER=gitlab-ci
export LOGNAME=gitlab-ci
export PMIX_PYTHON=/lustre/orion/scratch/kbadami/gen243/reframe_practice/pmix-py310/bin/python
export RFM_BIN=/ccs/home/kbadami/.local/bin/reframe
export PYTHONPATH=/ccs/home/kbadami/.local/lib/python3.11/site-packages
unset PMIX_TESTS_PR_SANITIZED_STAGE

[[ -x $PMIX_PYTHON && -x $RFM_BIN ]] || {
    printf '%s\n' 'error: fixed Frontier Python or ReFrame executable is unavailable' >&2
    exit 2
}

exec /bin/bash "$script_dir/run_trusted_pmix_tests_pr.sh"
