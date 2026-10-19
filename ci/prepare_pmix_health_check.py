#!/usr/bin/env python3
"""Select the saved PMIx commit for a daily health check only on an empty backlog."""

import argparse
from pathlib import Path
import time

from reconcile_pmix_results import atomic_write, parse_commits, parse_state


def prepare(state_path: Path, commits_path: Path, now: int) -> bool:
    state, _ = parse_state(state_path)
    commits = parse_commits(commits_path)
    if commits:
        print("New PMIx commits found; using normal per-commit testing")
        return False
    age = now - int(state["LAST_SUCCESS_EPOCH"])
    if age < 86400:
        print("No new PMIx commits; daily health check is not due")
        return False
    atomic_write(commits_path, (state["PMIX_COMMIT"] + "\n").encode("ascii"))
    print("Daily health check due; retesting saved PMIx commit " + state["PMIX_COMMIT"])
    return True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", required=True, type=Path)
    parser.add_argument("--commits", required=True, type=Path)
    options = parser.parse_args()
    prepare(options.state, options.commits, int(time.time()))


if __name__ == "__main__":
    main()
