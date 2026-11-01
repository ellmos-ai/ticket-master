import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

_spec = importlib.util.spec_from_file_location("_mirror_guard_conftest", Path(__file__).with_name("conftest.py"))
_conftest = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_conftest)


def _configure(root: Path) -> None:
    _conftest.pytest_configure(SimpleNamespace(rootpath=root))


def test_refuses_gitless_mirror(tmp_path):
    (tmp_path / "REPO.pointer.json").write_text("{}", encoding="utf-8")
    with pytest.raises(pytest.UsageError, match="git-less mirror"):
        _configure(tmp_path)


def test_allows_clone_with_git_dir(tmp_path):
    (tmp_path / "REPO.pointer.json").write_text("{}", encoding="utf-8")
    (tmp_path / ".git").mkdir()
    _configure(tmp_path)


def test_allows_worktree_with_git_file(tmp_path):
    (tmp_path / "REPO.pointer.json").write_text("{}", encoding="utf-8")
    (tmp_path / ".git").write_text("gitdir: elsewhere", encoding="utf-8")
    _configure(tmp_path)


def test_allows_tree_without_pointer(tmp_path):
    _configure(tmp_path)
