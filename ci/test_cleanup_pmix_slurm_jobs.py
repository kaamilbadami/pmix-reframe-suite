import importlib.util
from pathlib import Path
import subprocess
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('cleanup', Path(__file__).with_name('cleanup_pmix_slurm_jobs.py'))
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class CleanupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name)
        self.root = self.project / '.ci-work' / '239018'
        self.env = dict(CI_PROJECT_DIR=str(self.project), CI_JOB_ID='239018', CI_JOB_STATUS='failed')
        self.commands = []

    def record(self, number, root=None, uid=20841, name='PMIxTest'):
        cwd = str((root or self.root) / 'reframe' / 'stage' / 'frontier' / name)
        return 'JobId={} UserId=gen243_auser({}) JobState=PENDING WorkDir={} Command={}/rfm_job.sh StdErr={}/rfm_job.err'.format(number, uid, cwd, cwd, cwd)

    def run_fake(self, records, cancel_error=False):
        def run(argv, **kwargs):
            self.commands.append(argv)
            if argv[0] == 'squeue':
                return subprocess.CompletedProcess(argv, 0, '\n'.join(records), '')
            if argv[0] == 'scontrol':
                return subprocess.CompletedProcess(argv, 0, records[argv[3]], '')
            return subprocess.CompletedProcess(argv, int(cancel_error), '', 'denied' if cancel_error else '')
        return module.cleanup(self.env, run=run, uid=20841)

    def test_only_exact_ci_job_is_canceled(self):
        records = {'10': self.record('10'),
                   '11': self.record('11', self.project / '.ci-work' / '239019'),
                   '12': self.record('12', self.project / '.ci-work' / '2390189'),
                   '13': self.record('13', uid=1000),
                   '14': self.record('14', self.project / 'unrelated')}
        report = self.run_fake(records)
        self.assertEqual(report['canceled'], ['10'])
        self.assertEqual([c for c in self.commands if c[0] == 'scancel'], [['scancel', '10']])

    def test_success_does_not_query_or_cancel(self):
        self.env['CI_JOB_STATUS'] = 'success'
        report = self.run_fake({'10': self.record('10')})
        self.assertEqual(self.commands, [])
        self.assertEqual(report['canceled'], [])

    def test_canceled_ci_job_cleans_up(self):
        self.env['CI_JOB_STATUS'] = 'canceled'
        self.assertEqual(self.run_fake({'10': self.record('10')})['canceled'], ['10'])

    def test_invalid_identity_fails_before_any_command(self):
        for value in ('', '0', '../239018', '239018;scancel'):
            self.env['CI_JOB_ID'] = value
            with self.assertRaises(ValueError): self.run_fake({})
        self.assertEqual(self.commands, [])

    def test_unexpected_status_fails_closed(self):
        self.env['CI_JOB_STATUS'] = 'running'
        with self.assertRaises(ValueError): self.run_fake({})
        self.assertEqual(self.commands, [])

    def test_path_escape_is_not_canceled(self):
        bad = self.record('10').replace('/stage/frontier/PMIxTest', '/stage/../../../../other')
        self.assertEqual(self.run_fake({'10': bad})['canceled'], [])

    def test_record_identity_and_command_must_match(self):
        for record in (self.record('11'), self.record('10').replace('Command=', 'WrongField='),
                       self.record('10').replace('rfm_job.sh', 'other.sh')):
            with self.subTest(record=record):
                self.assertEqual(self.run_fake({'10': record})['canceled'], [])

    def test_alias_paths_match_same_real_workspace(self):
        alias = self.project / 'alias'
        alias.symlink_to(self.project, target_is_directory=True)
        record = self.record('10', alias / '.ci-work' / '239018')
        self.assertEqual(self.run_fake({'10': record})['canceled'], ['10'])

    def test_cancel_error_is_reported(self):
        result = self.run_fake({'10': self.record('10')}, cancel_error=True)
        self.assertEqual(result['canceled'], [])
        self.assertEqual(result['errors'][0]['job_id'], '10')

    def test_list_failure_cancels_nothing(self):
        def failed(argv, **kwargs):
            self.commands.append(argv)
            return subprocess.CompletedProcess(argv, 1, '', 'unavailable')
        with self.assertRaises(RuntimeError): module.cleanup(self.env, run=failed, uid=20841)
        self.assertEqual(len(self.commands), 1)

    def test_disappeared_job_is_ignored(self):
        def run(argv, **kwargs):
            self.commands.append(argv)
            return subprocess.CompletedProcess(argv, 0 if argv[0] == 'squeue' else 1,
                                               '10' if argv[0] == 'squeue' else '', '')
        self.assertEqual(module.cleanup(self.env, run=run, uid=20841)['canceled'], [])

    def test_command_timeout_is_reported(self):
        def run(argv, **kwargs):
            self.commands.append(argv)
            if argv[0] == 'squeue': return subprocess.CompletedProcess(argv, 0, '10', '')
            raise subprocess.TimeoutExpired(argv, 20)
        result = module.cleanup(self.env, run=run, uid=20841)
        self.assertEqual(len(result['errors']), 1)
        self.assertFalse(any(c[0] == 'scancel' for c in self.commands))

if __name__ == '__main__': unittest.main()
