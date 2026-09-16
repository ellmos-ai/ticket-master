"""Local, pointer-only Trithon projection for shadow-mode experiments.

This module is deliberately a projection boundary, not another task or ticket
system.  It reads explicit JSON source documents, stores only the small
allow-listed metadata needed to reconstruct a local task view, and never opens
or mutates a ticket file.  The database is host-local and must be supplied
explicitly by the caller; no production path, process launcher, Ollama client,
or network transport is hidden here.

The public contract is ``trithon.shadow.v1``.  A source document may contain a
single ``route_intent`` object, a Phase-0 task-projection or outcome-receipt
envelope, or ``route_intents``/``receipts`` arrays.  Canonical source payloads
are fingerprinted, and a changed stable document at the same URI fails closed
until the caller explicitly performs a rebuild. Delivery-only retries use the
stable contract idempotency key and remain replayable.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence


SHADOW_SCHEMA = "trithon.shadow.v1"
ROUTE_INTENT_SCHEMA = "ellmos.ticket.route-intent.v1"
TASK_PROJECTION_SCHEMA = "ellmos.trithon.task-projection.v1"
OUTCOME_RECEIPT_SCHEMA = "ellmos.trithon.outcome-receipt.v1"
SHADOW_SCHEMA_VERSION = 2
_TICKET_ID_RE = re.compile(r"^T-\d{8}-\d+$")
_SAFE_SCALAR_RE = re.compile(r"^[^\x00\r\n]{1,512}$")
_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_PROJECTION_ID_RE = re.compile(r"^proj-[a-f0-9]{32}$")
_TASK_CONTRACT_ID_RE = re.compile(r"^trithon-task:[A-Za-z0-9][A-Za-z0-9:._-]{8,191}$")
_EVENT_ID_RE = re.compile(r"^evt-[a-f0-9]{32}$")
_PUBLISHER_ID_RE = re.compile(r"^(?:ticket-master|trithon)@[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_DISPATCH_ID_RE = re.compile(r"^dsp-[a-f0-9]{32}$")
_ASSIGNMENT_ID_RE = re.compile(r"^asgn-[a-f0-9]{32}$")
_RECEIPT_ID_RE = re.compile(r"^rcpt-[a-f0-9]{32}$")
_EVIDENCE_REF_RE = re.compile(r"^urn:[A-Za-z0-9][A-Za-z0-9:._-]{1,255}$")
_WINDOWS_PATH_RE = re.compile(r"(?:^[A-Za-z]:[\\/]|^\\\\|(?:^|[\s(])[A-Za-z]:[\\/])")
_POSIX_PATH_RE = re.compile(r"(?:^|[\s(])/[^\s)]+")
_PARENT_PATH_RE = re.compile(r"(?:^|[\\/])\.\.(?:[\\/]|$)")
_SECRET_VALUE_RE = re.compile(
    r"(?i)(?:\b(?:api[_-]?key|password|bearer)\s*[:=]|-----BEGIN .*PRIVATE KEY-----|\bsk-[A-Za-z0-9_-]{16,}\b)"
)
_FORBIDDEN_KEYS = {
    "body",
    "content",
    "description",
    "full_text",
    "fulltext",
    "originaltext",
    "password",
    "prompt",
    "secret",
    "token",
}
_FORBIDDEN_KEY_FRAGMENTS = (
    "body",
    "credential",
    "content",
    "fulltext",
    "originaltext",
    "password",
    "prompt",
    "rawtext",
    "secret",
    "token",
    "transcript",
)
_POINTER_URI_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:[^\s]+$")
_ROUTE_KEYS = {
    "route_intent",
    "ticket_id",
    "task_id",
    "target_snapshot",
    "receipt_to",
    "idempotency_key",
    "source_revision",
    "requested_capability",
    "priority",
}
_TARGET_KEYS = {"kind", "systems", "at", "source", "source_ref", "fingerprint"}
_RECEIPT_KEYS = {
    "ticket_id",
    "task_id",
    "signature",
    "status",
    "executed_by",
    "actual_provider",
    "actual_model",
    "occurred_at",
    "evidence",
    "source_revision",
}
_PROJECTION_KEYS = {
    "contract",
    "projection_id",
    "source",
    "task",
    "target_snapshot",
    "receipt_to",
    "delivery",
    "idempotency_key",
}
_PROJECTION_SOURCE_KEYS = {
    "route_intent",
    "ticket_id",
    "ticket_revision",
    "route_intent_idempotency_key",
    "source_status",
}
_PROJECTION_TASK_KEYS = {"task_id", "capability", "priority", "task_state"}
_DELIVERY_KEYS = {
    "event_id",
    "publisher_id",
    "publisher_epoch",
    "sequence",
    "attempt",
    "emitted_at",
}
_OUTCOME_KEYS = {"status", "result_code", "reason_code", "proposed_ticket_status"}
_OUTCOME_RECEIPT_KEYS = {
    "contract",
    "receipt_id",
    "dispatch_id",
    "assignment_id",
    "task_ref",
    "outcome",
    "evidence",
    "delivery",
    "idempotency_key",
}
_OUTCOME_TASK_REF_KEYS = {
    "projection_id",
    "task_id",
    "ticket_id",
    "ticket_revision",
    "receipt_to",
}
_EVIDENCE_KEYS = {"kind", "ref", "digest"}
_EVIDENCE_KINDS = {"test_report", "commit", "artifact", "review", "runtime_receipt"}
_STATUSES = {"done", "blocked", "released", "waiting"}
_TARGET_KINDS = {"any", "all", "grouped", "exact"}
_OUTCOME_STATUS_MAP = {
    ("completed", "task_done", "none", "SOLVED"): "done",
    ("released", "not_finished", "none", "ACTIONABLE"): "released",
    ("interrupted", "keyboard_interrupt", "operator_interrupt", "ACTIONABLE"): "released",
    ("error", "runtime_error", "executor_error", "WAITING"): "waiting",
    ("error", "runtime_error", "budget_exhausted", "WAITING"): "waiting",
    ("error", "runtime_error", "local_lock_error", "BLOCKED"): "blocked",
    ("error", "completion_mark_failed", "completion_write_failed", "BLOCKED"): "blocked",
}


class ShadowStoreError(ValueError):
    """Base error for a shadow store operation."""


class InvalidSourceError(ShadowStoreError):
    """A source is incomplete, contains disallowed data, or has bad schema."""


class SourceConflictError(ShadowStoreError):
    """A source or idempotency key changed underneath an existing projection."""


class CorruptStoreError(ShadowStoreError):
    """The local SQLite store failed its integrity gate."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _scalar(value: Any, *, field: str, required: bool = True) -> str | None:
    if value is None:
        if required:
            raise InvalidSourceError(f"{field} is required")
        return None
    if not isinstance(value, str) or not _SAFE_SCALAR_RE.fullmatch(value):
        raise InvalidSourceError(f"{field} must be one non-empty line of at most 512 characters")
    return value


def _assert_pointer_only(value: Any, *, path: str = "source") -> None:
    """Reject common raw-text and credential fields recursively."""

    if isinstance(value, Mapping):
        for key, child in value.items():
            if not isinstance(key, str):
                raise InvalidSourceError(f"{path} contains a non-string key")
            normalized_key = key.lower().replace("-", "_")
            compact_key = normalized_key.replace("_", "")
            if normalized_key in _FORBIDDEN_KEYS or any(
                fragment in compact_key for fragment in _FORBIDDEN_KEY_FRAGMENTS
            ):
                raise InvalidSourceError(f"{path}.{key} is not allowed in a pointer-only source")
            _assert_pointer_only(child, path=f"{path}.{key}")
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for index, child in enumerate(value):
            _assert_pointer_only(child, path=f"{path}[{index}]")
    elif isinstance(value, str) and (
        _WINDOWS_PATH_RE.search(value)
        or _POSIX_PATH_RE.search(value)
        or _PARENT_PATH_RE.search(value)
        or _SECRET_VALUE_RE.search(value)
    ):
        raise InvalidSourceError(f"{path} contains a path or secret value not allowed in a pointer-only source")


