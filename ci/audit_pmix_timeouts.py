#!/usr/bin/env python3
"""Audit execution timeouts in failed runs of one GitLab schedule (GET only)."""

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sys
from urllib.error import HTTPError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


LIMIT = 16 * 1024 * 1024
TIMEOUT_MARKERS = (
    "script timeout: context deadline exceeded",
    "step_script could not run to completion because the timeout was exceeded",
    "execution took longer than",
    "job's execution time exceeded the configured limit",
    "the script exceeded the maximum execution time set for the job",
)


class AuditError(Exception):
    pass


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise AuditError("API redirect rejected")


def identity(value):
    if type(value) is not int or value < 1:
        raise AuditError("Invalid API object identity")
    return value


class Client:
    def __init__(self, api_url, project_id, token, private=False):
        parts = urlsplit(api_url)
        if (parts.scheme != "https" or not parts.hostname or parts.username or
                parts.password or parts.query or parts.fragment or
                not re.fullmatch(r"[1-9][0-9]*", project_id) or not token):
            raise AuditError("HTTPS CI_API_V4_URL, numeric CI_PROJECT_ID, and a read token are required")
        self.base = api_url.rstrip("/") + "/projects/" + project_id
        self.project = int(project_id)
        self.origin = (parts.scheme, parts.netloc)
        self.headers = {"PRIVATE-TOKEN" if private else "JOB-TOKEN": token}
        self.calls = 0

    def get(self, path, query=None, raw=False):
        self.calls += 1
        if self.calls > 10000:
            raise AuditError("Audit request limit reached; result is incomplete")
        url = self.base + path + ("?" + urlencode(query) if query else "")
        try:
            request = Request(url, headers=self.headers, method="GET")
            with build_opener(NoRedirect()).open(request, timeout=30) as response:
                body = response.read(LIMIT + 1)
        except HTTPError as error:
            if error.code in (401, 403):
                raise AuditError("GitLab denied history access (HTTP {}). Set protected, masked GITLAB_READ_TOKEN with read_api access; do not paste its value into a log or chat.".format(error.code)) from None
            raise AuditError("GitLab returned HTTP {} for {}".format(error.code, path)) from None
        except AuditError:
            raise
        except Exception:
            raise AuditError("GitLab request failed for " + path) from None
        if len(body) > LIMIT:
            raise AuditError("API response exceeds audit size limit for " + path)
        if raw:
            return body.decode("utf-8", errors="replace")
        try:
            return json.loads(body)
        except (ValueError, UnicodeDecodeError):
            raise AuditError("Invalid JSON response for " + path) from None

    def pages(self, path, query=None):
        for page in range(1, 1001):
            data = self.get(path, dict(query or {}, per_page=100, page=page))
            if not isinstance(data, list):
                raise AuditError("Expected a list for " + path)
            yield from data
            if len(data) < 100:
                return
        raise AuditError("Pagination limit reached; result is incomplete")

    def link(self, value):
        if not isinstance(value, str) or any(ord(c) <= 32 for c in value):
            raise AuditError("Invalid API web link")
        parts = urlsplit(value)
        if (parts.scheme, parts.netloc) != self.origin or parts.username or parts.password:
            raise AuditError("API web link is outside this GitLab instance")
        return value


