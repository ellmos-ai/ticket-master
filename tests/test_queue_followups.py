"""Follow-ups of the queue-marker hardening (ticket-master#53, T-20261107-165954775).

1. The old "write the marker by hand" recipe is gone from the example config and the READMEs.
2. TICKET_MASTER_TICKETS_DIR is honoured by bin/ticket_master.py too (argument > environment > config).
3. Every writer that changes an existing ticket checks the queue through one helper, so a shadow queue that
   already exists cannot be triaged with ticket_mover / ticket_note any more.
4. A folder directly below a module's `tickets/` fixture is not a queue home either.

Only temporary queues are used; nothing here touches a real ticket queue.
"""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))

import ticket_mover  # noqa: E402
import ticket_note  # noqa: E402
import ticket_writer  # noqa: E402
from queue_helpers import load_cli, verified_queue, write_file  # noqa: E402

TICKET_TEXT = """==============================================================
TICKET
==============================================================
ID:            T-20261107-000000001
TITEL:         Shadow queue probe
STATUS:        ACTIONABLE
PRIORITAET:    mittel

--------------------------------------------------------------
VERLAUF / LOG
--------------------------------------------------------------
2026-11-07  Aufgenommen.

--------------------------------------------------------------
LOESUNG / ERGEBNIS
--------------------------------------------------------------
<offen>
==============================================================
"""
TICKET_NAME = "T-20261107-000000001.txt"


def ticket_in(queue: Path, folder: str = "ACTIONABLE") -> Path:
    return write_file(queue / folder / TICKET_NAME, TICKET_TEXT)


def legacy_queue(path: Path) -> Path:
    """A queue that predates the marker: root README and _templates/TICKET.txt name the contract."""
    write_file(path / "README.md", "# Tickets\nEvery TICKET starts in INBOX.\n")
    write_file(path / "_templates" / "TICKET.txt", "TICKET\nSTATUS: INBOX\n")
    return path


# --- 1. documentation -------------------------------------------------------------------------------------------


def test_example_config_points_to_init_queue_not_to_a_hand_made_marker():
    config = json.loads((ROOT / "config" / "ticket-master.config.example.json").read_text(encoding="utf-8"))
    note = config["_comment_tickets_dir"]

    assert "--init-queue" in note
    assert "complete content" not in note
    assert "ticket-master-queue-v1" not in note


@pytest.mark.parametrize("name", ["README.md", "README_de.md"])
def test_readmes_describe_the_real_queue_setting_and_the_init_command(name):
    text = (ROOT / name).read_text(encoding="utf-8")

    assert "ticket_dir" not in text, "the config key is tickets_dir; ticket_dir was never read"
    assert "configure `queue_id` in your local configuration" not in text
    assert "--init-queue" in text
    assert "TICKET_MASTER_TICKETS_DIR" in text


# --- 2. TICKET_MASTER_TICKETS_DIR in bin/ticket_master.py ---------------------------------------------------------


def write_config(path: Path, tickets_dir: str | None) -> Path:
    path.write_text(json.dumps({"tickets_dir": tickets_dir} if tickets_dir else {}), encoding="utf-8")
    return path


def test_cli_reads_the_environment_variable(tmp_path, monkeypatch):
    cli = load_cli()
    env_dir = verified_queue(tmp_path / "from-env")
    monkeypatch.setenv("TICKET_MASTER_TICKETS_DIR", str(env_dir))

    resolved = cli.resolve_tickets_dir(write_config(tmp_path / "empty.json", None), None)

    assert resolved == env_dir.resolve()


def test_cli_precedence_is_argument_then_environment_then_config(tmp_path, monkeypatch):
    cli = load_cli()
    from_arg = tmp_path / "from-arg"
    from_env = tmp_path / "from-env"
    from_cfg = tmp_path / "from-config"
    config = write_config(tmp_path / "cfg.json", str(from_cfg))

    monkeypatch.setenv("TICKET_MASTER_TICKETS_DIR", str(from_env))
    assert cli.resolve_tickets_dir(config, from_arg) == from_arg.resolve()
    assert cli.resolve_tickets_dir(config, None) == from_env.resolve()

    monkeypatch.delenv("TICKET_MASTER_TICKETS_DIR")
    assert cli.resolve_tickets_dir(config, None) == from_cfg.resolve()


