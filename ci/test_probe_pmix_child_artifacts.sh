#!/bin/bash
set -euo pipefail

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
repo_root=$(cd -- "$script_dir/.." && pwd)
probe="$script_dir/probe_pmix_child_artifacts.py"
parent_ci="$repo_root/.gitlab-ci.yml"

python3 - "$probe" "$parent_ci" <<'PY'
import ast
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import threading
from urllib.parse import parse_qs, urlsplit


probe_path = Path(sys.argv[1]).resolve()
parent_ci_path = Path(sys.argv[2]).resolve()
success_pipeline = "137826"
success_job = "209739"
success_pmix = "f79787b07ced1c04d183685b5ec73e06cfc4a0e4"
success_suite = "9492c66eda274889531a469789ceb07292bd15fc"
failed_pipeline = "137841"
failed_job = "209755"
failed_pmix = "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef"
failed_suite = "561816352144639463822cf489460254e58eae00"
success_parent = "137825"
failed_parent = "137840"
project_id = "77"
token = "job-token-secret-that-must-never-be-disclosed"
report_fields = [
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
]
pass_count = 0


def passed(message):
    global pass_count
    pass_count += 1
    print(f"ok - {message}")


def check(condition, message):
    if not condition:
        raise AssertionError(message)


def record(label, overrides=None):
    values = {
        "PMIX_COMMIT": success_pmix if label == "success" else failed_pmix,
        "SUITE_COMMIT": success_suite if label == "success" else failed_suite,
        "CI_JOB_STATUS": label,
        "CI_JOB_ID": success_job if label == "success" else failed_job,
        "CI_PIPELINE_ID": (
            success_pipeline if label == "success" else failed_pipeline
        ),
    }
    if overrides:
        values.update(overrides)
    return "".join(f"{key}={values[key]}\n" for key in (
        "PMIX_COMMIT",
        "SUITE_COMMIT",
        "CI_JOB_STATUS",
        "CI_JOB_ID",
        "CI_PIPELINE_ID",
    )).encode()


class Router:
    def __init__(self):
        self.mode = "complete"
        self.requests = []
        self.artifact_status = None
        self.bad_failed_record = None
        self.redirect_base = ""

    def reset(self, mode):
        self.mode = mode
        self.requests = []
        self.artifact_status = None
        self.bad_failed_record = None


router = Router()


