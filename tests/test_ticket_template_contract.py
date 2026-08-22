"""Regression tests for the random-ID and categories-v1 ticket surfaces."""

from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

import pytest

from lib import ticket_writer


REPO_ROOT = Path(__file__).resolve().parents[1]
RANDOM_ID_PLACEHOLDER = "T-YYYYMMDD-#########"


def _read(relative: str) -> str:
    return (REPO_ROOT / relative).read_text(encoding="utf-8")


def test_current_id_surfaces_use_the_random_id_placeholder():
    current_surfaces = (
        "tickets/_templates/TICKET.txt",
        "README.md",
        "README_de.md",
        "docs/CATEGORIES.en.md",
        "docs/CATEGORIES.de.md",
        "lib/ticket_writer.py",
        "lib/ticket_audit.py",
        "prompts/TICKET-MASTER.en.md",
        "prompts/TICKET-MASTER.de.md",
    )
    for relative in current_surfaces:
        text = _read(relative)
        assert RANDOM_ID_PLACEHOLDER in text, relative
        assert "T-YYYYMMDD-NN" not in text, relative


def test_master_prompts_do_not_claim_random_ids_auto_increment():
    english = _read("prompts/TICKET-MASTER.en.md")
    german = _read("prompts/TICKET-MASTER.de.md")
    assert "auto-increments on collision" not in english
    assert "zählt bei einer Kollision automatisch hoch" not in german
    assert "draws a new 9-digit random number" in english
    assert "würfelt bei einer Kollision eine neue 9-stellige Zufallszahl" in german


def test_template_requires_writer_only_creation_and_categories_v1():
    template = _read("tickets/_templates/TICKET.txt")
    assert "ticket_writer.py" in template
    assert "never copy this file or choose/count an ID manually" in template
    for cluster, subcategories in ticket_writer.LIFECYCLE_SUBCATEGORIES.items():
        assert cluster in template
        for subcategory in subcategories:
            assert subcategory in template
    for field in (
        "ROUTING_SCHEMA", "TICKET_KIND", "TARGET_KIND", "TARGET_SYSTEMS",
        "RESOLUTION_NOTE", "EXECUTION_MATRIX", "CLAIMED_BY_HOST", "SYSTEM_LEDGER",
    ):
        assert f"{field}:" in template
    assert "create_routed_ticket()" in template
    assert ".to-<target>" in template


def test_cli_help_advertises_a_nine_digit_random_id():
    stdout = StringIO()
    with pytest.raises(SystemExit) as exc_info, redirect_stdout(stdout):
        ticket_writer._cli(["--help"])
    assert exc_info.value.code == 0
    help_text = stdout.getvalue()
    assert RANDOM_ID_PLACEHOLDER in help_text
    assert "9-digit random" in help_text