def test_cli_ignores_an_empty_environment_variable(tmp_path, monkeypatch):
    cli = load_cli()
    from_cfg = tmp_path / "from-config"
    monkeypatch.setenv("TICKET_MASTER_TICKETS_DIR", "")

    assert cli.resolve_tickets_dir(write_config(tmp_path / "cfg.json", str(from_cfg)), None) == from_cfg.resolve()


def test_cli_error_names_the_environment_variable(tmp_path, monkeypatch):
    cli = load_cli()
    monkeypatch.delenv("TICKET_MASTER_TICKETS_DIR", raising=False)

    with pytest.raises(cli.ConfigError, match="TICKET_MASTER_TICKETS_DIR"):
        cli.resolve_tickets_dir(write_config(tmp_path / "empty.json", None), None)


# --- 3. one queue check for every writer ----------------------------------------------------------------------


def test_helper_returns_the_queue_root_of_a_ticket(tmp_path):
    queue = verified_queue(tmp_path / "queue")

    assert ticket_writer.require_ticket_in_verified_queue(ticket_in(queue, "QUEUED")) == queue
    assert ticket_writer.queue_root_of(queue / "QUEUED" / TICKET_NAME) == queue
    assert ticket_writer.queue_root_of(queue / TICKET_NAME) == queue
    assert ticket_mover.queue_root(queue / "SOLVED" / TICKET_NAME) == queue


def test_helper_returns_the_queue_root_of_a_ticket_with_lowercase_lifecycle(tmp_path):
    queue = verified_queue(tmp_path / "queue")
    for folder in ("inbox", "queued", "actionable", "solved", "user"):
        sub = queue / folder
        sub.mkdir()
        ticket = sub / TICKET_NAME
        assert ticket_writer.queue_root_of(ticket) == queue
        assert ticket_mover.queue_root(ticket) == queue


def test_note_refuses_a_ticket_in_an_unverified_queue(tmp_path):
    shadow = tmp_path / "shadow"
    ticket = ticket_in(shadow)
    before = ticket.read_bytes()

    with pytest.raises(ValueError, match="verified ticket queue"):
        ticket_note.update_ticket_note(ticket, verlauf="triage attempt")
    with pytest.raises(ValueError, match="verified ticket queue"):
        ticket_note.update_ticket_note(ticket, status="SOLVED", dry_run=True)

    assert ticket.read_bytes() == before


def test_move_refuses_a_ticket_in_an_unverified_queue(tmp_path):
    shadow = tmp_path / "shadow"
    ticket = ticket_in(shadow)

    for dry_run in (True, False):
        with pytest.raises(ValueError, match="verified ticket queue"):
            ticket_mover.move_ticket(ticket, shadow / "SOLVED", dry_run=dry_run)

    assert ticket.is_file()
    assert not (shadow / "SOLVED").exists()


def test_move_refuses_destination_outside_verified_queue(tmp_path):
    queue = verified_queue(tmp_path / "queue")
    ticket = ticket_in(queue, "ACTIONABLE")
    outside = tmp_path / "outside"
    outside_solved = outside / "SOLVED"
    before = ticket.read_bytes()

    for dry_run in (True, False):
        with pytest.raises(ValueError, match="verified ticket queue"):
            ticket_mover.move_ticket(ticket, outside_solved, dry_run=dry_run)
        with pytest.raises(ValueError, match="verified ticket queue"):
            ticket_mover.move_ticket(ticket, outside, dry_run=dry_run)

    assert ticket.is_file()
    assert ticket.read_bytes() == before
    assert not outside.exists()


def test_mark_delegated_and_release_claim_refuse_an_unverified_queue(tmp_path):
    shadow = tmp_path / "shadow"
    ticket = ticket_in(shadow, "QUEUED")
    claimed = write_file(shadow / "ACTIONABLE" / "T-20261107-000000002.ASUS-GEI.txt", TICKET_TEXT)
    before = ticket.read_bytes()

    with pytest.raises(ValueError, match="verified ticket queue"):
        ticket_mover.mark_delegated(ticket, "worker@HOST")
    with pytest.raises(ValueError, match="verified ticket queue"):
        ticket_mover.release_claim(claimed)

    assert ticket.read_bytes() == before
    assert claimed.is_file()