class ApiHandler(BaseHTTPRequestHandler):
    def log_message(self, format_string, *args):
        return

    def send(self, status, body=b"", headers=None):
        self.send_response(status)
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        router.requests.append((self.path, self.headers.get("JOB-TOKEN"), "GET"))
        parsed = urlsplit(self.path)
        path = parsed.path
        prefix = f"/api/v4/projects/{project_id}/"
        if not path.startswith(prefix):
            self.send(404)
            return
        suffix = path[len(prefix):]

        jobs_match = re.fullmatch(r"pipelines/([1-9][0-9]*)/jobs", suffix)
        if jobs_match:
            query = parse_qs(parsed.query)
            page = query.get("page", [""])[0]
            per_page = query.get("per_page", [""])[0]
            include_retried = query.get("include_retried", [""])[0]
            if per_page != "100" or not page or include_retried != "true":
                self.send(400)
                return
            if router.mode == "partial_403":
                self.send(403, token.encode())
                return
            if router.mode == "listing_401":
                self.send(401, token.encode())
                return
            if router.mode == "listing_404":
                self.send(404)
                return
            if router.mode == "malformed_json" and jobs_match.group(1) == success_pipeline:
                self.send(200, b"{not-json")
                return
            pipeline_id = jobs_match.group(1)
            label = "success" if pipeline_id == success_pipeline else "failed"
            expected_job = int(success_job if label == "success" else failed_job)
            expected_pipeline = int(
                success_pipeline if label == "success" else failed_pipeline
            )
            job = {
                "id": expected_job,
                "status": label,
                "pipeline": {"id": expected_pipeline},
                "artifacts_file": {"filename": "artifacts.zip", "size": 123},
            }
            if router.mode == "retried_job":
                job["name"] = "pmix-retried-job"
                job["retried"] = True
                newer_retry = {
                    "id": expected_job + 1000,
                    "name": "pmix-retried-job",
                    "status": label,
                    "pipeline": {"id": expected_pipeline},
                    "artifacts_file": {
                        "filename": "artifacts.zip",
                        "size": 123,
                    },
                }
                self.send(200, json.dumps([newer_retry, job]).encode())
                return
            if page == "1":
                self.send(
                    200,
                    json.dumps([{"id": 1}]).encode(),
                    {"Content-Type": "application/json", "X-Next-Page": "2"},
                )
            elif page == "2":
                self.send(200, json.dumps([job]).encode(), {"X-Next-Page": ""})
            else:
                self.send(400)
            return

        parent_match = re.fullmatch(
            r"pipelines/([1-9][0-9]*)/(trigger_jobs|bridges)", suffix
        )
        if parent_match:
            parent_id, endpoint = parent_match.groups()
            query = parse_qs(parsed.query)
            page = query.get("page", [""])[0]
            per_page = query.get("per_page", [""])[0]
            if per_page != "100" or not page:
                self.send(400)
                return
            if router.mode == "parent_denied":
                self.send(403)
                return
            if router.mode == "compatibility" and endpoint == "trigger_jobs":
                self.send(404)
                return
            if router.mode == "parent_unsupported":
                self.send(404)
                return
            child_id = success_pipeline if parent_id == success_parent else failed_pipeline
            status = "success" if child_id == success_pipeline else "failed"
            if router.mode == "wrong_success_parent_status" and (
                child_id == success_pipeline
            ):
                status = "failed"
            if router.mode == "wrong_failed_parent_status" and (
                child_id == failed_pipeline
            ):
                status = "success"
            match = {
                "name": f"trigger-{status}",
                "downstream_pipeline": {"id": int(child_id), "status": status},
            }
            unrelated = {
                "name": "unrelated-trigger",
                "downstream_pipeline": {"id": 999999, "status": "success"},
            }
            if router.mode == "parent_trigger_page2":
                body = [unrelated] if page == "1" else [match]
                headers = {"X-Next-Page": "2" if page == "1" else ""}
                self.send(200, json.dumps(body).encode(), headers)
                return
            if router.mode == "parent_bridge_page2":
                if endpoint == "trigger_jobs":
                    self.send(404)
                    return
                body = [unrelated] if page == "1" else [match]
                headers = {"X-Next-Page": "2" if page == "1" else ""}
                self.send(200, json.dumps(body).encode(), headers)
                return
            if router.mode == "parent_invalid_pagination":
                self.send(
                    200,
                    json.dumps([unrelated]).encode(),
                    {"X-Next-Page": "01"},
                )
                return
            if router.mode == "parent_duplicate_matches":
                self.send(
                    200,
                    json.dumps([match]).encode(),
                    {"X-Next-Page": "2" if page == "1" else ""},
                )
                return
            if router.mode == "parent_later_404":
                if page == "1":
                    self.send(
                        200,
                        json.dumps([unrelated]).encode(),
                        {"X-Next-Page": "2"},
                    )
                else:
                    self.send(404)
                return
            self.send(200, json.dumps([match]).encode())
            return

        artifact_match = re.fullmatch(
            r"jobs/([1-9][0-9]*)/artifacts/ci-results/([0-9a-f]{40})\.env",
            suffix,
        )
        if artifact_match:
            job_id, pmix_sha = artifact_match.groups()
            label = "success" if job_id == success_job else "failed"
            expected_sha = success_pmix if label == "success" else failed_pmix
            if pmix_sha != expected_sha:
                self.send(404)
                return
            if router.mode == "stale_outputs":
                self.send(404 if label == "success" else 403)
                return
            if router.artifact_status and label == "failed":
                self.send(router.artifact_status, token.encode())
                return
            if router.mode == "malicious_body" and label == "failed":
                self.send(200, token.encode())
                return
            if router.mode == "redirect" and label == "failed":
                self.send(302, headers={
                    "Location": f"{router.redirect_base}/download/{label}"
                })
                return
            body = router.bad_failed_record if (
                label == "failed" and router.bad_failed_record is not None
            ) else record(label)
            self.send(200, body)
            return

        self.send(404)


redirect_requests = []


class RedirectHandler(BaseHTTPRequestHandler):
    def log_message(self, format_string, *args):
        return

    def do_GET(self):
        redirect_requests.append((self.path, self.headers.get("JOB-TOKEN")))
        body = record("failed") if self.path == "/download/failed" else b""
        status = 200 if body else 404
        self.send_response(status)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


api_server = HTTPServer(("127.0.0.1", 0), ApiHandler)
redirect_server = HTTPServer(("127.0.0.1", 0), RedirectHandler)
api_thread = threading.Thread(target=api_server.serve_forever, daemon=True)
redirect_thread = threading.Thread(target=redirect_server.serve_forever, daemon=True)
api_thread.start()
redirect_thread.start()
api_base = f"http://127.0.0.1:{api_server.server_port}/api/v4"
router.redirect_base = f"http://127.0.0.1:{redirect_server.server_port}"


