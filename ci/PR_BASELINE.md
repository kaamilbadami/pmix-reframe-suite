# Repeatable PR execution validation

Run the complete PR checkout, build, ReFrame execution, and result-finalization
path without waiting for an open upstream PR:

1. Open GitLab **Build > Pipelines > New pipeline**.
2. Select `fix/scheduled-batch-lifecycle` after the GitHub mirror updates.
3. Add the variable `OPENPMIX_PR_BASELINE` with value `1` and run the pipeline.
   Do not add other opt-in workflow variables for this run.

The jobs are `prepare-openpmix-baseline`, `validate-openpmix-baseline`, and
`finalize-openpmix-baseline`. The validation and finalization inherit the normal
PR execution scripts, runner, credential isolation, resource lock, artifacts,
and time limits. The entire suite runs; this is not an empty discovery poll.

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