def test_release_claims_refuses_an_unverified_queue(tmp_path):
    shadow = tmp_path / "shadow"
    claimed = write_file(shadow / "ACTIONABLE" / "T-20261107-000000002.ASUS-GEI.txt", TICKET_TEXT)

    with pytest.raises(ValueError, match="verified ticket queue"):
        ticket_mover.release_claims(shadow, host="ASUS-GEI")

    assert claimed.is_file()


def test_formalize_idempotent_branch_refuses_an_unverified_queue(tmp_path):
    shadow = tmp_path / "shadow"
    # a ticket that already names the informal file: this is the branch that used to archive without any check
    write_file(shadow / "INBOX" / "T-20261107-000000003.txt", "ID: T-20261107-000000003\nQuelle: idea.txt\n")
    source = write_file(shadow / "INBOX" / "idea.txt", "an informal idea\n")

    with pytest.raises(ValueError, match="verified ticket queue"):
        ticket_writer.formalize_informal_entry(source, shadow)

    assert source.is_file()
    assert not (shadow / "INBOX" / "_formalisiert").exists()


@pytest.mark.parametrize("kind", ["v1", "v2", "legacy"])
def test_every_accepted_queue_kind_still_works_for_note_and_move(tmp_path, kind):
    """Backward compatibility: v1 marker, v2 marker from --init-queue, and a queue that predates the marker."""
    queue = tmp_path / "queue"
    if kind == "v1":
        verified_queue(queue)
    elif kind == "v2":
        ticket_writer.init_queue(queue)
    else:
        legacy_queue(queue)
    ticket = ticket_in(queue)

    ticket_note.update_ticket_note(ticket, verlauf="ok")
    moved = ticket_mover.move_ticket(ticket, queue / "SOLVED")

    assert moved == queue / "SOLVED" / TICKET_NAME
    assert "ok" in moved.read_text(encoding="utf-8")


def test_a_queue_at_a_canonical_looking_path_is_accepted(tmp_path):
    """The live queue sits at <home>/OneDrive/.TOPICS/_control-center/_TICKETS with a v1 marker."""
    queue = verified_queue(tmp_path / "OneDrive" / ".TOPICS" / "_control-center" / "_TICKETS")
    ticket = ticket_in(queue, "QUEUED")

    result = ticket_note.update_ticket_note(ticket, status="QUEUED (test)", verlauf="ok")

    assert result["status_updated"] and result["verlauf_appended"]
    ticket_mover.mark_delegated(ticket, "worker@HOST")
    assert "DELEGIERT_AN: worker@HOST" in ticket.read_text(encoding="utf-8")


# --- 4. <module root>/tickets/<sub> ------------------------------------------------------------------------------


def module_root(path: Path) -> Path:
    write_file(path / "ellmos-module.v2.json", "{}\n")
    return path


def test_a_folder_below_a_module_fixture_is_not_a_queue_home(tmp_path):
    root = module_root(tmp_path / "ticket-master-mirror")

    problem = ticket_writer.queue_location_problem(root / "tickets" / "sub")

    assert problem is not None and "fixture" in problem
    with pytest.raises(ValueError, match="cannot initialise"):
        ticket_writer.init_queue(root / "tickets" / "sub")
    verified_queue_marker = root / "tickets" / "sub"
    verified_queue_marker.mkdir(parents=True)
    (verified_queue_marker / ".ticket-master-queue").write_text("ticket-master-queue-v1\n", encoding="utf-8")
    with pytest.raises(ValueError, match="not a live ticket queue"):
        ticket_writer.require_verified_queue_root(verified_queue_marker)


def test_neutral_places_stay_valid_queue_homes(tmp_path):
    root = module_root(tmp_path / "ticket-master-mirror")

    assert ticket_writer.queue_location_problem(root / "elsewhere" / "sub") is None  # a normal folder in a module
    assert ticket_writer.queue_location_problem(tmp_path / "tickets" / "sub") is None  # tickets/ without a module root
    assert ticket_writer.queue_location_problem(tmp_path / "neutral" / "queue") is None


def test_the_fixture_folder_itself_is_still_refused(tmp_path):
    root = module_root(tmp_path / "ticket-master-mirror")

    assert ticket_writer.queue_location_problem(root / "tickets") is not None
