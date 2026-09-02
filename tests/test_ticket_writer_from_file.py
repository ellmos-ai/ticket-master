# -*- coding: utf-8 -*-
"""Verifikation von ticket_writer.formalize_informal_entry() / --from-file
(Nutzerentscheid 3A, T-20260830-145228426): eine formlose INBOX-Datei ohne
"T-"-Praefix wird zu einem regulaeren Ticket, der Wortlaut bleibt bytegleich
in einem ORIGINALTEXT-Block erhalten, die Quelle wird archiviert statt
geloescht, und ein bereits formalisierter Eintrag wird nicht doppelt
angelegt."""
import sys
import tempfile
import unittest
from pathlib import Path

LIB_DIR = Path(__file__).resolve().parent.parent / "lib"
sys.path.insert(0, str(LIB_DIR))

import ticket_writer  # noqa: E402
from queue_helpers import verified_queue  # noqa: E402


class TestFormalizeInformalEntry(unittest.TestCase):
    def test_creates_ticket_with_header_and_verbatim_originaltext(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            verified_queue(base)
            (base / "INBOX").mkdir(parents=True)
            wortlaut = "Modelle machen manchmal Fehler beim Anlegen von Tickets.\nZweite Zeile."
            source = base / "INBOX" / "formlos-codex-20260830-1200.txt"
            source.write_text(wortlaut, encoding="utf-8")

            ticket_path = ticket_writer.formalize_informal_entry(
                source, base, submitter="codex", today="2026-08-30")

            text = Path(ticket_path).read_text(encoding="utf-8")
            self.assertIn("Modelle machen manchmal Fehler beim Anlegen von Tickets.", text)
            self.assertIn("--- ORIGINALTEXT (unveraendert, massgeblich) ---", text)
            self.assertIn(wortlaut, text)  # bytegleich, keine Umformulierung
            self.assertIn("codex", text)
            self.assertIn(source.name, text)

    def test_title_is_first_non_empty_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            verified_queue(base)
            (base / "INBOX").mkdir(parents=True)
            source = base / "INBOX" / "formlos-agent-x.txt"
            source.write_text("\n\n  Erste sichtbare Zeile als Titel  \nRest.\n",
                              encoding="utf-8")
            ticket_path = ticket_writer.formalize_informal_entry(source, base, today="2026-08-30")
            text = Path(ticket_path).read_text(encoding="utf-8")
            self.assertIn("Erste sichtbare Zeile als Titel", text)

    def test_source_is_archived_not_deleted(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            verified_queue(base)
            (base / "INBOX").mkdir(parents=True)
            source = base / "INBOX" / "formlos-agent-y.txt"
            source.write_text("Ein formloser Eintrag.", encoding="utf-8")

            ticket_writer.formalize_informal_entry(source, base, today="2026-08-30")

            self.assertFalse(source.exists())
            archived = base / "INBOX" / "_formalisiert" / "formlos-agent-y.txt"
            self.assertTrue(archived.exists())
            self.assertEqual(archived.read_text(encoding="utf-8"), "Ein formloser Eintrag.")

    def test_idempotent_when_a_ticket_already_names_the_source(self):
        """Ein Ticket, das den Dateinamen bereits nennt (schon formalisiert),
        darf kein zweites Mal angelegt werden -- die Quelle wird trotzdem
        archiviert, damit sie nicht liegen bleibt."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            verified_queue(base)
            (base / "INBOX").mkdir(parents=True)
            existing = base / "ACTIONABLE"
            existing.mkdir()
            (existing / "T-20260830-100000001.txt").write_text(
                "ID: T-20260830-100000001\nQuelle: formlos-agent-z.txt schon erledigt\n",
                encoding="utf-8")

            source = base / "INBOX" / "formlos-agent-z.txt"
            source.write_text("Zweiter Versuch, gleicher Wortlaut.", encoding="utf-8")

            result = ticket_writer.formalize_informal_entry(source, base, today="2026-08-30")

            self.assertEqual(Path(result).name, "T-20260830-100000001.txt")
            self.assertEqual(
                len(list((base / "INBOX").glob("T-*.txt"))), 0,
                "kein zweites Ticket darf entstanden sein")
            self.assertFalse(source.exists())
            self.assertTrue((base / "INBOX" / "_formalisiert" / "formlos-agent-z.txt").exists())

    def test_cli_from_file_formalizes_and_moves_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            verified_queue(base)
            (base / "INBOX").mkdir(parents=True)
            source = base / "INBOX" / "formlos-cli-test.txt"
            source.write_text("CLI-Wortlaut, unveraendert.", encoding="utf-8")

            exit_code = ticket_writer._cli([
                "--from-file", str(source), "--tickets-dir", str(base),
                "--submitter", "cli-agent",
            ])
            self.assertEqual(exit_code, 0)
            created = list((base / "INBOX").glob("T-*.txt"))
            self.assertEqual(len(created), 1)
            text = created[0].read_text(encoding="utf-8")
            self.assertIn("CLI-Wortlaut, unveraendert.", text)
            self.assertIn("cli-agent", text)
            self.assertFalse(source.exists())


if __name__ == "__main__":
    unittest.main()
