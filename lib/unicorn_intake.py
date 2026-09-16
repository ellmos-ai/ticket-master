"""Small, transport-neutral Unicorn intake for ticket-master.

Unicorn is deliberately a one-way entry point.  Web and tray clients may ask
for a preview and may submit the same public request again with the same
request id, but they cannot provide worker, task, claim, model, lock, or
transport controls.  The canonical ticket writer remains the only component
that draws an ID and creates the ticket file.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

try:  # package import (``from lib import unicorn_intake``)
    from . import routing_contract as _routing
    from . import ticket_writer as _writer
except ImportError:  # direct module import with ``lib`` on sys.path
    import routing_contract as _routing
    import ticket_writer as _writer


PREVIEW_SCHEMA = "ellmos.unicorn.intake-preview.v1"
RECEIPT_SCHEMA = "ellmos.unicorn.intake-receipt.v1"
ROUTE_INTENT_SCHEMA = "ellmos.ticket.route-intent.v1"

_PUBLIC_FIELDS = frozenset(
    {"title", "body", "project", "priority", "pipeline", "request_id"}
)
_TICKET_ID_RE = re.compile(r"^T-\d{8}-\d+$")
_HASH_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_MAX_TITLE = 120
_MAX_BODY = 100_000
_MAX_SCALAR = 120
_MAX_REQUEST_ID = 256


class IntakeValidationError(ValueError):
    """The public Unicorn request or route-intent receipt is invalid."""


@dataclass(frozen=True)
class IntakeRequest:
    """Normalised public fields; internal routing controls are not included."""

    title: str
    body: str
    project: str | None
    priority: str
    pipeline: str
    intake_idempotency_key: str


def _text(
    value: Any,
    name: str,
    *,
    maximum: int,
    multiline: bool = False,
    optional: bool = False,
) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str) or not value.strip():
        raise IntakeValidationError(f"{name} must be a non-empty string")
    if "\x00" in value:
        raise IntakeValidationError(f"{name} contains NUL")
    if not multiline and ("\r" in value or "\n" in value):
        raise IntakeValidationError(f"{name} must be single-line")
    normalised = value.replace("\r\n", "\n").replace("\r", "\n").strip()
    if len(normalised) > maximum:
        raise IntakeValidationError(f"{name} exceeds {maximum} characters")
    return normalised


def _request(payload: Mapping[str, Any]) -> IntakeRequest:
    if not isinstance(payload, Mapping):
        raise IntakeValidationError("Unicorn intake must be a JSON object")
    unknown = sorted(set(payload) - _PUBLIC_FIELDS)
    if unknown:
        raise IntakeValidationError(
            "unsupported Unicorn fields: " + ", ".join(map(str, unknown))
        )

    title = _text(payload.get("title"), "title", maximum=_MAX_TITLE)
    body = _text(payload.get("body"), "body", maximum=_MAX_BODY, multiline=True)
    project = _text(
        payload.get("project"), "project", maximum=_MAX_SCALAR, optional=True,
    )
    priority = _text(
        payload.get("priority", "mittel"), "priority", maximum=_MAX_SCALAR,
    )
    pipeline = _text(
        payload.get("pipeline", "<offen>"), "pipeline", maximum=_MAX_SCALAR,
    )
    request_id = _text(
        payload.get("request_id"), "request_id", maximum=_MAX_REQUEST_ID,
        optional=True,
    )

    canonical = {
        "title": title,
        "body": body,
        "project": project,
        "priority": priority,
        "pipeline": pipeline,
    }
    # A caller-provided request id makes retries stable even when a transport
    # reserialises the body.  Without one, the normalised public request itself
    # is the retry identity.  The key never contains user text.
    identity = {"request_id": request_id} if request_id else {"request": canonical}
    key = "sha256:" + hashlib.sha256(
        json.dumps(identity, ensure_ascii=False, sort_keys=True,
                   separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return IntakeRequest(title, body, project, priority, pipeline, key)


def preview_intake(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Validate and return the user-visible preview without writing anything."""
    request = _request(payload)
    return {
        "schema": PREVIEW_SCHEMA,
        "title": request.title,
        "body": request.body,
        "project": request.project,
        "priority": request.priority,
        "pipeline": request.pipeline,
        "intake_idempotency_key": request.intake_idempotency_key,
        "route_intent_schema": ROUTE_INTENT_SCHEMA,
    }


