#!/bin/bash
# Compare one historical failing check; no checkpoint or upstream status writes.
set +x
set -euo pipefail
umask 077
cd -- "${BASH_SOURCE[0]%/*}/.."
[[ ${CI_JOB_ID:-} =~ ^[1-9][0-9]*$ && ${CI_PIPELINE_ID:-} =~ ^[1-9][0-9]*$ ]]
[[ ${CI_COMMIT_SHA:-} =~ ^[0-9a-f]{40}$ ]]
root="$PWD/.ci-work/$CI_JOB_ID"
[[ ! -e $root && ! -L $root ]]
mkdir -p "$root/home" "$root/tmp"
module load miniforge3/23.11.0-0
python3 -m venv "$root/venv"
source "$root/venv/bin/activate"
python -m pip install 'Cython==3.2.6' 'reframe-hpc==4.10.0' PyYAML
# Python constructs allowlisted environments for all source/build/test commands.
exec "$root/venv/bin/python" ci/openpmix_environment_compare.py run
