#!/usr/bin/env python3

import argparse
import os
from pathlib import Path
import re
import sys
import subprocess
import tempfile


SHA_PATTERN = re.compile(r"[0-9A-Fa-f]{40}")

HEADER = """\
stages:
  - test

include:
  - project: ci/resources/templates
    ref: main
    file:
      - /runners.yml

variables:
  OLCF_SERVICE_ACCOUNT: "gen243_auser"
  FF_GIT_URLS_WITHOUT_TOKENS: "1"
"""

NOOP_JOB = """\
no-untested-pmix-commits:
  stage: test
  extends:
    - .frontier-shell-runner
  script:
    - |
      printf '%s\\n' 'No untested OpenPMIx commits were discovered.'
"""

COMMIT_JOB = """\
pmix-__SHA__:
  stage: test
  extends:
    - .frontier-shell-runner
  timeout: 2h
  variables:
    PMIX_COMMIT: "__SHA__"
    RUNNER_SCRIPT_TIMEOUT: "1h45m"
    RUNNER_AFTER_SCRIPT_TIMEOUT: "5m"
  script:
    - |
      set -euo pipefail
      module load miniforge3/23.11.0-0
      export PMIX_JOB_ROOT="${CI_PROJECT_DIR}/.ci-work/${CI_JOB_ID}"
      export PMIX_VENV="${PMIX_JOB_ROOT}/venv"
      export PMIX_RFM_PREFIX="${PMIX_JOB_ROOT}/reframe"
      export PIP_CACHE_DIR="${PMIX_JOB_ROOT}/pip-cache"
      mkdir -p -- "$PMIX_JOB_ROOT" "$PIP_CACHE_DIR"
      python3 -m venv "$PMIX_VENV"
      source "$PMIX_VENV/bin/activate"
      python -m pip install --upgrade pip
      python -m pip install "Cython==3.2.6" "reframe-hpc==4.10.0"
      export PMIX_PYTHON="${PMIX_VENV}/bin/python"
      export RFM_BIN="${PMIX_VENV}/bin/reframe"
      bash ci/run_exact_pmix_commit.sh
  after_script:
    - python3 ci/cleanup_pmix_slurm_jobs.py || echo 'WARNING - inspect Slurm cleanup errors above'
    - bash ci/write_pmix_commit_result.sh ci-results
  artifacts:
    when: always
    expire_in: 14 days
    paths:
      - ci-results/__RESULT_SHA__.env
      - ci-results/slurm-cleanup-$CI_JOB_ID.json
      - .ci-work/$CI_JOB_ID/reframe/stage/**/rfm_job.sh
      - .ci-work/$CI_JOB_ID/reframe/output/
      - .ci-work/$CI_JOB_ID/reframe/perflogs/
      - .ci-work/$CI_JOB_ID/reframe/stage/frontier/batch/pmix_test/fetch_prrte_*/prrte-source.env
      - .ci-work/$CI_JOB_ID/reframe/stage/frontier/batch/pmix_test/PMIxPython*Compat*Test/
      - .ci-work/$CI_JOB_ID/reframe/stage/frontier/batch/pmix_test/PMIxPythonMappingPPRL3CacheTest/
      - .ci-work/$CI_JOB_ID/reframe/stage/frontier/batch/pmix_test/PMIxPythonScalingMultinodeTest/
"""

FAILED_COMMIT_JOB = """\
pmix-__SHA__:
  stage: test
  extends:
    - .frontier-shell-runner
  timeout: 2h
  variables:
    PMIX_COMMIT: "__SHA__"
    RUNNER_SCRIPT_TIMEOUT: "1h45m"
    RUNNER_AFTER_SCRIPT_TIMEOUT: "5m"
  script:
    - |
      set -euo pipefail
      export PMIX_JOB_ROOT="${CI_PROJECT_DIR}/.ci-work/${CI_JOB_ID}"
      mkdir -p -- "$PMIX_JOB_ROOT"
      printf 'Intentional multi-commit pilot failure for OpenPMIx commit: %s\\n' "$PMIX_COMMIT" >&2
      exit 1
  after_script:
    - python3 ci/cleanup_pmix_slurm_jobs.py || echo 'WARNING - inspect Slurm cleanup errors above'
    - bash ci/write_pmix_commit_result.sh ci-results
  artifacts:
    when: always
    expire_in: 14 days
    paths:
      - ci-results/__RESULT_SHA__.env
      - ci-results/slurm-cleanup-$CI_JOB_ID.json
      - .ci-work/$CI_JOB_ID/reframe/stage/**/rfm_job.sh
      - .ci-work/$CI_JOB_ID/reframe/output/
      - .ci-work/$CI_JOB_ID/reframe/perflogs/
      - .ci-work/$CI_JOB_ID/reframe/stage/frontier/batch/pmix_test/fetch_prrte_*/prrte-source.env
      - .ci-work/$CI_JOB_ID/reframe/stage/frontier/batch/pmix_test/PMIxPython*Compat*Test/
      - .ci-work/$CI_JOB_ID/reframe/stage/frontier/batch/pmix_test/PMIxPythonMappingPPRL3CacheTest/
      - .ci-work/$CI_JOB_ID/reframe/stage/frontier/batch/pmix_test/PMIxPythonScalingMultinodeTest/
"""


def fail(message, status=1):
    print(f"error: {message}", file=sys.stderr)
    raise SystemExit(status)


