#!/bin/bash
set -euo pipefail

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)

python3.11 - "$script_dir/finalize_pmix_tests_pr_status.sh" \
    "$script_dir/pmix_tests_pr_artifacts.py" <<'PY'
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


finalizer_source = Path(sys.argv[1]).resolve()
records_source = Path(sys.argv[2]).resolve()
sha = "0123456789abcdef0123456789abcdef01234567"
other_sha = "89abcdef0123456789abcdef0123456789abcdef"
execution_id = "b" * 32
pipeline_id = "456"
stale_pipeline_id = "455"
pass_count = 0
case_count = 0


def check(condition, message):
    if not condition:
        raise AssertionError(message)


def passed(message):
    global pass_count
    pass_count += 1
    print(f"ok - {message}")


def preparation(result="ready", selected_sha=sha, selected_pipeline=pipeline_id):
    return (
        "PMIX_TESTS_PR_PREPARATION_VERSION=2\n"
        f"CI_PIPELINE_ID={selected_pipeline}\n"
        "PR_REPOSITORY=kaamilbadami/pmix-tests\n"
        "PR_NUMBER=42\n"
        "PR_AUTHOR=kaamilbadami\n"
        f"PR_HEAD_SHA={selected_sha}\n"
        "PR_FROM_FORK=0\n"
        f"PREPARATION_RESULT={result}\n"
    )


def execution(result, selected_sha=sha, selected_pipeline=pipeline_id):
    return (
        "PMIX_TESTS_PR_EXECUTION_RESULT_VERSION=2\n"
        f"CI_PIPELINE_ID={selected_pipeline}\n"
        f"PR_HEAD_SHA={selected_sha}\n"
        f"RESULT={result}\n"
        "EXPECTED_CHECK=PMIxTestsPRPythonSmokeTest\n"
        f"CHECK_RAN={'0' if result == 'error' else '1'}\n"
        f"REPORT_SHA256={'missing' if result == 'error' else 'a' * 64}\n"
        f"EXECUTION_ID={execution_id}\n"
        f"REFRAME_EXIT_STATUS={'unavailable' if result == 'error' else ('0' if result == 'success' else '1')}\n"
    )


class Case:
    def __init__(self, prep=preparation(), result=execution("success"), token=True):
        global case_count
        case_count += 1
        self.root = Path(temporary.name) / f"case-{case_count}"
        self.ci = self.root / "ci"
        self.bin = self.root / "bin"
        self.ci.mkdir(parents=True)
        self.bin.mkdir()
        python_path = self.bin / "fixed-python"
        python_path.symlink_to(shutil.which("python3.11"))
        finalizer_text = finalizer_source.read_text().replace(
            "/lustre/orion/scratch/kbadami/gen243/reframe_practice/pmix-py310/bin/python",
            str(python_path),
        )
        (self.ci / finalizer_source.name).write_text(finalizer_text)
        shutil.copy2(records_source, self.ci / records_source.name)
        reporter = self.ci / "report_pmix_tests_pr_status.sh"
        reporter.write_text(r'''#!/bin/bash
set -euo pipefail
[[ -n ${GITHUB_STATUS_TOKEN:-} ]] || exit 19
printf '%s\0' "$@" > report.args
''')
        reporter.chmod(0o755)
        (self.root / "preparation.env").write_text(prep)
        if result is not None:
            (self.root / "result.env").write_text(result)
        self.environment = {
            "PATH": f"{self.bin}:{os.environ['PATH']}",
            "HOME": str(self.root),
            "CI_PIPELINE_URL": "https://gitlab.example/group/project/-/pipelines/42",
            "CI_PIPELINE_ID": pipeline_id,
        }
        if token:
            self.environment["GITHUB_STATUS_TOKEN"] = "mock-token"

    def run(self, prep="preparation.env", result="result.env"):
        completed = subprocess.run(
            ["bash", f"ci/{finalizer_source.name}", prep, result],
            cwd=self.root, env=self.environment, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, check=False,
        )
        arguments = None
        record = self.root / "report.args"
        if record.exists():
            arguments = [value.decode() for value in
                         record.read_bytes().split(b"\0")[:-1]]
        return completed, arguments


