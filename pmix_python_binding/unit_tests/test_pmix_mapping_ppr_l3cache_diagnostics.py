import ast
import os
from pathlib import Path
import shlex
import subprocess
import tempfile
import unittest


WORKLOAD = (
    Path(__file__).resolve().parents[1]
    / "workloads"
    / "spawn_mapping_ppr_l3cache_test.py"
)


def topology_command(proof_directory):
    tree = ast.parse(WORKLOAD.read_text(encoding="utf-8"))
    assignment = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name)
            and target.id == "topology_command"
            for target in node.targets
        )
    )

    return eval(
        compile(ast.Expression(assignment.value), str(WORKLOAD), "eval"),
        {"proof_directory_shell": shlex.quote(str(proof_directory))}
    )


def assigned_value(name):
    tree = ast.parse(WORKLOAD.read_text(encoding="utf-8"))
    assignment = next(
        node
        for node in tree.body
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == name
            for target in node.targets
        )
    )

    return ast.literal_eval(assignment.value)


class TestL3TopologyDiagnostics(unittest.TestCase):

    def test_topology_proof_timeout_is_60_seconds(self):
        self.assertEqual(
            assigned_value("TOPOLOGY_PROOF_TIMEOUT_SECONDS"),
            60
        )

        tree = ast.parse(WORKLOAD.read_text(encoding="utf-8"))
        topology_wait = next(
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "wait_for_files"
            and isinstance(node.args[0], ast.Name)
            and node.args[0].id == "topology_pattern"
        )
        timeout_argument = topology_wait.args[3]
        self.assertIsInstance(timeout_argument, ast.Name)
        self.assertEqual(
            timeout_argument.id,
            "TOPOLOGY_PROOF_TIMEOUT_SECONDS"
        )

    def run_topology_command(self, inject_read_failure=False):
        with tempfile.TemporaryDirectory(
            prefix="pmix-l3-topology-",
            dir="/tmp"
        ) as temporary:
            proof_directory = Path(temporary)
            command = topology_command(proof_directory)

            if inject_read_failure:
                bad_level = (
                    proof_directory
                    / "fake-sysfs"
                    / "cpu0"
                    / "cache"
                    / "index0"
                    / "level"
                )
                bad_level.mkdir(parents=True)

                good_cache = (
                    proof_directory
                    / "fake-sysfs"
                    / "cpu1"
                    / "cache"
                    / "index0"
                )
                good_cache.mkdir(parents=True)
                (good_cache / "level").write_text(
                    "3\n",
                    encoding="utf-8"
                )
                (good_cache / "id").write_text(
                    "7\n",
                    encoding="utf-8"
                )
                (good_cache / "shared_cpu_list").write_text(
                    "0-7\n",
                    encoding="utf-8"
                )

                fake_glob = (
                    proof_directory
                    / "fake-sysfs"
                    / "cpu[0-9]*"
                    / "cache"
                    / "index*"
                    / "level"
                )
                command = command.replace(
                    "/sys/devices/system/cpu/"
                    "cpu[0-9]*/cache/index*/level",
                    str(fake_glob),
                    1
                )

            subprocess.run(
                ["/bin/bash", "-n", "-c", command],
                check=True
            )

            environment = os.environ.copy()
            environment["PMIX_RANK"] = "0"
            completed = subprocess.run(
                ["/bin/bash", "-c", command],
                env=environment,
                check=False
            )

            status_path = next(
                proof_directory.glob("l3_topology_*.status")
            )
            status = dict(
                line.split("=", 1)
                for line in status_path.read_text(
                    encoding="utf-8"
                ).splitlines()
            )

            final_proofs = list(
                proof_directory.glob("topology_*_l3")
            )
            temporary_proofs = list(
                proof_directory.glob("topology_*_l3.tmp")
            )
            temporary_contents = [
                proof.read_text(encoding="utf-8")
                for proof in temporary_proofs
            ]

            return (
                completed.returncode,
                status,
                len(final_proofs),
                len(temporary_proofs),
                temporary_contents
            )

    def test_normal_scan_creates_final_proof(self):
        (
            returncode,
            status,
            final_count,
            temporary_count,
            _
        ) = (
            self.run_topology_command()
        )

        self.assertEqual(returncode, 0)
        self.assertEqual(status["exit_status"], "0")
        self.assertEqual(status["final_exists"], "yes")
        self.assertEqual(status["temporary_exists"], "no")
        self.assertEqual(final_count, 1)
        self.assertEqual(temporary_count, 0)

    def test_internal_read_failure_is_not_masked(self):
        (
            returncode,
            status,
            final_count,
            temporary_count,
            temporary_contents
        ) = (
            self.run_topology_command(inject_read_failure=True)
        )

        self.assertNotEqual(returncode, 0)
        self.assertNotEqual(status["exit_status"], "0")
        self.assertEqual(status["final_exists"], "no")
        self.assertEqual(status["temporary_exists"], "yes")
        self.assertEqual(final_count, 0)
        self.assertEqual(temporary_count, 1)
        self.assertIn("|7|0-7\n", temporary_contents[0])


if __name__ == "__main__":
    unittest.main()