def read_shas(input_path):
    try:
        lines = input_path.read_text(encoding="utf-8").splitlines()
    except OSError as error:
        fail(f"could not read input SHA file: {input_path}: {error}")

    shas = []
    seen = set()
    for line_number, line in enumerate(lines, start=1):
        if not line:
            fail(f"blank input line at line {line_number}")
        if line != line.strip():
            fail(f"input whitespace at line {line_number}")
        if SHA_PATTERN.fullmatch(line) is None:
            fail(f"invalid 40-character hexadecimal SHA at line {line_number}")
        if line.lower() in seen:
            fail(f"duplicate SHA at line {line_number}: {line}")
        seen.add(line.lower())
        shas.append(line)

    return shas


PMIX_GETLINE_CHANGE = "1244a4498895f07f0b1a6e2d81c97d4104c2e12b"
PRRTE_LEGACY = ("v5.0", "22820a01e17547dbf1c4f9628eac327f193caa45")
PRRTE_GETLINE = ("master", "a783dab5948c76bca5bde1647f291d9492f33fca")


def select_prrte(shas, history_repo):
    """Select exact dependency pins using complete OpenPMIx ancestry."""
    def git(*args):
        return subprocess.run(
            ["git", "-C", str(history_repo), *args],
            universal_newlines=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )

    shallow = git("rev-parse", "--is-shallow-repository")
    if shallow.returncode or shallow.stdout.strip() != "false":
        fail("PRRTE selection requires a complete OpenPMIx history")
    # A history ending before the change legitimately has only old commits.
    boundary = git("cat-file", "-e", PMIX_GETLINE_CHANGE + "^{commit}")
    selections = {}
    for sha in shas:
        if git("cat-file", "-e", sha + "^{commit}").returncode:
            fail(f"OpenPMIx history is missing requested commit: {sha}")
        if boundary.returncode:
            # Verify the selected source itself has the legacy declaration.
            header = git("show", sha + ":src/util/pmix_string_copy.h")
            if header.returncode or not re.search(
                r"pmix_getline\s*\(\s*FILE\s*\*\s*fp\s*\)", header.stdout
            ):
                fail("OpenPMIx history lacks compatibility boundary; cannot select PRRTE")
            selections[sha] = PRRTE_LEGACY
            continue
        result = git("merge-base", "--is-ancestor", PMIX_GETLINE_CHANGE, sha)
        if result.returncode not in (0, 1):
            fail(f"OpenPMIx ancestry check failed: {result.stderr.strip()}")
        selections[sha] = PRRTE_GETLINE if result.returncode == 0 else PRRTE_LEGACY
    return selections


def render_pipeline(shas, fail_commit=None, prrte_selections=None):
    jobs = []
    for sha in shas:
        template = COMMIT_JOB
        if fail_commit is not None and sha.lower() == fail_commit:
            template = FAILED_COMMIT_JOB
        if prrte_selections is not None:
            branch, commit = prrte_selections[sha]
            template = template.replace(
                '    PMIX_COMMIT: "__SHA__"',
                '    PMIX_COMMIT: "__SHA__"\n'
                f'    PRRTE_BRANCH: "{branch}"\n'
                f'    PRRTE_COMMIT: "{commit}"'
            )
        jobs.append(
            template.replace("__SHA__", sha).replace(
                "__RESULT_SHA__", sha.lower()
            )
        )
    if not jobs:
        jobs = [NOOP_JOB]
    return "\n".join([HEADER, *jobs])


def atomic_write(output_path, content):
    temporary_name = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=output_path.parent,
            prefix=f".{output_path.name}.",
            delete=False,
        ) as temporary_file:
            temporary_name = temporary_file.name
            temporary_file.write(content)
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        os.replace(temporary_name, output_path)
    except OSError as error:
        if temporary_name is not None:
            try:
                Path(temporary_name).unlink()
            except FileNotFoundError:
                pass
        fail(f"could not write output YAML: {output_path}: {error}")


def parse_arguments(arguments=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("input_sha_file", type=Path)
    parser.add_argument("output_yaml", type=Path)
    parser.add_argument("--fail-commit")
    parser.add_argument("--select-prrte", action="store_true")
    parser.add_argument("--pmix-history", type=Path)
    return parser.parse_args(arguments)


def main(arguments=None):
    options = parse_arguments(arguments)
    input_path = options.input_sha_file
    output_path = options.output_yaml
    if not input_path.is_file():
        fail(f"input SHA file is missing: {input_path}")
    if not output_path.parent.is_dir():
        fail(f"output directory does not exist: {output_path.parent}")
    if output_path.is_dir():
        fail(f"output YAML is an existing directory: {output_path}")
    if input_path.resolve() == output_path.resolve():
        fail("input SHA file and output YAML must be different files")

    shas = read_shas(input_path)
    fail_commit = options.fail_commit
    if fail_commit is not None:
        if SHA_PATTERN.fullmatch(fail_commit) is None:
            fail("--fail-commit must be exactly 40 hexadecimal characters")
        matches = [sha for sha in shas if sha.lower() == fail_commit.lower()]
        if len(matches) != 1:
            fail("--fail-commit must match exactly one discovered commit")
        fail_commit = matches[0].lower()

    selections = None
    if options.pmix_history is not None and not options.select_prrte:
        fail("--pmix-history requires --select-prrte")
    if options.select_prrte and shas:
        if options.pmix_history is not None:
            selections = select_prrte(shas, options.pmix_history)
        else:
            with tempfile.TemporaryDirectory(prefix="pmix-prrte-selection-") as temporary:
                history = Path(temporary) / "history.git"
                result = subprocess.run([
                    "git", "clone", "--bare", "--single-branch", "--branch", "master",
                    "https://github.com/openpmix/openpmix.git", str(history)
                ], stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
                if result.returncode:
                    fail(f"could not fetch OpenPMIx history: {result.stderr.strip()}")
                selections = select_prrte(shas, history)
    atomic_write(output_path, render_pipeline(shas, fail_commit, selections))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
