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

            new_path = ticket_writer.split_ticket(source, base, today="2026-09-02")

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


if __name__ == "__main__":
    unittest.main()
