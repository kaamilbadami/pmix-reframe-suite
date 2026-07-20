#!/bin/bash
set -euo pipefail

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
report_script="${script_dir}/report_github_status.sh"

tmpdir=$(mktemp -d)
trap 'rm -rf -- "$tmpdir"' EXIT

mock_bin="${tmpdir}/bin"
mkdir -p -- "$mock_bin"

cat > "${mock_bin}/curl" <<'MOCK_CURL'
#!/bin/bash
set -euo pipefail

: "${MOCK_CURL_ARGS:?}"
: "${MOCK_CURL_HEADERS:?}"
: "${MOCK_CURL_PAYLOAD:?}"
: "${MOCK_CURL_URL:?}"
: "${MOCK_CURL_CALLED:?}"

: > "$MOCK_CURL_CALLED"
printf '%s\0' "$@" > "$MOCK_CURL_ARGS"
command cat > "$MOCK_CURL_HEADERS"

payload=
data_follows=0
for argument in "$@"; do
    if (( data_follows )); then
        payload=$argument
        data_follows=0
    elif [[ $argument == --data ]]; then
        data_follows=1
    fi
done

printf '%s' "$payload" > "$MOCK_CURL_PAYLOAD"
printf '%s' "${!#}" > "$MOCK_CURL_URL"
exit "${MOCK_CURL_EXIT_STATUS:-0}"
MOCK_CURL
chmod +x -- "${mock_bin}/curl"

args_file="${tmpdir}/curl.args"
headers_file="${tmpdir}/curl.headers"
payload_file="${tmpdir}/curl.payload"
url_file="${tmpdir}/curl.url"
called_file="${tmpdir}/curl.called"
stdout_file="${tmpdir}/report.stdout"
stderr_file="${tmpdir}/report.stderr"

token='test-token-must-only-appear-in-authorization-header'
pipeline_url='https://gitlab.example.test/group/project/-/pipelines/42'
lower_sha='0123456789abcdef0123456789abcdef01234567'
upper_sha='ABCDEF0123456789ABCDEF0123456789ABCDEF01'
context='olcf/frontier-pmix-master'

pass_count=0

fail() {
    printf 'not ok - %s\n' "$1" >&2
    exit 1
}

assert_equal() {
    local expected=$1
    local actual=$2
    local label=$3

    [[ $actual == "$expected" ]] || {
        printf 'expected: %q\nactual:   %q\n' "$expected" "$actual" >&2
        fail "$label"
    }
}

assert_contains() {
    local haystack=$1
    local needle=$2
    local label=$3

    [[ $haystack == *"$needle"* ]] || fail "$label"
}

