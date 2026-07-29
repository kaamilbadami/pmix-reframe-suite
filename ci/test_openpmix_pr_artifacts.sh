#!/bin/bash
set -euo pipefail

script_dir=$(cd -- "${BASH_SOURCE[0]%/*}" && pwd -P)
python3 - "$script_dir/openpmix_pr_artifacts.py" <<'PY'
import os
from pathlib import Path
import subprocess
import sys
import tempfile


tool = Path(sys.argv[1]).resolve()
tests = 0
sha = "5f08dede7495ce1b332691b3ac7eaf6478a979b9"
digest = "a" * 64
execution_id = "b" * 32


def passed(message):
    global tests
    tests += 1
    print("ok - " + message)


def check(condition, message):
    if not condition:
        raise AssertionError(message)


def run(arguments):
    return subprocess.run(
        [sys.executable, str(tool)] + list(arguments),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


eligibility = (
    "OPENPMIX_PR_ELIGIBILITY_VERSION=1\n"
    "PR_NUMBER=4028\n"
    "PR_STATE=open\n"
    "PR_DRAFT=0\n"
    "PR_AUTHOR=rhc54\n"
    "PR_BASE_REPOSITORY=openpmix/openpmix\n"
    "PR_HEAD_REPOSITORY=rhc54/openpmix\n"
    "PR_HEAD_SHA={}\n"
    "PR_SOURCE_POLICY=verified-author-fork\n"
).format(sha)


with tempfile.TemporaryDirectory(prefix="test-openpmix-records-") as temporary:
    root = Path(temporary)
    original_cwd = Path.cwd()
    os.chdir(str(root))
    try:
        Path("eligibility.env").write_text(eligibility)
        completed = run([
            "write-preparation",
            "--eligibility", "eligibility.env",
            "--pr-number", "4028",
            "--pipeline-id", "9001",
            "--result", "ready",
            "--output", "preparation.env",
        ])
        check(completed.returncode == 0, completed.stderr.decode())
        expected_preparation = (
            "OPENPMIX_PR_PREPARATION_VERSION=1\n"
            "CI_PIPELINE_ID=9001\n"
            "PR_NUMBER=4028\n"
            "PR_AUTHOR=rhc54\n"
            "PR_BASE_REPOSITORY=openpmix/openpmix\n"
            "PR_HEAD_REPOSITORY=rhc54/openpmix\n"
            "PR_HEAD_SHA={}\n"
            "PR_SOURCE_POLICY=verified-author-fork\n"
            "PREPARATION_RESULT=ready\n"
        ).format(sha)
        check(Path("preparation.env").read_text() == expected_preparation,
              "preparation schema or order changed")
        passed("ready preparation records bind every required identity field")

        for field, expected in (
                ("CI_PIPELINE_ID", "9001"),
                ("PR_NUMBER", "4028"),
                ("PR_AUTHOR", "rhc54"),
                ("PR_BASE_REPOSITORY", "openpmix/openpmix"),
                ("PR_HEAD_REPOSITORY", "rhc54/openpmix"),
                ("PR_HEAD_SHA", sha),
                ("PR_SOURCE_POLICY", "verified-author-fork"),
                ("PREPARATION_RESULT", "ready")):
            completed = run([
                "read-preparation",
                "--input", "preparation.env",
                "--require-ready",
                "--expected-pipeline-id", "9001",
                "--field", field,
            ])
            check(completed.returncode == 0, completed.stderr.decode())
            check(completed.stdout.decode().strip() == expected,
                  "preparation field changed: " + field)
        passed("strict preparation reads preserve canonical identity")

        completed = run([
            "write-preparation",
            "--eligibility", "eligibility.env",
            "--pr-number", "4028",
            "--pipeline-id", "9001",
            "--result", "error",
            "--output", "error-preparation.env",
        ])
        check(completed.returncode == 0, "error preparation write failed")
        completed = run([
            "read-preparation",
            "--input", "error-preparation.env",
            "--require-ready",
            "--expected-pipeline-id", "9001",
            "--field", "PR_HEAD_SHA",
        ])
        check(completed.returncode == 2,
              "fail-closed preparation authorized later work")
        passed("error preparation records retain identity but never authorize work")

        for invalid_pipeline in ("", "0", "01", "-1", "+1", " 1", "1 "):
            completed = run([
                "write-preparation",
                "--eligibility", "eligibility.env",
                "--pr-number", "4028",
                "--pipeline-id", invalid_pipeline,
                "--result", "ready",
                "--output", "invalid-preparation.env",
            ])
            check(completed.returncode == 2,
                  "invalid pipeline ID was accepted")
        for invalid_number in ("", "0", "01", "-1", "4028 "):
            completed = run([
                "write-preparation",
                "--eligibility", "eligibility.env",
                "--pr-number", invalid_number,
                "--pipeline-id", "9001",
                "--result", "ready",
                "--output", "invalid-preparation.env",
            ])
            check(completed.returncode == 2,
                  "invalid requested PR number was accepted")
        passed("PR and pipeline identifiers must be canonical positive integers")

        expected_arguments = (
            ("--expected-sha", "0" * 40),
            ("--expected-author", "kaamilbadami"),
            ("--expected-head-repository", "openpmix/openpmix"),
        )
        for option, value in expected_arguments:
            completed = run([
                "write-preparation",
                "--eligibility", "eligibility.env",
                "--pr-number", "4028",
                "--pipeline-id", "9001",
                option, value,
                "--result", "ready",
                "--output", "changed-preparation.env",
            ])
            check(completed.returncode == 2,
                  "changed revalidation identity was accepted")
        passed("preparation publication rechecks SHA, author, and head repository")

        eligibility_variants = {
            "extra": eligibility + "EXTRA=value\n",
            "missing": "\n".join(eligibility.splitlines()[:-1]) + "\n",
            "duplicate": eligibility.replace(
                "PR_NUMBER=4028\n",
                "PR_NUMBER=4028\nPR_NUMBER=4028\n"),
            "reordered": eligibility.replace(
                "PR_STATE=open\nPR_DRAFT=0\n",
                "PR_DRAFT=0\nPR_STATE=open\n"),
        }
        for label, content in eligibility_variants.items():
            Path("malformed-eligibility.env").write_text(content)
            completed = run([
                "write-preparation",
                "--eligibility", "malformed-eligibility.env",
                "--pr-number", "4028",
                "--pipeline-id", "9001",
                "--result", "ready",
                "--output", "malformed-eligibility-preparation.env",
            ])
            check(completed.returncode == 2,
                  "{} eligibility was accepted".format(label))
        passed("eligibility records reject extra, missing, duplicate, and reordered fields")

        completed = run([
            "write-checkout",
            "--preparation", "preparation.env",
            "--pipeline-id", "9001",
            "--fetched-sha", sha,
            "--checked-out-sha", sha,
            "--output", "checkout.env",
        ])
        check(completed.returncode == 0, completed.stderr.decode())
        expected_checkout = (
            "OPENPMIX_PR_CHECKOUT_VERSION=1\n"
            "CI_PIPELINE_ID=9001\n"
            "PR_NUMBER=4028\n"
            "PR_AUTHOR=rhc54\n"
            "PR_BASE_REPOSITORY=openpmix/openpmix\n"
            "PR_HEAD_REPOSITORY=rhc54/openpmix\n"
            "PR_HEAD_SHA={}\n"
            "PR_SOURCE_POLICY=verified-author-fork\n"
            "UPSTREAM_REPOSITORY=openpmix/openpmix\n"
            "PULL_REF=refs/pull/4028/head\n"
            "FETCHED_SHA={}\n"
            "CHECKED_OUT_SHA={}\n"
            "DETACHED_HEAD=1\n"
            "CLEAN_CHECKOUT=1\n"
            "GITLINK_COUNT=0\n"
        ).format(sha, sha, sha)
        check(Path("checkout.env").read_text() == expected_checkout,
              "checkout schema or order changed")
        passed("checkout evidence binds the fixed pull ref and exact prepared SHA")

        for mutation in (
                expected_checkout.replace("DETACHED_HEAD=1", "DETACHED_HEAD=0"),
                expected_checkout.replace("CLEAN_CHECKOUT=1", "CLEAN_CHECKOUT=0"),
                expected_checkout.replace("GITLINK_COUNT=0", "GITLINK_COUNT=1"),
                expected_checkout.replace("refs/pull/4028/head",
                                          "refs/heads/master"),
                expected_checkout + "EXTRA=value\n"):
            Path("invalid-checkout.env").write_text(mutation)
            completed = run([
                "read-checkout", "--input", "invalid-checkout.env",
                "--field", "PR_HEAD_SHA",
            ])
            check(completed.returncode == 2,
                  "unsafe checkout evidence was accepted")
        passed("checkout evidence rejects attached, dirty, submodule, wrong-ref, and extra fields")

        completed = run([
            "write-result",
            "--preparation", "preparation.env",
            "--pipeline-id", "9001",
            "--execution-id", execution_id,
            "--result", "success",
            "--classification", "tests-passed",
            "--runner-exit-status", "0",
            "--report-sha256", digest,
            "--checkout-evidence-sha256", digest,
            "--output", "result.env",
        ])
        check(completed.returncode == 0, completed.stderr.decode())
        expected_result = (
            "OPENPMIX_PR_RESULT_VERSION=2\n"
            "CI_PIPELINE_ID=9001\n"
            "PR_NUMBER=4028\n"
            "PR_AUTHOR=rhc54\n"
            "PR_BASE_REPOSITORY=openpmix/openpmix\n"
            "PR_HEAD_REPOSITORY=rhc54/openpmix\n"
            "PR_HEAD_SHA={}\n"
            "PR_SOURCE_POLICY=verified-author-fork\n"
            "EXECUTION_ID={}\n"
            "RESULT=success\n"
            "RESULT_CLASSIFICATION=tests-passed\n"
            "RUNNER_EXIT_STATUS=0\n"
            "REPORT_SHA256={}\n"
            "CHECKOUT_EVIDENCE_SHA256={}\n"
        ).format(sha, execution_id, digest, digest)
        check(Path("result.env").read_text() == expected_result,
              "result schema or order changed")
        passed("result records copy the complete preparation identity")

        completed = run([
            "validate-agreement",
            "--preparation", "preparation.env",
            "--result", "result.env",
            "--pipeline-id", "9001",
        ])
        check(completed.returncode == 0, completed.stderr.decode())
        check(completed.stdout.decode().strip() ==
              "{} success tests-passed".format(sha),
              "agreement output changed")
        passed("matching preparation and result records validate exactly")

        valid_outcomes = (
            ("success", "tests-passed", "0", digest, digest),
            ("failure", "test-failure", "1", digest, digest),
            ("failure", "source-build-failure", "1", digest, digest),
            ("error", "configuration-error", "2", "missing", digest),
            ("error", "scheduler-error", "1", digest, digest),
            ("error", "fixture-error", "1", "missing", digest),
            ("error", "frontend-error", "unavailable", "missing", "missing"),
            ("error", "malformed-report", "2", digest, digest),
            ("error", "checkout-error", "2", "missing", "missing"),
            ("error", "head-changed", "unavailable", "missing", "missing"),
            ("error", "missing-pr-ref", "unavailable", "missing", "missing"),
            ("error", "unauthorized-submodule", "unavailable", "missing", "missing"),
        )
        for index, (result, classification, status, report,
                    checkout) in enumerate(valid_outcomes):
            completed = run([
                "write-result",
                "--preparation", "preparation.env",
                "--pipeline-id", "9001",
                "--execution-id", execution_id,
                "--result", result,
                "--classification", classification,
                "--runner-exit-status", status,
                "--report-sha256", report,
                "--checkout-evidence-sha256", checkout,
                "--output", "outcome-{}.env".format(index),
            ])
            check(completed.returncode == 0,
                  "valid outcome rejected: " + classification)
        passed("result schema distinguishes test, checkout, fixture, scheduler, and frontend outcomes")

        inconsistent = (
            ("success", "test-failure", "0", digest, digest),
            ("failure", "tests-passed", "1", digest, digest),
            ("error", "source-build-failure", "2", digest, digest),
            ("success", "tests-passed", "1", digest, digest),
            ("success", "tests-passed", "0", "missing", digest),
            ("failure", "test-failure", "1", digest, "missing"),
            ("error", "frontend-error", "2", "not-a-digest", digest),
        )
        for index, (result, classification, status, report,
                    checkout) in enumerate(inconsistent):
            completed = run([
                "write-result",
                "--preparation", "preparation.env",
                "--pipeline-id", "9001",
                "--execution-id", execution_id,
                "--result", result,
                "--classification", classification,
                "--runner-exit-status", status,
                "--report-sha256", report,
                "--checkout-evidence-sha256", checkout,
                "--output", "invalid-outcome-{}.env".format(index),
            ])
            check(completed.returncode == 2,
                  "inconsistent outcome was accepted")
        passed("result, classification, and evidence consistency is enforced")

        completed = run([
            "write-final",
            "--preparation", "preparation.env",
            "--result", "result.env",
            "--pipeline-id", "9001",
            "--output", "final.env",
        ])
        check(completed.returncode == 0, completed.stderr.decode())
        completed = run([
            "read-final", "--input", "final.env",
            "--expected-pipeline-id", "9001",
            "--field", "FINALIZATION_RESULT",
        ])
        check(completed.returncode == 0 and
              completed.stdout.decode().strip() == "validated",
              completed.stderr.decode())
        passed("fresh finalization produces a strict validated artifact only")

        malformed_variants = {
            "extra": expected_preparation + "EXTRA=value\n",
            "missing": "\n".join(expected_preparation.splitlines()[:-1]) + "\n",
            "duplicate": expected_preparation.replace(
                "CI_PIPELINE_ID=9001\n",
                "CI_PIPELINE_ID=9001\nCI_PIPELINE_ID=9001\n"),
            "reordered": expected_preparation.replace(
                "CI_PIPELINE_ID=9001\nPR_NUMBER=4028\n",
                "PR_NUMBER=4028\nCI_PIPELINE_ID=9001\n"),
            "no-newline": expected_preparation.rstrip("\n"),
            "carriage-return": expected_preparation.replace("\n", "\r\n"),
        }
        for label, content in malformed_variants.items():
            Path("malformed.env").write_text(content)
            completed = run([
                "read-preparation",
                "--input", "malformed.env",
                "--field", "PR_HEAD_SHA",
            ])
            check(completed.returncode == 2,
                  "{} preparation was accepted".format(label))
        passed("extra, missing, duplicated, reordered, and malformed fields are rejected")

        malformed_result_variants = {
            "extra": expected_result + "EXTRA=value\n",
            "missing": "\n".join(expected_result.splitlines()[:-1]) + "\n",
            "duplicate": expected_result.replace(
                "RESULT=success\n",
                "RESULT=success\nRESULT=success\n"),
            "reordered": expected_result.replace(
                "RESULT=success\nRESULT_CLASSIFICATION=tests-passed\n",
                "RESULT_CLASSIFICATION=tests-passed\nRESULT=success\n"),
            "no-newline": expected_result.rstrip("\n"),
        }
        for label, content in malformed_result_variants.items():
            Path("malformed-result.env").write_text(content)
            completed = run([
                "read-result",
                "--input", "malformed-result.env",
                "--field", "PR_HEAD_SHA",
            ])
            check(completed.returncode == 2,
                  "{} result was accepted".format(label))
        passed("result records reject extra, missing, duplicate, reordered, and malformed fields")

        identity_mutations = (
            ("CI_PIPELINE_ID=9001", "CI_PIPELINE_ID=9002"),
            ("PR_NUMBER=4028", "PR_NUMBER=4029"),
            ("PR_AUTHOR=rhc54", "PR_AUTHOR=kaamilbadami"),
            ("PR_BASE_REPOSITORY=openpmix/openpmix",
             "PR_BASE_REPOSITORY=other/openpmix"),
            ("PR_HEAD_REPOSITORY=rhc54/openpmix",
             "PR_HEAD_REPOSITORY=openpmix/openpmix"),
            ("PR_HEAD_SHA={}".format(sha), "PR_HEAD_SHA={}".format("0" * 40)),
            ("PR_SOURCE_POLICY=verified-author-fork",
             "PR_SOURCE_POLICY=same-repository"),
        )
        for index, (old, new) in enumerate(identity_mutations):
            Path("mismatch.env").write_text(expected_result.replace(old, new))
            completed = run([
                "validate-agreement",
                "--preparation", "preparation.env",
                "--result", "mismatch.env",
                "--pipeline-id", "9001",
            ])
            check(completed.returncode == 2,
                  "identity mismatch accepted: {}".format(index))
        passed("every stale or mismatched result identity fails agreement")

        target = Path("record-target.env")
        target.write_text(expected_preparation)
        Path("linked.env").symlink_to(target)
        completed = run([
            "read-preparation", "--input", "linked.env",
            "--field", "PR_HEAD_SHA",
        ])
        check(completed.returncode == 2, "symlinked record was accepted")
        os.link(str(target), "hardlinked.env")
        completed = run([
            "read-preparation", "--input", "hardlinked.env",
            "--field", "PR_HEAD_SHA",
        ])
        check(completed.returncode == 2, "hard-linked record was accepted")
        passed("record symlinks and hard links are rejected")

        Path(".ci-state").mkdir()
        Path(".ci-state/protected.env").write_text("protected")
        completed = run([
            "write-preparation",
            "--eligibility", "eligibility.env",
            "--pr-number", "4028",
            "--pipeline-id", "9001",
            "--result", "ready",
            "--output", ".ci-state/protected.env",
        ])
        check(completed.returncode == 2, "state output was accepted")
        check(Path(".ci-state/protected.env").read_text() == "protected",
              "state output was modified")
        passed(".ci-state is excluded from all prototype records")
    finally:
        os.chdir(str(original_cwd))

print("1..{}".format(tests))
PY
