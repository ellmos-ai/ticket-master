import hashlib
import json
import random
from datetime import datetime, timezone
from pathlib import Path

import jsonschema
import pytest

from lib.messages_line import (
    LOG_REGISTRY_SCHEMA,
    MESSAGE_RECEIPT_SCHEMA,
    MESSAGES_CONFIG_SCHEMA,
    MessageLineError,
    MessagesLine,
    ReceiptConflictError,
    message_delivery_state,
    selected_targets,
    validate_log_registry,
    validate_message_receipt,
    validate_message_ticket,
    validate_messages_config,
)
from lib.ticket_writer import draw_id


ROOT = Path(__file__).resolve().parents[1]
SCHEMA_DIR = ROOT / "contracts" / "messages" / "v1"


def schema(name):
    return json.loads((SCHEMA_DIR / name).read_text(encoding="utf-8"))


def assert_schema_valid(contract, value):
    jsonschema.Draft202012Validator(
        contract, format_checker=jsonschema.FormatChecker()
    ).validate(value)


def config(**changes):
    value = {
        "schema": MESSAGES_CONFIG_SCHEMA,
        "updated_at": "2026-10-27T12:00:00Z",
        "enabled": True,
        "delivery_modes": {
            "chat_start": True,
            "on_demand": True,
            "active_chat": True,
        },
        "targets": [
            {"target_ref": "fixture:ticket-master", "surface": "ticket_master"},
            {"target_ref": "fixture:local-gui", "surface": "local_gui"},
        ],
        "gui_open_counts_as_delivery": True,
        "message_content_logging": False,
        "audio_logging": False,
        "transport_contract_ref": "ellmos.ticket.route-intent.v1",
    }
    value.update(changes)
    return value


