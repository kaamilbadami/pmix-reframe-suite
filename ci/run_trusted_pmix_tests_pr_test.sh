#!/bin/bash
# Launch exactly one suite-owned ReFrame check after credential scrubbing.
set +x
set -euo pipefail

if (( $# != 0 )); then
    printf 'usage: %s\n' "${0##*/}" >&2
    exit 2
fi
for forbidden_name in \
    GITHUB_PR_READ_TOKEN GITHUB_STATUS_TOKEN CI_JOB_TOKEN CI_REPOSITORY_URL \
    CI_JOB_JWT CI_JOB_JWT_V2
do
    if [[ -v $forbidden_name ]]; then
        printf '%s\n' 'error: a forbidden credential reached the ReFrame launcher' >&2
        exit 2
    fi
done

script_dir=$(cd -- "${BASH_SOURCE[0]%/*}" && pwd -P)
repo_root=$(cd -- "$script_dir/.." && pwd -P)
cd -- "$repo_root"

records=ci/pmix_tests_pr_artifacts.py
expected_source=$repo_root/ci-pr-execution/pmix-tests
expected_pmix_python=/lustre/orion/gen243/proj-shared/pmix-reframe-ci-tools/pmix-py310/bin/python
expected_rfm_bin=/lustre/orion/gen243/proj-shared/pmix-reframe-ci-tools/reframe-4.10/bin/reframe
[[ ${PMIX_TESTS_SOURCE_DIR:-} == "$expected_source" ]] || {
    printf '%s\n' 'error: PMIX_TESTS_SOURCE_DIR is not the fixed checkout' >&2
    exit 2
}
[[ ${PMIX_TESTS_PR_HEAD_SHA:-} =~ ^[0-9a-f]{40}$ ]] || {
    printf '%s\n' 'error: exact PR head SHA is unavailable' >&2
    exit 2
}
[[ ${PMIX_TESTS_PR_EXECUTION_ID:-} =~ ^[0-9a-f]{32}$ ]] || {
    printf '%s\n' 'error: execution identifier is unavailable' >&2
    exit 2
}
[[ ${PMIX_PYTHON:-} == "$expected_pmix_python" && -x $PMIX_PYTHON ]] || {
    printf '%s\n' 'error: PMIX_PYTHON is not the fixed Frontier Python' >&2
    exit 2
}
[[ ${RFM_BIN:-} == "$expected_rfm_bin" && -x $RFM_BIN ]] || {
    printf '%s\n' 'error: RFM_BIN is not the fixed ReFrame executable' >&2
    exit 2
}
[[ ${PYTHONPATH:-} == /lustre/orion/gen243/proj-shared/pmix-reframe-ci-tools/reframe-4.10/lib/python3.11/site-packages ]] || {
    printf '%s\n' 'error: ReFrame Python path is not the fixed installation' >&2
    exit 2
}

python_version=$("$PMIX_PYTHON" -c 'import platform; print(platform.python_version())')
[[ $python_version == 3.10.20 ]] || {
    printf 'error: fixed PMIx Python version is %s, expected 3.10.20\n' \
        "$python_version" >&2
    exit 2
}
cython_version=$("$PMIX_PYTHON" -c 'import Cython; print(Cython.__version__)')
[[ $cython_version == 3.2.6 ]] || {
    printf 'error: fixed Cython version is %s, expected 3.2.6\n' \
        "$cython_version" >&2
    exit 2
}
reframe_version=$("$RFM_BIN" --version)
[[ $reframe_version == 4.10.0 ]] || {
    printf 'error: fixed ReFrame version is %s, expected 4.10.0\n' \
        "$reframe_version" >&2
    exit 2
}

"$PMIX_PYTHON" "$records" validate-checkout \
    --checkout ci-pr-execution/pmix-tests >/dev/null

reframe_prefix=$repo_root/ci-pr-execution/reframe
for path in "$reframe_prefix"; do
    if [[ -L $path || -e $path ]]; then
        printf 'error: trusted test output path already exists: %s\n' "$path" >&2
        exit 2
    fi
done

mkdir -m 700 -- "$reframe_prefix"

reframe_status=0
"$RFM_BIN" \
    -C "$repo_root/sysconfig.yaml" \
    -c "$repo_root/pmix_python_binding/reframe/pmix_tests_pr_hello_world_test.py" \
    -r \
    --system=frontier:batch \
    -n '^PMIxTestsPRHelloWorldTest$' \
    --keep-stage-files \
    --prefix "$reframe_prefix" \
    --report-file "$reframe_prefix/run-report.json" || reframe_status=$?
exit "$reframe_status"
