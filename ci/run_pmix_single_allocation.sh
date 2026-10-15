#!/bin/bash
# Opt-in, one exact commit. No authoritative-state writes.
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
repo=$(pwd -P)
[[ ${CI_JOB_ID:-} =~ ^[1-9][0-9]*$ && ${CI_PROJECT_DIR:-} == /* ]] || {
    echo 'error: run this pilot as a GitLab CI job' >&2; exit 2;
}
[[ $(cd -- "$CI_PROJECT_DIR" && pwd -P) == "$repo" ]] || exit 2
[[ ${PMIX_COMMIT:-} =~ ^[0-9a-f]{40}$ && ${PRRTE_COMMIT:-} =~ ^[0-9a-f]{40}$ ]] || {
    echo 'error: provide exact lowercase PMIX_COMMIT and compatible PRRTE_COMMIT pins' >&2; exit 2;
}
git check-ref-format "refs/heads/${PRRTE_BRANCH:-v5.0}" >/dev/null
export PRRTE_BRANCH=${PRRTE_BRANCH:-v5.0}
: "${PMIX_PYTHON:?}" "${RFM_BIN:?}"
export PMIX_JOB_ROOT="$repo/.ci-work/$CI_JOB_ID"
export PMIX_RFM_PREFIX="$PMIX_JOB_ROOT/reframe"
export PMIX_RFM_REPORT_FILE="$PMIX_JOB_ROOT/report.json"
export PMIX_RFM_CONFIG_FILE="$repo/sysconfig-single-allocation.py"
export PMIX_RFM_EXEC_POLICY=serial
# Source preparation must finish on the login node before any allocation request.
export PMIX_PILOT_OFFLINE_SOURCES="$PMIX_JOB_ROOT/sources"
"$PMIX_PYTHON" ci/pmix_pilot_sources.py prepare "$PMIX_PILOT_OFFLINE_SOURCES"
allocation="$PMIX_RFM_PREFIX/stage/single-allocation"
mkdir -p -- "$allocation"
# This exact stage/command scope is recognized by the existing cleanup helper.
[[ ! -e "$allocation/rfm_job.sh" ]] || {
    echo 'error: allocation script already exists; refusing duplicate submission' >&2; exit 2;
}
{
    printf '#!/bin/bash\nset -eo pipefail\n'
    printf '. /etc/profile.d/olcf-env.sh && . /etc/bash.bashrc.local\n'
    printf 'module load miniforge3/23.11.0-0\nset -u\n'
    printf 'cd -- %q\n' "$repo"
    printf 'export PATH=%q:"$PATH"\n' "$(dirname -- "$PMIX_PYTHON")"
    printf 'exec bash ci/run_exact_pmix_commit.sh\n'
} > "$allocation/rfm_job.sh"
chmod 700 "$allocation/rfm_job.sh"
wait_pid=''
log_pid=''
cleanup() {
    rc=$?
    trap - EXIT INT TERM
    [[ -z $log_pid ]] || kill "$log_pid" 2>/dev/null || true
    if (( rc != 0 )); then
        CI_JOB_STATUS=failed python3 ci/cleanup_pmix_slurm_jobs.py || true
        [[ -z $wait_pid ]] || kill "$wait_pid" 2>/dev/null || true
    fi
    exit "$rc"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
: > "$allocation/allocation.out"
: > "$allocation/allocation.err"
tail -n +1 -F "$allocation/allocation.out" "$allocation/allocation.err" &
log_pid=$!
printf 'Requesting four nodes for one commit; allocated limit 45m. Queue wait counts toward CI script limit.\n'
# --wait propagates a failed batch exit, including Slurm time-limit failure.
# Explicit absolute paths permit cleanup even if submission is interrupted.
sbatch --wait --parsable --account=gen243 --partition=batch \
    --nodes=4 --ntasks=128 --ntasks-per-node=32 --exclusive \
    --time=00:45:00 --export=ALL --job-name="pmix-pilot-$CI_JOB_ID" \
    --chdir="$allocation" --output="$allocation/allocation.out" \
    --error="$allocation/allocation.err" "$allocation/rfm_job.sh" &
wait_pid=$!
rc=0
wait "$wait_pid" || rc=$?
wait_pid=''
kill "$log_pid" 2>/dev/null || true
wait "$log_pid" 2>/dev/null || true
log_pid=''
printf '\nFinal allocation output:\n'
cat "$allocation/allocation.out" "$allocation/allocation.err"
exit "$rc"
