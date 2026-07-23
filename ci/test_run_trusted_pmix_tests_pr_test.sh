#!/bin/bash
set -euo pipefail

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
repo_root=$(cd -- "$script_dir/.." && pwd -P)

python3.11 - "$script_dir/run_trusted_pmix_tests_pr_test.sh" \
    "$script_dir/pmix_tests_pr_artifacts.py" \
    "$repo_root/pmix_python_binding/reframe/pmix_tests_pr_hello_world_test.py" <<'PY'
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile


runner_source = Path(sys.argv[1]).resolve()
records_source = Path(sys.argv[2]).resolve()
adapter_source = Path(sys.argv[3]).resolve()
real_python = shutil.which("python3.11")
sha = "0123456789abcdef0123456789abcdef01234567"
execution_id = "0123456789abcdef0123456789abcdef"
passed_count = 0
case_count = 0


def check(condition, message):
    if not condition:
        raise AssertionError(message)


def passed(message):
    global passed_count
    passed_count += 1
    print(f"ok - {message}")


python_stub = r'''#!/bin/bash
set -euo pipefail
if [[ ${1:-} == -c && ${2:-} == *platform.python_version* ]]; then
    printf '%s\n' "${MOCK_PYTHON_VERSION:-3.10.20}"
    exit 0
fi
if [[ ${1:-} == -c && ${2:-} == *Cython.__version__* ]]; then
    printf '%s\n' "${MOCK_CYTHON_VERSION:-3.2.6}"
    exit 0
fi
if [[ ${1:-} == */pmix_tests_pr_artifacts.py ]]; then
    exec "$REAL_PYTHON" "$@"
fi
exit 71
'''

reframe_stub = r'''#!/bin/bash
set -euo pipefail
if [[ ${1:-} == --version ]]; then
    printf '%s\n' "${MOCK_REFRAME_VERSION:-4.10.0}"
    exit 0
fi
for forbidden in GITHUB_PR_READ_TOKEN GITHUB_STATUS_TOKEN CI_JOB_TOKEN CI_REPOSITORY_URL CI_JOB_JWT PROTECTED_DEPLOY_PASSWORD; do
    [[ ! -v $forbidden ]] || exit 73
done
printf '%s\0' "$@" > reframe.args
env -0 > reframe.env
cat "$PMIX_TESTS_SOURCE_DIR/prrte/hello_world/build.sh" \
    "$PMIX_TESTS_SOURCE_DIR/prrte/hello_world/hello.c" > consumed-source.txt
exit "${MOCK_REFRAME_STATUS:-0}"
'''


class Case:
    def __init__(self):
        global case_count
        case_count += 1
        self.root = Path(temporary.name) / f"case-{case_count}"
        self.ci = self.root / "ci"
        self.adapter = self.root / "pmix_python_binding/reframe"
        self.source = (
            self.root /
            "ci-pr-execution/pmix-tests/prrte/hello_world"
        )
        self.tools = self.root / "fixed-tools"
        self.pythonpath = self.root / "fixed-reframe-pythonpath"
        self.ci.mkdir(parents=True)
        self.adapter.mkdir(parents=True)
        self.source.mkdir(parents=True)
        self.tools.mkdir()
        self.pythonpath.mkdir()
        self.pmix_python = self.tools / "python"
        self.rfm_bin = self.tools / "reframe"
        self.pmix_python.write_text(python_stub)
        self.rfm_bin.write_text(reframe_stub)
        self.pmix_python.chmod(0o755)
        self.rfm_bin.chmod(0o755)

        runner_text = runner_source.read_text()
        runner_text = runner_text.replace(
            "/lustre/orion/gen243/proj-shared/pmix-reframe-ci-tools/pmix-py310/bin/python",
            str(self.pmix_python),
        ).replace(
            "/lustre/orion/gen243/proj-shared/pmix-reframe-ci-tools/reframe-4.10/bin/reframe",
            str(self.rfm_bin),
        ).replace(
            "/lustre/orion/gen243/proj-shared/pmix-reframe-ci-tools/reframe-4.10/lib/python3.11/site-packages",
            str(self.pythonpath),
        )
        (self.ci / runner_source.name).write_text(runner_text)
        shutil.copy2(records_source, self.ci / records_source.name)
        shutil.copy2(adapter_source, self.adapter / adapter_source.name)
        (self.root / "sysconfig.yaml").write_text("systems: []\n")
        (self.source / "build.sh").write_text("build selected source\n")
        (self.source / "hello.c").write_text("hello selected source\n")
        self.environment = {
            "PATH": "/usr/bin:/bin",
            "HOME": str(self.root / "home"),
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "REAL_PYTHON": real_python,
            "PYTHONPATH": str(self.pythonpath),
            "PMIX_PYTHON": str(self.pmix_python),
            "RFM_BIN": str(self.rfm_bin),
            "PMIX_TESTS_SOURCE_DIR": str(self.root / "ci-pr-execution/pmix-tests"),
            "PMIX_TESTS_PR_HEAD_SHA": sha,
            "PMIX_TESTS_PR_EXECUTION_ID": execution_id,
            "MOCK_REFRAME_STATUS": "0",
        }
        (self.root / "home").mkdir()

    def run(self, arguments=()):
        return subprocess.run(
            ["/bin/bash", f"ci/{runner_source.name}", *arguments],
            cwd=self.root, env=self.environment,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        )