def base_environment(with_parents=False):
    environment = os.environ.copy()
    for key in list(environment):
        if key.startswith("PMIX_PROBE_"):
            environment.pop(key)
    environment.update({
        "CI_API_V4_URL": api_base,
        "CI_PROJECT_ID": project_id,
        "CI_JOB_TOKEN": token,
        "PMIX_PROBE_SUCCESS_CHILD_PIPELINE_ID": success_pipeline,
        "PMIX_PROBE_SUCCESS_JOB_ID": success_job,
        "PMIX_PROBE_SUCCESS_COMMIT": success_pmix,
        "PMIX_PROBE_SUCCESS_SUITE_COMMIT": success_suite,
        "PMIX_PROBE_FAILED_CHILD_PIPELINE_ID": failed_pipeline,
        "PMIX_PROBE_FAILED_JOB_ID": failed_job,
        "PMIX_PROBE_FAILED_COMMIT": failed_pmix,
        "PMIX_PROBE_FAILED_SUITE_COMMIT": failed_suite,
    })
    if with_parents:
        environment.update({
            "PMIX_PROBE_SUCCESS_PARENT_PIPELINE_ID": success_parent,
            "PMIX_PROBE_FAILED_PARENT_PIPELINE_ID": failed_parent,
        })
    return environment


def parse_report(path):
    lines = path.read_text(encoding="utf-8").splitlines()
    check(len(lines) == len(report_fields), "report line count changed")
    fields = []
    values = {}
    for line in lines:
        check("=" in line, "report line lacks equals sign")
        field, value = line.split("=", 1)
        fields.append(field)
        check(field not in values, "report has a duplicate field")
        values[field] = value
    check(fields == report_fields, "report schema or order changed")
    return values


