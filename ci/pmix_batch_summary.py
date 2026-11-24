#!/usr/bin/env python3
"""Checkpoint-first batch reporting; never writes authoritative CI state."""
import argparse
from datetime import datetime, timezone
import errno
import json
from pathlib import Path
import tempfile

from collect_pmix_child_results import atomic_write
from pmix_result_summary import read_json, publish, SHA, eastern_time
from reconcile_pmix_results import parse_state, parse_commits, parse_result, InputError


def result_status(path, commit, suite):
    try:
        return parse_result(path, commit, suite)
    except InputError as error:
        cause = error.__cause__
        return 'missing' if isinstance(cause, OSError) and cause.errno == errno.ENOENT else 'malformed'
    except FileNotFoundError:
        return 'missing'


def attempt_details(collection, baseline, suite, output, job, index):
    if not job:
        return None
    # Reuse the existing identity/count validation and timeout rendering.
    # Temporary inputs cannot overwrite the original collected records.
    with tempfile.TemporaryDirectory(prefix='pmix-display-') as temporary:
        inputs = Path(temporary)
        (inputs / 'summary-attempt.json').write_text(json.dumps(dict(
            job=job, child_pipeline_id=index.get('child_pipeline_id', ''),
            pipeline_url=index.get('pipeline_url'))))
        source = collection / (job['commit'] + '.summary.json')
        if not source.exists():
            source = collection / 'latest-attempt.json'
        if source.is_file() and not source.is_symlink():
            (inputs / 'latest-attempt.json').write_bytes(source.read_bytes())
        return publish(inputs, baseline, suite, output)


