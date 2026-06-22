# SPDX-FileCopyrightText: 2026 Niccolo Tosato niccolo.tosato@yahoo.it
#
# SPDX-License-Identifier: MIT

import os
import time
from packaging.version import parse as parse_version

import reframe as rfm
import reframe.utility.typecheck as typ
import reframe.utility.sanity as sn


from prrte_build_class import build_prrte
from pmix_build_class import build_pmix
from libevent_build_class import build_libevent
from build_pmix_test import build_cycle, build_hello_world, build_prun_wrapper, fetch_pmixtest, build_manystress

class base_test(rfm.RunOnlyRegressionTest):
    valid_systems = ['*']
    valid_prog_environs = ['*']
    prrte = fixture(build_prrte, scope = 'environment')
    pmix =  fixture(build_pmix, scope = 'environment')
    libevent = fixture(build_libevent, scope = 'environment')
    pmix_tests = fixture(fetch_pmixtest, scope = 'session')
    path = list()
    ld_library_path = list()
    
    time_limit = '0d0h15m0s'
    num_tasks = 640
    num_tasks_per_node = 32
    num_cpus_per_task = 1

    @run_before('run')
    def prepare_run(self):
        for fix in [self.prrte, self.pmix, self.libevent]:
            self.path.append(os.path.join(fix.stagedir,"bin"))
            self.ld_library_path.append(os.path.join(fix.stagedir,"lib"))
        self.env_vars = {
            "PATH" : ":".join(self.path) + ":${PATH}",
            "LD_LIBRARY_PATH" : ":".join(self.ld_library_path) + ":${LD_LIBRARY_PATH}"
        }
        self.executable = os.path.join("")
    
    def get_pmix_version(self):
        # Pmix is a fixuter (build_pmix), but the actual version is container in the fixture fetch_pmix
        return parse_version(self.pmix.pmix.version)
    
    def check_errors(self):
        total_errors = sn.count(sn.findall(r'\bERROR\b', self.stderr))
        if self.get_pmix_version()  == parse_version("6.1.0"):
            # Ignore some errors from  pmix v6.1.0, we expect a race condition, see https://github.com/openpmix/prrte/issues/2431
            known_bug_pattern = r'contact information is unknown in file iof_hnp\.c at line 222'
            known_bugs = sn.count(sn.findall(known_bug_pattern, self.stderr))
            # Assert that every 'ERROR' found is accounted for by the known bug
            print(f"Known race condition count: {known_bugs}")
            return sn.assert_eq(total_errors, known_bugs)
        else:
            return sn.assert_eq(total_errors, 0)

    def check_host_count(self,expected_count = None ):
        if expected_count is None:
            expected_count = self.num_tasks
        patt = self.current_system.hostnames[0]
        line_count = sn.count(sn.extractall(patt,self.stdout,0))
        return sn.assert_eq(line_count,expected_count)

    @sanity_function
    def retcode(self):
        print("This is the baseclass sanity function")
        return sn.assert_eq(self.job.exitcode,0)

    @performance_function('s')
    def walltime(self):
        patt = r"runtime,(\d+\.\d+),(\d+\.\d+),(\d+\.\d+)"
        # Extract the values
        return sn.extractsingle(
            patt, 
            self.stderr,          
            tag=(1),        # Capture Group 1 (Real), Group 2 (User), Group 3 (Sys), Get only 1
            conv=float            
        )


@rfm.simple_test
class hostname_test(base_test):
    descr = "Test pmix hostname"
    test_name = "hostname"
    hello_test = fixture(build_hello_world,scope = 'environment')

    @run_before("run")
    def prepare_test(self):
        test_path = self.hello_test.test_path
        #1. Change folder 2. Init the DVM 3. set time output to be parsable later
        self.prerun_cmds = [ 
            f'cd {test_path}', 
            #"export HOSTS=$(scontrol show hostnames | xargs | tr ' ' ',')",
            f"prte --no-ready-msg --host $(scontrol show hostnames | xargs | sed 's/ /:{self.num_tasks_per_node},/g' | sed 's/$/:{self.num_tasks_per_node}/') &",
            'TIMEFORMAT="runtime,%R,%U,%S"',
            'sleep 10'
        ]
        self.executable="time"
        self.executable_opts = ["prun", f"--map-by ppr:{self.num_tasks_per_node}:node", "hostname"]
        # At the end shutdown the dvm
        self.postrun_cmds = ["pterm", "sleep 2"]
    @sanity_function
    def check_test(self):
        flags = [self.check_host_count(),self.check_errors()]
        return sn.all(flags)
        