def run_probe(mode, with_parents=False, environment_update=None, prepopulate=None):
    router.reset(mode)
    environment = base_environment(with_parents)
    if environment_update:
        for key, value in environment_update.items():
            if value is None:
                environment.pop(key, None)
            else:
                environment[key] = value
    temporary = tempfile.TemporaryDirectory()
    workdir = Path(temporary.name)
    if prepopulate:
        output_directory = workdir / "ci-probe-results"
        output_directory.mkdir()
        for name, content in prepopulate.items():
            (output_directory / name).write_bytes(content)
    completed = subprocess.run(
        [sys.executable, str(probe_path)],
        cwd=workdir,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    report_path = workdir / "ci-probe-results" / "probe.env"
    report = parse_report(report_path) if report_path.is_file() else None
    return temporary, workdir, completed, report


def assert_token_safe(temporary, workdir, completed):
    check(token.encode() not in completed.stdout, "token leaked to stdout")
    check(token.encode() not in completed.stderr, "token leaked to stderr")
    for output in workdir.rglob("*"):
        if output.is_file():
            check(token.encode() not in output.read_bytes(), f"token leaked to {output.name}")
    for path, _, method in router.requests:
        check(token not in path, "token leaked into a request URL")
        check(method == "GET", "probe issued a non-GET request")
    for _, request_token, _ in router.requests:
        check(request_token == token, "main-origin request lacked JOB-TOKEN authentication")
    temporary.cleanup()


try:
    temporary, workdir, completed, report = run_probe("complete", with_parents=True)
    check(completed.returncode == 0, "complete probe did not exit 0")
    check(report["OVERALL_RESULT"] == "complete", "complete result not reported")
    for label in ("SUCCESS", "FAILED"):
        check(report[f"{label}_JOB_LIST_RESULT"] == "allowed", "job list failed")
        check(report[f"{label}_JOB_LIST_HTTP_STATUS"] == "200", "list status lost")
        check(
            report[f"{label}_PARENT_DISCOVERY_RESULT"] == "allowed",
            "parent discovery failed",
        )
        check(
            report[f"{label}_PARENT_DISCOVERY_ENDPOINT"] == "trigger_jobs",
            "modern parent endpoint was not recorded",
        )
        check(report[f"{label}_PARENT_TRIGGER_NAME"] == f"trigger-{label.lower()}",
              "parent trigger name was not recorded")
        check(report[f"{label}_DOWNSTREAM_STATUS"] == label.lower(),
              "downstream status was not recorded")
        check(
            report[f"{label}_ARTIFACT_DOWNLOAD_RESULT"] == "valid",
            "artifact was not valid",
        )
    check((workdir / "ci-probe-results/success.env").read_bytes() == record("success"),
          "success artifact was not saved exactly")
    check((workdir / "ci-probe-results/failed.env").read_bytes() == record("failed"),
          "failed artifact was not saved exactly")
    check(any("page=2" in request[0] for request in router.requests),
          "job-list pagination was not followed")
    assert_token_safe(temporary, workdir, completed)
    passed("complete success validates lists, parents, pagination, and both artifacts")

    temporary, workdir, completed, report = run_probe("retried_job")
    check(completed.returncode == 0, "retried-job lookup was incomplete")
    for label in ("SUCCESS", "FAILED"):
        check(report[f"{label}_JOB_LIST_RESULT"] == "allowed",
              "exact older retried job was not found")
    job_requests = [
        path for path, _, _ in router.requests
        if urlsplit(path).path.endswith("/jobs")
    ]
    check(job_requests, "retried-job test made no listing requests")
    check(all(parse_qs(urlsplit(path).query).get("include_retried") == ["true"]
              for path in job_requests),
          "job listing omitted include_retried=true")
    assert_token_safe(temporary, workdir, completed)
    passed("exact older retried jobs are included and found by job ID")

    for mode, invalid_label, allowed_label in (
        ("wrong_success_parent_status", "SUCCESS", "FAILED"),
        ("wrong_failed_parent_status", "FAILED", "SUCCESS"),
    ):
        temporary, workdir, completed, report = run_probe(
            mode, with_parents=True
        )
        check(completed.returncode == 3, f"{mode} did not yield partial")
        check(report["OVERALL_RESULT"] == "partial",
              f"{mode} did not report partial")
        check(report[f"{invalid_label}_PARENT_DISCOVERY_RESULT"] ==
              "invalid_response", f"{mode} was not invalid_response")
        check(report[f"{allowed_label}_PARENT_DISCOVERY_RESULT"] == "allowed",
              f"{mode} invalidated the correct parent status")
        for label in ("SUCCESS", "FAILED"):
            check(report[f"{label}_ARTIFACT_DOWNLOAD_RESULT"] == "valid",
                  f"{mode} prevented artifact validation")
        assert_token_safe(temporary, workdir, completed)
    passed("wrong success and failed downstream statuses are rejected independently")

    temporary, workdir, completed, report = run_probe("partial_403")
    check(completed.returncode == 3, "partial probe did not exit 3")
    check(report["OVERALL_RESULT"] == "partial", "partial result not reported")
    for label in ("SUCCESS", "FAILED"):
        check(report[f"{label}_JOB_LIST_RESULT"] == "permission_denied",
              "403 list was not permission_denied")
        check(report[f"{label}_JOB_LIST_HTTP_STATUS"] == "403", "403 status lost")
        check(report[f"{label}_ARTIFACT_DOWNLOAD_RESULT"] == "valid",
              "artifact did not continue after list denial")
        check(report[f"{label}_PARENT_DISCOVERY_RESULT"] == "not_requested",
              "absent parent was not not_requested")
    assert_token_safe(temporary, workdir, completed)
    passed("403 listings yield partial while both direct artifacts still validate")

    temporary, workdir, completed, report = run_probe(
        "compatibility", with_parents=True
    )
    check(completed.returncode == 0, "bridge fallback probe was incomplete")
    for label in ("SUCCESS", "FAILED"):
        check(report[f"{label}_PARENT_DISCOVERY_ENDPOINT"] == "bridges",
              "bridge endpoint was not recorded")
        check(report[f"{label}_PARENT_DISCOVERY_RESULT"] == "allowed",
              "bridge fallback did not find child")
    check(any(urlsplit(path).path.endswith("/trigger_jobs")
              for path, _, _ in router.requests),
          "modern trigger endpoint was not tried first")
    check(any(urlsplit(path).path.endswith("/bridges")
              for path, _, _ in router.requests),
          "compatible bridge endpoint was not tried")
    assert_token_safe(temporary, workdir, completed)
    passed("404 from trigger_jobs falls back to the compatible bridges endpoint")

    temporary, workdir, completed, report = run_probe(
        "parent_trigger_page2", with_parents=True
    )
    check(completed.returncode == 0, "paginated trigger_jobs probe was incomplete")
    for label in ("SUCCESS", "FAILED"):
        check(report[f"{label}_PARENT_DISCOVERY_RESULT"] == "allowed",
              "page-2 trigger_jobs child was not found")
        check(report[f"{label}_PARENT_DISCOVERY_ENDPOINT"] == "trigger_jobs",
              "paginated trigger endpoint was not preserved")
        check(report[f"{label}_PARENT_DISCOVERY_HTTP_STATUS"] == "200",
              "paginated trigger status was not preserved")
    trigger_requests = [
        path for path, _, _ in router.requests
        if urlsplit(path).path.endswith("/trigger_jobs")
    ]
    check(any("per_page=100" in path and "page=1" in path
              for path in trigger_requests), "trigger_jobs did not begin at page 1")
    check(any("per_page=100" in path and "page=2" in path
              for path in trigger_requests), "trigger_jobs page 2 was not requested")
    check(not any(urlsplit(path).path.endswith("/bridges")
                  for path, _, _ in router.requests),
          "successful paginated trigger_jobs unexpectedly fell back")
    assert_token_safe(temporary, workdir, completed)
    passed("trigger_jobs pagination locates the expected child on page 2")

    temporary, workdir, completed, report = run_probe(
        "parent_bridge_page2", with_parents=True
    )
    check(completed.returncode == 0, "paginated bridges fallback was incomplete")
    for label in ("SUCCESS", "FAILED"):
        check(report[f"{label}_PARENT_DISCOVERY_RESULT"] == "allowed",
              "page-2 bridges child was not found")
        check(report[f"{label}_PARENT_DISCOVERY_ENDPOINT"] == "bridges",
              "paginated bridge endpoint was not preserved")
        check(report[f"{label}_PARENT_DISCOVERY_HTTP_STATUS"] == "200",
              "paginated bridge status was not preserved")
    bridge_requests = [
        path for path, _, _ in router.requests
        if urlsplit(path).path.endswith("/bridges")
    ]
    check(any("per_page=100" in path and "page=1" in path
              for path in bridge_requests), "bridges did not begin at page 1")
    check(any("per_page=100" in path and "page=2" in path
              for path in bridge_requests), "bridges page 2 was not requested")
    assert_token_safe(temporary, workdir, completed)
    passed("first-page trigger 404 falls back and locates a bridge on page 2")

    for mode, description in (
        ("parent_invalid_pagination", "invalid parent pagination"),
        ("parent_duplicate_matches", "duplicate parent matches"),
    ):
        temporary, workdir, completed, report = run_probe(
            mode, with_parents=True
        )
        check(completed.returncode == 3, f"{description} did not yield partial")
        for label in ("SUCCESS", "FAILED"):
            check(report[f"{label}_PARENT_DISCOVERY_RESULT"] == "invalid_response",
                  f"{description} was not invalid_response")
            check(report[f"{label}_PARENT_DISCOVERY_HTTP_STATUS"] == "200",
                  f"{description} did not preserve the last HTTP status")
            check(report[f"{label}_PARENT_DISCOVERY_ENDPOINT"] == "trigger_jobs",
                  f"{description} did not preserve the selected endpoint")
            check(report[f"{label}_ARTIFACT_DOWNLOAD_RESULT"] == "valid",
                  f"{description} prevented artifact validation")
        assert_token_safe(temporary, workdir, completed)
    passed("invalid parent pagination and duplicate cross-page matches are rejected")

    temporary, workdir, completed, report = run_probe(
        "parent_later_404", with_parents=True
    )
    check(completed.returncode == 3, "later trigger_jobs 404 did not yield partial")
    for label in ("SUCCESS", "FAILED"):
        check(report[f"{label}_PARENT_DISCOVERY_ENDPOINT"] == "trigger_jobs",
              "later trigger_jobs 404 changed the selected endpoint")
        check(report[f"{label}_PARENT_DISCOVERY_HTTP_STATUS"] == "404",
              "later trigger_jobs 404 status was not preserved")
    check(not any(urlsplit(path).path.endswith("/bridges")
                  for path, _, _ in router.requests),
          "later trigger_jobs 404 incorrectly fell back to bridges")
    assert_token_safe(temporary, workdir, completed)
    passed("only a first-page trigger_jobs 404 activates the bridges fallback")

    for mode, expected_status, expected_result in (
        ("parent_denied", "403", "permission_denied"),
        ("parent_unsupported", "404", "unsupported"),
    ):
        temporary, workdir, completed, report = run_probe(
            mode, with_parents=True
        )
        check(completed.returncode == 3, f"{mode} did not yield partial")
        for label in ("SUCCESS", "FAILED"):
            check(report[f"{label}_PARENT_DISCOVERY_HTTP_STATUS"] == expected_status,
                  f"{mode} status was not preserved")
            check(report[f"{label}_PARENT_DISCOVERY_RESULT"] == expected_result,
                  f"{mode} was misclassified")
            check(report[f"{label}_ARTIFACT_DOWNLOAD_RESULT"] == "valid",
                  f"{mode} prevented artifact validation")
        assert_token_safe(temporary, workdir, completed)
    passed("parent discovery denial and unsupported routes remain distinct and nonfatal")

    temporary, workdir, completed, report = run_probe("complete")
    failed_artifact_path = (
        f"/jobs/{failed_job}/artifacts/ci-results/{failed_pmix}.env"
    )
    check(any(failed_artifact_path in path for path, _, _ in router.requests),
          "failed artifact was not requested by exact job and file")
    check(report["FAILED_ARTIFACT_DOWNLOAD_RESULT"] == "valid",
          "failed artifact did not validate")
    assert_token_safe(temporary, workdir, completed)
    passed("failed-job artifact is downloaded by exact job ID and validates")

    malformed_records = {
        "missing field": record("failed").splitlines(keepends=True)[:-1],
        "duplicate field": [
            f"PMIX_COMMIT={failed_pmix}\n".encode(),
            f"PMIX_COMMIT={failed_pmix}\n".encode(),
            b"CI_JOB_STATUS=failed\n",
            f"CI_JOB_ID={failed_job}\n".encode(),
            f"CI_PIPELINE_ID={failed_pipeline}\n".encode(),
        ],
        "wrong order": [
            f"SUITE_COMMIT={failed_suite}\n".encode(),
            f"PMIX_COMMIT={failed_pmix}\n".encode(),
            b"CI_JOB_STATUS=failed\n",
            f"CI_JOB_ID={failed_job}\n".encode(),
            f"CI_PIPELINE_ID={failed_pipeline}\n".encode(),
        ],
        "wrong SHA": [record("failed", {"PMIX_COMMIT": "a" * 40})],
        "wrong status": [record("failed", {"CI_JOB_STATUS": "success"})],
        "wrong job ID": [record("failed", {"CI_JOB_ID": "999"})],
        "wrong pipeline ID": [record("failed", {"CI_PIPELINE_ID": "998"})],
    }
    for label, chunks in malformed_records.items():
        router.reset("malformed_artifact")
        router.bad_failed_record = b"".join(chunks)
        environment = base_environment()
        temporary = tempfile.TemporaryDirectory()
        workdir = Path(temporary.name)
        completed = subprocess.run(
            [sys.executable, str(probe_path)], cwd=workdir, env=environment,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        )
        report = parse_report(workdir / "ci-probe-results/probe.env")
        check(completed.returncode == 4, f"{label} did not exit 4")
        check(report["FAILED_ARTIFACT_DOWNLOAD_RESULT"] == "invalid",
              f"{label} was not invalid")
        check(report["OVERALL_RESULT"] == "failed", f"{label} was not failed")
        check(not (workdir / "ci-probe-results/failed.env").exists(),
              f"{label} was saved")
        assert_token_safe(temporary, workdir, completed)
    passed("all seven malformed record variants are rejected without being saved")

    for status, expected in ((401, "permission_denied"), (403, "permission_denied"),
                             (404, "not_found")):
        router.reset("artifact_http")
        router.artifact_status = status
        environment = base_environment()
        temporary = tempfile.TemporaryDirectory()
        workdir = Path(temporary.name)
        completed = subprocess.run(
            [sys.executable, str(probe_path)], cwd=workdir, env=environment,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        )
        report = parse_report(workdir / "ci-probe-results/probe.env")
        check(completed.returncode == 4, f"artifact HTTP {status} did not fail")
        check(report["FAILED_ARTIFACT_DOWNLOAD_RESULT"] == expected,
              f"artifact HTTP {status} misclassified")
        check(report["FAILED_ARTIFACT_HTTP_STATUS"] == str(status),
              f"artifact HTTP {status} was not preserved")
        assert_token_safe(temporary, workdir, completed)
    passed("artifact HTTP 401, 403, and 404 classifications preserve status")

    stale_marker = b"old-stale-probe-content\n"
    temporary, workdir, completed, report = run_probe(
        "stale_outputs",
        prepopulate={
            "success.env": stale_marker,
            "failed.env": stale_marker,
            "probe.env": stale_marker,
            "unknown.keep": b"preserve-unknown\n",
        },
    )
    output_directory = workdir / "ci-probe-results"
    check(completed.returncode == 4, "stale-output failure did not exit 4")
    check(report["OVERALL_RESULT"] == "failed",
          "stale-output failure did not write a new failed report")
    check(report["FAILED_ARTIFACT_DOWNLOAD_RESULT"] == "permission_denied",
          "failed artifact 403 was not preserved after stale cleanup")
    check(report["FAILED_ARTIFACT_HTTP_STATUS"] == "403",
          "failed artifact 403 status was not preserved after stale cleanup")
    check(not (output_directory / "success.env").exists(),
          "old success.env survived startup cleanup")
    check(not (output_directory / "failed.env").exists(),
          "old failed.env survived startup cleanup")
    check((output_directory / "unknown.keep").read_bytes() ==
          b"preserve-unknown\n", "startup cleanup removed an unknown file")
    check((output_directory / "probe.env").read_bytes() != stale_marker,
          "old probe.env survived startup cleanup")
    for output in output_directory.iterdir():
        if output.is_file():
            check(stale_marker not in output.read_bytes(),
                  f"stale content survived in {output.name}")
    assert_token_safe(temporary, workdir, completed)
    passed("startup cleanup removes only stale known outputs before a failed probe")

    router.reset("complete")
    environment = base_environment()
    temporary = tempfile.TemporaryDirectory()
    workdir = Path(temporary.name)
    output_directory = workdir / "ci-probe-results"
    output_directory.mkdir()
    symlink_target = workdir / "symlink-target"
    symlink_target.write_bytes(b"preserve-target\n")
    (output_directory / "success.env").symlink_to(symlink_target)
    completed = subprocess.run(
        [sys.executable, str(probe_path)], cwd=workdir, env=environment,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
    )
    check(completed.returncode == 4, "known-output symlink was not rejected")
    check(completed.stdout == b"", "symlink cleanup wrote unexpected stdout")
    check(completed.stderr == b"error: could not clean ci-probe-results\n",
          "symlink cleanup did not use the deterministic cleanup error")
    check(not router.requests, "symlink cleanup failure made an HTTP request")
    check(symlink_target.read_bytes() == b"preserve-target\n",
          "symlink cleanup modified its target")
    check(not (output_directory / "probe.env").exists(),
          "symlink cleanup failure wrote a report")
    assert_token_safe(temporary, workdir, completed)
    passed("unsafe known-output symlinks fail cleanup without touching their targets")

    for mode, expected_status, expected_result in (
        ("listing_401", "401", "permission_denied"),
        ("listing_404", "404", "unsupported"),
        ("malformed_json", "200", "invalid_response"),
    ):
        temporary, workdir, completed, report = run_probe(mode)
        check(completed.returncode == 3, f"{mode} did not yield partial")
        check(report["SUCCESS_JOB_LIST_HTTP_STATUS"] == expected_status,
              f"{mode} status was not preserved")
        check(report["SUCCESS_JOB_LIST_RESULT"] == expected_result,
              f"{mode} was misclassified")
        assert_token_safe(temporary, workdir, completed)
    passed("listing 401, 404, and malformed JSON remain distinct classifications")

    redirect_requests.clear()
    temporary, workdir, completed, report = run_probe("redirect")
    check(completed.returncode == 0, "redirected artifact did not validate")
    check(report["FAILED_ARTIFACT_DOWNLOAD_RESULT"] == "valid",
          "redirected failed artifact was not valid")
    check(redirect_requests == [("/download/failed", None)],
          "cross-origin redirect forwarded the token or used a wrong URL")
    assert_token_safe(temporary, workdir, completed)
    passed("artifact redirects are followed without cross-origin token forwarding")

    temporary, workdir, completed, report = run_probe("malicious_body")
    check(completed.returncode == 4, "token-bearing response did not fail")
    check(report["FAILED_ARTIFACT_DOWNLOAD_RESULT"] == "invalid",
          "token-bearing response was not invalid")
    assert_token_safe(temporary, workdir, completed)
    passed("token never appears in logs, reports, output files, exceptions, or URLs")

    invalid_inputs = (
        ("missing required variable", {"PMIX_PROBE_FAILED_JOB_ID": None}),
        ("zero ID", {"PMIX_PROBE_SUCCESS_JOB_ID": "0"}),
        ("leading-zero ID", {"PMIX_PROBE_SUCCESS_CHILD_PIPELINE_ID": "01"}),
        ("invalid SHA", {"PMIX_PROBE_FAILED_COMMIT": "g" * 40}),
        ("short SHA", {"PMIX_PROBE_SUCCESS_SUITE_COMMIT": "abc"}),
        ("invalid optional parent", {"PMIX_PROBE_SUCCESS_PARENT_PIPELINE_ID": "01"}),
        ("token-bearing API URL", {"CI_API_V4_URL": f"{api_base}/{token}"}),
        ("malformed HTTP API URL", {"CI_API_V4_URL": "http://["}),
        ("malformed HTTPS API URL", {"CI_API_V4_URL": "https://["}),
    )
    for label, update in invalid_inputs:
        temporary, workdir, completed, report = run_probe(
            "complete", environment_update=update
        )
        check(completed.returncode == 4, f"{label} was accepted")
        check(report["OVERALL_RESULT"] == "failed", f"{label} lacked failed report")
        check(not router.requests, f"{label} contacted the server")
        check(completed.stdout == b"", f"{label} wrote unexpected stdout")
        check(completed.stderr == b"error: invalid probe configuration\n",
              f"{label} did not use the deterministic configuration error")
        assert_token_safe(temporary, workdir, completed)
    passed("required inputs, IDs, SHAs, and optional parent IDs are validated")

    source = probe_path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    check(".ci-state" not in source, "probe references .ci-state")
    for forbidden in (
        "reframe", "run_exact_pmix_commit", "openpmix", "prrte", "slurm",
        "srun", "sbatch", "report_github_status", "reconcil", "github",
    ):
        check(forbidden not in source.lower(), f"probe references forbidden {forbidden}")
    check("subprocess" not in source, "probe can execute external commands")
    request_methods = re.findall(
        r"method\s*=\s*[\"'](GET|POST|PUT|PATCH|DELETE)[\"']", source
    )
    check(request_methods and set(request_methods) == {"GET"},
          "probe contains a non-GET HTTP method")
    passed("probe is isolated from state, builds, schedulers, status, and reconciliation")

    try:
        import yaml
    except ImportError as error:
        raise AssertionError("PyYAML is required for structural CI tests") from error
    parent = yaml.safe_load(parent_ci_path.read_text(encoding="utf-8"))
    probe_rule = (
        '$CI_PIPELINE_SOURCE == "web" && '
        '$PMIX_ARTIFACT_RETRIEVAL_PILOT == "1"'
    )
    execution_exclusion = {
        "if": '$PMIX_TESTS_PR_EXECUTION_PILOT == "1"',
        "when": "never",
    }
    job = parent["probe-pmix-child-artifacts"]
    check(job == {
        "stage": "test",
        "extends": [".frontier-shell-runner"],
        "timeout": "10m",
        "rules": [execution_exclusion, {"if": probe_rule, "when": "manual"}, {"when": "never"}],
        "script": ["python3 ci/probe_pmix_child_artifacts.py"],
        "artifacts": {
            "when": "always",
            "expire_in": "14 days",
            "paths": ["ci-probe-results/"],
        },
    }, "manual probe job structure changed")
    check("resource_group" not in job, "probe uses the PMIx resource group")
    normal_rule = '$CI_PIPELINE_SOURCE == "web" && $PMIX_CHILD_PIPELINE_PILOT == "1"'
    failed_rule = '$CI_PIPELINE_SOURCE == "web" && $PMIX_FAILED_RESULT_PILOT == "1"'
    guarded_rules = lambda rule: [execution_exclusion, {"if": rule}, {"when": "never"}]
    check(parent["generate-pmix-child-pipeline-pilot"]["rules"] == guarded_rules(normal_rule),
          "dynamic generation pilot guard changed")
    check(parent["trigger-pmix-child-pipeline-pilot"]["rules"] == guarded_rules(normal_rule),
          "dynamic trigger pilot guard changed")
    check(parent["trigger-pmix-failed-result-pilot"]["rules"] == guarded_rules(failed_rule),
          "failed-result pilot guard changed")
    suite_rules = parent["pmix-python-suite"]["rules"]
    check(suite_rules == [
        execution_exclusion,
        {"if": '$PMIX_TESTS_PR_PILOT == "1"', "when": "never"},
        {"if": probe_rule, "when": "never"},
        {"if": failed_rule, "when": "never"},
        {"if": normal_rule, "when": "never"},
        {"if": '$CI_PIPELINE_SOURCE == "web"'},
        {"if": '$CI_PIPELINE_SOURCE == "schedule"'},
        {"when": "never"},
    ], "production suite probe exclusion or existing rules changed")
    check(parent["workflow"]["rules"] == [
        {"if": '$CI_PIPELINE_SOURCE == "web"'},
        {"if": '$CI_PIPELINE_SOURCE == "schedule"'},
        {"when": "never"},
    ], "workflow schedule behavior changed")
    check("bash ci/test_probe_pmix_child_artifacts.sh" in parent["pmix-python-suite"]["script"],
          "new helper test is absent from the helper-test sequence")
    passed("parent YAML has the exact guard, isolation, schedule, and artifact contract")
finally:
    api_server.shutdown()
    redirect_server.shutdown()
    api_server.server_close()
    redirect_server.server_close()

print(f"1..{pass_count}")
PY
