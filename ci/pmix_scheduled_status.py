#!/usr/bin/env python3
"""Safely clean, publish, and verify scheduled PMIx flow status markers."""

import argparse
import errno
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
from typing import Dict, Optional, Sequence


ID_PATTERN = re.compile(r"[1-9][0-9]*")
STATUS_PATTERN = re.compile(r"0|[1-9][0-9]*")
SHA_PATTERN = re.compile(r"[0-9a-f]{40}")
KINDS = {
    "collector": ("collector-status.env", "PMIX_COLLECTOR_STATUS"),
    "reconciliation": (
        "reconciliation-status.env",
        "PMIX_RECONCILIATION_STATUS",
    ),
}
class StatusError(Exception):
    """A status path or record is malformed, unsafe, stale, or unavailable."""


class StatusArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise StatusError(message)


def safe_path(path: Path) -> Path:
    absolute = Path(os.path.abspath(os.fspath(path)))
    current = Path(absolute.anchor)
    for component in absolute.parts[1:]:
        current /= component
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            continue
        except OSError as error:
            raise StatusError("status path is unavailable") from error
        if stat.S_ISLNK(metadata.st_mode):
            raise StatusError("status path contains a symbolic link")
    return absolute


def status_directory(path: Path) -> Path:
    directory = safe_path(path)
    try:
        metadata = directory.lstat()
    except OSError as error:
        raise StatusError("status directory is unavailable") from error
    if not stat.S_ISDIR(metadata.st_mode):
        raise StatusError("status path is not a directory")
    return directory


def marker_path(directory: Path, kind: str) -> Path:
    return directory / KINDS[kind][0]


def validate_identity(pipeline_id: str, suite_commit: str) -> None:
    if ID_PATTERN.fullmatch(pipeline_id) is None:
        raise StatusError("pipeline ID is not a canonical positive integer")
    if SHA_PATTERN.fullmatch(suite_commit) is None:
        raise StatusError("suite commit is not a lowercase 40-character SHA")


def known_paths(directory: Path, kind: str):
    name = KINDS[kind][0]
    yield directory / name
    yield from directory.glob(f".{name}.tmp.*")


def clean(directory: Path, kinds: Sequence[str]) -> None:
    directory = status_directory(directory)
    candidates = []
    for kind in kinds:
        candidates.extend(known_paths(directory, kind))
    for candidate in candidates:
        try:
            metadata = candidate.lstat()
        except FileNotFoundError:
            continue
        except OSError as error:
            raise StatusError("known status output is unavailable") from error
        if not stat.S_ISREG(metadata.st_mode):
            raise StatusError("known status output is not a regular file")
    for candidate in candidates:
        try:
            candidate.unlink()
        except FileNotFoundError:
            pass
        except OSError as error:
            raise StatusError("cannot remove stale status output") from error


def fsync_directory(directory: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(directory, flags)
    try:
        try:
            os.fsync(descriptor)
        except OSError as error:
            if error.errno not in (errno.EINVAL, errno.ENOTSUP):
                raise
    finally:
        os.close(descriptor)


def encode(kind: str, status: str, pipeline_id: str, suite_commit: str) -> bytes:
    status_field = KINDS[kind][1]
    return (
        f"{status_field}={status}\n"
        f"CI_PIPELINE_ID={pipeline_id}\n"
        f"SUITE_COMMIT={suite_commit}\n"
    ).encode("ascii")


def publish(
    directory: Path,
    kind: str,
    status: str,
    pipeline_id: str,
    suite_commit: str,
) -> None:
    directory = status_directory(directory)
    validate_identity(pipeline_id, suite_commit)
    if STATUS_PATTERN.fullmatch(status) is None:
        raise StatusError("status is not canonical decimal")
    target = marker_path(directory, kind)
    try:
        metadata = target.lstat()
    except FileNotFoundError:
        pass
    except OSError as error:
        raise StatusError("status output is unavailable") from error
    else:
        if not stat.S_ISREG(metadata.st_mode):
            raise StatusError("status output is not a regular file")

    temporary_name: Optional[str] = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=directory,
            prefix=f".{target.name}.tmp.",
            delete=False,
        ) as temporary:
            temporary_name = temporary.name
            descriptor_metadata = os.fstat(temporary.fileno())
            if not stat.S_ISREG(descriptor_metadata.st_mode):
                raise StatusError("status temporary is not a regular file")
            temporary.write(encode(kind, status, pipeline_id, suite_commit))
            temporary.flush()
            os.fsync(temporary.fileno())
            path_metadata = Path(temporary_name).lstat()
            if (
                not stat.S_ISREG(path_metadata.st_mode)
                or path_metadata.st_dev != descriptor_metadata.st_dev
                or path_metadata.st_ino != descriptor_metadata.st_ino
            ):
                raise StatusError("status temporary path changed unexpectedly")
            os.replace(temporary_name, target)
            temporary_name = None
        fsync_directory(directory)
    finally:
        if temporary_name is not None:
            try:
                Path(temporary_name).unlink()
            except FileNotFoundError:
                pass


