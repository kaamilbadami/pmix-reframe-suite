#!/usr/bin/env python3
"""Validate the sole supported OpenPMIx submodule without following its URL."""

import argparse
from pathlib import Path
import re
import subprocess
import sys


OAC_URL = "https://github.com/open-mpi/oac.git"


def git(root, *args):
    return subprocess.check_output(
        ["/usr/bin/git", "-c", "core.hooksPath=/dev/null", "-C", str(root), *args],
        stderr=subprocess.DEVNULL)


def validate(root, populated=False):
    entries = git(root, "ls-tree", "-rz", "HEAD").split(b"\0")
    links = []
    metadata = None
    for entry in filter(None, entries):
        header, path = entry.split(b"\t", 1)
        mode, kind, sha = header.split()
        if mode == b"160000":
            links.append((path, sha))
        if path == b".gitmodules":
            metadata = mode
    if not links and metadata is None:
        return "none"
    if (len(links) != 1 or links[0][0] != b"config/oac" or
            metadata != b"100644"):
        raise ValueError("only the config/oac submodule and regular .gitmodules are supported")
    config = git(root, "config", "--blob", "HEAD:.gitmodules", "--null", "--list")
    values = {}
    for item in filter(None, config.split(b"\0")):
        key, value = item.split(b"\n", 1)
        if key in values:
            raise ValueError("duplicate submodule configuration")
        values[key] = value
    if (set(values) != {b"submodule.oac.path", b"submodule.oac.url"} or
            values[b"submodule.oac.path"] != b"config/oac" or
            values[b"submodule.oac.url"] not in (
                b"../../open-mpi/oac", OAC_URL.encode(),
                b"https://github.com/open-mpi/oac")):
        raise ValueError("unexpected OAC path, URL, or submodule options")
    sha = links[0][1].decode("ascii")
    if re.fullmatch(r"[0-9a-f]{40}", sha) is None:
        raise ValueError("invalid OAC commit")
    if populated:
        sub = root / "config/oac"
        if (sub.is_symlink() or (root / "config").is_symlink() or
                not (sub / ".git").is_dir()):
            raise ValueError("OAC checkout is absent or unsafe")
        if (git(sub, "rev-parse", "HEAD").strip().decode() != sha or
                git(root, "config", "--local", "--get", "submodule.oac.url").strip().decode() != OAC_URL or
                git(sub, "remote", "get-url", "origin").strip().decode() != OAC_URL or
                git(sub, "status", "--porcelain=v1", "--untracked-files=all")):
            raise ValueError("OAC checkout commit, origin, or clean state differs")
        status = git(root, "submodule", "status", "--", "config/oac").decode()
        if not status.startswith(" " + sha + " "):
            raise ValueError("OAC is not initialized at the expected revision in the parent repository")
        if subprocess.run(["/usr/bin/git", "-C", str(sub), "symbolic-ref", "-q", "HEAD"],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode != 1:
            raise ValueError("OAC checkout must be detached")
        # No recursive submodule fetching is allowed.
        for entry in filter(None, git(sub, "ls-tree", "-rz", "HEAD").split(b"\0")):
            header, path = entry.split(b"\t", 1)
            if header.startswith(b"160000 ") or path == b".gitmodules":
                raise ValueError("nested OAC submodules are unsupported")
    return sha


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("checkout", type=Path)
    parser.add_argument("--populated", action="store_true")
    args = parser.parse_args()
    try:
        print(validate(args.checkout, args.populated))
    except (ValueError, OSError, subprocess.CalledProcessError):
        print("error: OpenPMIx OAC metadata or checkout validation failed", file=sys.stderr)
        return 7
    return 0


if __name__ == "__main__":
    sys.exit(main())
