#!/bin/bash

# Stop immediately if an important command fails, an unset variable is used,
# or a command in a pipeline fails.
set -euo pipefail


# Directory containing this copy of the test files. Under ReFrame, this is
# the stage directory.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ReFrame supplies the interpreter and all fixture installation paths.
: "${PYTHON:?ReFrame must provide PYTHON}"
: "${PMIX:?ReFrame must provide PMIX}"
: "${PRRTE:?ReFrame must provide PRRTE}"
: "${LIBEVENT:?ReFrame must provide LIBEVENT}"
: "${PMIX_PYTHON_PACKAGE:?ReFrame must provide PMIX_PYTHON_PACKAGE}"

if ! command -v "$PYTHON" >/dev/null 2>&1
then
    echo "PMIx Python executable is not available: $PYTHON"
    exit 1
fi

for fixture_path in "$PMIX" "$PRRTE" "$LIBEVENT"
do
    if [[ ! -d "$fixture_path" ]]
    then
        echo "ReFrame fixture path is not a directory: $fixture_path"
        exit 1
    fi
done

if [[ ! -d "$PMIX_PYTHON_PACKAGE" ]]
then
    echo "PMIx Python package directory does not exist: $PMIX_PYTHON_PACKAGE"
    exit 1
fi


# Let Python find the PMIx Python module.
export PYTHONPATH="$PMIX_PYTHON_PACKAGE${PYTHONPATH:+:$PYTHONPATH}"

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
