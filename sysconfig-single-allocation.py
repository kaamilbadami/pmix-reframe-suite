"""Opt-in configuration: run only on the first node of a four-node allocation."""
import os
import socket
import subprocess

job_id = os.environ.get('SLURM_JOB_ID', '')
if not job_id.isdigit() or not os.environ.get('SLURM_JOB_NODELIST'):
    raise RuntimeError('single-allocation config requires an active Slurm allocation')
hosts = subprocess.check_output(
    ['scontrol', 'show', 'hostnames', os.environ['SLURM_JOB_NODELIST']],
    universal_newlines=True
).split()
if len(hosts) != 4 or socket.gethostname().split('.')[0] != hosts[0].split('.')[0]:
    raise RuntimeError('run ReFrame on the first compute node of exactly four allocated nodes')

site_configuration = {
    'systems': [{
        'name': 'frontier', 'hostnames': ['frontier[0-9]+'],
        'modules_system': 'lmod',
        'max_local_jobs': 1,
        'partitions': [{
            'name': 'batch', 'scheduler': 'local', 'launcher': 'local',
            'max_jobs': 1,
            'prepare_cmds': ['. /etc/profile.d/olcf-env.sh && . /etc/bash.bashrc.local'],
            'environs': ['pmix_test']
        }]
    }],
    'environments': [{'name': 'pmix_test'}]
}
