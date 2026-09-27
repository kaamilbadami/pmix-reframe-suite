#!/bin/bash
set -euo pipefail

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
applier="$script_dir/apply_reconciled_pmix_state.py"

python3 - "$applier" <<'PY'
import ast
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import tempfile


applier = Path(sys.argv[1]).resolve()
base = "a" * 40
commit_b = "b" * 40
commit_c = "c" * 40
commit_d = "d" * 40
suite = "e" * 40
other_suite = "f" * 40
pass_count = 0
case_count = 0
temporary = tempfile.TemporaryDirectory()
root = Path(temporary.name)


def check(condition, message):
    if not condition:
        raise AssertionError(message)


def passed(message):
    global pass_count
    pass_count += 1
    print(f"ok - {message}")


def state_bytes(commit=base, suite_commit=suite, epoch="123456"):
    return (
        f"PMIX_COMMIT={commit}\n"
        f"SUITE_COMMIT={suite_commit}\n"
        f"LAST_SUCCESS_EPOCH={epoch}\n"
    ).encode()


def report_bytes(commits, result, prefix, reason=""):
    boundary = base if prefix == 0 else commits[prefix - 1]
    blocked = commits[prefix] if result == "blocked" else ""
    fields = [
        ("RECONCILIATION_RESULT", result),
        ("BASELINE_COMMIT", base),
        ("CURRENT_COMMIT", base),
        ("EXPECTED_SUITE_COMMIT", suite),
        ("DISCOVERED_COUNT", str(len(commits))),
        ("SUCCESSFUL_PREFIX_COUNT", str(prefix)),
        ("PREVIOUS_GOOD_COMMIT", base),
        ("PROPOSED_GOOD_COMMIT", boundary),
        ("FIRST_BLOCKED_COMMIT", blocked),
        ("FIRST_BLOCKED_REASON", reason),
        ("STATE_UPDATE_PROPOSED", "1" if prefix else "0"),
    ]
    return "".join(f"{name}={value}\n" for name, value in fields).encode()


