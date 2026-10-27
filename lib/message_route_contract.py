"""Versioned metadata handoff for Messages-Line over an existing transport.

This is a message subject, not a T-ticket route or an executable task. The
canonical message store owns content and ReceiptIDs. No content enters the
sync envelope; a target must resolve the payload through its authorized store.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from typing import Any

from .messages_line import (
    DELIVERY_MODES, SURFACES, MESSAGE_ROUTE_SCHEMA, MessageLineError, MessagesLine,
    validate_message_ticket, validate_messages_config,
)

_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_MESSAGE_ID_RE = re.compile(r"^M-[0-9]{8}-[1-9][0-9]{8}$")
_RECEIPT_ID_RE = re.compile(r"^R-[0-9]{8}-[1-9][0-9]{8}$")
_SAFE_REF_RE = re.compile(r"^[^\x00-\x20\x7f\\]{1,256}$")
_FIELDS = frozenset({
    "route_intent", "message_id", "receipt_id", "origin_ref", "recipient_ref",
    "target_ref", "surface", "delivery_mode", "payload_ref", "payload_sha256",
    "idempotency_key",
})


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def payload_digest(message: Mapping[str, Any]) -> str:
    """Digest the validated full record, including text, without exporting it."""
    return "sha256:" + hashlib.sha256(
        canonical_json(validate_message_ticket(message)).encode("utf-8")
    ).hexdigest()


def validate_message_route_intent(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping) or frozenset(value) != _FIELDS:
        raise MessageLineError("message route intent fields are invalid")
    if value["route_intent"] != MESSAGE_ROUTE_SCHEMA:
        raise MessageLineError("unsupported message route intent")
    for field, regex in (("message_id", _MESSAGE_ID_RE), ("receipt_id", _RECEIPT_ID_RE)):
        if not isinstance(value[field], str) or not regex.fullmatch(value[field]):
            raise MessageLineError(f"invalid {field}")
    for field in ("origin_ref", "recipient_ref", "target_ref"):
        ref = value[field]
        if (not isinstance(ref, str) or not _SAFE_REF_RE.fullmatch(ref)
                or ref.startswith("/") or re.match(r"^[A-Za-z]:/", ref)):
            raise MessageLineError(f"invalid {field}")
    if value["surface"] not in SURFACES or value["delivery_mode"] not in DELIVERY_MODES:
        raise MessageLineError("invalid message surface or delivery mode")
    if value["payload_ref"] != f"messages-line:{value['message_id']}":
        raise MessageLineError("payload reference does not bind its MessageID")
    if not isinstance(value["payload_sha256"], str) or not _SHA256_RE.fullmatch(value["payload_sha256"]):
        raise MessageLineError("payload digest is invalid")
    stable = {key: value[key] for key in _FIELDS - {"idempotency_key"}}
    expected = "sha256:" + hashlib.sha256(canonical_json(stable).encode("utf-8")).hexdigest()
    if value["idempotency_key"] != expected:
        raise MessageLineError("message route idempotency key does not match its payload")
    # The message-store validator below supplies the detailed ID/ref checks.
    # This detached form is also checked by the transport owner's validator.
    return dict(value)


def build_message_route_intent(
    line: MessagesLine,
    message_id: str,
    *,
    target_ref: str,
    surface: str,
    current_config: Mapping[str, Any],
) -> dict[str, Any]:
    """Build a deterministic, content-free envelope after the live policy gate."""
    if validate_messages_config(current_config)["transport_contract_ref"] != MESSAGE_ROUTE_SCHEMA:
        raise MessageLineError("current config has not selected the message route contract")
    message = line.read_for_delivery(
        message_id, target_ref=target_ref, surface=surface, current_config=current_config
    )
    stable = {
        "route_intent": MESSAGE_ROUTE_SCHEMA,
        "message_id": message["message_id"],
        "receipt_id": message["receipt_id"],
        "origin_ref": message["origin_ref"],
        "recipient_ref": message["recipient_ref"],
        "target_ref": target_ref,
        "surface": surface,
        "delivery_mode": message["delivery_mode"],
        "payload_ref": f"messages-line:{message['message_id']}",
        "payload_sha256": payload_digest(message),
    }
    return validate_message_route_intent({
        **stable,
        "idempotency_key": "sha256:" + hashlib.sha256(canonical_json(stable).encode("utf-8")).hexdigest(),
    })
