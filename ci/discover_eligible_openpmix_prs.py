#!/usr/bin/env python3
"""Discover eligible OpenPMIx PR heads not already represented by a pipeline.

Automatic use is opt-in at the GitLab rule layer. Production requests use only
the fixed openpmix/openpmix pulls collection and this GitLab project's pipeline
API. Test fixtures are accepted only with an explicit test-mode environment.
"""

import argparse
import json
import os
from pathlib import Path
import re
import tempfile
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from check_trusted_openpmix_pr import (
    EligibilityError, TARGET_REPOSITORY, source_policy, validate_metadata,
)


GITHUB_ORIGIN = "https://api.github.com"
GITHUB_COLLECTION = "/repos/openpmix/openpmix/pulls"
SHA_RE = re.compile(r"[0-9a-f]{40}\Z")
NUMBER_RE = re.compile(r"[1-9][0-9]*\Z")
PIPELINE_STATES = frozenset((
    "created", "waiting_for_resource", "preparing", "pending", "running",
    "success", "failed", "canceled", "skipped", "manual", "scheduled",
))
OUTPUT_KEYS = (
    "pr_number", "head_sha", "author", "base_repository",
    "head_repository", "source_policy",
)
MAX_BODY = 4 * 1024 * 1024


class DiscoveryError(Exception):
    pass


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, file_pointer, code, message, headers,
                         new_url):
        raise DiscoveryError("redirects are forbidden")


def strict_json(data):
    def pairs_hook(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise DiscoveryError("duplicate JSON key")
            result[key] = value
        return result
    try:
        return json.loads(
            data.decode("utf-8"), object_pairs_hook=pairs_hook,
            parse_constant=lambda value: (_ for _ in ()).throw(
                DiscoveryError("nonstandard JSON number")
            ),
        )
    except (UnicodeDecodeError, ValueError, RecursionError) as error:
        raise DiscoveryError("malformed JSON") from error


def get_json(url, headers):
    request = Request(url, headers=headers, method="GET")
    try:
        with build_opener(NoRedirect()).open(request, timeout=30) as response:
            if getattr(response, "status", None) != 200:
                raise DiscoveryError("unexpected HTTP status")
            body = response.read(MAX_BODY + 1)
    except DiscoveryError:
        raise
    except Exception as error:
        raise DiscoveryError("metadata request failed") from error
    if len(body) > MAX_BODY:
        raise DiscoveryError("metadata response is too large")
    return strict_json(body)


def read_fixture(path):
    if path.is_symlink() or not path.is_file():
        raise DiscoveryError("test fixture is unsafe")
    return strict_json(path.read_bytes())


def github_prs(token):
    if not token:
        raise DiscoveryError("GITHUB_PR_READ_TOKEN is required")
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": "Bearer " + token,
        "User-Agent": "frontier-openpmix-pr-discovery/1",
        "X-GitHub-Api-Version": "2026-03-10",
    }
    discovered = []
    for page in range(1, 11):
        query = urlencode({
            "state": "open", "per_page": "100",
            "page": str(page),
        })
        document = get_json(
            "{}{}?{}".format(GITHUB_ORIGIN, GITHUB_COLLECTION, query),
            headers,
        )
        if not isinstance(document, list):
            raise DiscoveryError("GitHub pulls response is not a list")
        discovered.extend(document)
        if len(document) < 100:
            return discovered
    raise DiscoveryError("GitHub pull pagination exceeded its bound")


def pipeline_pairs(api_url, project_id, token):
    parts = urlsplit(api_url)
    if (parts.scheme != "https" or not parts.netloc or parts.query or
            parts.fragment or not NUMBER_RE.fullmatch(project_id or "") or
            not token):
        raise DiscoveryError("GitLab discovery configuration is invalid")
    base = api_url.rstrip("/") + "/projects/" + quote(project_id, safe="")
    headers = {"JOB-TOKEN": token, "Accept": "application/json"}
    pairs = set()
    for page in range(1, 11):
        query = urlencode({
            "source": "parent_pipeline", "per_page": "100",
            "page": str(page),
        })
        pipelines = get_json(base + "/pipelines?" + query, headers)
        if not isinstance(pipelines, list):
            raise DiscoveryError("GitLab pipelines response is not a list")
        for pipeline in pipelines:
            if (not isinstance(pipeline, dict) or
                    pipeline.get("status") not in PIPELINE_STATES or
                    not isinstance(pipeline.get("id"), int)):
                raise DiscoveryError("GitLab pipeline identity is malformed")
            variables = get_json(
                base + "/pipelines/{}/variables".format(pipeline["id"]),
                headers,
            )
            if not isinstance(variables, list):
                raise DiscoveryError("GitLab variables response is malformed")
            mapping = {}
            for variable in variables:
                if (not isinstance(variable, dict) or
                        not isinstance(variable.get("key"), str) or
                        not isinstance(variable.get("value"), str)):
                    raise DiscoveryError("GitLab pipeline variable is malformed")
                key = variable["key"]
                if key in mapping:
                    raise DiscoveryError("duplicate GitLab pipeline variable")
                mapping[key] = variable["value"]
            number = mapping.get("OPENPMIX_PR_NUMBER", "")
            sha = mapping.get("OPENPMIX_PR_EXPECTED_SHA", "")
            if (mapping.get("OPENPMIX_PR_INTERNAL") == "1" and
                    NUMBER_RE.fullmatch(number) and SHA_RE.fullmatch(sha)):
                pairs.add((number, sha))
        if len(pipelines) < 100:
            return pairs
    raise DiscoveryError("GitLab pipeline pagination exceeded its bound")


