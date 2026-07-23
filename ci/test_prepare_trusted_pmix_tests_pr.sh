#!/bin/bash
set -euo pipefail

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)

python3.11 - "$script_dir/prepare_trusted_pmix_tests_pr.sh" \
    "$script_dir/pmix_tests_pr_artifacts.py" <<'PY'
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


wrapper_source = Path(sys.argv[1]).resolve()
records_source = Path(sys.argv[2]).resolve()
sha = "0123456789abcdef0123456789abcdef01234567"
changed_sha = "89abcdef0123456789abcdef0123456789abcdef"
pipeline_id = "456"
stale_pipeline_id = "455"
read_token = "mock-read-token-never-print"
status_token = "mock-status-token-never-print"
passed_count = 0
case_count = 0


def check(condition, message):
    if not condition:
        raise AssertionError(message)


def passed(message):
    global passed_count
    passed_count += 1
    print(f"ok - {message}")


metadata_stub = r'''#!/bin/bash
set -euo pipefail
output=$2
mkdir -m 700 -p -- "$output"
printf '{"title":"$(touch raw-title-executed)","head":{"repo":{"clone_url":"https://evil.invalid/repo.git"}}}\n' > "$output/pr.json"
printf 'PR_ELIGIBLE=1\nPR_NUMBER=42\nPR_AUTHOR=%s\nPR_HEAD_SHA=%s\nPR_HEAD_REPOSITORY=%s\nPR_BASE_REPOSITORY=%s\nPR_FROM_FORK=%s\n' \
    "$STUB_AUTHOR" "$STUB_INITIAL_SHA" "$STUB_HEAD_REPOSITORY" \
    "$STUB_BASE_REPOSITORY" "$STUB_FROM_FORK" > "$output/trusted-pr.env"
'''

fetch_stub = r'''#!/bin/bash
set -euo pipefail
printf '%s\0' "$@" > fetch.args
output=
while (( $# )); do
    if [[ $1 == --output ]]; then output=$2; break; fi
    shift
done
printf '{"head":{"ref":"refs/tags/evil","repo":{"ssh_url":"ssh://evil.invalid/repo"}}}\n' > "$output"
'''

checker_stub = r'''#!/bin/bash
set -euo pipefail
printf '%s\0' "$@" > checker.args
output=
expected=
while (( $# )); do
    case $1 in
        --output) output=$2; shift 2 ;;
        --expected-head-sha) expected=$2; shift 2 ;;
        *) shift ;;
    esac
done
if [[ ${STUB_CHANGED_HEAD:-0} == 1 ]]; then
    exit 5
fi
[[ $expected == "$STUB_INITIAL_SHA" ]] || exit 17
printf 'PR_ELIGIBLE=1\nPR_NUMBER=42\nPR_AUTHOR=%s\nPR_HEAD_SHA=%s\nPR_HEAD_REPOSITORY=%s\nPR_BASE_REPOSITORY=%s\nPR_FROM_FORK=%s\n' \
    "$STUB_AUTHOR" "$STUB_REVALIDATED_SHA" "$STUB_HEAD_REPOSITORY" \
    "$STUB_BASE_REPOSITORY" "$STUB_FROM_FORK" > "$output"
'''

reporter_stub = r'''#!/bin/bash
set -euo pipefail
[[ -n ${GITHUB_STATUS_TOKEN:-} ]] || exit 19
printf '%s\0' "$@" > reporter.args
exit "${STUB_REPORTER_STATUS:-0}"
'''


