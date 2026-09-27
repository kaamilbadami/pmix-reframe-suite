#!/bin/bash
set -euo pipefail

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
status_helper="$script_dir/pmix_scheduled_status.py"

python3 - "$status_helper" <<'PY'
import importlib.util
from pathlib import Path
import subprocess
import sys
import tempfile


helper = Path(sys.argv[1]).resolve()
pipeline_id = "456"
other_pipeline_id = "457"
suite = "a" * 40
other_suite = "b" * 40
pass_count = 0
temporary = tempfile.TemporaryDirectory()
root = Path(temporary.name)


def check(condition, message):
    if not condition:
        raise AssertionError(message)


def passed(message):
    global pass_count
    pass_count += 1
    print(f"ok - {message}")


def run(*arguments, expected=0):
    completed = subprocess.run(
        [sys.executable, str(helper), *arguments],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    check(
        completed.returncode == expected,
        f"expected {expected}, got {completed.returncode}: "
        f"{completed.stderr.decode(errors='replace')}",
    )
    return completed


directory = root / "scheduled"
directory.mkdir()
(directory / "collector-status.env").write_text("stale\n")
(directory / "reconciliation-status.env").write_text("stale\n")
(directory / ".collector-status.env.tmp.old").write_text("stale\n")
(directory / ".reconciliation-status.env.tmp.old").write_text("stale\n")
run("clean", "--directory", str(directory))
check(not list(directory.iterdir()), "clean left stale status output")
passed("stale collector, reconciliation, and temporary markers are removed")

for kind in ("collector", "reconciliation"):
    run(
        "publish",
        "--directory", str(directory),
        "--kind", kind,
        "--status", "3",
        "--pipeline-id", pipeline_id,
        "--suite-commit", suite,
    )
    completed = run(
        "verify",
        "--directory", str(directory),
        "--kind", kind,
        "--pipeline-id", pipeline_id,
        "--suite-commit", suite,
    )
    check(completed.stdout == b"3\n", f"{kind} status changed")
    check(not list(directory.glob(f".{kind}-status.env.tmp.*")),
          f"{kind} publication left a temporary")
passed("current pipeline and suite markers publish and verify atomically")

for kind in ("collector", "reconciliation"):
    for label, supplied_pipeline, supplied_suite in (
        ("pipeline", other_pipeline_id, suite),
        ("suite", pipeline_id, other_suite),
    ):
        run(
            "verify",
            "--directory", str(directory),
            "--kind", kind,
            "--pipeline-id", supplied_pipeline,
            "--suite-commit", supplied_suite,
            expected=1,
        )
        passed(f"stale {kind} marker with mismatched {label} identity is rejected")

target = root / "outside.env"
target.write_text("keep\n")
run("clean", "--directory", str(directory), "--kind", "collector")
(directory / "collector-status.env").symlink_to(target)
run("clean", "--directory", str(directory), "--kind", "collector", expected=1)
check(target.read_text() == "keep\n", "unsafe cleanup changed symlink target")
passed("stale-status cleanup rejects symlinks without following them")
(directory / "collector-status.env").unlink()

run(
    "publish",
    "--directory", str(directory),
    "--kind", "collector",
    "--status", "0",
    "--pipeline-id", pipeline_id,
    "--suite-commit", suite,
)
(directory / "collector-status.env").write_text(
    "PMIX_COLLECTOR_STATUS=0\n"
    f"CI_PIPELINE_ID={pipeline_id}\n"
    f"SUITE_COMMIT={other_suite}\n"
)
run(
    "verify",
    "--directory", str(directory),
    "--kind", "collector",
    "--pipeline-id", pipeline_id,
    "--suite-commit", suite,
    expected=1,
)
passed("tampered collector metadata is rejected")

spec = importlib.util.spec_from_file_location("status_under_test", helper)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
module.clean(directory, ["collector"])
real_replace = module.os.replace


def fail_replace(source, destination):
    raise OSError("injected replacement failure")


module.os.replace = fail_replace
try:
    try:
        module.publish(directory, "collector", "0", pipeline_id, suite)
    except OSError:
        pass
    else:
        raise AssertionError("injected marker replacement failure was accepted")
finally:
    module.os.replace = real_replace
check(not list(directory.glob(".collector-status.env.tmp.*")),
      "failed marker publication left a temporary")
passed("failed status publication removes its temporary")

temporary.cleanup()
print(f"1..{pass_count}")
PY
