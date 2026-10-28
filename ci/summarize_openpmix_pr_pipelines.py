#!/usr/bin/env python3
"""Read-only summary of named OpenPMIx PR child pipelines."""

import argparse
from datetime import datetime, timedelta, timezone
import os
import sys
from urllib.parse import urlencode, urlsplit

from discover_eligible_openpmix_prs import (
    DiscoveryError, NUMBER_RE, PIPELINE_NAME_RE, PIPELINE_STATES, get_json,
)


def collect(api_url, project_id, token, hours, private_token=False):
    parts = urlsplit(api_url)
    if (parts.scheme != "https" or not parts.hostname or parts.username or
            parts.password or parts.query or parts.fragment or
            not NUMBER_RE.fullmatch(project_id) or not token):
        raise DiscoveryError("GitLab API URL, numeric project ID, and read token are required")
    headers = {"PRIVATE-TOKEN" if private_token else "JOB-TOKEN": token,
               "Accept": "application/json"}
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    rows = {}
    for page in range(1, 101):
        query = urlencode({"source": "parent_pipeline", "per_page": 100,
                           "page": page, "updated_after": since,
                           "order_by": "id", "sort": "desc"})
        pipelines = get_json(api_url.rstrip("/") + "/projects/" + project_id +
                             "/pipelines?" + query, headers)
        if not isinstance(pipelines, list):
            raise DiscoveryError("GitLab pipelines response is not a list")
        for pipeline in pipelines:
            if not isinstance(pipeline, dict):
                raise DiscoveryError("GitLab pipeline record is malformed")
            name = pipeline.get("name")
            match = PIPELINE_NAME_RE.fullmatch(name) if isinstance(name, str) else None
            if not match:
                continue
            identity, status = pipeline.get("id"), pipeline.get("status")
            if (type(identity) is not int or identity < 1 or
                    status not in PIPELINE_STATES):
                raise DiscoveryError("Named PR pipeline identity or status is malformed")
            link = pipeline.get("web_url")
            if not isinstance(link, str) or any(ord(c) <= 32 for c in link):
                raise DiscoveryError("PR pipeline link is malformed")
            target = urlsplit(link)
            if (target.scheme != parts.scheme or target.netloc != parts.netloc or
                    target.username or target.password):
                raise DiscoveryError("PR pipeline link is outside this GitLab instance")
            rows[identity] = (match.group(1), match.group(2), status, link)
        if len(pipelines) < 100:
            return [rows[key] for key in sorted(rows, reverse=True)]
    raise DiscoveryError("Pipeline pagination limit reached; use a shorter --hours window")


def render(rows, hours):
    lines = ["OpenPMIx PR test pipelines updated in the last {} hours".format(hours),
             "Snapshot: " + datetime.now(timezone.utc).isoformat(),
             "Status is the child pipeline status; open its link for individual test evidence.",
             "Empty polls and PMIx master batches are excluded. Retries appear separately."]
    if rows:
        lines.extend(["", "PR\tPMIx commit\tPipeline status\tPipeline link"])
        lines.extend("\t".join(row) for row in rows)
    else:
        lines.extend(["", "No actual PR test pipelines found in this window."])
    return "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hours", type=int, default=48,
                        help="include pipelines updated in this many hours (default: 48)")
    args = parser.parse_args()
    if not 1 <= args.hours <= 720:
        parser.error("--hours must be between 1 and 720")
    private = os.environ.get("GITLAB_READ_TOKEN", "")
    try:
        rows = collect(os.environ.get("CI_API_V4_URL", ""),
                       os.environ.get("CI_PROJECT_ID", ""),
                       private or os.environ.get("CI_JOB_TOKEN", ""),
                       args.hours, private_token=bool(private))
    except DiscoveryError as error:
        print("PR summary unavailable: " + str(error), file=sys.stderr)
        return 2
    print(render(rows, args.hours), end="")
    return 0


if __name__ == "__main__":
    sys.exit(main())
