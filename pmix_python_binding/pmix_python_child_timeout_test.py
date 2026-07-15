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


TEST_DIR = os.path.dirname(__file__)


@rfm.simple_test
class PMIxPythonChildTimeoutTest(rfm.RunOnlyRegressionTest):
    """Verify that a long-running child produces a bounded failure."""

    valid_systems = ['frontier:batch']
    valid_prog_environs = ['pmix_test']

    prrte = fixture(build_prrte, scope='environment')
    pmix = fixture(build_pmix, scope='environment')
    libevent = fixture(build_libevent, scope='environment')

    sourcesdir = None

    num_tasks = 2
    num_tasks_per_node = 1
    time_limit = '5m'

    @run_before('run')
    def prepare_test(self):
        self.job.launcher = getlauncher('local')()

        python = self.pmix.python_env
        pmix_python_package = os.path.join(
            self.pmix.stagedir,
            'python-site-packages'
        )

        controller_args = [
            python,
            './run_pmix_python_targeted_compat.py',
            '--slots',
            '1',
            '--job-size',
            '1',
            '--min-time',
            '5',
            '--max-time',
            '5',
            '--iters',
            '1',
            '--out-file',
            'child_timeout.out',
            '--delay',
            '0',
            '--completion-timeout',
            '1',
            '--job',
            './sleeper_mpi_new'
        ]
        controller_cmd = shlex.join(controller_args)
        wrapper = (
            'set +e; '
            f'{controller_cmd}; '
            'controller_rc=$?; '
            'set -e; '
            'printf "CONTROLLER_EXIT_CODE=%s\\n" "$controller_rc"; '
            'test "$controller_rc" -eq 1; '
            "grep -q 'CONTROLLER TIMEOUT: completed=0 expected=1' "
            'child_timeout.out; '
            "grep -q 'CHILD PROCESS TIMEOUT' child_timeout.out; "
            "grep -q 'DVM SHUTDOWN PASS' child_timeout.out; "
            "! grep -q 'TARGETED PLACEMENT PASS' child_timeout.out; "
            "echo 'PMIX PYTHON CHILD TIMEOUT PASS'"
        )

        self.executable = '/bin/bash'
        self.executable_opts = ['-c', shlex.quote(wrapper)]

        self.prerun_cmds = [
            'set -e',
            'mapfile -t nodes < <(scontrol show hostnames "$SLURM_JOB_NODELIST")',
            'test ${#nodes[@]} -ge 2',
            "printf '%s slots=1\\n' \"${nodes[1]}\" > ci.hostfile",
            f'cp {os.path.join(TEST_DIR, "run_pmix_python_targeted_compat.py")} .',
            f'cp {os.path.join(TEST_DIR, "pmix_event_utils.py")} .',
            f'cp {os.path.join(TEST_DIR, "sleeper_mpi_new.c")} .',
            'mpicc -o sleeper_mpi_new sleeper_mpi_new.c'
        ]

        pythonpath = pmix_python_package
        if os.environ.get('PYTHONPATH'):
            pythonpath = f"{pythonpath}:{os.environ['PYTHONPATH']}"

        ld_library_path = (
            f'{self.pmix.stagedir}/lib:'
            f'{self.prrte.stagedir}/lib:'
            f'{self.libevent.stagedir}/lib'
        )
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
    def validate_child_timeout(self):
        return sn.all([
            sn.assert_eq(self.job.exitcode, 0),
            sn.assert_found(r'CONTROLLER_EXIT_CODE=1', self.stdout),
            sn.assert_found(
                r'CONTROLLER TIMEOUT: completed=0 expected=1',
                'child_timeout.out'
            ),
            sn.assert_found(
                r'CHILD PROCESS TIMEOUT',
                'child_timeout.out'
            ),
            sn.assert_found(
                r'DVM SHUTDOWN PASS',
                'child_timeout.out'
            ),
            sn.assert_not_found(
                r'TARGETED PLACEMENT PASS',
                'child_timeout.out'
            ),
            sn.assert_found(
                r'PMIX PYTHON CHILD TIMEOUT PASS',
                self.stdout
            )
        ])
