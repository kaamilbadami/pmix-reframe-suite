#!/usr/bin/env python3
"""Read Slurm accounting for exact test allocations found in this run's evidence."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import subprocess


def job_ids(root):
    identifiers = set()
    # Environment files are written on compute nodes, so this also works when
    # ReFrame never gets far enough to write its final report.
    for path in root.glob('**/runtime-environment.json'):
        try:
            value = json.loads(path.read_text())['environment'].get('SLURM_JOB_ID')
            if re.fullmatch(r'[1-9][0-9]*', str(value)):
                identifiers.add(str(value))
        except (OSError, ValueError, KeyError, TypeError):
            pass
    for path in root.glob('**/runtime-site-environment.log'):
        try:
            identifiers.update(re.findall(r'^SLURM_JOB_ID=([1-9][0-9]*)$', path.read_text(), re.M))
        except OSError:
            pass
    for path in root.glob('**/run-report.json'):
        try:
            for run in json.loads(path.read_text()).get('runs', []):
                for case in run.get('testcases', []):
                    value = str(case.get('jobid', ''))
                    if case.get('fixture') is False and re.fullmatch(r'[1-9][0-9]*', value):
                        identifiers.add(value)
        except (OSError, ValueError, TypeError):
            pass
    return sorted(identifiers, key=int)


def collect(root, run=subprocess.run):
    ids = job_ids(root)
    report = dict(captured_at=datetime.now(timezone.utc).isoformat(), job_ids=ids,
                  note='Accounting may lag; empty records are unavailable evidence, not a passing result.')
    if not ids:
        report['status'] = 'no-test-job-ids-found'
        return report
    if len(ids) > 256:
        report.update(status='unavailable', error='unexpected allocation count')
        return report
    columns = 'JobIDRaw,State,ExitCode,Elapsed,Timelimit,NodeList%500,Reason%200,Submit,Start,End'
    try:
        result = run(['sacct', '-j', ','.join(ids), '--noheader', '--parsable2', '--format=' + columns],
                     stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                     universal_newlines=True, timeout=15, check=False)
        report.update(status='collected' if result.returncode == 0 and result.stdout.strip() else 'unavailable',
                      columns=columns, records=result.stdout[:262144], error=result.stderr[:8192],
                      returncode=result.returncode)
    except (OSError, subprocess.TimeoutExpired) as error:
        report.update(status='unavailable', error=str(error))
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    args = parser.parse_args()
    if not args.root.is_dir():
        return
    report = collect(args.root)
    (args.root / 'slurm-accounting.json').write_text(json.dumps(report, indent=2) + '\n')
    print('Slurm runtime evidence: ' + report['status'])


if __name__ == '__main__':
    main()
