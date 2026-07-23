#!/usr/bin/env python3
"""Validate the fixed records used by the trusted-author pmix-tests PR pilot.

All records are deliberately line-oriented, ordered, and closed-schema.  They
are data, never shell input.  This helper also classifies the ReFrame 4.10 JSON
report; a ReFrame process status by itself is never treated as a test result.

Only same-repository PRs from the fixed author allowlist reach execution.  The
MVP trusts those authors' code under the Frontier service account; these data
checks are not a sandbox for arbitrary or fork-originated PR code.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile


TARGET_REPOSITORY = "kaamilbadami/pmix-tests"
TRUSTED_AUTHORS = frozenset(("rhc54", "kaamilbadami"))
EXPECTED_CHECK = "PMIxTestsPRPythonSmokeTest"
PREPARATION_VERSION = "2"
RESULT_VERSION = "2"
RUN_EVIDENCE_VERSION = "2"
MAX_RECORD_SIZE = 4096
MAX_REPORT_SIZE = 8 * 1024 * 1024

SHA_RE = re.compile(r"[0-9a-f]{40}\Z")
DIGEST_RE = re.compile(r"[0-9a-f]{64}\Z")
EXECUTION_ID_RE = re.compile(r"[0-9a-f]{32}\Z")
PR_NUMBER_RE = re.compile(r"[1-9][0-9]*\Z")
PIPELINE_ID_RE = re.compile(r"[1-9][0-9]*\Z")
AUTHOR_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?\Z")
JOB_ID_RE = re.compile(r"[0-9]+(?:_[0-9]+)?\Z")

TRUSTED_RECORD_FIELDS = (
    "PR_ELIGIBLE",
    "PR_NUMBER",
    "PR_AUTHOR",
    "PR_HEAD_SHA",
    "PR_HEAD_REPOSITORY",
    "PR_BASE_REPOSITORY",
    "PR_FROM_FORK",
)
PREPARATION_FIELDS = (
    "PMIX_TESTS_PR_PREPARATION_VERSION",
    "CI_PIPELINE_ID",
    "PR_REPOSITORY",
    "PR_NUMBER",
    "PR_AUTHOR",
    "PR_HEAD_SHA",
    "PR_FROM_FORK",
    "PREPARATION_RESULT",
)
RESULT_FIELDS = (
    "PMIX_TESTS_PR_EXECUTION_RESULT_VERSION",
    "CI_PIPELINE_ID",
    "PR_HEAD_SHA",
    "RESULT",
    "EXPECTED_CHECK",
    "CHECK_RAN",
    "REPORT_SHA256",
    "EXECUTION_ID",
    "REFRAME_EXIT_STATUS",
)
START_FIELDS = (
    "PMIX_TESTS_PR_RUN_EVIDENCE_VERSION",
    "PR_HEAD_SHA",
    "EXECUTION_ID",
    "PYTHON_PREFLIGHT_EXIT_CODE",
)
COMPLETE_FIELDS = START_FIELDS + ("SERVER_EXIT_CODE",)
CLIENT_START_FIELDS = (
    "PMIX_TESTS_PR_RUN_EVIDENCE_VERSION",
    "PR_HEAD_SHA",
    "EXECUTION_ID",
)
CLIENT_COMPLETE_FIELDS = CLIENT_START_FIELDS + ("CLIENT_EXIT_CODE",)


class RecordError(Exception):
    """An input cannot be used across a trust boundary."""


def safe_relative_path(value, label):
    path = Path(value)
    if path.is_absolute() or not path.parts:
        raise RecordError("{} must be a non-empty relative path".format(label))
    if any(component in ("", ".", "..") for component in path.parts):
        raise RecordError("{} contains an unsafe component".format(label))
    if ".ci-state" in path.parts:
        raise RecordError("{} uses a forbidden state path".format(label))
    return path


def inspect_components(path, label, allow_missing_final=False):
    """Use lstat for every repository-relative component."""
    path = safe_relative_path(path, label)
    current = Path(".")
    for index, component in enumerate(path.parts):
        current /= component
        final = index == len(path.parts) - 1
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            if final and allow_missing_final:
                return path, None
            raise RecordError("{} does not exist".format(label))
        except OSError as error:
            raise RecordError("{} is unavailable".format(label)) from error
        if stat.S_ISLNK(metadata.st_mode):
            raise RecordError("{} contains a symbolic link".format(label))
        if not final and not stat.S_ISDIR(metadata.st_mode):
            raise RecordError("{} has a non-directory parent".format(label))
    return path, metadata


def read_regular_bytes(path, label, maximum):
    path, metadata = inspect_components(path, label)
    if metadata is None or not stat.S_ISREG(metadata.st_mode):
        raise RecordError("{} is not a regular file".format(label))
    if metadata.st_nlink != 1:
        raise RecordError("{} has an unsafe link count".format(label))
    if metadata.st_size > maximum:
        raise RecordError("{} is too large".format(label))
    try:
        data = path.read_bytes()
    except OSError as error:
        raise RecordError("{} could not be read".format(label)) from error
    if len(data) != metadata.st_size:
        raise RecordError("{} changed while it was read".format(label))
    return data


def parse_ordered_record(path, fields, label):
    data = read_regular_bytes(path, label, MAX_RECORD_SIZE)
    try:
        text = data.decode("ascii")
    except UnicodeDecodeError as error:
        raise RecordError("{} is not ASCII".format(label)) from error
    if not text.endswith("\n") or "\r" in text or "\x00" in text:
        raise RecordError("{} has invalid line encoding".format(label))
    lines = text[:-1].split("\n")
    if len(lines) != len(fields):
        raise RecordError("{} has an invalid field count".format(label))
    values = {}
    for expected, line in zip(fields, lines):
        key, separator, value = line.partition("=")
        if separator != "=" or key != expected or not value or "=" in key:
            raise RecordError("{} has an invalid schema".format(label))
        if key in values:
            raise RecordError("{} contains a duplicate key".format(label))
        values[key] = value
    return values


def validate_trusted_record(path, requested_number, expected_sha=None,
                            expected_author=None):
    values = parse_ordered_record(path, TRUSTED_RECORD_FIELDS,
                                  "trusted eligibility record")
    if values["PR_ELIGIBLE"] != "1":
        raise RecordError("PR is not eligible")
    if PR_NUMBER_RE.fullmatch(values["PR_NUMBER"]) is None:
        raise RecordError("PR number is invalid")
    if values["PR_NUMBER"] != requested_number:
        raise RecordError("PR number does not match the request")
    if AUTHOR_RE.fullmatch(values["PR_AUTHOR"]) is None:
        raise RecordError("PR author is malformed")
    if values["PR_AUTHOR"] not in TRUSTED_AUTHORS:
        raise RecordError("PR author is not approved")
    if expected_author is not None and values["PR_AUTHOR"] != expected_author:
        raise RecordError("PR author changed during revalidation")
    if values["PR_BASE_REPOSITORY"] != TARGET_REPOSITORY:
        raise RecordError("PR base repository is invalid")
    if values["PR_HEAD_REPOSITORY"] != TARGET_REPOSITORY:
        raise RecordError("fork-originated PRs are not supported")
    if values["PR_FROM_FORK"] != "0":
        raise RecordError("fork-originated PRs are not supported")
    if SHA_RE.fullmatch(values["PR_HEAD_SHA"]) is None:
        raise RecordError("PR head SHA is not canonical lowercase hexadecimal")
    if expected_sha is not None and values["PR_HEAD_SHA"] != expected_sha:
        raise RecordError("PR head SHA changed during revalidation")
    return values


def validate_preparation_values(values, require_ready=False,
                                expected_pipeline_id=None):
    if values["PMIX_TESTS_PR_PREPARATION_VERSION"] != PREPARATION_VERSION:
        raise RecordError("preparation version is unsupported")
    if values["PR_REPOSITORY"] != TARGET_REPOSITORY:
        raise RecordError("preparation repository is invalid")
    if PIPELINE_ID_RE.fullmatch(values["CI_PIPELINE_ID"]) is None:
        raise RecordError("preparation pipeline identifier is invalid")
    if (expected_pipeline_id is not None and
            values["CI_PIPELINE_ID"] != expected_pipeline_id):
        raise RecordError("preparation belongs to another pipeline")
    if PR_NUMBER_RE.fullmatch(values["PR_NUMBER"]) is None:
        raise RecordError("preparation PR number is invalid")
    if values["PR_AUTHOR"] not in TRUSTED_AUTHORS:
        raise RecordError("preparation author is not approved")
    if SHA_RE.fullmatch(values["PR_HEAD_SHA"]) is None:
        raise RecordError("preparation SHA is invalid")
    if values["PR_FROM_FORK"] != "0":
        raise RecordError("preparation describes a fork")
    if values["PREPARATION_RESULT"] not in ("ready", "error"):
        raise RecordError("preparation result is invalid")
    if require_ready and values["PREPARATION_RESULT"] != "ready":
        raise RecordError("preparation did not complete successfully")
    return values


def read_preparation(path, require_ready=False, expected_pipeline_id=None):
    values = parse_ordered_record(path, PREPARATION_FIELDS,
                                  "preparation artifact")
    return validate_preparation_values(
        values, require_ready, expected_pipeline_id)


def validate_result_values(values):
    if values["PMIX_TESTS_PR_EXECUTION_RESULT_VERSION"] != RESULT_VERSION:
        raise RecordError("execution result version is unsupported")
    if SHA_RE.fullmatch(values["PR_HEAD_SHA"]) is None:
        raise RecordError("execution result SHA is invalid")
    if PIPELINE_ID_RE.fullmatch(values["CI_PIPELINE_ID"]) is None:
        raise RecordError("execution result pipeline identifier is invalid")
    if values["RESULT"] not in ("success", "failure", "error"):
        raise RecordError("execution result is invalid")
    if values["EXPECTED_CHECK"] != EXPECTED_CHECK:
        raise RecordError("execution result check name is invalid")
    if values["CHECK_RAN"] not in ("0", "1"):
        raise RecordError("execution run evidence is invalid")
    if (values["REPORT_SHA256"] != "missing" and
            DIGEST_RE.fullmatch(values["REPORT_SHA256"]) is None):
        raise RecordError("execution report digest is invalid")
    if EXECUTION_ID_RE.fullmatch(values["EXECUTION_ID"]) is None:
        raise RecordError("execution identifier is invalid")
    status = values["REFRAME_EXIT_STATUS"]
    status_number = None
    if status != "unavailable":
        try:
            status_number = int(status)
        except ValueError as error:
            raise RecordError("ReFrame status is invalid") from error
        if str(status_number) != status or not 0 <= status_number <= 255:
            raise RecordError("ReFrame status is invalid")
    if values["RESULT"] == "success":
        if (values["CHECK_RAN"] != "1" or
                values["REPORT_SHA256"] == "missing" or status_number != 0):
            raise RecordError("successful execution result is inconsistent")
    elif values["RESULT"] == "failure":
        if (values["CHECK_RAN"] != "1" or
                values["REPORT_SHA256"] == "missing" or status_number != 1):
            raise RecordError("failed execution result is inconsistent")
    return values


def read_result(path):
    values = parse_ordered_record(path, RESULT_FIELDS,
                                  "execution result artifact")
    return validate_result_values(values)


def validate_output_path(path, label):
    path, metadata = inspect_components(path, label, allow_missing_final=True)
    try:
        parent_metadata = path.parent.lstat()
    except OSError as error:
        raise RecordError("{} parent is unavailable".format(label)) from error
    if stat.S_ISLNK(parent_metadata.st_mode) or not stat.S_ISDIR(parent_metadata.st_mode):
        raise RecordError("{} parent is unsafe".format(label))
    if metadata is not None:
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise RecordError("{} is not a safe regular file".format(label))
    return path


def atomic_record(path, fields, values, label):
    path = validate_output_path(path, label)
    content = "".join("{}={}\n".format(field, values[field])
                      for field in fields).encode("ascii")
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


def preparation_values(trusted, preparation_result, pipeline_id):
    return {
        "PMIX_TESTS_PR_PREPARATION_VERSION": PREPARATION_VERSION,
        "CI_PIPELINE_ID": pipeline_id,
        "PR_REPOSITORY": TARGET_REPOSITORY,
        "PR_NUMBER": trusted["PR_NUMBER"],
        "PR_AUTHOR": trusted["PR_AUTHOR"],
        "PR_HEAD_SHA": trusted["PR_HEAD_SHA"],
        "PR_FROM_FORK": "0",
        "PREPARATION_RESULT": preparation_result,
    }


def result_values(sha, result, check_ran, report_digest, execution_id,
                  reframe_status, pipeline_id):
    values = {
        "PMIX_TESTS_PR_EXECUTION_RESULT_VERSION": RESULT_VERSION,
        "CI_PIPELINE_ID": pipeline_id,
        "PR_HEAD_SHA": sha,
        "RESULT": result,
        "EXPECTED_CHECK": EXPECTED_CHECK,
        "CHECK_RAN": "1" if check_ran else "0",
        "REPORT_SHA256": report_digest,
        "EXECUTION_ID": execution_id,
        "REFRAME_EXIT_STATUS": reframe_status,
    }
    return validate_result_values(values)


def reject_duplicate_json_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise RecordError("ReFrame report contains a duplicate JSON key")
        result[key] = value
    return result


def read_json_report(path):
    data = read_regular_bytes(path, "ReFrame report", MAX_REPORT_SIZE)
    digest = hashlib.sha256(data).hexdigest()
    try:
        document = json.loads(
            data.decode("utf-8"), object_pairs_hook=reject_duplicate_json_keys,
            parse_constant=lambda value: (_ for _ in ()).throw(
                RecordError("ReFrame report contains a nonstandard number")))
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as error:
        raise RecordError("ReFrame report is malformed") from error
    if not isinstance(document, dict):
        raise RecordError("ReFrame report top level is invalid")
    return document, digest


def read_evidence(path, fields, sha, execution_id, label):
    values = parse_ordered_record(path, fields, label)
    if values["PMIX_TESTS_PR_RUN_EVIDENCE_VERSION"] != RUN_EVIDENCE_VERSION:
        raise RecordError("{} version is invalid".format(label))
    if values["PR_HEAD_SHA"] != sha:
        raise RecordError("{} SHA is invalid".format(label))
    if values["EXECUTION_ID"] != execution_id:
        raise RecordError("{} execution identifier is invalid".format(label))
    if values["PYTHON_PREFLIGHT_EXIT_CODE"] != "0":
        raise RecordError("{} Python preflight did not pass".format(label))
    if "SERVER_EXIT_CODE" in values:
        try:
            exit_code = int(values["SERVER_EXIT_CODE"])
        except ValueError as error:
            raise RecordError("server exit code is invalid") from error
        if str(exit_code) != values["SERVER_EXIT_CODE"] or not 0 <= exit_code <= 255:
            raise RecordError("server exit code is invalid")
    return values


def read_client_evidence(path, fields, sha, execution_id, label):
    values = parse_ordered_record(path, fields, label)
    if values["PMIX_TESTS_PR_RUN_EVIDENCE_VERSION"] != RUN_EVIDENCE_VERSION:
        raise RecordError("{} version is invalid".format(label))
    if values["PR_HEAD_SHA"] != sha:
        raise RecordError("{} SHA is invalid".format(label))
    if values["EXECUTION_ID"] != execution_id:
        raise RecordError("{} execution identifier is invalid".format(label))
    if "CLIENT_EXIT_CODE" in values:
        try:
            exit_code = int(values["CLIENT_EXIT_CODE"])
        except ValueError as error:
            raise RecordError("client exit code is invalid") from error
        if str(exit_code) != values["CLIENT_EXIT_CODE"] or not 0 <= exit_code <= 255:
            raise RecordError("client exit code is invalid")
    return values


def evidence_state(directory, sha, execution_id):
    directory, metadata = inspect_components(directory, "run evidence directory")
    if metadata is None or not stat.S_ISDIR(metadata.st_mode):
        raise RecordError("run evidence directory is not a real directory")
    started_path = directory / "pmix-tests-pr-run-started.env"
    completed_path = directory / "pmix-tests-pr-run-completed.env"
    client_started_path = directory / "pmix-tests-pr-client-started.env"
    client_completed_path = directory / "pmix-tests-pr-client-completed.env"
    client_duplicate_path = directory / "pmix-tests-pr-client-duplicate"
    try:
        client_duplicate_path.lstat()
    except FileNotFoundError:
        pass
    else:
        raise RecordError("client wrapper ran more than once")
    try:
        started_path.lstat()
    except FileNotFoundError:
        return False, None, None
    started = read_evidence(started_path, START_FIELDS, sha, execution_id,
                            "run-start evidence")
    del started
    try:
        completed_path.lstat()
    except FileNotFoundError:
        return True, None, None
    completed = read_evidence(completed_path, COMPLETE_FIELDS, sha,
                              execution_id, "run-complete evidence")
    try:
        client_started_path.lstat()
    except FileNotFoundError:
        return True, int(completed["SERVER_EXIT_CODE"]), None
    read_client_evidence(client_started_path, CLIENT_START_FIELDS, sha,
                         execution_id, "client-start evidence")
    try:
        client_completed_path.lstat()
    except FileNotFoundError:
        return True, int(completed["SERVER_EXIT_CODE"]), None
    client_completed = read_client_evidence(
        client_completed_path, CLIENT_COMPLETE_FIELDS, sha, execution_id,
        "client-complete evidence")
    return (True, int(completed["SERVER_EXIT_CODE"]),
            int(client_completed["CLIENT_EXIT_CODE"]))


def numeric(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def classify_report(document, evidence_directory, reframe_status,
                    check_ran, server_exit, client_exit):
    if set(document) != set(("session_info", "runs", "restored_cases")):
        raise RecordError("ReFrame report schema is unexpected")
    session = document["session_info"]
    runs = document["runs"]
    if not isinstance(session, dict) or not isinstance(runs, list):
        raise RecordError("ReFrame report structure is invalid")
    if document["restored_cases"] != [] or len(runs) != 1:
        raise RecordError("ReFrame report contains unexpected runs")
    if session.get("version") != "4.10.0" or session.get("data_version") != "4.2":
        raise RecordError("ReFrame report version is unsupported")
    run = runs[0]
    if not isinstance(run, dict) or not isinstance(run.get("testcases"), list):
        raise RecordError("ReFrame run structure is invalid")
    integer_counts = (run.get("num_cases"), run.get("num_failures"),
                      run.get("num_aborted"), run.get("num_skipped"))
    if (any(not isinstance(value, int) or isinstance(value, bool) or value < 0
            for value in integer_counts) or
            run["num_cases"] != len(run["testcases"]) or
            sum(integer_counts[1:]) > run["num_cases"] or
            run.get("run_index") != 0):
        raise RecordError("ReFrame run counts are invalid")
    matches = [case for case in run["testcases"]
               if isinstance(case, dict) and case.get("name") == EXPECTED_CHECK]
    if len(matches) != 1:
        raise RecordError("expected PR smoke check does not appear exactly once")
    case = matches[0]
    expected_filename = str((Path(__file__).parent.parent /
                             "pmix_python_binding/reframe/"
                             "pmix_tests_pr_python_smoke_test.py").resolve())
    try:
        reported_filename = str(Path(case.get("filename", "")).resolve())
    except (OSError, TypeError) as error:
        raise RecordError("ReFrame check filename is invalid") from error
    if reported_filename != expected_filename:
        raise RecordError("ReFrame report does not identify the trusted adapter")
    if (case.get("unique_name") != EXPECTED_CHECK or
            case.get("fixture") is not False or
            case.get("system") != "frontier" or
            case.get("partition") != "batch" or
            case.get("environ") != "pmix_test" or
            case.get("scheduler") != "slurm"):
        raise RecordError("ReFrame check identity is invalid")
    stagedir = case.get("stagedir")
    if not isinstance(stagedir, str) or not stagedir:
        raise RecordError("ReFrame report lacks a stage directory")
    try:
        reported_stage = Path(stagedir).resolve(strict=True)
        evidence_stage = Path(evidence_directory).resolve(strict=True)
    except (OSError, TypeError) as error:
        raise RecordError("ReFrame stage evidence is unavailable") from error
    if reported_stage != evidence_stage:
        raise RecordError("ReFrame report stage does not match run evidence")
    jobid = case.get("jobid")
    if not isinstance(jobid, str) or JOB_ID_RE.fullmatch(jobid) is None:
        raise RecordError("ReFrame report lacks a valid Slurm job identifier")
    submit_time = case.get("job_submit_time")
    completion_text = case.get("job_completion_time")
    completion_time = case.get("job_completion_time_unix")
    time_run = case.get("time_run")
    if (not numeric(submit_time) or not numeric(completion_time) or
            not isinstance(completion_text, str) or not completion_text or
            completion_time < submit_time or not numeric(time_run) or time_run <= 0):
        raise RecordError("ReFrame report lacks completed run timing evidence")
    if not check_ran or server_exit is None or client_exit is None:
        raise RecordError("the selected workload did not demonstrably complete")

    report_result = case.get("result")
    fail_phase = case.get("fail_phase")
    job_exitcode = case.get("job_exitcode")
    if not isinstance(job_exitcode, int) or isinstance(job_exitcode, bool):
        raise RecordError("ReFrame report lacks a valid job exit code")
    if (report_result == "pass" and fail_phase is None and
            client_exit == 0 and server_exit == 0 and
            job_exitcode == 0 and reframe_status == 0):
        return "success"
    # A sanity failure is an ordinary test failure only after both selected
    # Python programs and the Slurm job completed successfully.  Scheduler,
    # wait, submission, setup, dependency, timeout, launch, and all other
    # ambiguous outcomes remain infrastructure errors.
    if (report_result == "fail" and fail_phase == "sanity" and
            client_exit == 0 and server_exit == 0 and
            job_exitcode == 0 and reframe_status == 1):
        return "failure"
    raise RecordError("ReFrame outcome is ambiguous or infrastructural")


def command_write_preparation(options):
    if PR_NUMBER_RE.fullmatch(options.pr_number) is None:
        raise RecordError("requested PR number is invalid")
    if options.expected_sha is not None and SHA_RE.fullmatch(options.expected_sha) is None:
        raise RecordError("expected SHA is invalid")
    if options.expected_author is not None and options.expected_author not in TRUSTED_AUTHORS:
        raise RecordError("expected author is invalid")
    if PIPELINE_ID_RE.fullmatch(options.pipeline_id) is None:
        raise RecordError("pipeline identifier is invalid")
    trusted = validate_trusted_record(
        options.trusted_record, options.pr_number,
        options.expected_sha, options.expected_author)
    values = preparation_values(trusted, options.result, options.pipeline_id)
    atomic_record(options.output, PREPARATION_FIELDS, values,
                  "preparation output")
    return 0


def command_read_preparation(options):
    if (options.expected_pipeline_id is not None and
            PIPELINE_ID_RE.fullmatch(options.expected_pipeline_id) is None):
        raise RecordError("expected pipeline identifier is invalid")
    values = read_preparation(
        options.input, options.require_ready, options.expected_pipeline_id)
    print(values[options.field])
    return 0


def command_write_error(options):
    if PIPELINE_ID_RE.fullmatch(options.pipeline_id) is None:
        raise RecordError("pipeline identifier is invalid")
    preparation = read_preparation(
        options.preparation, expected_pipeline_id=options.pipeline_id)
    if EXECUTION_ID_RE.fullmatch(options.execution_id) is None:
        raise RecordError("execution identifier is invalid")
    values = result_values(
        preparation["PR_HEAD_SHA"], "error", False, "missing",
        options.execution_id, "unavailable", options.pipeline_id)
    atomic_record(options.output, RESULT_FIELDS, values,
                  "execution result output")
    return 0


def command_classify(options):
    if PIPELINE_ID_RE.fullmatch(options.pipeline_id) is None:
        raise RecordError("pipeline identifier is invalid")
    preparation = read_preparation(
        options.preparation, require_ready=True,
        expected_pipeline_id=options.pipeline_id)
    if EXECUTION_ID_RE.fullmatch(options.execution_id) is None:
        raise RecordError("execution identifier is invalid")
    try:
        reframe_status = int(options.reframe_status)
    except ValueError as error:
        raise RecordError("ReFrame status is invalid") from error
    if str(reframe_status) != options.reframe_status or not 0 <= reframe_status <= 255:
        raise RecordError("ReFrame status is invalid")

    report_digest = "missing"
    check_ran = False
    classification = "error"
    try:
        document, report_digest = read_json_report(options.report)
        check_ran, server_exit, client_exit = evidence_state(
            options.evidence_directory, preparation["PR_HEAD_SHA"],
            options.execution_id)
        classification = classify_report(
            document, options.evidence_directory, reframe_status,
            check_ran, server_exit, client_exit)
    except RecordError:
        classification = "error"

    values = result_values(
        preparation["PR_HEAD_SHA"], classification, check_ran,
        report_digest, options.execution_id, str(reframe_status),
        options.pipeline_id)
    atomic_record(options.output, RESULT_FIELDS, values,
                  "execution result output")
    return {"success": 0, "failure": 1, "error": 2}[classification]


def command_final_decision(options):
    if PIPELINE_ID_RE.fullmatch(options.pipeline_id) is None:
        raise RecordError("pipeline identifier is invalid")
    preparation = read_preparation(
        options.preparation, expected_pipeline_id=options.pipeline_id)
    decision = "error"
    if preparation["PREPARATION_RESULT"] == "ready":
        try:
            result = read_result(options.result)
            if result["CI_PIPELINE_ID"] != preparation["CI_PIPELINE_ID"]:
                raise RecordError("preparation and execution pipelines differ")
            if result["PR_HEAD_SHA"] != preparation["PR_HEAD_SHA"]:
                raise RecordError("preparation and execution SHAs differ")
            decision = result["RESULT"]
        except RecordError:
            decision = "error"
    print("{} {}".format(preparation["PR_HEAD_SHA"], decision))
    return 0


def command_validate_checkout(options):
    root, root_metadata = inspect_components(options.checkout, "checkout root")
    if root_metadata is None or not stat.S_ISDIR(root_metadata.st_mode):
        raise RecordError("checkout root is not a real directory")
    if root_metadata.st_mode & 0o022:
        raise RecordError("checkout root has unsafe write permissions")
    root_real = root.resolve(strict=True)
    for relative, expected_type in (("python", "directory"),
                                    ("python/server.py", "file"),
                                    ("python/client.py", "file")):
        candidate = root / relative
        _, metadata = inspect_components(candidate, "checkout {}".format(relative))
        if expected_type == "directory":
            if not stat.S_ISDIR(metadata.st_mode):
                raise RecordError("checkout python path is not a directory")
        else:
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                raise RecordError("checkout source is not a regular single-link file")
        if metadata.st_mode & 0o022:
            raise RecordError("checkout source path has unsafe write permissions")
        resolved = candidate.resolve(strict=True)
        try:
            resolved.relative_to(root_real)
        except ValueError as error:
            raise RecordError("checkout source escapes the checkout root") from error
    print(str(root_real))
    return 0


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    prepare = subparsers.add_parser("write-preparation")
    prepare.add_argument("--trusted-record", required=True, type=Path)
    prepare.add_argument("--pr-number", required=True)
    prepare.add_argument("--result", required=True, choices=("ready", "error"))
    prepare.add_argument("--pipeline-id", required=True)
    prepare.add_argument("--expected-sha")
    prepare.add_argument("--expected-author")
    prepare.add_argument("--output", required=True, type=Path)
    prepare.set_defaults(function=command_write_preparation)

    read_prepare = subparsers.add_parser("read-preparation")
    read_prepare.add_argument("--input", required=True, type=Path)
    read_prepare.add_argument("--require-ready", action="store_true")
    read_prepare.add_argument("--expected-pipeline-id")
    read_prepare.add_argument("--field", required=True,
                              choices=PREPARATION_FIELDS)
    read_prepare.set_defaults(function=command_read_preparation)

    write_error = subparsers.add_parser("write-error-result")
    write_error.add_argument("--preparation", required=True, type=Path)
    write_error.add_argument("--execution-id", required=True)
    write_error.add_argument("--pipeline-id", required=True)
    write_error.add_argument("--output", required=True, type=Path)
    write_error.set_defaults(function=command_write_error)

    classify = subparsers.add_parser("classify-report")
    classify.add_argument("--preparation", required=True, type=Path)
    classify.add_argument("--report", required=True, type=Path)
    classify.add_argument("--evidence-directory", required=True, type=Path)
    classify.add_argument("--execution-id", required=True)
    classify.add_argument("--pipeline-id", required=True)
    classify.add_argument("--reframe-status", required=True)
    classify.add_argument("--output", required=True, type=Path)
    classify.set_defaults(function=command_classify)

    final = subparsers.add_parser("final-decision")
    final.add_argument("--preparation", required=True, type=Path)
    final.add_argument("--result", required=True, type=Path)
    final.add_argument("--pipeline-id", required=True)
    final.set_defaults(function=command_final_decision)

    checkout = subparsers.add_parser("validate-checkout")
    checkout.add_argument("--checkout", required=True, type=Path)
    checkout.set_defaults(function=command_validate_checkout)
    return parser


def main():
    try:
        options = build_parser().parse_args()
        return options.function(options)
    except RecordError as error:
        print("error: {}".format(error), file=os.sys.stderr)
        return 2
    except OSError as error:
        print("error: artifact operation failed: {}".format(error),
              file=os.sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
