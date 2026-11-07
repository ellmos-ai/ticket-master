"""Ein Ordner im Modul-/Spiegelbaum ist nie eine Ticket-Queue (T-20261107-847936256).

Am 2026-09-09 legte jemand von Hand die Queue-Marke in der Fixture `tickets/` des
ticket-master-Spiegels an. Danach galt der Ordner als Live-Queue, und 27 Tickets landeten
dort statt in der kanonischen Queue. Mit Marke ist ein Ordner neben `ellmos-module.v2.json`
oder `REPO.pointer.json`, unter `.MODULES` oder selbst Modulwurzel trotzdem nie eine Queue.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

import ticket_writer  # noqa: E402
from queue_helpers import verified_queue, write_file  # noqa: E402


def create(queue: Path):
    return ticket_writer.create("Titel", "Body", tickets_dir=queue, today="2026-11-07")


@pytest.mark.parametrize("module_file", ["ellmos-module.v2.json", "REPO.pointer.json"])
def test_queue_next_to_a_module_manifest_is_refused_even_with_a_hand_made_marker(tmp_path, module_file):
    """Genau der Vorfall: Fixture `tickets/` neben dem Manifest, Marke von Hand angelegt."""
    write_file(tmp_path / "ticket-master" / module_file, "{}")
    queue = verified_queue(tmp_path / "ticket-master" / "tickets")

    with pytest.raises(ValueError, match="module"):
        create(queue)

    assert not (queue / "INBOX").exists()


def test_queue_under_a_modules_tree_is_refused_even_with_a_marker(tmp_path):
    queue = verified_queue(tmp_path / ".MODULES" / ".CONTROL" / "ticket-master" / "tickets")

    with pytest.raises(ValueError, match="module"):
        create(queue)

    assert not (queue / "INBOX").exists()


def test_a_module_root_itself_is_not_a_queue(tmp_path):
    write_file(tmp_path / "module" / "ellmos-module.v2.json", "{}")
    queue = verified_queue(tmp_path / "module")

    with pytest.raises(ValueError, match="module"):
        create(queue)


def test_routed_writer_applies_the_same_refusal(tmp_path):
    write_file(tmp_path / "ticket-master" / "ellmos-module.v2.json", "{}")
    queue = verified_queue(tmp_path / "ticket-master" / "tickets")

    with pytest.raises(ValueError, match="module"):
        ticket_writer.create_routed_ticket(
            "Titel", "Body", tickets_dir=queue, registry_snapshot={"systems": []}, today="2026-11-07")

    assert not (queue / "INBOX").exists()


def test_the_README_contract_does_not_rescue_a_module_location(tmp_path):
    """Auch der Kompatibilitaetsvertrag (README + Vorlage) darf einen Modulordner nicht freigeben."""
    write_file(tmp_path / "ticket-master" / "REPO.pointer.json", "{}")
    queue = tmp_path / "ticket-master" / "tickets"
    write_file(queue / "README.md", "Ticket queue. New tickets land in INBOX.\n")
    write_file(queue / "_templates" / "TICKET.txt", "TICKET\nSTATUS:        INBOX\n")

    with pytest.raises(ValueError, match="module"):
        create(queue)


def test_location_check_lets_canonical_looking_paths_through(tmp_path):
    canonical = tmp_path / ".TOPICS" / "_control-center" / "_TICKETS"
    assert ticket_writer.queue_location_problem(canonical) is None
    assert ticket_writer.queue_location_problem(tmp_path / "anywhere" / "queue") is None


def test_existing_valid_queues_keep_working(tmp_path):
    """Regression: Marke v1 (Tests/Altbestand) und der README-Vertrag der kanonischen Queue."""
    legacy_marker = verified_queue(tmp_path / "with-marker")
    assert Path(create(legacy_marker)).parent == legacy_marker / "INBOX"

    contract = tmp_path / "_control-center" / "_TICKETS"
    write_file(contract / "README.md", "Ticket queue. New tickets land in INBOX.\n")
    write_file(contract / "_templates" / "TICKET.txt", "TICKET\nSTATUS:        INBOX\n")
    assert Path(create(contract)).parent == contract / "INBOX"
