#!/usr/bin/env python3
"""Record runtime evidence and publish one compact latest-attempt summary.

Reporting only: never changes checkpoint state or reconciliation decisions.
"""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import subprocess

from collect_pmix_child_results import atomic_write
from openpmix_pr_artifacts import EXPECTED_TESTS
from reconcile_pmix_results import parse_state


TESTS = sorted(EXPECTED_TESTS)
SHA = re.compile(r'[0-9a-f]{40}')
ANSI = re.compile(r'\x1b\[[0-9;]*m')


def read_json(path):
    if path.is_symlink():
        raise ValueError('symlinked reporting input')
    return json.loads(path.read_text())


def counts(outcomes):
    return {key: sum(value == key for value in outcomes.values())
            for key in ('passed', 'failed', 'incomplete')}


def runtime_outcomes(root):
    outcomes = dict.fromkeys(TESTS, 'incomplete')
    # Terminal console records survive a killed ReFrame process. Ignore fixtures,
    # listings and RUN records. Later terminal records supersede earlier attempts.
    console = root / 'reframe/run-console.log'
    if console.is_file() and not console.is_symlink():
        for line in ANSI.sub('', console.read_text(errors='replace')).splitlines():
            match = re.match(r'^\[\s*(OK|FAIL|SKIP|ABORT)\s*\]\s*(?:\(\s*\d+/\d+\)\s*)?(PMIxPython\w+)\s+/', line)
            if match and match[2] in outcomes:
                outcomes[match[2]] = {'OK': 'passed', 'FAIL': 'failed'}.get(match[1], 'incomplete')
    report = root / 'reframe/run-report.json'
    try:
        document = read_json(report)
        # Final run includes the authoritative outcome for any rerun cases.
        for run in document['runs']:
            for case in run['testcases']:
                name = case.get('name')
                if name in outcomes and case.get('fixture') is False:
                    outcomes[name] = {'pass': 'passed', 'fail': 'failed'}.get(case.get('result'), 'incomplete')
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        pass  # A truncated/missing final report does not erase console evidence.
    return outcomes


def record(env, output):
    for key in ('PMIX_COMMIT', 'CI_COMMIT_SHA'):
        if not SHA.fullmatch(env.get(key, '').lower()):
            raise ValueError('invalid reporting revision')
    for key in ('CI_JOB_ID', 'CI_PIPELINE_ID'):
        if not re.fullmatch(r'[1-9][0-9]*', env.get(key, '')):
            raise ValueError('invalid reporting job identity')
    root = Path(env.get('CI_PROJECT_DIR', '.')) / '.ci-work' / env['CI_JOB_ID']
    outcomes = runtime_outcomes(root)
    revision = env.get('PRRTE_COMMIT', '')
    revision = revision if SHA.fullmatch(revision) else None
    source = 'requested' if revision else 'unknown'
    for path in sorted((root / 'reframe').glob('**/prrte-source.env')):
        if path.is_symlink():
            continue
        fields = dict(line.split('=', 1) for line in path.read_text().splitlines() if '=' in line)
        actual = fields.get('PRRTE_COMMIT') or fields.get('PRRTE_VERSION')
        if actual and (SHA.fullmatch(actual) or re.fullmatch(r'v?\d+\.\d+\.\d+(?:[A-Za-z0-9.-]*)', actual)):
            revision, source = actual, 'checkout'
            break
    document = dict(schema_version=1, pmix_commit=env['PMIX_COMMIT'].lower(),
                    suite_commit=env['CI_COMMIT_SHA'].lower(), prrte_revision=revision,
                    prrte_revision_source=source, job_id=int(env['CI_JOB_ID']),
                    pipeline_id=int(env['CI_PIPELINE_ID']), ci_status=env.get('CI_JOB_STATUS', 'unknown'),
                    recorded_at=datetime.now(timezone.utc).isoformat(),
                    checks=counts(outcomes), total_checks=len(TESTS))
    policy_path = root / 'prrte-policy.json'
    if policy_path.exists():
        document['prrte_policy'] = read_json(policy_path)
    output.mkdir(parents=True, exist_ok=True)
    atomic_write(output / (document['pmix_commit'] + '.summary.json'),
                 (json.dumps(document, indent=2) + '\n').encode())
    return document


def parse_timestamp(value):
    """Parse GitLab timestamps using APIs available on login-node Python 3.6."""
    if not isinstance(value, str):
        return None
    value = re.sub(r'([+-]\d{2}):(\d{2})$', r'\1\2', value.replace('Z', '+0000'))
    for pattern in ('%Y-%m-%dT%H:%M:%S.%f%z', '%Y-%m-%dT%H:%M:%S%z'):
        try:
            return datetime.strptime(value, pattern)
        except ValueError:
            pass
    return None


def eastern_time(value):
    """Render using system timezone data, including DST, on Python 3.6 runners."""
    parsed = parse_timestamp(value)
    if parsed is None:
        return 'Unknown'
    try:
        return subprocess.check_output(
            ['date', '--date=@{}'.format(parsed.timestamp()),
             '+%Y-%m-%d %I:%M:%S %p %Z'],
            env=dict(os.environ, TZ='America/New_York', LC_ALL='C'),
            universal_newlines=True, timeout=5).strip()
    except (OSError, subprocess.SubprocessError):
        return parsed.astimezone(timezone.utc).strftime(
            '%Y-%m-%d %H:%M:%S UTC (Eastern conversion unavailable)')


