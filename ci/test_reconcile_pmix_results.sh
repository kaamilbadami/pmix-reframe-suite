#!/bin/bash
set -euo pipefail

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
reconciler="$script_dir/reconcile_pmix_results.py"

python3 - "$reconciler" <<'PY'
import ast
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import tempfile


reconciler = Path(sys.argv[1]).resolve()
baseline_sha = "a" * 40
commit_b = "b" * 40
commit_c = "c" * 40
commit_d = "d" * 40
suite_sha = "e" * 40
other_suite_sha = "f" * 40
test_epoch = "1700000000"
report_fields = [
    "RECONCILIATION_RESULT",
    "BASELINE_COMMIT",
    "CURRENT_COMMIT",
    "EXPECTED_SUITE_COMMIT",
    "DISCOVERED_COUNT",
    "SUCCESSFUL_PREFIX_COUNT",
    "PREVIOUS_GOOD_COMMIT",
    "PROPOSED_GOOD_COMMIT",
    "FIRST_BLOCKED_COMMIT",
    "FIRST_BLOCKED_REASON",
    "STATE_UPDATE_PROPOSED",
]
pass_count = 0
case_count = 0
temporary_root = tempfile.TemporaryDirectory()
root = Path(temporary_root.name)


def check(condition, message):
    if not condition:
        raise AssertionError(message)


def passed(message):
    global pass_count
    pass_count += 1
    print(f"ok - {message}")


def state_bytes(pmix=baseline_sha, suite=suite_sha, epoch="123456"):
    return (
        f"PMIX_COMMIT={pmix}\n"
        f"SUITE_COMMIT={suite}\n"
        f"LAST_SUCCESS_EPOCH={epoch}\n"
    ).encode()


def result_bytes(
    pmix,
    status="success",
    suite=suite_sha,
    job_id="123",
    pipeline_id="456",
):
    return (
        f"PMIX_COMMIT={pmix}\n"
        f"SUITE_COMMIT={suite}\n"
        f"CI_JOB_STATUS={status}\n"
        f"CI_JOB_ID={job_id}\n"
        f"CI_PIPELINE_ID={pipeline_id}\n"
    ).encode()


