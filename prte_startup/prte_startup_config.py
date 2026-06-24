site_configuration = {
    'systems': [
        {
            'name': 'frontier',
            'descr': 'frontier system',
            'modules_system': 'lmod',
            'hostnames': ['login.*'],
            'partitions': [
                {
                    'name': 'login',
                    'scheduler': 'local',
                    'launcher': 'local',
                    'environs': ['baseline']
                },
                {
                    'name': 'compute',
                    'scheduler': 'slurm',
                    'launcher': 'srun',
                    'access': ['-A gen243', '-p batch'],
                    'environs': ['baseline']
                }
            ]
        }
    ],
    'environments': [
        {
            'name': 'baseline',
            'modules': ['PrgEnv-amd']
        }
    ]
}
