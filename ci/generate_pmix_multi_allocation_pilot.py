#!/usr/bin/env python3
"""Generate proposal-only full-suite allocation jobs from a fixed commit range."""
import argparse
from pathlib import Path
import re
import subprocess
import tempfile

from apply_reconciled_pmix_state import parse_state
from generate_pmix_child_pipeline import atomic_write, fail, render_pipeline, select_prrte


def git(history, *args):
    return subprocess.check_output(
        ['git', '-C', str(history), *args], universal_newlines=True, timeout=180
    ).strip()


def generate(history, tip, output, base=None):
    if re.fullmatch(r'[0-9a-f]{40}', tip) is None:
        fail('PMIX_MULTI_ALLOCATION_TIP must be a lowercase 40-character SHA')
    if git(history, 'rev-parse', '--is-shallow-repository') != 'false':
        fail('pilot requires complete OpenPMIx history')
    subprocess.run(['git', '-C', str(history), 'merge-base', '--is-ancestor',
                    tip, 'master'], check=True, timeout=180)
    two_commit_mode = base is None
    if two_commit_mode:
        base = git(history, 'rev-parse', tip + '~2')
    elif re.fullmatch(r'[0-9a-f]{40}', base) is None:
        fail('baseline must be a lowercase 40-character SHA')
    subprocess.run(['git', '-C', str(history), 'merge-base', '--is-ancestor',
                    base, tip], check=True, timeout=180)
    commits = git(history, 'rev-list', '--reverse', '--topo-order',
                  base + '..' + tip).splitlines()
    if two_commit_mode and (len(commits) != 2 or commits[-1] != tip):
        fail('tip must end a two-commit range without extra merged commits; choose another tip')
    selections = select_prrte(commits, history)
    output.mkdir(parents=True, exist_ok=True)
    atomic_write(output / 'commits.txt', ''.join(sha + '\n' for sha in commits))
    atomic_write(output / 'base-sha.txt', base + '\n')
    atomic_write(output / 'tip-sha.txt', tip + '\n')
    atomic_write(output / 'pipeline.yml', render_pipeline(
        commits, prrte_selections=selections, single_allocation=True))
    print('Pilot baseline (excluded): ' + base)
    print('Frozen upstream tip: ' + tip)
    print('Exact commits: {} (oldest first)\n{}'.format(len(commits), '\n'.join(commits)))
    print('Full suite per commit; allocations may run concurrently. No live state updates.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('tip')
    parser.add_argument('output', type=Path)
    parser.add_argument('--from-state', type=Path, help='validated local checkpoint snapshot; tip must be master')
    options = parser.parse_args()
    if options.from_state is not None:
        if options.tip != 'master':
            fail('--from-state requires master as tip; explicit tip overrides are not accepted')
        base = parse_state(options.from_state)[0]['PMIX_COMMIT']
    else:
        base = None
    if options.from_state is None and re.fullmatch(r'[0-9a-f]{40}', options.tip) is None:
        fail('PMIX_MULTI_ALLOCATION_TIP must be a lowercase 40-character SHA')
    with tempfile.TemporaryDirectory(prefix='pmix-two-commit-') as temporary:
        history = Path(temporary) / 'history.git'
        subprocess.run(['git', 'clone', '--bare', '--single-branch', '--branch',
                        'master', 'https://github.com/openpmix/openpmix.git', str(history)],
                       check=True, timeout=180)
        tip = git(history, 'rev-parse', 'master') if options.from_state else options.tip
        generate(history, tip, options.output, base=base)


if __name__ == '__main__':
    main()