def latest_job(jobs):
    """Most recently finished attempt, not the newest SHA or last API page."""
    def key(job):
        timestamp = job.get('finished_at') or job.get('started_at') or job.get('created_at')
        parsed = parse_timestamp(timestamp)
        epoch = parsed.timestamp() if parsed else 0
        return epoch, job['id']
    return max(jobs, key=key) if jobs else None


def publish(collection, baseline, suite_commit, output):
    index = read_json(collection / 'summary-attempt.json')
    job = index.get('job')
    checkpoint, _ = parse_state(baseline)
    data = dict(schema_version=1, pmix_commit=None, prrte_revision=None,
                suite_commit=suite_commit, last_known_good_commit=checkpoint['PMIX_COMMIT'],
                last_known_good_scope='batch-start checkpoint', status='incomplete',
                checks=dict(passed=0, failed=0, incomplete=len(TESTS)), total_checks=len(TESTS),
                tested_at=None, job_url=None, pipeline_url=None,
                reason='No commit test attempt was available in this batch.')
    if job:
        data.update(pmix_commit=job['commit'], tested_at=job.get('finished_at') or job.get('started_at'),
                    job_url=job.get('web_url'), pipeline_url=index.get('pipeline_url'),
                    ci_status=job.get('status'), ci_failure_reason=job.get('failure_reason'))
        parsed_time = parse_timestamp(data['tested_at'])
        data['tested_at'] = parsed_time.astimezone(timezone.utc).isoformat() if parsed_time else None
        evidence = None
        try:
            evidence = read_json(collection / 'latest-attempt.json')
            if (evidence['pmix_commit'] != job['commit'] or evidence['job_id'] != job['id'] or
                    evidence['pipeline_id'] != int(index['child_pipeline_id']) or
                    evidence['suite_commit'] != suite_commit or evidence['schema_version'] != 1 or
                    evidence['total_checks'] != len(TESTS)):
                raise ValueError('mismatched reporting evidence')
            values = evidence['checks']
            if (set(values) != {'passed', 'failed', 'incomplete'} or
                    any(type(v) is not int or v < 0 for v in values.values()) or
                    sum(values.values()) != len(TESTS)):
                raise ValueError('invalid counts')
            data.update(checks=values, prrte_revision=evidence.get('prrte_revision'),
                        prrte_revision_source=evidence.get('prrte_revision_source'))
            if 'prrte_policy' in evidence:
                data['prrte_policy'] = evidence['prrte_policy']
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            evidence = None
        status = job.get('status')
        if data['checks']['failed']:
            data.update(status='failed', reason='Runtime check failure; see job logs.')
        elif status == 'success' and evidence and data['checks']['passed'] == len(TESTS):
            data.update(status='passed', reason=None)
        elif job.get('failure_reason') in ('job_execution_timeout', 'stuck_or_timeout_failure') or (evidence and evidence.get('ci_status') == 'timedout'):
            data['reason'] = 'CI job timed out; check logs/accounting to distinguish allocation waiting from execution.'
        elif status == 'failed':
            data['reason'] = 'CI job failed; inspect logs for build, setup, or infrastructure errors.'
        else:
            data['reason'] = 'Runtime results are incomplete or unavailable (CI status: {}).'.format(status or 'unknown')
    output.mkdir(parents=True, exist_ok=True)
    atomic_write(output / 'latest-summary.json', (json.dumps(data, indent=2) + '\n').encode())
    revision = data['prrte_revision'] or 'Unknown'
    if data.get('prrte_revision_source') == 'requested':
        revision += ' (requested; checkout not recorded)'
    rows = [('OpenPMIx commit tested', data['pmix_commit'] or 'No attempt'),
            ('PRRTE revision', revision), ('Suite commit', suite_commit),
            ('Test time (Eastern)', eastern_time(data['tested_at'])),
            ('Overall status', data['status'].capitalize()),
            ('Runtime checks for this commit', '{passed} passed / {failed} failed / {incomplete} incomplete ({} total)'.format(len(TESTS), **data['checks'])),
            ('Last known good (batch-start checkpoint)', data['last_known_good_commit']),
            ('Test job for this commit', data['job_url'] or 'Unavailable'),
            ('Batch pipeline (all commit jobs)', data['pipeline_url'] or 'Unavailable')]
    text = '# Latest OpenPMIx test attempt\n\n' + '\n'.join('**{}:** {}  '.format(k, v) for k, v in rows) + '\n'
    if data['reason']:
        text += '\n' + data['reason'] + '\n'
    atomic_write(output / 'latest-summary.md', text.encode())
    return data


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='action')
    writer = commands.add_parser('record')
    writer.add_argument('--output', type=Path, required=True)
    publisher = commands.add_parser('publish')
    publisher.add_argument('--collection', type=Path, required=True)
    publisher.add_argument('--baseline', type=Path, required=True)
    publisher.add_argument('--suite-commit', required=True)
    publisher.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.action is None:
        parser.error('a command is required: record or publish')
    if args.action == 'record':
        record(os.environ, args.output)
    else:
        publish(args.collection, args.baseline, args.suite_commit, args.output)


if __name__ == '__main__':
    main()