with tempfile.TemporaryDirectory() as temporary_name:
    temporary = type("Temporary", (), {"name": temporary_name})()

    mappings = (
        ("success", "success", "Frontier PMIx tests PR check passed"),
        ("failure", "failure", "Frontier PMIx tests PR check failed"),
        ("error", "error", "Frontier PMIx tests PR infrastructure or validation error"),
    )
    for result, state, description in mappings:
        case = Case(result=execution(result))
        completed, arguments = case.run()
        check(completed.returncode == 0, completed.stderr.decode())
        check(arguments == [sha, state, description], "final status mapping changed")
    passed("strict execution outcomes map to the three fixed final statuses")

    for result in (None, "{not-json}\n", execution("success", other_sha),
                   execution("success").replace("CHECK_RAN=1", "CHECK_RAN=0"),
                   execution("success") + "STATE=success\nCONTEXT=evil\n"):
        case = Case(result=result)
        completed, arguments = case.run()
        check(completed.returncode == 0, completed.stderr.decode())
        check(arguments == [
            sha, "error",
            "Frontier PMIx tests PR infrastructure or validation error",
        ], "malformed execution artifact did not fail closed on original SHA")
    case = Case()
    target = case.root / "real-result.env"
    target.write_text(execution("success"))
    (case.root / "result.env").unlink()
    (case.root / "result.env").symlink_to(target.name)
    completed, arguments = case.run()
    check(completed.returncode == 0 and arguments[0] == sha
          and arguments[1] == "error",
          "symlinked execution result did not fail closed on original SHA")
    passed("missing, malformed, mismatched, and extra-field results map to error")

    case = Case(result=execution("success", selected_pipeline=stale_pipeline_id))
    completed, arguments = case.run()
    check(completed.returncode == 0 and arguments == [
        sha, "error",
        "Frontier PMIx tests PR infrastructure or validation error",
    ], "stale result selected success or failure")
    passed("another pipeline's result fails closed on the current preparation SHA")

    override = execution("failure", other_sha) + (
        "TARGET_SHA=ffffffffffffffffffffffffffffffffffffffff\n"
        "CONTEXT=attacker/context\nSTATE=success\nDESCRIPTION=attacker text\n"
    )
    case = Case(result=override)
    completed, arguments = case.run()
    check(completed.returncode == 0, completed.stderr.decode())
    check(arguments[0] == sha and arguments[1] == "error"
          and arguments[2] == "Frontier PMIx tests PR infrastructure or validation error",
          "execution artifact overrode trusted finalization fields")
    passed("execution data cannot override SHA, context, state, or description")

    case = Case(prep=preparation("error"), result=execution("success"))
    completed, arguments = case.run()
    check(completed.returncode == 0 and arguments[0] == sha
          and arguments[1] == "error", "failed preparation did not remain an error")
    passed("a preparation failure is finalized as error on its original SHA")

    bad_preparations = (
        preparation().replace("kaamilbadami\n", "unknown\n", 1),
        preparation().replace("PR_FROM_FORK=0", "PR_FROM_FORK=1"),
        preparation().replace(sha, sha.upper()),
        preparation() + "TARGET_SHA=" + other_sha + "\n",
        preparation(selected_sha=other_sha, selected_pipeline=stale_pipeline_id),
    )
    for prep in bad_preparations:
        case = Case(prep=prep)
        completed, arguments = case.run()
        check(completed.returncode == 2 and arguments is None,
              "invalid preparation caused a guessed status target")
    case = Case()
    target = case.root / "real-preparation.env"
    target.write_text(preparation())
    (case.root / "preparation.env").unlink()
    (case.root / "preparation.env").symlink_to(target.name)
    completed, arguments = case.run()
    check(completed.returncode == 2 and arguments is None,
          "symlinked preparation reached status reporting")
    passed("invalid or symlinked preparation data never causes a guessed status")

    case = Case(
        prep=preparation(selected_sha=other_sha,
                         selected_pipeline=stale_pipeline_id),
        result=execution("success", selected_sha=other_sha,
                         selected_pipeline=stale_pipeline_id),
    )
    completed, arguments = case.run()
    check(completed.returncode == 2 and arguments is None,
          "stale preparation reported to a stale SHA")
    passed("finalization never reports when preparation belongs to another pipeline")

    case = Case(token=False)
    completed, arguments = case.run()
    check(completed.returncode == 2 and arguments is None,
          "missing status token did not stop trusted finalization")
    passed("finalization requires its trusted status credential")

source = finalizer_source.read_text()
check("CI_JOB_STATUS" not in source and "after_script" not in source,
      "finalizer still depends on the execution job lifecycle")
check("final-decision" in source and "pmix_tests_pr_artifacts.py" in source,
      "finalizer bypasses the strict shared parser")
passed("production finalization is artifact-driven trusted code")

print(f"1..{pass_count}")
PY
