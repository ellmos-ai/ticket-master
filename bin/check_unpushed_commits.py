#!/usr/bin/env python3
"""Reports local commits that never reached any remote, across every clone
under a repos root.

T-20260903-623938888: 28 of 174 clones under C:\\_Local_DEV\\repos carried
commits reachable only from local branches -- 8 of them on the mainline
branch (main/master), one over a month old. Nothing flagged this before a
ticket-master run stumbled onto it by accident. This is the repeatable,
reporting-only version of that manual sweep.

Uses ``git log --branches --not --remotes`` per clone -- fast, but reads
whatever the last ``git fetch`` knew about the remote. Pass ``--fetch`` for
an accurate reading (slower, needs network); without it, a commit already
pushed from elsewhere can still be reported as unpushed.

Never pushes, merges, or changes anything -- read-only, like
check_onedrive_mirror.py next to it.

Usage:
    python bin/check_unpushed_commits.py <repos-root> [--fetch]
    python bin/check_unpushed_commits.py  # uses $TICKET_MASTER_REPOS_ROOT
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

MAINLINE_BRANCHES = {"main", "master"}


def _run(args: list[str], cwd: Path) -> str:
    return subprocess.run(
        args, cwd=cwd, capture_output=True, text=True, check=False,
    ).stdout


def _local_branches(repo: Path) -> list[str]:
    out = _run(["git", "for-each-ref", "--format=%(refname:short)", "refs/heads/"], repo)
    return [line for line in out.splitlines() if line]


def find_unpushed(repos_root: Path, *, fetch: bool) -> list[tuple[str, str, int]]:
    """Returns (repo_name, branch, unpushed_commit_count) for every LOCAL
    branch with at least one commit reachable only from that branch -- not
    just the checked-out one. A repo can carry unpushed work on a side
    branch while its checked-out mainline branch is fully in sync (seen on
    ``gardener`` while building this: 3 stray local branches, ``master``
    clean)."""
    results = []
    for entry in sorted(repos_root.iterdir()):
        if not (entry / ".git").exists():
            continue
        if fetch:
            subprocess.run(["git", "fetch", "--quiet"], cwd=entry, check=False)
        for branch in _local_branches(entry):
            count = int(_run(
                ["git", "rev-list", "--count", branch, "--not", "--remotes"], entry,
            ).strip() or 0)
            if count:
                results.append((entry.name, branch, count))
    return results


def main(argv: list[str]) -> int:
    fetch = "--fetch" in argv
    argv = [a for a in argv if a != "--fetch"]
    root_arg = argv[0] if argv else os.environ.get("TICKET_MASTER_REPOS_ROOT")
    if not root_arg:
        print("usage: check_unpushed_commits.py <repos-root> [--fetch]", file=sys.stderr)
        return 2
    repos_root = Path(root_arg)
    if not repos_root.is_dir():
        print(f"not a directory: {repos_root}", file=sys.stderr)
        return 2

    results = find_unpushed(repos_root, fetch=fetch)
    if not results:
        print("No unpushed commits found in any clone.")
        return 0

    mainline = [r for r in results if r[1] in MAINLINE_BRANCHES]
    other = [r for r in results if r[1] not in MAINLINE_BRANCHES]
    print(f"{len(results)} clone(s) with unpushed commits "
          f"({len(mainline)} on mainline, {len(other)} on other branches)"
          f"{'' if fetch else ' -- without --fetch, this is a lower bound'}:")
    for name, branch, count in sorted(mainline, key=lambda r: -r[2]):
        print(f"  MAINLINE  {name:45s} {count:3d}  {branch}")
    for name, branch, count in sorted(other, key=lambda r: -r[2]):
        print(f"  other     {name:45s} {count:3d}  {branch}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
