#!/bin/bash
set -euo pipefail

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
reporter="$script_dir/report_pmix_tests_pr_status.sh"

python3 - "$reporter" <<'PY'
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile


reporter = Path(__import__("sys").argv[1]).resolve()
lower_sha = "0123456789abcdef0123456789abcdef01234567"
upper_sha = "ABCDEF0123456789ABCDEF0123456789ABCDEF01"
token = "mock-status-token-never-log-this"
pipeline_url = "https://gitlab.example.test/group/project/-/pipelines/42"
pass_count = 0


def check(condition, message):
    if not condition:
        raise AssertionError(message)


def passed(message):
    global pass_count
    pass_count += 1
    print(f"ok - {message}")


temporary = tempfile.TemporaryDirectory()
root = Path(temporary.name)
mock_bin = root / "bin"
mock_bin.mkdir()
curl = mock_bin / "curl"
curl.write_text(r'''#!/bin/bash
set -euo pipefail
printf '%s\0' "$@" > "$MOCK_ARGS"
command cat > "$MOCK_HEADERS"
payload=
previous=
for argument in "$@"; do
    if [[ $previous == --data ]]; then payload=$argument; fi
    previous=$argument
done
printf '%s' "$payload" > "$MOCK_PAYLOAD"
printf '%s' "${!#}" > "$MOCK_URL"
exit "${MOCK_CURL_STATUS:-0}"
''')
curl.chmod(0o755)
test_reporter = root / "report_pmix_tests_pr_status.sh"
test_reporter.write_text(reporter.read_text().replace("/usr/bin/curl", str(curl)))


def run(arguments, *, token_value=token, url=pipeline_url, curl_status=0):
    case = root / f"case-{len(list(root.glob('case-*'))) + 1}"
    case.mkdir()
    paths = {name: case / name for name in (
        "args", "headers", "payload", "url", "stdout", "stderr"
    )}
    environment = os.environ.copy()
    environment["PATH"] = f"{mock_bin}:{environment['PATH']}"
    environment.update({
        "MOCK_ARGS": str(paths["args"]),
        "MOCK_HEADERS": str(paths["headers"]),
        "MOCK_PAYLOAD": str(paths["payload"]),
        "MOCK_URL": str(paths["url"]),
        "MOCK_CURL_STATUS": str(curl_status),
    })
    if token_value is None:
        environment.pop("GITHUB_STATUS_TOKEN", None)
    else:
        environment["GITHUB_STATUS_TOKEN"] = token_value
    if url is None:
        environment.pop("CI_PIPELINE_URL", None)
    else:
        environment["CI_PIPELINE_URL"] = url
    completed = subprocess.run(
        ["bash", str(test_reporter), *arguments], env=environment,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
    )
    paths["stdout"].write_bytes(completed.stdout)
    paths["stderr"].write_bytes(completed.stderr)
    return completed, paths


for state, sha, description in (
    ("pending", lower_sha, "Frontier PMIx tests PR check is running"),
    ("success", lower_sha, "description with a \"quote\""),
    ("failure", lower_sha, r"description with a backslash \\"),
    ("error", lower_sha, "Frontier PMIx tests PR check ended unexpectedly"),
):
    completed, paths = run([sha, state, description])
    check(completed.returncode == 0, f"{state} reporter failed")
    arguments = paths["args"].read_bytes().split(b"\0")[:-1]
    decoded = [argument.decode() for argument in arguments]
    expected_url = (
        "https://api.github.com/repos/kaamilbadami/pmix-tests/statuses/" + sha
    )
    check(paths["url"].read_text() == expected_url, "fixed API target changed")
    payload = json.loads(paths["payload"].read_text())
    check(payload == {
        "state": state,
        "target_url": pipeline_url,
        "description": description,
        "context": "olcf/frontier-pmix-tests-pr",
    }, "status payload changed")
    check("--max-redirs" in decoded and decoded[decoded.index("--max-redirs") + 1] == "0",
          "redirect rejection is absent")
    check("--location" not in decoded, "reporter follows redirects")
    check(all(token not in argument for argument in decoded),
          "token appeared in curl arguments")
    check(token not in paths["url"].read_text(), "token appeared in URL")
    check(token not in paths["payload"].read_text(), "token appeared in payload")
    check(token.encode() not in completed.stdout + completed.stderr,
          "token appeared in reporter output")
passed("all four states use the exact PR repository, SHA, context, and pipeline URL")

completed, _ = run([lower_sha, "success", "curl failure"], curl_status=22)
check(completed.returncode == 22, "curl failure was not propagated")
passed("status delivery failures propagate")

rejections = (
    ([], token, pipeline_url),
    (["short", "success", "description"], token, pipeline_url),
    (["g" * 40, "success", "description"], token, pipeline_url),
    ([upper_sha, "success", "description"], token, pipeline_url),
    ([lower_sha, "unsupported", "description"], token, pipeline_url),
    ([lower_sha, "success", "description"], None, pipeline_url),
    ([lower_sha, "success", "description"], token, None),
    ([lower_sha, "success", "description"], token, "http://gitlab.example/pipelines/1"),
    ([lower_sha, "success", "description"], token, "https://gitlab.example/bad"),
)
for arguments, token_value, url in rejections:
    completed, paths = run(arguments, token_value=token_value, url=url)
    check(completed.returncode == 2, f"invalid reporter input passed: {arguments}")
    check(not paths["args"].exists(), "invalid reporter input invoked curl")
passed("arguments, exact SHA, state, token, and GitLab pipeline URL are validated")

source = reporter.read_text()
check("kaamilbadami/pmix-tests/statuses/${pr_head_sha}" in source,
      "production reporter lacks the fixed repository endpoint")
check("olcf/frontier-pmix-tests-pr" in source, "production context changed")
for forbidden in ("GITHUB_REPOSITORY", "GITHUB_API", "--location"):
    check(forbidden not in source, f"reporter accepts unsafe destination input: {forbidden}")
passed("production source has no configurable repository or redirect behavior")

temporary.cleanup()
print(f"1..{pass_count}")
PY
