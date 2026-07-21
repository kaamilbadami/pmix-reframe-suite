#!/usr/bin/env python3
"""Probe child-pipeline result access with the current GitLab CI job token.

The probe performs HTTP GET requests only. Exit statuses are:

* 0: complete (artifacts valid and every requested discovery probe allowed)
* 3: partial (artifacts valid but at least one discovery probe is incomplete)
* 4: failed (configuration invalid or either artifact is unavailable/invalid)
"""

from collections import namedtuple
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Dict, Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


EXIT_COMPLETE = 0
EXIT_PARTIAL = 3
EXIT_FAILED = 4
OUTPUT_DIRECTORY = Path("ci-probe-results")
JSON_LIMIT = 10 * 1024 * 1024
ARTIFACT_LIMIT = 64 * 1024
ID_PATTERN = re.compile(r"[1-9][0-9]*")
SHA_PATTERN = re.compile(r"[0-9A-Fa-f]{40}")
LOWER_SHA_PATTERN = re.compile(r"[0-9a-f]{40}")
RESULT_FIELDS = (
    "PMIX_COMMIT",
    "SUITE_COMMIT",
    "CI_JOB_STATUS",
    "CI_JOB_ID",
    "CI_PIPELINE_ID",
)
REPORT_FIELDS = (
    "SUCCESS_JOB_LIST_RESULT",
    "SUCCESS_JOB_LIST_HTTP_STATUS",
    "FAILED_JOB_LIST_RESULT",
    "FAILED_JOB_LIST_HTTP_STATUS",
    "SUCCESS_PARENT_DISCOVERY_RESULT",
    "SUCCESS_PARENT_DISCOVERY_HTTP_STATUS",
    "SUCCESS_PARENT_DISCOVERY_ENDPOINT",
    "SUCCESS_PARENT_TRIGGER_NAME",
    "SUCCESS_DOWNSTREAM_STATUS",
    "FAILED_PARENT_DISCOVERY_RESULT",
    "FAILED_PARENT_DISCOVERY_HTTP_STATUS",
    "FAILED_PARENT_DISCOVERY_ENDPOINT",
    "FAILED_PARENT_TRIGGER_NAME",
    "FAILED_DOWNSTREAM_STATUS",
    "SUCCESS_ARTIFACT_DOWNLOAD_RESULT",
    "SUCCESS_ARTIFACT_HTTP_STATUS",
    "FAILED_ARTIFACT_DOWNLOAD_RESULT",
    "FAILED_ARTIFACT_HTTP_STATUS",
    "OVERALL_RESULT",
)
KNOWN_OUTPUT_NAMES = ("success.env", "failed.env", "probe.env")


RecordProbe = namedtuple(
    "RecordProbe",
    (
        "label",
        "child_pipeline_id",
        "job_id",
        "pmix_commit",
        "suite_commit",
        "expected_status",
        "parent_pipeline_id",
    ),
)
HttpResult = namedtuple("HttpResult", ("status", "body", "headers"))


class SafeRedirectHandler(HTTPRedirectHandler):
    """Follow safe redirects without leaking JOB-TOKEN across origins."""

    def redirect_request(self, request, file_pointer, code, message, headers, new_url):
        old_parts = urlsplit(request.full_url)
        new_parts = urlsplit(new_url)
        if new_parts.scheme not in ("http", "https") or not new_parts.netloc:
            return None
        if new_parts.username is not None or new_parts.password is not None:
            return None
        if old_parts.scheme == "https" and new_parts.scheme != "https":
            return None
        request_token = request.get_header("Job-token")
        if request_token and request_token in new_url:
            return None

        same_origin = (
            old_parts.scheme.lower(),
            old_parts.hostname,
            old_parts.port,
        ) == (
            new_parts.scheme.lower(),
            new_parts.hostname,
            new_parts.port,
        )
        redirected_headers = {
            key: value
            for key, value in request.header_items()
            if key.lower() != "job-token"
        }
        if same_origin:
            if request_token is not None:
                redirected_headers["JOB-TOKEN"] = request_token

        return Request(
            new_url,
            headers=redirected_headers,
            origin_req_host=request.origin_req_host,
            unverifiable=True,
            method="GET",
        )


