# Manual checkpoint-to-master allocation pilot

Create a new web pipeline on `feat/pmix-single-allocation-pilot` with only these pilot variables:

| Variable | Value |
|---|---|
| `PMIX_MULTI_ALLOCATION_PILOT` | `1` |
| `PMIX_MULTI_ALLOCATION_FROM_STATE` | `1` |

Remove `PMIX_MULTI_ALLOCATION_TIP` and old single-commit pilot switches.

Generation reads a validated snapshot of the configured saved checkpoint, fetches upstream master once, and freezes its exact SHA. It tests every commit after that checkpoint through the frozen tip, including merged commits. Commits arriving later wait for a future run. If nothing is new, the child is a no-op.

Each commit receives a separate four-node allocation and runs the full suite. Commit jobs can overlap when runners and the scheduler allow. The existing limits remain: 1h45m for setup, queue waiting and execution per job, with 45 minutes once nodes are assigned.

Results are collected and reconciled into a proposal. This manual run does not apply that proposal or change the saved checkpoint or production schedule.

Each commit's artifacts include its allocation ID, timestamped queue status and pending reasons, and final accounting fields for submit, eligibility, start, end, elapsed time, state and exit code. Sampling is approximately once a minute. These diagnostics are read-only; errors in diagnostics do not change test results. Accounting records can lag completion.

Check that generation selects the intended baseline and tip, all child jobs finish, and collection reconciles the complete successful prefix. Retain artifacts from failed or timed-out jobs for diagnosis.
