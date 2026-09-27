#!/usr/bin/env python3
"""Validate or atomically apply a reconciled PMIx known-good state.

This helper is deliberately local: it performs no network access, command
execution, scheduling, build, test, collection, or reconciliation work.
"""

import argparse
from contextlib import contextmanager
import errno
import fcntl
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
from typing import Dict, List, Optional, Sequence, Tuple


SHA_PATTERN = re.compile(r"[0-9a-f]{40}")
POSITIVE_DECIMAL_PATTERN = re.compile(r"[1-9][0-9]*")
NONNEGATIVE_DECIMAL_PATTERN = re.compile(r"0|[1-9][0-9]*")
STATE_FIELDS = ("PMIX_COMMIT", "SUITE_COMMIT", "LAST_SUCCESS_EPOCH")
REPORT_FIELDS = (
    "RECONCILIATION_RESULT",
    "BASELINE_COMMIT",
    "CURRENT_COMMIT",
    "EXPECTED_SUITE_COMMIT",
    "DISCOVERED_COUNT",
    "SUCCESSFUL_PREFIX_COUNT",
    "PREVIOUS_GOOD_COMMIT",
    "PROPOSED_GOOD_COMMIT",
    "FIRST_BLOCKED_COMMIT",
    "FIRST_BLOCKED_REASON",
    "STATE_UPDATE_PROPOSED",
)
BLOCKING_REASONS = frozenset({"failed", "canceled", "unknown", "missing", "malformed"})


class ValidationError(Exception):
    """An input is unsafe, malformed, stale, or internally inconsistent."""


class ArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise ValidationError(message)


def ensure_no_symlink_components(path: Path) -> Path:
    absolute = Path(os.path.abspath(os.fspath(path)))
    current = Path(absolute.anchor)
    for component in absolute.parts[1:]:
        current /= component
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            continue
        except OSError as error:
            raise ValidationError(f"path is unavailable: {path}") from error
        if stat.S_ISLNK(metadata.st_mode):
            raise ValidationError(f"symbolic-link path component is not allowed: {path}")
    return absolute


def read_regular_file(path: Path) -> bytes:
    path = ensure_no_symlink_components(path)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise ValidationError(f"cannot read regular file: {path}") from error
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ValidationError(f"not a regular file: {path}")
        with os.fdopen(descriptor, "rb") as input_file:
            descriptor = -1
            return input_file.read()
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def decode_fields(content: bytes, fields: Sequence[str]) -> Dict[str, str]:
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValidationError("record is not valid UTF-8") from error
    if "\r" in text or not text.endswith("\n"):
        raise ValidationError("record must use newline-terminated Unix lines")
    lines = text.splitlines()
    if len(lines) != len(fields):
        raise ValidationError("record has the wrong number of fields")
    values: Dict[str, str] = {}
    for expected, line in zip(fields, lines):
        if "=" not in line:
            raise ValidationError("record field is missing '='")
        name, value = line.split("=", 1)
        if name != expected or name in values:
            raise ValidationError("record fields are missing, duplicated, unknown, or out of order")
        values[name] = value
    return values


def valid_sha(value: str) -> bool:
    return SHA_PATTERN.fullmatch(value) is not None


def parse_state(path: Path) -> Tuple[Dict[str, str], bytes]:
    content = read_regular_file(path)
    values = decode_fields(content, STATE_FIELDS)
    if not valid_sha(values["PMIX_COMMIT"]):
        raise ValidationError("state PMIX_COMMIT is not a lowercase 40-character SHA")
    if not valid_sha(values["SUITE_COMMIT"]):
        raise ValidationError("state SUITE_COMMIT is not a lowercase 40-character SHA")
    if POSITIVE_DECIMAL_PATTERN.fullmatch(values["LAST_SUCCESS_EPOCH"]) is None:
        raise ValidationError("state LAST_SUCCESS_EPOCH is not a canonical positive epoch")
    return values, content


def fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise ValidationError(f"cannot open state directory: {path}") from error
    try:
        try:
            os.fsync(descriptor)
        except OSError as error:
            if error.errno not in (errno.EINVAL, errno.ENOTSUP):
                raise
    finally:
        os.close(descriptor)


def snapshot_state(source: Path, destination: Path) -> None:
    """Validate source and preserve its exact bytes in a new local snapshot."""

    _, content = parse_state(source)
    target = ensure_no_symlink_components(destination)
    parent = target.parent
    try:
        metadata = parent.lstat()
    except OSError as error:
        raise ValidationError("snapshot directory is unavailable") from error
    if not stat.S_ISDIR(metadata.st_mode):
        raise ValidationError("snapshot parent is not a directory")

    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = -1
    created = False
    completed = False
    try:
        descriptor = os.open(target, flags, 0o600)
        created = True
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ValidationError("snapshot is not a regular file")
        with os.fdopen(descriptor, "wb") as output_file:
            descriptor = -1
            output_file.write(content)
            output_file.flush()
            os.fsync(output_file.fileno())
        fsync_directory(parent)
        completed = True
    except FileExistsError as error:
        raise ValidationError("snapshot output already exists") from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if created and not completed:
            try:
                target.unlink()
            except FileNotFoundError:
                pass


@contextmanager
def locked_state(path: Path):
    """Serialize cooperating state appliers with a safe same-directory lock."""

    target = ensure_no_symlink_components(path)
    lock_path = target.parent / f".{target.name}.lock"
    ensure_no_symlink_components(lock_path)
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(lock_path, flags, 0o600)
    except OSError as error:
        raise ValidationError("cannot open authoritative-state lock") from error
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ValidationError("authoritative-state lock is not a regular file")
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        os.close(descriptor)


def parse_commits(path: Path) -> List[str]:
    content = read_regular_file(path)
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValidationError("commit list is not valid UTF-8") from error
    if "\r" in text:
        raise ValidationError("commit list must use Unix line endings")
    if not text:
        return []
    lines = text.split("\n")
    if lines[-1] == "":
        lines.pop()
    if not lines or any(not line for line in lines):
        raise ValidationError("commit list contains a blank line")
    if any(not valid_sha(line) for line in lines):
        raise ValidationError("commit list contains a malformed SHA")
    if len(lines) != len(set(lines)):
        raise ValidationError("commit list contains a duplicate SHA")
    return lines


def parse_report(path: Path) -> Dict[str, str]:
    values = decode_fields(read_regular_file(path), REPORT_FIELDS)
    for field in (
        "BASELINE_COMMIT",
        "CURRENT_COMMIT",
        "EXPECTED_SUITE_COMMIT",
        "PREVIOUS_GOOD_COMMIT",
        "PROPOSED_GOOD_COMMIT",
    ):
        if not valid_sha(values[field]):
            raise ValidationError(f"report {field} is not a lowercase 40-character SHA")
    for field in ("DISCOVERED_COUNT", "SUCCESSFUL_PREFIX_COUNT"):
        if NONNEGATIVE_DECIMAL_PATTERN.fullmatch(values[field]) is None:
            raise ValidationError(f"report {field} is not canonical decimal")
    if values["STATE_UPDATE_PROPOSED"] not in ("0", "1"):
        raise ValidationError("report STATE_UPDATE_PROPOSED must be 0 or 1")
    return values


def proposal_exists(path: Path) -> bool:
    absolute = ensure_no_symlink_components(path)
    try:
        metadata = absolute.lstat()
    except FileNotFoundError:
        return False
    except OSError as error:
        raise ValidationError("proposal path is unavailable") from error
    if not stat.S_ISREG(metadata.st_mode):
        raise ValidationError("proposal is not a regular file")
    return True