class Case:
    def __init__(self):
        global case_count
        case_count += 1
        self.root = root / f"case-{case_count}"
        self.root.mkdir()
        self.baseline = self.root / "baseline-state.env"
        self.current = self.root / "current-state.env"
        self.commits = self.root / "ordered-commits.txt"
        self.results = self.root / "results"
        self.output = self.root / "output"
        self.baseline.write_bytes(state_bytes())
        self.current.write_bytes(state_bytes())
        self.commits.write_bytes(b"")
        self.results.mkdir()

    def set_commits(self, *commits):
        content = "".join(f"{commit}\n" for commit in commits).encode()
        self.commits.write_bytes(content)

    def result(self, commit, status="success", **overrides):
        (self.results / f"{commit}.env").write_bytes(
            result_bytes(commit, status=status, **overrides)
        )

    def arguments(self, *, output=None, suite=suite_sha):
        return [
            "--baseline-state",
            str(self.baseline),
            "--current-state",
            str(self.current),
            "--commits",
            str(self.commits),
            "--results",
            str(self.results),
            "--suite-commit",
            suite,
            "--output",
            str(output if output is not None else self.output),
        ]

    def run(self, expected_code, *, epoch=test_epoch, output=None, suite=suite_sha):
        environment = os.environ.copy()
        environment.pop("PMIX_RECONCILE_TEST_EPOCH", None)
        if epoch is not None:
            environment["PMIX_RECONCILE_TEST_EPOCH"] = epoch
        command = [sys.executable, str(reconciler)] + self.arguments(
            output=output,
            suite=suite,
        )
        completed = subprocess.run(
            command,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        check(
            completed.returncode == expected_code,
            f"expected exit {expected_code}, got {completed.returncode}: "
            f"{completed.stderr.decode(errors='replace')}",
        )
        return completed


def parse_report(case):
    lines = (case.output / "reconciliation.env").read_text().splitlines()
    check(len(lines) == len(report_fields), "report has the wrong line count")
    names = []
    values = {}
    for line in lines:
        check("=" in line, "report line has no equals sign")
        name, value = line.split("=", 1)
        check(name not in values, "report contains a duplicate field")
        names.append(name)
        values[name] = value
    check(names == report_fields, "report field schema or order changed")
    for numeric in (
        "DISCOVERED_COUNT",
        "SUCCESSFUL_PREFIX_COUNT",
        "STATE_UPDATE_PROPOSED",
    ):
        check(
            values[numeric] == "0" or values[numeric].startswith(tuple("123456789")),
            f"{numeric} is not canonical decimal",
        )
        check(values[numeric].isdigit(), f"{numeric} is not decimal")
    return values


def check_proposal(case, commit, epoch=test_epoch):
    expected = state_bytes(commit, suite_sha, epoch)
    check(
        (case.output / "proposed-pmix-master.env").read_bytes() == expected,
        "proposed state content changed",
    )


def check_no_proposal(case):
    check(
        not (case.output / "proposed-pmix-master.env").exists(),
        "unexpected proposed state was written",
    )


case = Case()
case.run(0)
report = parse_report(case)
check(report["RECONCILIATION_RESULT"] == "unchanged", "empty list was not unchanged")
check(report["DISCOVERED_COUNT"] == "0", "empty list count was not zero")
check(report["STATE_UPDATE_PROPOSED"] == "0", "empty list proposed an update")
check_no_proposal(case)
passed("empty commit list is unchanged and exits 0")

case = Case()
case.set_commits(commit_b)
case.result(commit_b)
case.run(0)
report = parse_report(case)
check(report["RECONCILIATION_RESULT"] == "complete", "one success was not complete")
check(report["SUCCESSFUL_PREFIX_COUNT"] == "1", "one-success prefix was wrong")
check(report["PROPOSED_GOOD_COMMIT"] == commit_b, "one success did not advance")
check(report["STATE_UPDATE_PROPOSED"] == "1", "one success lacked proposal")
check_proposal(case, commit_b)
passed("one successful commit completes and advances the proposal")

case = Case()
case.set_commits(commit_b, commit_c, commit_d)
for commit in (commit_b, commit_c, commit_d):
    case.result(commit)
case.run(0)
report = parse_report(case)
check(report["DISCOVERED_COUNT"] == "3", "multiple-success count was wrong")
check(report["SUCCESSFUL_PREFIX_COUNT"] == "3", "multiple-success prefix was wrong")
check(report["PROPOSED_GOOD_COMMIT"] == commit_d, "newest success was not proposed")
check_proposal(case, commit_d)
passed("multiple successful commits advance to the newest commit")

case = Case()
case.set_commits(commit_b)
case.result(commit_b, "failed")
case.run(3)
report = parse_report(case)
check(report["RECONCILIATION_RESULT"] == "blocked", "first failure was not blocked")
check(report["FIRST_BLOCKED_COMMIT"] == commit_b, "first failure commit was wrong")
check(report["FIRST_BLOCKED_REASON"] == "failed", "failed reason was wrong")
check(report["SUCCESSFUL_PREFIX_COUNT"] == "0", "failed-first prefix was not zero")
check_no_proposal(case)
passed("failure at the first commit blocks without a proposal and exits 3")

case = Case()
case.set_commits(commit_b, commit_c)
case.result(commit_b)
case.result(commit_c, "failed")
case.run(3)
report = parse_report(case)
check(report["PROPOSED_GOOD_COMMIT"] == commit_b, "success/failure skipped boundary")
check(report["SUCCESSFUL_PREFIX_COUNT"] == "1", "success/failure prefix was wrong")
check_proposal(case, commit_b)
passed("success then failure proposes only the contiguous successful prefix")

case = Case()
case.set_commits(commit_b, commit_c, commit_d)
case.result(commit_b)
case.result(commit_c, "failed")
case.result(commit_d)
case.run(3)
report = parse_report(case)
check(report["PROPOSED_GOOD_COMMIT"] == commit_b, "later success skipped a failure")
check(report["FIRST_BLOCKED_COMMIT"] == commit_c, "wrong first blocker recorded")
check(report["SUCCESSFUL_PREFIX_COUNT"] == "1", "later success increased prefix")
check_proposal(case, commit_b)
passed("a later success cannot advance across an earlier failure")

case = Case()
case.set_commits(commit_b, commit_c)
case.result(commit_c)
case.run(3)
report = parse_report(case)
check(report["FIRST_BLOCKED_REASON"] == "missing", "missing result misclassified")
check(report["PROPOSED_GOOD_COMMIT"] == baseline_sha, "missing result advanced state")
check_no_proposal(case)
passed("a missing first result blocks without inspecting later advancement")

case = Case()
case.set_commits(commit_b, commit_c)
(case.results / f"{commit_b}.env").write_text("PMIX_COMMIT=broken\n")
case.result(commit_c)
case.run(3)
report = parse_report(case)
check(report["FIRST_BLOCKED_REASON"] == "malformed", "malformed result misclassified")
check(report["SUCCESSFUL_PREFIX_COUNT"] == "0", "malformed result advanced prefix")
passed("a malformed result is a blocking result rather than an input crash")

for status in ("canceled", "unknown"):
    case = Case()
    case.set_commits(commit_b)
    case.result(commit_b, status)
    case.run(3)
    report = parse_report(case)
    check(report["FIRST_BLOCKED_REASON"] == status, f"{status} reason was wrong")
    check_no_proposal(case)
    passed(f"{status} result blocks with its exact reason")

malformed_result_cases = [
    ("wrong PMIx SHA", result_bytes(commit_c)),
    ("wrong suite SHA", result_bytes(commit_b, suite=other_suite_sha)),
    ("wrong job ID", result_bytes(commit_b, job_id="01")),
    ("wrong pipeline ID", result_bytes(commit_b, pipeline_id="0")),
    (
        "duplicate result field",
        (
            f"PMIX_COMMIT={commit_b}\nPMIX_COMMIT={commit_b}\n"
            "CI_JOB_STATUS=success\nCI_JOB_ID=123\nCI_PIPELINE_ID=456\n"
        ).encode(),
    ),
    (
        "extra result field",
        result_bytes(commit_b) + b"EXTRA_FIELD=value\n",
    ),
    (
        "wrong result-field order",
        (
            f"SUITE_COMMIT={suite_sha}\nPMIX_COMMIT={commit_b}\n"
            "CI_JOB_STATUS=success\nCI_JOB_ID=123\nCI_PIPELINE_ID=456\n"
        ).encode(),
    ),
]
for label, content in malformed_result_cases:
    case = Case()
    case.set_commits(commit_b)
    (case.results / f"{commit_b}.env").write_bytes(content)
    case.run(3)
    report = parse_report(case)
    check(report["FIRST_BLOCKED_REASON"] == "malformed", f"{label} was accepted")
    check_no_proposal(case)
    passed(f"{label} is rejected as a malformed blocking result")

for label, content in (
    ("duplicate commit-list SHA", f"{commit_b}\n{commit_b}\n"),
    ("blank commit-list line", f"{commit_b}\n\n{commit_c}\n"),
    ("uppercase commit SHA", f"{commit_b.upper()}\n"),
):
    case = Case()
    case.commits.write_text(content)
    case.run(4)
    report = parse_report(case)
    check(report["RECONCILIATION_RESULT"] == "invalid", f"{label} was not invalid")
    check_no_proposal(case)
    passed(f"{label} is invalid and exits 4")

case = Case()
case.set_commits(commit_b, baseline_sha)
(case.results / f"{commit_b}.env").mkdir()
case.run(4)
report = parse_report(case)
check(report["RECONCILIATION_RESULT"] == "invalid", "baseline commit was not invalid")
check(report["DISCOVERED_COUNT"] == "2", "baseline-containing list count was wrong")
check(report["SUCCESSFUL_PREFIX_COUNT"] == "0", "a result was inspected before rejection")
check(report["STATE_UPDATE_PROPOSED"] == "0", "baseline commit proposed an update")
check_no_proposal(case)
passed("a commit list containing the baseline is invalid without inspecting results")

case = Case()
case.current.write_bytes(state_bytes(commit_c))
case.set_commits(commit_b, baseline_sha)
(case.results / f"{commit_b}.env").mkdir()
case.run(5)
report = parse_report(case)
check(report["RECONCILIATION_RESULT"] == "stale", "stale state lost precedence")
check(report["CURRENT_COMMIT"] == commit_c, "combined stale commit was not reported")
check(report["DISCOVERED_COUNT"] == "2", "combined stale list count was wrong")
check(report["SUCCESSFUL_PREFIX_COUNT"] == "0", "combined stale run inspected a result")
check(report["STATE_UPDATE_PROPOSED"] == "0", "combined stale run proposed an update")
check_no_proposal(case)
passed("stale state precedes a baseline-containing list without inspecting results")

invalid_states = [
    b"",
    f"PMIX_COMMIT={baseline_sha}\nSUITE_COMMIT={suite_sha}\n".encode(),
    state_bytes() + b"EXTRA=value\n",
    (
        f"PMIX_COMMIT={baseline_sha}\nPMIX_COMMIT={baseline_sha}\n"
        "LAST_SUCCESS_EPOCH=123456\n"
    ).encode(),
    (
        f"SUITE_COMMIT={suite_sha}\nPMIX_COMMIT={baseline_sha}\n"
        "LAST_SUCCESS_EPOCH=123456\n"
    ).encode(),
    state_bytes(baseline_sha.upper()),
    state_bytes(epoch="0"),
    state_bytes(epoch="0123"),
]
for content in invalid_states:
    case = Case()
    case.baseline.write_bytes(content)
    case.run(4)
    report = parse_report(case)
    check(report["RECONCILIATION_RESULT"] == "invalid", "invalid baseline accepted")
    check_no_proposal(case)
passed("missing, extra, duplicate, reordered, uppercase, and bad-epoch baseline states are invalid")

case = Case()
case.current.write_bytes(state_bytes(epoch="01"))
case.run(4)
report = parse_report(case)
check(report["RECONCILIATION_RESULT"] == "invalid", "invalid current state accepted")
check_no_proposal(case)
passed("invalid current state is rejected and exits 4")

case = Case()
case.current.write_bytes(state_bytes(commit_b))
case.set_commits(commit_c)
case.result(commit_c)
case.run(5)
report = parse_report(case)
check(report["RECONCILIATION_RESULT"] == "stale", "changed current commit was not stale")
check(report["CURRENT_COMMIT"] == commit_b, "stale current commit was not reported")
check(report["SUCCESSFUL_PREFIX_COUNT"] == "0", "stale run trusted a result")
check_no_proposal(case)
passed("current PMIx commit differing from baseline is stale and exits 5")

case = Case()
case.current.write_bytes(state_bytes(epoch="123457"))
case.set_commits(commit_b)
case.result(commit_b)
case.run(5)
report = parse_report(case)
check(report["RECONCILIATION_RESULT"] == "stale", "byte-different state was not stale")
check(report["SUCCESSFUL_PREFIX_COUNT"] == "0", "byte-different stale run read result")
check_no_proposal(case)
passed("byte-different states with the same PMIx commit are stale")

case = Case()
target = case.root / "state-target.env"
target.write_bytes(state_bytes())
case.baseline.unlink()
case.baseline.symlink_to(target)
case.run(4)
report = parse_report(case)
check(report["RECONCILIATION_RESULT"] == "invalid", "state symlink was accepted")
check(target.read_bytes() == state_bytes(), "state symlink target was changed")
passed("input state symbolic links are rejected without changing the target")

case = Case()
target = case.root / "commit-target.txt"
target.write_text(f"{commit_b}\n")
case.commits.unlink()
case.commits.symlink_to(target)
case.run(4)
report = parse_report(case)
check(report["RECONCILIATION_RESULT"] == "invalid", "commit-list symlink accepted")
passed("commit-list symbolic links are rejected")

case = Case()
case.set_commits(commit_b)
target = case.root / "result-target.env"
target.write_bytes(result_bytes(commit_b))
(case.results / f"{commit_b}.env").symlink_to(target)
case.run(3)
report = parse_report(case)
check(report["FIRST_BLOCKED_REASON"] == "malformed", "result symlink misclassified")
check(target.read_bytes() == result_bytes(commit_b), "result symlink target changed")
passed("result symbolic links block as malformed without following the target")

case = Case()
case.set_commits(commit_b, commit_c)
case.results.rmdir()
case.run(3)
report = parse_report(case)
check(report["RECONCILIATION_RESULT"] == "blocked", "absent results was not blocked")
check(report["FIRST_BLOCKED_COMMIT"] == commit_b, "absent results blocked wrong commit")
check(report["FIRST_BLOCKED_REASON"] == "missing", "absent results was not missing")
check(report["SUCCESSFUL_PREFIX_COUNT"] == "0", "absent results advanced prefix")
check_no_proposal(case)
passed("an absent results directory blocks on the first missing result and exits 3")

case = Case()
case.set_commits(commit_b)
case.results.rmdir()
results_target = case.root / "results-target"
results_target.mkdir()
sentinel = results_target / "sentinel"
sentinel.write_bytes(b"preserve\n")
case.results.symlink_to(results_target, target_is_directory=True)
case.run(4)
report = parse_report(case)
check(report["RECONCILIATION_RESULT"] == "invalid", "results symlink was accepted")
check(sentinel.read_bytes() == b"preserve\n", "results symlink target was changed")
check_no_proposal(case)
passed("a results-directory symbolic link is invalid and exits 4")

case = Case()
case.set_commits(commit_b)
case.results.rmdir()
case.results.write_bytes(b"not a directory\n")
case.run(4)
report = parse_report(case)
check(report["RECONCILIATION_RESULT"] == "invalid", "regular results path was accepted")
check_no_proposal(case)
passed("a regular-file results path is invalid and exits 4")

case = Case()
output_target = case.root / "output-target"
output_target.mkdir()
output_link = case.root / "output-link"
output_link.symlink_to(output_target, target_is_directory=True)
case.run(4, output=output_link)
check(not any(output_target.iterdir()), "output-directory symlink target was touched")
passed("output-directory symbolic links are rejected")

case = Case()
case.output.mkdir()
target = case.root / "known-output-target"
target.write_text("preserve\n")
(case.output / "reconciliation.env").symlink_to(target)
case.run(4)
check(target.read_text() == "preserve\n", "known-output symlink target changed")
check((case.output / "reconciliation.env").is_symlink(), "known symlink was removed")
passed("known stale output symbolic links are rejected without being followed")

case = Case()
case.output.mkdir()
(case.output / "reconciliation.env").write_text("stale report\n")
(case.output / "proposed-pmix-master.env").write_text("stale proposal\n")
case.run(0)
report = parse_report(case)
check(report["RECONCILIATION_RESULT"] == "unchanged", "stale report survived")
check_no_proposal(case)
passed("known stale outputs are removed before processing")

case = Case()
case.output.mkdir()
unknown = case.output / "unknown.keep"
unknown.write_bytes(b"preserve unknown\n")
case.run(0)
parse_report(case)
check(unknown.read_bytes() == b"preserve unknown\n", "unknown output was removed")
passed("unknown output files are preserved")

case = Case()
case.set_commits(commit_b)
case.result(commit_b)
case.run(0, epoch="1800000001")
check_proposal(case, commit_b, "1800000001")
passed("the validated test-only epoch makes proposal time deterministic")

for invalid_epoch in ("", "0", "01", "-1", "abc"):
    case = Case()
    case.run(4, epoch=invalid_epoch)
    report = parse_report(case)
    check(report["RECONCILIATION_RESULT"] == "invalid", "invalid test epoch accepted")
    check_no_proposal(case)
passed("invalid test-only epochs are rejected")

spec = importlib.util.spec_from_file_location("reconciler_under_test", reconciler)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def run_with_replace_failure(case, failed_output):
    real_replace = module.os.replace
    replaced_outputs = []
    previous_epoch = os.environ.get("PMIX_RECONCILE_TEST_EPOCH")

    def fail_selected_replace(source, destination):
        output_name = Path(destination).name
        replaced_outputs.append(output_name)
        if output_name == failed_output:
            raise OSError(f"injected {output_name} replacement failure")
        return real_replace(source, destination)

    module.os.replace = fail_selected_replace
    os.environ["PMIX_RECONCILE_TEST_EPOCH"] = test_epoch
    try:
        exit_status = module.reconcile(case.arguments())
    finally:
        module.os.replace = real_replace
        if previous_epoch is None:
            os.environ.pop("PMIX_RECONCILE_TEST_EPOCH", None)
        else:
            os.environ["PMIX_RECONCILE_TEST_EPOCH"] = previous_epoch
    check(exit_status == 4, f"{failed_output} replacement did not exit 4")
    return replaced_outputs


case = Case()
case.set_commits(commit_b)
case.result(commit_b)
replaced = run_with_replace_failure(case, "proposed-pmix-master.env")
report = parse_report(case)
check(report["RECONCILIATION_RESULT"] == "invalid", "proposal failure report not invalid")
check(report["STATE_UPDATE_PROPOSED"] == "0", "proposal failure report claims proposal")
check_no_proposal(case)
check(
    replaced == ["proposed-pmix-master.env", "reconciliation.env"],
    "proposal failure did not publish the invalid report last",
)
check(not list(case.output.glob(".*.tmp.*")), "proposal failure left a temporary file")
passed("proposal replacement failure leaves an invalid report and no proposal")

case = Case()
case.set_commits(commit_b)
case.result(commit_b)
replaced = run_with_replace_failure(case, "reconciliation.env")
check(not (case.output / "reconciliation.env").exists(), "failed final report was published")
check_no_proposal(case)
check(
    replaced == ["proposed-pmix-master.env", "reconciliation.env"],
    "final report failure did not attempt proposal then report",
)
check(not list(case.output.glob(".*.tmp.*")), "report failure left a temporary file")
passed("final report replacement failure removes the published proposal")

atomic_dir = root / "atomic-write"
atomic_dir.mkdir()
atomic_target = atomic_dir / "reconciliation.env"
atomic_target.write_bytes(b"prior output\n")
real_replace = module.os.replace


def fail_replace(source, destination):
    raise OSError("injected replacement failure")


module.os.replace = fail_replace
try:
    try:
        module.atomic_write(atomic_target, b"new output\n")
    except OSError:
        pass
    else:
        raise AssertionError("injected atomic replacement failure was ignored")
finally:
    module.os.replace = real_replace
check(atomic_target.read_bytes() == b"prior output\n", "failed atomic write replaced prior")
check(not list(atomic_dir.glob(".reconciliation.env.tmp.*")), "atomic temp survived")
passed("atomic replacement preserves prior output when publication fails")

case = Case()
case.set_commits(commit_b)
case.result(commit_b)
case.run(0)
parse_report(case)
check(
    not [path for path in case.output.iterdir() if path.name.startswith(".")],
    "temporary output remains after success",
)
bad = Case()
bad.baseline.write_bytes(b"bad\n")
bad.run(4)
parse_report(bad)
check(
    not [path for path in bad.output.iterdir() if path.name.startswith(".")],
    "temporary output remains after failure",
)
passed("no temporary output files remain after success or failure")

case = Case()
case.set_commits(commit_b, commit_c)
case.result(commit_b)
case.result(commit_c, "failed")
tracked_inputs = [case.baseline, case.current, case.commits] + sorted(case.results.iterdir())
before = {path: path.read_bytes() for path in tracked_inputs}
case.run(3)
parse_report(case)
after = {path: path.read_bytes() for path in tracked_inputs}
check(before == after, "an input file changed during reconciliation")
passed("all state, commit-list, and result inputs remain byte-for-byte unchanged")

case = Case()
hidden_name = ".ci" + "-state"
hidden = case.root / hidden_name
hidden.mkdir()
sentinel = hidden / "sentinel"
sentinel.write_bytes(b"preserve\n")
case.run(0)
parse_report(case)
check(sentinel.read_bytes() == b"preserve\n", "hidden state sentinel changed")
source = reconciler.read_text()
check(hidden_name not in source, "production source references the hidden state path")
absent = Case()
absent.run(0)
parse_report(absent)
check(not (absent.root / hidden_name).exists(), "hidden state directory was created")
passed("the reconciler neither references, creates, nor changes the hidden state path")

tree = ast.parse(source)
imports = set()
for node in ast.walk(tree):
    if isinstance(node, ast.Import):
        imports.update(alias.name.split(".")[0] for alias in node.names)
    elif isinstance(node, ast.ImportFrom) and node.module:
        imports.add(node.module.split(".")[0])
check("subprocess" not in imports, "production source imports subprocess")
check("urllib" not in imports and "socket" not in imports, "network module imported")
check("CI_JOB_TOKEN" not in source, "production source references the CI job token")
check("os.system" not in source and "os.popen" not in source, "external command API used")
for forbidden in ("reframe", "prrte", "slurm", "sbatch", "srun"):
    check(forbidden not in source.lower(), f"production source references {forbidden}")
passed("production code has no network or external-command capability")

nonregular_cases = []
case = Case()
case.baseline.unlink()
case.baseline.mkdir()
nonregular_cases.append(case)
case = Case()
case.commits.unlink()
case.commits.mkdir()
nonregular_cases.append(case)
for case in nonregular_cases:
    case.run(4)
    report = parse_report(case)
    check(report["RECONCILIATION_RESULT"] == "invalid", "non-regular input accepted")
passed("non-regular state and commit-list inputs are rejected")

temporary_root.cleanup()
print(f"1..{pass_count}")
PY
