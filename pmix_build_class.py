# SPDX-FileCopyrightText: 2026 Niccolo Tosato niccolo.tosato@yahoo.it
#
# SPDX-License-Identifier: MIT

import os
import reframe as rfm
import reframe.utility.typecheck as typ
import reframe.utility.sanity as sn
from libevent_build_class import fetch_libevent,build_libevent


class fetch_pmix(rfm.RunOnlyRegressionTest):
    descr = "Fetch pmix"
    version = variable(str,value= '6.1.0')
    executable = 'wget'
    local = True
    @sanity_function
    def validate_download(self):
        return sn.assert_eq(self.job.exitcode,0)
    @run_before('run')
    def prepare_download(self):
        self.url = f"https://github.com/openpmix/openpmix/releases/download/v{self.version}/pmix-{self.version}.tar.gz"
        self.executable_opts = [f"{self.url}"]
        

class build_pmix(rfm.CompileOnlyRegressionTest):
    descr = 'Build pmix'
    build_system = 'Autotools'
    build_prefix = variable(str)
    pmix = fixture(fetch_pmix, scope='session')
    libevent = fixture(build_libevent, scope='environment')

    @run_before('compile')
    def prepare_build(self):
        tarball = f"pmix-{self.pmix.version}.tar.gz"
        self.build_prefix = ".".join(tarball.split(".")[:3])
        fullpath = os.path.join(self.pmix.stagedir, tarball)
        self.prebuild_cmds = [
            f'cp {fullpath} {self.stagedir}',
            f'tar xzf {tarball}',
            f'cd {self.build_prefix}',
            "sed -i '2113i\if (0 == bo.size) return;' src/common/pmix_iof.c"
        ]
        self.build_system.max_concurrency = 12
        self.postbuild_cmds = ['make install']
        self.build_system.config_opts = [f"--prefix={self.stagedir} --with-libevent={self.libevent.stagedir}"]
