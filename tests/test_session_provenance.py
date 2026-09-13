import pytest
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


# --- T-20260913-695955668: `<agent>@<host>` wird einmal aufgeloest ----------
#
# Zwei CLI-Optionen verlangen gegensaetzliche Wertformen -- `ticket_mover
# --agent` will "claude-code@ASUS-GEI", `ticket_writer --session-agent` den
# blossen Namen. Wer den einen Wert in die andere Option gibt, erzeugte einen
# Stempel mit ZWEI @-Trennungen, fail-silent (real erzeugt am 2026-09-13 in
# T-20260913-667086783, von Hand korrigiert).


def _stamp(**kwargs) -> str:
    return session_provenance.resolve_session_provenance(
        "sess", day="2026-09-13", environ={}, **kwargs).value


def test_both_value_forms_produce_the_same_stamp():
    """Die Idempotenz, um die es geht: beide Optionen, ein Ergebnis."""
    bare = _stamp(agent="control1", host="ASUS-GEI")
    qualified = _stamp(agent="control1@ASUS-GEI")
    redundant = _stamp(agent="control1@ASUS-GEI", host="ASUS-GEI")
    assert bare == "sess | control1@ASUS-GEI | 2026-09-13"
    assert qualified == bare
    assert redundant == bare
    assert qualified.count("@") == 1


def test_host_case_does_not_count_as_a_contradiction():
    assert (_stamp(agent="control1@asus-gei", host="ASUS-GEI")
            == "sess | control1@ASUS-GEI | 2026-09-13")


def test_two_different_hosts_are_refused_not_silently_merged():
    with pytest.raises(session_provenance.SessionProvenanceError, match="WORKSTATION-LG"):
        _stamp(agent="control1@ASUS-GEI", host="WORKSTATION-LG")


@pytest.mark.parametrize("broken", ["a@b@c", "@HOST", "agent@"])
def test_shapes_that_mean_nothing_are_refused(broken):
    with pytest.raises(session_provenance.SessionProvenanceError):
        _stamp(agent=broken)


def test_a_bare_name_still_resolves_the_host_the_old_way():
    assert session_provenance.resolve_session_provenance(
        "sess", agent="control1", day="2026-09-13",
        environ={"COMPUTERNAME": "ASUS-GEI"}).value == (
            "sess | control1@ASUS-GEI | 2026-09-13")


def test_mark_delegated_keeps_accepting_the_qualified_form(tmp_path):
    """Der Mover zerlegte den Wert bisher SELBST. Die Kopie ist entfallen --
    dieser Test haelt fest, dass sich sein Verhalten dadurch nicht aendert."""
    queue = _queue(tmp_path / "q")
    ticket = queue / "T-20260913-000000001.txt"
    ticket.write_text("ID: T-20260913-000000001\nPRIORITAET: mittel\n",
                      encoding="utf-8")
    ticket_mover.mark_delegated(ticket, "claude-code@ASUS-GEI", session="sess")
    stamped = ticket.read_text(encoding="utf-8")
    assert "SESSION:       sess | claude-code@ASUS-GEI |" in stamped
    assert "DELEGIERT_AN: claude-code@ASUS-GEI" in stamped


def test_mark_delegated_refuses_a_contradictory_host(tmp_path):
    queue = _queue(tmp_path / "q")
    ticket = queue / "T-20260913-000000002.txt"
    ticket.write_text("ID: T-20260913-000000002\nPRIORITAET: mittel\n",
                      encoding="utf-8")
    before = ticket.read_bytes()
    with pytest.raises(session_provenance.SessionProvenanceError):
        ticket_mover.mark_delegated(
            ticket, "claude-code@ASUS-GEI", session="sess",
            session_host="WORKSTATION-LG")
    assert ticket.read_bytes() == before
