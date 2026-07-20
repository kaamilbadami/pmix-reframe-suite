#!/bin/bash
set -euo pipefail

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
gate_script="${script_dir}/should_run_pmix_suite.sh"
test_dir=$(mktemp -d)
trap 'rm -rf -- "$test_dir"' EXIT

mock_bin="${test_dir}/bin"
mkdir -p -- "$mock_bin"

cat > "${mock_bin}/git" <<'MOCK_GIT'
#!/bin/bash
set -euo pipefail

[[ $# == 3 && $1 == ls-remote &&
   $2 == https://github.com/openpmix/openpmix.git &&
   $3 == refs/heads/master ]] || exit 2
printf '%s\trefs/heads/master\n' "${MOCK_PMIX_SHA:?}"
MOCK_GIT

cat > "${mock_bin}/date" <<'MOCK_DATE'
#!/bin/bash
set -euo pipefail

[[ $# == 2 && $1 == -u && $2 == +%s ]] || exit 2
printf '%s\n' "${MOCK_NOW_EPOCH:?}"
MOCK_DATE

chmod +x -- "${mock_bin}/git" "${mock_bin}/date"

pmix_sha=1111111111111111111111111111111111111111
other_pmix_sha=2222222222222222222222222222222222222222
suite_sha=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
other_suite_sha=bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb
now_epoch=200000
fresh_epoch=199999
expired_epoch=113600
pass_count=0

fail() {
    printf 'not ok - %s\n' "$1" >&2
    exit 1
}

valid_state() {
    printf 'PMIX_COMMIT=%s\nSUITE_COMMIT=%s\nLAST_SUCCESS_EPOCH=%s\n' \
        "$1" "$2" "$3"
}

run_case() {
    local label=$1
    local pipeline_source=$2
    local current_pmix_sha=$3
    local current_suite_sha=$4
    local state_text=$5
    local expected_run=$6
    local case_dir="${test_dir}/case-${pass_count}"
    local state_file="${case_dir}/state.env"
    local decision_file="${case_dir}/decision.env"

    mkdir -p -- "$case_dir"
    printf '%s\n' "$state_text" > "$state_file"

    PATH="${mock_bin}:${PATH}" \
    MOCK_PMIX_SHA=$current_pmix_sha \
    MOCK_NOW_EPOCH=$now_epoch \
    CI_PIPELINE_SOURCE=$pipeline_source \
    CI_COMMIT_SHA=$current_suite_sha \
    PMIX_STATE_FILE=$state_file \
        bash "$gate_script" "$decision_file" > "${case_dir}/output"

    unset PMIX_COMMIT PMIX_RUN_SUITE PMIX_RUN_REASON
    source "$decision_file"
    [[ $PMIX_COMMIT == "$current_pmix_sha" ]] ||
        fail "$label recorded the wrong OpenPMIx SHA"
    [[ $PMIX_RUN_SUITE == "$expected_run" ]] ||
        fail "$label produced PMIX_RUN_SUITE=${PMIX_RUN_SUITE}"

    printf 'ok - %s\n' "$label"
    pass_count=$((pass_count + 1))
}

matching_state=$(valid_state "$pmix_sha" "$suite_sha" "$fresh_epoch")

run_case 'manual web pipeline always runs' \
    web "$pmix_sha" "$suite_sha" "$matching_state" 1

run_case 'matching PMIx and suite SHAs skip' \
    schedule "$pmix_sha" "$suite_sha" "$matching_state" 0

run_case 'changed OpenPMIx SHA runs' \
    schedule "$pmix_sha" "$suite_sha" \
    "$(valid_state "$other_pmix_sha" "$suite_sha" "$fresh_epoch")" 1

run_case 'changed suite SHA runs' \
    schedule "$pmix_sha" "$suite_sha" \
    "$(valid_state "$pmix_sha" "$other_suite_sha" "$fresh_epoch")" 1

run_case 'expired state runs' \
    schedule "$pmix_sha" "$suite_sha" \
    "$(valid_state "$pmix_sha" "$suite_sha" "$expired_epoch")" 1

old_state=$(printf 'PMIX_COMMIT=%s\nLAST_SUCCESS_EPOCH=%s\n' \
    "$pmix_sha" "$fresh_epoch")
run_case 'old two-line state runs' \
    schedule "$pmix_sha" "$suite_sha" "$old_state" 1

printf '1..%d\n' "$pass_count"
