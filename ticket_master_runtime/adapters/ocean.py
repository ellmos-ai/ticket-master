"""OCEAN adapter surface for the shared read-only ticket inventory engine."""

from ..inventory import capture_inventory as _capture_inventory


def capture_inventory(queue, *, include_content: bool = False):
    """Capture through the shared inventory engine without task/database access."""
    return _capture_inventory(queue, include_content=include_content)
