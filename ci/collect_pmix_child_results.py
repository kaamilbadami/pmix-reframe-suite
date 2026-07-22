#!/usr/bin/env python3
"""Collect ordered PMIx child-job result artifacts through the GitLab API.

Required environment variables are CI_API_V4_URL, CI_PROJECT_ID, and
CI_JOB_TOKEN.  The command writes byte-exact ``<sha>.env`` files plus a
deterministic ``collection-report.json`` in the output directory.

Exit statuses are deliberately conservative:

* 0: every requested artifact was collected (including an empty request)
* 3: at least one child pipeline, job, or artifact was not found
* 4: GitLab rejected at least one request with HTTP 401 or 403
* 5: an API response, redirect, or transport result was unsafe or malformed
* 6: local input, configuration, or publication was invalid
"""

import argparse
import json
import os
from pathlib import Path
import re
import stat
import tempfile
from typing import Dict, List, Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


EXIT_OK = 0
EXIT_INCOMPLETE = 3
EXIT_AUTH = 4
EXIT_API = 5
EXIT_LOCAL = 6
JSON_LIMIT = 10 * 1024 * 1024
ARTIFACT_LIMIT = 64 * 1024
ID_PATTERN = re.compile(r"[1-9][0-9]*")
SHA_PATTERN = re.compile(r"[0-9a-f]{40}")
TRIGGER_JOB_NAME = "trigger-pmix-child-pipeline-pilot"
REPORT_NAME = "collection-report.json"
FORBIDDEN_LOCAL_COMPONENT = ".ci" + "-state"
AUTHENTICATION_HEADERS = frozenset({
    "authorization",
    "job-token",
    "private-token",
    "proxy-authorization",
})


class LocalInputError(Exception):
    """A local path or configuration value is invalid."""


class UnsafeRedirectError(Exception):
    """A redirect cannot safely receive the authenticated request."""


class CollectorArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise LocalInputError(message)


class SafeRedirectHandler(HTTPRedirectHandler):
    """Follow safe GET redirects without leaking authentication across origins."""

    max_repeats = 4
    max_redirections = 10

    def redirect_request(self, request, file_pointer, code, message, headers, new_url):
        if request.get_method() != "GET":
            raise UnsafeRedirectError()
        try:
            old_parts = urlsplit(request.full_url)
            new_parts = urlsplit(new_url)
            old_origin = normalized_origin(old_parts)
            new_origin = normalized_origin(new_parts)
        except (ValueError, LocalInputError) as error:
            raise UnsafeRedirectError() from error
        if new_parts.username is not None or new_parts.password is not None:
            raise UnsafeRedirectError()
        if old_parts.scheme.lower() == "https" and new_parts.scheme.lower() != "https":
            raise UnsafeRedirectError()
        token = request.get_header("Job-token")
        if token and token in new_url:
            raise UnsafeRedirectError()
        same_origin = old_origin == new_origin
        redirected_headers = {
            key: value
            for key, value in request.header_items()
            if same_origin or key.lower() not in AUTHENTICATION_HEADERS
        }
        return Request(
            new_url,
            headers=redirected_headers,
            origin_req_host=request.origin_req_host,
            unverifiable=True,
            method="GET",
        )


def normalized_origin(parts) -> Tuple[str, str, int]:
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise LocalInputError("URL does not have an allowed origin")
    if parts.username is not None or parts.password is not None:
        raise LocalInputError("URL contains credentials")
    try:
        port = parts.port
    except ValueError as error:
        raise LocalInputError("URL has an invalid port") from error
    if port is None:
        port = 443 if parts.scheme == "https" else 80
    hostname = parts.hostname
    if hostname is None:
        raise LocalInputError("URL has no hostname")
    return parts.scheme.lower(), hostname.lower(), port


class GitLabGetClient:
    def __init__(self, api_url: str, project_id: str, token: str):
        self.api_url = api_url.rstrip("/")
        self.project_id = quote(project_id, safe="")
        self.token = token
        self.opener = build_opener(SafeRedirectHandler())

    def project_url(self, suffix: str) -> str:
        return f"{self.api_url}/projects/{self.project_id}/{suffix.lstrip('/')}"

    def get(self, url: str, limit: int) -> Tuple[str, Optional[int], Optional[bytes], object]:
        request = Request(
            url,
            headers={
                "Accept": "application/json, application/octet-stream;q=0.9",
                "JOB-TOKEN": self.token,
                "User-Agent": "pmix-child-result-collector/1",
            },
            method="GET",
        )
        try:
            with self.opener.open(request, timeout=30) as response:
                body = response.read(limit + 1)
                if len(body) > limit:
                    return "malformed_api_response", response.status, None, {}
                return "ok", response.status, body, response.headers
        except UnsafeRedirectError:
            return "unsafe_redirect", None, None, {}
        except HTTPError as error:
            status = error.code
            error.close()
            if status in (401, 403):
                return "api_authentication_failure", status, None, {}
            return "http_error", status, None, {}
        except (URLError, OSError, ValueError):
            return "malformed_api_response", None, None, {}