def _validate_keys(value: Mapping[str, Any], allowed: set[str], *, field: str) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise InvalidSourceError(f"{field} contains unsupported fields: {', '.join(unknown)}")


def _digest(value: Any, *, field: str) -> str:
    digest = _scalar(value, field=field)
    if digest is None or not _SHA256_RE.fullmatch(digest):
        raise InvalidSourceError(f"{field} must use sha256:<64 lowercase hex characters>")
    return digest


def _timestamp(value: Any, *, field: str) -> str:
    timestamp = _scalar(value, field=field)
    if timestamp is None:
        raise InvalidSourceError(f"{field} is required")
    try:
        parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError as exc:
        raise InvalidSourceError(f"{field} must be an RFC 3339 timestamp") from exc
    if parsed.tzinfo is None:
        raise InvalidSourceError(f"{field} must include a timezone")
    return timestamp


def _normalise_delivery(value: Any, *, publisher_prefix: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise InvalidSourceError("delivery must be an object")
    _validate_keys(value, _DELIVERY_KEYS, field="delivery")
    event_id = _scalar(value.get("event_id"), field="delivery.event_id")
    if event_id is None or not _EVENT_ID_RE.fullmatch(event_id):
        raise InvalidSourceError("delivery.event_id is not valid")
    publisher_id = _scalar(value.get("publisher_id"), field="delivery.publisher_id")
    if publisher_id is None or not _PUBLISHER_ID_RE.fullmatch(publisher_id):
        raise InvalidSourceError("delivery.publisher_id is not valid")
    if not publisher_id.startswith(publisher_prefix + "@"):
        raise InvalidSourceError(f"delivery.publisher_id must start with {publisher_prefix}@")
    publisher_epoch = _scalar(value.get("publisher_epoch"), field="delivery.publisher_epoch")
    if publisher_epoch is None or not re.fullmatch(r"epoch-[A-Za-z0-9][A-Za-z0-9._-]{0,63}", publisher_epoch):
        raise InvalidSourceError("delivery.publisher_epoch is not valid")
    sequence = value.get("sequence")
    attempt = value.get("attempt")
    if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 1:
        raise InvalidSourceError("delivery.sequence must be a positive integer")
    if isinstance(attempt, bool) or not isinstance(attempt, int) or not 1 <= attempt <= 100:
        raise InvalidSourceError("delivery.attempt must be an integer from 1 to 100")
    emitted_at = _timestamp(value.get("emitted_at"), field="delivery.emitted_at")
    return {
        "event_id": event_id,
        "publisher_id": publisher_id,
        "publisher_epoch": publisher_epoch,
        "sequence": sequence,
        "attempt": attempt,
        "emitted_at": emitted_at,
    }


def _stable_source_payload(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    """Remove delivery-only data from the two closed contract envelopes."""

    if payload.get("contract") not in {TASK_PROJECTION_SCHEMA, OUTCOME_RECEIPT_SCHEMA}:
        return payload
    stable = dict(payload)
    stable.pop("delivery", None)
    return stable


def _checkpoint_fingerprint(document: SourceDocument) -> str:
    stable_payload = _stable_source_payload(document.payload)
    if stable_payload is document.payload:
        return document.digest
    return _sha256(_canonical(stable_payload).encode("utf-8"))


def _normalised_equivalent(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    """Compare stable records while ignoring their source transport envelope."""

    ignored = set()
    if left.get("projection_id") is not None and right.get("projection_id") is not None:
        ignored.update({"delivery", "source_uri", "source_sha256"})
    if left.get("receipt_idempotency_key") is not None and right.get("receipt_idempotency_key") is not None:
        ignored.update({"delivery", "occurred_at", "source_uri", "source_sha256"})
    return _canonical({key: value for key, value in left.items() if key not in ignored}) == _canonical(
        {key: value for key, value in right.items() if key not in ignored}
    )


@dataclass(frozen=True)
class SourceDocument:
    """An explicit, already-fingerprinted JSON source document."""

    uri: str
    digest: str
    payload: Mapping[str, Any]

    @classmethod
    def from_payload(cls, uri: str, payload: Mapping[str, Any]) -> "SourceDocument":
        uri_value = _scalar(uri, field="source uri")
        if not isinstance(payload, Mapping):
            raise InvalidSourceError("source payload must be a JSON object")
        _assert_pointer_only(payload)
        encoded = (_canonical(payload) + "\n").encode("utf-8")
        return cls(uri_value or "", _sha256(encoded), dict(payload))


def load_source(path: str | Path) -> SourceDocument:
    """Read one JSON source without writing or interpreting ticket contents."""

    source_path = Path(path).expanduser().resolve()
    try:
        raw = source_path.read_bytes()
    except OSError as exc:
        raise InvalidSourceError(f"cannot read source {source_path}: {exc}") from exc
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InvalidSourceError(f"source is not valid UTF-8 JSON: {source_path}") from exc
    if not isinstance(payload, Mapping):
        raise InvalidSourceError("source root must be a JSON object")
    _assert_pointer_only(payload)
    canonical = (_canonical(payload) + "\n").encode("utf-8")
    return SourceDocument(source_path.as_uri(), _sha256(canonical), dict(payload))


def _route_records(document: SourceDocument) -> list[Mapping[str, Any]]:
    payload = document.payload
    if "route_intent" in payload and isinstance(payload.get("route_intent"), Mapping):
        return [payload["route_intent"]]  # type: ignore[list-item]
    if payload.get("route_intent") == ROUTE_INTENT_SCHEMA and "ticket_id" in payload:
        return [payload]
    value = payload.get("route_intents", [])
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, Mapping) for item in value):
        raise InvalidSourceError(f"{document.uri}: route_intents must be an array of objects")
    return list(value)  # type: ignore[return-value]


def _projection_records(document: SourceDocument) -> list[Mapping[str, Any]]:
    payload = document.payload
    if payload.get("contract") == TASK_PROJECTION_SCHEMA:
        return [payload]
    value = payload.get("task_projections", [])
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, Mapping) for item in value):
        raise InvalidSourceError(f"{document.uri}: task_projections must be an array of objects")
    return list(value)  # type: ignore[return-value]


def _receipt_records(document: SourceDocument) -> list[Mapping[str, Any]]:
    payload = document.payload
    if payload.get("contract") == OUTCOME_RECEIPT_SCHEMA:
        return [payload]
    value = payload.get("receipts", [])
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, Mapping) for item in value):
        raise InvalidSourceError(f"{document.uri}: receipts must be an array of objects")
    return list(value)  # type: ignore[return-value]


def _normalise_target(
    value: Any, *, strict_registry_ref: bool = False
) -> tuple[str, list[str], str | None]:
    if not isinstance(value, Mapping):
        raise InvalidSourceError("target_snapshot must be an object")
    _validate_keys(value, _TARGET_KEYS, field="target_snapshot")
    kind = _scalar(value.get("kind"), field="target_snapshot.kind")
    if kind not in _TARGET_KINDS:
        raise InvalidSourceError("target_snapshot.kind is not a supported target kind")
    systems = value.get("systems", [])
    if not isinstance(systems, list) or not all(isinstance(item, str) for item in systems):
        raise InvalidSourceError("target_snapshot.systems must be an array of strings")
    if len(systems) > 64 or len(systems) != len(set(systems)):
        raise InvalidSourceError("target_snapshot.systems must contain at most 64 unique systems")
    clean_systems: list[str] = []
    for item in systems:
        clean = _scalar(item, field="target_snapshot.system")
        if not clean or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", clean):
            raise InvalidSourceError("target_snapshot.system must be a safe system identifier")
        clean_systems.append(clean)
    source_ref = value.get("source_ref", value.get("source"))
    if strict_registry_ref:
        source_value = _scalar(source_ref, field="target_snapshot.source_ref")
        if source_value is None or not re.fullmatch(
            r"(?:inventory|fixture|registry):[A-Za-z0-9][A-Za-z0-9._-]{0,191}", source_value
        ):
            raise InvalidSourceError("target_snapshot.source_ref must be a path-free registry reference")
    source_fingerprint = _scalar(value.get("fingerprint"), field="target_snapshot.fingerprint", required=False)
    return kind or "", clean_systems, source_fingerprint


