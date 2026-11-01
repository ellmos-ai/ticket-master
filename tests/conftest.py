from pathlib import Path

import pytest


def pytest_configure(config: pytest.Config) -> None:
    root = Path(config.rootpath)
    # addopts sets a relative --basetemp, so a run inside the git-less cloud mirror writes .pytest_temp into the sync tree.
    if (root / "REPO.pointer.json").is_file() and not (root / ".git").exists():
        raise pytest.UsageError(
            f"{root} is a git-less mirror (REPO.pointer.json, no .git). "
            "Run the tests in the canonical clone instead (see REPO.pointer.json)."
        )
