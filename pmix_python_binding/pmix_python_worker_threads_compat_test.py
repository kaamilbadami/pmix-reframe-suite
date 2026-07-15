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
    'python3'
)

SLEEPER = (
    './sleeper_mpi_new'
)

TEST_DIR = os.path.dirname(__file__)


class _PMIxPythonWorkerThreadsCompatBase(rfm.RunOnlyRegressionTest):

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

    num_workers = 1

    @run_before('run')
    def prepare_test(self):
        self.job.launcher = getlauncher('local')()
        self.executable = self.pmix.python_env
        self.executable_opts = [
            './run_pmix_python_worker_threads_compat.py',
            '--slots',
            '2',
            '--job-size',
            '1',
            '--min-time',
            '1',
            '--max-time',
            '1',
            '--iters',
            '2',
            '--out-file',
            f'worker_threads_{self.num_workers}.out',
            '--delay',
            '0',
            '--job',
            SLEEPER,
            '--num-workers',
            str(self.num_workers)
        ]

        self.prerun_cmds = [
            'mapfile -t nodes < <(scontrol show hostnames "$SLURM_JOB_NODELIST")',
            'test ${#nodes[@]} -ge 3',
            "printf '%s slots=1\\n' \"${nodes[1]}\" \"${nodes[2]}\" > ci.hostfile",
            f'cp {os.path.join(TEST_DIR, "run_pmix_python_worker_threads_compat.py")} .',
            f'cp {os.path.join(TEST_DIR, "pmix_event_utils.py")} .',
            f'cp {os.path.join(TEST_DIR, "sleeper_mpi_new.c")} .',
            'mpicc -o sleeper_mpi_new sleeper_mpi_new.c'
        ]

        pythonpath = os.path.join(
            self.pmix.stagedir,
            'python-site-packages'
        )
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
    def check_output(self):
        return sn.all([
            sn.assert_found(
                fr'WORKER COUNT {self.num_workers} PASS',
                self.stdout
            ),
            sn.assert_eq(
                sn.count(sn.findall(r'DONE \(slept 1 seconds\)', self.stdout)),
                4
            ),
            sn.assert_not_found(r'Spawn Oops', self.stdout),
            sn.assert_not_found(r'Some jobs failed', self.stdout)
        ])


@rfm.simple_test
class PMIxPythonWorkerThreadsCompat1Test(
    _PMIxPythonWorkerThreadsCompatBase
):
    num_workers = 1


@rfm.simple_test
class PMIxPythonWorkerThreadsCompat2Test(
    _PMIxPythonWorkerThreadsCompatBase
):
    num_workers = 2