def valid_id(value: str) -> bool:
    return ID_PATTERN.fullmatch(value) is not None


def valid_json_id(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def parse_arguments(arguments: Optional[List[str]] = None) -> argparse.Namespace:
    parser = CollectorArgumentParser(description=__doc__)
    parser.add_argument("--commits", required=True, type=Path)
    parser.add_argument("--parent-pipeline-id", required=True)
    parser.add_argument("--output", required=True, type=Path)
    return parser.parse_args(arguments)


def validate_api_url(value: str, token: str) -> str:
    if not value or (token and token in value):
        raise LocalInputError("invalid API URL")
    try:
        parts = urlsplit(value)
        normalized_origin(parts)
    except ValueError as error:
        raise LocalInputError("invalid API URL") from error
    if parts.query or parts.fragment:
        raise LocalInputError("invalid API URL")
    return value.rstrip("/")


def validate_local_path(path: Path, label: str) -> Path:
    absolute = Path(os.path.abspath(os.fspath(path)))
    if FORBIDDEN_LOCAL_COMPONENT in absolute.parts:
        raise LocalInputError(f"{label} uses a forbidden local path")
    current = Path(absolute.anchor)
    for component in absolute.parts[1:]:
        current /= component
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            continue
        except OSError as error:
            raise LocalInputError(f"{label} path is unavailable") from error
        if stat.S_ISLNK(metadata.st_mode):
            raise LocalInputError(f"{label} path contains a symbolic link")
    return absolute


def read_regular_file(path: Path) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise LocalInputError("commit list is not a readable regular file") from error
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise LocalInputError("commit list is not a regular file")
        with os.fdopen(descriptor, "rb") as input_file:
            descriptor = -1
            return input_file.read()
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def parse_commits(path: Path) -> List[str]:
    content = read_regular_file(path)
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as error:
        raise LocalInputError("commit list is not valid UTF-8") from error
    if "\r" in text:
        raise LocalInputError("commit list must use Unix line endings")
    if not text:
        return []
    lines = text.split("\n")
    if lines[-1] == "":
        lines.pop()
    if not lines or any(not line for line in lines):
        raise LocalInputError("commit list contains a blank line")
    if any(SHA_PATTERN.fullmatch(line) is None for line in lines):
        raise LocalInputError("commit list contains a malformed SHA")
    if len(lines) != len(set(lines)):
        raise LocalInputError("commit list contains a duplicate SHA")
    return lines


def prepare_output_directory(path: Path) -> Path:
    output = validate_local_path(path, "output")
    if output == Path(output.anchor):
        raise LocalInputError("filesystem root is not an output directory")
    try:
        output.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise LocalInputError("cannot create output directory") from error
    try:
        metadata = output.lstat()
    except OSError as error:
        raise LocalInputError("output directory is unavailable") from error
    if not stat.S_ISDIR(metadata.st_mode):
        raise LocalInputError("output path is not a directory")

    return output


def remove_stale_outputs(output: Path, commits: List[str], commits_path: Path) -> None:
    """Validate all known outputs, then remove only those stale regular files."""

    known = [output / REPORT_NAME] + [output / f"{commit}.env" for commit in commits]
    if commits_path in known:
        raise LocalInputError("commit list collides with a known output")
    for candidate in known:
        try:
            candidate_metadata = candidate.lstat()
        except FileNotFoundError:
            continue
        except OSError as error:
            raise LocalInputError("known output is unavailable") from error
        if stat.S_ISLNK(candidate_metadata.st_mode):
            raise LocalInputError("known output may not be a symbolic link")
        if stat.S_ISDIR(candidate_metadata.st_mode):
            raise LocalInputError("known output may not be a directory")
        if not stat.S_ISREG(candidate_metadata.st_mode):
            raise LocalInputError("known output is not a regular file")
    try:
        for candidate in known:
            try:
                os.unlink(candidate)
            except FileNotFoundError:
                pass
    except OSError as error:
        raise LocalInputError("cannot remove stale known output") from error


def parse_json_list(body: Optional[bytes]) -> Optional[list]:
    if body is None:
        return None
    try:
        value = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, list) else None


