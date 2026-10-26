"""BACH adapter surface; the host CLI binding remains a separate integration gate."""

from ..inventory import capture_inventory as _capture_inventory


def capture_inventory(queue, *, include_content: bool = False):
    """Capture through the shared inventory engine without BACH storage access."""
    return _capture_inventory(queue, include_content=include_content)
