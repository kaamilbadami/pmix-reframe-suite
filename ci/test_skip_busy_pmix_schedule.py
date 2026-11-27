"""Non-queued schedule admission keeps the whole-batch resource lock intact."""
from pathlib import Path
import tempfile
import unittest
from urllib.parse import parse_qs, urlsplit

import yaml
from discover_eligible_openpmix_prs import DiscoveryError
import skip_busy_pmix_schedule as admission

ROOT = Path(__file__).resolve().parents[1]


class FakeAPI:
    def __init__(self, candidates=(), own_started=False, fail=False):
        self.candidates = {row['id']: row for row in candidates}
        self.calls = []
        self.own_started, self.fail = own_started, fail

    def request(self, method, path):
        self.calls.append((method, path))
        if self.fail:
            raise DiscoveryError('API denied access')
        if method == 'POST':
            return dict(id=50, status='canceled')
        if path == '/pipelines/50':
            return dict(id=50, source='schedule', sha='a' * 40)
        status = parse_qs(urlsplit(path).query)['status'][0]
        return [dict(id=row['id'], source='schedule', status=row['status'])
                for row in self.candidates.values() if row['status'] == status]

    def bridges(self, pipeline):
        if pipeline == 50:
            return [dict(name=admission.BRIDGE,
                         status='running' if self.own_started else 'created',
                         downstream_pipeline=dict(id=100) if self.own_started else None)]
        row = self.candidates[pipeline]
        return [dict(name=row.get('bridge_name', admission.BRIDGE), status=row['bridge_status'])]


class ScheduleAdmissionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.report = Path(self.tmp.name) / 'decision.json'
        self.env = dict(CI_PIPELINE_ID='50', CI_PIPELINE_SOURCE='schedule',
                        CI_COMMIT_SHA='a' * 40)

    def test_idle_schedule_continues_without_any_mutation(self):
        api = FakeAPI()
        self.assertEqual(admission.admit(self.env, api, self.report)['decision'], 'admit')
        self.assertTrue(all(method == 'GET' for method, _ in api.calls))

    def test_running_batch_only_cancels_current_idle_poll(self):
        for owner in (40, 60):
            api = FakeAPI([dict(id=owner, status='running', bridge_status='running')])
            result = admission.admit(self.env, api, self.report)
            self.assertEqual(result['active_pipeline_id'], owner)
            self.assertEqual([(m, p) for m, p in api.calls if m != 'GET'],
                             [('POST', '/pipelines/50/cancel')])

    def test_overlapping_polls_favor_older_starting_pipeline(self):
        api = FakeAPI([dict(id=40, status='running', bridge_status='created')])
        self.assertEqual(admission.admit(self.env, api, self.report)['decision'], 'skip-busy')
        api = FakeAPI([dict(id=60, status='running', bridge_status='created')])
        self.assertEqual(admission.admit(self.env, api, self.report)['decision'], 'admit')

    def test_pr_schedules_and_finished_bridges_do_not_block_master(self):
        api = FakeAPI([dict(id=40, status='running', bridge_status='running',
                            bridge_name='trigger-openpmix-pr-pipelines'),
                       dict(id=41, status='running', bridge_status='success')])
        self.assertEqual(admission.admit(self.env, api, self.report)['decision'], 'admit')

    def test_api_failure_does_not_admit_or_cancel_anything(self):
        api = FakeAPI(fail=True)
        with self.assertRaises(DiscoveryError):
            admission.admit(self.env, api, self.report)
        self.assertFalse(any(method == 'POST' for method, _ in api.calls))

    def test_refuses_cancellation_if_this_batch_has_started(self):
        api = FakeAPI(own_started=True)
        with self.assertRaisesRegex(DiscoveryError, 'already have started'):
            admission.admit(self.env, api, self.report)
        self.assertFalse(any(method == 'POST' for method, _ in api.calls))

    def test_rechecks_own_batch_before_cancellation(self):
        api = FakeAPI([dict(id=40, status='running', bridge_status='running')])
        original = api.bridges
        count = [0]
        def bridges(pipeline):
            if pipeline == 50:
                count[0] += 1
                if count[0] > 1:
                    api.own_started = True
            return original(pipeline)
        api.bridges = bridges
        with self.assertRaisesRegex(DiscoveryError, 'started during admission'):
            admission.admit(self.env, api, self.report)
        self.assertFalse(any(method == 'POST' for method, _ in api.calls))

    def test_rejects_manual_and_pr_polling_callers(self):
        for env in (dict(self.env, CI_PIPELINE_SOURCE='web'),
                    dict(self.env, OPENPMIX_PR_AUTODISCOVERY='1')):
            with self.assertRaises(DiscoveryError):
                admission.admit(env, FakeAPI(), self.report)

    def test_existing_batch_lock_and_preflight_dependency(self):
        config = yaml.safe_load((ROOT / '.gitlab-ci.yml').read_text())
        check = config['check-pmix-scheduled-admission']
        batch = config['run-pmix-scheduled-batch']
        self.assertEqual(batch['resource_group'], 'pmix-scheduled-batch')
        self.assertEqual(batch['trigger']['strategy'], 'mirror')
        self.assertEqual(batch['needs'], [dict(job='check-pmix-scheduled-admission', optional=True)])
        self.assertIn('$GITLAB_PIPELINE_CONTROL_TOKEN', check['rules'][1]['if'])
        self.assertEqual(check['rules'][0],
                         {'if': '$OPENPMIX_PR_AUTODISCOVERY == "1"', 'when': 'never'})
        self.assertLess(config['stages'].index(check['stage']),
                        config['stages'].index(batch['stage']))
        self.assertFalse(batch['trigger']['forward']['pipeline_variables'])


if __name__ == '__main__':
    unittest.main()
