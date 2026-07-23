#!/bin/bash
set -euo pipefail

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
repo_root=$(cd -- "$script_dir/.." && pwd -P)

python3.11 - "$script_dir/run_trusted_pmix_tests_pr.sh" \
    "$script_dir/pmix_tests_pr_artifacts.py" \
    "$repo_root/pmix_python_binding/reframe/pmix_tests_pr_python_smoke_test.py" <<'PY'
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


wrapper_source = Path(sys.argv[1]).resolve()
records_source = Path(sys.argv[2]).resolve()
adapter_source = Path(sys.argv[3]).resolve()
sha = "0123456789abcdef0123456789abcdef01234567"
passed_count = 0
case_count = 0


def check(condition, message):
    if not condition:
        raise AssertionError(message)


def passed(message):
    global passed_count
    passed_count += 1
    print(f"ok - {message}")


git_stub = r'''#!/bin/bash
set -euo pipefail
for forbidden in GITHUB_PR_READ_TOKEN GITHUB_STATUS_TOKEN CI_JOB_TOKEN CI_REPOSITORY_URL CI_JOB_JWT; do
    [[ ! -v $forbidden ]] || exit 90
done
printf 'CALL\0' >> git.calls
printf '%s\0' "$@" >> git.calls

directory=
arguments=("$@")
index=0
while (( index < ${#arguments[@]} )); do
    case ${arguments[index]} in
        -C)
            directory=${arguments[index+1]}
            index=$((index + 2))
            ;;
        -c)
            index=$((index + 2))
            ;;
        *)
            command_name=${arguments[index]}
            command_index=$index
            break
            ;;
    esac
done

case $command_name in
    clone)
        destination=${arguments[${#arguments[@]}-1]}
        if [[ -f make-root-symlink ]]; then
            mkdir -p real-checkout/python
            ln -s ../../real-checkout "$destination"
        else
            mkdir -p "$destination/python"
        fi
        printf '%s\n' 'server from selected checkout' > "$destination/python/server.py"
        printf '%s\n' 'client from selected checkout' > "$destination/python/client.py"
        if [[ -f make-python-symlink ]]; then
            rm -rf "$destination/python"
            mkdir -p outside-python
            ln -s ../../outside-python "$destination/python"
        fi
        ;;
    fetch)
        ;;
    checkout)
        for ((i=command_index+1; i<${#arguments[@]}; i++)); do
            if [[ ${arguments[i]} == --detach ]]; then
                printf '%s\n' "${arguments[i+1]}" > "$directory/.mock-head"
            fi
        done
        ;;
    rev-parse)
        if [[ -f checkout-mismatch ]]; then
            printf '%040d\n' 9
        else
            cat "$directory/.mock-head"
        fi
        ;;
    symbolic-ref)
        [[ -f attached-head ]]
        ;;
    remote)
        printf '%s\n' 'https://github.com/kaamilbadami/pmix-tests.git'
        ;;
    show)
        cat "$directory/.mock-head"
        ;;
    *)
        exit 91
        ;;
esac
'''

runner_stub = r'''#!/usr/bin/env python3
import json
import os
from pathlib import Path

for name in ("GITHUB_PR_READ_TOKEN", "GITHUB_STATUS_TOKEN", "CI_JOB_TOKEN",
             "CI_REPOSITORY_URL", "CI_JOB_JWT"):
    if name in os.environ:
        raise SystemExit(92)
source = Path(os.environ["PMIX_TESTS_SOURCE_DIR"])
Path("consumed-source.txt").write_text(
    (source / "python/server.py").read_text()
    + (source / "python/client.py").read_text()
)
sha = os.environ["PMIX_TESTS_PR_HEAD_SHA"]
execution_id = os.environ["PMIX_TESTS_PR_EXECUTION_ID"]
prefix = Path("ci-pr-execution/reframe")
evidence = prefix / "stage/frontier/batch/pmix_test/PMIxTestsPRPythonSmokeTest"
evidence.mkdir(parents=True)
common = (
    "PMIX_TESTS_PR_RUN_EVIDENCE_VERSION=2\n"
    f"PR_HEAD_SHA={sha}\n"
    f"EXECUTION_ID={execution_id}\n"
    "PYTHON_PREFLIGHT_EXIT_CODE=0\n"
)
(evidence / "pmix-tests-pr-run-started.env").write_text(common)
(evidence / "pmix-tests-pr-run-completed.env").write_text(
    common + "SERVER_EXIT_CODE=0\n"
)
client_common = (
    "PMIX_TESTS_PR_RUN_EVIDENCE_VERSION=2\n"
    f"PR_HEAD_SHA={sha}\n"
    f"EXECUTION_ID={execution_id}\n"
)
(evidence / "pmix-tests-pr-client-started.env").write_text(client_common)
(evidence / "pmix-tests-pr-client-completed.env").write_text(
    client_common + "CLIENT_EXIT_CODE=0\n"
)
case = {
    "name": "PMIxTestsPRPythonSmokeTest",
    "unique_name": "PMIxTestsPRPythonSmokeTest",
    "filename": str(Path("pmix_python_binding/reframe/pmix_tests_pr_python_smoke_test.py").resolve()),
    "fixture": False, "system": "frontier", "partition": "batch",
    "environ": "pmix_test", "scheduler": "slurm",
    "jobid": "12345", "job_submit_time": 1.0,
    "job_exitcode": 0,
    "job_completion_time": "1970-01-01T00:00:02+00:00",
    "job_completion_time_unix": 2.0, "time_run": 1.0,
    "stagedir": str(evidence.resolve()),
    "result": "pass", "fail_phase": None,
}
report = {
    "session_info": {
        "version": "4.10.0", "data_version": "4.2",
        "hostname": "frontier-login", "time_elapsed": 1.0,
        "time_start_unix": 1.0, "time_end_unix": 2.0,
        "uuid": "00000000-0000-0000-0000-000000000000",
    },
    "runs": [{"num_cases": 1, "num_failures": 0, "num_aborted": 0,
              "num_skipped": 0, "run_index": 0, "testcases": [case]}],
    "restored_cases": [],
}
(prefix / "run-report.json").write_text(json.dumps(report))
'''


