#!/usr/bin/env python3
"""One fixed-revision two-node check; no live PR eligibility or checkpoint writes."""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys

from collect_pmix_child_results import atomic_write
from validate_openpmix_oac import OAC_URL, validate

PMIX_SHA = '0fb45ff3d1b67a91f690b68918566acc45131062'
PRRTE_SHA = '9c715a9cd3d42adf8deb7246586e8712599ee55b'
PMIX_URL = 'https://github.com/openpmix/openpmix.git'
CHECK = 'PMIxPythonMappingPPRNodeTest'
ROOT = Path(__file__).resolve().parents[1]


def git(tree, *args):
    return subprocess.check_output([
        '/usr/bin/git', '-c', 'credential.helper=', '-c', 'core.hooksPath=/dev/null',
        '-c', 'protocol.file.allow=never', '-c', 'http.followRedirects=false',
        '-C', str(tree), *args], universal_newlines=True).strip()


def checkout_exact(destination, sha=PMIX_SHA):
    if not re.fullmatch(r'[0-9a-f]{40}', sha):
        raise ValueError('source must be an exact SHA')
    if destination.exists() or destination.is_symlink():
        raise ValueError('diagnostic checkout must be fresh')
    destination.mkdir()
    git(destination, 'init', '-q')
    git(destination, 'remote', 'add', 'origin', PMIX_URL)
    # Fetch the object itself, not refs/pull/<number>/head or the moving master.
    git(destination, 'fetch', '--no-tags', '--no-recurse-submodules', '--depth=1', 'origin', sha)
    git(destination, 'checkout', '--detach', sha)
    if git(destination, 'rev-parse', 'HEAD') != sha:
        raise ValueError('fetched OpenPMIx revision differs')
    oac = validate(destination)
    if oac != 'none':
        submodule = destination / 'config/oac'
        if submodule.is_symlink() or (destination / 'config').is_symlink():
            raise ValueError('unsafe OAC checkout path')
        submodule.mkdir(parents=True, exist_ok=True)
        git(submodule, 'init', '-q')
        git(submodule, 'remote', 'add', 'origin', OAC_URL)
        git(submodule, 'fetch', '--no-tags', '--no-recurse-submodules', '--depth=1', 'origin', oac)
        git(submodule, 'checkout', '--detach', oac)
        git(destination, 'config', '--local', 'submodule.oac.url', OAC_URL)
    validate(destination, populated=True)
    if git(destination, 'status', '--porcelain=v1', '--untracked-files=all'):
        raise ValueError('diagnostic checkout is not clean')
    return oac


def context(env):
    for key in ('CI_JOB_ID', 'CI_PIPELINE_ID'):
        if not re.fullmatch(r'[1-9][0-9]*', env.get(key, '')):
            raise ValueError('invalid diagnostic job identity')
    if not re.fullmatch(r'[0-9a-f]{40}', env.get('CI_COMMIT_SHA', '')):
        raise ValueError('invalid suite revision')
    return ROOT / '.ci-work' / env['CI_JOB_ID']


def command(env, root):
    return [env['RFM_BIN'], '-C', str(ROOT / 'ci/openpmix_pr_sysconfig.yaml'),
            '-c', str(ROOT / 'pmix_python_binding/reframe/pmix_python_mapping_ppr_node_test.py'),
            '-n', CHECK, '-p', 'pmix_test', '--system=frontier:batch',
            '--prefix', str(root / 'reframe'), '--report-file', str(root / 'reframe/run-report.json'),
            '-S', 'node_counts=2', '-S', 'ppr_values=1', '-S', 'trials=1',
            '-S', 'slots_per_node=1', '--keep-stage-files', '-r']


def record(env, reason=None, runner_exit=None):
    root = context(env)
    data = dict(mode='fixed-multinode-diagnostic', suite_commit=env['CI_COMMIT_SHA'],
                pmix_commit=PMIX_SHA, prrte_commit=PRRTE_SHA, check=CHECK,
                node_count=2, trials=1, passed=0, failed=0, incomplete=1,
                status='incomplete', reason=reason or 'Runtime result unavailable.',
                job_id=env['CI_JOB_ID'], pipeline_id=env['CI_PIPELINE_ID'])
    try:
        document = json.loads((root / 'reframe/run-report.json').read_text())
        cases = document['runs'][-1]['testcases']
        runtime = [case for case in cases if case.get('fixture') is False]
        fixtures = [case for case in cases if case.get('fixture') is True]
        if len(runtime) != 1 or runtime[0]['name'] != CHECK or len(fixtures) != 6 or len(cases) != 7:
            raise ValueError('unexpected diagnostic case set')
        case = runtime[0]
        if case['result'] == 'pass' and all(c['result'] == 'pass' for c in fixtures):
            data.update(status='passed', passed=1, incomplete=0, reason=None)
        elif case['result'] == 'fail':
            data.update(status='failed', failed=1, incomplete=0,
                        reason='Selected runtime check failed; see retained startup and job logs.')
    except (OSError, ValueError, KeyError, TypeError):
        pass
    if (runner_exit not in (None, 0) or env.get('CI_JOB_STATUS') in ('failed', 'canceled', 'timedout')) and data['status'] == 'passed':
        data.update(status='incomplete', reason='Runner failed after reporting a runtime pass.')
    root.mkdir(parents=True, exist_ok=True)
    atomic_write(root / 'diagnostic-result.json', (json.dumps(data, indent=2) + '\n').encode())
    return data


def run(env):
    for key in ('CI_JOB_TOKEN', 'CI_REPOSITORY_URL', 'GITHUB_PR_READ_TOKEN', 'GITHUB_STATUS_TOKEN'):
        if key in env:
            raise ValueError('diagnostic must run inside its clean environment')
    root = context(env)
    record(env, 'Diagnostic setup/build/test has not completed.')
    print('Fixed historical diagnostic; not a live PR test or full-suite result.', flush=True)
    print('OpenPMIx={} PRRTE={} check={}'.format(PMIX_SHA, PRRTE_SHA, CHECK), flush=True)
    status = 2
    try:
        oac = checkout_exact(root / 'openpmix')
        atomic_write(root / 'source-selection.json', (json.dumps(dict(
            pmix_commit=PMIX_SHA, oac_commit=oac, prrte_commit=PRRTE_SHA,
            suite_commit=env['CI_COMMIT_SHA']), indent=2) + '\n').encode())
        execution_env = dict(env, PMIX_COMMIT=PMIX_SHA, PRRTE_COMMIT=PRRTE_SHA,
                             PRRTE_BRANCH='master', PMIX_TRUSTED_SOURCE_DIR=str(root / 'openpmix'))
        execution_env.pop('PMIX_SINGLE_OFFLINE_SOURCES', None)
        execution_env.pop('PMIX_RFM_EXEC_POLICY', None)
        # Fixtures build on the shell runner. Only this runtime check requests
        # a two-node allocation, using the repaired hardened configuration.
        status = subprocess.run(command(execution_env, root), env=execution_env, cwd=str(ROOT)).returncode
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        print('Diagnostic setup failed: {}'.format(error), file=sys.stderr)
    result = record(env, runner_exit=status)
    print(json.dumps(result), flush=True)
    return 0 if status == 0 and result['status'] == 'passed' else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('run', 'record'))
    args = parser.parse_args()
    if args.action == 'run':
        return run(os.environ)
    record(os.environ)
    return 0


if __name__ == '__main__':
    sys.exit(main())
