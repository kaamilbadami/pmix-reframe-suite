"""Exercise offline source identity, integrity and preparation without network."""
import importlib.util
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('sources', Path(__file__).with_name('pmix_pilot_sources.py'))
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


class SourcesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'sources'
        self.root.mkdir()
        self.destination = Path(self.temp.name) / 'dest'
        self.destination.mkdir()
        self.env = {'PMIX_COMMIT': 'a'*40, 'PRRTE_COMMIT': 'b'*40, 'PRRTE_BRANCH': 'master'}
        self.manifest = dict(m.pins(self.env), libevent_sha256=hashlib.sha256(b'archive').hexdigest(),
                             prrte_branch_commit='c'*40)
        self.save()
        (self.root / m.TARBALL).write_bytes(b'archive')

    def save(self):
        (self.root / 'manifest.json').write_text(json.dumps(self.manifest))

    def test_local_archive_is_copied_without_a_network_command(self):
        with patch.object(m.subprocess, 'Popen', side_effect=AssertionError('network forbidden')):
            m.consume('libevent', self.root, self.destination, self.env)
        self.assertEqual((self.destination / m.TARBALL).read_bytes(), b'archive')

    def test_changed_archive_fails(self):
        (self.root / m.TARBALL).write_bytes(b'changed')
        with self.assertRaises(ValueError):
            m.consume('libevent', self.root, self.destination, self.env)

    def test_pins_must_match_before_consumption(self):
        self.manifest['pmix'] = 'd'*40
        self.save()
        with self.assertRaises(ValueError):
            m.consume('libevent', self.root, self.destination, self.env)

    def test_local_git_copy_checks_exact_clean_commit_and_preserves_metadata(self):
        for kind, key in [('pmix', 'PMIX_COMMIT'), ('prrte', 'PRRTE_COMMIT')]:
            tree = self.root / (kind + '-git')
            subprocess.run(['git','init','-q',str(tree)], check=True)
            (tree / 'source.c').write_text('int main(void) {return 0;}\n')
            subprocess.run(['git','-C',str(tree),'add','.'],check=True)
            subprocess.run(['git','-C',str(tree),'-c','user.name=Test','-c','user.email=test@example.com',
                            'commit','-qm','source'],check=True)
            sha = m.git(tree, 'rev-parse', 'HEAD')
            subprocess.run(['git','-C',str(tree),'checkout','--detach','-q',sha],check=True)
            subprocess.run(['git','-C',str(tree),'remote','add','origin',m.URLS[kind]],check=True)
            self.env[key] = sha
            self.manifest[kind] = sha
        self.save()
        with patch.object(m.subprocess, 'Popen', wraps=subprocess.Popen) as run:
            for kind in ('pmix', 'prrte'):
                m.consume(kind, self.root, self.destination, self.env)
            commands = [c.args[0] for c in run.call_args_list]
            self.assertTrue(all(c[0] == 'git' and c[3] in ('rev-parse','status','remote') for c in commands))
        self.assertIn(self.env['PMIX_COMMIT'], (self.destination/'pmix-commit.env').read_text())
        self.assertIn(self.env['PRRTE_COMMIT'], (self.destination/'prrte-source.env').read_text())
        (self.root/'pmix-git/source.c').write_text('dirty')
        with self.assertRaises(ValueError):
            m.checked_tree(self.root/'pmix-git', 'pmix', self.env['PMIX_COMMIT'])
        with self.assertRaises(ValueError):
            m.checked_tree(self.root/'prrte-git', 'prrte', 'e'*40)

    def test_prepare_failure_never_publishes_manifest(self):
        empty = Path(self.temp.name) / 'new'
        with patch.object(m, 'bounded', side_effect=RuntimeError('network unavailable')):
            with self.assertRaises(RuntimeError):
                m.prepare(empty, self.env)
        self.assertFalse((empty/'manifest.json').exists())

    def test_network_commands_have_deadlines_and_kill_timed_out_process_groups(self):
        log = self.root/'prepare.log'
        import unittest.mock as mock
        process = mock.Mock(pid=123, returncode=None)
        process.wait.side_effect = [subprocess.TimeoutExpired('git', 1), -9]
        with patch.object(m.subprocess, 'Popen', return_value=process) as launch, \
             patch.object(m.os, 'killpg') as kill:
            with self.assertRaises(RuntimeError):
                m.bounded(['git','clone','fixed-url'], log, seconds=1)
        self.assertTrue(launch.call_args.kwargs['start_new_session'])
        self.assertEqual(process.wait.call_args_list[0].kwargs['timeout'], 1)
        kill.assert_called_once_with(123, m.signal.SIGKILL)

    @unittest.skipUnless(importlib.util.find_spec('reframe'), 'requires ReFrame')
    def test_fetch_hooks_use_only_local_helper_when_pilot_enabled(self):
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        from libevent_build_class import fetch_libevent
        from pmix_build_class import fetch_pmix
        from prrte_build_class import fetch_prrte
        with patch.dict(os.environ, dict(self.env, PMIX_PILOT_OFFLINE_SOURCES=str(self.root),
                                         PMIX_PYTHON=sys.executable)):
            for cls, kind in [(fetch_libevent,'libevent'), (fetch_pmix,'pmix'), (fetch_prrte,'prrte')]:
                obj = SimpleNamespace()
                cls.prepare_download(obj)
                self.assertEqual(obj.executable, sys.executable)
                self.assertEqual(obj.executable_opts[1:3], ['use', kind])
                self.assertFalse(any('https://' in arg for arg in obj.executable_opts))
        # Production fetching still follows the original release/Git paths.
        with patch.dict(os.environ, {'PMIX_PILOT_OFFLINE_SOURCES': ''}):
            obj = SimpleNamespace(version='2.1.12')
            fetch_libevent.prepare_download(obj)
            self.assertIn('https://github.com/', obj.executable_opts[0])
            obj = SimpleNamespace(branch='master', commit='a'*40, trusted_source='')
            fetch_pmix.prepare_download(obj)
            self.assertIn('git --no-pager clone', obj.executable_opts[1])
            obj = SimpleNamespace(commit='', version='4.1.0')
            fetch_prrte.prepare_download(obj)
            self.assertIn('https://github.com/', obj.executable_opts[0])


if __name__ == '__main__':
    unittest.main()
