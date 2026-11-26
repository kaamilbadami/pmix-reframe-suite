#!/usr/bin/env python3
"""Compare one fixed failing check under inherited and isolated environments."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

import yaml
from openpmix_fixed_diagnostic import checkout_exact, context
from pmix_result_summary import runtime_evidence, counts

ROOT = Path(__file__).resolve().parents[1]
PMIX_SHA = 'ef46a45d896777040eea85a892cf9c338091245b'
PRRTE_SHA = 'f684f116a2ae797923364a1274871ad2dd9691f7'
CHECK = 'PMIxPythonChildTimeoutTest'
STARTUP_KEYS = ('BASH_ENV', 'CONDA_PREFIX', 'CONDA_DEFAULT_ENV', 'CONDA_SHLVL',
                'CONDA_EXE', '_CE_CONDA', '_CE_M', 'MODULEPATH', 'LOADEDMODULES',
                '_LMFILES_', 'LMOD_CMD', 'LMOD_DIR', 'LMOD_SYSTEM_DEFAULT_MODULES',
                'LD_LIBRARY_PATH', 'LIBRARY_PATH', 'CPATH', 'PKG_CONFIG_PATH')


def stamp():
    return datetime.now(timezone.utc).isoformat()


def environment(source, root, mode):
    # Keep only known execution settings, never CI credentials or arbitrary
    # caller variables. Preserve site/Python startup state only for normal.
    env = dict(PATH=str(root / 'venv/bin') + ':/usr/bin:/bin',
               LANG='C.UTF-8', LC_ALL='C.UTF-8', HOME=str(root / 'home'),
               TMPDIR=str(root / 'tmp'))
    if mode == 'normal':
        for key in ('PATH', 'HOME') + STARTUP_KEYS:
            if key in source:
                env[key] = source[key]
    env.update(PMIX_COMMIT=PMIX_SHA, PRRTE_COMMIT=PRRTE_SHA, PRRTE_BRANCH='master',
               PMIX_PYTHON=str(root / 'venv/bin/python'),
               PMIX_TRUSTED_SOURCE_DIR=str(root / 'openpmix'),
               GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL='/dev/null',
               GIT_TERMINAL_PROMPT='0', GIT_ASKPASS='/bin/false')
    return env


def config(root, mode):
    configuration = yaml.safe_load((ROOT / 'sysconfig.yaml').read_text())
    partition = configuration['systems'][0]['partitions'][0]
    if mode == 'clean':
        partition['access'].append('--export=NIL')
    prepare = partition.get('prepare_cmds', [])
    partition['prepare_cmds'] = [
        "printf 'ENV_COMPARE site-init-start %s host=%s\\n' \"$(date -u +%FT%TZ)\" \"$(hostname)\""
    ] + prepare + [
        "printf 'ENV_COMPARE site-init-finished %s\\n' \"$(date -u +%FT%TZ)\"",
        "printf 'ENV_COMPARE BASH_ENV=%s CONDA_SHLVL=%s CONDA_PREFIX=%s\\n' \"${BASH_ENV-}\" \"${CONDA_SHLVL-}\" \"${CONDA_PREFIX-}\""
    ]
    path = root / 'comparisons' / mode / 'config.yaml'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(configuration))
    return path


def command(root, configuration):
    return [str(root / 'venv/bin/reframe'), '-C', str(configuration),
            '-c', str(ROOT / 'pmix_python_binding/reframe/pmix_python_child_timeout_test.py'),
            '-n', CHECK, '-p', 'pmix_test', '--system=frontier:batch',
            '--prefix', str(root / 'reframe'),
            '--report-file', str(root / 'reframe/run-report.json'),
            '--exec-policy=serial', '--keep-stage-files', '-r']


def result(root, mode, exit_code, started, elapsed):
    data = dict(mode=mode, started_at=started, elapsed_seconds=elapsed,
                runner_exit_code=exit_code, status='incomplete', check=CHECK,
                runtime_checks=dict(passed=0, failed=0, incomplete=1), fixtures_passed=0)
    report = root / 'reframe/run-report.json'
    try:
        cases = json.loads(report.read_text())['runs'][-1]['testcases']
        runtime = [case for case in cases if case.get('fixture') is False]
        fixtures = [case for case in cases if case.get('fixture') is True]
        if len(runtime) != 1 or runtime[0]['name'] != CHECK or len(fixtures) != 6:
            raise ValueError('unexpected diagnostic case set')
        data['fixtures_passed'] = sum(c['result'] == 'pass' for c in fixtures)
        outcomes, reasons = runtime_evidence(root)
        outcome = outcomes[CHECK]
        data.update(status=outcome, runtime_checks={k: int(outcome == k) for k in ('passed', 'failed', 'incomplete')},
                    incomplete_reason=reasons.get(CHECK), slurm_job_id=runtime[0].get('jobid'),
                    fail_reason=runtime[0].get('fail_reason'))
        if data['fixtures_passed'] != 6 or exit_code != 0 and outcome == 'passed':
            data['status'] = 'incomplete'
            data['runtime_checks'] = dict(passed=0, failed=0, incomplete=1)
    except (OSError, ValueError, KeyError, TypeError) as error:
        data['report_error'] = str(error)
    return data


def run(source):
    root = context(source)
    summary = dict(mode='environment-comparison', pmix_commit=PMIX_SHA, prrte_commit=PRRTE_SHA,
                   suite_commit=source['CI_COMMIT_SHA'], check=CHECK, node_count=2,
                   status='incomplete', attempts=[], started_at=stamp(),
                   caveat='Sequential runs use separate allocations; node and load differences remain possible.')
    output = root / 'environment-comparison.json'
    output.write_text(json.dumps(summary, indent=2) + '\n')
    try:
        checkout_exact(root / 'openpmix', PMIX_SHA)
        for mode in ('normal', 'clean'):
            configuration = config(root, mode)
            env = environment(source, root, mode)
            # This small metadata file excludes all credentials and arbitrary env.
            (configuration.parent / 'startup-settings.json').write_text(json.dumps(
                {key: env.get(key) for key in STARTUP_KEYS}, indent=2) + '\n')
            started, clock = stamp(), time.monotonic()
            print('ENV_COMPARE {} starting at {}'.format(mode, started), flush=True)
            (root / 'reframe').mkdir()
            with (root / 'reframe/run-console.log').open('w') as log:
                completed = subprocess.run(command(root, configuration), cwd=str(ROOT), env=env,
                                           stdout=log, stderr=subprocess.STDOUT)
            attempt = result(root, mode, completed.returncode, started, time.monotonic() - clock)
            summary['attempts'].append(attempt)
            shutil.move(str(root / 'reframe'), str(configuration.parent / 'reframe'))
            output.write_text(json.dumps(summary, indent=2) + '\n')
            print(json.dumps(attempt), flush=True)
        summary.update(status='complete', finished_at=stamp())
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        summary['error'] = str(error)
    output.write_text(json.dumps(summary, indent=2) + '\n')
    print(json.dumps(summary, indent=2), flush=True)
    return 0 if summary['status'] == 'complete' and all(a['status'] == 'passed' for a in summary['attempts']) else 1


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['run'])
    parser.parse_args()
    sys.exit(run(os.environ))