def _normalise_route(document: SourceDocument, record: Mapping[str, Any]) -> dict[str, Any]:
    _assert_pointer_only(record, path=f"{document.uri}.route_intent")
    _validate_keys(record, _ROUTE_KEYS, field="route_intent")
    schema = _scalar(record.get("route_intent"), field="route_intent")
    if schema != ROUTE_INTENT_SCHEMA:
        raise InvalidSourceError(f"route_intent must be {ROUTE_INTENT_SCHEMA}")
    ticket_id = _scalar(record.get("ticket_id"), field="ticket_id")
    if not ticket_id or not _TICKET_ID_RE.fullmatch(ticket_id):
        raise InvalidSourceError("ticket_id is not a valid ticket identifier")
    idempotency_key = _scalar(record.get("idempotency_key"), field="idempotency_key")
    kind, systems, target_fingerprint = _normalise_target(record.get("target_snapshot"))
    source_revision = _scalar(record.get("source_revision"), field="source_revision", required=False)
    if source_revision is None:
        source_revision = target_fingerprint or document.digest
    task_id = _scalar(record.get("task_id"), field="task_id", required=False)
    if task_id is None:
        task_id = "trithon-task-" + hashlib.sha256((idempotency_key or "").encode("utf-8")).hexdigest()[:32]
    return {
        "task_id": task_id,
        "projection_id": None,
        "ticket_id": ticket_id,
        "source_revision": source_revision,
        "idempotency_key": idempotency_key,
        "requested_capability": _scalar(
            record.get("requested_capability"), field="requested_capability", required=False
        ),
        "priority": _scalar(record.get("priority"), field="priority", required=False),
        "target_kind": kind,
        "target_systems": systems,
        "receipt_to": _scalar(record.get("receipt_to"), field="receipt_to", required=False),
        "source_uri": document.uri,
        "source_sha256": document.digest,
    }