def fixture_pairs(document):
    if not isinstance(document, list):
        raise DiscoveryError("completed-pipeline fixture is not a list")
    pairs = set()
    for item in document:
        if set(item) != {"pr_number", "head_sha"}:
            raise DiscoveryError("completed-pipeline fixture schema is invalid")
        number = item["pr_number"]
        sha = item["head_sha"]
        if (not isinstance(number, str) or not isinstance(sha, str) or
                NUMBER_RE.fullmatch(number) is None or
                SHA_RE.fullmatch(sha) is None or (number, sha) in pairs):
            raise DiscoveryError("completed-pipeline fixture is invalid")
        pairs.add((number, sha))
    return pairs


def eligible_records(documents, suppressed):
    if not isinstance(documents, list):
        raise DiscoveryError("pull-request fixture is not a list")
    records = []
    seen = set()
    seen_numbers = set()
    for document in documents:
        try:
            if not isinstance(document, dict):
                raise EligibilityError("PR item is not an object")
            number = document.get("number")
            if not isinstance(number, int) or isinstance(number, bool):
                raise EligibilityError("PR number is malformed")
            metadata = validate_metadata(document, number)
        except EligibilityError:
            continue
        policy = source_policy(metadata)
        if (metadata["state"] != "open" or metadata["draft"] != "0" or
                metadata["base_repository"] != TARGET_REPOSITORY or
                metadata["author"] not in ("kaamilbadami", "rhc54") or
                policy is None):
            continue
        pair = (str(number), metadata["head_sha"])
        if pair in seen or pair[0] in seen_numbers:
            raise DiscoveryError("GitHub returned a duplicate PR head")
        seen.add(pair)
        seen_numbers.add(pair[0])
        if pair in suppressed:
            continue
        records.append({
            "pr_number": pair[0],
            "head_sha": pair[1],
            "author": metadata["author"],
            "base_repository": metadata["base_repository"],
            "head_repository": metadata["head_repository"],
            "source_policy": policy,
        })
    return sorted(records, key=lambda item: int(item["pr_number"]))


def atomic_write(path, records):
    if path.is_absolute() or ".." in path.parts or not path.parent.is_dir():
        raise DiscoveryError("output path is unsafe")
    content = "".join(
        json.dumps({key: record[key] for key in OUTPUT_KEYS},
                   separators=(",", ":"), sort_keys=False) + "\n"
        for record in records
    )
    name = None
    try:
        with tempfile.NamedTemporaryFile(
                mode="w", encoding="ascii", dir=str(path.parent),
                prefix="." + path.name + ".", delete=False) as output:
            name = output.name
            os.fchmod(output.fileno(), 0o600)
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.replace(name, str(path))
        name = None
    finally:
        if name:
            try:
                os.unlink(name)
            except OSError:
                pass


def clear_output(path):
    if path.is_absolute() or ".." in path.parts or not path.parent.is_dir():
        raise DiscoveryError("output path is unsafe")
    try:
        if path.is_symlink() or (path.exists() and not path.is_file()):
            raise DiscoveryError("stale output is unsafe")
        path.unlink()
    except FileNotFoundError:
        pass


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--test-prs", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--test-pipelines", type=Path, help=argparse.SUPPRESS)
    options = parser.parse_args()
    try:
        clear_output(options.output)
        test_mode = os.environ.get("OPENPMIX_PR_DISCOVERY_TEST_MODE") == "1"
        if (options.test_prs is None) != (options.test_pipelines is None):
            raise DiscoveryError("both test fixtures are required")
        if options.test_prs is not None:
            if not test_mode:
                raise DiscoveryError("test fixtures require explicit test mode")
            prs = read_fixture(options.test_prs)
            suppressed = fixture_pairs(read_fixture(options.test_pipelines))
        else:
            if test_mode:
                raise DiscoveryError("test mode requires fixtures")
            prs = github_prs(os.environ.get("GITHUB_PR_READ_TOKEN", ""))
            suppressed = pipeline_pairs(
                os.environ.get("CI_API_V4_URL", ""),
                os.environ.get("CI_PROJECT_ID", ""),
                os.environ.get("CI_JOB_TOKEN", ""),
            )
        atomic_write(options.output, eligible_records(prs, suppressed))
        return 0
    except (DiscoveryError, OSError):
        print("error: OpenPMIx PR discovery failed", file=os.sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
