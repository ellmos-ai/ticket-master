"""Phase-2 Unicorn one-way intake contract tests."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

LIB = Path(__file__).resolve().parents[1] / "lib"
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))

import routing_contract as rc  # noqa: E402
import unicorn_intake as intake  # noqa: E402
from queue_helpers import verified_queue  # noqa: E402


REGISTRY = {
    "source": "fixture://unicorn-systems",
    "checked_at": "2026-09-16T08:00:00Z",
    "systems": {
        "ASUS-GEI": {"active": True, "slot": "laptop"},
    },
}


class FixedRandom:
    def randrange(self, *_args, **_kwargs):
        return 123456789


def request(**overrides):
    value = {
        "title": "Neue Anfrage",
        "body": "Bitte als Ticket aufnehmen.\r\nMit zweiter Zeile.",
        "project": "ticket-master",
        "priority": "hoch",
        "pipeline": ".AI/ticket-master",
    }
    value.update(overrides)
    return value


def test_preview_is_strict_public_boundary_and_deterministic():
    first = intake.preview_intake(request())
    second = intake.preview_intake(request(body="Bitte als Ticket aufnehmen.\nMit zweiter Zeile."))

    assert first == second
    assert set(first) == {
        "schema", "title", "body", "project", "priority", "pipeline",
        "intake_idempotency_key", "route_intent_schema",
    }
    assert first["body"] == "Bitte als Ticket aufnehmen.\nMit zweiter Zeile."
    assert "worker" not in json.dumps(first).lower()
    assert "task" not in json.dumps(first).lower()


@pytest.mark.parametrize(
    "bad",
    [
        {"title": "x", "body": "y", "worker": "start"},
        {"title": "x\n", "body": "y"},
        {"title": "x", "body": "y\x00"},
        {"title": "", "body": "y"},
    ],
)
def test_preview_rejects_unsupported_or_unsafe_input(bad):
    with pytest.raises(intake.IntakeValidationError):
        intake.preview_intake(bad)


def test_submit_is_idempotent_and_returns_only_a_valid_intake_receipt(tmp_path):
    verified_queue(tmp_path)
    first = intake.submit_intake(
        request(request_id="unicorn-request-42"),
        tickets_dir=tmp_path,
        registry_snapshot=REGISTRY,
        today="2026-09-16",
        rng=FixedRandom(),
    )
    second = intake.submit_intake(
        request(request_id="unicorn-request-42"),
        tickets_dir=tmp_path,
        registry_snapshot=REGISTRY,
        today="2026-09-16",
        rng=FixedRandom(),
    )

    assert first == second
    assert set(first) == {"schema", "ticket_id", "intake_idempotency_key", "route_intent"}
    assert first["schema"] == intake.RECEIPT_SCHEMA
    assert first["route_intent"] == intake.validate_route_intent(first["route_intent"])
    assert "worker" not in json.dumps(first).lower()
    assert "task" not in json.dumps(first).lower()
    files = list((tmp_path / "INBOX").glob("T-*.txt"))
    assert len(files) == 1
    assert rc.contract_errors(files[0]) == []


def test_same_request_id_with_changed_content_fails_closed_without_second_ticket(tmp_path):
    verified_queue(tmp_path)
    intake.submit_intake(
        request(request_id="unicorn-request-43"),
        tickets_dir=tmp_path,
        registry_snapshot=REGISTRY,
        today="2026-09-16",
        rng=FixedRandom(),
    )

    with pytest.raises(ValueError, match="idempotency key already belongs"):
        intake.submit_intake(
            request(request_id="unicorn-request-43", body="Andere Anfrage"),
            tickets_dir=tmp_path,
            registry_snapshot=REGISTRY,
            today="2026-09-16",
            rng=FixedRandom(),
        )
    assert len(list((tmp_path / "INBOX").glob("T-*.txt"))) == 1


def test_route_intent_rejects_extra_worker_or_transport_fields():
    valid = {
        "route_intent": intake.ROUTE_INTENT_SCHEMA,
        "ticket_id": "T-20260916-123456789",
        "target_snapshot": {
            "kind": "any",
            "systems": [],
            "at": "2026-09-16T08:00:00Z",
            "source": "fixture://systems",
            "fingerprint": "sha256:" + "a" * 64,
        },
        "receipt_to": "T-20260916-123456789",
        "idempotency_key": "sha256:" + "b" * 64,
    }
    assert intake.validate_route_intent(valid) == valid
    with pytest.raises(intake.IntakeValidationError):
        intake.validate_route_intent({**valid, "worker": "run"})


def test_route_intent_rejects_non_string_target_kind_without_leaking_type_error():
    valid = {
        "route_intent": intake.ROUTE_INTENT_SCHEMA,
        "ticket_id": "T-20260916-123456789",
        "target_snapshot": {
            "kind": [],
            "systems": [],
            "at": "2026-09-16T08:00:00Z",
            "source": "fixture://systems",
            "fingerprint": "sha256:" + "a" * 64,
        },
        "receipt_to": "T-20260916-123456789",
        "idempotency_key": "sha256:" + "b" * 64,
    }
    with pytest.raises(intake.IntakeValidationError, match="target kind"):
        intake.validate_route_intent(valid)
