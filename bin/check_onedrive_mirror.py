#!/usr/bin/env python3
"""Compares this Plan-D clone against a gitless OneDrive mirror.

T-20260902-534538840: a feature (fail-closed queue-root validation) lived
only in the OneDrive projection for two days, invisible to every "tests
green" claim made from the clone, because nothing ever compared the two
trees. Run this before trusting a clone-side test result to also hold for
the deployed mirror, and again right after a deploy.

Compares every ``git ls-files`` entry, CRLF-normalized (OneDrive/Windows line
endings must not read as content drift). Skips ``config/*.json`` (the
gitignored, host-local config Plan D says belongs only in OneDrive -- its
``*.example.json`` templates ARE tracked and ARE compared) and anything under
``__pycache__``/``.pytest_cache``/``.ruff_cache``/``dist``.

Usage:
    python bin/check_onedrive_mirror.py <path-to-onedrive-module-copy>
    python bin/check_onedrive_mirror.py  # uses $TICKET_MASTER_ONEDRIVE_MIRROR

Exit 0: mirror matches (or a listed file is deploy-only and absent, which is
fine). Exit 1: content differs, or a tracked file the clone has is missing
from the mirror -- the direction of the queue-root bug.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SKIP_DIR_PARTS = {"__pycache__", ".pytest_cache", ".ruff_cache", "dist"}


def _tracked_files() -> list[str]:
    out = subprocess.run(
        ["git", "ls-files"], cwd=REPO_ROOT, capture_output=True, text=True, check=True,
    )
    return [line for line in out.stdout.splitlines() if line]


def _is_local_config(rel_path: str) -> bool:
    parts = Path(rel_path).parts
    return (
        len(parts) == 2 and parts[0] == "config"
        and parts[1].endswith(".json") and not parts[1].endswith(".example.json")
    )


def find_diffs(mirror_root: Path) -> list[str]:
    diffs = []
    for rel_path in _tracked_files():
        if any(part in SKIP_DIR_PARTS for part in Path(rel_path).parts):
            continue
        if _is_local_config(rel_path):
            continue
        clone_file = REPO_ROOT / rel_path
        mirror_file = mirror_root / rel_path
        if not mirror_file.is_file():
            diffs.append(f"MISSING IN MIRROR: {rel_path}")
            continue
        clone_bytes = clone_file.read_bytes().replace(b"\r\n", b"\n")
        mirror_bytes = mirror_file.read_bytes().replace(b"\r\n", b"\n")
        if clone_bytes != mirror_bytes:
            diffs.append(f"DIFFERS: {rel_path}")
    return diffs


def main(argv: list[str]) -> int:
    mirror_arg = argv[0] if argv else os.environ.get("TICKET_MASTER_ONEDRIVE_MIRROR")
    if not mirror_arg:
        print("usage: check_onedrive_mirror.py <onedrive-module-path>", file=sys.stderr)
        return 2
    mirror_root = Path(mirror_arg)
    if not mirror_root.is_dir():
        print(f"not a directory: {mirror_root}", file=sys.stderr)
        return 2

    diffs = find_diffs(mirror_root)
    if not diffs:
        print("OneDrive mirror matches the clone (tracked files, config/*.json excluded).")
        return 0
    print(f"{len(diffs)} file(s) diverged between clone and OneDrive mirror:")
    for diff in diffs:
        print(f"  {diff}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
