#!/usr/bin/env python3
"""Fetch authoritative kaamilbadami/pmix-tests pull-request metadata.

Usage: fetch_pmix_tests_pr.py --pr-number NUMBER --output PATH

Exit statuses are deliberately conservative:

* 0: PR JSON was fetched and published successfully
* 3: the PR was not found or the service was unavailable
* 4: GitHub rejected authentication or authorization
* 5: an HTTP, redirect, or JSON response was unsafe or malformed
* 6: local arguments, configuration, or output publication were invalid
"""

import argparse
import http.client
import ipaddress
import json
import os
from pathlib import Path
import re
import stat
import tempfile
from typing import List, Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


EXIT_OK = 0
EXIT_UNAVAILABLE = 3
EXIT_AUTH = 4
EXIT_UNSAFE = 5
EXIT_LOCAL = 6

PRODUCTION_ORIGIN = "https://api.github.com"
PR_PATH_PREFIX = "/repos/kaamilbadami/pmix-tests/pulls/"
TOKEN_ENVIRONMENT = "GITHUB_PR_READ_TOKEN"
API_VERSION = "2026-03-10"
RESPONSE_LIMIT = 2 * 1024 * 1024
REQUEST_TIMEOUT = 30
MAX_REDIRECTS = 5
PR_NUMBER_PATTERN = re.compile(r"[1-9][0-9]*")
CONTENT_LENGTH_PATTERN = re.compile(r"0|[1-9][0-9]*")
FORBIDDEN_STATE_COMPONENT = ".ci-state"


class LocalConfigurationError(Exception):
    """A local argument, secret, or output path is invalid."""


class AuthenticationError(Exception):
    """The API rejected authentication or authorization."""


class UnavailableError(Exception):
    """The requested PR or API service is unavailable."""


class UnsafeResponseError(Exception):
    """An HTTP, redirect, or JSON response cannot be trusted."""


class FetchArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise LocalConfigurationError("invalid arguments")


