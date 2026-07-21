#!/usr/bin/env python3

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
  resource_group: pmix-python-suite-frontier
  variables:
    PMIX_COMMIT: "__SHA__"
  script:
    - |
      set -euo pipefail
      module load miniforge3/23.11.0-0
      python3 -m venv .ci-venv
      source .ci-venv/bin/activate
      python -m pip install --upgrade pip
      python -m pip install "Cython==3.2.6" "reframe-hpc==4.10.0"
      export PMIX_PYTHON="${CI_PROJECT_DIR}/.ci-venv/bin/python"
      export RFM_BIN="${CI_PROJECT_DIR}/.ci-venv/bin/reframe"
      bash ci/run_exact_pmix_commit.sh
  after_script:
    - bash ci/write_pmix_commit_result.sh ci-results
  artifacts:
    when: always
    expire_in: 14 days
    paths:
      - ci-results/__RESULT_SHA__.env
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


def render_pipeline(shas):
    jobs = [
        COMMIT_JOB.replace("__SHA__", sha).replace("__RESULT_SHA__", sha.lower())
        for sha in shas
    ]
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


def main():
    if len(sys.argv) != 3:
        print(
            "usage: generate_pmix_child_pipeline.py INPUT_SHA_FILE OUTPUT_YAML",
            file=sys.stderr,
        )
        return 2

    input_path = Path(sys.argv[1])
    output_path = Path(sys.argv[2])
    if not input_path.is_file():
        fail(f"input SHA file is missing: {input_path}")
    if not output_path.parent.is_dir():
        fail(f"output directory does not exist: {output_path.parent}")
    if output_path.is_dir():
        fail(f"output YAML is an existing directory: {output_path}")
    if input_path.resolve() == output_path.resolve():
        fail("input SHA file and output YAML must be different files")

    atomic_write(output_path, render_pipeline(read_shas(input_path)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
