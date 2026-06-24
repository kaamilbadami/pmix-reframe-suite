#!/bin/bash

N=$1
TRIALS=$2

PRTE_DIR=/lustre/orion/scratch/kbadami/gen243/reframe_practice/pmix_by_hand/prrte/bin

ALLOCATED=$(scontrol show hostnames | wc -l)

if ((N > ALLOCATED)); then
	echo "Error: requested too many nodes"
	exit 1
fi

HOSTS=$(scontrol show hostnames | head -n "$N" | xargs | tr ' ' ',')

for ((trial=1; trial<=TRIALS; trial++)); do
	rm -f dvm.uri prte.log

	START=$(date +%s.%N)

	"$PRTE_DIR/prte" --host "$HOSTS" --report-uri dvm.uri > prte.log 2>&1 &

	PRTE_PID=$!

	until grep -q "DVM ready" prte.log; do sleep 0.01; done

	END=$(date +%s.%N)

	awk -v nodes="$N" -v start="$START" -v end="$END" \
		'BEGIN {printf "nodes = %d PRTE startup = %.2f seconds \n", nodes, end - start}'

	"$PRTE_DIR/pterm" --dvm-uri file:dvm.uri > /dev/null 2>&1

	wait "$PRTE_PID"
done

echo "SUCCESS"