def batch_summary(collection, baseline, commits_file, suite, output,
                  saved_state=None, apply_exit_code=None):
    if not SHA.fullmatch(suite):
        raise ValueError('invalid suite revision')
    start, _ = parse_state(baseline)
    commits = parse_commits(commits_file)
    baseline_commit = start['PMIX_COMMIT']
    last_commit = None
    try:
        head = read_json(Path(str(commits_file) + '.head.json'))
        if (isinstance(head, dict) and isinstance(head.get('commit'), str) and
                SHA.fullmatch(head['commit']) and
                type(head.get('committed_epoch')) is int and head['committed_epoch'] >= 0):
            last_commit = dict(commit=head['commit'], committed_at=datetime.fromtimestamp(
                head['committed_epoch'], timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC'))
    except (OSError, ValueError, TypeError, OverflowError):
        pass
    if baseline_commit in commits and commits != [baseline_commit]:
        raise ValueError('baseline may only appear as a single health-check retest')
    try:
        index = read_json(collection / 'attempts.json')
    except (OSError, ValueError):
        index = dict(jobs=[], child_pipeline_id='', pipeline_url=None)
    try:
        latest_index = read_json(collection / 'summary-attempt.json')
    except (OSError, ValueError):
        latest_index = dict(job=None)
    jobs = {}
    for job in index.get('jobs', []):
        if (not isinstance(job, dict) or job.get('commit') not in commits
                or type(job.get('id')) is not int or job['id'] <= 0
                or job['commit'] in jobs):
            raise ValueError('invalid batch attempt metadata')
        jobs[job['commit']] = job
    latest_job = latest_index.get('job')
    if latest_job and latest_job.get('commit') not in commits:
        raise ValueError('latest attempt is outside this batch')

    totals = dict(passed=0, failed=0, incomplete=0)
    boundary, prefix, first = baseline_commit, 0, None
    items = []
    for commit in commits:
        status = result_status(collection / (commit + '.env'), commit, suite)
        category = 'passed' if status == 'success' else 'failed' if status == 'failed' else 'incomplete'
        totals[category] += 1
        items.append(dict(commit=commit, status=status, category=category))
        if first is None:
            if status == 'success':
                boundary, prefix = commit, prefix + 1
            else:
                first = dict(commit=commit, status=status, category=category,
                             job_url=jobs.get(commit, {}).get('web_url'))

    blocker = attempt_details(collection, baseline, suite, output / 'attempt-details/blocker',
                              jobs.get(first['commit']) if first else None, index)
    boundary_attempt = attempt_details(collection, baseline, suite, output / 'attempt-details/boundary',
                                      jobs.get(boundary) if prefix else None, index)
    latest = attempt_details(collection, baseline, suite, output / 'attempt-details/latest',
                             latest_job, latest_index)
    if first:
        descriptions = {
            'failed': 'CI job failed; inspect the job for test, build, or infrastructure errors.',
            'timedout': 'CI job timed out; this is incomplete testing, not proof of a software failure.',
            'canceled': 'CI job was canceled; testing is incomplete.',
            'missing': 'Result record is missing; successful testing is not established.',
            'malformed': 'Result record is invalid or mismatched; successful testing is not established.',
            'unknown': 'CI outcome is unknown; testing is incomplete.',
        }
        first['reason'] = descriptions[first['status']]
        if blocker:
            first['runtime_checks'] = blocker['checks']
            first['prrte_revision'] = blocker['prrte_revision']
            # Do not let a generic failed GitLab status erase a strict timeout record.
            if first['status'] != 'timedout' and blocker.get('reason'):
                first['reason'] = blocker['reason']

    saved, saved_scope = baseline_commit, 'batch-start checkpoint; Apply not yet confirmed'
    update = 'pending'
    if apply_exit_code is not None:
        update = 'failed' if apply_exit_code else 'unconfirmed'
    if saved_state:
        saved_values, _ = parse_state(saved_state)
        saved, saved_scope = saved_values['PMIX_COMMIT'], 'observed after Apply'
        if apply_exit_code == 0 and saved == boundary:
            update = 'applied' if saved != baseline_commit else 'unchanged'
    status = 'blocked' if first else 'complete' if commits else 'no_new_commits'
    if apply_exit_code is not None and update not in ('applied', 'unchanged'):
        status = 'checkpoint_update_failed' if apply_exit_code else 'checkpoint_update_unconfirmed'
    data = dict(schema_version=2, status=status, suite_commit=suite,
                batch_start_checkpoint=baseline_commit, saved_checkpoint=saved,
                saved_checkpoint_scope=saved_scope, passing_boundary=boundary,
                successful_prefix_count=prefix, checkpoint_update=update,
                apply_exit_code=apply_exit_code, commits_in_batch=len(commits),
                new_commits=0 if commits == [baseline_commit] else len(commits),
                commit_counts=totals, commits_beyond_passing_boundary=len(commits) - prefix,
                first_blocker=first, boundary_attempt=boundary_attempt,
                latest_attempt=latest, pipeline_url=index.get('pipeline_url'), items=items,
                last_commit=last_commit)
    output.mkdir(parents=True, exist_ok=True)
    atomic_write(output / 'latest-summary.json', (json.dumps(data, indent=2) + '\n').encode())
    labels = dict(blocked='Blocked', complete='All tested commits passed',
                  no_new_commits='No new commits to test.',
                  checkpoint_update_failed='Could not save the last-known-good update',
                  checkpoint_update_unconfirmed='Last-known-good update could not be confirmed')
    if status == 'no_new_commits' and last_commit and last_commit['commit'] == baseline_commit:
        labels['no_new_commits'] = 'No new commits to test since {}.'.format(last_commit['committed_at'])
    rows = [('Status', labels[status]), ('Last known good commit', saved)]
    if first:
        rows += [('First blocker', first['commit']), ('Reason', first['reason'])]
    if commits:
        rows.append(('Commits tested', '{passed} passed / {failed} failed / {incomplete} incomplete'.format(**totals)))
        if blocker:
            rows.append(('Runtime checks for first blocked commit', '{passed} passed / {failed} failed / {incomplete} incomplete (11 total)'.format(**blocker['checks'])))
    text = '# OpenPMIx testing status\n\n' + '\n'.join(
        '**{}:** {}  '.format(key, value) for key, value in rows) + '\n'
    if not commits:
        text += '\nNo tests ran during this check.'
        if update == 'unchanged':
            text += ' The last-known-good commit is unchanged.'
        text += '\n'
    if first and len(commits) > prefix + 1:
        text += '\nLater passing commits do not advance the last-known-good commit past this blocker.\n'
    if update == 'pending':
        text += '\nPreliminary report: this is not a confirmed saved update. Check the Apply job for the final report.\n'
    if commits:
        details = [('Suite revision', suite)]
        # Focus on the blocker when blocked; otherwise show the final passing
        # commit. Other attempts and checkpoint bookkeeping remain in JSON.
        focus = blocker if first else boundary_attempt or latest
        if focus:
            details += [('OpenPMIx commit', focus['pmix_commit']),
                        ('PRRTE revision', focus['prrte_revision'] or 'Unknown'),
                        ('Test time (Eastern)', eastern_time(focus['tested_at']))]
            if not first:
                details.append(('Runtime checks for this commit', '{passed} passed / {failed} failed / {incomplete} incomplete (11 total)'.format(**focus['checks'])))
        job_url = first['job_url'] if first else focus['job_url'] if focus else None
        if job_url:
            details.append(('Test job for this commit', job_url))
        if index.get('pipeline_url'):
            details.append(('Batch pipeline (all {} commit jobs)'.format(len(commits)), index['pipeline_url']))
        text += '\n## Test details\n\n' + ('Details below are for the first blocked commit only.\n\n' if first else 'Details below are for the final passing commit only.\n\n') + '\n'.join(
            '**{}:** {}  '.format(key, value) for key, value in details) + '\n'
    atomic_write(output / 'latest-summary.md', text.encode())
    return data


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('collection', 'baseline', 'commits', 'output'):
        parser.add_argument('--' + name, type=Path, required=True)
    parser.add_argument('--suite-commit', required=True)
    parser.add_argument('--saved-state', type=Path)
    parser.add_argument('--apply-exit-code', type=int)
    args = parser.parse_args()
    batch_summary(args.collection, args.baseline, args.commits, args.suite_commit,
                  args.output, args.saved_state, args.apply_exit_code)


if __name__ == '__main__':
    main()