def audit(client, schedule_id):
    report = {"schedule_id": schedule_id, "snapshot_utc": datetime.now(timezone.utc).isoformat(),
              "scope": "All available failed runs of this schedule; retried job attempts included. Successful parent runs are outside this audit.",
              "complete": False, "failed_schedule_runs_found": 0,
              "schedule_runs_fully_scanned": 0, "jobs": [], "errors": []}
    seen_jobs = set()
    try:
        roots = list(client.pages("/pipeline_schedules/{}/pipelines".format(schedule_id),
                                  {"status": "failed", "sort": "desc"}))
        report["failed_schedule_runs_found"] = len(roots)
        for root in roots:
            root_id = identity(root["id"])
            root_url = client.link(root["web_url"])
            queue, visited = [root_id], set()
            errors_before = len(report["errors"])
            while queue:
                pipeline = queue.pop()
                if pipeline in visited:
                    continue
                visited.add(pipeline)
                for job in client.pages("/pipelines/{}/jobs".format(pipeline),
                                        {"scope[]": "failed", "include_retried": "true"}):
                    job_id = identity(job["id"])
                    if job_id in seen_jobs:
                        continue
                    seen_jobs.add(job_id)
                    reason = job.get("failure_reason")
                    row = {"schedule_pipeline_id": root_id, "schedule_pipeline_url": root_url,
                           "pipeline_id": pipeline, "job_id": job_id,
                           "job_name": job.get("name"), "job_url": client.link(job["web_url"]),
                           "failure_reason": reason, "duration_seconds": job.get("duration"),
                           "classification": "other_failure", "evidence": "GitLab failure reason"}
                    if reason == "job_execution_timeout":
                        row["classification"] = "confirmed_execution_timeout"
                    elif reason in ("stuck_or_timeout_failure", "script_failure", None):
                        try:
                            trace = client.get("/jobs/{}/trace".format(job_id), raw=True).lower()
                            marker = next((m for m in TIMEOUT_MARKERS if m in trace), None)
                            if marker:
                                row.update(classification="confirmed_execution_timeout", evidence=marker)
                            elif reason == "stuck_or_timeout_failure":
                                row.update(classification="stuck_or_timeout_unconfirmed",
                                           evidence="API groups stuck and timeout failures; no execution-limit marker found")
                            elif not trace.strip():
                                row.update(classification="unclassified", evidence="job trace is empty")
                        except AuditError as error:
                            row.update(classification="unclassified", evidence="job trace unavailable")
                            report["errors"].append({"job_id": job_id, "error": str(error)})
                    report["jobs"].append(row)
                for bridge in client.pages("/pipelines/{}/bridges".format(pipeline)):
                    child = bridge.get("downstream_pipeline")
                    if child:
                        if child.get("project_id", client.project) != client.project:
                            raise AuditError("Cross-project child found; not included in this project audit")
                        queue.append(identity(child["id"]))
            if len(report["errors"]) == errors_before:
                report["schedule_runs_fully_scanned"] += 1
        report["complete"] = not report["errors"]
    except (AuditError, KeyError, TypeError) as error:
        report["errors"].append({"error": str(error) if isinstance(error, AuditError) else "Malformed pipeline or job record"})
    confirmed = [j for j in report["jobs"] if j["classification"] == "confirmed_execution_timeout"]
    report["confirmed_execution_timeout_job_attempts"] = len(confirmed)
    report["failed_schedule_runs_with_confirmed_timeouts"] = len({j["schedule_pipeline_id"] for j in confirmed})
    report["unclassified_or_ambiguous_jobs"] = sum(
        j["classification"] in ("unclassified", "stuck_or_timeout_unconfirmed") for j in report["jobs"])
    return report


def render(report):
    lines = ["PMIx master schedule {} timeout audit".format(report["schedule_id"]),
             report["scope"], "Snapshot (UTC): " + report["snapshot_utc"],
             "Audit coverage: " + ("COMPLETE" if report["complete"] else "INCOMPLETE - counts are lower bounds"),
             "Failed schedule runs found: {}".format(report["failed_schedule_runs_found"]),
             "Failed schedule runs fully scanned: {}".format(report["schedule_runs_fully_scanned"]),
             "Failed schedule runs with confirmed execution timeouts: {}".format(report["failed_schedule_runs_with_confirmed_timeouts"]),
             "Confirmed execution-timeout job attempts: {}".format(report["confirmed_execution_timeout_job_attempts"]),
             "Unclassified or stuck/timeout ambiguous jobs: {}".format(report["unclassified_or_ambiguous_jobs"]),
             "Timeouts do not establish a PMIx test failure or a Slurm queue cause.", "",
             "Schedule pipeline\tJob\tClassification\tJob URL"]
    for job in report["jobs"]:
        lines.append("{}\t{}\t{}\t{}".format(job["schedule_pipeline_id"], job["job_id"], job["classification"], job["job_url"]))
    for error in report["errors"]:
        lines.append("INCOMPLETE: " + error["error"])
    return "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--schedule-id", type=int, default=46)
    parser.add_argument("--output-dir", type=Path, default=Path("ci-timeout-audit"))
    args = parser.parse_args()
    if args.schedule_id < 1:
        parser.error("schedule ID must be positive")
    private = os.environ.get("GITLAB_READ_TOKEN", "")
    try:
        client = Client(os.environ.get("CI_API_V4_URL", ""), os.environ.get("CI_PROJECT_ID", ""),
                        private or os.environ.get("CI_JOB_TOKEN", ""), bool(private))
    except AuditError as error:
        print("error: " + str(error), file=sys.stderr)
        return 2
    report = audit(client, args.schedule_id)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    (args.output_dir / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    text = render(report)
    (args.output_dir / "summary.txt").write_text(text)
    print(text, end="")
    return 0 if report["complete"] else 2


if __name__ == "__main__":
    sys.exit(main())
