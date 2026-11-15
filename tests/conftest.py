import sys
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


@pytest.fixture(autouse=True)
def _no_live_bach_lease(monkeypatch):
    """Tests never read the host's real bach_lease config (it may be enabled and point at a live lead)."""
    for name in ("bach_lease", "lib.bach_lease"):
        module = sys.modules.get(name)
        if module is not None:
            monkeypatch.setattr(
                module, "load_config",
                lambda config=None, _orig=module.load_config: _orig({} if config is None else config),
            )
