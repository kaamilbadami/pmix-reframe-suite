#!/bin/bash
# Run only the fixed historical multinode diagnostic, without CI credentials.
set +x
set -euo pipefail
umask 077
cd -- "${BASH_SOURCE[0]%/*}/.."
repo=$(pwd -P)
python=/lustre/orion/gen243/proj-shared/pmix-reframe-ci-tools/pmix-py310/bin/python
for key in CI_JOB_ID CI_PIPELINE_ID; do
    [[ ${!key:-} =~ ^[1-9][0-9]*$ ]] || exit 2
done
[[ ${CI_COMMIT_SHA:-} =~ ^[0-9a-f]{40}$ && -x $python ]] || exit 2
root="$repo/.ci-work/$CI_JOB_ID"
if [[ ${1:-} != --clean ]]; then
    (( $# == 0 )) || exit 2
    [[ ! -e $root && ! -L $root ]] || exit 2
    mkdir -p -m 700 -- "$root/home" "$root/tmp"
    exec /usr/bin/env -i PATH=/usr/bin:/bin LANG=C.UTF-8 LC_ALL=C.UTF-8 \
        HOME="$root/home" TMPDIR="$root/tmp" \
        CI_PROJECT_DIR="$repo" CI_JOB_ID="$CI_JOB_ID" \
        CI_PIPELINE_ID="$CI_PIPELINE_ID" CI_COMMIT_SHA="$CI_COMMIT_SHA" \
        /bin/bash --noprofile --norc ci/run_openpmix_fixed_diagnostic.sh --clean
fi
(( $# == 1 )) || exit 2
set +eu
. /etc/profile.d/olcf-env.sh
olcf_status=$?
. /etc/bash.bashrc.local
cray_status=$?
set -euo pipefail
(( olcf_status == 0 && cray_status == 0 )) || exit 2
module load craype-x86-trento PrgEnv-cray/8.6.0 miniforge3/23.11.0-0
export HOME="$root/home" TMPDIR="$root/tmp"
export PMIX_PYTHON=$python
export RFM_BIN=/lustre/orion/gen243/proj-shared/pmix-reframe-ci-tools/reframe-4.10/bin/reframe
export PYTHONPATH=/lustre/orion/gen243/proj-shared/pmix-reframe-ci-tools/reframe-4.10/lib/python3.11/site-packages
export PATH="$(dirname -- "$python"):$PATH"
export GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_GLOBAL=/dev/null GIT_TERMINAL_PROMPT=0 GIT_ASKPASS=/bin/false
"$python" ci/openpmix_fixed_diagnostic.py run 2>&1 | tee "$root/diagnostic.log"
