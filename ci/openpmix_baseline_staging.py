#!/usr/bin/env python3
"""Stage fixed baseline sources on login; validate the runner-owned allocation."""
import argparse
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import socket
import subprocess
import tarfile

from cleanup_pmix_slurm_jobs import field
from openpmix_pr_baseline import PMIX_SHA, PRRTE_SHA, RecordError, select
import pmix_single_sources as sources


ARCHIVE = Path('ci-openpmix-pr-preparation/baseline-sources.tar.gz')
STAGED = Path('ci-openpmix-baseline-sources')
RESTORED = Path('ci-openpmix-baseline-restored')


def command(argv):
    return subprocess.check_output(argv, text=True, timeout=30).strip()


def verify_pmix(path):
    if path.is_symlink() or not path.is_dir():
        raise ValueError('baseline PMIx tree is unavailable')
    def git(*args):
        return command(['git', '-C', str(path), *args])
    if (git('rev-parse', 'HEAD') != PMIX_SHA or
            git('rev-parse', 'refs/remotes/origin/openpmix-pr-4370') != PMIX_SHA or
            git('remote', 'get-url', 'origin') != 'https://github.com/openpmix/openpmix.git' or
            git('status', '--porcelain', '--untracked-files=all')):
        raise ValueError('staged PMIx identity differs from the fixed baseline')
    detached = subprocess.run(['git', '-C', str(path), 'symbolic-ref', '-q', 'HEAD'],
                              capture_output=True, timeout=10)
    if detached.returncode != 1:
        raise ValueError('staged PMIx must have a detached HEAD')
    command([os.environ['PMIX_PYTHON'], 'ci/validate_openpmix_oac.py', str(path), '--populated'])


def prepare():
    STAGED.mkdir(mode=0o700)
    subprocess.run(['/bin/bash', 'ci/fetch_openpmix_pr_checkout.sh', '4370', PMIX_SHA,
                    str(STAGED / 'openpmix')], check=True)
    verify_pmix(STAGED / 'openpmix')
    env = dict(os.environ, PMIX_COMMIT=PMIX_SHA, PRRTE_COMMIT=PRRTE_SHA, PRRTE_BRANCH='master')
    sources.prepare(STAGED / 'cache', env)
    with tarfile.open(ARCHIVE, 'w:gz') as archive:
        archive.add(STAGED, arcname='sources')
    print('Fixed baseline sources staged for the Slurm runner.')


def restore():
    if ARCHIVE.is_symlink() or not ARCHIVE.is_file():
        raise ValueError('baseline source archive is unavailable')
    RESTORED.mkdir(mode=0o700)
    root = RESTORED.resolve()
    with tarfile.open(ARCHIVE, 'r:gz') as archive:
        members = archive.getmembers()
        for member in members:
            name = PurePosixPath(member.name)
            if (name.is_absolute() or '..' in name.parts or not name.parts or
                    name.parts[0] != 'sources' or
                    not (member.isdir() or member.isfile() or member.issym())):
                raise ValueError('unsafe baseline archive member')
        # Write directories/files before symlinks, preventing link traversal
        # while writing. Never restore owners, devices, hardlinks, or setuid bits.
        for member in members:
            path = root / member.name
            if member.isdir():
                path.mkdir(mode=0o700, parents=True, exist_ok=True)
            elif member.isfile():
                path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                with path.open('xb') as output, archive.extractfile(member) as stream:
                    shutil.copyfileobj(stream, output)
                path.chmod(member.mode & 0o777)
        for member in members:
            if member.issym():
                if os.path.isabs(member.linkname):
                    raise ValueError('absolute baseline archive link')
                path = root / member.name
                path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                if path.parent.resolve() != path.parent:
                    raise ValueError('baseline archive link has a symlink parent')
                path.symlink_to(member.linkname)
        for member in members:
            if member.issym():
                resolved = (root / member.name).resolve()
                if root not in resolved.parents:
                    raise ValueError('baseline archive link escapes its root')
    verify_pmix(RESTORED / 'sources/openpmix')
    print('Fixed baseline source artifact restored and verified.')


def allocation(env=os.environ):
    job = env.get('SLURM_JOB_ID', '')
    if not re.fullmatch(r'[1-9][0-9]*', job):
        raise ValueError('Slurm runner did not provide a canonical allocation ID')
    details = command(['scontrol', 'show', 'job', job, '--oneliner'])
    user = field(details, 'UserId') or ''
    if (field(details, 'JobId') != job or field(details, 'JobState') != 'RUNNING' or
            field(details, 'Account') != 'gen243' or field(details, 'NumNodes') != '4' or
            not re.fullmatch(r'[^\s()]+\(' + str(os.getuid()) + r'\)', user)):
        raise ValueError('runner allocation must be four running GEN243 nodes owned by this UID')
    nodes = field(details, 'NodeList') or ''
    if not re.fullmatch(r'[A-Za-z0-9_,.\[\]-]+', nodes):
        raise ValueError('runner allocation node list is invalid')
    hosts = command(['scontrol', 'show', 'hostnames', nodes]).split()
    if len(hosts) != 4 or socket.gethostname().split('.')[0] != hosts[0].split('.')[0]:
        raise ValueError('runner script must run on the first of four allocated compute nodes')
    return {'pipeline_id': env['CI_PIPELINE_ID'], 'slurm_id': job, 'nodes': nodes,
            'node_count': 4, 'mode': 'jacamar-slurm-runner'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('prepare', 'restore', 'verify', 'allocation', 'record', 'accounting'))
    parser.add_argument('path', nargs='?')
    args = parser.parse_args()
    if args.action == 'accounting':
        evidence = Path('ci-openpmix-pr-execution/evidence')
        record = evidence / 'runner-allocation.json'
        if record.is_file() and not record.is_symlink():
            value = json.loads(record.read_text())
            if (value.get('pipeline_id') != os.environ.get('CI_PIPELINE_ID') or
                    not re.fullmatch(r'[1-9][0-9]*', value.get('slurm_id', ''))):
                raise ValueError('allocation accounting identity is invalid')
            output = command(['sacct', '-X', '-j', value['slurm_id'], '--parsable2',
                              '--format=JobID,Submit,Eligible,Start,End,Elapsed,State,ExitCode'])
            (evidence / 'runner-accounting.txt').write_text(output + '\n')
            print(output)
        else:
            print('No compute allocation record; inspect the Jacamar job trace for a queue timeout.')
        return
    if select(os.environ.get('CI_PIPELINE_ID', '')) == 'live-pr':
        raise ValueError('runner staging is restricted to the fixed baseline')
    if args.action == 'prepare':
        prepare()
    elif args.action == 'restore':
        restore()
    elif args.action == 'verify':
        verify_pmix(Path(args.path))
    else:
        value = allocation()
        if args.action == 'record':
            path = Path('ci-openpmix-pr-execution/evidence/runner-allocation.json')
            path.write_text(json.dumps(value, indent=2) + '\n')
            print(json.dumps(value))
        else:
            print(value['slurm_id'], value['nodes'])


if __name__ == '__main__':
    try:
        main()
    except (RecordError, ValueError, OSError, subprocess.SubprocessError, tarfile.TarError) as error:
        print('error: baseline Slurm staging: ' + str(error))
        raise SystemExit(2)
