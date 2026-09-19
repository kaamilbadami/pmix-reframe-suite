#!/bin/bash
set -euo pipefail

repo_root=$(cd -- "${BASH_SOURCE[0]%/*}/.." && pwd -P)
cd "$repo_root"
test_dir=$(mktemp -d /tmp/openpmix-pr-rfm-test.XXXXXX)
trap '/usr/bin/rm -rf -- "$test_dir"' EXIT

rfm=/lustre/orion/gen243/proj-shared/pmix-reframe-ci-tools/reframe-4.10/bin/reframe
python=/lustre/orion/gen243/proj-shared/pmix-reframe-ci-tools/pmix-py310/bin/python
rfm_python=/lustre/orion/gen243/proj-shared/pmix-reframe-ci-tools/reframe-4.10/lib/python3.11/site-packages
[[ -x $rfm && -x $python ]] || {
    printf '%s\n' 'not ok - fixed Frontier validation tools are unavailable' >&2
    exit 1
}

export PMIX_COMMIT
PMIX_COMMIT=$(git rev-parse HEAD)
export PMIX_TRUSTED_SOURCE_DIR=$repo_root
export PMIX_PYTHON=$python
export PYTHONPATH=$rfm_python
unset PRRTE_BRANCH PRRTE_COMMIT

"$rfm" -C ci/openpmix_pr_sysconfig.yaml \
    -c pmix_python_binding/reframe --dry-run \
    --system=frontier:batch --prefix "$test_dir/reframe" \
    --report-file "$test_dir/run-report.json" --keep-stage-files \
    > "$test_dir/dry-run.out"

grep -Fq 'Ran 17/17 test case(s)' "$test_dir/dry-run.out" ||
    { printf '%s\n' 'not ok - dry run did not contain the exact 17-case graph' >&2; exit 1; }
printf '%s\n' 'ok - dry run generates the exact 11 checks and 6 fixtures'

