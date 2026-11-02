"""Public inventory API, backed by the single canonical module implementation."""

from lib.ticket_inventory import capture_inventory

__all__ = ["capture_inventory"]
