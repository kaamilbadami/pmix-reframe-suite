#!/bin/bash
set -euo pipefail

script_dir=$(cd -- "${BASH_SOURCE[0]%/*}" && pwd -P)
python3 - "$script_dir/run_pmix_tests_pr_isolated.sh" \
    "$script_dir/run_openpmix_pr_isolated.sh" <<'PY'
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


shared = Path(sys.argv[1]).resolve()
wrapper = Path(sys.argv[2]).resolve()
passed_count = 0


def check(condition, message):
    if not condition:
        raise AssertionError(message)


def passed(message):
    global passed_count
    passed_count += 1
    print("ok - " + message)


with tempfile.TemporaryDirectory(prefix="openpmix-isolation-") as temporary:
    root = Path(temporary)
    ci = root / "ci"
    ci.mkdir()
    shutil.copy2(shared, ci / shared.name)
    shutil.copy2(wrapper, ci / wrapper.name)
    child = ci / "run_trusted_openpmix_pr.sh"
    child.write_text(
        "#!/bin/bash\nset -euo pipefail\n/usr/bin/env -0 > openpmix.env\n")
    child.chmod(0o755)
    environment = os.environ.copy()
    environment.update({
        "CI_PIPELINE_ID": "456",
        "GITHUB_PR_READ_TOKEN": "protected-read",
        "GITHUB_STATUS_TOKEN": "protected-status",
        "CI_JOB_TOKEN": "protected-job",
        "CI_REPOSITORY_URL": "https://token@example.invalid/repo.git",
        "CI_JOB_JWT": "protected-jwt",
        "OPENPMIX_PR_PROTECTED_SENTINEL": "sentinel-value",
        "RUNNER_SECRET": "protected-runner",
        "PATH": "/hostile/bin",
        "LD_PRELOAD": "/hostile/preload.so",
        "MODULEPATH": "/hostile/modules",
    })
    completed = subprocess.run(
        ["/bin/bash", "ci/run_openpmix_pr_isolated.sh"],
        cwd=str(root), env=environment, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, check=False,
    )
    check(completed.returncode == 0, completed.stderr.decode(errors="replace"))
    entries = (root / "openpmix.env").read_bytes().split(b"\0")
    values = {}
    for entry in entries:
        if entry:
            key, value = entry.decode().split("=", 1)
            values[key] = value
    forbidden = {
        "GITHUB_PR_READ_TOKEN", "GITHUB_STATUS_TOKEN", "CI_JOB_TOKEN",
        "CI_REPOSITORY_URL", "CI_JOB_JWT",
        "OPENPMIX_PR_PROTECTED_SENTINEL", "RUNNER_SECRET", "LD_PRELOAD",
    }
    check(forbidden.isdisjoint(values),
          "credential or sentinel crossed env -i")
    check(values.get("CI_PIPELINE_ID") == "456",
          "pipeline identity did not cross clean boundary")
    check(values.get("PMIX_PYTHON", "").startswith(
          "/lustre/orion/gen243/proj-shared/pmix-reframe-ci-tools/"),
          "fixed PMIx Python was not selected")
    check(values.get("RFM_BIN", "").startswith(
          "/lustre/orion/gen243/proj-shared/pmix-reframe-ci-tools/"),
          "fixed ReFrame was not selected")
    passed("OpenPMIx build receives only the fixed credential-free environment")

    root2 = root / "unsafe"
    ci2 = root2 / "ci"
    ci2.mkdir(parents=True)
    shutil.copy2(shared, ci2 / shared.name)
    shutil.copy2(wrapper, ci2 / wrapper.name)
    child2 = ci2 / "run_trusted_openpmix_pr.sh"
    child2.write_text("#!/bin/bash\nexit 0\n")
    child2.chmod(0o755)
    stale = root2 / "ci-openpmix-pr-execution"
    stale.mkdir()
    (stale / "result.env").write_text("stale\n")
    unsafe = root2 / ".ci-openpmix-pr-execution-home"
    unsafe.mkdir()
    rejected = subprocess.run(
        ["/bin/bash", "ci/run_openpmix_pr_isolated.sh"],
        cwd=str(root2), env=environment, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, check=False,
    )
    check(rejected.returncode == 2 and not stale.exists(),
          "stale output survived an isolation-boundary failure")
    passed("stale OpenPMIx result artifacts are cleared before setup")

text = shared.read_text()
check("exec /usr/bin/env -i" in text, "absolute env -i is absent")
check("/bin/bash --noprofile --norc" in text,
      "profile-free shell boundary is absent")
check(text.index(". /etc/profile.d/olcf-env.sh") <
      text.index(". /etc/bash.bashrc.local") <
      text.index("module load"),
      "system Frontier initialization order changed")
passed("OpenPMIx isolation preserves env -i, profile-free Bash, and system initialization")

wrapper_text = wrapper.read_text()
check("openpmix-pr" in wrapper_text and
      "run_pmix_tests_pr_isolated.sh" in wrapper_text,
      "wrapper does not select the shared fixed OpenPMIx mode")
passed("OpenPMIx reuses the proven isolation helper through a fixed mode")

print("1..{}".format(passed_count))
PY
