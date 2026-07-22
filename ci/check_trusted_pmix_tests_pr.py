#!/usr/bin/env python3
"""Validate supplied GitHub pull-request metadata without external side effects.

Exit statuses are deliberately conservative:

* 0: the pull request is eligible
* 3: the metadata is valid, but the pull request is rejected by policy
* 4: input, metadata, or local output configuration is invalid
* 5: the authoritative head SHA differs from the expected head SHA
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

TARGET_REPOSITORY = "kaamilbadami/pmix-tests"
TRUSTED_AUTHORS = frozenset({"rhc54", "kaamilbadami"})
OUTPUT_FIELDS = (
    "PR_ELIGIBLE",
    "PR_NUMBER",
    "PR_AUTHOR",
    "PR_HEAD_SHA",
    "PR_HEAD_REPOSITORY",
    "PR_BASE_REPOSITORY",
    "PR_FROM_FORK",
)
POSITIVE_DECIMAL_PATTERN = re.compile(r"[1-9][0-9]*")
SHA_PATTERN = re.compile(r"[0-9a-f]{40}")
LOGIN_PATTERN = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?")
REPOSITORY_PATTERN = re.compile(r"[A-Za-z0-9_.-]{1,100}")
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
    return parser.parse_args(arguments)


def absolute_path(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def validate_path_components(path: Path, label: str, allow_missing_final: bool) -> Path:
    absolute = absolute_path(path)
    if FORBIDDEN_STATE_COMPONENT in absolute.parts:
        raise EligibilityError(f"{label} uses a forbidden local path")

    current = Path(absolute.anchor)
    for index, component in enumerate(absolute.parts[1:], start=1):
        current /= component
        is_final = index == len(absolute.parts) - 1
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            if is_final and allow_missing_final:
                return absolute
            raise EligibilityError(f"{label} path does not exist")
        except OSError as error:
            raise EligibilityError(f"{label} path is unavailable") from error
        if stat.S_ISLNK(metadata.st_mode):
            raise EligibilityError(f"{label} path contains a symbolic link")
    return absolute


def validate_output_path(path: Path) -> Path:
    output = absolute_path(path)
    if output == Path(output.anchor):
        raise EligibilityError("filesystem root is not a safe output")
    output = validate_path_components(output, "output", allow_missing_final=True)
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
    input_path = validate_path_components(path, "PR JSON", allow_missing_final=False)
    try:
        metadata = input_path.lstat()
    except OSError as error:
        raise EligibilityError("PR JSON is unavailable") from error
    if not stat.S_ISREG(metadata.st_mode):
        raise EligibilityError("PR JSON is not a regular file")
    return input_path, metadata


def reject_collision(
    input_path: Path,
    input_metadata: os.stat_result,
    output_path: Path,
) -> None:
    if input_path == output_path:
        raise EligibilityError("PR JSON and output must be different files")
    try:
        output_metadata = output_path.lstat()
    except FileNotFoundError:
        return
    except OSError as error:
        raise EligibilityError("output path is unavailable") from error
    if (
        input_metadata.st_dev == output_metadata.st_dev
        and input_metadata.st_ino == output_metadata.st_ino
    ):
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
        descriptor = os.open(path, flags)
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
    raise EligibilityError(f"JSON contains a nonstandard number: {value}")


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
    except json.JSONDecodeError as error:
        raise EligibilityError("PR JSON is invalid") from error
    if not isinstance(document, dict):
        raise EligibilityError("PR JSON top level is not an object")
    return document


def require_object(container: dict, field: str) -> dict:
    value = container.get(field)
    if not isinstance(value, dict):
        raise EligibilityError(f"required object is missing or invalid: {field}")
    return value


def require_string(container: dict, field: str) -> str:
    value = container.get(field)
    if not isinstance(value, str):
        raise EligibilityError(f"required string is missing or invalid: {field}")
    return value


def require_positive_id(container: dict, field: str) -> int:
    value = container.get(field)
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise EligibilityError(f"required numeric ID is missing or invalid: {field}")
    return value


def valid_login(value: str) -> bool:
    return LOGIN_PATTERN.fullmatch(value) is not None and "--" not in value


def valid_repository_name(value: str) -> bool:
    if value.count("/") != 1:
        return False
    owner, repository = value.split("/", 1)
    return (
        valid_login(owner)
        and REPOSITORY_PATTERN.fullmatch(repository) is not None
        and repository not in (".", "..")
    )


def validate_metadata(document: dict, cli_number: int) -> Dict[str, str]:
    require_positive_id(document, "id")
    json_number = require_positive_id(document, "number")
    if json_number != cli_number:
        raise EligibilityError("CLI PR number does not match PR JSON")

    state = require_string(document, "state")
    user = require_object(document, "user")
    require_positive_id(user, "id")
    author = require_string(user, "login")
    if not valid_login(author):
        raise EligibilityError("author login is malformed")

    head = require_object(document, "head")
    head_sha = require_string(head, "sha")
    if SHA_PATTERN.fullmatch(head_sha) is None:
        raise EligibilityError("head SHA is not a lowercase 40-character SHA")
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
        "author": author,
        "head_sha": head_sha,
        "head_repository": head_repository_name,
        "base_repository": base_repository_name,
    }


def output_bytes(pr_number: int, metadata: Dict[str, str]) -> bytes:
    values = {
        "PR_ELIGIBLE": "1",
        "PR_NUMBER": str(pr_number),
        "PR_AUTHOR": metadata["author"],
        "PR_HEAD_SHA": metadata["head_sha"],
        "PR_HEAD_REPOSITORY": metadata["head_repository"],
        "PR_BASE_REPOSITORY": metadata["base_repository"],
        "PR_FROM_FORK": (
            "1" if metadata["head_repository"] != metadata["base_repository"] else "0"
        ),
    }
    return "".join(f"{field}={values[field]}\n" for field in OUTPUT_FIELDS).encode(
        "utf-8"
    )


def atomic_write(path: Path, content: bytes) -> None:
    temporary_name: Optional[str] = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=path.parent,
            prefix=f".{path.name}.tmp.",
            delete=False,
        ) as temporary_file:
            temporary_name = temporary_file.name
            temporary_file.write(content)
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        os.replace(temporary_name, path)
        temporary_name = None
    finally:
        if temporary_name is not None:
            try:
                Path(temporary_name).unlink()
            except FileNotFoundError:
                pass


def check(arguments: Optional[List[str]] = None) -> int:
    try:
        options = parse_arguments(arguments)
    except EligibilityError as error:
        print(f"error: invalid arguments: {error}", file=os.sys.stderr)
        return EXIT_INVALID

    try:
        output_path = validate_output_path(options.output)
        input_path, input_metadata = inspect_input_path(options.pr_json)
        reject_collision(input_path, input_metadata, output_path)
        remove_stale_output(output_path)

        if POSITIVE_DECIMAL_PATTERN.fullmatch(options.pr_number) is None:
            raise EligibilityError("PR number is not a canonical positive integer")
        pr_number = int(options.pr_number)
        if (
            options.expected_head_sha is not None
            and SHA_PATTERN.fullmatch(options.expected_head_sha) is None
        ):
            raise EligibilityError(
                "expected head SHA is not a lowercase 40-character SHA"
            )

        document = parse_json_document(read_regular_file(input_path))
        metadata = validate_metadata(document, pr_number)

        if (
            options.expected_head_sha is not None
            and options.expected_head_sha != metadata["head_sha"]
        ):
            print("error: PR head SHA changed", file=os.sys.stderr)
            return EXIT_CHANGED

        # Rule A: trusted author AND exact target repository AND open PR.
        # Head-repository inequality is allowed and only records fork provenance.
        eligible = (
            metadata["author"] in TRUSTED_AUTHORS
            and metadata["base_repository"] == TARGET_REPOSITORY
            and metadata["state"] == "open"
        )
        if not eligible:
            print("error: pull request is not eligible", file=os.sys.stderr)
            return EXIT_REJECTED

        atomic_write(output_path, output_bytes(pr_number, metadata))
        return EXIT_ELIGIBLE
    except EligibilityError as error:
        print(f"error: invalid eligibility input: {error}", file=os.sys.stderr)
        return EXIT_INVALID
    except OSError as error:
        try:
            output_path.unlink()
        except (NameError, FileNotFoundError):
            pass
        except OSError:
            pass
        print(f"error: eligibility output failed: {error}", file=os.sys.stderr)
        return EXIT_INVALID


if __name__ == "__main__":
    raise SystemExit(check())
