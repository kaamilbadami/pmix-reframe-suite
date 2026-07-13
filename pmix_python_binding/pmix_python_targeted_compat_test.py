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


class _PMIxPythonTargetedCompatBase(rfm.RunOnlyRegressionTest):

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
            './run_pmix_python_targeted_compat.py',
            '--slots',
            '2',
            '--job-size',
            '1',
            '--min-time',
            '1',
            '--max-time',
            '1',
            '--iters',
            '1',
            '--out-file',
            'targeted_compat.out',
            '--delay',
            '0',
            '--job',
            SLEEPER
        ]

        self.prerun_cmds = [
            'set -e',
            'mapfile -t nodes < <(scontrol show hostnames "$SLURM_JOB_NODELIST")',
            'test ${#nodes[@]} -ge 3',
            "printf '%s slots=1\\n' \"${nodes[1]}\" \"${nodes[2]}\" > ci.hostfile",
            f'cp {os.path.join(TEST_DIR, "run_pmix_python_targeted_compat.py")} .',
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
            "expected_hosts=$(awk '{print $1}' \"$CI_HOSTFILE\"); "
            # This only proves which hosts the controller requested, not where
            # the sleepers actually ran.
            # "actual_hosts=$(awk '/targeted to/ {print $NF}' targeted_compat.out); "
            "actual_hosts=$(awk '/DONE \\(slept 1 seconds\\)/ { "
            "if (match($0, /\\[[^]]+\\]/)) { "
            "print substr($0, RSTART + 1, RLENGTH - 2); "
            "} "
            "}' rfm_job.out); "
            "test \"$(printf '%s\\n' \"$expected_hosts\" | sed '/^$/d' | wc -l)\" -eq 2; "
            "test \"$(printf '%s\\n' \"$actual_hosts\" | sed '/^$/d' | wc -l)\" -eq 2; "
            "sorted_expected=$(printf '%s\\n' \"$expected_hosts\" | sort); "
            "sorted_actual=$(printf '%s\\n' \"$actual_hosts\" | sort); "
            "test \"$sorted_expected\" = \"$sorted_actual\"; "
            "echo 'TARGET HOST SET PASS'"
        ]

    @sanity_function
    def check_output(self):
        return sn.all([
            sn.assert_found(r'TARGETED PLACEMENT PASS', self.stdout),
            sn.assert_found(r'TARGET HOST SET PASS', self.stdout),
            sn.assert_eq(
                sn.count(sn.findall(r'targeted to', self.stdout)),
                2
            ),
            sn.assert_eq(
                sn.count(sn.findall(r'DONE \(slept 1 seconds\)', self.stdout)),
                2
            ),
            sn.assert_not_found(r'Spawn Oops', self.stdout),
            sn.assert_not_found(r'Some jobs failed', self.stdout)
        ])


@rfm.simple_test
class PMIxPythonTargetedCompatTest(
    _PMIxPythonTargetedCompatBase
):
    pass
