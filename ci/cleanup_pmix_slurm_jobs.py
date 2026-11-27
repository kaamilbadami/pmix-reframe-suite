#!/usr/bin/env python3
"""Cancel only this failed CI job's remaining ReFrame Slurm allocations."""
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time


def field(record, name):
    match = re.search(r'(?:^|\s)' + re.escape(name) + r'=(.*?)(?=\s+[A-Z][A-Za-z0-9]*=|$)', record)
    return match.group(1) if match else None


def owned_job(record, job_id, root, uid):
    if field(record, 'JobId') != job_id:
        return False
    user = field(record, 'UserId') or ''
    if not re.fullmatch(r'[^\s()]+\(' + str(uid) + r'\)', user):
        return False
    cwd = field(record, 'WorkDir')
    command = field(record, 'Command')
    if not cwd or not command or not os.path.isabs(cwd):
        return False
    stage = os.path.realpath(str(root / 'reframe' / 'stage')) + os.sep
    resolved = os.path.realpath(cwd)
    return (resolved.startswith(stage)
            and os.path.realpath(command) == os.path.join(resolved, 'rfm_job.sh'))


def cleanup(environ, run=subprocess.run, uid=None):
    if environ.get('CI_JOB_STATUS') == 'success':
        return {'skipped': 'successful CI job', 'canceled': [], 'errors': []}
    # Script timeouts may enter after_script before a final failure status is set.
    # This helper is called only after script termination; exact UID/workspace
    # ownership checks below remain the cancellation boundary.
    if environ.get('CI_JOB_STATUS') not in ('failed', 'canceled', 'running', 'timedout'):
        raise ValueError('unsupported after_script CI_JOB_STATUS: ' + repr(environ.get('CI_JOB_STATUS')))
    job_id = environ.get('CI_JOB_ID', '')
    project = environ.get('CI_PROJECT_DIR', '')
    if not re.fullmatch(r'[1-9][0-9]*', job_id) or not os.path.isabs(project):
        raise ValueError('cleanup requires canonical CI_JOB_ID and absolute CI_PROJECT_DIR')
    # Derive scope from runner identity; never accept a caller-supplied prefix.
    root = Path(os.path.realpath(project)) / '.ci-work' / job_id
    uid = os.getuid() if uid is None else uid
    report = {'ci_job_status': environ.get('CI_JOB_STATUS'), 'ci_job_id': job_id, 'root': str(root), 'canceled': [], 'errors': []}
    def command(argv):
        return run(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                   universal_newlines=True, timeout=20, check=False)
    listed = command(['squeue', '--noheader', '--user', str(uid), '--format=%i'])
    if listed.returncode:
        raise RuntimeError('could not list Slurm jobs: ' + listed.stderr.strip())
    deadline = time.monotonic() + 240
    for slurm_id in listed.stdout.split():
        if time.monotonic() >= deadline:
            report['errors'].append({'error': 'cleanup deadline exceeded'})
            break
        if not re.fullmatch(r'[1-9][0-9]*', slurm_id):
            continue
        try:
            inspected = command(['scontrol', 'show', 'job', slurm_id, '--oneliner'])
            # A job may have completed between listing and inspection.
            if inspected.returncode:
                continue
            if not owned_job(inspected.stdout.strip(), slurm_id, root, uid):
                continue
            canceled = command(['scancel', slurm_id])
            if canceled.returncode:
                report['errors'].append({'job_id': slurm_id, 'error': canceled.stderr.strip()})
            else:
                report['canceled'].append(slurm_id)
        except (OSError, subprocess.TimeoutExpired) as error:
            report['errors'].append({'job_id': slurm_id, 'error': str(error)})
    return report


def main():
    try:
        report = cleanup(os.environ)
        job_id = os.environ.get('CI_JOB_ID', '')
        if re.fullmatch(r'[1-9][0-9]*', job_id):
            Path('ci-results').mkdir(exist_ok=True)
            Path('ci-results/slurm-cleanup-' + job_id + '.json').write_text(json.dumps(report, indent=2) + '\n')
        print(json.dumps(report))
        return 1 if report['errors'] else 0
    except (ValueError, RuntimeError, OSError, subprocess.TimeoutExpired) as error:
        print('Slurm cleanup failed: ' + str(error), file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