mapfile -t job_scripts < <(
    find "$test_dir/reframe" -type f -name rfm_job.sh -print | sort
)
(( ${#job_scripts[@]} == 14 )) ||
    { printf 'not ok - expected 14 run scripts, found %d\n' \
        "${#job_scripts[@]}" >&2; exit 1; }
slurm_count=$(rg -l -- '--export=NIL' "${job_scripts[@]}" | wc -l)
(( slurm_count == 11 )) ||
    { printf 'not ok - expected 11 Slurm scripts with --export=NIL, found %d\n' \
        "$slurm_count" >&2; exit 1; }
printf '%s\n' 'ok - all 11 submitted test scripts use Slurm --export=NIL'

mapfile -t generated_scripts < <(
    find "$test_dir/reframe" -type f \
        \( -name rfm_job.sh -o -name rfm_build.sh \) -print | sort
)
(( ${#generated_scripts[@]} == 17 )) ||
    { printf 'not ok - expected 17 generated scripts, found %d\n' \
        "${#generated_scripts[@]}" >&2; exit 1; }
runtime_count=$(rg -l \
    'frontier-openpmix-pr-|PMIX_MCA_tmpdir_base|PRTE_MCA_tmpdir_base' \
    "${generated_scripts[@]}" | wc -l)
(( runtime_count == 17 )) ||
    { printf 'not ok - node-local runtime protection missing from generated scripts\n' >&2; exit 1; }
init_count=$(rg -l \
    '/etc/profile.d/olcf-env.sh.*|/etc/bash.bashrc.local' \
    "${generated_scripts[@]}" | wc -l)
(( init_count == 17 )) ||
    { printf 'not ok - system initialization missing from generated scripts\n' >&2; exit 1; }
printf '%s\n' 'ok - generated scripts preserve system initialization and node-local runtime storage'

if rg -n 'GITHUB|CI_JOB_TOKEN|OPENPMIX_PR_PROTECTED_SENTINEL' \
        "${generated_scripts[@]}" >/dev/null; then
    printf '%s\n' 'not ok - protected variable appeared in generated script' >&2
    exit 1
fi
printf '%s\n' 'ok - generated build and Slurm environments contain no protected sentinel'

build_script=$(find "$test_dir/reframe" -type f \
    -path '*/build_pmix_*/rfm_build.sh' -print -quit)
fetch_script=$(find "$test_dir/reframe" -type f \
    -path '*/fetch_pmix_*/rfm_job.sh' -print -quit)
prrte_release_fetch_script=$(find "$test_dir/reframe" -type f \
    -path '*/fetch_prrte_*/rfm_job.sh' -print -quit)
prrte_release_build_script=$(find "$test_dir/reframe" -type f \
    -path '*/build_prrte_*/rfm_build.sh' -print -quit)
[[ -n $build_script && -n $fetch_script ]] ||
    { printf '%s\n' 'not ok - PMIx fixture scripts are missing' >&2; exit 1; }
[[ -n $prrte_release_fetch_script && -n $prrte_release_build_script ]] ||
    { printf '%s\n' 'not ok - release PRRTE fixture scripts are missing' >&2; exit 1; }
grep -Fq './autogen.pl' "$build_script" ||
    { printf '%s\n' 'not ok - existing PMIx build path was not generated' >&2; exit 1; }
grep -Fq 'PMIX_MODE=trusted-pr' "$fetch_script" ||
    { printf '%s\n' 'not ok - trusted exact-source mode was not generated' >&2; exit 1; }
if grep -Fq 'submodule update' "$fetch_script"; then
    # The normal branch remains in the trusted script, but it must be outside
    # the trusted-source branch. The structural source test below verifies it.
    :
fi
python3 - <<'PY'
from pathlib import Path
text = Path("pmix_build_class.py").read_text()
trusted = text.index('if test -n "$PMIX_TRUSTED_SOURCE"')
normal = text.index("else", trusted)
submodule = text.index("submodule update", normal)
assert trusted < normal < submodule
assert "cp -a -- \"$PMIX_TRUSTED_SOURCE\" pmix-git" in text[trusted:normal]
PY
printf '%s\n' 'ok - trusted checkout feeds the existing exact PMIx Autotools build path'

grep -Fq \
    'https://github.com/openpmix/prrte/releases/download/v4.1.0/prrte-4.1.0.tar.gz' \
    "$prrte_release_fetch_script" ||
    { printf '%s\n' 'not ok - default PRRTE release URL changed' >&2; exit 1; }
grep -Fq 'PRRTE_MODE=release' "$prrte_release_fetch_script" ||
    { printf '%s\n' 'not ok - release PRRTE provenance is missing' >&2; exit 1; }
grep -Fq 'tar xzf prrte-4.1.0.tar.gz' "$prrte_release_build_script" ||
    { printf '%s\n' 'not ok - default PRRTE extraction changed' >&2; exit 1; }
grep -Fq "sed -i '/PRTE_ERROR_LOG(PRTE_ERR_ADDRESSEE_UNKNOWN);/d'" \
    "$prrte_release_build_script" ||
    { printf '%s\n' 'not ok - legacy release-only PRRTE edit changed' >&2; exit 1; }
if grep -Fq './autogen.pl' "$prrte_release_build_script"; then
    printf '%s\n' 'not ok - release PRRTE path unexpectedly runs autogen.pl' >&2
    exit 1
fi
printf '%s\n' 'ok - default PRRTE 4.1.0 release path is unchanged'

prrte_sha=22820a01e17547dbf1c4f9628eac327f193caa45
PRRTE_BRANCH=v5.0 PRRTE_COMMIT=$prrte_sha \
    "$rfm" -C ci/openpmix_pr_sysconfig.yaml \
    -c pmix_python_binding/reframe --dry-run \
    --system=frontier:batch --prefix "$test_dir/prrte-exact" \
    --report-file "$test_dir/prrte-exact-report.json" --keep-stage-files \
    > "$test_dir/prrte-exact-dry-run.out"

prrte_exact_fetch_script=$(find "$test_dir/prrte-exact" -type f \
    -path '*/fetch_prrte_*/rfm_job.sh' -print -quit)
prrte_exact_build_script=$(find "$test_dir/prrte-exact" -type f \
    -path '*/build_prrte_*/rfm_build.sh' -print -quit)
[[ -n $prrte_exact_fetch_script && -n $prrte_exact_build_script ]] ||
    { printf '%s\n' 'not ok - exact PRRTE fixture scripts are missing' >&2; exit 1; }

for expected in \
    "PRRTE_REQUESTED_BRANCH=v5.0" \
    "PRRTE_REQUESTED_COMMIT=$prrte_sha" \
    'PRRTE_ORIGIN=https://github.com/openpmix/prrte.git' \
    'git check-ref-format "refs/heads/$PRRTE_REQUESTED_BRANCH"' \
    'git --no-pager clone --no-checkout "$PRRTE_ORIGIN" prrte-git' \
    'merge-base --is-ancestor' \
    'checkout --detach "$PRRTE_SHA"' \
    'submodule update --init --recursive' \
    'PRRTE_MODE=exact' \
    'PRRTE_BRANCH_COMMIT=%s'; do
    grep -Fq "$expected" "$prrte_exact_fetch_script" || {
        printf 'not ok - exact PRRTE fetch command is missing: %s\n' \
            "$expected" >&2
        exit 1
    }
done
grep -Fq 'cd prrte-git' "$prrte_exact_build_script" &&
    grep -Fq './autogen.pl' "$prrte_exact_build_script" || {
        printf '%s\n' 'not ok - exact PRRTE checkout does not use the Git Autotools path' >&2
        exit 1
    }
if grep -Eq 'PMIX_LIST_STATIC_INIT|PRTE_ERROR_LOG\(PRTE_ERR_ADDRESSEE_UNKNOWN\)' \
        "$prrte_exact_build_script"; then
    printf '%s\n' 'not ok - exact PRRTE checkout receives a local source edit' >&2
    exit 1
fi
[[ $(grep -Fc "$prrte_sha" "$prrte_exact_fetch_script") -ge 1 ]] ||
    { printf '%s\n' 'not ok - exact PRRTE SHA was not preserved' >&2; exit 1; }
printf '%s\n' 'ok - exact PRRTE mode preserves and builds the unmodified detached checkout'

invalid_index=0
for invalid_commit in \
    invalid-invalid-invalid-invalid-invalid0 \
    0123456789abcdef \
    22820A01E17547DBF1C4F9628EAC327F193CAA45 \
    v5.0 \
    v4.1.0; do
    invalid_index=$((invalid_index + 1))
    invalid_prefix="$test_dir/prrte-invalid-$invalid_index"
    PRRTE_BRANCH=v5.0 PRRTE_COMMIT=$invalid_commit \
        "$rfm" -C ci/openpmix_pr_sysconfig.yaml \
        -c pmix_python_binding/reframe --dry-run \
        --system=frontier:batch --prefix "$invalid_prefix" \
        > "$test_dir/prrte-invalid-$invalid_index-dry-run.out"
    invalid_fetch_script=$(find "$invalid_prefix" -type f \
        -path '*/fetch_prrte_*/rfm_job.sh' -print -quit)
    [[ -n $invalid_fetch_script ]] || {
        printf '%s\n' 'not ok - invalid PRRTE dry run omitted its fetch script' >&2
        exit 1
    }
    invalid_stage=$(dirname -- "$invalid_fetch_script")
    if (cd -- "$invalid_stage" && \
        bash <(sed -n '/^\/bin\/bash -c /,$p' ./rfm_job.sh)) \
            > "$test_dir/prrte-invalid-$invalid_index.out" 2>&1; then
        printf 'not ok - invalid PRRTE commit was accepted: %s\n' \
            "$invalid_commit" >&2
        exit 1
    fi
    grep -Fq \
        'ERROR: PRRTE_COMMIT must be a lowercase exact 40-character SHA' \
        "$test_dir/prrte-invalid-$invalid_index.out" || {
        printf 'not ok - invalid PRRTE commit produced the wrong error: %s\n' \
            "$invalid_commit" >&2
        exit 1
    }
    [[ ! -e $invalid_stage/prrte-git ]] || {
        printf 'not ok - invalid PRRTE commit reached clone: %s\n' \
            "$invalid_commit" >&2
        exit 1
    }
done

invalid_branch_prefix="$test_dir/prrte-invalid-branch"
PRRTE_BRANCH='bad branch' PRRTE_COMMIT=$prrte_sha \
    "$rfm" -C ci/openpmix_pr_sysconfig.yaml \
    -c pmix_python_binding/reframe --dry-run \
    --system=frontier:batch --prefix "$invalid_branch_prefix" \
    > "$test_dir/prrte-invalid-branch-dry-run.out"
invalid_branch_script=$(find "$invalid_branch_prefix" -type f \
    -path '*/fetch_prrte_*/rfm_job.sh' -print -quit)
invalid_branch_stage=$(dirname -- "$invalid_branch_script")
if (cd -- "$invalid_branch_stage" && \
    bash <(sed -n '/^\/bin\/bash -c /,$p' ./rfm_job.sh)) \
        > "$test_dir/prrte-invalid-branch.out" 2>&1; then
    printf '%s\n' 'not ok - invalid PRRTE branch was accepted' >&2
    exit 1
fi
grep -Fq 'ERROR: invalid PRRTE branch name: bad branch' \
    "$test_dir/prrte-invalid-branch.out" || {
    printf '%s\n' 'not ok - invalid PRRTE branch produced the wrong error' >&2
    exit 1
}
[[ ! -e $invalid_branch_stage/prrte-git ]] || {
    printf '%s\n' 'not ok - invalid PRRTE branch reached clone' >&2
    exit 1
}
printf '%s\n' 'ok - malformed PRRTE commits and branch names fail before cloning'

printf '%s\n' '1..8'
