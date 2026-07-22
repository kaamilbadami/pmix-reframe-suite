#!/bin/bash
set -euo pipefail

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
collector="$script_dir/collect_pmix_child_results.py"

python3 - "$collector" <<'PY'
import ast
import contextlib
from http.server import BaseHTTPRequestHandler, HTTPServer
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import threading
from urllib.parse import parse_qs, urlsplit
from urllib.request import Request


collector_path = Path(sys.argv[1]).resolve()
commit_a = "a" * 40
commit_b = "b" * 40
project_id = "77"
parent_id = "100"
child_id = "200"
token = "collector-token-that-must-never-appear"
artifact_a = b"PMIX_COMMIT=" + commit_a.encode() + b"\nopaque=\x00first\n"
artifact_b = b"PMIX_COMMIT=" + commit_b.encode() + b"\nopaque=second\xff\n"
pass_count = 0


def check(condition, message):
    if not condition:
        raise AssertionError(message)


def passed(message):
    global pass_count
    pass_count += 1
    print(f"ok - {message}")


class Router:
    def reset(self):
        self.requests = []
        self.cross_requests = []
        self.trigger_fallback = False
        self.trigger_duplicate = False
        self.trigger_missing = False
        self.trigger_pages = False
        self.jobs_pages = False
        self.jobs = [
            {"id": 302, "name": f"pmix-{commit_b}", "pipeline": {"id": 200}},
            {"id": 301, "name": f"pmix-{commit_a}", "pipeline": {"id": 200}},
        ]
        self.list_status = None
        self.list_body = None
        self.artifact_status = {}
        self.redirect = {}


router = Router()
router.reset()


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
        if parsed.path.startswith("/same-origin/"):
            _, _, job_id, sha = parsed.path.split("/", 3)
            body = artifact_a if sha.startswith(commit_a) else artifact_b
            self.send(200, body)
            return

        prefix = f"/api/v4/projects/{project_id}/"
        if not parsed.path.startswith(prefix):
            self.send(404)
            return
        suffix = parsed.path[len(prefix):]

        trigger_match = re.fullmatch(
            r"pipelines/([1-9][0-9]*)/(trigger_jobs|bridges)", suffix
        )
        if trigger_match:
            requested_parent, endpoint = trigger_match.groups()
            query = parse_qs(parsed.query)
            page = query.get("page", [""])[0]
            if requested_parent != parent_id or query.get("per_page") != ["100"]:
                self.send(400)
                return
            if router.trigger_fallback and endpoint == "trigger_jobs":
                self.send(404)
                return
            match = {
                "name": "trigger-pmix-child-pipeline-pilot",
                "downstream_pipeline": {"id": int(child_id), "status": "failed"},
            }
            unrelated = {"name": "unrelated", "downstream_pipeline": None}
            if router.trigger_missing:
                self.send(200, json.dumps([unrelated]).encode())
                return
            if router.trigger_duplicate:
                self.send(200, json.dumps([match, match]).encode())
                return
            if router.trigger_pages:
                body = [unrelated] if page == "1" else [match]
                headers = {"X-Next-Page": "2" if page == "1" else ""}
                self.send(200, json.dumps(body).encode(), headers)
                return
            self.send(200, json.dumps([match]).encode())
            return

        jobs_match = re.fullmatch(r"pipelines/([1-9][0-9]*)/jobs", suffix)
        if jobs_match:
            query = parse_qs(parsed.query)
            page = query.get("page", [""])[0]
            if (
                jobs_match.group(1) != child_id
                or query.get("per_page") != ["100"]
                or query.get("include_retried") != ["true"]
            ):
                self.send(400)
                return
            if router.list_status is not None:
                self.send(router.list_status, token.encode())
                return
            if router.list_body is not None:
                self.send(200, router.list_body)
                return
            if router.jobs_pages:
                body = router.jobs[:1] if page == "1" else router.jobs[1:]
                headers = {"X-Next-Page": "2" if page == "1" else ""}
                self.send(200, json.dumps(body).encode(), headers)
                return
            self.send(200, json.dumps(router.jobs).encode())
            return

        artifact_match = re.fullmatch(
            r"jobs/([1-9][0-9]*)/artifacts/ci-results/([0-9a-f]{40})\.env",
            suffix,
        )
        if artifact_match:
            job_id, sha = artifact_match.groups()
            key = (job_id, sha)
            if key in router.redirect:
                destination = router.redirect[key]
                self.send(302, headers={"Location": destination})
                return
            status = router.artifact_status.get(key)
            if status is not None:
                self.send(status, token.encode())
                return
            expected = {("301", commit_a): artifact_a, ("302", commit_b): artifact_b}
            if key not in expected:
                self.send(404)
                return
            self.send(200, expected[key])
            return
        self.send(404)


