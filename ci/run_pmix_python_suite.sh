#!/bin/bash
set -euo pipefail

usage() {
    cat <<'EOF'
Usage: ci/run_pmix_python_suite.sh [--list-only|--help]

Run the PMIx Python unit tests and ReFrame suite. --list-only validates
the 11-check listing without running the ReFrame checks.

Required environment variables:
  PMIX_PYTHON  Python executable selected for the PMIx build
  RFM_BIN      ReFrame executable
EOF
}

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
repo_root=$(cd -- "$script_dir/.." && pwd)
cd "$repo_root"

mode=run
case $# in
    0) ;;
    1)
        case "$1" in
            --list-only) mode=list-only ;;
            --help) usage; exit 0 ;;
            *) printf 'error: unknown argument: %s\n' "$1" >&2; usage >&2; exit 2 ;;
        esac
        ;;
    *) printf 'error: expected at most one argument\n' >&2; usage >&2; exit 2 ;;
esac

if [[ -z "${PMIX_PYTHON:-}" ]]; then
    printf 'error: PMIX_PYTHON must be set to the PMIx Python executable\n' >&2
    exit 2
fi
if [[ -z "${RFM_BIN:-}" ]]; then
    printf 'error: RFM_BIN must be set to the ReFrame executable\n' >&2
    exit 2
fi
if [[ ! -x "$PMIX_PYTHON" ]]; then
    printf 'error: PMIX_PYTHON is not executable: %s\n' "$PMIX_PYTHON" >&2
    exit 1
fi

pmix_python_dir=$(cd -- "$(dirname -- "$PMIX_PYTHON")" && pwd)
PMIX_PYTHON="$pmix_python_dir/$(basename -- "$PMIX_PYTHON")"
export PATH="$pmix_python_dir:$PATH"

if ! "$PMIX_PYTHON" -c 'import Cython' >/dev/null; then
    printf 'error: PMIX_PYTHON cannot import Cython\n' >&2
    exit 1
fi
if ! python3_bin=$(command -v python3); then
    printf 'error: python3 is not available after updating PATH\n' >&2
    exit 1
fi
if ! "$python3_bin" -c 'import Cython' >/dev/null; then
    printf 'error: selected python3 cannot import Cython: %s\n' "$python3_bin" >&2
    exit 1
fi
if ! cython_bin=$(command -v cython); then
    printf 'error: cython is not available after updating PATH\n' >&2
    exit 1
fi

pmix_python_executable=$("$PMIX_PYTHON" -c 'import sys; print(sys.executable)')
pmix_python_version=$("$PMIX_PYTHON" -c 'import platform; print(platform.python_version())')
pmix_python_prefix=$("$PMIX_PYTHON" -c 'import sys; print(sys.prefix)')
python3_prefix=$("$python3_bin" -c 'import sys; print(sys.prefix)')
cython_version=$("$PMIX_PYTHON" -c 'import Cython; print(Cython.__version__)')

if [[ "$pmix_python_prefix" != "$python3_prefix" ]]; then
    printf 'error: PMIX_PYTHON and python3 use different Python prefixes:\n' >&2
    printf '  PMIX_PYTHON: %s\n  python3:      %s\n' \
        "$pmix_python_prefix" "$python3_prefix" >&2
    exit 1
fi

printf 'Python environment:\n'
printf '  executable: %s\n' "$pmix_python_executable"
printf '  version:    %s\n' "$pmix_python_version"
printf '  prefix:     %s\n' "$pmix_python_prefix"
printf '  python3:    %s\n' "$python3_bin"
printf '  Cython:     %s (%s)\n' "$cython_version" "$cython_bin"

if [[ ! -x "$RFM_BIN" ]]; then
    printf 'error: RFM_BIN is not executable: %s\n' "$RFM_BIN" >&2
    exit 1
fi
printf '\nReFrame version:\n'
"$RFM_BIN" --version

if [[ -n "${PYTHONPATH:-}" ]]; then
    export PYTHONPATH="$repo_root/pmix_python_binding:$PYTHONPATH"
else
    export PYTHONPATH="$repo_root/pmix_python_binding"
fi

printf '\nPython unit tests:\n'
"$PMIX_PYTHON" -m unittest discover \
    -s pmix_python_binding/unit_tests -p 'test_*.py'

rfm_common=(-C sysconfig.yaml -c pmix_python_binding/reframe)
rfm_system=(--system=frontier:batch)

printf '\nReFrame check listing:\n'
if listing_output=$("$RFM_BIN" "${rfm_common[@]}" -l "${rfm_system[@]}" 2>&1); then
    printf '%s\n' "$listing_output"
else
    status=$?
    printf '%s\n' "$listing_output"
    printf 'error: ReFrame listing failed with exit status %d\n' "$status" >&2
    exit "$status"
fi

check_count=$(sed -n 's/^Found \([0-9][0-9]*\) check(s)$/\1/p' <<<"$listing_output")
if [[ ! "$check_count" =~ ^[0-9]+$ ]]; then
    printf 'error: could not read the check count from ReFrame listing output\n' >&2
    exit 1
fi
if (( check_count != 11 )); then
    printf 'error: expected exactly 11 ReFrame checks, found %d\n' "$check_count" >&2
    exit 1
fi
printf 'Validated ReFrame listing: exactly 11 checks.\n'

if [[ "$mode" == list-only ]]; then
    printf 'List-only validation complete; ReFrame checks were not run.\n'
    exit 0
fi

"$RFM_BIN" "${rfm_common[@]}" -r "${rfm_system[@]}" --keep-stage-files
