# Repeatable PR execution validation

Run the complete PR checkout, build, ReFrame execution, and result-finalization
path without waiting for an open upstream PR:

1. Open GitLab **Build > Pipelines > New pipeline**.
2. Select `fix/scheduled-batch-lifecycle` after the GitHub mirror updates.
3. Add the variable `OPENPMIX_PR_BASELINE` with value `1` and run the pipeline.
   Do not add other opt-in workflow variables for this run.

The jobs are `prepare-openpmix-baseline`, `validate-openpmix-baseline`, and
`finalize-openpmix-baseline`. The baseline reuses the normal PR execution scripts,
credential isolation, resource lock, full-suite result checks, and finalizer.
The entire suite runs; this is not an empty discovery poll.

The preparation job stays on a Frontier shell runner and stages the fixed PMIx,
PRRTE, and libevent sources in a compressed artifact. Preparation packs only
the pinned snapshots by compacting each staged Git repository to a
single shallow commit, retaining origins, the validated pull ref, and populated
submodule metadata. Old history and unreachable objects are not uploaded. The
preparation log prints the final archive size before upload.

The validation job targets the **Frontier Slurm runners (#49/#51)** using explicit
`frontier, slurm` tags.
It sets `timeout: 4h`, `RUNNER_SCRIPT_TIMEOUT: 3h45m`, and requests **one four-node
allocation for 45 minutes** through Jacamar's `SCHEDULER_PARAMETERS`.

Jacamar's default Slurm action submits the CI script with `sbatch --wait` on the
first compute node. Queue waiting counts toward CI's script limit, while the
45-minute Slurm limit starts only after allocation. The isolated launcher checks
the allocation's UID, GEN243 account, running state, four nodes, and current host
before preserving its scheduler identity. Sources are restored and revalidated
without network access from compute nodes. The six fetch/build fixtures and
eleven runtime checks then run serially on the allocated nodes using ReFrame's
local scheduler, with **no nested `sbatch` or `salloc` request**.

Normal scheduled and live PR jobs retain their separate allocations until this
baseline completes successfully and the change is promoted.

Allocation identity is saved in `evidence/runner-allocation.json`; the login-side
after-script saves Slurm accounting in `evidence/runner-accounting.txt`. Jacamar
owns the outer allocation's monitoring and cancellation. If CI expires before
the compute script ever starts, no compute-side record exists: use the Jacamar
job trace for its submission and cancellation evidence.

This integration follows the installed Jacamar 0.30.0 implementation:
[Slurm stage dispatch](https://gitlab.com/ecp-ci/jacamar-ci/-/blob/v0.30.0/internal/executors/slurm/slurm.go),
[submission and wait](https://gitlab.com/ecp-ci/jacamar-ci/-/blob/v0.30.0/internal/executors/slurm/run.go),
and [termination monitoring](https://gitlab.com/ecp-ci/jacamar-ci/-/blob/v0.30.0/internal/executors/slurm/monitor.go).
ORNL's [ADIOS2 Frontier configuration](https://github.com/ornladios/ADIOS2/blob/e2027fb66bd4ae3378f10702d994746a6441fd40/.gitlab/gitlab-ci-frontier.yml)
also uses `frontier, slurm` tags with `SCHEDULER_PARAMETERS`.

## Fixed inputs

- Historical upstream PR: https://github.com/openpmix/openpmix/pull/4370
- PMIx: `5841b3b26aff607854423ed8847b326b964e89a2`
- PRRTE: `5cbf467d2819519ba75da93e250cf592ca7fa7a9`

These are repeatable source inputs, not a claim that our suite already passes
on Frontier. The existing checkout helper fetches the retained upstream PR head
and requires it to match the pinned SHA. The PR may be closed or merged; no
open-PR eligibility record is fabricated. A missing or changed ref fails closed.
The normal live PR policy and PRRTE-master selection remain in effect elsewhere.

The preparation artifact and execution evidence contain `baseline.env`, which
labels the run as `fixed-historical-baseline` and records both revisions. These
manual runs are not counted as discovered live PR tests, do not report upstream
status, and do not update scheduled PMIx master state.

## Reading the result

Inspect `validate-openpmix-baseline` and its final result artifacts. A complete
pass must include actual ReFrame results; a green preparation job alone does not
validate execution. Checkout, module initialization, OAC setup, build, and test
failures can now be reproduced immediately using the same fixed sources.
Frontier allocation waits and the existing execution deadline still apply;
this workflow does not solve queue availability or validate discovery itself.
