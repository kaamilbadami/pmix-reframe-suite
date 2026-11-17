# GitLab CI on Frontier

This pipeline builds OpenPMIx and runs our 11-check PMIx Python ReFrame suite on Frontier. Scheduled testing tracks new commits; manual runs let us check the suite without changing the saved checkpoint.

## How the scheduled pipeline works

1. Acquire the GitLab `pmix-scheduled-batch` resource group, then read the saved last-known-good OpenPMIx commit.
2. Find every newer commit on OpenPMIx master, oldest first.
3. Run a separate test job for each exact commit. Jobs use separate workspaces and can run in parallel.
4. Collect results and advance last-known-good only through consecutive successful commits.

For example, if **A and B pass, C fails, and D passes**, state advances to **B**. The next schedule retries from C. Missing or invalid results also stop advancement. With no new commits, the saved commit is retested using the full suite when `LAST_SUCCESS_EPOCH` is at least 24 hours old. Success refreshes the timestamp without advancing the commit; failure leaves the health check due. Before 24 hours, no tests run and state stays unchanged. The schedule starts a wrapper pipeline that holds this lock until its five-job orchestration child completes, including state application. New scheduled pipelines wait before discovery, then read fresh state. Commit jobs inside each batch remain parallel. This uses two child levels: orchestration, then exact-commit jobs. It does not serialize manual or PR testing.

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

## Compact batch report

Open `ci-scheduled/report/latest-summary.md` (or `latest-summary.json`) in the
scheduled **Apply** job artifacts for the final checkpoint-first report. The
filename is retained for compatibility; its headline now describes the batch,
not just the most recently finished attempt. **Collect** publishes an early
copy marked preliminary, because checkpoint application has not happened yet.
Both jobs retain reports for 14 days. This is file output only, not a website
or notification service.

The Markdown leads with status, the saved last-known-good commit, commit
counts, and the first blocker with its reason and runtime counts. Revisions,
test time, and available job/pipeline links follow under Test details, focused
on the blocker or final passing commit. An empty batch shows only status,
last-known-good commit, and an explanation that no tests ran. Its status includes
the fetched master commit's committer date/time in UTC, captured during discovery
in `pmix-untested-commits.txt.head.json`. Missing metadata leaves the date out;
the pipeline run time and last successful test time are never substituted. Commit outcomes are
separate from the eleven runtime checks inside a job. A timeout is incomplete
testing; a failed CI job alone does not identify a PMIx software defect.

For A passed, B failed, C passed: the passing boundary is A, B is the blocker,
and counts are 2 passed / 1 failed / 0 incomplete. C's success stays recorded
but cannot move the checkpoint across B. Collect labels A as a candidate;
the final report only calls it a saved update after Apply succeeds and a fresh
state snapshot confirms it. Failed or unconfirmed application is explicit.
Passing-boundary and checkpoint-confirmation bookkeeping remains in JSON.

Individual attempt details remain in JSON and under
`report/attempt-details/`. The collector retains compact metadata for current
non-retried jobs and downloads runtime summaries for the latest attempt, first
non-successful job, and preceding successful boundary where available. This
adds reporting evidence without changing strict checkpoint result records.
Latest-attempt selection still uses completion time, falling back to start or
creation time and job ID, rather than discovery order.

Runtime counts exclude Python unit tests and fetch/build fixtures. Terminal
console results survive missing or truncated ReFrame JSON. Missing, skipped,
aborted, expired, or mismatched evidence remains incomplete. PRRTE revisions
are marked as requested if checkout provenance is unavailable. Detailed
attempt views retain exact revisions, UTC time, runtime counts, job/pipeline
links, and timeout classification. The report does not guess whether a timeout
was allocation waiting or test execution.

These changes apply to new pipelines. Retrying an old pipeline keeps its original
suite revision. After individual job retries, rerun Collect → Reconcile → Apply
to refresh the final batch report and checkpoint confirmation.

## Relevant CI variables

Normal runs do not require development-pilot switches. CI supplies the job identifiers, tool paths, and exact OpenPMIx SHA for each generated commit job.

