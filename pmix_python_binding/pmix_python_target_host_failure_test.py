import os
import shlex
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

TEST_DIR = os.path.dirname(__file__)


@rfm.simple_test
class PMIxPythonTargetHostFailurePropagationTest(rfm.RunOnlyRegressionTest):
    """Reject an invalid PMIX_HOST and verify failure propagation."""

    valid_systems = ['frontier:batch']
    valid_prog_environs = ['pmix_test']

    prrte = fixture(build_prrte, scope='environment')
    pmix = fixture(build_pmix, scope='environment')
    libevent = fixture(build_libevent, scope='environment')

    sourcesdir = None

    num_tasks = 96
    num_tasks_per_node = 32
    time_limit = '10m'

    @run_before('run')
    def prepare_test(self):
        self.job.launcher = getlauncher('local')()
        controller_args = [
            PYTHON,
            './run_pmix_python_targeted_compat.py',
            '--slots', '2',
            '--job-size', '1',
            '--min-time', '1',
            '--max-time', '1',
            '--iters', '1',
            '--out-file', 'targeted_host_failure.out',
            '--delay', '0',
            '--job', './sleeper_mpi_new',
            '--target-host-override',
            'pmix-host-that-does-not-exist.invalid'
        ]
        controller_cmd = shlex.join(controller_args)
        wrapper = (
            'set +e; '
            f'{controller_cmd}; '
            'controller_rc=$?; '
            'set -e; '
            'printf "CONTROLLER_EXIT_CODE=%s\\n" "$controller_rc"; '
            'test "$controller_rc" -eq 1; '
            "grep -q 'Spawn Oops' targeted_host_failure.out; "
            "grep -q 'TARGETED PLACEMENT FAIL' targeted_host_failure.out; "
            "! grep -q 'TARGETED PLACEMENT PASS' targeted_host_failure.out; "
            "! grep -q 'DONE (slept' rfm_job.out; "
            "echo 'PMIX TARGET HOST FAILURE PROPAGATION PASS'"
        )

        self.executable = '/bin/bash'
        self.executable_opts = ['-c', shlex.quote(wrapper)]

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

    @sanity_function
    def validate_target_host_failure(self):
        return sn.all([
            sn.assert_eq(self.job.exitcode, 0),
            sn.assert_found(r'CONTROLLER_EXIT_CODE=1', self.stdout),
            sn.assert_found(r'Spawn Oops', 'targeted_host_failure.out'),
            sn.assert_found(
                r'TARGETED PLACEMENT FAIL',
                'targeted_host_failure.out'
            ),
            sn.assert_not_found(
                r'TARGETED PLACEMENT PASS',
                'targeted_host_failure.out'
            ),
            sn.assert_not_found(r'DONE \(slept', self.stdout),
            sn.assert_found(
                r'PMIX TARGET HOST FAILURE PROPAGATION PASS',
                self.stdout
            )
        ])
