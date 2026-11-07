"""Die Queue-Marke entsteht nur per Befehl und traegt eine Kennung (T-20261107-847936256).

Die Fehlermeldung der Queue-Pruefung empfahl bisher, die Marke `.ticket-master-queue` mit dem
Inhalt `ticket-master-queue-v1` selbst anzulegen -- und genau das tat jemand am 2026-09-09 in der
Fixture des Spiegels. Jetzt nennt die Meldung den Befehl `ticket_master.py --init-queue`; die
Marke (v2) enthaelt eine Kennung, ein von Hand getipptes `v1`/`v2` ohne gueltige Kennung gilt nicht.
"""
import contextlib
import io
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

import ticket_writer  # noqa: E402
from queue_helpers import load_cli, verified_queue, write_file  # noqa: E402


def create(queue: Path):
    return ticket_writer.create("Titel", "Body", tickets_dir=queue, today="2026-11-07")


def test_init_queue_creates_a_marker_with_an_identity_and_writers_accept_it(tmp_path):
    queue = tmp_path / "queue"

    assert ticket_writer.init_queue(queue) == queue

    marker = (queue / ".ticket-master-queue").read_text(encoding="utf-8").splitlines()
    assert marker[0] == "ticket-master-queue-v2"
    fields = dict(line.split(": ", 1) for line in marker[1:])
    assert re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", fields["queue_id"])
    assert {"created", "host", "root"} <= set(fields)
    assert Path(create(queue)).parent == queue / "INBOX"


def test_init_queue_is_idempotent_and_never_rewrites_an_existing_marker(tmp_path):
    queue = tmp_path / "queue"
    ticket_writer.init_queue(queue)
    before = (queue / ".ticket-master-queue").read_bytes()

    ticket_writer.init_queue(queue)

    assert (queue / ".ticket-master-queue").read_bytes() == before


def test_init_queue_leaves_a_legacy_queue_untouched(tmp_path):
    queue = verified_queue(tmp_path / "queue")  # Marke v1 wie in bestehenden Queues
    before = (queue / ".ticket-master-queue").read_bytes()

    ticket_writer.init_queue(queue)

    assert (queue / ".ticket-master-queue").read_bytes() == before


@pytest.mark.parametrize("module_file", ["ellmos-module.v2.json", "REPO.pointer.json"])
def test_init_queue_refuses_module_and_mirror_locations_and_creates_nothing(tmp_path, module_file):
    write_file(tmp_path / "ticket-master" / module_file, "{}")
    queue = tmp_path / "ticket-master" / "tickets"

    with pytest.raises(ValueError, match="module"):
        ticket_writer.init_queue(queue)
    with pytest.raises(ValueError, match="module"):
        ticket_writer.init_queue(tmp_path / ".MODULES" / "ticket-master" / "tickets")

    assert not queue.exists()
    assert not (tmp_path / ".MODULES").exists()


@pytest.mark.parametrize("content", [
    "ticket-master-queue-v2\n",                                  # Kennung fehlt
    "ticket-master-queue-v2\nqueue_id: nicht-gueltig\n",          # keine UUID
    "ticket-master-queue-v3\nqueue_id: 5b7f0f4e-3f43-4b57-9a8c-1d2e3f4a5b6c\n",  # unbekannte Version
])
def test_hand_made_v2_looking_marker_without_a_valid_identity_is_refused(tmp_path, content):
    queue = write_file(tmp_path / "queue" / ".ticket-master-queue", content).parent

    with pytest.raises(ValueError, match="verified ticket queue"):
        create(queue)


def test_error_message_names_the_command_and_gives_no_recipe_for_the_marker(tmp_path):
    queue = tmp_path / "wrong" / "tickets"

    with pytest.raises(ValueError) as caught:
        create(queue)

    message = str(caught.value)
    assert "verified ticket queue" in message
    assert "--init-queue" in message
    assert "ticket-master-queue-v1" not in message
    assert "ticket-master-queue-v2" not in message
    assert "Add .ticket-master-queue" not in message
    assert not queue.exists()


def test_cli_init_queue_mode(tmp_path):
    cli = load_cli()
    queue = tmp_path / "fresh-queue"
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        assert cli.main(["--init-queue", str(queue)]) == 0
    assert "INITIALIZED" in out.getvalue()
    assert (queue / ".ticket-master-queue").read_text(encoding="utf-8").startswith("ticket-master-queue-v2")

    write_file(tmp_path / "ticket-master" / "ellmos-module.v2.json", "{}")
    stderr = io.StringIO()
    with contextlib.redirect_stderr(stderr):
        assert cli.main(["--init-queue", str(tmp_path / "ticket-master" / "tickets")]) == 2
    assert "module" in stderr.getvalue()
    assert not (tmp_path / "ticket-master" / "tickets").exists()
