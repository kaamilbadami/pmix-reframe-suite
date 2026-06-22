# SPDX-FileCopyrightText: 2026 Niccolo Tosato niccolo.tosato@yahoo.it
#
# SPDX-License-Identifier: MIT

import os
import reframe as rfm
import reframe.utility.typecheck as typ
import reframe.utility.sanity as sn
from libevent_build_class import build_libevent
from pmix_build_class import build_pmix


class fetch_prrte(rfm.RunOnlyRegressionTest):
    descr = "Fetch prrte"
    version = variable(str,value='4.1.0')
    executable = 'wget'
    local = True
    @sanity_function
    def validate_download(self):
        return sn.assert_eq(self.job.exitcode,0)
    @run_before('run')
    def prepare_download(self):
        self.url = f"https://github.com/openpmix/prrte/releases/download/v{self.version}/prrte-{self.version}.tar.gz"

        self.executable_opts = [f"{self.url}"]
        

class build_prrte(rfm.CompileOnlyRegressionTest):
    descr = 'Build prrte'
    build_system = 'Autotools'
    build_prefix = variable(str)
    prrte = fixture(fetch_prrte, scope='session')
    libevent = fixture(build_libevent, scope='environment')
    pmix = fixture(build_pmix, scope='environment')
    @run_before('compile')
    def prepare_build(self):
        tarball = f"prrte-{self.prrte.version}.tar.gz"
        self.build_prefix = ".".join(tarball.split(".")[:3])
        fullpath = os.path.join(self.prrte.stagedir, tarball)
        self.prebuild_cmds = [
            f'cp {fullpath} {self.stagedir}',
            f'tar xzf {tarball}',
            f'cd {self.build_prefix}',
            "sed -i '/PRTE_ERROR_LOG(PRTE_ERR_ADDRESSEE_UNKNOWN);/d' src/mca/iof/hnp/iof_hnp.c"
        ]
        self.build_system.max_concurrency = 12
        self.postbuild_cmds = ['make install']
        self.build_system.config_opts = [f"--prefix={self.stagedir}  --with-libevent={self.libevent.stagedir}  --with-pmix={self.pmix.stagedir}"]
        
    
