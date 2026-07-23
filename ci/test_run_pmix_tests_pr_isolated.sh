#!/bin/bash
set -euo pipefail

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)

python3.11 - "$script_dir/run_pmix_tests_pr_isolated.sh" <<'PY'
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


source = Path(sys.argv[1]).resolve()
passed_count = 0


def check(condition, message):
    if not condition:
        raise AssertionError(message)


def passed(message):
    global passed_count
    passed_count += 1
    print(f"ok - {message}")


with tempfile.TemporaryDirectory() as temporary:
    root = Path(temporary)
    ci = root / "ci"
    ci.mkdir()
    shutil.copy2(source, ci / source.name)
    child = ci / "run_trusted_pmix_tests_pr.sh"
    child.write_text(r'''#!/bin/bash
set -euo pipefail
/usr/bin/env -0 > isolated.env
''')
    child.chmod(0o755)

    environment = os.environ.copy()
    environment.update({
        "GITHUB_PR_READ_TOKEN": "secret-read",
        "GITHUB_STATUS_TOKEN": "secret-status",
        "CI_JOB_TOKEN": "secret-job",
        "CI_REPOSITORY_URL": "https://token@example.invalid/repository.git",
        "CI_JOB_JWT": "secret-jwt",
        "PROTECTED_DEPLOY_PASSWORD": "secret-project-variable",
        "RUNNER_UNRELATED_VALUE": "runner-value",
        "HTTP_PROXY": "http://credential@example.invalid",
        "CRAY_PASSWORD": "secret-cray",
        "MODULE_TOKEN": "secret-module",
        "MODULEPATH": "/hostile/modules",
        "PATH": "/hostile/bin",
        "LD_LIBRARY_PATH": "/hostile/lib",
        "PKG_CONFIG_PATH": "/hostile/pkgconfig",
        "CI_JOB_JWT_V2": "secret-jwt-v2",
        "OIDC_ID_TOKEN": "secret-oidc",
        "CI_PIPELINE_ID": "456",
    })
    completed = subprocess.run(
        ["/bin/bash", f"ci/{source.name}"], cwd=root, env=environment,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
    )
    check(completed.returncode == 0,
          f"isolated launcher failed ({completed.returncode}): " +
          completed.stderr.decode(errors="replace"))
    entries = (root / "isolated.env").read_bytes().split(b"\0")
    values = {}
    for entry in entries:
        if not entry:
            continue
        name, value = entry.decode().split("=", 1)
        values[name] = value
    forbidden = {
        "GITHUB_PR_READ_TOKEN", "GITHUB_STATUS_TOKEN", "CI_JOB_TOKEN",
        "CI_REPOSITORY_URL", "CI_JOB_JWT", "PROTECTED_DEPLOY_PASSWORD",
        "CI_JOB_JWT_V2", "OIDC_ID_TOKEN", "RUNNER_UNRELATED_VALUE",
        "HTTP_PROXY", "CRAY_PASSWORD", "MODULE_TOKEN",
    }
    check(forbidden.isdisjoint(values),
          f"protected or unrelated variables crossed env -i: {forbidden & set(values)}")
    passed("GitHub, GitLab, JWT, proxy, protected, and unrelated runner variables are absent")

    for required in (
        "HOME", "TMPDIR", "PATH", "LANG", "LC_ALL", "SHELL", "USER",
        "LOGNAME", "PMIX_PYTHON", "RFM_BIN", "PYTHONPATH", "CI_PIPELINE_ID",
    ):
        check(required in values, f"safe allowlisted variable missing: {required}")
    check(values["PMIX_PYTHON"] ==
          "/lustre/orion/gen243/proj-shared/pmix-reframe-ci-tools/pmix-py310/bin/python",
          "PMIX_PYTHON is not the fixed installation")
    check(values["RFM_BIN"] ==
          "/lustre/orion/gen243/proj-shared/pmix-reframe-ci-tools/reframe-4.10/bin/reframe",
          "RFM_BIN is not the fixed installation")
    check(values["PYTHONPATH"] ==
          "/lustre/orion/gen243/proj-shared/pmix-reframe-ci-tools/reframe-4.10/lib/python3.11/site-packages",
          "ReFrame Python path is not fixed")
    check(values["CI_PIPELINE_ID"] == "456",
          "current pipeline identity did not cross the clean boundary")
    check(values["PATH"] != "/hostile/bin"
          and values["MODULEPATH"] != "/hostile/modules"
          and values.get("LD_LIBRARY_PATH") != "/hostile/lib"
          and values.get("PKG_CONFIG_PATH") != "/hostile/pkgconfig",
          "hostile search or loader path crossed env -i")
    passed("fixed tool paths and regenerated Frontier module state replace caller paths")

    secret_values = (
        "secret-read", "secret-status", "secret-job", "secret-jwt",
        "secret-project-variable", "token@example.invalid", "secret-cray",
        "secret-module", "secret-jwt-v2", "secret-oidc", "/hostile/",
    )
    serialized = b"\0".join(entries).decode(errors="replace")
    check(not any(secret in serialized for secret in secret_values),
          "a protected value crossed the clean environment")
    passed("protected values are absent from the execution process, not merely renamed")

    root2 = root / "unsafe"
    (root2 / "ci").mkdir(parents=True)
    shutil.copy2(source, root2 / "ci" / source.name)
    (root2 / "ci/run_trusted_pmix_tests_pr.sh").write_text("#!/bin/bash\nexit 0\n")
    (root2 / "ci/run_trusted_pmix_tests_pr.sh").chmod(0o755)
    target = root2 / "target"
    target.mkdir()
    (root2 / ".ci-pr-execution-home").symlink_to(target, target_is_directory=True)
    rejected = subprocess.run(
        ["/bin/bash", f"ci/{source.name}"], cwd=root2, env=environment,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
    )
    check(rejected.returncode == 2 and not list(target.iterdir()),
          "isolated home symlink was followed or modified")
    passed("isolated HOME and TMPDIR must be fresh real directories")

    root3 = root / "stale-output"
    (root3 / "ci").mkdir(parents=True)
    shutil.copy2(source, root3 / "ci" / source.name)
    (root3 / "ci/run_trusted_pmix_tests_pr.sh").write_text("#!/bin/bash\nexit 23\n")
    (root3 / "ci/run_trusted_pmix_tests_pr.sh").chmod(0o755)
    stale = root3 / "ci-pr-execution"
    stale.mkdir()
    (stale / "result.env").write_text("stale result\n")
    failed = subprocess.run(
        ["/bin/bash", f"ci/{source.name}"], cwd=root3, env=environment,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
    )
    check(failed.returncode == 23 and not stale.exists(),
          "clean-boundary failure retained a stale execution artifact")
    passed("execution entry removes stale output before clean-boundary setup")

source_text = source.read_text()
check("exec /usr/bin/env -i" in source_text,
      "production launcher does not use absolute env -i")
for forbidden_name in (
    "GITHUB_PR_READ_TOKEN", "GITHUB_STATUS_TOKEN", "CI_JOB_TOKEN",
    "CI_REPOSITORY_URL", "CI_JOB_JWT", "PROTECTED", "CRAY*", "PE_*",
    "LMOD*", "MODULE*", '"PATH=$PATH"', "LD_LIBRARY_PATH|",
):
    check(forbidden_name not in source_text,
          f"production allowlist names a protected variable: {forbidden_name}")
passed("production isolation is an allowlist and contains no credential-name passthrough")

print(f"1..{passed_count}")
PY
