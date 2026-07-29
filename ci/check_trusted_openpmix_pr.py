#!/usr/bin/env python3
"""Validate OpenPMIx PR metadata for the manual Frontier prototype.

This helper has no network, command-execution, scheduler, checkout, or status
capability.  It consumes one saved GitHub API response and publishes one
strict, ordered eligibility record.

Exit statuses:

* 0: eligible
* 3: valid metadata rejected by policy
* 4: malformed input, arguments, or local output configuration
* 5: authoritative identity changed from explicitly expected values
"""

import argparse
import json
import os
from pathlib import Path
import re
import stat
import tempfile
from typing import Dict, List, Optional, Tuple


EXIT_ELIGIBLE = 0
EXIT_REJECTED = 3
EXIT_INVALID = 4
EXIT_CHANGED = 5

TARGET_REPOSITORY = "openpmix/openpmix"
TRUSTED_AUTHORS = frozenset(("kaamilbadami", "rhc54"))
VERIFIED_AUTHOR_FORKS = {
    "rhc54": frozenset(("rhc54/openpmix",)),
    "kaamilbadami": frozenset(),
}
ELIGIBILITY_VERSION = "1"
OUTPUT_FIELDS = (
    "OPENPMIX_PR_ELIGIBILITY_VERSION",
    "PR_NUMBER",
    "PR_STATE",
    "PR_DRAFT",
    "PR_AUTHOR",
    "PR_BASE_REPOSITORY",
    "PR_HEAD_REPOSITORY",
    "PR_HEAD_SHA",
    "PR_SOURCE_POLICY",
)

POSITIVE_DECIMAL_PATTERN = re.compile(r"[1-9][0-9]*")
SHA_PATTERN = re.compile(r"[0-9a-f]{40}")
LOGIN_PATTERN = re.compile(
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?"
)
REPOSITORY_COMPONENT_PATTERN = re.compile(r"[A-Za-z0-9_.-]{1,100}")
FORBIDDEN_STATE_COMPONENT = ".ci-state"


class EligibilityError(Exception):
    """Supplied metadata or local configuration cannot be trusted."""


class EligibilityArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise EligibilityError(message)


