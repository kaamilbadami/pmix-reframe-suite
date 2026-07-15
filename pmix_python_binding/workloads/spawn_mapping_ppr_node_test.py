import glob
import os
import shlex
import sys
import time
from collections import Counter

import pmix


# Expected command:
# python spawn_mapping_ppr_node_test.py HOSTS PROCESSES_PER_NODE
if len(sys.argv) != 3:
    raise SystemExit(
        "usage: spawn_mapping_ppr_node_test.py "
        "EXPECTED_HOSTS PROCESSES_PER_NODE"
    )


# Read the values passed by the shell script.
expected_hosts = sys.argv[1].split(",")
processes_per_node = int(sys.argv[2])


if any(not hostname for hostname in expected_hosts):
    raise SystemExit("expected host list contains an empty hostname")


if processes_per_node < 1:
    raise SystemExit("processes per node must be at least 1")


# Calculate the total number of processes to launch.
num_processes = len(expected_hosts) * processes_per_node


# Every spawned process creates a unique hostname proof file.
proof_directory = os.path.abspath(".")
proof_pattern = os.path.join(proof_directory, "process_*_host")

for old_proof_file in glob.glob(proof_pattern):
    os.remove(old_proof_file)


# Read the address of the running PRRTE DVM.
with open("dvm.uri") as file:
    dvm_uri = file.readline().strip()


# Create the PMIx tool.
tool = pmix.PMIxTool()


# Connect the PMIx tool to PRRTE.
init_result = tool.init([
    {
        "key": "pmix.srvr.uri",
        "value": dvm_uri,
        "val_type": pmix.PMIX_STRING
    }
])

print("init:", init_result)

if init_result[0] != 0:
    raise SystemExit("init failed")


# Create one application with the requested copies per selected node.
# The hostname and PID make every proof filename unique.
proof_directory_shell = shlex.quote(proof_directory)

apps = [
    {
        "cmd": "/bin/bash",
        "argv": [
            "bash",
            "-c",
            (
                'host=$(hostname -s); '
                f'printf "%s\\n" "$host" > '
                f'{proof_directory_shell}/process_${{host}}_$$_host'
            )
        ],
        "maxprocs": num_processes
    }
]


# Build a reusable processes-per-node mapping policy.
map_policy = f"ppr:{processes_per_node}:node"

job_info = [
    {
        "key": pmix.PMIX_MAPBY,
        "value": map_policy,
        "val_type": pmix.PMIX_STRING
    }
]

print("map-by policy:", map_policy)
print("processes per node:", processes_per_node)
print("total process count:", num_processes)


# Ask PMIx and PRRTE to launch the application.
spawn_result = tool.spawn(job_info, apps)
print("spawn:", spawn_result)

if spawn_result[0] != 0:
    tool.finalize()
    raise SystemExit("spawn failed")


# Wait up to five seconds for every hostname file.
proof_files = []

for attempt in range(50):
    proof_files = sorted(glob.glob(proof_pattern))

    if len(proof_files) == num_processes:
        break

    time.sleep(0.1)


# Read the hostname written by every process.
proof_files = sorted(glob.glob(proof_pattern))
observed_hosts = []

for proof_file in proof_files:
    with open(proof_file) as file:
        hostname = file.readline().strip()

    observed_hosts.append(hostname)
    print(f"{os.path.basename(proof_file)} host:", hostname)


# Count how many processes ran on each hostname.
host_counts = Counter(observed_hosts)

print("expected hosts:", ",".join(expected_hosts))

for hostname in expected_hosts:
    print(f"host {hostname} process count:", host_counts[hostname])


# Disconnect the PMIx tool from PRRTE.
finalize_result = tool.finalize()
print("finalize:", finalize_result)


# Verify that every process created a hostname file.
if len(observed_hosts) != num_processes:
    raise SystemExit(
        f"found {len(observed_hosts)} process proof files; "
        f"expected {num_processes}"
    )


# Verify that no process ran on an unexpected host.
if set(observed_hosts) != set(expected_hosts):
    raise SystemExit("processes did not run on the expected hosts")


# Verify that every selected node ran the requested process count.
for hostname in expected_hosts:
    if host_counts[hostname] != processes_per_node:
        raise SystemExit(
            f"{hostname} ran {host_counts[hostname]} processes; "
            f"expected {processes_per_node}"
        )


if finalize_result != 0:
    raise SystemExit("finalize failed")


print("PPR NODE PLACEMENT VERIFIED")
