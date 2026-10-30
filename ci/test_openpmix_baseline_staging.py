import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

import openpmix_baseline_staging as staging

ROOT = Path(__file__).resolve().parents[1]


class StagingTests(unittest.TestCase):
    def record(self, **changes):
        values = dict(JobId='42', JobState='RUNNING', Account='gen243', NumNodes='4',
                      UserId='test(' + str(os.getuid()) + ')', NodeList='frontier[1-4]')
        values.update(changes)
        return ' '.join(k + '=' + v for k, v in values.items())

    def allocation(self, record, host='frontier1'):
        with patch.object(staging, 'command', side_effect=[record, 'frontier1 frontier2 frontier3 frontier4']), \
             patch('socket.gethostname', return_value=host):
            return staging.allocation({'SLURM_JOB_ID': '42', 'CI_PIPELINE_ID': '123'})

    def test_only_owned_running_four_node_allocation_is_accepted(self):
        self.assertEqual(self.allocation(self.record())['slurm_id'], '42')
        for changes in [dict(JobId='43'), dict(JobState='PENDING'), dict(Account='other'),
                        dict(NumNodes='1'), dict(UserId='other(999999)'), dict(NodeList='bad;command')]:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.allocation(self.record(**changes))
        with self.assertRaises(ValueError):
            self.allocation(self.record(), host='login06')

    def test_source_archive_preserves_git_metadata_and_executable_modes(self):
        previous = Path.cwd()
        with tempfile.TemporaryDirectory() as temp:
            os.chdir(temp)
            try:
                staging.ARCHIVE.parent.mkdir()
                source = Path('input')
                (source / 'openpmix/.git').mkdir(parents=True)
                (source / 'openpmix/.git/HEAD').write_text('fixed source metadata')
                executable = source / 'openpmix/autogen.pl'
                executable.write_text('trusted fixture')
                executable.chmod(0o755)
                (source / 'link').symlink_to('openpmix/autogen.pl')
                with tarfile.open(staging.ARCHIVE, 'w:gz') as archive:
                    archive.add(source, arcname='sources')
                with patch.object(staging, 'verify_pmix') as verify:
                    staging.restore()
                    verify.assert_called_once_with(staging.RESTORED / 'sources/openpmix')
                restored = staging.RESTORED / 'sources'
                self.assertEqual((restored / 'openpmix/.git/HEAD').read_text(), 'fixed source metadata')
                self.assertEqual((restored / 'openpmix/autogen.pl').stat().st_mode & 0o777, 0o755)
                self.assertEqual((restored / 'link').read_text(), 'trusted fixture')
            finally:
                os.chdir(previous)

    def test_staged_git_identity_and_cleanliness_are_rechecked(self):
        with tempfile.TemporaryDirectory() as temp:
            tree = Path(temp)
            def git(*args):
                return subprocess.check_output(['git', '-C', str(tree), *args], text=True).strip()
            git('init', '-q')
            git('config', 'user.email', 'test@example.invalid')
            git('config', 'user.name', 'Test')
            (tree / 'source').write_text('fixture')
            git('add', 'source')
            git('commit', '-qm', 'fixture')
            sha = git('rev-parse', 'HEAD')
            git('checkout', '--detach', '-q', sha)
            git('update-ref', 'refs/remotes/origin/openpmix-pr-4370', sha)
            git('remote', 'add', 'origin', 'https://github.com/openpmix/openpmix.git')
            with patch.object(staging, 'PMIX_SHA', sha), patch.dict(os.environ, {'PMIX_PYTHON': sys.executable}):
                staging.verify_pmix(tree)
                (tree / 'extra').write_text('untracked')
                with self.assertRaises(ValueError):
                    staging.verify_pmix(tree)
                (tree / 'extra').unlink()
                git('remote', 'set-url', 'origin', 'https://example.invalid/other.git')
                with self.assertRaises(ValueError):
                    staging.verify_pmix(tree)

    def test_unsafe_archive_members_are_rejected(self):
        previous = Path.cwd()
        for name, target in [('../escape', None), ('/absolute', None), ('sources/link', '/outside'),
                             ('sources/link', '../../outside')]:
            with self.subTest(name=name, target=target), tempfile.TemporaryDirectory() as temp:
                os.chdir(temp)
                try:
                    staging.ARCHIVE.parent.mkdir()
                    with tarfile.open(staging.ARCHIVE, 'w:gz') as archive:
                        member = tarfile.TarInfo(name)
                        if target:
                            member.type = tarfile.SYMTYPE
                            member.linkname = target
                        archive.addfile(member, None if target else io.BytesIO(b''))
                    with patch.object(staging, 'verify_pmix'), self.assertRaises(ValueError):
                        staging.restore()
                finally:
                    os.chdir(previous)

    def test_isolation_preserves_only_validated_scheduler_identity(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            ci = root / 'ci'
            ci.mkdir()
            shared = (ROOT / 'ci/run_pmix_tests_pr_isolated.sh').read_text()
            fixed_python = '/lustre/orion/gen243/proj-shared/pmix-reframe-ci-tools/pmix-py310/bin/python'
            fixed_rfm = '/lustre/orion/gen243/proj-shared/pmix-reframe-ci-tools/reframe-4.10/bin/reframe'
            shared = shared.replace(fixed_python, sys.executable).replace(fixed_rfm, sys.executable)
            (root / 'olcf.sh').write_text('module() { :; }\n')
            (root / 'cray.sh').write_text('true\n')
            shared = shared.replace('/etc/profile.d/olcf-env.sh', str(root / 'olcf.sh'))
            shared = shared.replace('/etc/bash.bashrc.local', str(root / 'cray.sh'))
            (ci / 'run_pmix_tests_pr_isolated.sh').write_text(shared)
            (ci / 'openpmix_baseline_staging.py').write_text(
                'import json, os, pathlib\n'
                'pathlib.Path("preflight.json").write_text(json.dumps(dict(os.environ)))\n'
                'print("42 frontier[1-4]")\n')
            (ci / 'run_trusted_openpmix_pr.sh').write_text('#!/bin/bash\n/usr/bin/env -0 > received.env\n')
            env = dict(PATH='/usr/bin:/bin', CI_PIPELINE_ID='123', SLURM_JOB_ID='42',
                       SLURM_JOB_NODELIST='untrusted-caller-value', CI_JOB_TOKEN='SECRET',
                       GITHUB_PR_READ_TOKEN='SECRET', SLURM_SPANK_INJECT='SECRET')
            result = subprocess.run(['/bin/bash', 'ci/run_pmix_tests_pr_isolated.sh', 'openpmix-pr'],
                                    cwd=root, env=env, text=True, capture_output=True, timeout=5)
            self.assertEqual(result.returncode, 0, result.stderr)
            received = (root / 'received.env').read_bytes()
            self.assertNotIn(b'SECRET', received)
            self.assertNotIn(b'untrusted-caller-value', received)
            self.assertIn(b'SLURM_JOB_NODELIST=frontier[1-4]\0', received)
            self.assertIn(b'OPENPMIX_RUNNER_ALLOCATION=1\0', received)
            preflight = json.loads((root / 'preflight.json').read_text())
            self.assertNotIn('CI_JOB_TOKEN', preflight)
            self.assertNotIn('SLURM_SPANK_INJECT', preflight)


if __name__ == '__main__':
    unittest.main()