class CrossHandler(BaseHTTPRequestHandler):
    def log_message(self, format_string, *args):
        return

    def do_GET(self):
        router.cross_requests.append((self.path, dict(self.headers.items())))
        self.send_response(200)
        self.send_header("Content-Length", str(len(artifact_a)))
        self.end_headers()
        self.wfile.write(artifact_a)


api_server = HTTPServer(("127.0.0.1", 0), ApiHandler)
cross_server = HTTPServer(("127.0.0.1", 0), CrossHandler)
api_thread = threading.Thread(target=api_server.serve_forever, daemon=True)
cross_thread = threading.Thread(target=cross_server.serve_forever, daemon=True)
api_thread.start()
cross_thread.start()
api_base = f"http://127.0.0.1:{api_server.server_port}/api/v4"
cross_base = f"http://127.0.0.1:{cross_server.server_port}"


def environment(update=None):
    result = os.environ.copy()
    result.update({
        "CI_API_V4_URL": api_base,
        "CI_PROJECT_ID": project_id,
        "CI_JOB_TOKEN": token,
    })
    for key, value in (update or {}).items():
        if value is None:
            result.pop(key, None)
        else:
            result[key] = value
    return result


def parse_report(output):
    content = (output / "collection-report.json").read_bytes()
    check(content.endswith(b"\n"), "report is not newline terminated")
    report = json.loads(content)
    check(list(report) == sorted(report), "report object keys are not deterministic")
    check(report["schema_version"] == 1, "report schema version changed")
    return report