def validate_route_intent(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Validate the minimal route-intent boundary and return a safe copy."""
    if not isinstance(payload, Mapping):
        raise IntakeValidationError("route-intent must be a JSON object")
    expected = {"route_intent", "ticket_id", "target_snapshot", "receipt_to", "idempotency_key"}
    if set(payload) != expected:
        raise IntakeValidationError("route-intent fields do not match v1")
    if payload["route_intent"] != ROUTE_INTENT_SCHEMA:
        raise IntakeValidationError("unsupported route-intent schema")
    if not isinstance(payload["ticket_id"], str) or not _TICKET_ID_RE.fullmatch(payload["ticket_id"]):
        raise IntakeValidationError("route-intent ticket_id is invalid")
    if not isinstance(payload["receipt_to"], str) or not _TICKET_ID_RE.fullmatch(payload["receipt_to"]):
        raise IntakeValidationError("route-intent receipt_to is invalid")
    if not isinstance(payload["idempotency_key"], str) or not _HASH_RE.fullmatch(payload["idempotency_key"]):
        raise IntakeValidationError("route-intent idempotency_key is invalid")

    snapshot = payload["target_snapshot"]
    if not isinstance(snapshot, Mapping):
        raise IntakeValidationError("route-intent target_snapshot is invalid")
    if set(snapshot) != {"kind", "systems", "at", "source", "fingerprint"}:
        raise IntakeValidationError("route-intent target_snapshot fields do not match v1")
    if (
        not isinstance(snapshot["kind"], str)
        or snapshot["kind"] not in _routing.TARGET_KINDS
    ):
        raise IntakeValidationError("route-intent target kind is invalid")
    systems = snapshot["systems"]
    if (
        not isinstance(systems, list)
        or any(not isinstance(system, str) or not system for system in systems)
        or len(systems) != len(set(systems))
    ):
        raise IntakeValidationError("route-intent target systems are invalid")
    if snapshot["kind"] != "any" and not systems:
        raise IntakeValidationError("route-intent target systems are empty")
    for name in ("at", "source", "fingerprint"):
        if not isinstance(snapshot[name], str) or not snapshot[name].strip():
            raise IntakeValidationError(f"route-intent target snapshot {name} is invalid")
    if not _HASH_RE.fullmatch(snapshot["fingerprint"]):
        raise IntakeValidationError("route-intent target fingerprint is invalid")

    return {
        "route_intent": payload["route_intent"],
        "ticket_id": payload["ticket_id"],
        "target_snapshot": {
            "kind": snapshot["kind"],
            "systems": list(systems),
            "at": snapshot["at"],
            "source": snapshot["source"],
            "fingerprint": snapshot["fingerprint"],
        },
        "receipt_to": payload["receipt_to"],
        "idempotency_key": payload["idempotency_key"],
    }


def submit_intake(
    payload: Mapping[str, Any],
    *,
    tickets_dir: Path | str,
    registry_snapshot: Mapping[str, Any],
    resolver: Callable[..., Any] | None = None,
    today: str | None = None,
    rng: Any = None,
    session: str | None = None,
    session_agent: str | None = None,
    session_host: str | None = None,
) -> dict[str, Any]:
    """Atomically create or retry one public intake and return only its receipt."""
    request = _request(payload)
    path = _writer.create_routed_ticket(
        request.title,
        request.body,
        tickets_dir=tickets_dir,
        registry_snapshot=registry_snapshot,
        ticket_kind="normal",
        target_kind="any",
        idempotency_key=request.intake_idempotency_key,
        project=request.project,
        priority=request.priority,
        pipeline=request.pipeline,
        resolver=resolver,
        today=today,
        rng=rng,
        session=session,
        session_agent=session_agent,
        session_host=session_host,
    )
    intent = validate_route_intent(
        _routing.build_route_intent(_routing.load_contract(path))
    )
    return {
        "schema": RECEIPT_SCHEMA,
        "ticket_id": intent["ticket_id"],
        "intake_idempotency_key": request.intake_idempotency_key,
        "route_intent": intent,
    }


__all__ = [
    "IntakeRequest",
    "IntakeValidationError",
    "PREVIEW_SCHEMA",
    "RECEIPT_SCHEMA",
    "ROUTE_INTENT_SCHEMA",
    "preview_intake",
    "submit_intake",
    "validate_route_intent",
]
