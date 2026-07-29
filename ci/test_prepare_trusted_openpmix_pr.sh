#!/bin/bash
set -euo pipefail

script_dir=$(cd -- "${BASH_SOURCE[0]%/*}" && pwd -P)
python3 - "$script_dir" <<'PY'
import copy
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


source_ci = Path(sys.argv[1])
tests = 0
token = "mock-openpmix-read-token-never-print"


def passed(message):
    global tests
    tests += 1
    print("ok - " + message)


def check(condition, message):
    if not condition:
        raise AssertionError(message)


fetch_stub = r'''#!/usr/bin/env python3
import argparse
from pathlib import Path
import sys

parser = argparse.ArgumentParser()
parser.add_argument("--pr-number", required=True)
parser.add_argument("--output", required=True)
options = parser.parse_args()
mock = Path("mock")
counter_path = mock / "counter"
count = int(counter_path.read_text()) if counter_path.exists() else 0
count += 1
counter_path.write_text(str(count))
with (mock / "calls").open("a") as stream:
    stream.write("{} {}\n".format(options.pr_number, options.output))
if count == 2 and (mock / "fail-second").exists():
    raise SystemExit(3)
response = mock / "response-{}.json".format(count)
if not response.is_file():
    raise SystemExit(3)
output = Path(options.output)
output.write_bytes(response.read_bytes())
'''


