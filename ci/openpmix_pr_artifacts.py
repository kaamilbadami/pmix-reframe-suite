#!/usr/bin/env python3
"""Create and validate strict records for the OpenPMIx PR prototype.

Records are ordered ASCII data with closed schemas.  They are never sourced as
shell code.  Every preparation and result is bound to the canonical PR number,
author, base repository, head repository, exact SHA, source policy, and GitLab
pipeline ID.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile


TARGET_REPOSITORY = "openpmix/openpmix"
TRUSTED_AUTHORS = frozenset(("kaamilbadami", "rhc54"))
ALLOWED_SOURCE_IDENTITIES = frozenset((
    ("kaamilbadami", "openpmix/openpmix", "same-repository"),
    ("rhc54", "openpmix/openpmix", "same-repository"),
    ("rhc54", "rhc54/openpmix", "verified-author-fork"),
))

ELIGIBILITY_VERSION = "1"
PREPARATION_VERSION = "1"
CHECKOUT_VERSION = "1"
RESULT_VERSION = "2"
FINAL_VERSION = "1"
MAX_RECORD_SIZE = 4096
MAX_REPORT_SIZE = 16 * 1024 * 1024

SHA_RE = re.compile(r"[0-9a-f]{40}\Z")
DIGEST_RE = re.compile(r"[0-9a-f]{64}\Z")
EXECUTION_ID_RE = re.compile(r"[0-9a-f]{32}\Z")
PR_NUMBER_RE = re.compile(r"[1-9][0-9]*\Z")
PIPELINE_ID_RE = re.compile(r"[1-9][0-9]*\Z")
AUTHOR_RE = re.compile(
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?\Z"
)

ELIGIBILITY_FIELDS = (
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
PREPARATION_FIELDS = (
    "OPENPMIX_PR_PREPARATION_VERSION",
    "CI_PIPELINE_ID",
    "PR_NUMBER",
    "PR_AUTHOR",
    "PR_BASE_REPOSITORY",
    "PR_HEAD_REPOSITORY",
    "PR_HEAD_SHA",
    "PR_SOURCE_POLICY",
    "PREPARATION_RESULT",
)
RESULT_FIELDS = (
    "OPENPMIX_PR_RESULT_VERSION",
    "CI_PIPELINE_ID",
    "PR_NUMBER",
    "PR_AUTHOR",
    "PR_BASE_REPOSITORY",
    "PR_HEAD_REPOSITORY",
    "PR_HEAD_SHA",
    "PR_SOURCE_POLICY",
    "EXECUTION_ID",
    "RESULT",
    "RESULT_CLASSIFICATION",
    "RUNNER_EXIT_STATUS",
    "REPORT_SHA256",
    "CHECKOUT_EVIDENCE_SHA256",
)
CHECKOUT_FIELDS = (
    "OPENPMIX_PR_CHECKOUT_VERSION",
    "CI_PIPELINE_ID",
    "PR_NUMBER",
    "PR_AUTHOR",
    "PR_BASE_REPOSITORY",
    "PR_HEAD_REPOSITORY",
    "PR_HEAD_SHA",
    "PR_SOURCE_POLICY",
    "UPSTREAM_REPOSITORY",
    "PULL_REF",
    "FETCHED_SHA",
    "CHECKED_OUT_SHA",
    "DETACHED_HEAD",
    "CLEAN_CHECKOUT",
    "GITLINK_COUNT",
)
FINAL_FIELDS = (
    "OPENPMIX_PR_FINAL_VERSION",
    "CI_PIPELINE_ID",
    "PR_NUMBER",
    "PR_AUTHOR",
    "PR_BASE_REPOSITORY",
    "PR_HEAD_REPOSITORY",
    "PR_HEAD_SHA",
    "PR_SOURCE_POLICY",
    "EXECUTION_ID",
    "RESULT",
    "RESULT_CLASSIFICATION",
    "RUNNER_EXIT_STATUS",
    "REPORT_SHA256",
    "CHECKOUT_EVIDENCE_SHA256",
    "FINALIZATION_RESULT",
)
IDENTITY_FIELDS = (
    "CI_PIPELINE_ID",
    "PR_NUMBER",
    "PR_AUTHOR",
    "PR_BASE_REPOSITORY",
    "PR_HEAD_REPOSITORY",
    "PR_HEAD_SHA",
    "PR_SOURCE_POLICY",
)

SUCCESS_CLASSIFICATIONS = frozenset(("tests-passed",))
FAILURE_CLASSIFICATIONS = frozenset((
    "test-failure",
    "source-build-failure",
))
ERROR_CLASSIFICATIONS = frozenset((
    "configuration-error",
    "scheduler-error",
    "fixture-error",
    "frontend-error",
    "malformed-report",
    "checkout-error",
    "head-changed",
    "missing-pr-ref",
    "unauthorized-submodule",
))
ALL_CLASSIFICATIONS = (
    SUCCESS_CLASSIFICATIONS | FAILURE_CLASSIFICATIONS | ERROR_CLASSIFICATIONS
)


class RecordError(Exception):
    """An artifact cannot be trusted across a job boundary."""


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


def read_regular_bytes(path, label):
    path, metadata = inspect_components(path, label)
    if metadata is None or not stat.S_ISREG(metadata.st_mode):
        raise RecordError("{} is not a regular file".format(label))
    if metadata.st_nlink != 1:
        raise RecordError("{} has an unsafe link count".format(label))
    if metadata.st_size > MAX_RECORD_SIZE:
        raise RecordError("{} is too large".format(label))
    try:
        data = path.read_bytes()
    except OSError as error:
        raise RecordError("{} could not be read".format(label)) from error
    if len(data) != metadata.st_size:
        raise RecordError("{} changed while it was read".format(label))
    return data


def parse_ordered_record(path, fields, label):
    data = read_regular_bytes(path, label)
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
        if separator != "=" or key != expected or not value:
            raise RecordError("{} has an invalid schema".format(label))
        if key in values:
            raise RecordError("{} contains a duplicate key".format(label))
        values[key] = value
    return values


def validate_identity(values, label):
    if PR_NUMBER_RE.fullmatch(values["PR_NUMBER"]) is None:
        raise RecordError("{} PR number is invalid".format(label))
    if (AUTHOR_RE.fullmatch(values["PR_AUTHOR"]) is None or
            values["PR_AUTHOR"] not in TRUSTED_AUTHORS):
        raise RecordError("{} author is invalid".format(label))
    if values["PR_BASE_REPOSITORY"] != TARGET_REPOSITORY:
        raise RecordError("{} base repository is invalid".format(label))
    source_identity = (
        values["PR_AUTHOR"],
        values["PR_HEAD_REPOSITORY"],
        values["PR_SOURCE_POLICY"],
    )
    if source_identity not in ALLOWED_SOURCE_IDENTITIES:
        raise RecordError("{} source identity is invalid".format(label))
    if SHA_RE.fullmatch(values["PR_HEAD_SHA"]) is None:
        raise RecordError("{} SHA is invalid".format(label))
    if ("CI_PIPELINE_ID" in values and
            PIPELINE_ID_RE.fullmatch(values["CI_PIPELINE_ID"]) is None):
        raise RecordError("{} pipeline ID is invalid".format(label))
    return values


def read_eligibility(path):
    values = parse_ordered_record(
        path, ELIGIBILITY_FIELDS, "eligibility record"
    )
    if values["OPENPMIX_PR_ELIGIBILITY_VERSION"] != ELIGIBILITY_VERSION:
        raise RecordError("eligibility version is unsupported")
    if values["PR_STATE"] != "open" or values["PR_DRAFT"] != "0":
        raise RecordError("eligibility state is invalid")
    return validate_identity(values, "eligibility")


def validate_preparation_values(values, require_ready=False,
                                expected_pipeline_id=None):
    if values["OPENPMIX_PR_PREPARATION_VERSION"] != PREPARATION_VERSION:
        raise RecordError("preparation version is unsupported")
    validate_identity(values, "preparation")
    if (expected_pipeline_id is not None and
            values["CI_PIPELINE_ID"] != expected_pipeline_id):
        raise RecordError("preparation belongs to another pipeline")
    if values["PREPARATION_RESULT"] not in ("ready", "error"):
        raise RecordError("preparation result is invalid")
    if require_ready and values["PREPARATION_RESULT"] != "ready":
        raise RecordError("preparation did not complete successfully")
    return values


def read_preparation(path, require_ready=False, expected_pipeline_id=None):
    values = parse_ordered_record(
        path, PREPARATION_FIELDS, "preparation record"
    )
    return validate_preparation_values(
        values, require_ready, expected_pipeline_id
    )


def validate_checkout_values(values):
    if values["OPENPMIX_PR_CHECKOUT_VERSION"] != CHECKOUT_VERSION:
        raise RecordError("checkout version is unsupported")
    validate_identity(values, "checkout")
    expected_ref = "refs/pull/{}/head".format(values["PR_NUMBER"])
    if values["UPSTREAM_REPOSITORY"] != TARGET_REPOSITORY:
        raise RecordError("checkout upstream repository is invalid")
    if values["PULL_REF"] != expected_ref:
        raise RecordError("checkout pull ref is invalid")
    if (values["FETCHED_SHA"] != values["PR_HEAD_SHA"] or
            values["CHECKED_OUT_SHA"] != values["PR_HEAD_SHA"]):
        raise RecordError("checkout SHA does not match preparation")
    if values["DETACHED_HEAD"] != "1":
        raise RecordError("checkout is not detached")
    if values["CLEAN_CHECKOUT"] != "1":
        raise RecordError("checkout is not clean")
    if values["GITLINK_COUNT"] != "0":
        raise RecordError("checkout contains unauthorized submodules")
    return values


def read_checkout(path):
    values = parse_ordered_record(path, CHECKOUT_FIELDS, "checkout record")
    return validate_checkout_values(values)


def expected_result_for_classification(classification):
    if classification in SUCCESS_CLASSIFICATIONS:
        return "success"
    if classification in FAILURE_CLASSIFICATIONS:
        return "failure"
    if classification in ERROR_CLASSIFICATIONS:
        return "error"
    raise RecordError("result classification is invalid")


def validate_result_values(values):
    if values["OPENPMIX_PR_RESULT_VERSION"] != RESULT_VERSION:
        raise RecordError("result version is unsupported")
    validate_identity(values, "result")
    if EXECUTION_ID_RE.fullmatch(values["EXECUTION_ID"]) is None:
        raise RecordError("execution identifier is invalid")
    classification = values["RESULT_CLASSIFICATION"]
    expected_result = expected_result_for_classification(classification)
    if values["RESULT"] != expected_result:
        raise RecordError("result and classification are inconsistent")
    runner_status = values["RUNNER_EXIT_STATUS"]
    runner_status_number = None
    if runner_status != "unavailable":
        try:
            runner_status_number = int(runner_status)
        except ValueError as error:
            raise RecordError("runner exit status is invalid") from error
        if (str(runner_status_number) != runner_status or
                not 0 <= runner_status_number <= 255):
            raise RecordError("runner exit status is invalid")
    for field in ("REPORT_SHA256", "CHECKOUT_EVIDENCE_SHA256"):
        digest = values[field]
        if digest != "missing" and DIGEST_RE.fullmatch(digest) is None:
            raise RecordError("{} is invalid".format(field))
    if expected_result in ("success", "failure"):
        if (values["REPORT_SHA256"] == "missing" or
                values["CHECKOUT_EVIDENCE_SHA256"] == "missing"):
            raise RecordError("test outcome lacks retained evidence")
    if expected_result == "success" and runner_status_number != 0:
        raise RecordError("successful result has inconsistent runner status")
    if expected_result == "failure" and runner_status_number != 1:
        raise RecordError("failed result has inconsistent runner status")
    return values


def read_result(path):
    values = parse_ordered_record(path, RESULT_FIELDS, "result record")
    return validate_result_values(values)


EXPECTED_TESTS = frozenset((
    "PMIxPythonScalingTest",
    "PMIxPythonScalingMultinodeTest",
    "PMIxPythonMappingPPRNodeTest",
    "PMIxPythonMappingPPRL3CacheTest",
    "PMIxPythonWorkerThreadsCompat1Test",
    "PMIxPythonWorkerThreadsCompat2Test",
    "PMIxPythonTargetedCompatTest",
    "PMIxPythonMixedThreadCompatTest",
    "PMIxPythonChildTimeoutTest",
    "PMIxPythonEventFailurePropagationTest",
    "PMIxPythonTargetHostFailurePropagationTest",
))
EXPECTED_FIXTURES = frozenset((
    "fetch_libevent",
    "build_libevent",
    "fetch_pmix",
    "build_pmix",
    "fetch_prrte",
    "build_prrte",
))


def read_report(path):
    path, metadata = inspect_components(path, "ReFrame report")
    if metadata is None or not stat.S_ISREG(metadata.st_mode):
        raise RecordError("ReFrame report is not a regular file")
    if metadata.st_nlink != 1 or metadata.st_size > MAX_REPORT_SIZE:
        raise RecordError("ReFrame report metadata is unsafe")
    data = path.read_bytes()
    if len(data) != metadata.st_size:
        raise RecordError("ReFrame report changed while it was read")
    digest = hashlib.sha256(data).hexdigest()
    try:
        document = json.loads(
            data.decode("utf-8"),
            object_pairs_hook=lambda pairs: reject_duplicate_json_keys(pairs),
            parse_constant=lambda value: (_ for _ in ()).throw(
                RecordError("ReFrame report contains a nonstandard number")
            ),
        )
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as error:
        raise RecordError("ReFrame report is malformed") from error
    if not isinstance(document, dict):
        raise RecordError("ReFrame report top level is invalid")
    return document, digest


def reject_duplicate_json_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise RecordError("ReFrame report contains a duplicate key")
        result[key] = value
    return result


def case_base_name(case):
    name = case.get("name")
    if not isinstance(name, str):
        raise RecordError("ReFrame case name is invalid")
    return name.split(" ~", 1)[0]


def validate_case_identity(case):
    if not isinstance(case, dict):
        raise RecordError("ReFrame case is not an object")
    if (case.get("system") != "frontier" or
            case.get("partition") != "batch" or
            case.get("environ") != "pmix_test" or
            case.get("scheduler") != "slurm"):
        raise RecordError("ReFrame case target identity is invalid")
    if case.get("result") not in ("pass", "fail", "skip", "abort"):
        raise RecordError("ReFrame case result is invalid")
    return case_base_name(case)


def classify_reframe_report(document, runner_status):
    if set(document) != set(("session_info", "runs", "restored_cases")):
        raise RecordError("ReFrame report schema is unexpected")
    session = document["session_info"]
    runs = document["runs"]
    if (not isinstance(session, dict) or
            session.get("version") != "4.10.0" or
            session.get("data_version") != "4.2"):
        raise RecordError("ReFrame report version is unsupported")
    if document["restored_cases"] != [] or not isinstance(runs, list) or len(runs) != 1:
        raise RecordError("ReFrame report run structure is invalid")
    run = runs[0]
    if not isinstance(run, dict) or not isinstance(run.get("testcases"), list):
        raise RecordError("ReFrame testcases are invalid")
    cases = run["testcases"]
    counts = (
        run.get("num_cases"), run.get("num_failures"),
        run.get("num_aborted"), run.get("num_skipped"),
    )
    if (any(not isinstance(value, int) or isinstance(value, bool) or value < 0
            for value in counts) or
            counts[0] != len(cases) or
            sum(counts[1:]) > counts[0] or
            run.get("run_index") != 0):
        raise RecordError("ReFrame report counts are invalid")

    test_names = []
    fixture_names = []
    for case in cases:
        name = validate_case_identity(case)
        fixture = case.get("fixture")
        if fixture is True:
            fixture_names.append(name)
        elif fixture is False:
            test_names.append(name)
        else:
            raise RecordError("ReFrame fixture marker is invalid")
    if (len(cases) != 17 or len(test_names) != 11 or
            frozenset(test_names) != EXPECTED_TESTS or
            len(set(test_names)) != 11 or
            len(fixture_names) != 6 or
            frozenset(fixture_names) != EXPECTED_FIXTURES or
            len(set(fixture_names)) != 6):
        raise RecordError("ReFrame report does not contain the exact suite")

    failed = [case for case in cases if case.get("result") == "fail"]
    all_passed = all(case.get("result") == "pass" for case in cases)
    if all_passed and counts[1:] == (0, 0, 0) and runner_status == 0:
        return "success", "tests-passed"
    if runner_status != 1:
        return "error", "frontend-error"
    if not failed:
        return "error", "configuration-error"

    failure_classifications = []
    for case in failed:
        name = case_base_name(case)
        phase = case.get("fail_phase")
        reason = str(case.get("fail_reason", "")).lower()
        if (any(term in reason for term in (
                "scheduler", "slurm", "cancel", "timeout", "submission",
                "job error", "wait")) or phase in ("startup", "cleanup")):
            failure_classifications.append(("error", "scheduler-error"))
        elif case.get("fixture") is True:
            if name in ("fetch_libevent", "build_libevent", "fetch_prrte"):
                failure_classifications.append(("error", "fixture-error"))
            elif name == "fetch_pmix":
                failure_classifications.append(("error", "checkout-error"))
            elif name in ("build_pmix", "build_prrte"):
                failure_classifications.append(
                    ("failure", "source-build-failure")
                )
            else:
                failure_classifications.append(
                    ("error", "configuration-error")
                )
        elif phase in ("run", "sanity", "performance"):
            failure_classifications.append(("failure", "test-failure"))
        else:
            failure_classifications.append(("error", "configuration-error"))

    errors = [item for item in failure_classifications if item[0] == "error"]
    if errors:
        priority = (
            "scheduler-error", "fixture-error", "checkout-error",
            "configuration-error",
        )
        for classification in priority:
            if any(item[1] == classification for item in errors):
                return "error", classification
    if any(item[1] == "source-build-failure"
           for item in failure_classifications):
        return "failure", "source-build-failure"
    return "failure", "test-failure"


def validate_output_path(path, label):
    path, metadata = inspect_components(
        path, label, allow_missing_final=True
    )
    try:
        parent_metadata = path.parent.lstat()
    except OSError as error:
        raise RecordError("{} parent is unavailable".format(label)) from error
    if (stat.S_ISLNK(parent_metadata.st_mode) or
            not stat.S_ISDIR(parent_metadata.st_mode)):
        raise RecordError("{} parent is unsafe".format(label))
    if metadata is not None and (
            not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1):
        raise RecordError("{} is not a safe regular file".format(label))
    return path


def atomic_record(path, fields, values, label):
    path = validate_output_path(path, label)
    content = "".join(
        "{}={}\n".format(field, values[field]) for field in fields
    ).encode("ascii")
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


def preparation_values(eligibility, pipeline_id, result):
    return {
        "OPENPMIX_PR_PREPARATION_VERSION": PREPARATION_VERSION,
        "CI_PIPELINE_ID": pipeline_id,
        "PR_NUMBER": eligibility["PR_NUMBER"],
        "PR_AUTHOR": eligibility["PR_AUTHOR"],
        "PR_BASE_REPOSITORY": eligibility["PR_BASE_REPOSITORY"],
        "PR_HEAD_REPOSITORY": eligibility["PR_HEAD_REPOSITORY"],
        "PR_HEAD_SHA": eligibility["PR_HEAD_SHA"],
        "PR_SOURCE_POLICY": eligibility["PR_SOURCE_POLICY"],
        "PREPARATION_RESULT": result,
    }


def checkout_values(preparation, fetched_sha, checked_out_sha):
    values = {
        "OPENPMIX_PR_CHECKOUT_VERSION": CHECKOUT_VERSION,
        "CI_PIPELINE_ID": preparation["CI_PIPELINE_ID"],
        "PR_NUMBER": preparation["PR_NUMBER"],
        "PR_AUTHOR": preparation["PR_AUTHOR"],
        "PR_BASE_REPOSITORY": preparation["PR_BASE_REPOSITORY"],
        "PR_HEAD_REPOSITORY": preparation["PR_HEAD_REPOSITORY"],
        "PR_HEAD_SHA": preparation["PR_HEAD_SHA"],
        "PR_SOURCE_POLICY": preparation["PR_SOURCE_POLICY"],
        "UPSTREAM_REPOSITORY": TARGET_REPOSITORY,
        "PULL_REF": "refs/pull/{}/head".format(preparation["PR_NUMBER"]),
        "FETCHED_SHA": fetched_sha,
        "CHECKED_OUT_SHA": checked_out_sha,
        "DETACHED_HEAD": "1",
        "CLEAN_CHECKOUT": "1",
        "GITLINK_COUNT": "0",
    }
    return validate_checkout_values(values)


def result_values(preparation, execution_id, result, classification,
                  runner_status, report_digest, checkout_digest):
    values = {
        "OPENPMIX_PR_RESULT_VERSION": RESULT_VERSION,
        "CI_PIPELINE_ID": preparation["CI_PIPELINE_ID"],
        "PR_NUMBER": preparation["PR_NUMBER"],
        "PR_AUTHOR": preparation["PR_AUTHOR"],
        "PR_BASE_REPOSITORY": preparation["PR_BASE_REPOSITORY"],
        "PR_HEAD_REPOSITORY": preparation["PR_HEAD_REPOSITORY"],
        "PR_HEAD_SHA": preparation["PR_HEAD_SHA"],
        "PR_SOURCE_POLICY": preparation["PR_SOURCE_POLICY"],
        "EXECUTION_ID": execution_id,
        "RESULT": result,
        "RESULT_CLASSIFICATION": classification,
        "RUNNER_EXIT_STATUS": runner_status,
        "REPORT_SHA256": report_digest,
        "CHECKOUT_EVIDENCE_SHA256": checkout_digest,
    }
    return validate_result_values(values)


def require_pipeline_id(value, label):
    if PIPELINE_ID_RE.fullmatch(value) is None:
        raise RecordError("{} is invalid".format(label))


def command_write_preparation(options):
    require_pipeline_id(options.pipeline_id, "pipeline ID")
    if PR_NUMBER_RE.fullmatch(options.pr_number) is None:
        raise RecordError("requested PR number is invalid")
    eligibility = read_eligibility(options.eligibility)
    if eligibility["PR_NUMBER"] != options.pr_number:
        raise RecordError("eligibility PR number does not match request")
    expected = (
        ("PR_HEAD_SHA", options.expected_sha),
        ("PR_AUTHOR", options.expected_author),
        ("PR_HEAD_REPOSITORY", options.expected_head_repository),
    )
    for field, value in expected:
        if value is not None and eligibility[field] != value:
            raise RecordError("{} changed during revalidation".format(field))
    values = preparation_values(
        eligibility, options.pipeline_id, options.result
    )
    atomic_record(
        options.output, PREPARATION_FIELDS, values, "preparation output"
    )
    return 0


def command_read_preparation(options):
    if options.expected_pipeline_id is not None:
        require_pipeline_id(
            options.expected_pipeline_id, "expected pipeline ID"
        )
    values = read_preparation(
        options.input, options.require_ready, options.expected_pipeline_id
    )
    print(values[options.field])
    return 0


def command_write_checkout(options):
    require_pipeline_id(options.pipeline_id, "pipeline ID")
    preparation = read_preparation(
        options.preparation,
        require_ready=True,
        expected_pipeline_id=options.pipeline_id,
    )
    if (SHA_RE.fullmatch(options.fetched_sha) is None or
            SHA_RE.fullmatch(options.checked_out_sha) is None):
        raise RecordError("checkout SHA argument is invalid")
    values = checkout_values(
        preparation, options.fetched_sha, options.checked_out_sha
    )
    atomic_record(options.output, CHECKOUT_FIELDS, values, "checkout output")
    return 0


def command_read_checkout(options):
    values = read_checkout(options.input)
    if options.expected_pipeline_id is not None:
        require_pipeline_id(
            options.expected_pipeline_id, "expected pipeline ID"
        )
        if values["CI_PIPELINE_ID"] != options.expected_pipeline_id:
            raise RecordError("checkout belongs to another pipeline")
    print(values[options.field])
    return 0


def command_write_result(options):
    require_pipeline_id(options.pipeline_id, "pipeline ID")
    preparation = read_preparation(
        options.preparation,
        require_ready=True,
        expected_pipeline_id=options.pipeline_id,
    )
    values = result_values(
        preparation,
        options.execution_id,
        options.result,
        options.classification,
        options.runner_exit_status,
        options.report_sha256,
        options.checkout_evidence_sha256,
    )
    atomic_record(options.output, RESULT_FIELDS, values, "result output")
    return 0


def command_read_result(options):
    values = read_result(options.input)
    if options.expected_pipeline_id is not None:
        require_pipeline_id(
            options.expected_pipeline_id, "expected pipeline ID"
        )
        if values["CI_PIPELINE_ID"] != options.expected_pipeline_id:
            raise RecordError("result belongs to another pipeline")
    print(values[options.field])
    return 0


def parse_runner_status(value):
    try:
        status = int(value)
    except ValueError as error:
        raise RecordError("runner exit status is invalid") from error
    if str(status) != value or not 0 <= status <= 255:
        raise RecordError("runner exit status is invalid")
    return status


def command_classify_report(options):
    require_pipeline_id(options.pipeline_id, "pipeline ID")
    preparation = read_preparation(
        options.preparation,
        require_ready=True,
        expected_pipeline_id=options.pipeline_id,
    )
    checkout = read_checkout(options.checkout)
    for field in IDENTITY_FIELDS:
        if preparation[field] != checkout[field]:
            raise RecordError(
                "preparation and checkout differ at {}".format(field)
            )
    if EXECUTION_ID_RE.fullmatch(options.execution_id) is None:
        raise RecordError("execution identifier is invalid")
    runner_status = parse_runner_status(options.runner_exit_status)
    checkout_digest = hashlib.sha256(
        read_regular_bytes(options.checkout, "checkout record")
    ).hexdigest()
    report_digest = "missing"
    result = "error"
    classification = "malformed-report"
    try:
        document, report_digest = read_report(options.report)
        result, classification = classify_reframe_report(
            document, runner_status
        )
    except RecordError:
        result, classification = "error", "malformed-report"
    values = result_values(
        preparation,
        options.execution_id,
        result,
        classification,
        str(runner_status),
        report_digest,
        checkout_digest,
    )
    atomic_record(options.output, RESULT_FIELDS, values, "result output")
    return {"success": 0, "failure": 1, "error": 2}[result]


def command_validate_agreement(options):
    require_pipeline_id(options.pipeline_id, "pipeline ID")
    preparation = read_preparation(
        options.preparation,
        require_ready=True,
        expected_pipeline_id=options.pipeline_id,
    )
    result = read_result(options.result)
    for field in IDENTITY_FIELDS:
        if preparation[field] != result[field]:
            raise RecordError(
                "preparation and result differ at {}".format(field)
            )
    print("{} {} {}".format(
        preparation["PR_HEAD_SHA"],
        result["RESULT"],
        result["RESULT_CLASSIFICATION"],
    ))
    return 0


def final_values(result):
    values = {
        "OPENPMIX_PR_FINAL_VERSION": FINAL_VERSION,
        "CI_PIPELINE_ID": result["CI_PIPELINE_ID"],
        "PR_NUMBER": result["PR_NUMBER"],
        "PR_AUTHOR": result["PR_AUTHOR"],
        "PR_BASE_REPOSITORY": result["PR_BASE_REPOSITORY"],
        "PR_HEAD_REPOSITORY": result["PR_HEAD_REPOSITORY"],
        "PR_HEAD_SHA": result["PR_HEAD_SHA"],
        "PR_SOURCE_POLICY": result["PR_SOURCE_POLICY"],
        "EXECUTION_ID": result["EXECUTION_ID"],
        "RESULT": result["RESULT"],
        "RESULT_CLASSIFICATION": result["RESULT_CLASSIFICATION"],
        "RUNNER_EXIT_STATUS": result["RUNNER_EXIT_STATUS"],
        "REPORT_SHA256": result["REPORT_SHA256"],
        "CHECKOUT_EVIDENCE_SHA256": result["CHECKOUT_EVIDENCE_SHA256"],
        "FINALIZATION_RESULT": "validated",
    }
    return values


def read_final(path):
    values = parse_ordered_record(path, FINAL_FIELDS, "final record")
    if values["OPENPMIX_PR_FINAL_VERSION"] != FINAL_VERSION:
        raise RecordError("final version is unsupported")
    if values["FINALIZATION_RESULT"] != "validated":
        raise RecordError("finalization result is invalid")
    result = {
        "OPENPMIX_PR_RESULT_VERSION": RESULT_VERSION,
        "CI_PIPELINE_ID": values["CI_PIPELINE_ID"],
        "PR_NUMBER": values["PR_NUMBER"],
        "PR_AUTHOR": values["PR_AUTHOR"],
        "PR_BASE_REPOSITORY": values["PR_BASE_REPOSITORY"],
        "PR_HEAD_REPOSITORY": values["PR_HEAD_REPOSITORY"],
        "PR_HEAD_SHA": values["PR_HEAD_SHA"],
        "PR_SOURCE_POLICY": values["PR_SOURCE_POLICY"],
        "EXECUTION_ID": values["EXECUTION_ID"],
        "RESULT": values["RESULT"],
        "RESULT_CLASSIFICATION": values["RESULT_CLASSIFICATION"],
        "RUNNER_EXIT_STATUS": values["RUNNER_EXIT_STATUS"],
        "REPORT_SHA256": values["REPORT_SHA256"],
        "CHECKOUT_EVIDENCE_SHA256": values["CHECKOUT_EVIDENCE_SHA256"],
    }
    validate_result_values(result)
    return values


def command_write_final(options):
    require_pipeline_id(options.pipeline_id, "pipeline ID")
    preparation = read_preparation(
        options.preparation,
        require_ready=True,
        expected_pipeline_id=options.pipeline_id,
    )
    result = read_result(options.result)
    for field in IDENTITY_FIELDS:
        if preparation[field] != result[field]:
            raise RecordError(
                "preparation and result differ at {}".format(field)
            )
    atomic_record(
        options.output, FINAL_FIELDS, final_values(result), "final output"
    )
    return 0


def command_read_final(options):
    values = read_final(options.input)
    if options.expected_pipeline_id is not None:
        require_pipeline_id(
            options.expected_pipeline_id, "expected pipeline ID"
        )
        if values["CI_PIPELINE_ID"] != options.expected_pipeline_id:
            raise RecordError("final record belongs to another pipeline")
    print(values[options.field])
    return 0


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    # Python 3.6's argparse does not support required=True here. main()
    # performs the equivalent closed-command check explicitly.
    subparsers = parser.add_subparsers(dest="command")

    prepare = subparsers.add_parser("write-preparation")
    prepare.add_argument("--eligibility", required=True, type=Path)
    prepare.add_argument("--pr-number", required=True)
    prepare.add_argument("--pipeline-id", required=True)
    prepare.add_argument("--result", choices=("ready", "error"), required=True)
    prepare.add_argument("--expected-sha")
    prepare.add_argument("--expected-author")
    prepare.add_argument("--expected-head-repository")
    prepare.add_argument("--output", required=True, type=Path)
    prepare.set_defaults(function=command_write_preparation)

    read_prepare = subparsers.add_parser("read-preparation")
    read_prepare.add_argument("--input", required=True, type=Path)
    read_prepare.add_argument("--require-ready", action="store_true")
    read_prepare.add_argument("--expected-pipeline-id")
    read_prepare.add_argument(
        "--field", choices=PREPARATION_FIELDS, required=True
    )
    read_prepare.set_defaults(function=command_read_preparation)

    checkout = subparsers.add_parser("write-checkout")
    checkout.add_argument("--preparation", required=True, type=Path)
    checkout.add_argument("--pipeline-id", required=True)
    checkout.add_argument("--fetched-sha", required=True)
    checkout.add_argument("--checked-out-sha", required=True)
    checkout.add_argument("--output", required=True, type=Path)
    checkout.set_defaults(function=command_write_checkout)

    read_checkout_parser = subparsers.add_parser("read-checkout")
    read_checkout_parser.add_argument("--input", required=True, type=Path)
    read_checkout_parser.add_argument("--expected-pipeline-id")
    read_checkout_parser.add_argument(
        "--field", choices=CHECKOUT_FIELDS, required=True
    )
    read_checkout_parser.set_defaults(function=command_read_checkout)

    result = subparsers.add_parser("write-result")
    result.add_argument("--preparation", required=True, type=Path)
    result.add_argument("--pipeline-id", required=True)
    result.add_argument("--execution-id", required=True)
    result.add_argument(
        "--result", choices=("success", "failure", "error"), required=True
    )
    result.add_argument(
        "--classification", choices=tuple(sorted(ALL_CLASSIFICATIONS)),
        required=True
    )
    result.add_argument("--runner-exit-status", required=True)
    result.add_argument("--report-sha256", required=True)
    result.add_argument("--checkout-evidence-sha256", required=True)
    result.add_argument("--output", required=True, type=Path)
    result.set_defaults(function=command_write_result)

    read_result_parser = subparsers.add_parser("read-result")
    read_result_parser.add_argument("--input", required=True, type=Path)
    read_result_parser.add_argument("--expected-pipeline-id")
    read_result_parser.add_argument(
        "--field", choices=RESULT_FIELDS, required=True
    )
    read_result_parser.set_defaults(function=command_read_result)

    classify = subparsers.add_parser("classify-report")
    classify.add_argument("--preparation", required=True, type=Path)
    classify.add_argument("--checkout", required=True, type=Path)
    classify.add_argument("--report", required=True, type=Path)
    classify.add_argument("--pipeline-id", required=True)
    classify.add_argument("--execution-id", required=True)
    classify.add_argument("--runner-exit-status", required=True)
    classify.add_argument("--output", required=True, type=Path)
    classify.set_defaults(function=command_classify_report)

    agreement = subparsers.add_parser("validate-agreement")
    agreement.add_argument("--preparation", required=True, type=Path)
    agreement.add_argument("--result", required=True, type=Path)
    agreement.add_argument("--pipeline-id", required=True)
    agreement.set_defaults(function=command_validate_agreement)

    final = subparsers.add_parser("write-final")
    final.add_argument("--preparation", required=True, type=Path)
    final.add_argument("--result", required=True, type=Path)
    final.add_argument("--pipeline-id", required=True)
    final.add_argument("--output", required=True, type=Path)
    final.set_defaults(function=command_write_final)

    read_final_parser = subparsers.add_parser("read-final")
    read_final_parser.add_argument("--input", required=True, type=Path)
    read_final_parser.add_argument("--expected-pipeline-id")
    read_final_parser.add_argument(
        "--field", choices=FINAL_FIELDS, required=True
    )
    read_final_parser.set_defaults(function=command_read_final)
    return parser


def main():
    try:
        options = build_parser().parse_args()
        if not hasattr(options, "function"):
            raise RecordError("a record command is required")
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
