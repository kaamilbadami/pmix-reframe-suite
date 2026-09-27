#!/usr/bin/env python3

import argparse
import os
from pathlib import Path
import re
import sys
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
  timeout: 1h
  variables:
    PMIX_COMMIT: "__SHA__"
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
    - bash ci/write_pmix_commit_result.sh ci-results
  artifacts:
    when: always
    expire_in: 14 days
    paths:
      - ci-results/__RESULT_SHA__.env
      - .ci-work/$CI_JOB_ID/reframe/output/
      - .ci-work/$CI_JOB_ID/reframe/perflogs/
      - .ci-work/$CI_JOB_ID/reframe/stage/frontier/batch/pmix_test/fetch_prrte_*/prrte-source.env
      - .ci-work/$CI_JOB_ID/reframe/stage/frontier/batch/pmix_test/PMIxPython*Compat*Test/
      - .ci-work/$CI_JOB_ID/reframe/stage/frontier/batch/pmix_test/PMIxPythonMappingPPRL3CacheTest/
"""

FAILED_COMMIT_JOB = """\
pmix-__SHA__:
  stage: test
  extends:
    - .frontier-shell-runner
  timeout: 1h
  variables:
    PMIX_COMMIT: "__SHA__"
  script:
    - |
      set -euo pipefail
      export PMIX_JOB_ROOT="${CI_PROJECT_DIR}/.ci-work/${CI_JOB_ID}"
      mkdir -p -- "$PMIX_JOB_ROOT"
      printf 'Intentional multi-commit pilot failure for OpenPMIx commit: %s\\n' "$PMIX_COMMIT" >&2
      exit 1
  after_script:
    - bash ci/write_pmix_commit_result.sh ci-results
  artifacts:
    when: always
    expire_in: 14 days
    paths:
      - ci-results/__RESULT_SHA__.env
      - .ci-work/$CI_JOB_ID/reframe/output/
      - .ci-work/$CI_JOB_ID/reframe/perflogs/
      - .ci-work/$CI_JOB_ID/reframe/stage/frontier/batch/pmix_test/fetch_prrte_*/prrte-source.env
      - .ci-work/$CI_JOB_ID/reframe/stage/frontier/batch/pmix_test/PMIxPython*Compat*Test/
      - .ci-work/$CI_JOB_ID/reframe/stage/frontier/batch/pmix_test/PMIxPythonMappingPPRL3CacheTest/
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


def render_pipeline(shas, fail_commit=None):
    jobs = []
    for sha in shas:
        template = COMMIT_JOB
        if fail_commit is not None and sha.lower() == fail_commit:
            template = FAILED_COMMIT_JOB
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
            Path(temporary_name).unlink(missing_ok=True)
        fail(f"could not write output YAML: {output_path}: {error}")


def parse_arguments(arguments=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("input_sha_file", type=Path)
    parser.add_argument("output_yaml", type=Path)
    parser.add_argument("--fail-commit")
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

    atomic_write(output_path, render_pipeline(shas, fail_commit))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
