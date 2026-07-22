#!/usr/bin/env python3
"""Reconcile ordered PMIx result records into a proposed state update.

This component is deliberately local and side-effect limited: it reads two state
snapshots, an ordered commit list, and result records, then writes a report and,
when appropriate, a proposed state file in the requested output directory.  It
does not apply the proposal to either input state.

Exit statuses are:

* 0: reconciliation is complete, or the commit list is unchanged (empty)
* 3: reconciliation is blocked by the first non-successful result
* 4: configuration, state, or commit-list input is invalid
* 5: the current state is stale relative to the discovery baseline
"""

import argparse
import errno
import os
from pathlib import Path
import re
import stat
import tempfile
import time
from typing import Dict, List, Optional, Tuple


EXIT_OK = 0
EXIT_BLOCKED = 3
EXIT_INVALID = 4
EXIT_STALE = 5

SHA_PATTERN = re.compile(r"[0-9a-f]{40}")
POSITIVE_DECIMAL_PATTERN = re.compile(r"[1-9][0-9]*")
STATE_FIELDS = ("PMIX_COMMIT", "SUITE_COMMIT", "LAST_SUCCESS_EPOCH")
RESULT_FIELDS = (
    "PMIX_COMMIT",
    "SUITE_COMMIT",
    "CI_JOB_STATUS",
    "CI_JOB_ID",
    "CI_PIPELINE_ID",
)
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
KNOWN_OUTPUTS = ("reconciliation.env", "proposed-pmix-master.env")
ALLOWED_STATUSES = ("success", "failed", "canceled", "unknown")


class InputError(Exception):
    """An input cannot be safely used for reconciliation."""


class ConfigurationError(Exception):
    """Command-line or runtime configuration is invalid."""


class ReconcileArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise ConfigurationError(message)


def valid_sha(value: str) -> bool:
    return SHA_PATTERN.fullmatch(value) is not None


def valid_positive_decimal(value: str) -> bool:
    return POSITIVE_DECIMAL_PATTERN.fullmatch(value) is not None


def read_regular_file(path: Path) -> bytes:
    """Read a regular file without following a final-component symlink."""

    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    if nofollow:
        flags |= nofollow
    elif path.is_symlink():
        raise InputError(f"symbolic link is not allowed: {path}")

    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise InputError(f"cannot read regular file: {path}") from error

    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise InputError(f"not a regular file: {path}")
        with os.fdopen(descriptor, "rb") as input_file:
            descriptor = -1
            return input_file.read()
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def decode_strict_lines(content: bytes, fields: Tuple[str, ...]) -> Dict[str, str]:
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as error:
        raise InputError("input is not valid UTF-8") from error
    if "\r" in text or not text.endswith("\n"):
        raise InputError("record must use newline-terminated Unix lines")

    lines = text.splitlines()
    if len(lines) != len(fields):
        raise InputError("record has the wrong number of fields")

    values: Dict[str, str] = {}
    for expected, line in zip(fields, lines):
        if "=" not in line:
            raise InputError("record field is missing '='")
        name, value = line.split("=", 1)
        if name != expected:
            raise InputError("record fields are missing, duplicated, unknown, or out of order")
        if name in values:
            raise InputError("record contains a duplicate field")
        values[name] = value
    return values


def parse_state(path: Path) -> Tuple[Dict[str, str], bytes]:
    content = read_regular_file(path)
    values = decode_strict_lines(content, STATE_FIELDS)
    if not valid_sha(values["PMIX_COMMIT"]):
        raise InputError("state PMIX_COMMIT is not a lowercase 40-character SHA")
    if not valid_sha(values["SUITE_COMMIT"]):
        raise InputError("state SUITE_COMMIT is not a lowercase 40-character SHA")
    if not valid_positive_decimal(values["LAST_SUCCESS_EPOCH"]):
        raise InputError("state LAST_SUCCESS_EPOCH is not a canonical positive epoch")
    return values, content


def parse_commits(path: Path) -> List[str]:
    content = read_regular_file(path)
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as error:
        raise InputError("commit list is not valid UTF-8") from error
    if "\r" in text:
        raise InputError("commit list must use Unix line endings")
    if not text:
        return []

    lines = text.split("\n")
    if lines[-1] == "":
        lines.pop()
    if not lines or any(not line for line in lines):
        raise InputError("commit list contains a blank line")
    if any(not valid_sha(line) for line in lines):
        raise InputError("commit list contains a malformed SHA")
    if len(set(lines)) != len(lines):
        raise InputError("commit list contains a duplicate SHA")
    return lines


