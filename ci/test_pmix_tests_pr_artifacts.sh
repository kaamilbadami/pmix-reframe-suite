#!/bin/bash
set -euo pipefail

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
repo_root=$(cd -- "$script_dir/.." && pwd -P)

python3.11 - "$script_dir/pmix_tests_pr_artifacts.py" \
    "$repo_root/pmix_python_binding/reframe/pmix_tests_pr_python_smoke_test.py" <<'PY'
import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile


tool = Path(sys.argv[1]).resolve()
adapter = Path(sys.argv[2]).resolve()
sha = "0123456789abcdef0123456789abcdef01234567"
other_sha = "89abcdef0123456789abcdef0123456789abcdef"
execution_id = "0123456789abcdef0123456789abcdef"
pipeline_id = "456"
other_pipeline_id = "455"
passed_count = 0


def check(condition, message):
    if not condition:
        raise AssertionError(message)


def passed(message):
    global passed_count
    passed_count += 1
    print(f"ok - {message}")


def run(root, *arguments):
    return subprocess.run(
        [sys.executable, str(tool), *map(str, arguments)], cwd=root,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
    )


def trusted(author="kaamilbadami", head_repository="kaamilbadami/pmix-tests",
            base_repository="kaamilbadami/pmix-tests", fork="0", head_sha=sha):
    return (
        "PR_ELIGIBLE=1\n"
        "PR_NUMBER=42\n"
        f"PR_AUTHOR={author}\n"
        f"PR_HEAD_SHA={head_sha}\n"
        f"PR_HEAD_REPOSITORY={head_repository}\n"
        f"PR_BASE_REPOSITORY={base_repository}\n"
        f"PR_FROM_FORK={fork}\n"
    )


def preparation(result="ready", head_sha=sha, selected_pipeline=pipeline_id):
    return (
        "PMIX_TESTS_PR_PREPARATION_VERSION=2\n"
        f"CI_PIPELINE_ID={selected_pipeline}\n"
        "PR_REPOSITORY=kaamilbadami/pmix-tests\n"
        "PR_NUMBER=42\n"
        "PR_AUTHOR=kaamilbadami\n"
        f"PR_HEAD_SHA={head_sha}\n"
        "PR_FROM_FORK=0\n"
        f"PREPARATION_RESULT={result}\n"
    )


def evidence(root, *, completed=True, server_status=0, client_started=True,
             client_completed=True, client_status=0, malformed_client=False,
             duplicate_client=False):
    evidence_dir = root / "evidence"
    evidence_dir.mkdir(exist_ok=True)
    common = (
        "PMIX_TESTS_PR_RUN_EVIDENCE_VERSION=2\n"
        f"PR_HEAD_SHA={sha}\n"
        f"EXECUTION_ID={execution_id}\n"
        "PYTHON_PREFLIGHT_EXIT_CODE=0\n"
    )
    (evidence_dir / "pmix-tests-pr-run-started.env").write_text(common)
    if completed:
        (evidence_dir / "pmix-tests-pr-run-completed.env").write_text(
            common + f"SERVER_EXIT_CODE={server_status}\n"
        )
    client_common = (
        "PMIX_TESTS_PR_RUN_EVIDENCE_VERSION=2\n"
        f"PR_HEAD_SHA={sha}\n"
        f"EXECUTION_ID={execution_id}\n"
    )
    if client_started:
        (evidence_dir / "pmix-tests-pr-client-started.env").write_text(
            client_common)
    if client_completed:
        content = client_common + f"CLIENT_EXIT_CODE={client_status}\n"
        if malformed_client:
            content = content.replace("CLIENT_EXIT_CODE=", "CLIENT_STATUS=")
        (evidence_dir / "pmix-tests-pr-client-completed.env").write_text(content)
    if duplicate_client:
        (evidence_dir / "pmix-tests-pr-client-duplicate").write_text("duplicate\n")
    return evidence_dir