def _normalise_task_projection(document: SourceDocument, record: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize the closed Phase-0 task projection without retaining delivery data."""

    _assert_pointer_only(record, path=f"{document.uri}.task_projection")
    _validate_keys(record, _PROJECTION_KEYS, field="task_projection")
    contract = _scalar(record.get("contract"), field="task_projection.contract")
    if contract != TASK_PROJECTION_SCHEMA:
        raise InvalidSourceError(f"task_projection must be {TASK_PROJECTION_SCHEMA}")
    projection_id = _scalar(record.get("projection_id"), field="task_projection.projection_id")
    if projection_id is None or not _PROJECTION_ID_RE.fullmatch(projection_id):
        raise InvalidSourceError("task_projection.projection_id is not valid")

    source = record.get("source")
    if not isinstance(source, Mapping):
        raise InvalidSourceError("task_projection.source must be an object")
    _validate_keys(source, _PROJECTION_SOURCE_KEYS, field="task_projection.source")
    route_schema = _scalar(source.get("route_intent"), field="task_projection.source.route_intent")
    if route_schema != ROUTE_INTENT_SCHEMA:
        raise InvalidSourceError("task_projection.source.route_intent is not valid")
    ticket_id = _scalar(source.get("ticket_id"), field="task_projection.source.ticket_id")
    if ticket_id is None or not _TICKET_ID_RE.fullmatch(ticket_id):
        raise InvalidSourceError("task_projection.source.ticket_id is not valid")
    ticket_revision = _digest(source.get("ticket_revision"), field="task_projection.source.ticket_revision")
    route_key = _digest(
        source.get("route_intent_idempotency_key"),
        field="task_projection.source.route_intent_idempotency_key",
    )
    source_status = _scalar(source.get("source_status"), field="task_projection.source.source_status")
    if source_status != "ACTIONABLE":
        raise InvalidSourceError("task_projection.source.source_status must be ACTIONABLE")

    task = record.get("task")
    if not isinstance(task, Mapping):
        raise InvalidSourceError("task_projection.task must be an object")
    _validate_keys(task, _PROJECTION_TASK_KEYS, field="task_projection.task")
    task_id = _scalar(task.get("task_id"), field="task_projection.task.task_id")
    if task_id is None or not _TASK_CONTRACT_ID_RE.fullmatch(task_id):
        raise InvalidSourceError("task_projection.task.task_id is not valid")
    capability = _scalar(task.get("capability"), field="task_projection.task.capability")
    if capability is None or not re.fullmatch(
        r"[a-z][a-z0-9]*(?:[._-][a-z0-9]+){0,7}", capability
    ):
        raise InvalidSourceError("task_projection.task.capability is not valid")
    priority = _scalar(task.get("priority"), field="task_projection.task.priority")
    if priority not in {"low", "normal", "high", "urgent"}:
        raise InvalidSourceError("task_projection.task.priority is not valid")
    task_state = _scalar(task.get("task_state"), field="task_projection.task.task_state")
    if task_state != "ready":
        raise InvalidSourceError("task_projection.task.task_state must be ready")

    target_kind, systems, target_fingerprint = _normalise_target(
        record.get("target_snapshot"), strict_registry_ref=True
    )
    receipt_to = _scalar(record.get("receipt_to"), field="task_projection.receipt_to")
    if receipt_to is None or not _TICKET_ID_RE.fullmatch(receipt_to) or receipt_to != ticket_id:
        raise InvalidSourceError("task_projection.receipt_to must match source.ticket_id")
    delivery = _normalise_delivery(record.get("delivery"), publisher_prefix="ticket-master")

    stable = {key: value for key, value in record.items() if key not in {"delivery", "idempotency_key"}}
    idempotency_key = _digest(record.get("idempotency_key"), field="task_projection.idempotency_key")
    if idempotency_key != _sha256(_canonical(stable).encode("utf-8")):
        raise SourceConflictError("task_projection.idempotency_key does not match stable content")
    return {
        "task_id": task_id,
        "projection_id": projection_id,
        "ticket_id": ticket_id,
        "source_revision": ticket_revision,
        "idempotency_key": idempotency_key,
        "requested_capability": capability,
        "priority": priority,
        "target_kind": target_kind,
        "target_systems": systems,
        "receipt_to": receipt_to,
        "source_uri": document.uri,
        "source_sha256": document.digest,
        "route_intent_idempotency_key": route_key,
        "source_status": source_status,
        "target_fingerprint": target_fingerprint,
        "delivery": delivery,
    }


def _normalise_outcome_receipt(document: SourceDocument, record: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize a Phase-0 outcome proposal into a derived execution receipt."""

    _assert_pointer_only(record, path=f"{document.uri}.outcome_receipt")
    _validate_keys(record, _OUTCOME_RECEIPT_KEYS, field="outcome_receipt")
    contract = _scalar(record.get("contract"), field="outcome_receipt.contract")
    if contract != OUTCOME_RECEIPT_SCHEMA:
        raise InvalidSourceError(f"outcome_receipt must be {OUTCOME_RECEIPT_SCHEMA}")
    receipt_id = _scalar(record.get("receipt_id"), field="outcome_receipt.receipt_id")
    if receipt_id is None or not _RECEIPT_ID_RE.fullmatch(receipt_id):
        raise InvalidSourceError("outcome_receipt.receipt_id is not valid")
    dispatch_id = _scalar(record.get("dispatch_id"), field="outcome_receipt.dispatch_id")
    if dispatch_id is None or not _DISPATCH_ID_RE.fullmatch(dispatch_id):
        raise InvalidSourceError("outcome_receipt.dispatch_id is not valid")
    assignment_id = _scalar(record.get("assignment_id"), field="outcome_receipt.assignment_id")
    if assignment_id is None or not _ASSIGNMENT_ID_RE.fullmatch(assignment_id):
        raise InvalidSourceError("outcome_receipt.assignment_id is not valid")

    task_ref = record.get("task_ref")
    if not isinstance(task_ref, Mapping):
        raise InvalidSourceError("outcome_receipt.task_ref must be an object")
    _validate_keys(task_ref, _OUTCOME_TASK_REF_KEYS, field="outcome_receipt.task_ref")
    projection_id = _scalar(task_ref.get("projection_id"), field="outcome_receipt.task_ref.projection_id")
    if projection_id is None or not _PROJECTION_ID_RE.fullmatch(projection_id):
        raise InvalidSourceError("outcome_receipt.task_ref.projection_id is not valid")
    task_id = _scalar(task_ref.get("task_id"), field="outcome_receipt.task_ref.task_id")
    if task_id is None or not _TASK_CONTRACT_ID_RE.fullmatch(task_id):
        raise InvalidSourceError("outcome_receipt.task_ref.task_id is not valid")
    ticket_id = _scalar(task_ref.get("ticket_id"), field="outcome_receipt.task_ref.ticket_id")
    if ticket_id is None or not _TICKET_ID_RE.fullmatch(ticket_id):
        raise InvalidSourceError("outcome_receipt.task_ref.ticket_id is not valid")
    ticket_revision = _digest(
        task_ref.get("ticket_revision"), field="outcome_receipt.task_ref.ticket_revision"
    )
    receipt_to = _scalar(task_ref.get("receipt_to"), field="outcome_receipt.task_ref.receipt_to")
    if receipt_to is None or not _TICKET_ID_RE.fullmatch(receipt_to) or receipt_to != ticket_id:
        raise InvalidSourceError("outcome_receipt.task_ref.receipt_to must match ticket_id")

    outcome = record.get("outcome")
    if not isinstance(outcome, Mapping):
        raise InvalidSourceError("outcome_receipt.outcome must be an object")
    _validate_keys(outcome, _OUTCOME_KEYS, field="outcome_receipt.outcome")
    outcome_values = tuple(
        _scalar(outcome.get(field), field=f"outcome_receipt.outcome.{field}") or ""
        for field in ("status", "result_code", "reason_code", "proposed_ticket_status")
    )
    mapped_status = _OUTCOME_STATUS_MAP.get(outcome_values)
    if mapped_status is None:
        raise InvalidSourceError("outcome_receipt.outcome is not a supported status combination")

    evidence = record.get("evidence")
    if not isinstance(evidence, list) or not 1 <= len(evidence) <= 16:
        raise InvalidSourceError("outcome_receipt.evidence must contain 1 to 16 objects")
    evidence_refs: list[str] = []
    for index, item in enumerate(evidence):
        if not isinstance(item, Mapping):
            raise InvalidSourceError(f"outcome_receipt.evidence[{index}] must be an object")
        _validate_keys(item, _EVIDENCE_KEYS, field=f"outcome_receipt.evidence[{index}]")
        kind = _scalar(item.get("kind"), field=f"outcome_receipt.evidence[{index}].kind")
        if kind not in _EVIDENCE_KINDS:
            raise InvalidSourceError(f"outcome_receipt.evidence[{index}].kind is not valid")
        reference = _scalar(item.get("ref"), field=f"outcome_receipt.evidence[{index}].ref")
        if reference is None or not _EVIDENCE_REF_RE.fullmatch(reference):
            raise InvalidSourceError(f"outcome_receipt.evidence[{index}].ref is not valid")
        _digest(item.get("digest"), field=f"outcome_receipt.evidence[{index}].digest")
        evidence_refs.append(reference)

    delivery = _normalise_delivery(record.get("delivery"), publisher_prefix="trithon")
    stable = {key: value for key, value in record.items() if key not in {"delivery", "idempotency_key"}}
    idempotency_key = _digest(record.get("idempotency_key"), field="outcome_receipt.idempotency_key")
    if idempotency_key != _sha256(_canonical(stable).encode("utf-8")):
        raise SourceConflictError("outcome_receipt.idempotency_key does not match stable content")
    return {
        "task_id": task_id,
        "projection_id": projection_id,
        "ticket_id": ticket_id,
        "signature": receipt_id,
        "status": mapped_status,
        "executed_by": _scalar(
            record["delivery"].get("publisher_id"), field="outcome_receipt.delivery.publisher_id"
        ),
        "actual_provider": "trithon",
        "actual_model": "outcome-receipt.v1",
        "occurred_at": _scalar(
            record["delivery"].get("emitted_at"), field="outcome_receipt.delivery.emitted_at"
        ),
        "evidence": evidence_refs[0],
        "source_revision": ticket_revision,
        "source_uri": document.uri,
        "source_sha256": document.digest,
        "dispatch_id": dispatch_id,
        "assignment_id": assignment_id,
        "outcome_status": outcome_values[0],
        "result_code": outcome_values[1],
        "reason_code": outcome_values[2],
        "proposed_ticket_status": outcome_values[3],
        "evidence_refs": evidence_refs,
        "receipt_idempotency_key": idempotency_key,
        "receipt_to": receipt_to,
        "delivery": delivery,
    }


def _normalise_receipt(document: SourceDocument, record: Mapping[str, Any]) -> dict[str, Any]:
    if record.get("contract") == OUTCOME_RECEIPT_SCHEMA:
        return _normalise_outcome_receipt(document, record)
    _assert_pointer_only(record, path=f"{document.uri}.receipt")
    _validate_keys(record, _RECEIPT_KEYS, field="receipt")
    task_id = _scalar(record.get("task_id"), field="receipt.task_id", required=False)
    ticket_id = _scalar(record.get("ticket_id"), field="receipt.ticket_id", required=False)
    if task_id is None and ticket_id is None:
        raise InvalidSourceError("receipt needs task_id or ticket_id")
    signature = _scalar(record.get("signature"), field="receipt.signature")
    status = _scalar(record.get("status"), field="receipt.status")
    if status not in _STATUSES:
        raise InvalidSourceError("receipt.status must be done or blocked")
    evidence = _scalar(record.get("evidence"), field="receipt.evidence")
    if evidence is None or not _POINTER_URI_RE.fullmatch(evidence):
        raise InvalidSourceError("receipt.evidence must be a URI/pointer")
    return {
        "task_id": task_id,
        "projection_id": None,
        "ticket_id": ticket_id,
        "signature": signature,
        "status": status,
        "executed_by": _scalar(record.get("executed_by"), field="receipt.executed_by"),
        "actual_provider": _scalar(record.get("actual_provider"), field="receipt.actual_provider"),
        "actual_model": _scalar(record.get("actual_model"), field="receipt.actual_model"),
        "occurred_at": _scalar(record.get("occurred_at"), field="receipt.occurred_at"),
        "evidence": evidence,
        "source_revision": _scalar(
            record.get("source_revision"), field="receipt.source_revision", required=False
        ),
        "source_uri": document.uri,
        "source_sha256": document.digest,
        "receipt_to": None,
        "receipt_idempotency_key": None,
        "delivery": None,
    }


def _prepare_documents(
    documents: Iterable[SourceDocument],
) -> tuple[list[SourceDocument], list[dict[str, Any]], list[dict[str, Any]]]:
    """Validate and normalize a batch before any SQLite write is opened."""

    unique: dict[str, SourceDocument] = {}
    for document in documents:
        if not isinstance(document, SourceDocument):
            raise InvalidSourceError("import_documents accepts SourceDocument values only")
        _scalar(document.uri, field="source uri")
        digest = _scalar(document.digest, field="source digest")
        if not digest or not _SHA256_RE.fullmatch(digest):
            raise InvalidSourceError("source digest must use sha256:<hex> format")
        if not isinstance(document.payload, Mapping):
            raise InvalidSourceError("source payload must be a JSON object")
        _assert_pointer_only(document.payload)
        previous = unique.get(document.uri)
        if previous is not None:
            if _checkpoint_fingerprint(previous) != _checkpoint_fingerprint(document):
                raise SourceConflictError(f"source URI has two different fingerprints: {document.uri}")
            if _canonical(_stable_source_payload(previous.payload)) != _canonical(
                _stable_source_payload(document.payload)
            ):
                raise SourceConflictError(f"source URI has two different payloads: {document.uri}")
        unique[document.uri] = document

    normalized_routes = [
        _normalise_route(document, item)
        for document in unique.values()
        for item in _route_records(document)
    ] + [
        _normalise_task_projection(document, item)
        for document in unique.values()
        for item in _projection_records(document)
    ]
    normalized_receipts = [
        _normalise_receipt(document, item)
        for document in unique.values()
        for item in _receipt_records(document)
    ]
    for document in unique.values():
        if not _route_records(document) and not _projection_records(document) and not _receipt_records(document):
            raise InvalidSourceError(
                f"{document.uri}: source has no supported route intent, task projection, or receipt"
            )

    routes_by_task: dict[str, dict[str, Any]] = {}
    for route in normalized_routes:
        prior = routes_by_task.get(route["task_id"])
        if prior is not None and not _normalised_equivalent(prior, route):
            raise SourceConflictError(f"task projection changed within one import: {route['task_id']}")
        routes_by_task[route["task_id"]] = route
    receipts_by_signature: dict[str, dict[str, Any]] = {}
    for receipt in normalized_receipts:
        prior = receipts_by_signature.get(receipt["signature"])
        if prior is not None and not _normalised_equivalent(prior, receipt):
            raise SourceConflictError(f"receipt signature changed within one import: {receipt['signature']}")
        receipts_by_signature[receipt["signature"]] = receipt
    return list(unique.values()), list(routes_by_task.values()), list(receipts_by_signature.values())


@dataclass(frozen=True)
class ImportResult:
    documents: int
    projected: int
    unchanged: int
    receipts: int
    receipt_unchanged: int
    checkpoints: int

    def as_dict(self) -> dict[str, int]:
        return {
            "documents": self.documents,
            "projected": self.projected,
            "unchanged": self.unchanged,
            "receipts": self.receipts,
            "receipt_unchanged": self.receipt_unchanged,
            "checkpoints": self.checkpoints,
        }


class ShadowStore:
    """A local SQLite projection with transactionally idempotent imports."""

    def __init__(
        self,
        db_path: str | Path,
        *,
        trusted_publishers: Iterable[str] | None = None,
    ):
        path = Path(db_path).expanduser()
        if not path.is_absolute():
            raise InvalidSourceError("db_path must be absolute; choose the local shadow store explicitly")
        if path.name in {"", ".", ".."}:
            raise InvalidSourceError("db_path must name a database file")
        if isinstance(trusted_publishers, (str, bytes)):
            raise InvalidSourceError("trusted_publishers must be an iterable of publisher IDs")
        publishers = tuple(trusted_publishers or ())
        for publisher in publishers:
            if not isinstance(publisher, str) or not _PUBLISHER_ID_RE.fullmatch(publisher):
                raise InvalidSourceError(f"invalid trusted publisher: {publisher!r}")
        self.trusted_publishers = frozenset(publishers)
        self.path = path.resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        try:
            connection = sqlite3.connect(str(self.path), timeout=10, isolation_level=None)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA busy_timeout=10000")
            connection.execute("PRAGMA foreign_keys=ON")
            self._integrity_gate(connection)
            return connection
        except sqlite3.DatabaseError as exc:
            raise CorruptStoreError(f"shadow database is not readable: {self.path}") from exc

    @staticmethod
    def _integrity_gate(connection: sqlite3.Connection) -> None:
        try:
            result = connection.execute("PRAGMA integrity_check").fetchone()
        except sqlite3.DatabaseError as exc:
            raise CorruptStoreError("SQLite integrity check failed") from exc
        if not result or result[0] != "ok":
            raise CorruptStoreError(f"SQLite integrity check returned {result[0] if result else 'no result'}")

    def _ensure_schema(self) -> None:
        connection = self._connect_without_schema_gate()
        try:
            connection.execute("BEGIN IMMEDIATE")
            for statement in (
                """
                CREATE TABLE IF NOT EXISTS shadow_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                """,
                """
                CREATE TABLE IF NOT EXISTS tasks (
                    task_id TEXT PRIMARY KEY,
                    projection_id TEXT,
                    ticket_id TEXT NOT NULL,
                    source_revision TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    requested_capability TEXT,
                    priority TEXT,
                    target_kind TEXT NOT NULL,
                    target_systems_json TEXT NOT NULL,
                    receipt_to TEXT,
                    source_uri TEXT NOT NULL,
                    source_sha256 TEXT NOT NULL,
                    shadow_state TEXT NOT NULL DEFAULT 'projected',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """,
                """
                CREATE TABLE IF NOT EXISTS checkpoints (
                    source_uri TEXT PRIMARY KEY,
                    source_sha256 TEXT NOT NULL,
                    source_revision TEXT NOT NULL,
                    imported_at TEXT NOT NULL
                )
                """,
                """
                CREATE TABLE IF NOT EXISTS publisher_checkpoints (
                    publisher_id TEXT PRIMARY KEY,
                    publisher_epoch TEXT NOT NULL,
                    last_sequence INTEGER NOT NULL
                )
                """,
                """
                CREATE TABLE IF NOT EXISTS publisher_events (
                    event_id TEXT PRIMARY KEY,
                    publisher_id TEXT NOT NULL,
                    publisher_epoch TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    stable_key TEXT NOT NULL
                )
                """,
                """
                CREATE TABLE IF NOT EXISTS receipts (
                    signature TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL REFERENCES tasks(task_id),
                    idempotency_key TEXT,
                    status TEXT NOT NULL,
                    executed_by TEXT NOT NULL,
                    actual_provider TEXT NOT NULL,
                    actual_model TEXT NOT NULL,
                    occurred_at TEXT NOT NULL,
                    evidence TEXT NOT NULL,
                    source_uri TEXT NOT NULL,
                    source_sha256 TEXT NOT NULL
                )
                """,
                """
                CREATE TABLE IF NOT EXISTS task_history (
                    event_key TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL REFERENCES tasks(task_id),
                    event_type TEXT NOT NULL,
                    source_revision TEXT NOT NULL,
                    metadata_json TEXT NOT NULL,
                    occurred_at TEXT NOT NULL
                )
                """,
                "CREATE INDEX IF NOT EXISTS idx_tasks_ticket_id ON tasks(ticket_id)",
                "CREATE INDEX IF NOT EXISTS idx_history_task_id ON task_history(task_id)",
                "CREATE INDEX IF NOT EXISTS idx_receipts_task_id ON receipts(task_id)",
                "CREATE INDEX IF NOT EXISTS idx_publisher_events_order ON publisher_events(publisher_id, publisher_epoch, sequence)",
                ):
                connection.execute(statement)
            task_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(tasks)").fetchall()
            }
            if "projection_id" not in task_columns:
                connection.execute("ALTER TABLE tasks ADD COLUMN projection_id TEXT")
            receipt_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(receipts)").fetchall()
            }
            if "idempotency_key" not in receipt_columns:
                connection.execute("ALTER TABLE receipts ADD COLUMN idempotency_key TEXT")
            row = connection.execute(
                "SELECT value FROM shadow_meta WHERE key = 'schema'"
            ).fetchone()
            if row is None:
                connection.execute(
                    "INSERT INTO shadow_meta(key, value) VALUES('schema', ?)",
                    (SHADOW_SCHEMA,),
                )
            elif row[0] != SHADOW_SCHEMA:
                raise CorruptStoreError(f"unsupported shadow schema: {row[0]}")
            version_row = connection.execute(
                "SELECT value FROM shadow_meta WHERE key = 'schema_version'"
            ).fetchone()
            if version_row is not None:
                try:
                    stored_version = int(version_row[0])
                except (TypeError, ValueError) as exc:
                    raise CorruptStoreError("shadow schema version is not an integer") from exc
                if stored_version > SHADOW_SCHEMA_VERSION:
                    raise CorruptStoreError(
                        f"shadow schema version {stored_version} is newer than supported {SHADOW_SCHEMA_VERSION}"
                    )
            connection.execute(
                "INSERT OR REPLACE INTO shadow_meta(key, value) VALUES('schema_version', ?)",
                (str(SHADOW_SCHEMA_VERSION),),
            )
            connection.execute("COMMIT")
        except sqlite3.DatabaseError as exc:
            try:
                connection.execute("ROLLBACK")
            except sqlite3.DatabaseError:
                pass
            raise CorruptStoreError(f"shadow database migration failed: {self.path}") from exc
        except Exception:
            try:
                connection.execute("ROLLBACK")
            except sqlite3.DatabaseError:
                pass
            raise
        finally:
            connection.close()

    def _connect_without_schema_gate(self) -> sqlite3.Connection:
        try:
            connection = sqlite3.connect(str(self.path), timeout=10, isolation_level=None)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA busy_timeout=10000")
            connection.execute("PRAGMA foreign_keys=ON")
            return connection
        except sqlite3.DatabaseError as exc:
            raise CorruptStoreError(f"shadow database is not readable: {self.path}") from exc

    @staticmethod
    def _checkpoint_guard(connection: sqlite3.Connection, document: SourceDocument) -> None:
        row = connection.execute(
            "SELECT source_sha256 FROM checkpoints WHERE source_uri = ?", (document.uri,)
        ).fetchone()
        if row is not None and row[0] != _checkpoint_fingerprint(document):
            raise SourceConflictError(
                f"source changed at {document.uri}; rebuild explicitly to replace the projection"
            )

    def _record_delivery(
        self,
        connection: sqlite3.Connection,
        delivery: Mapping[str, Any] | None,
        *,
        stable_key: str,
    ) -> bool:
        """Accept one trusted, monotonic delivery or replay an exact event."""

        if delivery is None:
            return False
        publisher_id = delivery["publisher_id"]
        if publisher_id not in self.trusted_publishers:
            raise InvalidSourceError(f"untrusted delivery publisher: {publisher_id}")
        existing_event = connection.execute(
            "SELECT * FROM publisher_events WHERE event_id = ?", (delivery["event_id"],)
        ).fetchone()
        if existing_event is not None:
            expected = (
                publisher_id,
                delivery["publisher_epoch"],
                delivery["sequence"],
                stable_key,
            )
            actual = tuple(existing_event[key] for key in (
                "publisher_id", "publisher_epoch", "sequence", "stable_key"
            ))
            if actual != expected:
                raise SourceConflictError(f"delivery event changed: {delivery['event_id']}")
            return False

        same_sequence = connection.execute(
            """
            SELECT event_id FROM publisher_events
            WHERE publisher_id = ? AND publisher_epoch = ? AND sequence = ?
            """,
            (publisher_id, delivery["publisher_epoch"], delivery["sequence"]),
        ).fetchone()
        if same_sequence is not None:
            raise SourceConflictError(
                f"publisher sequence already belongs to another event: {publisher_id}:{delivery['sequence']}"
            )
        checkpoint = connection.execute(
            "SELECT * FROM publisher_checkpoints WHERE publisher_id = ?", (publisher_id,)
        ).fetchone()
        if checkpoint is not None:
            if checkpoint["publisher_epoch"] != delivery["publisher_epoch"]:
                raise SourceConflictError(f"publisher epoch changed: {publisher_id}")
            if delivery["sequence"] <= checkpoint["last_sequence"]:
                raise SourceConflictError(f"stale publisher sequence: {publisher_id}:{delivery['sequence']}")
            connection.execute(
                "UPDATE publisher_checkpoints SET last_sequence = ? WHERE publisher_id = ?",
                (delivery["sequence"], publisher_id),
            )
        else:
            connection.execute(
                """
                INSERT INTO publisher_checkpoints(publisher_id, publisher_epoch, last_sequence)
                VALUES (?, ?, ?)
                """,
                (publisher_id, delivery["publisher_epoch"], delivery["sequence"]),
            )
        connection.execute(
            """
            INSERT INTO publisher_events(event_id, publisher_id, publisher_epoch, sequence, stable_key)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                delivery["event_id"], publisher_id, delivery["publisher_epoch"],
                delivery["sequence"], stable_key,
            ),
        )
        return True

    @staticmethod
    def _history(
        connection: sqlite3.Connection,
        *,
        event_key: str,
        task_id: str,
        event_type: str,
        source_revision: str,
        metadata: Mapping[str, Any],
    ) -> bool:
        cursor = connection.execute(
            """
            INSERT OR IGNORE INTO task_history
                (event_key, task_id, event_type, source_revision, metadata_json, occurred_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (event_key, task_id, event_type, source_revision, _canonical(dict(metadata)), _now()),
        )
        return cursor.rowcount == 1

    @staticmethod
    def _lookup_task(
        connection: sqlite3.Connection, *, task_id: str | None, ticket_id: str | None
    ) -> sqlite3.Row | None:
        if task_id is not None:
            return connection.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
        rows = connection.execute("SELECT * FROM tasks WHERE ticket_id = ?", (ticket_id,)).fetchall()
        if len(rows) > 1:
            raise SourceConflictError(f"ticket_id maps to multiple shadow tasks: {ticket_id}")
        return rows[0] if rows else None

    def import_documents(
        self,
        documents: Iterable[SourceDocument],
        *,
        failure_hook: Callable[[str], None] | None = None,
    ) -> ImportResult:
        """Atomically import route intents and receipts from explicit sources.

        ``failure_hook`` exists only for deterministic crash-recovery tests.  A
        raised exception rolls back the complete transaction, just as a process
        crash before commit would.
        """
        docs, routes, receipts = _prepare_documents(documents)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            result = self._import_in_transaction(
                connection, docs, routes, receipts, failure_hook=failure_hook
            )
            connection.execute("COMMIT")
            return result
        except Exception:
            try:
                connection.execute("ROLLBACK")
            except sqlite3.DatabaseError:
                pass
            raise
        finally:
            connection.close()

    def _import_in_transaction(
        self,
        connection: sqlite3.Connection,
        docs: list[SourceDocument],
        routes: list[dict[str, Any]],
        receipts: list[dict[str, Any]],
        *,
        failure_hook: Callable[[str], None] | None = None,
    ) -> ImportResult:
        """Apply a validated batch while the caller owns ``BEGIN IMMEDIATE``."""

        projected = unchanged = receipt_count = receipt_unchanged = 0
        for document in docs:
            self._checkpoint_guard(connection, document)

        deliveries = [
            (route["delivery"], route["idempotency_key"])
            for route in routes
            if route.get("delivery") is not None
        ] + [
            (receipt["delivery"], receipt.get("receipt_idempotency_key") or receipt["signature"])
            for receipt in receipts
            if receipt.get("delivery") is not None
        ]
        for delivery, stable_key in sorted(
            deliveries,
            key=lambda item: (
                item[0]["publisher_id"], item[0]["publisher_epoch"],
                item[0]["sequence"], item[0]["event_id"],
            ),
        ):
            self._record_delivery(connection, delivery, stable_key=stable_key)

        for route in sorted(routes, key=lambda item: item["task_id"]):
            existing = connection.execute(
                    "SELECT * FROM tasks WHERE task_id = ? OR idempotency_key = ?",
                (route["task_id"], route["idempotency_key"]),
            ).fetchall()
            if len(existing) > 1 or (
                existing
                and (
                    existing[0]["task_id"] != route["task_id"]
                    or existing[0]["idempotency_key"] != route["idempotency_key"]
                )
            ):
                raise SourceConflictError(
                    f"task or idempotency key already belongs to another projection: {route['task_id']}"
                )
            if existing:
                row = existing[0]
                comparison_keys = [
                    "projection_id", "ticket_id", "source_revision", "target_kind", "target_systems_json",
                    "requested_capability", "priority", "receipt_to",
                ]
                if route.get("projection_id") is None:
                    comparison_keys.extend(("source_uri", "source_sha256"))
                comparable = tuple(row[key] for key in comparison_keys)
                expected = (
                    route.get("projection_id"),
                    route["ticket_id"],
                    route["source_revision"],
                    route["target_kind"],
                    _canonical(route["target_systems"]),
                    route["requested_capability"],
                    route["priority"],
                    route["receipt_to"],
                )
                if route.get("projection_id") is None:
                    expected += (route["source_uri"], route["source_sha256"])
                if comparable != expected:
                    raise SourceConflictError(f"task projection changed: {route['task_id']}")
                unchanged += 1
            else:
                timestamp = _now()
                connection.execute(
                    """
                    INSERT INTO tasks
                        (task_id, projection_id, ticket_id, source_revision, idempotency_key,
                         requested_capability, priority, target_kind, target_systems_json,
                         receipt_to, source_uri, source_sha256, shadow_state,
                         created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'projected', ?, ?)
                    """,
                    (
                        route["task_id"], route.get("projection_id"), route["ticket_id"],
                        route["source_revision"], route["idempotency_key"],
                        route["requested_capability"], route["priority"],
                        route["target_kind"], _canonical(route["target_systems"]), route["receipt_to"],
                        route["source_uri"], route["source_sha256"], timestamp, timestamp,
                    ),
                )
                self._history(
                    connection,
                    event_key=f"projection:{route['task_id']}:{route['source_revision']}",
                    task_id=route["task_id"],
                    event_type="projected",
                    source_revision=route["source_revision"],
                    metadata={
                        "source_uri": route["source_uri"],
                        "source_sha256": route["source_sha256"],
                        "ticket_id": route["ticket_id"],
                    },
                )
                projected += 1
                if failure_hook is not None:
                    failure_hook("after_task_insert")

        for receipt in sorted(receipts, key=lambda item: item["signature"]):
            task = self._lookup_task(
                connection, task_id=receipt["task_id"], ticket_id=receipt["ticket_id"]
            )
            if task is None:
                raise InvalidSourceError(
                    f"receipt references no projected task: {receipt['task_id'] or receipt['ticket_id']}"
                )
            if receipt["task_id"] is None:
                receipt["task_id"] = task["task_id"]
            if receipt["ticket_id"] and receipt["ticket_id"] != task["ticket_id"]:
                raise SourceConflictError(f"receipt ticket does not match task: {task['task_id']}")
            if receipt["source_revision"] and receipt["source_revision"] != task["source_revision"]:
                raise SourceConflictError(f"receipt revision does not match task: {task['task_id']}")
            if receipt.get("projection_id") and receipt["projection_id"] != task["projection_id"]:
                raise SourceConflictError(f"receipt projection does not match task: {task['task_id']}")
            if receipt.get("receipt_to") and receipt["receipt_to"] != task["receipt_to"]:
                raise SourceConflictError(f"receipt target does not match task: {task['task_id']}")
            existing = connection.execute(
                "SELECT * FROM receipts WHERE signature = ?", (receipt["signature"],)
            ).fetchone()
            receipt_fields = [
                "task_id", "status", "executed_by", "actual_provider", "actual_model", "evidence",
                "idempotency_key",
            ]
            if receipt.get("receipt_idempotency_key") is None:
                receipt_fields.insert(5, "occurred_at")
                receipt_fields.extend(("source_uri", "source_sha256"))
            if existing is not None:
                comparable = tuple(existing[key] for key in receipt_fields)
                expected = tuple(
                    receipt["receipt_idempotency_key"]
                    if key == "idempotency_key"
                    else receipt[key]
                    for key in receipt_fields
                )
                if comparable != expected:
                    raise SourceConflictError(f"receipt signature changed: {receipt['signature']}")
                receipt_unchanged += 1
                continue
            connection.execute(
                """
                INSERT INTO receipts
                    (signature, task_id, idempotency_key, status, executed_by, actual_provider,
                     actual_model, occurred_at, evidence, source_uri, source_sha256)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    receipt["signature"], receipt["task_id"], receipt.get("receipt_idempotency_key"),
                    receipt["status"],
                    receipt["executed_by"], receipt["actual_provider"], receipt["actual_model"],
                    receipt["occurred_at"], receipt["evidence"], receipt["source_uri"],
                    receipt["source_sha256"],
                ),
            )
            connection.execute(
                "UPDATE tasks SET shadow_state = ?, updated_at = ? WHERE task_id = ?",
                (receipt["status"], _now(), receipt["task_id"]),
            )
            self._history(
                connection,
                event_key=f"receipt:{receipt['signature']}",
                task_id=receipt["task_id"],
                event_type="receipt",
                source_revision=task["source_revision"],
                metadata={
                    "signature": receipt["signature"],
                    "status": receipt["status"],
                    "evidence": receipt["evidence"],
                    "source_uri": receipt["source_uri"],
                    **{
                        key: receipt[key]
                        for key in (
                            "projection_id", "dispatch_id", "assignment_id", "outcome_status",
                            "result_code", "reason_code", "proposed_ticket_status", "receipt_to",
                        )
                        if receipt.get(key) is not None
                    },
                },
            )
            receipt_count += 1
            if failure_hook is not None:
                failure_hook("after_receipt_insert")

        revisions_by_uri: dict[str, list[str]] = {}
        for route in routes:
            revisions_by_uri.setdefault(route["source_uri"], []).append(route["source_revision"])
        for document in docs:
            revisions = revisions_by_uri.get(document.uri, [])
            revision = sorted(revisions)[0] if revisions else _checkpoint_fingerprint(document)
            connection.execute(
                """
                INSERT OR IGNORE INTO checkpoints(source_uri, source_sha256, source_revision, imported_at)
                VALUES (?, ?, ?, ?)
                """,
                (document.uri, _checkpoint_fingerprint(document), revision, _now()),
            )
            if failure_hook is not None:
                failure_hook("after_checkpoint")
        return ImportResult(
            documents=len(docs),
            projected=projected,
            unchanged=unchanged,
            receipts=receipt_count,
            receipt_unchanged=receipt_unchanged,
            checkpoints=len(docs),
        )

    def rebuild(
        self,
        documents: Iterable[SourceDocument],
        *,
        failure_hook: Callable[[str], None] | None = None,
    ) -> ImportResult:
        """Atomically clear and recreate the complete derived projection."""
        docs, routes, receipts = _prepare_documents(documents)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("DELETE FROM receipts")
            connection.execute("DELETE FROM task_history")
            connection.execute("DELETE FROM tasks")
            connection.execute("DELETE FROM checkpoints")
            connection.execute("DELETE FROM publisher_events")
            connection.execute("DELETE FROM publisher_checkpoints")
            result = self._import_in_transaction(
                connection, docs, routes, receipts, failure_hook=failure_hook
            )
            connection.execute("COMMIT")
            return result
        except Exception:
            try:
                connection.execute("ROLLBACK")
            except sqlite3.DatabaseError:
                pass
            raise
        finally:
            connection.close()

    def reset_checkpoints(self) -> int:
        """Forget source and delivery checkpoints while retaining projections/history."""

        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute("DELETE FROM checkpoints")
            removed = cursor.rowcount
            removed += connection.execute("DELETE FROM publisher_events").rowcount
            removed += connection.execute("DELETE FROM publisher_checkpoints").rowcount
            connection.execute("COMMIT")
            return removed
        except Exception:
            try:
                connection.execute("ROLLBACK")
            except sqlite3.DatabaseError:
                pass
            raise
        finally:
            connection.close()

    def mock_execute(
        self,
        task_id: str,
        *,
        status: str = "done",
        evidence: str | None = None,
    ) -> dict[str, str]:
        """Record a deterministic no-op execution; no process or file is run."""

        task_value = _scalar(task_id, field="task_id")
        status_value = _scalar(status, field="status")
        if status_value not in _STATUSES:
            raise InvalidSourceError("mock status is not a supported shadow status")
        evidence_value = _scalar(evidence or f"mock://{task_value}", field="evidence")
        if evidence_value is None or not _POINTER_URI_RE.fullmatch(evidence_value):
            raise InvalidSourceError("mock evidence must be a URI/pointer")
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            task = connection.execute("SELECT * FROM tasks WHERE task_id = ?", (task_value,)).fetchone()
            if task is None:
                raise InvalidSourceError(f"unknown shadow task: {task_value}")
            if not task["source_uri"].startswith(("synthetic://", "fixture://", "memory://")):
                raise InvalidSourceError(
                    "mock executor accepts synthetic, fixture, or memory source tasks only"
                )
            real_receipt = connection.execute(
                "SELECT signature FROM receipts WHERE task_id = ?", (task_value,)
            ).fetchone()
            if real_receipt is not None:
                raise InvalidSourceError(
                    "mock executor cannot overwrite a transport receipt: "
                    f"{real_receipt['signature']}"
                )
            event_key = "mock:" + hashlib.sha256(
                _canonical({"task_id": task_value, "status": status_value, "evidence": evidence_value}).encode(
                    "utf-8"
                )
            ).hexdigest()
            self._history(
                connection,
                event_key=event_key,
                task_id=task_value or "",
                event_type="mock-executed",
                source_revision=task["source_revision"],
                metadata={"executor": "mock", "status": status_value, "evidence": evidence_value},
            )
            connection.execute(
                "UPDATE tasks SET shadow_state = ?, updated_at = ? WHERE task_id = ?",
                (f"mock-{status_value}", _now(), task_value),
            )
            connection.execute("COMMIT")
        except Exception:
            try:
                connection.execute("ROLLBACK")
            except sqlite3.DatabaseError:
                pass
            raise
        finally:
            connection.close()
        return {"task_id": task_value or "", "status": status_value or "", "evidence": evidence_value or ""}

    def verify(self) -> dict[str, Any]:
        """Return a read-only integrity and invariant report."""

        connection = self._connect()
        try:
            counts = {
                "tasks": connection.execute("SELECT COUNT(*) FROM tasks").fetchone()[0],
                "receipts": connection.execute("SELECT COUNT(*) FROM receipts").fetchone()[0],
                "history": connection.execute("SELECT COUNT(*) FROM task_history").fetchone()[0],
                "checkpoints": connection.execute("SELECT COUNT(*) FROM checkpoints").fetchone()[0],
            }
            delivery_counts = {
                "events": connection.execute("SELECT COUNT(*) FROM publisher_events").fetchone()[0],
                "publishers": connection.execute("SELECT COUNT(*) FROM publisher_checkpoints").fetchone()[0],
            }
            orphan_receipts = connection.execute(
                "SELECT COUNT(*) FROM receipts r LEFT JOIN tasks t ON t.task_id = r.task_id WHERE t.task_id IS NULL"
            ).fetchone()[0]
            orphan_history = connection.execute(
                "SELECT COUNT(*) FROM task_history h LEFT JOIN tasks t ON t.task_id = h.task_id WHERE t.task_id IS NULL"
            ).fetchone()[0]
            return {
                "schema": SHADOW_SCHEMA,
                "db": str(self.path),
                "integrity": "ok",
                "counts": counts,
                "delivery": delivery_counts,
                "orphan_receipts": orphan_receipts,
                "orphan_history": orphan_history,
                "ok": orphan_receipts == 0 and orphan_history == 0,
            }
        finally:
            connection.close()

    def tasks(self) -> list[dict[str, Any]]:
        """Return non-secret task metadata for readback and tests."""

        connection = self._connect()
        try:
            rows = connection.execute(
                "SELECT * FROM tasks ORDER BY task_id"
            ).fetchall()
            return [
                {
                    "task_id": row["task_id"],
                    "projection_id": row["projection_id"],
                    "ticket_id": row["ticket_id"],
                    "source_revision": row["source_revision"],
                    "idempotency_key": row["idempotency_key"],
                    "requested_capability": row["requested_capability"],
                    "priority": row["priority"],
                    "target_kind": row["target_kind"],
                    "target_systems": json.loads(row["target_systems_json"]),
                    "receipt_to": row["receipt_to"],
                    "source_uri": row["source_uri"],
                    "source_sha256": row["source_sha256"],
                    "shadow_state": row["shadow_state"],
                }
                for row in rows
            ]
        finally:
            connection.close()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Local pointer-only Trithon shadow store")
    parser.add_argument("--db", required=True, help="absolute path to the local shadow SQLite database")
    parser.add_argument(
        "--trusted-publisher",
        action="append",
        default=[],
        help="exact delivery publisher ID allowed for contract envelopes; repeatable",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("import", "rebuild"):
        command = sub.add_parser(name, help=f"{name} explicit JSON source documents")
        command.add_argument("--source", action="append", required=True, help="JSON source path; repeatable")
    sub.add_parser("verify", help="verify SQLite integrity and projection invariants")
    sub.add_parser(
        "reset-checkpoints",
        help="clear source and delivery fingerprints without deleting projections",
    )
    mock = sub.add_parser("mock-execute", help="record a no-op shadow execution")
    mock.add_argument("--task-id", required=True)
    mock.add_argument("--status", choices=sorted(_STATUSES), default="done")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        store = ShadowStore(args.db, trusted_publishers=args.trusted_publisher)
        if args.command in {"import", "rebuild"}:
            documents = [load_source(path) for path in args.source]
            result = (
                store.rebuild(documents)
                if args.command == "rebuild"
                else store.import_documents(documents)
            )
            print(json.dumps(result.as_dict(), ensure_ascii=False, sort_keys=True))
        elif args.command == "verify":
            print(json.dumps(store.verify(), ensure_ascii=False, sort_keys=True))
        elif args.command == "reset-checkpoints":
            print(json.dumps({"removed": store.reset_checkpoints()}, ensure_ascii=False, sort_keys=True))
        elif args.command == "mock-execute":
            print(json.dumps(store.mock_execute(args.task_id, status=args.status), ensure_ascii=False, sort_keys=True))
        return 0
    except (ShadowStoreError, OSError, sqlite3.DatabaseError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