def parse_result(path: Path, commit: str, suite_commit: str) -> str:
    values = decode_strict_lines(read_regular_file(path), RESULT_FIELDS)
    if not valid_sha(values["PMIX_COMMIT"]):
        raise InputError("result PMIX_COMMIT is malformed")
    if not valid_sha(values["SUITE_COMMIT"]):
        raise InputError("result SUITE_COMMIT is malformed")
    if values["PMIX_COMMIT"] != commit:
        raise InputError("result PMIX_COMMIT does not match its commit")
    if values["SUITE_COMMIT"] != suite_commit:
        raise InputError("result SUITE_COMMIT does not match the expected suite")
    if values["CI_JOB_STATUS"] not in ALLOWED_STATUSES:
        raise InputError("result CI_JOB_STATUS is invalid")
    if not valid_positive_decimal(values["CI_JOB_ID"]):
        raise InputError("result CI_JOB_ID is invalid")
    if not valid_positive_decimal(values["CI_PIPELINE_ID"]):
        raise InputError("result CI_PIPELINE_ID is invalid")
    return values["CI_JOB_STATUS"]


def prepare_output_directory(path: Path) -> None:
    if path.is_symlink():
        raise ConfigurationError("output directory may not be a symbolic link")
    if path.exists():
        if not path.is_dir():
            raise ConfigurationError("output path is not a directory")
    else:
        try:
            path.mkdir(parents=True)
        except OSError as error:
            raise ConfigurationError("cannot create output directory") from error

    known_paths = [path / name for name in KNOWN_OUTPUTS]
    if any(known.is_symlink() for known in known_paths):
        raise ConfigurationError("known output may not be a symbolic link")
    try:
        for known in known_paths:
            try:
                known.unlink()
            except FileNotFoundError:
                pass
    except OSError as error:
        raise ConfigurationError("cannot remove stale known output") from error


def validate_result_directory(path: Path) -> None:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return
    except OSError as error:
        raise ConfigurationError("result directory is unavailable") from error
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise ConfigurationError("result path is not a real directory")


def atomic_write(path: Path, content: bytes) -> None:
    """Atomically replace path, removing the same-directory temporary on error."""

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


def initial_report(suite_commit: str = "") -> Dict[str, str]:
    return {
        "RECONCILIATION_RESULT": "invalid",
        "BASELINE_COMMIT": "",
        "CURRENT_COMMIT": "",
        "EXPECTED_SUITE_COMMIT": suite_commit,
        "DISCOVERED_COUNT": "0",
        "SUCCESSFUL_PREFIX_COUNT": "0",
        "PREVIOUS_GOOD_COMMIT": "",
        "PROPOSED_GOOD_COMMIT": "",
        "FIRST_BLOCKED_COMMIT": "",
        "FIRST_BLOCKED_REASON": "",
        "STATE_UPDATE_PROPOSED": "0",
    }


def encode_fields(fields: Tuple[str, ...], values: Dict[str, str]) -> bytes:
    return "".join(f"{field}={values[field]}\n" for field in fields).encode("utf-8")


def write_report(output: Path, report: Dict[str, str]) -> None:
    atomic_write(output / "reconciliation.env", encode_fields(REPORT_FIELDS, report))


def proposed_state(commit: str, suite_commit: str, epoch: str) -> bytes:
    values = {
        "PMIX_COMMIT": commit,
        "SUITE_COMMIT": suite_commit,
        "LAST_SUCCESS_EPOCH": epoch,
    }
    return encode_fields(STATE_FIELDS, values)


def publish_outputs(
    output: Path,
    report: Dict[str, str],
    proposal_content: Optional[bytes],
) -> bool:
    """Publish a proposal before its report and roll it back on failure."""

    if proposal_content is None:
        write_report(output, report)
        return True

    proposal_path = output / "proposed-pmix-master.env"
    try:
        atomic_write(proposal_path, proposal_content)
    except OSError:
        try:
            proposal_path.unlink()
        except FileNotFoundError:
            pass
        report["RECONCILIATION_RESULT"] = "invalid"
        report["STATE_UPDATE_PROPOSED"] = "0"
        write_report(output, report)
        return False

    try:
        write_report(output, report)
    except OSError:
        try:
            proposal_path.unlink()
        except FileNotFoundError:
            pass
        raise
    return True


def parse_arguments(arguments: Optional[List[str]] = None) -> argparse.Namespace:
    parser = ReconcileArgumentParser(description=__doc__)
    parser.add_argument("--baseline-state", required=True, type=Path)
    parser.add_argument("--current-state", required=True, type=Path)
    parser.add_argument("--commits", required=True, type=Path)
    parser.add_argument("--results", required=True, type=Path)
    parser.add_argument("--suite-commit", required=True)
    parser.add_argument("--output", required=True, type=Path)
    return parser.parse_args(arguments)


