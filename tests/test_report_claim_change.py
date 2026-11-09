# -*- coding: utf-8 -*-
"""Unit tests for report_claim_change.py."""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

import pytest

# Ensure lib and tools are on path
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "lib"))
sys.path.insert(0, str(REPO_ROOT / "tools"))

from report_claim_change import format_claim_ticket_body, format_claim_ticket_title, report_claim_change, main


@pytest.fixture
def temp_queue(tmp_path: Path):
    """Creates a temporary valid queue structure."""
    queue_dir = tmp_path / "queue"
    queue_dir.mkdir()
    (queue_dir / ".ticket-master-queue").write_text("ticket-master-queue-v1", encoding="utf-8")
    (queue_dir / "INBOX").mkdir()

    # Systems registry fixture
    registry_file = tmp_path / "systems-registry.json"
    registry_file.write_text(
        json.dumps({
            "checked_at": "2026-11-09T12:00:00Z",
            "source": "derived from inventory seeds: test",
            "systems": {
                "ASUS-GEI": {"hostname": "ASUS-GEI", "role": "mobile"},
                "WORKSTATION-LG": {"hostname": "WORKSTATION-LG", "role": "primary-dev"},
            }
        }),
        encoding="utf-8",
    )
    return queue_dir, registry_file


def test_validation_fails_on_empty_fields(temp_queue):
    queue_dir, reg_file = temp_queue
    with pytest.raises(ValueError, match="project"):
        report_claim_change(
            project="",
            old_claim="Entwurf",
            new_claim="Bewiesen",
            location="BEWEISNOTIZ.md",
            reason="Review",
            tickets_dir=queue_dir,
            systems_registry=reg_file,
        )

    with pytest.raises(ValueError, match="old_claim"):
        report_claim_change(
            project="CRM",
            old_claim="",
            new_claim="Bewiesen",
            location="BEWEISNOTIZ.md",
            reason="Review",
            tickets_dir=queue_dir,
            systems_registry=reg_file,
        )

    with pytest.raises(ValueError, match="new_claim"):
        report_claim_change(
            project="CRM",
            old_claim="Entwurf",
            new_claim="",
            location="BEWEISNOTIZ.md",
            reason="Review",
            tickets_dir=queue_dir,
            systems_registry=reg_file,
        )

    with pytest.raises(ValueError, match="location"):
        report_claim_change(
            project="CRM",
            old_claim="Entwurf",
            new_claim="Bewiesen",
            location="",
            reason="Review",
            tickets_dir=queue_dir,
            systems_registry=reg_file,
        )

    with pytest.raises(ValueError, match="reason"):
        report_claim_change(
            project="CRM",
            old_claim="Entwurf",
            new_claim="Bewiesen",
            location="BEWEISNOTIZ.md",
            reason="",
            tickets_dir=queue_dir,
            systems_registry=reg_file,
        )


def test_successful_ticket_creation_with_routing_v2(temp_queue):
    queue_dir, reg_file = temp_queue

    ticket_path_str = report_claim_change(
        project="Feedback Equilibrium Dynamics",
        old_claim="WIDERLEGT / CHANGES_REQUIRED",
        new_claim="BEWIESEN / REPARIERT",
        location="BEWEISNOTIZ.md Nachtrag 5, Sektion 2 & Theorem 1",
        reason="Turnusgemäßer dialektischer 3-Agenten-Review abgeschlossen",
        tickets_dir=queue_dir,
        systems_registry=reg_file,
        priority="hoch",
        pipeline="research",
        today="2026-11-09",
    )

    ticket_path = Path(ticket_path_str)
    assert ticket_path.is_file()
    assert ticket_path.parent.name == "INBOX"

    content = ticket_path.read_text(encoding="utf-8")
    assert "ID:            T-20261109-" in content
    assert "PRIORITAET:    hoch" in content
    assert "PIPELINE:      research" in content
    assert "PROJEKTORDNER: Feedback Equilibrium Dynamics" in content
    assert "## Gemeldete wissenschaftliche Claim-Änderung" in content
    assert "- **Projekt:** Feedback Equilibrium Dynamics" in content
    assert "- **Fundstelle (Quelle/Stelle):** BEWEISNOTIZ.md Nachtrag 5, Sektion 2 & Theorem 1" in content
    assert "- **Alter Wortlaut / Stand:** WIDERLEGT / CHANGES_REQUIRED" in content
    assert "- **Neuer Wortlaut / Stand:** BEWIESEN / REPARIERT" in content
    assert "- **Anlass der Änderung:** Turnusgemäßer dialektischer 3-Agenten-Review abgeschlossen" in content
    assert "Benachrichtigung an Lukas / Persönlichen Assistenten" in content
    assert "Routing schema v2" in content


def test_idempotency_prevents_duplicate_tickets(temp_queue):
    queue_dir, reg_file = temp_queue

    path1 = report_claim_change(
        project="TestProject",
        old_claim="Old Wording",
        new_claim="New Wording",
        location="doc.md",
        reason="Fix",
        tickets_dir=queue_dir,
        systems_registry=reg_file,
        idempotency_key="idemp-key-12345",
        today="2026-11-09",
    )

    path2 = report_claim_change(
        project="TestProject",
        old_claim="Old Wording",
        new_claim="New Wording",
        location="doc.md",
        reason="Fix",
        tickets_dir=queue_dir,
        systems_registry=reg_file,
        idempotency_key="idemp-key-12345",
        today="2026-11-09",
    )

    assert path1 == path2
    inbox_files = list((queue_dir / "INBOX").glob("*.txt"))
    assert len(inbox_files) == 1


def test_cli_execution(temp_queue, capsys):
    queue_dir, reg_file = temp_queue

    exit_code = main([
        "--project", "CRM-Cosmology",
        "--old-claim", "Scalaron Screening aktiv",
        "--new-claim", "Scalaron Screening zurückgenommen",
        "--source", "papers/Paper4_EN.tex Gl. (12)",
        "--reason", "Forensischer Nachweis der Vainshtein-Entkopplung",
        "--tickets-dir", str(queue_dir),
        "--systems-registry", str(reg_file),
    ])

    assert exit_code == 0
    captured = capsys.readouterr()
    ticket_path = Path(captured.out.strip())
    assert ticket_path.is_file()
    assert "T-" in ticket_path.name