class Case:
    def __init__(self, **updates):
        global case_count
        case_count += 1
        self.root = Path(temporary.name) / f"case-{case_count}"
        self.ci = self.root / "ci"
        self.bin = self.root / "bin"
        self.ci.mkdir(parents=True)
        self.bin.mkdir()
        python_path = self.bin / "fixed-python"
        python_path.symlink_to(shutil.which("python3.11"))
        wrapper_text = wrapper_source.read_text().replace(
            "/lustre/orion/scratch/kbadami/gen243/reframe_practice/pmix-py310/bin/python",
            str(python_path),
        )
        (self.ci / wrapper_source.name).write_text(wrapper_text)
        shutil.copy2(records_source, self.ci / records_source.name)
        helpers = {
            "run_pmix_tests_pr_metadata_pilot.sh": metadata_stub,
            "fetch_pmix_tests_pr.py": fetch_stub,
            "check_trusted_pmix_tests_pr.py": checker_stub,
            "report_pmix_tests_pr_status.sh": reporter_stub,
        }
        for name, content in helpers.items():
            path = self.ci / name
            path.write_text(content)
            path.chmod(0o755)
        self.environment = os.environ.copy()
        self.environment.update({
            "PATH": f"{self.bin}:{self.environment['PATH']}",
            "GITHUB_PR_READ_TOKEN": read_token,
            "GITHUB_STATUS_TOKEN": status_token,
            "CI_PIPELINE_ID": pipeline_id,
            "STUB_AUTHOR": "kaamilbadami",
            "STUB_INITIAL_SHA": sha,
            "STUB_REVALIDATED_SHA": sha,
            "STUB_HEAD_REPOSITORY": "kaamilbadami/pmix-tests",
            "STUB_BASE_REPOSITORY": "kaamilbadami/pmix-tests",
            "STUB_FROM_FORK": "0",
            "STUB_CHANGED_HEAD": "0",
            "STUB_REPORTER_STATUS": "0",
        })
        self.environment.update({key: str(value) for key, value in updates.items()})

    def run(self, arguments=("42",)):
        completed = subprocess.run(
            ["bash", f"ci/{wrapper_source.name}", *arguments], cwd=self.root,
            env=self.environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            check=False,
        )
        check(read_token.encode() not in completed.stdout + completed.stderr,
              "read token leaked to output")
        check(status_token.encode() not in completed.stdout + completed.stderr,
              "status token leaked to output")
        return completed