def next_page(headers: object, current: int) -> Optional[int]:
    try:
        value = headers.get("X-Next-Page", "")
    except AttributeError as error:
        raise ValueError("invalid pagination headers") from error
    if not value:
        return None
    if not isinstance(value, str) or not valid_id(value):
        raise ValueError("invalid next page")
    page = int(value)
    if page <= current or page > 10000:
        raise ValueError("invalid next page")
    return page


def discover_child(client: GitLabGetClient, parent_id: str) -> Tuple[str, str, str]:
    for endpoint in ("trigger_jobs", "bridges"):
        page = 1
        matches = []
        while True:
            query = urlencode({"per_page": "100", "page": str(page)})
            suffix = f"pipelines/{quote(parent_id, safe='')}/{endpoint}?{query}"
            kind, status, body, headers = client.get(client.project_url(suffix), JSON_LIMIT)
            if endpoint == "trigger_jobs" and page == 1 and status == 404:
                break
            if kind == "api_authentication_failure":
                return kind, endpoint, ""
            if kind != "ok" or status != 200:
                return "malformed_api_response", endpoint, ""
            records = parse_json_list(body)
            if records is None:
                return "malformed_api_response", endpoint, ""
            for record in records:
                if not isinstance(record, dict) or not isinstance(record.get("name"), str):
                    return "malformed_api_response", endpoint, ""
                if record["name"] != TRIGGER_JOB_NAME:
                    continue
                downstream = record.get("downstream_pipeline")
                if not isinstance(downstream, dict) or not valid_json_id(downstream.get("id")):
                    return "malformed_api_response", endpoint, ""
                matches.append(str(downstream["id"]))
            try:
                following = next_page(headers, page)
            except ValueError:
                return "malformed_api_response", endpoint, ""
            if following is None:
                if len(matches) == 1:
                    return "ok", endpoint, matches[0]
                if not matches:
                    return "child_pipeline_not_found", endpoint, ""
                return "malformed_api_response", endpoint, ""
            page = following
        # Only the first-page 404 above activates this compatibility endpoint.
    return "child_pipeline_not_found", "bridges", ""


def list_jobs(client: GitLabGetClient, child_id: str) -> Tuple[str, List[dict]]:
    page = 1
    jobs = []
    seen_ids = set()
    while True:
        query = urlencode({
            "per_page": "100",
            "page": str(page),
            "include_retried": "true",
        })
        suffix = f"pipelines/{quote(child_id, safe='')}/jobs?{query}"
        kind, status, body, headers = client.get(client.project_url(suffix), JSON_LIMIT)
        if kind == "api_authentication_failure":
            return kind, []
        if kind != "ok" or status != 200:
            return "malformed_api_response", []
        records = parse_json_list(body)
        if records is None:
            return "malformed_api_response", []
        for job in records:
            if not isinstance(job, dict):
                return "malformed_api_response", []
            job_id = job.get("id")
            name = job.get("name")
            if not valid_json_id(job_id) or not isinstance(name, str):
                return "malformed_api_response", []
            if job_id in seen_ids:
                return "malformed_api_response", []
            seen_ids.add(job_id)
            if "retried" in job and not isinstance(job["retried"], bool):
                return "malformed_api_response", []
            pipeline = job.get("pipeline")
            if not isinstance(pipeline, dict) or pipeline.get("id") != int(child_id):
                return "malformed_api_response", []
            jobs.append(job)
        try:
            following = next_page(headers, page)
        except ValueError:
            return "malformed_api_response", []
        if following is None:
            return "ok", jobs
        page = following


def select_job(jobs: List[dict], commit: str) -> Tuple[str, Optional[int]]:
    expected_name = f"pmix-{commit}"
    matches = [job for job in jobs if job["name"] == expected_name]
    if not matches:
        return "job_not_found", None
    current = [job for job in matches if job.get("retried") is not True]
    if len(current) != 1:
        return "malformed_api_response", None
    return "ok", current[0]["id"]


