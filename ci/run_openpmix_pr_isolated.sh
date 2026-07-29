#!/bin/bash
# Enter the shared proven Frontier credential-isolation boundary in OpenPMIx
# source-PR mode.
set +x
set -euo pipefail

if (( $# != 0 )); then
    printf 'usage: %s\n' "${0##*/}" >&2
    exit 2
fi

script_dir=$(cd -- "${BASH_SOURCE[0]%/*}" && pwd -P)
exec /bin/bash --noprofile --norc \
    "$script_dir/run_pmix_tests_pr_isolated.sh" openpmix-pr
