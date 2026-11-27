"""Bounded, allowlisted runtime evidence. Never record argv or the full environment."""
import argparse
from datetime import datetime, timezone
import json
import os
import subprocess
import time

ENV_KEYS = ('PATH', 'LD_LIBRARY_PATH', 'PYTHONPATH', 'BASH_ENV', 'TMPDIR',
            'CONDA_PREFIX', 'CONDA_DEFAULT_ENV', 'CONDA_SHLVL', 'LMOD_CMD',
            'MODULEPATH', 'LOADEDMODULES', 'SLURM_JOB_ID', 'SLURM_JOB_NODELIST',
            'SLURM_NNODES', 'SLURM_CPUS_PER_TASK')


def timestamp():
    return datetime.now(timezone.utc).isoformat()


def process_snapshot(pid):
    # One host only. Select descendants; never include process arguments or env.
    result = subprocess.run(['ps', '-eo', 'pid=,ppid=,stat=,etimes=,wchan:32=,comm='],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            universal_newlines=True, timeout=3)
    rows = [line.split(None, 5) for line in result.stdout.splitlines()]
    selected = {pid}
    for _ in range(len(rows)):
        children = {int(r[0]) for r in rows if len(r) == 6 and int(r[1]) in selected}
        new = children - selected
        if not new:
            break
        selected.update(new)
    return [r for r in rows if len(r) == 6 and int(r[0]) in selected]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['environment', 'watch'])
    parser.add_argument('--pid', type=int)
    args = parser.parse_args()
    if args.action == 'environment':
        print(json.dumps(dict(timestamp=timestamp(), hostname=os.uname().nodename,
                              environment={k: os.environ.get(k) for k in ENV_KEYS}), indent=2))
        return
    if not args.pid or args.pid < 1:
        parser.error('watch requires a positive PID')
    # At most 30 minutes of local snapshots, one every 30 seconds. The job's
    # EXIT trap normally stops this child immediately. No extra Slurm steps.
    for _ in range(60):
        try:
            os.kill(args.pid, 0)
            print(json.dumps(dict(timestamp=timestamp(), root_pid=args.pid,
                                  columns=['pid', 'ppid', 'state', 'elapsed_seconds', 'wait_channel', 'name'],
                                  processes=process_snapshot(args.pid))), flush=True)
        except (OSError, subprocess.TimeoutExpired) as error:
            print(json.dumps(dict(timestamp=timestamp(), error=str(error))), flush=True)
            return
        time.sleep(30)


if __name__ == '__main__':
    main()