def read_regular(path: Path) -> bytes:
    path = safe_path(path)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise StatusError("cannot read status marker") from error
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise StatusError("status marker is not a regular file")
        with os.fdopen(descriptor, "rb") as marker:
            descriptor = -1
            return marker.read()
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def decode(kind: str, content: bytes) -> Dict[str, str]:
    try:
        text = content.decode("ascii")
    except UnicodeDecodeError as error:
        raise StatusError("status marker is not ASCII") from error
    if "\r" in text or not text.endswith("\n"):
        raise StatusError("status marker must use newline-terminated Unix lines")
    lines = text.splitlines()
    expected = (KINDS[kind][1], "CI_PIPELINE_ID", "SUITE_COMMIT")
    if len(lines) != len(expected):
        raise StatusError("status marker has the wrong number of fields")
    values = {}
    for name, line in zip(expected, lines):
        if "=" not in line:
            raise StatusError("status field is missing '='")
        actual, value = line.split("=", 1)
        if actual != name or actual in values:
            raise StatusError("status fields are missing, duplicated, or out of order")
        values[actual] = value
    return values


def verify(
    directory: Path,
    kind: str,
    pipeline_id: str,
    suite_commit: str,
) -> str:
    directory = status_directory(directory)
    validate_identity(pipeline_id, suite_commit)
    values = decode(kind, read_regular(marker_path(directory, kind)))
    status = values[KINDS[kind][1]]
    if STATUS_PATTERN.fullmatch(status) is None:
        raise StatusError("status marker contains noncanonical status")
    if values["CI_PIPELINE_ID"] != pipeline_id:
        raise StatusError("status marker belongs to a different pipeline")
    if values["SUITE_COMMIT"] != suite_commit:
        raise StatusError("status marker belongs to a different suite commit")
    return status


def parse_arguments(arguments: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = StatusArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command")
    clean_parser = subparsers.add_parser("clean")
    clean_parser.add_argument("--directory", required=True, type=Path)
    clean_parser.add_argument("--kind", choices=tuple(KINDS))
    for command in ("publish", "verify"):
        child = subparsers.add_parser(command)
        child.add_argument("--directory", required=True, type=Path)
        child.add_argument("--kind", required=True, choices=tuple(KINDS))
        child.add_argument("--pipeline-id", required=True)
        child.add_argument("--suite-commit", required=True)
        if command == "publish":
            child.add_argument("--status", required=True)
    options = parser.parse_args(arguments)
    if options.command is None:
        raise StatusError("a command is required")
    return options


def main(arguments: Optional[Sequence[str]] = None) -> int:
    try:
        options = parse_arguments(arguments)
        if options.command == "clean":
            clean(options.directory, [options.kind] if options.kind else list(KINDS))
        elif options.command == "publish":
            publish(
                options.directory,
                options.kind,
                options.status,
                options.pipeline_id,
                options.suite_commit,
            )
        else:
            print(
                verify(
                    options.directory,
                    options.kind,
                    options.pipeline_id,
                    options.suite_commit,
                )
            )
        return 0
    except (OSError, StatusError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
