import os
import sys

# Allow this test to import build classes from the repository root.
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

import reframe as rfm
import reframe.utility.sanity as sn

from reframe.core.backends import getlauncher
from reframe.core.builtins import fixture, run_before, sanity_function

from libevent_build_class import build_libevent
from pmix_build_class import build_pmix
from prrte_build_class import build_prrte


PYTHON = os.environ.get(
    'PMIX_PYTHON',
    '/lustre/orion/scratch/kbadami/gen243/reframe_practice/pmix-py310/bin/python'
)

SLEEPER = (
    './sleeper_mpi_new'
)

TEST_DIR = os.path.dirname(__file__)


class _PMIxPythonMixedThreadCompatBase(rfm.RunOnlyRegressionTest):

    valid_systems = ['frontier:batch']
    valid_prog_environs = ['pmix_test']

    prrte = fixture(build_prrte, scope='environment')
    pmix = fixture(build_pmix, scope='environment')
    libevent = fixture(build_libevent, scope='environment')

    # Do not stage the whole directory: it contains ReFrame's stage/output
    # trees, which can recursively copy themselves into the next run.
    sourcesdir = None

    executable = PYTHON

    # Reserve three nodes so the driver can use two remote PRRTE hosts.
    num_tasks = 96
    num_tasks_per_node = 32
    time_limit = '10m'

    @run_before('run')
    def prepare_test(self):
        self.job.launcher = getlauncher('local')()
        self.executable_opts = [
            './run_pmix_python_mixed_thread_compat.py',
            '--slots',
            '2',
            '--job-size-min',
            '1',
            '--job-size-max',
            '2',
            '--min-time',
            '1',
            '--max-time',
            '1',
            '--iters',
            '8',
            '--out-file',
            'mixed_thread_compat.out',
            '--delay',
            '0',
            '--job',
            SLEEPER,
            '--seed',
            '235'
        ]

        self.prerun_cmds = [
            'set -e',
            'mapfile -t nodes < <(scontrol show hostnames "$SLURM_JOB_NODELIST")',
            'test ${#nodes[@]} -ge 3',
            "printf '%s slots=1\\n' \"${nodes[1]}\" \"${nodes[2]}\" > ci.hostfile",
            f'cp {os.path.join(TEST_DIR, "run_pmix_python_mixed_thread_compat.py")} .',
            f'cp {os.path.join(TEST_DIR, "pmix_event_utils.py")} .',
            f'cp {os.path.join(TEST_DIR, "sleeper_mpi_new.c")} .',
            'mpicc -o sleeper_mpi_new sleeper_mpi_new.c'
        ]

        pythonpath = f'{self.pmix.stagedir}/lib/python3.10/site-packages'
        ld_library_path = (
            f'{self.pmix.stagedir}/lib:'
            f'{self.prrte.stagedir}/lib:'
            f'{self.libevent.stagedir}/lib'
        )
        if os.environ.get('PYTHONPATH'):
            pythonpath = f"{pythonpath}:{os.environ['PYTHONPATH']}"
        if os.environ.get('LD_LIBRARY_PATH'):
            ld_library_path = (
                f"{ld_library_path}:{os.environ['LD_LIBRARY_PATH']}"
            )

        self.env_vars = {
            'PYTHONPATH': pythonpath,
            'LD_LIBRARY_PATH': ld_library_path,
            'PMIX': self.pmix.stagedir,
            'PRRTE': self.prrte.stagedir,
            'LIBEVENT': self.libevent.stagedir,
            'CI_HOSTFILE': f'{self.stagedir}/ci.hostfile'
        }

        self.postrun_cmds = [
            "set -euo pipefail; "
            "job_sizes_file=mixed_thread_compat.out.job_sizes; "
            "run_log=mixed_thread_compat.out; "
            "stdout_log=rfm_job.out; "
            "mapfile -t job_sizes < <(grep -v '^$' \"$job_sizes_file\"); "
            "test \"${#job_sizes[@]}\" -eq 8; "
            "expected=(1 1 1 1 1 2 1 2); "
            "for idx in \"${!expected[@]}\"; do "
            "test \"${job_sizes[$idx]}\" = \"${expected[$idx]}\"; "
            "done; "
            "count1=0; "
            "count2=0; "
            "total=0; "
            "for size in \"${job_sizes[@]}\"; do "
            "case \"$size\" in "
            "1) count1=$((count1 + 1));; "
            "2) count2=$((count2 + 1));; "
            "esac; "
            "total=$((total + size)); "
            "done; "
            "test \"$count1\" -ge 1; "
            "test \"$count2\" -ge 1; "
            "test \"$total\" -eq 10; "
            "test \"$(grep -c 'Completion handler: job ' \"$run_log\")\" -eq 8; "
            "test \"$(grep -c 'DONE (slept 1 seconds)' \"$stdout_log\")\" -eq 10; "
            "awk '/Selected [0-9]+ tasks in [0-9]+ slots to dispatch/ { "
            "for (i = 1; i <= NF; i++) { "
            "if ($i == \"in\" && $(i+2) == \"slots\" && $(i+3) == \"to\") { "
            "matched = 1; "
            "if ($(i+1) > 2) bad = 1; "
            "} "
            "} "
            "} END { "
            "if (!matched || bad) exit 1; "
            "}' \"$run_log\"; "
            "! grep -q 'Spawn Oops' \"$run_log\"; "
            "! grep -q 'Some jobs failed' \"$run_log\"; "
            "grep -q 'MIXED JOB SIZES PASS' \"$run_log\"; "
            "echo 'MIXED JOB SIZE SET PASS'"
        ]

    @sanity_function
    def check_output(self):
        return sn.all([
            sn.assert_found(r'MIXED JOB SIZES PASS', self.stdout),
            sn.assert_found(r'MIXED JOB SIZE SET PASS', self.stdout),
            sn.assert_eq(
                sn.count(sn.findall(r'DONE \(slept 1 seconds\)', self.stdout)),
                10
            ),
            sn.assert_not_found(r'Spawn Oops', self.stdout),
            sn.assert_not_found(r'Some jobs failed', self.stdout)
        ])


@rfm.simple_test
class PMIxPythonMixedThreadCompatTest(
    _PMIxPythonMixedThreadCompatBase
):
    pass
