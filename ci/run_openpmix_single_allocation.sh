#!/bin/bash
# Fixed historical baseline only; called after the PR credential boundary.
set +x
set -euo pipefail
umask 077
cd -- "${BASH_SOURCE[0]%/*}/.."
repo=$(pwd -P)
[[ ${CI_PIPELINE_ID:-} =~ ^[1-9][0-9]*$ ]] || exit 2
[[ $("$PMIX_PYTHON" ci/openpmix_pr_baseline.py select --pipeline-id "$CI_PIPELINE_ID") != live-pr ]] || exit 2
for key in PMIX_PYTHON RFM_BIN PMIX_COMMIT PRRTE_COMMIT PMIX_TRUSTED_SOURCE_DIR PMIX_RFM_PREFIX PMIX_RFM_REPORT_FILE; do
    [[ -n ${!key:-} ]] || { printf 'error: missing %s\n' "$key" >&2; exit 2; }
done
[[ $PMIX_COMMIT =~ ^[0-9a-f]{40}$ && $PRRTE_COMMIT =~ ^[0-9a-f]{40}$ ]] || exit 2
[[ $PMIX_RFM_PREFIX == "$repo/ci-openpmix-pr-execution/reframe" ]] || exit 2
if [[ ${OPENPMIX_RUNNER_ALLOCATION:-} == 1 ]]; then
    "$PMIX_PYTHON" ci/openpmix_baseline_staging.py record
    export PMIX_SINGLE_OFFLINE_SOURCES="$repo/ci-openpmix-baseline-restored/sources/cache"
    export PMIX_RFM_CONFIG_FILE="$repo/ci/openpmix_single_allocation_config.py"
    export PMIX_RFM_EXEC_POLICY=serial
    export SLURM_EXPORT_ENV=ALL
    printf 'Using the existing four-node Jacamar allocation; no nested sbatch.\n'
    exec /bin/bash ci/run_exact_pmix_commit.sh
fi
export PMIX_SINGLE_OFFLINE_SOURCES="$repo/ci-openpmix-pr-execution/sources"
"$PMIX_PYTHON" ci/pmix_single_sources.py prepare "$PMIX_SINGLE_OFFLINE_SOURCES"
allocation="$PMIX_RFM_PREFIX/stage/single-allocation-$CI_PIPELINE_ID"
mkdir -p -- "$allocation"
[[ ! -e $allocation/rfm_job.sh && ! -L $allocation/rfm_job.sh ]] || exit 2
export PMIX_RFM_CONFIG_FILE="$repo/ci/openpmix_single_allocation_config.py"
export PMIX_RFM_EXEC_POLICY=serial
{
    printf '#!/bin/bash\nset -eo pipefail\n'
    # Use the same checked site initialization as ordinary PR execution, but
    # leave per-test node-local scratch creation to the ReFrame configuration.
    "$PMIX_PYTHON" - <<'PY'
import yaml
with open('ci/openpmix_pr_sysconfig.yaml') as stream:
    commands = yaml.safe_load(stream)['systems'][0]['partitions'][0]['prepare_cmds'][0]
print(commands.split('if [[ -n ${SLURM_JOB_ID:-} ]]; then', 1)[0])
PY
    printf 'module load craype-x86-trento PrgEnv-cray/8.6.0 miniforge3/23.11.0-0\n'
    printf 'cd -- %q\n' "$repo"
    # Explicit allowlist. No CI job token, GitHub token, or caller variables are
    # exported to Slurm. --export=NIL also avoids submitting the caller's env.
    for key in HOME TMPDIR PMIX_PYTHON RFM_BIN PYTHONPATH PMIX_COMMIT PRRTE_BRANCH PRRTE_COMMIT PMIX_TRUSTED_SOURCE_DIR PMIX_RFM_CONFIG_FILE PMIX_RFM_PREFIX PMIX_RFM_REPORT_FILE PMIX_RFM_EXEC_POLICY PMIX_SINGLE_OFFLINE_SOURCES; do
        printf 'export %s=%q\n' "$key" "${!key}"
    done
    printf 'export PATH=%q:"$PATH"\n' "$(dirname -- "$PMIX_PYTHON")"
    printf 'export SLURM_EXPORT_ENV=ALL\n'
    printf 'exec /bin/bash ci/run_exact_pmix_commit.sh\n'
} > "$allocation/rfm_job.sh"
chmod 700 "$allocation/rfm_job.sh"
wait_pid=''
monitor_pid=''
log_pid=''
stop_monitor() {
    if [[ -n $monitor_pid ]]; then
        kill -TERM "$monitor_pid" 2>/dev/null || true
        wait "$monitor_pid" || true
        monitor_pid=''
    fi
}
finish() {
    rc=$?
    trap - EXIT INT TERM
    if (( rc != 0 )); then
        "$PMIX_PYTHON" ci/cleanup_openpmix_allocation.py || true
        [[ -z $wait_pid ]] || kill "$wait_pid" 2>/dev/null || true
    fi
    stop_monitor
    [[ -z $log_pid ]] || kill "$log_pid" 2>/dev/null || true
    exit "$rc"
}
trap finish EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
: > "$allocation/allocation.out"
: > "$allocation/allocation.err"
: > "$allocation/submission.out"
tail -n +1 -F "$allocation/allocation.out" "$allocation/allocation.err" &
log_pid=$!
printf 'Requesting ONE four-node allocation, 45-minute Slurm limit; full suite runs serially.\n'
sbatch --wait --parsable --account=gen243 --partition=batch \
    --nodes=4 --ntasks=256 --ntasks-per-node=64 --exclusive \
    --core-spec=0 --threads-per-core=1 --time=00:45:00 --export=NIL \
    --job-name="pmix-baseline-$CI_PIPELINE_ID" \
    --chdir="$allocation" --output="$allocation/allocation.out" \
    --error="$allocation/allocation.err" "$allocation/rfm_job.sh" > "$allocation/submission.out" &
wait_pid=$!
"$PMIX_PYTHON" ci/monitor_pmix_allocation.py "$allocation" &
monitor_pid=$!
rc=0
wait "$wait_pid" || rc=$?
wait_pid=''
stop_monitor
kill "$log_pid" 2>/dev/null || true
wait "$log_pid" 2>/dev/null || true
log_pid=''
cat "$allocation/allocation.out" "$allocation/allocation.err"
exit "$rc"
