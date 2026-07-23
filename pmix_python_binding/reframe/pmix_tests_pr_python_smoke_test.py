import os
import shlex
import sys

# Allow this test to import build classes from the repository root.
SUITE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO_ROOT = os.path.dirname(SUITE_DIR)
sys.path.insert(0, REPO_ROOT)

import reframe as rfm
import reframe.utility.sanity as sn

from reframe.core.backends import getlauncher
from reframe.core.builtins import fixture, run_before, sanity_function

from libevent_build_class import build_libevent
from pmix_build_class import build_pmix


PR_SOURCE_DIR = os.environ.get("PMIX_TESTS_SOURCE_DIR", "")
PYTHON = os.environ.get("PMIX_PYTHON", "python3")
PR_HEAD_SHA = os.environ.get("PMIX_TESTS_PR_HEAD_SHA", "")
EXECUTION_ID = os.environ.get("PMIX_TESTS_PR_EXECUTION_ID", "")


class _PMIxTestsPRPythonSmokeBase(rfm.RunOnlyRegressionTest):
    valid_systems = ["frontier:batch"]
    valid_prog_environs = ["pmix_test"]

    pmix = fixture(build_pmix, scope="environment")
    libevent = fixture(build_libevent, scope="environment")

    sourcesdir = None
    executable = "/bin/bash"
    num_tasks = 1
    num_tasks_per_node = 1
    time_limit = "10m"
    tags = {
        f"pmix-tests-pr-sha:{PR_HEAD_SHA}",
        f"pmix-tests-pr-execution:{EXECUTION_ID}",
    }

    @run_before("run")
    def prepare_test(self):
        source_dir = os.path.abspath(PR_SOURCE_DIR)
        server_source = os.path.join(source_dir, "python", "server.py")
        client_source = os.path.join(source_dir, "python", "client.py")
        client_wrapper = f"""#!{PYTHON}
import os
from pathlib import Path
import subprocess
import sys


STAGE_DIRECTORY = Path(__file__).resolve().parent
CLIENT_UNDER_TEST = STAGE_DIRECTORY / "client-under-test.py"
CLIENT_STARTED = STAGE_DIRECTORY / "pmix-tests-pr-client-started.env"
CLIENT_COMPLETED = STAGE_DIRECTORY / "pmix-tests-pr-client-completed.env"
CLIENT_DUPLICATE = STAGE_DIRECTORY / "pmix-tests-pr-client-duplicate"
PR_HEAD_SHA = {PR_HEAD_SHA!r}
EXECUTION_ID = {EXECUTION_ID!r}


def create_exclusive(path):
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_NOFOLLOW", 0)
    return os.open(path, flags, 0o600)


def write_all(descriptor, content):
    view = memoryview(content)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise OSError("could not write client evidence")
        view = view[written:]
    os.fsync(descriptor)


common = (
    "PMIX_TESTS_PR_RUN_EVIDENCE_VERSION=2\\n"
    "PR_HEAD_SHA=" + PR_HEAD_SHA + "\\n"
    "EXECUTION_ID=" + EXECUTION_ID + "\\n"
).encode("ascii")

try:
    started_fd = create_exclusive(CLIENT_STARTED)
except FileExistsError:
    try:
        duplicate_fd = create_exclusive(CLIENT_DUPLICATE)
        os.close(duplicate_fd)
    except OSError:
        pass
    raise SystemExit(125)

try:
    write_all(started_fd, common)
finally:
    os.close(started_fd)

try:
    completed_fd = create_exclusive(CLIENT_COMPLETED)
except FileExistsError:
    try:
        duplicate_fd = create_exclusive(CLIENT_DUPLICATE)
        os.close(duplicate_fd)
    except OSError:
        pass
    raise SystemExit(125)

try:
    completed = subprocess.run(
        [sys.executable, str(CLIENT_UNDER_TEST), *sys.argv[1:]],
        check=False,
    )
    client_status = completed.returncode
    if client_status < 0:
        client_status = min(255, 128 - client_status)
    elif client_status > 255:
        client_status = 255
    write_all(
        completed_fd,
        common + ("CLIENT_EXIT_CODE=" + str(client_status) + "\\n").encode("ascii"),
    )
finally:
    os.close(completed_fd)

raise SystemExit(client_status)
"""
        wrapper_path = os.path.join(self.stagedir, "client.py")
        with open(wrapper_path, "x", encoding="ascii") as wrapper:
            wrapper.write(client_wrapper)
        os.chmod(wrapper_path, 0o700)

        self.job.launcher = getlauncher("local")()
        # Frontier Slurm 25.11 supports NIL.  Unlike NONE, NIL does not invoke
        # --get-user-env on the compute node.  Only the fixed env_vars below
        # are established by the generated trusted job script.
        self.job.options = ["--export=NIL"]
        self.prerun_cmds = [
            "set -euo pipefail",
            f"cp -- {shlex.quote(server_source)} ./server.py",
            f"cp -- {shlex.quote(client_source)} ./client-under-test.py",
            "chmod 700 -- ./server.py ./client.py ./client-under-test.py",
            "mkdir -m 700 -- ./pmix-tests-pr-tmp",
        ]
        run_script = f"""
set +e
{shlex.quote(PYTHON)} -c 'import pmix'
python_preflight_status=$?
set -e
if (( python_preflight_status != 0 )); then
    exit "$python_preflight_status"
fi
printf '%s\\n' \\
    'PMIX_TESTS_PR_RUN_EVIDENCE_VERSION=2' \\
    'PR_HEAD_SHA={PR_HEAD_SHA}' \\
    'EXECUTION_ID={EXECUTION_ID}' \\
    'PYTHON_PREFLIGHT_EXIT_CODE=0' > pmix-tests-pr-run-started.env
set +e
{shlex.quote(PYTHON)} ./server.py
server_status=$?
set -e
printf '%s\\n' \\
    'PMIX_TESTS_PR_RUN_EVIDENCE_VERSION=2' \\
    'PR_HEAD_SHA={PR_HEAD_SHA}' \\
    'EXECUTION_ID={EXECUTION_ID}' \\
    'PYTHON_PREFLIGHT_EXIT_CODE=0' \\
    "SERVER_EXIT_CODE=$server_status" > pmix-tests-pr-run-completed.env
exit "$server_status"
"""
        self.executable_opts = ["-c", shlex.quote(run_script)]

        pythonpath = os.path.join(self.pmix.stagedir, "python-site-packages")
        ld_library_path = (
            f"{self.pmix.stagedir}/lib:{self.libevent.stagedir}/lib"
        )
        python_bin = os.path.dirname(PYTHON)
        path = f"{python_bin}:/usr/bin:/bin"

        self.env_vars = {
            "PATH": path,
            "PYTHONPATH": pythonpath,
            "LD_LIBRARY_PATH": ld_library_path,
            "HOME": self.stagedir,
            "TMPDIR": f"{self.stagedir}/pmix-tests-pr-tmp",
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "PMIX": self.pmix.stagedir,
            "LIBEVENT": self.libevent.stagedir,
        }

    @sanity_function
    def check_output(self):
        return sn.all([
            sn.assert_found(r"Testing server version", self.stdout),
            sn.assert_found(r"stdout: Init result\s+0", self.stdout),
            sn.assert_found(r"stdout: Put result\s+0", self.stdout),
            sn.assert_found(r"stdout: Commit result\s+0", self.stdout),
            sn.assert_found(r"stdout: Fence result\s+0", self.stdout),
            sn.assert_found(r"stdout: Get result:\s+0", self.stdout),
            sn.assert_found(r"stdout: Client finalize complete", self.stdout),
            sn.assert_found(r"FINALIZING", self.stdout),
            sn.assert_not_found(r"FAILED TO", self.stdout),
            sn.assert_not_found(r"Traceback \(most recent call last\)", self.stdout),
        ])


if PR_SOURCE_DIR:
    @rfm.simple_test
    class PMIxTestsPRPythonSmokeTest(_PMIxTestsPRPythonSmokeBase):
        """Run the fixed pmix-tests Python server/client smoke workload."""
