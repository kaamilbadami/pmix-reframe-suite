# GitLab CI on Frontier

This pipeline builds OpenPMIx and runs our 11-check PMIx Python ReFrame suite on Frontier. Scheduled testing tracks new commits; manual runs let us check the suite without changing the saved checkpoint.

## How the scheduled pipeline works

1. Read the saved last-known-good OpenPMIx commit.
2. Find every newer commit on OpenPMIx master, oldest first.
3. Run a separate test job for each exact commit. Jobs use separate workspaces and can run in parallel.
4. Collect results and advance last-known-good only through consecutive successful commits.

For example, if **A and B pass, C fails, and D passes**, state advances to **B**. The next schedule retries from C. Missing or invalid results also stop advancement. With no new commits, state stays unchanged.

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
| `PRRTE_BRANCH`, `PRRTE_COMMIT` | Select the PRRTE source used with OpenPMIx. Scheduled commit jobs currently pin branch `v5.0` at `22820a01e17547dbf1c4f9628eac327f193caa45`. |
| `GITHUB_PR_READ_TOKEN` | Reads PR information for PR workflows; ordinary scheduled commit discovery does not need it. |
| `GITHUB_STATUS_TOKEN` | Posts GitHub status in supported workflows. Scheduled multi-commit testing and internal OpenPMIx PR testing do not post GitHub status. |

Tokens belong in GitLab CI/CD variables, never in this README. The observed project tokens are protected, masked, and hidden; the selected branch must qualify to receive protected variables.

## Manual runs and results

To run the full suite manually, choose **Build → Pipelines → New pipeline**, select the suite branch, and run without extra workflow switches. This tests the resolved OpenPMIx master commit without updating scheduled state. Push and merge-request pipelines are not enabled by the current configuration.

Open the pipeline's commit jobs for test logs and artifacts. Each generated commit job publishes `ci-results/<sha>.env`. Collection/reconciliation reports explain the successful boundary and any blocking result. Artifacts are generally retained for 14 days.

If runners are disabled, pause the schedule to avoid accumulating waiting pipelines. Pausing does not cancel existing pipelines or erase saved state.

PR testing is under development and will be documented separately once validated.

Configuration: [`.gitlab-ci.yml`](../.gitlab-ci.yml), [`openpmix_pr_jobs.yml`](openpmix_pr_jobs.yml), and [`run_pmix_python_suite.sh`](run_pmix_python_suite.sh). See the [root README](../README.md) for installation and test coverage. Development-only pilots are outside this guide.