| Variable | What it does during a run |
|---|---|
| `PMIX_AUTHORITATIVE_STATE_FILE` | Selects the scheduled checkpoint; defaults to the Frontier path above. |
| `OLCF_SERVICE_ACCOUNT` | Selects the Frontier execution account through runner configuration; defaults to `gen243_auser`. |
| `PMIX_COMMIT` | Identifies the exact OpenPMIx commit being tested. Scheduled jobs set it individually. The normal manual web run resolves master itself rather than honoring a supplied pin. |
| `PRRTE_BRANCH`, `PRRTE_COMMIT` | Select the PRRTE source used with OpenPMIx. Scheduled batches resolve one exact PRRTE master SHA; manual pilots retain compatibility pins. See the compatibility section below. |
| `GITHUB_PR_READ_TOKEN` | Reads PR information for PR workflows; ordinary scheduled commit discovery does not need it. |
| `GITHUB_STATUS_TOKEN` | Posts GitHub status in supported workflows. Scheduled multi-commit testing and internal OpenPMIx PR testing do not post GitHub status. |

Tokens belong in GitLab CI/CD variables, never in this README. The observed project tokens are protected, masked, and hidden; the selected branch must qualify to receive protected variables.

## Manual runs and results

To run the full suite manually, choose **Build → Pipelines → New pipeline**, select the suite branch, and run without extra workflow switches. This tests the resolved OpenPMIx master commit without updating scheduled state. Push and merge-request pipelines are not enabled by the current configuration.

Open the pipeline's commit jobs for test logs and artifacts. Each generated commit job publishes `ci-results/<sha>.env`. Collection/reconciliation reports explain the successful boundary and any blocking result. Artifacts are generally retained for 14 days.

Generated per-test-allocation commit jobs have a six-hour CI limit.
`RUNNER_SCRIPT_TIMEOUT=5h45m`
reserves time for `RUNNER_AFTER_SCRIPT_TIMEOUT=5m`, result records, cleanup, and
artifact upload. Slurm queue waiting counts toward CI time, but each test's
Slurm execution limit remains unchanged. A runner maximum timeout can cap the
configured limit. In GitLab, open **Settings → CI/CD → Runners**, edit each
Frontier shell runner eligible for these jobs, and ensure **Maximum job timeout**
is at least `21600` seconds (six hours), or unset if permitted by runner policy.
Changing this shared runner setting requires runner-management permission.
Keep `PMIX_SCHEDULED_SLURM=0` to retain separate allocations for each test;
this longer CI budget does not change their Slurm walltimes. Verify a newly
generated commit job shows an effective six-hour timeout; existing jobs retain
the configuration generated for their original pipeline.

### Skip redundant scheduled master polls

Set a masked `GITLAB_PIPELINE_CONTROL_TOKEN` CI/CD variable with project-scoped
API access and permission to cancel pipelines. Use a protected variable when the
scheduled branch is protected; the variable must be available on the schedule's
actual target ref. Do not put its value in source, logs, or chat.

When configured, `check-pmix-scheduled-admission` checks active scheduled master
batch bridges before `run-pmix-scheduled-batch` can obtain its lock. If another
batch is running (including allocation waiting), or an older master poll is
already starting, it cancels **only its own idle scheduled pipeline**. GitLab
shows that redundant poll as canceled, not as a passing test. Running batches
are not canceled, and the next scheduled check discovers any remaining commits.
The existing whole-batch resource lock and checkpoint reconciliation remain in
place. Concurrent polls favor the older pipeline; the resource lock remains the
final protection against races.

The ordinary CI job token cannot cancel pipelines. Without the control token,
the admission job is not enabled and the old queued behavior remains. API or
identity-check failures stop the new poll before its batch starts. Existing
queued pipelines keep their old configuration and need manual cancellation.

### Scheduled Slurm runner trial

Set the schedule variable `PMIX_SCHEDULED_SLURM=1` on
`fix/scheduled-batch-lifecycle` to generate Slurm-runner commit jobs. The default
is `0`, using the existing shell jobs. Existing pipelines and their retries keep
the configuration with which they were created.

