#!/bin/bash
# Reuse the baseline's offline, serial execution inside a Jacamar allocation.
set +x
set -euo pipefail
umask 077
cd -- "${BASH_SOURCE[0]%/*}/.."
repo=$(pwd -P)
python=/lustre/orion/gen243/proj-shared/pmix-reframe-ci-tools/pmix-py310/bin/python

for key in PMIX_COMMIT PRRTE_COMMIT; do
    [[ ${!key:-} =~ ^[0-9a-f]{40}$ ]] || exit 2
done
[[ ${CI_JOB_ID:-} =~ ^[1-9][0-9]*$ && ${CI_PIPELINE_ID:-} =~ ^[1-9][0-9]*$ ]] || exit 2
[[ ${PRRTE_BRANCH:-} == master && -x $python ]] || exit 2
root="$repo/.ci-work/$CI_JOB_ID"

if [[ ${1:-} != --clean ]]; then
    (( $# == 0 )) || exit 2
    identity=$(/usr/bin/env -i PATH=/usr/bin:/bin LANG=C.UTF-8 \
        CI_JOB_ID="$CI_JOB_ID" CI_PIPELINE_ID="$CI_PIPELINE_ID" \
        SLURM_JOB_ID="${SLURM_JOB_ID:-}" \
        "$python" ci/pmix_scheduled_sources.py allocation)
    read -r allocation_id allocation_nodes <<< "$identity"
    [[ $allocation_id =~ ^[1-9][0-9]*$ && -n $allocation_nodes ]] || exit 2
    mkdir -m 700 -- "$root/home" "$root/tmp"
    exec /usr/bin/env -i PATH=/usr/bin:/bin LANG=C.UTF-8 LC_ALL=C.UTF-8 \
        HOME="$root/home" TMPDIR="$root/tmp" \
        PMIX_COMMIT="$PMIX_COMMIT" PRRTE_COMMIT="$PRRTE_COMMIT" PRRTE_BRANCH=master \
        CI_JOB_ID="$CI_JOB_ID" CI_PIPELINE_ID="$CI_PIPELINE_ID" \
        SLURM_JOB_ID="$allocation_id" SLURM_JOBID="$allocation_id" \
        SLURM_JOB_NODELIST="$allocation_nodes" SLURM_NODELIST="$allocation_nodes" \
        SLURM_NNODES=4 SLURM_JOB_NUM_NODES=4 SLURM_NTASKS=256 \
        'SLURM_TASKS_PER_NODE=64(x4)' SLURM_EXPORT_ENV=ALL \
        /bin/bash --noprofile --norc ci/run_pmix_scheduled_allocation.sh --clean
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
export PMIX_PYTHON=$python
export RFM_BIN=/lustre/orion/gen243/proj-shared/pmix-reframe-ci-tools/reframe-4.10/bin/reframe
export PYTHONPATH=/lustre/orion/gen243/proj-shared/pmix-reframe-ci-tools/reframe-4.10/lib/python3.11/site-packages
export PATH="$(dirname -- "$python"):$PATH"
[[ -x $RFM_BIN ]] || exit 2
"$python" ci/pmix_scheduled_sources.py restore
export PMIX_TRUSTED_SOURCE_DIR="$repo/ci-scheduled-restored/sources/openpmix"
export PMIX_SINGLE_OFFLINE_SOURCES="$repo/ci-scheduled-restored/sources/cache"
export PMIX_RFM_CONFIG_FILE="$repo/ci/openpmix_single_allocation_config.py"
export PMIX_RFM_EXEC_POLICY=serial
export PMIX_RFM_PREFIX="$root/reframe"
export PMIX_RFM_REPORT_FILE="$root/reframe/run-report.json"
exec /bin/bash ci/run_exact_pmix_commit.sh
