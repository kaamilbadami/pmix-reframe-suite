import reframe as rfm
import reframe.utility.sanity as sn

from reframe.core.backends import getlauncher
from reframe.core.builtins import sanity_function, performance_function, run_before

@rfm.simple_test
class PrteStartupTest(rfm.RunOnlyRegressionTest):
    valid_systems = ['frontier:compute']
    valid_prog_environs = ['baseline']

    node_count = 32
    num_trials = 25

    sourcesdir = '.'
    executable = './prte_startup_test.sh'

    executable_opts = [str(node_count), str(num_trials)]

    num_tasks = node_count
    num_tasks_per_node = 1
    time_limit = '5m'

    reference = {
        'frontier:compute': {
            'average_startup': (2.0, None, 0.0, 's')
        }
    }

    @run_before('run')
    def configure_test(self):
        self.job.launcher = getlauncher('local')()

    @sanity_function
    def validate(self):
        return sn.assert_found(r'SUCCESS', self.stdout)

    @performance_function('s')
    def average_startup(self):
        times = sn.extractall(
            r'PRTE startup = (\S+) seconds', # captures number
            self.stdout,
            1,
            float
        )

        return sn.avg(times)
