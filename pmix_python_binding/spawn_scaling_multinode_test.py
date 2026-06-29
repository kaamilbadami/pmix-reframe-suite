import os
import sys
import time
from collections import Counter

import pmix


# Expected command:
# python spawn_scaling_multinode_test.py PROCESSES HOSTS SLOTS_PER_NODE
if len(sys.argv) != 4:
    raise SystemExit(
        "usage: spawn_scaling_multinode_test.py "
        "NUMBER_OF_PROCESSES EXPECTED_HOSTS SLOTS_PER_NODE"
    )


# Read the values passed by the shell script.
num_processes = int(sys.argv[1])
expected_hosts = sys.argv[2].split(",")
slots_per_node = int(sys.argv[3])


# Create one hostname proof file for each process.
proof_files = [
    os.path.abspath(f"process_{number}_host")
    for number in range(1, num_processes + 1)
]


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


# Create one app for each process.
# Each process writes its hostname into its own proof file.
apps = [
    {
        "cmd": "/bin/bash",
        "argv": [
            "bash",
            "-c",
            f"hostname -s > {proof_file}"
        ],
        "maxprocs": 1
    }
    for proof_file in proof_files
]


# Ask PMIx and PRRTE to launch every process.
spawn_result = tool.spawn([], apps)
print("spawn:", spawn_result)

if spawn_result[0] != 0:
    tool.finalize()
    raise SystemExit("spawn failed")


# Wait up to five seconds for every hostname file.
for attempt in range(50):
    if all(os.path.exists(name) for name in proof_files):
        break

    time.sleep(0.1)


# Read the hostname written by every process.
observed_hosts = []

for process_number, proof_file in enumerate(proof_files, start=1):
    if not os.path.exists(proof_file):
        print(f"process {process_number} host: MISSING")
        continue

    with open(proof_file) as file:
        hostname = file.readline().strip()

    observed_hosts.append(hostname)
    print(f"process {process_number} host:", hostname)


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
    raise SystemExit("not every process created a hostname file")


# Verify that no process ran on an unexpected host.
if set(observed_hosts) != set(expected_hosts):
    raise SystemExit("processes did not run on the expected hosts")


# Verify that every selected node ran the expected number of processes.
for hostname in expected_hosts:
    if host_counts[hostname] != slots_per_node:
        raise SystemExit(
            f"{hostname} ran {host_counts[hostname]} processes; "
            f"expected {slots_per_node}"
        )


if finalize_result != 0:
    raise SystemExit("finalize failed")


print("PLACEMENT VERIFIED")
