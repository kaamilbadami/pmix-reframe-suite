"""Read-only telemetry tests; no Slurm connection or compute requests."""
import io
import json
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch

import monitor_pmix_allocation as monitor


class MonitorTests(unittest.TestCase):
    def test_id_only_accepts_parsable_submission(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'submission.out'
            self.assertIsNone(monitor.submitted_id(path))
            for text, expected in [('123;frontier\n', '123'), ('123\n', '123'),
                                   ('error 123\n', None), ('0\n', None),
                                   ('123;$(false)\n', None)]:
                path.write_text(text)
                self.assertEqual(monitor.submitted_id(path), expected)

    def test_pending_reason_and_final_accounting_are_retained(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            (directory / 'submission.out').write_text('123\n')
            stop = threading.Event()
            commands = []
            def run(argv, **kwargs):
                commands.append(argv)
                if argv[0] == 'squeue':
                    stop.set()
                    output = '123|PENDING|0:00|45:00|4|Priority|N/A\n'
                elif argv[0] == 'scontrol':
                    output = 'JobId=123 Reason=Priority\n'
                else:
                    output = 'JobID|Submit|Eligible|Start|End|Elapsed|State|ExitCode\n'
                return subprocess.CompletedProcess(argv, 0, output, '')
            with patch('subprocess.run', side_effect=run):
                monitor.monitor(directory, stop, interval=0)
            self.assertEqual((directory / 'slurm-id.txt').read_text(), '123\n')
            rows = [json.loads(line) for line in (directory / 'allocation-diagnostics.jsonl').read_text().splitlines()]
            self.assertTrue(any('Priority' in row.get('stdout', '') for row in rows))
            self.assertEqual([argv[0] for argv in commands], ['squeue', 'scontrol', 'sacct'])
            self.assertTrue(all('123' in argv for argv in commands))
            self.assertEqual(rows[-1]['event'], 'monitor_stopped')

    def test_unsubmitted_job_never_queries_slurm(self):
        with tempfile.TemporaryDirectory() as temp:
            stop = threading.Event()
            stop.set()
            with patch('subprocess.run') as run:
                monitor.monitor(Path(temp), stop)
                run.assert_not_called()

    def test_unavailable_accounting_is_diagnostic_only(self):
        stream = io.StringIO()
        def unavailable(*args, **kwargs):
            raise subprocess.TimeoutExpired(args[0], 10)
        self.assertEqual(monitor.capture(stream, ['sacct', '-j', '123'], run=unavailable), '')
        self.assertEqual(json.loads(stream.getvalue())['event'], 'diagnostic_error')


if __name__ == '__main__':
    unittest.main()