def report(result="pass", fail_phase=None, *, duplicate=False, jobid="12345",
           job_exitcode=0, filename=None):
    case = {
        "name": "PMIxTestsPRPythonSmokeTest",
        "unique_name": "PMIxTestsPRPythonSmokeTest",
        "filename": str(adapter if filename is None else filename),
        "fixture": False,
        "system": "frontier",
        "partition": "batch",
        "environ": "pmix_test",
        "scheduler": "slurm",
        "jobid": jobid,
        "job_exitcode": job_exitcode,
        "job_submit_time": 10.0,
        "job_completion_time": "1970-01-01T00:00:20+00:00",
        "job_completion_time_unix": 20.0,
        "time_run": 9.0,
        "stagedir": "__EVIDENCE_DIRECTORY__",
        "result": result,
        "fail_phase": fail_phase,
    }
    cases = [case, copy.deepcopy(case)] if duplicate else [case]
    return {
        "session_info": {
            "version": "4.10.0", "data_version": "4.2",
            "hostname": "frontier-login", "time_elapsed": 10.0,
            "time_start_unix": 10.0, "time_end_unix": 20.0,
            "uuid": "00000000-0000-0000-0000-000000000000",
        },
        "runs": [{
            "num_cases": len(cases),
            "num_failures": 0 if result == "pass" else len(cases),
            "num_aborted": 0, "num_skipped": 0, "run_index": 0,
            "testcases": cases,
        }],
        "restored_cases": [],
    }


def classify(root, document, status, *, make_evidence=True, completed=True,
             server_status=0, client_started=True, client_completed=True,
             client_status=0, malformed_client=False, duplicate_client=False):
    report_path = root / "run-report.json"
    if document is not None:
        if isinstance(document, bytes):
            report_path.write_bytes(document)
        else:
            document = copy.deepcopy(document)
            for run_record in document.get("runs", []):
                for case_record in run_record.get("testcases", []):
                    if case_record.get("stagedir") == "__EVIDENCE_DIRECTORY__":
                        case_record["stagedir"] = str(root / "evidence")
            report_path.write_text(json.dumps(document))
    if make_evidence:
        evidence(
            root, completed=completed, server_status=server_status,
            client_started=client_started, client_completed=client_completed,
            client_status=client_status, malformed_client=malformed_client,
            duplicate_client=duplicate_client)
    completed_process = run(
        root, "classify-report", "--preparation", "preparation.env",
        "--report", "run-report.json", "--evidence-directory", "evidence",
        "--execution-id", execution_id, "--reframe-status", str(status),
        "--pipeline-id", pipeline_id,
        "--output", "result.env",
    )
    result_path = root / "result.env"
    values = None
    if result_path.exists():
        values = dict(
            line.split("=", 1)
            for line in result_path.read_text().splitlines()
        )
    return completed_process, values


