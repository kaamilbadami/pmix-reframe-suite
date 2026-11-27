#!/usr/bin/env python3
"""Cancel only the current pipeline's owned single-allocation baseline job."""
import json
import os
from pathlib import Path
import re
import subprocess

from cleanup_pmix_slurm_jobs import field, owned_job
from monitor_pmix_allocation import submitted_id


def cleanup(repo, pipeline_id, run=subprocess.run, uid=None):
    if not re.fullmatch(r'[1-9][0-9]*', pipeline_id):
        raise ValueError('canonical pipeline ID is required')
    root = repo.resolve() / 'ci-openpmix-pr-execution'
    allocation = root / 'reframe/stage' / ('single-allocation-' + pipeline_id)
    uid = os.getuid() if uid is None else uid
    report = {'pipeline_id': pipeline_id, 'canceled': [], 'errors': []}
    # Enumerate owned UID jobs as well as the recorded ID: this covers a signal
    # between successful submission and sbatch flushing its parsable response.
    def command(argv):
        return run(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                   text=True, timeout=10, check=False)
    listed = command(['squeue', '--noheader', '--user', str(uid), '--format=%i'])
    if listed.returncode:
        raise RuntimeError('could not enumerate allocation jobs')
    candidates = set(listed.stdout.split())
    recorded = submitted_id(allocation / 'submission.out')
    if recorded:
        candidates.add(recorded)
    for job in candidates:
        if not re.fullmatch(r'[1-9][0-9]*', job):
            continue
        details = command(['scontrol', 'show', 'job', job, '--oneliner'])
        if details.returncode:
            continue
        record = details.stdout.strip()
        if not owned_job(record, job, root, uid):
            continue
        if field(record, 'JobState') not in ('PENDING', 'RUNNING', 'CONFIGURING', 'COMPLETING', 'SUSPENDED'):
            continue
        # The general helper accepts all stages in a root; this workflow only
        # cancels the exact pipeline's outer allocation script.
        if (Path(field(record, 'WorkDir')).resolve() != allocation.resolve() or
                Path(field(record, 'Command')).resolve() != allocation / 'rfm_job.sh'):
            continue
        canceled = command(['scancel', job])
        if canceled.returncode:
            report['errors'].append(job)
        else:
            report['canceled'].append(job)
    return report


if __name__ == '__main__':
    try:
        repo = Path(__file__).resolve().parent.parent
        report = cleanup(repo, os.environ.get('CI_PIPELINE_ID', ''))
        evidence = repo / 'ci-openpmix-pr-execution/evidence'
        evidence.mkdir(parents=True, exist_ok=True)
        (evidence / 'allocation-cleanup.json').write_text(json.dumps(report, indent=2) + '\n')
        print(json.dumps(report))
        raise SystemExit(bool(report['errors']))
    except (ValueError, RuntimeError, OSError, subprocess.SubprocessError) as error:
        print('Allocation cleanup failed: ' + str(error))
        raise SystemExit(2)