Each selected commit gets a shell preparation job and a `pmix-<SHA>` execution
job. Preparation stages exact PMIx, OAC, PRRTE, and libevent sources in an artifact.
The execution job targets `frontier, slurm`, with a six-hour CI timeout and a
5h45m script limit. Jacamar requests four nodes for 30 minutes. All six fixtures
and eleven runtime checks run serially inside that allocation, using the
baseline's local ReFrame configuration and fixed tool installations. Compute
execution consumes the staged sources without downloading dependencies.

The commit job keeps its existing name and result record, so collection,
reconciliation, checkpoint advancement, and individual job retries continue to
work. Its artifacts include the ReFrame tree and JSON report, allocation identity,
and accounting when available. If allocation waiting expires before the script
starts, inspect the Jacamar trace for the outer Slurm job ID.

This path needs a successful live Frontier trial before becoming the default.
The 30-minute allocation includes compilation and all runtime checks. The
six-hour CI limit allows longer queue waiting but cannot guarantee an allocation.
PR workflows retain their separate configuration.

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
Automatic OpenPMIx PR discovery remains opt-in (`OPENPMIX_PR_AUTODISCOVERY=1`). Use a separate
schedule on the lifecycle branch with that variable for a temporary PR trial
(e.g. `*/5 * * * *`, subject to GitLab's allowed frequency). PR schedules
exclude `run-pmix-scheduled-batch`; leave the normal 30-minute schedule without
this variable. Empty discovery creates a no-op child without building or
requesting compute nodes. PR execution jobs share their existing resource lock;
discovery suppresses every previously attempted PR number + head SHA, including
failed, canceled, and skipped attempts. A new head SHA is eligible immediately;
failures on an unchanged head require an explicit retry rather than a new
attempt every five minutes. Disable the temporary schedule after validation;
this configuration does not disable it automatically.

It requires job-token access to the pipeline-list and individual-pipeline APIs
(`READ_PIPELINES` where fine-grained permissions are used). Named child
pipelines identify exact PR heads. After acquiring the execution resource lock,
automatic jobs recheck GitLab: a newer pipeline selecting a different SHA for
the same PR supersedes a queued selection, and only the oldest pipeline for
an exact PR head may execute automatically. This closes the compute-launch
race between overlapping polls; it does not prevent every duplicate lightweight
child from being created. Other PR numbers do not supersede one another.
Already running tests are not canceled.

Duplicate or superseded queued jobs finish without builds or Slurm submissions.
Their final artifact is `ci-openpmix-pr-final/not-run.json`, explicitly **not a
passing test result**. GitLab may show these lightweight jobs as successful
because the admission check completed. API errors stop execution rather than
allowing an unchecked run. Manual PR runs without `OPENPMIX_PR_AUTOMATIC=1`
bypass automatic admission, so a deliberate retest remains possible. Retry the
original canonical pipeline job, or start an explicit manual PR pipeline.

Existing pipelines retain their generated configuration. This change does not
cancel an existing backlog or update jobs already created on older suite SHAs.
Pause the PR schedule and cancel unwanted older queued pipelines in GitLab when
switching to this revision. Continue to use a separate master-testing schedule.
OpenPMIx PR execution resolves one exact PRRTE master SHA per execution; see the compatibility section below.

Configuration: [`.gitlab-ci.yml`](../.gitlab-ci.yml), [`openpmix_pr_jobs.yml`](openpmix_pr_jobs.yml), and [`run_pmix_python_suite.sh`](run_pmix_python_suite.sh). See the [root README](../README.md) for installation and test coverage. Development-only pilots are outside this guide.


### PRRTE compatibility for exact OpenPMIx commits

Scheduled batches temporarily use `--prrte-master`: the generator resolves
upstream PRRTE master once and writes that exact SHA into every commit job.
This includes a saved-commit daily health check. Empty batches do not resolve
PRRTE. A failed or malformed lookup stops generation without falling back to
an older revision. Child jobs retain `prrte-source.env` with the built SHA;
the generated YAML also records the batch's chosen SHA. PRRTE changes between
batches can affect results, and compilation success does not establish runtime
compatibility. Normal checkpoint and successful-prefix rules still apply.

Manual multi-commit generators continue to use `--select-prrte` to set
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

OpenPMIx PR execution resolves PRRTE master once, builds that exact SHA, and
records it in `evidence/prrte-selection.env` and `prrte-source.env` artifacts.
PR execution has a six-hour job limit and a 5h45m main-script limit.
The eligible shell runner maximum must also allow six hours (see above).
Results remain internal to GitLab; this workflow does not post GitHub statuses. Scheduled
commit jobs retain the multinode scaling stage, including PRRTE launch logs,
for investigation when ReFrame output is incomplete.

## Runtime directories and startup diagnostics

The OpenPMIx PR and single-allocation configurations use node-local runtime
storage. Before PRRTE starts, initialization uses one `srun` task on every
allocated node to validate `/tmp` and create the job's private runtime directory.
A failed setup prevents the workload and rolls back the directories already
created on other nodes. Cleanup runs on every allocated node and only removes
directories whose ownership marker matches this invocation. Steps are bound to
the existing Slurm job and do not request another allocation. The batch keeps
`--export=NIL`; setup/cleanup steps use `--export=NONE` plus `env -i`.
A hard allocation termination may prevent cleanup; failures are logged rather
than treated as successful cleanup.

Targeted and mixed controllers use the same bounded, file-backed PRRTE startup
helper as worker-thread controllers. Full stdout/stderr remain in
`*.prte.stdout` and `*.prte.stderr` even when startup fails; the controller error
includes both log tails. Mapping and multinode wrappers also print their startup
log on readiness failure. PR, baseline, and scheduled job artifacts include
those streams, `*.dvm_output`, and `prte-*node.log`, including failed stages.

Offline runtime tests use distinct temporary filesystems for simulated nodes;
they verify all-node readiness, cleanup after workload failure, partial-setup
rollback, and rejection of pre-existing directories and symlinks. These checks
do not replace live Frontier validation of Slurm steps and remote PRRTE startup.

## Fixed multinode diagnostic (independent of live PRs)

Choose **New pipeline** on the suite branch containing this option, set only
`OPENPMIX_FIXED_DIAGNOSTIC=1`, and run it as a manual web pipeline. This is not a
schedule option. The expected job is `run-openpmix-fixed-diagnostic` on the shell
runner; the ordinary suite, live OpenPMIx PR workflow, and historical baseline
are excluded in this mode. Do not combine it with other pilot switches.

The pins reproduce the earlier remote-daemon failure:

- OpenPMIx: `0fb45ff3d1b67a91f690b68918566acc45131062`
- PRRTE: `9c715a9cd3d42adf8deb7246586e8712599ee55b`

The diagnostic fetches the OpenPMIx commit object directly from its fixed public
upstream and validates/populates its exact approved OAC gitlink. It never looks
up a live PR or requires that PR to remain open. If an upstream object becomes
unavailable, it fails rather than substituting a newer revision. PRRTE uses the
existing exact-pin fixture. Caller-provided PMIx/PRRTE pins cannot change this
bounded diagnostic; edit the trusted constants to select another reproduction.

Build fixtures run on the shell runner. Only `PMIxPythonMappingPPRNodeTest`
requests a test allocation: two nodes, one slot per node, one process per node,
one trial. It uses the repaired PR runtime configuration, including all-node
runtime-directory preparation and cleanup. This diagnostic enables
`--debug-daemons --leave-session-attached --prtemca plm_base_verbose 10`
to retain daemon-launch and `srun` errors in `prte-2node.log`. Ordinary mapping
runs keep daemon debugging disabled. The shell CI job has a six-hour
limit with a 5h45m script limit, subject to the runner maximum described above. Build and queue time still count against CI
walltime; there is no enclosing four-node, 30-minute allocation.

The launcher enters a clean environment before fetching or building sources.
The result is explicitly a **diagnostic**, not a live PR verdict or a full-suite
pass. It does not post upstream statuses, run Collect/Reconcile/Apply, or change
the scheduled checkpoint. Artifacts under `.ci-work/<job-id>/` include the pins,
suite revision, diagnostic result/log, ReFrame report, build output, and complete
startup logs. Cleanup is limited to Slurm jobs owned by this CI job.
