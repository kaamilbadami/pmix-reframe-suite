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
[[ -n $build_script && -n $fetch_script ]] ||
    { printf '%s\n' 'not ok - PMIx fixture scripts are missing' >&2; exit 1; }
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

printf '%s\n' '1..5'
