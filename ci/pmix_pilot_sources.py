#!/usr/bin/env python3
"""Stage pilot sources on the login node; consume verified local copies on compute."""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys

URLS = {'pmix': 'https://github.com/openpmix/openpmix.git',
        'prrte': 'https://github.com/openpmix/prrte.git'}
TARBALL = 'libevent-2.1.12-stable.tar.gz'
LIBEVENT_URL = 'https://github.com/libevent/libevent/releases/download/release-2.1.12-stable/' + TARBALL


def pins(env):
    result = {}
    for kind, key in [('pmix', 'PMIX_COMMIT'), ('prrte', 'PRRTE_COMMIT')]:
        value = env.get(key, '')
        if not re.fullmatch(r'[0-9a-f]{40}', value):
            raise ValueError('missing exact lowercase ' + key)
        result[kind] = value
    result['branch'] = env.get('PRRTE_BRANCH', 'v5.0')
    return result


def git(path, *args):
    return subprocess.check_output(['git', '-C', str(path)] + list(args),
                                   universal_newlines=True, timeout=30).strip()


def checked_tree(path, kind, sha):
    if path.is_symlink() or not path.is_dir():
        raise ValueError('missing local source tree: ' + str(path))
    if git(path, 'rev-parse', 'HEAD') != sha:
        raise ValueError('local ' + kind + ' commit mismatch')
    if git(path, 'status', '--porcelain', '--untracked-files=all'):
        raise ValueError('local ' + kind + ' tree is dirty')
    if git(path, 'remote', 'get-url', 'origin') != URLS[kind]:
        raise ValueError('local ' + kind + ' origin mismatch')


def bounded(argv, log, seconds=300):
    print('Preparing source: ' + ' '.join(argv), flush=True)
    env = dict(os.environ, GIT_TERMINAL_PROMPT='0')
    with log.open('a') as stream:
        process = subprocess.Popen(argv, stdout=stream, stderr=subprocess.STDOUT,
                                   env=env, start_new_session=True)
        try:
            rc = process.wait(timeout=seconds)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
            raise RuntimeError('source preparation command exceeded {}s'.format(seconds))
    if rc:
        raise RuntimeError('source preparation command failed with exit {}'.format(rc))


def prepare(root, env):
    identity = pins(env)
    # No partial source directory is accepted on a second attempt.
    root.mkdir(parents=True, exist_ok=False)
    log = root / 'prepare.log'
    try:
        for kind, branch in [('pmix', 'master'), ('prrte', identity['branch'])]:
            bounded(['git', 'check-ref-format', 'refs/heads/' + branch], log, 10)
            path = root / (kind + '-git')
            bounded(['git', 'clone', '--single-branch', '--branch', branch,
                     '--no-checkout', URLS[kind], str(path)], log)
            bounded(['git', '-C', str(path), 'merge-base', '--is-ancestor',
                     identity[kind], 'origin/' + branch], log, 30)
            bounded(['git', '-C', str(path), 'checkout', '--detach', identity[kind]], log, 60)
            bounded(['git', '-C', str(path), 'submodule', 'update', '--init', '--recursive'], log)
            checked_tree(path, kind, identity[kind])
        bounded(['wget', '--timeout=20', '--tries=2', '-O', str(root / TARBALL),
                 LIBEVENT_URL], log, 180)
        bounded(['tar', 'tzf', str(root / TARBALL)], log, 30)
        identity['libevent_sha256'] = hashlib.sha256((root / TARBALL).read_bytes()).hexdigest()
        identity['prrte_branch_commit'] = git(root / 'prrte-git', 'rev-parse',
                                             'origin/' + identity['branch'])
        (root / 'manifest.json').write_text(json.dumps(identity, indent=2) + '\n')
        print('Source preparation complete; compute jobs will use local copies only.', flush=True)
    except Exception:
        if log.exists():
            print('\n'.join(log.read_text(errors='replace').splitlines()[-40:]), file=sys.stderr)
        raise


def consume(kind, root, destination, env):
    expected = pins(env)
    if root.is_symlink() or not root.is_absolute():
        raise ValueError('offline source root must be an absolute real directory')
    manifest = root / 'manifest.json'
    if manifest.is_symlink():
        raise ValueError('invalid source manifest')
    recorded = json.loads(manifest.read_text())
    if any(recorded.get(k) != v for k, v in expected.items()):
        raise ValueError('offline source pins do not match this run')
    if kind == 'libevent':
        archive = root / TARBALL
        if archive.is_symlink() or hashlib.sha256(archive.read_bytes()).hexdigest() != recorded['libevent_sha256']:
            raise ValueError('libevent archive checksum mismatch')
        shutil.copyfile(str(archive), str(destination / TARBALL))
    elif kind in URLS:
        tree = root / (kind + '-git')
        checked_tree(tree, kind, expected[kind])
        shutil.copytree(str(tree), str(destination / (kind + '-git')), symlinks=True)
        if kind == 'pmix':
            metadata = ('PMIX_MODE=exact\nPMIX_COMMIT={pmix}\nPMIX_BRANCH=origin/master\n'
                        'PMIX_REQUESTED_COMMIT={pmix}\n').format(**expected)
            (destination / 'pmix-commit.env').write_text(metadata)
        else:
            metadata = ('PRRTE_MODE=exact\nPRRTE_VERSION=\nPRRTE_COMMIT={prrte}\n'
                        'PRRTE_BRANCH=origin/{branch}\nPRRTE_BRANCH_COMMIT={prrte_branch_commit}\n'
                        'PRRTE_ORIGIN=' + URLS['prrte'] + '\n').format(**recorded)
            (destination / 'prrte-source.env').write_text(metadata)
        print(metadata, end='')
    else:
        raise ValueError('unknown source kind')
    print('Using verified offline ' + kind + ' source')


def main():
    try:
        if len(sys.argv) == 3 and sys.argv[1] == 'prepare':
            prepare(Path(sys.argv[2]), os.environ)
        elif len(sys.argv) == 4 and sys.argv[1] == 'use':
            consume(sys.argv[2], Path(sys.argv[3]), Path.cwd(), os.environ)
        else:
            raise ValueError('usage: pmix_pilot_sources.py prepare ROOT | use KIND ROOT')
    except (ValueError, RuntimeError, OSError, subprocess.SubprocessError) as error:
        print('Pilot source error: ' + str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
