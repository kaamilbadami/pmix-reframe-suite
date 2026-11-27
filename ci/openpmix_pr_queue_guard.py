#!/usr/bin/env python3
"""Read-only admission check after obtaining the PR execution resource lock."""
import json
import os
from pathlib import Path
import sys
from urllib.parse import urlencode, urlsplit

from discover_eligible_openpmix_prs import (
    DiscoveryError, NUMBER_RE, SHA_RE, PIPELINE_NAME_RE, get_json,
)

RECORD = Path('ci-openpmix-pr-admission.json')


def identity(env):
    number, sha, pipeline = (env.get(k, '') for k in
                            ('OPENPMIX_PR_NUMBER', 'OPENPMIX_PR_EXPECTED_SHA', 'CI_PIPELINE_ID'))
    if not NUMBER_RE.fullmatch(number) or not SHA_RE.fullmatch(sha) or not NUMBER_RE.fullmatch(pipeline):
        raise DiscoveryError('invalid PR admission identity')
    return number, sha, int(pipeline)


def decision(env, get=get_json):
    number, sha, current = identity(env)
    api = env.get('CI_API_V4_URL', '')
    parts = urlsplit(api)
    project, token = env.get('CI_PROJECT_ID', ''), env.get('CI_JOB_TOKEN', '')
    if (parts.scheme != 'https' or not parts.netloc or parts.query or parts.fragment
            or not NUMBER_RE.fullmatch(project) or not token):
        raise DiscoveryError('invalid PR admission API configuration')
    base = api.rstrip('/') + '/projects/' + project + '/pipelines'
    headers = {'JOB-TOKEN': token, 'Accept': 'application/json'}
    wanted = 'openpmix-pr:{}:{}'.format(number, sha)
    own = get(base + '/' + str(current), headers)
    if not isinstance(own, dict) or own.get('id') != current or own.get('name') != wanted:
        raise DiscoveryError('pipeline identity does not match the selected PR head')

    # Check newer selections first: an obsolete queued revision must not build.
    for page in range(1, 21):
        query = urlencode(dict(source='parent_pipeline', order_by='id', sort='desc',
                               per_page=100, page=page))
        rows = get(base + '?' + query, headers)
        if not isinstance(rows, list):
            raise DiscoveryError('invalid pipeline list')
        reached_current = False
        for row in rows:
            if not isinstance(row, dict) or type(row.get('id')) is not int:
                raise DiscoveryError('invalid pipeline list identity')
            if row['id'] <= current:
                reached_current = True
                continue
            match = PIPELINE_NAME_RE.fullmatch(row.get('name') or '')
            if match and match.group(1) == number and match.group(2) != sha:
                return 'not-run', 'superseded', row['id']
        if reached_current or len(rows) < 100:
            break
    else:
        raise DiscoveryError('newer pipeline scan exceeded its bound')

    # Concurrent polls can create duplicate children before either is visible.
    # Only the oldest child for this exact PR head may execute automatically.
    query = urlencode(dict(source='parent_pipeline', name=wanted, order_by='id',
                           sort='asc', per_page=1))
    rows = get(base + '?' + query, headers)
    if not isinstance(rows, list) or not rows:
        raise DiscoveryError('selected pipeline is absent from named-head lookup')
    first = rows[0]
    if (not isinstance(first, dict) or first.get('name') != wanted
            or type(first.get('id')) is not int or first['id'] > current):
        raise DiscoveryError('invalid canonical PR pipeline')
    if first['id'] < current:
        return 'not-run', 'duplicate', first['id']
    return 'run', 'selected', None


def skipped(env, path=RECORD):
    if not path.exists():
        return False
    if path.is_symlink() or not path.is_file():
        raise DiscoveryError('unsafe PR admission record')
    data = json.loads(path.read_text())
    number, sha, pipeline = identity(env)
    if (not isinstance(data, dict) or data.get('version') != 1
            or data.get('pipeline_id') != pipeline or data.get('pr_number') != number
            or data.get('head_sha') != sha or data.get('decision') not in ('run', 'not-run')):
        raise DiscoveryError('PR admission record identity mismatch')
    if data['decision'] == 'not-run':
        related = data.get('related_pipeline_id')
        if (type(related) is not int or related <= 0
                or not ((data.get('reason') == 'duplicate' and related < pipeline)
                        or (data.get('reason') == 'superseded' and related > pipeline))):
            raise DiscoveryError('invalid PR admission skip reason')
        print('PR tests NOT RUN: {} (pipeline {}).'.format(data['reason'], related))
        return True
    return False


def main():
    try:
        if len(sys.argv) != 2 or sys.argv[1] not in ('check', 'was-skipped'):
            raise DiscoveryError('expected check or was-skipped')
        if sys.argv[1] == 'was-skipped':
            return 0 if skipped(os.environ) else 1
        if os.environ.get('OPENPMIX_PR_AUTOMATIC') != '1':
            return 0  # Explicit manual tests and retries are allowed.
        number, sha, pipeline = identity(os.environ)
        action, reason, related = decision(os.environ)
        if RECORD.exists() or RECORD.is_symlink():
            raise DiscoveryError('stale PR admission record')
        with RECORD.open('x') as stream:
            json.dump(dict(version=1, pipeline_id=pipeline, pr_number=number, head_sha=sha,
                           decision=action, reason=reason, related_pipeline_id=related), stream)
            stream.write('\n')
        print('PR admission: {} ({})'.format(action, reason))
        return 3 if action == 'not-run' else 0
    except (DiscoveryError, OSError, ValueError) as error:
        print('error: PR admission failed: ' + str(error), file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
