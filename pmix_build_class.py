# SPDX-FileCopyrightText: 2026 Niccolo Tosato niccolo.tosato@yahoo.it
#
# SPDX-License-Identifier: MIT

import os
import shlex
import reframe as rfm
import reframe.utility.sanity as sn
from libevent_build_class import build_libevent


class fetch_pmix(rfm.RunOnlyRegressionTest):
    descr = "Fetch pmix"
    branch = variable(str, value='master')
    commit = variable(
        str,
        value=os.environ.get('PMIX_COMMIT', '')
    )
    trusted_source = variable(
        str,
        value=os.environ.get('PMIX_TRUSTED_SOURCE_DIR', '')
    )
    executable = '/bin/bash'
    local = True

    @sanity_function
    def validate_download(self):
        return sn.assert_eq(self.job.exitcode,0)

    @run_before('run')
    def prepare_download(self):
        branch = shlex.quote(self.branch)
        requested_commit = shlex.quote(self.commit)
        trusted_source = shlex.quote(self.trusted_source)
        script = f"""
set -euo pipefail
PMIX_REQUESTED_BRANCH={branch}
PMIX_REQUESTED_COMMIT={requested_commit}
PMIX_TRUSTED_SOURCE={trusted_source}
PMIX_BRANCH="origin/$PMIX_REQUESTED_BRANCH"
PMIX_BRANCH_REF="refs/remotes/$PMIX_BRANCH"

rm -rf pmix-git
if test -n "$PMIX_TRUSTED_SOURCE"; then
    if [[ ! $PMIX_REQUESTED_COMMIT =~ ^[0-9a-f]{{40}}$ ]]; then
        printf '%s\\n' \
            'ERROR: trusted PMIx source requires a lowercase exact commit' >&2
        exit 1
    fi
    if [[ $PMIX_TRUSTED_SOURCE != /* || ! -d $PMIX_TRUSTED_SOURCE ||
          -L $PMIX_TRUSTED_SOURCE ]]; then
        printf '%s\\n' 'ERROR: trusted PMIx source path is invalid' >&2
        exit 1
    fi
    PMIX_SHA=$(git --no-pager -C "$PMIX_TRUSTED_SOURCE" rev-parse \
        --verify HEAD)
    if [[ $PMIX_SHA != "$PMIX_REQUESTED_COMMIT" ]] ||
       git --no-pager -C "$PMIX_TRUSTED_SOURCE" symbolic-ref -q HEAD \
           >/dev/null ||
       [[ -n $(git --no-pager -C "$PMIX_TRUSTED_SOURCE" status \
           --porcelain=v1 --untracked-files=all) ]]; then
        printf '%s\\n' 'ERROR: trusted PMIx checkout identity is invalid' >&2
        exit 1
    fi
    PMIX_ORIGIN=$(git --no-pager -C "$PMIX_TRUSTED_SOURCE" \
        remote get-url origin)
    if [[ $PMIX_ORIGIN != https://github.com/openpmix/openpmix.git ]]; then
        printf '%s\\n' 'ERROR: trusted PMIx checkout origin is invalid' >&2
        exit 1
    fi
    if git --no-pager -C "$PMIX_TRUSTED_SOURCE" ls-files \
           --error-unmatch .gitmodules >/dev/null 2>&1 ||
       git --no-pager -C "$PMIX_TRUSTED_SOURCE" ls-files --stage |
           awk '$1 == "160000" {{ found=1 }} END {{ exit !found }}'; then
        printf '%s\\n' 'ERROR: trusted PMIx source contains submodules' >&2
        exit 1
    fi
    cp -a -- "$PMIX_TRUSTED_SOURCE" pmix-git
    PMIX_MODE=trusted-pr
    PMIX_BRANCH=refs/pull/validated/head
else
    if ! git check-ref-format "refs/heads/$PMIX_REQUESTED_BRANCH" \
            >/dev/null; then
        printf 'ERROR: invalid PMIx branch name: %s\\n' \
            "$PMIX_REQUESTED_BRANCH" >&2
        exit 1
    fi

    git --no-pager clone --no-checkout \
        https://github.com/openpmix/openpmix.git pmix-git
    git --no-pager -C pmix-git fetch --no-tags origin \
        "+refs/heads/$PMIX_REQUESTED_BRANCH:$PMIX_BRANCH_REF"
    PMIX_BRANCH_SHA=$(git --no-pager -C pmix-git rev-parse --verify \
        "$PMIX_BRANCH_REF^{{commit}}")

    if test -z "$PMIX_REQUESTED_COMMIT"; then
        PMIX_MODE=latest
        PMIX_SHA=$PMIX_BRANCH_SHA
    else
        PMIX_MODE=exact
        if ! PMIX_SHA=$(git --no-pager -C pmix-git rev-parse --verify \
            --end-of-options "$PMIX_REQUESTED_COMMIT^{{commit}}"); then
            printf 'ERROR: requested PMIx commit is invalid or does not exist: %s\\n' \
                "$PMIX_REQUESTED_COMMIT" >&2
            exit 1
        fi
        if ! git --no-pager -C pmix-git merge-base --is-ancestor \
            "$PMIX_SHA" "$PMIX_BRANCH_SHA"; then
            printf 'ERROR: requested PMIx commit %s is not part of %s\\n' \
                "$PMIX_REQUESTED_COMMIT" "$PMIX_BRANCH" >&2
            exit 1
        fi
    fi
    git --no-pager -C pmix-git checkout --detach "$PMIX_SHA"
    git --no-pager -C pmix-git submodule update --init --recursive
fi

printf 'PMIX_MODE=%s\\nPMIX_COMMIT=%s\\nPMIX_BRANCH=%s\\nPMIX_REQUESTED_COMMIT=%s\\n' \
    "$PMIX_MODE" "$PMIX_SHA" "$PMIX_BRANCH" "$PMIX_REQUESTED_COMMIT" \
    > pmix-commit.env
cat pmix-commit.env
git --no-pager -C pmix-git show -s --format='PMIX_DATE=%cI\\nPMIX_SUBJECT=%s' HEAD
"""
        self.executable_opts = ['-c', shlex.quote(script)]
        

