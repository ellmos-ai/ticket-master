import random
from pathlib import Path

from lib import session_provenance, ticket_mover, ticket_writer


def _queue(root: Path) -> Path:
    root.mkdir()
    (root / ".ticket-master-queue").write_text(
        "ticket-master-queue-v1\n", encoding="utf-8")
    return root


def test_resolution_priority_explicit_then_provider_environment_then_unknown():
    env = {
        "TICKET_MASTER_SESSION_ID": "tm-parent",
        "CLAUDE_SESSION_ID": "claude-parent",
        "CODEX_SESSION_ID": "codex-parent",
        "SESSION_ID": "must-not-be-used",
    }
    explicit = session_provenance.resolve_session_provenance(
        "explicit", agent="worker", host="ASUS-GEI", day="2026-09-09",
        environ=env)
    assert explicit.stamp == "session: explicit | worker@ASUS-GEI | 2026-09-09"

    runtime = session_provenance.resolve_session_provenance(
        agent="worker", host="ASUS-GEI", day="2026-09-09", environ=env)
    assert runtime.session == "tm-parent"

    unknown = session_provenance.resolve_session_provenance(
        agent="worker", host="ASUS-GEI", day="2026-09-09",
        environ={"SESSION_ID": "ambiguous"})
    assert unknown.session == "unbekannt"


def test_writer_adds_ticket_session_header_from_explicit_value(tmp_path):
    path = Path(ticket_writer.create(
        "Session proof", "body", tickets_dir=_queue(tmp_path / "tickets"),
        today="2026-09-09", rng=random.Random(1),
        session="parent-123", session_agent="worker", session_host="ASUS-GEI"))
    text = path.read_text(encoding="utf-8")
    assert "SESSION:       parent-123 | worker@ASUS-GEI | 2026-09-09" in text
    assert text.rstrip().endswith(
        "session: parent-123 | worker@ASUS-GEI | 2026-09-09")


def test_mark_delegated_adds_header_and_history_stamp_without_duplicates(tmp_path):
    ticket = tmp_path / "T-20260909-123456789.txt"
    ticket.write_text(
        "ID: T-20260909-123456789\nPRIORITAET: mittel\n", encoding="utf-8")
    ticket_mover.mark_delegated(
        ticket, "documents_review@ASUS-GEI", session="parent-123")
    first = ticket.read_text(encoding="utf-8")
    assert "SESSION:       parent-123 | documents_review@ASUS-GEI |" in first
    assert "session: parent-123 | documents_review@ASUS-GEI |" in first
    ticket_mover.mark_delegated(
        ticket, "documents_review@ASUS-GEI", session="parent-123")
    second = ticket.read_text(encoding="utf-8")
    assert second.count("SESSION:") == 1
    assert second.count("session: parent-123") == 1