with tempfile.TemporaryDirectory() as temporary_name:
    temporary = type("Temporary", (), {"name": temporary_name})()

    case = Case()
    completed = case.run()
    check(completed.returncode == 0, completed.stderr.decode())
    args = [item.decode() for item in
            (case.root / "reframe.args").read_bytes().split(b"\0")[:-1]]
    check(args == [
        "-C", str(case.root / "sysconfig.yaml"),
        "-c", str(case.adapter / adapter_source.name),
        "-r", "--system=frontier:batch", "-n", "^PMIxTestsPRHelloWorldTest$",
        "--keep-stage-files", "--prefix", str(case.root / "ci-pr-execution/reframe"),
        "--report-file", str(case.root / "ci-pr-execution/reframe/run-report.json"),
    ], "trusted ReFrame command changed")
    consumed = (case.root / "consumed-source.txt").read_text()
    check("build selected source" in consumed and "hello selected source" in consumed,
          "ReFrame did not receive both exact hello-world source files")
    passed("fixed installed tools select only the exact PR hello-world check and source")

    reframe_environment = (case.root / "reframe.env").read_bytes()
    for secret_name in (
        b"GITHUB_PR_READ_TOKEN=", b"GITHUB_STATUS_TOKEN=", b"CI_JOB_TOKEN=",
        b"CI_REPOSITORY_URL=", b"CI_JOB_JWT=", b"PROTECTED_DEPLOY_PASSWORD=",
    ):
        check(secret_name not in reframe_environment,
              f"credential reached ReFrame: {secret_name!r}")
    passed("ReFrame receives no GitHub, GitLab, JWT, or protected credential variable")

    for status in (1, 7, 143):
        case = Case()
        case.environment["MOCK_REFRAME_STATUS"] = str(status)
        completed = case.run()
        check(completed.returncode == status,
              f"raw ReFrame status {status} was remapped by the launcher")
    passed("the launcher preserves ReFrame status for JSON-based classification")

    for version_name, value in (
        ("MOCK_PYTHON_VERSION", "3.10.19"),
        ("MOCK_CYTHON_VERSION", "3.1.0"),
        ("MOCK_REFRAME_VERSION", "4.9.0"),
    ):
        case = Case()
        case.environment[version_name] = value
        completed = case.run()
        check(completed.returncode == 2 and not (case.root / "reframe.args").exists(),
              f"wrong fixed tool version reached ReFrame: {version_name}")
    passed("Python, Cython, and ReFrame version mismatches fail before execution")

    for secret_name in (
        "GITHUB_PR_READ_TOKEN", "GITHUB_STATUS_TOKEN", "CI_JOB_TOKEN",
        "CI_REPOSITORY_URL", "CI_JOB_JWT",
    ):
        case = Case()
        case.environment[secret_name] = "must-not-reach-child"
        completed = case.run()
        check(completed.returncode == 2 and not (case.root / "reframe.args").exists(),
              f"{secret_name} reached ReFrame")
    passed("known credentials are rejected before Python validation or ReFrame")

    for filename in ("build.sh", "hello.c"):
        case = Case()
        target = case.root / f"{filename}.target"
        target.write_text("preserve\n")
        (case.source / filename).unlink()
        (case.source / filename).symlink_to(target)
        completed = case.run()
        check(completed.returncode == 2 and not (case.root / "reframe.args").exists(),
              f"symlinked {filename} reached ReFrame")
        check(target.read_text() == "preserve\n", f"symlink target changed: {filename}")
    passed("symlinked hello-world build and source files are rejected before ReFrame")