def parse_arguments(arguments: Optional[List[str]] = None) -> argparse.Namespace:
    parser = FetchArgumentParser(description=__doc__)
    parser.add_argument("--pr-number", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--test-only-base-url", help=argparse.SUPPRESS)
    return parser.parse_args(arguments)


def absolute_path(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def validate_output_path(path: Path) -> Path:
    output = absolute_path(path)
    if output == Path(output.anchor):
        raise LocalConfigurationError("unsafe output path")
    if FORBIDDEN_STATE_COMPONENT in output.parts:
        raise LocalConfigurationError("unsafe output path")

    current = Path(output.anchor)
    for index, component in enumerate(output.parts[1:], start=1):
        current /= component
        final = index == len(output.parts) - 1
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            if final:
                break
            raise LocalConfigurationError("output parent does not exist")
        except OSError as error:
            raise LocalConfigurationError("output path is unavailable") from error
        if stat.S_ISLNK(metadata.st_mode):
            raise LocalConfigurationError("output path contains a symbolic link")
        if final:
            if not stat.S_ISREG(metadata.st_mode):
                raise LocalConfigurationError("output is not a regular file")
        elif not stat.S_ISDIR(metadata.st_mode):
            raise LocalConfigurationError("output parent is not a directory")

    try:
        parent_metadata = output.parent.lstat()
    except OSError as error:
        raise LocalConfigurationError("output parent is unavailable") from error
    if not stat.S_ISDIR(parent_metadata.st_mode):
        raise LocalConfigurationError("output parent is not a directory")
    return output


def remove_stale_output(path: Path) -> None:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return
    except OSError as error:
        raise LocalConfigurationError("stale output is unavailable") from error
    if not stat.S_ISREG(metadata.st_mode):
        raise LocalConfigurationError("stale output is not a regular file")
    try:
        path.unlink()
    except OSError as error:
        raise LocalConfigurationError("cannot remove stale output") from error


def remove_failed_output(path: Optional[Path]) -> None:
    if path is None:
        return
    try:
        metadata = path.lstat()
        if stat.S_ISREG(metadata.st_mode):
            path.unlink()
    except OSError:
        pass


def validate_pr_number(value: str) -> int:
    if PR_NUMBER_PATTERN.fullmatch(value) is None:
        raise LocalConfigurationError("PR number is not a canonical positive integer")
    return int(value)


def validate_token(value: str) -> str:
    if not value or any(ord(character) < 33 or ord(character) > 126 for character in value):
        raise LocalConfigurationError(f"{TOKEN_ENVIRONMENT} is required")
    return value


def normalized_origin(parts) -> Tuple[str, str, int]:
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https") or not parts.netloc:
        raise UnsafeResponseError("unsupported URL origin")
    if parts.username is not None or parts.password is not None:
        raise UnsafeResponseError("credential-bearing URL")
    try:
        port = parts.port
    except ValueError as error:
        raise UnsafeResponseError("invalid URL port") from error
    hostname = parts.hostname
    if hostname is None:
        raise UnsafeResponseError("missing URL hostname")
    if port is None:
        port = 443 if scheme == "https" else 80
    return scheme, hostname.lower(), port


def validate_test_base_url(value: str) -> str:
    try:
        parts = urlsplit(value)
        normalized_origin(parts)
        address = ipaddress.ip_address(parts.hostname or "")
    except (ValueError, UnsafeResponseError) as error:
        raise LocalConfigurationError("invalid test-only base URL") from error
    if not address.is_loopback:
        raise LocalConfigurationError("test-only base URL is not loopback")
    if parts.path not in ("", "/") or parts.query or parts.fragment:
        raise LocalConfigurationError("invalid test-only base URL")
    return value.rstrip("/")


class SafeRedirectHandler(HTTPRedirectHandler):
    """Follow only bounded, credential-free GET redirects."""

    max_repeats = 2
    max_redirections = MAX_REDIRECTS

    def http_error_308(self, request, file_pointer, code, message, headers):
        # Python 3.6 does not register 308 in HTTPRedirectHandler.
        return self.http_error_302(
            request, file_pointer, code, message, headers
        )

    def redirect_request(self, request, file_pointer, code, message, headers, new_url):
        if code not in (301, 302, 303, 307, 308) or request.get_method() != "GET":
            raise UnsafeResponseError("unsupported redirect")
        try:
            old_parts = urlsplit(request.full_url)
            new_parts = urlsplit(new_url)
            old_origin = normalized_origin(old_parts)
            new_origin = normalized_origin(new_parts)
        except (ValueError, UnsafeResponseError) as error:
            raise UnsafeResponseError("malformed redirect") from error
        if new_parts.fragment:
            raise UnsafeResponseError("redirect contains a fragment")
        if old_parts.scheme.lower() == "https" and new_parts.scheme.lower() != "https":
            raise UnsafeResponseError("HTTPS redirect downgrade")
        authorization = request.get_header("Authorization")
        if authorization:
            _, _, credential = authorization.partition(" ")
            if credential and credential in new_url:
                raise UnsafeResponseError("redirect contains authentication material")
        if old_origin != new_origin:
            raise UnsafeResponseError("cross-origin redirect")

        return Request(
            new_url,
            headers=dict(request.header_items()),
            origin_req_host=request.origin_req_host,
            unverifiable=True,
            method="GET",
        )


def require_json_content_type(headers) -> None:
    try:
        values = headers.get_all("Content-Type")
    except AttributeError as error:
        raise UnsafeResponseError("malformed response headers") from error
    if not values or len(values) != 1 or not isinstance(values[0], str):
        raise UnsafeResponseError("invalid content type")
    media_type = values[0].split(";", 1)[0].strip().lower()
    if "/" not in media_type:
        raise UnsafeResponseError("invalid content type")
    main_type, subtype = media_type.split("/", 1)
    if media_type != "application/json" and not (
        main_type == "application" and subtype.endswith("+json")
    ):
        raise UnsafeResponseError("invalid content type")

    encodings = headers.get_all("Content-Encoding") or []
    if len(encodings) > 1 or (encodings and encodings[0].strip().lower() != "identity"):
        raise UnsafeResponseError("unsupported content encoding")


def expected_content_length(headers) -> Optional[int]:
    try:
        values = headers.get_all("Content-Length")
    except AttributeError as error:
        raise UnsafeResponseError("malformed response headers") from error
    if not values:
        return None
    if len(values) != 1 or CONTENT_LENGTH_PATTERN.fullmatch(values[0]) is None:
        raise UnsafeResponseError("invalid content length")
    length = int(values[0])
    if length > RESPONSE_LIMIT:
        raise UnsafeResponseError("response is oversized")
    return length


def request_pr(url: str, token: str) -> bytes:
    request = Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "Accept-Encoding": "identity",
            "Authorization": f"Bearer {token}",
            "User-Agent": "pmix-tests-pr-fetcher/1",
            "X-GitHub-Api-Version": API_VERSION,
        },
        method="GET",
    )
    opener = build_opener(SafeRedirectHandler())
    try:
        with opener.open(request, timeout=REQUEST_TIMEOUT) as response:
            if getattr(response, "status", None) != 200:
                raise UnsafeResponseError("unexpected success status")
            require_json_content_type(response.headers)
            declared_length = expected_content_length(response.headers)
            body = response.read(RESPONSE_LIMIT + 1)
            if len(body) > RESPONSE_LIMIT:
                raise UnsafeResponseError("response is oversized")
            if declared_length is not None and len(body) != declared_length:
                raise UnsafeResponseError("response body is truncated")
            if token.encode("ascii") in body:
                raise UnsafeResponseError("response contains authentication material")
            return body
    except UnsafeResponseError:
        raise
    except HTTPError as error:
        status = error.code
        error.close()
        if status in (401, 403):
            raise AuthenticationError() from None
        if status == 404 or status == 429 or 500 <= status <= 599:
            raise UnavailableError() from None
        raise UnsafeResponseError("unexpected HTTP status") from None
    except http.client.IncompleteRead as error:
        raise UnsafeResponseError("response body is truncated") from error
    except http.client.HTTPException as error:
        raise UnsafeResponseError("malformed HTTP response") from error
    except (URLError, TimeoutError, OSError):
        raise UnavailableError() from None
    except (ValueError, TypeError) as error:
        raise UnsafeResponseError("malformed HTTP response") from error


