#!/usr/bin/env python3
"""Persist a scheduled dependency pin; promote only verified prefix results."""
import argparse
import json
import os
from pathlib import Path
import re

from apply_reconciled_pmix_state import (
    atomic_replace, decode_fields, ensure_no_symlink_components, fsync_directory,
    locked_state, parse_state, read_regular_file,
)
from reconcile_pmix_results import RESULT_FIELDS
from collect_pmix_child_results import atomic_write
from openpmix_pr_artifacts import EXPECTED_TESTS


SHA = re.compile(r"[0-9a-f]{40}")


def read_json(path):
    return json.loads(read_regular_file(path).decode())


def load(path):
    value = read_json(path)
    if (value.get("schema_version") != 1 or
            not isinstance(value.get("prrte_commit"), str) or
            not SHA.fullmatch(value["prrte_commit"]) or
            not isinstance(value.get("history"), list)):
        raise ValueError("invalid persistent PRRTE pin")
    return value


def content(value):
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode()


def prepare(path, snapshot, resolve, bootstrap=None):
    path = ensure_no_symlink_components(path)
    with locked_state(path):
        if not path.exists():
            if bootstrap:
                value = load(bootstrap)
            else:
                value = dict(schema_version=1, prrte_commit=resolve(), history=[],
                             initialized_from="PRRTE master; runtime validation pending")
                if not SHA.fullmatch(value["prrte_commit"]):
                    raise ValueError("invalid initial PRRTE master revision")
            fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as stream:
                stream.write(content(value))
                stream.flush()
                os.fsync(stream.fileno())
            fsync_directory(path.parent)
        value = load(path)
        atomic_write(snapshot, content(value))
    print("Scheduled persistent PRRTE pin: " + value["prrte_commit"])
    return value["prrte_commit"]


def promote(path, snapshot, saved_state, collection, suite, output):
    baseline = load(snapshot)
    saved, _ = parse_state(saved_state)
    commit = saved["PMIX_COMMIT"]
    decision = dict(status="unchanged", prrte_commit=baseline["prrte_commit"])
    evidence_file = collection / (commit + ".summary.json")
    result_file = collection / (commit + ".env")
    # Empty/blocked batches need no dependency promotion. Missing display
    # evidence may never authorize an update; ordinary checkpoint validation
    # is still performed independently by Apply.
    if not evidence_file.exists() or not result_file.exists():
        decision["reason"] = "no verified runtime evidence at saved boundary"
    else:
        evidence = read_json(evidence_file)
        result = decode_fields(read_regular_file(result_file), RESULT_FIELDS)
        index = read_json(collection / "attempts.json")
        matching_job = any(
            job.get("commit") == commit and str(job.get("id")) == result["CI_JOB_ID"] and
            job.get("status") == "success" for job in index.get("jobs", []))
        checks = evidence.get("checks", {})
        valid = (
            matching_job and str(index.get("child_pipeline_id")) == result["CI_PIPELINE_ID"] and
            evidence.get("schema_version") == 1 and
            saved["SUITE_COMMIT"] == suite and
            result["PMIX_COMMIT"] == commit and result["SUITE_COMMIT"] == suite and
            result["CI_JOB_STATUS"] == "success" and
            evidence.get("pmix_commit") == commit and evidence.get("suite_commit") == suite and
            evidence.get("ci_status") == "success" and
            str(evidence.get("job_id")) == result["CI_JOB_ID"] and
            str(evidence.get("pipeline_id")) == result["CI_PIPELINE_ID"] and
            evidence.get("prrte_revision_source") == "checkout" and
            type(evidence.get("total_checks")) is int and
            evidence["total_checks"] == len(EXPECTED_TESTS) and
            all(type(checks.get(key)) is int and checks[key] == count for key, count in
                (("passed", len(EXPECTED_TESTS)), ("failed", 0), ("incomplete", 0)))
        )
        revision = evidence.get("prrte_revision")
        valid = valid and isinstance(revision, str) and SHA.fullmatch(revision)
        policy = evidence.get("prrte_policy", {})
        if revision == baseline["prrte_commit"]:
            decision["reason"] = "saved boundary passed with existing pin"
        elif not valid or not (
                policy.get("pinned") == baseline["prrte_commit"] and
                policy.get("candidate") == revision and policy.get("fallback_attempted") is True):
            raise ValueError("candidate PRRTE promotion lacks matching full-suite evidence")
        else:
            transition = dict(previous=baseline["prrte_commit"], prrte_commit=revision,
                              pmix_commit=commit, suite_commit=suite,
                              job_id=evidence["job_id"], pipeline_id=evidence["pipeline_id"],
                              recorded_at=evidence["recorded_at"])
            with locked_state(path):
                current = load(path)
                if current != baseline:
                    # A replay of Apply may confirm the same already-saved transition.
                    if not (current["prrte_commit"] == revision and
                            current["history"] == baseline["history"] + [transition]):
                        raise ValueError("persistent PRRTE pin changed since discovery")
                else:
                    current["prrte_commit"] = revision
                    current["history"].append(transition)
                    atomic_replace(path, content(current))
            decision.update(status="promoted", prrte_commit=revision, evidence=transition)
    atomic_write(output, content(decision))
    print("PRRTE pin: " + json.dumps(decision, sort_keys=True))
    return decision


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", required=True, type=Path)
    parser.add_argument("--snapshot", required=True, type=Path)
    parser.add_argument("--saved-state", required=True, type=Path)
    parser.add_argument("--collection", required=True, type=Path)
    parser.add_argument("--suite-commit", required=True)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    promote(args.state, args.snapshot, args.saved_state, args.collection,
            args.suite_commit, args.output)


if __name__ == "__main__":
    main()
