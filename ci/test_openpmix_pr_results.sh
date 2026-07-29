#!/bin/bash
set -euo pipefail

script_dir=$(cd -- "${BASH_SOURCE[0]%/*}" && pwd -P)
python3 - "$script_dir/openpmix_pr_artifacts.py" <<'PY'
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile


tool = Path(sys.argv[1]).resolve()
sha = "5f08dede7495ce1b332691b3ac7eaf6478a979b9"
execution_id = "c" * 32
tests = (
    "PMIxPythonScalingTest",
    "PMIxPythonScalingMultinodeTest",
    "PMIxPythonMappingPPRNodeTest",
    "PMIxPythonMappingPPRL3CacheTest",
    "PMIxPythonWorkerThreadsCompat1Test",
    "PMIxPythonWorkerThreadsCompat2Test",
    "PMIxPythonTargetedCompatTest",
    "PMIxPythonMixedThreadCompatTest",
    "PMIxPythonChildTimeoutTest",
    "PMIxPythonEventFailurePropagationTest",
    "PMIxPythonTargetHostFailurePropagationTest",
)
fixtures = (
    "fetch_libevent", "build_libevent", "fetch_pmix", "build_pmix",
    "fetch_prrte", "build_prrte",
)
passed_count = 0


def check(condition, message):
    if not condition:
        raise AssertionError(message)


def passed(message):
    global passed_count
    passed_count += 1
    print("ok - " + message)


def run(*arguments):
    return subprocess.run(
        [sys.executable, str(tool)] + list(arguments),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
    )


def report(failed=None, phase="run", reason="assertion failed"):
    cases = []
    for name in fixtures + tests:
        is_fixture = name in fixtures
        case = {
            "name": name,
            "system": "frontier",
            "partition": "batch",
            "environ": "pmix_test",
            "scheduler": "slurm",
            "fixture": is_fixture,
            "result": "fail" if name == failed else "pass",
        }
        if name == failed:
            case["fail_phase"] = phase
            case["fail_reason"] = reason
        cases.append(case)
    failure_count = 1 if failed else 0
    return {
        "session_info": {"version": "4.10.0", "data_version": "4.2"},
        "runs": [{
            "run_index": 0,
            "num_cases": 17,
            "num_failures": failure_count,
            "num_aborted": 0,
            "num_skipped": 0,
            "testcases": cases,
        }],
        "restored_cases": [],
    }


with tempfile.TemporaryDirectory(prefix="openpmix-result-test-") as temporary:
    root = Path(temporary)
    os.chdir(str(root))
    Path("preparation.env").write_text(
        "OPENPMIX_PR_PREPARATION_VERSION=1\n"
        "CI_PIPELINE_ID=9001\n"
        "PR_NUMBER=4028\n"
        "PR_AUTHOR=rhc54\n"
        "PR_BASE_REPOSITORY=openpmix/openpmix\n"
        "PR_HEAD_REPOSITORY=rhc54/openpmix\n"
        "PR_HEAD_SHA={}\n"
        "PR_SOURCE_POLICY=verified-author-fork\n"
        "PREPARATION_RESULT=ready\n".format(sha)
    )
    completed = run(
        "write-checkout", "--preparation", "preparation.env",
        "--pipeline-id", "9001", "--fetched-sha", sha,
        "--checked-out-sha", sha, "--output", "checkout.env",
    )
    check(completed.returncode == 0, completed.stderr.decode())

    outcomes = (
        (None, "run", "", "0", 0, "success", "tests-passed"),
        ("PMIxPythonScalingTest", "sanity", "assertion", "1", 1,
         "failure", "test-failure"),
        ("build_pmix", "compile", "make failed", "1", 1,
         "failure", "source-build-failure"),
        ("fetch_libevent", "setup", "download failed", "1", 2,
         "error", "fixture-error"),
        ("PMIxPythonScalingTest", "run", "slurm job timeout", "1", 2,
         "error", "scheduler-error"),
    )
    for index, (failed, phase, reason, runner, status, result,
                classification) in enumerate(outcomes):
        Path("report.json").write_text(json.dumps(
            report(failed, phase, reason), sort_keys=True))
        completed = run(
            "classify-report", "--preparation", "preparation.env",
            "--checkout", "checkout.env", "--report", "report.json",
            "--pipeline-id", "9001", "--execution-id", execution_id,
            "--runner-exit-status", runner,
            "--output", "result-{}.env".format(index),
        )
        check(completed.returncode == status, completed.stderr.decode())
        text = Path("result-{}.env".format(index)).read_text()
        check("RESULT={}\n".format(result) in text, "wrong result")
        check("RESULT_CLASSIFICATION={}\n".format(classification) in text,
              "wrong classification")
    passed("strict reports distinguish success, test, build, fixture, and scheduler outcomes")

    malformed = report()
    malformed["runs"][0]["testcases"].pop()
    malformed["runs"][0]["num_cases"] = 16
    Path("malformed.json").write_text(json.dumps(malformed))
    completed = run(
        "classify-report", "--preparation", "preparation.env",
        "--checkout", "checkout.env", "--report", "malformed.json",
        "--pipeline-id", "9001", "--execution-id", execution_id,
        "--runner-exit-status", "2", "--output", "malformed-result.env",
    )
    check(completed.returncode == 2, completed.stderr.decode())
    check("RESULT_CLASSIFICATION=malformed-report\n" in
          Path("malformed-result.env").read_text(),
          "malformed report was not classified")
    passed("missing, extra, duplicated, or incomplete suite reports fail closed")

    changed = Path("checkout.env").read_text().replace(
        "CI_PIPELINE_ID=9001", "CI_PIPELINE_ID=9002")
    Path("stale-checkout.env").write_text(changed)
    completed = run(
        "classify-report", "--preparation", "preparation.env",
        "--checkout", "stale-checkout.env", "--report", "report.json",
        "--pipeline-id", "9001", "--execution-id", execution_id,
        "--runner-exit-status", "0", "--output", "stale-result.env",
    )
    check(completed.returncode == 2 and not Path("stale-result.env").exists(),
          "stale checkout artifact was accepted")
    passed("stale checkout evidence cannot produce a result")

    Path("result.env").write_text(Path("result-0.env").read_text())
    Path("stale-result.env").write_text(
        Path("result.env").read_text().replace(
            "PR_HEAD_SHA={}".format(sha),
            "PR_HEAD_SHA={}".format("0" * 40)))
    completed = run(
        "write-final", "--preparation", "preparation.env",
        "--result", "stale-result.env", "--pipeline-id", "9001",
        "--output", "final.env",
    )
    check(completed.returncode == 2 and not Path("final.env").exists(),
          "stale result was finalized")
    passed("finalization rejects pipeline or exact-SHA disagreement")

print("1..{}".format(passed_count))
PY
