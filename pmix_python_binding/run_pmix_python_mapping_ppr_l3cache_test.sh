#!/bin/bash

# Stop immediately if an important command fails, an unset variable is used,
# or a command in a pipeline fails.
set -euo pipefail


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


# Comma-separated node counts to test.
# Examples:
#   NODE_COUNTS="1"
#   NODE_COUNTS="1,2"
NODE_COUNT_VALUES="${NODE_COUNTS:-1}"
IFS=',' read -r -a NODE_COUNT_LIST <<< "$NODE_COUNT_VALUES"


# Comma-separated processes-per-L3-cache values to test.
# Examples:
#   PPR_VALUES="1"
#   PPR_VALUES="1,2,4,8"
PPR_VALUE_TEXT="${PPR_VALUES:-8}"
IFS=',' read -r -a PPR_VALUE_LIST <<< "$PPR_VALUE_TEXT"


# Number of process slots advertised for every selected node.
# ppr:8:l3cache may require 64 processes on a Frontier node.
SLOTS_PER_NODE="${SLOTS_PER_NODE:-64}"


# Number of times to repeat each mapping test.
TRIALS="${TRIALS:-1}"


# Validate the slot count.
if [[ ! "$SLOTS_PER_NODE" =~ ^[1-9][0-9]*$ ]]
then
    echo "Invalid slots-per-node value: $SLOTS_PER_NODE"
    exit 1
fi


# Validate the trial count.
if [[ ! "$TRIALS" =~ ^[1-9][0-9]*$ ]]
then
    echo "Invalid trial count: $TRIALS"
    exit 1
fi


# Validate the requested node counts and find the largest one.
REQUIRED_NODES=0

for node_count in "${NODE_COUNT_LIST[@]}"
do
    if [[ ! "$node_count" =~ ^[1-9][0-9]*$ ]]
    then
        echo "Invalid node count: $node_count"
        exit 1
    fi

    if (( node_count > REQUIRED_NODES ))
    then
        REQUIRED_NODES=$node_count
    fi
done


# Validate the requested processes-per-L3-cache values.
for processes_per_l3cache in "${PPR_VALUE_LIST[@]}"
do
    if [[ ! "$processes_per_l3cache" =~ ^[1-9][0-9]*$ ]]
    then
        echo "Invalid PPR value: $processes_per_l3cache"
        exit 1
    fi
done


# Get every hostname in the Slurm allocation.
mapfile -t ALLOCATED_HOSTS < <(
    scontrol show hostnames "$SLURM_JOB_NODELIST"
)


# Make sure Slurm allocated enough nodes for the largest test.
if (( ${#ALLOCATED_HOSTS[@]} < REQUIRED_NODES ))
then
    echo "Expected at least $REQUIRED_NODES allocated nodes; received ${#ALLOCATED_HOSTS[@]}"
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

    rm -f dvm.uri topology_*_l3* process_*_l3* started_*_l3*
}


# Run cleanup if the script exits early.
trap cleanup_dvm EXIT


# Test each node-count subset inside the same allocation.
for node_count in "${NODE_COUNT_LIST[@]}"
do
    # Select the first requested number of allocated nodes.
    SELECTED_HOSTS=(
        "${ALLOCATED_HOSTS[@]:0:$node_count}"
    )

    # Create the comma-separated host list expected by the Python verifier.
    EXPECTED_HOSTS="${SELECTED_HOSTS[0]}"

    # Create the explicit PRRTE host list with advertised slots.
    PRRTE_HOSTS="${SELECTED_HOSTS[0]}:${SLOTS_PER_NODE}"

    for ((host_index=1; host_index<node_count; host_index++))
    do
        EXPECTED_HOSTS+=",${SELECTED_HOSTS[$host_index]}"
        PRRTE_HOSTS+=",${SELECTED_HOSTS[$host_index]}:${SLOTS_PER_NODE}"
    done

    echo "NODE COUNT $node_count START"
    echo "EXPECTED HOSTS: $EXPECTED_HOSTS"
    echo "PRRTE HOSTS: $PRRTE_HOSTS"

    LOG_FILE="prte-${node_count}node-l3cache.log"

    rm -f \
        dvm.uri \
        "$LOG_FILE" \
        topology_*_l3* \
        process_*_l3*


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


    # Test every requested processes-per-L3-cache mapping value.
    for processes_per_l3cache in "${PPR_VALUE_LIST[@]}"
    do
        echo \
            "NODES $node_count PPR $processes_per_l3cache L3CACHE START"

        for ((trial=1; trial<=TRIALS; trial++))
        do
            echo \
                "NODES $node_count PPR $processes_per_l3cache L3CACHE TRIAL $trial START"

            rm -f topology_*_l3* process_*_l3* started_*_l3*

            timeout \
                --signal=TERM \
                --kill-after=30s \
                300s \
                "$PYTHON" -u spawn_mapping_ppr_l3cache_test.py \
                "$EXPECTED_HOSTS" \
                "$processes_per_l3cache"

            echo \
                "NODES $node_count PPR $processes_per_l3cache L3CACHE TRIAL $trial PASS"
        done

        echo \
            "NODES $node_count PPR $processes_per_l3cache L3CACHE PASS"
    done


    # Stop this DVM before testing another node subset.
    cleanup_dvm

    echo "NODE COUNT $node_count PASS"
done


echo "PPR L3CACHE MAPPING TEST PASS"