def reject_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise UnsafeResponseError("duplicate JSON object key")
        result[key] = value
    return result


def reject_nonstandard_number(value: str):
    raise UnsafeResponseError("nonstandard JSON number")


def validate_json(body: bytes) -> None:
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError as error:
        raise UnsafeResponseError("response is not valid UTF-8") from error
    try:
        document = json.loads(
            text,
            object_pairs_hook=reject_duplicate_keys,
            parse_constant=reject_nonstandard_number,
        )
    except (json.JSONDecodeError, RecursionError) as error:
        raise UnsafeResponseError("response is not valid JSON") from error
    if not isinstance(document, dict):
        raise UnsafeResponseError("JSON top level is not an object")


def atomic_write(path: Path, content: bytes) -> None:
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


def fetch(arguments: Optional[List[str]] = None) -> int:
    output_path: Optional[Path] = None
    try:
        options = parse_arguments(arguments)
        output_path = validate_output_path(options.output)
        remove_stale_output(output_path)
        pr_number = validate_pr_number(options.pr_number)
        token = validate_token(os.environ.get(TOKEN_ENVIRONMENT, ""))

        if options.test_only_base_url is None:
            base_url = PRODUCTION_ORIGIN
        else:
            base_url = validate_test_base_url(options.test_only_base_url)
        url = f"{base_url}{PR_PATH_PREFIX}{pr_number}"

        body = request_pr(url, token)
        validate_json(body)
        atomic_write(output_path, body)
        return EXIT_OK
    except AuthenticationError:
        remove_failed_output(output_path)
        print("error: GitHub authentication or authorization failed", file=os.sys.stderr)
        return EXIT_AUTH
    except UnavailableError:
        remove_failed_output(output_path)
        print("error: pull request or GitHub API is unavailable", file=os.sys.stderr)
        return EXIT_UNAVAILABLE
    except UnsafeResponseError:
        remove_failed_output(output_path)
        print("error: unsafe or malformed GitHub API response", file=os.sys.stderr)
        return EXIT_UNSAFE
    except LocalConfigurationError:
        remove_failed_output(output_path)
        print("error: invalid fetcher configuration or output", file=os.sys.stderr)
        return EXIT_LOCAL
    except OSError:
        remove_failed_output(output_path)
        print("error: PR JSON output failed", file=os.sys.stderr)
        return EXIT_LOCAL


if __name__ == "__main__":
    raise SystemExit(fetch())
