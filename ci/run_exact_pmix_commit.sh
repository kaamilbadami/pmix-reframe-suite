#!/bin/bash
set -euo pipefail

if [[ ! ${PMIX_COMMIT+x} ]] ||
   [[ ! $PMIX_COMMIT =~ ^[0-9A-Fa-f]{40}$ ]]; then
    printf 'error: PMIX_COMMIT must be exactly 40 hexadecimal characters\n' >&2
    exit 2
fi

export PMIX_COMMIT
printf 'Selected exact OpenPMIx commit: %s\n' "$PMIX_COMMIT"

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
exec bash "$script_dir/run_pmix_python_suite.sh"