with tempfile.TemporaryDirectory(prefix="test-openpmix-prepare-") as temporary:
    root = Path(temporary)
    ci = root / "ci"
    ci.mkdir()
    for name in (
            "prepare_trusted_openpmix_pr.sh",
            "check_trusted_openpmix_pr.py",
            "openpmix_pr_artifacts.py"):
        shutil.copy2(str(source_ci / name), str(ci / name))
    (ci / "fetch_openpmix_pr.py").write_text(fetch_stub)
    ralph = json.loads((
        source_ci / "fixtures/openpmix_pr/rhc54_verified_fork_open.json"
    ).read_text())

    def run_case(first, second=None, *, pr_number="4028",
                 pipeline_id="9001", include_token=True, fail_second=False,
                 expected_sha=None):
        mock = root / "mock"
        if mock.exists():
            shutil.rmtree(str(mock))
        mock.mkdir()
        (mock / "response-1.json").write_text(json.dumps(first))
        if second is not None:
            (mock / "response-2.json").write_text(json.dumps(second))
        if fail_second:
            (mock / "fail-second").write_text("1")
        environment = os.environ.copy()
        environment["CI_PIPELINE_ID"] = pipeline_id
        if include_token:
            environment["GITHUB_PR_READ_TOKEN"] = token
        else:
            environment.pop("GITHUB_PR_READ_TOKEN", None)
        command = ["/bin/bash", "ci/prepare_trusted_openpmix_pr.sh",
                   pr_number]
        if expected_sha is not None:
            command.append(expected_sha)
        completed = subprocess.run(
            command,
            cwd=str(root),
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        return completed

    completed = run_case(ralph, ralph)
    check(completed.returncode == 0, completed.stderr.decode())
    preparation = root / "ci-openpmix-pr-preparation/preparation.env"
    content = preparation.read_text()
    check("PREPARATION_RESULT=ready\n" in content,
          "valid preparation was not ready")
    check("CI_PIPELINE_ID=9001\n" in content and
          "PR_NUMBER=4028\n" in content and
          "PR_AUTHOR=rhc54\n" in content and
          "PR_BASE_REPOSITORY=openpmix/openpmix\n" in content and
          "PR_HEAD_REPOSITORY=rhc54/openpmix\n" in content and
          "PR_HEAD_SHA=5f08dede7495ce1b332691b3ac7eaf6478a979b9\n"
          in content and
          "PR_SOURCE_POLICY=verified-author-fork\n" in content,
          "ready preparation lost identity")
    calls = (root / "mock/calls").read_text().splitlines()
    check(calls == [
        "4028 ci-openpmix-pr-preparation/private-work/initial/pr.json",
        "4028 ci-openpmix-pr-preparation/private-work/revalidated/pr.json",
    ], "preparation did not fetch exactly twice")
    passed("ready publication requires two fixed-number metadata fetches")

    selected_sha = ralph["head"]["sha"]
    completed = run_case(
        ralph, ralph, expected_sha=selected_sha)
    check(completed.returncode == 0 and
          "PREPARATION_RESULT=ready\n" in preparation.read_text(),
          "matching discovery SHA was rejected")
    completed = run_case(
        ralph, ralph, expected_sha="0" * 40)
    check(completed.returncode != 0 and not preparation.exists() and
          (root / "mock/counter").read_text() == "1",
          "stale discovery SHA reached ready publication")
    passed("automatic discovery SHA is checked on the first authoritative fetch")

    changed_sha = copy.deepcopy(ralph)
    changed_sha["head"]["sha"] = "0" * 40
    completed = run_case(ralph, changed_sha)
    check(completed.returncode != 0, "changed head SHA became ready")
    content = preparation.read_text()
    check("PREPARATION_RESULT=error\n" in content and
          "PR_HEAD_SHA=5f08dede7495ce1b332691b3ac7eaf6478a979b9\n"
          in content,
          "changed head did not retain fail-closed original identity")
    passed("a changed head SHA leaves only the original error preparation")

    changed_author = copy.deepcopy(ralph)
    changed_author["user"]["login"] = "kaamilbadami"
    changed_author["head"]["repo"]["full_name"] = "openpmix/openpmix"
    completed = run_case(ralph, changed_author)
    check(completed.returncode != 0, "changed author became ready")
    check("PREPARATION_RESULT=error\n" in preparation.read_text(),
          "changed author removed fail-closed record")
    passed("author changes are rejected during authoritative revalidation")

    changed_repository = copy.deepcopy(ralph)
    changed_repository["head"]["repo"]["full_name"] = "openpmix/openpmix"
    completed = run_case(ralph, changed_repository)
    check(completed.returncode != 0, "changed head repository became ready")
    check("PREPARATION_RESULT=error\n" in preparation.read_text(),
          "changed repository removed fail-closed record")
    passed("head-repository changes are rejected during revalidation")

    for label, mutation in (
            ("closed", {"state": "closed"}),
            ("draft", {"draft": True})):
        changed = copy.deepcopy(ralph)
        changed.update(mutation)
        completed = run_case(ralph, changed)
        check(completed.returncode != 0,
              "{} PR became ready".format(label))
        check("PREPARATION_RESULT=error\n" in preparation.read_text(),
              "{} PR removed fail-closed identity".format(label))
    passed("closed or draft state on the second response prevents readiness")

    completed = run_case(ralph, ralph, fail_second=True)
    check(completed.returncode != 0, "second fetch failure became ready")
    check("PREPARATION_RESULT=error\n" in preparation.read_text(),
          "second fetch failure removed fail-closed identity")
    passed("second-fetch failure cannot publish a ready record")

    untrusted = copy.deepcopy(ralph)
    untrusted["user"]["login"] = "not-allowlisted"
    untrusted["head"]["repo"]["full_name"] = "openpmix/openpmix"
    completed = run_case(untrusted)
    check(completed.returncode != 0, "untrusted first response succeeded")
    check(not preparation.exists(),
          "untrusted first response published preparation")
    check((root / "mock/counter").read_text() == "1",
          "untrusted first response was fetched again")
    passed("initially ineligible metadata never publishes a record")

    stale_dir = root / "ci-openpmix-pr-preparation"
    stale_dir.mkdir(exist_ok=True)
    stale = stale_dir / "preparation.env"
    stale.write_text("stale-ready-record")
    completed = run_case(ralph, ralph, pr_number="01")
    check(completed.returncode != 0 and not stale_dir.exists(),
          "malformed PR number left stale output")
    check(not (root / "mock/counter").exists(),
          "malformed PR number reached the fetcher")
    passed("canonical PR validation precedes metadata access and clears stale output")

    for include_token, pipeline_id in ((False, "9001"), (True, "0"),
                                       (True, "01"), (True, "bad")):
        stale_dir.mkdir(exist_ok=True)
        (stale_dir / "preparation.env").write_text("stale")
        completed = run_case(
            ralph, ralph, include_token=include_token,
            pipeline_id=pipeline_id,
        )
        check(completed.returncode != 0, "invalid prerequisites succeeded")
        check(not stale_dir.exists(), "invalid prerequisites left stale output")
        check(not (root / "mock/counter").exists(),
              "invalid prerequisites reached fetcher")
    passed("read token and canonical pipeline ID are required before fetching")

    completed = run_case(ralph, ralph)
    combined = completed.stdout + completed.stderr
    check(token.encode() not in combined, "token leaked to logs")
    for path in (root / "ci-openpmix-pr-preparation").rglob("*"):
        if path.is_file():
            check(token.encode() not in path.read_bytes(),
                  "token leaked to artifact: " + str(path))
    check("clone_url" not in preparation.read_text() and
          "example.invalid" not in preparation.read_text(),
          "PR-provided clone URL reached preparation")
    passed("tokens and PR-provided URLs are absent from logs and records")

    source = (ci / "prepare_trusted_openpmix_pr.sh").read_text()
    check("report_" not in source and "GITHUB_STATUS_TOKEN" not in source and
          "statuses/" not in source,
          "metadata-only preparation gained status capability")
    check("run_exact" not in source and "run_pmix_python_suite" not in source and
          "RFM_BIN" not in source and "sbatch" not in source and
          "srun" not in source,
          "metadata-only preparation gained execution capability")
    passed("preparation has no status, build, ReFrame, or scheduler behavior")

print("1..{}".format(tests))
PY