class Case:
    def __init__(self, commits=()):
        global case_count
        case_count += 1
        self.root = root / f"case-{case_count}"
        self.root.mkdir()
        self.baseline = self.root / "baseline.env"
        self.current = self.root / "authoritative.env"
        self.commits = self.root / "commits.txt"
        self.report = self.root / "reconciliation.env"
        self.proposal = self.root / "proposed.env"
        self.commit_values = list(commits)
        self.baseline.write_bytes(state_bytes())
        self.current.write_bytes(state_bytes())
        self.commits.write_text("".join(f"{commit}\n" for commit in commits))

    def set_report(self, result, prefix, reason=""):
        self.report.write_bytes(report_bytes(self.commit_values, result, prefix, reason))

    def set_proposal(self, commit, *, suite_commit=suite, epoch="1700000000"):
        self.proposal.write_bytes(state_bytes(commit, suite_commit, epoch))

    def arguments(self):
        return [
            sys.executable,
            str(applier),
            "apply",
            "--baseline-state", str(self.baseline),
            "--authoritative-state", str(self.current),
            "--commits", str(self.commits),
            "--report", str(self.report),
            "--proposal", str(self.proposal),
            "--suite-commit", suite,
        ]

    def run(self, expected):
        before = self.current.read_bytes() if self.current.is_file() else None
        completed = subprocess.run(
            self.arguments(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        check(
            completed.returncode == expected,
            f"expected {expected}, got {completed.returncode}: "
            f"{completed.stderr.decode(errors='replace')}",
        )
        if expected != 0 and before is not None and self.current.is_file():
            check(self.current.read_bytes() == before, "failed application changed state")
        return completed


case = Case([commit_b, commit_c, commit_d])
case.set_report("complete", 3)
case.set_proposal(commit_d)
case.run(0)
check(case.current.read_bytes() == state_bytes(commit_d, suite, "1700000000"),
      "all-success case did not apply newest commit")
passed("all successful commits apply the newest commit")

for label, commits, prefix, reason, expected_commit in (
    ("first failure", [commit_b, commit_c], 0, "failed", base),
    ("middle failure", [commit_b, commit_c, commit_d], 1, "failed", commit_b),
    ("last failure", [commit_b, commit_c, commit_d], 2, "failed", commit_c),
):
    case = Case(commits)
    case.set_report("blocked", prefix, reason)
    if prefix:
        case.set_proposal(expected_commit)
    case.run(0)
    expected = state_bytes() if prefix == 0 else state_bytes(expected_commit, suite, "1700000000")
    check(case.current.read_bytes() == expected, f"{label} applied the wrong boundary")
    passed(f"{label} applies only its successful prefix")

case = Case([commit_b, commit_c, commit_d])
case.set_report("blocked", 1, "failed")
case.set_proposal(commit_b)
case.run(0)
check(case.current.read_bytes() == state_bytes(commit_b, suite, "1700000000"),
      "later success advanced across a failed commit")
passed("a later success cannot advance across an earlier blocker")

for reason in ("canceled", "unknown", "missing", "malformed"):
    case = Case([commit_b])
    case.set_report("blocked", 0, reason)
    case.run(0)
    check(case.current.read_bytes() == state_bytes(), f"{reason} blocker advanced state")
    passed(f"{reason} blocker is a no-op at a zero-length prefix")

case = Case([])
case.set_report("unchanged", 0)
before = case.current.stat().st_mtime_ns
case.run(0)
check(case.current.read_bytes() == state_bytes(), "empty discovery changed state")
check(case.current.stat().st_mtime_ns == before, "empty discovery rewrote state")
passed("no new commits performs no state write")

case = Case([commit_b])
case.current.write_bytes(state_bytes(commit_c))
case.set_report("complete", 1)
case.set_proposal(commit_b)
case.run(1)
passed("stale live state before application is rejected")

case = Case([commit_b])
case.current.unlink()
case.set_report("complete", 1)
case.set_proposal(commit_b)
case.run(1)
passed("missing authoritative state is rejected")

case = Case([commit_b, commit_c])
case.set_report("blocked", 1, "failed")
case.set_proposal(commit_b)
lines = case.report.read_text().splitlines()
lines[4] = "DISCOVERED_COUNT=1"
case.report.write_text("\n".join(lines) + "\n")
case.run(1)
passed("proposal, report, and ordered-list mismatch is rejected")

case = Case([commit_b])
case.set_report("complete", 1)
case.run(1)
passed("a required but missing proposal is rejected")

case = Case([])
case.set_report("unchanged", 0)
case.set_proposal(commit_b)
case.run(1)
passed("an unexpected proposal for a no-update result is rejected")

for label, mutate in (
    ("wrong proposal SHA", lambda case: case.set_proposal(commit_c)),
    ("wrong proposal suite", lambda case: case.set_proposal(commit_b, suite_commit=other_suite)),
):
    case = Case([commit_b])
    case.set_report("complete", 1)
    mutate(case)
    case.run(1)
    passed(f"{label} is rejected")

for label, line_index, value in (
    ("wrong report SHA", 7, commit_c),
    ("wrong report suite", 3, other_suite),
    ("wrong blocked commit", 8, commit_d),
):
    case = Case([commit_b, commit_c])
    case.set_report("blocked", 1, "failed")
    case.set_proposal(commit_b)
    lines = case.report.read_text().splitlines()
    field = lines[line_index].split("=", 1)[0]
    lines[line_index] = f"{field}={value}"
    case.report.write_text("\n".join(lines) + "\n")
    case.run(1)
    passed(f"{label} is rejected")

case = Case([commit_b])
case.set_report("complete", 1)
case.set_proposal(commit_b)
target = case.root / "state-target.env"
target.write_bytes(state_bytes())
case.current.unlink()
case.current.symlink_to(target)
case.run(1)
check(target.read_bytes() == state_bytes(), "state symlink target changed")
passed("symbolic-link state files are rejected without changing their target")

case = Case([commit_b])
case.set_report("complete", 1)
case.set_proposal(commit_b)
case.current.unlink()
case.current.mkdir()
case.run(1)
passed("non-regular state files are rejected")

for malformed in (
    b"",
    (base + "\n").encode(),
    state_bytes() + b"EXTRA=value\n",
    state_bytes(base.upper()),
    state_bytes(epoch="0"),
):
    path = root / f"validation-{pass_count}.env"
    path.write_bytes(malformed)
    completed = subprocess.run(
        [sys.executable, str(applier), "validate-state", str(path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    check(completed.returncode == 1, "malformed production state was accepted")
passed("strict state validation rejects malformed three-field states")

valid = root / "valid-state.env"
valid.write_bytes(state_bytes())
subprocess.run(
    [sys.executable, str(applier), "validate-state", str(valid)],
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
    check=True,
)
passed("strict state validation accepts the canonical state schema")

snapshot = root / "snapshot-state.env"
subprocess.run(
    [
        sys.executable,
        str(applier),
        "snapshot",
        "--state",
        str(valid),
        "--output",
        str(snapshot),
    ],
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
    check=True,
)
check(snapshot.read_bytes() == valid.read_bytes(), "snapshot changed state bytes")
passed("valid authoritative state is read into an exact baseline snapshot")

completed = subprocess.run(
    [
        sys.executable,
        str(applier),
        "snapshot",
        "--state",
        str(root / "missing-state.env"),
        "--output",
        str(root / "missing-snapshot.env"),
    ],
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
    check=False,
)
check(completed.returncode == 1, "missing authoritative state was accepted")
passed("missing authoritative state cannot produce a baseline")

spec = importlib.util.spec_from_file_location("applier_under_test", applier)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

case = Case([commit_b])
case.set_report("complete", 1)
case.set_proposal(commit_b)
before = case.current.read_bytes()
real_validate = module.validate_reconciliation


def change_state_after_validation(*arguments):
    result = real_validate(*arguments)
    case.current.write_bytes(state_bytes(commit_c))
    return result


module.validate_reconciliation = change_state_after_validation
try:
    status = module.main(case.arguments()[2:])
finally:
    module.validate_reconciliation = real_validate
check(status == 1, "in-application stale state did not fail")
check(case.current.read_bytes() == state_bytes(commit_c),
      "in-application stale state was overwritten")
passed("a live-state change between validation and replacement fails closed")

case = Case([commit_b])
case.set_report("complete", 1)
case.set_proposal(commit_b)
before = case.current.read_bytes()
real_replace = module.os.replace


def fail_replace(source, destination):
    raise OSError("injected state replacement failure")


module.os.replace = fail_replace
try:
    status = module.main(case.arguments()[2:])
finally:
    module.os.replace = real_replace
check(status == 1, "atomic replacement failure did not fail")
check(case.current.read_bytes() == before, "atomic replacement failure changed state")
check(not list(case.root.glob(".authoritative.env.tmp.*")),
      "atomic failure left a temporary")
passed("atomic application failure preserves the previous state")

case = Case([commit_b])
case.set_report("complete", 1)
case.set_proposal(commit_b)
before = case.current.read_bytes()
real_access = module.os.access
module.os.access = lambda *arguments, **keywords: False
try:
    status = module.main(case.arguments()[2:])
finally:
    module.os.access = real_access
check(status == 1, "unwritable authoritative state was accepted")
check(case.current.read_bytes() == before, "unwritable state was changed")
passed("unwritable authoritative state fails closed")

case = Case([commit_b])
case.set_report("complete", 1)
case.set_proposal(commit_b)
case.run(0)
check(not list(case.root.glob(".authoritative.env.tmp.*")),
      "successful atomic replacement left a temporary")
passed("successful atomic application leaves no replacement temporary")

case = Case([commit_b])
case.set_report("complete", 1)
case.set_proposal(commit_b)
unsafe_parent = case.root / "unsafe-parent"
real_parent = case.root / "real-parent"
real_parent.mkdir()
(real_parent / "state.env").write_bytes(state_bytes())
unsafe_parent.symlink_to(real_parent, target_is_directory=True)
arguments = case.arguments()
arguments[arguments.index("--authoritative-state") + 1] = str(
    unsafe_parent / "state.env"
)
completed = subprocess.run(
    arguments,
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
    check=False,
)
check(completed.returncode == 1, "symlinked authoritative parent was accepted")
check((real_parent / "state.env").read_bytes() == state_bytes(),
      "symlinked authoritative target changed")
passed("symbolic-link authoritative path components fail safely")

case = Case([commit_b])
case.set_report("complete", 1)
case.set_proposal(commit_b)
lock_target = case.root / "lock-target"
lock_target.write_text("keep\n")
(case.root / ".authoritative.env.lock").symlink_to(lock_target)
case.run(1)
check(lock_target.read_text() == "keep\n", "lock symlink target changed")
check(case.current.read_bytes() == state_bytes(), "unsafe lock changed state")
passed("symbolic-link authoritative lock files fail safely")

source = applier.read_text(encoding="utf-8")
tree = ast.parse(source)
imports = set()
for node in ast.walk(tree):
    if isinstance(node, ast.Import):
        imports.update(alias.name.split(".")[0] for alias in node.names)
    elif isinstance(node, ast.ImportFrom) and node.module:
        imports.add(node.module.split(".")[0])
check("subprocess" not in imports, "application helper imports subprocess")
check("urllib" not in imports and "socket" not in imports, "application helper imports network support")
for forbidden in ("reframe", "slurm", "sbatch", "srun", "gitlab", "github"):
    check(forbidden not in source.lower(), f"application helper contains forbidden {forbidden}")
passed("application helper has no network, build, scheduler, or external-command capability")

temporary.cleanup()
print(f"1..{pass_count}")
PY
