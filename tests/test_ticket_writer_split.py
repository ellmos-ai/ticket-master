# -*- coding: utf-8 -*-
"""Verifikation von ticket_writer.split_ticket() / --split-from
(T-20260902-379329038): eine echte ID-Kollision entstand durch Kopieren
einer Ticketdatei statt durch Ziehen einer neuen ID -- split_ticket() ist
der unterstuetzte Weg, ein Ticket in zwei zu teilen, ohne dass das passieren
kann: er geht immer durch create()'s exklusive Ziehung, traegt die
Ursprungs-ID als Herkunftsfeld nach und laesst den Wortlaut bytegleich in
einem ORIGINALTEXT-Block stehen (dieselbe Konvention wie
formalize_informal_entry())."""
import sys
import tempfile
import unittest
from pathlib import Path

LIB_DIR = Path(__file__).resolve().parent.parent / "lib"
sys.path.insert(0, str(LIB_DIR))

import ticket_writer  # noqa: E402
from queue_helpers import verified_queue  # noqa: E402


class TestSplitTicket(unittest.TestCase):
    def test_split_draws_a_new_id_and_keeps_origin_and_originaltext(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            verified_queue(base)
            source_dir = base / "PARKED"
            source_dir.mkdir()
            source = source_dir / "T-20260830-517795746.WORKSTATION-LG.txt"
            wortlaut = (
                "ID:            T-20260830-517795746\n"
                "TITEL:         USER-Tickets speisen nicht ins Entscheidungsregister ein\n"
                "STATUS:        PARKED (seit 2026-08-30)\n"
            )
            source.write_text(wortlaut, encoding="utf-8")

            new_path = ticket_writer.split_ticket(
                source, base, today="2026-09-02", include_origin_text=True
            )

            self.assertTrue(Path(new_path).is_file())
            self.assertNotIn("T-20260830-517795746", Path(new_path).name)
            text = Path(new_path).read_text(encoding="utf-8")
            self.assertIn("ORIGIN-TICKET: T-20260830-517795746", text)
            self.assertIn("--- ORIGINALTEXT (unveraendert, massgeblich) ---", text)
            self.assertIn(wortlaut, text)  # bytegleich, keine Umformulierung
            self.assertIn(source.name, text)
            # Die Quelle bleibt unangetastet -- split_ticket() entscheidet nicht,
            # ob/wie sie danach behandelt wird.
            self.assertTrue(source.is_file())
            self.assertEqual(source.read_text(encoding="utf-8"), wortlaut)

    def test_split_never_collides_with_a_second_split_of_the_same_source(self):
        """Der eigentliche Regressionsfall: zwei Aufspaltungen DERSELBEN
        Quelle muessen zwei VERSCHIEDENE IDs bekommen -- der Fehler, den
        dieses Ticket aufdeckte, war genau das Gegenteil (Dateikopie behielt
        die alte ID)."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            verified_queue(base)
            source = base / "PARKED" / "T-20260830-517795746.WORKSTATION-LG.txt"
            source.parent.mkdir()
            source.write_text("ID: T-20260830-517795746\nTITEL: x\n", encoding="utf-8")

            first = ticket_writer.split_ticket(source, base, today="2026-09-02")
            second = ticket_writer.split_ticket(source, base, today="2026-09-02")
            self.assertNotEqual(Path(first).name, Path(second).name)

    def test_title_falls_back_to_first_non_empty_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            verified_queue(base)
            source = base / "SOLVED" / "T-20260830-000000001.txt"
            source.parent.mkdir()
            source.write_text("Erste inhaltliche Zeile als Titel\nRest.", encoding="utf-8")

            new_path = ticket_writer.split_ticket(source, base, today="2026-09-02")
            self.assertIn(
                "Erste inhaltliche Zeile als Titel",
                Path(new_path).read_text(encoding="utf-8"),
            )


class TestSplitStaysUsable(unittest.TestCase):
    """T-20260912-793529183: das Kindticket kam als Elternkopie heraus
    (mehrere STATUS-Zeilen, hunderte Zeilen lang) und ein uebergebener
    --body wurde kommentarlos verworfen. Zweimal am 2026-09-12 beobachtet."""

    def _parent(self, base):
        source = base / "USER" / "T-20260822-230246761.txt"
        source.parent.mkdir(exist_ok=True)
        kopf = [
            "==============================================================",
            "TICKET",
            "==============================================================",
            "ID:            T-20260822-230246761",
            "TITEL:         Grosse Vertragsakte",
            "STATUS:        USER/freigabe (seit 2026-09-12)",
            "PIPELINE:      ai / control / ticket-master",
            "PROJEKTORDNER: C:/_Local_DEV/repos/ticket-master",
        ]
        verlauf = ["STATUS: WAITING/review-due -- Zeile aus dem Verlauf"] * 40
        source.write_text("\n".join(kopf + verlauf) + "\n", encoding="utf-8")
        return source

    def test_body_is_never_silently_dropped(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            verified_queue(base)
            source = self._parent(base)

            new_path = ticket_writer.split_ticket(
                source, base, today="2026-09-12",
                title="Einheit 3 auskoppeln",
                body="BEFUND: Routing v2 hat keinen Snapshot-Erzeuger.",
            )

            text = Path(new_path).read_text(encoding="utf-8")
            self.assertIn("BEFUND: Routing v2 hat keinen Snapshot-Erzeuger.", text)

    def test_child_has_exactly_one_status_line_and_stays_short(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            verified_queue(base)
            source = self._parent(base)
            self.assertGreater(
                len(source.read_text(encoding="utf-8").splitlines()), 40
            )

            new_path = ticket_writer.split_ticket(
                source, base, today="2026-09-12", body="Kurzer Auftrag."
            )

            lines = Path(new_path).read_text(encoding="utf-8").splitlines()
            status_lines = [line for line in lines if line.startswith("STATUS:")]
            self.assertEqual(len(status_lines), 1, status_lines)
            self.assertLess(len(lines), 120, "Kindticket darf keine Elternkopie sein")

    def test_child_references_the_parent_instead_of_copying_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            verified_queue(base)
            source = self._parent(base)

            new_path = ticket_writer.split_ticket(
                source, base, today="2026-09-12", body="Kurzer Auftrag."
            )

            text = Path(new_path).read_text(encoding="utf-8")
            self.assertIn("ORIGIN-TICKET: T-20260822-230246761", text)
            self.assertNotIn("--- ORIGINALTEXT", text)
            self.assertNotIn("Grosse Vertragsakte", text)

    def test_origin_text_stays_available_as_an_explicit_option(self):
        """Die Vollkopie wird nicht entfernt, nur entthront: die Konvention
        'Kopf davor, Wortlaut unveraendert' bleibt auf Wunsch erreichbar."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            verified_queue(base)
            source = self._parent(base)

            new_path = ticket_writer.split_ticket(
                source, base, today="2026-09-12", body="Kurzer Auftrag.",
                include_origin_text=True,
            )

            text = Path(new_path).read_text(encoding="utf-8")
            self.assertIn("--- ORIGINALTEXT (unveraendert, massgeblich) ---", text)
            self.assertIn("Grosse Vertragsakte", text)
            self.assertIn("Kurzer Auftrag.", text)

    def test_pipeline_and_project_are_inherited_from_the_parent(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            verified_queue(base)
            source = self._parent(base)

            new_path = ticket_writer.split_ticket(
                source, base, today="2026-09-12", body="Kurzer Auftrag."
            )

            text = Path(new_path).read_text(encoding="utf-8")
            self.assertIn("ai / control / ticket-master", text)
            self.assertIn("C:/_Local_DEV/repos/ticket-master", text)


if __name__ == "__main__":
    unittest.main()
