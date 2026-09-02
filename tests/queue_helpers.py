"""Test-only helpers for explicit ticket-queue setup."""

from pathlib import Path


def verified_queue(path: str | Path) -> Path:
    """Create the smallest intentional queue root accepted by writers."""
    root = Path(path)
    root.mkdir(parents=True, exist_ok=True)
    (root / ".ticket-master-queue").write_text(
        "ticket-master-queue-v1\n", encoding="utf-8",
    )
    return root
