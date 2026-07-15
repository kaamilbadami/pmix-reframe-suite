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


TEST_DIR = os.path.dirname(__file__)


# ReFrame test for PMIx Python spawning across multiple nodes.
@rfm.simple_test
class PMIxPythonScalingMultinodeTest(rfm.RunOnlyRegressionTest):

    # Use the Frontier configuration defined in sysconfig.yaml.
    valid_systems = ['frontier:batch']
    valid_prog_environs = ['pmix_test']

    # Build PMIx, PRRTE, and libevent through ReFrame.
    prrte = fixture(build_prrte, scope='environment')
    pmix = fixture(build_pmix, scope='environment')
    libevent = fixture(build_libevent, scope='environment')

    # Do not stage the whole directory: it contains ReFrame's stage/output
    # trees, which can recursively copy themselves into the next run.
    sourcesdir = None

    # Run the staged shell script.
    executable = './run_pmix_python_scaling_multinode_test.sh'

    # Reserve four nodes with 32 process slots on each node.
    num_tasks = 128
    num_tasks_per_node = 32
    time_limit = '15m'

    @run_before('run')
    def prepare_test(self):
        # Run the shell script directly inside the Slurm allocation.
        self.job.launcher = getlauncher('local')()
        self.prerun_cmds = [
            f'cp {os.path.join(TEST_DIR, "run_pmix_python_scaling_multinode_test.sh")} .',
            f'cp {os.path.join(TEST_DIR, "spawn_scaling_multinode_test.py")} .'
        ]

        # Use the software installations produced by the ReFrame fixtures.
        self.env_vars = {
            'PYTHON': self.pmix.python_env,
            'PMIX': self.pmix.stagedir,
            'PRRTE': self.prrte.stagedir,
            'LIBEVENT': self.libevent.stagedir,
            'PMIX_PYTHON_PACKAGE': os.path.join(
                self.pmix.stagedir,
                'python-site-packages'
            )
        }

    # Pass only if the 1-, 2-, and 4-node tests succeed.
    @sanity_function
    def check_output(self):
        return sn.all([
            sn.assert_found(r'NODE COUNT 1 PASS', self.stdout),
            sn.assert_found(r'NODE COUNT 2 PASS', self.stdout),
            sn.assert_found(r'NODE COUNT 4 PASS', self.stdout)
        ])