def run_case(commits, configure=None, env_update=None, parent=parent_id,
             output_setup=None):
    router.reset()
    if configure:
        configure()
    temporary = tempfile.TemporaryDirectory()
    root = Path(temporary.name)
    commit_file = root / "ordered.txt"
    if isinstance(commits, bytes):
        commit_file.write_bytes(commits)
    else:
        commit_file.write_bytes("".join(f"{sha}\n" for sha in commits).encode())
    output = root / "results"
    if output_setup:
        output_setup(root, output)
    completed = subprocess.run(
        [
            sys.executable,
            str(collector_path),
            "--commits", str(commit_file),
            "--parent-pipeline-id", parent,
            "--output", str(output),
        ],
        env=environment(env_update),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    report_path = output / "collection-report.json"
    report = parse_report(output) if report_path.is_file() else None
    return temporary, root, commit_file, output, completed, report


def assert_safe(temporary, root, completed, *, expect_primary_token=True):
    check(token.encode() not in completed.stdout, "token leaked to stdout")
    check(token.encode() not in completed.stderr, "token leaked to stderr")
    for path in root.rglob("*"):
        if path.is_file():
            check(token.encode() not in path.read_bytes(), f"token leaked to {path.name}")
    for path, request_token, method in router.requests:
        check(token not in path, "token leaked into a request URL")
        check(method == "GET", "a non-GET request was issued")
        if expect_primary_token:
            check(request_token == token, "primary-origin request lacked the job token")
    for path, headers in router.cross_requests:
        check(token not in path, "token leaked into a cross-origin URL")
        authentication_names = {
            "authorization", "job-token", "private-token", "proxy-authorization"
        }
        check(not authentication_names.intersection(
            name.lower() for name in headers
        ), "authentication was forwarded across origins")
    temporary.cleanup()


try:
    temporary, root, commits, output, completed, report = run_case([])
    check(completed.returncode == 0, "empty list did not exit 0")
    check(report["overall_result"] == "collected" and report["items"] == [],
          "empty list report is incorrect")
    check(not router.requests, "empty list made an HTTP request")
    assert_safe(temporary, root, completed)
    passed("empty ordered list succeeds without contacting the server")

    temporary, root, commits, output, completed, report = run_case([commit_a])
    check(completed.returncode == 0, "one commit was not collected")
    check((output / f"{commit_a}.env").read_bytes() == artifact_a,
          "single artifact bytes changed")
    check(report["items"] == [{
        "commit": commit_a, "http_status": 200, "job_id": 301,
        "result": "collected",
    }], "single-item report changed")
    assert_safe(temporary, root, completed)
    passed("one commit is collected by exact job and artifact path")

    temporary, root, commits, output, completed, report = run_case(
        [commit_a, commit_b]
    )
    check(completed.returncode == 0, "two commits were not collected")
    check([item["commit"] for item in report["items"]] == [commit_a, commit_b],
          "input order was not preserved")
    check([item["job_id"] for item in report["items"]] == [301, 302],
          "jobs were paired in API order instead of commit order")
    check((output / f"{commit_b}.env").read_bytes() == artifact_b,
          "second artifact bytes changed")
    assert_safe(temporary, root, completed)
    passed("two commits preserve input order when the job order differs")

    def paginated():
        router.trigger_pages = True
        router.jobs_pages = True

    temporary, root, commits, output, completed, report = run_case(
        [commit_a, commit_b], paginated
    )
    check(completed.returncode == 0, "paginated collection failed")
    check(sum("page=2" in path for path, _, _ in router.requests) == 2,
          "trigger and job page 2 were not both requested")
    assert_safe(temporary, root, completed)
    passed("parent discovery and child job listings follow pagination")

    def retried():
        router.jobs = [
            {"id": 201, "name": f"pmix-{commit_a}", "pipeline": {"id": 200},
             "retried": True},
            {"id": 301, "name": f"pmix-{commit_a}", "pipeline": {"id": 200},
             "retried": False},
        ]

    temporary, root, commits, output, completed, report = run_case(
        [commit_a], retried
    )
    check(completed.returncode == 0 and report["items"][0]["job_id"] == 301,
          "current retry was not selected exactly")
    check(not any("jobs/201/artifacts" in path for path, _, _ in router.requests),
          "superseded retry artifact was requested")
    assert_safe(temporary, root, completed)
    passed("retried jobs select the unique current attempt")

    def missing_job():
        router.jobs = [router.jobs[0]]

    def stale_outputs(root, output):
        output.mkdir()
        (output / f"{commit_a}.env").write_bytes(b"stale-result\n")
        (output / "collection-report.json").write_bytes(b"stale-report\n")
        (output / "unknown.keep").write_bytes(b"preserve-unknown\n")

    temporary, root, commits, output, completed, report = run_case(
        [commit_a], missing_job, output_setup=stale_outputs
    )
    check(completed.returncode == 3, "missing job did not exit 3")
    check(report["items"][0]["result"] == "job_not_found",
          "missing job was misclassified")
    check(not (output / f"{commit_a}.env").exists(),
          "stale requested result survived a missing job")
    check((output / "collection-report.json").read_bytes() != b"stale-report\n",
          "stale report survived collection")
    check((output / "unknown.keep").read_bytes() == b"preserve-unknown\n",
          "unknown output was removed")
    assert_safe(temporary, root, completed)
    passed("missing jobs remove stale requested results and replace only known reports")

    for status, expected, exit_code in (
        (401, "api_authentication_failure", 4),
        (403, "api_authentication_failure", 4),
        (404, "missing_artifact", 3),
    ):
        def artifact_failure(status=status):
            router.artifact_status[("301", commit_a)] = status
        setup = stale_outputs if status == 404 else None
        temporary, root, commits, output, completed, report = run_case(
            [commit_a], artifact_failure, output_setup=setup
        )
        check(completed.returncode == exit_code, f"artifact HTTP {status} exit changed")
        check(report["items"][0]["result"] == expected,
              f"artifact HTTP {status} was misclassified")
        check(report["items"][0]["http_status"] == status,
              f"artifact HTTP {status} was omitted")
        check(not (output / f"{commit_a}.env").exists(),
              f"artifact HTTP {status} published an output")
        if status == 404:
            check((output / "unknown.keep").read_bytes() == b"preserve-unknown\n",
                  "artifact 404 cleanup removed an unknown file")
        assert_safe(temporary, root, completed)
    passed("artifact HTTP 401, 403, and 404 remain distinct and non-successful")

    def later_missing():
        router.jobs = [router.jobs[1]]

    temporary, root, commits, output, completed, report = run_case(
        [commit_a, commit_b], later_missing
    )
    check(completed.returncode == 3, "later missing job did not exit 3")
    check((output / f"{commit_a}.env").read_bytes() == artifact_a,
          "current successful-prefix artifact was removed")
    check(report["items"][1]["result"] == "job_not_found",
          "later missing job was not reported")
    assert_safe(temporary, root, completed)
    passed("a later missing result preserves artifacts collected in this invocation")

    for status in (401, 403):
        def listing_failure(status=status):
            router.list_status = status
        temporary, root, commits, output, completed, report = run_case(
            [commit_a], listing_failure
        )
        check(completed.returncode == 4, f"listing HTTP {status} did not exit 4")
        check(report["items"][0]["result"] == "api_authentication_failure",
              f"listing HTTP {status} was misclassified")
        assert_safe(temporary, root, completed)
    def malformed_listing():
        router.list_body = b"{bad-json"
    temporary, root, commits, output, completed, report = run_case(
        [commit_a], malformed_listing
    )
    check(completed.returncode == 5, "malformed listing did not exit 5")
    check(report["items"][0]["result"] == "malformed_api_response",
          "malformed listing was misclassified")
    assert_safe(temporary, root, completed)
    passed("listing HTTP 401, 403, and malformed JSON are classified")

    def fallback():
        router.trigger_fallback = True
    temporary, root, commits, output, completed, report = run_case(
        [commit_a], fallback
    )
    check(completed.returncode == 0, "bridges fallback failed")
    check(report["parent_discovery_endpoint"] == "bridges",
          "bridges fallback was not reported")
    check(any("/trigger_jobs" in path for path, _, _ in router.requests),
          "trigger_jobs was not attempted first")
    check(any("/bridges" in path for path, _, _ in router.requests),
          "bridges fallback was not attempted")
    assert_safe(temporary, root, completed)
    passed("trigger_jobs page-1 404 falls back to bridges")

    for configure, expected, exit_code in (
        (lambda: setattr(router, "trigger_missing", True),
         "child_pipeline_not_found", 3),
        (lambda: setattr(router, "trigger_duplicate", True),
         "malformed_api_response", 5),
    ):
        temporary, root, commits, output, completed, report = run_case(
            [commit_a], configure
        )
        check(completed.returncode == exit_code, "child match exit changed")
        check(report["items"][0]["result"] == expected,
              "missing or ambiguous child match was misclassified")
        assert_safe(temporary, root, completed)
    passed("missing and duplicate child-pipeline matches are rejected distinctly")

    invalid_lists = (
        (f"{commit_a}\n{commit_a}\n".encode(), "duplicate"),
        (f"{commit_a.upper()}\n".encode(), "uppercase"),
        (f"{commit_a}\n\n".encode(), "blank"),
        (f" {commit_a}\n".encode(), "leading whitespace"),
        (f"{commit_a} \n".encode(), "trailing whitespace"),
        (b"abc\n", "malformed"),
    )
    for content, label in invalid_lists:
        temporary, root, commits, output, completed, report = run_case(content)
        check(completed.returncode == 6, f"{label} list was accepted")
        check(report["overall_result"] == "invalid_local_input",
              f"{label} list lacked an invalid report")
        check(not router.requests, f"{label} list contacted the server")
        assert_safe(temporary, root, completed)
    passed("duplicate, malformed, uppercase, blank, and padded SHA lines are rejected")

    for bad_parent in ("0", "01", "-1", "abc"):
        temporary, root, commits, output, completed, report = run_case(
            [commit_a], parent=bad_parent
        )
        check(completed.returncode == 6, f"parent ID {bad_parent} was accepted")
        check(not router.requests, "malformed parent ID contacted the server")
        assert_safe(temporary, root, completed)
    for bad_project in ("0", "01", "abc"):
        temporary, root, commits, output, completed, report = run_case(
            [commit_a], env_update={"CI_PROJECT_ID": bad_project}
        )
        check(completed.returncode == 6, f"project ID {bad_project} was accepted")
        check(not router.requests, "malformed project ID contacted the server")
        assert_safe(temporary, root, completed)
    passed("parent and project numeric identifiers are strictly validated")

    def output_symlink(root, output):
        target = root / "output-target"
        target.mkdir()
        (target / "sentinel").write_bytes(b"preserve\n")
        output.symlink_to(target, target_is_directory=True)
    temporary, root, commits, output, completed, report = run_case(
        [commit_a], output_setup=output_symlink
    )
    check(completed.returncode == 6 and report is None,
          "output-directory symlink was accepted")
    check((root / "output-target/sentinel").read_bytes() == b"preserve\n",
          "output symlink target changed")
    check(not router.requests, "output symlink contacted the server")
    assert_safe(temporary, root, completed)

    def known_symlink(root, output):
        output.mkdir()
        target = root / "known-target"
        target.write_bytes(b"preserve\n")
        (output / f"{commit_a}.env").symlink_to(target)
    temporary, root, commits, output, completed, report = run_case(
        [commit_a], output_setup=known_symlink
    )
    check(completed.returncode == 6, "known-output symlink was accepted")
    check((root / "known-target").read_bytes() == b"preserve\n",
          "known-output symlink target changed")
    check(not router.requests, "known-output symlink contacted the server")
    assert_safe(temporary, root, completed)

    def known_report_symlink(root, output):
        output.mkdir()
        target = root / "report-target"
        target.write_bytes(b"preserve-report-target\n")
        (output / "collection-report.json").symlink_to(target)
    temporary, root, commits, output, completed, report = run_case(
        [commit_a], output_setup=known_report_symlink
    )
    check(completed.returncode == 6, "known report symlink was accepted")
    check((root / "report-target").read_bytes() == b"preserve-report-target\n",
          "known report symlink target changed")
    check(not router.requests, "known report symlink contacted the server")
    assert_safe(temporary, root, completed)
    passed("output-directory and all known-output symbolic links are rejected safely")

    spec = importlib.util.spec_from_file_location("collector_under_test", collector_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    def same_redirect():
        router.redirect[("301", commit_a)] = (
            f"http://127.0.0.1:{api_server.server_port}/same-origin/301/{commit_a}.env"
        )
    temporary, root, commits, output, completed, report = run_case(
        [commit_a], same_redirect
    )
    check(completed.returncode == 0, "safe same-origin redirect failed")
    check((output / f"{commit_a}.env").read_bytes() == artifact_a,
          "same-origin redirect changed bytes")
    same_origin_requests = [
        request_token for path, request_token, _ in router.requests
        if path.startswith("/same-origin/")
    ]
    check(same_origin_requests == [token],
          "same-origin redirect did not retain the job token")
    assert_safe(temporary, root, completed)
    passed("safe same-origin redirects retain authenticated GET behavior")

    def cross_redirect():
        router.redirect[("301", commit_a)] = f"{cross_base}/artifact/{commit_a}.env"
    temporary, root, commits, output, completed, report = run_case(
        [commit_a], cross_redirect
    )
    check(completed.returncode == 0, "safe cross-origin redirect failed")
    check(report["items"][0]["result"] == "collected",
          "safe cross-origin redirect was not collected")
    check((output / f"{commit_a}.env").read_bytes() == artifact_a,
          "cross-origin redirect changed artifact bytes")
    check(len(router.cross_requests) == 1,
          "safe cross-origin destination was not contacted exactly once")
    check(cross_base.encode() not in completed.stdout + completed.stderr,
          "cross-origin destination appeared in diagnostics")
    check(cross_base not in json.dumps(report),
          "cross-origin destination appeared in the report")
    assert_safe(temporary, root, completed)
    passed("safe cross-origin redirects are followed without authentication headers")

    def credential_redirect():
        router.redirect[("301", commit_a)] = (
            f"http://user:password@127.0.0.1:{cross_server.server_port}/artifact"
        )
    temporary, root, commits, output, completed, report = run_case(
        [commit_a], credential_redirect
    )
    check(completed.returncode == 5, "credential-bearing redirect was accepted")
    check(report["items"][0]["result"] == "unsafe_redirect",
          "credential-bearing redirect was misclassified")
    check(not router.cross_requests, "credential-bearing destination was contacted")
    assert_safe(temporary, root, completed)

    def malformed_redirect():
        router.redirect[("301", commit_a)] = "http://["
    temporary, root, commits, output, completed, report = run_case(
        [commit_a], malformed_redirect
    )
    check(completed.returncode == 5, "malformed redirect was accepted")
    check(report["items"][0]["result"] in {
        "malformed_api_response", "unsafe_redirect"
    }, "malformed redirect was misclassified")
    assert_safe(temporary, root, completed)

    redirect_handler = module.SafeRedirectHandler()
    redirected_request = redirect_handler.redirect_request(
        Request(
            "https://gitlab.example.test/source",
            headers={
                "Authorization": "Bearer secret",
                "JOB-TOKEN": token,
                "PRIVATE-TOKEN": "private-secret",
                "Proxy-Authorization": "Basic secret",
            },
            method="GET",
        ),
        None,
        302,
        "redirect",
        {},
        "https://artifacts.example.test/destination",
    )
    redirected_header_names = {
        name.lower() for name, _ in redirected_request.header_items()
    }
    check(not module.AUTHENTICATION_HEADERS.intersection(redirected_header_names),
          "cross-origin redirect retained an authentication header")
    check(redirected_request.get_method() == "GET",
          "cross-origin redirect changed the request method")

    def expect_unsafe_redirect(old_url, new_url, method="GET", headers=None):
        request = Request(old_url, headers=headers or {}, method=method)
        try:
            redirect_handler.redirect_request(
                request, None, 302, "redirect", {}, new_url
            )
        except module.UnsafeRedirectError as error:
            check(token not in str(error) and new_url not in str(error),
                  "unsafe redirect exception disclosed sensitive context")
        else:
            raise AssertionError("unsafe redirect was accepted")

    expect_unsafe_redirect(
        "https://gitlab.example.test/source",
        "http://gitlab.example.test/destination",
        headers={"JOB-TOKEN": token},
    )
    expect_unsafe_redirect(
        "https://gitlab.example.test/source",
        "ftp://gitlab.example.test/destination",
        headers={"JOB-TOKEN": token},
    )
    expect_unsafe_redirect(
        "https://gitlab.example.test/source",
        f"https://gitlab.example.test/{token}",
        headers={"JOB-TOKEN": token},
    )
    expect_unsafe_redirect(
        "https://gitlab.example.test/source",
        "https://gitlab.example.test/destination",
        method="POST",
        headers={"JOB-TOKEN": token},
    )
    check(redirect_handler.max_redirections == 10,
          "redirect-count limit changed")
    passed("credential, downgrade, malformed, unsupported, token-bearing, and non-GET redirects are rejected")

    temporary, root, commits, output, completed, report = run_case([commit_a])
    check(token.encode() not in json.dumps(report).encode(), "token entered report")
    check(all(token not in path for path, _, _ in router.requests),
          "token entered a URL")
    assert_safe(temporary, root, completed)
    passed("the token is absent from logs, reports, files, exceptions, and URLs")

    def preexisting(root, output):
        output.mkdir()
        (output / f"{commit_a}.env").write_bytes(b"old\n")
    temporary, root, commits, output, completed, report = run_case(
        [commit_a], output_setup=preexisting
    )
    check(completed.returncode == 0, "atomic replacement run failed")
    check((output / f"{commit_a}.env").read_bytes() == artifact_a,
          "existing output was not replaced")
    check(not list(output.glob(f".{commit_a}.env.tmp.*")),
          "successful publication left a temporary")
    assert_safe(temporary, root, completed)
    passed("stale output is cleaned before byte-exact atomic publication")

    temporary = tempfile.TemporaryDirectory()
    root = Path(temporary.name)
    commit_file = root / "ordered.txt"
    commit_file.write_text(f"{commit_a}\n")
    before_input = commit_file.read_bytes()
    output = root / "results"
    output.mkdir()
    stale_result = output / f"{commit_a}.env"
    stale_result.write_bytes(b"stale-result\n")
    unknown = output / "unknown.keep"
    unknown.write_bytes(b"preserve-unknown\n")
    real_unlink = module.os.unlink
    previous = {name: os.environ.get(name) for name in (
        "CI_API_V4_URL", "CI_PROJECT_ID", "CI_JOB_TOKEN"
    )}

    def fail_stale_cleanup(path, *args, **kwargs):
        if Path(path).name == f"{commit_a}.env":
            raise OSError("injected cleanup failure")
        return real_unlink(path, *args, **kwargs)

    router.reset()
    os.environ.update(environment())
    module.os.unlink = fail_stale_cleanup
    cleanup_stderr = io.StringIO()
    try:
        with contextlib.redirect_stderr(cleanup_stderr):
            exit_status = module.collect([
                "--commits", str(commit_file),
                "--parent-pipeline-id", parent_id,
                "--output", str(output),
            ])
    finally:
        module.os.unlink = real_unlink
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
    check(exit_status == 6, "injected cleanup failure did not exit 6")
    check(cleanup_stderr.getvalue() == "error: invalid collector input\n",
          "cleanup failure diagnostic changed or disclosed context")
    check(token not in cleanup_stderr.getvalue(),
          "cleanup failure diagnostic disclosed the token")
    check(not router.requests, "cleanup failure contacted the API")
    check(stale_result.read_bytes() == b"stale-result\n",
          "failed cleanup changed the stale result")
    check(unknown.read_bytes() == b"preserve-unknown\n",
          "failed cleanup changed an unknown output")
    check(parse_report(output)["overall_result"] == "invalid_local_input",
          "cleanup failure lacked an invalid-local-input report")
    check(commit_file.read_bytes() == before_input,
          "cleanup failure changed the commit-list input")
    check(not list(output.glob(".*.tmp.*")),
          "cleanup failure left a temporary file")
    temporary.cleanup()
    passed("cleanup failure exits 6 without HTTP, input changes, or temporary files")

    temporary = tempfile.TemporaryDirectory()
    root = Path(temporary.name)
    commit_file = root / "ordered.txt"
    commit_file.write_text(f"{commit_a}\n")
    before_input = commit_file.read_bytes()
    output = root / "results"
    output.mkdir()
    existing = output / f"{commit_a}.env"
    existing.write_bytes(b"preserve-old\n")
    real_replace = module.os.replace
    previous = {name: os.environ.get(name) for name in (
        "CI_API_V4_URL", "CI_PROJECT_ID", "CI_JOB_TOKEN"
    )}

    def fail_artifact_replace(source, destination):
        if Path(destination).name == f"{commit_a}.env":
            raise OSError("injected publication failure")
        return real_replace(source, destination)

    router.reset()
    os.environ.update(environment())
    module.os.replace = fail_artifact_replace
    try:
        exit_status = module.collect([
            "--commits", str(commit_file),
            "--parent-pipeline-id", parent_id,
            "--output", str(output),
        ])
    finally:
        module.os.replace = real_replace
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
    check(exit_status == 6, "injected publication failure did not exit 6")
    check(not existing.exists(),
          "failed publication left the stale pre-invocation output")
    check(not list(output.glob(f".{commit_a}.env.tmp.*")),
          "failed replacement left a temporary")
    check(parse_report(output)["items"][0]["result"] == "invalid_local_input",
          "publication failure was not reported")
    check(commit_file.read_bytes() == before_input, "commit input changed")
    temporary.cleanup()
    passed("injected publication failure cleans up without restoring stale output")

    hidden_name = ".ci" + "-state"
    temporary = tempfile.TemporaryDirectory()
    root = Path(temporary.name)
    hidden = root / hidden_name
    hidden.mkdir()
    sentinel = hidden / "sentinel"
    sentinel.write_bytes(b"preserve\n")
    commit_file = root / "ordered.txt"
    commit_file.write_bytes(b"")
    output = root / "results"
    completed = subprocess.run([
        sys.executable, str(collector_path),
        "--commits", str(commit_file),
        "--parent-pipeline-id", parent_id,
        "--output", str(output),
    ], env=environment(), stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    check(completed.returncode == 0, "isolated empty collection failed")
    check(sentinel.read_bytes() == b"preserve\n", "hidden sentinel changed")
    source = collector_path.read_text(encoding="utf-8")
    check(hidden_name not in source, "production source names the hidden path")
    absent_root = root / "absent"
    absent_root.mkdir()
    absent_commits = absent_root / "ordered.txt"
    absent_commits.write_bytes(b"")
    subprocess.run([
        sys.executable, str(collector_path),
        "--commits", str(absent_commits),
        "--parent-pipeline-id", parent_id,
        "--output", str(absent_root / "results"),
    ], env=environment(), stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
    check(not (absent_root / hidden_name).exists(), "hidden path was created")
    temporary.cleanup()
    passed("hidden shared data is never named, created, read, or modified")

    source = collector_path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    imports = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module.split(".")[0])
    check("subprocess" not in imports, "production source can launch commands")
    check("os.system" not in source and "os.popen" not in source,
          "production source contains an external-command API")
    methods = re.findall(r"method\s*=\s*[\"'](GET|POST|PUT|PATCH|DELETE)[\"']", source)
    check(methods and set(methods) == {"GET"}, "production source contains non-GET HTTP")
    for forbidden in (
        "reframe", "openpmix", "prrte", "slurm", "sbatch", "srun",
        "github", "run_exact_pmix_commit", "report_github_status", "reconcil",
        "state_update", "update_state",
    ):
        check(forbidden not in source.lower(), f"production source references {forbidden}")
    passed("production has only GET networking and no command, scheduler, build, or updater capability")
finally:
    api_server.shutdown()
    cross_server.shutdown()
    api_server.server_close()
    cross_server.server_close()

print(f"1..{pass_count}")
PY
