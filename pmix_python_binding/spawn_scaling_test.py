import os
import sys
import time

import pmix


# Read the process count passed by the shell script.
if len(sys.argv) != 2:
    raise SystemExit("usage: spawn_scaling_test.py NUMBER_OF_PROCESSES")

try:
    num_processes = int(sys.argv[1])
except ValueError:
    raise SystemExit("process count must be an integer")

if num_processes < 1:
    raise SystemExit("process count must be at least 1")


# Create one proof-file name for each requested process.
proof_files = [
    f"process_{number}_worked"
    for number in range(1, num_processes + 1)
]


# Read the address of the running PRRTE DVM.
with open("dvm.uri") as file:
    dvm_uri = file.readline().strip()


# Create the PMIx tool used to connect, spawn processes, and finalize.
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


# Create one PMIx app entry for each requested process.
apps = [
    {
        "cmd": "/usr/bin/touch",
        "argv": ["touch", proof_file],
        "maxprocs": 1
    }
    for proof_file in proof_files
]


# Ask PMIx and PRRTE to launch all requested processes.
spawn_result = tool.spawn([], apps)
print("spawn:", spawn_result)

if spawn_result[0] != 0:
    tool.finalize()
    raise SystemExit("spawn failed")


# Wait up to two seconds for every proof file to appear.
for attempt in range(20):
    if all(os.path.exists(name) for name in proof_files):
        break

    time.sleep(0.1)


# Check and print whether every process created its proof file.
proof_results = []

for process_number, proof_file in enumerate(proof_files, start=1):
    proof_exists = os.path.exists(proof_file)
    proof_results.append(proof_exists)

    print(f"process {process_number} proof:", proof_exists)


# Disconnect the PMIx tool from PRRTE.
finalize_result = tool.finalize()
print("finalize:", finalize_result)


# Fail if any process did not create its proof file.
if not all(proof_results):
    raise SystemExit("not all processes created proof files")


# Fail if PMIx did not finalize successfully.
if finalize_result != 0:
    raise SystemExit("finalize failed")
