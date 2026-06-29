#!/bin/bash

# Stop the script immediately if an important command fails.
set -e


# Directory containing this copy of the test files. Under ReFrame, this is
# the stage directory.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Default locations of the externally installed software. These values may
# be overridden by setting the variables before running the test.
INSTALL_DIR="${INSTALL_DIR:-/lustre/orion/scratch/kbadami/gen243/reframe_practice/pmix_by_hand/python_binding_test}"
PYTHON="${PYTHON:-/lustre/orion/scratch/kbadami/gen243/reframe_practice/pmix-py310/bin/python}"
PMIX="${PMIX:-$INSTALL_DIR/pmix-install-finalize-fix}"
PRRTE="${PRRTE:-$INSTALL_DIR/prrte-install-py310}"
LIBEVENT="${LIBEVENT:-/lustre/orion/scratch/kbadami/gen243/reframe_practice/pmix_by_hand/libevent}"


# Let Python find the PMIx Python module.
export PYTHONPATH="$PMIX/lib/python3.10/site-packages${PYTHONPATH:+:$PYTHONPATH}"

# Let programs find the PMIx, PRRTE, and libevent libraries.
export LD_LIBRARY_PATH="$PMIX/lib:$PRRTE/lib:$LIBEVENT/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"


# Run from the directory containing the staged test programs.
cd "$SCRIPT_DIR"


# Process counts that will be tested.
PROCESS_COUNTS=(1 2 4 8 16 32)

# Number of times to test each process count.
TRIALS=5


# Get the name of the current compute node.
NODE=$(hostname -s)

# Tell PRRTE that this node has 32 available slots.
HOSTS="${NODE}:32"

echo "PRRTE HOSTS: $HOSTS"


# Remove files left over from an earlier run.
rm -f dvm.uri prte-test.log process_*_worked


# Start the PRRTE DVM on the current compute node.
"$PRRTE/bin/prte" \
    --host "$HOSTS" \
    --report-uri dvm.uri \
    > prte-test.log 2>&1 &


# Stop PRRTE and remove temporary files when the script ends.
cleanup()
{
    "$PRRTE/bin/pterm" \
        --dvm-uri file:dvm.uri \
        >/dev/null 2>&1 || true

    rm -f dvm.uri process_*_worked
}

trap cleanup EXIT


# Wait until the PRRTE DVM reports that it is ready.
for attempt in {1..100}
do
    grep -q "DVM ready" prte-test.log && break
    sleep 0.1
done


# Fail if PRRTE did not become ready.
grep -q "DVM ready" prte-test.log


# Test each process count using the same allocation and PRRTE DVM.
for num_processes in "${PROCESS_COUNTS[@]}"
do
    echo "PROCESS COUNT $num_processes START"

    # Repeat this process count for the requested number of trials.
    for ((trial=1; trial<=TRIALS; trial++))
    do
        echo "PROCESSES $num_processes TRIAL $trial START"

        # Remove proof files from the previous trial.
        rm -f process_*_worked

        # Run the Python PMIx spawn test.
        "$PYTHON" spawn_scaling_test.py "$num_processes"

        echo "PROCESSES $num_processes TRIAL $trial PASS"
    done

    echo "PROCESS COUNT $num_processes PASS"
done
