import importlib.util
from pathlib import Path
import subprocess
import tempfile
import unittest
from select_prrte_for_source import select
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('generator', Path(__file__).with_name('generate_pmix_child_pipeline.py'))
generator = importlib.util.module_from_spec(spec)
spec.loader.exec_module(generator)


class SelectionTests(unittest.TestCase):
    def test_exact_source_declarations(self):
        self.assertEqual(select("PMIX_EXPORT char *pmix_getline(FILE *fp);"), generator.PRRTE_LEGACY)
        self.assertEqual(select("PMIX_EXPORT char *pmix_getline(FILE *fp, bool *failed);"), generator.PRRTE_GETLINE)
        for header in ("", "PMIX_EXPORT char *pmix_getline(FILE *fp, int unknown);", "PMIX_EXPORT char *pmix_getline(FILE *fp);" * 2):
            with self.assertRaises(ValueError):
                select(header)

    def test_scaling_launch_artifacts_are_retained(self):
        yaml = generator.render_pipeline(["a" * 40])
        self.assertIn("stage/frontier/batch/pmix_test/PMIxPythonScalingMultinodeTest/", yaml)

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

    def test_triggers_do_not_override_child_prrte_pins(self):
        config = Path(__file__).resolve().parent.parent.joinpath(".gitlab-ci.yml").read_text()
        for name in ("pilot", "scheduled"):
            block = config.split("\ntrigger-pmix-child-pipeline-" + name + ":\n", 1)[1].split("\n\n", 1)[0]
            self.assertNotIn("PRRTE_BRANCH:", block)
            self.assertNotIn("PRRTE_COMMIT:", block)

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