class GitLabGetClient:
    def __init__(self, api_url: str, project_id: str, token: str):
        self.api_url = api_url.rstrip("/")
        self.project_id = quote(project_id, safe="")
        self.token = token
        self.opener = build_opener(SafeRedirectHandler())

    def project_url(self, suffix: str) -> str:
        return f"{self.api_url}/projects/{self.project_id}/{suffix.lstrip('/')}"

    def get(self, url: str, limit: int) -> HttpResult:
        request = Request(
            url,
            headers={
                "Accept": "application/json, application/octet-stream;q=0.9",
                "JOB-TOKEN": self.token,
                "User-Agent": "pmix-child-artifact-probe/1",
            },
            method="GET",
        )
        try:
            with self.opener.open(request, timeout=30) as response:
                body = response.read(limit + 1)
                if len(body) > limit:
                    return HttpResult(response.status, None, response.headers)
                return HttpResult(response.status, body, response.headers)
        except HTTPError as error:
            status = error.code
            error.close()
            return HttpResult(status, None, {})
        except (URLError, OSError, ValueError):
            return HttpResult(None, None, {})


def initial_report() -> Dict[str, str]:
    report = {field: "" for field in REPORT_FIELDS}
    report.update(
        {
            "SUCCESS_JOB_LIST_RESULT": "invalid_response",
            "FAILED_JOB_LIST_RESULT": "invalid_response",
            "SUCCESS_PARENT_DISCOVERY_RESULT": "not_requested",
            "FAILED_PARENT_DISCOVERY_RESULT": "not_requested",
            "SUCCESS_ARTIFACT_DOWNLOAD_RESULT": "invalid",
            "FAILED_ARTIFACT_DOWNLOAD_RESULT": "invalid",
            "OVERALL_RESULT": "failed",
        }
    )
    return report


def prepare_output_directory() -> None:
    if OUTPUT_DIRECTORY.is_symlink():
        raise OSError("output directory may not be a symbolic link")
    if OUTPUT_DIRECTORY.exists() and not OUTPUT_DIRECTORY.is_dir():
        raise OSError("output path is not a directory")
    OUTPUT_DIRECTORY.mkdir(exist_ok=True)


def remove_stale_outputs() -> None:
    for name in KNOWN_OUTPUT_NAMES:
        path = OUTPUT_DIRECTORY / name
        if path.is_symlink():
            raise OSError("known output may not be a symbolic link")
        try:
            path.unlink()
        except FileNotFoundError:
            pass


def atomic_write(path: Path, content: bytes) -> None:
    temporary_name = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=path.parent,
            prefix=f".{path.name}.",
            delete=False,
        ) as temporary_file:
            temporary_name = temporary_file.name
            temporary_file.write(content)
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        os.replace(temporary_name, path)
    except OSError:
        if temporary_name is not None:
            try:
                Path(temporary_name).unlink()
            except FileNotFoundError:
                pass
        raise


def report_value(value: str, token: str) -> str:
    if token and token in value:
        return "redacted"
    return quote(value, safe="-._~")


def write_report(report: Dict[str, str], token: str) -> None:
    lines = []
    for field in REPORT_FIELDS:
        value = report_value(str(report[field]), token)
        lines.append(f"{field}={value}\n")
    content = "".join(lines).encode("utf-8")
    token_bytes = token.encode("utf-8")
    if token_bytes and token_bytes in content:
        raise OSError("refusing to write sensitive report content")
    atomic_write(OUTPUT_DIRECTORY / "probe.env", content)


def valid_id(value: str) -> bool:
    return ID_PATTERN.fullmatch(value) is not None


def required_environment(name: str, errors: list) -> str:
    value = os.environ.get(name, "")
    if not value:
        errors.append(name)
    return value