with tempfile.TemporaryDirectory() as temporary:
    base = Path(temporary)

    for author in ("kaamilbadami", "rhc54"):
        root = base / f"approved-{author}"
        root.mkdir()
        (root / "trusted.env").write_text(trusted(author=author))
        completed_process = run(
            root, "write-preparation", "--trusted-record", "trusted.env",
            "--pr-number", "42", "--result", "ready",
            "--pipeline-id", pipeline_id,
            "--output", "preparation.env",
        )
        check(completed_process.returncode == 0, f"approved author rejected: {author}")
    passed("the approved-author allowlist accepts exactly both existing authors")

    rejected = (
        (trusted(author="unknown"), "unapproved author"),
        (trusted(head_repository="attacker/pmix-tests", fork="1"), "fork PR"),
        (trusted(base_repository="attacker/pmix-tests"), "wrong base repository"),
        (trusted(head_sha=sha.upper()), "uppercase SHA"),
        (trusted(head_sha="g" * 40), "malformed SHA"),
    )
    for index, (record, label) in enumerate(rejected):
        root = base / f"rejected-{index}"
        root.mkdir()
        (root / "trusted.env").write_text(record)
        completed_process = run(
            root, "write-preparation", "--trusted-record", "trusted.env",
            "--pr-number", "42", "--result", "ready",
            "--pipeline-id", pipeline_id,
            "--output", "preparation.env",
        )
        check(completed_process.returncode == 2 and not (root / "preparation.env").exists(),
              f"{label} was accepted")
    passed("unapproved authors, forks, wrong repositories, and noncanonical SHAs are rejected")

    root = base / "changed"
    root.mkdir()
    (root / "trusted.env").write_text(trusted())
    completed_process = run(
        root, "write-preparation", "--trusted-record", "trusted.env",
        "--pr-number", "42", "--expected-sha", other_sha,
        "--pipeline-id", pipeline_id,
        "--result", "ready", "--output", "preparation.env",
    )
    check(completed_process.returncode == 2, "changed head was accepted")
    passed("head identity must match the originally selected SHA")

    root = base / "checkout"
    (root / "checkout/python").mkdir(parents=True)
    for name in ("server.py", "client.py"):
        (root / f"checkout/python/{name}").write_text("source\n")
    completed_process = run(root, "validate-checkout", "--checkout", "checkout")
    check(completed_process.returncode == 0, "safe checkout was rejected")
    passed("a real checkout root, python ancestor, and regular source files are accepted")

    def unsafe_checkout(label, mutate, argument="checkout"):
        case = base / f"unsafe-{label}"
        (case / "checkout/python").mkdir(parents=True)
        (case / "checkout/python/server.py").write_text("server\n")
        (case / "checkout/python/client.py").write_text("client\n")
        mutate(case)
        result = run(case, "validate-checkout", "--checkout", argument)
        check(result.returncode == 2, f"unsafe checkout passed: {label}")

    unsafe_checkout("root-symlink", lambda case: (
        (case / "checkout/python/server.py").unlink(),
        (case / "checkout/python/client.py").unlink(),
        (case / "checkout/python").rmdir(),
        (case / "checkout").rmdir(),
        (case / "real/python").mkdir(parents=True),
        (case / "checkout").symlink_to("real", target_is_directory=True),
    ))
    unsafe_checkout("python-symlink", lambda case: (
        (case / "checkout/python/server.py").unlink(),
        (case / "checkout/python/client.py").unlink(),
        (case / "checkout/python").rmdir(),
        (case / "outside").mkdir(),
        (case / "checkout/python").symlink_to("../outside", target_is_directory=True),
    ))
    for filename in ("server.py", "client.py"):
        unsafe_checkout(filename.replace(".", "-"), lambda case, name=filename: (
            (case / f"checkout/python/{name}").unlink(),
            (case / f"checkout/python/{name}").symlink_to(
                "client.py" if name == "server.py" else "server.py"),
        ))
    unsafe_checkout("path-escape", lambda case: None, "../checkout")
    def make_hardlink(case):
        outside = case / "outside-source"
        outside.write_text("outside\n")
        (case / "checkout/python/server.py").unlink()
        os.link(outside, case / "checkout/python/server.py")

    unsafe_checkout("hardlink", make_hardlink)
    passed("checkout-root, ancestor, source symlinks, traversal, and hard links are rejected")

    root = base / "classification"
    root.mkdir()
    (root / "preparation.env").write_text(preparation())
    completed_process, values = classify(root, report(), 0)
    check(completed_process.returncode == 0 and values["RESULT"] == "success"
          and values["CHECK_RAN"] == "1", "executed pass was not success")
    check(values["REPORT_SHA256"] == hashlib.sha256(
        (root / "run-report.json").read_bytes()).hexdigest(),
        "report digest was not recorded")
    passed("one completed matching ReFrame check with run evidence maps to success")

    client_error_cases = (
        ("client-nonzero", {"client_status": 1}),
        ("client-missing", {"client_started": False, "client_completed": False}),
        ("client-incomplete", {"client_completed": False}),
        ("client-malformed", {"malformed_client": True}),
        ("client-duplicate", {"duplicate_client": True}),
    )
    for label, options in client_error_cases:
        root = base / label
        root.mkdir()
        (root / "preparation.env").write_text(preparation())
        completed_process, values = classify(root, report(), 0, **options)
        check(completed_process.returncode == 2 and values["RESULT"] == "error",
              f"{label} became success or failure")
    passed("nonzero, missing, incomplete, malformed, and duplicate client evidence map to error")

    root = base / "failure"
    root.mkdir()
    (root / "preparation.env").write_text(preparation())
    completed_process, values = classify(
        root, report(result="fail", fail_phase="sanity"), 1, server_status=0)
    check(completed_process.returncode == 1 and values["RESULT"] == "failure"
          and values["CHECK_RAN"] == "1", "actual executed failure was not failure")
    passed("a completed selected workload failing in sanity maps to failure")

    error_cases = (
        ("missing-report", None, 2, False, True),
        ("malformed-report", b"{not-json", 1, True, True),
        ("duplicate-check", report(duplicate=True), 1, True, True),
        ("fixture-setup", report(result="fail_deps", fail_phase="setup"), 1, True, True),
        ("scheduler", report(result="fail", fail_phase="run", jobid=None), 1, False, True),
        ("timeout", report(result="fail", fail_phase="run_wait"), 1, True, False),
        ("python-127", report(result="fail", fail_phase="run_wait", job_exitcode=127), 1, True, True),
        ("loader-126", report(result="fail", fail_phase="run_wait", job_exitcode=126), 1, True, True),
        ("missing-pmix-binding", report(result="fail", fail_phase="run_wait", job_exitcode=1), 1, False, True),
        ("run-wait-exit-one", report(result="fail", fail_phase="run_wait", job_exitcode=1), 1, True, True),
        ("wrong-adapter", report(filename=base / "attacker.py"), 0, True, True),
    )
    for label, document, status, make_run_evidence, complete in error_cases:
        root = base / label
        root.mkdir()
        (root / "preparation.env").write_text(preparation())
        server_status = 0
        if label == "python-127":
            server_status = 127
        elif label == "loader-126":
            server_status = 126
        elif label == "run-wait-exit-one":
            server_status = 1
        completed_process, values = classify(
            root, document, status, make_evidence=make_run_evidence,
            completed=complete, server_status=server_status)
        check(completed_process.returncode == 2 and values["RESULT"] == "error",
              f"{label} did not map to error")
    passed("missing/malformed reports, duplicate checks, setup/scheduler/wait failures, timeouts, launch failures, missing bindings, and wrong adapters map to error")

    root = base / "final"
    root.mkdir()
    (root / "preparation.env").write_text(preparation())
    result_text = (
        "PMIX_TESTS_PR_EXECUTION_RESULT_VERSION=2\n"
        f"CI_PIPELINE_ID={pipeline_id}\n"
        f"PR_HEAD_SHA={other_sha}\n"
        "RESULT=success\n"
        "EXPECTED_CHECK=PMIxTestsPRPythonSmokeTest\n"
        "CHECK_RAN=1\n"
        f"REPORT_SHA256={'a' * 64}\n"
        f"EXECUTION_ID={execution_id}\n"
        "REFRAME_EXIT_STATUS=0\n"
    )
    (root / "result.env").write_text(result_text)
    decision = run(
        root, "final-decision", "--preparation", "preparation.env",
        "--result", "result.env", "--pipeline-id", pipeline_id)
    check(decision.returncode == 0 and decision.stdout.decode().strip() == f"{sha} error",
          "mismatched execution SHA replaced the preparation SHA")
    (root / "result.env").write_text(result_text.replace(
        f"PR_HEAD_SHA={other_sha}\n", f"PR_HEAD_SHA={sha}\nCONTEXT=evil\n"))
    decision = run(
        root, "final-decision", "--preparation", "preparation.env",
        "--result", "result.env", "--pipeline-id", pipeline_id)
    check(decision.stdout.decode().strip() == f"{sha} error",
          "extra execution fields overrode final policy")
    passed("SHA mismatch and execution attempts to add policy fields fail closed on the original SHA")

    root = base / "stale-pipeline"
    root.mkdir()
    (root / "preparation.env").write_text(
        preparation(selected_pipeline=other_pipeline_id))
    stale_execution = classify(root, report(), 0)
    check(stale_execution[0].returncode == 2
          and not (root / "result.env").exists(),
          "another pipeline's preparation started classification")

    (root / "preparation.env").write_text(preparation())
    stale_result = result_text.replace(
        f"CI_PIPELINE_ID={pipeline_id}\n",
        f"CI_PIPELINE_ID={other_pipeline_id}\n")
    (root / "result.env").write_text(stale_result)
    decision = run(
        root, "final-decision", "--preparation", "preparation.env",
        "--result", "result.env", "--pipeline-id", pipeline_id)
    check(decision.returncode == 0
          and decision.stdout.decode().strip() == f"{sha} error",
          "another pipeline's result selected success or failure")

    (root / "preparation.env").write_text(
        preparation(head_sha=other_sha, selected_pipeline=other_pipeline_id))
    decision = run(
        root, "final-decision", "--preparation", "preparation.env",
        "--result", "result.env", "--pipeline-id", pipeline_id)
    check(decision.returncode == 2 and not decision.stdout,
          "stale preparation caused status selection on a stale SHA")
    passed("pipeline binding rejects stale preparation and fails stale results closed on the current preparation SHA")

print(f"1..{passed_count}")
PY
