#!/bin/bash
set -euo pipefail

script_dir=$(cd -- "${BASH_SOURCE[0]%/*}" && pwd -P)
python3 - "$script_dir/fetch_openpmix_pr.py" \
    "$script_dir/fixtures/openpmix_pr/rhc54_verified_fork_open.json" <<'PY'
import importlib.util
import os
from pathlib import Path
import tempfile
import sys


module_path = Path(sys.argv[1])
fixture_path = Path(sys.argv[2])
spec = importlib.util.spec_from_file_location("fetch_openpmix_pr", module_path)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
fixture = fixture_path.read_bytes()
tests = 0


def passed(message):
    global tests
    tests += 1
    print("ok - " + message)


def check(condition, message):
    if not condition:
        raise AssertionError(message)


with tempfile.TemporaryDirectory(prefix="test-openpmix-fetch-") as temporary:
    root = Path(temporary)
    original_cwd = Path.cwd()
    original_token = os.environ.get(module.TOKEN_ENVIRONMENT)
    original_request = module.request_pr
    os.chdir(str(root))
    try:
        calls = []

        def successful_request(url, token):
            calls.append((url, token))
            return fixture

        module.request_pr = successful_request
        os.environ[module.TOKEN_ENVIRONMENT] = "mock-read-token"
        status = module.fetch([
            "--pr-number", "4028", "--output", "pr.json",
        ])
        check(status == module.EXIT_OK, "valid fetch failed")
        check(Path("pr.json").read_bytes() == fixture,
              "valid response changed during publication")
        check(calls == [(
            "https://api.github.com/repos/openpmix/openpmix/pulls/4028",
            "mock-read-token",
        )], "production URL or token changed")
        check("mock-read-token" not in Path("pr.json").read_text(),
              "token reached output")
        passed("valid metadata uses only the exact fixed OpenPMIx endpoint")

        for invalid in ("", "0", "01", "+1", "-1", " 1", "1 ", "1/2",
                        "abc"):
            calls.clear()
            Path("pr.json").write_text("stale")
            status = module.fetch([
                "--pr-number", invalid, "--output", "pr.json",
            ])
            check(status == module.EXIT_LOCAL,
                  "invalid PR number was not rejected: {!r}".format(invalid))
            check(not calls, "invalid PR number reached network helper")
            check(not Path("pr.json").exists(), "stale output survived")
        passed("noncanonical PR numbers fail before metadata access")

        del os.environ[module.TOKEN_ENVIRONMENT]
        calls.clear()
        status = module.fetch([
            "--pr-number", "4028", "--output", "pr.json",
        ])
        check(status == module.EXIT_LOCAL and not calls,
              "missing token reached metadata access")
        os.environ[module.TOKEN_ENVIRONMENT] = ""
        status = module.fetch([
            "--pr-number", "4028", "--output", "pr.json",
        ])
        check(status == module.EXIT_LOCAL and not calls,
              "empty token reached metadata access")
        passed("missing and empty read tokens fail closed")

        os.environ[module.TOKEN_ENVIRONMENT] = "mock-read-token"
        unsafe_documents = (
            b"not-json",
            b"[]",
            b'{"duplicate":1,"duplicate":2}',
            b'{"number":NaN}',
            b"\xff",
        )
        for body in unsafe_documents:
            module.request_pr = lambda url, token, body=body: body
            Path("pr.json").write_text("stale")
            status = module.fetch([
                "--pr-number", "4028", "--output", "pr.json",
            ])
            check(status == module.EXIT_UNSAFE,
                  "unsafe JSON received wrong status")
            check(not Path("pr.json").exists(),
                  "unsafe JSON left stale output")
        passed("invalid, duplicate-key, non-object, and non-UTF-8 JSON fails")

        for exception, expected in (
                (module.AuthenticationError(), module.EXIT_AUTH),
                (module.UnavailableError(), module.EXIT_UNAVAILABLE),
                (module.UnsafeResponseError(), module.EXIT_UNSAFE)):
            def failing_request(url, token, exception=exception):
                raise exception
            module.request_pr = failing_request
            status = module.fetch([
                "--pr-number", "4028", "--output", "pr.json",
            ])
            check(status == expected, "fetch failure classification changed")
            check(not Path("pr.json").exists(),
                  "fetch failure published output")
        passed("authentication, availability, and unsafe responses stay distinct")

        module.request_pr = successful_request
        target = root / "target"
        target.write_text("protected")
        Path("pr.json").symlink_to(target)
        status = module.fetch([
            "--pr-number", "4028", "--output", "pr.json",
        ])
        check(status == module.EXIT_LOCAL, "output symlink was accepted")
        check(target.read_text() == "protected", "output symlink was followed")
        Path("pr.json").unlink()
        passed("output symlinks are rejected without following them")

        Path(".ci-state").mkdir()
        state = Path(".ci-state/state.json")
        state.write_text("protected")
        status = module.fetch([
            "--pr-number", "4028", "--output", str(state),
        ])
        check(status == module.EXIT_LOCAL, "state output was accepted")
        check(state.read_text() == "protected", "state output was modified")
        passed(".ci-state is excluded from metadata publication")

        for url in (
                "https://api.github.com",
                "http://192.0.2.1:8080",
                "file:///tmp",
                "http://127.0.0.1/path"):
            try:
                module.validate_test_base_url(url)
            except module.LocalConfigurationError:
                continue
            raise AssertionError("unsafe test URL accepted: " + url)
        check(module.validate_test_base_url("http://127.0.0.1:12345")
              == "http://127.0.0.1:12345",
              "numeric loopback test URL rejected")
        passed("the hidden test override is numeric-loopback-only")

        handler = module.SafeRedirectHandler()
        request = module.Request(
            "https://api.github.com/repos/openpmix/openpmix/pulls/4028",
            method="GET",
        )
        try:
            handler.redirect_request(
                request, None, 302, "Found", {},
                "https://api.github.com/repos/openpmix/openpmix/pulls/4028",
            )
        except module.UnsafeResponseError:
            pass
        else:
            raise AssertionError("same-endpoint redirect was accepted")
        passed("all redirects are rejected to preserve the exact endpoint")

        source = module_path.read_text()
        check("openpmix/openpmix" in source, "fixed repository absent")
        check("clone_url" not in source, "fetcher references clone URLs")
        check("--repository" not in source and "--url" not in source,
              "production CLI accepts repository or URL input")
        check("method=\"GET\"" in source, "production request is not fixed GET")
        passed("production capability is fixed GET metadata retrieval only")
    finally:
        module.request_pr = original_request
        os.chdir(str(original_cwd))
        if original_token is None:
            os.environ.pop(module.TOKEN_ENVIRONMENT, None)
        else:
            os.environ[module.TOKEN_ENVIRONMENT] = original_token

print("1..{}".format(tests))
PY
