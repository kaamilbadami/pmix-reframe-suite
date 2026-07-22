#!/bin/bash
set -euo pipefail

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
fetcher="$script_dir/fetch_pmix_tests_pr.py"
checker="$script_dir/check_trusted_pmix_tests_pr.py"

python3 - "$fetcher" "$checker" <<'PY'
import ast
import contextlib
from http.server import BaseHTTPRequestHandler, HTTPServer
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
from urllib.request import Request


fetcher_path = Path(sys.argv[1]).resolve()
checker_path = Path(sys.argv[2]).resolve()
token = "dummy-read-token-that-must-never-appear"
pr_number = "42"
request_path = "/repos/kaamilbadami/pmix-tests/pulls/42"
head_sha = "0123456789abcdef0123456789abcdef01234567"
pass_count = 0


def check(condition, message):
    if not condition:
        raise AssertionError(message)


def passed(message):
    global pass_count
    pass_count += 1
    print(f"ok - {message}")


def pr_body(author="kaamilbadami"):
    document = {
        "id": 9001,
        "number": 42,
        "state": "open",
        "user": {"id": 101, "login": author},
        "head": {
            "sha": head_sha,
            "repo": {"id": 202, "full_name": "kaamilbadami/pmix-tests"},
        },
        "base": {
            "repo": {"id": 303, "full_name": "kaamilbadami/pmix-tests"},
        },
    }
    return (json.dumps(document, separators=(",", ":")) + "\n \t").encode()


valid_body = pr_body()


class Router:
    def reset(self):
        self.mode = "valid"
        self.status = 200
        self.body = valid_body
        self.redirect_status = 302
        self.requests = []
        self.cross_requests = []


router = Router()
router.reset()


class ApiHandler(BaseHTTPRequestHandler):
    def log_message(self, format_string, *args):
        return

    def send(self, status, body=b"", *, content_type="application/json; charset=utf-8",
             headers=None, declared_length=None):
        self.send_response(status)
        if content_type is not None:
            self.send_header("Content-Type", content_type)
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        length = len(body) if declared_length is None else declared_length
        self.send_header("Content-Length", str(length))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def do_GET(self):
        router.requests.append({
            "path": self.path,
            "authorization": self.headers.get("Authorization"),
            "accept": self.headers.get("Accept"),
            "version": self.headers.get("X-GitHub-Api-Version"),
            "method": "GET",
        })
        if self.path == "/same-origin-result":
            self.send(200, router.body)
            return
        if self.path == "/redirect-loop":
            self.send(302, headers={"Location": "/redirect-loop"})
            return
        if self.path.startswith("/redirect-chain/"):
            index = int(self.path.rsplit("/", 1)[1])
            if index < 9:
                self.send(302, headers={"Location": f"/redirect-chain/{index + 1}"})
            else:
                self.send(200, router.body)
            return
        if self.path != request_path:
            self.send(404)
            return

        if router.mode == "valid":
            self.send(200, router.body)
        elif router.mode == "status":
            self.send(router.status, b'{"message":"failure"}')
        elif router.mode == "invalid-content-type":
            self.send(200, router.body, content_type="text/vnd.example+json")
        elif router.mode == "invalid-utf8":
            self.send(200, b"{\"bad\":\xff}")
        elif router.mode == "invalid-json":
            self.send(200, b"{invalid}")
        elif router.mode == "duplicate-json":
            self.send(200, b'{"id":1,"id":2}')
        elif router.mode == "array-json":
            self.send(200, b"[]")
        elif router.mode == "oversized":
            self.send(200, b"", declared_length=2 * 1024 * 1024 + 1)
        elif router.mode == "truncated":
            self.send(200, b'{"id":1}', declared_length=20)
            self.close_connection = True
        elif router.mode == "same-redirect":
            self.send(router.redirect_status, headers={"Location": "/same-origin-result"})
        elif router.mode == "cross-redirect":
            self.send(302, headers={"Location": cross_base + "/cross-result"})
        elif router.mode == "credential-redirect":
            destination = (
                f"http://user:password@127.0.0.1:{cross_server.server_port}/cross-result"
            )
            self.send(302, headers={"Location": destination})
        elif router.mode == "unsupported-redirect":
            self.send(302, headers={"Location": "ftp://127.0.0.1/cross-result"})
        elif router.mode == "malformed-redirect":
            self.send(302, headers={"Location": "http://["})
        elif router.mode == "token-redirect":
            self.send(302, headers={"Location": cross_base + "/" + token})
        elif router.mode == "redirect-loop":
            self.send(302, headers={"Location": "/redirect-loop"})
        elif router.mode == "redirect-excess":
            self.send(302, headers={"Location": "/redirect-chain/1"})
        else:
            self.send(500)