text = adapter_source.read_text()
for required in (
    'self.job.options = ["--export=NIL", "--nodes=1"]',
    '"prrte", "hello_world"',
    'num_tasks = 2', 'num_tasks_per_node = 2', 'time_limit = "5m"',
    '/bin/bash ./build.sh',
    'exec prterun --host "${{nodes[0]}}:2" -n 2 --map-by ppr:2:node ./hello',
    'pmix-tests-pr-run-started.env', 'pmix-tests-pr-run-completed.env',
    'PMIX_TESTS_PR_RUN_EVIDENCE_VERSION=4',
    '"WORKLOAD_EXIT_CODE=$workload_status"',
    '"PMIX_COMMIT=$pmix_commit"',
    '"HOME": self.stagedir', '"TMPDIR": f"{self.stagedir}/pmix-tests-pr-tmp"',
    'sn.assert_eq(self.job.exitcode, 0)',
    'sn.assert_not_found(r"ERROR:", self.stdout)',
    'sn.assert_not_found(r"ERROR:", self.stderr)',
):
    check(required in text, f"adapter lost required behavior: {required}")
check("--export=NONE" not in text, "adapter retained Slurm get-user-env mode")
start_write = text.index("/bin/mv -f -- \"$start_tmp\"")
build_call = text.index("/bin/bash ./build.sh", start_write)
launch_call = text.index("exec prterun", build_call)
status_capture = text.index("workload_status=$?", launch_call)
completion_write = text.index(
    '"WORKLOAD_EXIT_CODE=$workload_status"', status_capture
)
final_exit = text.index('exit "$workload_status"', completion_write)
check(start_write < build_call < launch_call < status_capture <
      completion_write < final_exit,
      "start/build/launch/completion evidence ordering changed")
for forbidden in (
    "client-under-test", "CLIENT_EXIT_CODE", "PYTHON_PREFLIGHT",
    "PYTHONUNBUFFERED", "WORKLOAD_TIMED_OUT", "SERVER_EXIT_CODE",
    "run_pmix_tests_pr_workload", "pterm", "sleep 5", "prte --no-ready-msg",
):
    check(forbidden not in text, f"adapter retained obsolete behavior: {forbidden}")
passed("the adapter uses one foreground build/launch and atomic completion evidence")

hello_output = (
    "1/2 [1/2] Hello World from frontier00002 (pid 22)\n"
    "0/2 [0/2] Hello World from frontier00002 (pid 21)\n"
)
line_pattern = re.compile(
    r"(?m)^([01])/2 \[([01])/2\] Hello World from "
    r"\S+ \(pid [1-9][0-9]*\)$"
)
matches = line_pattern.findall(hello_output)
check(len(matches) == 2
      and {int(rank) for rank, _ in matches} == {0, 1}
      and {int(local_rank) for _, local_rank in matches} == {0, 1},
      "unordered two-rank hello-world output was not accepted")
passed("sanity semantics accept exactly the unordered ranks and local ranks 0 and 1")

runner_text = runner_source.read_text()
for forbidden in ("pip install", "-m venv", "pip --upgrade"):
    check(forbidden not in runner_text, f"launcher retained package bootstrap: {forbidden}")
passed("launcher performs no virtualenv creation or network package bootstrap")

print(f"1..{passed_count}")
PY
