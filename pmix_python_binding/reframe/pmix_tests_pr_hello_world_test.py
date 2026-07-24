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
from prrte_build_class import build_prrte


PR_SOURCE_DIR = os.environ.get("PMIX_TESTS_SOURCE_DIR", "")
PR_HEAD_SHA = os.environ.get("PMIX_TESTS_PR_HEAD_SHA", "")
EXECUTION_ID = os.environ.get("PMIX_TESTS_PR_EXECUTION_ID", "")


class _PMIxTestsPRHelloWorldBase(rfm.RunOnlyRegressionTest):
    valid_systems = ["frontier:batch"]
    valid_prog_environs = ["pmix_test"]
    modules = ["PrgEnv-amd"]

    prrte = fixture(build_prrte, scope="environment")
    pmix = fixture(build_pmix, scope="environment")
    libevent = fixture(build_libevent, scope="environment")

    sourcesdir = None
    executable = "/bin/bash"
    num_tasks = 2
    num_tasks_per_node = 2
    time_limit = "5m"
    tags = {
        f"pmix-tests-pr-sha:{PR_HEAD_SHA}",
        f"pmix-tests-pr-execution:{EXECUTION_ID}",
    }

    @run_before("run")
    def prepare_test(self):
        test_dir = os.path.join(
            os.path.abspath(PR_SOURCE_DIR), "prrte", "hello_world"
        )
        pmix_commit_record = os.path.join(self.pmix.stagedir, "PMIX_COMMIT")

        self.job.launcher = getlauncher("local")()
        # Frontier Slurm 25.11 supports NIL. Unlike NONE, NIL does not invoke
        # --get-user-env on the compute node. Only the fixed env_vars below
        # are established by the generated trusted job script.
        self.job.options = ["--export=NIL", "--nodes=1"]
        self.prerun_cmds = [
            "set -euo pipefail",
            (
                f"cp -- {shlex.quote(pmix_commit_record)} "
                "./pmix-fixture-commit.env"
            ),
        ]

        run_script = f"""
set -euo pipefail
umask 077
mapfile -t pmix_commit_lines < <(
    sed -n 's/^PMIX_COMMIT=//p' pmix-fixture-commit.env
)
if (( ${{#pmix_commit_lines[@]}} != 1 )) ||
   [[ ! ${{pmix_commit_lines[0]}} =~ ^[0-9a-f]{{40}}$ ]]; then
    printf '%s\\n' 'error: resolved PMIx fixture commit is unavailable' >&2
    exit 2
fi
pmix_commit=${{pmix_commit_lines[0]}}

if [[ ! ${{SLURM_JOB_ID:-}} =~ ^[1-9][0-9]*$ ]]; then
    printf '%s\\n' 'error: canonical Slurm job ID is unavailable' >&2
    exit 2
fi
node_tmp_root=/tmp
if [[ ! -d $node_tmp_root || -L $node_tmp_root ]] ||
   [[ $(/usr/bin/stat -c %a -- "$node_tmp_root") != 1777 ]]; then
    printf '%s\\n' 'error: Frontier node-local /tmp is not trustworthy' >&2
    exit 2
fi
node_tmp_fstype=$(/usr/bin/stat -f -c %T -- "$node_tmp_root")
case $node_tmp_fstype in
    tmpfs|xfs|ext2/ext3|ext4|btrfs) ;;
    *)
        printf 'error: /tmp is not on an approved node-local filesystem: %s\\n' \\
            "$node_tmp_fstype" >&2
        exit 2
        ;;
esac

readonly expected_runtime_dir="/tmp/pmix-tests-pr-${{SLURM_JOB_ID}}-{EXECUTION_ID}"
readonly runtime_dir=$expected_runtime_dir
readonly current_uid=$(/usr/bin/id -u)
if [[ -L $runtime_dir || -e $runtime_dir ]]; then
    printf '%s\\n' 'error: trusted runtime directory already exists' >&2
    exit 2
fi
/usr/bin/mkdir -m 700 -- "$runtime_dir"

cleanup_runtime_dir()
{{
    cleanup_status=$?
    trap - EXIT
    if [[ $runtime_dir == "$expected_runtime_dir" &&
          $runtime_dir == "/tmp/pmix-tests-pr-${{SLURM_JOB_ID}}-{EXECUTION_ID}" &&
          -d $runtime_dir && ! -L $runtime_dir &&
          $(/usr/bin/stat -c %u -- "$runtime_dir") == "$current_uid" ]]; then
        /usr/bin/rm -rf --one-file-system -- "$runtime_dir" ||
            cleanup_status=2
    fi
    if [[ -L $runtime_dir || -e $runtime_dir ]]; then
        printf 'error: trusted runtime directory cleanup failed: %s\\n' \\
            "$runtime_dir" >&2
        cleanup_status=2
    else
        printf 'PMIX_TESTS_PR_RUNTIME_CLEANED=%s\\n' "$runtime_dir"
    fi
    exit "$cleanup_status"
}}
trap cleanup_runtime_dir EXIT

if [[ ! -d $runtime_dir || -L $runtime_dir ]] ||
   [[ $(/usr/bin/stat -c %a -- "$runtime_dir") != 700 ]] ||
   [[ $(/usr/bin/stat -c %u -- "$runtime_dir") != "$current_uid" ]]; then
    printf '%s\\n' 'error: trusted runtime directory validation failed' >&2
    exit 2
fi
export TMPDIR=$runtime_dir
printf 'PMIX_TESTS_PR_RUNTIME_TMPDIR=%s\\n' "$TMPDIR"
printf 'PMIX_TESTS_PR_RUNTIME_FSTYPE=%s\\n' "$node_tmp_fstype"

start_tmp=pmix-tests-pr-run-started.env.tmp
complete_tmp=pmix-tests-pr-run-completed.env.tmp
printf '%s\\n' \\
    'PMIX_TESTS_PR_RUN_EVIDENCE_VERSION=4' \\
    'PR_HEAD_SHA={PR_HEAD_SHA}' \\
    'EXECUTION_ID={EXECUTION_ID}' \\
    "PMIX_COMMIT=$pmix_commit" > "$start_tmp"
/bin/mv -f -- "$start_tmp" pmix-tests-pr-run-started.env

set +e
/bin/bash -c '
set -euo pipefail
test_dir=$1
cd -- "$test_dir"
/bin/bash ./build.sh
if [[ ! -d $TMPDIR || -L $TMPDIR ]] ||
   [[ $(/usr/bin/stat -c %a -- "$TMPDIR") != 700 ]] ||
   [[ $(/usr/bin/stat -c %u -- "$TMPDIR") != $(/usr/bin/id -u) ]]; then
    printf "%s\\n" "error: trusted runtime directory changed during build" >&2
    exit 2
fi
mapfile -t nodes < <(/usr/bin/scontrol show hostnames "$SLURM_JOB_NODELIST")
(( ${{#nodes[@]}} == 1 ))
exec prterun --tmpdir "$TMPDIR" --host "${{nodes[0]}}:2" \\
    -n 2 --map-by ppr:2:node ./hello
' pmix-tests-pr-hello-world {shlex.quote(test_dir)}
workload_status=$?
set -e

printf '%s\\n' \\
    'PMIX_TESTS_PR_RUN_EVIDENCE_VERSION=4' \\
    'PR_HEAD_SHA={PR_HEAD_SHA}' \\
    'EXECUTION_ID={EXECUTION_ID}' \\
    "PMIX_COMMIT=$pmix_commit" \\
    "WORKLOAD_EXIT_CODE=$workload_status" > "$complete_tmp"
/bin/mv -f -- "$complete_tmp" pmix-tests-pr-run-completed.env
exit "$workload_status"
"""
        self.executable_opts = ["-c", shlex.quote(run_script)]

        inherited_path = os.environ.get("PATH", "/usr/bin:/bin")
        path = (
            f"{self.prrte.stagedir}/bin:"
            f"{self.pmix.stagedir}/bin:"
            f"{self.libevent.stagedir}/bin:"
            f"{inherited_path}"
        )
        # With --export=NIL, this expands only the library path established by
        # the trusted system initialization and PrgEnv-amd module above.
        ld_library_path = (
            f"{self.prrte.stagedir}/lib:"
            f"{self.pmix.stagedir}/lib:"
            f"{self.libevent.stagedir}/lib:"
            "${LD_LIBRARY_PATH:-}"
        )
        self.env_vars = {
            "PATH": path,
            "LD_LIBRARY_PATH": ld_library_path,
            "HOME": self.stagedir,
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "PMIX": self.pmix.stagedir,
            "PRRTE": self.prrte.stagedir,
            "LIBEVENT": self.libevent.stagedir,
        }

    @sanity_function
    def check_output(self):
        valid_line = (
            r"(?m)^[01]/2 \[[01]/2\] Hello World from "
            r"\S+ \(pid [1-9][0-9]*\)$"
        )
        rank_zero = (
            r"(?m)^0/2 \[0/2\] Hello World from "
            r"\S+ \(pid [1-9][0-9]*\)$"
        )
        rank_one = (
            r"(?m)^1/2 \[1/2\] Hello World from "
            r"\S+ \(pid [1-9][0-9]*\)$"
        )
        return sn.all([
            sn.assert_eq(self.job.exitcode, 0),
            sn.assert_eq(sn.count(sn.findall(valid_line, self.stdout)), 2),
            sn.assert_eq(
                sn.count(sn.findall(r"(?m)^.*Hello World from .*$", self.stdout)),
                2,
            ),
            sn.assert_found(rank_zero, self.stdout),
            sn.assert_found(rank_one, self.stdout),
            sn.assert_not_found(r"ERROR:", self.stdout),
            sn.assert_not_found(r"ERROR:", self.stderr),
        ])


if PR_SOURCE_DIR:
    @rfm.simple_test
    class PMIxTestsPRHelloWorldTest(_PMIxTestsPRHelloWorldBase):
        """Build and run the trusted PR checkout's PMIx hello-world test."""