def load_configuration() -> Tuple[Optional[str], Optional[str], Optional[str], list]:
    errors = []
    api_url = required_environment("CI_API_V4_URL", errors)
    project_id = required_environment("CI_PROJECT_ID", errors)
    token = required_environment("CI_JOB_TOKEN", errors)

    if api_url:
        try:
            parts = urlsplit(api_url)
        except ValueError:
            errors.append("CI_API_V4_URL")
        else:
            if (
                parts.scheme not in ("http", "https")
                or not parts.netloc
                or parts.query
                or parts.fragment
                or parts.username is not None
                or parts.password is not None
            ):
                errors.append("CI_API_V4_URL")
        if token and token in api_url:
            errors.append("CI_API_V4_URL")
    if project_id and not valid_id(project_id):
        errors.append("CI_PROJECT_ID")

    probes = []
    for label, status in (("SUCCESS", "success"), ("FAILED", "failed")):
        child_id = required_environment(
            f"PMIX_PROBE_{label}_CHILD_PIPELINE_ID", errors
        )
        job_id = required_environment(f"PMIX_PROBE_{label}_JOB_ID", errors)
        pmix_commit = required_environment(f"PMIX_PROBE_{label}_COMMIT", errors)
        suite_commit = required_environment(
            f"PMIX_PROBE_{label}_SUITE_COMMIT", errors
        )
        parent_id = os.environ.get(f"PMIX_PROBE_{label}_PARENT_PIPELINE_ID", "")

        for name, value in (
            (f"PMIX_PROBE_{label}_CHILD_PIPELINE_ID", child_id),
            (f"PMIX_PROBE_{label}_JOB_ID", job_id),
        ):
            if value and not valid_id(value):
                errors.append(name)
        if parent_id and not valid_id(parent_id):
            errors.append(f"PMIX_PROBE_{label}_PARENT_PIPELINE_ID")
        for name, value in (
            (f"PMIX_PROBE_{label}_COMMIT", pmix_commit),
            (f"PMIX_PROBE_{label}_SUITE_COMMIT", suite_commit),
        ):
            if value and SHA_PATTERN.fullmatch(value) is None:
                errors.append(name)

        probes.append(
            RecordProbe(
                label=label,
                child_pipeline_id=child_id,
                job_id=job_id,
                pmix_commit=pmix_commit.lower(),
                suite_commit=suite_commit.lower(),
                expected_status=status,
                parent_pipeline_id=parent_id or None,
            )
        )

    return api_url or None, project_id or None, token or None, (errors, probes)


def discovery_classification(status: Optional[int]) -> str:
    if status in (401, 403):
        return "permission_denied"
    if status == 404:
        return "unsupported"
    return "invalid_response"


def artifact_classification(status: Optional[int]) -> str:
    if status in (401, 403):
        return "permission_denied"
    if status == 404:
        return "not_found"
    return "invalid"


def parse_json_list(result: HttpResult):
    if result.body is None:
        return None
    try:
        value = json.loads(result.body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, list) else None


def probe_job_list(client: GitLabGetClient, probe: RecordProbe) -> Tuple[str, Optional[int]]:
    page = 1
    matched_jobs = []
    last_status = None
    while True:
        query = urlencode(
            {
                "per_page": "100",
                "page": str(page),
                "include_retried": "true",
            }
        )
        url = client.project_url(
            f"pipelines/{quote(probe.child_pipeline_id, safe='')}/jobs?{query}"
        )
        result = client.get(url, JSON_LIMIT)
        last_status = result.status
        if result.status != 200:
            return discovery_classification(result.status), result.status
        jobs = parse_json_list(result)
        if jobs is None:
            return "invalid_response", result.status
        for job in jobs:
            if not isinstance(job, dict) or job.get("id") != int(probe.job_id):
                continue
            matched_jobs.append(job)

        next_page = result.headers.get("X-Next-Page", "")
        if not next_page:
            break
        if not valid_id(next_page) or int(next_page) <= page or int(next_page) > 10000:
            return "invalid_response", result.status
        page = int(next_page)

    if len(matched_jobs) != 1:
        return "invalid_response", last_status
    job = matched_jobs[0]
    pipeline = job.get("pipeline")
    if not isinstance(pipeline, dict) or pipeline.get("id") != int(
        probe.child_pipeline_id
    ):
        return "invalid_response", last_status
    if job.get("status") != probe.expected_status:
        return "invalid_response", last_status
    if "artifacts_file" in job:
        archive = job["artifacts_file"]
        if not isinstance(archive, dict) or not isinstance(
            archive.get("filename"), str
        ) or not archive["filename"]:
            return "invalid_response", last_status
    return "allowed", last_status


