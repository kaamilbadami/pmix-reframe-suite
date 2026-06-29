import os
import reframe as rfm
import reframe.utility.sanity as sn

from reframe.core.backends import getlauncher
from reframe.core.builtins import run_before, sanity_function


# ReFrame test for PMIx Python spawning across multiple nodes.
@rfm.simple_test
class PMIxPythonScalingMultinodeTest(rfm.RunOnlyRegressionTest):

    # Run on Frontier compute nodes.
    valid_systems = ['frontier:compute']
    valid_prog_environs = ['baseline']

    # Copy the test files from this repository directory into the
    # ReFrame stage directory.
    sourcesdir = os.path.dirname(__file__)

    # Run the staged shell script.
    executable = './run_pmix_python_scaling_multinode_test.sh'

    # Reserve four nodes with 32 task slots on each node.
    num_tasks = 128
    num_tasks_per_node = 32
    time_limit = '15m'

    # Run the shell script directly inside the allocation.
    @run_before('run')
    def use_local_launcher(self):
        self.job.launcher = getlauncher('local')()

    # Pass only if the 1-, 2-, and 4-node tests succeed.
    @sanity_function
    def check_output(self):
        return sn.all([
            sn.assert_found(r'NODE COUNT 1 PASS', self.stdout),
            sn.assert_found(r'NODE COUNT 2 PASS', self.stdout),
            sn.assert_found(r'NODE COUNT 4 PASS', self.stdout)
        ])
