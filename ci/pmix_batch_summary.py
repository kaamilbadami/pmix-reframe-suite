#!/usr/bin/env python3
"""Checkpoint-first batch reporting; never writes authoritative CI state."""
import argparse
from datetime import datetime, timezone
import errno
import json
from pathlib import Path
import tempfile

from collect_pmix_child_results import atomic_write
from pmix_result_summary import read_json, publish, SHA, eastern_time, age_text, write_text_report
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
                  saved_state=None, apply_exit_code=None, now=None):
    now = now or datetime.now(timezone.utc)
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

    descriptions = {
        'failed': 'CI job failed; inspect the job for test, build, or infrastructure errors.',
        'timedout': 'CI job timed out; this is incomplete testing, not proof of a software failure.',
        'canceled': 'CI job was canceled; testing is incomplete.',
        'missing': 'Result record is missing; successful testing is not established.',
        'malformed': 'Result record is invalid or mismatched; successful testing is not established.',
        'unknown': 'CI outcome is unknown; testing is incomplete.',
    }
    attempts = {}
    for item in items:
        commit = item['commit']
        attempt = attempt_details(collection, baseline, suite,
                                  output / 'attempt-details' / commit, jobs.get(commit), index)
        attempts[commit] = attempt
        item['attempt'] = attempt
        if item['category'] != 'passed':
            item['reason'] = descriptions[item['status']]
            if item['status'] != 'timedout' and attempt and attempt.get('reason'):
                item['reason'] = attempt['reason']
    # Only the display category changes; strict result records and the
    # consecutive-success boundary above remain authoritative.
    for item in items:
        attempt = item['attempt']
        if (item['status'] == 'failed' and attempt and
                attempt['checks']['failed'] == 0 and attempt['checks']['incomplete'] > 0):
            item['category'] = 'incomplete'
    totals = {key: sum(item['category'] == key for item in items)
              for key in ('passed', 'failed', 'incomplete')}
    blocker = attempts.get(first['commit']) if first else None
    boundary_attempt = attempts.get(boundary) if prefix else None
    latest = attempts.get(latest_job['commit']) if latest_job else None
    if first:
        first_item = next(item for item in items if item['commit'] == first['commit'])
        first['reason'] = first_item['reason']
        first['category'] = first_item['category']
        if blocker:
            first['runtime_checks'] = blocker['checks']
            first['prrte_revision'] = blocker['prrte_revision']

    saved_values = start
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
    try:
        good_time = datetime.fromtimestamp(int(saved_values['LAST_SUCCESS_EPOCH']), timezone.utc)
        good_at = good_time.isoformat()
        good_age = (now - good_time).total_seconds()
    except (ValueError, OverflowError, OSError):
        good_at, good_age = None, None
    data = dict(schema_version=2, status=status, suite_commit=suite,
                generated_at=now.isoformat(), last_known_good_recorded_at=good_at,
                last_known_good_age_seconds=good_age,
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
        upstream_time = datetime.strptime(last_commit['committed_at'], '%Y-%m-%d %H:%M:%S UTC').replace(tzinfo=timezone.utc).isoformat()
        labels['no_new_commits'] = 'No new commits to test since {}.'.format(eastern_time(upstream_time))
    rows = [('Status', labels[status]), ('Last known good commit', saved),
            ('Last known good recorded', eastern_time(good_at)),
            ('Time since last known good', age_text(good_age)),
            ('Report generated', eastern_time(now.isoformat()))]
    if first:
        rows += [('First blocker', first['commit']), ('Reason', first['reason'])]
    if commits:
        rows.append(('Commits tested', '{passed} passed / {failed} failed / {incomplete} incomplete'.format(**totals)))
        rows.append(('Latest test time', eastern_time(latest['tested_at']) if latest else 'Unknown'))
    text = 'OpenPMIx testing status\n\n' + '\n'.join(
        '{}: {}'.format(key, value) for key, value in rows) + '\n'
    if not commits:
        text += '\nNo tests ran during this check.'
        if update == 'unchanged':
            text += ' The last-known-good commit is unchanged.'
        text += '\n'
    if first and any(item['category'] == 'passed' for item in items[prefix + 1:]):
        text += '\nLater passing commits do not advance the last-known-good commit past this blocker.\n'
    if update == 'pending':
        text += '\nPreliminary report: this is not a confirmed saved update. Check the Apply job for the final report.\n'
    if index.get('pipeline_url'):
        text += '\nBatch pipeline (all commit jobs): {}\n'.format(index['pipeline_url'])
    affected = [item for item in items if item['category'] != 'passed']
    if affected:
        text += '\nFailed or incomplete commits\n'
        for item in affected:
            attempt = item['attempt']
            details = [('Result', item['category'].capitalize()), ('Reason', item['reason']),
                       ('PRRTE revision', (attempt['prrte_revision'] or 'Unknown') if attempt else 'Unknown')]
            if attempt:
                details += [('Runtime checks for this commit',
                             '{passed} passed / {failed} failed / {incomplete} incomplete (11 total)'.format(**attempt['checks'])),
                            ('Test time', eastern_time(attempt['tested_at']))]
                if attempt.get('check_reasons'):
                    details.append(('Incomplete checks', '\n' + '\n'.join('  {}: {}'.format(name, reason) for name, reason in sorted(attempt['check_reasons'].items()))))
            else:
                details.append(('Runtime checks for this commit', 'Unavailable; successful runtime testing is not established.'))
            job_url = jobs.get(item['commit'], {}).get('web_url')
            details.append(('Test job for this commit', job_url or 'Unavailable'))
            text += '\nCommit: {}\n'.format(item['commit']) + '\n'.join(
                '{}: {}'.format(key, value) for key, value in details) + '\n'
    text += '\nLast-known-good time is the saved success/checkpoint record time, not the upstream commit date.\nAges are measured when this report was generated.\n'
    write_text_report(output, text)
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