class Case:
    def __init__(self):
        global case_count
        case_count += 1
        self.root = Path(temporary.name) / f"case-{case_count}"
        self.ci = self.root / "ci"
        self.bin = self.root / "bin"
        self.adapter = self.root / "pmix_python_binding/reframe"
        self.ci.mkdir(parents=True)
        self.bin.mkdir()
        self.adapter.mkdir(parents=True)
        python_path = self.bin / "python"
        git_path = self.bin / "git"
        wrapper_text = wrapper_source.read_text().replace(
            "/lustre/orion/gen243/proj-shared/pmix-reframe-ci-tools/pmix-py310/bin/python",
            str(python_path),
        ).replace("/usr/bin/git", str(git_path))
        (self.ci / wrapper_source.name).write_text(wrapper_text)
        shutil.copy2(records_source, self.ci / records_source.name)
        shutil.copy2(adapter_source, self.adapter / adapter_source.name)
        runner = self.ci / "run_trusted_pmix_tests_pr_test.sh"
        runner.write_text(runner_stub)
        runner.chmod(0o755)
        git_path.write_text(git_stub)
        git_path.chmod(0o755)
        python_path.symlink_to(shutil.which("python3.11"))
        preparation_dir = self.root / "ci-pr-preparation"
        preparation_dir.mkdir()
        (preparation_dir / "preparation.env").write_text(
            "PMIX_TESTS_PR_PREPARATION_VERSION=2\n"
            "CI_PIPELINE_ID=456\n"
            "PR_REPOSITORY=kaamilbadami/pmix-tests\n"
            "PR_NUMBER=42\n"
            "PR_AUTHOR=kaamilbadami\n"
            f"PR_HEAD_SHA={sha}\n"
            "PR_FROM_FORK=0\n"
            "PREPARATION_RESULT=ready\n"
        )
        self.environment = {
            "PATH": f"{self.bin}:{os.environ['PATH']}",
            "HOME": str(self.root / "home"),
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "PMIX_PYTHON": str(python_path),
            "CI_PIPELINE_ID": "456",
        }
        (self.root / "home").mkdir()

    def run(self):
        return subprocess.run(
            ["bash", f"ci/{wrapper_source.name}"], cwd=self.root,
            env=self.environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            check=False,
        )


def calls(path):
    fields = [field.decode() for field in path.read_bytes().split(b"\0") if field]
    result = []
    current = None
    for field in fields:
        if field == "CALL":
            if current is not None:
                result.append(current)
            current = []
        else:
            current.append(field)
    if current is not None:
        result.append(current)
    return result


