#!/usr/bin/env python3
"""Prepare/select a fixed historical baseline for the existing PR runner.

This is a trusted, manually invoked infrastructure check. It does not claim
that the historical PR is currently open or eligible for automatic discovery.
"""

import argparse
from pathlib import Path
import sys

from openpmix_pr_artifacts import (
    PREPARATION_FIELDS, PIPELINE_ID_RE, RecordError, atomic_record,
    parse_ordered_record, read_preparation, validate_preparation_values,
)


PMIX_SHA = "5841b3b26aff607854423ed8847b326b964e89a2"
PRRTE_SHA = "5cbf467d2819519ba75da93e250cf592ca7fa7a9"
PREPARATION = Path("ci-openpmix-pr-preparation/preparation.env")
MANIFEST = Path("ci-openpmix-pr-preparation/baseline.env")
FIELDS = ("BASELINE_VERSION", "MODE", "CI_PIPELINE_ID", "PMIX_COMMIT", "PRRTE_COMMIT")


def preparation(pipeline_id):
    return validate_preparation_values({
        "OPENPMIX_PR_PREPARATION_VERSION": "1", "CI_PIPELINE_ID": pipeline_id,
        "PR_NUMBER": "4370", "PR_AUTHOR": "rhc54",
        "PR_BASE_REPOSITORY": "openpmix/openpmix",
        "PR_HEAD_REPOSITORY": "rhc54/openpmix", "PR_HEAD_SHA": PMIX_SHA,
        "PR_SOURCE_POLICY": "verified-author-fork", "PREPARATION_RESULT": "ready",
    }, require_ready=True, expected_pipeline_id=pipeline_id)


def manifest(pipeline_id):
    return {"BASELINE_VERSION": "1", "MODE": "fixed-historical-baseline",
            "CI_PIPELINE_ID": pipeline_id, "PMIX_COMMIT": PMIX_SHA,
            "PRRTE_COMMIT": PRRTE_SHA}


def prepare(pipeline_id):
    # The job clears this fixed directory before calling this function.
    if not PREPARATION.parent.is_dir() or PREPARATION.parent.is_symlink():
        raise RecordError("baseline preparation directory is unavailable")
    if PREPARATION.exists() or MANIFEST.exists() or PREPARATION.is_symlink() or MANIFEST.is_symlink():
        raise RecordError("baseline output must be fresh")
    atomic_record(PREPARATION, PREPARATION_FIELDS, preparation(pipeline_id), "baseline preparation")
    atomic_record(MANIFEST, FIELDS, manifest(pipeline_id), "baseline manifest")


def select(pipeline_id):
    if not MANIFEST.exists() and not MANIFEST.is_symlink():
        return "live-pr"
    values = parse_ordered_record(MANIFEST, FIELDS, "baseline manifest")
    if values != manifest(pipeline_id):
        raise RecordError("baseline identity or pinned revisions differ")
    if read_preparation(PREPARATION, True, pipeline_id) != preparation(pipeline_id):
        raise RecordError("baseline manifest does not match preparation")
    return "master " + PRRTE_SHA


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "select"))
    parser.add_argument("--pipeline-id", required=True)
    args = parser.parse_args()
    if PIPELINE_ID_RE.fullmatch(args.pipeline_id) is None:
        parser.error("pipeline ID must be canonical")
    try:
        if args.action == "prepare":
            prepare(args.pipeline_id)
            print("Fixed historical baseline; not a live PR eligibility check.")
            print("PMIx=" + PMIX_SHA + " PRRTE=" + PRRTE_SHA)
        else:
            print(select(args.pipeline_id))
    except (RecordError, OSError) as error:
        print("error: baseline preparation/selection failed: " + str(error), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
