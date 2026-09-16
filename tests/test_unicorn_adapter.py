"""Contract tests for the inert Web/Tray Unicorn adapter."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

LIB = Path(__file__).resolve().parents[1] / "lib"
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))

import routing_contract as rc  # noqa: E402
import unicorn_adapter as adapter  # noqa: E402
from queue_helpers import verified_queue  # noqa: E402


REGISTRY = {
    "source": "fixture://unicorn-systems",
    "checked_at": "2026-09-16T08:00:00Z",
    "systems": {"ASUS-GEI": {"active": True, "slot": "laptop"}},
}


class FixedRandom:
    def randrange(self, *_args, **_kwargs):
        return 123456789


def request(**overrides):
    value = {
        "title": "Neue Anfrage",
        "body": "Bitte als Ticket aufnehmen.",
        "project": "ticket-master",
        "priority": "hoch",
        "pipeline": ".AI/ticket-master",
    }
    value.update(overrides)
    return value


def test_form_is_accessible_and_contains_only_public_fields():
    html = adapter.render_form()

    assert '<html lang="de">' in html
    assert 'aria-describedby="form-help"' in html
    assert set(name for name in adapter._FORM_FIELDS if f'name="{name}"' in html) == set(adapter._FORM_FIELDS)
    for forbidden in ("worker", "task", "claim", "model", "lock", "transport"):
        assert f'name="{forbidden}"' not in html.lower()


def test_web_dispatch_exposes_form_preview_and_submit_only(tmp_path):
    form = adapter.web_dispatch("GET", adapter.FORM_PATH)
    assert form.status_code == 200
    assert form.content_type.startswith("text/html")

    preview = adapter.web_dispatch("POST", adapter.PREVIEW_PATH, request())
    assert preview.status_code == 200
    preview_object = json.loads(preview.body)
    assert preview_object["schema"] == "ellmos.unicorn.intake-preview.v1"
    assert "worker" not in preview.body.lower()

    verified_queue(tmp_path)
    receipt = adapter.web_dispatch(
        "POST",
        adapter.SUBMIT_PATH,
        request(request_id="web-42"),
        tickets_dir=tmp_path,
        registry_snapshot=REGISTRY,
        today="2026-09-16",
        rng=FixedRandom(),
    )
    assert receipt.status_code == 200
    receipt_object = json.loads(receipt.body)
    assert set(receipt_object) == {"schema", "ticket_id", "intake_idempotency_key", "route_intent"}
    assert receipt_object["schema"] == "ellmos.unicorn.intake-receipt.v1"
    ticket = next((tmp_path / "INBOX").glob("T-*.txt"))
    assert rc.contract_errors(ticket) == []


def test_web_dispatch_rejects_internal_fields_and_unknown_routes():
    rejected = adapter.web_dispatch("POST", adapter.PREVIEW_PATH, request(worker="start"))
    assert rejected.status_code == 422
    assert json.loads(rejected.body)["schema"] == adapter.ERROR_SCHEMA

    missing = adapter.web_dispatch("POST", adapter.SUBMIT_PATH, request())
    assert missing.status_code == 503

    unknown = adapter.web_dispatch("GET", "/unicorn/internal")
    assert unknown.status_code == 404


def test_tray_uses_the_same_public_core_and_is_idempotent(tmp_path):
    verified_queue(tmp_path)
    first = adapter.tray_submit(
        request(request_id="tray-42"),
        tickets_dir=tmp_path,
        registry_snapshot=REGISTRY,
        today="2026-09-16",
        rng=FixedRandom(),
    )
    second = adapter.tray_submit(
        request(request_id="tray-42"),
        tickets_dir=tmp_path,
        registry_snapshot=REGISTRY,
        today="2026-09-16",
        rng=FixedRandom(),
    )
    assert first == second
    assert adapter.tray_preview(request())["schema"] == "ellmos.unicorn.intake-preview.v1"
    assert len(list((tmp_path / "INBOX").glob("T-*.txt"))) == 1


@pytest.mark.parametrize("method,path", [("GET", adapter.PREVIEW_PATH), ("POST", adapter.FORM_PATH)])
def test_public_endpoints_fail_closed_on_wrong_method(method, path):
    response = adapter.web_dispatch(method, path, request())
    assert response.status_code == 405