def receipt(
    message,
    event,
    *,
    target=None,
    recipient_ref=None,
    observed_at=None,
    gui_open_counts_as_delivery=False,
):
    target_ref, surface = target or ("fixture:local-gui", "local_gui")
    receipt_base = {
        "schema": MESSAGE_RECEIPT_SCHEMA,
        "receipt_id": message["receipt_id"],
        "message_id": message["message_id"],
        "recipient_ref": recipient_ref or message["recipient_ref"],
        "target_ref": target_ref,
        "surface": surface,
        "channel_ref": "fixture:channel:local-gui",
        "event": event,
        "observed_at": observed_at or "2026-10-27T12:01:00Z",
        "gui_open_counts_as_delivery": gui_open_counts_as_delivery,
        "actor_ref": "fixture:actor:operator",
        "evidence_ref": f"fixture:evidence:{event}",
    }
    observation_id = "O-" + hashlib.sha256(
        json.dumps(receipt_base, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return {**receipt_base, "observation_id": observation_id}


def make_message(tmp_path, *, gui_open_counts=True, mode="on_demand", thread_id=None):
    settings = config(gui_open_counts_as_delivery=gui_open_counts)
    line = MessagesLine(tmp_path / "messages")
    message = line.create(
        config=settings,
        origin_ref="fixture:system:automation",
        recipient_ref="fixture:principal:user",
        content_text="Fixture message text.",
        delivery_mode=mode,
        thread_id=thread_id,
        created_at=datetime(2026, 10, 27, 12, 0, tzinfo=timezone.utc),
        rng=random.Random(14),
    )
    return line, message


def test_example_files_are_valid_closed_contracts():
    cases = [
        (
            "ellmos.ticket.messages-line-config.v1.schema.json",
            ROOT / "config" / "messages-line.example.json",
            validate_messages_config,
        ),
        (
            "ellmos.ticket.log-registry.v1.schema.json",
            ROOT / "config" / "log-registry.example.json",
            validate_log_registry,
        ),
    ]
    for schema_name, example_path, validator in cases:
        contract = schema(schema_name)
        example = json.loads(example_path.read_text(encoding="utf-8"))
        assert_schema_valid(contract, example)
        assert validator(example)
        if example["schema"] == MESSAGES_CONFIG_SCHEMA:
            assert example["gui_open_counts_as_delivery"] is True


def test_disabled_config_routes_nothing_and_content_logging_cannot_be_enabled():
    disabled = config(enabled=False)
    assert selected_targets(disabled, "on_demand") == []

    disabled["message_content_logging"] = True
    with pytest.raises(MessageLineError, match="content logging"):
        validate_messages_config(disabled)

    disabled = config()
    disabled["audio_logging"] = True
    with pytest.raises(MessageLineError, match="audio logging"):
        validate_messages_config(disabled)


def test_modes_and_target_surfaces_are_configurable_without_endpoint_values():
    settings = config(
        delivery_modes={"chat_start": False, "on_demand": True, "active_chat": False}
    )
    assert selected_targets(settings, "on_demand") == settings["targets"]
    assert selected_targets(settings, "chat_start") == []
    settings["targets"] = [{"target_ref": "fixture:server", "surface": "server_gui"}]
    assert selected_targets(settings, "on_demand")[0]["surface"] == "server_gui"
    assert "endpoint" not in settings["targets"][0]


def test_current_global_off_and_target_removal_override_saved_target_snapshot(tmp_path):
    line, message = make_message(tmp_path)
    assert line.delivery_targets(message["message_id"], config()) == message["target_snapshot"]

    assert line.delivery_targets(message["message_id"], config(enabled=False)) == []
    assert line.delivery_targets(
        message["message_id"],
        config(delivery_modes={"chat_start": True, "on_demand": False, "active_chat": True}),
    ) == []
    current = config(targets=[{"target_ref": "fixture:local-gui", "surface": "local_gui"}])
    assert line.delivery_targets(message["message_id"], current) == current["targets"]
    current["targets"].append({"target_ref": "fixture:new-server", "surface": "server_gui"})
    assert line.delivery_targets(message["message_id"], current) == [
        {"target_ref": "fixture:local-gui", "surface": "local_gui"}
    ]


def test_administrative_list_and_read_do_not_ack_or_bypass_delivery_gate(tmp_path):
    line, message = make_message(tmp_path)
    disabled = config(enabled=False, updated_at="2026-10-27T12:02:00Z")

    eligible = line.list_for_delivery(
        delivery_mode="on_demand",
        target_ref="fixture:local-gui",
        surface="local_gui",
        current_config=config(),
    )
    assert [item["message_id"] for item in eligible] == [message["message_id"]]
    assert line.read_for_delivery(
        message["message_id"],
        target_ref="fixture:local-gui",
        surface="local_gui",
        current_config=config(),
    )["content_text"] == "Fixture message text."

    assert [item["message_id"] for item in line.list_messages()] == [message["message_id"]]
    assert line.get(message["message_id"])["content_text"] == "Fixture message text."
    assert line.by_thread(message["thread_id"])[0]["message_id"] == message["message_id"]
    assert line.receipts(message["message_id"]) == []
    assert line.delivery_state(message["message_id"]) == "prepared"

    assert line.list_for_delivery(
        delivery_mode="on_demand",
        target_ref="fixture:local-gui",
        surface="local_gui",
        current_config=disabled,
    ) == []
    with pytest.raises(MessageLineError, match="current config"):
        line.read_for_delivery(
            message["message_id"],
            target_ref="fixture:local-gui",
            surface="local_gui",
            current_config=disabled,
        )
    assert line.delivery_state(message["message_id"]) == "prepared"


def test_current_config_gates_active_chat_and_gui_open_ack_entrypoints(tmp_path):
    line, active_message = make_message(tmp_path, mode="active_chat")
    active_off = config(
        delivery_modes={"chat_start": True, "on_demand": True, "active_chat": False},
        updated_at="2026-10-27T12:02:00Z",
    )
    with pytest.raises(MessageLineError, match="current config"):
        line.record_observation(
            active_message["message_id"],
            target_ref="fixture:local-gui",
            surface="local_gui",
            channel_ref="fixture:channel:local-gui",
            actor_ref="fixture:actor:operator",
            evidence_ref="fixture:evidence:active-chat-ack",
            event="active_chat_ack",
            current_config=active_off,
            observed_at="2026-10-27T12:03:00Z",
        )
    assert line.delivery_state(active_message["message_id"]) == "prepared"

    line, gui_message = make_message(tmp_path / "gui", gui_open_counts=True)
    gui_off = config(
        gui_open_counts_as_delivery=False,
        updated_at="2026-10-27T12:02:00Z",
    )
    with pytest.raises(MessageLineError, match="GUI-open"):
        line.record_observation(
            gui_message["message_id"],
            target_ref="fixture:local-gui",
            surface="local_gui",
            channel_ref="fixture:channel:local-gui",
            actor_ref="fixture:actor:operator",
            evidence_ref="fixture:evidence:gui-open",
            event="gui_opened",
            current_config=gui_off,
            observed_at="2026-10-27T12:03:00Z",
        )
    assert line.delivery_state(gui_message["message_id"]) == "prepared"

    line, demand_message = make_message(tmp_path / "demand", mode="on_demand")
    demand_off = config(
        enabled=False,
        updated_at="2026-10-27T12:02:00Z",
    )
    with pytest.raises(MessageLineError, match="current config"):
        line.record_observation(
            demand_message["message_id"],
            target_ref="fixture:local-gui",
            surface="local_gui",
            channel_ref="fixture:channel:local-gui",
            actor_ref="fixture:actor:operator",
            evidence_ref="fixture:evidence:on-demand-ack",
            event="on_demand_ack",
            current_config=demand_off,
            observed_at="2026-10-27T12:03:00Z",
        )
    with pytest.raises(MessageLineError, match="current config"):
        line.record_observation(
            demand_message["message_id"],
            target_ref="fixture:local-gui",
            surface="local_gui",
            channel_ref="fixture:channel:local-gui",
            actor_ref="fixture:runtime",
            evidence_ref="fixture:evidence:backdated-automatic-ack",
            event="on_demand_ack",
            current_config=demand_off,
            observed_at="2026-10-27T12:01:00Z",
        )
    assert line.delivery_state(demand_message["message_id"]) == "prepared"


def test_removed_target_blocks_delivery_read_and_automatic_observation(tmp_path):
    line, message = make_message(tmp_path)
    route_removed = config(
        targets=[{"target_ref": "fixture:ticket-master", "surface": "ticket_master"}],
        updated_at="2026-10-27T12:02:00Z",
    )
    assert line.list_for_delivery(
        delivery_mode="on_demand",
        target_ref="fixture:local-gui",
        surface="local_gui",
        current_config=route_removed,
    ) == []
    with pytest.raises(MessageLineError, match="current config"):
        line.read_for_delivery(
            message["message_id"],
            target_ref="fixture:local-gui",
            surface="local_gui",
            current_config=route_removed,
        )
    with pytest.raises(MessageLineError, match="current config"):
        line.record_observation(
            message["message_id"],
            target_ref="fixture:local-gui",
            surface="local_gui",
            channel_ref="fixture:channel:local-gui",
            actor_ref="fixture:transport",
            evidence_ref="fixture:evidence:transported",
            event="transported",
            current_config=route_removed,
            observed_at="2026-10-27T12:03:00Z",
        )
    assert line.delivery_state(message["message_id"]) == "prepared"


def test_historical_gui_open_ack_survives_a_later_disable_for_audit(tmp_path):
    line, message = make_message(tmp_path, gui_open_counts=True)
    opened = line.record_observation(
        message["message_id"],
        target_ref="fixture:local-gui",
        surface="local_gui",
        channel_ref="fixture:channel:local-gui",
        actor_ref="fixture:gui",
        evidence_ref="fixture:evidence:gui-open",
        event="gui_opened",
        current_config=config(gui_open_counts_as_delivery=True),
        observed_at="2026-10-27T12:01:00Z",
    )
    assert opened["gui_open_counts_as_delivery"] is True
    assert line.delivery_state(message["message_id"]) == "delivered"

    disabled = config(
        enabled=False,
        gui_open_counts_as_delivery=False,
        updated_at="2026-10-27T12:02:00Z",
    )
    assert line.delivery_targets(message["message_id"], disabled) == []
    assert line.delivery_state(message["message_id"]) == "delivered"
    assert line.receipts(message["message_id"])[0]["gui_open_counts_as_delivery"] is True


def test_manual_ack_when_disabled_is_narrow_and_does_not_reenable_routing(tmp_path):
    line, message = make_message(tmp_path)
    disabled = config(enabled=False, updated_at="2026-10-27T12:02:00Z")
    with pytest.raises(MessageLineError, match=r"acknowledge\(\)"):
        line.record_observation(
            message["message_id"],
            target_ref="fixture:local-gui",
            surface="local_gui",
            channel_ref="fixture:channel:local-gui",
            actor_ref="fixture:runtime",
            evidence_ref="fixture:evidence:mislabelled-automatic-ack",
            event="explicit_ack",
            current_config=disabled,
            observed_at="2026-10-27T12:03:00Z",
        )
    ack = line.acknowledge(
        message["message_id"],
        target_ref="fixture:local-gui",
        surface="local_gui",
        channel_ref="fixture:channel:local-gui",
        actor_ref="fixture:actor:operator",
        evidence_ref="fixture:evidence:manual-ack",
        current_config=disabled,
        observed_at="2026-10-27T12:03:00Z",
    )
    assert ack["event"] == "explicit_ack"
    assert line.delivery_state(message["message_id"]) == "delivered"
    assert line.list_for_delivery(
        delivery_mode="on_demand",
        target_ref="fixture:local-gui",
        surface="local_gui",
        current_config=disabled,
    ) == []


def test_unverified_historical_receipt_import_is_rejected_after_config_disable(tmp_path):
    line, message = make_message(tmp_path)
    historical = receipt(
        message,
        "on_demand_ack",
        observed_at="2026-10-27T12:01:00Z",
    )
    disabled = config(enabled=False, updated_at="2026-10-27T12:02:00Z")

    with pytest.raises(MessageLineError, match="current config"):
        line.record_receipt(historical, current_config=disabled)
    assert line.delivery_state(message["message_id"]) == "prepared"


def test_message_ids_use_ticket_writer_library_contract():
    class FixedRandom:
        def randrange(self, _start, _stop):
            return 123456789

    assert draw_id("M", "20261027", set(), FixedRandom()) == "M-20261027-123456789"
    with pytest.raises(ValueError, match="real calendar date"):
        draw_id("M", "20261340", set(), FixedRandom())


def test_messages_get_stable_ids_and_thread_grouping_preserve_message_text(tmp_path):
    line, first = make_message(tmp_path)
    assert first["message_id"].startswith("M-20261027-")
    assert first["thread_id"].startswith("TH-20261027-")
    assert line.get(first["message_id"]) == first
    assert first["content_text"] == "Fixture message text."
    assert "body" not in first
    assert "audio" not in first
    assert len(list((tmp_path / "messages").glob("*.json"))) == 1

    second = line.create(
        config=config(),
        origin_ref="fixture:system:automation",
        recipient_ref=first["recipient_ref"],
        content_text="Second fixture message.",
        delivery_mode="active_chat",
        thread_id=first["thread_id"],
        created_at=datetime(2026, 10, 27, 12, 2, tzinfo=timezone.utc),
        rng=random.Random(18),
    )
    assert second["message_id"] != first["message_id"]
    assert second["receipt_id"] != first["receipt_id"]
    assert second["thread_id"] == first["thread_id"]
    assert [item["message_id"] for item in line.by_thread(first["thread_id"])] == [
        first["message_id"],
        second["message_id"],
    ]


def test_message_create_uses_exclusive_ids_and_recovers_from_a_local_collision(tmp_path, monkeypatch):
    line, first = make_message(tmp_path)
    candidates = iter(
        [
            first["message_id"],
            "R-20261027-123456780",
            "M-20261027-123456780",
            "R-20261027-123456781",
        ]
    )

    def scripted_draw(prefix, _datestr, _used, _rng=None):
        candidate = next(candidates)
        assert candidate.startswith(prefix + "-")
        return candidate

    monkeypatch.setattr("lib.messages_line.draw_id", scripted_draw)
    second = line.create(
        config=config(),
        origin_ref="fixture:system:automation",
        recipient_ref=first["recipient_ref"],
        content_text="A second message after an ID collision.",
        delivery_mode="on_demand",
        thread_id=first["thread_id"],
        created_at=datetime(2026, 10, 27, 12, 4, tzinfo=timezone.utc),
    )

    assert second["message_id"] == "M-20261027-123456780"
    assert second["receipt_id"] == "R-20261027-123456781"
    assert line.get(first["message_id"]) == first


def test_message_ticket_is_closed_and_rejects_host_paths_and_body_fields(tmp_path):
    _line, message = make_message(tmp_path)
    assert_schema_valid(schema("ellmos.ticket.message-ticket.v1.schema.json"), message)
    assert_schema_valid(
        schema("ellmos.ticket.message-thread-ledger.v1.schema.json"),
        json.loads(
            (tmp_path / "messages" / "_threads" / f"{message['thread_id']}.json").read_text(
                encoding="utf-8"
            )
        ),
    )

    with pytest.raises(MessageLineError, match="invalid fields"):
        validate_message_ticket({**message, "body": "private content"})
    with pytest.raises(MessageLineError, match="message text"):
        validate_message_ticket({**message, "content_text": ""})
    with pytest.raises(MessageLineError, match="real calendar date"):
        validate_message_ticket({**message, "message_id": "M-20261340-123456789"})


@pytest.mark.parametrize(
    ("event", "expected"),
    [
        ("prepared", "prepared"),
        ("offered", "offered"),
        ("transported", "transported"),
        ("fetched", "fetched"),
        ("listed", "listed"),
        ("previewed", "previewed"),
        ("gui_opened", "gui_opened"),
        ("explicit_ack", "delivered"),
        ("on_demand_ack", "delivered"),
        ("active_chat_ack", "delivered"),
    ],
)
def test_delivery_projection_distinguishes_transport_presentation_and_ack(
    tmp_path, event, expected
):
    mode = "active_chat" if event == "active_chat_ack" else "on_demand"
    _line, message = make_message(tmp_path, mode=mode)
    observed = receipt(message, event)
    assert_schema_valid(schema("ellmos.ticket.message-receipt.v1.schema.json"), observed)
    assert validate_message_receipt(observed) == observed
    assert message_delivery_state(message, [observed]) == expected


def test_gui_open_counts_only_when_the_message_saved_policy_allows_it(tmp_path):
    _line, disabled_rule = make_message(tmp_path / "default", gui_open_counts=False)
    opened = receipt(disabled_rule, "gui_opened")
    assert message_delivery_state(disabled_rule, [opened]) == "gui_opened"

    _line, enabled_rule = make_message(tmp_path / "enabled", gui_open_counts=True)
    opened = receipt(enabled_rule, "gui_opened", gui_open_counts_as_delivery=True)
    assert message_delivery_state(enabled_rule, [opened]) == "delivered"
    missing_event_policy = receipt(enabled_rule, "gui_opened")
    assert message_delivery_state(enabled_rule, [missing_event_policy]) == "gui_opened"


def test_local_create_resolve_ack_restart_and_cross_channel_delivery_are_idempotent(tmp_path):
    line, first = make_message(tmp_path)
    second = line.create(
        config=config(),
        origin_ref="fixture:system:automation",
        recipient_ref=first["recipient_ref"],
        content_text="Different message in the same thread.",
        delivery_mode="active_chat",
        thread_id=first["thread_id"],
        created_at=datetime(2026, 10, 27, 12, 2, tzinfo=timezone.utc),
        rng=random.Random(18),
    )
    transported = line.record_observation(
        first["message_id"],
        target_ref="fixture:local-gui",
        surface="local_gui",
        channel_ref="fixture:channel:local-gui",
        actor_ref="fixture:transport",
        evidence_ref="fixture:evidence:transported",
        event="transported",
        observed_at="2026-10-27T12:01:00Z",
        current_config=config(),
    )
    assert line.delivery_state(first["message_id"]) == "transported"
    assert line.delivery_state(second["message_id"]) == "prepared"
    ledger_path = (
        tmp_path
        / "messages"
        / "_receipts"
        / first["receipt_id"]
        / "ledger.json"
    )
    ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
    assert_schema_valid(schema("ellmos.ticket.message-receipt-ledger.v1.schema.json"), ledger)

    restarted = MessagesLine(tmp_path / "messages")
    resolved = restarted.get(first["message_id"])
    assert resolved["content_text"] == "Fixture message text."
    disabled = config(enabled=False, updated_at="2026-10-27T12:02:00Z")
    assert restarted.record_receipt(transported, current_config=disabled) == transported
    assert restarted.delivery_state(first["message_id"]) == "transported"
    gui_ack = restarted.acknowledge(
        first["message_id"],
        target_ref="fixture:local-gui",
        surface="local_gui",
        channel_ref="fixture:channel:local-gui",
        actor_ref="fixture:actor:operator",
        evidence_ref="fixture:evidence:gui-ack",
        current_config=disabled,
        observed_at="2026-10-27T12:03:00Z",
    )
    assert gui_ack["receipt_id"] == first["receipt_id"]
    assert restarted.delivery_state(first["message_id"]) == "delivered"

    server_ack = restarted.acknowledge(
        first["message_id"],
        target_ref="fixture:ticket-master",
        surface="ticket_master",
        channel_ref="fixture:channel:ticket-master",
        actor_ref="fixture:actor:operator",
        evidence_ref="fixture:evidence:server-ack",
        current_config=disabled,
        observed_at="2026-10-27T12:03:01Z",
    )
    replay = restarted.acknowledge(
        first["message_id"],
        target_ref="fixture:local-gui",
        surface="local_gui",
        channel_ref="fixture:channel:local-gui",
        actor_ref="fixture:actor:operator",
        evidence_ref="fixture:evidence:gui-ack",
        current_config=disabled,
        observed_at="2026-10-27T12:03:00Z",
    )
    assert server_ack["receipt_id"] == gui_ack["receipt_id"]
    assert replay == gui_ack
    assert len(restarted.receipts(first["message_id"])) == 3
    assert restarted.delivery_state(first["message_id"]) == "delivered"
    assert restarted.delivery_state(second["message_id"]) == "prepared"
    with pytest.raises(ReceiptConflictError, match="reused"):
        restarted.record_receipt(
            {**transported, "evidence_ref": "fixture:evidence:changed"},
            current_config=disabled,
        )


def test_receipt_replay_is_idempotent_and_conflicting_observation_fails_closed(tmp_path):
    _line, message = make_message(tmp_path, mode="active_chat")
    ack = receipt(message, "active_chat_ack")
    assert message_delivery_state(message, [ack]) == message_delivery_state(message, [ack, ack])

    conflict = {**ack, "evidence_ref": "fixture:evidence:changed"}
    with pytest.raises(ReceiptConflictError, match="reused"):
        message_delivery_state(message, [ack, conflict])


def test_receipts_for_another_recipient_or_unconfigured_target_do_not_deliver(tmp_path):
    _line, message = make_message(tmp_path)
    wrong_recipient = receipt(
        message, "explicit_ack", recipient_ref="fixture:other"
    )
    wrong_target = receipt(
        message,
        "explicit_ack",
        target=("fixture:unknown", "server_gui"),
    )
    assert message_delivery_state(message, [wrong_recipient, wrong_target]) == "prepared"


def test_log_registry_is_pointer_only_and_supports_24h_and_monthly_sources():
    registry = {
        "schema": LOG_REGISTRY_SCHEMA,
        "sources": [
            {
                "source_ref": "fixture:log:24h",
                "system_ref": "fixture:system:a",
                "owner_ref": "fixture:owner:a",
                "window": "24h",
                "observed_at": "2026-10-27T12:00:00Z",
                "access_policy_ref": "fixture:policy:access",
                "redaction_policy_ref": "fixture:policy:redaction",
                "provenance_ref": "fixture:provenance:1",
                "retention_ref": "fixture:retention:short",
                "materialization": "reference_only",
            },
            {
                "source_ref": "fixture:log:monthly",
                "system_ref": "fixture:system:b",
                "owner_ref": "fixture:owner:b",
                "window": "monthly",
                "observed_at": "2026-10-27T12:00:00Z",
                "access_policy_ref": "fixture:policy:access",
                "redaction_policy_ref": "fixture:policy:redaction",
                "provenance_ref": "fixture:provenance:2",
                "retention_ref": "fixture:retention:monthly",
                "materialization": "reference_only",
            },
        ],
    }
    assert_schema_valid(schema("ellmos.ticket.log-registry.v1.schema.json"), registry)
    assert validate_log_registry(registry) == registry
    with pytest.raises(MessageLineError, match="invalid fields"):
        validate_log_registry({**registry, "raw_log_content": "private"})
    with pytest.raises(MessageLineError, match="pointer-only"):
        validate_log_registry(
            {**registry, "sources": [{**registry["sources"][0], "materialization": "copy"}]}
        )