@rfm.simple_test
class hello_world_test(base_test):
    descr = "Test pmix hello_world"
    test_name = "hello_world"
    hello_test = fixture(build_hello_world,scope = 'environment')
    @run_before("run")
    def prepare_test(self):
        test_path = self.hello_test.test_path
        #1. Change folder 2. Init the DVM 3. set time output to be parsable later
        self.prerun_cmds = [
            f'cd {test_path}',
            f"prte --no-ready-msg --host $(scontrol show hostnames | xargs | sed 's/ /:{self.num_tasks_per_node},/g' | sed 's/$/:{self.num_tasks_per_node}/') &",
            'TIMEFORMAT="runtime,%R,%U,%S"',
            'sleep 5'
        ]
        self.executable="time"
        self.executable_opts = ["prun", f"--map-by ppr:{self.num_tasks_per_node}:node", "./hello"]
        # At the end shutdown the dvm
        self.postrun_cmds = ["pterm", "sleep 2"]


    @sanity_function
    def check_test(self):
        flags = [self.check_host_count(),self.check_errors()]
        return sn.all(flags)

@rfm.simple_test
class cycle_test_hostname(base_test):
    descr = "Test Cycle in pmix-test"
    test_name = "cycle"
    cycle_test = fixture(build_cycle,scope = 'environment')
    num_iters=100

    @run_before("run")
    def prepare_test(self):
        test_path = self.cycle_test.test_path
        self.prerun_cmds = [
            f'cd {test_path}',
            #'prte --no-ready-msg --report-uri dvm.uri_0 &',
            f"prte --no-ready-msg --report-uri dvm.uri_0 --host $(scontrol show hostnames | xargs | sed 's/ /:{self.num_tasks_per_node},/g' | sed 's/$/:{self.num_tasks_per_node}/') &",
            'TIMEFORMAT="runtime,%R,%U,%S"',
            'sleep 10'
        ]    
        cmd = f"prun --dvm-uri file:dvm.uri_0 --num-connect-retries 1000 hostname"
        one_liner = f'for n in $(seq 1 {self.num_iters}); do {cmd}; done'
        self.executable = 'time'
        self.executable_opts = ['bash','-c', f"'{one_liner}'"]
        self.postrun_cmds = ["pterm --dvm-uri file:dvm.uri_0", "sleep 2"]

    @sanity_function
    def check_test(self):
        flags = [self.check_host_count(expected_count=self.num_iters*self.num_tasks),
                 self.check_errors()]
        return sn.all(flags)

@rfm.simple_test
class cycle_test_initialize_finalize(base_test):
    descr = "Test Cycle in pmix-test"
    test_name = "cycle"
    num_tasks = 120
    num_tasks_per_node = 12
    cycle_test = fixture(build_cycle,scope = 'environment')
    num_iters=100

    @run_before("run")
    def prepare_test(self):
        test_path = self.cycle_test.test_path
        self.prerun_cmds = [
            f'cd {test_path}',
            #'prte --no-ready-msg --report-uri dvm.uri_1 &',
            f"prte --no-ready-msg --report-uri dvm.uri_1 --host $(scontrol show hostnames | xargs | sed 's/ /:{self.num_tasks_per_node},/g' | sed 's/$/:{self.num_tasks_per_node}/') &",
            'TIMEFORMAT="runtime,%R,%U,%S"',
            'sleep 5'
        ]    
        cmd = f"prun --dvm-uri file:dvm.uri_1 --num-connect-retries 1000  ./init_finalize_pmix"
        one_liner = f'for n in $(seq 1 {self.num_iters}); do {cmd}; done'
        self.executable = 'time'
        self.executable_opts = ['bash','-c', f"'{one_liner}'"]
        self.postrun_cmds = ["pterm --dvm-uri file:dvm.uri_1", "sleep 2"]

    @sanity_function
    def check_test(self):
        flags = [self.check_host_count(expected_count=0),
                 self.check_errors()]
        return sn.all(flags)

@rfm.simple_test
class cycle_test_initialize_finalize_multi(base_test):
    descr = "Test Cycle in pmix-test"
    test_name = "cycle"
    cycle_test = fixture(build_cycle,scope = 'environment')
    num_iters=100

    @run_before("run")
    def prepare_test(self):
        test_path = self.cycle_test.test_path
        self.prerun_cmds = [
            f'cd {test_path}',
            #'prte --no-ready-msg --report-uri dvm.uri_2 &'
            f"prte --no-ready-msg --report-uri dvm.uri_2 --host $(scontrol show hostnames | xargs | sed 's/ /:{self.num_tasks_per_node},/g' | sed 's/$/:{self.num_tasks_per_node}/') &"
        ]    
        cmd = f"prun --dvm-uri file:dvm.uri_2 --num-connect-retries 1000  ./multi_init_finalize_pmix"
        one_liner = (
            f'for n in $(seq 1 {self.num_iters}); do '
            f'{cmd}; '
            f' done; '
            f'echo CYCLE_MULTI_COMPLETED'
        )
        self.executable = 'time'
        self.executable_opts = ['bash', '-c', f"'{one_liner}'"]
        self.postrun_cmds = ["pterm --dvm-uri file:dvm.uri_2", "sleep 2"]

    @sanity_function
    def check_test(self):
        flags = [
            self.check_host_count(expected_count=0),
            self.check_errors(),
            sn.assert_found(r'CYCLE_MULTI_COMPLETED', self.stdout)
        ]
        return sn.all(flags)