def validate_reconciliation(
    baseline: Dict[str, str],
    commits: List[str],
    report: Dict[str, str],
    suite_commit: str,
) -> Tuple[bool, str]:
    if not valid_sha(suite_commit):
        raise ValidationError("suite commit is not a lowercase 40-character SHA")
    base_commit = baseline["PMIX_COMMIT"]
    if base_commit in commits:
        raise ValidationError("ordered commit list contains the baseline commit")
    if report["BASELINE_COMMIT"] != base_commit:
        raise ValidationError("report baseline does not match discovery state")
    if report["CURRENT_COMMIT"] != base_commit:
        raise ValidationError("report current commit does not match discovery state")
    if report["PREVIOUS_GOOD_COMMIT"] != base_commit:
        raise ValidationError("report previous-good commit does not match discovery state")
    if report["EXPECTED_SUITE_COMMIT"] != suite_commit:
        raise ValidationError("report suite commit does not match this pipeline")

    discovered_count = int(report["DISCOVERED_COUNT"])
    prefix_count = int(report["SUCCESSFUL_PREFIX_COUNT"])
    if discovered_count != len(commits):
        raise ValidationError("report discovered count does not match ordered commits")
    if prefix_count > discovered_count:
        raise ValidationError("reported successful prefix exceeds discovered commits")

    boundary = base_commit if prefix_count == 0 else commits[prefix_count - 1]
    if report["PROPOSED_GOOD_COMMIT"] != boundary:
        raise ValidationError("proposed commit is not the successful-prefix boundary")
    update_expected = prefix_count > 0
    if (report["STATE_UPDATE_PROPOSED"] == "1") != update_expected:
        raise ValidationError("state-update flag does not match successful prefix")

    result = report["RECONCILIATION_RESULT"]
    blocked_commit = report["FIRST_BLOCKED_COMMIT"]
    blocked_reason = report["FIRST_BLOCKED_REASON"]
    if result == "unchanged":
        if discovered_count != 0 or prefix_count != 0:
            raise ValidationError("unchanged reconciliation is not empty")
        if blocked_commit or blocked_reason or update_expected:
            raise ValidationError("unchanged reconciliation contains update or blocker data")
    elif result == "complete":
        if discovered_count == 0 or prefix_count != discovered_count:
            raise ValidationError("complete reconciliation does not cover every commit")
        if blocked_commit or blocked_reason or not update_expected:
            raise ValidationError("complete reconciliation contains invalid blocker or update data")
    elif result == "blocked":
        if discovered_count == 0 or prefix_count >= discovered_count:
            raise ValidationError("blocked reconciliation has no blocking boundary")
        if blocked_commit != commits[prefix_count]:
            raise ValidationError("blocked commit does not follow the successful prefix")
        if blocked_reason not in BLOCKING_REASONS:
            raise ValidationError("blocked reconciliation has an invalid reason")
    else:
        raise ValidationError("reconciliation result is not applicable")
    return update_expected, boundary


def atomic_replace(path: Path, content: bytes) -> None:
    target = ensure_no_symlink_components(path)
    parent = target.parent
    try:
        metadata = parent.lstat()
    except OSError as error:
        raise ValidationError("state directory is unavailable") from error
    if not stat.S_ISDIR(metadata.st_mode):
        raise ValidationError("state parent is not a directory")
    try:
        target_metadata = target.lstat()
    except OSError as error:
        raise ValidationError("authoritative state is unavailable") from error
    if not stat.S_ISREG(target_metadata.st_mode):
        raise ValidationError("authoritative state is not a regular file")
    replacement_mode = stat.S_IMODE(target_metadata.st_mode)

    temporary_name: Optional[str] = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=parent,
            prefix=f".{target.name}.tmp.",
            delete=False,
        ) as temporary_file:
            temporary_name = temporary_file.name
            descriptor_metadata = os.fstat(temporary_file.fileno())
            if not stat.S_ISREG(descriptor_metadata.st_mode):
                raise ValidationError("replacement temporary is not a regular file")
            os.fchmod(temporary_file.fileno(), replacement_mode)
            temporary_file.write(content)
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
            path_metadata = Path(temporary_name).lstat()
            if (
                not stat.S_ISREG(path_metadata.st_mode)
                or path_metadata.st_dev != descriptor_metadata.st_dev
                or path_metadata.st_ino != descriptor_metadata.st_ino
            ):
                raise ValidationError("replacement temporary path changed unexpectedly")
            os.replace(temporary_name, target)
            temporary_name = None
        fsync_directory(parent)
    finally:
        if temporary_name is not None:
            try:
                Path(temporary_name).unlink()
            except FileNotFoundError:
                pass