def probe_parent(
    client: GitLabGetClient, probe: RecordProbe
) -> Tuple[str, Optional[int], str, str, str]:
    parent_id = quote(probe.parent_pipeline_id or "", safe="")
    endpoint = "trigger_jobs"
    page = 1
    matches = []
    last_status = None
    while True:
        query = urlencode({"per_page": "100", "page": str(page)})
        result = client.get(
            client.project_url(
                f"pipelines/{parent_id}/{endpoint}?{query}"
            ),
            JSON_LIMIT,
        )
        last_status = result.status
        if endpoint == "trigger_jobs" and page == 1 and result.status == 404:
            endpoint = "bridges"
            page = 1
            matches = []
            continue
        if result.status != 200:
            return (
                discovery_classification(result.status),
                result.status,
                endpoint,
                "",
                "",
            )

        trigger_jobs = parse_json_list(result)
        if trigger_jobs is None:
            return "invalid_response", result.status, endpoint, "", ""
        for trigger_job in trigger_jobs:
            if not isinstance(trigger_job, dict):
                continue
            downstream = trigger_job.get("downstream_pipeline")
            if isinstance(downstream, dict) and downstream.get("id") == int(
                probe.child_pipeline_id
            ):
                matches.append((trigger_job, downstream))

        next_page = result.headers.get("X-Next-Page", "")
        if not next_page:
            break
        if not valid_id(next_page) or int(next_page) <= page or int(next_page) > 10000:
            return "invalid_response", result.status, endpoint, "", ""
        page = int(next_page)

    if len(matches) != 1:
        return "invalid_response", last_status, endpoint, "", ""
    trigger_job, downstream = matches[0]
    name = trigger_job.get("name")
    status = downstream.get("status")
    if (
        not isinstance(name, str)
        or not name
        or not isinstance(status, str)
        or status != probe.expected_status
    ):
        return "invalid_response", last_status, endpoint, "", ""
    return "allowed", last_status, endpoint, name, status


def validate_record(body: bytes, probe: RecordProbe, token: str) -> bool:
    if token.encode("utf-8") in body:
        return False
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        return False
    if "\r" in text or not text.endswith("\n"):
        return False
    lines = text.splitlines()
    if len(lines) != len(RESULT_FIELDS):
        return False

    values = {}
    for expected_field, line in zip(RESULT_FIELDS, lines):
        if "=" not in line:
            return False
        field, value = line.split("=", 1)
        if field != expected_field or field in values or not value:
            return False
        values[field] = value
    if set(values) != set(RESULT_FIELDS):
        return False
    if LOWER_SHA_PATTERN.fullmatch(values["PMIX_COMMIT"]) is None:
        return False
    if LOWER_SHA_PATTERN.fullmatch(values["SUITE_COMMIT"]) is None:
        return False
    if values["PMIX_COMMIT"] != probe.pmix_commit:
        return False
    if values["SUITE_COMMIT"] != probe.suite_commit:
        return False
    if values["CI_JOB_STATUS"] != probe.expected_status:
        return False
    if not valid_id(values["CI_JOB_ID"]) or values["CI_JOB_ID"] != probe.job_id:
        return False
    if not valid_id(values["CI_PIPELINE_ID"]) or values[
        "CI_PIPELINE_ID"
    ] != probe.child_pipeline_id:
        return False
    return True


