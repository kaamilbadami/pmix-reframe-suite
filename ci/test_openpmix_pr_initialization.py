"""Execute the generated-job initialization with simulated site scripts."""

from pathlib import Path
import subprocess
import tempfile
import unittest

import yaml


class InitializationTests(unittest.TestCase):
    def run_initialization(self, olcf, cray, after=""):
        config = Path(__file__).resolve().parent / "openpmix_pr_sysconfig.yaml"
        commands = yaml.safe_load(config.read_text())["systems"][0]["partitions"][0]["prepare_cmds"][0]
        # Execute the real configuration's initialization without its later
        # node-local runtime setup. Only site source paths are substituted.
        prefix = commands.split('if [[ -n ${SLURM_JOB_ID:-} ]]; then', 1)[0]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "olcf.sh").write_text(olcf)
            (root / "cray.sh").write_text(cray)
            prefix = prefix.replace("/etc/profile.d/olcf-env.sh", str(root / "olcf.sh"))
            prefix = prefix.replace("/etc/bash.bashrc.local", str(root / "cray.sh"))
            return subprocess.run(["/bin/bash", "--noprofile", "--norc", "-euc", prefix + after],
                                  env={"PATH": "/usr/bin:/bin", "LANG": "C"},
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    def test_optional_site_variables_and_status_probes_are_tolerated(self):
        result = self.run_initialization(
            'printf "%s" "$lmod_prefix_path"\nfalse\nmodule() { :; }\ntrue\n',
            'printf "%s" "$optional_cray_setting"\ntrue\n',
            '[[ $- == *e* && $- == *u* ]]\n[[ $(set -o | awk \'$1 == "pipefail" {print $2}\') == on ]]\nprintf "INITIALIZED\\n"\n')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("INITIALIZED", result.stdout)

    def test_each_site_script_failure_stops_before_workload(self):
        for olcf, cray in [('module() { :; }\nreturn 7\n', 'true\n'),
                           ('module() { :; }\n', 'return 8\n')]:
            with self.subTest(olcf=olcf, cray=cray):
                result = self.run_initialization(olcf, cray, 'echo SHOULD_NOT_RUN\n')
                self.assertEqual(result.returncode, 2)
                self.assertIn("initialization failed", result.stderr)
                self.assertNotIn("SHOULD_NOT_RUN", result.stdout)

    def test_missing_module_command_stops_before_workload(self):
        result = self.run_initialization('true\n', 'true\n', 'echo SHOULD_NOT_RUN\n')
        self.assertEqual(result.returncode, 2)
        self.assertIn("module command is unavailable", result.stderr)
        self.assertNotIn("SHOULD_NOT_RUN", result.stdout)

    def test_nounset_is_restored_for_workload(self):
        result = self.run_initialization('module() { :; }\n', 'true\n',
                                         'echo "$missing_workload_variable"\n')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unbound variable", result.stderr)

    def test_errexit_is_restored_for_workload(self):
        result = self.run_initialization('module() { :; }\n', 'true\n',
                                         'false\necho SHOULD_NOT_RUN\n')
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("SHOULD_NOT_RUN", result.stdout)


if __name__ == "__main__":
    unittest.main()