def parse_arguments(arguments: Optional[List[str]] = None) -> argparse.Namespace:
    parser = EligibilityArgumentParser(description=__doc__)
    parser.add_argument("--pr-json", required=True, type=Path)
    parser.add_argument("--pr-number", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--expected-head-sha")
    parser.add_argument("--expected-author")
    parser.add_argument("--expected-head-repository")
    return parser.parse_args(arguments)


def absolute_path(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def validate_path_components(path: Path, label: str,
                             allow_missing_final: bool) -> Path:
    absolute = absolute_path(path)
    if FORBIDDEN_STATE_COMPONENT in absolute.parts:
        raise EligibilityError("{} uses a forbidden local path".format(label))
    current = Path(absolute.anchor)
    for index, component in enumerate(absolute.parts[1:], start=1):
        current /= component
        final = index == len(absolute.parts) - 1
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            if final and allow_missing_final:
                return absolute
            raise EligibilityError("{} path does not exist".format(label))
        except OSError as error:
            raise EligibilityError("{} path is unavailable".format(label)) from error
        if stat.S_ISLNK(metadata.st_mode):
            raise EligibilityError("{} path contains a symbolic link".format(label))
    return absolute


def validate_output_path(path: Path) -> Path:
    output = absolute_path(path)
    if output == Path(output.anchor):
        raise EligibilityError("filesystem root is not a safe output")
    output = validate_path_components(
        output, "output", allow_missing_final=True
    )
    try:
        metadata = output.lstat()
    except FileNotFoundError:
        return output
    except OSError as error:
        raise EligibilityError("output path is unavailable") from error
    if not stat.S_ISREG(metadata.st_mode):
        raise EligibilityError("known output is not a regular file")
    return output


def inspect_input_path(path: Path) -> Tuple[Path, os.stat_result]:
    input_path = validate_path_components(
        path, "PR JSON", allow_missing_final=False
    )
    try:
        metadata = input_path.lstat()
    except OSError as error:
        raise EligibilityError("PR JSON is unavailable") from error
    if not stat.S_ISREG(metadata.st_mode):
        raise EligibilityError("PR JSON is not a regular file")
    return input_path, metadata


def reject_collision(input_path: Path, input_metadata: os.stat_result,
                     output_path: Path) -> None:
    if input_path == output_path:
        raise EligibilityError("PR JSON and output must be different files")
    try:
        output_metadata = output_path.lstat()
    except FileNotFoundError:
        return
    except OSError as error:
        raise EligibilityError("output path is unavailable") from error
    if (input_metadata.st_dev == output_metadata.st_dev and
            input_metadata.st_ino == output_metadata.st_ino):
        raise EligibilityError("PR JSON and output must not be hard links")


def remove_stale_output(output_path: Path) -> None:
    try:
        output_path.unlink()
    except FileNotFoundError:
        pass
    except OSError as error:
        raise EligibilityError("cannot remove stale eligibility output") from error


def read_regular_file(path: Path) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    if nofollow:
        flags |= nofollow
    elif path.is_symlink():
        raise EligibilityError("PR JSON may not be a symbolic link")
    try:
        descriptor = os.open(str(path), flags)
    except OSError as error:
        raise EligibilityError("cannot read PR JSON") from error
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise EligibilityError("PR JSON is not a regular file")
        with os.fdopen(descriptor, "rb") as input_file:
            descriptor = -1
            return input_file.read()
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def reject_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise EligibilityError("JSON contains a duplicate object key")
        result[key] = value
    return result


def reject_nonstandard_number(value: str):
    raise EligibilityError("JSON contains a nonstandard number")


def parse_json_document(content: bytes) -> dict:
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as error:
        raise EligibilityError("PR JSON is not valid UTF-8") from error
    try:
        document = json.loads(
            text,
            object_pairs_hook=reject_duplicate_keys,
            parse_constant=reject_nonstandard_number,
        )
    except (json.JSONDecodeError, RecursionError) as error:
        raise EligibilityError("PR JSON is invalid") from error
    if not isinstance(document, dict):
        raise EligibilityError("PR JSON top level is not an object")
    return document


def require_object(container: dict, field: str) -> dict:
    value = container.get(field)
    if not isinstance(value, dict):
        raise EligibilityError(
            "required object is missing or invalid: {}".format(field)
        )
    return value


def require_string(container: dict, field: str) -> str:
    value = container.get(field)
    if not isinstance(value, str):
        raise EligibilityError(
            "required string is missing or invalid: {}".format(field)
        )
    return value


def require_positive_id(container: dict, field: str) -> int:
    value = container.get(field)
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise EligibilityError(
            "required numeric ID is missing or invalid: {}".format(field)
        )
    return value


def valid_login(value: str) -> bool:
    return LOGIN_PATTERN.fullmatch(value) is not None and "--" not in value


def valid_repository_name(value: str) -> bool:
    if value.count("/") != 1:
        return False
    owner, repository = value.split("/", 1)
    return (
        valid_login(owner)
        and REPOSITORY_COMPONENT_PATTERN.fullmatch(repository) is not None
        and repository not in (".", "..")
    )


def validate_metadata(document: dict, cli_number: int) -> Dict[str, str]:
    require_positive_id(document, "id")
    json_number = require_positive_id(document, "number")
    if json_number != cli_number:
        raise EligibilityError("CLI PR number does not match PR JSON")

    state = require_string(document, "state")
    draft = document.get("draft")
    if not isinstance(draft, bool):
        raise EligibilityError("required boolean is missing or invalid: draft")

    user = require_object(document, "user")
    require_positive_id(user, "id")
    author = require_string(user, "login")
    if not valid_login(author):
        raise EligibilityError("author login is malformed")

    head = require_object(document, "head")
    head_sha = require_string(head, "sha")
    if SHA_PATTERN.fullmatch(head_sha) is None:
        raise EligibilityError(
            "head SHA is not a lowercase 40-character SHA"
        )
    head_repository = require_object(head, "repo")
    require_positive_id(head_repository, "id")
    head_repository_name = require_string(head_repository, "full_name")
    if not valid_repository_name(head_repository_name):
        raise EligibilityError("head repository name is malformed")

    base = require_object(document, "base")
    base_repository = require_object(base, "repo")
    require_positive_id(base_repository, "id")
    base_repository_name = require_string(base_repository, "full_name")
    if not valid_repository_name(base_repository_name):
        raise EligibilityError("base repository name is malformed")

    return {
        "state": state,
        "draft": "1" if draft else "0",
        "author": author,
        "head_sha": head_sha,
        "head_repository": head_repository_name,
        "base_repository": base_repository_name,
    }


def source_policy(metadata: Dict[str, str]) -> Optional[str]:
    if metadata["head_repository"] == TARGET_REPOSITORY:
        return "same-repository"
    if metadata["head_repository"] in VERIFIED_AUTHOR_FORKS.get(
            metadata["author"], frozenset()):
        return "verified-author-fork"
    return None


def output_bytes(pr_number: int, metadata: Dict[str, str],
                 selected_policy: str) -> bytes:
    values = {
        "OPENPMIX_PR_ELIGIBILITY_VERSION": ELIGIBILITY_VERSION,
        "PR_NUMBER": str(pr_number),
        "PR_STATE": metadata["state"],
        "PR_DRAFT": metadata["draft"],
        "PR_AUTHOR": metadata["author"],
        "PR_BASE_REPOSITORY": metadata["base_repository"],
        "PR_HEAD_REPOSITORY": metadata["head_repository"],
        "PR_HEAD_SHA": metadata["head_sha"],
        "PR_SOURCE_POLICY": selected_policy,
    }
    return "".join(
        "{}={}\n".format(field, values[field]) for field in OUTPUT_FIELDS
    ).encode("ascii")


def atomic_write(path: Path, content: bytes) -> None:
    temporary_name = None
    try:
        with tempfile.NamedTemporaryFile(
                mode="wb", dir=str(path.parent),
                prefix=".{}.tmp.".format(path.name), delete=False) as temporary:
            temporary_name = temporary.name
            os.fchmod(temporary.fileno(), 0o600)
            temporary.write(content)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_name, str(path))
        temporary_name = None
    finally:
        if temporary_name is not None:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass


def check(arguments: Optional[List[str]] = None) -> int:
    output_path = None
    try:
        options = parse_arguments(arguments)
        output_path = validate_output_path(options.output)
        input_path, input_metadata = inspect_input_path(options.pr_json)
        reject_collision(input_path, input_metadata, output_path)
        remove_stale_output(output_path)

        if POSITIVE_DECIMAL_PATTERN.fullmatch(options.pr_number) is None:
            raise EligibilityError(
                "PR number is not a canonical positive integer"
            )
        pr_number = int(options.pr_number)
        if (options.expected_head_sha is not None and
                SHA_PATTERN.fullmatch(options.expected_head_sha) is None):
            raise EligibilityError("expected head SHA is not canonical")
        if (options.expected_author is not None and
                not valid_login(options.expected_author)):
            raise EligibilityError("expected author is malformed")
        if (options.expected_head_repository is not None and
                not valid_repository_name(options.expected_head_repository)):
            raise EligibilityError("expected head repository is malformed")

        document = parse_json_document(read_regular_file(input_path))
        metadata = validate_metadata(document, pr_number)

        identity_changed = (
            (options.expected_head_sha is not None and
             options.expected_head_sha != metadata["head_sha"])
            or (options.expected_author is not None and
                options.expected_author != metadata["author"])
            or (options.expected_head_repository is not None and
                options.expected_head_repository !=
                metadata["head_repository"])
        )
        if identity_changed:
            print("error: PR identity changed", file=os.sys.stderr)
            return EXIT_CHANGED

        selected_policy = source_policy(metadata)
        eligible = (
            metadata["state"] == "open"
            and metadata["draft"] == "0"
            and metadata["base_repository"] == TARGET_REPOSITORY
            and metadata["author"] in TRUSTED_AUTHORS
            and selected_policy is not None
        )
        if not eligible:
            print("error: pull request is not eligible", file=os.sys.stderr)
            return EXIT_REJECTED

        atomic_write(
            output_path, output_bytes(pr_number, metadata, selected_policy)
        )
        return EXIT_ELIGIBLE
    except EligibilityError as error:
        print("error: invalid eligibility input: {}".format(error),
              file=os.sys.stderr)
        return EXIT_INVALID
    except OSError as error:
        if output_path is not None:
            try:
                output_path.unlink()
            except OSError:
                pass
        print("error: eligibility output failed: {}".format(error),
              file=os.sys.stderr)
        return EXIT_INVALID


if __name__ == "__main__":
    raise SystemExit(check())
