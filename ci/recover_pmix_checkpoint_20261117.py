#!/usr/bin/env python3
"""Apply the explicitly authorized November 17 checkpoint recovery once.

This accepts a historical coverage gap; it does not change normal reconciliation.
The target and provenance come from verified job 256304 in pipeline 173147.
"""
import argparse
import json
import os
from pathlib import Path
import uuid

from apply_reconciled_pmix_state import (
    atomic_replace, locked_state, parse_state, snapshot_state,
)

EXPECTED = "caa7dc477abfea115e3170afe2c6ddfff4914afa"
TARGET = "3b2cedde4d2c796b3a2e99452f9d8d8fc82e2b3b"
SUITE = "4bd310411f5ce4a2c83470f49f43dded96025a35"
SUCCESS_EPOCH = 1794944364


def recover(state, output):
    output.mkdir(parents=True, exist_ok=True)
    with locked_state(state):
        current, _ = parse_state(state)
        snapshot_state(state, output / "before.env")
        if current["PMIX_COMMIT"] == TARGET:
            print("Checkpoint already points to the verified passing commit.")
            snapshot_state(state, output / "after.env")
            return
        if current["PMIX_COMMIT"] != EXPECTED:
            raise ValueError("Stopped: unexpected checkpoint " + current["PMIX_COMMIT"])
        if int(current["LAST_SUCCESS_EPOCH"]) > SUCCESS_EPOCH:
            raise ValueError("Stopped: checkpoint has newer successful-test evidence")

        stem = state.name + ".manual-3b2cedde-" + uuid.uuid4().hex
        backup = state.with_name(stem + ".before.env")
        snapshot_state(state, backup)
        record = {
            "action": "authorized checkpoint recovery accepting a coverage gap",
            "before": current,
            "target": TARGET,
            "suite_commit": SUITE,
            "last_success_epoch": SUCCESS_EPOCH,
            "verified_job_id": 256304,
            "verified_pipeline_id": 173147,
            "prrte_commit": "f684f116a2ae797923364a1274871ad2dd9691f7",
            "runtime_checks": {"passed": 11, "failed": 0, "incomplete": 0},
            "accepted_gap": {
                "pmix_commit": "24735901af00045ccaefd15f6f529aa59576116e",
                "reason": "PRRTE build requires PMIx identifiers added in the next commit",
            },
            "recovery_job_id": os.environ.get("CI_JOB_ID"),
            "recovery_pipeline_id": os.environ.get("CI_PIPELINE_ID"),
            "backup": str(backup),
        }
        # Persist the authorization/provenance before changing the checkpoint.
        audit = state.with_name(stem + ".json")
        with audit.open("x") as stream:
            json.dump(record, stream, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        (output / "recovery.json").write_text(json.dumps(record, indent=2) + "\n")
        replacement = "PMIX_COMMIT={}\nSUITE_COMMIT={}\nLAST_SUCCESS_EPOCH={}\n".format(
            TARGET, SUITE, SUCCESS_EPOCH).encode()
        atomic_replace(state, replacement)
        snapshot_state(state, output / "after.env")
        print("Recovered checkpoint to {}\nBackup: {}\nAudit: {}".format(TARGET, backup, audit))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", required=True, type=Path)
    parser.add_argument("--output", type=Path, default=Path("ci-checkpoint-recovery"))
    args = parser.parse_args()
    recover(args.state, args.output)
