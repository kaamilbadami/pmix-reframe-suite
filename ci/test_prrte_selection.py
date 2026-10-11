import importlib.util
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('generator', Path(__file__).with_name('generate_pmix_child_pipeline.py'))
generator = importlib.util.module_from_spec(spec)
spec.loader.exec_module(generator)


class SelectionTests(unittest.TestCase):
    def test_ancestry_and_rendering(self):
        with tempfile.TemporaryDirectory() as directory:
            def git(*args):
                return subprocess.check_output(['git', '-C', directory, *args], universal_newlines=True).strip()
            git('init', '-q')
            git('config', 'user.name', 'Test')
            git('config', 'user.email', 'test@example.com')
            git('commit', '--allow-empty', '-qm', 'before')
            before = git('rev-parse', 'HEAD')
            git('checkout', '-qb', 'side')
            git('commit', '--allow-empty', '-qm', 'side branch')
            side = git('rev-parse', 'HEAD')
            git('checkout', '--detach', before)
            git('commit', '--allow-empty', '-qm', 'boundary')
            boundary = git('rev-parse', 'HEAD')
            git('commit', '--allow-empty', '-qm', 'after')
            after = git('rev-parse', 'HEAD')
            with patch.object(generator, 'PMIX_GETLINE_CHANGE', boundary):
                selection = generator.select_prrte([before, boundary, after, side], directory)
                self.assertEqual(selection[before], generator.PRRTE_LEGACY)
                self.assertEqual(selection[side], generator.PRRTE_LEGACY)
                self.assertEqual(selection[boundary], generator.PRRTE_GETLINE)
                self.assertEqual(selection[after], generator.PRRTE_GETLINE)
                yaml = generator.render_pipeline([before, boundary, after], prrte_selections=selection)
                self.assertEqual(yaml.count('PRRTE_BRANCH: "master"'), 2)
                self.assertEqual(yaml.count('PRRTE_BRANCH: "v5.0"'), 1)
                with self.assertRaises(SystemExit):
                    generator.select_prrte(['f' * 40], directory)

    def test_subprocess_calls_use_python36_keywords(self):
        original = subprocess.run
        def compatible_run(*args, **kwargs):
            self.assertNotIn("text", kwargs)
            self.assertNotIn("capture_output", kwargs)
            return original(*args, **kwargs)
        with tempfile.TemporaryDirectory() as directory:
            original(["git", "init", "--bare", "-q", directory], check=True)
            with patch.object(generator.subprocess, "run", side_effect=compatible_run):
                self.assertEqual(generator.select_prrte([], directory), {})

    def test_shallow_history_is_rejected(self):
        with patch.object(generator.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, 'true\n', '')):
            with self.assertRaises(SystemExit):
                generator.select_prrte(['a' * 40], '.')


if __name__ == '__main__':
    unittest.main()
