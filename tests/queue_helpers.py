"""Test-only helpers for explicit ticket-queue setup."""

import importlib.util
import tempfile
from pathlib import Path


def verified_queue(path: str | Path) -> Path:
    """Create the smallest intentional queue root accepted by writers."""
    root = Path(path)
    root.mkdir(parents=True, exist_ok=True)
    (root / ".ticket-master-queue").write_text(
        "ticket-master-queue-v1\n", encoding="utf-8",
    )
    return root


class QueueTemporaryDirectory(tempfile.TemporaryDirectory):
    """A TemporaryDirectory that is already an intentional queue root (v1 marker).

    Writers that change an existing ticket refuse any root without a valid marker (T-20261107-165954775), so
    tests that build their tickets straight below a temp folder say so explicitly by using this class.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        verified_queue(self.name)


def write_file(path: str | Path, text: str) -> Path:
    """Write *text* (creating parents) and return the path."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def load_cli(name: str = "ticket_master_cli_under_test"):
    """Import bin/ticket_master.py as a fresh module."""
    path = Path(__file__).resolve().parents[1] / "bin" / "ticket_master.py"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
