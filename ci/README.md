# GitLab CI on Frontier

This pipeline builds OpenPMIx and runs our 11-check PMIx Python ReFrame suite on Frontier. Scheduled testing tracks new commits; manual runs let us check the suite without changing the saved checkpoint.

## How the scheduled pipeline works

1. Acquire the GitLab `pmix-scheduled-batch` resource group, then read the saved last-known-good OpenPMIx commit.
2. Find every newer commit on OpenPMIx master, oldest first.
3. Run a separate test job for each exact commit. Jobs use separate workspaces and can run in parallel.
4. Collect results and advance last-known-good only through consecutive successful commits.

For example, if **A and B pass, C fails, and D passes**, state advances to **B**. The next schedule retries from C. Missing or invalid results also stop advancement. With no new commits, state stays unchanged. The schedule starts a wrapper pipeline that holds this lock until its five-job orchestration child completes, including state application. New scheduled pipelines wait before discovery, then read fresh state. Commit jobs inside each batch remain parallel. This uses two child levels: orchestration, then exact-commit jobs. It does not serialize manual or PR testing.

## Last-known-good state

The authoritative checkpoint is this shared Frontier file:

```text
/lustre/orion/gen243/world-shared/kbadami/openpmix-ci/pmix-master.env
```

| Field | Meaning |
|---|---|
| `PMIX_COMMIT` | Last OpenPMIx commit reached through consecutive successful results. |
| `SUITE_COMMIT` | Suite commit that recorded that boundary. |
| `LAST_SUCCESS_EPOCH` | Time the boundary was recorded, as a UTC Unix timestamp. |

To view it on Frontier:

```bash
cat /lustre/orion/gen243/world-shared/kbadami/openpmix-ci/pmix-master.env
```

Only the scheduled state-update job changes this file. It locks and rechecks state before replacing it, preventing an older pipeline from overwriting newer state. Missing or malformed state causes an error. GitLab cache is not the authoritative checkpoint; manual and PR runs do not advance it.

## Relevant CI variables

Normal runs do not require development-pilot switches. CI supplies the job identifiers, tool paths, and exact OpenPMIx SHA for each generated commit job.

| Variable | What it does during a run |
|---|---|
| `PMIX_AUTHORITATIVE_STATE_FILE` | Selects the scheduled checkpoint; defaults to the Frontier path above. |
| `OLCF_SERVICE_ACCOUNT` | Selects the Frontier execution account through runner configuration; defaults to `gen243_auser`. |
| `PMIX_COMMIT` | Identifies the exact OpenPMIx commit being tested. Scheduled jobs set it individually. The normal manual web run resolves master itself rather than honoring a supplied pin. |
| `PRRTE_BRANCH`, `PRRTE_COMMIT` | Select the PRRTE source used with OpenPMIx. Multi-commit generators select an exact compatible pin per job; see the compatibility section below. |
| `GITHUB_PR_READ_TOKEN` | Reads PR information for PR workflows; ordinary scheduled commit discovery does not need it. |
| `GITHUB_STATUS_TOKEN` | Posts GitHub status in supported workflows. Scheduled multi-commit testing and internal OpenPMIx PR testing do not post GitHub status. |

Tokens belong in GitLab CI/CD variables, never in this README. The observed project tokens are protected, masked, and hidden; the selected branch must qualify to receive protected variables.

## Manual runs and results

To run the full suite manually, choose **Build → Pipelines → New pipeline**, select the suite branch, and run without extra workflow switches. This tests the resolved OpenPMIx master commit without updating scheduled state. Push and merge-request pipelines are not enabled by the current configuration.

Open the pipeline's commit jobs for test logs and artifacts. Each generated commit job publishes `ci-results/<sha>.env`. Collection/reconciliation reports explain the successful boundary and any blocking result. Artifacts are generally retained for 14 days.

Generated commit jobs have a three-hour CI limit. `RUNNER_SCRIPT_TIMEOUT=2h45m`
reserves time for `RUNNER_AFTER_SCRIPT_TIMEOUT=5m`, result records, cleanup, and
artifact upload. Slurm queue waiting counts toward CI time, but each test's
Slurm execution limit remains unchanged. A runner maximum timeout can cap the
configured limit; verify the effective timeout in GitLab before re-enabling.

On failure or cancellation, `cleanup_pmix_slurm_jobs.py` lists allocations owned
by the executing UID and cancels only jobs whose working directory and batch
script resolve inside that CI job's `.ci-work/<CI_JOB_ID>/reframe/stage/` tree.
It retains `ci-results/slurm-cleanup-<CI_JOB_ID>.json`; successful jobs skip
cleanup. No account-wide cancellation is used. Runner failures or forced kills
can prevent `after_script` from running, so cleanup is best effort, not a
replacement for operator checks. Older leftover jobs need separate cleanup.

Before rolling out this configuration, pause the schedule and let existing
batches finish or cancel them and clean up their verified Slurm allocations.
Older pipelines do not acquire the new batch lock. Then resume the schedule
and verify the first wrapper, orchestration child, and commit child in GitLab.

If runners are disabled, pause the schedule to avoid accumulating waiting pipelines. Pausing does not cancel existing pipelines or erase saved state.

PR testing is under development and will be documented separately once validated.
Automatic OpenPMIx PR discovery remains opt-in (`OPENPMIX_PR_AUTODISCOVERY=1`).
It requires job-token access to the pipeline-list API (`READ_PIPELINES` where
fine-grained permissions are used). It identifies PR heads by child pipeline
names, suppresses active or successful attempts, and retries failed, canceled,
or skipped attempts. Pipelines created before this naming convention are not
recognized and can be tested again. Overlapping discovery runs can still launch
duplicates; validate live API access and address that race before enabling it.
OpenPMIx PR execution selects a compatible exact PRRTE pin from the function declaration in its verified source checkout; see the compatibility section below.

Configuration: [`.gitlab-ci.yml`](../.gitlab-ci.yml), [`openpmix_pr_jobs.yml`](openpmix_pr_jobs.yml), and [`run_pmix_python_suite.sh`](run_pmix_python_suite.sh). See the [root README](../README.md) for installation and test coverage. Development-only pilots are outside this guide.


### PRRTE compatibility for exact OpenPMIx commits

Scheduled and manual multi-commit generators use `--select-prrte` to set
`PRRTE_BRANCH` and `PRRTE_COMMIT` separately for each child job. Complete
OpenPMIx Git ancestry selects `v5.0` / `22820a01e17547dbf1c4f9628eac327f193caa45`
for commits without the `1244a4498895f07f0b1a6e2d81c97d4104c2e12b` change,
and `master` / `a783dab5948c76bca5bde1647f291d9492f33fca` for that commit
and its descendants. This handles the two-argument `pmix_getline` change.
The newer pairing passed all 17 Frontier checks on `1244a449`, `dba155a5`,
and `effa2f89` on October 11, 2026. It does not guarantee compatibility
with future upstream changes.

Selection fetches official OpenPMIx master history once per nonempty generator
run. Missing commits or shallow history stop generation rather than guessing.
For local checks, pass `--pmix-history PATH` with `--select-prrte` to use a
complete existing OpenPMIx repository. Parent triggers do not forward fixed PRRTE pins that would override the
per-commit selections. Explicit pipeline-level PRRTE overrides should not be
supplied for automatic compatibility runs.

OpenPMIx PR execution selects an exact PRRTE pin from the function declaration
in the exact PR checkout. Unknown declarations fail before the build. Scheduled
commit jobs retain the multinode scaling stage, including PRRTE launch logs,
for investigation when ReFrame output is incomplete.
