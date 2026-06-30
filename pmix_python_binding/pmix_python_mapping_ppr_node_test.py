import os
import sys

# Allow this test to import build classes from the repository root.
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

import reframe as rfm
import reframe.utility.sanity as sn
import reframe.utility.typecheck as typ

from reframe.core.backends import getlauncher
from reframe.core.builtins import (
    fixture,
    run_after,
    run_before,
    sanity_function,
    variable
)

from libevent_build_class import build_libevent
from pmix_build_class import build_pmix
from prrte_build_class import build_prrte


# ReFrame test for PMIx Python PPR node mapping.
@rfm.simple_test
class PMIxPythonMappingPPRNodeTest(rfm.RunOnlyRegressionTest):

    # Use the Frontier configuration defined in sysconfig.yaml.
    valid_systems = ['frontier:batch']
    valid_prog_environs = ['pmix_test']

    # Build PMIx, PRRTE, and libevent through ReFrame.
    prrte = fixture(build_prrte, scope='environment')
    pmix = fixture(build_pmix, scope='environment')
    libevent = fixture(build_libevent, scope='environment')

    # Copy this directory into the ReFrame stage directory.
    sourcesdir = os.path.dirname(__file__)

    # Run the staged PPR mapping shell script.
    executable = './run_pmix_python_mapping_ppr_node_test.sh'

    # Configurable test values. These may be changed with ReFrame -S.
    node_counts = variable(typ.List[int], value=[2])
    ppr_values = variable(typ.List[int], value=[1])
    trials = variable(int, value=5)
    slots_per_node = variable(int, value=32)

    # Default resources. The post-init hook recalculates these values.
    num_tasks = 64
    num_tasks_per_node = 32
    time_limit = '15m'

    @run_after('init')
    def set_resources(self):
        if (
            not self.node_counts
            or any(value < 1 for value in self.node_counts)
        ):
            raise ValueError(
                "node_counts must contain at least one positive integer"
            )

        if (
            not self.ppr_values
            or any(value < 1 for value in self.ppr_values)
        ):
            raise ValueError(
                "ppr_values must contain at least one positive integer"
            )

        if self.slots_per_node < 1:
            raise ValueError("slots_per_node must be positive")

        if self.trials < 1:
            raise ValueError("trials must be positive")

        if any(
            value > self.slots_per_node
            for value in self.ppr_values
        ):
            raise ValueError(
                "a PPR value exceeds the available slots per node"
            )

        self.num_tasks_per_node = self.slots_per_node
        self.num_tasks = (
            max(self.node_counts) * self.slots_per_node
        )

    @run_before('run')
    def prepare_test(self):
        # Run the shell script directly inside the Slurm allocation.
        self.job.launcher = getlauncher('local')()

        # Use the software installations produced by the ReFrame fixtures.
        self.env_vars = {
            'PYTHON': (
                '/lustre/orion/scratch/kbadami/gen243/'
                'reframe_practice/pmix-py310/bin/python'
            ),
            'PMIX': self.pmix.stagedir,
            'PRRTE': self.prrte.stagedir,
            'LIBEVENT': self.libevent.stagedir,
            'NODE_COUNTS': ','.join(
                str(value) for value in self.node_counts
            ),
            'PPR_VALUES': ','.join(
                str(value) for value in self.ppr_values
            ),
            'TRIALS': str(self.trials),
            'SLOTS_PER_NODE': str(self.slots_per_node)
        }

    # Pass only after every requested mapping test succeeds.
    @sanity_function
    def check_output(self):
        return sn.assert_found(
            r'PPR NODE MAPPING TEST PASS',
            self.stdout
        )
