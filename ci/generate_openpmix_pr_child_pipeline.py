#!/usr/bin/env python3
"""Generate isolated bridge jobs for eligible OpenPMIx PR heads."""

import json
import os
from pathlib import Path
import re
import sys
import tempfile


NUMBER_RE = re.compile(r"[1-9][0-9]*\Z")
SHA_RE = re.compile(r"[0-9a-f]{40}\Z")
KEYS = (
    "pr_number", "head_sha", "author", "base_repository",
    "head_repository", "source_policy",
)
HEADER = """stages:
  - trigger

"""
NOOP = """no-eligible-openpmix-prs:
  stage: trigger
  script:
    - printf '%s\\n' 'No new eligible OpenPMIx PR heads were discovered.'
"""
JOB = """openpmix-pr-{number}-{short_sha}:
  stage: trigger
  variables:
    OPENPMIX_PR_INTERNAL: "1"
    OPENPMIX_PR_NUMBER: "{number}"
    OPENPMIX_PR_EXPECTED_SHA: "{sha}"
  trigger:
    include:
      - local: ci/openpmix_pr_child.yml
    strategy: mirror
"""


def fail(message):
    print("error: " + message, file=sys.stderr)
    raise SystemExit(2)


def read_records(path):
    records = []
    seen = set()
    try:
        lines = path.read_text(encoding="ascii").splitlines()
    except OSError:
        fail("could not read discovery records")
    for line in lines:
        try:
            record = json.loads(line)
        except ValueError:
            fail("discovery record is malformed")
        if (not isinstance(record, dict) or tuple(record) != KEYS or
                any(not isinstance(record[key], str) for key in KEYS) or
                NUMBER_RE.fullmatch(record["pr_number"]) is None or
                SHA_RE.fullmatch(record["head_sha"]) is None or
                record["base_repository"] != "openpmix/openpmix" or
                (record["author"], record["head_repository"],
                 record["source_policy"]) not in (
                    ("kaamilbadami", "openpmix/openpmix",
                     "same-repository"),
                    ("rhc54", "openpmix/openpmix", "same-repository"),
                    ("rhc54", "rhc54/openpmix", "verified-author-fork"),
                )):
            fail("discovery record schema is invalid")
        pair = (record["pr_number"], record["head_sha"])
        if pair in seen:
            fail("duplicate discovery record")
        seen.add(pair)
        records.append(record)
    return records


def main():
    if len(sys.argv) != 3:
        fail("expected INPUT and OUTPUT")
    source = Path(sys.argv[1])
    output = Path(sys.argv[2])
    if (source.is_symlink() or not source.is_file() or output.is_absolute() or
            ".." in output.parts or not output.parent.is_dir()):
        fail("unsafe pipeline input or output")
    try:
        if output.is_symlink() or (output.exists() and not output.is_file()):
            fail("unsafe stale pipeline output")
        output.unlink()
    except FileNotFoundError:
        pass
    records = read_records(source)
    content = HEADER + ("\n".join(
        JOB.format(
            number=record["pr_number"],
            sha=record["head_sha"],
            short_sha=record["head_sha"][:12],
        ) for record in records
    ) if records else NOOP)
    name = None
    try:
        with tempfile.NamedTemporaryFile(
                mode="w", encoding="ascii", dir=str(output.parent),
                prefix="." + output.name + ".", delete=False) as temporary:
            name = temporary.name
            temporary.write(content)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(name, str(output))
    except OSError:
        if name:
            try:
                os.unlink(name)
            except OSError:
                pass
        fail("could not publish generated pipeline")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
