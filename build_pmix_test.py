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

class fetch_pmixtest(rfm.RunOnlyRegressionTest):
    descr = "Fetch pmix test"
    repository = f"https://github.com/NiccoloTosato/pmix-tests.git"
    executable = 'git'
    executable_opts = ["clone",f"{repository}"]
    local = True
    @sanity_function
    def validate_download(self):
        return sn.assert_eq(self.job.exitcode,0)

class test_builder(rfm.CompileOnlyRegressionTest):
    build_system = 'CustomBuild'
    prrte = fixture(build_prrte, scope = 'environment')
    pmix =  fixture(build_pmix, scope = 'environment')
    libevent = fixture(build_libevent, scope = 'environment')
    pmix_tests = fixture(fetch_pmixtest, scope = 'session')
    path = list()
    test_base_path=""
    ld_library_path = list()
    @run_before('compile')
    def prepare_env(self):
        for fix in [self.prrte, self.pmix, self.libevent]:
            self.path.append(os.path.join(fix.stagedir,"bin"))
            self.ld_library_path.append(os.path.join(fix.stagedir,"lib"))
        self.env_vars = {
            "PATH" : ":".join(self.path) + ":${PATH}",
            "LD_LIBRARY_PATH" : ":".join(self.ld_library_path) + ":${LD_LIBRARY_PATH}"
        }
        self.test_base_path=os.path.join(self.pmix_tests.stagedir,"pmix-tests","prrte")

class build_hello_world(test_builder):
    descr = 'Build pmix hello world test'
    test_name = "hello_world"
    @run_before('compile',always_last=True)
    def prepare_build(self):
        self.test_path = os.path.join(self.test_base_path, self.test_name)
        self.build_system.commands = [
            f'cd {self.test_path}', './build.sh'
        ]

class build_prun_wrapper(test_builder):
    descr = 'Build pmix prun-wrapper'
    test_name = "prun-wrapper"
    @run_before('compile',always_last=True)
    def prepare_build(self):
        self.test_path = os.path.join(self.test_base_path, self.test_name)
        self.build_system.commands = [
            f'cd {self.test_path}', './build.sh'
        ]

class build_cycle(test_builder):
    descr = 'Build pmix cycle'
    test_name = "cycle"
    @run_before('compile',always_last=True)
    def prepare_build(self):
        self.test_path = os.path.join(self.test_base_path, self.test_name)
        self.build_system.commands = [
            f'cd {self.test_path}', './build.sh'
        ]
    
class build_manystress(test_builder):
    descr = 'Build manystress sleeper executable'
    test_name = "manystress"
    @run_before('compile',always_last=True)
    def prepare_build(self):
        self.test_path = os.path.join(self.test_base_path, self.test_name)
        self.build_system.commands = [
            f'cd {self.test_path}', './build.sh'
        ]
   
