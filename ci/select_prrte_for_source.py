#!/usr/bin/env python3
"""Select a supported exact PRRTE pin from an exact OpenPMIx checkout."""
import argparse
from contextlib import redirect_stdout
import sys
import re
import subprocess
from generate_pmix_child_pipeline import PRRTE_LEGACY, PRRTE_GETLINE, resolve_prrte_master


def select(header):
    declarations = re.findall(r'PMIX_EXPORT\s+char\s*\*\s*pmix_getline\s*\(([^)]*)\)\s*;', header)
    if len(declarations) == 1:
        signature = re.sub(r'\s+', '', declarations[0])
        if signature == 'FILE*fp':
            return PRRTE_LEGACY
        if signature == 'FILE*fp,bool*failed':
            return PRRTE_GETLINE
    raise ValueError('unsupported or ambiguous OpenPMIx pmix_getline declaration')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('checkout')
    parser.add_argument('commit')
    parser.add_argument('--prrte-master', action='store_true',
                        help='resolve one exact upstream PRRTE master revision')
    args = parser.parse_args()
    if not re.fullmatch(r'[0-9a-f]{40}', args.commit):
        parser.error('commit must be an exact lowercase SHA')
    if args.prrte_master:
        # Keep stdout a strict branch/SHA pair for the shell caller.
        with redirect_stdout(sys.stderr):
            commit = resolve_prrte_master()
        print('master', commit)
        return
    result = subprocess.run(['git', '-C', args.checkout, 'show', args.commit + ':src/util/pmix_string_copy.h'], stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
    if result.returncode:
        parser.error('cannot read the exact OpenPMIx header')
    try:
        branch, commit = select(result.stdout)
    except ValueError as error:
        parser.error(str(error))
    print(branch, commit)


if __name__ == '__main__':
    main()