def download_artifact(
    client: GitLabGetClient, job_id: int, commit: str
) -> Tuple[str, Optional[int], Optional[bytes]]:
    artifact_path = quote(f"ci-results/{commit}.env", safe="/")
    suffix = f"jobs/{quote(str(job_id), safe='')}/artifacts/{artifact_path}"
    kind, status, body, _ = client.get(client.project_url(suffix), ARTIFACT_LIMIT)
    if kind == "api_authentication_failure":
        return kind, status, None
    if kind == "unsafe_redirect":
        return kind, status, None
    if status == 404:
        return "missing_artifact", status, None
    if kind != "ok" or status != 200 or body is None:
        return "malformed_api_response", status, None
    if client.token.encode("utf-8") in body:
        return "malformed_api_response", status, None
    return "collected", status, body


def atomic_write(path: Path, content: bytes) -> None:
    temporary_name = None
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


def initial_report() -> Dict[str, object]:
    return {
        "child_pipeline_id": "",
        "items": [],
        "overall_result": "invalid_local_input",
        "parent_discovery_endpoint": "",
        "schema_version": 1,
    }


def report_bytes(report: Dict[str, object], token: str) -> bytes:
    content = (json.dumps(report, sort_keys=True, separators=(",", ":")) + "\n").encode()
    if token and token.encode("utf-8") in content:
        raise OSError("sensitive report content")
    return content


def write_report(output: Path, report: Dict[str, object], token: str) -> None:
    atomic_write(output / REPORT_NAME, report_bytes(report, token))


def result_exit(items: List[dict]) -> Tuple[str, int]:
    results = {item["result"] for item in items}
    if "invalid_local_input" in results:
        return "invalid_local_input", EXIT_LOCAL
    if "api_authentication_failure" in results:
        return "api_authentication_failure", EXIT_AUTH
    if results.intersection({"malformed_api_response", "unsafe_redirect"}):
        return "malformed_api_response", EXIT_API
    if results.intersection({"job_not_found", "missing_artifact", "child_pipeline_not_found"}):
        return "incomplete", EXIT_INCOMPLETE
    return "collected", EXIT_OK


def collect(arguments: Optional[List[str]] = None) -> int:
    report = initial_report()
    output = None
    token = os.environ.get("CI_JOB_TOKEN", "")
    try:
        options = parse_arguments(arguments)
        commits_path = validate_local_path(options.commits, "commit list")
        output = prepare_output_directory(options.output)
        commits = parse_commits(commits_path)
        remove_stale_outputs(output, commits, commits_path)

        api_url = validate_api_url(os.environ.get("CI_API_V4_URL", ""), token)
        project_id = os.environ.get("CI_PROJECT_ID", "")
        if not valid_id(project_id) or not token:
            raise LocalInputError("invalid GitLab configuration")
        if not valid_id(options.parent_pipeline_id):
            raise LocalInputError("invalid parent pipeline ID")

        report["items"] = [
            {"commit": commit, "http_status": None, "job_id": None, "result": "not_attempted"}
            for commit in commits
        ]
        if not commits:
            report["overall_result"] = "collected"
            write_report(output, report, token)
            return EXIT_OK

        client = GitLabGetClient(api_url, project_id, token)
        discovery, endpoint, child_id = discover_child(
            client, options.parent_pipeline_id
        )
        report["parent_discovery_endpoint"] = endpoint
        if discovery != "ok":
            for item in report["items"]:
                item["result"] = discovery
            overall, exit_status = result_exit(report["items"])
            report["overall_result"] = overall
            write_report(output, report, token)
            return exit_status

        report["child_pipeline_id"] = child_id
        listing, jobs = list_jobs(client, child_id)
        if listing != "ok":
            for item in report["items"]:
                item["result"] = listing
            overall, exit_status = result_exit(report["items"])
            report["overall_result"] = overall
            write_report(output, report, token)
            return exit_status

        for item in report["items"]:
            selection, job_id = select_job(jobs, item["commit"])
            item["result"] = selection
            if selection != "ok" or job_id is None:
                continue
            item["job_id"] = job_id
            result, status, content = download_artifact(client, job_id, item["commit"])
            item["http_status"] = status
            item["result"] = result
            if result == "collected" and content is not None:
                try:
                    atomic_write(output / f"{item['commit']}.env", content)
                except OSError:
                    item["result"] = "invalid_local_input"

        overall, exit_status = result_exit(report["items"])
        report["overall_result"] = overall
        write_report(output, report, token)
        return exit_status
    except LocalInputError:
        if output is not None:
            try:
                write_report(output, report, token)
            except OSError:
                pass
        print("error: invalid collector input", file=os.sys.stderr)
        return EXIT_LOCAL
    except OSError:
        print("error: collector output failed", file=os.sys.stderr)
        return EXIT_LOCAL


if __name__ == "__main__":
    raise SystemExit(collect())
