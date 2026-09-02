"""Smoke test for bin/check_unpushed_commits.py: builds two throwaway git
repos (bare 'remote' + clone) with one local-only commit, confirms it is
found on the mainline branch and disappears once pushed."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

BIN = Path(__file__).resolve().parent.parent / "bin"
sys.path.insert(0, str(BIN))
import check_unpushed_commits as m  # noqa: E402


def _git(args: list[str], cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def test_finds_and_clears_mainline_unpushed_commit(tmp_path: Path) -> None:
    remote = tmp_path / "remote.git"
    _git(["init", "--bare", "-b", "main", str(remote)], tmp_path)

    clone = tmp_path / "clone"
    _git(["clone", str(remote), str(clone)], tmp_path)
    _git(["config", "user.email", "t@example.com"], clone)
    _git(["config", "user.name", "t"], clone)
    (clone / "f.txt").write_text("x")
    _git(["add", "f.txt"], clone)
    _git(["commit", "-m", "local only"], clone)

    # tmp_path itself is the "repos root": it contains exactly one .git dir
    # (clone/) plus the bare remote (remote.git, correctly skipped: no
    # working tree, so no "--not --remotes" divergence to report either).
    results = m.find_unpushed(tmp_path, fetch=False)
    assert results == [("clone", "main", 1)]

    _git(["push", "origin", "main"], clone)
    assert m.find_unpushed(tmp_path, fetch=False) == []


if __name__ == "__main__":
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        test_finds_and_clears_mainline_unpushed_commit(Path(d))
    print("OK")
