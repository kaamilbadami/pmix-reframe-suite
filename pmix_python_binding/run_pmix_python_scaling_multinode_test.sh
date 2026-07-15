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


# Test 1-node, 2-node, and 4-node subsets.
NODE_COUNTS=(1 2 4)

# Use 32 process slots on each selected node.
SLOTS_PER_NODE=32

# Repeat each node-count test five times.
TRIALS=5


# Get every hostname in the Slurm allocation.
mapfile -t ALLOCATED_HOSTS < <(
    scontrol show hostnames "$SLURM_JOB_NODELIST"
)


# Make sure Slurm allocated at least four nodes.
if (( ${#ALLOCATED_HOSTS[@]} < 4 ))
then
    echo "Expected at least 4 allocated nodes"
    exit 1
fi


# Store the running PRRTE process ID.
PRTE_PID=""


# Stop the current PRRTE DVM and remove temporary files.
cleanup_dvm()
{
    if [[ -f dvm.uri ]]
    then
        "$PRRTE/bin/pterm" \
            --dvm-uri file:dvm.uri \
            >/dev/null 2>&1 || true
    fi

    if [[ -n "$PRTE_PID" ]]
    then
        wait "$PRTE_PID" 2>/dev/null || true
    fi

    PRTE_PID=""

    rm -f dvm.uri process_*_host
}


# Run cleanup if the script exits early.
trap cleanup_dvm EXIT


# Test each node-count subset inside the same allocation.
for node_count in "${NODE_COUNTS[@]}"
do
    # Select the first requested number of allocated nodes.
    SELECTED_HOSTS=(
        "${ALLOCATED_HOSTS[@]:0:$node_count}"
    )

    # Create a comma-separated host list for the Python test.
    EXPECTED_HOSTS=$(
        printf '%s\n' "${SELECTED_HOSTS[@]}" |
        xargs |
        sed 's/ /,/g'
    )

    # Add 32 slots to each hostname for prte --host.
    PRRTE_HOSTS=$(
        printf '%s\n' "${SELECTED_HOSTS[@]}" |
        xargs |
        sed "s/ /:${SLOTS_PER_NODE},/g; s/$/:${SLOTS_PER_NODE}/"
    )

    # Calculate the total number of processes.
    NUM_PROCESSES=$((node_count * SLOTS_PER_NODE))

    echo "NODE COUNT $node_count START"
    echo "EXPECTED HOSTS: $EXPECTED_HOSTS"
    echo "PRRTE HOSTS: $PRRTE_HOSTS"
    echo "PROCESS COUNT: $NUM_PROCESSES"

    LOG_FILE="prte-${node_count}node.log"

    rm -f dvm.uri "$LOG_FILE" process_*_host


    # Start PRRTE on only the selected hosts.
    "$PRRTE/bin/prte" \
        --host "$PRRTE_HOSTS" \
        --report-uri dvm.uri \
        > "$LOG_FILE" 2>&1 &

    PRTE_PID=$!


    # Wait up to 30 seconds for PRRTE to become ready.
    for ((attempt=1; attempt<=300; attempt++))
    do
        grep -q "DVM ready" "$LOG_FILE" && break
        sleep 0.1
    done


    # Fail if PRRTE did not become ready.
    grep -q "DVM ready" "$LOG_FILE"


    # Run the PMIx spawn and placement test.
    for ((trial=1; trial<=TRIALS; trial++))
    do
        echo "NODES $node_count TRIAL $trial START"

        rm -f process_*_host

        "$PYTHON" spawn_scaling_multinode_test.py \
            "$NUM_PROCESSES" \
            "$EXPECTED_HOSTS" \
            "$SLOTS_PER_NODE"

        echo "NODES $node_count TRIAL $trial PASS"
    done


    # Stop this DVM before testing another node subset.
    cleanup_dvm

    echo "NODE COUNT $node_count PASS"
done
