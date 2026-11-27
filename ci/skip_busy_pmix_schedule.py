#!/usr/bin/env python3
"""Cancel only this idle scheduled poll when another master batch is active."""
import json
import os
from pathlib import Path
import sys
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, build_opener

from discover_eligible_openpmix_prs import DiscoveryError, NoRedirect, NUMBER_RE, strict_json

ACTIVE = ('created', 'waiting_for_resource', 'preparing', 'pending', 'running')
BRIDGE = 'run-pmix-scheduled-batch'


class API:
    def __init__(self, env):
        url = env.get('CI_API_V4_URL', '')
        parts = urlsplit(url)
        project, token = env.get('CI_PROJECT_ID', ''), env.get('GITLAB_PIPELINE_CONTROL_TOKEN', '')
        if (parts.scheme != 'https' or not parts.netloc or parts.query or parts.fragment
                or not NUMBER_RE.fullmatch(project) or not token):
            raise DiscoveryError('schedule admission requires GitLab API configuration and GITLAB_PIPELINE_CONTROL_TOKEN')
        self.base = url.rstrip('/') + '/projects/' + project
        self.headers = {'PRIVATE-TOKEN': token, 'Accept': 'application/json'}

    def request(self, method, path):
        request = Request(self.base + path, headers=self.headers, method=method)
        try:
            with build_opener(NoRedirect()).open(request, timeout=20) as response:
                if response.status != 200:
                    raise DiscoveryError('unexpected schedule-admission HTTP status')
                body = response.read(4 * 1024 * 1024 + 1)
        except Exception:
            # Never include the credential-bearing request or raw HTTP body.
            raise DiscoveryError('schedule admission API request failed; check token permissions and job log') from None
        if len(body) > 4 * 1024 * 1024:
            raise DiscoveryError('schedule admission response is too large')
        return strict_json(body)

    def bridges(self, pipeline):
        rows = []
        for page in range(1, 11):
            batch = self.request('GET', '/pipelines/{}/bridges?per_page=100&page={}'.format(pipeline, page))
            if not isinstance(batch, list):
                raise DiscoveryError('invalid bridge list')
            rows.extend(batch)
            if len(batch) < 100:
                return rows
        raise DiscoveryError('bridge pagination exceeded its bound')


def batch_bridge(rows):
    matches = []
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get('name'), str):
            raise DiscoveryError('invalid bridge identity')
        if row['name'] == BRIDGE:
            matches.append(row)
    if len(matches) > 1:
        raise DiscoveryError('ambiguous scheduled batch bridge')
    return matches[0] if matches else None


def admit(env, api, report):
    current = env.get('CI_PIPELINE_ID', '')
    if (env.get('CI_PIPELINE_SOURCE') != 'schedule' or env.get('OPENPMIX_PR_AUTODISCOVERY') == '1'
            or not NUMBER_RE.fullmatch(current)):
        raise DiscoveryError('only a scheduled master poll may use this admission check')
    current = int(current)
    own = api.request('GET', '/pipelines/' + str(current))
    if (not isinstance(own, dict) or own.get('id') != current or own.get('source') != 'schedule'
            or own.get('sha') != env.get('CI_COMMIT_SHA')):
        raise DiscoveryError('current scheduled pipeline identity mismatch')
    own_bridge = batch_bridge(api.bridges(current))
    if (own_bridge is None or own_bridge.get('downstream_pipeline') is not None
            or own_bridge.get('status') not in ('created', 'pending', 'waiting_for_resource')):
        raise DiscoveryError('refusing to cancel a pipeline whose batch may already have started')

    candidates = {}
    for status in ACTIVE:
        for page in range(1, 21):
            query = urlencode(dict(source='schedule', status=status, per_page=100, page=page))
            rows = api.request('GET', '/pipelines?' + query)
            if not isinstance(rows, list):
                raise DiscoveryError('invalid scheduled pipeline list')
            for row in rows:
                if (not isinstance(row, dict) or type(row.get('id')) is not int
                        or row.get('source') != 'schedule' or row.get('status') != status):
                    raise DiscoveryError('scheduled pipeline filters were not honored')
                if row['id'] != current:
                    candidates[row['id']] = row
            if len(rows) < 100:
                break
        else:
            raise DiscoveryError('active schedule pagination exceeded its bound')

    busy = None
    for pipeline in sorted(candidates):
        bridge = batch_bridge(api.bridges(pipeline))
        if bridge is None:
            continue  # PR polling and unrelated schedules do not own this batch lock.
        status = bridge.get('status')
        if (status == 'running' or
                (pipeline < current and status in ACTIVE)):
            busy = pipeline
            break

    data = dict(pipeline_id=current, decision='skip-busy' if busy else 'admit',
                active_pipeline_id=busy)
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(json.dumps(data, indent=2) + '\n')
    if busy:
        print('Master batch {} is active. Canceling only idle scheduled poll {}; no tests or checkpoint updates started.'.format(busy, current), flush=True)
        # This is the sole mutation. No caller-controlled target pipeline ID.
        # Recheck immediately before cancellation to avoid canceling a retried live batch.
        bridge = batch_bridge(api.bridges(current))
        if (bridge is None or bridge.get('downstream_pipeline') is not None
                or bridge.get('status') not in ('created', 'pending', 'waiting_for_resource')):
            raise DiscoveryError('scheduled batch started during admission; refusing cancellation')
        result = api.request('POST', '/pipelines/{}/cancel'.format(current))
        if not isinstance(result, dict) or result.get('id') != current or result.get('status') != 'canceled':
            raise DiscoveryError('GitLab did not confirm cancellation of this idle poll')
    else:
        print('No active master batch: admit this scheduled poll.', flush=True)
    return data


def main():
    try:
        admit(os.environ, API(os.environ), Path('ci-scheduled-admission/decision.json'))
        return 0
    except (DiscoveryError, OSError, ValueError) as error:
        print('error: ' + str(error), file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
