#!/usr/bin/env python3
"""Generate exactly two full-suite allocation jobs; never read/write live state."""
import argparse
from pathlib import Path
import re
import subprocess
import tempfile

from generate_pmix_child_pipeline import atomic_write, fail, render_pipeline, select_prrte


def git(history, *args):
    return subprocess.check_output(
        ['git', '-C', str(history), *args], universal_newlines=True, timeout=180
    ).strip()


def generate(history, tip, output):
    if re.fullmatch(r'[0-9a-f]{40}', tip) is None:
        fail('PMIX_MULTI_ALLOCATION_TIP must be a lowercase 40-character SHA')
    if git(history, 'rev-parse', '--is-shallow-repository') != 'false':
        fail('pilot requires complete OpenPMIx history')
    subprocess.run(['git', '-C', str(history), 'merge-base', '--is-ancestor',
                    tip, 'master'], check=True, timeout=180)
    base = git(history, 'rev-parse', tip + '~2')
    commits = git(history, 'rev-list', '--reverse', '--topo-order',
                  base + '..' + tip).splitlines()
    if len(commits) != 2 or commits[-1] != tip:
        fail('tip must end a two-commit range without extra merged commits; choose another tip')
    selections = select_prrte(commits, history)
    output.mkdir(parents=True, exist_ok=True)
    atomic_write(output / 'commits.txt', '\n'.join(commits) + '\n')
    atomic_write(output / 'base-sha.txt', base + '\n')
    atomic_write(output / 'pipeline.yml', render_pipeline(
        commits, prrte_selections=selections, single_allocation=True))
    print('Pilot baseline (excluded): ' + base)
    print('Two exact commits, oldest first:\n' + '\n'.join(commits))
    print('Full suite per commit; allocations may run concurrently. No live state updates.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('tip')
    parser.add_argument('output', type=Path)
    options = parser.parse_args()
    if re.fullmatch(r'[0-9a-f]{40}', options.tip) is None:
        fail('PMIX_MULTI_ALLOCATION_TIP must be a lowercase 40-character SHA')
    with tempfile.TemporaryDirectory(prefix='pmix-two-commit-') as temporary:
        history = Path(temporary) / 'history.git'
        subprocess.run(['git', 'clone', '--bare', '--single-branch', '--branch',
                        'master', 'https://github.com/openpmix/openpmix.git', str(history)],
                       check=True, timeout=180)
        generate(history, options.tip, options.output)


if __name__ == '__main__':
    main()