with tempfile.TemporaryDirectory() as temporary_name:
    temporary = type("Temporary", (), {"name": temporary_name})()

    case = Case()
    stale = case.root / "reports"
    stale.mkdir()
    (stale / "run-report.json").write_text("stale root report\n")
    completed = case.run()
    check(completed.returncode == 0, completed.stderr.decode())
    git_calls = calls(case.root / "git.calls")
    clone_call = next(call for call in git_calls if "clone" in call)
    check(clone_call[-2:] == [
        "https://github.com/kaamilbadami/pmix-tests.git",
        "ci-pr-execution/pmix-tests",
    ], "clone did not use the fixed repository and path")
    check("--no-checkout" in clone_call and "--no-recurse-submodules" in clone_call,
          "clone can run checkout or submodule behavior")
    check("http.followRedirects=false" in clone_call,
          "clone permits HTTP redirects")
    fetch_call = next(call for call in git_calls if "fetch" in call)
    check("http.followRedirects=false" in fetch_call,
          "fetch permits HTTP redirects")
    checkout_call = next(call for call in git_calls if "checkout" in call)
    check("--detach" in checkout_call
          and checkout_call[checkout_call.index("--detach") + 1] == sha,
          "checkout was not detached at the exact SHA")
    all_arguments = "\n".join("\0".join(call) for call in git_calls)
    for forbidden in ("refs/heads/", "refs/tags/", "evil.invalid", "merge"):
        check(forbidden not in all_arguments, f"untrusted ref or URL reached Git: {forbidden}")
    passed("only the fixed clone URL and exact detached SHA reach Git")

    consumed = (case.root / "consumed-source.txt").read_text()
    check("server from selected checkout" in consumed
          and "client from selected checkout" in consumed,
          "selected source files were not consumed")
    result = (case.root / "ci-pr-execution/result.env").read_text()
    check("RESULT=success\n" in result and f"PR_HEAD_SHA={sha}\n" in result,
          "trusted report classification did not publish success on the exact SHA")
    check("CI_PIPELINE_ID=456\n" in result,
          "execution result was not bound to the current pipeline")
    check((stale / "run-report.json").read_text() == "stale root report\n",
          "root stale report was read or changed")
    passed("the fixed smoke source is consumed and only the job-specific report is classified")

    case = Case()
    stale_output = case.root / "ci-pr-execution"
    stale_output.mkdir()
    (stale_output / "result.env").write_text(
        "PMIX_TESTS_PR_EXECUTION_RESULT_VERSION=2\n"
        "CI_PIPELINE_ID=455\n"
        f"PR_HEAD_SHA={'8' * 40}\n"
        "RESULT=success\n"
        "EXPECTED_CHECK=PMIxTestsPRPythonSmokeTest\n"
        "CHECK_RAN=1\n"
        f"REPORT_SHA256={'a' * 64}\n"
        f"EXECUTION_ID={'b' * 32}\n"
        "REFRAME_EXIT_STATUS=0\n"
    )
    preparation_path = case.root / "ci-pr-preparation/preparation.env"
    preparation_path.write_text(
        preparation_path.read_text().replace(
            "CI_PIPELINE_ID=456", "CI_PIPELINE_ID=455"))
    completed = case.run()
    check(completed.returncode == 2
          and not (case.root / "ci-pr-execution/result.env").exists()
          and not (case.root / "git.calls").exists(),
          "stale preparation or result survived to checkout")
    case = Case()
    target = case.root / "stale-execution-target"
    target.mkdir()
    (target / "sentinel").write_text("preserve\n")
    stale_link = case.root / "ci-pr-execution"
    stale_link.symlink_to(target.name, target_is_directory=True)
    preparation_path = case.root / "ci-pr-preparation/preparation.env"
    preparation_path.write_text(
        preparation_path.read_text().replace(
            "CI_PIPELINE_ID=456", "CI_PIPELINE_ID=455"))
    completed = case.run()
    check(completed.returncode == 2 and stale_link.is_dir()
          and not stale_link.is_symlink()
          and not (stale_link / "result.env").exists()
          and (target / "sentinel").read_text() == "preserve\n"
          and not (case.root / "git.calls").exists(),
          "execution cleanup followed a stale output symlink")
    passed("execution removes stale output and rejects another pipeline's preparation before Git")

    case = Case()
    (case.root / "checkout-mismatch").write_text("1\n")
    completed = case.run()
    check(completed.returncode == 2 and not (case.root / "consumed-source.txt").exists(),
          "checkout SHA mismatch reached the workload")
    check("RESULT=error\n" in (case.root / "ci-pr-execution/result.env").read_text(),
          "checkout mismatch lost the fail-closed result")
    passed("a checkout resolving to a different commit is rejected before execution")

    case = Case()
    (case.root / "attached-head").write_text("1\n")
    completed = case.run()
    check(completed.returncode == 2 and not (case.root / "consumed-source.txt").exists(),
          "attached checkout reached the workload")
    passed("an attached branch checkout is rejected")

    case = Case()
    (case.root / "make-root-symlink").write_text("1\n")
    completed = case.run()
    check(completed.returncode != 0 and not (case.root / "consumed-source.txt").exists(),
          "symlinked checkout root reached the workload")
    passed("a symlinked checkout root is rejected")

    case = Case()
    for name, value in (
        ("GITHUB_PR_READ_TOKEN", "read"),
        ("GITHUB_STATUS_TOKEN", "status"),
        ("CI_JOB_TOKEN", "job"),
        ("CI_REPOSITORY_URL", "https://token@example.invalid/repo"),
        ("CI_JOB_JWT", "jwt"),
    ):
        case.environment[name] = value
    completed = case.run()
    check(completed.returncode == 2 and not (case.root / "git.calls").exists(),
          "forbidden credential environment reached Git")
    passed("known GitHub, GitLab, repository, and JWT credentials fail before Git")

source_text = wrapper_source.read_text()
check(source_text.count("https://github.com/kaamilbadami/pmix-tests.git") == 1,
      "production clone URL is not one fixed constant")
for forbidden in ("PR_CLONE_URL", "PR_HEAD_REF", "source pr.json", ".ci-state"):
    check(forbidden not in source_text, f"execution wrapper references unsafe input: {forbidden}")
passed("production execution uses no PR URL/ref fields or shared PMIx state")

print(f"1..{passed_count}")
PY
