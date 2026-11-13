#!/usr/bin/env python3
"""Checkpoint-first batch reporting; never writes authoritative CI state."""
import argparse
import errno
import json
from pathlib import Path
import tempfile

from collect_pmix_child_results import atomic_write
from pmix_result_summary import read_json, publish, SHA
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
                latest_attempt=latest, pipeline_url=index.get('pipeline_url'), items=items)
    output.mkdir(parents=True, exist_ok=True)
    atomic_write(output / 'latest-summary.json', (json.dumps(data, indent=2) + '\n').encode())
    labels = dict(blocked='Blocked', complete='All commit jobs passed', no_new_commits='No new commits',
                  checkpoint_update_failed='Checkpoint update failed',
                  checkpoint_update_unconfirmed='Checkpoint update unconfirmed')
    rows = [
        ('Batch status', labels[status]),
        ('Last known good (saved checkpoint)', saved),
        ('Checkpoint confirmation', saved_scope),
        ('Passing boundary in this batch', boundary),
        ('Checkpoint update', update.capitalize()),
        ('Commits in batch', '{} ({} new commits)'.format(len(commits), data['new_commits'])),
        ('Commit job outcomes', '{passed} passed / {failed} failed / {incomplete} incomplete'.format(**totals)),
        ('Commits beyond the passing boundary', len(commits) - prefix),
        ('Suite revision', suite),
        ('Test pipeline', index.get('pipeline_url') or 'Unavailable'),
    ]
    if first:
        rows += [('First blocker', first['commit']), ('Blocker reason', first['reason']),
                 ('Blocker job', first['job_url'] or 'Unavailable')]
        if blocker:
            rows += [('Blocker PRRTE revision', blocker['prrte_revision'] or 'Unknown'),
                     ('Blocker runtime checks', '{passed} passed / {failed} failed / {incomplete} incomplete (11 total)'.format(**blocker['checks']))]
    if boundary_attempt:
        rows.append(('Passing-boundary PRRTE revision', boundary_attempt['prrte_revision'] or 'Unknown'))
    text = '# OpenPMIx batch test summary\n\n' + '\n'.join(
        '**{}:** {}  '.format(key, value) for key, value in rows) + '\n'
    if first:
        text += '\nPassing commits after the blocker remain recorded, but do not let the checkpoint skip it.\n'
    if update == 'pending':
        text += '\nPreliminary report: the passing boundary is a candidate, not a confirmed saved update. Check the Apply job for the final report.\n'
    text += '\nCommit job outcomes above count commits, not the 11 runtime checks within each job.\n'
    if latest:
        text += '\n## Latest attempt — supporting detail\n\n'
        text += '**OpenPMIx commit:** {}  \n**Attempt status:** {}  \n'.format(latest['pmix_commit'], latest['status'].capitalize())
        text += '**PRRTE revision:** {}  \n**Test time (UTC):** {}  \n'.format(latest['prrte_revision'] or 'Unknown', latest['tested_at'] or 'Unknown')
        text += '**Runtime checks:** {passed} passed / {failed} failed / {incomplete} incomplete (11 total)  \n'.format(**latest['checks'])
        text += '**Job details:** {}\n'.format(latest['job_url'] or 'Unavailable')
        if latest['reason']:
            text += '\n' + latest['reason'] + '\n'
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