@rfm.simple_test
class prun_wrapper_test_hostname(base_test):
    descr = "Test prun-wrapper in pmix-test"
    test_name = "prun-wrapper"
    prun_test = fixture(build_prun_wrapper,scope = 'environment')
    @run_before("run")
    def prepare_test(self):
        test_path = self.prun_test.test_path
        self.prerun_cmds = [ f'cd {test_path}', 'TIMEFORMAT="runtime,%R,%U,%S"'  ]    
        #cmd = f" prterun --map-by node hostname"
        cmd = f"prterun  --host $(scontrol show hostnames | xargs | sed 's/ /:{self.num_tasks_per_node},/g' | sed 's/$/:{self.num_tasks_per_node}/') --map-by node hostname"
        self.executable = 'time'
        self.executable_opts = [ f"{cmd}"]
        self.postrun_cmds = ["sleep 2"]
    @sanity_function
    def check_test(self):
        flags = [self.check_host_count(),
                 self.check_errors()]
        return sn.all(flags)

@rfm.simple_test
class prun_wrapper_test_hostname_absolute(base_test):
    descr = "Test prun-wrapper in pmix-test"
    test_name = "prun-wrapper"
    prun_test = fixture(build_prun_wrapper,scope = 'environment')
    @run_before("run")
    def prepare_test(self):
        test_path = self.prun_test.test_path
        self.prerun_cmds = [
            f'cd {test_path}',
            'TIMEFORMAT="runtime,%R,%U,%S"',
            'ABS_PATH=$(which prterun)',
            'ABS_PATH=$(dirname $ABS_PATH)'
        ]
        #cmd = f"$ABS_PATH/prterun --map-by node hostname"
        cmd = f"$ABS_PATH/prterun  --host $(scontrol show hostnames | xargs | sed 's/ /:{self.num_tasks_per_node},/g' | sed 's/$/:{self.num_tasks_per_node}/') --map-by node hostname"
        self.executable = 'time'
        self.executable_opts = [ f"{cmd}"]
        self.postrun_cmds = ["sleep 2"]
    @sanity_function
    def check_test(self):
        flags = [self.check_host_count(),
                 self.check_errors()]
        return sn.all(flags)

@rfm.simple_test
class prun_wrapper_test_hello(base_test):
    descr = "Test prun-wrapper in pmix-test"
    test_name = "prun-wrapper"
    prun_test = fixture(build_prun_wrapper,scope = 'environment')
    @run_before("run")
    def prepare_test(self):
        test_path = self.prun_test.test_path
        self.prerun_cmds = [ f'cd {test_path}', 'TIMEFORMAT="runtime,%R,%U,%S"'  ]    
        #cmd = f"prterun --map-by node  ../hello_world/hello"
        cmd = f"prterun  --host $(scontrol show hostnames | xargs | sed 's/ /:{self.num_tasks_per_node},/g' | sed 's/$/:{self.num_tasks_per_node}/') --map-by node ../hello_world/hello"
        self.executable = 'time'
        self.executable_opts = [ f"{cmd}"]
        self.postrun_cmds = ["sleep 2"]
    @sanity_function
    def check_test(self):
        flags = [self.check_host_count(),
                 self.check_errors()]
        return sn.all(flags)

@rfm.simple_test
class manystress_test(base_test):
    descr = "Test manystress in pmix-test"
    test_name = "manystress"

    manystress_build = fixture(build_manystress, scope = 'environment')

    @run_before("run")
    def prepare_test(self):

        test_path = self.manystress_build.test_path

        self.prerun_cmds = [
            f'cd {test_path}',
            f'export CI_NUM_NODES={self.num_tasks // self.num_tasks_per_node}',
            f'export CI_NUM_CORES_PER_NODE={self.num_tasks_per_node}',
            f"scontrol show hostnames | sed 's/$/ slots={self.num_tasks_per_node}/' > prte.hostfile",
            'export CI_HOSTFILE=$PWD/prte.hostfile',
            'TIMEFORMAT="runtime,%R,%U,%S"'
        ]

        self.executable = "time"
        self.executable_opts = ["./run.sh"]

    @sanity_function
    def check_test(self):
        flags = [
            sn.assert_found(r"SUCCESS", self.stdout),
            self.check_errors()
        ]
        return sn.all(flags)