class build_pmix(rfm.CompileOnlyRegressionTest):
    descr = 'Build pmix'
    build_system = 'Autotools'
    build_prefix = variable(str)

    # Select the Python environment supplied by the experiment environment.
    python_env = variable(str, value=os.environ.get('PMIX_PYTHON', 'python3'))
    pmix = fixture(fetch_pmix, scope='session')
    libevent = fixture(build_libevent, scope='environment')

    @run_before('compile')
    def prepare_build(self):
        self.build_prefix = 'pmix-git'
        source_tree = os.path.join(self.pmix.stagedir, self.build_prefix)
        commit_file = os.path.join(self.pmix.stagedir, 'pmix-commit.env')
        python_executable = self.python_env
        python_bin = os.path.dirname(python_executable)
        path_prefix = f'{python_bin}:' if python_bin else ''

        self.prebuild_cmds = [
            f'rm -rf {self.stagedir}/{self.build_prefix}',
            f'cp -a {source_tree} {self.stagedir}/{self.build_prefix}',
            f'cp {commit_file} {self.stagedir}/PMIX_COMMIT',
            f'cat {self.stagedir}/PMIX_COMMIT',
            f'cd {self.build_prefix}',
            './autogen.pl'
        ]

        # Make configure find the experiment Python and its Cython command.
        self.env_vars = {
            'PATH': f'{path_prefix}${{PATH}}',
            'LD_LIBRARY_PATH': (
                f'{self.stagedir}/lib:{self.libevent.stagedir}/lib:'
                '${LD_LIBRARY_PATH}'
            )
        }

        self.build_system.max_concurrency = 12

        # Install PMIx, discover the installed Python package directory, and
        # verify that the selected Python can import the new binding.
        self.postbuild_cmds = [
            'make install',
            (
                'set -euo pipefail; '
                f'python_site_packages=$(find {self.stagedir} -type d '
                "-path '*/site-packages' -print -quit); "
                'test -n "$python_site_packages"; '
                'python_egg=$(find "$python_site_packages" -mindepth 1 '
                '-maxdepth 1 -name \'pypmix-*.egg\' -print -quit); '
                'python_import_path=${python_egg:-$python_site_packages}; '
                f'rm -f {self.stagedir}/python-site-packages; '
                f'ln -s "$python_import_path" '
                f'{self.stagedir}/python-site-packages; '
                f'printf "PMIX_PYTHONPATH=%s\\n" "$python_import_path" '
                f'> {self.stagedir}/PMIX_PYTHONPATH; '
                f'PYTHONPATH={self.stagedir}/python-site-packages '
                f'{python_executable} -c '
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
