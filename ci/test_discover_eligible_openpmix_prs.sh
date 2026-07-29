#!/bin/bash
set -euo pipefail

script_dir=$(cd -- "${BASH_SOURCE[0]%/*}" && pwd -P)
python3 - "$script_dir" <<'PY'
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import yaml


ci = Path(sys.argv[1]).resolve()
discover = ci / "discover_eligible_openpmix_prs.py"
generate = ci / "generate_openpmix_pr_child_pipeline.py"
fixture_dir = ci / "fixtures/openpmix_pr"
ralph = json.loads((fixture_dir / "rhc54_verified_fork_open.json").read_text())
kaamil = json.loads(
    (fixture_dir / "kaamilbadami_same_repository_open.json").read_text())
passed_count = 0


def passed(message):
    global passed_count
    passed_count += 1
    print("ok - " + message)


def check(condition, message):
    if not condition:
        raise AssertionError(message)


def run(root, prs, pipelines):
    (root / "prs.json").write_text(json.dumps(prs))
    (root / "pipelines.json").write_text(json.dumps(pipelines))
    environment = os.environ.copy()
    environment["OPENPMIX_PR_DISCOVERY_TEST_MODE"] = "1"
    return subprocess.run([
        sys.executable, str(discover),
        "--test-prs", "prs.json", "--test-pipelines", "pipelines.json",
        "--output", "eligible.jsonl",
    ], cwd=str(root), env=environment, stdout=subprocess.PIPE,
       stderr=subprocess.PIPE, check=False)


with tempfile.TemporaryDirectory(prefix="openpmix-discovery-") as temporary:
    root = Path(temporary)
    invalid_author = copy.deepcopy(ralph)
    invalid_author["number"] = 41
    invalid_author["id"] = 41
    invalid_author["user"]["login"] = "third-party"
    invalid_author["user"]["id"] = 41
    invalid_author["head"]["sha"] = "1" * 40
    kaamil_fork = copy.deepcopy(kaamil)
    kaamil_fork["number"] = 42
    kaamil_fork["id"] = 42
    kaamil_fork["head"]["sha"] = "2" * 40
    kaamil_fork["head"]["repo"]["full_name"] = "kaamilbadami/openpmix"
    draft = copy.deepcopy(ralph)
    draft["number"] = 43
    draft["id"] = 43
    draft["head"]["sha"] = "3" * 40
    draft["draft"] = True
    wrong_base = copy.deepcopy(ralph)
    wrong_base["number"] = 44
    wrong_base["id"] = 44
    wrong_base["head"]["sha"] = "4" * 40
    wrong_base["base"]["repo"]["full_name"] = "rhc54/openpmix"

    completed = [{
        "pr_number": str(ralph["number"]),
        "head_sha": ralph["head"]["sha"],
    }]
    result = run(root, [
        ralph, kaamil, invalid_author, kaamil_fork, draft, wrong_base,
    ], completed)
    check(result.returncode == 0, result.stderr.decode())
    records = [
        json.loads(line)
        for line in (root / "eligible.jsonl").read_text().splitlines()
    ]
    check(len(records) == 1 and
          records[0]["pr_number"] == str(kaamil["number"]) and
          records[0]["head_repository"] == "openpmix/openpmix",
          "policy filtering or duplicate suppression changed")
    passed("discovery filters policy failures and suppresses an unchanged completed head")

    updated = copy.deepcopy(ralph)
    updated["head"]["sha"] = "a" * 40
    result = run(root, [updated], completed)
    check(result.returncode == 0, result.stderr.decode())
    record = json.loads((root / "eligible.jsonl").read_text().strip())
    check(record["pr_number"] == str(ralph["number"]) and
          record["head_sha"] == "a" * 40,
          "updated head was not selected")
    passed("a new SHA on the same eligible PR triggers a new isolated record")

    result = run(root, [updated, copy.deepcopy(updated)], [])
    check(result.returncode == 2 and not
          (root / "eligible.jsonl").exists(),
          "duplicate GitHub identity did not fail closed")
    passed("duplicated authoritative PR heads fail closed")

    result = run(root, [ralph], completed)
    check(result.returncode == 0 and
          (root / "eligible.jsonl").read_text() == "",
          "empty eligible result was not represented safely")
    generated = subprocess.run([
        sys.executable, str(generate), "eligible.jsonl", "pipeline.yml",
    ], cwd=str(root), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
       check=False)
    check(generated.returncode == 0 and
          "no-eligible-openpmix-prs:" in
          (root / "pipeline.yml").read_text(),
          "empty discovery did not generate a no-op child")
    passed("empty discovery produces one harmless no-op pipeline")

    result = run(root, [updated, kaamil], [])
    check(result.returncode == 0, result.stderr.decode())
    generated = subprocess.run([
        sys.executable, str(generate), "eligible.jsonl", "pipeline.yml",
    ], cwd=str(root), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
       check=False)
    text = (root / "pipeline.yml").read_text()
    check(generated.returncode == 0 and
          isinstance(yaml.safe_load(text), dict) and
          text.count("OPENPMIX_PR_INTERNAL:") == 2 and
          text.count("ci/openpmix_pr_child.yml") == 2 and
          ".ci-state" not in text and "GITHUB_STATUS" not in text,
          "generated child pipeline is not isolated")
    passed("each new head gets one status-free child workflow with no PMIx master state")

    environment = os.environ.copy()
    environment.pop("OPENPMIX_PR_DISCOVERY_TEST_MODE", None)
    rejected = subprocess.run([
        sys.executable, str(discover),
        "--test-prs", "prs.json", "--test-pipelines", "pipelines.json",
        "--output", "not-created.jsonl",
    ], cwd=str(root), env=environment, stdout=subprocess.PIPE,
       stderr=subprocess.PIPE, check=False)
    check(rejected.returncode == 2 and
          not (root / "not-created.jsonl").exists(),
          "network-free override was available in production mode")
    passed("fixture inputs are inaccessible unless explicit test mode is enabled")

print("1..{}".format(passed_count))
PY
