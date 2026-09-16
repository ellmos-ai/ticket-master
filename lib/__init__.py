"""Public ticket-master library API.

The repository also ships prompts and workflow assets, while this package is
the stable programmatic boundary used by integrations such as
system-gap-master. Callers create IDs only through :func:`create_routed_ticket`
and mutate schema-v2 contracts only through the lease-aware routing helpers.
"""

from .routing_contract import (
    build_route_intent,
    claim_contract,
    complete_contract,
    load_contract,
    record_receipt,
    release_contract,
)
from .ticket_writer import create, create_routed_ticket
from .unicorn_intake import (
    IntakeValidationError,
    preview_intake,
    submit_intake,
    validate_route_intent,
)
from .unicorn_adapter import (
    AdapterResponse,
    render_form,
    tray_preview,
    tray_submit,
    web_dispatch,
    web_preview,
    web_submit,
)

__all__ = [
    "build_route_intent",
    "claim_contract",
    "complete_contract",
    "create",
    "create_routed_ticket",
    "IntakeValidationError",
    "AdapterResponse",
    "load_contract",
    "preview_intake",
    "render_form",
    "record_receipt",
    "release_contract",
    "submit_intake",
    "tray_preview",
    "tray_submit",
    "validate_route_intent",
    "web_dispatch",
    "web_preview",
    "web_submit",
]
