"""Same hardened test environment, inside one existing four-node allocation."""
from pathlib import Path
import os
import socket
import subprocess

import yaml


job_id = os.environ.get('SLURM_JOB_ID', '')
if not job_id.isdigit() or not os.environ.get('SLURM_JOB_NODELIST'):
    raise RuntimeError('single-allocation configuration requires an active Slurm allocation')
hosts = subprocess.check_output(
    ['scontrol', 'show', 'hostnames', os.environ['SLURM_JOB_NODELIST']], text=True
).split()
if len(hosts) != 4 or socket.gethostname().split('.')[0] != hosts[0].split('.')[0]:
    raise RuntimeError('ReFrame must run on the first of exactly four allocated nodes')

site_configuration = yaml.safe_load(
    (Path(__file__).resolve().parent / 'openpmix_pr_sysconfig.yaml').read_text())
system = site_configuration['systems'][0]
system['max_local_jobs'] = 1
partition = system['partitions'][0]
partition.update(scheduler='local', launcher='local', max_jobs=1)
partition.pop('access')
