# SPDX-FileCopyrightText: 2026 Niccolo Tosato niccolo.tosato@yahoo.it
#
# SPDX-License-Identifier: MIT

import os
import shlex
import reframe as rfm
import reframe.utility.sanity as sn
from libevent_build_class import build_libevent
from pmix_build_class import build_pmix


class fetch_prrte(rfm.RunOnlyRegressionTest):
    descr = "Fetch prrte"
    version = variable(str,value='4.1.0')
    branch = variable(
        str,
        value=os.environ.get('PRRTE_BRANCH', 'v5.0')
    )
    commit = variable(
        str,
        value=os.environ.get('PRRTE_COMMIT', '')
    )
    executable = 'wget'
    local = True

    @sanity_function
    def validate_download(self):
        return sn.assert_eq(self.job.exitcode,0)

    @run_before('run')
    def prepare_download(self):
        if not self.commit:
            self.url = (
                'https://github.com/openpmix/prrte/releases/download/'
                f'v{self.version}/prrte-{self.version}.tar.gz'
            )
            self.executable_opts = [self.url]
            version = shlex.quote(self.version)
            url = shlex.quote(self.url)
            self.postrun_cmds = [
                (
                    "printf 'PRRTE_MODE=release\\nPRRTE_VERSION=%s\\n"
                    "PRRTE_COMMIT=\\nPRRTE_BRANCH=\\nPRRTE_ORIGIN=%s\\n' "
                    f'{version} {url} > prrte-source.env'
                )
            ]
            return

        branch = shlex.quote(self.branch)
        requested_commit = shlex.quote(self.commit)
        script = f"""
set -euo pipefail
PRRTE_REQUESTED_BRANCH={branch}
PRRTE_REQUESTED_COMMIT={requested_commit}
PRRTE_ORIGIN=https://github.com/openpmix/prrte.git
PRRTE_BRANCH="origin/$PRRTE_REQUESTED_BRANCH"
PRRTE_BRANCH_REF="refs/remotes/$PRRTE_BRANCH"

if [[ ! $PRRTE_REQUESTED_COMMIT =~ ^[0-9a-f]{{40}}$ ]]; then
    printf '%s\\n' \
        'ERROR: PRRTE_COMMIT must be a lowercase exact 40-character SHA' >&2
    exit 2
fi
if ! git check-ref-format "refs/heads/$PRRTE_REQUESTED_BRANCH" >/dev/null; then
    printf 'ERROR: invalid PRRTE branch name: %s\\n' \
        "$PRRTE_REQUESTED_BRANCH" >&2
    exit 2
fi

rm -rf prrte-git
git --no-pager clone --no-checkout "$PRRTE_ORIGIN" prrte-git
git --no-pager -C prrte-git fetch --no-tags origin \
    "+refs/heads/$PRRTE_REQUESTED_BRANCH:$PRRTE_BRANCH_REF"
PRRTE_BRANCH_SHA=$(git --no-pager -C prrte-git rev-parse --verify \
    "$PRRTE_BRANCH_REF^{{commit}}")
if ! PRRTE_SHA=$(git --no-pager -C prrte-git rev-parse --verify \
        --end-of-options "$PRRTE_REQUESTED_COMMIT^{{commit}}"); then
    printf 'ERROR: requested PRRTE commit is invalid or does not exist: %s\\n' \
        "$PRRTE_REQUESTED_COMMIT" >&2
    exit 1
fi
if [[ $PRRTE_SHA != "$PRRTE_REQUESTED_COMMIT" ]]; then
    printf 'ERROR: requested PRRTE object is not the exact commit SHA: %s\\n' \
        "$PRRTE_REQUESTED_COMMIT" >&2
    exit 1
fi
if ! git --no-pager -C prrte-git merge-base --is-ancestor \
        "$PRRTE_SHA" "$PRRTE_BRANCH_SHA"; then
    printf 'ERROR: requested PRRTE commit %s is not part of %s\\n' \
        "$PRRTE_REQUESTED_COMMIT" "$PRRTE_BRANCH" >&2
    exit 1
fi

git --no-pager -C prrte-git checkout --detach "$PRRTE_SHA"
git --no-pager -C prrte-git submodule update --init --recursive

printf 'PRRTE_MODE=exact\\nPRRTE_VERSION=\\nPRRTE_COMMIT=%s\\nPRRTE_BRANCH=%s\\nPRRTE_BRANCH_COMMIT=%s\\nPRRTE_ORIGIN=%s\\n' \
    "$PRRTE_SHA" "$PRRTE_BRANCH" "$PRRTE_BRANCH_SHA" "$PRRTE_ORIGIN" \
    > prrte-source.env
cat prrte-source.env
git --no-pager -C prrte-git show -s \
    --format='PRRTE_DATE=%cI%nPRRTE_SUBJECT=%s' HEAD
"""
        self.executable = '/bin/bash'
        self.executable_opts = ['-c', shlex.quote(script)]
        

class build_prrte(rfm.CompileOnlyRegressionTest):
    descr = 'Build prrte'
    build_system = 'Autotools'
    build_prefix = variable(str)
    prrte = fixture(fetch_prrte, scope='session')
    libevent = fixture(build_libevent, scope='environment')
    pmix = fixture(build_pmix, scope='environment')
    @run_before('compile')
    def prepare_build(self):
        provenance = os.path.join(self.prrte.stagedir, 'prrte-source.env')
        if self.prrte.commit:
            self.build_prefix = 'prrte-git'
            source_tree = os.path.join(
                self.prrte.stagedir,
                self.build_prefix
            )
            self.prebuild_cmds = [
                f'rm -rf {self.stagedir}/{self.build_prefix}',
                f'cp -a {source_tree} {self.stagedir}/{self.build_prefix}',
                f'cp {provenance} {self.stagedir}/prrte-source.env',
                f'cat {self.stagedir}/prrte-source.env',
                f'cd {self.build_prefix}',
                './autogen.pl'
            ]
        else:
            tarball = f"prrte-{self.prrte.version}.tar.gz"
            self.build_prefix = ".".join(tarball.split(".")[:3])
            fullpath = os.path.join(self.prrte.stagedir, tarball)
            self.prebuild_cmds = [
                f'cp {fullpath} {self.stagedir}',
                f'cp {provenance} {self.stagedir}/prrte-source.env',
                f'cat {self.stagedir}/prrte-source.env',
                f'tar xzf {tarball}',
                f'cd {self.build_prefix}',
                (
                    "sed -i '/PRTE_ERROR_LOG(PRTE_ERR_ADDRESSEE_UNKNOWN);/d' "
                    'src/mca/iof/hnp/iof_hnp.c'
                )
            ]

        self.build_system.max_concurrency = 12
        self.postbuild_cmds = ['make install']
        self.build_system.config_opts = [
            f"--prefix={self.stagedir}  "
            f"--with-libevent={self.libevent.stagedir}  "
            f"--with-pmix={self.pmix.stagedir}"
        ]
        
    