assert_curl_pair() {
    local option=$1
    local value=$2
    local label=$3
    local index

    for (( index = 0; index + 1 < ${#curl_args[@]}; index++ )); do
        if [[ ${curl_args[index]} == "$option" &&
              ${curl_args[index + 1]} == "$value" ]]; then
            return 0
        fi
    done
    fail "$label"
}

run_success() {
    local state=$1
    local description=$2
    local sha=$3
    local escaped_description=$4
    local label=$5
    local expected_headers
    local expected_payload
    local expected_url
    local actual_headers
    local actual_payload
    local actual_url
    local stdout_text
    local stderr_text
    local argument
    local header_option_count=0

    rm -f -- "$called_file"
    : > "$args_file"
    : > "$headers_file"
    : > "$payload_file"
    : > "$url_file"

    PATH="${mock_bin}:${PATH}" \
    MOCK_CURL_ARGS=$args_file \
    MOCK_CURL_HEADERS=$headers_file \
    MOCK_CURL_PAYLOAD=$payload_file \
    MOCK_CURL_URL=$url_file \
    MOCK_CURL_CALLED=$called_file \
    MOCK_CURL_EXIT_STATUS=0 \
    GITHUB_STATUS_TOKEN=$token \
    CI_COMMIT_SHA=$sha \
    CI_PIPELINE_URL=$pipeline_url \
        bash "$report_script" "$state" "$description" \
        > "$stdout_file" 2> "$stderr_file" || fail "$label exited unsuccessfully"

    [[ -f $called_file ]] || fail "$label did not invoke curl"
    mapfile -d '' -t curl_args < "$args_file"

    assert_curl_pair --request POST "$label uses POST"
    assert_curl_pair --header @- "$label uses --header @-"

    for argument in "${curl_args[@]}"; do
        [[ $argument != --location ]] || fail "$label uses --location"
        [[ $argument != *"$token"* ]] || fail "$label exposes token in curl arguments"
        if [[ $argument == --header ]]; then
            header_option_count=$((header_option_count + 1))
        fi
    done
    assert_equal 1 "$header_option_count" "$label has one stdin header source"

    expected_headers=$'Accept: application/vnd.github+json\nAuthorization: Bearer '
    expected_headers+="$token"
    expected_headers+=$'\nX-GitHub-Api-Version: 2026-03-10\nContent-Type: application/json'
    actual_headers=$(< "$headers_file")
    assert_equal "$expected_headers" "$actual_headers" "$label sends expected headers through stdin"

    expected_payload='{"state":"'"$state"'","target_url":"'"$pipeline_url"'","description":"'"$escaped_description"'","context":"'"$context"'"}'
    actual_payload=$(< "$payload_file")
    assert_equal "$expected_payload" "$actual_payload" "$label sends expected JSON payload"
    assert_curl_pair --data "$expected_payload" "$label passes expected request payload"
    [[ $actual_payload != *"$token"* ]] || fail "$label exposes token in payload"

    expected_url="https://api.github.com/repos/kaamilbadami/pmix-reframe-suite/statuses/${sha}"
    actual_url=$(< "$url_file")
    assert_equal "$expected_url" "$actual_url" "$label targets expected API URL"
    [[ $actual_url != *"$token"* ]] || fail "$label exposes token in API URL"

    stdout_text=$(< "$stdout_file")
    stderr_text=$(< "$stderr_file")
    [[ $stdout_text != *"$token"* ]] || fail "$label exposes token in stdout"
    [[ $stderr_text != *"$token"* ]] || fail "$label exposes token in stderr"

    printf 'ok - %s\n' "$label"
    pass_count=$((pass_count + 1))
}

run_curl_failure() {
    local label='mocked curl failure'
    local status
    local stdout_text
    local stderr_text
    local actual_payload
    local actual_url
    local argument

    rm -f -- "$called_file"
    : > "$args_file"
    : > "$headers_file"
    : > "$payload_file"
    : > "$url_file"

    set +e
    PATH="${mock_bin}:${PATH}" \
    MOCK_CURL_ARGS=$args_file \
    MOCK_CURL_HEADERS=$headers_file \
    MOCK_CURL_PAYLOAD=$payload_file \
    MOCK_CURL_URL=$url_file \
    MOCK_CURL_CALLED=$called_file \
    MOCK_CURL_EXIT_STATUS=22 \
    GITHUB_STATUS_TOKEN=$token \
    CI_COMMIT_SHA=$lower_sha \
    CI_PIPELINE_URL=$pipeline_url \
        bash "$report_script" success 'curl failure description' \
        > "$stdout_file" 2> "$stderr_file"
    status=$?
    set -e

    assert_equal 22 "$status" "$label propagates a nonzero status"
    [[ -f $called_file ]] || fail "$label did not invoke curl"

    stdout_text=$(< "$stdout_file")
    stderr_text=$(< "$stderr_file")
    actual_payload=$(< "$payload_file")
    actual_url=$(< "$url_file")
    mapfile -d '' -t curl_args < "$args_file"

    [[ $stdout_text != *"$token"* ]] || fail "$label exposes token in stdout"
    [[ $stderr_text != *"$token"* ]] || fail "$label exposes token in stderr"
    [[ $actual_payload != *"$token"* ]] || fail "$label exposes token in payload"
    [[ $actual_url != *"$token"* ]] || fail "$label exposes token in API URL"
    for argument in "${curl_args[@]}"; do
        [[ $argument != *"$token"* ]] || fail "$label exposes token in curl arguments"
    done

    printf 'ok - %s\n' "$label"
    pass_count=$((pass_count + 1))
}

run_rejection() {
    local label=$1
    local expected_error=$2
    local token_value=$3
    local sha_value=$4
    local url_value=$5
    shift 5

    local -a unset_args=()
    local -a environment=(
        "PATH=${mock_bin}:${PATH}"
        "MOCK_CURL_ARGS=${args_file}"
        "MOCK_CURL_HEADERS=${headers_file}"
        "MOCK_CURL_PAYLOAD=${payload_file}"
        "MOCK_CURL_URL=${url_file}"
        "MOCK_CURL_CALLED=${called_file}"
    )
    local status
    local stderr_text

    if [[ $token_value == __UNSET__ ]]; then
        unset_args+=(-u GITHUB_STATUS_TOKEN)
    else
        environment+=("GITHUB_STATUS_TOKEN=${token_value}")
    fi
    if [[ $sha_value == __UNSET__ ]]; then
        unset_args+=(-u CI_COMMIT_SHA)
    else
        environment+=("CI_COMMIT_SHA=${sha_value}")
    fi
    if [[ $url_value == __UNSET__ ]]; then
        unset_args+=(-u CI_PIPELINE_URL)
    else
        environment+=("CI_PIPELINE_URL=${url_value}")
    fi

    rm -f -- "$called_file"
    set +e
    env "${unset_args[@]}" "${environment[@]}" \
        bash "$report_script" "$@" > "$stdout_file" 2> "$stderr_file"
    status=$?
    set -e

    assert_equal 2 "$status" "$label exits with status 2"
    [[ ! -e $called_file ]] || fail "$label invoked curl"
    stderr_text=$(< "$stderr_file")
    assert_contains "$stderr_text" "$expected_error" "$label reports expected error"

    printf 'ok - %s\n' "$label"
    pass_count=$((pass_count + 1))
}

run_success pending 'pending description' "$lower_sha" \
    'pending description' 'pending state with lowercase SHA'
run_success success 'success description' "$upper_sha" \
    'success description' 'success state with uppercase SHA'
run_success failure 'failure description' "$lower_sha" \
    'failure description' 'failure state'
run_success error 'error description' "$upper_sha" \
    'error description' 'error state'
run_success success 'description says "hello"' "$lower_sha" \
    'description says \"hello\"' 'description containing quotes'
run_success success 'path\to\artifact' "$lower_sha" \
    'path\\to\\artifact' 'description containing backslashes'
run_success success $'control:\001\t\n\r\b\f' "$lower_sha" \
    'control:\u0001\u0009\u000a\u000d\u0008\u000c' \
    'description containing control characters'

run_curl_failure

run_rejection 'missing all arguments' 'usage:' \
    "$token" "$lower_sha" "$pipeline_url"
run_rejection 'missing description argument' 'usage:' \
    "$token" "$lower_sha" "$pipeline_url" success
run_rejection 'too many arguments' 'usage:' \
    "$token" "$lower_sha" "$pipeline_url" success description extra
run_rejection 'unsupported state' 'error: unsupported GitHub status state' \
    "$token" "$lower_sha" "$pipeline_url" unsupported description
run_rejection 'missing GITHUB_STATUS_TOKEN' 'error: GITHUB_STATUS_TOKEN is required' \
    __UNSET__ "$lower_sha" "$pipeline_url" success description
run_rejection 'missing CI_COMMIT_SHA' \
    'error: CI_COMMIT_SHA must be exactly 40 hexadecimal characters' \
    "$token" __UNSET__ "$pipeline_url" success description
run_rejection 'malformed CI_COMMIT_SHA' \
    'error: CI_COMMIT_SHA must be exactly 40 hexadecimal characters' \
    "$token" 'not-a-sha' "$pipeline_url" success description
run_rejection 'non-40-character CI_COMMIT_SHA' \
    'error: CI_COMMIT_SHA must be exactly 40 hexadecimal characters' \
    "$token" '0123456789abcdef' "$pipeline_url" success description
run_rejection 'missing CI_PIPELINE_URL' \
    'error: CI_PIPELINE_URL must be a valid HTTP(S) URL' \
    "$token" "$lower_sha" __UNSET__ success description
run_rejection 'invalid CI_PIPELINE_URL scheme' \
    'error: CI_PIPELINE_URL must be a valid HTTP(S) URL' \
    "$token" "$lower_sha" 'ftp://gitlab.example.test/pipeline/42' success description
run_rejection 'invalid CI_PIPELINE_URL whitespace' \
    'error: CI_PIPELINE_URL must be a valid HTTP(S) URL' \
    "$token" "$lower_sha" 'https://gitlab.example.test/bad url' success description

printf '1..%d\n' "$pass_count"
