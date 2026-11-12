"""Execute actual runtime setup with independent /tmp trees on simulated nodes."""
import json
import fnmatch
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]


class RuntimeNodeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for host in ('node-a', 'node-b'):
            (self.root / host).mkdir(mode=0o1777)
            (self.root / host).chmod(0o1777)
        self.bin = self.root / 'bin'
        self.bin.mkdir()
        (self.root / 'site.sh').write_text('module() { :; }\n')
        self.config = yaml.safe_load((ROOT / 'ci/openpmix_pr_sysconfig.yaml').read_text())
        self.body = self.config['systems'][0]['partitions'][0]['prepare_cmds'][0]
        self.body = self.body.replace('/etc/profile.d/olcf-env.sh', str(self.root / 'site.sh'))
        self.body = self.body.replace('/etc/bash.bashrc.local', str(self.root / 'site.sh'))
        self.fake('scontrol', '#!/bin/bash\nprintf "node-a\\nnode-b\\n"\n')
        self.fake('srun', '#!' + sys.executable + '\n' + '''import json, os, pathlib, subprocess, sys
root = pathlib.Path(os.environ['MOCK_NODES'])
args = sys.argv[1:]
assert '--jobid=123' in args and '--nodes=2' in args and '--ntasks=2' in args
assert '--ntasks-per-node=1' in args and '--export=NONE' in args
assert '--overlap' in args and '--mpi=none' in args
command = args[args.index('/usr/bin/env'):]
with (root / 'steps.jsonl').open('a') as f: f.write(json.dumps(command) + '\\n')
rc = 0
for host in ('node-a', 'node-b'):
    cmd = command[:]
    script = cmd.index('-c') + 1
    # Replace only the node-local root, so the actual checks, mkdir, marker
    # ownership, rollback, and workload gating execute unchanged.
    cmd[script] = cmd[script].replace('node_tmp_root=/tmp', 'node_tmp_root=' + str(root / host))
    cmd[script] = '[[ ! ${GITHUB_STATUS_TOKEN+x} && ! ${CI_JOB_TOKEN+x} ]] || exit 99\\n' + cmd[script]
    result = subprocess.run(cmd)
    rc = max(rc, result.returncode)
sys.exit(rc)
''')

    def fake(self, name, text):
        path = self.bin / name
        path.write_text(text)
        path.chmod(0o755)

    def directory(self, host):
        return self.root / host / 'frontier-openpmix-pr-123'

    def launch(self, workload='exit 0\n', job_id='123'):
        return subprocess.run(['/bin/bash', '--noprofile', '--norc', '-c', self.body + workload],
                              env=dict(PATH=str(self.bin) + ':/usr/bin:/bin', LANG='C',
                                       SLURM_JOB_ID=job_id, SLURM_JOB_NODELIST='node-[a-b]',
                                       MOCK_NODES=str(self.root), CI_JOB_TOKEN='SECRET',
                                       GITHUB_STATUS_TOKEN='SECRET'),
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              universal_newlines=True, timeout=10)

    def test_configuration_renders_through_reframe_jinja_stage(self):
        # ReFrame processes YAML through Jinja before parsing it as YAML.
        # Use the test interpreter, or the system interpreter's installed Jinja.
        interpreters = [sys.executable, '/usr/bin/python3']
        source = ROOT / 'ci/openpmix_pr_sysconfig.yaml'
        program = (
            'import pathlib,sys,jinja2,yaml,subprocess; '
            'text=pathlib.Path(sys.argv[1]).read_text(); '
            'rendered=jinja2.Environment().from_string(text).render(); '
            'config=yaml.safe_load(rendered); '
            'script=config["systems"][0]["partitions"][0]["prepare_cmds"][0]; '
            'subprocess.run(["/bin/bash","-n"],input=script.encode(),check=True)'
        )
        for executable in interpreters:
            probe = subprocess.run([executable, '-c', 'import jinja2,yaml'],
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            if probe.returncode == 0:
                result = subprocess.run([executable, '-c', program, str(source)],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                break
        else:
            self.fail('Jinja2 and PyYAML are required to validate the ReFrame configuration')

    def test_all_nodes_ready_before_workload_and_all_cleaned_on_success(self):
        script = '\n'.join('[[ -d "{}" ]] || exit 55'.format(self.directory(h))
                           for h in ('node-a', 'node-b')) + '\necho WORKLOAD_RAN\n'
        result = self.launch(script)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('WORKLOAD_RAN', result.stdout)
        self.assertEqual(result.stdout.count('OPENPMIX_PR_RUNTIME_READY'), 2)
        self.assertEqual(result.stdout.count('OPENPMIX_PR_RUNTIME_REMOVED'), 2)
        for host in ('node-a', 'node-b'):
            self.assertFalse(self.directory(host).exists())
        steps = [json.loads(line) for line in (self.root / 'steps.jsonl').read_text().splitlines()]
        self.assertEqual([step[-3] for step in steps], ['create', 'cleanup'])
        self.assertNotIn('SECRET', json.dumps(steps))

    def test_workload_failure_preserves_status_and_cleans_both_nodes(self):
        result = self.launch('exit 7\n')
        self.assertEqual(result.returncode, 7, result.stderr)
        self.assertTrue(all(not self.directory(h).exists() for h in ('node-a', 'node-b')))

    def test_partial_setup_failure_rolls_back_without_deleting_preexisting_directory(self):
        other = self.directory('node-b')
        other.mkdir(mode=0o700)
        (other / '.ci-runtime-owner').write_text('another-invocation\n')
        result = self.launch('echo SHOULD_NOT_RUN\n')
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn('SHOULD_NOT_RUN', result.stdout)
        self.assertFalse(self.directory('node-a').exists())
        self.assertEqual((other / '.ci-runtime-owner').read_text(), 'another-invocation\n')

    def test_symlink_collision_is_rejected_and_target_is_untouched(self):
        target = self.root / 'unrelated'
        target.mkdir()
        (target / 'keep').write_text('keep')
        self.directory('node-b').symlink_to(target, target_is_directory=True)
        result = self.launch('echo SHOULD_NOT_RUN\n')
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn('SHOULD_NOT_RUN', result.stdout)
        self.assertTrue(self.directory('node-b').is_symlink())
        self.assertEqual((target / 'keep').read_text(), 'keep')
        self.assertFalse(self.directory('node-a').exists())

    def test_invalid_job_id_cannot_launch_steps(self):
        result = self.launch(job_id='123;evil')
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.root / 'steps.jsonl').exists())

    def test_pr_and_baseline_artifacts_include_complete_startup_logs(self):
        jobs = yaml.safe_load((ROOT / 'ci/openpmix_pr_jobs.yml').read_text())
        for job in ('run-openpmix-pr', 'validate-openpmix-baseline'):
            patterns = jobs[job]['artifacts']['paths']
            for file in ('controller.out.prte.stdout', 'controller.out.prte.stderr',
                         'controller.out.dvm_output', 'prte-2node.log'):
                path = 'ci-openpmix-pr-execution/reframe/stage/frontier/batch/pmix_test/Example/' + file
                self.assertTrue(any(fnmatch.fnmatch(path, pattern) for pattern in patterns), (job, file))

    def test_no_allocation_does_not_run_srun(self):
        result = self.launch('echo LOCAL_BUILD\n', job_id='')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.root / 'steps.jsonl').exists())


if __name__ == '__main__':
    unittest.main()
