import ast
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest

from collect_pmix_runtime_evidence import collect, job_ids
from pmix_python_binding.common.reframe_diagnostics import instrument_test
from pmix_python_binding.common.runtime_diagnostics import process_snapshot

ROOT = Path(__file__).resolve().parents[1]


class RuntimeEvidenceTests(unittest.TestCase):
    def test_real_job_script_keeps_failure_and_last_phase(self):
        for commands, final, expected in [(['set -e', 'false'], 'echo NEVER', 1),
                                           (['set -e', 'true'], 'exit 7', 7)]:
            with self.subTest(expected=expected), tempfile.TemporaryDirectory() as directory:
                test = SimpleNamespace(pmix=SimpleNamespace(python_env=sys.executable),
                                       prerun_cmds=commands, keep_files=[])
                instrument_test(test)
                result = subprocess.run(['/bin/bash', '-c', '\n'.join(["trap 'printf \"previous=%s\\n\" \"$?\" > previous-exit.log' EXIT"] + test.prerun_cmds + [final])],
                                        cwd=directory, env=dict(os.environ, CUSTOM_SECRET='MUST_NOT_APPEAR'),
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=5)
                self.assertEqual(result.returncode, expected, result.stderr)
                self.assertEqual((Path(directory) / 'previous-exit.log').read_text().strip(), 'previous=' + str(expected))
                phases = (Path(directory) / 'runtime-phases.log').read_text()
                self.assertIn('job-exit rc=' + str(expected), phases)
                self.assertNotIn('NEVER', result.stdout)
                if expected == 1:
                    self.assertNotIn('controller-start', phases)
                else:
                    self.assertIn('controller-start', phases)
                env = (Path(directory) / 'runtime-environment.json').read_text()
                self.assertNotIn('MUST_NOT_APPEAR', env)
                self.assertNotIn('CUSTOM_SECRET', env)

    def test_all_runtime_classes_instrument_their_existing_prepare_hook(self):
        files = list((ROOT / 'pmix_python_binding/reframe').glob('pmix_python_*test.py'))
        for file in files:
            tree = ast.parse(file.read_text())
            for node in ast.walk(tree):
                if isinstance(node, ast.FunctionDef) and node.name == 'prepare_test':
                    self.assertEqual(ast.unparse(node.body[-1]), 'instrument_test(self)', str(file))

    def test_process_snapshot_is_scoped_and_never_records_arguments(self):
        rows = process_snapshot(os.getpid())
        self.assertTrue(any(int(r[0]) == os.getpid() for r in rows))
        self.assertTrue(all(len(r) == 6 for r in rows))
        self.assertFalse(any('test_pmix_runtime_evidence' in r[-1] for r in rows))

    def test_accounting_finds_partial_job_and_records_reason_without_changing_status(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'runtime-environment.json').write_text(json.dumps(
                dict(environment=dict(SLURM_JOB_ID='456'))))
            (root / 'run-report.json').write_text(json.dumps(dict(runs=[dict(testcases=[
                dict(fixture=False, jobid='123'), dict(fixture=True, jobid='789'),
                dict(fixture=False, jobid='123;echo bad')])])))
            def run(argv, **kwargs):
                self.assertEqual(argv[:3], ['sacct', '-j', '123,456'])
                self.assertLessEqual(kwargs['timeout'], 15)
                return subprocess.CompletedProcess(argv, 0, '456|TIMEOUT|0:15|00:05:00|\n', '')
            result = collect(root, run)
            self.assertEqual(result['job_ids'], ['123', '456'])
            self.assertIn('TIMEOUT', result['records'])
            self.assertEqual(result['status'], 'collected')

    def test_accounting_lag_or_unavailable_command_does_not_invent_result(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'runtime-environment.json').write_text('{"environment":{"SLURM_JOB_ID":"123"}}')
            self.assertEqual(collect(root, lambda *a, **k: subprocess.CompletedProcess([], 0, '', ''))['status'], 'unavailable')
            def missing(*args, **kwargs):
                raise FileNotFoundError('sacct unavailable')
            self.assertEqual(collect(root, missing)['status'], 'unavailable')


if __name__ == '__main__':
    unittest.main()