class CrossHandler(BaseHTTPRequestHandler):
    def log_message(self, format_string, *args):
        return

    def do_GET(self):
        router.cross_requests.append({
            "path": self.path,
            "headers": dict(self.headers.items()),
        })
        body = router.body
        self.send_response(200)
        self.send_header("Content-Type", "application/vnd.github+json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


api_server = HTTPServer(("127.0.0.1", 0), ApiHandler)
cross_server = HTTPServer(("127.0.0.1", 0), CrossHandler)
api_thread = threading.Thread(target=api_server.serve_forever, daemon=True)
cross_thread = threading.Thread(target=cross_server.serve_forever, daemon=True)
api_thread.start()
cross_thread.start()
api_base = f"http://127.0.0.1:{api_server.server_port}"
cross_base = f"http://127.0.0.1:{cross_server.server_port}"


def run_fetch(output, *, mode="valid", token_value=token, number=pr_number,
              body=None, stale=False, test_base=api_base, http_status=None,
              redirect_status=None):
    router.reset()
    router.mode = mode
    if body is not None:
        router.body = body
    if http_status is not None:
        router.status = http_status
    if redirect_status is not None:
        router.redirect_status = redirect_status
    if stale:
        output.write_bytes(b"stale-successful-pr-json\n")
    environment = os.environ.copy()
    environment.pop("GITHUB_PR_READ_TOKEN", None)
    if token_value is not None:
        environment["GITHUB_PR_READ_TOKEN"] = token_value
    completed = subprocess.run(
        [
            sys.executable,
            str(fetcher_path),
            "--pr-number", number,
            "--output", str(output),
            "--test-only-base-url", test_base,
        ],
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    check(token.encode() not in completed.stdout, "token leaked to stdout")
    check(token.encode() not in completed.stderr, "token leaked to stderr")
    return completed


def check_failure(output, completed, expected_status, label):
    check(completed.returncode == expected_status,
          f"{label} exited {completed.returncode}, expected {expected_status}")
    check(not output.exists(), f"{label} left stale or successful output")
    check(not list(output.parent.glob(f".{output.name}.tmp.*")),
          f"{label} left a temporary output")


try:
    with tempfile.TemporaryDirectory() as temporary_name:
        root = Path(temporary_name)

        output = root / "valid.json"
        completed = run_fetch(output)
        check(completed.returncode == 0, "valid response did not exit 0")
        check(output.read_bytes() == valid_body, "valid response bytes changed")
        passed("valid PR JSON is saved byte-for-byte without newline changes")

        check(len(router.requests) == 1 and router.requests[0]["path"] == request_path,
              "request path was not the fixed pmix-tests pull endpoint")
        check(router.requests[0]["method"] == "GET", "request method was not GET")
        passed("request uses the exact fixed repository path and GET method")

        check(router.requests[0]["authorization"] == f"Bearer {token}",
              "read token was not sent in the Authorization header")
        check(router.requests[0]["accept"] == "application/vnd.github+json",
              "GitHub JSON Accept header changed")
        check(router.requests[0]["version"] == "2026-03-10",
              "GitHub API version header changed")
        passed("environment token and explicit GitHub JSON headers are sent")

        check(token not in output.name and token.encode() not in output.read_bytes(),
              "token entered output name or content")
        passed("token is absent from output, logs, URLs, filenames, and response bytes")

        for value, label in ((None, "missing token"), ("", "empty token")):
            output = root / f"{label.replace(' ', '-')}.json"
            completed = run_fetch(output, token_value=value, stale=True)
            check_failure(output, completed, 6, label)
            check(not router.requests, f"{label} contacted the server")
        passed("missing and empty read tokens fail before network access")

        for number in ("0", "00", "042", "+42", "-42", " 42", "42 ", "4.2"):
            output = root / ("bad-number-" + str(abs(hash(number))) + ".json")
            completed = run_fetch(output, number=number, stale=True)
            check_failure(output, completed, 6, f"noncanonical PR number {number!r}")
            check(not router.requests, "noncanonical PR number contacted the server")
        passed("noncanonical PR numbers are rejected before network access")

        for status in (401, 403):
            output = root / f"status-{status}.json"
            completed = run_fetch(
                output, mode="status", http_status=status, stale=True
            )
            check_failure(output, completed, 4, f"HTTP {status}")
        passed("HTTP 401 and 403 map to authentication failure")

        output = root / "status-404.json"
        completed = run_fetch(
            output, mode="status", http_status=404, stale=True
        )
        check_failure(output, completed, 3, "HTTP 404")
        passed("HTTP 404 maps to not-found or unavailable")

        for status in (429, 500):
            output = root / f"status-{status}.json"
            completed = run_fetch(
                output, mode="status", http_status=status, stale=True
            )
            check_failure(output, completed, 3, f"HTTP {status}")
        passed("HTTP 429 and 500 fail safely as unavailable")

        malformed_cases = (
            ("invalid-content-type", 5, "invalid content type"),
            ("invalid-utf8", 5, "invalid UTF-8"),
            ("invalid-json", 5, "invalid JSON"),
            ("duplicate-json", 5, "duplicate JSON keys"),
            ("array-json", 5, "non-object JSON"),
            ("oversized", 5, "oversized response"),
            ("truncated", 5, "truncated response"),
        )
        for mode, status, label in malformed_cases:
            output = root / f"{mode}.json"
            completed = run_fetch(output, mode=mode, stale=True)
            check_failure(output, completed, status, label)
            passed(f"{label} is rejected without output")

        for redirect_status in (301, 302, 303, 307, 308):
            output = root / f"same-{redirect_status}.json"
            completed = run_fetch(
                output,
                mode="same-redirect",
                redirect_status=redirect_status,
            )
            check(completed.returncode == 0, f"HTTP {redirect_status} redirect failed")
            redirected = [item for item in router.requests
                          if item["path"] == "/same-origin-result"]
            check(len(redirected) == 1, "same-origin destination count changed")
            check(redirected[0]["authorization"] == f"Bearer {token}",
                  "same-origin redirect stripped authorization")
        passed("all supported same-origin redirects retain authorization")

        output = root / "cross.json"
        completed = run_fetch(output, mode="cross-redirect", stale=True)
        check_failure(output, completed, 5, "cross-origin redirect")
        check(len(router.requests) == 1, "cross-origin redirect made extra source requests")
        check(router.requests[0]["path"] == request_path,
              "cross-origin redirect changed the initial source request")
        check(not router.cross_requests, "cross-origin destination was contacted")
        check(all(
            "authorization" not in {
                name.lower() for name in request["headers"]
            }
            for request in router.cross_requests
        ), "authorization reached the cross-origin destination")
        for path in root.rglob("*"):
            if path.is_file():
                check(token.encode() not in path.read_bytes(),
                      f"token leaked to output file {path.name}")
        passed("cross-origin redirect is rejected before destination contact")

        spec = importlib.util.spec_from_file_location("fetcher_under_test", fetcher_path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        redirect_handler = module.SafeRedirectHandler()
        try:
            redirect_handler.redirect_request(
                Request("https://api.github.com/source",
                        headers={"Authorization": f"Bearer {token}"}, method="GET"),
                None, 302, "redirect", {}, "http://api.github.com/destination",
            )
        except module.UnsafeResponseError as error:
            check(token not in str(error), "downgrade exception disclosed token")
        else:
            raise AssertionError("HTTPS downgrade was accepted")
        passed("HTTPS-to-HTTP redirect downgrade is rejected")

        for mode, label in (
            ("credential-redirect", "credential-bearing redirect"),
            ("unsupported-redirect", "unsupported redirect scheme"),
            ("malformed-redirect", "malformed redirect target"),
            ("token-redirect", "token-bearing redirect target"),
        ):
            output = root / f"{mode}.json"
            completed = run_fetch(output, mode=mode, stale=True)
            check_failure(output, completed, 5, label)
            check(not router.cross_requests, f"{label} contacted destination")
            passed(f"{label} is rejected")

        for mode in ("redirect-loop", "redirect-excess"):
            output = root / f"{mode}.json"
            completed = run_fetch(output, mode=mode, stale=True)
            check_failure(output, completed, 5, mode)
        passed("redirect loops and excess redirect chains are bounded and rejected")

        output_target = root / "output-target.json"
        output_target.write_bytes(b"preserve-target\n")
        output_link = root / "output-link.json"
        output_link.symlink_to(output_target)
        completed = run_fetch(output_link)
        check(completed.returncode == 6, "output symlink was accepted")
        check(output_link.is_symlink(), "output symlink itself was removed")
        check(output_target.read_bytes() == b"preserve-target\n",
              "output symlink target changed")

        real_parent = root / "real-parent"
        real_parent.mkdir()
        parent_link = root / "parent-link"
        parent_link.symlink_to(real_parent, target_is_directory=True)
        completed = run_fetch(parent_link / "result.json")
        check(completed.returncode == 6, "parent-component symlink was accepted")
        check(not (real_parent / "result.json").exists(), "parent symlink was followed")
        check(not router.requests, "symlink path contacted server")
        passed("output and parent-component symlinks are rejected safely")

        state_dir = root / ".ci-state"
        state_dir.mkdir()
        sentinel = state_dir / "sentinel"
        sentinel.write_bytes(b"preserve-state\n")
        completed = run_fetch(state_dir / "pr.json")
        check(completed.returncode == 6, ".ci-state output was accepted")
        check(sentinel.read_bytes() == b"preserve-state\n", "state sentinel changed")
        check(not (state_dir / "pr.json").exists(), "state output was created")
        normal_output = root / "outside-state.json"
        completed = run_fetch(normal_output)
        check(completed.returncode == 0, "normal output beside state failed")
        check(sentinel.read_bytes() == b"preserve-state\n", "normal fetch changed state")
        passed(".ci-state paths are rejected and existing state remains unchanged")

        missing_parent_output = root / "missing-parent" / "result.json"
        completed = run_fetch(missing_parent_output)
        check(completed.returncode == 6, "missing parent was created")
        check(not missing_parent_output.parent.exists(), "fetcher created output parent")
        check(not router.requests, "missing output parent contacted server")
        passed("missing output parent directories are never created")

        replace_output = root / "replace-failure.json"
        replace_output.write_bytes(b"stale\n")
        real_replace = module.os.replace
        previous_token = os.environ.get("GITHUB_PR_READ_TOKEN")

        def fail_replace(source, destination):
            raise OSError("injected replacement failure")

        module.os.replace = fail_replace
        os.environ["GITHUB_PR_READ_TOKEN"] = token
        router.reset()
        captured_stdout = io.StringIO()
        captured_stderr = io.StringIO()
        try:
            with contextlib.redirect_stdout(captured_stdout), contextlib.redirect_stderr(captured_stderr):
                status = module.fetch([
                    "--pr-number", pr_number,
                    "--output", str(replace_output),
                    "--test-only-base-url", api_base,
                ])
        finally:
            module.os.replace = real_replace
            if previous_token is None:
                os.environ.pop("GITHUB_PR_READ_TOKEN", None)
            else:
                os.environ["GITHUB_PR_READ_TOKEN"] = previous_token
        check(status == 6, "replacement failure did not exit 6")
        check(not replace_output.exists(), "replacement failure left stale output")
        check(not list(root.glob(".replace-failure.json.tmp.*")),
              "replacement failure left temporary output")
        check(token not in captured_stdout.getvalue() + captured_stderr.getvalue(),
              "replacement failure disclosed token")
        passed("atomic replacement failure leaves no stale or temporary output")

        output = root / "nonloopback.json"
        completed = run_fetch(output, test_base="http://example.invalid")
        check(completed.returncode == 6, "non-loopback test override was accepted")
        check(not router.requests and not router.cross_requests,
              "non-loopback test override contacted a server")
        help_result = subprocess.run(
            [sys.executable, str(fetcher_path), "--help"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        )
        check(b"test-only-base-url" not in help_result.stdout + help_result.stderr,
              "test-only override appeared in normal help output")
        passed("test-only base override is loopback-only and hidden from production help")

        source = fetcher_path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        imports = set()
        calls = set()
        methods = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imports.add(node.module.split(".")[0])
            elif isinstance(node, ast.Call):
                if isinstance(node.func, ast.Attribute):
                    calls.add(node.func.attr)
                elif isinstance(node.func, ast.Name):
                    calls.add(node.func.id)
                for keyword in node.keywords:
                    if keyword.arg == "method":
                        if isinstance(keyword.value, ast.Str):
                            methods.add(keyword.value.s)
                        elif hasattr(ast, "Constant") and isinstance(
                                keyword.value, ast.Constant):
                            methods.add(keyword.value.value)
        check("subprocess" not in imports, "production fetcher imports subprocess")
        check(not calls.intersection({"system", "popen", "spawn", "fork", "execv", "execve"}),
              "production fetcher can execute external commands")
        check(methods == {"GET"}, "production fetcher contains a non-GET request")
        for forbidden in (
            "GITHUB_STATUS_TOKEN", "git checkout", "git clone", "sbatch", "srun",
            "reframe", "prrte", "slurm", "report_github_status", "statuses/",
            "webhook", "polling", "state_update", "update_state",
        ):
            check(forbidden not in source, f"production fetcher references {forbidden}")
        forbidden_state = ".ci-state"
        check(source.count(forbidden_state) == 1,
              f"production fetcher unexpectedly uses {forbidden_state}")
        passed("production capability is limited to fixed GET fetching and output publication")

        trusted_json = root / "trusted.json"
        trusted_env = root / "trusted-pr.env"
        completed = run_fetch(trusted_json, body=pr_body("kaamilbadami"))
        check(completed.returncode == 0, "trusted integration fetch failed")
        checked = subprocess.run(
            [sys.executable, str(checker_path), "--pr-json", str(trusted_json),
             "--pr-number", pr_number, "--output", str(trusted_env)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        )
        check(checked.returncode == 0, "trusted integration checker failed")
        check(b"PR_ELIGIBLE=1\n" in trusted_env.read_bytes(),
              "trusted integration did not produce PR_ELIGIBLE=1")
        check(b"PR_HEAD_REPOSITORY=kaamilbadami/pmix-tests\n" in
              trusted_env.read_bytes(),
              "trusted integration head repository changed")
        check(b"PR_BASE_REPOSITORY=kaamilbadami/pmix-tests\n" in
              trusted_env.read_bytes(),
              "trusted integration base repository changed")
        check(b"PR_FROM_FORK=0\n" in trusted_env.read_bytes(),
              "trusted same-repository integration was marked as a fork")
        passed("mock fetch feeds checker and trusted same-repository PR is eligible")

        untrusted_json = root / "untrusted.json"
        completed = run_fetch(untrusted_json, body=pr_body("untrusted-user"))
        check(completed.returncode == 0, "untrusted integration fetch failed")
        trusted_env.write_bytes(b"PR_ELIGIBLE=1\nSTALE=1\n")
        checked = subprocess.run(
            [sys.executable, str(checker_path), "--pr-json", str(untrusted_json),
             "--pr-number", pr_number, "--output", str(trusted_env)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        )
        check(checked.returncode == 3, "untrusted author was not policy-rejected")
        check(not trusted_env.exists(), "untrusted integration left trusted-pr.env")
        passed("untrusted author fetches successfully but checker exits 3 without output")

        check(all(item["path"].startswith("/") for item in router.requests),
              "test harness recorded a non-local request target")
        passed("all test HTTP traffic is confined to numeric loopback servers")
finally:
    api_server.shutdown()
    cross_server.shutdown()
    api_server.server_close()
    cross_server.server_close()

print(f"1..{pass_count}")
PY
