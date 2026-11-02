"""Tests for configurable ticket bundles and bidirectional bundles-reg.json pointers."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import jsonschema
import pytest

from lib.ticket_bundles import (
    BundlesRegistry,
    PreimageConflictError,
    TicketBundleError,
    normalize_ticket_id,
    parse_ticket_metadata,
    suggest_bundles,
    validate_bundles_registry,
)

ROOT = Path(__file__).resolve().parents[1]


def _write_ticket(
    folder: Path,
    filename: str,
    *,
    ticket_id: str,
    status: str = "ACTIONABLE",
    pipeline: str = "umbruch",
    project_dir: str = "C:/Users/User/OneDrive/.TOPICS/.UMBRUCH",
    delegated_to: str = "",
) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / filename
    lines = [
        "==============================================================",
        "TICKET",
        "==============================================================",
        f"ID:            {ticket_id}",
        "TITEL:         Umbruch-Testartikel",
        f"STATUS:        {status}",
        f"PIPELINE:      {pipeline}",
        f"PROJEKTORDNER: {project_dir}",
    ]
    if delegated_to:
        lines.append(f"DELEGIERT_AN:  {delegated_to}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def test_normalize_ticket_id_strips_host_and_lifecycle_path():
    assert (
        normalize_ticket_id("ACTIONABLE/T-20261027-369717637.ASUS-GEI.txt")
        == "T-20261027-369717637"
    )
    assert (
        normalize_ticket_id(Path("C:/tickets/QUEUED/T-20261025-777054661.WORKSTATION-LG.txt"))
        == "T-20261025-777054661"
    )


def test_config_disabled_blocks_planning_and_creation_but_preserves_history_read(tmp_path):
    actionable = tmp_path / "ACTIONABLE"
    t1 = _write_ticket(
        actionable, "T-20261027-111111111.ASUS-GEI.txt", ticket_id="T-20261027-111111111"
    )
    t2 = _write_ticket(
        actionable, "T-20261027-222222222.ASUS-GEI.txt", ticket_id="T-20261027-222222222"
    )
    reg = BundlesRegistry(tmp_path / "bundles-reg.json")

    with pytest.raises(TicketBundleError, match="disabled"):
        suggest_bundles([t1, t2], actor_ref="codex@ASUS-GEI", config={"bundles": {"enabled": False}})

    with pytest.raises(TicketBundleError, match="disabled"):
        reg.create_bundle(
            ticket_paths=[t1, t2],
            actor_ref="codex@ASUS-GEI",
            rationale="Umbruch-Paket",
            source_ref="ticket-master",
            scope_key="umbruch:.umbruch",
            config={"bundles": {"enabled": False}},
        )

    loaded, _ = reg.load()
    assert loaded["bundles"] == []


def test_bidirectional_pointers_idempotency_and_schema_validation(tmp_path):
    actionable = tmp_path / "ACTIONABLE"
    t1 = _write_ticket(
        actionable, "T-20261027-111111111.ASUS-GEI.txt", ticket_id="T-20261027-111111111"
    )
    t2 = _write_ticket(
        actionable, "T-20261027-222222222.WORKSTATION-LG.txt", ticket_id="T-20261027-222222222"
    )
    cfg = {"bundles": {"enabled": True}}
    suggestions = suggest_bundles([t1, t2], actor_ref="codex@ASUS-GEI", config=cfg)
    assert len(suggestions) == 1
    assert suggestions[0]["ticket_ids"] == ["T-20261027-111111111", "T-20261027-222222222"]

    reg = BundlesRegistry(tmp_path / "bundles-reg.json")
    now = datetime(2026, 10, 31, 12, 0, tzinfo=timezone.utc)
    bundle = reg.create_bundle(
        ticket_paths=[t1, t2],
        actor_ref="codex@ASUS-GEI",
        rationale="Gemeinsamer Umbruch-Kontext mit echten Umlauten äöüß",
        source_ref="ticket-master",
        scope_key="umbruch:.umbruch",
        config=cfg,
        now=now,
    )
    # Idempotent replay returns identical bundle without duplicating
    replay = reg.create_bundle(
        ticket_paths=[t1, t2],
        actor_ref="codex@ASUS-GEI",
        rationale="Gemeinsamer Umbruch-Kontext mit echten Umlauten äöüß",
        source_ref="ticket-master",
        scope_key="umbruch:.umbruch",
        config=cfg,
        now=now,
    )
    assert replay["bundle_id"] == bundle["bundle_id"]

    loaded, _ = reg.load()
    schema = json.loads(
        (ROOT / "contracts" / "bundles" / "v1" / "ellmos.ticket.bundles-reg.v1.schema.json").read_text(
            encoding="utf-8"
        )
    )
    jsonschema.Draft202012Validator(schema).validate(loaded)
    assert loaded["ticket_pointers"] == {
        "T-20261027-111111111": bundle["bundle_id"],
        "T-20261027-222222222": bundle["bundle_id"],
    }
    assert parse_ticket_metadata(t1)["bundle_id"].startswith(bundle["bundle_id"])
    assert parse_ticket_metadata(t2)["bundle_id"].startswith(bundle["bundle_id"])

    # Pointer corruption is caught fail-closed
    corrupted = dict(loaded)
    corrupted["ticket_pointers"] = {"T-20261027-111111111": bundle["bundle_id"]}
    with pytest.raises(TicketBundleError, match="bidirectional pointer mismatch"):
        validate_bundles_registry(corrupted)


def test_rejects_parked_foreign_claimed_locked_and_stale_preimages(tmp_path):
    actionable = tmp_path / "ACTIONABLE"
    parked = tmp_path / "PARKED"
    t_ok = _write_ticket(
        actionable, "T-20261027-111111111.txt", ticket_id="T-20261027-111111111"
    )
    t_parked = _write_ticket(
        parked, "T-20261027-333333333.txt", ticket_id="T-20261027-333333333", status="PARKED"
    )
    t_foreign = _write_ticket(
        actionable,
        "T-20261027-444444444.txt",
        ticket_id="T-20261027-444444444",
        delegated_to="other-agent@ASUS-GEI",
    )
    cfg = {"bundles": {"enabled": True}}
    reg = BundlesRegistry(tmp_path / "bundles-reg.json")

    with pytest.raises(TicketBundleError, match="not eligible"):
        reg.create_bundle(
            ticket_paths=[t_ok, t_parked],
            actor_ref="codex@ASUS-GEI",
            rationale="Darf nicht bündeln",
            source_ref="ticket-master",
            scope_key="umbruch:.umbruch",
            config=cfg,
        )

    with pytest.raises(TicketBundleError, match="foreign actor"):
        reg.create_bundle(
            ticket_paths=[t_ok, t_foreign],
            actor_ref="codex@ASUS-GEI",
            rationale="Darf fremden Claim nicht übernehmen",
            source_ref="ticket-master",
            scope_key="umbruch:.umbruch",
            config=cfg,
        )

    t_ok2 = _write_ticket(
        actionable, "T-20261027-555555555.txt", ticket_id="T-20261027-555555555"
    )
    with pytest.raises(PreimageConflictError, match="preimage mismatch"):
        reg.create_bundle(
            ticket_paths=[t_ok, t_ok2],
            actor_ref="codex@ASUS-GEI",
            rationale="Preimage-Test",
            source_ref="ticket-master",
            scope_key="umbruch:.umbruch",
            config=cfg,
            expected_preimages={"T-20261027-111111111": "0" * 64},
        )


def test_partial_failure_and_completed_bundle_preserve_individual_ticket_status(tmp_path):
    actionable = tmp_path / "ACTIONABLE"
    t1 = _write_ticket(
        actionable, "T-20261027-111111111.txt", ticket_id="T-20261027-111111111"
    )
    t2 = _write_ticket(
        actionable, "T-20261027-222222222.txt", ticket_id="T-20261027-222222222"
    )
    cfg = {"bundles": {"enabled": True}}
    reg = BundlesRegistry(tmp_path / "bundles-reg.json")
    bundle = reg.create_bundle(
        ticket_paths=[t1, t2],
        actor_ref="codex@ASUS-GEI",
        rationale="Teilfehler- und Abschlussprüfung",
        source_ref="ticket-master",
        scope_key="umbruch:.umbruch",
        config=cfg,
    )
    updated = reg.record_member_outcome(
        bundle["bundle_id"],
        ticket_id="T-20261027-111111111.ASUS-GEI.txt",
        outcome="done",
        actor_ref="codex@ASUS-GEI",
    )
    assert updated["state"] == "executing"
    partial = reg.record_member_outcome(
        bundle["bundle_id"],
        ticket_id="T-20261027-222222222",
        outcome="failed",
        actor_ref="codex@ASUS-GEI",
    )
    assert partial["state"] == "partial"
    completed = reg.record_member_outcome(
        bundle["bundle_id"],
        ticket_id="T-20261027-222222222",
        outcome="done",
        actor_ref="codex@ASUS-GEI",
    )
    assert completed["state"] == "completed"
    # Individual ticket files keep their own canonical STATUS (not auto-marked SOLVED)
    assert parse_ticket_metadata(t1)["status"] == "ACTIONABLE"
    assert parse_ticket_metadata(t2)["status"] == "ACTIONABLE"
