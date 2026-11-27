#!/usr/bin/env python3
"""Try the pin, then one exact master snapshot only after a PRRTE build failure."""
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

from generate_pmix_child_pipeline import resolve_prrte_master
from collect_pmix_child_results import atomic_write
from pmix_result_summary import TESTS


def prrte_build_failed(report):
    try:
        document = json.loads(report.read_text())
        cases = document["runs"][-1]["testcases"]
        builds = [case for case in cases if case.get("fixture") is True and
                  case.get("name", "").split()[0] == "build_prrte"]
        # Do not convert runtime, fetch, PMIx-build, or incomplete failures
        # into dependency updates. Require one conclusive PRRTE compile failure.
        return (len(builds) == 1 and builds[0].get("result") == "fail" and
                builds[0].get("fail_phase") in ("compile", "compile_wait") and
                not any(case.get("name") in TESTS and case.get("result") != "fail_deps"
                        for case in cases) and
                not any(case.get("fixture") is True and case is not builds[0] and
                        case.get("result") != "pass" for case in cases))
    except (OSError, ValueError, KeyError, IndexError, TypeError, AttributeError):
        return False


def run(env, execute=subprocess.call, resolve=resolve_prrte_master):
    pinned = env.get("PRRTE_COMMIT", "")
    if not re.fullmatch(r"[0-9a-f]{40}", pinned):
        raise ValueError("fallback requires an exact PRRTE pin")
    root = Path(env["PMIX_JOB_ROOT"])
    prefix = root / "reframe"
    if Path(env["PMIX_RFM_PREFIX"]) != prefix:
        raise ValueError("fallback requires the canonical isolated ReFrame prefix")
    policy_path = root / "prrte-policy.json"
    policy = dict(schema_version=1, pinned=pinned, candidate=None, fallback_attempted=False,
                  attempts=[])

    def save():
        atomic_write(policy_path, (json.dumps(policy, indent=2) + "\n").encode())

    def attempt(revision):
        child = dict(env, PRRTE_COMMIT=revision, PRRTE_BRANCH="master",
                     PMIX_RFM_REPORT_FILE=str(prefix / "run-report.json"))
        status = execute(["bash", "ci/run_exact_pmix_commit.sh"], env=child)
        policy["attempts"].append(dict(prrte_commit=revision, exit_code=status))
        save()
        return status

    root.mkdir(parents=True, exist_ok=True)
    save()
    status = attempt(pinned)
    if status == 0 or not prrte_build_failed(prefix / "run-report.json"):
        policy["reason"] = "pin passed" if status == 0 else "failure is not a confirmed PRRTE build failure"
        save()
        return status
    try:
        candidate = resolve()
    except (Exception, SystemExit) as error:
        policy["reason"] = "master lookup failed: " + str(error)
        save()
        return status
    if not re.fullmatch(r"[0-9a-f]{40}", candidate):
        raise ValueError("master lookup returned an invalid candidate")
    policy["candidate"] = candidate
    if candidate == pinned:
        policy["reason"] = "master is unchanged; no duplicate build"
        save()
        return status
    # Each dependency attempt gets its own complete ReFrame tree. after_script
    # reads the final canonical tree; the earlier build evidence is retained.
    archive = root / "prrte-attempts" / "pinned"
    archive.mkdir(parents=True)
    retained = {"run-report.json", "run-console.log", "rfm_build.out", "rfm_build.err",
                "rfm_build.sh", "rfm_job.out", "rfm_job.err", "rfm_job.sh", "prrte-source.env"}
    for source in prefix.rglob("*"):
        if source.name in retained and source.is_file() and not source.is_symlink():
            destination = archive / "reframe" / source.relative_to(prefix)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(source), str(destination))
    shutil.rmtree(str(prefix))
    policy["fallback_attempted"] = True
    policy["reason"] = "retrying the same PMIx commit against an exact master snapshot"
    save()
    return attempt(candidate)


if __name__ == "__main__":
    sys.exit(run(os.environ))
