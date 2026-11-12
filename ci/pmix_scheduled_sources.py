#!/usr/bin/env python3
"""Stage exact master sources for scheduled execution on compute nodes."""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tarfile

from openpmix_baseline_staging import allocation, compact_sources, restore_sources
import pmix_single_sources as sources
from validate_openpmix_oac import OAC_URL, validate


PREPARATION = Path('ci-scheduled-sources')
ARCHIVE = PREPARATION / 'sources.tar.gz'
RESTORED = Path('ci-scheduled-restored')
PMIX_URL = 'https://github.com/openpmix/openpmix.git'


def verify(root, env):
    expected = sources.pins(env)
    tree = root / 'openpmix'
    if tree.is_symlink() or not tree.is_dir():
        raise ValueError('scheduled PMIx source is unavailable')
    if (sources.git(tree, 'rev-parse', 'HEAD') != expected['pmix'] or
            sources.git(tree, 'remote', 'get-url', 'origin') != PMIX_URL or
            sources.git(tree, 'status', '--porcelain', '--untracked-files=all')):
        raise ValueError('scheduled PMIx source identity differs from the selected commit')
    detached = subprocess.run(['git', '-C', str(tree), 'symbolic-ref', '-q', 'HEAD'],
                              capture_output=True, timeout=10)
    if detached.returncode != 1:
        raise ValueError('scheduled PMIx source must have a detached HEAD')
    validate(tree, populated=True)
    sources.checked_tree(root / 'cache/prrte-git', 'prrte', expected['prrte'])
    recorded = json.loads((root / 'cache/manifest.json').read_text())
    if any(recorded.get(key) != value for key, value in expected.items()):
        raise ValueError('scheduled source manifest differs from the selected revisions')


def prepare(env):
    expected = sources.pins(env)
    PREPARATION.mkdir(mode=0o700)
    root = PREPARATION / 'sources'
    root.mkdir(mode=0o700)
    tree = root / 'openpmix'
    log = PREPARATION / 'prepare.log'
    def run(argv):
        sources.bounded(argv, log)
    run(['git', 'clone', '--single-branch', '--branch', 'master', '--no-checkout',
         PMIX_URL, str(tree)])
    run(['git', '-C', str(tree), 'merge-base', '--is-ancestor', expected['pmix'],
         'origin/master'])
    run(['git', '-C', str(tree), 'checkout', '--detach', expected['pmix']])
    oac = validate(tree)
    if oac != 'none':
        submodule = tree / 'config/oac'
        run(['git', 'init', str(submodule)])
        run(['git', '-C', str(submodule), 'remote', 'add', 'origin', OAC_URL])
        run(['git', '-C', str(submodule), 'fetch', '--no-tags',
             '--no-recurse-submodules', 'origin', oac])
        run(['git', '-C', str(submodule), 'checkout', '--detach', oac])
        run(['git', '-C', str(tree), 'config', '--local', 'submodule.oac.url', OAC_URL])
    sources.prepare(root / 'cache', env)
    compact_sources(root)
    verify(root, env)
    with tarfile.open(ARCHIVE, 'w:gz') as archive:
        archive.add(root, arcname='sources')
    print('Scheduled source archive: {:.2f} MiB'.format(ARCHIVE.stat().st_size / 1024**2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('prepare', 'restore', 'allocation', 'accounting'))
    action = parser.parse_args().action
    for key in ('CI_JOB_ID', 'CI_PIPELINE_ID'):
        if not re.fullmatch(r'[1-9][0-9]*', os.environ.get(key, '')):
            raise ValueError('scheduled staging requires canonical ' + key)
    if action == 'prepare':
        prepare(os.environ)
    elif action == 'restore':
        restore_sources(ARCHIVE, RESTORED)
        verify(RESTORED / 'sources', os.environ)
    elif action == 'allocation':
        value = allocation()
        value['ci_job_id'] = os.environ['CI_JOB_ID']
        evidence = Path('.ci-work') / os.environ['CI_JOB_ID']
        evidence.mkdir(parents=True, exist_ok=True)
        (evidence / 'runner-allocation.json').write_text(json.dumps(value, indent=2) + '\n')
        print(value['slurm_id'], value['nodes'])
    else:
        evidence = Path('.ci-work') / os.environ['CI_JOB_ID']
        record = evidence / 'runner-allocation.json'
        if not record.is_file() or record.is_symlink():
            print('No allocation record; inspect Jacamar output for a queue timeout.')
            return
        value = json.loads(record.read_text())
        if (value.get('pipeline_id') != os.environ['CI_PIPELINE_ID'] or
                value.get('ci_job_id') != os.environ['CI_JOB_ID'] or
                not re.fullmatch(r'[1-9][0-9]*', value.get('slurm_id', ''))):
            raise ValueError('allocation record belongs to a different job or is invalid')
        output = subprocess.check_output(
            ['sacct', '-X', '-j', value['slurm_id'], '--parsable2',
             '--format=JobID,Submit,Eligible,Start,End,Elapsed,State,ExitCode'],
            text=True, timeout=30)
        (evidence / 'runner-accounting.txt').write_text(output)
        print(output)


if __name__ == '__main__':
    try:
        main()
    except (ValueError, RuntimeError, OSError, subprocess.SubprocessError, tarfile.TarError) as error:
        print('error: scheduled source staging: ' + str(error), file=sys.stderr)
        raise SystemExit(2)