def configured_epoch() -> str:
    test_epoch = os.environ.get("PMIX_RECONCILE_TEST_EPOCH")
    if test_epoch is not None:
        if not valid_positive_decimal(test_epoch):
            raise ConfigurationError("PMIX_RECONCILE_TEST_EPOCH is invalid")
        return test_epoch
    epoch = str(int(time.time()))
    if not valid_positive_decimal(epoch):
        raise ConfigurationError("current epoch is invalid")
    return epoch


def reconcile(arguments: Optional[List[str]] = None) -> int:
    try:
        options = parse_arguments(arguments)
    except ConfigurationError as error:
        print(f"error: invalid arguments: {error}", file=os.sys.stderr)
        return EXIT_INVALID

    try:
        prepare_output_directory(options.output)
    except ConfigurationError as error:
        print(f"error: cannot prepare output: {error}", file=os.sys.stderr)
        return EXIT_INVALID

    report = initial_report(options.suite_commit)
    try:
        if not valid_sha(options.suite_commit):
            raise ConfigurationError("suite commit is not a lowercase 40-character SHA")
        epoch = configured_epoch()

        baseline, baseline_bytes = parse_state(options.baseline_state)
        report["BASELINE_COMMIT"] = baseline["PMIX_COMMIT"]
        report["PREVIOUS_GOOD_COMMIT"] = baseline["PMIX_COMMIT"]
        report["PROPOSED_GOOD_COMMIT"] = baseline["PMIX_COMMIT"]

        current, current_bytes = parse_state(options.current_state)
        report["CURRENT_COMMIT"] = current["PMIX_COMMIT"]

        commits = parse_commits(options.commits)
        report["DISCOVERED_COUNT"] = str(len(commits))

        if (
            current["PMIX_COMMIT"] != baseline["PMIX_COMMIT"]
            or current_bytes != baseline_bytes
        ):
            report["RECONCILIATION_RESULT"] = "stale"
            write_report(options.output, report)
            return EXIT_STALE

        if baseline["PMIX_COMMIT"] in commits:
            raise InputError("commit list contains the baseline PMIX_COMMIT")

        if not commits:
            report["RECONCILIATION_RESULT"] = "unchanged"
            write_report(options.output, report)
            return EXIT_OK

        validate_result_directory(options.results)
        candidate_good = baseline["PMIX_COMMIT"]
        successful_prefix_count = 0
        blocked_commit = ""
        blocked_reason = ""

        for commit in commits:
            result_path = options.results / f"{commit}.env"
            try:
                status = parse_result(result_path, commit, options.suite_commit)
            except InputError as error:
                cause = error.__cause__
                if isinstance(cause, FileNotFoundError) or (
                    isinstance(cause, OSError) and cause.errno == errno.ENOENT
                ):
                    blocked_reason = "missing"
                else:
                    blocked_reason = "malformed"
                blocked_commit = commit
                break

            if status != "success":
                blocked_commit = commit
                blocked_reason = status
                break
            candidate_good = commit
            successful_prefix_count += 1

        report["SUCCESSFUL_PREFIX_COUNT"] = str(successful_prefix_count)
        report["PROPOSED_GOOD_COMMIT"] = candidate_good
        report["FIRST_BLOCKED_COMMIT"] = blocked_commit
        report["FIRST_BLOCKED_REASON"] = blocked_reason

        if blocked_commit:
            report["RECONCILIATION_RESULT"] = "blocked"
            exit_status = EXIT_BLOCKED
        else:
            report["RECONCILIATION_RESULT"] = "complete"
            exit_status = EXIT_OK

        if candidate_good != baseline["PMIX_COMMIT"]:
            report["STATE_UPDATE_PROPOSED"] = "1"

        proposal_content = None
        if report["STATE_UPDATE_PROPOSED"] == "1":
            proposal_content = proposed_state(candidate_good, options.suite_commit, epoch)
        if not publish_outputs(options.output, report, proposal_content):
            return EXIT_INVALID
        return exit_status
    except (ConfigurationError, InputError) as error:
        try:
            write_report(options.output, report)
        except OSError:
            print("error: invalid reconciliation and report write failed", file=os.sys.stderr)
            return EXIT_INVALID
        print(f"error: invalid reconciliation input: {error}", file=os.sys.stderr)
        return EXIT_INVALID
    except OSError as error:
        print(f"error: reconciliation output failed: {error}", file=os.sys.stderr)
        return EXIT_INVALID


if __name__ == "__main__":
    raise SystemExit(reconcile())
