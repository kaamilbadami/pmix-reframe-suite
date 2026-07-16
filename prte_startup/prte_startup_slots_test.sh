#!/bin/bash

N=$1
TRIALS=$2
SLOTS=$3

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
CURRENT_PRTE_DIR=${SCRIPT_DIR}/../../../prrte/bin
PLANNED_PRTE_DIR=${SCRIPT_DIR}/../../../../dependencies/prrte/prrte/bin

is_prte_dir() {
	[[ -n "${1}" && -x "${1}/prte" && -x "${1}/pterm" ]]
}

if is_prte_dir "${PRTE_DIR:-}"; then
	:
elif PRTE_PATH=$(type -P prte) &&
	PTERM_PATH=$(type -P pterm) &&
	[[ "$(dirname -- "${PRTE_PATH}")" == "$(dirname -- "${PTERM_PATH}")" ]] &&
	is_prte_dir "$(dirname -- "${PRTE_PATH}")"; then
	PRTE_DIR=$(dirname -- "${PRTE_PATH}")
elif is_prte_dir "${CURRENT_PRTE_DIR}"; then
	PRTE_DIR=${CURRENT_PRTE_DIR}
elif is_prte_dir "${PLANNED_PRTE_DIR}"; then
	PRTE_DIR=${PLANNED_PRTE_DIR}
else
	printf '%s\n' \
		"Error: could not find executable prte and pterm in the same directory." \
		"Supported methods:" \
		"  1. Set PRTE_DIR to their directory." \
		"  2. Put both executables in the same directory on PATH." \
		"  3. Use the current pmix_by_hand/prrte/bin layout." \
		"  4. Use the planned pmix_by_hand/dependencies/prrte/prrte/bin layout." >&2
	exit 1
fi

ALLOCATED=$(scontrol show hostnames | wc -l)

if ((N > ALLOCATED)); then
	echo "Error: requested too many nodes"
	exit 1
fi

HOSTS=$(scontrol show hostnames | head -n "$N" | xargs | sed "s/ /:$SLOTS,/g" | sed "s/$/:$SLOTS/")

for ((trial=1; trial<=TRIALS; trial++)); do
	rm -f dvm.uri prte.log

	START=$(date +%s.%N)

	"$PRTE_DIR/prte" --host "$HOSTS" --report-uri dvm.uri > prte.log 2>&1 &

	PRTE_PID=$!

	until grep -q "DVM ready" prte.log; do sleep 0.01; done

	END=$(date +%s.%N)

	awk -v nodes="$N" -v slots="$SLOTS" -v start="$START" -v end="$END" \
		'BEGIN {printf "nodes = %d slots = %d PRTE startup = %.3f seconds \n", nodes, slots, end - start}'

	"$PRTE_DIR/pterm" --dvm-uri file:dvm.uri > /dev/null 2>&1

	wait "$PRTE_PID"
done

echo "SUCCESS"
