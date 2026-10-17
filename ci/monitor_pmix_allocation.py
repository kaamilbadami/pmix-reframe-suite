#!/usr/bin/env python3
"""Read-only allocation diagnostics; never submit, cancel or alter jobs."""
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import signal
import subprocess
import sys
import threading


def submitted_id(path):
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return None
    for line in lines:
        match = re.fullmatch(r'([1-9][0-9]*)(?:;[A-Za-z0-9_.-]+)?', line)
        if match:
            return match.group(1)
    return None


def record(stream, event, **fields):
    stream.write(json.dumps(dict(time=datetime.now(timezone.utc).isoformat(),
                                 event=event, **fields)) + '\n')
    stream.flush()


def capture(stream, argv, run=None):
    run = run or subprocess.run
    try:
        result = run(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                     universal_newlines=True, timeout=10)
        record(stream, 'command', argv=argv, status=result.returncode,
               stdout=result.stdout, stderr=result.stderr)
        return result.stdout if result.returncode == 0 else ''
    except (OSError, subprocess.TimeoutExpired) as error:
        record(stream, 'diagnostic_error', argv=argv, error=str(error))
        return ''


def monitor(directory, stop, interval=60):
    job = None
    previous = None
    with (directory / 'allocation-diagnostics.jsonl').open('a') as stream:
        record(stream, 'monitor_started')
        while not stop.is_set():
            if job is None:
                job = submitted_id(directory / 'submission.out')
                if job:
                    (directory / 'slurm-id.txt').write_text(job + '\n')
                    record(stream, 'allocation_submitted', job_id=job)
            if job:
                status = capture(stream, ['squeue', '--noheader', '--jobs', job,
                                         '--format=%i|%T|%M|%l|%D|%R|%S'])
                # Record full job details on changes in state/reason, not elapsed time.
                fields = status.strip().split('|')
                state_reason = (fields[1], fields[5]) if len(fields) >= 6 else status.strip()
                if state_reason != previous:
                    capture(stream, ['scontrol', 'show', 'job', job])
                    previous = state_reason
                stop.wait(interval)
            else:
                stop.wait(0.2)
        # --wait has returned (or cleanup interrupted it). Capture accounting,
        # including submit/eligible/start/end, even if Slurm no longer lists it.
        job = job or submitted_id(directory / 'submission.out')
        if job:
            capture(stream, ['sacct', '-X', '-j', job, '--parsable2',
                             '--format=JobID,Submit,Eligible,Start,End,Elapsed,State,ExitCode'])
        record(stream, 'monitor_stopped', job_id=job)


def main():
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    monitor(Path(sys.argv[1]), stop)


if __name__ == '__main__':
    main()
