#!/bin/bash
set -euo pipefail

script_dir=$(cd -- "${BASH_SOURCE[0]%/*}" && pwd -P)
python3 - "$script_dir/check_trusted_openpmix_pr.py" \
    "$script_dir/fixtures/openpmix_pr/rhc54_verified_fork_open.json" \
    "$script_dir/fixtures/openpmix_pr/kaamilbadami_same_repository_open.json" <<'PY'
import copy
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import sys


module_path = Path(sys.argv[1])
ralph_fixture = json.loads(Path(sys.argv[2]).read_text())
kaamil_fixture = json.loads(Path(sys.argv[3]).read_text())
spec = importlib.util.spec_from_file_location(
    "check_trusted_openpmix_pr", module_path
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
tests = 0


def passed(message):
    global tests
    tests += 1
    print("ok - " + message)


def check(condition, message):
    if not condition:
        raise AssertionError(message)


with tempfile.TemporaryDirectory(prefix="test-openpmix-policy-") as temporary:
    root = Path(temporary)
    original_cwd = Path.cwd()
    os.chdir(str(root))
    try:
        def run(document, number=None, expected=(), raw=None):
            Path("pr.json").write_bytes(
                raw if raw is not None else
                json.dumps(document, separators=(",", ":")).encode()
            )
            arguments = [
                "--pr-json", "pr.json",
                "--pr-number", str(
                    document.get("number", 4028) if number is None else number
                ),
                "--output", "eligibility.env",
            ]
            arguments.extend(expected)
            return module.check(arguments)

        status = run(ralph_fixture)
        check(status == module.EXIT_ELIGIBLE,
              "Ralph verified-fork fixture was rejected")
        expected_ralph = (
            "OPENPMIX_PR_ELIGIBILITY_VERSION=1\n"
            "PR_NUMBER=4028\n"
            "PR_STATE=open\n"
            "PR_DRAFT=0\n"
            "PR_AUTHOR=rhc54\n"
            "PR_BASE_REPOSITORY=openpmix/openpmix\n"
            "PR_HEAD_REPOSITORY=rhc54/openpmix\n"
            "PR_HEAD_SHA=5f08dede7495ce1b332691b3ac7eaf6478a979b9\n"
            "PR_SOURCE_POLICY=verified-author-fork\n"
        )
        check(Path("eligibility.env").read_text() == expected_ralph,
              "Ralph eligibility schema changed")
        passed("Ralph's verified rhc54/openpmix fork is accepted and recorded")

        status = run(kaamil_fixture)
        check(status == module.EXIT_ELIGIBLE,
              "Kaamil same-repository fixture was rejected")
        record = Path("eligibility.env").read_text()
        check("PR_AUTHOR=kaamilbadami\n" in record and
              "PR_HEAD_REPOSITORY=openpmix/openpmix\n" in record and
              "PR_SOURCE_POLICY=same-repository\n" in record,
              "same-repository source identity was not retained")
        check("clone_url" not in record and "example.invalid" not in record,
              "PR-provided URL reached eligibility output")
        passed("Kaamil's same-repository branch is accepted without URL data")

        ralph_same = copy.deepcopy(ralph_fixture)
        ralph_same["head"]["repo"]["full_name"] = "openpmix/openpmix"
        check(run(ralph_same) == module.EXIT_ELIGIBLE,
              "Ralph same-repository branch was rejected")
        check("PR_SOURCE_POLICY=same-repository\n" in
              Path("eligibility.env").read_text(),
              "Ralph same-repository policy was not recorded")
        passed("Ralph's same-repository branch is accepted")

        kaamil_fork = copy.deepcopy(kaamil_fixture)
        kaamil_fork["head"]["repo"]["full_name"] = "kaamilbadami/openpmix"
        check(run(kaamil_fork) == module.EXIT_REJECTED,
              "unverified Kaamil fork was accepted")
        check(not Path("eligibility.env").exists(),
              "unverified Kaamil fork published eligibility")
        passed("kaamilbadami/openpmix remains rejected until explicitly verified")

        unauthorized_sources = (
            "thirdparty/openpmix",
            "openpmix/other",
            "rhc54/not-openpmix",
            "someorg/openpmix",
        )
        for repository in unauthorized_sources:
            document = copy.deepcopy(ralph_fixture)
            document["head"]["repo"]["full_name"] = repository
            check(run(document) == module.EXIT_REJECTED,
                  "unauthorized source accepted: " + repository)
            check(not Path("eligibility.env").exists(),
                  "unauthorized source published eligibility")
        passed("arbitrary user and organization repositories are rejected")

        wrong_author = copy.deepcopy(ralph_fixture)
        wrong_author["user"]["login"] = "not-allowlisted"
        wrong_author["head"]["repo"]["full_name"] = "openpmix/openpmix"
        check(run(wrong_author) == module.EXIT_REJECTED,
              "disallowed same-repository author was accepted")
        passed("the author allowlist contains only the two required users")

        wrong_pair = copy.deepcopy(ralph_fixture)
        wrong_pair["user"]["login"] = "kaamilbadami"
        check(run(wrong_pair) == module.EXIT_REJECTED,
              "Ralph's fork was accepted for Kaamil")
        passed("verified fork permission is bound to its authoritative author")

        wrong_base = copy.deepcopy(ralph_fixture)
        wrong_base["base"]["repo"]["full_name"] = "rhc54/openpmix"
        check(run(wrong_base) == module.EXIT_REJECTED,
              "wrong base repository was accepted")
        passed("the base repository must be exactly openpmix/openpmix")

        closed = copy.deepcopy(ralph_fixture)
        closed["state"] = "closed"
        check(run(closed) == module.EXIT_REJECTED, "closed PR was accepted")
        draft = copy.deepcopy(ralph_fixture)
        draft["draft"] = True
        check(run(draft) == module.EXIT_REJECTED, "draft PR was accepted")
        for invalid_draft in (None, 0, 1, "false", [], {}):
            document = copy.deepcopy(ralph_fixture)
            document["draft"] = invalid_draft
            check(run(document) == module.EXIT_INVALID,
                  "non-boolean draft was accepted")
        missing_draft = copy.deepcopy(ralph_fixture)
        del missing_draft["draft"]
        check(run(missing_draft) == module.EXIT_INVALID,
              "missing draft field was accepted")
        passed("only open, explicitly non-draft PRs are eligible")

        for invalid_sha in (
                "5F08DEDE7495CE1B332691B3AC7EAF6478A979B9",
                "5f08dede",
                "g" * 40,
                "5f08dede7495ce1b332691b3ac7eaf6478a979b9 ",
                "refs/heads/main"):
            document = copy.deepcopy(ralph_fixture)
            document["head"]["sha"] = invalid_sha
            check(run(document) == module.EXIT_INVALID,
                  "invalid SHA accepted: {!r}".format(invalid_sha))
        passed("head SHA must be exactly 40 lowercase hexadecimal characters")

        for invalid_number in ("", "0", "01", "+1", "-1", " 4028",
                               "4028 ", "4/28", "refs/pull/1/head"):
            check(run(ralph_fixture, number=invalid_number) ==
                  module.EXIT_INVALID,
                  "noncanonical CLI PR number accepted")
        mismatch = copy.deepcopy(ralph_fixture)
        mismatch["number"] = 4027
        check(run(mismatch, number="4028") == module.EXIT_INVALID,
              "CLI/JSON PR mismatch was accepted")
        passed("PR number must be canonical and match authoritative metadata")

        malformed_documents = []
        for path in (
                ("user",), ("head",), ("head", "repo"), ("base",),
                ("base", "repo")):
            document = copy.deepcopy(ralph_fixture)
            target = document
            for component in path[:-1]:
                target = target[component]
            del target[path[-1]]
            malformed_documents.append(document)
        for field_owner, field in (
                ((), "id"), ((), "number"), (("user",), "id"),
                (("head", "repo"), "id"), (("base", "repo"), "id")):
            for invalid in (True, False, 0, -1, "1", None):
                document = copy.deepcopy(ralph_fixture)
                target = document
                for component in field_owner:
                    target = target[component]
                target[field] = invalid
                malformed_documents.append(document)
        for document in malformed_documents:
            check(run(document) == module.EXIT_INVALID,
                  "incomplete or invalid metadata was accepted")
        passed("missing objects and malformed authoritative IDs fail closed")

        duplicate = (
            b'{"id":1,"id":2,"number":4028,"state":"open","draft":false,'
            b'"user":{"id":1,"login":"rhc54"},"head":{"sha":"'
            b'5f08dede7495ce1b332691b3ac7eaf6478a979b9","repo":{"id":2,'
            b'"full_name":"rhc54/openpmix"}},"base":{"repo":{"id":3,'
            b'"full_name":"openpmix/openpmix"}}}'
        )
        check(run(ralph_fixture, raw=duplicate) == module.EXIT_INVALID,
              "duplicate JSON key was accepted")
        passed("duplicate JSON object keys are rejected")

        matching = (
            "--expected-head-sha",
            ralph_fixture["head"]["sha"],
            "--expected-author",
            "rhc54",
            "--expected-head-repository",
            "rhc54/openpmix",
        )
        check(run(ralph_fixture, expected=matching) == module.EXIT_ELIGIBLE,
              "matching revalidation identity failed")
        for field, replacement in (
                ("sha", "0" * 40),
                ("author", "kaamilbadami"),
                ("repository", "openpmix/openpmix")):
            expected = list(matching)
            option_index = {
                "sha": 1,
                "author": 3,
                "repository": 5,
            }[field]
            expected[option_index] = replacement
            Path("eligibility.env").write_text("stale")
            check(run(ralph_fixture, expected=tuple(expected)) ==
                  module.EXIT_CHANGED,
                  "changed {} did not receive status 5".format(field))
            check(not Path("eligibility.env").exists(),
                  "changed identity left stale eligibility")
        passed("SHA, author, and head repository are revalidation-bound")

        for invalid_expected in ("ABC", "0" * 39, "g" * 40):
            check(run(ralph_fixture, expected=(
                "--expected-head-sha", invalid_expected,
            )) == module.EXIT_INVALID,
                  "malformed expected SHA was accepted")
        passed("malformed expected identity is invalid local configuration")

        Path(".ci-state").mkdir()
        state = Path(".ci-state/eligibility.env")
        state.write_text("protected")
        Path("pr.json").write_text(json.dumps(ralph_fixture))
        status = module.check([
            "--pr-json", "pr.json", "--pr-number", "4028",
            "--output", str(state),
        ])
        check(status == module.EXIT_INVALID and state.read_text() == "protected",
              "state path was modified")
        passed(".ci-state paths are rejected without modification")

        source = module_path.read_text()
        forbidden = ("clone_url", "subprocess", "urllib", "requests",
                     "git clone", "statuses/", "sbatch", "srun")
        check(not any(value in source for value in forbidden),
              "policy helper gained external side-effect capability")
        passed("policy validation has no network, clone, status, or scheduler capability")
    finally:
        os.chdir(str(original_cwd))

print("1..{}".format(tests))
PY