def apply_state(options: argparse.Namespace) -> None:
    baseline, baseline_bytes = parse_state(options.baseline_state)
    commits = parse_commits(options.commits)
    report = parse_report(options.report)
    update_required, boundary = validate_reconciliation(
        baseline, commits, report, options.suite_commit
    )
    has_proposal = proposal_exists(options.proposal)
    proposal_bytes = b""
    if not update_required:
        if has_proposal:
            raise ValidationError("unexpected proposal when no state update is allowed")
    else:
        if not has_proposal:
            raise ValidationError("required proposed state is missing")
        proposal, proposal_bytes = parse_state(options.proposal)
        if proposal["PMIX_COMMIT"] != boundary:
            raise ValidationError("proposal PMIX_COMMIT does not match the prefix boundary")
        if proposal["SUITE_COMMIT"] != options.suite_commit:
            raise ValidationError("proposal SUITE_COMMIT does not match this pipeline")

    with locked_state(options.authoritative_state):
        current, current_bytes = parse_state(options.authoritative_state)
        if current != baseline or current_bytes != baseline_bytes:
            raise ValidationError("authoritative state is stale relative to discovery baseline")
        if not os.access(
            ensure_no_symlink_components(options.authoritative_state).parent,
            os.W_OK | os.X_OK,
            effective_ids=True,
        ):
            raise ValidationError("authoritative state directory is not writable")
        if not update_required:
            print(f"PMIx known-good state unchanged at {baseline['PMIX_COMMIT']}")
            return

        # Re-read while holding the inter-pipeline lock, immediately before rename.
        latest, latest_bytes = parse_state(options.authoritative_state)
        if latest != baseline or latest_bytes != baseline_bytes:
            raise ValidationError("authoritative state changed before application")
        atomic_replace(options.authoritative_state, proposal_bytes)
        print(f"Applied reconciled PMIx known-good state: {boundary}")


def parse_arguments(arguments: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command")
    validate = subparsers.add_parser("validate-state")
    validate.add_argument("state", type=Path)
    snapshot = subparsers.add_parser("snapshot")
    snapshot.add_argument("--state", required=True, type=Path)
    snapshot.add_argument("--output", required=True, type=Path)
    apply_parser = subparsers.add_parser("apply")
    apply_parser.add_argument("--baseline-state", required=True, type=Path)
    apply_parser.add_argument("--authoritative-state", required=True, type=Path)
    apply_parser.add_argument("--commits", required=True, type=Path)
    apply_parser.add_argument("--report", required=True, type=Path)
    apply_parser.add_argument("--proposal", required=True, type=Path)
    apply_parser.add_argument("--suite-commit", required=True)
    options = parser.parse_args(arguments)
    if options.command is None:
        raise ValidationError("a command is required")
    return options


def main(arguments: Optional[Sequence[str]] = None) -> int:
    try:
        options = parse_arguments(arguments)
        if options.command == "validate-state":
            parse_state(options.state)
        elif options.command == "snapshot":
            snapshot_state(options.state, options.output)
        else:
            apply_state(options)
        return 0
    except ValidationError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    except OSError as error:
        print(f"error: state application failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