def probe_artifact(
    client: GitLabGetClient, probe: RecordProbe, token: str
) -> Tuple[str, Optional[int]]:
    artifact_path = quote(f"ci-results/{probe.pmix_commit}.env", safe="/")
    url = client.project_url(
        f"jobs/{quote(probe.job_id, safe='')}/artifacts/{artifact_path}"
    )
    result = client.get(url, ARTIFACT_LIMIT)
    if result.status != 200:
        return artifact_classification(result.status), result.status
    if result.body is None or not validate_record(result.body, probe, token):
        return "invalid", result.status
    atomic_write(OUTPUT_DIRECTORY / f"{probe.label.lower()}.env", result.body)
    return "valid", result.status


def status_value(status: Optional[int]) -> str:
    return "" if status is None else str(status)


def run() -> int:
    report = initial_report()
    token = os.environ.get("CI_JOB_TOKEN", "")
    try:
        prepare_output_directory()
    except OSError:
        print("error: could not prepare ci-probe-results", file=os.sys.stderr)
        return EXIT_FAILED
    try:
        remove_stale_outputs()
    except OSError:
        print("error: could not clean ci-probe-results", file=os.sys.stderr)
        return EXIT_FAILED

    api_url, project_id, loaded_token, loaded = load_configuration()
    errors, probes = loaded
    if errors or api_url is None or project_id is None or loaded_token is None:
        try:
            write_report(report, token)
        except OSError:
            print("error: could not write probe report", file=os.sys.stderr)
        print("error: invalid probe configuration", file=os.sys.stderr)
        return EXIT_FAILED

    token = loaded_token
    client = GitLabGetClient(api_url, project_id, token)
    try:
        for probe in probes:
            prefix = probe.label
            list_result, list_status = probe_job_list(client, probe)
            report[f"{prefix}_JOB_LIST_RESULT"] = list_result
            report[f"{prefix}_JOB_LIST_HTTP_STATUS"] = status_value(list_status)

            if probe.parent_pipeline_id is not None:
                parent_result, parent_status, endpoint, name, downstream_status = (
                    probe_parent(client, probe)
                )
                report[f"{prefix}_PARENT_DISCOVERY_RESULT"] = parent_result
                report[f"{prefix}_PARENT_DISCOVERY_HTTP_STATUS"] = status_value(
                    parent_status
                )
                report[f"{prefix}_PARENT_DISCOVERY_ENDPOINT"] = endpoint
                report[f"{prefix}_PARENT_TRIGGER_NAME"] = name
                report[f"{prefix}_DOWNSTREAM_STATUS"] = downstream_status

            artifact_result, artifact_status = probe_artifact(client, probe, token)
            report[f"{prefix}_ARTIFACT_DOWNLOAD_RESULT"] = artifact_result
            report[f"{prefix}_ARTIFACT_HTTP_STATUS"] = status_value(artifact_status)

        artifacts_valid = all(
            report[f"{probe.label}_ARTIFACT_DOWNLOAD_RESULT"] == "valid"
            for probe in probes
        )
        discovery_complete = all(
            report[f"{probe.label}_JOB_LIST_RESULT"] == "allowed"
            and (
                probe.parent_pipeline_id is None
                or report[f"{probe.label}_PARENT_DISCOVERY_RESULT"] == "allowed"
            )
            for probe in probes
        )
        if not artifacts_valid:
            report["OVERALL_RESULT"] = "failed"
            exit_status = EXIT_FAILED
        elif discovery_complete:
            report["OVERALL_RESULT"] = "complete"
            exit_status = EXIT_COMPLETE
        else:
            report["OVERALL_RESULT"] = "partial"
            exit_status = EXIT_PARTIAL
    except (OSError, ValueError, TypeError):
        report["OVERALL_RESULT"] = "failed"
        exit_status = EXIT_FAILED

    try:
        write_report(report, token)
    except OSError:
        print("error: could not write probe report", file=os.sys.stderr)
        return EXIT_FAILED
    print(f"PMIx child artifact probe result: {report['OVERALL_RESULT']}")
    return exit_status


if __name__ == "__main__":
    raise SystemExit(run())
