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


# ReFrame test for PMIx Python process scaling.
@rfm.simple_test
class PMIxPythonScalingTest(rfm.RunOnlyRegressionTest):

    # Use the Frontier configuration defined in sysconfig.yaml.
    valid_systems = ['frontier:batch']
    valid_prog_environs = ['pmix_test']

    # Build PMIx, PRRTE, and libevent through ReFrame.
    prrte = fixture(build_prrte, scope='environment')
    pmix = fixture(build_pmix, scope='environment')
    libevent = fixture(build_libevent, scope='environment')

    # Copy this directory into the ReFrame stage directory.
    sourcesdir = os.path.dirname(__file__)

    # Run the staged shell script.
    executable = './run_pmix_python_scaling_test.sh'

    # Reserve one Frontier node with 32 process slots.
    num_tasks = 32
    num_tasks_per_node = 32
    time_limit = '10m'

    @run_before('run')
    def prepare_test(self):
        # Run the shell script directly inside the Slurm allocation.
        self.job.launcher = getlauncher('local')()

        # Override the shell script's fallback paths with installations
        # produced by the ReFrame fixtures.
        self.env_vars = {
            'PYTHON': (
                '/lustre/orion/scratch/kbadami/gen243/'
                'reframe_practice/pmix-py310/bin/python'
            ),
            'PMIX': self.pmix.stagedir,
            'PRRTE': self.prrte.stagedir,
            'LIBEVENT': self.libevent.stagedir
        }

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