with tempfile.TemporaryDirectory() as temporary_name:
    temporary = type("Temporary", (), {"name": temporary_name})()

    case = Case()
    completed = case.run()
    check(completed.returncode == 0, "trusted preparation did not succeed")
    expected = (
        "PMIX_TESTS_PR_PREPARATION_VERSION=2\n"
        f"CI_PIPELINE_ID={pipeline_id}\n"
        "PR_REPOSITORY=kaamilbadami/pmix-tests\n"
        "PR_NUMBER=42\n"
        "PR_AUTHOR=kaamilbadami\n"
        f"PR_HEAD_SHA={sha}\n"
        "PR_FROM_FORK=0\n"
        "PREPARATION_RESULT=ready\n"
    )
    check((case.root / "ci-pr-preparation/preparation.env").read_text() == expected,
          "published preparation schema changed")
    reporter_args = [item.decode() for item in
                     (case.root / "reporter.args").read_bytes().split(b"\0")[:-1]]
    check(reporter_args == [sha, "pending", "Frontier PMIx tests PR check is running"],
          "pending status did not target the original SHA")
    check(not (case.root / "raw-title-executed").exists(), "raw PR JSON was executed")
    passed("same-repository metadata is revalidated and pending targets the exact original SHA")
    successful_case = case

    case = Case()
    stale_directory = case.root / "ci-pr-preparation"
    stale_directory.mkdir()
    (stale_directory / "preparation.env").write_text(
        "PMIX_TESTS_PR_PREPARATION_VERSION=2\n"
        f"CI_PIPELINE_ID={stale_pipeline_id}\n"
        "PR_REPOSITORY=kaamilbadami/pmix-tests\n"
        "PR_NUMBER=99\n"
        "PR_AUTHOR=rhc54\n"
        f"PR_HEAD_SHA={changed_sha}\n"
        "PR_FROM_FORK=0\n"
        "PREPARATION_RESULT=ready\n"
    )
    case.environment.pop("GITHUB_PR_READ_TOKEN")
    completed = case.run()
    check(completed.returncode == 2 and not stale_directory.exists(),
          "early preparation failure retained another pipeline's artifact")
    case = Case()
    target = case.root / "stale-preparation-target"
    target.mkdir()
    (target / "sentinel").write_text("preserve\n")
    stale_link = case.root / "ci-pr-preparation"
    stale_link.symlink_to(target.name, target_is_directory=True)
    case.environment.pop("GITHUB_PR_READ_TOKEN")
    completed = case.run()
    check(completed.returncode == 2 and not stale_link.exists()
          and (target / "sentinel").read_text() == "preserve\n",
          "preparation cleanup followed a stale output symlink")
    passed("preparation removes a stale output directory before any early failure")

    checker_args = [item.decode() for item in
                    (successful_case.root / "checker.args").read_bytes().split(b"\0")[:-1]]
    check(checker_args[checker_args.index("--expected-head-sha") + 1] == sha,
          "revalidation did not use the selected SHA")
    check("evil.invalid" not in "\n".join(checker_args),
          "PR-controlled URL reached a trusted command")
    passed("second metadata validation requires the saved SHA and ignores PR URLs and refs")

    for updates, label in (
        ({"STUB_AUTHOR": "unknown"}, "unapproved author"),
        ({"STUB_HEAD_REPOSITORY": "attacker/pmix-tests", "STUB_FROM_FORK": "1"}, "fork"),
        ({"STUB_BASE_REPOSITORY": "attacker/pmix-tests"}, "wrong base"),
        ({"STUB_INITIAL_SHA": sha.upper(), "STUB_REVALIDATED_SHA": sha.upper()}, "uppercase SHA"),
    ):
        case = Case(**updates)
        completed = case.run()
        check(completed.returncode == 2 and not (case.root / "fetch.args").exists(),
              f"{label} reached revalidation")
    passed("author, same-repository, base-repository, and lowercase-SHA policy precede revalidation")

    case = Case(STUB_CHANGED_HEAD=1, STUB_REVALIDATED_SHA=changed_sha)
    completed = case.run()
    check(completed.returncode == 5 and not (case.root / "reporter.args").exists(),
          "changed head reached pending status")
    preparation_text = (case.root / "ci-pr-preparation/preparation.env").read_text()
    check(f"PR_HEAD_SHA={sha}\n" in preparation_text
          and f"CI_PIPELINE_ID={pipeline_id}\n" in preparation_text
          and preparation_text.endswith("PREPARATION_RESULT=error\n"),
          "changed head lost the trusted original error record")
    passed("changed heads stop before publication while preserving the original SHA for final error")

    case = Case(STUB_REPORTER_STATUS=22)
    completed = case.run()
    check(completed.returncode == 22, "pending delivery failure did not fail closed")
    check((case.root / "ci-pr-preparation/preparation.env").read_text().endswith(
        "PREPARATION_RESULT=error\n"), "pending failure published a ready record")
    passed("pending-status delivery must succeed before preparation becomes ready")

    for missing in ("GITHUB_PR_READ_TOKEN", "GITHUB_STATUS_TOKEN"):
        case = Case()
        case.environment.pop(missing)
        completed = case.run()
        check(completed.returncode == 2 and not (case.root / "ci-pr-preparation").exists(),
              f"missing {missing} reached metadata")
    for arguments in ((), ("0",), ("042",), ("refs/heads/main",)):
        case = Case()
        completed = case.run(arguments)
        check(completed.returncode == 2 and not (case.root / "ci-pr-preparation").exists(),
              f"invalid PR number reached metadata: {arguments}")
    passed("both tokens and a canonical positive PR number are required before metadata access")

    for invalid_pipeline in ("", "0", "0456", "not-numeric"):
        case = Case(CI_PIPELINE_ID=invalid_pipeline)
        completed = case.run()
        check(completed.returncode == 2
              and not (case.root / "ci-pr-preparation").exists(),
              f"invalid pipeline ID reached metadata: {invalid_pipeline!r}")
    passed("preparation requires the current canonical GitLab pipeline ID")

    case = Case()
    completed = case.run()
    for path in case.root.rglob("*"):
        if path.is_file():
            content = path.read_bytes()
            check(read_token.encode() not in content and status_token.encode() not in content,
                  f"token appeared in {path}")
    passed("preparation logs and artifacts contain no token material")

print(f"1..{passed_count}")
PY
