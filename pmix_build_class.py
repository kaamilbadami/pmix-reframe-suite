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

    # Python environment used to build the PMIx Python binding.
    python_env = variable(
        str,
        value=(
            '/lustre/orion/scratch/kbadami/gen243/'
            'reframe_practice/pmix-py310'
        )
    )
    pmix = fixture(fetch_pmix, scope='session')
    libevent = fixture(build_libevent, scope='environment')

    @run_before('compile')
    def prepare_build(self):
        tarball = f"pmix-{self.pmix.version}.tar.gz"
        self.build_prefix = ".".join(tarball.split(".")[:3])
        fullpath = os.path.join(self.pmix.stagedir, tarball)
        python_bin = os.path.join(self.python_env, 'bin')

        self.prebuild_cmds = [
            f'cp {fullpath} {self.stagedir}',
            f'tar xzf {tarball}',
            f'cd {self.build_prefix}',
            "sed -i '2113i\if (0 == bo.size) return;' src/common/pmix_iof.c",
            "sed -i 's/rc = PMIx_tool_set_server_module(&self.myserver);/# Disabled for client-only tool finalize test/' bindings/python/pmix.pyx"
        ]

        # Make configure find the known-working Python and Cython commands.
        self.env_vars = {
            'PATH': f'{python_bin}:${{PATH}}',
            'LD_LIBRARY_PATH': (
                f'{self.stagedir}/lib:{self.libevent.stagedir}/lib:'
                '${LD_LIBRARY_PATH}'
            )
        }

        self.build_system.max_concurrency = 12

        # Install PMIx, then verify that Python can import the installed binding.
        self.postbuild_cmds = [
            'make install',
            (
                f'PYTHONPATH={self.stagedir}/lib/python3.10/site-packages '
                f'{python_bin}/python -c '
                '"import pmix; print(pmix.__file__)"'
            )
        ]

        self.build_system.config_opts = [
            (
                f'--prefix={self.stagedir} '
                f'--with-libevent={self.libevent.stagedir} '
                '--enable-python-bindings'
            )
        ]
