import os
import reframe as rfm
import reframe.utility.sanity as sn

from reframe.core.backends import getlauncher
from reframe.core.builtins import run_before, sanity_function


# ReFrame test for PMIx Python process scaling.
@rfm.simple_test
class PMIxPythonScalingTest(rfm.RunOnlyRegressionTest):

    # Run this test on Frontier compute nodes using the baseline environment.
    valid_systems = ['frontier:compute']
    valid_prog_environs = ['baseline']

    # Copy the test files from this repository directory into the
    # ReFrame stage directory.
    sourcesdir = os.path.dirname(__file__)

    # Run the staged shell script.
    executable = './run_pmix_python_scaling_test.sh'

    # Reserve one full Frontier node so the script can test up to 32 processes.
    num_tasks = 32
    num_tasks_per_node = 32
    time_limit = '10m'

    # Run the shell script directly inside the Slurm allocation.
    @run_before('run')
    def use_local_launcher(self):
        self.job.launcher = getlauncher('local')()

    # Pass only if every process count completed all five trials.
    @sanity_function
    def check_output(self):
        return sn.all([
            sn.assert_found(r'PROCESS COUNT 1 PASS', self.stdout),
            sn.assert_found(r'PROCESS COUNT 2 PASS', self.stdout),
            sn.assert_found(r'PROCESS COUNT 4 PASS', self.stdout),
            sn.assert_found(r'PROCESS COUNT 8 PASS', self.stdout),
            sn.assert_found(r'PROCESS COUNT 16 PASS', self.stdout),
            sn.assert_found(r'PROCESS COUNT 32 PASS', self.stdout)
        ])
