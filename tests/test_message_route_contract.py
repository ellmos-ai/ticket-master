"""The message subject stays separate from executable T-ticket routing."""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest

from lib.message_route_contract import build_message_route_intent, validate_message_route_intent
from lib.messages_line import MessageLineError, MessagesLine, _make_observation_id
from lib.unicorn_intake import IntakeValidationError, validate_route_intent

ROOT = Path(__file__).resolve().parents[1]


def _config(*, enabled=True):
    return {
        "schema": "ellmos.ticket.messages-line-config.v1",
        "updated_at": "2026-10-27T12:00:00Z",
        "enabled": enabled,
        "delivery_modes": {"chat_start": True, "on_demand": True, "active_chat": True},
        "targets": [{"target_ref": "fixture:recipient", "surface": "registered_server"}],
        "gui_open_counts_as_delivery": False,
        "message_content_logging": False,
        "audio_logging": False,
        "transport_contract_ref": "ellmos.ticket.message-route-intent.v1",
    }


def test_subject_uses_real_message_store_and_never_forges_t_ticket(tmp_path):
    line = MessagesLine(tmp_path / "messages")
    message = line.create(
        config=_config(), origin_ref="fixture:automation", recipient_ref="fixture:user",
        content_text="Prüfung mit Umlauten äöü", delivery_mode="on_demand",
        today="2026-10-27",
    )
    first = build_message_route_intent(
        line, message["message_id"], target_ref="fixture:recipient",
        surface="registered_server", current_config=_config(),
    )
    assert first == build_message_route_intent(
        MessagesLine(tmp_path / "messages"), message["message_id"],
        target_ref="fixture:recipient", surface="registered_server", current_config=_config(),
    )
    assert first["receipt_id"] == message["receipt_id"]
    assert first["origin_ref"] == "fixture:automation"
    assert "ticket_id" not in first and "content_text" not in first
    assert validate_message_route_intent(first) == first
    schema = json.loads((ROOT / "contracts" / "messages" / "v1" /
                         "ellmos.ticket.message-route-intent.v1.schema.json").read_text(encoding="utf-8"))
    jsonschema.Draft202012Validator(schema).validate(first)
    with pytest.raises(IntakeValidationError, match="fields"):
        validate_route_intent(first)  # Ticket v1 never silently accepts a message subject.
    assert not (tmp_path / "INBOX").exists()


def test_current_disable_and_changed_ids_fail_before_a_handoff(tmp_path):
    line = MessagesLine(tmp_path / "messages")
    message = line.create(
        config=_config(), origin_ref="fixture:manual", recipient_ref="fixture:user",
        content_text="Synthetic", delivery_mode="on_demand", today="2026-10-27",
    )
    with pytest.raises(MessageLineError, match="current config"):
        build_message_route_intent(
            line, message["message_id"], target_ref="fixture:recipient",
            surface="registered_server", current_config=_config(enabled=False),
        )
    intent = build_message_route_intent(
        line, message["message_id"], target_ref="fixture:recipient",
        surface="registered_server", current_config=_config(),
    )
    with pytest.raises(MessageLineError, match="idempotency"):
        validate_message_route_intent({**intent, "receipt_id": "R-20261027-123456789"})


def test_unverified_external_ack_import_remains_closed(tmp_path):
    line = MessagesLine(tmp_path / "messages")
    message = line.create(
        config=_config(), origin_ref="fixture:log", recipient_ref="fixture:user",
        content_text="Synthetic", delivery_mode="on_demand", today="2026-10-27",
    )
    ack = line.record_observation(
        message["message_id"], target_ref="fixture:recipient", surface="registered_server",
        channel_ref="fixture:channel", actor_ref="fixture:actor", evidence_ref="fixture:evidence",
        event="on_demand_ack", current_config=_config(),
    )
    assert line.record_receipt(ack, current_config=_config(enabled=False)) == ack
    forged_base = {key: value for key, value in ack.items() if key != "observation_id"}
    forged_base["channel_ref"] = "fixture:external"
    forged = {**forged_base, "observation_id": _make_observation_id(forged_base)}
    with pytest.raises(MessageLineError, match="external receipt import"):
        line.record_receipt(forged, current_config=_config())

    transported_base = {
        **forged_base,
        "channel_ref": "fixture:unverified-transport",
        "event": "transported",
    }
    unverified_transport = {
        **transported_base,
        "observation_id": _make_observation_id(transported_base),
    }
    with pytest.raises(MessageLineError, match="external receipt import"):
        line.record_receipt(unverified_transport, current_config=_config())
